import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from land import candidate_checks, recon


def partial_context():
    from test_rgis_context import result
    context=result()
    context.update(version=2,state='partial',warnings=['source topology defect'])
    context['layers']['functional'].update(state='partial',received_count=2,accepted_count=1,
        invalid_count=1,geometry_processing_complete=False,
        rejected_geometries=[{'id':'FunctionalZones_polygon.<script>bad</script>',
            'reason':'Ring <img src=x onerror=bad> self-intersection',
            'original_geometry_sha256':'b'*64,'geometry_repaired':False}])
    return context


def test_partial_genplan_survives_frozen_dossier_and_export_without_changing_contours_or_clearance():
    from test_recon import fixture
    values=fixture();params=recon.options({'purpose':'housing'})
    before=recon.build(values,params)
    context=partial_context();original=json.dumps(context,sort_keys=True)
    values['rgis_context']=context;after=recon.build(values,params)
    assert {c['id']:c['geometry'] for c in before['candidates']}=={c['id']:c['geometry'] for c in after['candidates']}
    assert before['summary']==after['summary']
    assert after['version']==16 and after['sources']['rgis_context']['applied']
    assert after['sources']['rgis_context']['layers']['functional']['received_count']==2
    for candidate in after['candidates']:
        zoning=next(row for row in candidate['check_progress']['rows'] if row['key']=='zoning')
        assert zoning['state']=='attention' and not zoning['confirmed']
        assert 'Функциональные зоны генплана РГИС: получено 2 объектов' in zoning['observation']
        assert 'Геометрия обработана частично: принято 1 из 2; отклонено 1' in zoning['observation']
        assert not candidate['rights_confirmed'] and not candidate['srzu_ready']
        assert not candidate['access_evidence']['legal_access_confirmed']
    after.update(id='saved',operation_warnings=[])
    data={'result':after,'stale':False}
    html=recon.html_report(data).decode()
    assert 'Принято 1 из 2; отклонено 1' in html and 'Ошибочные объекты не исправлялись' in html
    assert '&lt;script&gt;bad&lt;/script&gt;' in html and '<script>bad</script>' not in html
    assert '&lt;img src=x onerror=bad&gt;' in html and '<img src=x onerror=bad>' not in html
    assert 'b'*64 in html
    feature=recon.collection(data)['features'][0]
    assert feature['properties']['check_progress']['rows'][2]['state']=='attention'
    assert json.dumps(context,sort_keys=True)==original


def test_all_invalid_is_distinct_from_empty_and_unapplied_partial_does_not_attach_to_another_area():
    layer={'state':'partial','count':0,'accepted_count':0,'received_count':1,'invalid_count':1,'received_at':'saved-date'}
    text=candidate_checks._layer('Генплан',layer)
    assert 'принято 0 из 1; отклонено 1' in text
    assert 'получено 1 объектов' in text
    assert 'Пустой ответ' not in text
    from test_recon import fixture
    values=fixture();context=partial_context();context['bounds']=[34.3,45,34.31,45.01]
    values['rgis_context']=context;built=recon.build(values,recon.options({'purpose':'housing'}))
    assert not built['sources']['rgis_context']['applied']
    assert all(not rows for candidate in built['candidates'] for rows in candidate['general_plan_matches'].values())
    rendered=recon.rgis_omissions_section(built)
    assert 'не применён' in rendered and 'Принято 1 из 2' not in rendered


def test_partial_status_renders_counts_and_omissions_without_fetching_or_claiming_success():
    node=shutil.which('node')
    if not node:pytest.skip('Node is required for UI acceptance')
    root=Path(__file__).resolve().parents[1]
    harness=r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const bounds=[34.20,44.99,34.21,45.0], elements={};
const el=id=>elements[id]??=({checked:false,textContent:''});
const layer=()=>({setView(){return this},addTo(){return this}});
const context=vm.createContext({el,boundsInput:()=>bounds,reconDate:String,Date,
 L:{map:layer,featureGroup:layer},LandBasemap:{bind(){}},
 api(){throw Error('Rendering must not call sources')},fetch(){throw Error('Must not fetch')},data:null});
let source=fs.readFileSync(process.argv[1],'utf8');
vm.runInContext(source.slice(0,source.indexOf('async function refreshRgis()')),context);
context.data={attempt:{state:'partial'},result:{bounds,warning:'Полнота не подтверждена',layers:{functional:{
 count:36,state:'partial',received_count:37,accepted_count:36,invalid_count:1,received_at:'saved-date',
 rejected_geometries:[{id:'FunctionalZones_polygon.7901',reason:'Ring Self-intersection'}]}}}};
vm.runInContext('rgisStatusData=data;drawRgisStatus();',context);
let text=elements['rgis-status'].textContent;
for(const phrase of ['получено частично','принято 36 из 37, отклонено 1','FunctionalZones_polygon.7901',
 'Ring Self-intersection','не исправлялись','saved-date','Полнота не подтверждена'])assert.ok(text.includes(phrase),phrase);
assert.ok(!text.includes('Последняя проверка РГИС: завершена'));
source=fs.readFileSync(process.argv[2],'utf8');
vm.runInContext(source.slice(0,source.indexOf('let reconWatch=[];')),context);
text=context.reconRgisOmissionsText({applied:true,layers:context.data.result.layers});
assert.ok(text.includes('частично')&&text.includes('36 из 37')&&text.includes('отклонено 1'));
assert.equal(context.reconRgisOmissionsText({applied:false,layers:context.data.result.layers}),'');
assert.equal(context.reconRgisOmissionsText({applied:true,layers:{functional:{count:36}}}),'');
'''
    subprocess.run([node,'-e',harness,str(root/'web/rgis.js'),str(root/'web/recon.js')],check=True,capture_output=True,text=True)
