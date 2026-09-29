import copy
import io
import json
import zipfile
from datetime import date, timedelta
import pytest
from pyproj import Geod
from shapely.geometry import box, mapping, shape, Polygon
from shapely.ops import unary_union
from land.geometry import analyse, validate_layer
from land.demo import fixture_layers
from land import store
from land.review import CHECKS, PACKAGE_CHECKS, status, present, validate_update
from land.network import inspect_har, fetch_geojson, public_url
from land.exports import bundle, csv_bytes


@pytest.fixture
def layers():
    return fixture_layers()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    return tmp_path


def candidate(layers):
    cs, _ = analyse(layers, {'min_area': 300, 'max_area': 2500, 'min_width': 10})
    return dict(cs[0], id='T-001', active=True, stale=False, checks={}, workflow='review', notes='', reference='', deadline='', history=[])


def checks():
    return {k: {'result': 'pass', 'date': date.today().isoformat(), 'note': 'Тестовое подтверждение', 'source': 'Официальный документ, раздел 1'} for k in {**CHECKS, **PACKAGE_CHECKS}}


def test_subtraction_and_area_against_geodesic(layers):
    results, summary = analyse(layers, {'min_area': 1, 'max_area': 3000, 'min_width': 0})
    boundary = shape(layers[0]['geojson']['features'][0]['geometry'])
    occupied = unary_union([shape(f['geometry']) for l in layers if l['role'] in ['parcels', 'roads', 'exclusions'] for f in l['geojson']['features']])
    for c in results:
        g = shape(c['geometry'])
        overlap_area = abs(Geod(ellps='WGS84').geometry_area_perimeter(g.intersection(occupied))[0])
        assert overlap_area < .1
        # Transforming straight segments between projections introduces sub-metre
        # edge interpolation differences. Bound these in physical area units.
        outside = g.difference(boundary)
        outside_area = abs(Geod(ellps='WGS84').geometry_area_perimeter(outside)[0])
        assert outside_area < .1
        from shapely.geometry.polygon import orient
        area, _ = Geod(ellps='WGS84').geometry_area_perimeter(orient(g))
        assert abs(c['area_m2'] - abs(area)) / abs(area) < .0001
    assert sum(c['area_m2'] for c in results) == pytest.approx(summary['remaining_m2'], abs=.03)


def test_missing_cadastre_is_not_empty_search(layers):
    with pytest.raises(ValueError, match='граница.*слой'):
        analyse([l for l in layers if l['role'] != 'parcels'], {})


def test_incomplete_cadastre_rejected(layers):
    next(l for l in layers if l['role'] == 'parcels')['metadata']['complete'] = False
    with pytest.raises(ValueError, match='полноты'):
        analyse(layers, {})


def test_coverage_hole_rejected(layers):
    p = next(l for l in layers if l['role'] == 'parcels')
    g = shape(p['metadata']['coverage'])
    p['metadata']['coverage'] = mapping(g.difference(g.centroid.buffer(.00005)))
    with pytest.raises(ValueError, match='не покрывают'):
        analyse(layers, {})


def test_holes_remain_unavailable(layers):
    results, _ = analyse(layers, {'min_area': 1, 'min_width': 0})
    g = shape(results[0]['geometry'])
    assert not g.contains(shape(next(l for l in layers if l['role']=='parcels')['geojson']['features'][0]['geometry']).centroid)


def test_restrictions_are_flagged_not_blanket_excluded(layers):
    results, _ = analyse(layers, {'min_area': 1, 'min_width': 0})
    assert any(c['overlays']['restrictions'] for c in results)
    assert all('status' not in c for c in results)


def test_large_windows_retained_with_flag(layers):
    results, _ = analyse(layers, {'min_area': 1, 'max_area': 10, 'min_width': 0})
    assert results and all(c['large_window'] for c in results)


def test_width_filter(layers):
    results, summary = analyse(layers, {'min_area': 1, 'min_width': 100})
    assert not results and summary['filtered']['narrow'] > 0


@pytest.mark.parametrize('params', [{'min_area':-1},{'max_area':0},{'clearance':-1},{'min_width':float('nan')}])
def test_invalid_parameters(layers, params):
    with pytest.raises(ValueError):
        analyse(layers, params)


def payload(g):
    return {'role':'boundary','metadata':{'title':'Test','source':'Test','checked_at':date.today().isoformat(),'crs':'EPSG:4326'},'geojson':{'type':'FeatureCollection','features':[{'type':'Feature','geometry':mapping(g),'properties':{}}]}}


def test_bad_geometry_rejected():
    with pytest.raises(ValueError, match='валидный'):
        validate_layer(payload(Polygon([(0,0),(1,1),(1,0),(0,1),(0,0)])))


def test_wrong_crs_rejected():
    with pytest.raises(ValueError, match='Координаты'):
        validate_layer(payload(box(500000,5000000,500100,5000100)))


def test_declared_crs_conflict():
    p = payload(box(34,45,34.01,45.01))
    p['geojson']['crs'] = {'properties': {'name':'EPSG:3857'}}
    with pytest.raises(ValueError, match='не совпадает'):
        validate_layer(p)


def test_cannot_promote_without_evidence(layers):
    c = candidate(layers)
    assert status(c) == 'yellow'
    with pytest.raises(ValueError, match='Пакет не готов'):
        validate_update(c, {'workflow':'ready'})
    with pytest.raises(ValueError, match='источник'):
        validate_update(c, {'checks':{'rights':{'result':'pass','source':'','note':''}}})


def test_green_and_staleness(layers):
    c = candidate(layers)
    c['checks'] = checks()
    assert status(c) == 'green'
    assert validate_update(c, {'workflow':'ready'})['workflow'] == 'ready'
    c.update(workflow='ready', stale=True)
    assert status(c) == 'yellow'
    assert present(c)['effective_workflow'] == 'review'
    c['stale'] = False
    c['checks']['rights']['date'] = (date.today()-timedelta(days=31)).isoformat()
    assert status(c) == 'yellow'
    c['checks']['rights']['result'] = 'fail'
    assert status(c) == 'red'


def test_submitted_requires_reference(layers):
    with pytest.raises(ValueError, match='номер'):
        validate_update(candidate(layers), {'workflow':'submitted'})


def test_stable_ids_and_revision_invalidates_checks(db, layers):
    results, summary = analyse(layers, {'min_area':1,'min_width':0})
    store.save_results('demo', results, summary)
    cs = store.candidates('demo')
    store.update_candidate('demo', cs[0]['id'], {'notes':'Keep me','checks':checks(),'workflow':'working'})
    results, summary = analyse(layers, {'min_area':1,'min_width':0})
    store.save_results('demo', results, summary)
    assert len(store.candidates('demo')) == len(cs)
    assert store.candidates('demo')[0]['checks']
    layers[0]['metadata']['title'] += ' revision'
    store.save_layer('demo', layers[0])
    assert store.candidates('demo')[0]['stale']
    results, summary = analyse(layers, {'min_area':1,'min_width':0})
    store.save_results('demo', results, summary)
    updated = store.candidates('demo')[0]
    assert not updated['checks']
    assert updated['notes'] == 'Keep me'
    assert updated['history']
    assert store.candidates('trudovoe') == []


def test_absent_results_archive_not_delete(db, layers):
    results, summary = analyse(layers, {'min_area':1,'min_width':0})
    store.save_results('demo', results, summary)
    store.save_results('demo', [], summary)
    assert store.candidates('demo')
    assert all(not c['active'] for c in store.candidates('demo'))


def test_concurrent_input_revision_rejected(db, layers):
    for layer in layers:
        store.save_layer('demo', layer)
    loaded = store.layers('demo')
    results, summary = analyse(loaded, {'min_area':1,'min_width':0})
    loaded[0]['metadata']['title'] = 'Concurrent change'
    store.save_layer('demo', loaded[0])
    with pytest.raises(ValueError, match='изменились во время'):
        store.save_results('demo', results, summary, expected_fingerprint=summary['fingerprint'])
    assert not store.candidates('demo')


def test_har_does_not_store_secrets():
    har={'log':{'entries':[{'startedDateTime':'2026-09-19','request':{'url':'https://nspd.gov.ru/api/features?token=VERYSECRET&q=private','method':'GET','headers':[{'name':'Cookie','value':'VERYSECRET'}],'postData':{'text':'VERYSECRET'}},'response':{'status':200,'content':{'mimeType':'application/json','text':'{"type":"FeatureCollection","features":[]}'}}}]}}
    rows=inspect_har(har)
    assert len(rows)==1
    assert 'VERYSECRET' not in json.dumps(rows)
    assert 'private' not in json.dumps(rows)
    assert rows[0]['parameters']==['token','q']
    assert rows[0]['type']=='XHR веб-клиента'


def test_partial_feed_rejected(monkeypatch):
    monkeypatch.setattr('land.network.fetch', lambda url:(json.dumps({'type':'FeatureCollection','features':[],'numberMatched':10}).encode(),'application/json',200))
    with pytest.raises(ValueError,match='Неполная'):
        fetch_geojson('https://example.com/data')


def test_export_is_self_contained_and_html_escaped(layers):
    c=candidate(layers)
    c['notes']='<script>alert(1)</script>'
    raw=bundle('demo',[c],[],[],[])
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        assert 'candidates.geojson' in z.namelist()
        html=z.read('dossiers/T-001.html').decode()
        assert '<script>' not in html
        assert 'УЧЕБНЫЙ ПРИМЕР' in html
        assert 'не является СРЗУ' in html
        assert json.loads(z.read('candidates.geojson'))['features'][0]['properties']['status']=='yellow'
    assert "'=SUM(1)" in csv_bytes([['=SUM(1)']]).decode('utf-8-sig')


def test_private_url_rejected():
    with pytest.raises(ValueError):
        public_url('https://127.0.0.1/data')
    with pytest.raises(ValueError):
        public_url('http://example.com/data')
