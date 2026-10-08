'use strict';
async function refreshRgis(){
 const d=await api('/api/rgis'),r=d.result,a=d.attempt;
 let text=a?`Последняя проверка РГИС: ${a.state==='done'?'завершена':a.state==='running'?'выполняется':a.state==='interrupted'?'прервана':'ошибка'}${a.error?' · '+a.error:''}. `:'';
 if(r){
  const same=r.bounds.every((value,i)=>Math.abs(value-boundsInput()[i])<1e-9);
  const labels={functional:'функциональных полигонов',settlements:'полигонов населённых пунктов',roads:'дорожных контуров'};
  text+=`Карта «ГП 2019г. Симферопольский район»: ${Object.entries(r.layers).map(([mode,l])=>`${l.count} ${labels[mode]} · получено ${reconDate(l.received_at)}`).join('; ')}. ${same?'Область совпадает с выбранной.':'Полученные слои относятся к другой области.'} `;
  if(Object.values(r.layers).some(l=>l.server_clock_warning))text+='Часы источника расходятся с временем получения; дата ответа не принята как дата актуальности. ';
  text+=r.warning+' После обновления пересчитайте контуры; можно отключить сетевые обновления НСПД и торгов.';
 }else text+='Генплан РГИС ещё не получен. Это отдельный источник; действующие территориальные зоны ПЗЗ он не подтверждает.';
 el('rgis-status').textContent=text;
}
el('rgis-run').addEventListener('click',async()=>{
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 el('rgis-status').textContent='Получение трёх опубликованных слоёв генплана РГИС…';
 try{
  const job=await api('/api/rgis',{bounds:boundsInput()});let state;
  do{await new Promise(resolve=>setTimeout(resolve,1000));state=await api('/api/jobs/'+job.job_id);await refreshRgis()}while(state.state==='running');
  await refreshRecon();if(state.state==='error')throw Error(state.error);
 }catch(error){el('rgis-status').textContent+=' Не выполнено: '+error.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);if(reconStale||!sameReconBounds())document.querySelectorAll('[data-recon-save]').forEach(b=>b.disabled=true)}
});
refreshRgis().catch(error=>el('rgis-status').textContent=error.message);
