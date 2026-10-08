import json
import pytest
from shapely.geometry import box, LineString, mapping
from land import nspd, nspd_context, recon, store
from test_recon import fixture, BOUNDS, feature


def context(mode,geom,fields=None,bounds=None):
    f=feature(geom,'context');f['properties']['options']=fields or {}
    return {'id':'context','bounds':bounds or BOUNDS,'layers':{mode:{'state':'received','received_at':'context-date','sha256':'context-hash','geojson':{'type':'FeatureCollection','features':[f]}}}}


def payload(geom,options=None,category=1):
    f=feature(geom,1);f['geometry']['crs']={'type':'name','properties':{'name':'EPSG:4326'}}
    f['properties'].update(category=category,options=options or {})
    return {'type':'FeatureCollection','features':[f]}


def test_catalog_categories_are_observed_not_guessed():
    rows=nspd_context.catalog()
    assert {k:v['categoryId'] for k,v in rows.items()}=={'settlements':472812,'quarters':36381,'schemes':38943,'planned_parcels':37158,'red_lines':38942,'water':472813,'forests':472847,'protected':472825,'heritage':472820}


def test_red_lines_explicitly_allow_lines_while_primary_normalizer_rejects_them():
    data=payload(LineString([(34.20,44.99),(34.21,45.0)]),{'owner':'secret','cnt_land_not_geom':11})
    with pytest.raises(ValueError):nspd.normalize(data)
    result=nspd.normalize(data,('LineString','MultiLineString'),nspd_context.FIELDS)
    fields=result['features'][0]['properties']['options']
    assert 'owner' not in fields and fields['cnt_land_not_geom']==11


def test_collection_distinguishes_empty_received_error_and_no_retry(tmp_path,monkeypatch):
    calls=[]
    def request(url,body):
        category=body['categories'][0]['id'];calls.append(category)
        if category==38943:raise nspd.StatusError(403)
        data=payload(LineString([(34.20,44.99),(34.21,45.0)])) if category==38942 else {'type':'FeatureCollection','features':[]}
        return data,'hash-'+str(category)
    monkeypatch.setattr(nspd,'request_json',request);monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    result=nspd_context.collect(BOUNDS);assert calls==[472812,36381,38943]
    assert result['layers']['schemes']['state']=='error' and '403' in result['layers']['schemes']['error']
    assert result['layers']['settlements']['state']=='received' and result['layers']['settlements']['count']==0
    for mode in ('planned_parcels','red_lines',*nspd_context.ENVIRONMENT):
        row=result['layers'][mode]
        assert row['state']=='not_requested' and not row['requested']
        assert row['checked_at'] and '403' in row['stopped_reason']
        assert 'received_at' not in row and 'count' not in row and 'geojson' not in row
    exposed=nspd_context.sources(result,BOUNDS)['layers']['red_lines']
    assert exposed['state']=='not_requested' and '403' in exposed['stopped_reason'] and not exposed['requested']
    assert not result['complete']
    nspd_context.save('trudovoe',result)
    assert json.loads((tmp_path/'nspd_context'/(result['id']+'.json')).read_text(encoding='utf-8'))==json.loads(json.dumps(result))


def test_failed_update_keeps_known_footprint_with_original_date_only_for_same_area(monkeypatch):
    old=context('schemes',box(*BOUNDS));prior=json.loads(json.dumps(old))
    monkeypatch.setattr(nspd,'request_json',lambda *a:(_ for _ in ()).throw(nspd.StatusError(403)))
    result=nspd_context.collect(BOUNDS,old);layer=result['layers']['schemes']
    assert layer['state']=='retained' and layer['received_at']=='context-date' and layer['sha256']=='context-hash'
    assert layer['checked_at']!='context-date' and '403' in layer['error'] and json.loads(json.dumps(old))==prior
    values=fixture();values['nspd_context']=result
    excluded=recon.build(values,recon.options({}));assert excluded['summary']['drafts']==0
    assert any('прежнее датированное' in f for c in excluded['candidates'] for f in c['flags'])
    wrong=nspd_context.collect([35,45,35.01,45.01],old)
    assert wrong['layers']['settlements']['state']=='error'
    assert all(l['state']=='not_requested' for mode,l in wrong['layers'].items() if mode!='settlements')
    assert all('geojson' not in l and 'received_at' not in l for l in wrong['layers'].values())


@pytest.mark.parametrize('data',[{'type':'FeatureCollection','features':[],'numberMatched':1},payload(box(*BOUNDS))])
def test_unconfirmed_crs_or_partial_response_not_accepted(data,monkeypatch):
    if data.get('features'):data['features'][0]['geometry'].pop('crs')
    calls=[]
    def request(*args):
        calls.append(args);return data,'hash'
    monkeypatch.setattr(nspd,'request_json',request)
    result=nspd_context.collect(BOUNDS)
    assert len(calls)==1 and result['layers']['settlements']['state']=='error'
    assert result['layers']['settlements']['http_status']==200
    assert all(row['state']=='not_requested' for mode,row in result['layers'].items() if mode!='settlements')
    assert all('geojson' not in row for row in result['layers'].values())


def test_successful_categories_validate_geometry_and_keep_empty_observations(monkeypatch):
    calls=[]
    def request(url,body):
        category=body['categories'][0]['id'];calls.append(category)
        data=payload(LineString([(34.20,44.99),(34.21,45.0)]),category=category) if category==38942 else {'type':'FeatureCollection','features':[]}
        return data,'hash-'+str(category)
    monkeypatch.setattr(nspd,'request_json',request)
    result=nspd_context.collect(BOUNDS)
    assert calls==[r['categoryId'] for r in nspd_context.catalog().values()]
    assert all(row['state']=='received' and row['requested'] for row in result['layers'].values())
    assert result['layers']['red_lines']['geojson']['features'][0]['geometry']['type']=='LineString'
    assert result['layers']['red_lines']['count']==1
    assert result['layers']['settlements']['count']==0 and not result['complete']


@pytest.mark.parametrize('status',[401,403,429])
@pytest.mark.parametrize('failure_index',[0,2])
def test_access_rejection_stops_first_or_middle_request(monkeypatch,status,failure_index):
    calls=[];categories=[r['categoryId'] for r in nspd_context.catalog().values()]
    def request(url,body):
        category=body['categories'][0]['id'];calls.append(category)
        if category==categories[failure_index]:raise nspd.StatusError(status)
        return payload(box(*BOUNDS),category=category),'hash-'+str(category)
    monkeypatch.setattr(nspd,'request_json',request)
    result=nspd_context.collect(BOUNDS)
    assert calls==categories[:failure_index+1]
    rows=list(result['layers'].values())
    assert all(row['state']=='received' for row in rows[:failure_index])
    assert rows[failure_index]['state']=='error' and rows[failure_index]['http_status']==status
    assert all(row['state']=='not_requested' and not row['requested'] for row in rows[failure_index+1:])
    assert all('received_at' not in row and 'geojson' not in row and 'count' not in row for row in rows[failure_index:])
    assert not result['complete']


def test_transport_failure_stops_and_retains_positive_geometry_without_renewing_dates(monkeypatch):
    old={'id':'old','bounds':BOUNDS,'layers':{}}
    for mode in nspd_context.TITLES:
        old['layers'].update(context(mode,box(*BOUNDS))['layers'])
        old['layers'][mode].update(count=1,received_at='2026-10-07T19:17:02+00:00',sha256='old-'+mode)
    prior=json.loads(json.dumps(old));calls=[]
    def request(url,body):
        category=body['categories'][0]['id'];calls.append(category)
        if category==36381:raise ValueError('НСПД не завершила запрос за отведённое время')
        return payload(box(*BOUNDS),category=category),'fresh-hash'
    monkeypatch.setattr(nspd,'request_json',request)
    monkeypatch.setattr(store,'now',lambda:'2026-10-08T17:40:00+00:00')
    result=nspd_context.collect(BOUNDS,old)
    assert calls==[472812,36381]
    assert result['layers']['settlements']['state']=='received'
    assert result['layers']['settlements']['received_at']=='2026-10-08T17:40:00+00:00'
    for mode,row in result['layers'].items():
        if mode=='settlements':continue
        assert row['state']=='retained' and row['received_at']==old['layers'][mode]['received_at']
        assert row['sha256']==old['layers'][mode]['sha256'] and row['geojson']==old['layers'][mode]['geojson']
        assert row['retained_from_context_id']=='old' and row['checked_at']=='2026-10-08T17:40:00+00:00'
        assert row['requested']==(mode=='quarters') and 'отведённое время' in row['stopped_reason']
        assert not row['complete']
    assert json.loads(json.dumps(old))==prior and not result['complete']


def test_empty_old_observation_is_not_retained_as_positive_geometry(monkeypatch):
    old={'id':'old','bounds':BOUNDS,'layers':{'water':{'state':'received','count':0,
        'received_at':'old-date','sha256':'old-hash','geojson':{'type':'FeatureCollection','features':[]}}}}
    calls=[]
    def request(*args):
        calls.append(args);raise nspd.StatusError(403)
    monkeypatch.setattr(nspd,'request_json',request)
    result=nspd_context.collect(BOUNDS,old)
    row=result['layers']['water']
    assert len(calls)==1 and row['state']=='not_requested'
    assert 'received_at' not in row and 'count' not in row and 'geojson' not in row


def test_wrong_category_is_rejected_before_remaining_categories_are_requested(monkeypatch):
    calls=[]
    def request(url,body):
        calls.append(body['categories'][0]['id'])
        return payload(box(*BOUNDS),category=36368),'wrong-parcel-response'
    monkeypatch.setattr(nspd,'request_json',request)
    result=nspd_context.collect(BOUNDS)
    assert calls==[472812]
    assert 'Категория' in result['layers']['settlements']['error']
    assert result['layers']['settlements']['state']=='error' and result['layers']['settlements']['http_status']==200
    assert all(row['state']=='not_requested' for mode,row in result['layers'].items() if mode!='settlements')
    assert all('geojson' not in row and 'received_at' not in row for row in result['layers'].values())


@pytest.mark.parametrize('bad_response',[
    {'type':'FeatureCollection','features':[],'numberMatched':1},
    payload(box(*BOUNDS),category=36368),
])
def test_middle_validation_failure_retains_failed_and_skipped_observations(monkeypatch,bad_response):
    old=context('quarters',box(*BOUNDS));old['layers'].update(context('water',box(*BOUNDS))['layers'])
    for mode,row in old['layers'].items():row.update(count=1,sha256='old-'+mode)
    calls=[]
    def request(url,body):
        category=body['categories'][0]['id'];calls.append(category)
        return (bad_response,'invalid-hash') if category==36381 else ({'type':'FeatureCollection','features':[]},'empty-hash')
    monkeypatch.setattr(nspd,'request_json',request)
    result=nspd_context.collect(BOUNDS,old)
    assert calls==[472812,36381]
    exposed=nspd_context.sources(result,BOUNDS)['layers']
    for mode in ('quarters','water'):
        row=result['layers'][mode]
        assert row['state']=='retained' and row['geojson']==old['layers'][mode]['geojson']
        assert row['received_at']=='context-date' and row['sha256']=='old-'+mode
        assert exposed[mode]['requested']==(mode=='quarters') and exposed[mode]['stopped_reason']
    assert exposed['quarters']['http_status']==200 and exposed['water']['http_status'] is None


@pytest.mark.parametrize('mode',['schemes','planned_parcels'])
def test_planned_footprints_excluded_optionally_and_not_claimed_to_be_legally_unavailable(mode):
    values=fixture();values['nspd_context']=context(mode,box(*BOUNDS))
    result=recon.build(values,recon.options({}))
    assert not result['summary']['drafts'] and all(c['context_matches'][mode] for c in result['candidates'])
    included=recon.build(values,recon.options({'avoid_planned':False}))
    assert included['summary']['drafts'] and all(not c['rights_confirmed'] for c in included['candidates'])
    assert all(c['context_matches'][mode] and not c['context_matches'][mode][0]['applies_legally_confirmed'] for c in included['candidates'])


@pytest.mark.parametrize('mode',nspd_context.ENVIRONMENT)
def test_environment_exclusion_is_independent_of_planned_switch_and_does_not_infer_legal_ban(mode):
    values=fixture();values['nspd_context']=context(mode,box(*BOUNDS))
    excluded=recon.build(values,recon.options({'avoid_planned':False}))
    assert not excluded['summary']['drafts'] and all(c['context_matches'][mode] for c in excluded['candidates'])
    included=recon.build(values,recon.options({'avoid_environment':False}))
    assert included['summary']['drafts'] and all(not c['rights_confirmed'] for c in included['candidates'])
    assert all(not c['context_matches'][mode][0]['applies_legally_confirmed'] for c in included['candidates'])


def test_old_context_missing_environment_is_not_reported_as_empty_received_layer():
    old=context('quarters',box(*BOUNDS));result=nspd_context.sources(old,BOUNDS)
    assert result['applied'] and result['layers']['water']['state']=='not_available'
    assert result['layers']['water']['title']==nspd_context.TITLES['water'] and 'count' not in result['layers']['water']


def test_algorithm_revision_invalidates_results_and_saved_candidates_even_if_sources_unchanged(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    for k,v in fixture().items():store.set_setting(k+'_trudovoe',v)
    recon.run('trudovoe',{'bounds':BOUNDS,'refresh_nspd':False})
    result=recon.report('trudovoe')['result'];recon.watch('trudovoe',{'result_id':result['id'],'id':result['candidates'][0]['id']})
    monkeypatch.setattr(recon,'VERSION',recon.VERSION+1)
    data=recon.report('trudovoe');assert data['stale'] and data['watchlist'][0]['stale']
    assert recon.load('trudovoe',result['id'])['stale']
    with pytest.raises(ValueError):recon.watch('trudovoe',{'result_id':result['id'],'id':result['candidates'][0]['id']})


def test_unrelated_bounds_never_apply_cached_planned_footprint_or_counters():
    values=fixture();values['nspd_context']=context('schemes',box(*BOUNDS),bounds=[35,45,35.01,45.01])
    result=recon.build(values,recon.options({}))
    assert result['summary']['drafts'] and not result['sources']['context']['applied']
    assert not any(c['context_matches']['schemes'] for c in result['candidates'])


def test_quarter_counters_are_whole_quarter_risk_not_located_missing_parcels():
    values=fixture();values['nspd_context']=context('quarters',box(*BOUNDS),{'cad_num':'90:12:170102','cnt_land_not_geom':11})
    result=recon.build(values,recon.options({}))
    for c in result['candidates']:
        q=c['context_matches']['quarters'][0]
        assert q['counter_scope']=='whole_quarter' and q['parcels_without_geometry']==11 and not q['aggregate_zero_quarter']
        assert any('положение неизвестно' in f for f in c['flags']) and not c['rights_confirmed']
    html=recon.html_report({'result':result}).decode()
    assert 'счётчики целого квартала' in html and 'context-date' in html
    values['nspd_context']['layers']['quarters']['geojson']['features'][0]['properties']['options']['cad_num']='90:12:000000'
    zero=recon.build(values,recon.options({}))['candidates'][0]
    assert zero['context_matches']['quarters'][0]['aggregate_zero_quarter'] and any('Сводный нулевой квартал' in f for f in zero['flags'])


def test_red_line_crossing_and_settlement_containment_do_not_confirm_planning_compliance():
    values=fixture();first=recon.build(values,recon.options({}))['candidates'][0];g=box(*BOUNDS)
    line=LineString([(BOUNDS[0],first['point'][1]),(BOUNDS[2],first['point'][1])])
    c=context('settlements',g);c['layers'].update(context('red_lines',line)['layers'])
    values['nspd_context']=c;result=recon.build(values,recon.options({}))
    assert all(row['context_matches']['settlements'][0]['spatially_inside'] for row in result['candidates'])
    assert all(not row['context_matches']['settlements'][0]['applies_legally_confirmed'] for row in result['candidates'])
    assert any(row['context_matches']['red_lines'] for row in result['candidates'])
    for row in result['candidates']:
        for red in row['context_matches']['red_lines']:assert red['length_m']>=0 and not red['applies_legally_confirmed']
    assert not result['sources']['planning']['geometry_confirmed'] and result['summary']['confirmed_free']==0


def test_partial_settlement_overlap_is_not_containment():
    values=fixture();first=recon.build(values,recon.options({}))['candidates'][0]
    x,y=first['point'];values['nspd_context']=context('settlements',box(x,y,BOUNDS[2],BOUNDS[3]))
    result=recon.build(values,recon.options({}));c=next(c for c in result['candidates'] if c['id']==first['id'])
    assert c['context_matches']['settlements'] and not c['context_matches']['settlements'][0]['spatially_inside']
    assert any('не целиком' in f for f in c['flags'])


@pytest.mark.parametrize('value',[True,-1,'bad','NaN',1.2])
def test_invalid_counters_not_interpreted_as_missing_parcels(value):
    assert nspd_context.nonnegative_count(value) is None


def test_fresh_workflow_collects_context_and_partial_failure_keeps_candidates(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    values=fixture();store.set_setting('survey_trudovoe',values['survey'])
    monkeypatch.setattr(recon.survey,'run',lambda *a:None)
    def request(url,body):
        if body['categories'][0]['id']==38943:raise nspd.StatusError(403)
        return {'type':'FeatureCollection','features':[]},'hash'
    monkeypatch.setattr(nspd,'request_json',request)
    recon.run('trudovoe',{'bounds':BOUNDS})
    data=recon.report('trudovoe');assert data['result']['summary']['drafts'] and not data['stale']
    assert any('403' in w for w in data['result']['operation_warnings'])
    store.set_setting('nspd_context_trudovoe',{'id':'changed'})
    assert recon.report('trudovoe')['stale']
