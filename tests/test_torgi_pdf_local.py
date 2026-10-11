import copy
import hashlib
import io
import json
import subprocess

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from land import municipal, store, torgi, torgi_docs as docs, torgi_file_worker as reader
from land import torgi_pdf_local as local, torgi_pdf_worker as worker, torgi_visual as visual


def pdf(pages=55):
    writer = PdfWriter()
    for number in range(1, pages + 1):
        page = writer.add_blank_page(300, 300)
        if number in (1, 41, pages):
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 10 Tf 10 100 Td (Land 90:12:172101:{number}) Tj ET'.encode())
            page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def lot(number=1):
    return {'id': f'22000144320000000046_{number}', 'notice_number': '22000144320000000046',
            'url': 'https://torgi.gov.ru/new/public/lots/lot/test', 'type': {'code': 'ZK'}}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    monkeypatch.setattr(docs, 'fetch', lambda *args, **kwargs: pytest.fail('Local continuation must never fetch'))
    return tmp_path


def seed(db, counts=(55,), active=False):
    files, cards = {}, []
    folder = db / 'torgi_documents'
    folder.mkdir(exist_ok=True)
    search = {'id': 'search', 'search_source_id': 'source', 'created_at': 'search-date', 'lots': []}
    for index, count in enumerate(counts, 1):
        raw = pdf(count)
        digest = hashlib.sha256(raw).hexdigest()
        path = folder / (digest + '.pdf')
        path.write_bytes(raw)
        path.with_suffix('.txt').write_text('original first 40 page text', 'utf-8')
        attachment = docs.descriptor({'fileId': str(index) * 24, 'fileName': 'scheme.pdf', 'fileSize': len(raw)}, 'lot')
        details, _ = reader.extract(raw, 'pdf')
        item = lot(index)
        file = {**attachment, **details, 'sha256': digest, 'algorithm': docs.ALGORITHM,
                'state': 'read', 'eligible': True, 'metadata_conflict': False,
                'received_at': 'original-source-date', 'http_status': 200, 'content_type': 'application/pdf',
                'actual_size': len(raw), 'visual_ocr': {'source_sha256': digest, 'pages': []},
                'associations': [{'lot_id': item['id'], 'notice_number': item['notice_number'], 'scope': 'lot', 'inactive': False}]}
        files[file['key']] = file
        search['lots'].append(item)
        cards.append({'lot_id': item['id'], 'notice_number': item['notice_number'], 'state': 'received'})
    catalog = {'id': 'docs', 'search_id': 'search', 'search_source_id': 'source', 'search_created_at': 'search-date',
               'cards': cards, 'files': files, 'geometry_confirmed': False}
    store.set_setting(torgi.setting_key('trudovoe', active), search)
    store.set_setting(docs.setting_key('trudovoe', active), catalog)
    return catalog, search


def test_real_child_reads_55_and_53_pages_and_preserves_prior_source_evidence(db):
    original, _ = seed(db, (55, 53))
    assert local.summary(original)['unread_pages'] == 28
    outcome = local.run('trudovoe', {'id': 'docs'})
    assert outcome == {'processed': 2, 'errors': 0, 'remaining': 0, 'unread_pages': 0, 'network_requests': 0, 'state': 'done'}
    catalog = store.get_setting(docs.setting_key('trudovoe'))
    for key, before in original['files'].items():
        file = catalog['files'][key]
        for field in ('sha256', 'received_at', 'http_status', 'content_type', 'actual_size', 'associations', 'visual_ocr'):
            assert file[field] == before[field]
        assert file['processed_pages'] == file['total_pages'] and file['unread_pages'] == 0
        assert not file['text_layer_complete']  # Blank/sparse pages still need OCR.
        assert not file['geometry_confirmed'] and not file['georeferenced']
        assert file['local_pdf_previous']['mentions'] == before['mentions']
        assert file['local_pdf_previous']['processed_pages'] == 40
        assert file['local_pdf_previous']['received_at'] == 'original-source-date'
        assert {'cadastral_number': '90:12:172101:41', 'pages': [41]} in file['mentions']
        path = db / 'torgi_documents' / (file['sha256'] + '.pdf')
        assert path.with_suffix('.txt').read_text('utf-8') == 'original first 40 page text'
        assert 'UNIT 41\n' in path.with_suffix('.continued.txt').read_text('utf-8')
        assert docs.pdf_scope(file) == (16, 120)
    assert local.report('trudovoe')['attempt']['network_requests'] == 0
    assert store.candidates('trudovoe') == []


def test_120_page_cap_leaves_remainder_and_does_not_repeat_same_prefix(db):
    original, _ = seed(db, (121,))
    local.run('trudovoe', {'id': original['id']})
    catalog = store.get_setting(docs.setting_key('trudovoe'))
    file = next(iter(catalog['files'].values()))
    assert file['processed_pages'] == 120 and file['unread_pages'] == 1
    assert not local.queue(catalog) and not local.queue(catalog, True)
    summary = local.summary(catalog)
    assert summary['remaining'] == 0 and summary['unread_pages'] == 1 and summary['limited_files'] == 1
    assert summary['max_pages'] == 120 and not file['text_layer_complete']


@pytest.mark.parametrize('change', [{'eligible': False}, {'metadata_conflict': True}, {'state': 'rejected'},
                                  {'format': 'docx'}, {'sha256': None}, {'received_at': None},
                                  {'processed_pages': True}, {'unread_pages': 99}])
def test_queue_ignores_unaccepted_incomplete_or_conflicting_sources(db, change):
    catalog, _ = seed(db)
    next(iter(catalog['files'].values())).update(change)
    assert not local.queue(catalog)


@pytest.mark.parametrize('damage', ['missing', 'changed', 'parser_error'])
def test_failed_local_read_preserves_all_prior_evidence_and_needs_explicit_retry(db, monkeypatch, damage):
    original, _ = seed(db)
    file = next(iter(original['files'].values()))
    path = db / 'torgi_documents' / (file['sha256'] + '.pdf')
    if damage == 'missing':
        path.unlink()
    elif damage == 'changed':
        path.write_bytes(b'changed')
    else:
        monkeypatch.setattr(local, 'read_saved', lambda *args: (_ for _ in ()).throw(ValueError('unsupported PDF')))
    outcome = local.run('trudovoe', {'id': 'docs'})
    catalog = store.get_setting(docs.setting_key('trudovoe'))
    current = catalog['files'][file['key']]
    assert outcome['state'] == 'partial' and outcome['errors'] == 1
    assert {key: value for key, value in current.items() if key != 'local_pdf_attempt'} == file
    assert current['local_pdf_attempt']['state'] == 'error'
    assert not local.queue(catalog) and len(local.queue(catalog, True)) == 1
    assert local.summary(catalog)['unread_pages'] == 15


def test_timeout_output_and_source_guards(db, monkeypatch):
    catalog, _ = seed(db)
    digest = next(iter(catalog['files'].values()))['sha256']
    def timeout(*args, **kwargs):
        assert kwargs['timeout'] == 60
        raise subprocess.TimeoutExpired('reader', 60)
    monkeypatch.setattr(local.subprocess, 'run', timeout)
    with pytest.raises(ValueError, match='60 секунд'):
        local.read_saved(digest)
    with pytest.raises(ValueError, match='SHA'):
        local.read_saved('../outside')
    monkeypatch.setattr(local.subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess([], 0, stdout='x' * (worker.MAX_OUTPUT + 1)))
    with pytest.raises(ValueError, match='4 МБ'):
        local.read_saved(digest)


@pytest.mark.parametrize('change', ['catalog_id', 'search_source', 'lot_same_id', 'catalog_content_same_id'])
def test_changed_catalog_search_or_lot_during_worker_is_not_overwritten(db, monkeypatch, change):
    original, search = seed(db, active=True)
    file = next(iter(original['files'].values()))
    details = worker.extract(db / 'torgi_documents' / (file['sha256'] + '.pdf'), file['sha256'])
    replacement = copy.deepcopy(original)
    def read_saved(*args):
        if change in ('catalog_id', 'catalog_content_same_id'):
            if change == 'catalog_id':
                replacement['id'] = 'concurrent'
            else:
                next(iter(replacement['files'].values()))['associations'][0]['inactive'] = True
            store.set_setting(docs.setting_key('trudovoe', True), replacement)
        else:
            revised = copy.deepcopy(search)
            if change == 'search_source':
                revised['search_source_id'] = 'new-source'
            else:
                revised['lots'][0]['notice_number'] = 'different-notice'
            store.set_setting(torgi.setting_key('trudovoe', True), revised)
        return details
    monkeypatch.setattr(local, 'read_saved', read_saved)
    with pytest.raises(ValueError, match='изменил'):
        local.run('trudovoe', {'id': 'docs', 'lot_id': lot()['id']}, active=True)
    assert store.get_setting(docs.setting_key('trudovoe', True)) == replacement
    assert store.get_setting(docs.setting_key('trudovoe', True, '_pdf_local_attempt'))['state'] == 'error'


def test_selected_regional_lot_only_and_stale_parameters_fail_before_worker(db, monkeypatch):
    original, _ = seed(db, (55, 53), active=True)
    historical = {'id': 'historical', 'files': {}}
    store.set_setting(docs.setting_key('trudovoe'), historical)
    with pytest.raises(ValueError, match='изменил'):
        local.run('trudovoe', {'id': 'stale'}, active=True)
    with pytest.raises(ValueError, match='отсутствуют'):
        local.run('trudovoe', {'id': 'docs', 'lot_id': lot(99)['id']}, active=True)
    with pytest.raises(ValueError, match='логическим'):
        local.run('trudovoe', {'id': 'docs', 'retry_errors': 'yes'}, active=True)
    outcome = local.run('trudovoe', {'id': 'docs', 'lot_id': lot(1)['id']}, active=True)
    catalog = store.get_setting(docs.setting_key('trudovoe', True))
    first, second = list(original['files'].values())
    assert outcome['processed'] == 1 and outcome['lot_id'] == lot(1)['id']
    assert catalog['files'][first['key']]['processed_pages'] == 55
    assert catalog['files'][second['key']] == second
    assert local.summary(catalog)['remaining'] == 1
    assert local.summary(catalog, lot(1)['id'])['remaining'] == 0
    assert store.get_setting(docs.setting_key('trudovoe')) == historical


def test_batch_is_three_files_and_report_detects_old_search(db, monkeypatch):
    catalog, search = seed(db, (41, 42, 43, 44))
    def read_saved(digest):
        return worker.extract(db / 'torgi_documents' / (digest + '.pdf'), digest)
    monkeypatch.setattr(local, 'read_saved', read_saved)
    outcome = local.run('trudovoe', {'id': catalog['id']})
    assert outcome['processed'] == 3 and outcome['remaining'] == 1
    search['search_source_id'] = 'changed'
    store.set_setting(torgi.setting_key('trudovoe'), search)
    assert local.report('trudovoe')['stale']


def test_table_crossing_page_40_uses_absolute_numbers_without_georeferencing(db, monkeypatch):
    from test_egrn_coordinates import pages as egrn_pages
    source = b'%PDF-source-bound-test'
    digest = hashlib.sha256(source).hexdigest()
    path = db / (digest + '.pdf')
    path.write_bytes(source)
    egrn = egrn_pages()
    pages = [(number, '') for number in range(1, 40)] + [(40, egrn[0][1]), (41, egrn[1][1])]
    def pdf_text(raw, **kwargs):
        selected = pages if kwargs.get('max_pages') == 120 else pages[:40]
        return {**municipal.evidence(selected), 'total_pages': 41, 'processed_pages': len(selected),
                'unread_pages': 41 - len(selected), 'image_or_sparse_pages': list(range(1, 40)), 'text_layer_complete': False}, selected
    monkeypatch.setattr(municipal, 'pdf_text', pdf_text)
    short, _ = reader.extract(source, 'pdf')
    extended = worker.extract(path, digest)
    assert short['egrn_tables'][0]['state'] == 'rejected'
    table = extended['egrn_tables'][0]
    assert table['pages'] == [40, 41] and table['heading_page'] == 40
    assert table['state'] == 'review_required' and table['crs_status'] == 'parameters_missing'
    assert not table['geometry_confirmed'] and not table['georeferenced']


def test_extended_sparse_pages_reach_ocr_only_with_source_bound_marker(db, monkeypatch):
    catalog, _ = seed(db)
    local.run('trudovoe', {'id': catalog['id']})
    catalog = store.get_setting(docs.setting_key('trudovoe'))
    file = next(iter(catalog['files'].values()))
    assert any(number > 40 for _, number in visual.queue(catalog))
    changed = copy.deepcopy(file)
    changed['local_pdf_source_sha256'] = '0' * 64
    assert docs.pdf_scope(changed) == (16, 40)
    with pytest.raises(ValueError, match='страница OCR'):
        visual.queue({'files': {changed['key']: changed}})
    calls = []
    def stop(args, timeout):
        calls.append(args)
        raise ValueError('Stop before OCR')
    monkeypatch.setattr(visual.ocr, 'worker', stop)
    with pytest.raises(ValueError, match='Stop before OCR'):
        visual.read_unit(file, 42)
    assert calls[0][calls[0].index('--max-pdf-pages') + 1] == '120'


def test_worker_rejects_changed_source_and_other_extended_formats(db):
    path = db / 'source.pdf'
    path.write_bytes(pdf())
    with pytest.raises(ValueError, match='SHA'):
        worker.extract(path, '0' * 64)
    with pytest.raises(ValueError, match='только для PDF'):
        reader.extract(b'not-docx', 'docx', max_pages=120)
