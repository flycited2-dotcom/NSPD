import json
import pytest
from land import store, torgi_docs as docs, regional_documents as regional


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    monkeypatch.setattr(docs.time, 'sleep', lambda _: None)
    lots=[{'id':f'22000000000000000000_{i}', 'notice_number':'22000000000000000000',
           'url':'https://torgi.gov.ru/new/public/lots/lot/test', 'type':{'code':'ZK'},
           'cadastral_numbers':[]} for i in range(1,14)]
    store.set_setting('torgi_active_trudovoe', {'id':'search', 'created_at':'source-date', 'lots':lots})
    store.set_setting('torgi_documents_trudovoe', {'id':'historical', 'files':{'old':{'state':'read'}}})
    def fetch(url, **kwargs):
        return json.dumps({'id':url.rsplit('/',1)[-1], 'noticeNumber':'22000000000000000000',
                           'subjectRFCode':'91', 'lotAttachments':[], 'noticeAttachments':[]}).encode(), 'application/json', 200
    monkeypatch.setattr(docs,'fetch',fetch)


def test_collect_completes_cards_then_cached_repeat_has_no_writes_or_network(workspace, monkeypatch):
    r=regional.collect('trudovoe',{'search_id':'search'})
    assert r['cards_processed']==13 and r['cards_remaining']==0 and r['files_processed']==0
    saved=store.get_setting('torgi_active_documents_trudovoe')
    monkeypatch.setattr(docs,'fetch',lambda *a,**k:pytest.fail('cached collection must not request cards'))
    again=regional.collect('trudovoe',{'search_id':'search'})
    assert again['cards_processed']==again['files_processed']==0
    assert store.get_setting('torgi_active_documents_trudovoe')==saved
    assert store.get_setting('torgi_documents_trudovoe')['id']=='historical'
    assert not r['geometry_confirmed']


def test_partial_card_access_stops_entire_collect_before_files(workspace,monkeypatch):
    calls=[]
    def blocked(url,**kwargs):
        calls.append(url)
        raise ValueError('HTTP 403: access denied')
    monkeypatch.setattr(docs,'fetch',blocked)
    monkeypatch.setattr(docs,'read',lambda *a,**k:pytest.fail('no file processing after access rejection'))
    r=regional.collect('trudovoe',{'search_id':'search'})
    assert r['state']=='partial' and len(calls)==1 and r['cards_remaining']>0


def test_bounded_card_work_leaves_explicit_queue(workspace,monkeypatch):
    monkeypatch.setattr(regional,'MAX_CARD_BATCHES',1)
    r=regional.collect('trudovoe',{'search_id':'search'})
    assert r['cards_processed']==5 and r['cards_remaining']==8


def test_collect_stops_file_batches_after_partial_and_reports_queue(workspace,monkeypatch):
    saved={'id':'docs','search_id':'search','search_created_at':'source-date',
           'cards':[{'lot_id':l['id'],'state':'received'} for l in store.get_setting('torgi_active_trudovoe')['lots']],
           'files':{}}
    store.set_setting('torgi_active_documents_trudovoe',saved)
    monkeypatch.setattr(docs,'file_queue',lambda *a,**k:[{}])
    calls=[]
    def partial(*a,**k):
        calls.append(True)
        return {'processed':0,'network_requests':1,'state':'partial','remaining':1}
    monkeypatch.setattr(docs,'read',partial)
    r=regional.collect('trudovoe',{'search_id':'search'})
    assert r['state']=='partial' and len(calls)==1 and r['files_remaining']==1


@pytest.mark.parametrize('params',[{'search_id':'old'},{'search_id':'search','retry_errors':'yes'}])
def test_invalid_collection_does_not_mutate_catalog(workspace,params):
    with pytest.raises(ValueError):regional.collect('trudovoe',params)
    assert store.get_setting('torgi_active_documents_trudovoe') is None
