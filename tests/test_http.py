import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
import pytest
import app
from land import store


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    http = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{http.server_port}'
    http.shutdown()
    http.server_close()


def request(server, path, data=None, token=True):
    req = urllib.request.Request(server+path)
    if data is not None:
        req.data=json.dumps(data).encode()
        req.add_header('Content-Type','application/json')
        if token:
            req.add_header('X-Local-Token',app.TOKEN)
    return urllib.request.urlopen(req, timeout=10)


def test_http_demo_dossier_and_export(server):
    with request(server,'/api/demo',{'project':'demo'}) as r:
        assert json.load(r)['count'] > 0
    with request(server,'/api/state?project=trudovoe') as r:
        assert not json.load(r)['candidates']
    with request(server,'/api/state?project=demo') as r:
        c=json.load(r)['candidates'][0]
    with request(server,'/api/candidate',{'project':'demo','id':c['id'],'workflow':'working','notes':'Test persistence'}) as r:
        assert json.load(r)['workflow']=='working'
    with request(server,'/api/dossier?project=demo&id='+c['id']) as r:
        assert 'Test persistence' in r.read().decode()
    with request(server,'/api/export?project=demo') as r:
        assert r.headers['Content-Type']=='application/zip'
        assert r.read().startswith(b'PK')


def test_http_rejects_external_write_and_invalid_project(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server,'/api/demo',{'project':'demo'},token=False)
    assert exc.value.code==403
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server,'/api/demo',{'project':'trudovoe'})
    assert exc.value.code==400
    with pytest.raises(urllib.error.HTTPError):
        request(server,'/api/state?project=../../etc')


def test_http_does_not_serve_workspace_files(server):
    for path in ['/app.py','/../app.py','/data/land.sqlite']:
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server,path)
        assert exc.value.code==404


def test_nspd_watchlist_and_export_preserve_provenance(server):
    from land import nspd
    nspd.search('trudovoe', {'mode': 'snapshot'})
    saved = store.get_setting('nspd_trudovoe')
    feature = saved['geojson']['features'][0]
    for _ in range(2):
        with request(server, '/api/nspd/watch', {'id': feature['id']}) as r:
            assert json.load(r)['count'] == 1
    with request(server, '/api/nspd') as r:
        state = json.load(r)
        assert state['watchlist'][0]['source_date'] == '2026-09-29'
    with request(server, '/api/nspd/export') as r:
        exported = json.load(r)
        assert len(exported['features']) == 5
        assert exported['metadata']['complete'] is False
        assert exported['metadata']['snapshot'] is True
    assert store.candidates('trudovoe') == []


def test_nspd_area_validates_and_preserves_previous(server):
    bounds = [34.202, 44.991, 34.209, 44.996]
    with request(server, '/api/nspd/area', {'bounds': bounds}) as r:
        assert json.load(r)['official_boundary'] is False
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, '/api/nspd/area', {'bounds': [0, 0, 100, 80]})
    assert exc.value.code == 400
    with request(server, '/api/nspd') as r:
        assert json.load(r)['area']['bounds'] == bounds
    assert store.layers('trudovoe') == []


def test_nspd_interrupted_attempt_not_reported_as_success(server):
    store.set_setting('nspd_attempt_trudovoe', {'state': 'running', 'started_at': store.now()})
    with request(server, '/api/nspd') as r:
        assert json.load(r)['attempt']['state'] == 'interrupted'


def test_survey_watch_rejects_stale_and_deduplicates(server):
    feature = {'type': 'Feature', 'id': 'gap-test', 'geometry': {'type':'Polygon','coordinates': [[[34,45],[34.01,45],[34.01,45.01],[34,45]]]}, 'properties': {'status':'unverified'}}
    store.set_setting('survey_trudovoe', {'id':'survey1','created_at':store.now(),'gaps':{'type':'FeatureCollection','features':[feature]},'summary':{'complete':False}})
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, '/api/survey/watch', {'survey_id':'old','id':'gap-test'})
    assert exc.value.code == 400
    for _ in range(2):
        with request(server, '/api/survey/watch', {'survey_id':'survey1','id':'gap-test'}) as r:
            assert json.load(r)['count'] == 1
    with request(server, '/api/survey/export') as r:
        assert json.load(r)['summary']['complete'] is False
    assert store.candidates('trudovoe') == []
