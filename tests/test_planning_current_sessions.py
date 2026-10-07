import hashlib
import copy
import json

import pytest

from land import planning_watch as pw, store


ROOT = 'https://simfmo-rk.ru/resheniya-rajonnogo-soveta-iii-sozyva/'
SESSION = 'https://simfmo-rk.ru/sessiya-38-ot-19-08-2026/'
PDF = 'https://simfmo-rk.ru/wp-content/uploads/2026/08/442_19.08.2026.pdf'
TITLE = ('№442 от 19.08.2026 «О внесении изменений в решение от 26.06.2019 № 1239 '
         '«Об утверждении правил землепользования и застройки Трудовского сельского поселения»»')


def listing(rows):
    return ('<article><ul>' + ''.join(
        f'<li><a href="{url}">{title}</a> Дата публикации {date}</li>'
        for url, title, date in rows) + '</ul></article>').encode()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    monkeypatch.setattr(pw, 'SOURCES', ({'id': 'council2026', 'url': ROOT},))
    return tmp_path


def test_common_council_source_is_the_observed_official_index():
    assert [x['url'] for x in pw.SOURCES if x['id'] == 'council2026'] == [ROOT]
    assert pw.MAX_SESSIONS == 16


def test_current_session_discovers_act_with_both_listing_hashes(workspace, monkeypatch):
    root = listing([(SESSION, 'Сессия №38 от 19.08.2026', '19.08.2026'),
                    (SESSION, 'Сессия №38 от 19.08.2026', '19.08.2026'),
                    ('/old/', 'Сессия №21 от 19.11.2025', '19.11.2025')])
    child = listing([(PDF, TITLE, '19.08.2026'),
                     ('/other.pdf', 'ПЗЗ Перовского поселения', '19.08.2026'),
                     ('/decision.pdf', 'Бюджет Трудовского сельского поселения', '19.08.2026'),
                     ('https://external.invalid/trud.pdf', TITLE, '19.08.2026')])
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        assert kwargs['max_bytes'] == 4 * 1024 * 1024
        return {ROOT: root, SESSION: child}[url], 'text/html', 200

    monkeypatch.setattr(pw, 'fetch', fetch)
    result = pw.catalog('trudovoe')
    data = store.get_setting('planning_watch_trudovoe')
    assert calls == [ROOT, SESSION] and result['documents'] == 1
    row = data['items'][0]
    ref = row['listing_references'][0]
    assert row['url'] == PDF and row['currently_listed'] and row['state'] == 'pending'
    assert pw.is_pzz(row) and ref['planning_kind'] == 'pzz'
    assert ref['root_url'] == ROOT and ref['parent_url'] == SESSION
    assert ref['publication_date'] == ref['session_publication_date'] == '19.08.2026'
    assert ref['listing_sha256'] == hashlib.sha256(child).hexdigest()
    assert ref['root_listing_sha256'] == hashlib.sha256(root).hexdigest()
    assert ref['listed_at'] == data['sources'][1]['checked_at']
    assert ref['root_listed_at'] == data['sources'][0]['checked_at']
    assert (workspace / 'planning_watch' / (ref['listing_sha256'] + '.html')).read_bytes() == child
    assert not data['complete'] and not data['legal_status_confirmed'] and not data['geometry_confirmed']


def test_common_root_limit_rejects_whole_session_queue(workspace, monkeypatch):
    raw = listing([(f'/s{i}/', f'Сессия №{i} от 19.08.2026', '') for i in range(17)])
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return raw, 'text/html', 200

    monkeypatch.setattr(pw, 'fetch', fetch)
    pw.catalog('trudovoe')
    data = store.get_setting('planning_watch_trudovoe')
    assert calls == [ROOT] and data['items'] == []
    assert data['sources'][0]['state'] == 'error'
    assert 'не обходился частично' in data['sources'][0]['error']


def test_common_root_failure_retains_old_document_as_unknown(workspace, monkeypatch):
    root = listing([(SESSION, 'Сессия №38 от 19.08.2026', '')])
    child = listing([(PDF, TITLE, '19.08.2026')])
    monkeypatch.setattr(pw, 'fetch', lambda url, **kw: ({ROOT: root, SESSION: child}[url], 'text/html', 200))
    pw.catalog('trudovoe')
    previous = store.get_setting('planning_watch_trudovoe')
    calls = []

    def blocked(url, **kwargs):
        calls.append(url)
        raise ValueError('HTTP 403: доступ ограничен')

    monkeypatch.setattr(pw, 'fetch', blocked)
    monkeypatch.setattr(pw, 'SOURCES', ({'id': 'council2026', 'url': ROOT},
                                     {'id': 'planning', 'url': 'https://simfmo-rk.ru/another/'}))
    pw.catalog('trudovoe')
    data = store.get_setting('planning_watch_trudovoe')
    assert calls == [ROOT]
    row = data['items'][0]
    assert row['url'] == PDF and not row['currently_listed'] and row['listing_state'] == 'unknown'
    assert row['listing_references'] == previous['items'][0]['listing_references']
    assert data['sources'][0]['previous_observation']['sha256'] == previous['sources'][0]['sha256']
    assert all(x['state'] == 'error' for x in data['sources'])


def test_challenge_stops_remaining_session_requests(workspace, monkeypatch):
    second = 'https://simfmo-rk.ru/second-session/'
    root = listing([(SESSION, 'Сессия №38 от 19.08.2026', ''),
                    (second, 'Сессия №39 от 16.09.2026', '')])
    challenge = '<title>Включена DDos защита</title><article>Подтвердите проверку</article>'.encode()
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return (root if url == ROOT else challenge), 'text/html', 200

    monkeypatch.setattr(pw, 'fetch', fetch)
    pw.catalog('trudovoe')
    data = store.get_setting('planning_watch_trudovoe')
    assert calls == [ROOT, SESSION]
    assert data['sources'][1]['state'] == data['sources'][2]['state'] == 'error'
    assert data['items'] == [] and not data['complete']


def test_general_session_document_participates_in_pzz_read_scope(workspace, monkeypatch):
    root = listing([(SESSION, 'Сессия №38 от 19.08.2026', '')])
    child = listing([(PDF, TITLE, '19.08.2026')])
    monkeypatch.setattr(pw, 'fetch', lambda url, **kw: ({ROOT: root, SESSION: child}[url], 'text/html', 200))
    pw.catalog('trudovoe')
    data = store.get_setting('planning_watch_trudovoe')
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return b'%PDF-test-current-act', 'application/pdf', 200

    monkeypatch.setattr(pw, 'fetch', fetch)
    monkeypatch.setattr(pw, 'read_pdf', lambda sha: {'source_sha256': sha, 'algorithm': pw.ALGORITHM,
        'document_role': 'act_text', 'act_identity': {'date': '2026-08-19', 'number': '442'},
        'action': 'amend_reference', 'subjects': [], 'references': [],
        'geometry_confirmed': False, 'legal_status_confirmed': False})
    assert pw.read('trudovoe', {'id': data['id'], 'scope': 'pzz'})['processed'] == 1
    row = store.get_setting('planning_watch_trudovoe')['items'][0]
    assert calls == [PDF] and row['act_identity']['number'] == '442'
    assert row['listing_conflicts'] == []
    assert not row['geometry_confirmed'] and not row['legal_status_confirmed']


def unread_catalog(count=3):
    return {'id': 'before', 'catalog_revision': 'fresh', 'items': [
        {'id': str(i), 'url': f'https://simfmo-rk.ru/file-{i}.pdf', 'state': 'pending',
         'currently_listed': True, 'listing_references': [
             {'parent_url': SESSION, 'root_url': ROOT, 'planning_kind': 'pzz', 'title': TITLE}]}
        for i in range(count)]}


def parsed_pdf(sha):
    return {'source_sha256': sha, 'algorithm': pw.ALGORITHM, 'document_role': 'act_text',
            'act_identity': {'date': '2026-08-19', 'number': '442'}, 'action': 'amend_reference',
            'references': [], 'subjects': [], 'geometry_confirmed': False, 'legal_status_confirmed': False}


@pytest.mark.parametrize('unread_state', ['pending', 'error'])
def test_unread_source_precedes_old_read_sources_on_catalog_refresh(workspace, monkeypatch, unread_state):
    data = unread_catalog(4)
    raw = b'%PDF-test-priority'
    for row in data['items'][:3]:
        row.update(state='read', read_revision='old', sha256=hashlib.sha256(raw).hexdigest(),
                   received_at='2026-10-01T00:00:00+00:00')
    data['items'][3]['state'] = unread_state
    store.set_setting('planning_watch_trudovoe', data)
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return raw, 'application/pdf', 200

    monkeypatch.setattr(pw, 'fetch', fetch)
    monkeypatch.setattr(pw, 'read_pdf', parsed_pdf)
    result = pw.read('trudovoe', {'id': data['id'], 'scope': 'pzz'})
    assert calls == [data['items'][3]['url'], data['items'][0]['url'], data['items'][1]['url']]
    assert result == {'processed': 3, 'remaining': 1}
    saved = store.get_setting('planning_watch_trudovoe')
    assert saved['items'][2] == data['items'][2]
    assert saved['items'][0]['received_at'] == data['items'][0]['received_at']


@pytest.mark.parametrize('status', [401, 403, 429])
@pytest.mark.parametrize('failure_index', [0, 1])
def test_access_rejection_stops_pdf_batch_and_preserves_unattempted_sources(workspace, monkeypatch, status, failure_index):
    data = unread_catalog(3)
    originals = copy.deepcopy(data['items'])
    store.set_setting('planning_watch_trudovoe', data)
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        if url == originals[failure_index]['url']:
            raise ValueError(f'HTTP {status}: доступ ограничен')
        return b'%PDF-success-before-block', 'application/pdf', 200

    monkeypatch.setattr(pw, 'fetch', fetch)
    monkeypatch.setattr(pw, 'read_pdf', parsed_pdf)
    result = pw.read('trudovoe', {'id': data['id'], 'scope': 'pzz'})
    assert calls == [r['url'] for r in originals[:failure_index + 1]]
    assert result == {'processed': failure_index + 1, 'remaining': 2 - failure_index}
    saved = store.get_setting('planning_watch_trudovoe')
    assert saved['items'][failure_index + 1:] == originals[failure_index + 1:]
    rejected = saved['items'][failure_index]
    assert rejected['read_revision'] == 'fresh' and rejected['state'] == 'error'
    assert rejected['read_attempt']['state'] == 'error' and f'HTTP {status}' in rejected['read_attempt']['error']
    with store.connect() as db:
        event = json.loads(db.execute("SELECT body FROM events WHERE kind='planning_read' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert event['processed'] == failure_index + 1 and event['errors'] == 1


def test_parse_failure_does_not_stop_an_unrelated_pdf_in_batch(workspace, monkeypatch):
    data = unread_catalog(2)
    store.set_setting('planning_watch_trudovoe', data)
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return (b'<html>invalid document</html>' if url == data['items'][0]['url'] else b'%PDF-valid'), 'application/pdf', 200

    monkeypatch.setattr(pw, 'fetch', fetch)
    monkeypatch.setattr(pw, 'read_pdf', parsed_pdf)
    assert pw.read('trudovoe', {'id': data['id'], 'scope': 'pzz'}) == {'processed': 2, 'remaining': 0}
    saved = store.get_setting('planning_watch_trudovoe')
    assert len(calls) == 2 and saved['items'][0]['state'] == 'error' and saved['items'][1]['state'] == 'read'
