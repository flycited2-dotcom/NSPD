'use strict';
let regulationReport=null;
async function refreshRegulations(){
 const data=await api('/api/planning/regulations');regulationReport=data;
 const c=data.counts;
 el('regulations-status').textContent=data.result?`Текст ПЗЗ: ${c.processed_pages}/${c.total_pages} страниц; документов полностью ${c.complete_documents}/${c.documents}; страниц со слабым текстом ${c.sparse_pages}; ошибок ${c.errors}. ${data.stale?'Каталог изменился; индекс нужно обновить.':''}`:'Полный текст сохранённых ПЗЗ ещё не проиндексирован.';
 el('regulations-read').disabled=running||!data.catalog_id||Boolean(data.result&&!data.stale&&!c.remaining_pages);
 const args=new URLSearchParams({zone:el('regulations-zone').value.trim(),q:el('regulations-query').value.trim()});
 if(data.result)args.set('version',data.result.id);
 el('regulations-search').href='/api/planning/regulations/report?'+args;
 return data;
}
async function readRegulations(){
 if(running)return;
 running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{
  // One click resumes bounded local jobs. Stop on any failure; no automatic retries.
  for(let batch=0;batch<10;batch++){
   const r=await refreshRegulations();
   const job=await api('/api/planning/regulations/read',{id:r.result?.id||null,planning_id:r.catalog_id,retry:el('regulations-retry').checked});let state;
   do{await new Promise(resolve=>setTimeout(resolve,1000));state=await api('/api/jobs/'+job.job_id);await refreshRegulations()}while(state.state==='running');
   if(state.state==='error')throw Error(state.error);
   const fresh=await refreshRegulations();
   if(state.result.run_errors||fresh.counts.errors)throw Error('Есть ошибки чтения. Сохранённые страницы доступны; для повторной попытки отметьте «Повторить ошибки» и запустите чтение.');
   if(!fresh.counts.remaining_pages||!state.result.new_pages)break;
  }
  if(typeof refreshRecon==='function')await refreshRecon();
 }catch(e){el('regulations-status').textContent+=' Не завершено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);const disabled=!regulationReport?.catalog_id||Boolean(regulationReport?.result&&!regulationReport.stale&&!regulationReport.counts.remaining_pages);el('regulations-read').disabled=disabled}
}
el('regulations-read').addEventListener('click',readRegulations);
for(const id of ['regulations-zone','regulations-query'])el(id).addEventListener('input',()=>{
 const args=new URLSearchParams({zone:el('regulations-zone').value.trim(),q:el('regulations-query').value.trim()});
 if(regulationReport?.result)args.set('version',regulationReport.result.id);
 el('regulations-search').href='/api/planning/regulations/report?'+args;
});
refreshRegulations().catch(e=>el('regulations-status').textContent=e.message);
