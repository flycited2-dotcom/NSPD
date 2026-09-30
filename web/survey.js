'use strict';
const smap=L.map('survey-map').setView([44.99335,34.20543],15);
const sdraw=L.featureGroup().addTo(smap);
let currentSurvey=null;
const areaFmt=x=>Number(x).toLocaleString('ru-RU',{maximumFractionDigits:0});
function roadInfo(p){const r=p.road_proximity;return r?`<br>До кадастрового контура дорожного назначения: ${areaFmt(r.distance_m)} м<br><small>${escapeHtml(r.label)} · законный подъезд не подтверждён</small>`:'<br><small>Близость к дорогам не установлена</small>';}
function zoneMatches(feature){
 const rows=[];
 for(const mode of ['restrictions','pzz'])for(const match of feature.properties.zone_intersections?.[mode]||[]){
  const zone=currentSurvey?.layers[mode]?.geojson.features.find(f=>f.id===match.id);
  const name=zone?.properties.options?.name_by_doc||zone?.properties.label||match.id;
  rows.push(`<p>${escapeHtml(name)}<br>Пересечение: ${areaFmt(match.area_m2)} м²</p>`);
 }
 return rows.length?'<details><summary>Какие зоны пересекаются</summary>'+rows.join('')+'</details>':'';
}
function gapTable(features,actions=false){
 if(!features.length)return '<p>Промежутков по заданным фильтрам нет. Это не вывод о правовом статусе территории.</p>';
 return '<div class="table-wrap"><table><thead><tr><th>Контур</th><th>Площадь</th><th>Особенности</th><th>Публикации НСПД</th><th></th></tr></thead><tbody>'+features.map(f=>{const p=f.properties;return `<tr><td>${escapeHtml(p.label)}<br><small>Не проверен</small></td><td>${areaFmt(p.area_m2)} м²</td><td>${p.large_window?'Крупный контур; нужна проработка. ':''}${p.touches_boundary?'Примыкает к границе поиска. ':''}<br><small>Права и ограничения не проверены</small>${roadInfo(p)}</td><td>Предложений: ${p.matches.free.length}<br>Аукционов: ${p.matches.auction.length}<br>Зоны / сервитуты: ${p.matches.restrictions?.length??'не проверено'}<br>Территориальных зон: ${p.matches.pzz?.length??'не проверено'}${actions?zoneMatches(f):''}</td><td>${actions?`<button class="button secondary" data-show-gap="${escapeHtml(f.id)}">На карте</button>`:''}${actions?` <button class="button secondary" data-gap="${escapeHtml(f.id)}">В разработку</button>`:''}</td></tr>`}).join('')+'</tbody></table></div>';
}
async function refreshSurvey(){
 const data=await api('/api/survey');currentSurvey=data.result;
 el('survey-watch').innerHTML=gapTable(data.watchlist.map(x=>x.feature));
 const a=data.attempt;
 el('survey-status').textContent=a?`Последняя попытка: ${a.state==='done'?'завершена':a.state==='running'?'выполняется':a.state==='interrupted'?'прервана':'ошибка'} · ${a.started_at}. Получены слои: ${a.completed_layers.map(k=>({parcels:'ЕГРН',free:'предложения',auction:'аукционы',buildings:'здания',pzz:'территориальные зоны',restrictions:'ЗОУИТ'}[k]||k)).join(', ')||'нет'}.${a.error?' '+a.error+' Предыдущий результат не обновлён.':''}`:'Обследование ещё не выполнялось.';
 if(!currentSurvey)return;
 const r=currentSurvey,s=r.summary;
 el('survey-road-status').textContent=`Дорожное назначение найдено у ${s.road_designated_parcels??'не проверено'} кадастровых контуров. Они уже вычтены вместе с остальными участками. Расстояние измеряется между ближайшими границами, не по маршруту; ограждения, вход и законный подъезд не проверены. Непоставленные на кадастр дороги и территории общего пользования требуют отдельного источника.`+(r.parent_id?` Пересчёт от ${r.created_at}; даты получения исходных слоёв сохранены.`:'');
 el('survey-status').textContent+=`\nПоказан расчёт от ${r.created_at}. Участков: ${r.layers.parcels.geojson.features.length}; предложений: ${r.layers.free.geojson.features.length}; аукционов: ${r.layers.auction.geojson.features.length}. Зданий: ${r.layers.buildings?.geojson.features.length??'не загружено'}; ЗОУИТ: ${r.layers.restrictions?.geojson.features.length??'не загружено'}; территориальных зон: ${r.layers.pzz?.geojson.features.length??'не загружено'}. Исключено зданий вне кадастровых контуров: ${areaFmt(s.buildings_excluded_m2??0)} м². Промежутков: ${r.gaps.features.length}. Отсеяно мелких: ${s.filtered.small}, узких: ${s.filtered.narrow}.\n${s.warning}`;
 el('survey-export').classList.remove('hidden');el('survey-results').innerHTML=gapTable(r.gaps.features,true);
 el('survey-zones').innerHTML=['pzz','restrictions'].map(mode=>{const fs=r.layers[mode]?.geojson.features||[];return `<h4>${mode==='pzz'?'Территориальные зоны':'Ограничения и сервитуты'}</h4>`+(fs.length?fs.map(f=>{const o=f.properties.options||{};return `<details><summary>${escapeHtml(o.name_by_doc||f.properties.label||f.id)}</summary><p>${escapeHtml(o.content_restrict_encumbrances||'Описание режима не получено')}</p><p>${escapeHtml(o.legal_act_document_name||'Реквизиты документа не получены')} ${escapeHtml(o.legal_act_document_number||'')} ${escapeHtml(o.legal_act_document_date||'')}</p><p>Источник: НСПД · ${escapeHtml(r.layers[mode].received_at)}. Требуется проверка действующего документа.</p></details>`}).join(''):'<p>Объекты не получены. Покрытие и отсутствие ограничений не подтверждены.</p>')}).join('');
 sdraw.clearLayers();
 for(const [mode,color] of [['parcels','#87918a'],['free','#318ab5'],['auction','#9957ae'],['buildings','#624332'],['pzz','#468951'],['restrictions','#bd4d4d']])L.geoJSON(r.layers[mode]?.geojson||{type:'FeatureCollection',features:[]},{style:{color,weight:1,fillOpacity:mode==='restrictions'||mode==='pzz'?0:.15,dashArray:mode==='restrictions'||mode==='pzz'?'5 4':null},onEachFeature:(f,l)=>l.bindPopup(escapeHtml(f.properties.label||f.id))}).addTo(sdraw);
 L.geoJSON(r.roads||{type:'FeatureCollection',features:[]},{style:{color:'#244c6b',weight:3,fillOpacity:.25},onEachFeature:(f,l)=>l.bindPopup(`${escapeHtml(f.properties.label||f.id)} · дорожное назначение; доступ не проверен`)}).addTo(sdraw);
 L.geoJSON(r.gaps,{style:{color:'#c7831d',weight:2,fillOpacity:.35},onEachFeature:(f,l)=>l.bindPopup(`${escapeHtml(f.properties.label)} · ${areaFmt(f.properties.area_m2)} м²<br>Предварительный, права не проверены`)}).addTo(sdraw);
 const [w,south,e,n]=r.bounds;L.rectangle([[south,w],[n,e]],{color:'#333',weight:1,fill:false,dashArray:'5 5'}).addTo(sdraw);smap.fitBounds([[south,w],[n,e]],{padding:[16,16]});
}
async function runSurvey(recalculate=false){
 if(running)return;running=true;stopSelection();document.querySelectorAll('button').forEach(b=>b.disabled=true);
 el('survey-status').textContent=recalculate?'Пересчёт сохранённого обследования; новые данные НСПД не запрашиваются.':'Загрузка шести слоёв и расчёт. Предыдущее обследование пока остаётся на карте.';
 try{const job=await api(recalculate?'/api/survey/recalculate':'/api/survey',{bounds:boundsInput(),min_area:Number(el('survey-min').value),max_area:Number(el('survey-max').value),min_width:Number(el('survey-width').value)});let status;
 do{await new Promise(r=>setTimeout(r,1000));status=await api('/api/jobs/'+job.job_id)}while(status.state==='running');
 await refreshSurvey();if(status.state==='error')throw Error(status.error);
 }catch(e){el('survey-status').textContent+='\nНе выполнено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false)}
}
el('survey-run').addEventListener('click',()=>runSurvey());
el('survey-recalculate').addEventListener('click',()=>runSurvey(true));
el('survey-results').addEventListener('click',async e=>{
 const show=e.target.closest('[data-show-gap]');if(show&&currentSurvey){const f=currentSurvey.gaps.features.find(f=>f.id===show.dataset.showGap);if(f)smap.fitBounds(L.geoJSON(f).getBounds(),{padding:[20,20]});}
 const b=e.target.closest('[data-gap]');if(!b||running||!currentSurvey)return;b.disabled=true;
 try{await api('/api/survey/watch',{survey_id:currentSurvey.id,id:b.dataset.gap});await refreshSurvey()}catch(err){el('survey-status').textContent=err.message}finally{b.disabled=false}
});
refreshSurvey().catch(e=>el('survey-status').textContent=e.message);

async function refreshPublications(){
 const {result:r}=await api('/api/publications');
 if(!r)return;
 el('publication-status').textContent=`Проверка от ${r.checked_at}. ${r.limitation}`;
 el('publication-results').innerHTML=r.sources.map(s=>`<details><summary>${escapeHtml(s.title)} · ${s.state==='received'?'получено':'ошибка'}</summary><p><a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer">Официальный источник</a> · ${escapeHtml(s.checked_at)}</p>${s.error?`<p>${escapeHtml(s.error)}${s.last_success_at?' · Последний успех: '+escapeHtml(s.last_success_at):''}</p>`:''}${s.format==='pdf'?'<p>Проверены файл и доступность. Применимость и актуальность редакции ПЗЗ не установлены.</p>':'<p>Отобраны заголовки по словам о земле, дорогах и планировке. Вложения и совпадения с контурами не проверены.</p>'}<ul>${(s.links||[]).map(l=>`<li><a href="${escapeHtml(l.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(l.title)}</a></li>`).join('')}</ul>${s.state==='received'&&s.format==='html'&&!s.links.length?'<p>Подходящие заголовки на проверенной странице не найдены; другие страницы не обследованы.</p>':''}</details>`).join('');
 el('publication-export').classList.remove('hidden');
}
el('publication-run').addEventListener('click',async()=>{
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 el('publication-status').textContent='Проверяются три известных официальных источника…';
 try{const job=await api('/api/publications',{});let s;do{await new Promise(r=>setTimeout(r,1000));s=await api('/api/jobs/'+job.job_id)}while(s.state==='running');if(s.state==='error')throw Error(s.error);await refreshPublications();}
 catch(e){el('publication-status').textContent=e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false)}
});
refreshPublications().catch(e=>el('publication-status').textContent=e.message);
