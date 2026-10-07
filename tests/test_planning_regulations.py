import hashlib
import subprocess
import pytest
from land import store, planning_regulations as pr, planning_text_worker as worker


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    row = {'id': 'document', 'state': 'read', 'url': 'https://simfmo-rk.ru/test.pdf',
           'title': 'Изменение ПЗЗ <script>', 'sha256': 'a' * 64, 'received_at': 'original-download-date',
           'act_identity': {'number': '922', 'date': '2023-06-28'}, 'document_role': 'act_text',
           'total_pages': 85, 'currently_listed': True, 'listing_conflicts': [],
           'listing_references': [{'planning_kind': 'pzz', 'parent_url': 'https://simfmo-rk.ru/index/'}]}
    source = {'id': 'catalog-v1', 'items': [row]}
    store.set_setting('planning_watch_trudovoe', source)
    monkeypatch.setattr(pr, 'MAX_WINDOWS', 1)
    monkeypatch.setattr(pr, 'read_window', fake_window)
    return source


def fake_window(sha, start, total):
    return {'algorithm': pr.ALGORITHM, 'source_sha256': sha, 'start': start, 'total_pages': total,
            'pages': [{'page': p, 'text': 'Ж1.1\nМинимальные размеры <script>\nусловно разрешённые' if p == 41 else 'Ж1\nДля индивидуального жилищного строительства'}
                      for p in range(start, min(start + pr.WINDOW, total + 1))]}


def params(**extra):
    return {'id': (store.get_setting('planning_regulations_trudovoe') or {}).get('id'),
            'planning_id': store.get_setting('planning_watch_trudovoe')['id'], **extra}


def test_resumable_windows_do_not_stop_at_200_and_keep_download_dates(catalog):
    catalog['items'][0]['total_pages'] = 507
    store.set_setting('planning_watch_trudovoe', catalog)
    first = None
    for _ in range(13):
        result = pr.read('trudovoe', params())
        if first is None:
            first = pr.report('trudovoe')['result']['id']
    assert result['total_pages'] == result['processed_pages'] == 507
    assert result['remaining_pages'] == 0 and result['complete_documents'] == 1
    snapshot = pr.report('trudovoe')['result']
    assert len(snapshot['documents'][0]['windows']) == 13
    assert snapshot['documents'][0]['received_at'] == 'original-download-date'
    assert not snapshot['legal_status_confirmed'] and not snapshot['candidate_zone_confirmed']
    assert pr.counts(pr.load('trudovoe', first))['processed_pages'] == 40


def test_exact_zone_mentions_do_not_merge_subzones_and_pages_are_pdf_positions(catalog):
    pr.read('trudovoe', params()); pr.read('trudovoe', params())
    subzone = pr.search('trudovoe', 'ж1.1', 'МИНИМАЛЬНЫЕ   размеры')
    assert subzone['total_matches'] == 1 and subzone['matches'][0]['page'] == 41
    assert subzone['matches'][0]['received_at'] == 'original-download-date'
    mainzone = pr.search('trudovoe', 'Ж1')
    assert mainzone['total_matches'] == 79 and mainzone['next_offset'] == 50
    assert len(mainzone['matches']) == 50
    second = pr.search('trudovoe', 'Ж1', offset=50, version=mainzone['id'])
    assert len(second['matches']) == 29 and second['next_offset'] is None
    assert all(x['page'] != 41 for x in mainzone['matches'] + second['matches'])
    assert pr.search('trudovoe')['total_matches'] == 0


def test_short_zone_codes_are_not_russian_conjunctions(catalog, monkeypatch):
    def sample(sha, start, total):
        value = fake_window(sha, start, total)
        value['pages'][0]['text'] = 'ЧАСТЬ I. ПРАВИЛА И ПОРЯДОК. Если то или и — слово.'
        value['pages'][1]['text'] = 'И. Зона размещения объектов инженерной инфраструктуры'
        value['pages'][2]['text'] = 'Территория общего пользования (ТО)'
        return value
    monkeypatch.setattr(pr, 'read_window', sample)
    pr.read('trudovoe', params())
    assert [m['page'] for m in pr.search('trudovoe', 'И')['matches']] == [2]
    assert [m['page'] for m in pr.search('trudovoe', 'ТО')['matches']] == [3]


def test_html_escapes_source_text_and_links_to_exact_dated_page(catalog):
    pr.read('trudovoe', params()); pr.read('trudovoe', params())
    raw = pr.html_report('trudovoe', 'Ж1.1').decode()
    assert '&lt;script&gt;' in raw and '<script>' not in raw
    assert 'https://simfmo-rk.ru/test.pdf#page=41' in raw
    assert '/api/planning/regulations/pdf?version=' in raw
    assert 'name="version"' in raw and 'original-download-date' in raw
    assert 'не сводная действующая редакция' in raw


def test_cached_pdf_is_exact_version_and_never_arbitrary_path(catalog):
    raw = b'%PDF-observed-official-file'
    sha = hashlib.sha256(raw).hexdigest()
    catalog['items'][0]['sha256'] = sha
    store.set_setting('planning_watch_trudovoe', catalog)
    folder = store.DATA / 'planning_watch'; folder.mkdir()
    path = folder / (sha + '.pdf'); path.write_bytes(raw)
    pr.read('trudovoe', params())
    version = pr.report('trudovoe')['result']['id']
    assert pr.cached_pdf('trudovoe', version, 'document') == raw
    with pytest.raises(ValueError, match='не найден'):
        pr.cached_pdf('trudovoe', version, '../secret')
    path.write_bytes(raw + b'changed')
    with pytest.raises(ValueError, match='изменился'):
        pr.cached_pdf('trudovoe', version, 'document')


def test_error_preserves_previous_windows_no_automatic_retry(catalog, monkeypatch):
    pr.read('trudovoe', params())
    before = pr.report('trudovoe')['result']
    calls = []
    def fail(*args):
        calls.append(args); raise ValueError('disk/read failure')
    monkeypatch.setattr(pr, 'read_window', fail)
    assert pr.read('trudovoe', params())['errors'] == 1
    assert pr.read('trudovoe', params())['new_pages'] == 0
    assert len(calls) == 1
    assert pr.report('trudovoe')['result']['documents'][0]['windows'] == before['documents'][0]['windows']
    monkeypatch.setattr(pr, 'read_window', fake_window)
    assert pr.read('trudovoe', params(retry=True))['new_pages'] == 40


def test_failed_document_is_attempted_once_in_explicit_retry_run(catalog, monkeypatch):
    monkeypatch.setattr(pr, 'MAX_WINDOWS', 20)
    calls = []
    def fail(*args):
        calls.append(args); raise ValueError('cannot read')
    monkeypatch.setattr(pr, 'read_window', fail)
    assert pr.read('trudovoe', params(retry=True))['errors'] == 1
    assert len(calls) == 1


def test_changed_window_is_rejected_by_search_and_resume(catalog):
    pr.read('trudovoe', params())
    row = pr.report('trudovoe')['result']['documents'][0]
    path = store.DATA / 'planning_regulations' / pr.ALGORITHM / row['sha256'] / '1.json'
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='изменилась'):
        pr.search('trudovoe', 'Ж1')
    result = pr.read('trudovoe', params())
    assert result['errors'] == 1 and result['processed_pages'] == 40 and result['remaining_pages'] == 45


def test_catalog_change_makes_old_index_stale_and_old_links_stay_dated(catalog):
    pr.read('trudovoe', params())
    first = pr.report('trudovoe')['result']['id']
    catalog['id'] = 'catalog-v2'
    store.set_setting('planning_watch_trudovoe', catalog)
    assert pr.report('trudovoe', first)['stale']
    pr.read('trudovoe', params())
    fresh = pr.report('trudovoe')
    assert not fresh['stale'] and fresh['counts']['processed_pages'] == 80
    assert pr.search('trudovoe', 'Ж1', version=first)['counts']['processed_pages'] == 40


def test_complete_same_bytes_are_reused_when_listing_metadata_changes(catalog, monkeypatch):
    for _ in range(3):
        pr.read('trudovoe', params())
    first = pr.report('trudovoe')['result']['id']
    catalog['id'] = 'catalog-v2'
    catalog['items'][0]['title'] = 'Новое название в перечне'
    store.set_setting('planning_watch_trudovoe', catalog)
    monkeypatch.setattr(pr, 'read_window', lambda *args: pytest.fail('Verified unchanged bytes must not be reread'))
    result = pr.read('trudovoe', params())
    current = pr.report('trudovoe')
    assert result['new_pages'] == 0 and current['counts']['processed_pages'] == 85
    assert current['result']['id'] != first and not current['stale']
    assert current['result']['documents'][0]['title'] == 'Новое название в перечне'
    assert pr.load('trudovoe', first)['documents'][0]['title'] != 'Новое название в перечне'


def test_concurrent_catalog_change_cannot_publish_as_current(catalog, monkeypatch):
    pr.read('trudovoe', params())
    before = pr.report('trudovoe')['result']
    def changed(*args):
        catalog['id'] = 'catalog-v2'; store.set_setting('planning_watch_trudovoe', catalog)
        return fake_window(*args)
    monkeypatch.setattr(pr, 'read_window', changed)
    with pytest.raises(ValueError, match='во время чтения'):
        pr.read('trudovoe', params())
    assert pr.report('trudovoe')['result']['id'] == before['id']


def test_stale_payload_and_bad_params_do_not_read(catalog):
    with pytest.raises(ValueError, match='изменился'):
        pr.read('trudovoe', {'id': None, 'planning_id': 'wrong'})
    with pytest.raises(ValueError, match='повтора'):
        pr.read('trudovoe', params(retry='yes'))
    for zone in ('Ж', 'Ж1<script>', '../file', 'Ж1.1.'):
        with pytest.raises(ValueError, match='Код зоны'):
            pr.search('trudovoe', zone)
    with pytest.raises(ValueError, match='Пределы'):
        pr.search('trudovoe', query='x' * 121)
    with pytest.raises(ValueError, match='версия'):
        pr.load('trudovoe', '../secret')


def test_mismatched_page_window_is_never_cached(catalog, monkeypatch):
    value = fake_window('a' * 64, 1, 85)
    value['pages'][5]['page'] = 900
    monkeypatch.setattr(pr, 'read_window', lambda *args: pr.validate_window(value, *args))
    assert pr.read('trudovoe', params())['errors'] == 1
    assert not list((store.DATA / 'planning_regulations' / pr.ALGORITHM).glob('**/*.json'))


def test_real_worker_reads_only_40_pages_and_checks_source_hash(tmp_path):
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(41):
        writer.add_blank_page(width=100, height=100)
    path = tmp_path / 'sample.pdf'
    with path.open('wb') as stream:
        writer.write(stream)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    result = worker.extract(path, sha, 1)
    assert result['total_pages'] == 41 and len(result['pages']) == 40
    last = worker.extract(path, sha, 41)
    assert len(last['pages']) == 1 and last['pages'][0]['page'] == 41
    with pytest.raises(ValueError, match='SHA-256'):
        worker.extract(path, 'a' * 64, 1)
    with pytest.raises(ValueError, match='вне пределов'):
        worker.extract(path, sha, 42)


def test_isolated_timeout_stops_worker(catalog, monkeypatch):
    def timeout(*args, **kw):
        assert kw['timeout'] == 60
        raise subprocess.TimeoutExpired(args[0], 60)
    monkeypatch.setattr(pr.subprocess, 'run', timeout)
    with pytest.raises(ValueError, match='60 секунд'):
        # Use the real wrapper rather than the fixture's fake reader.
        ORIGINAL_READ_WINDOW('a' * 64, 1, 85)


def test_dossier_link_pins_index_and_does_not_claim_usage_permission(catalog):
    pr.read('trudovoe', params())
    snapshot = pr.report('trudovoe')['result']
    source = {'id': snapshot['id'], 'counts': pr.counts(snapshot), 'catalog_matches': True}
    result = {'parameters': {'purpose': 'housing'}, 'sources': {'regulations': source}}
    section = pr.dossier_section(result)
    assert 'version=' + snapshot['id'] in section and 'q=' in section
    assert 'Зона этого контура не установлена' in section
    source['catalog_matches'] = False
    assert 'прежней версии каталога' in pr.dossier_section(result)


ORIGINAL_READ_WINDOW = pr.read_window
