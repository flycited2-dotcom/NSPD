'use strict';
// Explicit bounded operations have independent queues and never run on page load.
const torgiExtraSections=[['torgi-doc','/api/torgi/documents'],['regional-doc','/api/torgi/active/documents']];
const torgiExtraActions={
 'large-pdf':{summary:'large_pdf_summary',label:'Прочитать 1 крупный PDF',empty:'крупных PDF для чтения'},
 archives:{summary:'archive_summary',label:'Прочитать 1 ZIP',empty:'ZIP для чтения'},
 frames:{summary:'image_frames_summary',label:'Открыть дополнительные кадры 1 фото',empty:'дополнительных кадров для чтения'}
};
const torgiExtraVersions=new Map(),torgiExtraPending=new Map(),torgiExtraData=new Map();
const torgiExtraSelected=prefix=>prefix==='regional-doc'?el('regional-doc-lot').value:'';
function torgiExtraRemaining(data,action,retry){
 const summary=data?.[torgiExtraActions[action]?.summary]||{};
 const value=retry?(summary.retry_remaining??summary.retry_pending):(summary.remaining??summary.pending);
 return Number.isInteger(value)&&value>0?value:0;
}
function torgiExtraFrameSelection(data,retry){
 const queue=retry?data?.image_frames_retry_queue:data?.image_frames_queue;
 const first=Array.isArray(queue)?queue[0]:null;
 if(!first||typeof first.file_key!=='string'||!/^[0-9a-f]{24}$/.test(first.file_key)
  ||!Array.isArray(first.frames)||first.frames.length<1||first.frames.length>15
  ||first.frames.some(index=>!Number.isInteger(index)||index<1||index>15)
  ||new Set(first.frames).size!==first.frames.length)return null;
 return {file_key:first.file_key,frames:[...first.frames]};
}
function restoreTorgiExtraControls(){
 for(const [prefix] of torgiExtraSections){
  const saved=torgiExtraData.get(prefix);
  const data=saved?.selected===torgiExtraSelected(prefix)?saved.data:null;
  const retry=el(prefix+'-extra-retry')?.checked||false;
  for(const action of Object.keys(torgiExtraActions)){
   const button=el(prefix+'-'+action);if(!button)continue;
   button.disabled=running||Boolean(torgiExtraPending.get(prefix))||!data||data.search_stale||!data.result?.id
    ||!torgiExtraRemaining(data,action,retry)||(action==='frames'&&!torgiExtraFrameSelection(data,retry));
  }
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
 const archives=data.archive_summary||{},frames=data.image_frames_summary||{};
 const status=el(prefix+'-extra-status');
 status.textContent=`Крупные PDF: прочитано ${summary.read_files??0}; осталось ${torgiExtraRemaining(data,'large-pdf',false)}; для явного повтора ${torgiExtraRemaining(data,'large-pdf',true)}. До 32 МБ и 120 страниц; сканы читаются отдельно кнопкой OCR. ZIP: в каталоге ${archives.archive_count??0}; осталось ${torgiExtraRemaining(data,'archives',false)}; для явного повтора ${torgiExtraRemaining(data,'archives',true)}. Дополнительные кадры MPO: получено ${frames.received??0}; осталось ${torgiExtraRemaining(data,'frames',false)}; для явного повтора ${torgiExtraRemaining(data,'frames',true)}. OCR дополнительных кадров и сканов ZIP не выполнено; они не добавляются в геометрию. Это не подтверждает геопривязку или права.`;
 for(const [label,attempt] of [['Крупный PDF',data.large_pdf_attempt],['ZIP',archives.attempt||data.archive_attempt],['Кадры MPO',data.image_frames_attempt]]){
  if(!attempt)continue;
  status.textContent+=` ${label}: ${operationState(attempt.state)}; обработано ${attempt.processed??0}/${attempt.requested??0}, ошибок ${attempt.errors??0}.`;
  if(attempt.error)status.textContent+=' '+docErrorText(attempt.error);
 }
 for(const action of ['archives','frames'])for(const view of ['report','export']){
  const link=el(prefix+'-'+action+'-'+view);
  if(link)link.href=base+'/'+action+'/'+view+(selected?'?lot_id='+encodeURIComponent(selected):'');
 }
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
  if(!Object.hasOwn(torgiExtraActions,action))throw Error('Неизвестная операция дополнительных материалов.');
  const data=await refreshTorgiExtra(prefix,base);
  if(!data||selected!==torgiExtraSelected(prefix))throw Error('Выбранный лот или запрос изменился; операция отменена.');
  if(data.search_stale||!data.result?.id)throw Error('Поиск изменился; обновите карточки.');
  if(!torgiExtraRemaining(data,action,retry))throw Error('В выбранной очереди нет '+torgiExtraActions[action].empty+'.');
  const params={id:data.result.id,retry_errors:retry};
  if(selected)params.lot_id=selected;
  if(action==='frames'){
   const selectedFrames=torgiExtraFrameSelection(data,retry);
   if(!selectedFrames)throw Error('Очередь дополнительных кадров изменилась; обновите страницу.');
   Object.assign(params,selectedFrames);
  }
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
 for(const [action,details] of Object.entries(torgiExtraActions)){
  const button=document.createElement('button');button.id=prefix+'-'+action;button.type='button';button.className='button secondary';button.textContent=details.label;button.disabled=true;
  button.addEventListener('click',()=>runTorgiExtra(prefix,base,action).catch(error=>el(prefix+'-extra-status').textContent=error.message));
  section.append(button,document.createTextNode(' '));
 }
 const label=document.createElement('label'),retry=document.createElement('input');retry.id=prefix+'-extra-retry';retry.type='checkbox';
 retry.addEventListener('change',()=>refreshTorgiExtra(prefix,base).catch(error=>el(prefix+'-extra-status').textContent=error.message));
 label.append(retry,document.createTextNode(' Повторить ошибки дополнительных материалов'));
 const status=document.createElement('p');status.id=prefix+'-extra-status';status.setAttribute('role','status');
 section.append(label,status);
 for(const action of ['archives','frames'])for(const [view,title] of [['report','Отчёт'],['export','↓ Источники']]){
  const link=document.createElement('a');link.id=prefix+'-'+action+'-'+view;link.href=base+'/'+action+'/'+view;link.className='button secondary';
  link.textContent=title+' · '+(action==='archives'?'ZIP':'кадры фото');
  if(view==='report'){link.target='_blank';link.rel='noopener noreferrer';}
  section.append(link,document.createTextNode(' '));
 }
 el(prefix+'-status').after(section);
 if(prefix==='regional-doc')el('regional-doc-lot').addEventListener('change',()=>refreshTorgiExtra(prefix,base).catch(error=>status.textContent=error.message));
 refreshTorgiExtra(prefix,base).catch(error=>status.textContent=error.message);
}
