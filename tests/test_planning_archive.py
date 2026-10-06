import copy
import hashlib
import json
import pytest
from land import planning_archive as pa, planning_watch as pw, store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path); store.init(); return tmp_path


def configure(monkeypatch, n=12):
    root = ('<article>' + ''.join(f'<a href="/session-{i}/">Сессия №{i} от 01.01.2024</a>' for i in range(n)) + '</article>').encode()
    page = '<article><ul><li><a href="/pzz.pdf">Правила землепользования Трудовского поселения</a> Дата публикации 02.01.2024</li></ul></article>'.encode()
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        return (root if url == pa.ROOT_URL else page), 'text/html', 200
    monkeypatch.setattr(pa, 'fetch', fetch)
    return calls, fetch


def scan(params=None):
    old = store.get_setting('planning_archive_trudovoe')
    return pa.scan('trudovoe', {'id': (old or {}).get('id'), **(params or {})})


def test_bounded_resume_preserves_source_dates_and_never_downloads_pdf(db, monkeypatch):
    calls, _ = configure(monkeypatch)
    first = scan(); before = store.get_setting('planning_archive_trudovoe')
    assert first['received'] == 10 and first['remaining'] == 2 and len(calls) == 11
    second = scan(); after = store.get_setting('planning_archive_trudovoe')
    assert second['processed'] == 2 and second['all_observed_pages_received']
    assert before['pages'][:10] == after['pages'][:10] and calls.count(pa.ROOT_URL) == 1
    assert not any(u.endswith('.pdf') for u in calls)
    third = scan(); assert third['id'] == after['id'] and third['processed'] == 0
    assert not after['complete'] and not after['legal_status_confirmed'] and not after['geometry_confirmed']


def test_denied_page_stops_batch_and_failure_is_not_automatically_retried(db, monkeypatch):
    calls, good = configure(monkeypatch)
    def denied(url, **kwargs):
        if url.endswith('/session-0/'):
            calls.append(url); raise ValueError('HTTP 403: доступ ограничен')
        return good(url, **kwargs)
    monkeypatch.setattr(pa, 'fetch', denied)
    result = scan(); assert result['processed'] == 1 and result['errors'] == 1 and result['remaining'] == 11
    monkeypatch.setattr(pa, 'fetch', good)
    scan(); scan()
    assert calls.count('https://simfmo-rk.ru/session-0/') == 1
    before = store.get_setting('planning_archive_trudovoe')
    assert scan()['id'] == before['id']
    result = scan({'retry': True}); assert result['processed'] == 1 and result['errors'] == 0


def test_refresh_failure_keeps_last_successful_snapshot(db, monkeypatch):
    configure(monkeypatch, 1); scan(); old = store.get_setting('planning_archive_trudovoe')
    monkeypatch.setattr(pa, 'fetch', lambda *a, **k: (_ for _ in ()).throw(ValueError('HTTP 403')))
    with pytest.raises(ValueError, match='403'): scan({'refresh': True})
    assert store.get_setting('planning_archive_trudovoe') == old


def test_changed_cached_page_or_unobserved_link_blocks_catalog(db, monkeypatch):
    configure(monkeypatch, 1); scan(); old = store.get_setting('planning_archive_trudovoe')
    page = old['pages'][0]; path = db / 'planning_archive' / (page['sha256'] + '.html')
    raw = path.read_bytes(); path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='страница архива изменилась'): pa.links('trudovoe')
    path.write_bytes(raw)
    changed = copy.deepcopy(old); changed['pages'][0]['links'][0]['url'] = 'https://simfmo-rk.ru/invented.pdf'
    changed.pop('id'); changed['id'] = pw.digest(changed)[:20]; store.set_setting('planning_archive_trudovoe', changed)
    with pytest.raises(ValueError, match='не совпадают'): pa.links('trudovoe')


def test_snapshot_hash_and_root_chain_checked(db, monkeypatch):
    configure(monkeypatch, 1); scan(); old = store.get_setting('planning_archive_trudovoe')
    changed = copy.deepcopy(old); changed['pages'][0]['title'] = 'changed'
    store.set_setting('planning_archive_trudovoe', changed)
    with pytest.raises(ValueError, match='проверка архива изменилась'): pa.links('trudovoe')
    changed.pop('id'); changed['id'] = pw.digest(changed)[:20]
    store.set_setting('planning_archive_trudovoe', changed)
    with pytest.raises(ValueError, match='исходным перечнем'): pa.links('trudovoe')


def test_catalog_imports_only_observed_archive_pzz_with_original_dates(db, monkeypatch):
    calls, _ = configure(monkeypatch, 2); scan(); source = store.get_setting('planning_archive_trudovoe')
    monkeypatch.setattr(pw, 'SOURCES', ())
    pw.catalog('trudovoe'); catalog = store.get_setting('planning_watch_trudovoe')
    assert len(catalog['items']) == 1 and len(catalog['items'][0]['listing_references']) == 2
    row = catalog['items'][0]; ref = row['listing_references'][0]
    assert ref['publication_date'] == '02.01.2024' and ref['listed_at'] == source['pages'][0]['received_at']
    assert pw.is_pzz(row) and row['currently_listed'] and catalog['archive_source_id'] == source['id']
    assert not pw.report('trudovoe')['archive_stale'] and len(calls) == 3
    scan({'refresh': True})
    assert pw.report('trudovoe')['archive_stale']


def test_retained_failure_does_not_claim_fresh_listing(db, monkeypatch):
    configure(monkeypatch, 1); scan(); source = store.get_setting('planning_archive_trudovoe')
    calls, good = configure(monkeypatch, 1)
    def failing(url, **kw):
        if url != pa.ROOT_URL: raise ValueError('HTTP 403')
        return good(url, **kw)
    monkeypatch.setattr(pa, 'fetch', failing); scan({'refresh': True})
    _, records, found = pa.links('trudovoe'); ref = found['https://simfmo-rk.ru/pzz.pdf'][0]
    assert ref['listed_at'] == source['pages'][0]['received_at'] and ref['observation_state'] == 'error'
    monkeypatch.setattr(pw, 'SOURCES', ()); pw.catalog('trudovoe')
    row = store.get_setting('planning_watch_trudovoe')['items'][0]
    assert not row['currently_listed'] and row['listing_state'] == 'unknown'


def test_changed_archive_during_catalog_fetch_prevents_replacement(db, monkeypatch):
    configure(monkeypatch, 1); scan(); old = {'id': 'catalog', 'items': []}; store.set_setting('planning_watch_trudovoe', old)
    monkeypatch.setattr(pw, 'SOURCES', ({'id': 'test', 'url': 'https://simfmo-rk.ru/test/'},))
    def fetch(*a, **kw):
        store.set_setting('planning_archive_trudovoe', {'id': 'changed'})
        return b'<article></article>', 'text/html', 200
    monkeypatch.setattr(pw, 'fetch', fetch)
    with pytest.raises(ValueError, match='Архив ПЗЗ изменился'): pw.catalog('trudovoe')
    assert store.get_setting('planning_watch_trudovoe') == old


def test_stale_request_and_session_limit_do_not_replace_source(db, monkeypatch):
    configure(monkeypatch, 1); scan(); old = store.get_setting('planning_archive_trudovoe')
    with pytest.raises(ValueError, match='Архив|архива'): pa.scan('trudovoe', {'id': 'stale'})
    configure(monkeypatch, pa.MAX_PAGES + 1)
    with pytest.raises(ValueError, match='Предел'): scan({'refresh': True})
    assert store.get_setting('planning_archive_trudovoe') == old


def test_wrong_structure_and_external_links_are_not_treated_as_empty_archive(db, monkeypatch):
    monkeypatch.setattr(pa, 'fetch', lambda *a, **kw: (b'<html>Access blocked</html>', 'text/html', 200))
    with pytest.raises(ValueError, match='Структура'): scan()
    assert not store.get_setting('planning_archive_trudovoe')
    rows = pa.sessions('<article><a href="https://evil.invalid/a/">Сессия 1</a><a href="/a/?token=x">Сессия 2</a></article>'.encode())
    assert rows == []


def test_changed_archive_during_scan_prevents_replacement(db, monkeypatch):
    _, good = configure(monkeypatch, 12); scan(); current = store.get_setting('planning_archive_trudovoe')
    def fetch(*a, **kw):
        store.set_setting('planning_archive_trudovoe', dict(current, id='newer'))
        return good(*a, **kw)
    monkeypatch.setattr(pa, 'fetch', fetch)
    with pytest.raises(ValueError, match='Архив изменился'): scan()
    assert store.get_setting('planning_archive_trudovoe')['id'] == 'newer'


def test_invalid_parameters_do_not_fetch(db, monkeypatch):
    calls, _ = configure(monkeypatch)
    with pytest.raises(ValueError, match='Трудового'): pa.scan('unknown', {})
    with pytest.raises(ValueError, match='параметры'): scan({'retry': 'yes'})
    assert calls == []
