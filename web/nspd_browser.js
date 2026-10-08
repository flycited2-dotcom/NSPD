'use strict';
el('nspd-browser-import').addEventListener('click',async()=>{
 if(running)return;
 const file=el('nspd-browser-file').files[0];
 if(!file){el('nspd-browser-status').textContent='Выберите файл сохранённых ответов.';return}
 running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{
  if(file.size>10*1024*1024)throw Error('Файл должен быть не больше 10 МБ.');
  const bundle=JSON.parse(await file.text());
  const job=await api('/api/torgi/active/browser-geometry',{id:regionalTorgi?.id,survey_id:currentSurvey?.id,bundle});let state;
  el('nspd-browser-status').textContent='Проверка сохранённых ответов НСПД…';
  do{await new Promise(resolve=>setTimeout(resolve,1000));state=await api('/api/jobs/'+job.job_id)}while(state.state==='running');
  if(state.state==='error')throw Error(state.error);
  await refreshRegionalTorgi();await refreshRecon();
  el('nspd-browser-status').textContent=`Принято ответов: ${state.result.imported}. Положение лотов пересчитано для области обследования. Даты и источники сохранены; права и схема лота требуют сверки. Для обновления основного результата пересчитайте контуры.`;
 }catch(error){el('nspd-browser-status').textContent='Импорт не выполнен: '+error.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);if(reconStale||!sameReconBounds())document.querySelectorAll('[data-recon-save]').forEach(b=>b.disabled=true)}
});
