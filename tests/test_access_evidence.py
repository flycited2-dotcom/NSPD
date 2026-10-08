import copy
import math

import pytest
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from shapely.ops import transform

from land import access_evidence as access

METRIC='+proj=laea +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +no_defs'
TO_WORLD=Transformer.from_crs(METRIC,4326,always_xy=True).transform


def world(geometry):
    return mapping(transform(TO_WORLD,geometry))


def feature(identity,geometry,road=False,number=None):
    return {'type':'Feature','id':identity,'geometry':world(geometry),'properties':{'options':{
        'cad_num':number or '90:12:170102:100',
        'permitted_use_established_by_document':'Улично-дорожная сеть' if road else 'Иное назначение'}}}


def observation(parcels=(),buildings=()):
    def layer(items,letter,mode):
        return {'source':'https://example.invalid/observed/'+mode,
                'received_at':'2026-10-07T19:16:42+00:00','sha256':letter*64,
                'sha256_kind':'observation_manifest','geojson':{'type':'FeatureCollection','features':list(items)}}
    return {'id':'dated-survey','bounds':[-.01,-.01,.01,.01],
            'layers':{'parcels':layer(parcels,'a','parcels'),'buildings':layer(buildings,'b','buildings')}}


def candidate(geometry=None):
    return {'id':'candidate-one','geometry':world(geometry if geometry is not None else box(0,-1,2,1))}


def assert_no_legal_conclusion(result):
    assert result['legal_access_confirmed'] is False
    assert result['coverage_confirmed'] is False
    assert 'не является маршрутом' in result['warning']
    assert 'пустой слой' in result['warning']


def test_positive_intersections_report_other_parcel_and_building_with_source_provenance():
    road=feature('target-road',box(10,-1,12,1),road=True)
    parcel=feature('parcel-blocker',box(4,-2,6,2),number='90:12:170102:200')
    building=feature('building-blocker',box(7,-2,8,2),number='90:12:170102:300')
    survey=observation([road,parcel],[building])
    result=access.assessment(candidate(),survey)
    assert result['state']=='observed_intersections'
    assert result['road']['feature_id']=='target-road'
    assert result['road']['distance_m']==pytest.approx(8,abs=.01)
    assert result['nearest_parcel']['feature_id']=='parcel-blocker'
    assert result['nearest_parcel']['distance_m']==pytest.approx(2,abs=.01)
    assert result['nearest_building']['feature_id']=='building-blocker'
    assert result['nearest_building']['distance_m']==pytest.approx(5,abs=.01)
    assert result['direct_segment']['length_m']==pytest.approx(8,abs=.01)
    assert result['direct_segment']['within_survey_bounds'] is True
    assert shape(result['direct_segment']['geometry']).geom_type=='LineString'
    for mode,expected,length in [('parcels','parcel-blocker',2),('buildings','building-blocker',1)]:
        records=result['intersections'][mode]
        assert len(records)==1 and records[0]['feature_id']==expected
        assert records[0]['intersection_length_m']==pytest.approx(length,abs=.00001)
        for key in ('source','received_at','sha256','sha256_kind'):
            assert records[0][key]==survey['layers'][mode][key]
    assert_no_legal_conclusion(result)


def test_endpoint_contact_is_separate_from_positive_length_intersection():
    # This polygon is identical to the candidate; its contact with the outgoing
    # nearest segment is only the segment's starting point.
    touch=feature('endpoint-contact',box(0,-1,2,1))
    result=access.assessment(candidate(),observation([
        feature('target-road',box(10,-1,12,1),road=True),touch]))
    assert result['state']=='no_observed_intersections'
    assert result['intersections']=={'parcels':[],'buildings':[]}
    assert [item['feature_id'] for item in result['touches']['parcels']]==['endpoint-contact']
    assert result['touches']['parcels'][0]['intersection_length_m']==0
    assert_no_legal_conclusion(result)


@pytest.mark.parametrize('empty_parcels',[True,False])
def test_no_observed_road_is_unknown_even_with_nonroad_geometry(empty_parcels):
    parcels=[] if empty_parcels else [feature('ordinary-parcel',box(10,-1,12,1))]
    result=access.assessment(candidate(),observation(parcels))
    assert result['state']=='unknown_no_observed_road'
    assert result['road'] is None and result['direct_segment'] is None
    assert result['nearest_building'] is None
    assert result['observed_counts']['road_features']==0
    assert (result['nearest_parcel'] is None)==empty_parcels
    assert_no_legal_conclusion(result)


@pytest.mark.parametrize('geometry',[box(0,-1,2,1),box(-1,-2,3,2)])
def test_zero_distance_returns_valid_point_instead_of_degenerate_line(geometry):
    result=access.assessment(candidate(),observation([feature('road',geometry,road=True)]))
    assert result['state']=='zero_distance'
    assert result['road']['distance_m']==0
    assert result['direct_segment']['length_m']==0
    point=shape(result['direct_segment']['geometry'])
    assert point.geom_type=='Point' and point.is_valid and not point.is_empty
    assert result['intersections']=={'parcels':[],'buildings':[]}
    assert_no_legal_conclusion(result)


def test_only_target_road_is_excluded_and_ties_are_stable():
    first=feature('a-target',box(10,-1,12,1),road=True,number='90:12:170102:1')
    other=feature('b-other-road',box(10,-1,12,1),road=True,number='90:12:170102:2')
    result=access.assessment(candidate(),observation([other,first]))
    repeated=access.assessment(candidate(),observation([first,other]))
    assert result==repeated
    assert result['road']['feature_id']=='a-target'
    assert result['observed_counts']['road_features']==2
    # The second road touches the same endpoint. It must not be removed merely
    # because both objects have road designation.
    assert [item['feature_id'] for item in result['touches']['parcels']]==['b-other-road']
    assert result['intersections']['parcels']==[]


def test_multipolygon_total_intersection_length_and_inputs_are_unchanged():
    multipolygon=MultiPolygon([box(4,-2,5,2),box(7,-2,8,2)])
    survey=observation([feature('road',box(10,-1,12,1),road=True),
                        feature('multipart-parcel',multipolygon,number='90:12:170102:20')])
    target=candidate()
    before=copy.deepcopy((target,survey))
    result=access.assessment(target,survey)
    assert (target,survey)==before
    record=result['intersections']['parcels'][0]
    assert record['feature_id']=='multipart-parcel'
    assert record['cadastral_number']=='90:12:170102:20'
    assert record['intersection_length_m']==pytest.approx(2,abs=.00001)
    assert result['candidate_id']=='candidate-one' and result['survey_id']=='dated-survey'
    assert result['algorithm']==access.ALGORITHM
    assert_no_legal_conclusion(result)


def test_multipolygon_candidate_uses_the_nearest_part_without_joining_parts():
    target=candidate(MultiPolygon([box(0,-1,2,1),box(5,-1,8,1)]))
    result=access.assessment(target,observation([feature('road',box(10,-1,12,1),road=True)]))
    assert result['road']['distance_m']==pytest.approx(2,abs=.01)
    assert result['direct_segment']['length_m']==pytest.approx(2,abs=.01)
    assert result['state']=='no_observed_intersections'
    assert_no_legal_conclusion(result)


def test_missing_layers_do_not_become_observed_clear_access():
    result=access.assessment(candidate(),{'bounds':[-.01,-.01,.01,.01]})
    assert result['state']=='unknown_no_observed_road'
    assert result['observed_counts']=={'parcels':0,'buildings':0,'road_features':0}
    assert_no_legal_conclusion(result)


def test_unknown_properties_and_dates_are_not_invented():
    item=feature('ordinary',box(10,-1,12,1));item['properties']=None
    result=access.assessment(candidate(),{'bounds':[-.01,-.01,.01,.01],
        'layers':{'parcels':{'geojson':{'features':[item]}}}})
    assert result['nearest_parcel']['cadastral_number'] is None
    assert result['nearest_parcel']['received_at'] is None
    assert result['nearest_parcel']['sha256'] is None
    assert result['state']=='unknown_no_observed_road'


def test_feature_limit_rejects_instead_of_silently_dropping_obstacles(monkeypatch):
    monkeypatch.setattr(access,'MAX_FEATURES',1)
    with pytest.raises(ValueError,match='предел объектов'):
        access.assessment(candidate(),observation([feature('road',box(10,-1,12,1),road=True),
                                                   feature('blocker',box(4,-2,6,2))]))


def test_coordinate_limit_rejects_before_overlay(monkeypatch):
    monkeypatch.setattr(access,'MAX_COORDINATES',10)
    with pytest.raises(ValueError,match='предел координат'):
        access.assessment(candidate(),observation([feature('road',box(10,-1,12,1),road=True)]))


def test_invalid_geometry_is_rejected_without_repair():
    invalid=Polygon([(0,0),(2,2),(2,0),(0,2),(0,0)])
    with pytest.raises(ValueError,match='невалидна'):
        access.assessment(candidate(invalid),observation())


@pytest.mark.parametrize('bounds',[None,[0,0,0,1],[0,0,math.inf,1],[0,-90,1,90]])
def test_invalid_area_is_rejected(bounds):
    with pytest.raises(ValueError,match='область'):
        access.assessment(candidate(),{'bounds':bounds})


def test_prepared_survey_projects_and_validates_sources_once_for_several_candidates(monkeypatch):
    survey=observation([feature('road',box(10,-1,12,1),road=True),
                        feature('parcel',box(4,-2,6,2))],
                       [feature('building',box(7,-2,8,2))])
    targets=[candidate(box(0,-1,2,1)),candidate(box(1,-1,3,1)),candidate(box(2,-1,4,1))]
    before=copy.deepcopy((survey,targets))
    validated=[];projections=[];designations=[]
    original_polygon=access._polygon
    original_transform=access.transform
    original_roads=access.survey_module.road_features
    def polygon(geometry,budget):
        validated.append(id(geometry))
        return original_polygon(geometry,budget)
    def projected(operation,geometry):
        projections.append(operation)
        return original_transform(operation,geometry)
    def roads(layers):
        designations.append(layers)
        return original_roads(layers)
    monkeypatch.setattr(access,'_polygon',polygon)
    monkeypatch.setattr(access,'transform',projected)
    monkeypatch.setattr(access.survey_module,'road_features',roads)
    prepared=access.prepare(survey)
    results=[access.assessment_prepared(target,prepared) for target in targets]
    source_geometries=[f['geometry'] for layer in survey['layers'].values()
                       for f in layer['geojson']['features']]
    for geometry in source_geometries+[target['geometry'] for target in targets]:
        assert validated.count(id(geometry))==1
    assert sum(operation is prepared.forward for operation in projections)==len(source_geometries)+len(targets)
    assert sum(operation is prepared.inverse for operation in projections)==len(targets)
    assert len(designations)==1
    assert all(result['road']['feature_id']=='road' for result in results)
    assert (survey,targets)==before


def test_prepared_results_match_single_candidate_wrapper_and_keep_separate_provenance():
    survey=observation([feature('road',box(10,-1,12,1),road=True),
                        feature('parcel',box(4,-2,6,2))],
                       [feature('building',box(7,-2,8,2))])
    targets=[candidate(),candidate(box(3,-1,4,1)),candidate(box(10,-1,12,1))]
    before=copy.deepcopy((survey,targets))
    expected=[access.assessment(target,survey) for target in targets]
    prepared=access.prepare(survey)
    assert [access.assessment_prepared(target,prepared) for target in targets]==expected
    assert (survey,targets)==before
    # Reusing a prepared value must not observe later edits to the input source,
    # or accidentally mix it with another survey prepared in the same process.
    other=copy.deepcopy(survey);other['id']='other-survey'
    other['layers']['parcels']['received_at']='other-date'
    other['layers']['parcels']['sha256']='c'*64
    other_prepared=access.prepare(other)
    survey['id']='changed-after-prepare'
    survey['layers']['parcels']['received_at']='changed-after-prepare'
    survey['layers']['parcels']['geojson']['features'][0]['properties']['options']['cad_num']='changed-number'
    survey['layers']['parcels']['geojson']['features'].clear()
    assert [access.assessment_prepared(target,prepared) for target in targets]==expected
    alternate=access.assessment_prepared(targets[0],other_prepared)
    assert alternate['survey_id']=='other-survey'
    assert alternate['road']['received_at']=='other-date'
    assert alternate['road']['sha256']=='c'*64
    assert access.assessment_prepared(targets[0],prepared)==expected[0]


def test_prepared_coordinate_budget_includes_sources_and_current_candidate_without_accumulation(monkeypatch):
    survey=observation([feature('road',box(10,-1,12,1),road=True),
                        feature('parcel',box(4,-2,6,2))])
    target=candidate()
    # Each rectangle consumes seven budget entries: polygon, ring, five points.
    monkeypatch.setattr(access,'MAX_COORDINATES',20)
    prepared=access.prepare(survey)
    assert prepared.coordinate_count==14
    with pytest.raises(ValueError,match='предел координат'):
        access.assessment_prepared(target,prepared)
    with pytest.raises(ValueError,match='предел координат'):
        access.assessment(target,survey)
    monkeypatch.setattr(access,'MAX_COORDINATES',21)
    first=access.assessment_prepared(target,prepared)
    for _ in range(3):
        assert access.assessment_prepared(target,prepared)==first
    assert prepared.coordinate_count==14


def test_prepare_rejects_sources_exceeding_coordinate_budget_before_candidate(monkeypatch):
    monkeypatch.setattr(access,'MAX_COORDINATES',10)
    with pytest.raises(ValueError,match='предел координат'):
        access.prepare(observation([feature('road',box(10,-1,12,1),road=True),
                                    feature('parcel',box(4,-2,6,2))]))
