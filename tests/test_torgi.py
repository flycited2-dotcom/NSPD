import copy
import json
import pytest
from shapely.geometry import box, mapping
from land import torgi, store


def lot(fid='21000000000000000001_1', cad='90:12:172301:102'):
    return {'id':fid,'subjectRFCode':'91','noticeNumber':fid.split('_')[0],'lotNumber':1,
            'lotStatus':'FAILED','biddForm':{'code':'EA','name':'Электронный аукцион'},
            'biddType':{'code':'ZK','name':'Аренда и продажа'},'category':{'code':'301','name':'Земли населённых пунктов'},
            'characteristics':[{'code':'CadastralNumber','characteristicValue':cad}],
            'lotDescription':'Рядом с 90:12:172301:999', 'biddEndTime':'2026-09-01T00:00:00Z'}


def page(rows,number,total,last):
    return json.dumps({'content':rows,'number':number,'size':10,'totalElements':total,'last':last}).encode(),'application/geo+json',200


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    monkeypatch.setattr(torgi.time,'sleep',lambda *a:None)
    return tmp_path


def test_structured_number_only_and_deadline():
    r=torgi.normalize(lot(cad='90:12:0172301:0102'))
    assert r['cadastral_numbers']==['90:12:172301:102']
    assert torgi.deadline_state(r,'2026-09-30T00:00:00Z')=='expired'
    r['deadline']='2026-10-01T03:00:00+03:00'
    assert torgi.deadline_state(r,'2026-09-30T23:59:00Z')=='future'
    r['deadline']='2026-10-01'
    assert torgi.deadline_state(r,'2026-09-30T00:00:00Z')=='unknown'
    with pytest.raises(ValueError):torgi.normalize(lot(fid='javascript:bad'))
    wrong=lot();wrong['subjectRFCode']='77'
    with pytest.raises(ValueError,match='регион'):torgi.normalize(wrong)


def test_all_pages_not_whole_territory(db,monkeypatch):
    store.set_setting('torgi_geometry_attempt_trudovoe',{'state':'done','previous':True})
    first=[lot(f'210000000000000000{i:02}_1') for i in range(10)]
    responses=iter([page(first,0,11,False),page([lot('21000000000000000011_1')],1,11,True)])
    urls=[]
    def fetch(url):urls.append(url);return next(responses)
    monkeypatch.setattr(torgi,'fetch',fetch)
    torgi.run('trudovoe',{'query':'Трудовое','history':True})
    r=store.get_setting('torgi_trudovoe')
    assert len(r['lots'])==11 and r['query_pages_received'] and not r['complete']
    assert 'page=1' in urls[1] and 'lotStatus' not in urls[0] and 'dynSubjRF=12' in urls[0]
    assert store.candidates('trudovoe')==[]
    assert store.get_setting('torgi_geometry_attempt_trudovoe') is None
    assert (db/'torgi'/(r['id']+'.json')).is_file()


@pytest.mark.parametrize('failure',['repeat','changed_total','empty','http403'])
def test_failed_page_preserves_previous(db,monkeypatch,failure):
    store.set_setting('torgi_trudovoe',{'old':True})
    calls=[]
    first=[lot(f'210000000000000000{i:02}_1') for i in range(10)]
    def fetch(url):
        calls.append(url)
        if len(calls)==1:return page(first,0,11,False)
        if failure=='http403':raise ValueError('HTTP 403')
        if failure=='repeat':return page([first[0]],1,11,True)
        if failure=='changed_total':return page([],1,12,True)
        return page([],1,11,False)
    monkeypatch.setattr(torgi,'fetch',fetch)
    with pytest.raises(ValueError):torgi.run('trudovoe',{})
    assert len(calls)==2 and store.get_setting('torgi_trudovoe')=={'old':True}
    assert store.get_setting('torgi_attempt_trudovoe')['state']=='error'


def pilot():
    bounds=[34.20,44.99,34.21,45.0]
    f={'type':'Feature','id':'parcel','geometry':mapping(box(34.201,44.991,34.202,44.992)),
       'properties':{'label':'90:12:172301:102','category':36368}}
    g={'type':'Feature','id':'gap','geometry':mapping(box(34.2015,44.9915,34.203,44.993)),'properties':{}}
    return {'id':'survey','bounds':bounds,'gaps':{'features':[g]},'layers':{'parcels':{'geojson':{'features':[f]},'received_at':'old','source':'nspd'}}}


def test_relate_exact_cad_overlap_not_text_match():
    s=pilot();r=torgi.relate([torgi.normalize(lot())],torgi.survey_geometries(s),s)[0]
    assert r['in_survey'] and not r['geometry_confirmed']
    assert r['spatial_matches'][0]['gap_intersections'][0]['area_m2']>0
    wrong=torgi.relate([torgi.normalize(lot(cad='90:12:172301:999'))],torgi.survey_geometries(s),s)[0]
    assert not wrong['in_survey']


def test_lookup_stops_on_error_and_rejects_wrong_cad(db,monkeypatch):
    s=pilot();store.set_setting('survey_trudovoe',s)
    rows=[torgi.normalize(lot(cad='90:12:172301:1')),torgi.normalize(lot('21000000000000000002_1',cad='90:12:172301:2'))]
    store.set_setting('torgi_trudovoe',{'id':'old','lots':rows,'created_at':'source-date'})
    calls=[]
    def fetch(url):
        calls.append(url)
        if len(calls)==2:raise ValueError('HTTP 403')
        return {},'hash'
    monkeypatch.setattr(torgi.nspd,'request_json',fetch)
    monkeypatch.setattr(torgi.nspd,'normalize',lambda data:{'features':s['layers']['parcels']['geojson']['features']})
    torgi.locate('trudovoe',{'id':'old'})
    r=store.get_setting('torgi_trudovoe')
    assert r['created_at']=='source-date' and r['parent_id']=='old'
    assert len(calls)==2 and not any(l['in_survey'] for l in r['lots'])
    assert r['geometries']['90:12:172301:1']['state']=='not_found'
    assert store.get_setting('torgi_geometry_attempt_trudovoe')['state']=='partial'
    with pytest.raises(ValueError,match='изменился'):torgi.locate('trudovoe',{'id':'old'})


def test_404_geometry_does_not_stop_other_numbers(db,monkeypatch):
    s=pilot();store.set_setting('survey_trudovoe',s)
    rows=[torgi.normalize(lot(cad='90:12:172301:1')),torgi.normalize(lot('21000000000000000002_1',cad='90:12:172301:2'))]
    store.set_setting('torgi_trudovoe',{'id':'old','lots':rows,'created_at':'source-date'})
    calls=[]
    def fetch(url):
        calls.append(url)
        if len(calls)==1:raise torgi.nspd.StatusError(404)
        return {},'hash'
    monkeypatch.setattr(torgi.nspd,'request_json',fetch)
    monkeypatch.setattr(torgi.nspd,'normalize',lambda data:{'features':[]})
    torgi.locate('trudovoe',{'id':'old'})
    r=store.get_setting('torgi_trudovoe')
    assert len(calls)==2 and r['geometries']['90:12:172301:1']['state']=='not_returned'
    assert store.get_setting('torgi_geometry_attempt_trudovoe')['state']=='done'
    assert not any(l['geometry_confirmed'] for l in r['lots'])


def test_invalid_geometry_rejected_other_lookup_continues(db,monkeypatch):
    s=pilot();store.set_setting('survey_trudovoe',s)
    rows=[torgi.normalize(lot(cad='90:12:172301:1')),torgi.normalize(lot('21000000000000000002_1',cad='90:12:172301:2'))]
    store.set_setting('torgi_trudovoe',{'id':'old','lots':rows,'created_at':'source-date'})
    calls=[]
    def normalize(data):
        calls.append(data)
        if len(calls)==1:raise ValueError('Invalid geometry')
        return {'features':[]}
    monkeypatch.setattr(torgi.nspd,'request_json',lambda url:({},'hash'))
    monkeypatch.setattr(torgi.nspd,'normalize',normalize)
    torgi.locate('trudovoe',{'id':'old'})
    r=store.get_setting('torgi_trudovoe')
    assert len(calls)==2 and r['geometries']['90:12:172301:1']['state']=='rejected'
    assert not any(l['spatial_matches'] for l in r['lots'])


def test_geometry_limit_continues_without_refetching_previous(db,monkeypatch):
    s=pilot();store.set_setting('survey_trudovoe',s)
    rows=[torgi.normalize(lot(cad='90:12:172301:1')),torgi.normalize(lot('21000000000000000002_1',cad='90:12:172301:2'))]
    store.set_setting('torgi_trudovoe',{'id':'old','lots':rows,'created_at':'source-date'})
    calls=[]
    monkeypatch.setattr(torgi,'MAX_GEOMETRIES',1)
    def fetch(url):
        calls.append(url)
        return {'number':'90:12:172301:'+str(len(calls))},'hash'
    def normalize(data):
        f=copy.deepcopy(s['layers']['parcels']['geojson']['features'][0])
        f['properties']['label']=data['number']
        return {'features':[f]}
    monkeypatch.setattr(torgi.nspd,'request_json',fetch)
    monkeypatch.setattr(torgi.nspd,'normalize',normalize)
    torgi.locate('trudovoe',{'id':'old'})
    r=store.get_setting('torgi_trudovoe')
    assert r['geometry_unchecked_numbers']==['90:12:172301:2']
    date=r['geometries']['90:12:172301:1']['received_at']
    torgi.locate('trudovoe',{'id':r['id']})
    r=store.get_setting('torgi_trudovoe')
    assert not r['geometry_unchecked_numbers'] and len(calls)==2
    assert r['geometries']['90:12:172301:1']['received_at']==date
    assert r['created_at']=='source-date' and all(l['in_survey'] for l in r['lots'])
