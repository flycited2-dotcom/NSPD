'use strict';
let districtReport=null;
const districtLabel={pending:'не прочитан',read:'текст прочитан',error:'ошибка',received:'получен',unchecked:'нужна новая проверка',
 act_text:'текст акта',draft_text:'текст проекта',planning_document:'документация',unknown:'не установлено',
 approve_text:'утверждение по тексту',cancel_reference:'отмена указанного акта по тексту',amend_reference:'изменение указанного акта по тексту',multiple_text_actions:'несколько действий по тексту'};
const districtText=x=>escapeHtml(districtLabel[x]||x||'не установлено');
async function refreshDistrict(){
 const {result:r,limitation}=await api('/api/planning');districtReport=r;
 el('district-read').disabled=running||!r;
 el('district-status').textContent=r?`Перечни проверены: ${r.checked_at}. Документов: ${r.items.length}; ошибок перечней: ${r.sources.filter(s=>s.state==='error').length}. ${limitation}`:'Перечни района ещё не проверялись.';
 if(!r)return;
 const remaining=r.items.filter(x=>x.currently_listed&&x.read_revision!==r.catalog_revision).length;
 el('district-status').textContent+=` Осталось проверить PDF: ${remaining}.`;
 el('district-results').innerHTML=r.items.map(x=>{
  const act=x.act_identity;const conflicts=x.listing_conflicts||[];
  const dates=[...new Set(x.listing_references.map(a=>a.publication_date).filter(Boolean))];
  const refs=(x.reference_matches||[]).map(a=>`<p>${districtText(a.relation)}: ${escapeHtml(a.date)} № ${escapeHtml(a.number)}. ${a.observed_documents.length?'Есть документ с совпавшими реквизитами.':'Сам указанный акт не прочитан в этом перечне.'} Объём действия требует сверки.</p>`).join('');
  return `<details><summary>${escapeHtml(x.title)} · ${districtText(x.current_read_state)}</summary><p><a href="${escapeHtml(x.url)}" target="_blank" rel="noopener noreferrer">Официальный PDF</a></p><p>${districtText(x.document_role)}; ${districtText(x.action)}. Реквизиты в PDF: ${act?escapeHtml(act.date)+' № '+escapeHtml(act.number):'не извлечены'}.</p><p>Дата публикации по перечню: ${escapeHtml(dates.join(', ')||'не указана')}. Получение PDF: ${escapeHtml(x.received_at||'не получен')}. Последняя попытка: ${escapeHtml(x.read_attempt?.checked_at||'не выполнялась')}.</p>${x.currently_listed?'':'<p>В текущем обходе не найден; это не подтверждает отмену.</p>'}${conflicts.length?`<p class="badge yellow">Расхождение заголовка и содержимого: ${conflicts.length}</p><pre>${escapeHtml(JSON.stringify(conflicts,null,2))}</pre>`:''}${refs}<p>Прочитано страниц: ${x.processed_pages??0}/${x.total_pages??'неизвестно'}; без текста: ${escapeHtml((x.image_or_sparse_pages||[]).join(', ')||'не указаны')}. SHA-256: ${escapeHtml(x.sha256||'нет')}.</p>${x.same_bytes_municipal?.length?'<p>PDF совпадает по SHA-256 с сохранённым муниципальным документом. Совпадение файла не устанавливает права или действие акта.</p>':''}${x.read_attempt?.error?`<p>Ошибка последней проверки: ${escapeHtml(x.read_attempt.error)}. Прежние сведения сохранены со своей датой.</p>`:''}</details>`;
 }).join('');
}
async function runDistrict(action){
 if(running)return;
 running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 el('district-status').textContent=action==='catalog'?'Проверяются ограниченные перечни района и страницы сессий ПЗЗ.':'Проверяются следующие три PDF. Прежние результаты сохраняются.';
 try{
  const payload=action==='read'?{id:districtReport?.id,retry:el('district-retry').checked}:{};
  const job=await api('/api/planning/'+action,payload);let state;
  do{await new Promise(r=>setTimeout(r,1000));state=await api('/api/jobs/'+job.job_id)}while(state.state==='running');
  await refreshDistrict();if(state.state==='error')throw Error(state.error);
 }catch(e){el('district-status').textContent+=' Не выполнено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);el('district-read').disabled=!districtReport}
}
el('district-catalog').addEventListener('click',()=>runDistrict('catalog'));
el('district-read').addEventListener('click',()=>runDistrict('read'));
refreshDistrict().catch(e=>el('district-status').textContent=e.message);
