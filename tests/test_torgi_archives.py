import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile
import pytest
from land import archive_evidence, egrn_xml, store, torgi, torgi_docs
from land import torgi_archives as archives
from land import torgi_archive_worker as worker


PROJECT = 'archive-test'
LOT = '22000144320000000046_1'
DATE = '2026-10-11T12:00:00+00:00'
RTF = b'{\\rtf1 public evidence 90:12:040101:23}'


def xml_bytes():
    points = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    ordinates = ''.join('<ordinate><x>' + str(x) + '</x><y>' + str(y)
                       + '</y><ord_nmb>' + str(index + 1) + '</ord_nmb></ordinate>'
                       for index, (x, y) in enumerate(points))
    return ('<?xml version="1.0" encoding="UTF-8"?><extract_about_property_land>'
            '<details_request><address>PRIVATE_ADDRESS_NOT_COPIED</address></details_request>'
            '<right_records><owner>PRIVATE_OWNER_NOT_COPIED</owner></right_records>'
            '<recipient_statement><recipient>PRIVATE_RECIPIENT_NOT_COPIED</recipient></recipient_statement>'
            '<land_record><object><common_data><cad_number>99:99:1234567:8</cad_number>'
            '</common_data></object><contours_location><contours><contour><entity_spatial>'
            '<sk_id>UNKNOWN LOCAL &lt;CRS&gt;</sk_id><spatials_elements><spatial_element><ordinates>'
            + ordinates + '</ordinates></spatial_element></spatials_elements></entity_spatial>'
            '</contour></contours></contours_location></land_record></extract_about_property_land>').encode()


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


def test_xml_worker_retains_only_safe_selected_json_and_never_claims_complete(tmp_path):
    member = xml_bytes()
    raw = zip_bytes([('folder/private-name.xml', member)])
    result = worker.process(raw, hashlib.sha256(raw).hexdigest(), tmp_path)
    row = result['entries'][0]
    assert result['algorithm'] == 'torgi-archive-members-v2'
    assert result['xml_max_bytes'] == 2 * 1024 * 1024
    assert row['state'] == 'read' and row['reading_complete'] is False
    assert result['counts']['read'] == result['counts']['incomplete'] == 1
    assert result['all_supported_members_read'] is True
    assert result['state'] == 'partial' and result['archive_complete'] is False
    details = row['evidence']['details']
    assert details == egrn_xml.extract(member)
    assert details['accepted_contour_count'] == 1 and details['accepted_point_count'] == 5
    assert details['all_contours_accepted'] is True and details['complete_source'] is False
    assert details['georeferenced'] is details['rights_confirmed'] is details['free_land_confirmed'] is False
    digest = hashlib.sha256(member).hexdigest()
    selected = worker.xml_whitelist_bytes(details)
    selected_sha = hashlib.sha256(selected).hexdigest()
    assert list(tmp_path.iterdir()) == [tmp_path / (selected_sha + '.xml.whitelist.json')]
    assert (tmp_path / (selected_sha + '.xml.whitelist.json')).read_bytes() == selected
    assert row['evidence']['whitelist_cache_sha256'] == selected_sha
    assert row['evidence']['whitelist_cache_size'] == len(selected)
    assert b'PRIVATE_' not in selected and 'PRIVATE_' not in json.dumps(result)
    assert archives._validate_worker(result, raw, tmp_path) == result


def test_xml_run_preserves_primary_provenance_and_reports_only_selected_evidence(isolated, monkeypatch):
    raw = zip_bytes([('PRIVATE_FILENAME_NOT_SHOWN.xml', xml_bytes())])
    _, catalog = seed(raw)
    observe(monkeypatch, raw)
    observed = record()
    assert store.get_setting(torgi_docs.setting_key(PROJECT, True)) == catalog
    assert observed['entries'][0]['association_reference']['member_sha256'] == hashlib.sha256(xml_bytes()).hexdigest()
    assert observed['source_receipt']['sha256'] == hashlib.sha256(raw).hexdigest()
    report = archives.summary(PROJECT)
    assert report['xml_members_read'] == report['xml_contours'] == 1
    assert report['pending_cached'] == 0
    html = archives.html_report(PROJECT)
    assert '99:99:1234567:8' in html and 'UNKNOWN LOCAL &lt;CRS&gt;' in html
    assert '<CRS>' not in html and 'PRIVATE_' not in html
    assert 'не подтверждены' in html and 'Частичное чтение XML' in html
    assert html.count('data-xml-point') == 5


@pytest.mark.parametrize('member', [b'<extract_about_property_land><owner>PRIVATE_NOT_COPIED',
                                  b'<unknown><owner>PRIVATE_NOT_COPIED</owner></unknown>',
                                  b'<!DOCTYPE x [<!ENTITY x "PRIVATE_NOT_COPIED">]><x>&x;</x>'])
def test_malformed_or_unsupported_xml_has_fixed_error_and_no_member_cache(tmp_path, member):
    raw = zip_bytes([('selected.xml', member)])
    result = worker.process(raw, hashlib.sha256(raw).hexdigest(), tmp_path)
    assert result['counts']['errors'] == 1 and result['state'] == 'partial'
    assert result['entries'][0]['error'] == 'ЕГРН XML: чтение выбранных полей не удалось'
    assert not list(tmp_path.iterdir())
    assert 'PRIVATE_NOT_COPIED' not in json.dumps(result)
    assert archives._validate_worker(result, raw, tmp_path) == result


def test_oversized_xml_is_not_opened_and_other_supported_members_still_read(tmp_path, monkeypatch):
    raw = zip_bytes([('large.xml', b'<' + b'X' * archive_evidence.MAX_XML_BYTES), ('ok.rtf', RTF)])
    monkeypatch.setattr(worker.egrn_xml, 'extract', lambda *a: pytest.fail('Oversized XML must not be parsed'))
    result = worker.process(raw, hashlib.sha256(raw).hexdigest(), tmp_path)
    assert result['counts']['unsupported'] == result['counts']['read'] == 1
    assert result['entries'][0]['state'] == 'unsupported' and 'sha256' not in result['entries'][0]
    assert not list(tmp_path.glob('*.xml*'))


@pytest.mark.parametrize('change', ['private_details', 'false_rights', 'false_complete', 'private_cache',
                                    'cache_digest', 'cache_size', 'extra_text_cache', 'raw_geometry',
                                    'extra_private_row', 'private_reason', 'foreign_archive_sha'])
def test_xml_worker_cannot_persist_private_fields_false_claims_or_modified_safe_cache(isolated, monkeypatch, change):
    raw = zip_bytes([('selected.xml', xml_bytes())])
    seed(raw)
    def injected(path, digest, folder):
        result = worker.process(Path(path).read_bytes(), digest, folder)
        row = result['entries'][0]
        evidence = row['evidence']
        cache = Path(folder) / (evidence['whitelist_cache_sha256'] + '.xml.whitelist.json')
        if change == 'private_details': evidence['details']['owner'] = 'PRIVATE_INJECTED'
        elif change == 'false_rights': evidence['details']['rights_confirmed'] = True
        elif change == 'false_complete': row['reading_complete'] = True
        elif change == 'private_cache': cache.write_bytes(b'{"owner":"PRIVATE_INJECTED"}')
        elif change == 'cache_digest': evidence['whitelist_cache_sha256'] = '0' * 64
        elif change == 'cache_size': evidence['whitelist_cache_size'] += 1
        elif change == 'extra_text_cache': evidence['text_cache_sha256'] = '0' * 64
        elif change == 'extra_private_row': row['owner'] = 'PRIVATE_INJECTED'
        elif change == 'private_reason': row['reason'] = 'PRIVATE_INJECTED'
        elif change == 'foreign_archive_sha': row['archive_sha256'] = '0' * 64
        else: evidence['details']['geometry_confirmed'] = True
        return result
    monkeypatch.setattr(archives, 'extract_archive', injected)
    observe(monkeypatch, raw)
    assert record()['state'] == 'error' and 'entries' not in record()
    assert 'PRIVATE_' not in json.dumps(record())


def test_xml_error_worker_cannot_persist_private_exception_text(isolated, monkeypatch):
    raw = zip_bytes([('selected.xml', b'<broken>PRIVATE_BODY')])
    seed(raw)
    def injected(path, digest, folder):
        result = worker.process(Path(path).read_bytes(), digest, folder)
        result['entries'][0]['error'] = 'PRIVATE_EXCEPTION'
        return result
    monkeypatch.setattr(archives, 'extract_archive', injected)
    observe(monkeypatch, raw)
    assert record()['state'] == 'error' and 'entries' not in record()
    assert 'PRIVATE_' not in json.dumps(record())


def test_unsupported_xml_worker_cannot_claim_reading_complete(isolated, monkeypatch):
    raw = zip_bytes([('large.xml', b'<' + b'X' * archive_evidence.MAX_XML_BYTES)])
    seed(raw)
    def injected(path, digest, folder):
        result = worker.process(Path(path).read_bytes(), digest, folder)
        result['entries'][0]['reading_complete'] = True
        return result
    monkeypatch.setattr(archives, 'extract_archive', injected)
    observe(monkeypatch, raw)
    assert record()['state'] == 'error' and 'entries' not in record()


def old_observation(isolated):
    """Model a genuine v1 result without reinterpreting its immutable bytes."""
    collection = store.get_setting(archives.setting_key(PROJECT))
    observed = copy.deepcopy(next(iter(collection['records'].values())))
    observed['algorithm'] = 'torgi-archive-members-v1'
    observed.pop('xml_max_bytes')
    observed['manifest']['algorithm'] = 'archive-evidence-v1'
    observed['observation_id'] = archives._hash({'binding_id': observed['binding_id'],
                    'source_receipt': observed['source_receipt'], 'algorithm': observed['algorithm']})
    for entry in observed['entries']:
        entry['association_reference']['observation_id'] = observed['observation_id']
        if entry['format'] == 'xml':
            original = next(row for row in observed['manifest']['entries'] if row['id'] == entry['id'])
            original.update(state='unsupported', reason='Формат не поддержан; содержимое не читается и не исполняется')
            entry = {**original, 'geometry_confirmed': False, 'georeferenced': False,
                     'reading_complete': False, 'ocr_performed': False,
                     'association_reference': {**entry['association_reference'], 'member_sha256': None}}
            observed['entries'] = [entry]
            observed['counts'].update(supported=0, read=0, unsupported=1, incomplete=0)
            observed['all_supported_members_read'] = False
    reference = archives._snapshot(isolated / 'torgi_archives', observed)
    collection['records'][observed['binding_id']] = observed
    collection.setdefault('observations', {})[observed['evidence_sha256']] = reference
    collection['algorithm'] = 'torgi-archive-members-v1'
    store.set_setting(archives.setting_key(PROJECT), collection)
    return observed


def test_v1_xml_is_pending_cached_then_migrates_with_same_receipt_and_immutable_history(isolated, monkeypatch):
    raw = zip_bytes([('selected.xml', xml_bytes())])
    _, catalog = seed(raw)
    observe(monkeypatch, raw)
    before = old_observation(isolated)
    frozen = archives.cached_observation(PROJECT, before['evidence_sha256'])
    report = archives.summary(PROJECT)
    assert report['pending_cached'] == report['pending'] == report['remaining'] == 1
    assert report['xml_members_read'] == report['xml_contours'] == 0
    assert report['archives'][0]['state'] == 'pending_cached'
    monkeypatch.setattr(archives, 'fetch', lambda *a, **k: pytest.fail('Migration must never download'))
    result = archives.run(PROJECT, {'id': 'primary-v1'})
    assert result['processed'] == result['cached_files'] == 1 and result['network_requests'] == 0
    assert result['remaining'] == 0 and result['state'] == 'partial'
    after = record()
    assert after['algorithm'] == worker.ALGORITHM
    assert after['source_receipt'] == before['source_receipt']
    assert after['binding_id'] == before['binding_id']
    assert after['observation_id'] != before['observation_id']
    assert after['previous_evidence_sha256'] == before['evidence_sha256']
    assert archives.cached_observation(PROJECT, before['evidence_sha256']) == frozen
    assert store.get_setting(torgi_docs.setting_key(PROJECT, True)) == catalog
    assert archives.summary(PROJECT)['xml_members_read'] == 1
    assert archives.summary(PROJECT)['pending_cached'] == 0
    assert archives.run(PROJECT, {'id': 'primary-v1'})['processed'] == 0


@pytest.mark.parametrize('change', ['missing_cache', 'corrupt_cache', 'missing_receipt', 'foreign_binding', 'foreign_digest'])
def test_cached_v1_migration_fails_closed_without_http_and_keeps_history(isolated, monkeypatch, change):
    raw = zip_bytes()
    seed(raw)
    observe(monkeypatch, raw)
    before = old_observation(isolated)
    collection = store.get_setting(archives.setting_key(PROJECT))
    old = collection['records'][before['binding_id']]
    path = isolated / 'torgi_archives' / (old['archive_sha256'] + '.zip')
    if change == 'missing_cache': path.unlink()
    elif change == 'corrupt_cache': path.write_bytes(b'changed source')
    elif change == 'missing_receipt': old.pop('source_receipt')
    elif change == 'foreign_binding': old['source_binding']['search_source_id'] = 'foreign'
    else: old['archive_sha256'] = '0' * 64
    store.set_setting(archives.setting_key(PROJECT), collection)
    monkeypatch.setattr(archives, 'fetch', lambda *a, **k: pytest.fail('Cached migration cannot fall back to HTTP'))
    result = archives.run(PROJECT, {'id': 'primary-v1'})
    assert result['network_requests'] == 0 and result['state'] == 'partial'
    assert record()['state'] in ('error', 'rejected')
    assert record()['previous_evidence_sha256'] == before['evidence_sha256']
    assert archives.cached_observation(PROJECT, before['evidence_sha256'])['algorithm'] == 'torgi-archive-members-v1'


@pytest.mark.parametrize('has_receipt', [True, False])
def test_v1_error_needs_explicit_retry_and_never_falls_back_to_download(isolated, monkeypatch, has_receipt):
    raw = zip_bytes()
    seed(raw)
    observe(monkeypatch, raw)
    before = old_observation(isolated)
    collection = store.get_setting(archives.setting_key(PROJECT))
    prior = collection['records'][before['binding_id']]
    prior.update(state='error', retryable=True)
    if not has_receipt:
        prior.pop('source_receipt')
    store.set_setting(archives.setting_key(PROJECT), collection)
    monkeypatch.setattr(archives, 'fetch', lambda *a, **k: pytest.fail('Recognized v1 retry cannot download'))
    assert archives.run(PROJECT, {'id': 'primary-v1'})['processed'] == 0
    result = archives.run(PROJECT, {'id': 'primary-v1', 'retry_errors': True})
    assert result['processed'] == 1 and result['network_requests'] == 0
    assert record()['cache_only'] is True
    if has_receipt:
        assert result['cached_files'] == 1
        assert record()['source_receipt'] == before['source_receipt']
    else:
        assert record()['state'] == 'error'
        # A failed migration keeps cache-only semantics on subsequent explicit retries.
        assert archives.run(PROJECT, {'id': 'primary-v1', 'retry_errors': True})['network_requests'] == 0


@pytest.mark.parametrize('change', ['binding', 'record_hash', 'manifest_algorithm', 'receipt', 'observation_id'])
def test_unaccepted_xml_record_does_not_render_selected_data_or_count_as_read(isolated, monkeypatch, change):
    raw = zip_bytes([('selected.xml', xml_bytes())])
    seed(raw)
    observe(monkeypatch, raw)
    collection = store.get_setting(archives.setting_key(PROJECT))
    observed = next(iter(collection['records'].values()))
    if change == 'binding': observed['source_binding']['search_source_id'] = 'foreign'
    elif change == 'record_hash': observed['evidence_sha256'] = '0' * 64
    elif change == 'manifest_algorithm': observed['manifest']['algorithm'] = 'archive-evidence-v1'
    elif change == 'receipt': observed['source_receipt']['received_at'] = 'invalid'
    else: observed['observation_id'] = '0' * 64
    if change != 'record_hash':
        observed['evidence_sha256'] = archives._hash({key: value for key, value in observed.items() if key != 'evidence_sha256'})
    store.set_setting(archives.setting_key(PROJECT), collection)
    assert archives.summary(PROJECT)['xml_members_read'] == 0
    html = archives.html_report(PROJECT)
    assert '99:99:1234567:8' not in html and 'UNKNOWN LOCAL' not in html
    assert 'data-xml-point' not in html


def test_xml_report_uses_shared_hundred_point_budget_without_dumping_raw_fields():
    details = egrn_xml.extract(xml_bytes())
    points = [[str(index), str(index + 1)] for index in range(130)]
    details['contours'] = [{'source_crs_raw': 'LOCAL <UNKNOWN>', 'raw_points_xy': points}] * 2
    entry = {'evidence': {'details': details}, 'sha256': 'a' * 64}
    observed = {'source_receipt': {'received_at': DATE}, 'checked_at': DATE}
    html, shown = archives._xml_html(entry, observed, 100)
    assert shown == html.count('data-xml-point') == 100
    assert 'LOCAL &lt;UNKNOWN&gt;' in html and '<UNKNOWN>' not in html
    assert 'Точек не показано' in html
    assert '30' in html and '130' in html


def test_real_isolated_xml_worker_smoke(tmp_path):
    raw = zip_bytes([('selected.xml', xml_bytes())])
    digest = hashlib.sha256(raw).hexdigest()
    path = tmp_path / (digest + '.zip')
    path.write_bytes(raw)
    output = archives.extract_archive(path, digest, tmp_path / 'members')
    assert output['counts']['read'] == output['counts']['incomplete'] == 1
    assert archives._validate_worker(output, raw, tmp_path / 'members') == output


def test_same_xml_source_new_selected_interpretation_preserves_prior_safe_cache(tmp_path, monkeypatch):
    raw = zip_bytes([('selected.xml', xml_bytes())])
    digest = hashlib.sha256(raw).hexdigest()
    original = egrn_xml.extract
    omitted = original(xml_bytes())
    omitted.update(contours=[], accepted_contour_count=0, accepted_point_count=0,
                   omitted_contour_count=1, all_contours_accepted=False,
                   errors=[{'contour_index': 0, 'code': 'invalid_point_order'}])
    monkeypatch.setattr(worker.egrn_xml, 'extract', lambda member: copy.deepcopy(omitted))
    previous = worker.process(raw, digest, tmp_path)
    old_evidence = previous['entries'][0]['evidence']
    old_path = tmp_path / (old_evidence['whitelist_cache_sha256'] + '.xml.whitelist.json')
    old_bytes = old_path.read_bytes()
    monkeypatch.setattr(worker.egrn_xml, 'extract', original)
    current = worker.process(raw, digest, tmp_path)
    new_evidence = current['entries'][0]['evidence']
    new_path = tmp_path / (new_evidence['whitelist_cache_sha256'] + '.xml.whitelist.json')
    assert old_path != new_path and old_path.read_bytes() == old_bytes
    assert new_path.read_bytes() == worker.xml_whitelist_bytes(original(xml_bytes()))
    assert previous['entries'][0]['sha256'] == current['entries'][0]['sha256']
    assert len(list(tmp_path.glob('*.xml.whitelist.json'))) == 2


def test_xml_safe_cache_content_address_is_never_overwritten_when_corrupt(tmp_path):
    raw = zip_bytes([('selected.xml', xml_bytes())])
    digest = hashlib.sha256(raw).hexdigest()
    previous = worker.process(raw, digest, tmp_path)
    selected_sha = previous['entries'][0]['evidence']['whitelist_cache_sha256']
    path = tmp_path / (selected_sha + '.xml.whitelist.json')
    path.write_bytes(b'changed safe cache')
    result = worker.process(raw, digest, tmp_path)
    assert result['entries'][0]['state'] == 'error' and result['counts']['errors'] == 1
    assert path.read_bytes() == b'changed safe cache'
