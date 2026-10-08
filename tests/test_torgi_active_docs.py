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


def lot_catalog(search):
    raw=b'%PDF-1.4\nlot evidence'
    def attachment(letter, name, **updates):
        return {'fileId':letter*24, 'fileName':name, 'fileSize':len(raw), **updates}
    chosen=lot(3); other=lot(1,'90:11'); shared=lot(4)
    revised={**search, 'lots':[*search['lots'], shared]}
    store.set_setting('torgi_active_trudovoe', revised)
    notice=attachment('c','shared_scheme.pdf')
    partly_inactive=attachment('e','notice_inactive_for_selected.pdf')
    cards=[docs.normalize_card(card(chosen,
        lotAttachments=[attachment('a','selected.pdf')],
        noticeAttachments=[notice, {**partly_inactive, 'inactive':True}]), chosen),
        docs.normalize_card(card(shared, noticeAttachments=[notice]), shared),
        docs.normalize_card(card(other, lotAttachments=[attachment('b','other.pdf')],
            noticeAttachments=[attachment('d','same_notice_number.pdf'), partly_inactive]), other)]
    result=active_catalog(cards=cards, files=docs.files_for(cards,{}))
    return raw, result, revised


def test_read_one_regional_lot_uses_only_explicit_active_associations(db, monkeypatch):
    _, historical, search=db
    raw, old, _=lot_catalog(search)
    calls=[]
    def fetch(url, **kwargs):
        calls.append(url);return raw, 'application/pdf', 200
    monkeypatch.setattr(docs, 'fetch', fetch)
    monkeypatch.setattr(docs, 'extract_file', lambda path,digest,fmt:
                        {'sha256':digest, 'algorithm':docs.ALGORITHM, 'georeferenced':False})
    outcome=docs.read('trudovoe', {'id':old['id'], 'lot_id':lot(3)['id']}, active=True)
    current=store.get_setting('torgi_active_documents_trudovoe')
    assert outcome['processed']==2 and outcome['remaining']==0 and outcome['lot_id']==lot(3)['id']
    assert set(calls)=={docs.FILE+'a'*24, docs.FILE+'c'*24}
    for key, file in old['files'].items():
        if file['file_id'][0] in ('a','c'):
            assert current['files'][key]['state']=='read' and not current['files'][key]['geometry_confirmed']
        else:
            assert current['files'][key]==file
    notice=next(file for file in current['files'].values() if file['file_id']=='c'*24)
    assert {association['lot_id'] for association in notice['associations']}=={lot(3)['id'],lot(4)['id']}
    assert all(association['scope']=='notice' for association in notice['associations'])
    assert len(docs.file_queue(current))==3  # Other lots retain their own pending work.
    assert current['search_created_at']=='publication-query-date'
    assert store.get_setting('torgi_active_files_attempt_trudovoe')['lot_id']==lot(3)['id']
    assert store.get_setting('torgi_documents_trudovoe')==historical


@pytest.mark.parametrize('operation', [docs.read,docs.reprocess])
@pytest.mark.parametrize('reason', ['unknown','debtor','missing_card','error_card','stale_catalog','bad_id'])
def test_selected_lot_must_belong_to_fresh_zk_search_and_accepted_card(db, monkeypatch, operation, reason):
    _, historical, search=db
    _, old, _=lot_catalog(search)
    requested=lot(3)['id']
    if reason=='unknown':requested=lot(99)['id']
    elif reason=='debtor':
        requested=lot(2)['id']
        old['cards'].append(docs.normalize_card(card(lot(2)), lot(2)))
    elif reason=='missing_card':old['cards']=[card for card in old['cards'] if card['lot_id']!=requested]
    elif reason=='error_card':
        next(card for card in old['cards'] if card['lot_id']==requested)['state']='error'
    elif reason=='stale_catalog':old['search_source_id']='previous-source'
    elif reason=='bad_id':requested='../file'
    store.set_setting('torgi_active_documents_trudovoe',old)
    monkeypatch.setattr(docs, 'fetch', lambda *args,**kwargs:pytest.fail('Invalid selection must fail before network'))
    monkeypatch.setattr(docs, 'extract_file', lambda *args,**kwargs:pytest.fail('Invalid selection must fail before parsing'))
    with pytest.raises(ValueError):operation('trudovoe', {'id':old['id'],'lot_id':requested}, active=True)
    assert store.get_setting('torgi_active_documents_trudovoe')==old
    assert store.get_setting('torgi_active_files_attempt_trudovoe') is None
    assert store.get_setting('torgi_active_reprocess_attempt_trudovoe') is None
    assert store.get_setting('torgi_documents_trudovoe')==historical


def test_selected_lot_network_error_does_not_continue_to_another_attachment(db, monkeypatch):
    _, _, search=db
    _, old, _=lot_catalog(search)
    calls=[]
    def fetch(url,**kwargs):
        calls.append(url);raise ValueError('HTTP 403')
    monkeypatch.setattr(docs, 'fetch', fetch)
    outcome=docs.read('trudovoe', {'id':old['id'],'lot_id':lot(3)['id']}, active=True)
    current=store.get_setting('torgi_active_documents_trudovoe')
    assert outcome['state']=='partial' and len(calls)==1 and outcome['remaining']==1
    assert sum(file['state']=='error' for file in current['files'].values())==1
    assert sum(file['state']=='pending' for file in current['files'].values())==4
    for key, file in old['files'].items():
        if file['file_id'][0] not in ('a','c'):assert current['files'][key]==file


def test_reprocess_one_lot_preserves_other_evidence_and_source_dates(db, monkeypatch):
    path, _, search=db
    raw, old, _=lot_catalog(search)
    digest=hashlib.sha256(raw).hexdigest()
    folder=path/'torgi_documents';folder.mkdir();(folder/(digest+'.pdf')).write_bytes(raw)
    for file in old['files'].values():
        file.update(state='read', algorithm='previous-parser', sha256=digest, received_at='original-file-date')
    store.set_setting('torgi_active_documents_trudovoe',old)
    calls=[]
    def extract(*args):
        calls.append(args);return {'sha256':digest, 'algorithm':docs.ALGORITHM, 'georeferenced':False}
    monkeypatch.setattr(docs, 'extract_file', extract)
    monkeypatch.setattr(docs, 'fetch', lambda *args,**kwargs:pytest.fail('Reprocessing must stay offline'))
    outcome=docs.reprocess('trudovoe', {'id':old['id'],'lot_id':lot(3)['id']}, active=True)
    current=store.get_setting('torgi_active_documents_trudovoe')
    assert outcome['processed']==2 and outcome['remaining']==0 and outcome['network_requests']==0
    assert len(calls)==2 and len(docs.reprocess_queue(current))==3
    for key, file in old['files'].items():
        updated=current['files'][key]
        assert updated['received_at']=='original-file-date'
        if file['file_id'][0] not in ('a','c'):assert updated==file


@pytest.mark.parametrize('operation', [docs.read,docs.reprocess])
def test_historical_catalog_rejects_lot_parameter_and_keeps_existing_general_mode(db, monkeypatch, operation):
    _, historical, _=db
    monkeypatch.setattr(docs, 'fetch', lambda *args,**kwargs:pytest.fail('No source request expected'))
    with pytest.raises(ValueError,match='регионального'):
        operation('trudovoe', {'id':historical['id'],'lot_id':lot(3)['id']})
    assert store.get_setting('torgi_documents_trudovoe')==historical
    # Use an empty valid historical queue to confirm the established mode without lot_id.
    historical={**historical,'files':{}}
    store.set_setting('torgi_documents_trudovoe',historical)
    assert operation('trudovoe', {'id':historical['id']})['processed']==0
