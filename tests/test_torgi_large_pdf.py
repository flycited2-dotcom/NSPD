import copy
import hashlib
import json
import subprocess

import pytest

from land import store, torgi, torgi_docs as docs, torgi_large_pdf as large
from land import torgi_large_pdf_worker as worker
from test_torgi_pdf_local import lot, pdf


@pytest.fixture
def db(tmp_path_factory, monkeypatch):
    # Windows atomic output needs space for a SHA filename plus temporary suffix.
    path = tmp_path_factory.mktemp('lp')
    monkeypatch.setattr(store, 'DATA', path)
    monkeypatch.setattr(large, 'MIN_BYTES', 1)  # Keep source bytes real and fixture files small.
    store.init()
    return path


def seed(counts=(3,), active=False):
    files, cards, sources = {}, [], {}
    search = {'id': 'search', 'search_source_id': 'source', 'created_at': 'search-date', 'lots': []}
    for index, count in enumerate(counts, 1):
        raw = pdf(count)
        descriptor = docs.descriptor({'fileId': str(index) * 24, 'fileName': 'GPZU.pdf', 'fileSize': len(raw)}, 'lot')
        item = lot(index)
        file = {**descriptor, 'state': 'oversized', 'eligible': True, 'metadata_conflict': False,
                'associations': [{'lot_id': item['id'], 'notice_number': item['notice_number'], 'scope': 'lot', 'inactive': False}]}
        files[file['key']] = file
        sources[file['file_id']] = raw
        cards.append({'lot_id': item['id'], 'notice_number': item['notice_number'], 'state': 'received'})
        search['lots'].append(item)
    catalog = {'id': 'docs', 'search_id': 'search', 'search_source_id': 'source', 'search_created_at': 'search-date',
               'cards': cards, 'files': files, 'geometry_confirmed': False}
    store.set_setting(torgi.setting_key('trudovoe', active), search)
    store.set_setting(docs.setting_key('trudovoe', active), catalog)
    return catalog, search, sources


def mock_download(monkeypatch, sources):
    calls = []
    def fetch(url, max_bytes):
        assert max_bytes == 32 * 1024 * 1024
        assert url.startswith(docs.FILE)
        calls.append(url)
        return sources[url.removeprefix(docs.FILE)], 'application/pdf', 200
    monkeypatch.setattr(large, 'fetch', fetch)
    return calls


def cache(db, file, raw):
    digest = hashlib.sha256(raw).hexdigest()
    folder = db / 'torgi_documents'
    folder.mkdir(exist_ok=True)
    path = folder / (digest + '.pdf')
    path.write_bytes(raw)
    file.update(sha256=digest, received_at='2026-10-10T12:00:00+00:00', actual_size=len(raw),
                http_status=200, content_type='application/pdf')
    return path


def test_real_child_reads_one_downloaded_pdf_and_keeps_other_files_and_standard_limits(db, monkeypatch):
    before, _, sources = seed((3, 4))
    calls = mock_download(monkeypatch, sources)
    outcome = large.run('trudovoe', {'id': 'docs'})
    assert outcome == {'processed': 1, 'errors': 0, 'remaining': 1, 'unread_pages': 0,
                       'network_requests': 1, 'cached_files': 0, 'state': 'done'}
    catalog = store.get_setting(docs.setting_key('trudovoe'))
    changed = [file for key, file in catalog['files'].items() if file != before['files'][key]]
    assert len(changed) == len(calls) == 1
    file = changed[0]
    assert file['state'] == 'read' and worker.scope(file)
    assert file['text_engine'] == 'PDFium'
    assert file['processed_pages'] == file['total_pages'] == 3
    assert not file['geometry_confirmed'] and not file['georeferenced']
    assert not file['text_layer_complete']  # Blank pages need independent OCR.
    assert file['large_pdf_received_at'] == file['received_at']
    assert file['large_pdf_read_at'] != 'search-date'
    assert file['associations'] == before['files'][file['key']]['associations']
    path = db / 'torgi_documents' / (file['sha256'] + '.pdf')
    text = path.with_suffix(worker.TEXT_SUFFIX).read_bytes()
    assert hashlib.sha256(text).hexdigest() == file['continued_text_sha256']
    assert b'UNIT 1\n' in text and b'90:12:172101:1' in text
    assert docs.file_limit('pdf') == 16 * 1024 * 1024 and docs.file_limit('docx') == 8 * 1024 * 1024
    assert store.candidates('trudovoe') == []


def test_cached_receipt_prior_evidence_ocr_and_text_survive_success(db, monkeypatch):
    catalog, _, sources = seed((41,))
    file = next(iter(catalog['files'].values()))
    path = cache(db, file, sources[file['file_id']])
    file.update(mentions=[{'cadastral_number': '90:12:1:1', 'pages': [1]}],
                visual_ocr={'source_sha256': file['sha256'], 'pages': [{'page': 1, 'state': 'received'}]},
                tables=[{'state': 'unreadable'}])
    path.with_suffix('.txt').write_bytes(b'original text')
    path.with_suffix('.continued.txt').write_bytes(b'prior continuation')
    store.set_setting(docs.setting_key('trudovoe'), catalog)
    monkeypatch.setattr(large, 'fetch', lambda *a, **k: pytest.fail('Cache must never fetch'))
    outcome = large.run('trudovoe', {'id': 'docs'})
    current = next(iter(store.get_setting(docs.setting_key('trudovoe'))['files'].values()))
    assert outcome['network_requests'] == 0 and outcome['cached_files'] == 1
    assert current['sha256'] == file['sha256'] and current['received_at'] == file['received_at']
    assert current['visual_ocr'] == file['visual_ocr']
    assert current['large_pdf_previous']['mentions'] == file['mentions']
    assert current['large_pdf_previous']['tables'] == file['tables']
    assert {'cadastral_number': '90:12:172101:41', 'pages': [41]} in current['mentions']
    assert path.with_suffix('.txt').read_bytes() == b'original text'
    assert path.with_suffix('.continued.txt').read_bytes() == b'prior continuation'


def test_120_page_prefix_leaves_unread_pages_and_does_not_repeat(db, monkeypatch):
    catalog, _, sources = seed((121,))
    mock_download(monkeypatch, sources)
    large.run('trudovoe', {'id': 'docs'})
    current = store.get_setting(docs.setting_key('trudovoe'))
    file = next(iter(current['files'].values()))
    assert worker.scope(file) and file['processed_pages'] == 120 and file['unread_pages'] == 1
    assert not file['text_layer_complete'] and 121 not in file['image_or_sparse_pages']
    assert not large.queue(current) and not large.queue(current, True)
    assert large.summary(current)['unread_pages'] == 1


@pytest.mark.parametrize('change', [{'state': 'pending'}, {'state': 'read'}, {'eligible': False},
                                  {'metadata_conflict': True}, {'format': 'docx'}, {'declared_size': True},
                                  {'declared_size': 1}, {'declared_size': worker.MAX_BYTES + 1},
                                  {'file_id': '../outside'}, {'key': '../outside'}])
def test_queue_rejects_other_paths_limits_or_inactive_conflicting_files(db, change):
    catalog, _, _ = seed()
    next(iter(catalog['files'].values())).update(change)
    assert not large.queue(catalog)


def test_production_queue_only_accepts_16_to_32_mib(monkeypatch):
    monkeypatch.setattr(large, 'MIN_BYTES', 16 * 1024 * 1024)
    file = {'format': 'pdf', 'state': 'oversized', 'eligible': True, 'metadata_conflict': False,
            'file_id': 'a' * 24, 'key': 'b' * 24, 'declared_size': 24_186_796}
    assert large.queue({'files': {'b' * 24: file}}) == [file]
    for size in (16 * 1024 * 1024, worker.MAX_BYTES + 1):
        file['declared_size'] = size
        assert not large.queue({'files': {'b' * 24: file}})


def test_worker_error_retains_source_receipt_and_evidence_and_only_explicit_retry_uses_cache(db, monkeypatch):
    catalog, _, sources = seed()
    file = next(iter(catalog['files'].values()))
    file.update(mentions=[{'cadastral_number': '90:12:1:1', 'pages': [1]}], visual_ocr={'pages': []})
    store.set_setting(docs.setting_key('trudovoe'), catalog)
    calls = mock_download(monkeypatch, sources)
    original_read = large.read_saved
    monkeypatch.setattr(large, 'read_saved', lambda _: (_ for _ in ()).throw(ValueError('Parser failed')))
    outcome = large.run('trudovoe', {'id': 'docs'})
    failed = store.get_setting(docs.setting_key('trudovoe'))
    current = next(iter(failed['files'].values()))
    assert outcome['errors'] == 1 and outcome['state'] == 'partial'
    assert current['state'] == 'oversized' and not worker.scope(current)
    assert current['mentions'] == file['mentions'] and current['visual_ocr'] == file['visual_ocr']
    assert current['received_at'] and current['sha256'] and current['large_pdf_attempt']['state'] == 'error'
    assert not large.queue(failed) and len(large.queue(failed, True)) == 1
    monkeypatch.setattr(large, 'read_saved', original_read)
    large.run('trudovoe', {'id': failed['id'], 'retry_errors': True})
    accepted = next(iter(store.get_setting(docs.setting_key('trudovoe'))['files'].values()))
    assert len(calls) == 1 and accepted['received_at'] == current['received_at'] and worker.scope(accepted)


@pytest.mark.parametrize('damage', ['missing', 'changed', 'incomplete_receipt', 'wrong_size', 'wrong_status'])
def test_bad_cached_source_never_refetches_or_discards_old_evidence(db, monkeypatch, damage):
    catalog, _, sources = seed()
    file = next(iter(catalog['files'].values()))
    path = cache(db, file, sources[file['file_id']])
    file['tables'] = [{'original': True}]
    if damage == 'missing':path.unlink()
    elif damage == 'changed':path.write_bytes(b'%PDF-changed')
    elif damage == 'incomplete_receipt':file.pop('received_at')
    elif damage == 'wrong_size':file['actual_size'] += 1
    else:file['http_status'] = 206
    store.set_setting(docs.setting_key('trudovoe'), catalog)
    monkeypatch.setattr(large, 'fetch', lambda *a, **k: pytest.fail('No silent refetch'))
    outcome = large.run('trudovoe', {'id': 'docs'})
    current = next(iter(store.get_setting(docs.setting_key('trudovoe'))['files'].values()))
    assert outcome['errors'] == 1 and outcome['network_requests'] == 0
    assert {key: value for key, value in current.items() if key != 'large_pdf_attempt'} == file


@pytest.mark.parametrize('damage', ['size', 'signature', 'status', 'too_large', 'timeout'])
def test_bounded_bad_download_never_becomes_source_or_automatic_retry(db, monkeypatch, damage):
    catalog, _, sources = seed()
    file = next(iter(catalog['files'].values()))
    raw = sources[file['file_id']]
    def fetch(*args, **kwargs):
        if damage == 'timeout':raise ValueError('HTTP timeout')
        if damage == 'size':return raw + b'changed', 'application/pdf', 200
        if damage == 'signature':return b'X' * len(raw), 'application/pdf', 200
        if damage == 'status':return raw, 'application/pdf', 206
        return b'%PDF-' + b'x' * worker.MAX_BYTES, 'application/pdf', 200
    monkeypatch.setattr(large, 'fetch', fetch)
    outcome = large.run('trudovoe', {'id': 'docs'})
    current = store.get_setting(docs.setting_key('trudovoe'))
    assert outcome['errors'] == outcome['network_requests'] == 1
    assert not large.queue(current) and len(large.queue(current, True)) == 1
    assert 'sha256' not in next(iter(current['files'].values()))
    assert not list((db / 'torgi_documents').glob('*.pdf'))


@pytest.mark.parametrize('change', ['catalog', 'search_source', 'lot_same_id', 'file_receipt_same_id'])
def test_catalog_search_lot_and_same_id_receipt_changes_during_worker_are_not_overwritten(db, monkeypatch, change):
    catalog, search, sources = seed(active=True)
    mock_download(monkeypatch, sources)
    original = large.read_saved
    def read(digest):
        details = original(digest)
        if change in ('catalog', 'file_receipt_same_id'):
            revised = copy.deepcopy(catalog)
            if change == 'catalog':revised['id'] = 'concurrent'
            else:next(iter(revised['files'].values()))['received_at'] = 'concurrent-date'
            store.set_setting(docs.setting_key('trudovoe', True), revised)
        else:
            revised = copy.deepcopy(search)
            if change == 'search_source':revised['search_source_id'] = 'concurrent'
            else:revised['lots'][0]['notice_number'] = 'other-notice'
            store.set_setting(torgi.setting_key('trudovoe', True), revised)
        return details
    monkeypatch.setattr(large, 'read_saved', read)
    with pytest.raises(ValueError, match='изменил'):
        large.run('trudovoe', {'id': 'docs', 'lot_id': lot()['id']}, True)
    current = store.get_setting(docs.setting_key('trudovoe', True))
    assert current['id'] in ('docs', 'concurrent') and 'large_pdf_algorithm' not in next(iter(current['files'].values()))
    assert store.get_setting(docs.setting_key('trudovoe', True, '_large_pdf_attempt'))['state'] == 'error'


def test_selected_lot_explicit_file_key_and_invalid_parameters_fail_before_http(db, monkeypatch):
    catalog, search, sources = seed((3, 4), active=True)
    calls = mock_download(monkeypatch, sources)
    second = list(catalog['files'].values())[1]
    for params in ({'id': 'stale'}, {'id': 'docs', 'retry_errors': 1}, {'id': 'docs', 'file_key': '../bad'},
                   {'id': 'docs', 'lot_id': lot(99)['id']},
                   {'id': 'docs', 'lot_id': lot(1)['id'], 'file_key': second['key']}):
        with pytest.raises(ValueError):large.run('trudovoe', params, True)
    assert not calls
    large.run('trudovoe', {'id': 'docs', 'lot_id': lot(2)['id'], 'file_key': second['key']}, True)
    current = store.get_setting(docs.setting_key('trudovoe', True))
    assert current['files'][second['key']]['state'] == 'read'
    assert list(current['files'].values())[0] == list(catalog['files'].values())[0]
    assert large.summary(current, lot(1)['id'])['remaining'] == 1
    search['search_source_id'] = 'new-source'
    store.set_setting(torgi.setting_key('trudovoe', True), search)
    assert large.report('trudovoe', True)['stale']


def test_reader_timeout_output_sha_page_and_text_guards(db, monkeypatch):
    catalog, _, sources = seed()
    file = next(iter(catalog['files'].values()))
    path = cache(db, file, sources[file['file_id']])
    def timeout(*args, **kwargs):
        assert kwargs['timeout'] == 60
        raise subprocess.TimeoutExpired('worker', 60)
    monkeypatch.setattr(large.subprocess, 'run', timeout)
    with pytest.raises(ValueError, match='60 секунд'):large.read_saved(file['sha256'])
    with pytest.raises(ValueError, match='SHA'):large.read_saved('../bad')
    monkeypatch.setattr(large.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess([], 0, 'x' * (worker.MAX_OUTPUT + 1)))
    with pytest.raises(ValueError, match='4 МБ'):large.read_saved(file['sha256'])
    details = worker.extract(path, file['sha256'])
    def output(result):
        monkeypatch.setattr(large.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(result)))
    for field, value in (('source_sha256', '0' * 64), ('large_pdf_max_pages', True),
                         ('processed_pages', True), ('page_range', [2, 3]), ('page_range', [True, 3]),
                         ('continued_text_sha256', '0' * 64)):
        bad = {**details, field: value}
        output(bad)
        with pytest.raises(ValueError):large.read_saved(file['sha256'])
    output(details)
    path.with_suffix(worker.TEXT_SUFFIX).write_bytes(b'changed text')
    with pytest.raises(ValueError, match='Текст'):large.read_saved(file['sha256'])


def test_source_bound_scope_rejects_wrong_sha_date_type_size_or_algorithm(db):
    catalog, _, sources = seed()
    file = next(iter(catalog['files'].values()))
    path = cache(db, file, sources[file['file_id']])
    details = worker.extract(path, file['sha256'])
    accepted = {**file, **details, 'state': 'read', 'large_pdf_source_sha256': file['sha256'],
                'large_pdf_received_at': file['received_at']}
    assert worker.scope(accepted)
    for field, value in (('large_pdf_source_sha256', '0' * 64), ('received_at', 'new-date'),
                         ('large_pdf_max_bytes', True), ('large_pdf_max_pages', 120.0),
                         ('processed_pages', True), ('actual_size', worker.MAX_BYTES + 1),
                         ('declared_size', True), ('algorithm', 'unknown'), ('state', 'oversized')):
        assert not worker.scope({**accepted, field: value})


def test_worker_rejects_wrong_sha_signature_and_oversize_and_preserves_text_on_failure(db, monkeypatch):
    catalog, _, sources = seed()
    file = next(iter(catalog['files'].values()))
    path = cache(db, file, sources[file['file_id']])
    text = path.with_suffix(worker.TEXT_SUFFIX)
    text.write_bytes(b'previous output')
    with pytest.raises(ValueError, match='SHA'):worker.extract(path, '0' * 64)
    monkeypatch.setattr(worker, 'MAX_BYTES', 1)
    with pytest.raises(ValueError, match='32 МБ'):worker.extract(path, file['sha256'])
    assert text.read_bytes() == b'previous output'
