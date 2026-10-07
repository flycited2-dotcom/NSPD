import copy
import json
from datetime import datetime,timedelta,timezone
import pytest
from shapely.geometry import box,shape,mapping
from land import recon,survey,store,torgi

BOUNDS=[34.20,44.99,34.21,45.0]

def feature(g,id='parcel',number=None):
    return {'type':'Feature','id':id,'geometry':mapping(g),'properties':{'options':{'cad_num':number} if number else {}}}

def fixture():
    layers={mode:{'geojson':{'type':'FeatureCollection','features':[]},'received_at':'source-date','sha256':'source-hash','source':recon.nspd.INTERSECTS} for mode in survey.MODES}
    layers['parcels']['geojson']['features']=[feature(box(34.204,44.989,34.206,45.001),number='90:12:1:1')]
    layers['buildings']['geojson']['features']=[feature(box(34.207,44.993,34.208,44.994),'building')]
    gaps,summary=survey.gaps(BOUNDS,layers,{'min_area':400,'max_area':2500,'min_width':12})
    return dict.fromkeys(recon.KEYS) | {'survey':{'id':'survey','bounds':BOUNDS,'layers':layers,'gaps':gaps,'summary':summary}}

@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    for key,value in fixture().items():store.set_setting(key+'_trudovoe',value)
    return tmp_path

def test_drafts_covered_by_observed_gaps_disjoint_from_parcels_buildings_and_each_other():
    values=fixture();result=recon.build(values,recon.options({}))
    drafts=[c for c in result['candidates'] if c['kind']=='draft']
    assert 1<=len(drafts)<=10
    for i,c in enumerate(drafts):
        g=shape(c['geometry']);gap=next(f for f in values['survey']['gaps']['features'] if f['id']==c['parent_gap_id'])
        assert shape(gap['geometry']).covers(g) and abs(c['area_m2']-1000)<.01
        for mode in ('parcels','buildings'):
            assert all(g.intersection(shape(f['geometry'])).area==0 for f in values['survey']['layers'][mode]['geojson']['features'])
        assert all(g.intersection(shape(other['geometry'])).area==0 for other in drafts[i+1:])
        assert not c['rights_confirmed'] and not c['srzu_ready'] and c['required_checks']
    assert result['sources']['nspd']['parcels']['received_at']=='source-date'
    assert not result['summary']['coverage_confirmed'] and result['summary']['confirmed_free']==0

@pytest.mark.parametrize('params',[{'target_area':399},{'target_area':float('nan')},{'limit':True},{'limit':21}, {'refresh_nspd':'yes'},{'purpose':'other'},{'min_width':40,'target_area':1000}])
def test_invalid_filters_rejected(params):
    with pytest.raises(ValueError):recon.options(params)

def test_layout_failure_keeps_unverified_gap_instead_of_claiming_no_available_land():
    values=fixture();values['survey']['gaps']['features']=[feature(box(34.207,44.994,34.2072,44.999),'thin')]
    result=recon.build(values,recon.options({}))
    assert result['summary']['drafts']==0 and result['summary']['large_gaps']==1
    assert not result['layout']['layout_complete'] and result['layout']['unplaced_gaps']==['thin']


def test_empty_parcels_cannot_create_hypotheses_for_whole_area():
    values=fixture();values['survey']['layers']['parcels']['geojson']['features']=[]
    with pytest.raises(ValueError,match='кадастровых'):recon.build(values,recon.options({}))


def test_restriction_exclusion_is_explicit_geometric_filter_not_legal_clearance():
    values=fixture();zone=feature(box(*BOUNDS),'zone');values['survey']['layers']['restrictions']['geojson']['features']=[zone]
    excluded=recon.build(values,recon.options({}))
    assert excluded['summary']['drafts']==0 and excluded['summary']['confirmed_free']==0
    included=recon.build(values,recon.options({'avoid_restrictions':False}))
    assert included['summary']['drafts']>0 and all(c['matches']['restrictions'] for c in included['candidates'] if c['kind']=='draft')


def test_road_distance_does_not_confirm_legal_access():
    values=fixture();values['survey']['layers']['parcels']['geojson']['features'][0]['properties']['options']['permitted_use_established_by_document']='Улично-дорожная сеть'
    result=recon.build(values,recon.options({}))
    assert all(c['road_proximity']['distance_m']>=0 and not c['road_proximity']['legal_access_confirmed'] for c in result['candidates'])

def test_active_lot_deduplicated_with_nspd_auction_and_dated_geometry_preserved():
    values=fixture();number='90:12:1:2';g=box(34.207,44.995,34.208,44.996)
    f=feature(g,'lot-geometry',number)
    raw={'id':'21000000000000000001_1','subjectRFCode':'91','lotStatus':'APPLICATIONS_SUBMISSION',
         'biddEndTime':'2099-01-01T00:00:00Z','characteristics':[{'code':'CadastralNumber','characteristicValue':number}]}
    lot=torgi.normalize(raw)
    values['torgi']={'id':'search','created_at':store.now(),'lots':[lot],
                     'geometries':{number:{'lookup':True,'state':'received','features':[f],'received_at':'geometry-date','source':recon.nspd.INTERSECTS,'sha256':'geometry-hash'}}}
    values['survey']['layers']['auction']['geojson']['features']=[f]
    result=recon.build(values,recon.options({}))
    auctions=[c for c in result['candidates'] if c['kind']=='auction']
    assert len(auctions)==1 and auctions[0]['lots'][0]['active_observed']
    assert auctions[0]['lots'][0]['geometry_sources'][0]['received_at']=='source-date'
    values['survey']['layers']['auction']['geojson']['features']=[]
    without_current=recon.build(values,recon.options({}))
    assert next(c for c in without_current['candidates'] if c['kind']=='auction')['lots'][0]['geometry_sources'][0]['received_at']=='geometry-date'
    values['torgi']['created_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    assert not any(c['kind']=='auction' for c in recon.build(values,recon.options({}))['candidates'])

def test_document_number_context_is_not_zone_or_rights_confirmation():
    values=fixture();values['municipal']={'id':'catalog','items':[{'title':'<script>alert(1)</script>','url':'javascript:alert(1)','mentions':[{'cadastral_number':'90:12:1:1'}]}]}
    contexts=recon.document_context(values,{'90:12:1:1'})
    assert len(contexts)==1 and contexts[0]['url']=='' and 'не установлена' in contexts[0]['scope']
    assert recon.safe_url('https://torgi.gov.ru:bad/path')==''


def test_regional_debt_sale_is_evidence_but_never_land_provision_candidate():
    values=fixture();number='90:12:1:2';g=box(34.207,44.995,34.208,44.996)
    raw={'id':'21000000000000000001_1','subjectRFCode':'91','lotStatus':'APPLICATIONS_SUBMISSION',
         'biddType':{'code':'229FZ','name':'Реализация имущества должников'},
         'biddEndTime':'2099-01-01T00:00:00Z','characteristics':[{'code':'CadastralNumber','characteristicValue':number}]}
    source={'id':'regional','created_at':store.now(),'lots':[torgi.normalize(raw)],
            'geometries':{number:{'lookup':True,'features':[feature(g,'debt',number)],'source':recon.nspd.INTERSECTS}}}
    values['torgi_active']=source
    result=recon.build(values,recon.options({}))
    assert not any(c['kind']=='auction' for c in result['candidates'])
    assert result['sources']['torgi_active']['groups']['debt_sale']==1
    source['lots'][0]['type']['code']='ZK'
    result=recon.build(values,recon.options({}))
    auction=next(c for c in result['candidates'] if c['kind']=='auction')
    assert not auction['rights_confirmed'] and not auction['lots'][0]['documents_current']
    values['torgi_active_documents']={'id':'docs','search_id':'regional','search_created_at':source['created_at'],'cards':[{'lot_id':source['lots'][0]['id'],'state':'received'}]}
    result=recon.build(values,recon.options({}))
    assert next(c for c in result['candidates'] if c['kind']=='auction')['lots'][0]['documents_current']

def test_local_workflow_never_downloads_and_preserves_source_dates(db,monkeypatch):
    monkeypatch.setattr(recon.nspd,'request_json',lambda *a:pytest.fail('unexpected NSPD request'))
    monkeypatch.setattr(recon.torgi,'run',lambda *a:pytest.fail('unexpected Torgi request'))
    answer=recon.run('trudovoe',{'bounds':BOUNDS,'refresh_nspd':False})
    data=recon.report('trudovoe');result=data['result']
    assert answer['drafts']>0 and not data['stale'] and not store.candidates('trudovoe')
    assert result['sources']['nspd']['parcels']['received_at']=='source-date'
    assert recon.load('trudovoe',result['id'])['result']==result
    cid=result['candidates'][0]['id']
    assert recon.watch('trudovoe',{'id':cid,'result_id':result['id']})=={'count':1}
    assert recon.watch('trudovoe',{'id':cid,'result_id':result['id']})=={'count':1}
    html=recon.html_report(data,cid)
    assert b'WGS84' in html and 'к подаче не готов'.encode() in html
    exported=recon.collection(data)
    assert exported['type']=='FeatureCollection' and exported['source_result_id']==result['id']
    assert all(not f['properties']['rights_confirmed'] for f in exported['features'])
    store.set_setting('municipal_trudovoe',{'id':'new'})
    assert recon.report('trudovoe')['stale'] and recon.report('trudovoe')['watchlist'][0]['stale']
    with pytest.raises(ValueError):recon.watch('trudovoe',{'id':cid,'result_id':result['id']})
    assert recon.load('trudovoe',result['id'])['stale']

def test_new_area_cannot_use_unrelated_saved_survey(db):
    with pytest.raises(ValueError,match='другой области'):recon.run('trudovoe',{'bounds':[34.21,44.99,34.22,45.0],'refresh_nspd':False})
    assert store.get_setting('survey_trudovoe')['id']=='survey'


def test_regional_lot_has_its_own_date_and_precedence_over_old_history():
    values=fixture();number='90:12:1:2';g=box(34.207,44.995,34.208,44.996)
    f=feature(g,'regional-geometry',number)
    raw={'id':'21000000000000000001_1','subjectRFCode':'91','lotStatus':'APPLICATIONS_SUBMISSION',
         'biddType':{'code':'ZK','name':'Аренда и продажа земельных участков'},
         'biddEndTime':'2099-01-01T00:00:00Z','characteristics':[{'code':'CadastralNumber','characteristicValue':number}]}
    lot=torgi.normalize(raw)
    values['torgi']={'id':'history','created_at':'2020-01-01T00:00:00Z','lots':[dict(lot,status='FAILED')]}
    values['torgi_active']={'id':'regional','created_at':store.now(),'query_pages_received':True,'lots':[lot],
        'geometries':{number:{'lookup':True,'features':[f],'received_at':'geometry-date','source':recon.nspd.INTERSECTS,'sha256':'geometry-hash'}}}
    result=recon.build(values,recon.options({}))
    auctions=[c for c in result['candidates'] if c['kind']=='auction']
    assert len(auctions)==1
    related=auctions[0]['lots'][0]
    assert related['active_observed'] and related['regional_active'] and related['search_id']=='regional'
    assert related['search_date']==values['torgi_active']['created_at'] and not related['documents_current']
    assert result['sources']['torgi_active']['lots']==1 and result['source_inputs']['torgi_active']=='regional'


def test_unlocated_lots_do_not_trigger_empty_geometry_overlay(monkeypatch):
    from shapely.geometry.base import BaseGeometry
    original=BaseGeometry.intersection
    def intersection(self,other,*args,**kwargs):
        if other.is_empty:raise RuntimeError('GEOS empty-overlay failure observed in real-area pilot')
        return original(self,other,*args,**kwargs)
    values=fixture()
    lot=torgi.normalize({'id':'21000000000000000001_1','subjectRFCode':'91','lotStatus':'PUBLISHED'})
    values['torgi_active']={'id':'regional','created_at':store.now(),'lots':[lot]}
    monkeypatch.setattr(BaseGeometry,'intersection',intersection)
    result=recon.build(values,recon.options({}))
    assert result['summary']['drafts']>0 and not any(c['lots'] for c in result['candidates'])
    assert result['sources']['torgi_active']['lots_without_number']==1

def test_failed_refresh_preserves_previous_result_and_never_runs_torgi(db,monkeypatch):
    recon.run('trudovoe',{'bounds':BOUNDS,'refresh_nspd':False});old=store.get_setting('recon_trudovoe')
    monkeypatch.setattr(recon.survey,'run',lambda *a:(_ for _ in ()).throw(ValueError('HTTP 403')))
    monkeypatch.setattr(recon.torgi,'run',lambda *a:pytest.fail('unexpected Torgi request'))
    with pytest.raises(ValueError,match='403'):recon.run('trudovoe',{'bounds':BOUNDS,'refresh_torgi':True})
    assert store.get_setting('recon_trudovoe')==old

def test_concurrent_source_revision_prevents_result_write(db,monkeypatch):
    original=recon.build
    def build(*args):
        result=original(*args);store.set_setting('municipal_trudovoe',{'id':'concurrent'});return result
    monkeypatch.setattr(recon,'build',build)
    with pytest.raises(ValueError,match='Источники изменились'):recon.run('trudovoe',{'bounds':BOUNDS,'refresh_nspd':False})
    assert store.get_setting('recon_trudovoe') is None

def test_saved_version_tampering_and_traversal_rejected(db):
    result=recon.run('trudovoe',{'bounds':BOUNDS,'refresh_nspd':False})
    path=db/'recon'/'trudovoe'/(result['id']+'.json');body=json.loads(path.read_text(encoding='utf-8'));body['summary']['confirmed_free']=1
    path.write_text(json.dumps(body),encoding='utf-8')
    with pytest.raises(ValueError,match='изменился'):recon.load('trudovoe',result['id'])
    with pytest.raises(ValueError):recon.load('trudovoe','../../secret')


def test_optional_torgi_failure_records_warning_and_keeps_dated_search(db,monkeypatch):
    source={'id':'old-search','created_at':'old-date','lots':[]};store.set_setting('torgi_trudovoe',source)
    monkeypatch.setattr(recon.torgi,'run',lambda *a:(_ for _ in ()).throw(ValueError('HTTP 403')))
    answer=recon.run('trudovoe',{'bounds':BOUNDS,'refresh_nspd':False,'refresh_torgi':True})
    result=recon.report('trudovoe')['result']
    assert answer['drafts']>0 and result['operation_warnings'] and result['sources']['torgi']['received_at']=='old-date'
    assert store.get_setting('torgi_trudovoe')==source
