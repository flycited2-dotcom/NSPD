'use strict';
let regionalDocs=null;
let regionalDocLots=[];
let regionalRefresh=0;
function regionalFileInfo(file,card){
 if(!file)return '<p>Метаданные файла ещё не получены.</p>';
 return `<p>${escapeHtml(torgiFileState(file))} · ${file.declared_size} байт<br>Скачан: ${tdate(file.received_at)}${file.sha256?'<br>SHA-256: '+escapeHtml(file.sha256):''}</p>${file.metadata_conflict?'<p>Метаданные одного файла различаются; чтение остановлено.</p>':''}${file.error?'<p>'+escapeHtml(docErrorText(file.error))+'</p>':''}${file.state==='read'?torgiFileEvidence(file,card,regionalDocLots).replaceAll('/api/torgi/documents/image','/api/torgi/active/documents/image'):''}`;
}
async function refreshRegionalDocs(){
 const request=++regionalRefresh;
 const chosen=el('regional-doc-lot').value;
 const d=await api('/api/torgi/active/documents'+(chosen?'?lot_id='+encodeURIComponent(chosen):''));
 if(request!==regionalRefresh||chosen!==el('regional-doc-lot').value)return;
 regionalDocs=d.result;
 regionalDocLots=d.comparison_lots||[];
 const r=regionalDocs,files=Object.values(r?.files||{});
 let text=`Карточек ЗК осталось: ${d.cards_remaining}; файлов в очереди: ${d.files_remaining}; сканов для OCR: ${d.visual_remaining}. `;
 if(r)text+=`Получено карточек: ${r.cards.filter(c=>c.state==='received').length}; прочитано файлов: ${files.filter(f=>f.state==='read').length}; ошибки/отклонения файлов: ${files.filter(f=>['error','rejected'].includes(f.state)).length}. ${r.warning} ${d.search_stale?'Поиск изменился; нужны свежие карточки. ':''}`;
 if(d.selected_lot_id)text+=`Выбран лот ${d.selected_lot_id}: в очереди ${d.selected_files_remaining}; для локального перечтения ${d.selected_files_to_reprocess}. `;
 else text+=`Для локального перечтения: ${d.files_to_reprocess}. `;
 for(const [label,a] of [['Карточки',d.attempt],['Файлы',d.reading_attempt],['Перечтение',d.reprocess_attempt],['OCR',d.visual_attempt]])if(a)text+=`${label}: ${operationState(a.state)} · ${a.processed}/${a.requested}${a.lot_id?' · лот '+a.lot_id:''}${a.error?' · '+docErrorText(a.error):''}. `;
 el('regional-doc-status').textContent=text;
 if(!r)return;
 el('regional-doc-lot').innerHTML='<option value="">Все полученные лоты ЗК</option>'+r.cards.filter(c=>c.state==='received').map(c=>`<option value="${escapeHtml(c.lot_id)}">${escapeHtml(c.lot_id)}</option>`).join('');
 el('regional-doc-lot').value=chosen;
 el('regional-doc-export').classList.remove('hidden');
 el('regional-doc-results').innerHTML=r.cards.filter(c=>!chosen||c.lot_id===chosen).map(card=>`<details><summary>Лот ${escapeHtml(card.lot_id)} · ${card.state==='received'?'вложений: '+card.attachments.length:'карточка не получена'}</summary>${card.state==='received'?`<p><a href="${escapeHtml(card.lot_url)}" target="_blank" rel="noopener noreferrer">Официальная карточка</a> · ${tdate(card.received_at)} · ${escapeHtml(statusName(card.card_status))}</p>${card.attachments.map(a=>`<details><summary>${escapeHtml(a.file_name)} · ${a.scope==='lot'?'вложение этого лота':'общий файл извещения'}</summary><p><a href="${escapeHtml(a.url)}" target="_blank" rel="noopener noreferrer">Официальный файл</a> · ${escapeHtml(a.type_name||'тип не указан')}${a.inactive?' · неактивное вложение':''}</p>${regionalFileInfo(r.files[a.key],card)}</details>`).join('')}`:'<p>'+escapeHtml(docErrorText(card.error))+'</p>'}</details>`).join('');
}
async function runRegionalDocs(action='collect'){
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 el('regional-doc-lot').disabled=true;
 if(action==='collect'){el('regional-doc-lot').value='';++regionalRefresh}
 try{
  const args={retry_errors:el('regional-doc-retry').checked};
  if(action==='collect')args.search_id=(await api('/api/torgi/active')).result?.id;else args.id=regionalDocs?.id;
  if(['read','reprocess'].includes(action)&&el('regional-doc-lot').value)args.lot_id=el('regional-doc-lot').value;
  const job=await api('/api/torgi/active/documents/'+action,args);let state;
  do{await new Promise(r=>setTimeout(r,1000));state=await api('/api/jobs/'+job.job_id);await refreshRegionalDocs()}while(state.state==='running');
  if(state.state==='error')throw Error(state.error);
  await refreshRecon();
 }catch(e){el('regional-doc-status').textContent+=' Не завершено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);el('regional-doc-lot').disabled=false}
}
el('regional-doc-collect').addEventListener('click',()=>runRegionalDocs());
el('regional-doc-read').addEventListener('click',()=>runRegionalDocs('read'));
el('regional-doc-reprocess').addEventListener('click',()=>runRegionalDocs('reprocess'));
el('regional-doc-lot').addEventListener('change',()=>refreshRegionalDocs().catch(e=>el('regional-doc-status').textContent=e.message));
el('regional-doc-ocr').addEventListener('click',()=>runRegionalDocs('ocr'));
refreshRegionalDocs().catch(e=>el('regional-doc-status').textContent=e.message);
