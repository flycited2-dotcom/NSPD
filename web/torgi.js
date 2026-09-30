'use strict';
const tmap=L.map('torgi-map').setView([44.99335,34.20543],15);
const tdraw=L.featureGroup().addTo(tmap);
let currentTorgi=null, torgiStale=false;
const tdate=x=>{if(!x)return 'не указано';const d=new Date(x);return Number.isNaN(d.getTime())?escapeHtml(x):d.toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})+' МСК'};
const operationState=x=>({running:'выполняется',done:'завершено',partial:'частично',error:'ошибка',interrupted:'прервано',received:'получена',not_found:'совпадение по номеру не найдено',not_returned:'геометрия не получена',rejected:'геометрия отклонена'}[x]||x);
const statusName=x=>({PUBLISHED:'Опубликован',APPLICATIONS_SUBMISSION:'Приём заявок',FAILED:'Не состоялся',SUCCEED:'Состоялся',CANCELED:'Отменён'}[x]||x||'не указан');
function torgiTable(){
 if(!currentTorgi)return;
 const lots=currentTorgi.lots.filter(l=>!el('torgi-area-only').checked||(!torgiStale&&l.in_survey));
 el('torgi-results').innerHTML=lots.length?'<div class="table-wrap"><table><thead><tr><th>Лот / процедура</th><th>Статус и сроки</th><th>Участок</th><th>Сопоставление</th></tr></thead><tbody>'+lots.map(l=>`<tr><td><a href="${escapeHtml(l.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(l.id)}</a><br>${escapeHtml(l.procedure.name)}<br><small>${escapeHtml(l.type.name)}</small><details><summary>Описание лота</summary><p>${escapeHtml(l.title)}</p><p>${escapeHtml(l.description)}</p></details></td><td>${escapeHtml(statusName(l.status))} <small>(${escapeHtml(l.status)})</small><br>Публикация: ${tdate(l.published_at)}<br>Окончание приёма: ${tdate(l.deadline)}<br><small>${l.deadline_state==='expired'?'Срок истёк на дату загрузки':l.deadline_state==='future'?'Срок ещё не истёк на дату загрузки':'Срок не установлен'}${l.stopped?' · приостановлен':''}${l.annulled?' · результаты аннулированы':''}</small></td><td>${escapeHtml(l.cadastral_numbers.join(', ')||'Номер не указан в характеристиках')}<br><small>${escapeHtml(l.category.name)}</small><br>${escapeHtml(l.transaction==='rent'?'Аренда':l.transaction==='sale'?'Продажа':l.transaction)}${l.price_min!=null?'<br>Начальная цена: '+escapeHtml(l.price_min)+' '+escapeHtml(l.currency||''):''}</td><td>${torgiStale?'Обследование изменилось; пересопоставьте':l.in_survey?'Кадастровая геометрия пересекает область':'Пересечение с областью не установлено'}${!torgiStale&&l.spatial_matches?.length?'<br>Пересечений с промежутками: '+l.spatial_matches.reduce((n,m)=>n+m.gap_intersections.length,0):''}<details><summary>Проверка геометрии</summary>${(l.geometry_lookups||[]).map(g=>`<p>${escapeHtml(g.cadastral_number)} · ${escapeHtml(operationState(g.state)||'из обследования')} · ${escapeHtml(g.received_at||'')}<br>${escapeHtml(g.error||'')}</p>`).join('')||'<p>Геометрия не получена.</p>'}<p>Это геометрия НСПД по номеру; схема и условия лота требуют сверки.</p></details></td></tr>`).join('')+'</tbody></table></div>':'<p>В отображаемой выборке нет записей. Это не доказательство отсутствия торгов или заявлений в области.</p>';
}
async function refreshTorgi(){
 const d=await api('/api/torgi');currentTorgi=d.result;
 const a=d.attempt,g=d.geometry_attempt;
 el('torgi-status').textContent=a?`Последний поиск: ${operationState(a.state)} · ${a.started_at} · страниц ${a.pages_received}.${a.error?' '+a.error+' Предыдущий результат сохранён.':''}`:'Поиск ещё не выполнялся.';
 if(g)el('torgi-status').textContent+=`\nГеометрия: ${operationState(g.state)} · проверено ${g.processed} из ${g.requested}.${g.error?' '+g.error:''}`;
 if(!currentTorgi)return;
 const r=currentTorgi;torgiStale=r.survey_id!==d.current_survey_id;
 el('torgi-status').textContent+=`\nПоказан поиск «${r.query}» от ${r.created_at}: ${r.lots.length} записей, ${r.pages.length} страниц; ${r.history?'все статусы, включая историю':'только опубликованные и приём заявок'}. Совпадений с областью: ${torgiStale?'требует пересопоставления':r.lots.filter(l=>l.in_survey).length}.\n${r.warning}`;
 const geo=Object.values(r.geometries||{}).filter(g=>g.lookup); if(geo.length)el('torgi-status').textContent+=` Проверка номеров: геометрия получена ${geo.filter(g=>g.state==='received').length}; HTTP 404 ${geo.filter(g=>g.state==='not_returned').length}; отклонена ${geo.filter(g=>g.state==='rejected').length}; не проверено ${r.geometry_unchecked_numbers?.length??0}.`;
 el('torgi-export').classList.remove('hidden');torgiTable();tdraw.clearLayers();
 if(r.survey_bounds){const [w,s,e,n]=r.survey_bounds;L.rectangle([[s,w],[n,e]],{color:'#444',fill:false,dashArray:'5 5'}).addTo(tdraw);tmap.fitBounds([[s,w],[n,e]],{padding:[12,12]});}
 if(!torgiStale&&currentSurvey?.id===r.survey_id){
  L.geoJSON(currentSurvey.layers.parcels.geojson,{style:{color:'#87918a',weight:1,fillOpacity:.15}}).addTo(tdraw);
  L.geoJSON(currentSurvey.gaps,{style:{color:'#c7831d',weight:1,fillOpacity:.15}}).addTo(tdraw);
 }
 if(!torgiStale)for(const lot of r.lots)for(const m of lot.spatial_matches||[])L.geoJSON(m.feature,{style:{color:'#8e3f92',weight:3,fillOpacity:.2},onEachFeature:(f,l)=>l.bindPopup(`${escapeHtml(m.cadastral_number)}<br>${escapeHtml(lot.procedure.name)} · ${escapeHtml(statusName(lot.status))}<br><a href="${escapeHtml(lot.url)}" target="_blank" rel="noopener noreferrer">Официальный лот</a>`)}).addTo(tdraw);
 if(typeof refreshTorgiDocs==='function')await refreshTorgiDocs();
}
async function runTorgi(geometry=false){
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 el('torgi-status').textContent=geometry?'Проверка геометрии по кадастровым номерам в НСПД…':'Загрузка страниц ГИС Торги…';
 try{const job=await api(geometry?'/api/torgi/geometry':'/api/torgi',geometry?{id:currentTorgi?.id,retry_missing:el('torgi-retry').checked}:{query:el('torgi-query').value,history:el('torgi-history').checked});let s;
 do{await new Promise(r=>setTimeout(r,1000));s=await api('/api/jobs/'+job.job_id);await refreshTorgi()}while(s.state==='running');
 if(s.state==='error')throw Error(s.error);
 }catch(e){el('torgi-status').textContent+='\nНе выполнено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false)}
}
el('torgi-run').addEventListener('click',()=>runTorgi());el('torgi-locate').addEventListener('click',()=>runTorgi(true));
el('torgi-area-only').addEventListener('change',torgiTable);
refreshTorgi().catch(e=>el('torgi-status').textContent=e.message);
