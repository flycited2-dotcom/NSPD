import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile
import pytest
from land import archive_evidence, store, torgi, torgi_docs
from land import torgi_archives as archives
from land import torgi_archive_worker as worker


PROJECT = 'archive-test'
LOT = '22000144320000000046_1'
DATE = '2026-10-11T12:00:00+00:00'
RTF = b'{\\rtf1 public evidence 90:12:040101:23}'


def zip_bytes(entries=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as output:
        for name, raw in entries or [('folder/evidence.rtf', RTF)]:
            output.writestr(name, raw)
    return stream.getvalue()


def seed(raw, count=1):
    lot = {'id': LOT, 'notice_number': LOT.split('_')[0], 'type': {'code': 'ZK'},
           'url': 'https://torgi.gov.ru/new/public/lots/lot/' + LOT}
    search = {'id': 'search-v1', 'search_source_id': 'source-v1', 'created_at': DATE, 'lots': [lot]}
    attachments = [{'fileId': f'{number:024x}', 'fileName': f'public-{number}.zip',
                    'fileSize': len(raw), 'hash': 'b' * 64, 'inactive': False} for number in range(1, count + 1)]
    card = torgi_docs.normalize_card({'id': LOT, 'noticeNumber': lot['notice_number'],
                                    'subjectRFCode': '91', 'lotAttachments': attachments}, lot)
    card.update(source=torgi_docs.CARD + LOT, received_at=DATE, sha256='c' * 64, http_status=200)
    catalog = {'id': 'primary-v1', 'search_id': search['id'], 'search_source_id': search['search_source_id'],
               'cards': [card], 'files': torgi_docs.files_for([card], {})}
    for file in catalog['files'].values():
        file['ocr'] = {'algorithm': 'untouched-primary', 'state': 'done'}
    store.set_setting(torgi.setting_key(PROJECT, True), search)
    store.set_setting(torgi_docs.setting_key(PROJECT, True), catalog)
    return search, catalog


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    monkeypatch.setattr(archives, 'extract_archive',
                        lambda path, digest, folder: worker.process(Path(path).read_bytes(), digest, folder))
    monkeypatch.setattr(archives, 'fetch', lambda *args, **kwargs: pytest.fail('Unexpected network request'))
    return tmp_path


def observe(monkeypatch, raw, **params):
    monkeypatch.setattr(archives, 'fetch', lambda url, max_bytes: (raw, 'application/zip', 200))
    return archives.run(PROJECT, {'id': 'primary-v1', **params})


def record():
    rows = store.get_setting(archives.setting_key(PROJECT))['records']
    return next(iter(rows.values()))


def test_run_keeps_primary_and_ocr_untouched_with_exact_provenance(isolated, monkeypatch):
    raw = zip_bytes()
    search, catalog = seed(raw)
    original = copy.deepcopy(catalog)
    result = observe(monkeypatch, raw)
    assert result['processed'] == result['network_requests'] == 1
    assert result['state'] == 'partial'
    assert result['counts'] == {'read': 1, 'errors': 0, 'unsupported': 0, 'incomplete': 1}
    assert store.get_setting(torgi_docs.setting_key(PROJECT, True)) == original
    assert store.get_setting(torgi.setting_key(PROJECT, True)) == search
    observed = record()
    assert observed['state'] == 'partial' and observed['retryable'] is False
    assert observed['archive_complete'] is observed['geometry_confirmed'] is observed['georeferenced'] is False
    assert observed['counts'] == dict(entries=1, supported=1, read=1, errors=0, unsupported=0, directories=0, incomplete=1)
    entry = observed['entries'][0]
    digest = hashlib.sha256(RTF).hexdigest()
    assert entry['sha256'] == digest
    assert entry['association_reference'] == {
        'binding_id': observed['binding_id'], 'observation_id': observed['observation_id'],
        'parent_file_key': next(iter(catalog['files'])), 'archive_sha256': hashlib.sha256(raw).hexdigest(),
        'source_receipt_sha256': archives._hash(observed['source_receipt']),
        'entry_id': entry['id'], 'member_sha256': digest}
    binding = observed['source_binding']
    assert binding['search_source_id'] == 'source-v1'
    assert binding['card_receipts'][0]['sha256'] == 'c' * 64
    assert binding['associations'] == [{'lot_id': LOT, 'notice_number': LOT.split('_')[0], 'scope': 'lot', 'inactive': False}]
    assert observed['source_receipt']['sha256'] != binding['parent_file']['source_hash']
    assert not (isolated / 'torgi_documents').exists()
    assert not (isolated / 'torgi_archives' / 'members' / 'folder').exists()
    assert (isolated / 'torgi_archives' / 'members' / (digest + '.rtf')).read_bytes() == RTF
    assert archives.summary(PROJECT)['pending'] == 0


def test_one_zip_per_explicit_run(isolated, monkeypatch):
    raw = zip_bytes()
    seed(raw, count=2)
    calls = []
    monkeypatch.setattr(archives, 'fetch', lambda url, max_bytes: (calls.append((url, max_bytes)) or raw, 'application/zip', 200))
    first = archives.run(PROJECT, {'id': 'primary-v1'})
    assert len(calls) == 1 and first['remaining'] == 1
    assert calls[0][1] == 32 * 1024 * 1024
    second = archives.run(PROJECT, {'id': 'primary-v1'})
    assert len(calls) == 2 and second['remaining'] == 0
    assert len(store.get_setting(archives.setting_key(PROJECT))['records']) == 2


def test_selected_file_and_lot_cannot_borrow_another_association(isolated, monkeypatch):
    raw = zip_bytes()
    _, catalog = seed(raw, count=2)
    target = list(catalog['files'])[1]
    result = observe(monkeypatch, raw, file_key=target, lot_id=LOT)
    assert result['processed'] == 1 and record()['file_key'] == target
    assert archives.summary(PROJECT)['pending'] == 1
    with pytest.raises(ValueError, match='Лот ЗК'):
        archives.run(PROJECT, {'id': 'primary-v1', 'lot_id': LOT[:-1] + '2'})


@pytest.mark.parametrize('params', [{'id': 'stale'}, {'id': 'primary-v1', 'retry_errors': 1},
                                    {'id': 'primary-v1', 'file_key': '../wrong'},
                                    {'id': 'primary-v1', 'file_key': 'f' * 24}])
def test_bad_parameters_do_not_request(isolated, monkeypatch, params):
    seed(zip_bytes())
    with pytest.raises(ValueError):
        archives.run(PROJECT, params)
    assert store.get_setting(archives.setting_key(PROJECT)) is None


@pytest.mark.parametrize('change', ['missing_receipt', 'foreign_receipt', 'missing_association',
                                   'foreign_notice', 'inactive', 'metadata_conflict', 'oversized'])
def test_source_guards_fail_closed(isolated, change):
    _, catalog = seed(zip_bytes())
    file = next(iter(catalog['files'].values()))
    card = catalog['cards'][0]
    if change == 'missing_receipt': card.pop('received_at')
    if change == 'foreign_receipt': card['source'] = torgi_docs.CARD + 'foreign'
    if change == 'missing_association': file['associations'] = []
    if change == 'foreign_notice': card['notice_number'] = '99999999999'
    if change == 'inactive':
        card['attachments'][0]['inactive'] = True
        catalog['files'] = torgi_docs.files_for([card], {})
    if change == 'metadata_conflict':
        altered = copy.deepcopy(card['attachments'][0]); altered['key'] = 'f' * 24; altered['declared_size'] += 1
        card['attachments'].append(altered)
        catalog['files'] = torgi_docs.files_for([card], {})
    if change == 'oversized':
        card['attachments'][0]['declared_size'] = 32 * 1024 * 1024 + 1
        file['declared_size'] = card['attachments'][0]['declared_size']
    store.set_setting(torgi_docs.setting_key(PROJECT, True), catalog)
    result = archives.run(PROJECT, {'id': 'primary-v1'})
    assert result['processed'] == result['network_requests'] == 0
    assert archives.summary(PROJECT)['blocked_count'] >= 1


def test_access_error_stops_and_needs_explicit_retry(isolated, monkeypatch):
    raw = zip_bytes()
    seed(raw, count=2)
    calls = []
    def denied(url, max_bytes):
        calls.append(url)
        raise ValueError('HTTP 403: access limited')
    monkeypatch.setattr(archives, 'fetch', denied)
    assert archives.run(PROJECT, {'id': 'primary-v1'})['state'] == 'partial'
    assert len(calls) == 1
    first = record()
    assert first['state'] == 'error' and first['retryable']
    # Targeting the same failed source does not turn a normal action into a retry.
    assert archives.run(PROJECT, {'id': 'primary-v1', 'file_key': first['file_key']})['network_requests'] == 0
    assert len(calls) == 1
    result = observe(monkeypatch, raw, file_key=first['file_key'], retry_errors=True)
    assert result['network_requests'] == 1 and record()['state'] == 'partial'


@pytest.mark.parametrize('mutation', ['search', 'same_id_lot', 'same_id_type', 'same_id_notice',
                                     'catalog', 'receipt', 'associations', 'archive_collection'])
def test_changes_during_download_preserve_previous_observation(isolated, monkeypatch, mutation):
    raw = zip_bytes()
    search, catalog = seed(raw)
    previous = {'id': 'old-archive', 'records': {}}
    store.set_setting(archives.setting_key(PROJECT), previous)
    def changed(url, max_bytes):
        if mutation == 'search':
            search['id'] = 'new-search'; store.set_setting(torgi.setting_key(PROJECT, True), search)
        elif mutation.startswith('same_id_'):
            if mutation == 'same_id_lot': search['lots'] = []
            elif mutation == 'same_id_type': search['lots'][0]['type']['code'] = '229FZ'
            else: search['lots'][0]['notice_number'] = '99999999999999999999'
            store.set_setting(torgi.setting_key(PROJECT, True), search)
        elif mutation == 'catalog':
            catalog['id'] = 'new-primary'; store.set_setting(torgi_docs.setting_key(PROJECT, True), catalog)
        elif mutation in ('receipt', 'associations'):
            if mutation == 'receipt': catalog['cards'][0]['sha256'] = 'd' * 64
            else: next(iter(catalog['files'].values()))['associations'] = []
            store.set_setting(torgi_docs.setting_key(PROJECT, True), catalog)
        else:
            store.set_setting(archives.setting_key(PROJECT), {'id': 'foreign-archive', 'records': {}})
        return raw, 'application/zip', 200
    monkeypatch.setattr(archives, 'fetch', changed)
    with pytest.raises(ValueError, match='изменил|отсутствуют'):
        archives.run(PROJECT, {'id': 'primary-v1'})
    assert not store.get_setting(archives.setting_key(PROJECT))['records']
    assert not list((isolated / 'torgi_archives').glob('*.zip'))


def test_changed_receipt_retains_old_evidence_outside_current_queue(isolated, monkeypatch):
    raw = zip_bytes()
    _, catalog = seed(raw)
    observe(monkeypatch, raw)
    first = record()['binding_id']
    catalog['cards'][0]['sha256'] = 'd' * 64
    catalog['id'] = 'primary-v2'
    store.set_setting(torgi_docs.setting_key(PROJECT, True), catalog)
    result = archives.summary(PROJECT)
    assert result['retained_count'] == result['pending'] == 1
    assert result['archives'][0]['binding_id'] != first
    assert 'observation' not in result['archives'][0]


def test_geometry_rematch_with_same_source_can_reuse_observations(isolated, monkeypatch):
    raw = zip_bytes()
    search, _ = seed(raw)
    observe(monkeypatch, raw)
    search['id'] = 'geometry-rematch'
    store.set_setting(torgi.setting_key(PROJECT, True), search)
    assert archives.summary(PROJECT)['pending'] == 0
    assert archives.summary(PROJECT)['retained_count'] == 0


@pytest.mark.parametrize('previous_state', ['read', 'partial', 'unsupported'])
def test_explicit_reprocess_cached_preserves_receipt_without_network(isolated, monkeypatch, previous_state):
    raw = zip_bytes()
    _, catalog = seed(raw, count=2)
    observe(monkeypatch, raw)
    before = copy.deepcopy(record())
    collection = store.get_setting(archives.setting_key(PROJECT))
    old = collection['records'][before['binding_id']]
    old['state'] = previous_state
    old.pop('evidence_sha256')
    collection.pop('observations')
    store.set_setting(archives.setting_key(PROJECT), collection)
    monkeypatch.setattr(archives, 'fetch', lambda *a, **k: pytest.fail('Reprocessing cache must never use HTTP'))
    result = archives.run(PROJECT, {'id': 'primary-v1', 'file_key': before['file_key'], 'reprocess_cached': True})
    assert result['processed'] == result['cached_files'] == 1
    assert result['network_requests'] == 0 and result['remaining'] == 1
    assert result['reprocess_cached'] is True and result['state'] == 'partial'
    after = record()
    assert after['source_receipt'] == before['source_receipt']
    assert after['archive_sha256'] == before['archive_sha256']
    assert after['observation_id'] == before['observation_id']
    assert after['counts'] == before['counts']
    assert after['evidence_sha256']
    assert archives.cached_observation(PROJECT, after['evidence_sha256'])['source_receipt'] == before['source_receipt']
    assert store.get_setting(torgi_docs.setting_key(PROJECT, True)) == catalog


@pytest.mark.parametrize('params', [{'reprocess_cached': True}, {'reprocess_cached': 1},
                                    {'reprocess_cached': 'true'}, {'reprocess_cached': None}])
def test_reprocess_requires_boolean_and_explicit_key(isolated, params):
    seed(zip_bytes())
    with pytest.raises(ValueError, match='логическим|file_key'):
        archives.run(PROJECT, {'id': 'primary-v1', **params})
    assert store.get_setting(archives.setting_key(PROJECT)) is None


@pytest.mark.parametrize('change', ['missing_receipt', 'changed_date', 'changed_sha', 'changed_source',
                                   'changed_archive_sha', 'changed_binding', 'missing_cache', 'corrupt_cache',
                                   'new_card_receipt'])
def test_reprocess_refuses_unproven_or_foreign_cache_without_http(isolated, monkeypatch, change):
    raw = zip_bytes()
    _, catalog = seed(raw)
    observe(monkeypatch, raw)
    collection = store.get_setting(archives.setting_key(PROJECT))
    old = next(iter(collection['records'].values()))
    key = old['file_key']
    receipt = old['source_receipt']
    if change == 'missing_receipt': old.pop('source_receipt')
    elif change == 'changed_date': receipt['received_at'] = 'not a dated receipt'
    elif change == 'changed_sha': receipt['sha256'] = '0' * 64
    elif change == 'changed_source': receipt['source'] = torgi_docs.FILE + 'f' * 24
    elif change == 'changed_archive_sha': old['archive_sha256'] = '0' * 64
    elif change == 'changed_binding': old['source_binding']['search_source_id'] = 'foreign-source'
    elif change == 'missing_cache': (isolated / 'torgi_archives' / (receipt['sha256'] + '.zip')).unlink()
    elif change == 'corrupt_cache': (isolated / 'torgi_archives' / (receipt['sha256'] + '.zip')).write_bytes(b'changed')
    else:
        catalog['cards'][0]['sha256'] = 'd' * 64
        store.set_setting(torgi_docs.setting_key(PROJECT, True), catalog)
    store.set_setting(archives.setting_key(PROJECT), collection)
    unchanged = copy.deepcopy(collection)
    monkeypatch.setattr(archives, 'fetch', lambda *a, **k: pytest.fail('No fallback HTTP permitted'))
    with pytest.raises(ValueError):
        archives.run(PROJECT, {'id': 'primary-v1', 'file_key': key, 'reprocess_cached': True})
    assert store.get_setting(archives.setting_key(PROJECT)) == unchanged


def test_reprocess_retains_prior_immutable_observation(isolated, monkeypatch):
    raw = zip_bytes()
    seed(raw)
    observe(monkeypatch, raw)
    old = copy.deepcopy(record())
    monkeypatch.setattr(archives, 'fetch', lambda *a, **k: pytest.fail('No HTTP permitted'))
    archives.run(PROJECT, {'id': 'primary-v1', 'file_key': old['file_key'], 'reprocess_cached': True})
    assert record()['previous_evidence_sha256'] == old['evidence_sha256']
    previous = archives.cached_observation(PROJECT, old['evidence_sha256'])
    assert previous['observation_id'] == old['observation_id'] and previous['counts'] == old['counts']


def test_per_entry_error_remains_partial_and_explicit_retry_uses_cache(isolated, monkeypatch):
    raw = zip_bytes([('ok.rtf', RTF), ('bad.pdf', b'%PDF-not-readable'), ('nested.zip', b'not-read')])
    seed(raw)
    result = observe(monkeypatch, raw)
    assert result['network_requests'] == 1
    original = record()
    assert original['counts']['read'] == original['counts']['errors'] == original['counts']['unsupported'] == 1
    assert original['retryable'] is True
    assert 'sha256' not in original['entries'][2]
    assert archives.run(PROJECT, {'id': 'primary-v1'})['processed'] == 0
    monkeypatch.setattr(archives, 'fetch', lambda *a, **k: pytest.fail('Cached retry must not download'))
    result = archives.run(PROJECT, {'id': 'primary-v1', 'retry_errors': True})
    assert result['network_requests'] == 0 and result['cached_files'] == 1
    assert record()['source_receipt'] == original['source_receipt']
    assert record()['observation_id'] == original['observation_id']


@pytest.mark.parametrize('kind', ['bad_path', 'wrong_size', 'bad_zip'])
def test_unsafe_source_rejected_without_member_reads(isolated, monkeypatch, kind):
    raw = zip_bytes([('../escape.rtf', RTF)]) if kind == 'bad_path' else b'not a zip' if kind == 'bad_zip' else zip_bytes()
    seed(raw if kind != 'wrong_size' else raw + b'0')
    observe(monkeypatch, raw)
    assert record()['state'] == 'rejected' and record()['retryable'] is False
    assert not (isolated / 'torgi_archives' / 'members').exists()
    assert archives.run(PROJECT, {'id': 'primary-v1', 'retry_errors': True})['processed'] == 0


def test_corrupt_cached_source_is_never_redownloaded_implicitly(isolated, monkeypatch):
    raw = zip_bytes([('bad.pdf', b'%PDF-invalid')])
    seed(raw)
    observe(monkeypatch, raw)
    previous = copy.deepcopy(record())
    source = isolated / 'torgi_archives' / (hashlib.sha256(raw).hexdigest() + '.zip')
    source.write_bytes(b'changed')
    monkeypatch.setattr(archives, 'fetch', lambda *a, **k: pytest.fail('Corrupt cache must not trigger fetch'))
    archives.run(PROJECT, {'id': 'primary-v1', 'retry_errors': True})
    assert record()['state'] == 'rejected'
    assert record()['previous_evidence_sha256'] == previous['evidence_sha256']
    historical = archives.cached_observation(PROJECT, previous['evidence_sha256'])
    assert historical['historical'] is True and historical['counts'] == previous['counts']
    assert historical['archive_sha256'] == hashlib.sha256(raw).hexdigest()


def test_immutable_snapshot_cannot_be_rebound_or_changed(isolated, monkeypatch):
    raw = zip_bytes()
    seed(raw)
    observe(monkeypatch, raw)
    digest = record()['evidence_sha256']
    with pytest.raises(ValueError, match='отсутствует'):
        archives.cached_observation('different-project', digest)
    with pytest.raises(ValueError, match='Некорректный'):
        archives.cached_observation(PROJECT, '../escape')
    path = isolated / 'torgi_archives' / 'observations' / (digest + '.json')
    path.write_bytes(b'{}')
    with pytest.raises(ValueError, match='изменился'):
        archives.cached_observation(PROJECT, digest)


@pytest.mark.parametrize('field', ['archive_sha256', 'manifest', 'counts', 'geometry_confirmed', 'archive_complete',
                                  'entry_id', 'member_sha256', 'details_geometry', 'source_binding_injection'])
def test_foreign_or_false_complete_worker_result_rejected(isolated, monkeypatch, field):
    raw = zip_bytes()
    seed(raw)
    def wrong(path, digest, folder):
        result = worker.process(Path(path).read_bytes(), digest, folder)
        if field == 'archive_sha256': result['archive_sha256'] = '0' * 64
        elif field == 'manifest': result['manifest']['entries'][0]['name'] = 'foreign.rtf'
        elif field == 'counts': result['counts']['errors'] += 1
        elif field in ('geometry_confirmed', 'archive_complete'): result[field] = True
        elif field == 'entry_id': result['entries'][0]['id'] = '0' * 64
        elif field == 'member_sha256': result['entries'][0]['sha256'] = '0' * 64
        elif field == 'source_binding_injection': result['source_binding'] = {'binding_id': 'foreign'}
        else: result['entries'][0]['evidence']['details']['geometry_confirmed'] = True
        return result
    monkeypatch.setattr(archives, 'extract_archive', wrong)
    observe(monkeypatch, raw)
    assert record()['state'] == 'error' and 'entries' not in record()


def test_worker_has_default_pdf_limits_no_ocr_and_flat_cache(tmp_path, monkeypatch):
    raw = zip_bytes([('nested/plan.pdf', b'%PDF-synthetic'), ('nested/child.zip', b'no-read'), ('app.exe', b'no-exec')])
    calls = []
    def bounded(member, fmt, max_pages=None):
        calls.append((len(member), fmt, max_pages))
        return {'text_layer_complete': False, 'processed_pages': 40, 'unread_pages': 3}, [(1, '90:12:1:1')]
    monkeypatch.setattr(worker.torgi_file_worker, 'extract', bounded)
    output = worker.process(raw, hashlib.sha256(raw).hexdigest(), tmp_path / 'members')
    assert calls == [(len(b'%PDF-synthetic'), 'pdf', None)]
    assert output['pdf_max_pages'] == 40 and output['member_max_bytes'] == 16 * 1024 * 1024
    assert output['counts']['unsupported'] == 2 and output['counts']['incomplete'] == 1
    assert output['ocr_performed'] is output['archive_complete'] is False
    assert all(path.parent == tmp_path / 'members' and len(path.stem) == 64
               for path in (tmp_path / 'members').iterdir())


def test_source_changed_after_worker_cannot_persist_read(isolated, monkeypatch):
    raw = zip_bytes()
    seed(raw)
    def changed(path, digest, folder):
        result = worker.process(Path(path).read_bytes(), digest, folder)
        Path(path).write_bytes(b'changed after worker')
        return result
    monkeypatch.setattr(archives, 'extract_archive', changed)
    result = observe(monkeypatch, raw)
    assert result['state'] == 'partial' and result['counts']['errors'] == 1
    assert record()['state'] == 'error' and 'entries' not in record()
    assert not (isolated / 'torgi_archives' / 'observations').exists()


def test_source_checked_under_persist_lock(isolated, monkeypatch):
    raw = zip_bytes()
    seed(raw)
    original = archives._persist
    def changed(project, collection, search, catalog_id, binding, previous_id, active):
        digest = hashlib.sha256(raw).hexdigest()
        (isolated / 'torgi_archives' / (digest + '.zip')).write_bytes(b'changed before transaction')
        return original(project, collection, search, catalog_id, binding, previous_id, active)
    monkeypatch.setattr(archives, '_persist', changed)
    with pytest.raises(ValueError, match='изменился после чтения'):
        observe(monkeypatch, raw)
    assert store.get_setting(archives.setting_key(PROJECT)) is None


def test_worker_large_evidence_becomes_explicit_member_error(tmp_path, monkeypatch):
    raw = zip_bytes()
    monkeypatch.setattr(worker.torgi_file_worker, 'extract',
                        lambda *a, **k: ({'unbounded': 'x' * (4 * 1024 * 1024)}, []))
    result = worker.process(raw, hashlib.sha256(raw).hexdigest(), tmp_path)
    assert result['counts']['errors'] == 1 and result['state'] == 'partial'
    assert len(json.dumps(result).encode()) < worker.MAX_OUTPUT
    assert result['entries'][0]['crc_verified'] is True


def test_worker_unsupported_only_never_claims_full_or_invents_sha(tmp_path):
    raw = zip_bytes([('nested.zip', b'not-read'), ('app.exe', b'not-executed')])
    output = worker.process(raw, hashlib.sha256(raw).hexdigest(), tmp_path)
    assert output['state'] == 'partial' and output['all_supported_members_read'] is False
    assert output['archive_complete'] is False and output['counts']['read'] == 0
    assert all('sha256' not in entry for entry in output['entries'])
    assert not list(tmp_path.iterdir())


def test_extract_archive_process_timeout_and_output_ceiling(monkeypatch, tmp_path):
    captured = {}
    def result(args, **kwargs):
        captured.update(args=args, **kwargs)
        return SimpleNamespace(returncode=0, stdout=b'x' * (worker.MAX_OUTPUT + 1))
    monkeypatch.setattr(archives.subprocess, 'run', result)
    with pytest.raises(ValueError, match='4 МиБ'):
        archives.extract_archive(tmp_path / 'source.zip', 'a' * 64, tmp_path / 'members')
    assert captured['timeout'] == 60 and captured['stdout'] == archives.subprocess.PIPE
    assert captured['stderr'] == archives.subprocess.DEVNULL
    assert captured['args'][1:3] == ['-m', 'land.torgi_archive_worker']
    def timeout(*a, **k): raise archives.subprocess.TimeoutExpired('worker', 60)
    monkeypatch.setattr(archives.subprocess, 'run', timeout)
    with pytest.raises(ValueError, match='60 секунд'):
        archives.extract_archive(tmp_path / 'source.zip', 'a' * 64, tmp_path / 'members')


def test_real_isolated_worker_smoke(tmp_path):
    raw = zip_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    path = tmp_path / (digest + '.zip')
    path.write_bytes(raw)
    result = archives.extract_archive(path, digest, tmp_path / 'members')
    assert result['counts']['read'] == 1 and result['counts']['incomplete'] == 1
    assert archives._validate_worker(result, raw, tmp_path / 'members') == result


def test_report_escapes_member_name(isolated, monkeypatch):
    raw = zip_bytes([('<script>.rtf', RTF)])
    seed(raw)
    observe(monkeypatch, raw)
    html = archives.html_report(PROJECT)
    assert '<script>' not in html and '&lt;script&gt;.rtf' in html


def test_all_empty_zip_is_partial_observation(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w'):
        pass
    raw = stream.getvalue()
    result = worker.process(raw, hashlib.sha256(raw).hexdigest(), tmp_path)
    assert result['state'] == 'partial' and result['archive_complete'] is False
    assert result['counts']['entries'] == 0
