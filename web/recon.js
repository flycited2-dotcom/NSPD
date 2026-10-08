'use strict';
const reconMap=L.map('recon-map').setView([44.99335,34.20543],15);
const reconDraw=L.featureGroup().addTo(reconMap);
let reconOutline=null,reconResult=null,reconStale=false;
let reconPurposeLoaded=false;
let reconFocus=null;
LandBasemap.bind(reconMap,el('recon-osm'));
const reconNames={draft:'Пробный контур',gap:'Нужно проектирование',offer:'Предложение НСПД',auction:'Аукцион / торги'};
const reconDate=x=>x?new Date(x).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})+' МСК':'не получено';
const sameReconBounds=()=>reconResult&&reconResult.bounds.every((n,i)=>Math.abs(n-boundsInput()[i])<1e-9);
function drawReconArea(){
 if(reconOutline)reconMap.removeLayer(reconOutline);
 const [w,s,e,n]=boundsInput();
 if([w,s,e,n].every(Number.isFinite)&&w<e&&s<n){reconOutline=L.rectangle([[s,w],[n,e]],{color:'#9e7128',dashArray:'6 5',fill:false,interactive:false}).addTo(reconMap);el('recon-area').textContent=`Выбранная область: ${w}, ${s} — ${e}, ${n}. ${reconResult&&!sameReconBounds()?'Показанный результат относится к прежней области; запустите новый поиск.':''}`;}
}
function reconRows(rows,watch=false){
 if(!rows.length)return '<p>Контуры не найдены или ещё не сохранены. Отсутствие результата не подтверждает отсутствие подходящей земли.</p>';
 return '<div class="table-wrap"><table><thead><tr><th>Контур</th><th>Площадь</th><th>Что известно</th><th>Действия</th></tr></thead><tbody>'+rows.map(row=>{const c=watch?row.candidate:row;const stale=watch?row.stale:reconStale||!sameReconBounds();const version=watch?row.result_id:reconResult.id;return `<tr><td><b>${escapeHtml(reconNames[c.kind])}</b><br><small>${escapeHtml(c.id)} · ${stale?'требует обновления':'требует проверки прав'}</small></td><td>${areaFmt(c.area_m2)} м²</td><td>${c.flags.map(escapeHtml).join('<br>')||'Полученные препятствия внутри контура не установлены.'}<br>ЗОУИТ: ${c.matches.restrictions.length}; ПЗЗ: ${c.matches.pzz.length}; процедуры: ${c.lots.length}.${c.road_proximity?"<br>До контура дорожного назначения: "+areaFmt(c.road_proximity.distance_m)+" м":""}${reconAccessText(c)}<br><small>Права, подъезд и допустимость использования не подтверждены.</small></td><td><button class="button secondary" data-recon-show="${escapeHtml(c.id)}" ${watch?'data-recon-watch-show="1"':''}>На карте</button> <a class="button secondary" target="_blank" rel="noopener" href="/api/recon/dossier?result_id=${encodeURIComponent(version)}&id=${encodeURIComponent(c.id)}">Досье</a> <a class="button secondary" href="/api/recon/rights-request?result_id=${encodeURIComponent(version)}&id=${encodeURIComponent(c.id)}">Запрос сведений</a> <a class="button secondary" href="/api/recon/planning-request?result_id=${encodeURIComponent(version)}&id=${encodeURIComponent(c.id)}">Запрос ПЗЗ</a>${watch?'':` <button class="button secondary" data-recon-save="${escapeHtml(c.id)}" ${stale?'disabled':''}>В разработку</button>`}</td></tr>`}).join('')+'</tbody></table></div>';
}
function reconAccessText(c){
 const a=c.access_evidence;if(!a)return '';
 const distance=row=>row?areaFmt(row.distance_m)+' м':'не установлено';
 const gp=c.general_plan_matches?.functional||[];const codes=[...new Set(gp.map(row=>row.fields.SUBSUBTYPE).filter(code=>code!=null))];
 return '<br>До полученного участка: '+distance(a.nearest_parcel)+'; здания: '+distance(a.nearest_building)+'.'+(a.direct_segment?'<br>Прямой отрезок к дороге: '+areaFmt(a.direct_segment.length_m)+' м; пересечения участков: '+a.intersections.parcels.length+', зданий: '+a.intersections.buildings.length+'. Это не маршрут.':'<br>Контур дорожного назначения не получен.')+(gp.length?'<br>Генплан РГИС: '+gp.length+' функциональных полигонов; коды '+codes.map(escapeHtml).join(', ')+'. Актуальность и ПЗЗ не подтверждены.':'');
}
function showReconCandidate(c){
 if(reconFocus)reconDraw.removeLayer(reconFocus);
 reconFocus=L.featureGroup().addTo(reconDraw);
 L.geoJSON({type:'Feature',geometry:c.geometry,properties:{}},{style:{color:'#bc841e',weight:4,fillOpacity:.2}}).addTo(reconFocus);
 const a=c.access_evidence;if(a?.direct_segment)L.geoJSON({type:'Feature',geometry:a.direct_segment.geometry,properties:{}},{style:{color:'#82479b',weight:4,dashArray:'7 6'},pointToLayer:(f,p)=>L.circleMarker(p,{color:'#82479b',radius:6}),onEachFeature:(f,l)=>l.bindPopup('Прямой отрезок к полученному контуру дорожного назначения. Не маршрут; законный подъезд не подтверждён.')}).addTo(reconFocus);
 reconMap.fitBounds(reconFocus.getBounds(),{padding:[25,25]});
}
let reconWatch=[];
async function refreshRecon(){
 const d=await api('/api/recon');reconResult=d.result;reconStale=d.stale;reconWatch=d.watchlist;
 const a=d.attempt;el('recon-status').textContent=a?`Последняя операция: ${({running:'выполняется',done:'завершена',error:'ошибка',interrupted:'прервана'})[a.state]||a.state} · ${a.step||''}${a.error?' · '+a.error:''}`:'Выделите область, задайте площадь и нажмите «Найти контуры».';
 if(reconResult){
  const r=reconResult,s=r.summary;
  if(!reconPurposeLoaded){el('recon-purpose').value=r.parameters.purpose;reconPurposeLoaded=true}
  el('recon-status').textContent+=`\nРасчёт ${reconDate(r.created_at)}. Цель: ${({'housing':'ИЖС','personal_farm':'ЛПХ','agriculture':'Сельскохозяйственное использование'})[r.parameters.purpose]||'не выбрана'}. Пробных контуров: ${s.drafts}; предложений: ${s.offers}; аукционов: ${s.auctions}; промежутков для проектирования: ${s.large_gaps}. Вычтены полученные участки (${s.excluded_parcels}) и здания (${s.excluded_buildings}).\n${r.warning}${d.stale?' Источники или правила расчёта изменились; повторите расчёт.':''}${r.operation_warnings.length?'\n'+r.operation_warnings.join('\n'):''}`;
  const dates=Object.values(r.sources.nspd).map(x=>x.received_at).filter(Boolean).sort();
  const tileText=Object.entries(r.sources.nspd).filter(([k,v])=>v.coverage).map(([k,v])=>`${k==='parcels'?'Кадастр':'Здания'}: основной ответ ${v.coverage.parent_count}; по частям ${v.coverage.children_unique_count}; добавлено ${v.coverage.additional_ids.length}; полнота ЕГРН не подтверждена`).join('; ');
  const pzz=r.sources.nspd.pzz;const pzzText=pzz?.received_at?`Территориальные зоны НСПД: ${pzz.count??'число не установлено'} объектов · ${reconDate(pzz.received_at)}. ${pzz.count===0?'Зоны для проверки выбранного использования не получены; пустой ответ не означает отсутствие ПЗЗ.':'Действующая редакция ПЗЗ и допустимость выбранного использования требуют подтверждения.'}`:'Датированный ответ территориальных зон НСПД не получен; допустимость выбранного использования не установлена.';
  const regional=r.sources.torgi_active;const regionalText=regional?.id?`Региональные публикации: ${regional.lots} (ЗК: ${regional.groups?.land_provision??'не установлено'}; имущество должников: ${regional.groups?.debt_sale??'не установлено'}) · ${reconDate(regional.received_at)}; номеров без геометрии ${regional.unlocated_numbers.length}; лотов без номера ${regional.lots_without_number}.`:'Региональная выборка не подключена.';
  const context=r.sources.context;const contextText=context?Object.values(context.layers).map(l=>`${l.title}: ${context.applied&&['received','retained'].includes(l.state)?l.count+' объектов · '+reconDate(l.received_at)+(l.state==='retained'?' · прежнее наблюдение, обновление не удалось':''):'не получено'}${l.error?' · '+l.error:''}`).join('; '):'Кварталы, схемы и границы этой версии не получены';
  el('recon-sources').textContent=`${tileText}. ${pzzText} ${regionalText} НСПД: ${reconDate(dates[0])} — ${reconDate(dates.at(-1))}. ${contextText}. Пустые ответы не доказывают отсутствие объектов. Торги: ${reconDate(r.sources.torgi.received_at)}, запрос «${r.sources.torgi.query||'не задан'}»; номеров без геометрии ${r.sources.torgi.unlocated_numbers.length}. Полнота кадастра, ПЗЗ и ограничений не подтверждена. Подбор конфигурации ограничен: ${r.layout.layout_checks} проверок${r.layout.layout_limit_reached?', предел достигнут':''}. Отсеяно по площади: ${s.filtered.small}; по ширине: ${s.filtered.narrow}.${s.candidate_rows_omitted?' Не показано из-за предела: '+s.candidate_rows_omitted+'.':''}`;
  const kind=el('recon-kind').value;el('recon-results').innerHTML=reconRows(r.candidates.filter(c=>kind==='all'||c.kind===kind));
  el('recon-export').classList.remove('hidden');el('recon-report').classList.remove('hidden');
  reconDraw.clearLayers();
  reconFocus=null;
  for(const mode of ['parcels','buildings','restrictions'])L.geoJSON(r.map_layers[mode],{style:{color:mode==='restrictions'?'#bd4d4d':mode==='parcels'?'#87918a':'#624332',weight:1,fillOpacity:mode==='restrictions'?.04:.15,dashArray:mode==='restrictions'?'5 4':null},onEachFeature:(f,l)=>l.bindPopup(escapeHtml(f.properties.label))}).addTo(reconDraw);
  for(const mode of ['settlements','quarters','schemes','planned_parcels','red_lines','water','forests','protected','heritage'])if(r.map_layers[mode])L.geoJSON(r.map_layers[mode],{style:{color:mode==='red_lines'?'#cf3544':mode==='quarters'?'#8067aa':mode==='water'?'#4489a5':['forests','protected'].includes(mode)?'#567741':mode==='settlements'?'#4489a5':'#b05e35',weight:mode==='red_lines'?2:1,fillOpacity:.02,dashArray:'5 6'},onEachFeature:(f,l)=>l.bindPopup(escapeHtml(f.properties.label))}).addTo(reconDraw);
  if(el('boundary-show').checked&&r.map_layers.historical_boundary)L.geoJSON(r.map_layers.historical_boundary,{style:{color:'#9a5a32',weight:3,fill:false,dashArray:'9 7'},onEachFeature:(f,l)=>l.bindPopup(escapeHtml(f.properties.label))}).addTo(reconDraw);
  if(el('rgis-show').checked)for(const mode of ['functional','settlements','roads'])if(r.map_layers['rgis_'+mode])L.geoJSON(r.map_layers['rgis_'+mode],{style:{color:mode==='functional'?'#187f99':mode==='roads'?'#194da2':'#38878c',weight:2,fillOpacity:.04,dashArray:'3 5'},onEachFeature:(f,l)=>l.bindPopup(escapeHtml(f.properties.label))}).addTo(reconDraw);
  for(const c of r.candidates)L.geoJSON({type:'Feature',geometry:c.geometry,properties:{}},{style:{color:c.kind==='auction'?'#893f91':c.kind==='offer'?'#318ab5':'#bc841e',weight:2,fillOpacity:.25},onEachFeature:(f,l)=>l.bindPopup(`${escapeHtml(reconNames[c.kind])} · ${areaFmt(c.area_m2)} м²<br>Правовой статус не подтверждён`)}).addTo(reconDraw);
  const [w,south,e,n]=r.bounds;reconMap.fitBounds([[south,w],[n,e]],{padding:[16,16]});
 }
 el('recon-watch').innerHTML=reconRows(reconWatch,true);drawReconArea();
}
async function runRecon(){
 if(running)return;running=true;stopSelection();document.querySelectorAll('button').forEach(b=>b.disabled=true);
 el('recon-status').textContent='Выполняется обследование и подбор контуров…';
 try{const job=await api('/api/recon',{bounds:boundsInput(),min_area:Number(el('survey-min').value),max_area:Number(el('survey-max').value),min_width:Number(el('survey-width').value),purpose:el('recon-purpose').value,target_area:Number(el('recon-area-size').value),limit:Number(el('recon-limit').value),refresh_nspd:el('recon-update-nspd').checked,verify_nspd:el('recon-verify-nspd').checked,refresh_active_torgi:el('recon-update-active-torgi').checked,refresh_active_documents:el('recon-update-active-documents').checked,refresh_torgi:el('recon-update-torgi').checked,avoid_restrictions:el('recon-avoid-zones').checked,avoid_planned:el('recon-avoid-planned').checked,avoid_environment:el('recon-avoid-environment').checked,query:el('torgi-query').value});let state;
 do{await new Promise(r=>setTimeout(r,1000));state=await api('/api/jobs/'+job.job_id);await refreshRecon()}while(state.state==='running');
 await refreshSurvey();await refreshRecon();if(typeof refreshRegionalTorgi==='function')await refreshRegionalTorgi();if(typeof refreshRegionalDocs==='function')await refreshRegionalDocs();if(typeof refreshPzzMaps==='function')await refreshPzzMaps();if(state.state==='error')throw Error(state.error);
 }catch(e){el('recon-status').textContent+='\nНе выполнено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);if(reconStale||!sameReconBounds())document.querySelectorAll('[data-recon-save]').forEach(b=>b.disabled=true)}
}
el('recon-select').addEventListener('click',()=>startSelection(reconMap,el('recon-select')));
reconMap.on('click',selectCorner);
el('recon-run').addEventListener('click',runRecon);
el('recon-kind').addEventListener('change',refreshRecon);
el('boundary-show').addEventListener('change',refreshRecon);
el('rgis-show').addEventListener('change',refreshRecon);
for(const container of ['recon-results','recon-watch'])el(container).addEventListener('click',async e=>{
 const show=e.target.closest('[data-recon-show]');if(show){const c=show.dataset.reconWatchShow?reconWatch.find(r=>r.candidate.id===show.dataset.reconShow)?.candidate:reconResult?.candidates.find(c=>c.id===show.dataset.reconShow);if(c)showReconCandidate(c);return;}
 const save=e.target.closest('[data-recon-save]');if(!save||running||!reconResult)return;save.disabled=true;
 try{await api('/api/recon/watch',{result_id:reconResult.id,id:save.dataset.reconSave});await refreshRecon()}catch(error){el('recon-status').textContent=error.message}finally{save.disabled=reconStale||!sameReconBounds()}
});
refreshRecon().catch(e=>el('recon-status').textContent=e.message);
let boundaryResult=null;
async function refreshBoundary(){
 const d=await api('/api/planning/boundary');boundaryResult=d.result;
 const a=d.attempt;
 let text=a?`Проверка документов: ${({running:'выполняется',done:'завершена',error:'ошибка',interrupted:'прервана'})[a.state]||a.state} · ${a.step||''}${a.error?' · '+a.error:''}. `:'';
 if(d.result){const r=d.result;text+=`Документ границы 17.02.2021 получен ${reconDate(r.source.received_at)}. В перечнях найдено ${r.history.items.length} ссылок Трудовского поселения; ${r.history.all_observed_pages_received?'все выбранные страницы перечней получены':'часть страниц не обновлена, прежние наблюдения датированы'}. Предполагаемое положение границы и её актуальность требуют подтверждения. Включите её показ для сравнения; обновлённый источник применяется при следующем расчёте. Полнота истории и действующая редакция ПЗЗ не подтверждены.`;}
 else text+='Действующая граница требует проверки. Исторический документ не используется для исключения земли.';
 el('boundary-status').textContent=text;
}
async function runBoundary(){
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{const job=await api('/api/planning/boundary',{expected_id:boundaryResult?.id||null});let state;
 do{await new Promise(r=>setTimeout(r,1000));state=await api('/api/jobs/'+job.job_id);await refreshBoundary()}while(state.state==='running');
 await refreshRecon();if(state.state==='error')throw Error(state.error);
 }catch(e){el('boundary-status').textContent+=' Не выполнено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);if(typeof pzzCanRender==='function')el('pzz-maps-render').disabled=!pzzCanRender();if(reconStale||!sameReconBounds())document.querySelectorAll('[data-recon-save]').forEach(b=>b.disabled=true)}
}
el('boundary-run').addEventListener('click',runBoundary);
refreshBoundary().catch(e=>el('boundary-status').textContent=e.message);
