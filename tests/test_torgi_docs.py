import copy
import hashlib
import io
import json
import subprocess
import zipfile
import pytest
from land import store,torgi_docs as docs
from land.torgi_file_worker import docx_text,extract


def attachment(**kw):
    return {'fileId':'a'*24,'fileName':'notice.docx','fileSize':250,'hash':'b'*64,'inactive':False,**kw}


def lot(n=1):
    return {'id':f'22000144320000000046_{n}','notice_number':'22000144320000000046','url':'https://torgi.gov.ru/new/public/lots/lot/test'}


def card(n=1,**kw):
    return {'id':lot(n)['id'],'noticeNumber':lot(n)['notice_number'],'subjectRFCode':'91','lotStatus':'FAILED',
            'noticeAttachments':[attachment()], 'lotAttachments':[], 'depositPayAccount':'private','owner':'private',**kw}


def docx(xml=None,extra=None):
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w',compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('word/document.xml',xml or '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>90:12:172001:956 and 90:12:172001:957</w:t></w:r></w:p></w:body></w:document>')
        if extra:z.writestr(*extra)
    return raw.getvalue()


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init();monkeypatch.setattr(docs.time,'sleep',lambda *a:None)
    store.set_setting('torgi_trudovoe',{'id':'search','created_at':'search-date','lots':[lot(1),lot(2)]})
    return tmp_path


def test_card_whitelist_and_notice_scope_no_owner_account():
    result=docs.normalize_card(card(lotAttachments=[attachment(fileId='c'*24,fileName='photo.jpg')]),lot())
    assert [x['scope'] for x in result['attachments']]==['lot','notice']
    assert 'private' not in json.dumps(result) and not result['geometry_confirmed']
    with pytest.raises(ValueError):docs.normalize_card(card(id=lot(2)['id']),lot())
    with pytest.raises(ValueError):docs.normalize_card(card(subjectRFCode='77'),lot())
    with pytest.raises(ValueError):docs.descriptor(attachment(fileId='../../secret'),'notice')
    with pytest.raises(ValueError):docs.descriptor(attachment(fileSize=True),'notice')


def test_shared_files_keep_notice_associations_and_unknown_hash_algorithm():
    c=[docs.normalize_card(card(i),lot(i)) for i in (1,2)]
    files=docs.files_for(c,{})
    assert len(files)==1
    f=next(iter(files.values()))
    assert len(f['associations'])==2 and all(x['scope']=='notice' for x in f['associations'])
    assert f['source_hash_algorithm']=='not_specified' and f['eligible']
    f.update(state='read',sha256='d'*64,received_at='old')
    again=docs.files_for(c,files)
    assert next(iter(again.values()))['received_at']=='old'


def test_scheme_and_notice_read_before_payment_and_contract_templates():
    cards=[docs.normalize_card(card(noticeAttachments=[
        attachment(fileName='Квитанция.docx'),
        attachment(fileId='c'*24,fileName='Схема земельного участка.pdf'),
        attachment(fileId='d'*24,fileName='notice.docx',attachmentTypeCode='Notice_Document')]),lot())]
    queue=docs.file_queue({'files':docs.files_for(cards,{})})
    assert queue[-1]['file_name']=='Квитанция.docx'
    assert {f['file_name'] for f in queue[:2]}=={'Схема земельного участка.pdf','notice.docx'}


def test_explicit_retry_prioritizes_transport_errors_only():
    attachments=[attachment(fileId=letter*24,fileName=name) for letter,name in
                 [('a','Схема.pdf'),('c','Квитанция.docx'),('d','notice.docx'),('e','scan.pdf')]]
    files=docs.files_for([docs.normalize_card(card(noticeAttachments=attachments),lot())],{})
    for file in files.values():
        file['state']={'a':'pending','c':'error','d':'read','e':'rejected'}[file['file_id'][0]]
    assert [f['file_id'][0] for f in docs.file_queue({'files':files})]==['a']
    assert [f['file_id'][0] for f in docs.file_queue({'files':files},True)]==['c','a']


def test_conflicting_inactive_unsupported_and_large_files_excluded():
    c1=docs.normalize_card(card(noticeAttachments=[attachment(),attachment(fileId='c'*24,fileName='scan.gif'),
                                                  attachment(fileId='d'*24,fileSize=9*1024*1024),
                                                  attachment(fileId='e'*24,inactive=True)]),lot())
    c2=docs.normalize_card(card(2,noticeAttachments=[attachment(hash='f'*64)]),lot(2))
    files=docs.files_for([c1,c2],{})
    assert all(x['metadata_conflict'] and not x['eligible'] for x in files.values() if x['file_id']=='a'*24)
    assert {x['state'] for x in files.values()}=={'pending','unsupported','oversized','inactive'}
    assert not docs.file_queue({'files':files})


def test_metadata_continues_without_refreshing_dates_and_ignores_geometry_update(db,monkeypatch):
    monkeypatch.setattr(docs,'BATCH',1)
    calls=[]
    def fetch(url,**kw):
        calls.append(url);n=int(url.rsplit('_',1)[-1]);return json.dumps(card(n)).encode(),'application/json',200
    monkeypatch.setattr(docs,'fetch',fetch)
    assert docs.metadata('trudovoe',{'search_id':'search'})['remaining']==1
    first=copy.deepcopy(store.get_setting('torgi_documents_trudovoe')['cards'][0])
    assert docs.metadata('trudovoe',{'search_id':'search'})['remaining']==0
    r=store.get_setting('torgi_documents_trudovoe')
    assert r['cards'][0]==first and len(r['files'])==1
    search=store.get_setting('torgi_trudovoe');search['id']='geometry';store.set_setting('torgi_trudovoe',search)
    docs.metadata('trudovoe',{'search_id':'geometry'})
    assert len(calls)==2 and store.get_setting('torgi_documents_trudovoe')['search_id']=='geometry'
    with pytest.raises(ValueError):docs.metadata('trudovoe',{'search_id':'old'})


def test_fresh_search_invalidates_cards_even_when_created_at_is_equal(db,monkeypatch):
    calls=[]
    def fetch(url,**kw):
        calls.append(url);return json.dumps(card(int(url.rsplit('_',1)[-1]))).encode(),'application/json',200
    monkeypatch.setattr(docs,'fetch',fetch)
    docs.metadata('trudovoe',{'search_id':'search'})
    old=store.get_setting('torgi_documents_trudovoe')
    search=store.get_setting('torgi_trudovoe');search.update(id='new-search',search_source_id='fresh-source')
    store.set_setting('torgi_trudovoe',search)
    assert not docs.same_search(search,old)
    docs.metadata('trudovoe',{'search_id':'new-search'})
    current=store.get_setting('torgi_documents_trudovoe')
    assert len(calls)==4 and current['search_source_id']=='fresh-source'


@pytest.mark.parametrize('operation',[docs.read,docs.reprocess])
def test_local_search_revision_retains_documents_but_new_source_blocks_processing(db,monkeypatch,operation):
    catalog={'id':'docs','search_id':'search','cards':[],'files':{}}
    store.set_setting('torgi_documents_trudovoe',catalog)
    search=store.get_setting('torgi_trudovoe');search.update(id='local-revision',search_source_id='search')
    store.set_setting('torgi_trudovoe',search)
    monkeypatch.setattr(docs,'fetch',lambda *a,**kw:pytest.fail('unexpected source request'))
    assert operation('trudovoe',{'id':'docs'})['processed']==0
    current=store.get_setting('torgi_documents_trudovoe')
    search.update(id='new-search',search_source_id='new-source');store.set_setting('torgi_trudovoe',search)
    with pytest.raises(ValueError):operation('trudovoe',{'id':current['id']})
    assert store.get_setting('torgi_documents_trudovoe')==current


def test_metadata_transport_stops_and_requires_explicit_retry(db,monkeypatch):
    calls=[]
    def fetch(url,**kw):
        calls.append(url)
        if url.endswith('_2'):raise ValueError('HTTP 403')
        return json.dumps(card()).encode(),'application/json',200
    monkeypatch.setattr(docs,'fetch',fetch)
    assert docs.metadata('trudovoe',{'search_id':'search'})['state']=='partial'
    r=store.get_setting('torgi_documents_trudovoe')
    assert [c['state'] for c in r['cards']]==['received','error']
    assert not docs.metadata_queue(store.get_setting('torgi_trudovoe'),r)
    assert len(docs.metadata_queue(store.get_setting('torgi_trudovoe'),r,True))==1
    assert len(calls)==2


def test_docx_body_units_do_not_claim_pages_lot_membership_or_geometry():
    result,pages=extract(docx(),'docx')
    assert result['unit']=='docx_body' and result['paragraph_count']==1
    assert len(result['mentions'])==2 and all('sections' in x and 'pages' not in x for x in result['mentions'])
    assert not result['geometry_confirmed'] and not result['text_layer_complete'] and result['tables']==[]
    assert 'pages' not in result and len(pages)==1


@pytest.mark.parametrize('raw',[
    b'<html>login</html>',
    docx(extra=('word/vbaProject.bin',b'macro')),
    docx('<!DOCTYPE x [<!ENTITY e "value">]><x>&e;</x>'),
    docx('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY e "value">]><x>&e;</x>'.encode('utf-16')),
])
def test_docx_rejects_nonformat_macros_and_entities(raw):
    with pytest.raises(ValueError):docx_text(raw)


def seed_files(db,monkeypatch):
    raw=docx();a=attachment(fileSize=len(raw))
    monkeypatch.setattr(docs,'fetch',lambda url,**kw:(json.dumps(card(int(url.rsplit('_',1)[-1]),noticeAttachments=[a])).encode(),'application/json',200))
    docs.metadata('trudovoe',{'search_id':'search'})
    return raw,store.get_setting('torgi_documents_trudovoe')


def test_actual_child_reader_and_shared_download_once_despite_hash_difference(db,monkeypatch):
    raw,r=seed_files(db,monkeypatch);calls=[]
    def fetch(url,**kw):calls.append(url);return raw,'application/octet-stream',200
    monkeypatch.setattr(docs,'fetch',fetch)
    assert docs.read('trudovoe',{'id':r['id']})['processed']==1
    after=store.get_setting('torgi_documents_trudovoe');f=next(iter(after['files'].values()))
    assert f['state']=='read' and f['sha256']==hashlib.sha256(raw).hexdigest() and f['sha256']!=f['source_hash']
    assert len(f['mentions'])==2 and len(f['associations'])==2 and not f['geometry_confirmed']
    date=f['received_at'];docs.read('trudovoe',{'id':after['id']})
    assert len(calls)==1 and next(iter(store.get_setting('torgi_documents_trudovoe')['files'].values()))['received_at']==date
    assert store.candidates('trudovoe')==[]


def test_wrong_size_rejected_transport_stops_and_stale_id(db,monkeypatch):
    raw,r=seed_files(db,monkeypatch)
    monkeypatch.setattr(docs,'fetch',lambda *a,**kw:(raw+b'x','application/docx',200))
    docs.read('trudovoe',{'id':r['id']})
    assert next(iter(store.get_setting('torgi_documents_trudovoe')['files'].values()))['state']=='rejected'
    with pytest.raises(ValueError):docs.read('trudovoe',{'id':r['id']})
    r=store.get_setting('torgi_documents_trudovoe');next(iter(r['files'].values()))['state']='pending';store.set_setting('torgi_documents_trudovoe',r)
    monkeypatch.setattr(docs,'fetch',lambda *a,**kw:(_ for _ in ()).throw(ValueError('HTTP 429')))
    assert docs.read('trudovoe',{'id':r['id']})['state']=='partial'
    assert next(iter(store.get_setting('torgi_documents_trudovoe')['files'].values()))['state']=='error'


def test_child_timeout_and_wrong_hash(db,monkeypatch):
    def timeout(*a,**kw):raise subprocess.TimeoutExpired('worker',1)
    monkeypatch.setattr(docs.subprocess,'run',timeout)
    with pytest.raises(ValueError,match='время'):docs.extract_file(db/'test.docx','a'*64,'docx')
    monkeypatch.setattr(docs.subprocess,'run',lambda *a,**kw:type('R',(),{'stdout':json.dumps({'sha256':'wrong','algorithm':docs.DOCX_ALGORITHM}),'returncode':0})())
    with pytest.raises(ValueError,match='другому'):docs.extract_file(db/'test.docx','a'*64,'docx')


def test_local_pdf_reprocessing_keeps_receipt_dates_and_never_downloads(db,monkeypatch):
    folder=db/'torgi_documents';folder.mkdir()
    raw=b'%PDF-local';digest=hashlib.sha256(raw).hexdigest();(folder/(digest+'.pdf')).write_bytes(raw)
    file={'key':'pdf','format':'pdf','state':'read','sha256':digest,'received_at':'source-date','algorithm':'old','tables':[]}
    store.set_setting('torgi_documents_trudovoe',{'id':'docs','search_id':'search','files':{'pdf':file},'cards':[]})
    monkeypatch.setattr(docs,'fetch',lambda *a,**k:pytest.fail('Local reprocessing must not download'))
    monkeypatch.setattr(docs,'extract_file',lambda *a:{'algorithm':docs.ALGORITHM,'sha256':digest,'egrn_tables':[{'state':'review_required'}]})
    assert docs.reprocess('trudovoe',{'id':'docs'})=={'processed':1,'remaining':0,'network_requests':0}
    r=store.get_setting('torgi_documents_trudovoe');f=r['files']['pdf']
    assert f['received_at']=='source-date' and f['sha256']==digest and f['state']=='read' and not f['geometry_confirmed']
    assert f['egrn_tables'] and not docs.reprocess_queue(r)
    with pytest.raises(ValueError):docs.reprocess('trudovoe',{'id':'docs'})


def test_changed_local_pdf_rejected_without_using_stale_contour(db,monkeypatch):
    folder=db/'torgi_documents';folder.mkdir()
    digest='a'*64;(folder/(digest+'.pdf')).write_bytes(b'changed')
    file={'key':'pdf','format':'pdf','state':'read','sha256':digest,'received_at':'source-date','algorithm':'old','egrn_tables':[{'outline_xy':[1]}]}
    store.set_setting('torgi_documents_trudovoe',{'id':'docs','search_id':'search','files':{'pdf':file}})
    monkeypatch.setattr(docs,'extract_file',lambda *a:pytest.fail('Changed source cannot be parsed'))
    docs.reprocess('trudovoe',{'id':'docs'})
    f=store.get_setting('torgi_documents_trudovoe')['files']['pdf']
    assert f['state']=='rejected' and f['egrn_tables']==[] and f['received_at']=='source-date'


def test_saved_large_docx_reprocesses_locally_and_preserves_pdf_and_dates(db,monkeypatch):
    xml='<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p>'
    xml+='<w:r><w:rPr><w:b/><w:color w:val="123456"/></w:rPr></w:r>'*50000
    xml+='<w:r><w:t>90:12:172001:956</w:t></w:r></w:p></w:body></w:document>'
    raw=docx(xml);digest=hashlib.sha256(raw).hexdigest();folder=db/'torgi_documents';folder.mkdir()
    (folder/(digest+'.docx')).write_bytes(raw)
    file={'key':'large','format':'docx','eligible':True,'state':'rejected','error':'Основной XML DOCX превышает лимит',
          'sha256':digest,'received_at':'original-date','associations':[{'lot_id':'same','scope':'notice'}]}
    pdf={'key':'pdf','format':'pdf','state':'read','algorithm':docs.ALGORITHM,'egrn_tables':[{'points':[1,2]}]}
    store.set_setting('torgi_documents_trudovoe',{'id':'docs','search_id':'search','cards':[], 'files':{'large':file,'pdf':pdf}})
    monkeypatch.setattr(docs,'fetch',lambda *a,**k:pytest.fail('Saved DOCX must not be downloaded'))
    assert docs.reprocess('trudovoe',{'id':'docs'})=={'processed':1,'remaining':0,'network_requests':0}
    after=store.get_setting('torgi_documents_trudovoe')
    f=after['files']['large']
    assert f['state']=='read' and f['algorithm']==docs.DOCX_ALGORITHM and 'error' not in f
    assert f['received_at']=='original-date' and f['sha256']==digest and f['associations']==file['associations']
    assert f['mentions'][0]['cadastral_number']=='90:12:172001:956' and f['body_xml_complete']
    assert f['tables']==[] and not f['geometry_confirmed'] and not f['text_layer_complete']
    assert after['files']['pdf']==pdf


def test_docx_reprocess_queue_does_not_retry_other_rejections_or_loop(db,monkeypatch):
    file={'format':'docx','eligible':True,'state':'rejected','error':'Основной XML DOCX превышает лимит','sha256':'a'*64}
    files={'large':file,'bad':dict(file,error='Некорректный XML DOCX'),'inactive':dict(file,eligible=False),
           'no_source':dict(file,sha256=None),'already_attempted':dict(file,reprocess_algorithm=docs.DOCX_ALGORITHM)}
    assert docs.reprocess_queue({'files':files})==[file]
    store.set_setting('torgi_documents_trudovoe',{'id':'docs','search_id':'search','files':files})
    docs.reprocess('trudovoe',{'id':'docs'})
    after=store.get_setting('torgi_documents_trudovoe')
    assert after['files']['large']['state']=='rejected' and not docs.reprocess_queue(after)
