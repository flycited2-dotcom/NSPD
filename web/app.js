'use strict';
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeLink = url => /^https:\/\//i.test(url || '') ? esc(url) : '#';
let project = 'trudovoe', state, token, currentView = 'map', selected = null;
let drawing = L.featureGroup(), fittedProject = null;
const map = L.map('map', {zoomControl:true, attributionControl:true}).setView([45,34.2], 12);
drawing.addTo(map);
L.control.scale({imperial:false,position:'bottomright'}).addTo(map);
const statusNames = {yellow:'Нужна проверка',green:'Проверки пройдены',red:'Есть препятствие'};
const fmt = v => Number(v).toLocaleString('ru-RU', {maximumFractionDigits:0});
const dateText = s => s ? new Date(s).toLocaleString('ru-RU') : '—';
const kinds = {layer_import:'Обновлён слой',analysis:'Выполнен поиск',candidate_update:'Изменено досье',endpoint_audit:'Проверка НСПД',endpoint_registry_update:'Обновлён реестр интерфейсов',job_error:'Операция не выполнена'};
let toastTimer;
function toast(message, error=false) { $('toast').textContent=message; $('toast').className='toast'+(error?' error':''); clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),error?14000:6500); }
async function api(path, data) {
  const response = await fetch(path, data ? {method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify({project,...data})} : {});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Ошибка запроса');
  return result;
}
async function refresh(fit=false) {
  const expectedProject=project;
  const result=await api('/api/state?project='+project);
  if(project!==expectedProject)return;
  state=result; render(fit);
}
function go(view) {
  currentView=view;
  document.querySelectorAll('.view').forEach(x=>x.classList.toggle('hidden',x.id!=='view-'+view));
  document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x.dataset.view===view));
  $('page-title').textContent={map:'Карта поиска',registry:'Реестр кандидатов',layers:'Данные и слои',sources:'Источники и НСПД',guide:'От поиска до подачи',log:'Журнал работы'}[view];
  if(view==='map')setTimeout(()=>map.invalidateSize(),30);
}
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>go(b.dataset.view)));
document.querySelectorAll('[data-go]').forEach(b=>b.addEventListener('click',()=>go(b.dataset.go)));
function rows(cs) {
  if(!cs.length)return '<div class="empty-table">Пока нет кандидатов. Загрузите данные и запустите поиск или откройте учебный пример.</div>';
  return `<div class="table-wrap"><table><thead><tr><th>Кандидат</th><th>Площадь</th><th>Проверка</th><th>Стадия</th><th>Доступ</th><th></th></tr></thead><tbody>${cs.map(c=>`<tr><td><b>${esc(c.id)}</b><br><small class="muted">${c.stale?'Данные изменились':!c.active?'Нет в последнем расчёте':c.large_window?'Крупное окно':'Кандидат'}</small></td><td>${fmt(c.area_m2)} м²</td><td><span class="badge ${c.status}">${statusNames[c.status]}</span></td><td>${esc(state.workflows[c.effective_workflow])}</td><td>${c.road_distance_m===null?'Не подтверждён':fmt(c.road_distance_m)+' м до слоя дорог'}<br><small class="muted">Требуется проверка</small></td><td><button class="text-button open-candidate" data-id="${esc(c.id)}">Открыть →</button></td></tr>`).join('')}</tbody></table></div>`;
}
function renderRegistry() {
  const q=$('filter-text').value.toLocaleLowerCase(), filter=$('filter-status').value;
  const cs=state.candidates.filter(c=>(!q||JSON.stringify(c).toLocaleLowerCase().includes(q))&&(filter==='all'||filter===c.status||(filter==='working'&&c.workflow==='working')||(filter==='archived'&&!c.active)));
  $('registry-table').innerHTML=rows(cs);
}
function render(fit) {
  const active=state.candidates.filter(c=>c.active);
  $('stat-total').textContent=active.length;
  $('stat-working').textContent=state.candidates.filter(c=>c.workflow==='working').length;
  $('stat-green').textContent=active.filter(c=>c.status==='green').length;
  $('stat-layers').innerHTML=state.layers.length+'<span>/ 8</span>';
  $('nav-count').textContent=state.candidates.length;
  $('export').href='/api/export?project='+project;
  $('demo-banner').classList.toggle('hidden',project!=='demo');
  const boundary=state.layers.find(x=>x.role==='boundary'), parcels=state.layers.find(x=>x.role==='parcels');
  $('data-hint').textContent=boundary&&parcels?'проверьте полноту и актуальность':'нужны граница и кадастр';
  $('readiness').innerHTML=[['Граница исследования',!!boundary],['Кадастровый слой',!!parcels],['Полнота кадастра',!!parcels?.metadata.complete&&!!parcels?.metadata.coverage]].map(([t,v])=>`<div class="${v?'yes':'no'}">${v?'✓':'○'} ${t}</div>`).join('');
  $('map-label').textContent=(project==='demo'?'Учебный пример':'Трудовое')+' · '+(boundary?'загруженная область':'область не загружена');
  $('map-empty').classList.toggle('hidden',!!boundary);
  $('map-results').innerHTML=rows(active.slice(0,5));
  renderRegistry(); renderLayers(); renderSources(); renderEvents(); drawMap(fit);
  if(!$('layer-role').options.length)$('layer-role').innerHTML=Object.entries(state.roles).map(([k,v])=>`<option value="${k}">${esc(v)}</option>`).join('');
  const ep=state.endpoints.find(e=>e.id==='NSPD-HOME');
  $('connection').textContent=state.nspd_last?.received_at?(state.nspd_last.snapshot?'НСПД · сохранённый снимок':'НСПД · получен ответ поиска'):(ep&&ep.http_status===200?'Портал отвечает · API не проверен':'НСПД · откройте живой поиск');
}
function drawMap(fit=false) {
  drawing.clearLayers();
  const colors={boundary:'#698b72',parcels:'#929d91',roads:'#b9bcad',exclusions:'#ad6860',restrictions:'#9e6a94',buildings:'#718077',pzz:'#6b94ad',quarters:'#8fa5b3'};
  for(const layer of state.layers){
    L.geoJSON(layer.geojson,{style:{color:colors[layer.role],weight:layer.role==='boundary'?2:1,fillColor:colors[layer.role],fillOpacity:layer.role==='boundary'?0:.18,dashArray:layer.role==='boundary'?'7 5':null},onEachFeature:(f,l)=>l.bindTooltip(esc(state.roles[layer.role])+(f.properties.name?': '+esc(f.properties.name):''))}).addTo(drawing);
  }
  for(const c of state.candidates.filter(c=>c.active)){
    const color={yellow:'#c89324',green:'#2d7954',red:'#b85745'}[c.status];
    L.geoJSON(c.geometry,{style:{color,weight:2,fillOpacity:.22},onEachFeature:(f,l)=>{l.bindTooltip(esc(c.id)+' · '+fmt(c.area_m2)+' м²');l.on('click',()=>openCandidate(c.id));}}).addTo(drawing);
    L.marker([c.point[1],c.point[0]],{icon:L.divIcon({className:'',html:'<div class="candidate-label">'+esc(c.id)+'</div>',iconSize:[53,24]})}).on('click',()=>openCandidate(c.id)).addTo(drawing);
  }
  if((fit||fittedProject!==project)&&drawing.getLayers().length){map.fitBounds(drawing.getBounds(),{padding:[55,70],maxZoom:18});fittedProject=project;}
}
$('fit-map').addEventListener('click',()=>{if(drawing.getLayers().length)map.fitBounds(drawing.getBounds(),{padding:[50,60],maxZoom:18});});
LandBasemap.bind(map,$('basemap'));
function renderLayers(){
  $('layers-list').innerHTML=Object.entries(state.roles).map(([role,label])=>{const l=state.layers.find(x=>x.role===role);return `<div class="layer-row"><h3>${l?'✓':'○'} ${esc(label)}</h3>${l?`<p>${esc(l.metadata.title)} · ${l.geojson.features.length} объектов</p><p>${esc(l.metadata.source)}</p><p>Дата сведений: ${esc(l.metadata.checked_at)} · Исходный CRS: ${esc(l.metadata.original_crs)}</p><span class="badge ${l.metadata.official?'green':'yellow'}">${l.metadata.official?'Официальность отмечена пользователем':'Происхождение требует проверки'}</span>`:'<p>Не загружен</p>'}</div>`;}).join('');
}
function renderSources(){
  $('sources-list').innerHTML=state.sources.map(s=>`<article class="source-card"><small>${esc(s.kind)}</small><a href="${safeLink(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)} ↗</a><p>${esc(s.status)}</p></article>`).join('');
  $('endpoints-list').innerHTML=state.endpoints.length?state.endpoints.map(e=>`<div class="endpoint"><b>${esc(e.purpose)}</b><p><code>${esc(e.method)} ${esc(e.url)}</code></p><span class="badge yellow">${esc(e.status)}</span><p>${esc(e.error||e.note)}</p><p>${dateText(e.checked_at)} · ${esc(e.type)} · CRS: ${esc(e.crs)}</p></div>`).join(''):'<p class="muted">Проверенных интерфейсов нет. Выполните аудит или импортируйте наблюдения из HAR.</p>';
}
function renderEvents(){
  $('events-list').innerHTML=state.events.length?state.events.map(e=>`<div class="event"><time>${dateText(e.at)}</time><h3>${esc(kinds[e.kind]||e.kind)}</h3><details><summary>Подробности</summary><pre>${esc(JSON.stringify(e.body,null,2))}</pre></details></div>`).join(''):'<p class="muted">Здесь появятся импорты, поиски и изменения досье.</p>';
}
async function watchJob(id){
  const p=project;
  $('job-banner').className='job-banner';$('job-banner').textContent='Выполняется операция…';
  while(true){
    const job=await api('/api/jobs/'+id);
    if(job.state!=='running'){
      if(project===p){
        $('job-banner').className='job-banner'+(job.state==='error'?' error':'');
        $('job-banner').textContent=job.state==='error'?job.error:job.kind+' завершена'+(job.result?.count!==undefined?': '+job.result.count+' объектов':'');
        await refresh(job.kind==='Поиск кандидатов');
      }
      if(job.state==='error')throw new Error(job.error);
      return job;
    }
    await new Promise(resolve=>setTimeout(resolve,1200));
  }
}
async function busy(button, fn){const old=button.textContent;button.disabled=true;button.textContent='Выполняется…';try{await fn();}catch(e){toast(e.message,true);}finally{button.disabled=false;button.textContent=old;}}
$('search-form').addEventListener('submit',e=>{e.preventDefault();busy($('search-button'),async()=>{const j=await api('/api/search',{parameters:{min_area:Number($('min-area').value),max_area:Number($('max-area').value),min_width:Number($('min-width').value),clearance:Number($('clearance').value)}});await watchJob(j.job_id);});});
$('layer-role').addEventListener('change',()=>$('coverage-fields').classList.toggle('hidden',$('layer-role').value!=='parcels'));
$('layer-date').value=new Date().toLocaleDateString('en-CA');
$('layer-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{
  const role=$('layer-role').value,file=$('layer-file').files[0],url=$('layer-url').value.trim();
  if(!file&&!url)throw new Error('Выберите GeoJSON-файл или укажите ссылку');
  if(file&&url)throw new Error('Выберите один источник: файл или ссылку');
  if(file&&file.size>25*1024*1024)throw new Error('Максимальный размер файла — 25 МБ');
  const data={role,metadata:{title:$('layer-title').value,source:$('layer-source').value,checked_at:$('layer-date').value,crs:$('layer-crs').value,official:$('layer-official').checked,complete:role==='parcels'&&$('layer-complete').checked},coverage_current_boundary:role==='parcels'&&$('layer-coverage').checked};
  if(url){const j=await api('/api/import-url',{...data,url});await watchJob(j.job_id);}else{data.geojson=JSON.parse(await file.text());await api('/api/layers',data);await refresh(true);}
  toast('Слой сохранён. После обновления слоёв запустите поиск повторно.');
});});
$('project').addEventListener('change',async()=>{project=$('project').value;selected=null;$('dossier').close();$('job-banner').classList.add('hidden');await refresh(true);});
$('demo-start').addEventListener('click',()=>busy($('demo-start'),async()=>{project='demo';$('project').value='demo';await api('/api/demo',{});await refresh(true);go('map');toast('Учебный проект открыт. Все контуры в нём искусственные.');}));
$('audit').addEventListener('click',()=>busy($('audit'),async()=>{const j=await api('/api/audit',{});await watchJob(j.job_id);}));
$('har-import').addEventListener('click',()=>busy($('har-import'),async()=>{const file=$('har-file').files[0];if(!file)throw new Error('Выберите HAR-файл');if(file.size>25*1024*1024)throw new Error('HAR должен быть меньше 25 МБ');const result=await api('/api/har',{har:JSON.parse(await file.text())});await refresh();toast('Добавлено интерфейсов: '+result.count);}));
$('filter-text').addEventListener('input',renderRegistry);$('filter-status').addEventListener('change',renderRegistry);
document.addEventListener('click',e=>{const b=e.target.closest('.open-candidate');if(b)openCandidate(b.dataset.id);});
function openCandidate(id){
  const c=state.candidates.find(x=>x.id===id);if(!c)return;selected=id;
  $('dossier-title').textContent=id+' · '+(project==='demo'?'Учебный пример':'Трудовое');
  $('dossier-summary').innerHTML=`<div class="summary-grid"><div><b>${fmt(c.area_m2)} м²</b><small>Расчётная площадь</small></div><div><span class="badge ${c.status}">${statusNames[c.status]}</span><br><small>${c.blockers.length} проверок до зелёного статуса</small></div></div><div class="external-links"><a target="_blank" rel="noopener noreferrer" href="https://yandex.ru/maps/?ll=${encodeURIComponent(c.point.join(','))}&z=18&l=sat">Спутниковая карта ↗</a><a target="_blank" rel="noopener noreferrer" href="https://nspd.gov.ru/">НСПД ↗</a><span class="muted">${esc(c.point.join(', '))}</span></div><p class="blocker-text">${c.stale?'Исходные данные изменились. Нужен повторный расчёт. ':''}${c.large_window?'Крупное окно: нужен проект меньшего контура. ':''}До готовности пакета: ${c.package_blockers.length} проверок.</p>`;
  $('candidate-workflow').innerHTML=Object.entries(state.workflows).map(([k,v])=>`<option value="${k}">${esc(v)}</option>`).join('');
  $('candidate-workflow').value=c.effective_workflow;$('candidate-notes').value=c.notes;$('candidate-reference').value=c.reference;$('candidate-deadline').value=c.deadline;
  $('checks-list').innerHTML=Object.entries({...state.checks,...state.package_checks}).map(([key,label])=>{const ch=c.checks[key]||{};return `<div class="check-row" data-key="${key}"><h4>${esc(label)}</h4><select data-field="result" aria-label="Результат: ${esc(label)}"><option value="unknown">Не подтверждено</option><option value="pass" ${ch.result==='pass'?'selected':''}>Подтверждено</option><option value="fail" ${ch.result==='fail'?'selected':''}>Есть препятствие</option></select><input data-field="date" type="date" aria-label="Дата: ${esc(label)}" value="${esc(ch.date||'')}"><input data-field="source" placeholder="Источник, страница / раздел" aria-label="Источник: ${esc(label)}" value="${esc(ch.source||'')}"><textarea data-field="note" rows="2" placeholder="Вывод и обоснование" aria-label="Обоснование: ${esc(label)}">${esc(ch.note||'')}</textarea></div>`;}).join('');
  $('print-dossier').href='/api/dossier?project='+project+'&id='+encodeURIComponent(id);
  $('dossier').showModal();
}
$('close-dossier').addEventListener('click',()=>$('dossier').close());
$('dossier-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{
  const checks={};document.querySelectorAll('.check-row').forEach(row=>{checks[row.dataset.key]={};row.querySelectorAll('[data-field]').forEach(el=>checks[row.dataset.key][el.dataset.field]=el.value);});
  await api('/api/candidate',{id:selected,workflow:$('candidate-workflow').value,notes:$('candidate-notes').value,reference:$('candidate-reference').value,deadline:$('candidate-deadline').value,checks});
  $('dossier').close();await refresh();toast('Досье сохранено с историей изменений.');
});});
(async()=>{try{token=(await api('/api/session')).token;await refresh();}catch(e){toast('Не удалось открыть локальную базу: '+e.message,true);}})();
