import copy
import hashlib
import json
import subprocess
import pytest
from land import planning_watch as pw, store

BASE = 'https://simfmo-rk.ru/'


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    return tmp_path


def listing(title='СПК «Коммунальник»', url='a.pdf', date='02.02.2026'):
    return f'<article><ul><li><a href="{url}">{title}</a> Дата публикации {date}</li></ul></article>'.encode()


def test_listing_dates_are_local_and_external_links_ignored():
    raw = (b'<nav><a href="/nav.pdf">x</a></nav>' + listing()[:-10]
           + '<li><a href="b.pdf">СНТ «Труд»</a></li><li><a href="https://evil.invalid/a.pdf">x</a></li></article>'.encode())
    rows = pw.parse_links(raw, BASE)
    assert len(rows) == 2 and rows[0]['publication_date'] == '02.02.2026' and rows[1]['publication_date'] is None
    with pytest.raises(ValueError, match='Структура'):
        pw.parse_links(b'<html>Access denied</html>', BASE)


@pytest.mark.parametrize('url', ['http://simfmo-rk.ru/x', 'https://simfmo-rk.ru.evil.invalid/x', 'https://x@simfmo-rk.ru/x', '/x?token=abc', '/x#fragment', 'https://simfmo-rk.ru:444/x'])
def test_only_public_official_links(url):
    assert pw.safe_link(BASE, url) is None


def test_text_uses_own_header_and_operative_target():
    pages = [(1, 'ПОСТАНОВЛЕНИЕ\n04.07.2025 № 796-п\nСПК «Коммунальник»\nВ соответствии с постановлением от 12.02.2018 № 54-п\nПОСТАНОВЛЯЕТ:\n1. Отменить постановление от 15.08.2024 № 1072-п «Об утверждении документации».\n2. Разместить.')]
    result = pw.text_evidence(pages)
    assert result['act_identity'] == {'date': '2025-07-04', 'number': '796-п'}
    assert result['action'] == 'cancel_reference'
    assert result['references'][0]['number'] == '1072-п'
    assert not result['geometry_confirmed'] and not result['legal_status_confirmed']
    assert result['subjects'] == ['Коммунальник']


def test_new_approval_does_not_inherit_prior_cancellation():
    result = pw.text_evidence([(1, 'ПОСТАНОВЛЕНИЕ\n27.01.2026 № 42-п\nСПК «Коммунальник»\nПОСТАНОВЛЯЕТ:\n1. Утвердить документацию.\n2. Разместить.')])
    assert result['references'] == [] and result['action'] == 'approve_text'
    assert result['act_identity']['number'] == '42-п'


def test_word_date_and_draft_title_conflict():
    result = pw.text_evidence([(1, 'РЕШЕНИЕ\n19 августа 2026 года г. Симферополь № 442\nО внесении изменений\nрайонный совет решил:\n1. Внести в решение от 26.06.2019 № 1239 изменения.\n1.1. Разместить.')])
    assert result['act_identity'] == {'date': '2026-08-19', 'number': '442'}
    assert result['references'][0]['number'] == '1239'
    row = dict(result, listing_references=[{'title': 'Проект внесения изменений ПЗЗ Трудовского поселения', 'parent_url': BASE}])
    pw.compare_listing(row)
    assert row['listing_conflicts'][0]['kind'] == 'draft_title_act_text'
    draft = pw.text_evidence([(1, 'ПРОЕКТ\nРЕШЕНИЕ\n19.08.2026 № 442\nрешил:\n1. Внести изменения.')])
    assert draft['document_role'] == 'draft_text'


def test_base_act_in_listing_is_not_mistaken_for_own_identity():
    assert pw.listing_identity('В решение от 26.06.2019 № 1239 «Об утверждении правил»') is None
    assert pw.listing_identity('Проект внесения изменений в решение от 26.06.2019 № 1239') is None
    assert pw.listing_identity('№290 от 19.11.2025 «О внесении изменений в решение от 26.06.2019 № 1239»') == {'date': '2025-11-19', 'number': '290'}


def test_action_in_second_clause_is_retained_without_parsing_annex():
    result = pw.text_evidence([(1, 'РЕШЕНИЕ\n24 июня 2026 года № 413\nрайонный совет решил:\n1. Протест удовлетворить.\n2. Внести в решение от 26.06.2019 № 1239 изменения.\nПредседатель\nПодпись\nПриложение\n1. Отменить решение от 01.01.2020 № 99.')])
    assert result['action'] == 'amend_reference' and len(result['references']) == 1
    assert result['references'][0]['number'] == '1239'


def test_mislabeled_pdf_preserves_title_and_actual_identity():
    row = dict(pw.text_evidence([(1, 'ПОСТАНОВЛЕНИЕ\n27.01.2026 № 40-п\nСНТ «Труд»\nПОСТАНОВЛЯЕТ:\n1. Утвердить документацию.')]),
               listing_references=[{'title': 'ПОСТАНОВЛЕНИЕ от 27.01.2026 № 42-п СПК «Коммунальник»', 'parent_url': BASE}])
    pw.compare_listing(row)
    assert [x['kind'] for x in row['listing_conflicts']] == ['act_identity', 'subject']
    assert row['act_identity']['number'] == '40-п' and '42-п' in row['listing_references'][0]['title']


def configure(monkeypatch, raw=None):
    monkeypatch.setattr(pw, 'SOURCES', ({'id': 'planning', 'url': BASE + 'index/'},))
    monkeypatch.setattr(pw, 'fetch', lambda url, **kw: (raw or listing(), 'text/html', 200))


def test_catalog_failure_retains_previous_documents_without_fresh_success(workspace, monkeypatch):
    configure(monkeypatch)
    pw.catalog('trudovoe')
    before = store.get_setting('planning_watch_trudovoe')
    monkeypatch.setattr(pw, 'fetch', lambda *a, **kw: (_ for _ in ()).throw(ValueError('HTTP 403')))
    pw.catalog('trudovoe')
    r = store.get_setting('planning_watch_trudovoe')
    assert r['sources'][0]['state'] == 'error'
    assert r['sources'][0]['previous_observation']['checked_at'] == before['checked_at']
    assert r['items'][0]['listing_state'] == 'unknown' and not r['items'][0]['currently_listed']
    assert not r['complete'] and not r['legal_status_confirmed']


def test_pzz_root_error_does_not_claim_its_child_document_disappeared(workspace, monkeypatch):
    monkeypatch.setattr(pw, 'SOURCES', ({'id': 'pzz2026', 'url': BASE + 'year/'},))
    def receive(url, **kw):
        return (listing('Сессия 38 2026', 'session/') if url.endswith('/year/') else listing('Правил землепользования Трудовского поселения'), 'text/html', 200)
    monkeypatch.setattr(pw, 'fetch', receive)
    pw.catalog('trudovoe')
    monkeypatch.setattr(pw, 'fetch', lambda *a, **kw: (_ for _ in ()).throw(ValueError('blocked')))
    pw.catalog('trudovoe')
    assert store.get_setting('planning_watch_trudovoe')['items'][0]['listing_state'] == 'unknown'


def fake_read(sha):
    return {'source_sha256': sha, 'algorithm': pw.ALGORITHM, 'document_role': 'act_text', 'act_identity': {'date': '2026-01-27', 'number': '42-п'},
            'subjects': ['Коммунальник'], 'references': [], 'processed_pages': 2, 'total_pages': 2, 'geometry_confirmed': False, 'legal_status_confirmed': False}


def read_params():
    return {'id': store.get_setting('planning_watch_trudovoe')['id']}


def test_revision_refresh_same_bytes_dates_and_changed_document_history(workspace, monkeypatch):
    configure(monkeypatch)
    pw.catalog('trudovoe')
    monkeypatch.setattr(pw, 'read_pdf', fake_read)
    raw = b'%PDF-one'
    monkeypatch.setattr(pw, 'fetch', lambda *a, **kw: (raw, 'application/pdf', 200))
    assert pw.read('trudovoe', read_params())['processed'] == 1
    first = store.get_setting('planning_watch_trudovoe')['items'][0]
    assert pw.read('trudovoe', read_params())['processed'] == 0
    configure(monkeypatch)
    pw.catalog('trudovoe')
    assert pw.report('trudovoe')['result']['items'][0]['current_read_state'] == 'unchecked'
    monkeypatch.setattr(pw, 'fetch', lambda *a, **kw: (raw, 'application/pdf', 200))
    pw.read('trudovoe', read_params())
    second = store.get_setting('planning_watch_trudovoe')['items'][0]
    assert second['received_at'] == first['received_at'] and second.get('previous_versions', []) == []
    configure(monkeypatch)
    pw.catalog('trudovoe')
    monkeypatch.setattr(pw, 'fetch', lambda *a, **kw: (b'%PDF-two', 'application/pdf', 200))
    pw.read('trudovoe', read_params())
    third = store.get_setting('planning_watch_trudovoe')['items'][0]
    assert third['sha256'] != first['sha256'] and third['previous_versions'][0]['sha256'] == first['sha256']


def test_failed_recheck_preserves_pdf_and_does_not_repeat_implicitly(workspace, monkeypatch):
    configure(monkeypatch)
    pw.catalog('trudovoe')
    monkeypatch.setattr(pw, 'read_pdf', fake_read)
    monkeypatch.setattr(pw, 'fetch', lambda *a, **kw: (b'%PDF-one', 'application/pdf', 200))
    pw.read('trudovoe', read_params())
    old = copy.deepcopy(store.get_setting('planning_watch_trudovoe')['items'][0])
    configure(monkeypatch)
    pw.catalog('trudovoe')
    monkeypatch.setattr(pw, 'fetch', lambda *a, **kw: (b'<html>blocked</html>', 'text/html', 200))
    pw.read('trudovoe', read_params())
    row = pw.report('trudovoe')['result']['items'][0]
    assert row['state'] == 'read' and row['current_read_state'] == 'error' and row['sha256'] == old['sha256'] and row['received_at'] == old['received_at']
    assert pw.read('trudovoe', read_params())['processed'] == 0
    assert pw.read('trudovoe', dict(read_params(), retry=True))['processed'] == 1


def test_stale_id_and_concurrent_catalog_do_not_replace_results(workspace, monkeypatch):
    configure(monkeypatch)
    pw.catalog('trudovoe')
    with pytest.raises(ValueError, match='изменился'):
        pw.read('trudovoe', {'id': 'stale'})
    monkeypatch.setattr(pw, 'fetch', lambda *a, **kw: (b'%PDF-one', 'application/pdf', 200))
    def concurrent(sha):
        current = store.get_setting('planning_watch_trudovoe')
        current['id'] = 'other'
        store.set_setting('planning_watch_trudovoe', current)
        return fake_read(sha)
    monkeypatch.setattr(pw, 'read_pdf', concurrent)
    with pytest.raises(ValueError, match='во время чтения'):
        pw.read('trudovoe', read_params())
    assert store.get_setting('planning_watch_trudovoe')['id'] == 'other'


def test_report_mirror_is_hash_based_and_does_not_modify_other_sources(workspace, monkeypatch):
    configure(monkeypatch)
    pw.catalog('trudovoe')
    raw = b'%PDF-one'
    sha = hashlib.sha256(raw).hexdigest()
    municipal = {'items': [{'url': 'https://trudovskoe-rk.ru/m.pdf', 'sha256': sha, 'received_at': 'old'}]}
    store.set_setting('municipal_trudovoe', municipal)
    store.set_setting('survey_trudovoe', {'id': 'original'})
    monkeypatch.setattr(pw, 'read_pdf', fake_read)
    monkeypatch.setattr(pw, 'fetch', lambda *a, **kw: (raw, 'application/pdf', 200))
    pw.read('trudovoe', read_params())
    row = pw.report('trudovoe')['result']['items'][0]
    assert row['same_bytes_municipal'] == [{'url': municipal['items'][0]['url'], 'received_at': 'old'}]
    assert store.get_setting('municipal_trudovoe') == municipal and store.get_setting('survey_trudovoe') == {'id': 'original'}
    assert not store.candidates('trudovoe')
    assert '<script>' not in pw.html_report('trudovoe').decode()


def test_worker_limits_and_source_binding(workspace, monkeypatch):
    def output(*a, **kw):
        assert kw['timeout'] == 60
        return subprocess.CompletedProcess(a, 0, json.dumps({'source_sha256': 'wrong', 'algorithm': pw.ALGORITHM}), '')
    monkeypatch.setattr(pw.subprocess, 'run', output)
    with pytest.raises(ValueError, match='другому PDF'):
        pw.read_pdf('0' * 64)


def test_session_limit_preserves_unknown_scope(workspace, monkeypatch):
    monkeypatch.setattr(pw, 'SOURCES', ({'id': 'pzz2026', 'url': BASE + 'year/'},))
    raw = ('<article>' + ''.join(f'<li><a href="/s{i}/">Сессия {i} 2026</a></li>' for i in range(17)) + '</article>').encode()
    calls=[]
    def fetch(url, **kw):
        calls.append(url)
        return raw, 'text/html', 200
    monkeypatch.setattr(pw, 'fetch', fetch)
    pw.catalog('trudovoe')
    assert len(calls) == 1 and store.get_setting('planning_watch_trudovoe')['sources'][0]['state'] == 'error'


def test_actual_pdf_worker_bounds_and_tamper(workspace):
    from pypdf import PdfWriter
    import io
    writer = PdfWriter()
    for _ in range(201):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    raw = buffer.getvalue()
    sha = hashlib.sha256(raw).hexdigest()
    folder = workspace / 'planning_watch'
    folder.mkdir()
    path = folder / (sha + '.pdf')
    path.write_bytes(raw)
    result = pw.read_pdf(sha)
    assert result['processed_pages'] == 200 and result['unread_pages'] == 1
    assert len(result['image_or_sparse_pages']) == 200 and not result['text_layer_complete']
    path.write_bytes(raw + b'changed')
    with pytest.raises(ValueError, match='SHA-256'):
        pw.read_pdf(sha)


def test_worker_timeout_is_explicit(workspace, monkeypatch):
    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired(a, 60)
    monkeypatch.setattr(pw.subprocess, 'run', timeout)
    with pytest.raises(ValueError, match='60 секунд'):
        pw.read_pdf('0' * 64)


def test_timeline_deduplicates_same_bytes_without_cancelling_new_approval(workspace, monkeypatch):
    def row(number, date, action, sha, url):
        return {'id': number, 'url': BASE + url, 'title': 'акт', 'sha256': sha, 'act_identity': {'date': date, 'number': number},
                'action': action, 'subjects': ['Коммунальник'], 'document_role': 'act_text', 'references': [],
                'listing_references': [{'title': 'акт', 'parent_url': BASE}], 'read_revision': 'rev', 'read_attempt': {'state': 'received'}}
    items = [row('42-п','2026-01-27','approve_text','a','new.pdf'), row('796-п','2025-07-04','cancel_reference','b','old.pdf'),
             row('42-п','2026-01-27','approve_text','a','mirror.pdf')]
    store.set_setting('planning_watch_trudovoe', {'items': items, 'catalog_revision': 'rev'})
    data = pw.report('trudovoe')
    entries = data['timelines'][0]['entries']
    assert len(entries) == 2 and entries[0]['number'] == '796-п' and entries[1]['number'] == '42-п'
    assert len(entries[1]['urls']) == 2 and not data['timelines'][0]['legal_status_confirmed']
