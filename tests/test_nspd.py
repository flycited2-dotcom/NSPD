import copy
import json
import pytest
import httpx
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


def test_transport_http2_proxy_tls_and_body(monkeypatch):
    actual_client = httpx.Client
    captured = {}
    def handler(request):
        captured['method'] = request.method
        captured['body'] = json.loads(request.content)
        return httpx.Response(200, json={'type': 'FeatureCollection', 'features': []})
    def client(**kwargs):
        captured.update(kwargs)
        return actual_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(nspd, 'public_url', lambda url: url)
    monkeypatch.setattr(nspd.urllib.request, 'getproxies', lambda: {'https': 'http://127.0.0.1:9999'})
    monkeypatch.setattr(nspd.urllib.request, 'proxy_bypass', lambda host: False)
    monkeypatch.setattr(nspd.httpx, 'Client', client)
    monkeypatch.setattr(nspd, 'LAST_REQUEST', 0)
    body = {'categories': [{'id': 36368}]}
    data, digest = nspd.request_json(nspd.INTERSECTS, body)
    assert captured['http2'] is True and captured['follow_redirects'] is False
    assert captured['verify'].verify_mode == nspd.ssl.CERT_REQUIRED
    assert captured['proxy'] == 'http://127.0.0.1:9999'
    assert captured['method'] == 'POST' and captured['body'] == body
    assert data['features'] == [] and len(digest) == 64


@pytest.mark.parametrize('status', [302, 401, 403, 429, 597])
def test_transport_no_retry_or_redirect(status, monkeypatch):
    actual_client = httpx.Client
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Location': 'https://example.com'})
    monkeypatch.setattr(nspd, 'public_url', lambda url: url)
    monkeypatch.setattr(nspd.httpx, 'Client', lambda **kw: actual_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(nspd, 'LAST_REQUEST', 0)
    with pytest.raises(ValueError, match=str(status)):
        nspd.request_json(nspd.INTERSECTS, {})
    assert len(calls) == 1
