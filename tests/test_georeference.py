import hashlib
import pytest
from shapely.geometry import Polygon, mapping
from shapely.ops import transform
from pyproj import Transformer
from land import georeference as geo, store

CAD='90:12:171301:1630'
# Authority pipeline, independently fixed for synthetic reference data.
PIPELINE='proj=pipeline step inv proj=tmerc lat_0=0.0833333333333333 lon_0=35.5 k=1 x_0=5300000 y_0=0 ellps=krass step proj=push v_3 step proj=cart ellps=krass step proj=helmert x=23.57 y=-140.95 z=-79.8 rx=0 ry=-0.35 rz=-0.79 s=-0.22 convention=coordinate_frame step inv proj=cart ellps=WGS84 step proj=pop v_3 step proj=unitconvert xy_in=rad xy_out=deg'


def example():
    tables=[]
    for label,x,y in [(CAD+'(1)',4978800,5190000),(CAD+'(2)',4978700,5190100),(':ЗУ1',4978000,5189800)]:
        p=Polygon([(x,y),(x+10,y),(x+10,y+10),(x,y+10)])
        tables.append({'label':label,'state':'review_required','outline_xy':list(map(list,p.exterior.coords)),
                       'purpose_hint':'formed_label' if label.startswith(':ЗУ') else 'public_use_context'})
    return {'state':'extracted','document_id':'doc','title':'Synthetic plan','url':'https://trudovskoe-rk.ru/example.pdf',
            'source_sha256':'a'*64,'source_received_at':'original-date','crs_mentions':[{'label':'СК-63','page':8}], 'tables':tables}


def reference(d):
    t=Transformer.from_pipeline(PIPELINE)
    from shapely.ops import unary_union
    polygons=[transform(lambda x,y:t.transform(y,x),Polygon(x['outline_xy'])) for x in d['tables'][:2]]
    return {'source':'https://nspd.gov.ru/public-control','sha256':'b'*64,'received_at':'reference-date',
            'geojson':{'type':'FeatureCollection','features':[{'type':'Feature','geometry':mapping(unary_union(polygons)),
                        'properties':{'category':36368,'options':{'cad_num':CAD,'specified_area':200}}}]}}


def test_signed_subset_checks_both_parts_and_preview_does_not_confirm_formed_land():
    d=example();r=geo.evaluate(d,'revision',CAD,reference(d),{'id':'survey','bounds':[34.2,44.99,34.21,45]})
    assert r['state']=='consistent_with_reference' and r['boundary_distance_m']<.001
    assert r['reference_local_area_m2']==200 and len(r['part_checks'])==2
    assert r['preview_georeferenced'] and not r['geometry_confirmed']
    assert len(r['preview_geojson']['features'])==3 and r['intersecting_tables']==0
    assert sum(f['properties']['reference_table'] for f in r['preview_geojson']['features'])==2
    assert all(not f['properties']['geometry_confirmed'] and f['properties']['usage']=='research_preview_only' for f in r['preview_geojson']['features'])
    assert r['operation_code']=='EPSG:5044' and r['operation_expected_accuracy_m']==3
    assert r['source_pdf_received_at']=='original-date' and r['reference']['received_at']=='reference-date'


def test_displaced_reference_is_not_fitted_and_produces_no_preview():
    from shapely.geometry import shape
    d=example();ref=reference(d);f=ref['geojson']['features'][0]
    f['geometry']=mapping(transform(lambda x,y:(x+.001,y),shape(f['geometry'])))
    r=geo.evaluate(d,'revision',CAD,ref)
    assert r['state']=='reference_disagrees' and r['boundary_distance_m']>50
    assert not r['preview_georeferenced'] and not r['preview_geojson']['features']


def test_thin_connection_changes_hole_and_edges_despite_nearby_vertices():
    d=example();x,y=4978800,5190000
    offset=lambda ps:[(x+a,y+b) for a,b in ps]
    outline=offset([(0,0),(100,0),(100,100),(0,100),(0,90),(90,90),(90,10),(0,10)])
    d['tables'][0]['outline_xy']=list(map(list,Polygon(outline).exterior.coords))
    ref=reference(d);t=Transformer.from_pipeline(PIPELINE)
    control_local=Polygon(offset([(0,0),(100,0),(100,100),(0,100),(0,90),(.005,10),(0,10)]),
                          holes=[offset([(0,90),(90,90),(90,10),(.006,10)])])
    assert control_local.is_valid
    from shapely.geometry import shape
    from shapely.ops import unary_union
    control=transform(lambda a,b:t.transform(b,a),control_local)
    # Locate the separate small reference part without depending on collection order.
    other=min(shape(ref['geojson']['features'][0]['geometry']).geoms,key=lambda p:p.area)
    ref['geojson']['features'][0]['geometry']=mapping(unary_union([control,other]))
    r=geo.evaluate(d,'revision',CAD,ref)
    assert r['coordinates_consistent'] and r['vertex_max_distance_m']<.01
    assert r['state']=='vertices_consistent_boundary_differs' and not r['boundary_consistent']
    assert r['boundary_distance_m']>30 and r['reference_hole_count']==1 and r['preview_hole_count']==0
    assert r['preview_georeferenced'] and not r['geometry_confirmed']


def test_reference_identity_category_domain_and_ambiguous_crs_rejected():
    d=example();ref=reference(d);ref['geojson']['features'][0]['properties']['options']['cad_num']='90:12:171301:1'
    with pytest.raises(ValueError,match='единственную'):geo.evaluate(d,'revision',CAD,ref)
    ref=reference(d);ref['geojson']['features'][0]['properties']['category']=123
    with pytest.raises(ValueError,match='единственную'):geo.evaluate(d,'revision',CAD,ref)
    ref=reference(d);ref['geojson']['features'][0]['geometry']=mapping(Polygon([(40,45),(40.01,45),(40.01,45.01),(40,45.01)]))
    with pytest.raises(ValueError,match='вне области'):geo.evaluate(d,'revision',CAD,ref)
    assert len(geo.choices({'documents':[d]}))==1
    d['crs_mentions'].append({'label':'МСК-90','page':9})
    assert geo.choices({'documents':[d]})==[]


@pytest.fixture
def saved(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    d=example();raw=b'%PDF-synthetic-test';digest=hashlib.sha256(raw).hexdigest();d['source_sha256']=digest
    folder=tmp_path/'municipal';folder.mkdir();(folder/(digest+'.pdf')).write_bytes(raw)
    store.set_setting('schemes_trudovoe',{'id':'revision','documents':[d]})
    return d


def test_revision_source_hash_and_missing_control_table_guard(saved):
    params={'schemes_id':'revision','document_id':'doc'}
    assert geo.document('trudovoe',params)[2]==CAD
    with pytest.raises(ValueError,match='изменились'):geo.document('trudovoe',{**params,'schemes_id':'old'})
    (store.DATA/'municipal'/(saved['source_sha256']+'.pdf')).write_bytes(b'changed')
    with pytest.raises(ValueError,match='изменился'):geo.document('trudovoe',params)
    d=example();d['tables']=d['tables'][2:]
    assert geo.choices({'documents':[d]})==[]


def test_run_preserves_original_dates_other_sources_and_uses_one_request(saved,monkeypatch):
    calls=[]
    def receive(cad):calls.append(cad);return reference(saved)
    monkeypatch.setattr(geo,'receive_spatial',lambda doc,cad:receive(cad))
    store.set_setting('municipal_trudovoe',{'id':'municipal-source'})
    geo.run('trudovoe',{'schemes_id':'revision','document_id':'doc'})
    r=store.get_setting('georeference_trudovoe')
    assert calls==[CAD] and r['state']=='consistent_with_reference'
    assert store.get_setting('schemes_trudovoe')['documents'][0]==saved
    assert store.get_setting('municipal_trudovoe')['id']=='municipal-source' and not store.candidates('trudovoe')
    assert store.get_setting('georeference_attempt_trudovoe')['state']=='done'


def test_403_preserves_previous_result_without_retry(saved,monkeypatch):
    old={'id':'previous','state':'consistent_with_reference'};store.set_setting('georeference_trudovoe',old)
    calls=[]
    def failure(cad):calls.append(cad);raise ValueError('HTTP 403')
    monkeypatch.setattr(geo,'receive',failure)
    with pytest.raises(ValueError,match='403'):geo.run('trudovoe',{'schemes_id':'revision','document_id':'doc','reference_mode':'number'})
    assert calls==[CAD] and store.get_setting('georeference_trudovoe')==old
    assert store.get_setting('georeference_attempt_trudovoe')['state']=='error'


def test_source_changed_during_request_not_persisted(saved,monkeypatch):
    def receive(cad):
        (store.DATA/'municipal'/(saved['source_sha256']+'.pdf')).write_bytes(b'changed')
        return reference(saved)
    monkeypatch.setattr(geo,'receive_spatial',lambda doc,cad:receive(cad))
    with pytest.raises(ValueError,match='изменился'):geo.run('trudovoe',{'schemes_id':'revision','document_id':'doc'})
    assert store.get_setting('georeference_trudovoe') is None


def spatial_response(d):
    import json
    data=reference(d)['geojson']
    data['crs']={'type':'name','properties':{'name':'EPSG:4326'}}
    data['features'][0]['id']='control'
    return json.loads(json.dumps(data))


def test_control_window_uses_signed_parts_only_and_fixed_operation():
    from shapely.geometry import shape,box
    d=example();bounds,basis=geo.control_window(d,CAD)
    assert box(*bounds).covers(shape(reference(d)['geojson']['features'][0]['geometry']))
    assert basis['operation_code']=='EPSG:5044' and basis['padding_m']==25 and not basis['crs_confirmed']
    assert 0<basis['window_area_m2']<1_000_000
    d['tables'][2]['outline_xy']=[[x+50000,y+50000] for x,y in d['tables'][2]['outline_xy']]
    assert geo.control_window(d,CAD)==(bounds,basis)


@pytest.mark.parametrize('variant',['no_control','outside_domain','too_large','expanded_query'])
def test_control_window_guard_stops_before_network(variant,saved,monkeypatch):
    d=example()
    if variant=='no_control':d['tables']=d['tables'][2:]
    elif variant=='outside_domain':d['tables'][0]['outline_xy']=[[0,0],[1,0],[1,1],[0,1],[0,0]]
    else:
        x,y=4978800,5190000
        dx,dy=(915,915) if variant=='expanded_query' else (2000,2000)
        d['tables'][0]['outline_xy']=list(Polygon([(x,y),(x+dx,y),(x+dx,y+dy),(x,y+dy)]).exterior.coords)
        if variant=='expanded_query':d['tables']=d['tables'][:1]
    calls=[]
    monkeypatch.setattr(geo.nspd,'request_json',lambda *args:calls.append(args))
    with pytest.raises(ValueError,match='Итоговая' if variant=='expanded_query' else None):geo.receive_spatial(d,CAD)
    assert not calls


def test_spatial_reference_provenance_one_post_and_historical_snapshots(saved,monkeypatch):
    import json
    raw=spatial_response(saved);digest=hashlib.sha256(json.dumps(raw).encode()).hexdigest();calls=[]
    def request(url,body):calls.append((url,body));return raw,digest
    monkeypatch.setattr(geo.nspd,'request_json',request)
    monkeypatch.setattr(store,'now',lambda:'first-receipt')
    ref=geo.receive_spatial(saved,CAD)
    assert len(calls)==1 and calls[0][0]==geo.nspd.INTERSECTS and calls[0][1]['categories']==[{'id':36368}]
    assert ref['retrieval_method']=='spatial_control_window' and ref['requested_cadastral_number']==CAD
    assert ref['response_count']==1 and ref['received_at']=='first-receipt'
    path=store.DATA/'georeference_sources'/ref['source_snapshot']
    assert json.loads(path.read_text(encoding='utf-8'))['raw_response']==raw
    store.set_setting('georeference_trudovoe',{'id':'older-result'})
    monkeypatch.setattr(store,'now',lambda:'second-receipt')
    geo.run('trudovoe',{'schemes_id':'revision','document_id':'doc'})
    result=store.get_setting('georeference_trudovoe')
    assert result['previous_result_id']=='older-result' and result['reference']['received_at']=='second-receipt'
    assert result['reference']['source_snapshot']!=ref['source_snapshot'] and path.is_file()
    assert result['reference']['window_basis']['crs_confirmed'] is False and result['geometry_confirmed'] is False


@pytest.mark.parametrize('variant',['missing','duplicate','wrong_category'])
def test_spatial_reference_requires_exact_unique_number_and_category(variant,saved,monkeypatch):
    import copy
    raw=spatial_response(saved)
    if variant=='missing':raw['features'][0]['properties']['options']['cad_num']='90:12:171301:999'
    elif variant=='duplicate':
        extra=copy.deepcopy(raw['features'][0]);extra['id']='different-control-record';raw['features'].append(extra)
    else:raw['features'][0]['properties']['category']=123
    monkeypatch.setattr(geo.nspd,'request_json',lambda *args:(raw,'b'*64))
    with pytest.raises(ValueError):geo.receive_spatial(saved,CAD)
    assert not (store.DATA/'georeference_sources').exists()


def test_spatial_403_preserves_result_and_does_not_switch_to_number(saved,monkeypatch):
    old={'id':'old'};store.set_setting('georeference_trudovoe',old);calls=[]
    def request(url,body):calls.append((url,body));raise geo.nspd.StatusError(403)
    monkeypatch.setattr(geo.nspd,'request_json',request)
    monkeypatch.setattr(geo,'receive',lambda cad:pytest.fail('unexpected number fallback'))
    with pytest.raises(ValueError,match='403'):geo.run('trudovoe',{'schemes_id':'revision','document_id':'doc'})
    assert len(calls)==1 and store.get_setting('georeference_trudovoe')==old
    assert store.get_setting('georeference_attempt_trudovoe')['reference_mode']=='spatial'


def test_concurrent_survey_change_preserves_previous_georeference(saved,monkeypatch):
    old={'id':'old'};store.set_setting('georeference_trudovoe',old)
    store.set_setting('survey_trudovoe',{'id':'s1','bounds':[34.2,44.99,34.21,45]})
    monkeypatch.setattr(geo,'receive_spatial',lambda *args:reference(saved))
    original=geo.evaluate
    def evaluate(*args):
        result=original(*args);store.set_setting('survey_trudovoe',{'id':'s2'});return result
    monkeypatch.setattr(geo,'evaluate',evaluate)
    with pytest.raises(ValueError,match='Обследование изменилось'):geo.run('trudovoe',{'schemes_id':'revision','document_id':'doc'})
    assert store.get_setting('georeference_trudovoe')==old
