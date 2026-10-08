import copy
import hashlib
import json
import pytest
from land import land_status, municipal, store

TITLE = 'Постановление об утверждении регламента предварительного согласования предоставления земельного участка'
ROOT = municipal.SOURCES[-1]['url']


def act(number, date, body):
    return [(1, f'ГЛАВА\nПОСТАНОВЛЕНИЕ\n{date} № {number}\nОб утверждении регламента\nПОСТАНОВЛЯЮ:\n{body}\nГлава администрации')]


def test_plural_cancellation_is_two_direct_targets_not_quoted_citations():
    pages = act('1144', '05 октября 2026 года', '''1. Утвердить регламент.
2. Признать утратившими силу постановления главы администрации:
- от 13.11.2024 № 1213 «Об утверждении регламента»;
- от 27.01.2025 № 66 «О внесении изменений в акт от 13.11.2024 № 1213».
3. Опубликовать.''')
    own, refs = land_status.text_relations(pages)
    assert own == {'date': '2026-10-05', 'number': '1144'}
    assert [r['target'] for r in refs] == [{'date': '2024-11-13', 'number': '1213'}, {'date': '2025-01-27', 'number': '66'}]
    assert all(r['page'] == 1 and not r['legal_effect_confirmed'] for r in refs)


def test_quoted_target_inside_cancellation_does_not_cancel_its_base():
    pages = act('1076', '04 сентября 2026 года', '1. Признать утратившим силу постановление от 06.05.2016 № 318 «Об изменениях в постановление от 25.03.2016 № 225».')
    assert [r['target']['number'] for r in land_status.text_relations(pages)[1]] == ['318']


@pytest.mark.parametrize('body', ['1. Опубликовать сведения о признании утратившим силу постановления от 01.01.2020 № 5.',
                                 '1. Отказать в признании утратившим силу постановления от 01.01.2020 № 5.'])
def test_cancellation_mention_is_not_an_operative_cancellation(body):
    assert not land_status.text_relations(act('1', '01.02.2026', body))[1]


def test_no_own_header_never_inherits_target_identity():
    pages = [(1, 'О признании утратившим силу постановления от 01.01.2020 № 5\nПОСТАНОВЛЯЮ:\n1. Признать утратившим силу постановление от 01.01.2020 № 5.')]
    assert land_status.text_relations(pages) == (None, [])


def test_quoted_calendar_day_in_header_and_second_page_cancellation():
    pages = [(1, 'ПОСТАНОВЛЕНИЕ\n«5» марта 2016г. №225\nОб утверждении регламента\nПОСТАНОВЛЯЮ:\n1. Утвердить.'),
             (2, '2. Признать утратившим силу постановление от 02.01.2015 № 1.\nГлава администрации')]
    own, refs = land_status.text_relations(pages)
    assert own == {'date': '2016-03-05', 'number': '225'} and refs[0]['page'] == 2


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    links = [{'url': 'https://trudovskoe-rk.ru/a.pdf', 'title': TITLE},
             {'url': 'https://trudovskoe-rk.ru/b.pdf', 'title': TITLE + ' новый'}]
    raw = ('<article>' + ''.join('<a href="' + r['url'] + '">' + r['title'] + '</a>' for r in links) + '</article>').encode()
    sha = municipal.save_raw(raw, 'html')
    result = {'id': 'catalog', 'catalog_at': 'catalog-date', 'sources': [{'url': ROOT, 'sha256': sha, 'received_at': 'listing-date'}], 'items': []}
    for i, link in enumerate(links):
        row = municipal.item(link, ROOT, 'listing-date')
        row.update(state='read', sha256=str(i) * 64, received_at='pdf-date',
                   listing_references=[{'title': link['title'], 'parent_url': ROOT, 'listed_at': 'listing-date', 'sha256': sha}])
        result['items'].append(row)
    monkeypatch.setattr(land_status, 'first_pages', lambda sha: act('1213', '13.11.2024', '1. Утвердить.') if sha == '0' * 64 else act('1144', '05.10.2026', '1. Признать утратившим силу постановление от 13.11.2024 №1213.'))
    return result


def test_source_chain_links_cancelled_document_but_never_confirms_effect(catalog):
    snapshot = land_status.procedures(catalog)
    old, new = snapshot['documents']
    assert old['state'] == new['state'] == 'observed'
    assert old['cancellation_observations'][0]['sha256'] == new['sha256']
    assert old['cancellation_observations'][0]['received_at'] == 'pdf-date'
    assert not old['legal_status_confirmed'] and not old['applicable_to_candidate'] and not snapshot['complete']
    assert old['listing_evidence'][0]['listed_at'] == 'listing-date'


@pytest.mark.parametrize('field,value', [('title', 'подменённый заголовок'), ('listed_at', 'new'), ('sha256', '9' * 64)])
def test_changed_reference_cannot_create_cancellation_evidence(catalog, field, value):
    catalog['items'][1]['listing_references'][0][field] = value
    snapshot = land_status.procedures(catalog)
    assert snapshot['documents'][1]['state'] == 'unverified'
    assert not snapshot['documents'][0]['cancellation_observations']


def test_changed_listing_bytes_rejects_both_documents(catalog):
    path = store.DATA / 'municipal' / (catalog['sources'][0]['sha256'] + '.html')
    path.write_bytes(b'<article>changed</article>')
    assert all(r['state'] == 'unverified' for r in land_status.procedures(catalog)['documents'])


def test_real_source_pdf_hash_guard_and_missing_source(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    folder = tmp_path / 'municipal'; folder.mkdir()
    raw = b'%PDF-fake'; sha = hashlib.sha256(raw).hexdigest()
    (folder / (sha + '.pdf')).write_bytes(raw + b'changed')
    with pytest.raises(ValueError, match='изменился'): land_status.first_pages(sha)
    with pytest.raises(ValueError, match='отсутствует'): land_status.first_pages('a' * 64)
    with pytest.raises(ValueError, match='SHA'): land_status.first_pages('../secret')


def test_neighbor_private_rights_and_empty_layers_never_resolve_candidate_title():
    candidate = {'neighbours': [{'fields': {'ownership_type': 'Частная', 'right_type': 'Собственность'}}], 'context_matches': {'schemes': []}}
    values = {'survey': {'bounds': [1, 2, 3, 4]}, 'nspd_context': {'bounds': [1, 2, 3, 4], 'layers': {'schemes': {'state': 'received', 'received_at': 'old', 'sha256': 'hash'}}}}
    result = land_status.assessment(candidate, values)
    assert result['ownership'] == result['third_party_rights'] == result['prior_applications'] == 'unknown'
    assert result['schema_observations']['schemes']['candidate_matches'] == 0
    assert result['schema_observations']['schemes']['received_at'] == 'old'
    assert not result['prior_schemes_absent_confirmed']
    values['nspd_context']['bounds'] = [2, 3, 4, 5]
    assert land_status.assessment(candidate, values)['schema_observations']['schemes']['candidate_matches'] is None


def candidate():
    return {'id': 'candidate', 'area_m2': 1000, 'point': [34.2, 44.9], 'geometry': {'type': 'Polygon', 'coordinates': [[[34.2, 44.9], [34.3, 44.9], [34.3, 45], [34.2, 44.9]]]}, 'rights_confirmed': False, 'srzu_ready': False}


def test_dated_enquiry_matches_exact_geometry_and_contains_no_neighbor_identity():
    c = candidate(); c['neighbours'] = [{'fields': {'owner': 'secret', 'ownership_type': 'Частная'}}]
    data = {'result': {'id': 'result', 'created_at': 'original-date', 'candidates': [c]}, 'stale': True}
    before = copy.deepcopy(data)
    text = land_status.enquiry(data, 'candidate').decode()
    geo = land_status.enquiry_geometry(data, 'candidate')
    assert 'НЕ ОТПРАВЛЕН' in text and 'устарела' in text and 'original-date' in text
    assert 'secret' not in text and 'Частная' not in text
    assert geo['features'][0]['geometry'] == c['geometry'] and not geo['features'][0]['properties']['is_srzu']
    assert geo['source_result_id'] == 'result' and geo['stale'] and data == before
    with pytest.raises(ValueError, match='Контур'): land_status.enquiry(data, 'other')


@pytest.mark.parametrize('parameters,expected_purpose', [
    ({'purpose': 'housing'}, 'ИЖС'),
    ({'purpose': 'unspecified'}, '[указать цель использования]'),
    ({}, '[указать цель использования]'),
])
def test_unsent_enquiry_uses_search_purpose_without_inventing_applicant_status(parameters, expected_purpose):
    data = {'result': {'id': 'result', 'created_at': 'original-date',
                       'parameters': parameters, 'candidates': [candidate()]}, 'stale': False}
    before = copy.deepcopy(data)
    text = land_status.enquiry(data, 'candidate').decode('utf-8')
    assert 'Предполагаемая цель: ' + expected_purpose + '.' in text
    assert 'НЕ ОТПРАВЛЕН' in text
    assert 'Основания/статус заявителя: [уточнить перед отправкой]' in text
    assert 'Заявитель и обратный адрес: [заполнить перед отправкой]' in text
    assert 'не заявление о предоставлении, регистрации права или участии в торгах' in text
    assert data == before


def test_unsupported_or_malicious_links_never_become_dossier_hrefs(catalog):
    catalog['items'][0]['url'] = 'javascript:alert(1)'
    snapshot = land_status.procedures(catalog)
    section = land_status.dossier_section({'id': 'result', 'sources': {'land_status': snapshot}}, candidate())
    assert 'href="javascript:' not in section
    assert '/api/recon/rights-request?result_id=result&amp;id=candidate' in section
