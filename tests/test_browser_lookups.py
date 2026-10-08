"""Synthetic browser captures; no live NSPD response or credentials are fixtures."""
import copy
import hashlib
import json
from urllib.parse import urlencode

import pytest
from shapely.geometry import box, mapping, shape

from land import browser_lookups as browser, nspd, store, torgi
from land.geometry import convert


NUMBER = '90:12:172301:102'
OTHER = '90:12:172301:999'
RECEIVED = '2026-10-07T08:00:00+03:00'
IMPORTED = '2026-10-08T10:00:00+00:00'
ENDPOINT = 'https://nspd.gov.ru/api/geoportal/v2/search/geoportal'


def response(number=NUMBER, outside=False):
    geometry = mapping(convert(box(35, 45, 35.001, 45.001) if outside
                               else box(34.201, 44.991, 34.202, 44.992), 4326, 3857))
    geometry['crs'] = {'type': 'name', 'properties': {'name': 'EPSG:3857'}}
    return {'data': {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'id': 123, 'geometry': geometry,
         'properties': {'category': 36368, 'label': number, 'externalKey': number,
                        'descr': number, 'options': {'cad_num': number}}}]},
        'meta': [{'totalCount': 1, 'categoryId': 36368}]}


def capture(payload=None, number=NUMBER):
    # Preserve whitespace to check that SHA/file retention use exact body bytes.
    body = json.dumps(payload if payload is not None else response(number),
                      ensure_ascii=False, indent=2) + '\n'
    return {'url': ENDPOINT + '?' + urlencode({'thematicSearchId': 1, 'query': number}),
            'status': 200, 'received_at': RECEIVED,
            'sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(), 'body': body}


def lot(number=NUMBER, suffix=1):
    return torgi.normalize({'id': f'21000000000000000001_{suffix}', 'subjectRFCode': '91',
        'noticeNumber': '21000000000000000001', 'lotNumber': suffix,
        'lotStatus': 'APPLICATIONS_SUBMISSION', 'biddType': {'code': 'ZK'},
        'characteristics': [{'code': 'CadastralNumber', 'characteristicValue': number}],
        'lotDescription': 'Рядом также упомянут ' + OTHER})


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'now', lambda: IMPORTED)
    monkeypatch.setattr(nspd, 'catalog', lambda: {'parcels': {'categoryId': 36368}})
    monkeypatch.setattr(nspd, 'request_json', lambda *a, **kw: pytest.fail('Unexpected NSPD request'))
    monkeypatch.setattr(torgi, 'fetch', lambda *a, **kw: pytest.fail('Unexpected auction request'))
    store.init()
    survey = {'id': 'survey', 'bounds': [34.20, 44.99, 34.21, 45.0],
              'gaps': {'features': [{'type': 'Feature', 'id': 'gap',
                  'geometry': mapping(box(34.2005, 44.9905, 34.203, 44.993)), 'properties': {}}]},
              'layers': {}}
    survey = json.loads(json.dumps(survey))
    prior = {'id': 'old', 'search_source_id': 'original-source', 'created_at': RECEIVED,
             'pages': [{'received_at': RECEIVED, 'sha256': 'original-page'}],
             'lots': [lot()], 'geometries': {NUMBER: {'features': [], 'lookup': True,
                 'state': 'error', 'error': 'НСПД: HTTP 403', 'received_at': RECEIVED}},
             'geometry_unchecked_numbers': [NUMBER, OTHER], 'geometry_deferred_numbers': [NUMBER]}
    store.set_setting('survey_trudovoe', survey)
    for active in (False, True):
        store.set_setting(torgi.setting_key('trudovoe', active), copy.deepcopy(prior))
    return tmp_path, prior, survey


def params(entries=None):
    return {'id': 'old', 'survey_id': 'survey',
            'bundle': {'version': 1, 'observations': entries if entries is not None else [capture()]}}


def assert_unchanged(db, active=True):
    folder, prior, _ = db
    assert store.get_setting(torgi.setting_key('trudovoe', active)) == prior
    assert not (folder / 'nspd_browser').exists()
    assert not store.events('trudovoe')


def test_observation_retains_exact_bytes_capture_date_and_unverified_provenance(db):
    entry = capture()
    number, observation, raw = browser.observation(entry, {NUMBER})
    assert number == NUMBER and raw == entry['body'].encode('utf-8')
    assert observation['sha256'] == hashlib.sha256(raw).hexdigest()
    assert observation['received_at'] == RECEIVED and observation['imported_at'] == IMPORTED
    assert observation['transport'] == 'browser_response_import'
    assert observation['provenance_verified'] is False
    assert observation['state'] == 'received' and observation['lookup'] is True
    geometry = shape(observation['features'][0]['geometry'])
    assert geometry.is_valid and geometry.bounds == pytest.approx((34.201, 44.991, 34.202, 44.992))


@pytest.mark.parametrize('active', [True, False])
def test_import_matches_geometry_without_network_or_changing_search_evidence(db, active):
    folder, prior, _ = db
    key = torgi.setting_key('trudovoe', active)
    attempt_key = torgi.setting_key('trudovoe', active, '_geometry_attempt')
    document_key = torgi.setting_key('trudovoe', active, '_documents')
    failed = {'state': 'partial', 'error': 'НСПД: HTTP 403', 'processed': 1}
    documents = {'id': 'documents', 'search_source_id': 'original-source', 'files': {}}
    store.set_setting(attempt_key, failed)
    store.set_setting(document_key, documents)
    entry = capture()
    answer = browser.run('trudovoe', params([entry]), active=active)
    result = store.get_setting(key)
    assert answer['network_requests'] == 0 and answer['imported'] == 1
    assert result['id'] != prior['id'] and result['parent_id'] == prior['id']
    assert result['search_source_id'] == prior['search_source_id']
    assert result['created_at'] == prior['created_at'] and result['pages'] == prior['pages']
    assert result['lots'][0]['spatial_state'] == 'inside'
    assert result['lots'][0]['spatial_matches'][0]['gap_intersections'][0]['area_m2'] > 0
    assert result['lots'][0]['geometry_confirmed'] is False
    assert result['geometry_unchecked_numbers'] == [OTHER] and result['geometry_deferred_numbers'] == []
    assert store.get_setting(attempt_key) == failed
    assert store.get_setting(document_key) == documents
    assert store.get_setting(torgi.setting_key('trudovoe', not active)) == prior
    raw = folder / 'nspd_browser' / (entry['sha256'] + '.source.json')
    assert raw.read_bytes() == entry['body'].encode('utf-8')
    provenance = json.loads(raw.with_name(entry['sha256'] + '.observation.json').read_text(encoding='utf-8'))
    assert provenance['received_at'] == RECEIVED and provenance['source'] == entry['url']
    assert 'features' not in provenance and 'cookies' not in provenance and 'headers' not in provenance
    assert store.events('trudovoe')[0]['body']['network_requests'] == 0


def test_import_can_establish_outside_without_claiming_lot_geometry_confirmed(db):
    browser.run('trudovoe', params([capture(response(outside=True))]))
    found = store.get_setting('torgi_active_trudovoe')['lots'][0]
    assert found['spatial_state'] == 'outside' and not found['in_survey']
    assert found['geometry_confirmed'] is False


def test_a_partial_multinumber_lot_remains_unknown_when_known_number_is_outside(db):
    folder, prior, _ = db
    previous = copy.deepcopy(prior)
    previous['lots'][0]['cadastral_numbers'].append(OTHER)
    store.set_setting('torgi_active_trudovoe', previous)
    browser.run('trudovoe', params([capture(response(outside=True))]))
    found = store.get_setting('torgi_active_trudovoe')['lots'][0]
    assert found['spatial_state'] == 'unknown' and not found['in_survey']


@pytest.mark.parametrize('url', [
    'http://nspd.gov.ru/api/geoportal/v2/search/geoportal?thematicSearchId=1&query=' + NUMBER,
    'https://other.example/api/geoportal/v2/search/geoportal?thematicSearchId=1&query=' + NUMBER,
    'https://secret@nspd.gov.ru/api/geoportal/v2/search/geoportal?thematicSearchId=1&query=' + NUMBER,
    'https://nspd.gov.ru:443/api/geoportal/v2/search/geoportal?thematicSearchId=1&query=' + NUMBER,
    ENDPOINT + '?thematicSearchId=1&query=' + NUMBER + '&query=' + OTHER,
    ENDPOINT + '?thematicSearchId=1&query=' + NUMBER + '&query=',
    ENDPOINT + '?thematicSearchId=1&query=' + NUMBER + '&token=secret',
    ENDPOINT + '?thematicSearchId=1&query=' + NUMBER + '&token=',
    ENDPOINT + '?thematicSearchId=2&query=' + NUMBER,
    ENDPOINT + '?thematicSearchId=1&query=' + NUMBER + '#fragment',
    ENDPOINT + '/extra?thematicSearchId=1&query=' + NUMBER,
])
def test_foreign_credential_or_ambiguous_urls_are_rejected_without_storage(db, url):
    entry = capture()
    entry['url'] = url
    with pytest.raises(ValueError):
        browser.run('trudovoe', params([entry]))
    assert_unchanged(db)


@pytest.mark.parametrize('field,value', [
    ('status', 403), ('status', True), ('status', '200'),
    ('received_at', '2026-10-07T08:00:00'), ('received_at', 'not-a-date'),
    ('received_at', '1999-12-31T23:59:59Z'), ('received_at', '2099-01-01T00:00:00Z'),
    ('sha256', '0' * 64), ('body', {}),
])
def test_unsuccessful_undated_or_tampered_captures_do_not_replace_results(db, field, value):
    entry = capture()
    entry[field] = value
    with pytest.raises(ValueError):
        browser.run('trudovoe', params([entry]))
    assert_unchanged(db)


def test_changed_body_bytes_are_rejected_even_with_equivalent_json(db):
    entry = capture()
    entry['body'] += ' '
    with pytest.raises(ValueError, match='SHA'):
        browser.run('trudovoe', params([entry]))
    assert_unchanged(db)


def test_response_number_must_be_in_structured_lot_not_description(db):
    with pytest.raises(ValueError):
        browser.run('trudovoe', params([capture(number=OTHER)]))
    assert_unchanged(db)


@pytest.mark.parametrize('location,key', [
    ('root', 'headers'), ('root', 'cookies'),
    ('properties', 'authorization'), ('options', 'access_token'),
    ('options', 'accessToken'), ('options', 'refreshToken'),
])
def test_response_body_with_credentials_is_rejected_before_raw_retention(db, location, key):
    payload = response()
    container = payload if location == 'root' else payload['data']['features'][0]['properties']
    if location == 'options':
        container = container['options']
    container[key] = {'value': 'synthetic-do-not-retain'}
    with pytest.raises(ValueError):
        browser.run('trudovoe', params([capture(payload)]))
    assert_unchanged(db)


def test_five_distinct_captures_match_only_their_structured_lot_numbers(db):
    _, prior, _ = db
    numbers = [f'90:12:172301:{index}' for index in range(1, 6)]
    previous = copy.deepcopy(prior)
    previous['lots'] = [lot(number, index) for index, number in enumerate(numbers, 1)]
    store.set_setting('torgi_active_trudovoe', previous)
    answer = browser.run('trudovoe', params([capture(number=number) for number in numbers]))
    result = store.get_setting('torgi_active_trudovoe')
    assert answer['imported'] == 5 and answer['network_requests'] == 0
    for item, number in zip(result['lots'], numbers):
        assert item['spatial_state'] == 'inside' and item['geometry_confirmed'] is False
        assert len(item['spatial_matches']) == 1
        assert item['spatial_matches'][0]['cadastral_number'] == number


@pytest.mark.parametrize('problem', [
    'missing_meta', 'wrong_total', 'bool_total', 'wrong_meta_category', 'multiple_categories',
    'empty', 'duplicate', 'wrong_category', 'bool_category', 'wrong_cad_num',
    'missing_cad_num', 'conflicting_label', 'conflicting_externalKey', 'conflicting_descr',
    'pagination',
])
def test_incomplete_wrong_category_or_conflicting_number_response_is_rejected(db, problem):
    payload = response()
    feature = payload['data']['features'][0]
    if problem == 'missing_meta':
        payload.pop('meta')
    elif problem == 'wrong_total':
        payload['meta'][0]['totalCount'] = 2
    elif problem == 'bool_total':
        payload['meta'][0]['totalCount'] = True
    elif problem == 'wrong_meta_category':
        payload['meta'][0]['categoryId'] = 36369
    elif problem == 'multiple_categories':
        payload['meta'].append({'totalCount': 1, 'categoryId': 36369})
    elif problem == 'empty':
        payload['data']['features'] = []
    elif problem == 'duplicate':
        payload['data']['features'].append(copy.deepcopy(feature))
    elif problem == 'wrong_category':
        feature['properties']['category'] = 36369
    elif problem == 'bool_category':
        feature['properties']['category'] = True
    elif problem == 'wrong_cad_num':
        feature['properties']['options']['cad_num'] = OTHER
    elif problem == 'missing_cad_num':
        feature['properties']['options'].pop('cad_num')
    elif problem.startswith('conflicting_'):
        feature['properties'][problem.removeprefix('conflicting_')] = OTHER
    elif problem == 'pagination':
        payload['data']['links'] = [{'rel': 'next', 'href': 'https://nspd.gov.ru/next'}]
    with pytest.raises(ValueError):
        browser.run('trudovoe', params([capture(payload)]))
    assert_unchanged(db)


@pytest.mark.parametrize('problem', [
    'missing_crs', 'unsupported_crs', 'conflicting_crs', 'unclosed_ring',
    'nonfinite_xy', 'three_dimensions', 'line', 'self_intersection',
])
def test_ambiguous_or_invalid_geometry_cannot_become_a_lot_lookup(db, problem):
    # Deep-copy through JSON so ring coordinates are mutable synthetic lists.
    payload = json.loads(json.dumps(response()))
    geometry = payload['data']['features'][0]['geometry']
    if problem == 'missing_crs':
        geometry.pop('crs')
    elif problem == 'unsupported_crs':
        geometry['crs']['properties']['name'] = 'EPSG:19635'
    elif problem == 'conflicting_crs':
        payload['data']['crs'] = {'type': 'name', 'properties': {'name': 'EPSG:4326'}}
    elif problem == 'unclosed_ring':
        geometry['coordinates'][0].pop()
    elif problem == 'nonfinite_xy':
        geometry['coordinates'][0][1][0] = float('nan')
    elif problem == 'three_dimensions':
        for point in geometry['coordinates'][0]:
            point.append(1)
    elif problem == 'line':
        geometry.update(type='LineString', coordinates=geometry['coordinates'][0][:2])
    elif problem == 'self_intersection':
        first, second, third, fourth, _ = geometry['coordinates'][0]
        geometry['coordinates'] = [[first, third, second, fourth, first]]
    with pytest.raises(ValueError):
        browser.run('trudovoe', params([capture(payload)]))
    assert_unchanged(db)


@pytest.mark.parametrize('problem', ['empty', 'six', 'duplicate', 'bad_second', 'version', 'bool_version'])
def test_invalid_batch_is_atomic_and_never_partially_imported(db, problem):
    request = params()
    if problem == 'empty':
        request['bundle']['observations'] = []
    elif problem == 'six':
        request['bundle']['observations'] = [capture()] * 6
    elif problem == 'duplicate':
        request['bundle']['observations'] = [capture(), capture()]
    elif problem == 'bad_second':
        bad = capture(number=OTHER)
        bad['sha256'] = '0' * 64
        request['bundle']['observations'] = [capture(), bad]
    elif problem == 'version':
        request['bundle']['version'] = 2
    elif problem == 'bool_version':
        request['bundle']['version'] = True
    with pytest.raises(ValueError):
        browser.run('trudovoe', request)
    assert_unchanged(db)


@pytest.mark.parametrize('existing_source', ['lookup', 'survey'])
def test_import_never_replaces_existing_lookup_or_survey_geometry(db, existing_source):
    folder, prior, survey = db
    previous = copy.deepcopy(prior)
    current_survey = copy.deepcopy(survey)
    feature = json.loads(json.dumps(nspd.normalize(response())['features'][0]))
    if existing_source == 'lookup':
        previous['geometries'][NUMBER] = {'features': [feature], 'lookup': True,
                                          'received_at': RECEIVED, 'state': 'received'}
    else:
        current_survey['layers']['parcels'] = {'geojson': {'features': [feature]},
                                              'source': 'current-survey', 'received_at': RECEIVED}
    store.set_setting('torgi_active_trudovoe', previous)
    store.set_setting('survey_trudovoe', current_survey)
    with pytest.raises(ValueError, match='уже получена'):
        browser.run('trudovoe', params())
    assert store.get_setting('torgi_active_trudovoe') == previous
    assert not (folder / 'nspd_browser').exists()


@pytest.mark.parametrize('field,value', [('id', 'stale-search'), ('survey_id', 'stale-survey')])
def test_stale_initial_revision_is_rejected(db, field, value):
    request = params()
    request[field] = value
    with pytest.raises(ValueError):
        browser.run('trudovoe', request)
    assert_unchanged(db)


@pytest.mark.parametrize('changed', ['search', 'survey'])
def test_concurrent_revision_is_preserved_and_capture_is_not_written(db, monkeypatch, changed):
    folder, prior, survey = db
    original = torgi.relate
    key = 'torgi_active_trudovoe' if changed == 'search' else 'survey_trudovoe'
    concurrent = {'id': 'concurrent'}

    def relate(*args):
        result = original(*args)
        store.set_setting(key, concurrent)
        return result

    monkeypatch.setattr(torgi, 'relate', relate)
    with pytest.raises(ValueError, match='изменил'):
        browser.run('trudovoe', params())
    assert store.get_setting(key) == concurrent
    if changed == 'survey':
        assert store.get_setting('torgi_active_trudovoe') == prior
    else:
        assert store.get_setting('survey_trudovoe') == survey
    assert not (folder / 'nspd_browser').exists()
    assert not store.events('trudovoe')
