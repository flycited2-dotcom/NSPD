'use strict';
let pzzMapReport=null;
const pzzCanRender=()=>pzzMapReport?.result&&!pzzMapReport.stale&&((pzzMapReport.image_summary?.remaining||0)>0||(el('pzz-maps-retry').checked&&(pzzMapReport.image_summary?.errors||0)>0));
async function refreshPzzMaps(){
 const data=await api('/api/planning/maps');pzzMapReport=data;
 const r=data.result;
 el('pzz-maps-run').disabled=running||!data.current_planning_id;
 el('pzz-maps-render').disabled=running||!pzzCanRender();
 el('pzz-maps-status').textContent=data.warning;
 if(data.attempt)el('pzz-maps-status').textContent+=` Последняя попытка: ${data.attempt.state} · ${data.attempt.started_at}${data.attempt.error?' · '+data.attempt.error:''}.`;
 if(!r){el('pzz-maps-results').innerHTML='<p>Карты ещё не разобраны.</p>';return}
 if(data.stale){el('pzz-maps-results').innerHTML='<p>Документы или область изменились. Повторите локальную проверку; прежние карты скрыты.</p>';return}
 el('pzz-maps-status').textContent+=` Документов: ${r.documents.length}; картографических листов: ${r.documents.reduce((n,d)=>n+d.map_pages.length,0)}. Анализ от ${r.created_at}; новых запросов к сайтам: 0.`;
 const images=data.image_summary;el('pzz-maps-status').textContent+=` Изображений готово: ${images.rendered}/${images.total}; в очереди: ${images.remaining}; ошибок: ${images.errors}.`;
 el('pzz-maps-results').innerHTML=r.documents.map(d=>{
  const act=d.act_identity;const heading=act?`${act.date} № ${act.number}`:d.title;
  return `<details><summary>${escapeHtml(heading)} · листов: ${d.map_pages.length}</summary><p><a href="${escapeHtml(d.url)}" target="_blank" rel="noopener noreferrer">Официальный PDF</a> · получен ${escapeHtml(d.received_at)}</p>${d.error?`<p>${escapeHtml(d.error)}</p>`:''}${!d.map_pages.length?'<p>Поддержанные пары «старая / новая редакция» не найдены. Это не означает отсутствие карт или ограничений.</p>':''}${d.map_pages.map(p=>{
   const numbers=p.number_mentions.length?p.number_mentions.join(', '):'в тексте листа не найдены';
   const matches=p.parcel_number_matches||[];
   return `<p><b>PDF-страница ${p.page}</b> · коды зон: ${escapeHtml(p.zone_hints.join(', ')||'не найдены')}<br>Кадастровые упоминания: ${escapeHtml(numbers)}<br>Совпавших номеров сохранённых участков: ${matches.length}; с площадью внутри области: ${matches.filter(m=>m.parcel_intersects_survey).length}. Это не границы зон.</p>${p.image?`<button class="button secondary" data-pzz-doc="${escapeHtml(d.id)}" data-pzz-page="${p.page}">Посмотреть весь лист</button>`:`<p>${escapeHtml(p.image_error||'Изображение в очереди локальной отрисовки')}</p>`}${matches.map(m=>` <button class="button secondary" data-pzz-number="${escapeHtml(m.cadastral_number)}">Показать участок ${escapeHtml(m.cadastral_number)}</button><p>Кадастровая геометрия получена: ${escapeHtml(m.received_at)}. Дата документа и дата геометрии различаются.</p>`).join('')}`;
  }).join('')}<p>Страницы текстовых ссылок / неподдержанного вида: ${escapeHtml((d.map_reference_pages||[]).map(p=>p.page).join(', ')||'нет')}. Стандартных полей GeoPDF: ${d.standard_geopdf_markers?.length??'не проверено'}. Проверенная привязка не установлена.</p></details>`;
 }).join('');
}
async function runPzzMaps(action='index'){
 if(running||!pzzMapReport)return;
 running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 el('pzz-maps-status').textContent=action==='render'?'Готовятся изображения следующих трёх листов. Исходные даты сохраняются.':'Локальная проверка PDF, картографических страниц и упомянутых кадастровых номеров.';
 try{
  const job=await api(action==='render'?'/api/planning/maps/render':'/api/planning/maps',action==='render'?{id:pzzMapReport.result?.id,retry:el('pzz-maps-retry').checked}:{planning_id:pzzMapReport.current_planning_id,survey_id:pzzMapReport.current_survey_id});let status;
  do{await new Promise(r=>setTimeout(r,1000));status=await api('/api/jobs/'+job.job_id)}while(status.state==='running');
  await refreshPzzMaps();if(typeof refreshRecon==='function')await refreshRecon();if(status.state==='error')throw Error(status.error);
 }catch(e){el('pzz-maps-status').textContent+=' Не выполнено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);el('pzz-maps-render').disabled=!pzzCanRender()}
}
el('pzz-maps-run').addEventListener('click',()=>runPzzMaps());
el('pzz-maps-render').addEventListener('click',()=>runPzzMaps('render'));
el('pzz-maps-retry').addEventListener('change',()=>el('pzz-maps-render').disabled=running||!pzzCanRender());
el('pzz-maps-results').addEventListener('click',e=>{
 const numberButton=e.target.closest('[data-pzz-number]');
 if(numberButton){
  if(pzzMapReport?.stale||!currentSurvey||pzzMapReport?.current_survey_id!==currentSurvey.id){el('pzz-maps-status').textContent='Область изменилась; обновите обследование и повторите сопоставление карт.';return}
  const normalize=x=>String(x||'').split(':').map(p=>String(Number(p))).join(':');
  const features=currentSurvey.layers.parcels.geojson.features.filter(f=>normalize(f.properties?.options?.cad_num||f.properties?.label)===numberButton.dataset.pzzNumber);
  if(!features.length)return;
  const layer=L.geoJSON({type:'FeatureCollection',features},{style:{color:'#9a4f23',weight:4,fillOpacity:.1},onEachFeature:(f,l)=>l.bindPopup('Кадастровый контур, упомянутый в ПЗЗ. Граница зоны не установлена.')}).addTo(sdraw);
  smap.fitBounds(layer.getBounds(),{padding:[20,20]});el('survey-map').scrollIntoView({behavior:'smooth',block:'center'});return;
 }
 const button=e.target.closest('[data-pzz-doc]');if(!button||pzzMapReport?.stale)return;
 el('pzz-map-image').src='/api/planning/maps/page?'+new URLSearchParams({document:button.dataset.pzzDoc,page:button.dataset.pzzPage});
 el('pzz-map-dialog').showModal();
});
el('pzz-map-close').addEventListener('click',()=>el('pzz-map-dialog').close());
refreshPzzMaps().catch(e=>el('pzz-maps-status').textContent=e.message);
