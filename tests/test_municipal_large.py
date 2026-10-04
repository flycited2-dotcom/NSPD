import asyncio
import copy
import hashlib
import io
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from land import store, municipal, municipal_local, municipal_large as large, ocr, ocr_worker, schemes
from land.network import ResponseTooLarge


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(large.time, 'sleep', lambda _: None)
    store.init()
    return tmp_path


def pdf(payload=17*1024*1024, pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(300, 300)
    stream = DecodedStreamObject(); stream.set_data(b'x'*payload)
    writer._root_object[NameObject('/UnusedTestPayload')] = writer._add_object(stream)
    target = io.BytesIO(); writer.write(target)
    return target.getvalue()


def seed(count=1):
    rows = []
    for i in range(count):
        row = municipal.item({'title': 'Planning', 'url': f'https://trudovskoe-rk.ru/a{i}.pdf'},
                             municipal.SOURCES[0]['url'], 'listing-date')
        row.update(state='rejected', error=large.OLD_ERROR, checked_at='old-check',
                   last_error='Ответ превышает 25 МБ', last_error_at='older-check')
        rows.append(row)
    store.set_setting('municipal_trudovoe', {'id':'old', 'items':rows, 'catalog_at':'catalog-date', 'complete':False})
    return rows


def test_real_large_child_receipt_and_profile_defaults(db, monkeypatch):
    original = copy.deepcopy(seed()[0]); raw = pdf()
    calls = []
    def fetch(url, max_bytes):
        calls.append((url, max_bytes)); return raw, 'application/pdf', 200
    monkeypatch.setattr(large, 'fetch', fetch)
    result = large.run('trudovoe', {'id':'old'})
    after = store.get_setting('municipal_trudovoe'); row = after['items'][0]
    assert result == {'processed':1, 'remaining':0, 'failed':0, 'network_requests':1}
    assert calls == [(original['url'], 32*1024*1024)]
    for key in ('title','url','listed_at','parent_url','checked_at'): assert row[key] == original[key]
    assert row['previous_read_error'] == large.OLD_ERROR and row['previous_last_error_at']=='older-check'
    assert row['sha256']==hashlib.sha256(raw).hexdigest() and row['source_bytes']==len(raw)
    assert row['http_status']==200 and row['state']=='read' and not row['geometry_confirmed']
    assert after['catalog_at']=='catalog-date' and after['complete'] is False
    assert municipal.pdf_scope(row)==(32,200) and municipal_local.queue(after)==[]
    assert municipal.pdf_scope({})==(8,40)
    with pytest.raises(ValueError): municipal.pdf_text(raw)
    with pytest.raises(ValueError): municipal.pdf_text(raw,max_bytes=16*1024*1024)
    scheme = schemes.read_document(row['sha256'],large=True)
    assert scheme['total_pages']==1 and scheme['tables']==[]
    with pytest.raises(ValueError): schemes.read_document(row['sha256'],extended=True)
    assert store.candidates('trudovoe')==[]


def test_parse_retry_is_local_and_hash_failure_preserves_receipt(db, monkeypatch):
    seed(); calls=[]
    monkeypatch.setattr(large,'fetch',lambda *a,**k:(calls.append(a) or b'%PDF-1.4 malformed','application/pdf',200))
    assert large.run('trudovoe',{'id':'old'})['failed']==1
    old=store.get_setting('municipal_trudovoe'); row=old['items'][0]
    assert row['sha256'] and not large.queue(old) and len(large.queue(old,True))==1
    original=copy.deepcopy(row)
    (db/'municipal'/(row['sha256']+'.pdf')).write_bytes(b'changed')
    assert large.run('trudovoe',{'id':old['id'],'retry_errors':True})['network_requests']==0
    row=store.get_setting('municipal_trudovoe')['items'][0]
    for key in ('sha256','received_at','source_bytes','http_status'):assert row[key]==original[key]
    assert len(calls)==1 and 'изменился' in row['error']


@pytest.mark.parametrize('failure', [ResponseTooLarge('Ответ превышает 32 МБ'), ValueError('HTTP 403')])
def test_network_failure_does_not_loop_or_invent_receipt(db, monkeypatch, failure):
    rows=seed(2)
    def fail(*a,**k):raise failure
    monkeypatch.setattr(large,'fetch',fail)
    if isinstance(failure, ResponseTooLarge):large.run('trudovoe',{'id':'old'})
    else:
        with pytest.raises(ValueError,match='403'):large.run('trudovoe',{'id':'old'})
    after=store.get_setting('municipal_trudovoe')
    attempted=[r for r in after['items'] if r.get('large_pdf_attempt_algorithm')]
    assert attempted and all(not r.get('sha256') and not r.get('received_at') for r in attempted)
    assert len(large.queue(after,True))==2
    assert len(large.queue(after))==(0 if isinstance(failure,ResponseTooLarge) else 1)


def test_queue_is_specific_and_stale_or_unsafe_input_makes_no_requests(db,monkeypatch):
    row=seed()[0]
    monkeypatch.setattr(large,'fetch',lambda *a,**k:pytest.fail('must not fetch'))
    for change in ({'state':'read'},{'format':'doc'},{'error':'HTTP 403'},{'large_pdf_algorithm':large.ALGORITHM}):
        assert large.queue({'items':[{**row,**change}]})==[]
    with pytest.raises(ValueError,match='Каталог'):large.run('trudovoe',{'id':'stale'})
    with pytest.raises(ValueError,match='логическим'):large.run('trudovoe',{'id':'old','retry_errors':'yes'})
    row['url']='https://example.com/a.pdf'
    store.set_setting('municipal_trudovoe',{'id':'old','items':[row]})
    with pytest.raises(ValueError,match='ссылка'):large.run('trudovoe',{'id':'old'})


def test_actual_large_renderer_and_ocr_scope(db,monkeypatch):
    raw=pdf(pages=141);digest=hashlib.sha256(raw).hexdigest()
    folder=db/'municipal';folder.mkdir();path=folder/(digest+'.pdf');path.write_bytes(raw)
    target=db/'render';target.mkdir()
    monkeypatch.setattr(ocr_worker,'engine',lambda:(object(),{'max_image_dimension':10000}))
    async def recognize(engine,path):return {'text':'Test'}
    monkeypatch.setattr(ocr_worker,'recognize',recognize)
    with pytest.raises(ValueError):asyncio.run(ocr_worker.process(path,digest,141,target,16,120))
    actual=asyncio.run(ocr_worker.process(path,digest,141,target,32,200))
    assert actual['page']==141 and len(actual['views'])==2
    row={**seed()[0], 'state':'read','sha256':digest,'large_pdf_algorithm':large.ALGORITHM,
         'processed_pages':141,'image_or_sparse_pages':[141]}
    assert ocr.queue({'items':[row]})==[(row,141)]
    with pytest.raises(ValueError):ocr.queue({'items':[{**row,'large_pdf_algorithm':None}]})
    with pytest.raises(ValueError):ocr.queue({'items':[{**row,'processed_pages':201,'image_or_sparse_pages':[201]}]})
    calls=[]
    def worker(args,*a):calls.append(args);raise ValueError('stop')
    monkeypatch.setattr(ocr,'worker',worker)
    with pytest.raises(ValueError):ocr.read_page(digest,141,32,200)
    assert calls[0][-4:]==['--max-pdf-mib','32','--max-pdf-pages','200']


def test_large_page_cap_and_saved_profile_upgrade(db,monkeypatch):
    raw=pdf(payload=0,pages=201)
    details,_=municipal.pdf_text(raw,max_bytes=32*1024*1024,max_pages=200)
    assert details['processed_pages']==200 and details['unread_pages']==1
    assert not details['text_layer_complete'] and details['text_engine']=='PDFium'
    with pytest.raises(ValueError):municipal.pdf_text(raw,max_pages=200)
    row=seed()[0];digest=hashlib.sha256(raw).hexdigest()
    folder=db/'municipal';folder.mkdir();(folder/(digest+'.pdf')).write_bytes(raw)
    row.update(state='read',sha256=digest,received_at='source-date',http_status=200,
               previous_read_error=large.OLD_ERROR,large_pdf_algorithm=large.ALGORITHM,
               text_engine='PDFium',processed_pages=120,total_pages=201)
    store.set_setting('municipal_trudovoe',{'id':'old','items':[row]})
    monkeypatch.setattr(large,'fetch',lambda *a,**k:pytest.fail('received source must not be fetched again'))
    assert len(large.queue({'items':[row]}))==1
    result=large.run('trudovoe',{'id':'old'})
    after=store.get_setting('municipal_trudovoe')['items'][0]
    assert result['network_requests']==0 and result['remaining']==0
    assert after['received_at']=='source-date' and after['sha256']==digest and after['unread_pages']==1


def test_failed_upgrade_keeps_previous_read_and_has_no_automatic_loop(db,monkeypatch):
    row=seed()[0]
    row.update(state='read',sha256='a'*64,received_at='source-date',previous_read_error=large.OLD_ERROR,
               large_pdf_algorithm=large.ALGORITHM,processed_pages=120,total_pages=158,text_engine='PDFium')
    original=copy.deepcopy(row)
    store.set_setting('municipal_trudovoe',{'id':'old','items':[row]})
    monkeypatch.setattr(large,'fetch',lambda *a,**k:pytest.fail('upgrade must not download'))
    def fail(*a,**k):raise ValueError('parser timeout')
    monkeypatch.setattr(municipal_local,'read_saved',fail)
    result=large.run('trudovoe',{'id':'old'})
    catalog=store.get_setting('municipal_trudovoe');after=catalog['items'][0]
    assert result['failed']==1 and result['remaining']==0 and result['network_requests']==0
    for key in original:assert after[key]==original[key]
    assert after['large_pdf_error']=='parser timeout' and len(large.queue(catalog,True))==1
