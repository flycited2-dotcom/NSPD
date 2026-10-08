import copy
import json

from shapely.geometry import box, mapping

from land import pzz_context as pzz, recon, store
from test_recon import fixture, BOUNDS


def context(bounds=BOUNDS,empty=False):
    return {'id':'pzz-observation','version':pzz.VERSION,'bounds':bounds,'catalog':pzz.CATALOG,
            'layer':{'count':0 if empty else 1,'received_at':'original-date','sha256':'original-sha',
                     'geojson':{'type':'FeatureCollection','features':[] if empty else [
                         {'type':'Feature','id':'territorialzone_polygon.1','geometry':mapping(box(*BOUNDS)),
                          'properties':{'symbol':'Ж1','territorialzonename':'<script>zone</script>','status':'observed'}}]}}}


def test_regional_zones_are_dated_observations_without_granting_housing_rights_or_changing_layout():
    values=fixture();params=recon.options({'purpose':'housing'});before=recon.build(values,params)
    values['pzz_context']=context();after=recon.build(values,params)
    assert {c['id']:c['geometry'] for c in before['candidates']}=={c['id']:c['geometry'] for c in after['candidates']}
    assert before['summary']==after['summary']
    assert after['sources']['pzz_context']['applied']
    assert any(c['regional_pzz_matches'] for c in after['candidates'])
    for old,c in zip(before['candidates'],after['candidates']):
        assert c['flags']==old['flags'] and c['land_status']==old['land_status']
        assert not c['rights_confirmed'] and not c['srzu_ready']
        for row in c['regional_pzz_matches']:
            assert row['received_at']=='original-date' and row['sha256']=='original-sha'
            assert not row['currentness_confirmed']
    assert all(not f['properties']['used_for_exclusion'] for f in after['map_layers']['regional_pzz']['features'])
    after.update(id='result',operation_warnings=[]);data={'result':after,'stale':False}
    report=recon.html_report(data).decode()
    assert '&lt;script&gt;zone' in report and '<script>zone' not in report
    assert pzz.MAP in report and 'Региональный источник ПЗЗ Крыма' in report
    assert recon.collection(data)['features'][0]['properties']['regional_pzz_matches']==after['candidates'][0]['regional_pzz_matches']


def test_empty_or_other_area_never_establishes_zone_absence():
    values=fixture();params=recon.options({})
    values['pzz_context']=context(empty=True);result=recon.build(values,params)
    assert result['sources']['pzz_context']['applied']
    assert not result['sources']['pzz_context']['coverage_confirmed']
    assert not result['sources']['pzz_context']['currentness_confirmed']
    assert not result['map_layers']['regional_pzz']['features']
    result.update(id='result',operation_warnings=[])
    assert 'Это не подтверждает отсутствие ПЗЗ' in recon.html_report({'result':result}).decode()
    values['pzz_context']=context(bounds=[34.3,45,34.31,45.01]);different=recon.build(values,params)
    assert not different['sources']['pzz_context']['applied']
    assert not different['map_layers']['regional_pzz']['features']
    assert all(not c['regional_pzz_matches'] for c in different['candidates'])


def test_regional_zone_refresh_invalidates_prior_calculation(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init();values=fixture()
    for key,value in values.items():store.set_setting(key+'_trudovoe',value)
    recon.run('trudovoe',{'bounds':BOUNDS,'refresh_nspd':False})
    before=copy.deepcopy(store.get_setting('recon_trudovoe'))
    assert not recon.report('trudovoe')['stale']
    store.set_setting('pzz_context_trudovoe',json.loads(json.dumps(context(empty=True))))
    assert recon.report('trudovoe')['stale']
    assert store.get_setting('recon_trudovoe')==before
