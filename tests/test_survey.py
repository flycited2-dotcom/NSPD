import copy
import pytest
from shapely.geometry import box, shape, mapping
from shapely.ops import unary_union
from land import survey, store

BOUNDS = [34.20, 44.99, 34.21, 45.0]


def feature(g, fid='p'):
    return {'type': 'Feature', 'id': fid, 'geometry': mapping(g), 'properties': {}}


def layers():
    return {k: {'geojson': {'type': 'FeatureCollection', 'features': fs}} for k,fs in {
        'parcels': [feature(box(34.204, 44.989, 34.206, 45.001))],
        'free': [], 'auction': [feature(box(34.207,44.993,34.208,44.994),'auction-1')]}.items()}


def test_gaps_clipped_no_overlap_area_and_auction():
    ls=layers();fc,s=survey.gaps(BOUNDS,ls,{'min_area':1,'max_area':2000,'min_width':0})
    assert len(fc['features'])==2
    for f in fc['features']:
        g=shape(f['geometry'])
        assert box(*BOUNDS).buffer(1e-8).covers(g)
        assert g.intersection(shape(ls['parcels']['geojson']['features'][0]['geometry'])).area<1e-9
        assert f['properties']['status']=='unverified'
    assert sum(len(f['properties']['matches']['auction']) for f in fc['features'])==1
    assert abs(s['area_m2']-s['observed_parcels_m2']-s['remainder_m2'])<.02
    assert s['complete'] is False


def test_empty_parcels_never_makes_whole_area_candidate():
    ls=layers();ls['parcels']['geojson']['features']=[]
    with pytest.raises(ValueError,match='пуст'):
        survey.gaps(BOUNDS,ls,{})


def test_area_and_width_filters():
    fc,s=survey.gaps(BOUNDS,layers(),{'min_area':1e7,'max_area':1e7})
    assert not fc['features'] and s['filtered']['small']==2
    fc,s=survey.gaps(BOUNDS,layers(),{'min_area':1,'max_area':1e7,'min_width':1000})
    assert not fc['features'] and s['filtered']['narrow']==2


def test_failed_layer_preserves_previous_survey(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    store.set_setting('survey_trudovoe',{'old':True})
    calls=[]
    def fetch(url,body):
        calls.append(body)
        if len(calls)==2:raise ValueError('HTTP 403')
        return {},'hash'
    monkeypatch.setattr(survey.nspd,'request_json',fetch)
    fc=copy.deepcopy(layers()['parcels']['geojson'])
    fc['features'][0]['properties']['category']=36368
    monkeypatch.setattr(survey.nspd,'normalize',lambda data:fc)
    with pytest.raises(ValueError,match='403'):
        survey.run('trudovoe',{'bounds':BOUNDS})
    assert len(calls)==2
    assert store.get_setting('survey_trudovoe')=={'old':True}
    assert store.get_setting('survey_attempt_trudovoe')['state']=='error'
    assert store.candidates('trudovoe')==[]


def test_buildings_subtracted_zones_retained():
    ls=layers()
    building=box(34.207,44.993,34.208,44.994)
    zone=box(34.2065,44.992,34.209,44.998)
    ls['buildings']={'geojson':{'features':[feature(building,'building')]}}
    ls['restrictions']={'geojson':{'features':[feature(zone,'zone')]}}
    ls['pzz']={'geojson':{'features':[]}}
    fc,s=survey.gaps(BOUNDS,ls,{'min_area':1,'max_area':1e7,'min_width':0})
    remainder=unary_union([shape(f['geometry']) for f in fc['features']])
    assert remainder.intersection(building).area<1e-12
    assert remainder.intersection(zone).area>0
    assert s['buildings_excluded_m2']>0
    assert abs(s['area_m2']-s['observed_parcels_m2']-s['buildings_excluded_m2']-s['remainder_m2'])<.03
    assert any(f['properties']['matches']['restrictions']==['zone'] for f in fc['features'])
    assert s['pzz_coverage_confirmed'] is False


def test_building_inside_parcel_not_double_counted():
    ls=layers()
    ls['buildings']={'geojson':{'features':[feature(box(34.2045,44.993,34.2055,44.995))]}}
    fc,s=survey.gaps(BOUNDS,ls,{'min_area':1,'max_area':1e7,'min_width':0})
    assert s['buildings_excluded_m2']==0


def test_road_designation_distance_not_legal_access():
    ls=layers()
    road=ls['parcels']['geojson']['features'][0]
    road['properties']={'label':'road number','options':{'permitted_use_established_by_document':'Улично-дорожная сеть'}}
    fc,s=survey.gaps(BOUNDS,ls,{'min_area':1,'max_area':1e7,'min_width':0})
    assert s['road_designated_parcels']==1 and not s['roads_coverage_confirmed']
    for f in fc['features']:
        r=f['properties']['road_proximity']
        assert r['distance_m']<.1 and r['parcel_id']==road['id']
        assert not r['legal_access_confirmed']
    road['properties']['options']['permitted_use_established_by_document']='Автомобильный транспорт; хранение автотранспорта'
    fc,s=survey.gaps(BOUNDS,ls,{'min_area':1,'max_area':1e7,'min_width':0})
    assert not survey.road_features(ls)
    assert all(f['properties']['road_proximity'] is None for f in fc['features'])


def test_recalculate_preserves_source_dates_and_history(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    ls=layers()
    for layer in ls.values():layer['received_at']='2026-09-01T00:00:00Z'
    previous={'id':'previous','bounds':BOUNDS,'layers':ls}
    store.set_setting('survey_trudovoe',previous)
    saved_layers=store.get_setting('survey_trudovoe')['layers']
    monkeypatch.setattr(survey.nspd,'request_json',lambda *a:pytest.fail('Recalculation must not fetch'))
    survey.recalculate('trudovoe',{'min_area':1,'max_area':1e7,'min_width':0})
    result=store.get_setting('survey_trudovoe')
    assert result['parent_id']=='previous'
    assert result['layers']==saved_layers and result['summary']['complete'] is False
    assert (tmp_path/'surveys'/(result['id']+'.json')).is_file()
