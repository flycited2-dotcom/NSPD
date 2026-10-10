import copy
import json

import pytest

from land import candidate_checks as checks, network, review, store


def source(count=0, **extra):
    return {'source': 'https://nspd.gov.ru/observed', 'received_at': '2026-10-08T18:00:00+00:00',
            'sha256': 'a' * 64, 'count': count, 'state': 'received', **extra}


def snapshot():
    candidate = {'id': 'candidate', 'kind': 'draft', 'area_m2': 1000,
                 'geometry': {'type': 'Polygon', 'coordinates': []}, 'matches': {}, 'context_matches': {},
                 'rights_confirmed': False, 'srzu_ready': False, 'flags': [],
                 'land_status': {'ownership': 'unknown', 'third_party_rights': 'unknown', 'authority': 'unknown'},
                 'access_evidence': {'state': 'unknown_no_observed_road', 'intersections': {},
                                     'legal_access_confirmed': False, 'coverage_confirmed': False}}
    result = {'bounds': [34.20, 44.99, 34.21, 45.0], 'parameters': {'purpose': 'housing', 'max_area': 2500},
              'layout': {'layout_checks': 20, 'layout_complete': False}, 'sources': {
                  'nspd': {'parcels': source(42), 'buildings': source(67), 'pzz': source(), 'restrictions': source()},
                  'context': {'applied': True, 'layers': {mode: source() for mode in ('settlements', 'quarters', *checks.RESTRICTION_MODES)}},
                  'pzz_context': {'id': 'pzz', 'applied': True, 'layer': source()},
                  'rgis_context': {'id': 'gp', 'applied': True, 'layers': {'functional': source()}},
                  'torgi_active': {'id': 'active', 'received_at': '2026-10-08T18:05:00+00:00',
                                   'unlocated_numbers': [], 'lots_without_number': 0}}}
    return candidate, result


def row(progress, key):
    return next(item for item in progress['rows'] if item['key'] == key)


def no_confirmation(progress):
    assert not progress['coverage_confirmed'] and not progress['rights_confirmed'] and not progress['ready_to_submit']
    assert all(not item['confirmed'] for item in progress['rows'])
    assert all(item['state'] in ('observed', 'attention', 'unknown') for item in progress['rows'])


def test_eight_rows_cover_all_checks_without_legal_passes_or_percentages():
    candidate, result = snapshot()
    progress = checks.assessment(candidate, result)
    assert progress['algorithm'] == 'candidate-checks-v1'
    assert [item['key'] for item in progress['rows']] == ['cadastre', 'boundary', 'zoning', 'restrictions', 'access', 'publications', 'rights', 'contour']
    assert {key for item in progress['rows'] for key in item['check_keys']} == set(review.CHECKS)
    assert 1 <= len(progress['next_actions']) <= 4
    assert 'pass' not in progress and 'percentage' not in progress
    no_confirmation(progress)


def test_empty_dated_answers_are_distinct_from_sources_not_obtained():
    candidate, result = snapshot()
    dated = checks.assessment(candidate, result)
    missing = checks.assessment(candidate, {'parameters': {'purpose': 'housing'}, 'sources': {}})
    assert row(dated, 'zoning')['state'] == row(missing, 'zoning')['state'] == 'unknown'
    assert 'Пустой ответ не подтверждает отсутствие' in row(dated, 'zoning')['observation']
    assert '2026-10-08T18:00:00+00:00' in row(dated, 'zoning')['observation']
    assert 'датированный ответ не получен' in row(missing, 'zoning')['observation']
    assert 'источник другой области' not in row(missing, 'zoning')['observation']
    assert row(dated, 'restrictions')['state'] == 'observed'
    assert row(missing, 'restrictions')['state'] == 'unknown'
    no_confirmation(dated)


def test_wrong_area_regional_sources_and_context_matches_are_not_applied():
    candidate, result = snapshot()
    for key in ('context', 'pzz_context', 'rgis_context'):
        result['sources'][key]['applied'] = False
    candidate['regional_pzz_matches'] = [source(id='regional-zone')]
    candidate['general_plan_matches'] = {'functional': [source(id='gp-zone')]}
    candidate['context_matches'] = {'settlements': [source(spatially_inside=True)], 'water': [source()]}
    progress = checks.assessment(candidate, result)
    zoning = row(progress, 'zoning')
    assert zoning['state'] == 'unknown'
    assert 'региональные ПЗЗ 0; генплан РГИС 0' in zoning['observation']
    assert 'источник другой области' in zoning['observation']
    assert any(item['state'] == 'not_applied' and item['received_at'] == source()['received_at'] for item in zoning['evidence'])
    assert row(progress, 'boundary')['state'] == 'unknown'
    assert row(progress, 'restrictions')['state'] == 'observed'  # Only the independent dated ZOUIT response remains.
    no_confirmation(progress)


def test_quarter_missing_geometry_is_attention_without_assigning_quarter_counts_to_contour():
    candidate, result = snapshot()
    candidate['context_matches']['quarters'] = [source(parcels_without_geometry=123, aggregate_zero_quarter=True)]
    cadastre = row(checks.assessment(candidate, result), 'cadastre')
    assert cadastre['state'] == 'attention'
    assert 'счётчики относятся ко всему кварталу' in cadastre['observation']
    assert 'их положение неизвестно' in cadastre['observation']
    assert not cadastre['confirmed']
    assert checks.assessment(candidate, result)['next_actions'][0].startswith('Проверить кадастровые сведения')


def test_retained_and_failed_context_responses_are_attention_with_original_provenance():
    candidate, result = snapshot()
    result['sources']['context']['layers']['schemes'] = source(1, state='retained', received_at='original-date', error='HTTP 403')
    result['sources']['context']['layers']['red_lines'] = {'state': 'not_requested', 'error': 'stopped after HTTP 403'}
    candidate['context_matches']['schemes'] = [source(id='old-scheme', received_at='original-date', observation_state='retained')]
    progress = checks.assessment(candidate, result)
    restrictions = row(progress, 'restrictions')
    assert restrictions['state'] == 'attention' and 'не обновлена' in restrictions['observation']
    assert any(item['state'] == 'retained' and item['received_at'] == 'original-date' for item in restrictions['evidence'])
    assert any(item['label'] == 'Красные линии НСПД' and item['state'] == 'not_requested' for item in restrictions['evidence'])
    assert progress['next_actions'][0].startswith('Проверить документы по пересечениям')
    no_confirmation(progress)


@pytest.mark.parametrize('relation', ['outside', 'crosses', 'varies'])
def test_historical_boundary_attention_never_becomes_current_boundary_confirmation(relation):
    candidate, result = snapshot()
    candidate['boundary_observation'] = {'relation': relation, 'source': source(), 'current_boundary_confirmed': False, 'crs_confirmed': False}
    boundary = row(checks.assessment(candidate, result), 'boundary')
    assert boundary['state'] == 'attention' and 'Гипотеза исторической границы' in boundary['observation']
    assert 'действующая граница и система координат не подтверждены' in boundary['observation']
    assert not boundary['confirmed']


def test_zone_symbol_and_complete_text_index_are_observations_not_housing_authorization():
    candidate, result = snapshot()
    candidate['regional_pzz_matches'] = [source(id='zone', fields={'symbol': 'Ж1'})]
    candidate['general_plan_matches'] = {'functional': [source(id='functional-zone')]}
    result['sources']['regulations'] = {'id': 'texts', 'catalog_matches': True, 'counts': {'processed_pages': 100, 'total_pages': 100}}
    progress = checks.assessment(candidate, result)
    zoning = row(progress, 'zoning')
    assert zoning['state'] == 'observed' and 'прочитано 100/100' in zoning['observation']
    assert 'не подтверждение зоны контура' in zoning['observation']
    assert 'Установить действующую зону' in zoning['remaining']
    no_confirmation(progress)


def test_stale_text_catalog_is_attention_instead_of_applying_old_rules():
    candidate, result = snapshot()
    result['sources']['regulations'] = {'id': 'texts', 'catalog_matches': False, 'counts': {'processed_pages': 100, 'total_pages': 100}}
    zoning = row(checks.assessment(candidate, result), 'zoning')
    assert zoning['state'] == 'attention' and 'Индекс относится к прежнему каталогу' in zoning['observation']
    assert not zoning['confirmed']


def test_overlap_and_filter_options_are_reported_without_removing_or_approving_candidate():
    candidate, result = snapshot()
    candidate['matches']['restrictions'] = [source(id='zouit')]
    candidate['context_matches']['water'] = [source(id='water')]
    result['parameters'].update(avoid_restrictions=False, avoid_environment=False, avoid_planned=True)
    before = copy.deepcopy(candidate)
    restrictions = row(checks.assessment(candidate, result), 'restrictions')
    assert restrictions['state'] == 'attention'
    assert 'ЗОУИТ отключены' in restrictions['observation']
    assert 'природные территории отключены' in restrictions['observation']
    assert 'фильтры пробных контуров, не правовые выводы' in restrictions['observation']
    assert candidate == before


@pytest.mark.parametrize('state,length,has_intersections', [('zero_distance', 0, False), ('no_observed_intersections', 5, False), ('observed_intersections', 5, True)])
def test_direct_segment_never_confirms_access_even_with_zero_distance(state, length, has_intersections):
    candidate, result = snapshot()
    candidate['access_evidence'].update(state=state, road=source(distance_m=length),
        direct_segment={'length_m': length, 'within_survey_bounds': True},
        intersections={'parcels': [source(id='obstacle')] if has_intersections else [], 'buildings': []})
    progress = checks.assessment(candidate, result)
    access = row(progress, 'access')
    assert access['state'] == ('attention' if has_intersections else 'observed')
    assert 'Это не маршрут' in access['observation']
    if state == 'zero_distance':
        assert 'Нулевое расстояние не подтверждает' in access['observation']
    no_confirmation(progress)


def test_debt_and_old_procedure_status_are_candidate_specific_and_do_not_claim_current_availability():
    candidate, result = snapshot()
    candidate['lots'] = [{'id': 'debt-lot', 'url': 'https://torgi.gov.ru/lot', 'search_date': '2026-10-08T18:02:00+00:00',
                          'active_observed': False, 'land_group': 'debt_sale'}]
    result['sources']['torgi_active']['unlocated_numbers'] = ['90:12:1:2']
    progress = checks.assessment(candidate, result)
    publications = row(progress, 'publications')
    assert publications['state'] == 'attention'
    assert 'это не предоставление свободной земли' in publications['observation']
    assert 'текущий статус требует обновления' in publications['observation']
    assert publications['evidence'][0]['received_at'] == '2026-10-08T18:02:00+00:00'
    assert publications['evidence'][0]['state'] == 'status_requires_check'
    candidate['lots'] = []
    no_lots = row(checks.assessment(candidate, result), 'publications')
    assert no_lots['state'] == 'unknown' and 'номеров без геометрии: 1' in no_lots['observation']
    no_confirmation(progress)


def test_neighbor_rights_and_category_cannot_become_candidate_rights_or_category():
    candidate, result = snapshot()
    candidate['neighbours'] = [{'fields': {'ownership_type': 'Частная', 'category': 'Земли населённых пунктов'}}]
    result['sources']['land_status'] = {'documents': [source(id='regulation', legal_status_confirmed=False)]}
    progress = checks.assessment(candidate, result)
    assert row(progress, 'rights')['state'] == 'unknown'
    assert 'Частная' not in row(progress, 'rights')['observation']
    assert 'Земли населённых пунктов' not in row(progress, 'cadastre')['observation']
    no_confirmation(progress)


def test_large_gap_and_bounded_layout_require_design_not_an_absence_conclusion():
    candidate, result = snapshot()
    candidate.update(kind='gap', area_m2=5000)
    result['layout']['layout_limit_reached'] = True
    contour = row(checks.assessment(candidate, result), 'contour')
    assert contour['state'] == 'attention' and 'Требуется дополнительное проектирование' in contour['observation']
    assert 'полнота конфигураций не установлена' in contour['observation']
    assert not contour['confirmed']


def test_evidence_and_actions_are_bounded_with_visible_truncation_note():
    candidate, result = snapshot()
    candidate['lots'] = [{'id': str(i), 'url': 'https://torgi.gov.ru/lot', 'search_date': 'original'} for i in range(40)]
    progress = checks.assessment(candidate, result)
    publications = row(progress, 'publications')
    assert len(publications['evidence']) == 20
    assert 'перечень сокращён' in publications['observation']
    assert len(progress['next_actions']) <= 4


def test_assessment_is_pure_serializable_and_does_not_access_current_settings_or_network(monkeypatch):
    candidate, result = snapshot()
    before = copy.deepcopy((candidate, result))
    monkeypatch.setattr(store, 'get_setting', lambda *args: pytest.fail('Unexpected live setting read'))
    monkeypatch.setattr(network, 'fetch', lambda *args, **kwargs: pytest.fail('Unexpected network read'))
    progress = checks.assessment(candidate, result)
    assert progress == checks.assessment(candidate, result)
    assert (candidate, result) == before
    assert json.loads(json.dumps(progress)) == progress
    result['parameters']['purpose'] = 'unspecified'
    assert checks.assessment(candidate, result)['next_actions'][0].startswith('Выбрать цель использования')
