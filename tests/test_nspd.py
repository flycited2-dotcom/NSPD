import copy
import json
import pytest
from land import nspd, store


def sample():
    return json.loads((nspd.CAPTURE / 'intersects-control.browser.json').read_bytes())


def test_real_response_coordinates_and_dedup():
    data = sample()
    data['features'].append(copy.deepcopy(data['features'][0]))
    fc = nspd.normalize(data)
    assert len(fc['features']) == 5
    from shapely.geometry import shape
    for f in fc['features']:
        w, s, e, n = shape(f['geometry']).bounds
        assert 33 < w < e < 35 and 44 < s < n < 46
        assert 'systemInfo' not in f['properties']


def test_unknown_crs_rejected():
    data = sample()
    data['features'][0]['geometry'].pop('crs')
    with pytest.raises(ValueError, match='координат'):
        nspd.normalize(data)


def test_partial_page_rejected():
    data = sample()
    data['numberMatched'] = 100
    with pytest.raises(ValueError, match='неполную'):
        nspd.normalize(data)


def test_catalog_and_bounded_query():
    categories = nspd.catalog()
    assert categories['parcels']['categoryId'] == 36368
    assert categories['auction']['categoryId'] == 38981
    body = nspd.spatial_body([34.202, 44.991, 34.209, 44.996], categories['parcels']['categoryId'])
    assert body['geom']['features'][0]['geometry']['crs']['properties']['name'] == 'EPSG:3857'
    for bounds in ([0, 0, 100, 80], [34, 45, 33, 46], [0, 0, float('nan'), 1]):
        with pytest.raises(ValueError):
            nspd.spatial_body(bounds, 36368)


def test_failure_keeps_previous_result(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    store.set_setting('nspd_trudovoe', {'previous': True})
    def fail(*args):
        raise ValueError('HTTP 403')
    monkeypatch.setattr(nspd, 'request_json', fail)
    with pytest.raises(ValueError, match='403'):
        nspd.search('trudovoe', {'cadnum': '90:12:172101:420'})
    assert store.get_setting('nspd_trudovoe') == {'previous': True}
    attempt = store.get_setting('nspd_attempt_trudovoe')
    assert attempt['state'] == 'error' and '403' in attempt['error']
    assert attempt['cadnum'] == '90:12:172101:420'


def test_snapshot_never_claims_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    result = nspd.search('trudovoe', {'mode': 'snapshot'})
    assert result['count'] == 5
    saved = store.get_setting('nspd_trudovoe')
    assert saved['snapshot'] and saved['complete'] is False
    assert saved['source_date'] == '2026-09-29'
    assert store.get_setting('nspd_attempt_trudovoe')['state'] == 'done'
