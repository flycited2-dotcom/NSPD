import asyncio,copy,hashlib,io,subprocess
import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
from land import store,municipal,municipal_local as local,ocr,ocr_worker,schemes

def pdf(pages=41,payload=0):
    writer=PdfWriter()
    for n in range(pages):
        p=writer.add_blank_page(300,300)
        if n==40:
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
            p[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
            stream=DecodedStreamObject();stream.set_data(b'BT /F1 10 Tf 10 100 Td (Land 90:12:172101:420) Tj ET')
            p[NameObject('/Contents')]=writer._add_object(stream)
    if payload:
        stream=DecodedStreamObject();stream.set_data(b'x'*payload)
        writer._root_object[NameObject('/UnusedTestPayload')]=writer._add_object(stream)
    out=io.BytesIO();writer.write(out);return out.getvalue()

@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    monkeypatch.setattr(municipal,'fetch',lambda *a,**k:pytest.fail('Local reading must not fetch'))
    return tmp_path

def seed(raw,state='read'):
    digest=hashlib.sha256(raw).hexdigest();folder=store.DATA/'municipal';folder.mkdir(exist_ok=True)
    (folder/(digest+'.pdf')).write_bytes(raw)
    row=municipal.item({'title':'Planning','url':'https://trudovskoe-rk.ru/a.pdf'},municipal.SOURCES[0]['url'],'listing-date')
    row.update(state=state,sha256=digest,received_at='source-date',http_status=200)
    if state=='read':row.update(municipal.pdf_text(raw)[0],ocr={'source_sha256':digest,'pages':[]})
    else:row['error']='Ожидался PDF не более 8 МБ'
    store.set_setting('municipal_trudovoe',{'id':'old','complete':False,'catalog_at':'catalog-date','items':[row]})
    return row

def test_extension_keeps_default_and_stops_at_120():
    raw=pdf(121)
    short,_=municipal.pdf_text(raw)
    full,pages=municipal.pdf_text(raw,max_pages=120)
    assert short['processed_pages']==40 and short['mentions']==[]
    assert full['processed_pages']==120 and full['unread_pages']==1 and not full['text_layer_complete']
    assert full['mentions']==[{'cadastral_number':'90:12:172101:420','pages':[41]}]
    assert pages[-1][0]==120
    with pytest.raises(ValueError):municipal.pdf_text(raw,max_pages=121)

def test_real_child_adds_late_mentions_preserves_source_and_ocr(db):
    original=copy.deepcopy(seed(pdf()))
    result=local.run('trudovoe',{'id':'old'})
    after=store.get_setting('municipal_trudovoe');row=after['items'][0]
    assert result=={'processed':1,'remaining':0,'network_requests':0}
    for key in ('sha256','received_at','http_status','listed_at','ocr'):assert row[key]==original[key]
    assert after['catalog_at']=='catalog-date' and not after['complete']
    assert row['processed_pages']==41 and row['unread_pages']==0
    assert row['mentions']==[{'cadastral_number':'90:12:172101:420','pages':[41]}]
    assert not row['geometry_confirmed'] and store.candidates('trudovoe')==[]
    assert (db/'municipal'/(row['sha256']+'.continued.txt')).is_file()

def test_saved_large_pdf_is_eligible_but_other_rejections_are_not(db):
    raw=pdf(1,9*1024*1024);row=seed(raw,'rejected')
    assert len(local.queue(store.get_setting('municipal_trudovoe')))==1
    result=local.run('trudovoe',{'id':'old'})
    row=store.get_setting('municipal_trudovoe')['items'][0]
    assert result['remaining']==0 and row['state']=='read' and row['processed_pages']==1
    assert row['received_at']=='source-date' and row['local_pdf_algorithm']==local.ALGORITHM
    for change in ({'sha256':None},{'error':'HTTP 403'},{'format':'doc'},{'state':'pending'}):
        altered={**row,'state':'rejected','error':'Ожидался PDF не более 8 МБ','local_pdf_algorithm':None,'local_pdf_attempt_algorithm':None,**change}
        assert local.queue({'items':[altered]})==[]

@pytest.mark.parametrize('damage',['changed','missing','malformed'])
def test_source_or_parser_failure_keeps_prior_evidence_and_is_not_repeated(db,damage):
    original=seed(pdf());path=db/'municipal'/(original['sha256']+'.pdf')
    if damage=='missing':path.unlink()
    elif damage=='changed':path.write_bytes(b'changed')
    else:
        # A saved, hashed malformed source represents an earlier rejected parse.
        original=seed(b'%PDF-1.4 malformed','rejected')
    local.run('trudovoe',{'id':'old'})
    row=store.get_setting('municipal_trudovoe')['items'][0]
    assert row['local_pdf_error'] and not local.queue({'items':[row]})
    for key,value in original.items():assert row[key]==value,key

def test_stale_catalog_and_timeout_are_bounded(db,monkeypatch):
    row=seed(pdf())
    with pytest.raises(ValueError,match='Каталог'):local.run('trudovoe',{'id':'wrong'})
    assert store.get_setting('municipal_trudovoe')['id']=='old'
    def timeout(*a,**k):raise subprocess.TimeoutExpired('child',60)
    monkeypatch.setattr(local.subprocess,'run',timeout)
    with pytest.raises(ValueError,match='время'):local.read_saved(row['sha256'])
    with pytest.raises(ValueError):local.read_saved('../escape')

def test_late_ocr_and_schemes_follow_extended_scope_without_infinite_errors(db,monkeypatch):
    row=seed(pdf(42));local.run('trudovoe',{'id':'old'})
    row=store.get_setting('municipal_trudovoe')['items'][0]
    assert len(ocr.queue({'items':[row]}))==41
    old={**row,'local_pdf_algorithm':None}
    with pytest.raises(ValueError,match='страница'):ocr.queue({'items':[old]})
    prior={'documents':[{'document_id':row['id'],'source_sha256':row['sha256'],'algorithm':schemes.ALGORITHM,'state':'extracted','processed_pages':40}]}
    assert len(schemes.pending({'items':[row]},prior))==1
    store.set_setting('schemes_trudovoe',prior)
    schemes.run('trudovoe',{'catalog_id':store.get_setting('municipal_trudovoe')['id']})
    result=store.get_setting('schemes_trudovoe')
    assert result['documents'][0]['processed_pages']==42 and not schemes.pending({'items':[row]},result)
    result['documents'][0].update(state='error')
    assert not schemes.pending({'items':[row]},result)
    assert len(schemes.pending({'items':[row]},result,True))==1
    calls=[]
    def worker(args,*a):
        calls.append(args);raise ValueError('Stop before OCR')
    monkeypatch.setattr(ocr,'worker',worker)
    with pytest.raises(ValueError):ocr.read_page(row['sha256'],42,16,120)
    assert calls[0][-4:]==['--max-pdf-mib','16','--max-pdf-pages','120']


def test_actual_renderer_keeps_default_40_and_explicitly_allows_page_41(db,monkeypatch):
    row=seed(pdf(41));path=db/'municipal'/(row['sha256']+'.pdf')
    folder=db/'render';folder.mkdir()
    monkeypatch.setattr(ocr_worker,'engine',lambda:(object(),{'max_image_dimension':10000}))
    async def recognize(engine,path):return {'text':'Test'}
    monkeypatch.setattr(ocr_worker,'recognize',recognize)
    with pytest.raises(ValueError,match='1–40'):
        asyncio.run(ocr_worker.process(path,row['sha256'],41,folder))
    result=asyncio.run(ocr_worker.process(path,row['sha256'],41,folder,16,120))
    assert result['page']==41 and result['source_sha256']==row['sha256'] and len(result['views'])==2
    assert (folder/'p41-v0.png').exists() and (folder/'p41-v1.png').exists()
