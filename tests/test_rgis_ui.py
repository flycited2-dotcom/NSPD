"""Exercise the real area-loading and RGIS scripts with either response order."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('rgis_first',[True,False])
def test_rgis_area_status_tracks_async_restore_input_and_map_selection_without_fetching(rgis_first):
    node=shutil.which('node')
    if not node:pytest.skip('Node.js is required for the area restoration UI regression')
    web=Path(__file__).resolve().parents[1]/'web'
    harness=r'''
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const rgisFirst=process.argv[3]==='true';
const saved=[34.202832,44.992064,34.207553,44.995402];
const initial=[34.19,44.98,34.21,45.0];
const elements=new Map(),maps=new Map(),pending=new Map(),requests=[];
function element(id){
 if(!elements.has(id))elements.set(id,{
  value:'',checked:false,disabled:false,innerHTML:'',textContent:'',listeners:{},
  classList:{remove(){},add(){}},
  addEventListener(type,callback){this.listeners[type]=callback}
 });
 return elements.get(id);
}
['west','south','east','north'].forEach((id,i)=>element(id).value=String(initial[i]));
function layer(){return {
 addTo(){return this},setView(){return this},clearLayers(){},fitBounds(){},removeLayer(){},
 on(type,callback){this.listeners??={};this.listeners[type]=callback},
 getContainer(){return {style:{}}},getBounds(){return {isValid(){return true}}}
}}
const rgis={result:{id:'saved-rgis',bounds:saved,layers:{functional:{count:8,received_at:'original-date'}},
 warning:'Действующие зоны ПЗЗ не подтверждены.'},attempt:null};
const original=JSON.stringify(rgis);
function fetch(url,options){
 requests.push({url,options});
 assert.ok(options===undefined||Object.keys(options).length===0,'Area display must never initiate a remote refresh');
 if(url==='/api/session')return Promise.resolve({ok:true,json:async()=>({token:'local-test-token'})});
 assert.ok(['/api/nspd','/api/rgis'].includes(url),url);
 return new Promise(resolve=>pending.set(url,data=>resolve({ok:true,json:async()=>data})));
}
const context=vm.createContext({
 document:{getElementById:element,querySelectorAll(){return []}},fetch,
 L:{map:id=>{const value=layer();maps.set(id,value);return value},featureGroup:layer,
  rectangle:layer,circleMarker:layer,geoJSON:layer,control:{scale:layer}},
 LandBasemap:{bind(){}},reconDate:value=>String(value),
 setTimeout:callback=>{queueMicrotask(callback);return 1}
});
const drain=()=>new Promise(resolve=>setImmediate(resolve));
const status=()=>element('rgis-status').textContent;
const same='Область совпадает с выбранной.';
const different='Полученные слои относятся к другой области.';
(async()=>{
 vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context,{filename:process.argv[1]});
 vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context,{filename:process.argv[2]});
 await drain();
 assert.ok(pending.has('/api/nspd'));assert.ok(pending.has('/api/rgis'));
 const restore=()=>pending.get('/api/nspd')({result:null,attempt:null,watchlist:[],area:{bounds:saved}});
 if(rgisFirst){
  pending.get('/api/rgis')(rgis);await drain();
  assert.ok(status().includes(different),'Initial HTML coordinates differ from the saved RGIS area');
  restore();await drain();
 }else{
  restore();await drain();pending.get('/api/rgis')(rgis);await drain();
 }
 assert.ok(status().includes(same),'Status remained stale after restoring the saved area');
 assert.ok(!status().includes(different));assert.ok(status().includes('original-date'));
 const requestCount=requests.length;
 // Typing a new bound updates the cached-source comparison without fetching.
 element('west').value='34.203';element('west').listeners.input();
 assert.ok(status().includes(different));
 element('west').value=String(saved[0]);element('west').listeners.input();
 assert.ok(status().includes(same));
 // Selecting corners on the actual map uses writeBounds, the same path as restore.
 element('select-area').listeners.click();
 const nmap=maps.get('nmap');
 nmap.listeners.click({target:nmap,latlng:{lng:34.22,lat:45.01}});
 nmap.listeners.click({target:nmap,latlng:{lng:34.23,lat:45.02}});
 assert.ok(status().includes(different));
 element('select-area').listeners.click();
 nmap.listeners.click({target:nmap,latlng:{lng:saved[0],lat:saved[1]}});
 nmap.listeners.click({target:nmap,latlng:{lng:saved[2],lat:saved[3]}});
 assert.ok(status().includes(same));
 assert.equal(requests.length,requestCount,'Changing the area triggered another source request');
 assert.equal(requests.filter(r=>r.url==='/api/rgis').length,1);
 assert.equal(JSON.stringify(rgis),original,'Display changes altered dated source evidence');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
    completed=subprocess.run([node,'-e',harness,str(web/'nspd.js'),str(web/'rgis.js'),str(rgis_first).lower()],
                             capture_output=True,text=True,encoding='utf-8',timeout=20,**options)
    assert completed.returncode==0,completed.stdout+completed.stderr
