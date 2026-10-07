import copy
import pytest
from shapely.geometry import box, mapping
from land import spatial_collection as p, nspd, survey, store

BOUNDS = [34.20, 44.99, 34.21, 45.0]


def feature(number=1, bounds=(34.201,44.991,34.202,44.992), category=36368):
    geometry = mapping(box(*bounds))
    geometry['crs'] = {'type':'name','properties':{'name':'EPSG:4326'}}
    return {'type':'Feature','id':number,'geometry':geometry,'properties':{'category':category}}


def response(features):
    return {'type':'FeatureCollection','features':features}


def test_children_recover_parent_omission_without_claiming_full_cadastre(monkeypatch):
    first=feature();extra=feature(2,(34.207,44.997,34.208,44.998))
    replies=iter([[first],[first],[],[],[extra]])
    calls=[]
    def request(url,body):
        calls.append(body);return response(next(replies)),str(len(calls))
    monkeypatch.setattr(nspd,'request_json',request)
    result=p.collect(BOUNDS,36368)
    assert len(calls)==5 and len(result['geojson']['features'])==2
    assert result['coverage']['additional_ids']==['36368:2']
    assert not result['coverage']['responses_consistent'] and not result['coverage']['complete']
    assert result['sha256_kind']=='observation_manifest'
    assert [o['sha256'] for o in result['observations']]==['1','2','3','4','5']
    assert result['request']==nspd.spatial_body(BOUNDS,36368)


def test_duplicate_cross_tile_object_is_counted_once(monkeypatch):
    crossing=feature(bounds=(34.203,44.993,34.207,44.997))
    monkeypatch.setattr(nspd,'request_json',lambda *a:(response([crossing]),'hash'))
    result=p.collect(BOUNDS,36368)
    assert len(result['geojson']['features'])==1
    assert result['coverage']['responses_consistent'] and not result['coverage']['complete']


@pytest.mark.parametrize('failure',['403','different_geometry','category'])
def test_bad_child_stops_and_does_not_replace_survey(tmp_path,monkeypatch,failure):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    store.set_setting('survey_trudovoe',{'id':'old'})
    calls=[]
    def request(*args):
        calls.append(args)
        if len(calls)==2:
            if failure=='403':raise nspd.StatusError(403)
            return response([feature(category=999) if failure=='category' else feature(bounds=(34.201,44.991,34.203,44.992))]),'other'
        return response([feature()]),'parent'
    monkeypatch.setattr(nspd,'request_json',request)
    with pytest.raises(ValueError):survey.run('trudovoe',{'bounds':BOUNDS,'verify_nspd':True})
    assert len(calls)==2 and store.get_setting('survey_trudovoe')=={'id':'old'}
    assert store.get_setting('survey_attempt_trudovoe')['state']=='error'


def test_unrelated_features_are_not_added_to_area(monkeypatch):
    monkeypatch.setattr(nspd,'request_json',lambda *a:(response([feature(bounds=(35,45,35.01,45.01))]),'hash'))
    result=p.collect(BOUNDS,36368)
    assert not result['geojson']['features'] and not result['coverage']['complete']
