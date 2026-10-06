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


def test_municipal_local_export_and_protected_endpoint(server):
    store.set_setting('municipal_trudovoe',{'id':'m1','items':[]})
    store.set_setting('municipal_local_attempt_trudovoe',{'state':'running'})
    with request(server,'/api/municipal/export?project=trudovoe') as r:
        data=json.load(r)
    assert data['local_remaining']==0 and data['local_attempt']['state']=='interrupted'
    with pytest.raises(urllib.error.HTTPError) as error:
        request(server,'/api/municipal/local',{'project':'trudovoe','id':'m1'},token=False)
    assert error.value.code==403


def test_district_report_and_protected_operations(server, monkeypatch):
    from land import planning_watch
    with request(server,'/api/planning/export') as r:
        assert 'district-document-versions.json' in r.headers['Content-Disposition']
        assert json.load(r)['result'] is None
    with request(server,'/api/planning/report') as r:
        assert 'text/html' in r.headers['Content-Type']
        assert 'Документы района'.encode() in r.read()
    for path in ('/api/planning/catalog','/api/planning/read'):
        with pytest.raises(urllib.error.HTTPError) as error:
            request(server,path,{},token=False)
        assert error.value.code==403
    monkeypatch.setattr(planning_watch,'catalog',lambda project:{'documents':2})
    with request(server,'/api/planning/catalog',{'project':'trudovoe'}) as r:
        job_id=json.load(r)['job_id']
    import time
    for _ in range(30):
        with request(server,'/api/jobs/'+job_id) as r:
            job=json.load(r)
        if job['state']!='running':break
        time.sleep(.01)
    assert job['state']=='done' and job['result']=={'documents':2}


def test_municipal_large_export_and_protected_endpoint(server):
    store.set_setting('municipal_trudovoe',{'id':'m1','items':[]})
    store.set_setting('municipal_large_attempt_trudovoe',{'state':'running'})
    with request(server,'/api/municipal/export?project=trudovoe') as r:
        data=json.load(r)
    assert data['large_remaining']==0 and data['large_retry_remaining']==0
    assert data['large_attempt']['state']=='interrupted'
    with pytest.raises(urllib.error.HTTPError) as error:
        request(server,'/api/municipal/large',{'project':'trudovoe','id':'m1'},token=False)
    assert error.value.code==403


def test_scan_drafts_export_keeps_stale_source_and_protects_mutation(server):
    store.set_setting('municipal_trudovoe',{'id':'current','items':[]})
    store.set_setting('scan_tables_trudovoe',{'catalog_id':'old','pages':[],'geometry_confirmed':False})
    store.set_setting('scan_tables_attempt_trudovoe',{'state':'running','network_requests':0})
    with request(server,'/api/scan-tables/export') as r:
        body=json.load(r)
        assert 'scan-table-drafts.json' in r.headers['Content-Disposition']
        assert body['result']['catalog_id']=='old' and body['current_catalog_id']=='current'
        assert body['choices']==[] and body['attempt']['state']=='interrupted'
        assert not body['result']['geometry_confirmed']
    with pytest.raises(urllib.error.HTTPError) as error:
        request(server,'/api/scan-tables',{'catalog_id':'current','document_id':'unknown','page':1},token=False)
    assert error.value.code==403


def test_scan_cells_and_review_export_and_write_guards(server):
    store.set_setting('municipal_trudovoe',{'id':'current','items':[]})
    store.set_setting('scan_cells_trudovoe',{'rows':[],'id':'cells'})
    store.set_setting('scan_reviews_trudovoe',{'tables':[],'id':'reviews'})
    store.set_setting('scan_cells_attempt_trudovoe',{'state':'running','network_requests':0})
    with request(server,'/api/scan-tables/export') as r:
        body=json.load(r)
    assert body['cells']['id']=='cells' and body['reviews']['id']=='reviews'
    assert body['cells_attempt']['state']=='interrupted' and body['page_fingerprints']==[]
    for path in ('/api/scan-tables/cells','/api/scan-tables/review'):
        with pytest.raises(urllib.error.HTTPError) as error:request(server,path,{'draft_id':'old'},token=False)
        assert error.value.code==403


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
        review=body['result']['documents'][0]['review']
        assert review['accepted_count']==1 and not review['geometry_confirmed']
        assert 'union_rings_xy' in review and 'geojson' not in review
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


def test_torgi_local_reprocess_export_queue_and_post_guard(server):
    store.set_setting('torgi_trudovoe',{'id':'search','created_at':'date','lots':[]})
    store.set_setting('torgi_documents_trudovoe',{'id':'docs','search_id':'search','search_created_at':'date','cards':[],
                      'files':{'key':{'state':'read','format':'pdf','algorithm':'old','received_at':'source-date','eligible':True,
                                      'egrn_tables':[{'outline_xy':[[1,2]],'geometry_confirmed':False,'georeferenced':False}]},
                               'docx':{'state':'rejected','format':'docx','eligible':True,'received_at':'docx-source-date',
                                       'sha256':'a'*64,'error':'Основной XML DOCX превышает лимит'}}})
    store.set_setting('torgi_reprocess_attempt_trudovoe',{'state':'running','network_requests':0})
    with request(server,'/api/torgi/documents/export') as r:
        body=json.load(r)
        assert body['files_to_reprocess']==2 and body['reprocess_attempt']['state']=='interrupted'
        assert body['result']['files']['key']['received_at']=='source-date'
        assert not body['result']['files']['key']['egrn_tables'][0]['georeferenced']
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server,'/api/torgi/documents/reprocess',{'id':'docs'},token=False)
    assert exc.value.code==403


def test_torgi_visual_queue_interrupted_export_and_preview_guards(server):
    store.set_setting('torgi_documents_trudovoe',{'id':'docs','cards':[],'files':{
        'image':{'key':'image','state':'read','format':'jpg','eligible':True,'sha256':'a'*64,'received_at':'original'},
        'pdf':{'key':'pdf','state':'read','format':'pdf','eligible':True,'sha256':'b'*64,'processed_pages':2,
               'image_or_sparse_pages':[1,2]}}})
    store.set_setting('torgi_visual_attempt_trudovoe',{'state':'running','network_requests':0})
    with request(server,'/api/torgi/documents/export') as r:
        data=json.load(r)
        assert data['visual_remaining']==3 and data['visual_attempt']['state']=='interrupted'
        assert data['result']['files']['image']['received_at']=='original'
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server,'/api/torgi/documents/ocr',{'id':'docs'},token=False)
    assert exc.value.code==403
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server,'/api/torgi/documents/image?file=unknown')
    assert exc.value.code==400


def test_georeference_export_keeps_preview_status_and_revisions(server):
    store.set_setting('schemes_trudovoe',{'id':'new-schemes','documents':[]})
    store.set_setting('survey_trudovoe',{'id':'new-survey'})
    store.set_setting('georeference_trudovoe',{'id':'preview','schemes_id':'old','survey_id':'old-survey',
                      'geometry_confirmed':False,'preview_georeferenced':True,'operation_code':'EPSG:5044'})
    store.set_setting('georeference_attempt_trudovoe',{'state':'running'})
    with request(server,'/api/georeference/export') as r:
        body=json.load(r)
        assert 'scheme-georeference-preview.json' in r.headers['Content-Disposition']
        assert body['attempt']['state']=='interrupted'
        assert body['current_schemes_id']=='new-schemes' and body['current_survey_id']=='new-survey'
        assert body['result']['preview_georeferenced'] and not body['result']['geometry_confirmed']
    with pytest.raises(urllib.error.HTTPError) as exc:request(server,'/api/georeference',{},token=False)
    assert exc.value.code==403
