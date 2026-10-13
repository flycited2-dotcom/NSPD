import copy
import json

import pytest

from land import attachment_context as context, archive_evidence, image_evidence, store, torgi_docs as docs
from land import torgi_archives as archives, torgi_image_frames as frames

DATE = '2026-10-11T12:00:00+00:00'
LOT = '22000144320000000046_1'
PRIVATE = 'private-person@example.com +7 999 123 45 67'


def reseal(record):
    record['evidence_sha256'] = archives._hash({key: value for key, value in record.items() if key != 'evidence_sha256'})


def seed(active=True):
    lot = {'id': LOT, 'notice_number': LOT.split('_')[0], 'type': {'code': 'ZK'},
           'url': 'https://torgi.gov.ru/new/public/lots/lot/' + LOT}
    search = {'id': 'search-v1', 'search_source_id': 'search-source-v1', 'created_at': DATE, 'lots': [lot]}
    card = docs.normalize_card({'id': LOT, 'noticeNumber': lot['notice_number'], 'subjectRFCode': '91',
        'lotAttachments': [{'fileId': '1' * 24, 'fileName': 'private-person@example.com.zip', 'fileSize': 512},
                           {'fileId': '2' * 24, 'fileName': 'private-person@example.com.jpg', 'fileSize': 1000}]}, lot)
    card.update(source=docs.CARD + LOT, received_at=DATE, sha256='c' * 64, http_status=200)
    catalog = {'id': 'catalog-v1', 'search_id': 'search-v1', 'search_source_id': 'search-source-v1',
               'cards': [card], 'files': docs.files_for([card], {})}
    zip_file, image = list(catalog['files'].values())
    image.update(state='read', sha256='4' * 64, received_at=DATE,
                 image={'preview_algorithm': image_evidence.ALGORITHM, 'source_format': 'MPO',
                        'source_frame_count': 3, 'preview_frame_index': 0, 'processed_frame_count': 1,
                        'unprocessed_frame_count': 2, 'complete_frame': True, 'complete_source': False})
    binding = archives.source_binding(zip_file, search, catalog, active)
    receipt = {'source': docs.FILE + zip_file['file_id'], 'sha256': '3' * 64, 'received_at': DATE,
               'actual_size': 512, 'content_type': 'application/zip', 'http_status': 200}
    manifest_entries = [
        {'id': 'a' * 64, 'name': PRIVATE + '.rtf', 'size': 37, 'compressed_size': 37,
         'format': 'rtf', 'state': 'ready', 'reason': '', 'crc32': '12345678', 'compression': 0},
        {'id': 'b' * 64, 'name': PRIVATE + '.exe', 'size': 16, 'compressed_size': 16,
         'format': 'exe', 'state': 'unsupported', 'reason': PRIVATE, 'crc32': '87654321', 'compression': 0}]
    observation_id = archives._hash({'binding_id': binding['binding_id'], 'source_receipt': receipt,
                                     'algorithm': archives.ALGORITHM})
    rows = copy.deepcopy(manifest_entries)
    for row in rows:
        row.update(geometry_confirmed=False, georeferenced=False, ocr_performed=False, reading_complete=False)
    rows[0].update(state='read', sha256='5' * 64, actual_size=37, crc_verified=True,
                  evidence={'algorithm': archives.member_algorithm('rtf'), 'geometry_confirmed': False,
                            'georeferenced': False, 'text_cache_sha256': '6' * 64, 'text_cache_size': 37,
                            'details': {'geometry_confirmed': False, 'georeferenced': False,
                                        'text_layer_complete': False, 'private_body': PRIVATE,
                                        'mentions': [{'cadastral_number': '90:12:040101:23', 'text': PRIVATE}]}})
    for row in rows:
        row['association_reference'] = {'binding_id': binding['binding_id'], 'observation_id': observation_id,
            'parent_file_key': zip_file['key'], 'archive_sha256': receipt['sha256'],
            'source_receipt_sha256': archives._hash(receipt), 'entry_id': row['id'], 'member_sha256': row.get('sha256')}
    record = {'binding_id': binding['binding_id'], 'source_binding': binding, 'file_key': zip_file['key'],
              'algorithm': archives.ALGORITHM, 'source_receipt': receipt, 'checked_at': DATE,
              'archive_sha256': receipt['sha256'], 'observation_id': observation_id,
              'archive_complete': False, 'ocr_performed': False, 'geometry_confirmed': False, 'georeferenced': False,
              'pdf_max_pages': 40, 'member_max_bytes': archive_evidence.MAX_MEMBER_BYTES,
              'manifest': {'algorithm': archive_evidence.ALGORITHM, 'archive_sha256': receipt['sha256'],
                           'size': 512, 'entry_count': 2, 'entries': manifest_entries},
              'entries': rows, 'state': 'partial',
              'counts': {'entries': 2, 'supported': 1, 'read': 1, 'errors': 0,
                         'unsupported': 1, 'directories': 0, 'incomplete': 1}}
    reseal(record)
    collection = {'id': 'archives-v1', 'algorithm': archives.ALGORITHM, 'records': {binding['binding_id']: record}}
    image_binding = frames.binding(catalog, search, image)
    binding_id = frames._hash(image_binding)
    observed = {'algorithm': frames.ALGORITHM, 'binding_id': binding_id, 'source_sha256': image['sha256'],
                'source_format': 'jpg', 'frame_scope': frames.frame_scope(image, 1), 'processed_at': DATE,
                'state': 'received', 'png_sha256': 'd' * 64, 'ocr_read': False, 'private_body': PRIVATE}
    aux = {'algorithm': frames.ALGORITHM, 'entries': {binding_id: {'binding': image_binding, 'frames': {'1': observed}}}}
    prefix = 'torgi_active' if active else 'torgi'
    return {prefix: search, prefix + '_documents': catalog, prefix + '_archives': collection,
            prefix + '_documents_image_frames': aux}


def group(values, label='regional_active'):
    return context.snapshot(values)['groups'][label]


def record(values):
    return next(iter(values['torgi_active_archives']['records'].values()))


def auxiliary(values):
    return next(iter(values['torgi_active_documents_image_frames']['entries'].values()))['frames']['1']


def test_snapshot_is_pure_and_contains_only_dated_current_metadata(monkeypatch):
    values = seed()
    def forbidden(*args, **kwargs):
        pytest.fail('Frozen attachment metadata must not use live state, files or network')
    for method in ('get_setting', 'connect', 'set_setting', 'now'):
        monkeypatch.setattr(store, method, forbidden)
    monkeypatch.setattr(archives, '_cached_bytes', forbidden)
    monkeypatch.setattr(archives, 'fetch', forbidden)
    monkeypatch.setattr(archives, 'extract_archive', forbidden)
    monkeypatch.setattr(frames, 'source_path', forbidden)
    monkeypatch.setattr(frames, '_read_cache', forbidden)
    result = group(values)
    assert result['documents_current'] and result['state'] == 'current'
    zip_result = result['archives']
    assert zip_result['counts']['accepted_observations'] == 1
    assert zip_result['counts']['members_read'] == zip_result['counts']['members_unsupported'] == 1
    archive = zip_result['files'][0]
    assert archive['source_receipt']['source_sha256'] == '3' * 64
    assert archive['source_receipt']['received_at'] == DATE
    assert archive['evidence_sha256'] == record(values)['evidence_sha256']
    member = archive['members'][0]
    assert member['sha256'] == '5' * 64 and member['crc_verified_observed']
    assert member['number_hints'] == [{'cadastral_number': '90:12:40101:23', 'verification_required': True}]
    aux = result['auxiliary_frames']
    assert aux['counts'] == {'received': 1, 'error': 0, 'pending': 1, 'unaccepted': 0,
                             'declared_auxiliary_frames': 2, 'unaccepted_files': 0}
    assert aux['files'][0]['source_received_at'] == DATE
    assert aux['files'][0]['frames'][0]['png_sha256'] == 'd' * 64
    assert aux['files'][0]['frames'][1]['frame_scope'] is None
    assert not aux['cache_verified_now']


def test_snapshot_detaches_every_record_from_later_mutation():
    values = seed()
    frozen = context.snapshot(values)
    original = json.dumps(frozen, sort_keys=True)
    record(values)['source_receipt']['received_at'] = '2027-01-01T00:00:00+00:00'
    record(values)['entries'][0]['evidence']['details']['mentions'][0]['cadastral_number'] = '90:12:1:999'
    record(values)['counts']['read'] = 0
    auxiliary(values)['frame_scope']['source_frame_index'] = 0
    auxiliary(values)['png_sha256'] = 'e' * 64
    values['torgi_active_documents']['files'].clear()
    assert json.dumps(frozen, sort_keys=True) == original


def test_snapshot_does_not_copy_filenames_raw_text_private_errors_or_live_links():
    result = context.snapshot(seed())
    serialized = json.dumps(result)
    for text in (PRIVATE, '@', 'file_name', 'private_body', 'association_reference', '/api/', 'geometry_sources'):
        assert text not in serialized
    assert docs.FILE in serialized
    assert len(serialized) < 12000


def test_every_legal_or_geometric_status_stays_unconfirmed():
    result = context.snapshot(seed())
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in context.FLAGS or key in ('archive_complete', 'ocr_performed', 'ocr_read', 'cache_verified_now', 'number_hints_confirm_lot'):
                    assert item is False, key
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(result)
    frame = result['groups']['regional_active']['auxiliary_frames']['files'][0]['frames'][0]
    assert frame['complete_frame_observed'] and frame['frame_scope']['complete_source'] is False


@pytest.mark.parametrize('change', ['search_source', 'same_id_receipt', 'same_id_card_notice',
                                  'same_id_association', 'missing_card_receipt', 'foreign_file_descriptor'])
def test_stale_or_same_id_source_mutations_cannot_reuse_success(change):
    values = seed()
    catalog = values['torgi_active_documents']
    if change == 'search_source': values['torgi_active']['search_source_id'] = 'foreign-source'
    elif change == 'same_id_receipt': catalog['cards'][0]['sha256'] = 'e' * 64
    elif change == 'same_id_card_notice': catalog['cards'][0]['notice_number'] = '99999999999999'
    elif change == 'same_id_association': next(iter(catalog['files'].values()))['associations'] = []
    elif change == 'missing_card_receipt': catalog['cards'][0].pop('received_at')
    else: next(iter(catalog['files'].values()))['declared_size'] += 1
    result = group(values)
    assert result['archives']['counts']['accepted_observations'] == 0
    assert result['archives']['counts']['members_read'] == 0
    assert result['auxiliary_frames']['counts']['received'] == 0


@pytest.mark.parametrize('change', ['missing_evidence_hash', 'wrong_evidence_hash', 'source_sha', 'source_date',
                                  'source_url', 'observation_id', 'binding', 'counts_bool', 'false_geometry',
                                  'false_member_geometry', 'foreign_entry', 'member_reference', 'member_sha',
                                  'false_complete', 'row_complete_with_error'])
def test_foreign_legacy_or_ambiguous_archive_metadata_is_unaccepted_even_if_resealed(change):
    values = seed()
    observed = record(values)
    if change == 'missing_evidence_hash': observed.pop('evidence_sha256')
    elif change == 'wrong_evidence_hash': observed['evidence_sha256'] = 'f' * 64
    elif change == 'source_sha': observed['source_receipt']['sha256'] = 'f' * 64
    elif change == 'source_date': observed['source_receipt']['received_at'] = 'yesterday'
    elif change == 'source_url': observed['source_receipt']['source'] = docs.FILE + 'f' * 24
    elif change == 'observation_id': observed['observation_id'] = 'f' * 64
    elif change == 'binding': observed['source_binding']['card_receipts'][0]['sha256'] = 'f' * 64
    elif change == 'counts_bool': observed['counts']['read'] = True
    elif change == 'false_geometry': observed['georeferenced'] = True
    elif change == 'false_member_geometry': observed['entries'][0]['evidence']['details']['geometry_confirmed'] = True
    elif change == 'foreign_entry': observed['entries'][0]['id'] = 'f' * 64
    elif change == 'member_reference': observed['entries'][0]['association_reference']['member_sha256'] = 'f' * 64
    elif change == 'member_sha': observed['entries'][0]['sha256'] = 'f' * 64
    elif change == 'false_complete': observed['archive_complete'] = True
    else: observed['entries'][1]['reading_complete'] = True
    if change not in ('missing_evidence_hash', 'wrong_evidence_hash'):
        reseal(observed)
    result = group(values)['archives']
    assert result['counts']['accepted_observations'] == result['counts']['members_read'] == 0
    assert result['files'][0]['state'] == 'unaccepted'
    assert result['files'][0]['members'] == []


@pytest.mark.parametrize('change', ['source_sha', 'scope_bool', 'wrong_scope', 'png_sha', 'ocr_bool', 'processed_date'])
def test_auxiliary_metadata_requires_exact_typed_scope_source_hash_and_original_date(change):
    values = seed()
    observed = auxiliary(values)
    if change == 'source_sha': observed['source_sha256'] = 'e' * 64
    elif change == 'scope_bool': observed['frame_scope']['source_frame_index'] = True
    elif change == 'wrong_scope': observed['frame_scope']['source_frame_index'] = 0
    elif change == 'png_sha': observed['png_sha256'] = 'invalid'
    elif change == 'ocr_bool': observed['ocr_read'] = 0
    else: observed['processed_at'] = 'invalid-date'
    result = group(values)['auxiliary_frames']
    assert result['counts']['received'] == 0 and result['counts']['unaccepted'] == 1
    assert result['files'][0]['frames'][0]['accepted_observation'] is False


def test_recorded_errors_remain_visible_without_read_success_or_private_error_text():
    values = seed()
    observed = record(values)
    observed.update(state='error', error=PRIVATE)
    observed.pop('entries')
    auxiliary(values).update(state='error', error=PRIVATE)
    auxiliary(values).pop('png_sha256')
    result = group(values)
    assert result['archives']['counts']['error'] == 1
    assert result['archives']['counts']['accepted_observations'] == result['archives']['counts']['members_read'] == 0
    assert result['auxiliary_frames']['counts']['error'] == 1
    assert result['auxiliary_frames']['counts']['received'] == 0
    assert not result['auxiliary_frames']['files'][0]['frames'][0]['complete_frame_observed']
    assert PRIVATE not in json.dumps(result)


def test_historical_and_regional_groups_do_not_share_currentness_or_evidence():
    values = seed() | seed(active=False)
    values['torgi']['search_source_id'] = 'old-source'
    result = context.snapshot(values)
    assert result['groups']['historical']['documents_current'] is False
    assert result['groups']['historical']['archives']['counts']['accepted_observations'] == 0
    assert result['groups']['regional_active']['archives']['counts']['accepted_observations'] == 1


def test_archive_source_reuse_after_geometry_rematch_does_not_relax_exact_auxiliary_binding():
    values = seed()
    values['torgi_active']['id'] = 'geometry-rematch'
    result = group(values)
    assert result['documents_current']
    assert result['archives']['counts']['accepted_observations'] == 1
    assert result['auxiliary_frames']['counts']['received'] == 0
    assert result['auxiliary_frames']['retained_or_omitted_observations'] == 1


def test_missing_legacy_inputs_have_no_success_or_legal_confirmation():
    for values in ({}, {'torgi_active_documents': {'files': {}}}, {'torgi_active': [], 'torgi_documents': None}):
        result = context.snapshot(values)
        for item in result['groups'].values():
            assert not item['documents_current']
            assert item['archives']['counts']['accepted_observations'] == 0
            assert item['auxiliary_frames']['counts']['received'] == 0
    with pytest.raises(ValueError):
        context.snapshot(None)


def test_hint_projection_is_bounded_and_omissions_are_explicit():
    values = seed()
    observed = record(values)
    observed['entries'][0]['evidence']['details']['mentions'] = [
        {'cadastral_number': f'90:12:040101:{number}', 'raw_text': PRIVATE} for number in range(100)]
    reseal(observed)
    member = group(values)['archives']['files'][0]['members'][0]
    assert len(member['number_hints']) == context.MAX_HINTS
    assert member['omitted_number_hints'] == 100 - context.MAX_HINTS


def test_file_projection_is_bounded_without_silently_claiming_complete_coverage():
    values = seed()
    catalog = values['torgi_active_documents']
    card = catalog['cards'][0]
    attachments = []
    for number in range(context.MAX_FILES + 3):
        attachments.append(docs.descriptor({'fileId': f'{number + 10:024x}', 'fileName': f'archive{number}.zip',
                                           'fileSize': 512}, 'lot'))
    card['attachments'] = attachments
    catalog['files'] = docs.files_for([card], {})
    result = group(values)['archives']
    assert len(result['files']) == context.MAX_FILES
    assert result['declared_files'] == context.MAX_FILES + 3 and result['omitted_files'] == 3
    assert result['counts']['pending'] == context.MAX_FILES
    assert result['counts']['accepted_observations'] == 0 and not result['coverage_confirmed']
