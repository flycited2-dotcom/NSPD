"""Run the production recon UI to verify saved-purpose restoration and user choice."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_saved_purpose_loads_once_without_overwriting_user_choice_on_refresh():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the recon purpose UI regression')
    source = Path(__file__).resolve().parents[1] / 'web' / 'recon.js'
    harness = r'''
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const bounds=[34.202832,44.992064,34.207553,44.995402];
const elements=new Map();
function element(id){
 if(!elements.has(id))elements.set(id,{
  value:'',checked:false,disabled:false,innerHTML:'',textContent:'',listeners:{},
  classList:{remove(){},add(){}},
  addEventListener(type,callback){this.listeners[type]=callback}
 });
 return elements.get(id);
}
element('recon-purpose').value='unspecified';
element('recon-kind').value='all';
function layer(){return {
 addTo(){return this},setView(){return this},clearLayers(){},fitBounds(){},
 removeLayer(){},on(){}
}}
const result={id:'saved-housing-result',bounds,created_at:'2026-10-08T09:00:00Z',
 parameters:{purpose:'housing'},warning:'Rights remain unconfirmed.',operation_warnings:[],
 candidates:[],summary:{drafts:0,offers:0,auctions:0,large_gaps:0,
  excluded_parcels:42,excluded_buildings:67,filtered:{small:0,narrow:0}},
 sources:{nspd:{},torgi:{received_at:null,query:'',unlocated_numbers:[]}},
 layout:{layout_checks:0,layout_limit_reached:false},
 map_layers:{parcels:{},buildings:{},restrictions:{}}
};
let reconRequests=0;
async function api(url,args){
 assert.equal(args,undefined,'Refreshing the purpose must not start a search');
 if(url==='/api/recon'){
  reconRequests++;
  return {result:structuredClone(result),stale:false,watchlist:[],attempt:null};
 }
 if(url==='/api/planning/boundary')return {result:null,attempt:null};
 throw Error('Unexpected request '+url);
}
const context=vm.createContext({
 el:element,api,running:false,boundsInput:()=>bounds,
 L:{map:layer,featureGroup:layer,rectangle:layer,geoJSON:layer},
 LandBasemap:{bind(){}},selectCorner(){},startSelection(){},
 document:{querySelectorAll(){return []}},escapeHtml:value=>String(value??'')
});
const drain=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context,{filename:process.argv[1]});
 await drain();
 assert.equal(reconRequests,1);
 assert.equal(element('recon-purpose').value,'housing','Opening a saved result must restore its purpose');
 assert.ok(element('recon-status').textContent.includes('Цель: ИЖС'));
 // A display refresh must retain the user's next search purpose, even while the
 // currently displayed saved result still describes the earlier housing search.
 element('recon-purpose').value='personal_farm';
 await element('recon-kind').listeners.change();
 assert.equal(reconRequests,2);
 assert.equal(element('recon-purpose').value,'personal_farm','Refreshing results discarded the user purpose');
 assert.ok(element('recon-status').textContent.includes('Цель: ИЖС'));
 assert.equal(result.parameters.purpose,'housing','A UI choice must not mutate saved result evidence');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    completed = subprocess.run([node, '-e', harness, str(source)], capture_output=True, text=True,
                               encoding='utf-8', timeout=20, **options)
    assert completed.returncode == 0, completed.stdout + completed.stderr
