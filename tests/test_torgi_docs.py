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


def test_conflicting_inactive_unsupported_and_large_files_excluded():
    c1=docs.normalize_card(card(noticeAttachments=[attachment(),attachment(fileId='c'*24,fileName='scan.jpg'),
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
    monkeypatch.setattr(docs.subprocess,'run',lambda *a,**kw:type('R',(),{'stdout':json.dumps({'sha256':'wrong','algorithm':docs.ALGORITHM}),'returncode':0})())
    with pytest.raises(ValueError,match='другому'):docs.extract_file(db/'test.docx','a'*64,'docx')
