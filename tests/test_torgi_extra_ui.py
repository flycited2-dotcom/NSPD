"""Real extra-document controls preserve lot intent and errors across async refreshes."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_large_pdf_controls_ignore_stale_responses_lock_intent_and_retain_job_errors():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for extra-document UI races')
    source = Path(__file__).resolve().parents[1] / 'web' / 'torgi_extra.js'
    harness = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const code=source.slice(0,source.indexOf('for(const [prefix,base] of torgiExtraSections)'));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const payload=(remaining,lot='',extra={})=>({result:{id:'catalog'},selected_lot_id:lot,
 search_stale:false,large_pdf_summary:{remaining,retry_remaining:remaining},...extra});
function fixture(handler){
 const nodes={};const el=id=>nodes[id]??=({value:'',checked:false,disabled:false,textContent:''});
 const unrelated={disabled:true},enabled={disabled:false};
 const buttons=[el('torgi-doc-large-pdf'),el('regional-doc-large-pdf'),unrelated,enabled];
 const context=vm.createContext({api:handler,el,running:false,operationState:String,docErrorText:String,
  document:{querySelectorAll(selector){return selector==='button'?buttons:[]}},
  reconStale:false,sameReconBounds:()=>true,refreshRegionalDocs:async()=>{},
  refreshTorgiDocs:async()=>{},refreshRecon:async()=>{},setTimeout:callback=>callback()});
 vm.runInContext(code,context,{filename:process.argv[1]});
 return {context,el,unrelated,enabled};
}
(async()=>{
 // A late response (or late error) for A cannot change B's summary or its disabled mask.
 let pending=[];let f=fixture(url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject})));
 const base='/api/torgi/active/documents';
 f.el('regional-doc-lot').value='LOT-A';const a=f.context.refreshTorgiExtra('regional-doc',base);
 f.el('regional-doc-lot').value='LOT-B';const b=f.context.refreshTorgiExtra('regional-doc',base);
 pending[1].resolve(payload(0,'LOT-B'));await b;
 assert.equal(f.el('regional-doc-large-pdf').disabled,true);
 pending[0].resolve(payload(1,'LOT-A'));assert.equal(await a,null);
 assert.equal(f.el('regional-doc-large-pdf').disabled,true);
 assert.ok(f.el('regional-doc-extra-status').textContent.includes('осталось 0'));
 // Even a usable latest response stays masked until all older pending callbacks settle.
 pending=[];const old=f.context.refreshTorgiExtra('regional-doc',base);
 const fresh=f.context.refreshTorgiExtra('regional-doc',base);
 pending[1].resolve(payload(1,'LOT-B'));await fresh;
 assert.equal(f.el('regional-doc-large-pdf').disabled,true,'Pending earlier callback must keep controls masked');
 pending[0].reject(Error('stale failure'));assert.equal(await old,null);
 assert.equal(f.el('regional-doc-large-pdf').disabled,false);
 // Lock happens before initial GET, blocking a second click and preserving the disabled selector state.
 pending=[];let posts=[];
 f=fixture((url,args)=>{
  if(args){posts.push(args);return Promise.resolve({job_id:'job'});}
  if(url.startsWith('/api/jobs/'))return Promise.resolve({state:'done'});
  return new Promise(resolve=>pending.push({url,resolve}));
 });
 f.el('regional-doc-lot').value='LOT-A';f.el('regional-doc-lot').disabled=true;
 const run=f.context.runTorgiExtra('regional-doc',base,'large-pdf');
 assert.equal(f.context.running,true);assert.equal(f.enabled.disabled,true);
 await f.context.runTorgiExtra('regional-doc',base,'large-pdf');assert.equal(pending.length,1);
 // Programmatic navigation during the pending GET cancels, rather than reading another lot.
 f.el('regional-doc-lot').value='LOT-B';pending[0].resolve(payload(1,'LOT-A'));await tick();
 assert.equal(posts.length,0);assert.equal(pending.length,2);
 pending[1].resolve(payload(0,'LOT-B'));await run;
 assert.equal(f.context.running,false);assert.equal(f.el('regional-doc-lot').disabled,true);
 assert.equal(f.unrelated.disabled,true);assert.equal(f.enabled.disabled,false);
 assert.ok(f.el('regional-doc-extra-status').textContent.includes('операция отменена'));
 // An error before the backend writes an attempt must remain after the final summary refresh.
 for(const failure of ['post','job','refresh']){
  let gets=0;
  f=fixture(async(url,args)=>{
   if(args){if(failure==='post')throw Error('STALE POST REJECTED');return {job_id:'job'};}
   if(url.startsWith('/api/jobs/'))return failure==='job'?{state:'error',error:'JOB FAILED'}:{state:'done'};
   if(++gets===2&&failure==='refresh')throw Error('FINAL REFRESH FAILED');
   return payload(1,'LOT-A');
  });
  f.el('regional-doc-lot').value='LOT-A';f.el('regional-doc-lot').disabled=false;
  await f.context.runTorgiExtra('regional-doc',base,'large-pdf');
  assert.equal(f.context.running,false);assert.equal(f.el('regional-doc-lot').disabled,false);
  assert.equal(f.unrelated.disabled,true);assert.equal(f.enabled.disabled,false);
  assert.ok(f.el('regional-doc-extra-status').textContent.includes(
    failure==='post'?'STALE POST REJECTED':failure==='job'?'JOB FAILED':'FINAL REFRESH FAILED'),failure);
  if(failure==='refresh')assert.equal(f.el('regional-doc-large-pdf').disabled,true);
 }
 // Retry and lot parameters are frozen at click time; a successful job refreshes observations once.
 posts=[];let getCount=0,regionalRefreshes=0,reconRefreshes=0;
 f=fixture(async(url,args)=>{
  if(args){posts.push(args);return {job_id:'job'};}
  if(url.startsWith('/api/jobs/'))return {state:'done'};
  getCount++;return payload(1,'LOT-A');
 });
 f.context.refreshRegionalDocs=async()=>regionalRefreshes++;
 f.context.refreshRecon=async()=>reconRefreshes++;
 f.el('regional-doc-lot').value='LOT-A';f.el('regional-doc-extra-retry').checked=true;
 const success=f.context.runTorgiExtra('regional-doc',base,'large-pdf');
 f.el('regional-doc-extra-retry').checked=false;await success;
 assert.equal(posts.length,1);assert.equal(posts[0].id,'catalog');
 assert.equal(posts[0].lot_id,'LOT-A');assert.equal(posts[0].retry_errors,true);
 assert.equal(regionalRefreshes,1);assert.equal(reconRefreshes,1);assert.equal(getCount,2);
 // Neither a zero queue nor stale current search submits a job.
 for(const data of [payload(0,'LOT-A'),payload(1,'LOT-A',{search_stale:true})]){
  let count=0;f=fixture(async(url,args)=>{if(args)count++;return data});
  f.el('regional-doc-lot').value='LOT-A';
  await f.context.runTorgiExtra('regional-doc',base,'large-pdf');
  assert.equal(count,0);assert.ok(f.el('regional-doc-extra-status').textContent.includes('Не завершено'));
 }
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    completed = subprocess.run([node, '-e', harness, str(source)], capture_output=True,
                               encoding='utf-8', timeout=20, **options)
    assert completed.returncode == 0, completed.stdout + completed.stderr
