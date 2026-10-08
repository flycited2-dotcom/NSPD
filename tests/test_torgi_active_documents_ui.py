"""Exercise the real regional document UI with controlled asynchronous responses."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_late_lot_response_cannot_replace_new_selection_or_read_target():
    node=shutil.which('node')
    if not node:pytest.skip('Node.js is required for the asynchronous UI regression')
    source=Path(__file__).resolve().parents[1]/'web'/'torgi_active_documents.js'
    harness=r'''
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const A='22000084990000000030_1',B='24000026680000000644_1';
const elements=new Map();
function element(id){
 if(!elements.has(id))elements.set(id,{
  value:'',checked:false,disabled:false,innerHTML:'',textContent:'',listeners:{},
  classList:{remove(){},add(){}},
  addEventListener(type,callback){this.listeners[type]=callback}
 });
 return elements.get(id);
}
const selector=element('regional-doc-lot');selector.value=A;
const buttons=['collect','read','reprocess','ocr'].map(name=>element('regional-doc-'+name));
const waiting=[],posts=[];let race=true,reconRefreshes=0;
function card(id){return {lot_id:id,state:'received',attachments:[],lot_url:'https://torgi.gov.ru/example',received_at:'observed-date',card_status:'PUBLISHED'}}
function response(id,chosen){
 return {result:{id,cards:[card(A),card(B)],files:{},warning:id},
  selected_lot_id:chosen,selected_files_remaining:1,selected_files_to_reprocess:0,
  cards_remaining:0,files_remaining:2,files_to_reprocess:0,visual_remaining:0};
}
async function api(url,args){
 if(args!==undefined){
  assert.equal(url,'/api/torgi/active/documents/read');
  assert.equal(selector.disabled,true,'The selected lot must stay fixed during the job');
  posts.push(JSON.parse(JSON.stringify(args)));return {job_id:'read-B'};
 }
 if(url==='/api/jobs/read-B')return {state:'done'};
 assert.ok(url.startsWith('/api/torgi/active/documents?lot_id='),url);
 if(!race)return response('catalog-B',B);
 return new Promise(resolve=>waiting.push({url,resolve}));
}
const context=vm.createContext({
 el:element,api,running:false,document:{querySelectorAll(){return buttons}},
 escapeHtml:value=>String(value??''),tdate:value=>String(value??''),statusName:value=>value,
 operationState:value=>value,docErrorText:value=>value,
 refreshRecon:async()=>{reconRefreshes++},
 setTimeout:callback=>{queueMicrotask(callback);return 1}
});
const drain=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 // Loading the actual UI starts refresh A; then the user selects B before A completes.
 vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context,{filename:process.argv[1]});
 assert.equal(waiting.length,1);
 assert.equal(waiting[0].url,'/api/torgi/active/documents?lot_id='+A);
 selector.value=B;
 const refreshB=selector.listeners.change();
 assert.equal(waiting.length,2);
 assert.equal(waiting[1].url,'/api/torgi/active/documents?lot_id='+B);
 waiting[1].resolve(response('catalog-B',B));await refreshB;
 assert.equal(selector.value,B);
 assert.equal(vm.runInContext('regionalDocs.id',context),'catalog-B');
 const displayed=element('regional-doc-results').innerHTML;
 const status=element('regional-doc-status').textContent;
 assert.ok(displayed.includes(B));assert.ok(!displayed.includes(A));
 // The older A response must change neither the UI nor the catalog used by the action.
 waiting[0].resolve(response('catalog-A',A));await drain();
 assert.equal(selector.value,B,'A late response restored the previous lot');
 assert.equal(vm.runInContext('regionalDocs.id',context),'catalog-B','A late response replaced the action catalog');
 assert.equal(element('regional-doc-results').innerHTML,displayed);
 assert.equal(element('regional-doc-status').textContent,status);
 race=false;
 await element('regional-doc-read').listeners.click();
 assert.deepEqual(posts,[{retry_errors:false,id:'catalog-B',lot_id:B}]);
 assert.equal(selector.value,B);assert.equal(selector.disabled,false);
 assert.equal(reconRefreshes,1);
 assert.ok(buttons.every(button=>!button.disabled));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
    completed=subprocess.run([node,'-e',harness,str(source)],capture_output=True,text=True,
                             encoding='utf-8',timeout=20,**options)
    assert completed.returncode==0,completed.stdout+completed.stderr


def test_regional_egrn_comparison_uses_explicit_lots_without_historical_fallback():
    node=shutil.which('node')
    if not node:pytest.skip('Node.js is required for the EGRN evidence UI regression')
    source=Path(__file__).resolve().parents[1]/'web'/'torgi_documents.js'
    harness=r'''
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const lotId='22000084990000000030_1';
const regionalNumber='90:12:170102:31',historicalNumber='90:12:170102:99';
const elements=new Map();
function element(id){
 if(!elements.has(id))elements.set(id,{textContent:'',addEventListener(){},classList:{remove(){}}});
 return elements.get(id);
}
const card={lot_id:lotId};
const file={unit:'pdf_page',processed_pages:1,total_pages:1,mentions:[],tables:[],
 associations:[{lot_id:lotId,scope:'lot',inactive:false}],
 geometry_confirmed:false,georeferenced:false,
 egrn_tables:[{label:'Таблица из выписки',state:'review_required',cadastral_number:regionalNumber,
  pages:[1],section_sheets_expected:1,crs_label:'СК-63, зона 5',issues:[],
  points:[{label:'1',x:100,y:200,accuracy_stated_m:2.5,page:1}]}]};
const context=vm.createContext({
 el:element,escapeHtml:value=>String(value??''),
 currentTorgi:{lots:[{id:lotId,cadastral_numbers:[historicalNumber]}]},
 // The whole production script runs, including its harmless initial empty refresh.
 api:async()=>({result:null,cards_remaining:0}),
 file,card,regionalLots:[{id:lotId,cadastral_numbers:[regionalNumber]}]
});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context,{filename:process.argv[1]});
const match='Номер таблицы совпадает со структурированным номером лота.';
const mismatch='Номер таблицы не совпадает со структурированным номером лота';
const unknown='Номер лота не загружен для сравнения.';
for(const helper of ['egrnTableInfo','torgiFileEvidence']){
 const historical=vm.runInContext(helper+'(file,card)',context);
 assert.ok(historical.includes(mismatch),helper+' must retain the historical default');
 assert.ok(!historical.includes(match));
 const regional=vm.runInContext(helper+'(file,card,regionalLots)',context);
 assert.ok(regional.includes(match),helper+' ignored the explicit regional cadastral number');
 assert.ok(!regional.includes(mismatch));assert.ok(!regional.includes(unknown));
 const unavailable=vm.runInContext(helper+'(file,card,[])',context);
 assert.ok(unavailable.includes(unknown),helper+' fell back to unrelated historical data');
 assert.ok(!unavailable.includes(match));assert.ok(!unavailable.includes(mismatch));
 assert.ok(regional.includes('Географическая граница, действующие права и свободность земли не подтверждены.'));
}
assert.equal(context.currentTorgi.lots[0].cadastral_numbers[0],historicalNumber);
'''
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
    completed=subprocess.run([node,'-e',harness,str(source)],capture_output=True,text=True,
                             encoding='utf-8',timeout=20,**options)
    assert completed.returncode==0,completed.stdout+completed.stderr
