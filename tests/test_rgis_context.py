import copy
import json

import pytest
from shapely.geometry import box, mapping, shape

from land import recon, rgis_context as rgis, store
from land.geometry import convert

BOUNDS=[34.20,44.99,34.21,45.0]


def payload(mode='functional',world=None):
    g=convert(world if world is not None else box(34.202,44.992,34.208,44.998),4326,3857)
    return {'type':'FeatureCollection','totalFeatures':1,'numberMatched':1,'numberReturned':1,
            'crs':{'type':'name','properties':{'name':'urn:ogc:def:crs:EPSG::3857'}},
            'timeStamp':'2099-01-01T00:00:00Z','features':[{'type':'Feature','id':rgis.TYPES[mode]+'.1',
            'geometry':json.loads(json.dumps(mapping(g))),'properties':{'SUBSUBTYPE':1050101200,'NAME':'<script>name</script>','created_us':'not-exported'}}]}


def result(bounds=BOUNDS):
    return {'version':rgis.VERSION,'id':'rgis-one','bounds':bounds,'catalog':rgis.CATALOG,
            'layers':{mode:{'received_at':'received-date','sha256':'source-hash','geojson':rgis.normalize(payload(mode),bounds,mode)} for mode in rgis.TYPES}}


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init();return tmp_path


def test_explicit_response_crs_projects_coordinates_and_keeps_raw_codes_without_guessing_names():
    data=payload();before=copy.deepcopy(data)
    fc=rgis.normalize(data,BOUNDS,'functional');f=fc['features'][0]
    assert shape(f['geometry']).bounds==pytest.approx((34.202,44.992,34.208,44.998),abs=1e-9)
    assert f['properties']['SUBSUBTYPE']==1050101200 and 'created_us' not in f['properties']
    assert data==before


@pytest.mark.parametrize('change',[
    {'numberMatched':2},{'totalFeatures':'unknown'},{'numberReturned':True},
    {'crs':None},{'crs':{'type':'name','properties':{'name':'EPSG:4326'}}},
    {'next':'page2'},{'links':[{'rel':'next','href':'https://bad.invalid'}]},
])
def test_ambiguous_crs_counts_or_pages_rejected(change):
    data=payload();data.update(change)
    with pytest.raises(ValueError):rgis.normalize(data,BOUNDS,'functional')


def test_empty_response_with_explicit_zero_counts_is_observation_without_geometry():
    data={'type':'FeatureCollection','features':[],'totalFeatures':0,'numberMatched':0,'numberReturned':0}
    assert rgis.normalize(data,BOUNDS,'settlements')['features']==[]


@pytest.mark.parametrize('mode',['duplicate','wrong_layer','outside','invalid','coordinate_limit','count_limit'])
def test_bad_geometry_source_or_limits_stop_normalization(mode,monkeypatch):
    data=payload()
    if mode=='duplicate':data['features']*=2;data.update(totalFeatures=2,numberMatched=2,numberReturned=2)
    elif mode=='wrong_layer':data['features'][0]['id']='UDS_polygon.1'
    elif mode=='outside':data=payload(world=box(35,45,35.1,45.1))
    elif mode=='invalid':data['features'][0]['geometry']['coordinates'][0][0]=(float('nan'),0)
    elif mode=='coordinate_limit':monkeypatch.setattr(rgis,'MAX_COORDINATES',3)
    elif mode=='count_limit':monkeypatch.setattr(rgis,'MAX_FEATURES',1)
    with pytest.raises(ValueError):rgis.normalize(data,BOUNDS,'functional')


def test_collect_preserves_provenance_and_does_not_treat_server_future_clock_as_currentness(db,monkeypatch):
    calls=[]
    def fetch(url,**kwargs):
        calls.append(url);mode=next(mode for mode,name in rgis.TYPES.items() if name in url)
        return json.dumps(payload(mode)).encode(),'application/json',200
    monkeypatch.setattr(rgis.network,'fetch',fetch);monkeypatch.setattr(rgis.time,'sleep',lambda _:None)
    r=rgis.collect(BOUNDS)
    assert len(calls)==3 and all('EPSG%3A3857' in url and 'maxFeatures=1000' in url for url in calls)
    assert all(l['server_clock_warning'] and l['response_count_consistent'] and l['source_crs_confirmed'] for l in r['layers'].values())
    assert all(not l['coverage_confirmed'] and not l['currentness_confirmed'] for l in r['layers'].values())
    assert not r['territorial_zone_confirmed'] and not r['currentness_confirmed']
    assert len(list((db/'rgis'/'raw').glob('*.json')))==3
    assert r['catalog']==rgis.CATALOG


def test_403_stops_remaining_requests_and_preserves_prior_context(db,monkeypatch):
    old=result();store.set_setting('rgis_context_trudovoe',old);calls=[]
    def fetch(url,**kwargs):
        calls.append(url);raise ValueError('HTTP 403: access restricted')
    monkeypatch.setattr(rgis.network,'fetch',fetch)
    with pytest.raises(ValueError,match='403'):rgis.run('trudovoe',{'bounds':BOUNDS})
    assert len(calls)==1 and store.get_setting('rgis_context_trudovoe')==json.loads(json.dumps(old))
    assert store.get_setting('rgis_attempt_trudovoe')['state']=='error'


def test_context_for_another_area_does_not_attach_geometry_to_candidates():
    r=result();assert all(not rows for rows in rgis.projected(r,[34.3,45,34.31,45.01],recon.metric_for(BOUNDS)).values())
    assert not rgis.sources(r,[34.3,45,34.31,45.01])['applied']


def test_genplan_matches_never_change_layout_zone_rights_or_access_and_retain_source():
    from test_recon import fixture
    values=fixture();params=recon.options({'purpose':'housing'});before=recon.build(values,params)
    context=result();values['rgis_context']=context;after=recon.build(values,params)
    assert {c['id']:c['geometry'] for c in before['candidates']}=={c['id']:c['geometry'] for c in after['candidates']}
    assert before['summary']==after['summary']
    assert any(c['general_plan_matches']['functional'] for c in after['candidates'])
    assert after['sources']['rgis_context']['applied']
    for c in after['candidates']:
        assert not c['matches']['pzz'] and not c['rights_confirmed'] and not c['srzu_ready']
        assert not c['access_evidence']['legal_access_confirmed']
        for m in c['general_plan_matches']['functional']:
            assert m['sha256']=='source-hash' and m['received_at']=='received-date'
            assert not m['territorial_zone_confirmed'] and not m['currentness_confirmed']
    after.update(id='saved',operation_warnings=[])
    data={'result':after,'stale':False};report=recon.html_report(data).decode()
    assert '&lt;script&gt;name' in report and '<script>name' not in report
    assert '1050101200' in report and 'не являются территориальными зонами ПЗЗ' in report
    assert recon.collection(data)['features'][0]['properties']['general_plan_matches']==after['candidates'][0]['general_plan_matches']
