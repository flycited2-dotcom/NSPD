"""Local PDF controls use the displayed catalog and keep explicit retry separate."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_local_pdf_ui_counts_scope_version_stale_retry_and_no_download_route():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the local PDF UI regression')
    web = Path(__file__).resolve().parents[1] / 'web'
    harness = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const A='22000084990000000030_1',B='24000026680000000644_1';
const elements=new Map(),posts=[],gets=[];let reconRefreshes=0,stale=false;
const parent={insertAdjacentElement(position,node){node.parentElement=this}};
function makeElement(tag='div',id=''){
 const element={tagName:tag.toUpperCase(),value:'',checked:false,disabled:false,innerHTML:'',textContent:'',style:{},listeners:{},parentElement:parent,
  classList:{remove(){},add(){}},addEventListener(type,fn){this.listeners[type]=fn},setAttribute(){},
  append(...children){for(const child of children)if(child&&typeof child==='object')child.parentElement=this},
  insertAdjacentElement(position,node){node.parentElement=this.parentElement}};
 Object.defineProperty(element,'id',{get(){return this._id},set(value){this._id=value;elements.set(value,this)}});
 if(id)element.id=id;return element;
}
function el(id){if(!elements.has(id))makeElement(/-lot$/.test(id)?'select':/-(cards|read|reprocess|ocr|collect)$/.test(id)?'button':'div',id);return elements.get(id)}
el('regional-doc-lot').value=B;
el('torgi-doc-retry').checked=true;el('regional-doc-retry').checked=true;
const sourceDate='2026-10-08T17:41:00Z',hash='a'.repeat(64);
function file(key,total,processed,lot){return {key,format:'pdf',unit:'pdf_page',state:'read',eligible:true,declared_size:100,
 total_pages:total,processed_pages:processed,received_at:sourceDate,sha256:hash,mentions:[],tables:[],egrn_tables:[],
 associations:[{lot_id:lot,scope:'lot',inactive:false}]}}
function card(lot,key){return {lot_id:lot,state:'received',lot_url:'https://torgi.gov.ru/lot/'+lot,received_at:sourceDate,card_status:'PUBLISHED',
 attachments:[{key,file_name:key+'.pdf',scope:'lot',url:'https://torgi.gov.ru/file/'+key,declared_size:100}]}}
const historical={id:'historical-v1',cards:[card(A,'old')],files:{old:file('old',68,40,A)},warning:'Источник не подтверждает права.'};
const regional={id:'regional-v1',cards:[card(A,'other'),card(B,'partial'),card(B,'limited')],
 files:{other:file('other',42,40,A),partial:file('partial',55,40,B),limited:file('limited',175,120,B)},warning:'Источник не подтверждает права.'};
regional.files.partial.local_pdf_attempt={state:'error',source_sha256:hash,error:'<img src=x onerror="alert(1)">'};
regional.files.limited.local_pdf_read_at='local-process-date';regional.files.limited.local_pdf_source_sha256=hash;
const originals=[JSON.stringify(historical),JSON.stringify(regional)];
function report(active){const catalog=active?regional:historical;return {result:structuredClone(catalog),search_stale:stale,
 cards_remaining:0,files_remaining:0,files_to_reprocess:0,visual_remaining:0,comparison_lots:[],
 selected_lot_id:active?B:null,selected_files_remaining:0,selected_files_to_reprocess:0,
 pdf_local_attempt:{state:'partial',processed:1,requested:2,errors:1,network_requests:0,error:'<img src=x> local failure'},
 pdf_local_remaining:active?2:1,pdf_local_retry_remaining:active?3:2,
 pdf_local_summary:{remaining:active?2:1,retry_remaining:active?3:2,unread_pages:active?72:28,limited_files:active?1:0},
 selected_pdf_local_remaining:1,selected_pdf_local_retry_remaining:2,
 selected_pdf_local_summary:{remaining:1,retry_remaining:2,unread_pages:70,limited_files:1}}}
async function api(url,args){
 if(args!==undefined){
  assert.ok(['/api/torgi/documents/continue','/api/torgi/active/documents/continue'].includes(url),'Only the local continuation route may be posted');
  const active=url.includes('/active/');
  assert.equal(args.id,active?regional.id:historical.id,'POST must use the displayed catalog version');
  if(active){assert.equal(el('regional-doc-lot').disabled,true);assert.equal(args.lot_id,B)}
  assert.equal(el((active?'regional-doc':'torgi-doc')+'-pdf-local-retry').disabled,true);
  posts.push({url,args:structuredClone(args)});return {job_id:'local-'+posts.length};
 }
 gets.push(url);if(url.startsWith('/api/jobs/'))return {state:'done'};
 if(url==='/api/torgi/documents')return report(false);
 if(url==='/api/torgi/active/documents?lot_id='+B)return report(true);
 throw Error('Unexpected route '+url);
}
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const context=vm.createContext({el,api,escapeHtml,running:false,reconStale:false,sameReconBounds:()=>true,
 currentTorgi:{lots:[]},tdate:value=>String(value??''),statusName:value=>value,operationState:value=>value,
 document:{createElement:makeElement,createTextNode:text=>({textContent:text}),querySelectorAll(selector){return selector==='[data-recon-save]'?[el('recon-save')]:[...elements.values()].filter(element=>element.tagName==='BUTTON')}},
 refreshRecon:async()=>{reconRefreshes++},setTimeout:callback=>{queueMicrotask(callback);return 1}});
const drain=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 for(const path of process.argv.slice(1))vm.runInContext(fs.readFileSync(path,'utf8'),context,{filename:path});await drain();
 assert.equal(el('torgi-doc-pdf-local-retry').checked,false);assert.equal(el('regional-doc-pdf-local-retry').checked,false);
 let text=el('regional-doc-pdf-local-status').textContent;
 for(const expected of ['Выбранный лот','дочитывания 1','ошибок 2','страниц в сохранённых PDF: 70','пределом 120: 1','завершена с ошибками','сетевых запросов 0'])assert.ok(text.includes(expected),expected);
 assert.ok(el('torgi-doc-pdf-local-status').textContent.includes('страниц в сохранённых PDF: 28'));
 const fileHtml=el('regional-doc-results').innerHTML;
 assert.ok(fileHtml.includes('Непрочитанных страниц: 55'));
 assert.ok(fileHtml.includes('Остаток за пределом 120 страниц требует отдельного чтения'));
 assert.ok(fileHtml.includes(sourceDate)&&fileHtml.includes(hash));
 assert.ok(fileHtml.includes('Геопривязки нет'));
 assert.ok(fileHtml.includes('Ошибка локального дочитывания: &lt;img'));
 assert.ok(!fileHtml.includes('<img src=x onerror='));
 assert.ok(fileHtml.includes('local-process-date; дата скачивания сохранена'));
 context.changedFile={...regional.files.partial,sha256:'b'.repeat(64)};
 assert.ok(!vm.runInContext('torgiPdfUnreadInfo(changedFile)',context).includes('Ошибка локального дочитывания'),'An error for another source hash must not be attached to this PDF');
 await el('torgi-doc-pdf-local').listeners.click();
 assert.deepEqual(posts[0],{url:'/api/torgi/documents/continue',args:{id:'historical-v1',retry_errors:false}},'The unrelated download/OCR retry checkbox must not enable local retries');
 await el('regional-doc-pdf-local').listeners.click();
 assert.deepEqual(posts[1],{url:'/api/torgi/active/documents/continue',args:{retry_errors:false,id:'regional-v1',lot_id:B}});
 el('regional-doc-pdf-local-retry').checked=true;el('regional-doc-pdf-local-retry').listeners.change();
 await el('regional-doc-pdf-local').listeners.click();assert.equal(posts[2].args.retry_errors,true);
 assert.equal(reconRefreshes,3);assert.equal(el('regional-doc-lot').disabled,false);
 assert.equal(el('regional-doc-pdf-local-retry').disabled,false);
 // The backend's new catalog version, not an earlier one, is used for the next local job.
 regional.id='regional-v2';await context.refreshRegionalDocs();
 await el('regional-doc-pdf-local').listeners.click();assert.equal(posts[3].args.id,'regional-v2');
 stale=true;await context.refreshTorgiDocs();await context.refreshRegionalDocs();
 assert.equal(el('torgi-doc-pdf-local').disabled,true);assert.equal(el('regional-doc-pdf-local').disabled,true);
 const postCount=posts.length;await el('torgi-doc-pdf-local').listeners.click();await el('regional-doc-pdf-local').listeners.click();
 assert.equal(posts.length,postCount,'Stale catalogs must not start a job even through a direct handler call');
 assert.ok(el('regional-doc-pdf-local-status').textContent.includes('Поиск изменился'));
 // Empty normal queue becomes actionable only after the separate explicit retry choice.
 stale=false;el('torgi-doc-pdf-local-retry').checked=false;
 context.emptyData={...report(false),pdf_local_summary:{remaining:0,retry_remaining:1,unread_pages:28,limited_files:0}};
 vm.runInContext("drawTorgiPdfLocalStatus('torgi-doc',emptyData,[])",context);assert.equal(el('torgi-doc-pdf-local').disabled,true);
 el('torgi-doc-pdf-local-retry').checked=true;el('torgi-doc-pdf-local-retry').listeners.change();assert.equal(el('torgi-doc-pdf-local').disabled,false);
 context.oldData={result:historical};vm.runInContext("drawTorgiPdfLocalStatus('torgi-doc',oldData,[])",context);
 assert.equal(el('torgi-doc-pdf-local').disabled,true,'Old reports lacking continuation fields must not create a queue');
 assert.ok(!el('torgi-doc-pdf-local-status').textContent.includes('undefined'));
 assert.equal(JSON.stringify(historical),originals[0]);regional.id='regional-v1';assert.equal(JSON.stringify(regional),originals[1]);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    completed = subprocess.run([node, '-e', harness, str(web / 'torgi_documents.js'),
                                str(web / 'torgi_active_documents.js')], capture_output=True,
                               text=True, encoding='utf-8', timeout=20, **options)
    assert completed.returncode == 0, completed.stdout + completed.stderr
