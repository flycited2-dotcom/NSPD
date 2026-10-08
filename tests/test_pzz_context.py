"""Regional PZZ service observations cannot establish zoning completeness or rights."""
import copy
import json
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from shapely.geometry import box, mapping, shape

from land import pzz_context as pzz, store
from land.geometry import convert

BOUNDS = [34.20, 44.99, 34.21, 45.0]


def payload(world=None):
    polygon = convert(world if world is not None else box(34.202, 44.992, 34.208, 44.998), 4326, 3857)
    return {
        'type': 'FeatureCollection', 'totalFeatures': 1, 'numberMatched': 1, 'numberReturned': 1,
        'crs': {'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:EPSG::3857'}},
        'timeStamp': '2099-01-01T00:00:00Z',
        'features': [{'type': 'Feature', 'id': pzz.FEATURE_PREFIX + '1',
                      'geometry': json.loads(json.dumps(mapping(polygon))),
                      'properties': {'symbol': 'Ж1', 'territorialzonename': '<script>Жилая зона</script>',
                                     'territory': 'Трудовское', 'status': 'published', 'dataobjectkey': 'key-1',
                                     'created_by': 'not-exported'}}],
    }


def empty_payload():
    return {'type': 'FeatureCollection', 'features': [], 'totalFeatures': 0, 'numberMatched': 0, 'numberReturned': 0}


def context():
    return {
        'version': pzz.VERSION, 'id': 'saved-pzz', 'bounds': BOUNDS, 'catalog': copy.deepcopy(pzz.CATALOG),
        'layer': {'received_at': 'original-date', 'sha256': 'original-sha',
                  'count': 1, 'geojson': pzz.normalize(payload(), BOUNDS)},
    }


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    return tmp_path


def test_catalog_identifies_pzz_separately_from_genplan_and_zero_hits_are_dated_unknown_coverage():
    catalog = pzz.CATALOG
    assert catalog['type_name'] == 'rgis:territorialzone_polygon'
    assert catalog['map_id'] == '34f88ad7-6208-4f45-b29a-daabbb7119fe'
    assert catalog['layer_id'] == '8d18d817-1aa5-4d55-8afc-d7a93a19a4c7'
    assert catalog['checked_at'] == '2026-10-08T18:11:33+00:00'
    assert catalog['global_observation']['checked_at'] == '2026-10-08T18:09:58+00:00'
    assert catalog['global_observation']['count'] == 0
    assert not catalog['global_observation']['coverage_confirmed']
    assert not catalog['global_observation']['currentness_confirmed']
    assert 'not confirmed coverage' in catalog['advertised_bounds_meaning']


def test_bounds_are_limited_and_only_observed_pzz_type_is_requested():
    params = pzz.parameters(BOUNDS)
    assert params['typeName'] == 'rgis:territorialzone_polygon'
    assert params['version'] == '1.1.0' and params['srsName'] == 'EPSG:3857'
    assert list(map(float, params['bbox'].split(',')[:4])) == pytest.approx(convert(box(*BOUNDS), 4326, 3857).bounds)
    with pytest.raises(ValueError, match='10 км'):
        pzz.parameters([32, 44, 37, 47])


def test_crs_projection_preserves_only_observed_string_fields_and_does_not_alter_payload():
    data = payload()
    before = copy.deepcopy(data)
    feature = pzz.normalize(data, BOUNDS)['features'][0]
    assert shape(feature['geometry']).bounds == pytest.approx((34.202, 44.992, 34.208, 44.998), abs=1e-9)
    assert feature['properties']['symbol'] == 'Ж1'
    assert feature['properties']['territorialzonename'] == '<script>Жилая зона</script>'
    assert 'created_by' not in feature['properties']
    assert data == before


@pytest.mark.parametrize('change', [
    {'numberMatched': 2}, {'totalFeatures': 'unknown'}, {'numberReturned': True},
    {'crs': None}, {'crs': {'type': 'name', 'properties': {'name': 'EPSG:4326'}}},
    {'next': 'page2'}, {'links': [{'rel': 'next', 'href': 'https://bad.invalid'}]},
    {'@odata.nextLink': 'page2'},
])
def test_incomplete_count_crs_or_paging_rejected(change):
    data = payload()
    data.update(change)
    with pytest.raises(ValueError):
        pzz.normalize(data, BOUNDS)


@pytest.mark.parametrize('mode', ['duplicate', 'wrong_layer', 'outside', 'invalid', 'point',
                                 'coordinate_limit', 'count_limit', 'field_type', 'field_length'])
def test_bad_geometry_layer_or_field_does_not_create_zone_observation(mode, monkeypatch):
    data = payload()
    if mode == 'duplicate':
        data['features'] *= 2
        data.update(totalFeatures=2, numberMatched=2, numberReturned=2)
    elif mode == 'wrong_layer':
        data['features'][0]['id'] = 'FunctionalZones_polygon.1'
    elif mode == 'outside':
        data = payload(world=box(35, 45, 35.1, 45.1))
    elif mode == 'invalid':
        data['features'][0]['geometry']['coordinates'][0][0] = (float('nan'), 0)
    elif mode == 'point':
        data['features'][0]['geometry'] = {'type': 'Point', 'coordinates': [0, 0]}
    elif mode == 'coordinate_limit':
        monkeypatch.setattr(pzz, 'MAX_COORDINATES', 3)
    elif mode == 'count_limit':
        monkeypatch.setattr(pzz, 'MAX_FEATURES', 1)
    elif mode == 'field_type':
        data['features'][0]['properties']['symbol'] = {'nested': 'Ж1'}
    elif mode == 'field_length':
        data['features'][0]['properties']['symbol'] = 'Ж' * 2001
    with pytest.raises(ValueError):
        pzz.normalize(data, BOUNDS)


def test_empty_response_is_a_valid_observation_without_asserting_absent_zones():
    assert pzz.normalize(empty_payload(), BOUNDS) == {'type': 'FeatureCollection', 'features': []}


@pytest.mark.parametrize('empty', [False, True])
def test_collect_single_bounded_request_keeps_raw_provenance_and_never_confirms_full_map(db, monkeypatch, empty):
    calls = []
    data = empty_payload() if empty else payload()
    raw = json.dumps(data).encode()
    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return raw, 'application/json', 200
    monkeypatch.setattr(pzz.network, 'fetch', fetch)
    result = pzz.collect(BOUNDS)
    assert len(calls) == 1
    parsed = parse_qs(urlparse(calls[0][0]).query)
    assert parsed['typeName'] == [pzz.TYPE_NAME] and parsed['maxFeatures'] == ['1000']
    assert parsed['bbox'][0].endswith(',EPSG:3857')
    assert calls[0][1]['max_bytes'] == 5 * 1024 * 1024
    layer = result['layer']
    assert layer['count'] == (0 if empty else 1)
    assert layer['received_at'] and layer['http_status'] == 200 and layer['response_count_consistent']
    assert layer['source_crs_confirmed'] == (not empty)
    assert layer['server_clock_warning'] == (not empty)
    for value in (result, layer):
        assert not value['coverage_confirmed'] and not value['currentness_confirmed']
        assert not value['territorial_zone_confirmed']
    assert not layer['used_for_exclusion']
    assert (db / 'pzz' / 'raw' / (layer['sha256'] + '.json')).read_bytes() == raw
    assert result['catalog'] == pzz.CATALOG
    assert result['catalog']['global_observation']['count'] == 0  # Historical hits never overwritten by area count.


@pytest.mark.parametrize('failure', ['403', 'timeout', 'schema', 'status'])
def test_first_source_failure_preserves_old_context_and_records_failed_attempt(db, monkeypatch, failure):
    old = context()
    store.set_setting('pzz_context_trudovoe', old)
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        if failure in ('403', 'timeout'):
            raise ValueError('HTTP 403: access restricted' if failure == '403' else 'read timeout')
        return json.dumps({'error': 'unexpected'} if failure == 'schema' else payload()).encode(), 'application/json', 200 if failure == 'schema' else 206
    monkeypatch.setattr(pzz.network, 'fetch', fetch)
    with pytest.raises(ValueError):
        pzz.run('trudovoe', {'bounds': BOUNDS})
    assert len(calls) == 1
    assert store.get_setting('pzz_context_trudovoe') == json.loads(json.dumps(old))
    attempt = store.get_setting('pzz_attempt_trudovoe')
    assert attempt['state'] == 'error' and attempt['error'] and attempt['finished_at']


def test_success_stores_snapshot_context_and_event(db, monkeypatch):
    monkeypatch.setattr(pzz.network, 'fetch', lambda *args, **kwargs: (json.dumps(empty_payload()).encode(), 'application/json', 200))
    result = pzz.run('trudovoe', {'bounds': BOUNDS})
    saved = store.get_setting('pzz_context_trudovoe')
    assert result == {'id': saved['id'], 'count': 0}
    assert json.loads((db / 'pzz' / (saved['id'] + '.json')).read_text('utf-8')) == saved
    assert store.get_setting('pzz_attempt_trudovoe')['state'] == 'done'
    assert store.events('trudovoe')[0]['kind'] == 'pzz_context'


def test_event_failure_rolls_back_context_update(db, monkeypatch):
    old = context()
    store.set_setting('pzz_context_trudovoe', old)
    monkeypatch.setattr(pzz.network, 'fetch', lambda *args, **kwargs: (json.dumps(empty_payload()).encode(), 'application/json', 200))
    def fail_event(*args, **kwargs):
        raise sqlite3.OperationalError('simulated write failure')
    monkeypatch.setattr(store, 'event', fail_event)
    with pytest.raises(sqlite3.OperationalError):
        pzz.run('trudovoe', {'bounds': BOUNDS})
    assert store.get_setting('pzz_context_trudovoe') == json.loads(json.dumps(old))
    assert store.get_setting('pzz_attempt_trudovoe')['state'] == 'error'


def test_context_bound_or_version_mismatch_does_not_attach_zone_geometries():
    saved = context()
    assert pzz.projected(saved, [34.3, 45, 34.31, 45.01], 'EPSG:3857') == []
    assert not pzz.sources(saved, [34.3, 45, 34.31, 45.01])['applied']
    saved['version'] = -1
    assert pzz.projected(saved, BOUNDS, 'EPSG:3857') == []


def test_spatial_intersection_keeps_provenance_but_confirms_no_legal_fact():
    saved = context()
    before = copy.deepcopy(saved)
    entries = pzz.projected(saved, BOUNDS, 'EPSG:3857')
    candidate = convert(box(34.203, 44.993, 34.204, 44.994), 4326, 3857)
    matches = pzz.relate(candidate, entries, saved)
    assert len(matches) == 1 and matches[0]['spatially_covers']
    row = matches[0]
    assert row['fields']['symbol'] == 'Ж1' and row['area_m2'] > 0
    assert row['source'] == pzz.WFS and row['map_url'] == pzz.MAP
    assert row['sha256'] == 'original-sha' and row['received_at'] == 'original-date'
    assert not row['territorial_zone_confirmed'] and not row['currentness_confirmed']
    assert not row['coverage_confirmed'] and not row['used_for_exclusion']
    assert saved == before
    assert pzz.relate(convert(box(35, 45, 35.1, 45.1), 4326, 3857), entries, saved) == []


def test_source_report_removes_geometry_and_preserves_original_dates_even_without_context():
    saved = context()
    report = pzz.sources(saved, BOUNDS)
    assert report['applied'] and report['layer']['received_at'] == 'original-date'
    assert 'geojson' not in report['layer'] and report['catalog'] == pzz.CATALOG
    report['catalog']['global_observation']['count'] = 999
    assert saved['catalog']['global_observation']['count'] == 0 and pzz.CATALOG['global_observation']['count'] == 0
    empty = pzz.sources(None, BOUNDS)
    assert not empty['applied'] and empty['layer'] == {} and empty['catalog']['map_url'] == pzz.MAP
    assert not empty['coverage_confirmed'] and not empty['currentness_confirmed']
