'use strict';
const el=id=>document.getElementById(id);
const escapeHtml=x=>String(x??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const nmap=L.map('nmap').setView([44.99335,34.20543],15);
const contours=L.featureGroup().addTo(nmap);
L.control.scale({imperial:false}).addTo(nmap);
let token, latest, running=false;
async function api(path,data){
  const r=await fetch(path,data?{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify({project:'trudovoe',...data})}:{});
  const body=await r.json();if(!r.ok)throw Error(body.error||'Ошибка сервера');return body;
}
function table(features,actions){return features.length?'<div class="table-wrap"><table><thead><tr><th>Объект</th><th>Площадь, м²</th><th>Права / собственность</th><th>Использование</th><th></th></tr></thead><tbody>'+features.map(f=>{const p=f.properties,o=p.options||{};return `<tr><td><b>${escapeHtml(o.cad_num||p.label||p.externalKey)}</b><br><small>${escapeHtml(p.categoryName)}</small></td><td>${escapeHtml(o.specified_area||o.land_record_area)}</td><td>${escapeHtml(o.ownership_type)}<br><small>${escapeHtml(o.right_type)}</small></td><td>${escapeHtml(o.permitted_use_established_by_document)}</td><td>${actions?`<button class="button secondary" data-watch="${escapeHtml(f.id)}">В разработку</button>`:'Требуется проверка'}</td></tr>`}).join('')+'</tbody></table></div>':'<p class="hint">Объектов нет.</p>'}
async function refresh(){
 const data=await api('/api/nspd');latest=data.result;
 el('watch-count').textContent=`(${data.watchlist.length})`;el('watch').innerHTML=table(data.watchlist.map(x=>x.feature),false);
 if(!latest)return;
 el('count').textContent=`(${latest.count})`;el('download').classList.remove('hidden');
 el('results').innerHTML=table(latest.geojson.features,true);
 el('message').textContent=`${latest.snapshot?'СНИМОК БРАУЗЕРА':'ОТВЕТ НСПД'} · дата получения ${latest.source_date} · объектов: ${latest.count}.\n${latest.warning}`;
 contours.clearLayers();L.geoJSON(latest.geojson,{style:{color:'#557c66',weight:2,fillOpacity:.22},onEachFeature:(f,l)=>l.bindPopup(escapeHtml(f.properties.options?.cad_num||f.properties.label)+'<br>'+escapeHtml(f.properties.options?.ownership_type))}).addTo(contours);
 if(contours.getLayers().length&&contours.getBounds().isValid())nmap.fitBounds(contours.getBounds(),{padding:[24,24]});
}
async function search(params){
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 el('message').textContent='Запрос НСПД… Предыдущий результат остаётся на карте до получения нового.';
 try{const job=await api('/api/nspd/search',params);let status;
 do{await new Promise(r=>setTimeout(r,1000));status=await api('/api/jobs/'+job.job_id)}while(status.state==='running');
 if(status.state==='error')throw Error(status.error);await refresh();
 }catch(e){el('message').textContent='Запрос не выполнен: '+e.message+'\nПоказанные ранее контуры не обновлены.'}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false)}
}
el('query').addEventListener('submit',e=>{e.preventDefault();search({cadnum:el('cadnum').value,mode:el('mode').value,bounds:['west','south','east','north'].map(x=>Number(el(x).value))})});
el('snapshot').addEventListener('click',()=>search({mode:'snapshot'}));
el('results').addEventListener('click',async e=>{const b=e.target.closest('[data-watch]');if(!b||running)return;b.disabled=true;try{await api('/api/nspd/watch',{id:b.dataset.watch});await refresh()}catch(err){el('message').textContent=err.message}finally{b.disabled=false}});
(async()=>{try{token=(await api('/api/session')).token;await refresh()}catch(e){el('message').textContent=e.message}})();
