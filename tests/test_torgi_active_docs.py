"""Regional lot evidence stays separate and never becomes confirmed geometry."""
import copy
import hashlib
import json

import pytest

from land import store, torgi_docs as docs, torgi_visual as visual


def lot(number, district='90:12', kind='ZK'):
    return {'id':f'22000144320000000046_{number}', 'notice_number':'22000144320000000046',
            'url':'https://torgi.gov.ru/new/public/lots/lot/example',
            'type':{'code':kind}, 'cadastral_numbers':[district+':172001:'+str(number)]}


def card(item, **updates):
    return {'id':item['id'], 'noticeNumber':item['notice_number'], 'subjectRFCode':'91',
            'biddType':{'code':'ZK'}, 'lotStatus':'PUBLISHED',
            'noticeAttachments':[], 'lotAttachments':[], 'owner':'private', **updates}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(docs.time, 'sleep', lambda *args:None)
    store.init()
    historical={'id':'historical-docs', 'search_id':'historic-search', 'cards':[],
                'files':{'historic':{'state':'read', 'received_at':'historical-date'}}}
    store.set_setting('torgi_documents_trudovoe', historical)
    store.set_setting('torgi_trudovoe', {'id':'historic-search', 'created_at':'historic-date', 'lots':[]})
    # Deliberately put an unrelated ZK and a debtor first in the search order.
    search={'id':'active-search', 'search_source_id':'active-source', 'created_at':'publication-query-date',
            'lots':[lot(1,'90:11'), lot(2,'90:12','229FZ'), lot(3)]}
    store.set_setting('torgi_active_trudovoe', search)
    store.set_setting('survey_trudovoe', {'layers':{'parcels':{'geojson':{'features':[
        {'type':'Feature', 'geometry':None, 'properties':{'label':'90:12:170102:999'}}]}}}})
    return tmp_path, historical, search


def active_catalog(**updates):
    result={'id':'active-docs', 'search_id':'active-search', 'search_source_id':'active-source',
            'search_created_at':'publication-query-date', 'cards':[], 'files':{}, 'geometry_confirmed':False}
    result.update(updates)
    store.set_setting('torgi_active_documents_trudovoe', result)
    return result


def pending_pdf(raw=b'%PDF-1.4\nexample'):
    file=docs.descriptor({'fileId':'a'*24, 'fileName':'scheme.pdf', 'fileSize':len(raw)}, 'lot')
    file.update(state='pending', eligible=True, metadata_conflict=False,
                associations=[{'lot_id':lot(3)['id'], 'notice_number':lot(3)['notice_number'],
                               'scope':'lot', 'inactive':False}])
    return raw, file


def test_regional_cards_filter_debtors_prioritize_district_and_leave_history_unchanged(db, monkeypatch):
    _, historical, search=db
    calls=[]
    def fetch(url, **kwargs):
        calls.append(url)
        item=next(item for item in search['lots'] if url.endswith(item['id']))
        return json.dumps(card(item)).encode(), 'application/json', 200
    monkeypatch.setattr(docs, 'fetch', fetch)
    outcome=docs.metadata('trudovoe', {'search_id':search['id']}, active=True)
    result=store.get_setting('torgi_active_documents_trudovoe')
    assert outcome['processed']==2 and outcome['remaining']==0
    assert calls==[docs.CARD+lot(3)['id'], docs.CARD+lot(1)['id']]
    assert result['scope']=='regional_active_zk'
    assert result['search_created_at']=='publication-query-date'
    assert 'private' not in json.dumps(result) and not result['geometry_confirmed']
    assert store.get_setting('torgi_documents_trudovoe')==historical
    assert store.get_setting('torgi_documents_attempt_trudovoe') is None


@pytest.mark.parametrize('change', ['source', 'catalog', 'source_with_same_id'])
def test_search_or_catalog_changed_during_card_request_is_not_overwritten(db, monkeypatch, change):
    _, historical, search=db
    old=active_catalog()
    expected=copy.deepcopy(old)
    def fetch(url, **kwargs):
        nonlocal expected
        if change=='catalog':
            expected={**old, 'id':'concurrent-catalog'}
            store.set_setting('torgi_active_documents_trudovoe', expected)
        else:
            revised={**search, 'search_source_id':'different-source'}
            if change=='source':revised['id']='different-search'
            store.set_setting('torgi_active_trudovoe', revised)
        return json.dumps(card(lot(3))).encode(), 'application/json', 200
    monkeypatch.setattr(docs, 'fetch', fetch)
    with pytest.raises(ValueError, match='изменились'):
        docs.metadata('trudovoe', {'search_id':search['id']}, active=True)
    assert store.get_setting('torgi_active_documents_trudovoe')==expected
    assert store.get_setting('torgi_documents_trudovoe')==historical


@pytest.mark.parametrize('updates', [
    {'id':lot(1)['id']}, {'noticeNumber':'different-notice'}, {'subjectRFCode':'77'},
    {'biddType':{'code':'229FZ'}}])
def test_mismatched_card_preserves_regional_catalog(db, monkeypatch, updates):
    old=active_catalog()
    monkeypatch.setattr(docs, 'fetch', lambda *args, **kwargs:
                        (json.dumps(card(lot(3), **updates)).encode(), 'application/json', 200))
    with pytest.raises(ValueError):
        docs.metadata('trudovoe', {'search_id':'active-search'}, active=True)
    assert store.get_setting('torgi_active_documents_trudovoe')==old
    assert store.get_setting('torgi_active_documents_attempt_trudovoe')['state']=='error'


def test_stale_source_parameter_fails_before_request(db, monkeypatch):
    old=active_catalog()
    monkeypatch.setattr(docs, 'fetch', lambda *args, **kwargs:pytest.fail('Unexpected network request'))
    with pytest.raises(ValueError, match='Исходный поиск'):
        docs.metadata('trudovoe', {'search_id':'active-search', 'search_source_id':'old-source'}, active=True)
    assert store.get_setting('torgi_active_documents_trudovoe')==old


def test_regional_card_403_stops_batch_and_needs_explicit_retry_for_error(db, monkeypatch):
    _, historical, search=db
    calls=[]
    def fetch(url, **kwargs):
        calls.append(url)
        raise ValueError('HTTP 403')
    monkeypatch.setattr(docs, 'fetch', fetch)
    outcome=docs.metadata('trudovoe', {'search_id':search['id']}, active=True)
    result=store.get_setting('torgi_active_documents_trudovoe')
    assert outcome['state']=='partial' and calls==[docs.CARD+lot(3)['id']]
    assert result['cards'][0]['state']=='error'
    assert [item['id'] for item in docs.metadata_queue(search, result, active=True)]==[lot(1)['id']]
    queue=docs.metadata_queue(search, result, retry=True, active=True,
                              survey=store.get_setting('survey_trudovoe'))
    assert [item['id'] for item in queue]==[lot(3)['id'], lot(1)['id']]
    assert store.get_setting('torgi_documents_trudovoe')==historical


def test_regional_read_uses_verified_cached_bytes_with_original_date(db, monkeypatch):
    path, historical, _=db
    raw, file=pending_pdf()
    digest=hashlib.sha256(raw).hexdigest()
    file.update(sha256=digest, received_at='file-observation-date', content_type='application/pdf', http_status=200)
    folder=path/'torgi_documents';folder.mkdir();(folder/(digest+'.pdf')).write_bytes(raw)
    old=active_catalog(files={file['key']:file})
    monkeypatch.setattr(docs, 'fetch', lambda *args, **kwargs:pytest.fail('Cached file must not be downloaded'))
    monkeypatch.setattr(docs, 'extract_file', lambda *args:
                        {'algorithm':docs.ALGORITHM, 'sha256':digest, 'tables':[{'state':'review_required'}],
                         'georeferenced':False})
    outcome=docs.read('trudovoe', {'id':old['id']}, active=True)
    result=store.get_setting('torgi_active_documents_trudovoe')
    file=result['files'][file['key']]
    assert outcome['network_requests']==0 and outcome['cached_files']==1
    assert file['received_at']=='file-observation-date'
    assert result['search_created_at']=='publication-query-date'
    assert not file['geometry_confirmed'] and not file['georeferenced']
    assert store.get_setting('torgi_documents_trudovoe')==historical
    assert store.get_setting('torgi_active_files_attempt_trudovoe')['state']=='done'
    assert store.get_setting('torgi_files_attempt_trudovoe') is None


def test_search_changed_during_file_download_preserves_catalog_and_stops_before_cache_write(db, monkeypatch):
    path, _, search=db
    raw, file=pending_pdf();old=active_catalog(files={file['key']:file})
    def fetch(*args, **kwargs):
        store.set_setting('torgi_active_trudovoe', {**search, 'id':'different-search'})
        return raw, 'application/pdf', 200
    monkeypatch.setattr(docs, 'fetch', fetch)
    with pytest.raises(ValueError, match='изменились'):
        docs.read('trudovoe', {'id':old['id']}, active=True)
    assert store.get_setting('torgi_active_documents_trudovoe')==old
    assert list((path/'torgi_documents').iterdir())==[]


def test_file_403_stops_regional_batch_without_touching_history(db, monkeypatch):
    _, historical, _=db
    _, file=pending_pdf();old=active_catalog(files={file['key']:file})
    calls=[]
    def fetch(*args, **kwargs):
        calls.append(args[0]);raise ValueError('HTTP 403')
    monkeypatch.setattr(docs, 'fetch', fetch)
    outcome=docs.read('trudovoe', {'id':old['id']}, active=True)
    current=store.get_setting('torgi_active_documents_trudovoe')
    assert outcome['state']=='partial' and len(calls)==1
    assert not docs.file_queue(current)
    assert len(docs.file_queue(current, retry=True))==1
    assert store.get_setting('torgi_documents_trudovoe')==historical


def test_regional_reprocess_and_ocr_empty_batches_use_separate_attempt_keys(db, monkeypatch):
    _, historical, _=db
    old=active_catalog()
    monkeypatch.setattr(docs, 'fetch', lambda *args, **kwargs:pytest.fail('Local processing must stay offline'))
    monkeypatch.setattr(visual.ocr, 'worker', lambda *args, **kwargs:pytest.fail('No pages to process'))
    assert docs.reprocess('trudovoe', {'id':old['id']}, active=True)['processed']==0
    result=store.get_setting('torgi_active_documents_trudovoe')
    assert visual.run('trudovoe', {'id':result['id']}, active=True)['processed']==0
    assert store.get_setting('torgi_active_reprocess_attempt_trudovoe')['state']=='done'
    assert store.get_setting('torgi_active_visual_attempt_trudovoe')['state']=='done'
    assert store.get_setting('torgi_visual_attempt_trudovoe') is None
    assert store.get_setting('torgi_documents_trudovoe')==historical


def test_regional_preview_cannot_read_historical_file_or_stale_search(db, monkeypatch):
    path, historical, search=db
    raw=b'\x89PNG\r\n\x1a\nsource';png=b'preview-image';digest=hashlib.sha256(raw).hexdigest()
    folder=path/'torgi_documents';folder.mkdir()
    (folder/(digest+'.png')).write_bytes(raw);(folder/(digest+'.preview.png')).write_bytes(png)
    file={'key':'regional-image', 'state':'read', 'format':'png', 'sha256':digest,
          'image':{'preview_sha256':hashlib.sha256(png).hexdigest()}}
    active_catalog(files={'regional-image':file})
    assert visual.preview('trudovoe', 'regional-image', active=True)==png
    with pytest.raises(ValueError):visual.preview('trudovoe', 'historic', active=True)
    with pytest.raises(ValueError):visual.preview('trudovoe', 'regional-image')
    store.set_setting('torgi_active_trudovoe', {**search, 'search_source_id':'new-source'})
    with pytest.raises(ValueError, match='другому поиску'):
        visual.preview('trudovoe', 'regional-image', active=True)
    assert store.get_setting('torgi_documents_trudovoe')==historical


@pytest.mark.parametrize('operation', ['read', 'reprocess', 'ocr'])
def test_concurrent_catalog_during_local_processing_is_preserved(db, monkeypatch, operation):
    path, historical, _=db
    raw, file=pending_pdf();digest=hashlib.sha256(raw).hexdigest()
    file.update(sha256=digest, received_at='original-date', algorithm='previous-parser',
                state='pending' if operation=='read' else 'read', image_or_sparse_pages=[1], processed_pages=1)
    folder=path/'torgi_documents';folder.mkdir();(folder/(digest+'.pdf')).write_bytes(raw)
    old=active_catalog(files={file['key']:file})
    concurrent={**old, 'id':'concurrent-revision'}
    def revise(*args, **kwargs):
        store.set_setting('torgi_active_documents_trudovoe', concurrent)
        return {'algorithm':docs.ALGORITHM, 'sha256':digest, 'page':1, 'state':'received'}
    monkeypatch.setattr(docs, 'fetch', lambda *args, **kwargs:pytest.fail('Unexpected source download'))
    monkeypatch.setattr(docs, 'extract_file', revise)
    monkeypatch.setattr(visual, 'read_unit', revise)
    monkeypatch.setattr(visual.ocr, 'worker', lambda *args, **kwargs:{'available':True})
    action=visual.run if operation=='ocr' else getattr(docs, operation)
    with pytest.raises(ValueError, match='изменились'):
        action('trudovoe', {'id':old['id']}, active=True)
    assert store.get_setting('torgi_active_documents_trudovoe')==concurrent
    assert store.get_setting('torgi_documents_trudovoe')==historical


def test_fresh_regional_cards_reuse_same_file_evidence_without_borrowing_historical_dates(db, monkeypatch):
    _, historical, search=db
    attachment={'fileId':'a'*24, 'fileName':'scheme.pdf', 'fileSize':200, 'hash':'b'*64}
    calls=[]
    def fetch(url, **kwargs):
        calls.append(url)
        item=next(item for item in search['lots'] if url.endswith(item['id']))
        return json.dumps(card(item, lotAttachments=[attachment])).encode(), 'application/json', 200
    monkeypatch.setattr(docs, 'fetch', fetch)
    docs.metadata('trudovoe', {'search_id':search['id']}, active=True)
    old=store.get_setting('torgi_active_documents_trudovoe')
    file=next(iter(old['files'].values()))
    file.update(state='read', sha256='d'*64, received_at='regional-file-date', processed_at='old-reading-date')
    store.set_setting('torgi_active_documents_trudovoe', old)
    revised={**search, 'id':'fresh-search', 'search_source_id':'fresh-source', 'created_at':'fresh-query-date'}
    store.set_setting('torgi_active_trudovoe', revised)
    docs.metadata('trudovoe', {'search_id':revised['id']}, active=True)
    result=store.get_setting('torgi_active_documents_trudovoe')
    assert len(calls)==4 and result['files']==old['files']
    assert result['search_created_at']=='fresh-query-date'
    assert next(iter(result['files'].values()))['received_at']=='regional-file-date'
    assert store.get_setting('torgi_documents_trudovoe')==historical
