"""Regional PZZ evidence stays dated and separate while the selected area changes."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def run_node(harness, *arguments):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the regional PZZ UI regression')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    completed = subprocess.run([node, '-e', harness, *map(str, arguments)], capture_output=True,
                               text=True, encoding='utf-8', timeout=20, **options)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize('pzz_first', [True, False])
def test_empty_pzz_status_follows_area_restore_and_selection_without_source_refresh(pzz_first):
    web = Path(__file__).resolve().parents[1] / 'web'
    harness = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const saved=[34.202832,44.992064,34.207553,44.995402];
const initial=[34.19,44.98,34.21,45.0],elements=new Map(),maps=new Map(),pending=new Map(),requests=[];
function element(id){
 if(!elements.has(id))elements.set(id,{value:'',checked:false,disabled:false,innerHTML:'',textContent:'',listeners:{},
  classList:{remove(){},add(){}},addEventListener(type,callback){this.listeners[type]=callback}});
 return elements.get(id);
}
['west','south','east','north'].forEach((id,i)=>element(id).value=String(initial[i]));
function layer(){return {addTo(){return this},setView(){return this},clearLayers(){},fitBounds(){},removeLayer(){},
 on(type,callback){this.listeners??={};this.listeners[type]=callback},getContainer(){return {style:{}}},
 getBounds(){return {isValid(){return true}}}}}
const source={result:{id:'empty-pzz',bounds:saved,catalog:{map_name:'ПЗЗ Республики Крым'},
 layer:{count:0,http_status:200,received_at:'original-date',geojson:{type:'FeatureCollection',features:[]}},
 coverage_confirmed:false,currentness_confirmed:false,warning:'ИЖС требует подтверждения.'},attempt:null};
const original=JSON.stringify(source);
function fetch(url,options){
 requests.push({url,options});
 assert.ok(options===undefined||Object.keys(options).length===0,'Area changes must not request official source updates');
 if(url==='/api/session')return Promise.resolve({ok:true,json:async()=>({token:'local-test-token'})});
 assert.ok(['/api/nspd','/api/pzz'].includes(url),url);
 return new Promise(resolve=>pending.set(url,data=>resolve({ok:true,json:async()=>data})));
}
const context=vm.createContext({document:{getElementById:element,querySelectorAll(){return []}},fetch,
 L:{map:id=>{const result=layer();maps.set(id,result);return result},featureGroup:layer,rectangle:layer,
 circleMarker:layer,geoJSON:layer,control:{scale:layer}},LandBasemap:{bind(){}},reconDate:value=>String(value),
 setTimeout:callback=>{queueMicrotask(callback);return 1}});
const drain=()=>new Promise(resolve=>setImmediate(resolve));
const status=()=>element('pzz-status').textContent;
function checkEmpty(){
 assert.ok(status().includes('HTTP 200'));
 assert.ok(status().includes('original-date'));
 assert.ok(status().includes('Источник подключён, зоны не получены'));
 assert.ok(status().includes('Полное покрытие и действующая редакция не подтверждены'));
 assert.ok(!status().includes('земля свободна'));
}
(async()=>{
 vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
 vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);await drain();
 const restore=()=>pending.get('/api/nspd')({result:null,attempt:null,watchlist:[],area:{bounds:saved}});
 if(process.argv[3]==='true'){
  pending.get('/api/pzz')(source);await drain();
  assert.ok(status().includes('другой области'));checkEmpty();restore();await drain();
 }else{restore();await drain();pending.get('/api/pzz')(source);await drain()}
 checkEmpty();assert.ok(status().includes('Область совпадает с выбранной.'));
 const count=requests.length;
 element('west').value='34.203';element('west').listeners.input();
 assert.ok(status().includes('для выбранной области они не применяются'));checkEmpty();
 element('west').value=String(saved[0]);element('west').listeners.input();
 assert.ok(status().includes('Область совпадает с выбранной.'));
 element('select-area').listeners.click();const nmap=maps.get('nmap');
 nmap.listeners.click({target:nmap,latlng:{lng:34.22,lat:45.01}});
 nmap.listeners.click({target:nmap,latlng:{lng:34.23,lat:45.02}});
 assert.ok(status().includes('другой области'));checkEmpty();
 element('select-area').listeners.click();
 nmap.listeners.click({target:nmap,latlng:{lng:saved[0],lat:saved[1]}});
 nmap.listeners.click({target:nmap,latlng:{lng:saved[2],lat:saved[3]}});
 assert.ok(status().includes('Область совпадает с выбранной.'));
 assert.equal(requests.length,count);
 assert.equal(requests.filter(row=>row.url==='/api/pzz').length,1);
 assert.equal(JSON.stringify(source),original,'Area changes rewrote dated PZZ evidence');
 assert.equal(element('pzz-show').checked,false,'Regional PZZ overlay must start disabled');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    run_node(harness, web / 'nspd.js', web / 'pzz.js', str(pzz_first).lower())


def test_pzz_card_does_not_apply_other_area_source_or_new_source_to_saved_candidate():
    recon = Path(__file__).resolve().parents[1] / 'web' / 'recon.js'
    harness = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const code=fs.readFileSync(process.argv[1],'utf8');
const context=vm.createContext({reconDate:value=>value||'не получено'});
vm.runInContext(code.slice(code.indexOf('function reconRegionalPzzText('),code.indexOf('function showReconCandidate(')),context);
const render=(candidate,source)=>context.reconRegionalPzzText(candidate,source);
const empty={id:'empty',applied:true,layer:{count:0,received_at:'empty-date'}};
let text=render({regional_pzz_matches:[]},empty);
assert.ok(text.includes('Зоны не получены; пустой ответ не подтверждает отсутствие ПЗЗ'));
assert.ok(text.includes('empty-date'));assert.ok(text.includes('допустимость использования не подтверждены'));
text=render({regional_pzz_matches:[]},{id:'other-area',applied:false,layer:{count:10,received_at:'other-date'}});
assert.ok(text.includes('зоны не применены'));assert.ok(!text.includes('other-date'));
text=render({regional_pzz_matches:[{id:'old-zone',received_at:'old-date',fields:{CODE:'Ж1'}}]},null);
assert.ok(text.includes('1 пересечений'));assert.ok(text.includes('old-date'));assert.ok(!text.includes('empty-date'));
assert.ok(!text.includes('ИЖС разрешено'));assert.ok(!text.includes('Ж1'));
text=render({},null);assert.ok(text.includes('сведения источника этой версии не получены'));
'''
    run_node(harness, recon)
