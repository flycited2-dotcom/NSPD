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


def test_municipal_export_marks_interrupted_attempt_and_survey(server):
    store.set_setting('municipal_trudovoe', {'id':'m1','complete':False,'items':[],'survey_id':'old'})
    store.set_setting('municipal_attempt_trudovoe', {'state':'running'})
    store.set_setting('municipal_ocr_attempt_trudovoe', {'state':'running','network_requests':0})
    store.set_setting('survey_trudovoe', {'id':'current'})
    with request(server, '/api/municipal/export') as r:
        body = json.load(r)
        assert 'municipal-evidence.json' in r.headers['Content-Disposition']
        assert body['result']['complete'] is False
        assert body['attempt']['state'] == 'interrupted'
        assert body['ocr_attempt']['state'] == 'interrupted'
        assert body['ocr_attempt']['network_requests'] == 0
        assert body['current_survey_id'] == 'current'
    assert store.candidates('trudovoe') == []


def test_ocr_preview_is_bound_to_current_catalog_and_image(server):
    import hashlib
    raw = b'\x89PNG\r\n\x1a\nlocal-test-image'
    digest = hashlib.sha256(b'local-pdf').hexdigest()
    image_hash = hashlib.sha256(raw).hexdigest()
    folder = store.DATA / 'ocr' / digest
    folder.mkdir(parents=True)
    (folder / 'p1-v0.png').write_bytes(raw)
    row = {'id':'doc','sha256':digest,'ocr':{'source_sha256':digest,'pages':[
        {'page':1,'state':'received','views':[{'image_sha256':image_hash}]}]}}
    store.set_setting('municipal_trudovoe', {'items':[row]})
    with request(server, '/api/municipal/ocr/page?document=doc&page=1&view=0') as r:
        assert r.headers['Content-Type'] == 'image/png'
        assert r.read() == raw
    for suffix in ['document=other&page=1&view=0','document=doc&page=2&view=0',
                   'document=doc&page=1&view=4','document=doc&page=oops&view=0']:
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, '/api/municipal/ocr/page?' + suffix)
        assert exc.value.code == 400
    (folder / 'p1-v0.png').write_bytes(b'changed')
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, '/api/municipal/ocr/page?document=doc&page=1&view=0')
    assert exc.value.code == 400


def test_schemes_export_is_local_coordinates_and_marks_stale_interrupted(server):
    from land import schemes
    table=schemes.extract([(19,'Обозначение земельного участка :ЗУ1\nКоординаты, м\nX Y\n1 2 3\nн1 100,0 100,0\nн2 120,0 100,0\nн3 120,0 120,0\n')])
    store.set_setting('municipal_trudovoe',{'id':'current','items':[]})
    store.set_setting('schemes_trudovoe',{'catalog_id':'old','documents':[dict(table,document_id='doc')]})
    store.set_setting('schemes_attempt_trudovoe',{'state':'running','network_requests':0})
    with request(server,'/api/schemes/export') as r:
        body=json.load(r)
        assert 'scheme-coordinates.json' in r.headers['Content-Disposition']
        assert body['attempt']['state']=='interrupted' and body['remaining']==0
        assert body['current_catalog_id']=='current' and body['result']['catalog_id']=='old'
        t=body['result']['documents'][0]['tables'][0]
        assert not t['georeferenced'] and not t['geometry_confirmed']
        assert 'coordinates' not in t and 'outline_xy' in t
    assert store.candidates('trudovoe')==[]


def test_torgi_documents_export_preserves_scope_and_interruption(server):
    store.set_setting('torgi_trudovoe',{'id':'current','created_at':'new-date','lots':[{'id':'lot'}]})
    store.set_setting('torgi_documents_trudovoe',{'id':'docs','search_id':'old','search_created_at':'old-date','cards':[],
                      'files':{'key':{'state':'read','eligible':True,'associations':[{'scope':'notice','lot_id':'lot'}],
                                      'sha256':'local','source_hash':'declared','source_hash_algorithm':'not_specified','geometry_confirmed':False}}})
    store.set_setting('torgi_documents_attempt_trudovoe',{'state':'running'})
    store.set_setting('torgi_files_attempt_trudovoe',{'state':'running'})
    with request(server,'/api/torgi/documents/export') as r:
        body=json.load(r)
        assert 'torgi-documents.json' in r.headers['Content-Disposition']
        assert body['attempt']['state']==body['reading_attempt']['state']=='interrupted'
        assert body['cards_remaining']==1 and body['files_remaining']==0
        assert body['current_search_id']=='current'
        f=body['result']['files']['key']
        assert f['associations'][0]['scope']=='notice' and not f['geometry_confirmed']
        assert f['sha256']!=f['source_hash']
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server,'/api/torgi/documents/read',{'id':'docs'},token=False)
    assert exc.value.code==403
