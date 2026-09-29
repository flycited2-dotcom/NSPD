'use strict';
const smap=L.map('survey-map').setView([44.99335,34.20543],15);
const sdraw=L.featureGroup().addTo(smap);
let currentSurvey=null;
const areaFmt=x=>Number(x).toLocaleString('ru-RU',{maximumFractionDigits:0});
function gapTable(features,actions=false){
 if(!features.length)return '<p>Промежутков по заданным фильтрам нет. Это не вывод о правовом статусе территории.</p>';
 return '<div class="table-wrap"><table><thead><tr><th>Контур</th><th>Площадь</th><th>Особенности</th><th>Публикации НСПД</th><th></th></tr></thead><tbody>'+features.map(f=>{const p=f.properties;return `<tr><td>${escapeHtml(p.label)}<br><small>Не проверен</small></td><td>${areaFmt(p.area_m2)} м²</td><td>${p.large_window?'Крупный контур; нужна проработка. ':''}${p.touches_boundary?'Примыкает к границе поиска. ':''}<br><small>Права и ограничения не проверены</small></td><td>Предложений: ${p.matches.free.length}<br>Аукционов: ${p.matches.auction.length}</td><td>${actions?`<button class="button secondary" data-show-gap="${escapeHtml(f.id)}">На карте</button>`:''}${actions?` <button class="button secondary" data-gap="${escapeHtml(f.id)}">В разработку</button>`:''}</td></tr>`}).join('')+'</tbody></table></div>';
}
async function refreshSurvey(){
 const data=await api('/api/survey');currentSurvey=data.result;
 el('survey-watch').innerHTML=gapTable(data.watchlist.map(x=>x.feature));
 const a=data.attempt;
 el('survey-status').textContent=a?`Последняя попытка: ${a.state==='done'?'завершена':a.state==='running'?'выполняется':a.state==='interrupted'?'прервана':'ошибка'} · ${a.started_at}. Получены слои: ${a.completed_layers.map(k=>({parcels:'ЕГРН',free:'предложения',auction:'аукционы'}[k]||k)).join(', ')||'нет'}.${a.error?' '+a.error+' Предыдущий результат не обновлён.':''}`:'Обследование ещё не выполнялось.';
 if(!currentSurvey)return;
 const r=currentSurvey,s=r.summary;
 el('survey-status').textContent+=`\nПоказано обследование от ${r.created_at}. Участков: ${r.layers.parcels.geojson.features.length}; предложений: ${r.layers.free.geojson.features.length}; аукционов: ${r.layers.auction.geojson.features.length}. Промежутков: ${r.gaps.features.length}. Отсеяно мелких: ${s.filtered.small}, узких: ${s.filtered.narrow}.\n${s.warning}`;
 el('survey-export').classList.remove('hidden');el('survey-results').innerHTML=gapTable(r.gaps.features,true);
 sdraw.clearLayers();
 for(const [mode,color] of [['parcels','#87918a'],['free','#318ab5'],['auction','#9957ae']])L.geoJSON(r.layers[mode].geojson,{style:{color,weight:1,fillOpacity:.15},onEachFeature:(f,l)=>l.bindPopup(escapeHtml(f.properties.label||f.id))}).addTo(sdraw);
 L.geoJSON(r.gaps,{style:{color:'#c7831d',weight:2,fillOpacity:.35},onEachFeature:(f,l)=>l.bindPopup(`${escapeHtml(f.properties.label)} · ${areaFmt(f.properties.area_m2)} м²<br>Предварительный, права не проверены`)}).addTo(sdraw);
 const [w,south,e,n]=r.bounds;L.rectangle([[south,w],[n,e]],{color:'#333',weight:1,fill:false,dashArray:'5 5'}).addTo(sdraw);smap.fitBounds([[south,w],[n,e]],{padding:[16,16]});
}
el('survey-run').addEventListener('click',async()=>{
 if(running)return;running=true;stopSelection();document.querySelectorAll('button').forEach(b=>b.disabled=true);
 el('survey-status').textContent='Загрузка трёх слоёв и расчёт. Предыдущее обследование пока остаётся на карте.';
 try{const job=await api('/api/survey',{bounds:boundsInput(),min_area:Number(el('survey-min').value),max_area:Number(el('survey-max').value),min_width:Number(el('survey-width').value)});let status;
 do{await new Promise(r=>setTimeout(r,1000));status=await api('/api/jobs/'+job.job_id)}while(status.state==='running');
 await refreshSurvey();if(status.state==='error')throw Error(status.error);
 }catch(e){el('survey-status').textContent+='\nНе выполнено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false)}
});
el('survey-results').addEventListener('click',async e=>{
 const show=e.target.closest('[data-show-gap]');if(show&&currentSurvey){const f=currentSurvey.gaps.features.find(f=>f.id===show.dataset.showGap);if(f)smap.fitBounds(L.geoJSON(f).getBounds(),{padding:[20,20]});}
 const b=e.target.closest('[data-gap]');if(!b||running||!currentSurvey)return;b.disabled=true;
 try{await api('/api/survey/watch',{survey_id:currentSurvey.id,id:b.dataset.gap});await refreshSurvey()}catch(err){el('survey-status').textContent=err.message}finally{b.disabled=false}
});
refreshSurvey().catch(e=>el('survey-status').textContent=e.message);
