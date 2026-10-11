"""The actual recon UI restores a saved search once, preserving in-flight edits."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_saved_parameters_restore_once_and_user_edits_win_async_initial_load():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for recon parameter restoration')
    source = Path(__file__).resolve().parents[1] / 'web' / 'recon.js'
    harness = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const code=fs.readFileSync(process.argv[1],'utf8');
const bounds=[34.202832,44.992064,34.207553,44.995402];
const settings={purpose:'housing',target_area:1450.5,min_area:600.25,max_area:3200.75,
 min_width:16.5,limit:20,avoid_restrictions:false,avoid_planned:false,
 avoid_environment:false,verify_nspd:false,refresh_nspd:false,
 refresh_torgi:true,refresh_active_torgi:false,refresh_active_documents:false};
const ids={purpose:'recon-purpose',target_area:'recon-area-size',min_area:'survey-min',
 max_area:'survey-max',min_width:'survey-width',limit:'recon-limit',
 avoid_restrictions:'recon-avoid-zones',avoid_planned:'recon-avoid-planned',
 avoid_environment:'recon-avoid-environment',verify_nspd:'recon-verify-nspd'};
function layer(){return {addTo(){return this},setView(){return this},clearLayers(){},fitBounds(){},removeLayer(){},on(){}}}
const drain=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(){
 const elements=new Map();
 function el(id){
  if(!elements.has(id))elements.set(id,{value:'default',checked:true,disabled:false,innerHTML:'',textContent:'',
   listeners:{},classList:{remove(){},add(){}},
   addEventListener(type,callback){(this.listeners[type]??=[]).push(callback)},
   fire(type){for(const callback of this.listeners[type]||[])callback()}});
  return elements.get(id);
 }
 el('recon-kind').value='all';
 const result={id:'saved',bounds,created_at:'2026-10-11T09:00:00Z',parameters:structuredClone(settings),
  warning:'Rights unconfirmed',operation_warnings:[],candidates:[],
  summary:{drafts:0,offers:0,auctions:0,large_gaps:0,excluded_parcels:42,excluded_buildings:67,filtered:{small:0,narrow:0}},
  sources:{nspd:{},torgi:{received_at:null,query:'',unlocated_numbers:[]}},
  layout:{layout_checks:0,layout_limit_reached:false},map_layers:{parcels:{},buildings:{},restrictions:{}}};
 let release,requests=0;
 const initial=new Promise(resolve=>release=resolve);
 async function api(url,args){
  assert.equal(args,undefined,'Restoration must never submit or refresh source data');
  if(url==='/api/recon'){
   requests++;
   if(requests===1)await initial;
   return {result:structuredClone(result),stale:false,watchlist:[],attempt:null};
  }
  if(url==='/api/planning/boundary')return {result:null,attempt:null};
  throw Error('Unexpected request '+url);
 }
 const context=vm.createContext({el,api,running:false,boundsInput:()=>bounds,
  L:{map:layer,featureGroup:layer,rectangle:layer,geoJSON:layer},LandBasemap:{bind(){}},
  selectCorner(){},startSelection(){},document:{querySelectorAll(){return []}},escapeHtml:value=>String(value??'')});
 vm.runInContext(code,context,{filename:process.argv[1]});
 return {context,el,result,release,get requests(){return requests}};
}
(async()=>{
 // The production pure helper accepts actual typed values and excludes all network refresh flags.
 const f=fixture(),select=f.context.savedReconParameters;
 const frozen=JSON.stringify(settings);
 let restored=select(settings,new Set());
 assert.equal(restored.loaded,true);assert.equal(restored.updates.length,10);
 const byId=Object.fromEntries(restored.updates.map(update=>[update.id,update]));
 assert.equal(byId['recon-limit'].value,'20');
 for(const key of ['target_area','min_area','max_area','min_width'])assert.equal(byId[ids[key]].value,String(settings[key]));
 for(const key of ['avoid_restrictions','avoid_planned','avoid_environment','verify_nspd']){
  assert.equal(byId[ids[key]].property,'checked');assert.equal(byId[ids[key]].value,false);
 }
 assert.equal(byId['recon-purpose'].value,'housing');
 assert.ok(!restored.updates.some(update=>update.id.startsWith('recon-update-')));
 assert.equal(JSON.stringify(settings),frozen,'Restoring controls must not rewrite frozen result evidence');
 assert.equal(select(settings,new Set(),true).updates.length,0);
 assert.equal(select({},new Set()).updates.length,0,'Missing legacy fields must keep current defaults');
 assert.equal(select({purpose:'housing'},new Set()).updates.length,1);
 for(const invalid of [null,[],true,'saved'])assert.equal(select(invalid,new Set()).updates.length,0);
 for(const [key,bad] of Object.entries({target_area:'1450',min_area:true,max_area:Infinity,
   min_width:-1,limit:20.5,purpose:'future-purpose',avoid_restrictions:1,
   avoid_planned:'false',avoid_environment:null,verify_nspd:{checked:true}})){
  assert.equal(select({[key]:bad},new Set()).updates.length,0,key+' invalid type/range must not be restored');
 }
 for(const bad of [0,21,NaN,true,'20'])assert.equal(select({limit:bad},new Set()).updates.length,0);
 assert.equal(select({min_width:0},new Set()).updates[0].value,'0');
 // Bindings are active before the deferred API result; input and checkbox change both win independently.
 f.el('recon-area-size').value='1800';f.el('recon-area-size').fire('input');
 f.el('recon-purpose').value='personal_farm';f.el('recon-purpose').fire('change');
 f.el('recon-avoid-zones').checked=true;f.el('recon-avoid-zones').fire('change');
 f.release();await drain();
 assert.equal(f.el('recon-area-size').value,'1800');
 assert.equal(f.el('recon-purpose').value,'personal_farm');
 assert.equal(f.el('recon-avoid-zones').checked,true);
 assert.equal(f.el('recon-limit').value,'20');
 assert.equal(f.el('survey-min').value,'600.25');
 assert.equal(f.el('survey-max').value,'3200.75');
 assert.equal(f.el('survey-width').value,'16.5');
 assert.equal(f.el('recon-avoid-planned').checked,false);
 assert.equal(f.el('recon-avoid-environment').checked,false);
 assert.equal(f.el('recon-verify-nspd').checked,false);
 assert.equal(f.el('recon-update-nspd').checked,true,'Saved network refresh flags must not change checked defaults');
 assert.ok(f.el('recon-status').textContent.includes('Цель: ИЖС'),'Result status must describe frozen saved purpose');
 // Filter changes and later polling cannot reset any control, even without another DOM event.
 f.el('recon-limit').value='7';f.el('recon-avoid-environment').checked=true;
 f.result.parameters={...settings,target_area:2200,limit:13};
 await f.el('recon-kind').listeners.change[0]();
 await f.context.refreshRecon();
 assert.equal(f.requests,3);assert.equal(f.el('recon-limit').value,'7');
 assert.equal(f.el('recon-area-size').value,'1800');assert.equal(f.el('recon-avoid-environment').checked,true);
 // An untouched first result restores every field including all false booleans.
 const untouched=fixture();untouched.release();await drain();
 for(const [key,id] of Object.entries(ids)){
  const value=settings[key];
  assert.equal(untouched.el(id)[typeof value==='boolean'?'checked':'value'],typeof value==='boolean'?value:String(value),key);
 }
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    completed = subprocess.run([node, '-e', harness, str(source)], capture_output=True,
                               encoding='utf-8', timeout=20, **options)
    assert completed.returncode == 0, completed.stdout + completed.stderr
