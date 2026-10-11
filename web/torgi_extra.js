'use strict';
// Explicit bounded operations have independent queues and never run on page load.
const torgiExtraSections=[['torgi-doc','/api/torgi/documents'],['regional-doc','/api/torgi/active/documents']];
const torgiExtraVersions=new Map(),torgiExtraPending=new Map(),torgiExtraData=new Map();
const torgiExtraSelected=prefix=>prefix==='regional-doc'?el('regional-doc-lot').value:'';
function restoreTorgiExtraControls(){
 for(const [prefix] of torgiExtraSections){
  const button=el(prefix+'-large-pdf');if(!button)continue;
  const saved=torgiExtraData.get(prefix);
  const data=saved?.selected===torgiExtraSelected(prefix)?saved.data:null;
  const summary=data?.large_pdf_summary||{};
  button.disabled=running||Boolean(torgiExtraPending.get(prefix))||!data||data.search_stale||!data.result?.id
   ||!(el(prefix+'-extra-retry').checked?summary.retry_remaining:summary.remaining);
 }
}
async function refreshTorgiExtra(prefix,base){
 const selected=torgiExtraSelected(prefix),version=(torgiExtraVersions.get(prefix)||0)+1;
 torgiExtraVersions.set(prefix,version);
 torgiExtraPending.set(prefix,(torgiExtraPending.get(prefix)||0)+1);
 restoreTorgiExtraControls();
 try{
 const data=await api(base+(selected?'?lot_id='+encodeURIComponent(selected):''));
 if(version!==torgiExtraVersions.get(prefix)||selected!==torgiExtraSelected(prefix)
   ||(prefix==='regional-doc'&&(data.selected_lot_id||'')!==selected))return null;
 torgiExtraData.set(prefix,{selected,data});
 const summary=data.large_pdf_summary||{};
 const status=el(prefix+'-extra-status');
 status.textContent=`Крупные PDF: прочитано ${summary.read_files??0}; осталось ${summary.remaining??0}; для явного повтора ${summary.retry_remaining??0}. До 32 МБ и 120 страниц; сканы читаются отдельно кнопкой OCR. Это не подтверждает геопривязку или права.`;
 if(data.large_pdf_attempt)status.textContent+=` Последняя проверка: ${operationState(data.large_pdf_attempt.state)}; обработано ${data.large_pdf_attempt.processed}/${data.large_pdf_attempt.requested}, ошибок ${data.large_pdf_attempt.errors}.`;
 if(data.large_pdf_attempt?.error)status.textContent+=' '+docErrorText(data.large_pdf_attempt.error);
 return data;
 }catch(error){
  if(version!==torgiExtraVersions.get(prefix)||selected!==torgiExtraSelected(prefix))return null;
  torgiExtraData.delete(prefix);
  throw error;
 }finally{
  torgiExtraPending.set(prefix,torgiExtraPending.get(prefix)-1);
  restoreTorgiExtraControls();
 }
}
async function runTorgiExtra(prefix,base,action){
 if(running)return;
 const selected=torgiExtraSelected(prefix),retry=el(prefix+'-extra-retry').checked;
 const selector=prefix==='regional-doc'?el('regional-doc-lot'):null;
 const selectorDisabled=selector?.disabled;
 const buttons=Array.from(document.querySelectorAll('button'),button=>({button,disabled:button.disabled}));
 running=true;for(const {button} of buttons)button.disabled=true;
 if(selector)selector.disabled=true;
 let failure=null;
 try{
  const data=await refreshTorgiExtra(prefix,base);
  if(!data||selected!==torgiExtraSelected(prefix))throw Error('Выбранный лот или запрос изменился; операция отменена.');
  if(data.search_stale||!data.result?.id)throw Error('Поиск изменился; обновите карточки.');
  const remaining=retry?data.large_pdf_summary?.retry_remaining:data.large_pdf_summary?.remaining;
  if(!remaining)throw Error('В выбранной очереди нет крупных PDF для чтения.');
  const params={id:data.result.id,retry_errors:retry};
  if(selected)params.lot_id=selected;
  const job=await api(base+'/'+action,params);let state;
  do{await new Promise(resolve=>setTimeout(resolve,1000));state=await api('/api/jobs/'+job.job_id);}while(state.state==='running');
  if(state.state==='error')throw Error(state.error);
  if(prefix==='regional-doc')await refreshRegionalDocs();else await refreshTorgiDocs();
  await refreshRecon();
 }catch(error){failure=error;}
 finally{
  running=false;for(const {button,disabled} of buttons)button.disabled=disabled;
  if(selector)selector.disabled=selectorDisabled;
  if(typeof restoreTorgiPdfLocalControls==='function')restoreTorgiPdfLocalControls();
  if(reconStale||!sameReconBounds())document.querySelectorAll('[data-recon-save]').forEach(b=>b.disabled=true);
  try{await refreshTorgiExtra(prefix,base);}catch(error){if(!failure)failure=error;}
  if(failure)el(prefix+'-extra-status').textContent+=' Не завершено: '+failure.message;
  restoreTorgiExtraControls();
 }
}
for(const [prefix,base] of torgiExtraSections){
 const section=document.createElement('div');section.className='note';
 const button=document.createElement('button');button.id=prefix+'-large-pdf';button.type='button';button.className='button secondary';button.textContent='Прочитать 1 крупный PDF';button.disabled=true;
 button.addEventListener('click',()=>runTorgiExtra(prefix,base,'large-pdf').catch(error=>el(prefix+'-extra-status').textContent=error.message));
 const label=document.createElement('label'),retry=document.createElement('input');retry.id=prefix+'-extra-retry';retry.type='checkbox';
 retry.addEventListener('change',()=>refreshTorgiExtra(prefix,base).catch(error=>el(prefix+'-extra-status').textContent=error.message));
 label.append(retry,document.createTextNode(' Повторить ошибки дополнительных материалов'));
 const status=document.createElement('p');status.id=prefix+'-extra-status';status.setAttribute('role','status');
 section.append(button,document.createTextNode(' '),label,status);el(prefix+'-status').after(section);
 if(prefix==='regional-doc')el('regional-doc-lot').addEventListener('change',()=>refreshTorgiExtra(prefix,base).catch(error=>status.textContent=error.message));
 refreshTorgiExtra(prefix,base).catch(error=>status.textContent=error.message);
}
