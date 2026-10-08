'use strict';
let regionalTorgi=null,regionalStale=false;
const regionalGroup=l=>l.type?.code==='ZK'?'land_provision':l.type?.code==='229FZ'?'debt_sale':'other';
const regionalGroupName=l=>regionalGroup(l)==='land_provision'?'Аренда и продажа земли (ЗК)':regionalGroup(l)==='debt_sale'?'Реализация имущества должников':'Другой / неустановленный тип';
function regionalRows(){
 if(!regionalTorgi)return;
 const filter=el('torgi-active-filter').value;
 const group=el('torgi-active-group').value;
 const rows=regionalTorgi.lots.filter(l=>(group==='all'||regionalGroup(l)===group)&&(filter==='all'||(filter==='unknown'&&(regionalStale||l.spatial_state==='unknown'))||(!regionalStale&&l.spatial_state===filter)));
 el('torgi-active-results').innerHTML=rows.length?'<div class="table-wrap"><table><thead><tr><th>Публикация</th><th>Статус и срок</th><th>Связь с областью</th></tr></thead><tbody>'+rows.map(l=>`<tr><td><a href="${escapeHtml(l.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(l.title||l.id)}</a><br>${escapeHtml(l.cadastral_numbers.join(', ')||'Кадастровый номер не указан')}<br><small>${escapeHtml(regionalGroupName(l))}<br>${escapeHtml(l.procedure.name)} · ${escapeHtml(l.transaction)}</small></td><td>${escapeHtml(statusName(l.status))}<br>Окончание: ${tdate(l.deadline)}<br><small>${l.deadline_state==='expired'?'Срок истёк на дату получения':l.deadline_state==='future'?'Срок ещё не истёк на дату получения':'Срок не установлен'}${l.stopped?' · приостановлено':''}${l.annulled?' · результаты аннулированы':''}</small></td><td>${regionalStale?'Область изменилась; требуется сопоставление':l.spatial_state==='inside'?'Полученная кадастровая геометрия пересекает область':l.spatial_state==='outside'?'Полученная кадастровая геометрия вне области':'Положение не установлено; нужна геометрия или схема'}${regionalBrowserEvidence(l)}<br><small>Схема лота и условия требуют сверки.</small></td></tr>`).join('')+'</tbody></table></div>':'<p>В этом фильтре нет записей. Неустановленные положения доступны в фильтре «Нужна геометрия».</p>';
}
function regionalBrowserEvidence(lot){
 const observations=(lot.geometry_lookups||[]).filter(o=>o.transport==='browser_response_import');
 return observations.length?'<br><small>'+observations.map(o=>`Сохранённый ответ НСПД: ${escapeHtml(o.cadastral_number)} · ${tdate(o.received_at)}; происхождение файла требует сверки.`).join('<br>')+'</small>':'';
}
async function refreshRegionalTorgi(){
 const d=await api('/api/torgi/active');regionalTorgi=d.result;
 let text=d.attempt?`Последний поиск: ${operationState(d.attempt.state)} · ${tdate(d.attempt.started_at)}${d.attempt.error?' · '+d.attempt.error+'; прежняя выборка сохранена':''}. `:'Региональная выборка ещё не получена. ';
 if(d.geometry_attempt)text+=`Геометрия: ${operationState(d.geometry_attempt.state)} · ${d.geometry_attempt.processed}/${d.geometry_attempt.requested}${d.geometry_attempt.error?' · '+d.geometry_attempt.error:''}. `;
 if(d.rematch_attempt)text+=`Сопоставление: ${operationState(d.rematch_attempt.state)}${d.rematch_attempt.error?' · '+d.rematch_attempt.error:''}. `;
 if(regionalTorgi){
  const r=regionalTorgi;regionalStale=r.survey_signature?r.survey_signature!==d.current_survey_signature:r.survey_id!==d.current_survey_id;
  const count=state=>r.lots.filter(l=>l.spatial_state===state).length;
  text+=`Получено ${r.lots.length}/${r.reported_total} записей на ${r.pages.length} страницах · ${tdate(r.created_at)}. ${regionalStale?'Область изменилась; пересопоставьте.':`Пересечений: ${count('inside')}; вне области по полученной геометрии: ${count('outside')}; положение неизвестно: ${count('unknown')}.`} ${r.warning}`;
  if(r.geometry_deferred_numbers?.length)text+=` Номеров другого кадастрового района отложено: ${r.geometry_deferred_numbers.length}; их геометрия не проверена.`;
  if(r.browser_imported_at)text+=` Сохранённых браузерных ответов: ${Object.values(r.geometries||{}).filter(o=>o.transport==='browser_response_import').length}; последняя загрузка ${tdate(r.browser_imported_at)}. Это отдельное получение данных, доступ API приложения этим не подтверждается.`;
  text+=` Аренда и продажа земли (ЗК): ${r.lots.filter(l=>regionalGroup(l)==='land_provision').length}; имущество должников: ${r.lots.filter(l=>regionalGroup(l)==='debt_sale').length}; другие типы: ${r.lots.filter(l=>regionalGroup(l)==='other').length}. Продажа имущества должника не подтверждает предоставление свободной земли.`;
  el('torgi-active-export').classList.remove('hidden');regionalRows();
 }
 el('torgi-active-status').textContent=text;
}
async function runRegionalTorgi(action=''){
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{
  const params=action?{id:regionalTorgi?.id,survey_id:currentSurvey?.id,retry_missing:el('torgi-retry').checked}:{};
  const job=await api('/api/torgi/active'+(action?'/'+action:''),params);let state;
  do{await new Promise(r=>setTimeout(r,1000));state=await api('/api/jobs/'+job.job_id);await refreshRegionalTorgi()}while(state.state==='running');
  if(state.state==='error')throw Error(state.error);
  await refreshRecon();
 }catch(e){el('torgi-active-status').textContent+=' Не выполнено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);if(reconStale||!sameReconBounds())document.querySelectorAll('[data-recon-save]').forEach(b=>b.disabled=true)}
}
el('torgi-active-run').addEventListener('click',()=>runRegionalTorgi());
el('torgi-active-locate').addEventListener('click',()=>runRegionalTorgi('geometry'));
el('torgi-active-rematch').addEventListener('click',()=>runRegionalTorgi('rematch'));
el('torgi-active-filter').addEventListener('change',regionalRows);
el('torgi-active-group').addEventListener('change',regionalRows);
refreshRegionalTorgi().catch(e=>el('torgi-active-status').textContent=e.message);
