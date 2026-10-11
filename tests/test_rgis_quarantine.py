"""Only source topology defects may become explicit partial observations."""
import copy
import hashlib
import json

import pytest
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from shapely.validation import explain_validity

from land import recon, rgis_context as rgis, store
from land.geometry import convert
from test_rgis_context import BOUNDS, payload, result


def mixed(mode='functional', invalid_only=False):
    data=payload(mode)
    bad=copy.deepcopy(data['features'][0])
    bad['id']=rgis.TYPES[mode]+'.bad'
    world=Polygon([(34.203,44.993),(34.207,44.997),(34.207,44.993),
                   (34.203,44.997),(34.203,44.993)])
    bad['geometry']=json.loads(json.dumps(mapping(convert(world,4326,3857))))
    data['features']=([bad] if invalid_only else data['features']+[bad])
    count=len(data['features'])
    data.update(totalFeatures=count,numberMatched=count,numberReturned=count)
    return data


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    store.init()
    return tmp_path


def fake_source(monkeypatch, functional):
    calls=[]
    def fetch(url,**kwargs):
        mode=next(mode for mode,name in rgis.TYPES.items() if name in url)
        calls.append(mode)
        data=functional if mode=='functional' else payload(mode)
        return json.dumps(data).encode('utf-8'),'application/json',200
    monkeypatch.setattr(rgis.network,'fetch',fetch)
    monkeypatch.setattr(rgis.time,'sleep',lambda _:None)
    return calls


def test_default_normalization_stays_strict_and_does_not_modify_source():
    data=mixed();before=copy.deepcopy(data)
    with pytest.raises(ValueError,match='Невалидная'):
        rgis.normalize(data,BOUNDS,'functional')
    assert data==before


def test_explicit_quarantine_preserves_exact_id_reason_and_original_geometry_digest():
    data=mixed();before=copy.deepcopy(data);bad=data['features'][1]
    observed=rgis.normalize_partial(data,BOUNDS,'functional')
    assert data==before
    assert observed['state']=='partial'
    assert (observed['received_count'],observed['accepted_count'],observed['invalid_count'])==(2,1,1)
    assert [f['id'] for f in observed['geojson']['features']]==[data['features'][0]['id']]
    rejected=observed['rejected_geometries'][0]
    assert rejected['id']==bad['id']
    assert rejected['reason']==explain_validity(shape(bad['geometry']))
    assert rejected['original_geometry_sha256']==hashlib.sha256(json.dumps(
        bad['geometry'],sort_keys=True,separators=(',',':'),ensure_ascii=False,
        allow_nan=False).encode('utf-8')).hexdigest()
    assert rejected['bounds']==list(shape(bad['geometry']).bounds)
    assert rejected['original_crs']=='EPSG:3857'
    assert rejected['geometry_digest_encoding']=='canonical-json'
    assert not rejected['geometry_repaired']
    assert not observed['geometry_processing_complete'] and observed['warnings']


@pytest.mark.parametrize('change',[
    'bad_exported_property','bad_discarded_property','oversized_discarded_property',
    'bad_property_key','nonfinite_property','nonfinite_coordinate','coordinate_budget',
    'outside_invalid','outside_valid','duplicate','wrong_layer','empty_id_suffix',
    'wrong_feature_type','wrong_geometry_type','empty_coordinates','short_ring',
    'unclosed_ring','extra_coordinate_dimension','wrong_depth','missing_properties',
    'counts','count_budget','missing_crs','wrong_crs','pagination',
])
def test_partial_mode_never_hides_structural_source_scope_or_limit_errors(change,monkeypatch):
    data=mixed();bad=data['features'][1]
    if change=='bad_exported_property':bad['properties']['NAME']={'nested':'not allowed'}
    elif change=='bad_discarded_property':bad['properties']['created_us']={'nested':'not allowed'}
    elif change=='oversized_discarded_property':bad['properties']['created_us']='x'*2001
    elif change=='bad_property_key':bad['properties'][1]='bad key'
    elif change=='nonfinite_property':bad['properties']['created_us']=float('inf')
    elif change=='nonfinite_coordinate':bad['geometry']['coordinates'][0][0][0]=float('nan')
    elif change=='coordinate_budget':monkeypatch.setattr(rgis,'MAX_COORDINATES',10)
    elif change=='outside_invalid':
        g=Polygon([(35,45),(35.01,45.01),(35.01,45),(35,45.01),(35,45)])
        bad['geometry']=json.loads(json.dumps(mapping(convert(g,4326,3857))))
    elif change=='outside_valid':
        bad['geometry']=json.loads(json.dumps(mapping(convert(box(35,45,35.01,45.01),4326,3857))))
    elif change=='duplicate':bad['id']=data['features'][0]['id']
    elif change=='wrong_layer':bad['id']='UDS_polygon.bad'
    elif change=='empty_id_suffix':bad['id']=rgis.TYPES['functional']+'.'
    elif change=='wrong_feature_type':bad['type']='GeometryCollection'
    elif change=='wrong_geometry_type':bad['geometry']['type']='LineString'
    elif change=='empty_coordinates':bad['geometry']['coordinates']=[]
    elif change=='short_ring':bad['geometry']['coordinates'][0]=bad['geometry']['coordinates'][0][:3]
    elif change=='unclosed_ring':bad['geometry']['coordinates'][0][-1]=[3807500,5620600]
    elif change=='extra_coordinate_dimension':bad['geometry']['coordinates'][0][0].append(0)
    elif change=='wrong_depth':bad['geometry']['coordinates']=[3807500,5620600]
    elif change=='missing_properties':bad.pop('properties')
    elif change=='counts':data['numberReturned']=1
    elif change=='count_budget':monkeypatch.setattr(rgis,'MAX_FEATURES',2)
    elif change=='missing_crs':data.pop('crs')
    elif change=='wrong_crs':data['crs']['properties']['name']='EPSG:4326'
    elif change=='pagination':data['next']='https://public.invalid/page2'
    with pytest.raises(ValueError):
        rgis.normalize_partial(data,BOUNDS,'functional')


def test_later_record_error_rejects_response_after_a_quarantined_record():
    data=mixed(invalid_only=True)
    later=payload()['features'][0]
    later['properties']['NAME']={'nested':'not allowed'}
    data['features'].append(later)
    data.update(totalFeatures=2,numberMatched=2,numberReturned=2)
    with pytest.raises(ValueError,match='свойства'):
        rgis.normalize_partial(data,BOUNDS,'functional')


def test_invalid_feature_with_overlapping_bbox_but_no_actual_intersection_is_rejected():
    data=mixed(invalid_only=True)
    left=Polygon([(34.18,44.99),(34.19,45.0),(34.19,44.99),(34.18,45.0),(34.18,44.99)])
    right=Polygon([(34.22,44.99),(34.23,45.0),(34.23,44.99),(34.22,45.0),(34.22,44.99)])
    geom=convert(MultiPolygon([left,right]),4326,3857)
    query=convert(box(*BOUNDS),4326,3857)
    assert box(*geom.bounds).intersects(query) and not geom.intersects(query)
    data['features'][0]['geometry']=json.loads(json.dumps(mapping(geom)))
    with pytest.raises(ValueError,match='не относящаяся'):
        rgis.normalize_partial(data,BOUNDS,'functional')


def test_all_invalid_is_partial_missing_geometry_and_never_an_empty_source_response():
    observed=rgis.normalize_partial(mixed(invalid_only=True),BOUNDS,'functional')
    assert observed['geojson']['features']==[]
    assert (observed['received_count'],observed['accepted_count'],observed['invalid_count'])==(1,0,1)
    assert observed['state']=='partial' and not observed['geometry_processing_complete']
    assert any('Это не пустой ответ источника' in text for text in observed['warnings'])


def test_true_zero_and_all_valid_only_confirm_processing_of_returned_geometry():
    data=payload();data.update(features=[],totalFeatures=0,numberMatched=0,numberReturned=0)
    for source in (data,payload()):
        observed=rgis.normalize_partial(source,BOUNDS,'functional')
        assert observed['state']=='done' and observed['geometry_processing_complete']
        assert observed['invalid_count']==0 and observed['rejected_geometries']==[]
        assert observed['received_count']==observed['accepted_count']


def test_collect_separates_raw_counts_from_accepted_geometry_and_never_confirms_use(db,monkeypatch):
    data=mixed();calls=fake_source(monkeypatch,data)
    observed=rgis.collect(BOUNDS);layer=observed['layers']['functional']
    assert calls==['functional','settlements','roads']
    assert observed['version']==2 and observed['state']=='partial' and observed['warnings']
    assert layer['response_counts']=={'totalFeatures':2,'numberMatched':2,'numberReturned':2}
    assert layer['response_count_consistent'] and layer['count']==layer['accepted_count']==1
    assert layer['received_count']==2 and layer['invalid_count']==1
    assert layer['state']=='partial' and not layer['geometry_processing_complete']
    assert all(not layer[flag] for flag in ('coverage_confirmed','currentness_confirmed',
                                          'territorial_zone_confirmed','used_for_exclusion'))
    assert all(not observed[flag] for flag in ('coverage_confirmed','currentness_confirmed',
                                             'territorial_zone_confirmed','used_for_exclusion'))
    saved=json.loads((db/'rgis'/'raw'/(layer['sha256']+'.json')).read_bytes())
    assert saved==data
    sources=rgis.sources(observed,BOUNDS)
    assert sources['state']=='partial' and sources['applied']
    assert sources['layers']['functional']['rejected_geometries']==layer['rejected_geometries']
    assert 'geojson' not in sources['layers']['functional']


def test_collect_all_invalid_preserves_nonzero_raw_count_and_marks_missing_geometry(db,monkeypatch):
    fake_source(monkeypatch,mixed(invalid_only=True))
    observed=rgis.collect(BOUNDS)
    layer=observed['layers']['functional']
    assert observed['state']=='partial' and layer['state']=='partial'
    assert layer['count']==layer['accepted_count']==0
    assert layer['received_count']==layer['invalid_count']==1
    assert layer['response_counts']['numberReturned']==1
    assert layer['response_count_consistent'] and not layer['geometry_processing_complete']
    assert not layer['coverage_confirmed'] and not layer['used_for_exclusion']
    assert any('Это не пустой ответ источника' in text for text in layer['warnings'])
    assert not rgis.projected(observed,BOUNDS,recon.metric_for(BOUNDS))['functional']


def test_run_persists_partial_state_with_evidence_and_raw_mismatch_preserves_prior(db,monkeypatch):
    fake_source(monkeypatch,mixed())
    updated=rgis.run('trudovoe',{'bounds':BOUNDS})
    current=store.get_setting('rgis_context_trudovoe')
    assert updated['state']=='partial' and updated['invalid_counts']['functional']==1
    assert store.get_setting('rgis_attempt_trudovoe')['state']=='partial'
    assert current['layers']['functional']['rejected_geometries']
    bad=mixed();bad['numberMatched']=3;calls=fake_source(monkeypatch,bad)
    with pytest.raises(ValueError,match='Неполный'):
        rgis.run('trudovoe',{'bounds':BOUNDS})
    assert calls==['functional']
    assert store.get_setting('rgis_context_trudovoe')==current
    assert store.get_setting('rgis_attempt_trudovoe')['state']=='error'


def test_quarantined_geometry_is_not_projected_or_related_and_partial_never_changes_layout(db,monkeypatch):
    from test_recon import fixture
    values=fixture();options=recon.options({'purpose':'housing'})
    before=recon.build(values,options)
    fake_source(monkeypatch,mixed())
    context=rgis.collect(BOUNDS)
    values['rgis_context']=context
    after=recon.build(values,options)
    assert after['sources']['rgis_context']['applied']
    assert after['sources']['rgis_context']['state']=='partial'
    assert {c['id']:c['geometry'] for c in after['candidates']}=={
        c['id']:c['geometry'] for c in before['candidates']}
    assert after['summary']==before['summary']
    entries=rgis.projected(context,BOUNDS,recon.metric_for(BOUNDS))
    assert [f['id'] for _,f in entries['functional']]==['FunctionalZones_polygon.1']
    matches=rgis.relate(convert(box(*BOUNDS),4326,recon.metric_for(BOUNDS)),entries,context)
    assert matches['functional'] and all(row['source_state']=='partial' for row in matches['functional'])
    assert all(row['source_invalid_count']==1 for row in matches['functional'])
    for row in matches['functional']:
        assert not row['coverage_confirmed'] and not row['used_for_exclusion']
        assert not row['territorial_zone_confirmed'] and not row['currentness_confirmed']


def test_legacy_v1_is_readable_without_invented_geometry_processing_or_legal_claims():
    context=result();context['version']=1;before=copy.deepcopy(context)
    assert rgis.applicable(context,BOUNDS)
    assert rgis.projected(context,BOUNDS,recon.metric_for(BOUNDS))['functional']
    observed=rgis.sources(context,BOUNDS)
    assert observed['applied'] and observed['state']=='unknown_legacy'
    for layer in observed['layers'].values():
        assert layer['geometry_processing_complete'] is None
        assert layer['received_count'] is None and layer['invalid_count'] is None
        assert layer['state']=='unknown_legacy'
        assert not layer['coverage_confirmed'] and not layer['used_for_exclusion']
    assert context==before
    context['version']=99
    assert not rgis.applicable(context,BOUNDS)
    assert not rgis.sources(context,BOUNDS)['applied']
    for invalid_version in (True,1.0,'1'):
        context['version']=invalid_version
        assert not rgis.applicable(context,BOUNDS)
