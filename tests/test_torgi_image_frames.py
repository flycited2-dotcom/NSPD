import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess

from PIL import Image
import pytest

from land import image_evidence, store, torgi, torgi_docs as docs
from land import torgi_image_frames as local, torgi_image_frame_worker as worker
from test_image_frames import source_bytes


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    monkeypatch.setattr(docs, 'fetch', lambda *args, **kwargs: pytest.fail('MPO frame work must never fetch'))
    return tmp_path


def seed(db, counts=(2, 3), active=False):
    files, cards = {}, []
    search = {'id': 'search', 'search_source_id': 'search-source', 'created_at': 'search-date', 'lots': []}
    folder = db / 'torgi_documents'
    folder.mkdir(exist_ok=True)
    for number, count in enumerate(counts, 1):
        lot = {'id': f'22000144320000000046_{number}', 'notice_number': '22000144320000000046',
               'type': {'code': 'ZK'}}
        search['lots'].append(lot)
        cards.append({'lot_id': lot['id'], 'notice_number': lot['notice_number'], 'state': 'received'})
        raw = source_bytes(count=count)
        digest = hashlib.sha256(raw).hexdigest()
        descriptor = docs.descriptor({'fileId': str(number) * 24, 'fileName': f'photo{number}.jpg',
                                      'fileSize': len(raw)}, 'lot')
        png, image = image_evidence.preview(raw, 'jpg')
        image['preview_sha256'] = hashlib.sha256(png).hexdigest()
        (folder / (digest + '.jpg')).write_bytes(raw)
        (folder / (digest + '.preview.png')).write_bytes(png)
        file = {**descriptor, 'state': 'read', 'eligible': True, 'metadata_conflict': False,
                'sha256': digest, 'received_at': f'original-source-date-{number}', 'actual_size': len(raw),
                'image': image, 'visual_ocr': {'source_sha256': digest, 'pages': [{'page': 1, 'primary': True}]},
                'associations': [{'lot_id': lot['id'], 'scope': 'lot', 'inactive': False}]}
        files[file['key']] = file
    catalog = {'id': 'catalog', 'search_id': 'search', 'search_source_id': 'search-source',
               'cards': cards, 'files': files}
    store.set_setting(torgi.setting_key('trudovoe', active), search)
    store.set_setting(docs.setting_key('trudovoe', active), catalog)
    return catalog, search


@pytest.fixture
def fast_worker(monkeypatch):
    calls = []
    def invoke(args, **kwargs):
        assert args[1:3] == ['-m', 'land.torgi_image_frame_worker']
        assert kwargs['timeout'] == 60 and kwargs['cwd'] == docs.ROOT
        calls.append(args)
        data = worker.extract(Path(args[3]), args[4], args[5], int(args[6]), Path(args[7]), args[8])
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(data))
    monkeypatch.setattr(local.subprocess, 'run', invoke)
    return calls


def params(catalog, number=0, frames=None):
    return {'id': catalog['id'], 'file_key': list(catalog['files'])[number], 'frames': frames or [1]}


def test_real_child_decodes_exact_one_file_preserving_catalog_primary_ocr_and_source_dates(db):
    catalog, search = seed(db, active=True)
    before = {path.name: path.read_bytes() for path in (db / 'torgi_documents').iterdir()}
    outcome = local.run('trudovoe', params(catalog), active=True)
    assert outcome == {'processed': 1, 'received': 1, 'errors': 0, 'remaining': 2,
                       'retry_remaining': 2, 'network_requests': 0, 'state': 'done',
                       'file_key': list(catalog['files'])[0]}
    assert store.get_setting(docs.setting_key('trudovoe', True)) == catalog
    assert store.get_setting(torgi.setting_key('trudovoe', True)) == search
    assert {path.name: path.read_bytes() for path in (db / 'torgi_documents').iterdir()} == before
    assert store.get_setting(local.setting_key('trudovoe')) is None
    report = local.report('trudovoe', True)
    assert report['received'] == 1 and report['declared_auxiliary_frames'] == 3
    assert report['primary_not_modified'] and not report['ocr_read'] and not report['attempt_stale']
    assert report['files'][0]['source_received_at'] == 'original-source-date-1'
    png = local.frame_preview('trudovoe', list(catalog['files'])[0], 1, True)
    with Image.open(io.BytesIO(png)) as image:
        assert image.getpixel((1, 1))[2] > 240 and not image.getexif()
    assert store.candidates('trudovoe') == []
    assert len(store.events('trudovoe')) == 1


def test_completed_frames_are_not_repeated_and_valid_cache_keeps_original_processed_date(db, fast_worker):
    catalog, search = seed(db, counts=(2,))
    file = next(iter(catalog['files'].values()))
    identity = local.binding(catalog, search, file)
    first = local.read_frame(file, 1, identity)
    again = local.read_frame(file, 1, identity)
    assert len(fast_worker) == 1 and not first['cached'] and again['cached']
    assert first['processed_at'] == again['processed_at']
    local.run('trudovoe', params(catalog))
    outcome = local.run('trudovoe', params(catalog))
    assert outcome['processed'] == 0 and outcome['received'] == 0 and len(fast_worker) == 1
    assert local.report('trudovoe')['received'] == 1


@pytest.mark.parametrize('frames', [None, [], (), 1, [0], [-1], [16], [2], [True], [1.0], ['1'],
                                   [1, 1], list(range(1, 17))])
def test_frame_selection_requires_explicit_unique_bounded_integer_indices(db, fast_worker, frames):
    catalog, _ = seed(db, counts=(2,))
    request = params(catalog)
    request['frames'] = frames
    with pytest.raises(ValueError):
        local.run('trudovoe', request)
    assert not fast_worker and store.get_setting(local.setting_key('trudovoe')) is None


@pytest.mark.parametrize('change', [{'id': 'old'}, {'file_key': '../outside'}, {'retry_errors': 'yes'},
                                  {'file_key': True}, {'id': None}])
def test_stale_or_malformed_request_never_calls_worker(db, fast_worker, change):
    catalog, _ = seed(db)
    with pytest.raises(ValueError):
        local.run('trudovoe', params(catalog) | change)
    assert not fast_worker


@pytest.mark.parametrize('change', [{'state': 'rejected'}, {'eligible': False}, {'eligible': 1},
                                  {'metadata_conflict': True}, {'format': 'png'}, {'sha256': None},
                                  {'received_at': None}, {'declared_size': True}, {'declared_size': 8388609},
                                  {'image': {'source_format': 'JPEG'}},
                                  {'image': {'source_format': 'MPO', 'source_frame_count': True}}])
def test_queue_and_run_ignore_unaccepted_or_ambiguous_source(db, fast_worker, change):
    catalog, search = seed(db, counts=(2,))
    next(iter(catalog['files'].values())).update(change)
    store.set_setting(docs.setting_key('trudovoe'), catalog)
    assert local.queue(catalog, search) == []
    with pytest.raises(ValueError):
        local.run('trudovoe', params(catalog))
    assert not fast_worker


def test_partial_failure_never_auto_retries_and_explicit_retry_preserves_error_history(db, monkeypatch, fast_worker):
    catalog, search = seed(db, counts=(3,))
    real = local.read_frame
    def one_bad(file, index, identity, retry=False):
        if index == 1 and not retry:
            raise ValueError('selected frame failed')
        return real(file, index, identity, retry)
    monkeypatch.setattr(local, 'read_frame', one_bad)
    outcome = local.run('trudovoe', params(catalog, frames=[1, 2]))
    state = store.get_setting(local.setting_key('trudovoe'))
    assert outcome['state'] == 'partial' and outcome['received'] == outcome['errors'] == 1
    assert not local.queue(catalog, search, state)
    assert local.queue(catalog, search, state, True)[0]['frames'] == [1]
    assert local.run('trudovoe', params(catalog, frames=[1, 2]))['processed'] == 0
    outcome = local.run('trudovoe', params(catalog, frames=[1, 2]) | {'retry_errors': True})
    assert outcome['received'] == 1 and outcome['errors'] == 0
    report = local.report('trudovoe')
    assert report['received'] == 2 and report['errors'] == report['remaining'] == 0
    observation = report['files'][0]['frames'][0]
    assert observation['previous'][0]['state'] == 'error'


@pytest.mark.parametrize('change', ['catalog_id', 'catalog_same_id', 'search_source', 'search_lot_same_id', 'auxiliary_state'])
def test_changes_during_worker_are_not_overwritten_or_accepted(db, monkeypatch, fast_worker, change):
    catalog, search = seed(db, counts=(2,), active=True)
    real = local.read_frame
    revised_catalog, revised_search = copy.deepcopy(catalog), copy.deepcopy(search)
    def concurrent(file, index, identity, retry=False):
        observed = real(file, index, identity, retry)
        if change == 'catalog_id':
            revised_catalog['id'] = 'new-catalog'
        elif change == 'catalog_same_id':
            revised_catalog['files'][file['key']]['associations'][0]['inactive'] = True
        elif change == 'search_source':
            revised_search['search_source_id'] = 'new-source'
        elif change == 'search_lot_same_id':
            revised_search['lots'][0]['notice_number'] = 'new-notice'
        else:
            store.set_setting(local.setting_key('trudovoe', True), {'concurrent': True})
        store.set_setting(docs.setting_key('trudovoe', True), revised_catalog)
        store.set_setting(torgi.setting_key('trudovoe', True), revised_search)
        return observed
    monkeypatch.setattr(local, 'read_frame', concurrent)
    with pytest.raises(ValueError, match='изменил'):
        local.run('trudovoe', params(catalog), True)
    assert store.get_setting(docs.setting_key('trudovoe', True)) == revised_catalog
    assert store.get_setting(torgi.setting_key('trudovoe', True)) == revised_search
    assert store.get_setting(local.setting_key('trudovoe', True)) == ({'concurrent': True} if change == 'auxiliary_state' else None)
    assert store.get_setting(local.setting_key('trudovoe', True, '_attempt'))['state'] == 'error'


@pytest.mark.parametrize('change', ['source_bytes', 'source_missing', 'source_date', 'primary_count', 'catalog_content', 'search_lot'])
def test_preview_requires_current_exact_source_catalog_search_and_primary_scope(db, fast_worker, change):
    catalog, search = seed(db, counts=(2,))
    file = next(iter(catalog['files'].values()))
    local.run('trudovoe', params(catalog))
    if change in ('source_bytes', 'source_missing'):
        path = db / 'torgi_documents' / (file['sha256'] + '.jpg')
        path.write_bytes(b'changed') if change == 'source_bytes' else path.unlink()
    elif change == 'source_date':
        file['received_at'] = 'different-source-date'
    elif change == 'primary_count':
        file['image'].update(source_frame_count=3, unprocessed_frame_count=2)
    elif change == 'catalog_content':
        catalog['unrelated_current_detail'] = 'new'
    else:
        search['lots'][0]['notice_number'] = 'new-notice'
    store.set_setting(docs.setting_key('trudovoe'), catalog)
    store.set_setting(torgi.setting_key('trudovoe'), search)
    with pytest.raises(ValueError):
        local.frame_preview('trudovoe', file['key'], 1)


@pytest.mark.parametrize('damage', ['png_changed', 'png_missing', 'json_missing', 'source_hash', 'binding_id',
                                  'frame_index', 'frame_index_bool', 'scope_algorithm', 'complete_source',
                                  'png_hash', 'preview_size_bool', 'primary_algorithm', 'source_format'])
def test_cache_tampering_cannot_be_displayed_as_current_auxiliary_frame(db, fast_worker, damage):
    catalog, search = seed(db, counts=(2,))
    file = next(iter(catalog['files'].values()))
    local.run('trudovoe', params(catalog))
    target = local.folder(local.binding(catalog, search, file))
    png_path, json_path = target / 'f1.png', target / 'f1.json'
    if damage == 'png_changed':
        png_path.write_bytes(b'changed')
    elif damage == 'png_missing':
        png_path.unlink()
    elif damage == 'json_missing':
        json_path.unlink()
    else:
        data = json.loads(json_path.read_text('utf-8'))
        if damage == 'source_hash': data['source_sha256'] = '0' * 64
        elif damage == 'binding_id': data['binding_id'] = '0' * 64
        elif damage == 'frame_index': data['frame_scope']['source_frame_index'] = 0
        elif damage == 'frame_index_bool': data['frame_scope']['source_frame_index'] = True
        elif damage == 'scope_algorithm': data['frame_scope']['algorithm'] = image_evidence.ALGORITHM
        elif damage == 'complete_source': data['frame_scope']['complete_source'] = True
        elif damage == 'png_hash': data['png_sha256'] = '0' * 64
        elif damage == 'preview_size_bool': data['preview_width'] = True
        elif damage == 'primary_algorithm': data['primary_preview_algorithm'] = 'old'
        elif damage == 'source_format': data['source_format'] = 'jpeg'
        json_path.write_text(json.dumps(data), 'utf-8')
    with pytest.raises(ValueError):
        local.frame_preview('trudovoe', file['key'], 1)


def test_invalid_unaccepted_cache_requires_explicit_retry_before_regeneration(db, fast_worker):
    catalog, search = seed(db, counts=(2,))
    file = next(iter(catalog['files'].values()))
    identity = local.binding(catalog, search, file)
    local.read_frame(file, 1, identity)
    json_path = local.folder(identity) / 'f1.json'
    data = json.loads(json_path.read_text('utf-8'))
    data['frame_scope']['source_frame_index'] = 0
    json_path.write_text(json.dumps(data), 'utf-8')
    first = local.run('trudovoe', params(catalog))
    assert first['errors'] == 1 and len(fast_worker) == 1
    assert local.run('trudovoe', params(catalog))['processed'] == 0 and len(fast_worker) == 1
    repaired = local.run('trudovoe', params(catalog) | {'retry_errors': True})
    assert repaired['received'] == 1 and repaired['errors'] == 0 and len(fast_worker) == 2
    assert local.frame_preview('trudovoe', file['key'], 1).startswith(b'\x89PNG')


def test_source_changed_after_worker_never_becomes_received_evidence(db, monkeypatch, fast_worker):
    catalog, _ = seed(db, counts=(2,))
    real = local.read_frame
    def changed_after_read(file, index, identity, retry=False):
        observed = real(file, index, identity, retry)
        local.source_path(file).write_bytes(b'changed after decoding')
        return observed
    monkeypatch.setattr(local, 'read_frame', changed_after_read)
    with pytest.raises(ValueError, match='SHA'):
        local.run('trudovoe', params(catalog))
    assert store.get_setting(local.setting_key('trudovoe')) is None
    assert local.report('trudovoe')['received'] == 0


def test_metadata_observation_type_and_source_guards_ignore_forged_scope(db, fast_worker):
    catalog, search = seed(db, counts=(2,))
    file = next(iter(catalog['files'].values()))
    local.run('trudovoe', params(catalog))
    state = store.get_setting(local.setting_key('trudovoe'))
    identity = local.binding(catalog, search, file)
    for changes in ({'source_sha256': '0' * 64}, {'ocr_read': 0}, {'binding_id': '0' * 64},
                    {'frame_scope': local.frame_scope(file, 1) | {'source_frame_index': True}}):
        forged = copy.deepcopy(state)
        forged['entries'][local._hash(identity)]['frames']['1'].update(changes)
        assert local.queue(catalog, search, forged)[0]['frames'] == [1]


def test_worker_timeout_output_cap_and_wrong_result_never_create_received_observation(db, monkeypatch):
    catalog, _ = seed(db, counts=(2,))
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('frame decoder', 60)
    monkeypatch.setattr(local.subprocess, 'run', timeout)
    result = local.run('trudovoe', params(catalog))
    assert result['state'] == 'partial' and result['errors'] == 1 and result['received'] == 0
    for output in ('x' * (worker.MAX_OUTPUT + 1), json.dumps({'source_sha256': 'wrong'}), '[]'):
        monkeypatch.setattr(local.subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess([], 0, stdout=output))
        result = local.run('trudovoe', params(catalog) | {'retry_errors': True})
        assert result['state'] == 'partial' and result['errors'] == 1
    assert local.report('trudovoe')['received'] == 0


def test_new_current_binding_retains_old_auxiliary_observation_without_reusing_it(db, fast_worker):
    catalog, search = seed(db, counts=(2,))
    local.run('trudovoe', params(catalog))
    original = store.get_setting(local.setting_key('trudovoe'))
    catalog['id'] = 'new-catalog'
    store.set_setting(docs.setting_key('trudovoe'), catalog)
    report = local.report('trudovoe')
    assert report['received'] == 0 and report['remaining'] == 1 and report['attempt_stale']
    local.run('trudovoe', params(catalog))
    state = store.get_setting(local.setting_key('trudovoe'))
    assert len(state['entries']) == 2 and len(fast_worker) == 2
    for key, observation in original['entries'].items():
        assert state['entries'][key] == observation


def test_selected_lot_and_historical_regional_catalogs_cannot_cross(db, fast_worker):
    catalog, _ = seed(db, active=True)
    store.set_setting(docs.setting_key('trudovoe'), {'id': 'historical', 'files': {}})
    lot_id = '22000144320000000046_2'
    with pytest.raises(ValueError, match='лоту'):
        local.run('trudovoe', params(catalog) | {'lot_id': lot_id}, True)
    outcome = local.run('trudovoe', params(catalog, number=1, frames=[1, 2]) | {'lot_id': lot_id}, True)
    assert outcome['received'] == 2 and outcome['remaining'] == 0
    assert store.get_setting(local.setting_key('trudovoe')) is None
    assert store.get_setting(docs.setting_key('trudovoe')) == {'id': 'historical', 'files': {}}
    with pytest.raises(ValueError):
        local.frame_preview('trudovoe', list(catalog['files'])[1], 1)


@pytest.mark.parametrize('index', [True, 1.0, '1', 0, 2])
def test_preview_index_is_explicit_and_strictly_typed(db, fast_worker, index):
    catalog, _ = seed(db, counts=(2,))
    local.run('trudovoe', params(catalog))
    with pytest.raises(ValueError):
        local.frame_preview('trudovoe', next(iter(catalog['files'])), index)


def test_child_rejects_wrong_hash_and_selected_source_with_unexpected_count(db):
    catalog, search = seed(db, counts=(2,))
    file = next(iter(catalog['files'].values()))
    path = local.source_path(file)
    with pytest.raises(ValueError, match='SHA'):
        worker.extract(path, '0' * 64, 'jpg', 1, db / 'aux', '0' * 64)
    with pytest.raises(ValueError):
        worker.extract(path, file['sha256'], 'jpg', True, db / 'aux', '0' * 64)
    file['image'].update(source_frame_count=3, unprocessed_frame_count=2)
    store.set_setting(docs.setting_key('trudovoe'), catalog)
    result = local.run('trudovoe', params(catalog))
    assert result['errors'] == 1 and result['received'] == 0
