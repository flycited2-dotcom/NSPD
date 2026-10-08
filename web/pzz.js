'use strict';
let pzzStatusData=null;
function drawPzzStatus(){
 if(!pzzStatusData)return;
 const {result:r,attempt:a}=pzzStatusData;
 let text=a?`Последняя проверка региональных ПЗЗ: ${({done:'завершена',running:'выполняется',interrupted:'прервана',error:'ошибка'})[a.state]||a.state}${a.error?' · '+a.error:''}. `:'';
 if(r){
  const same=Array.isArray(r.bounds)&&r.bounds.length===4&&r.bounds.every((value,i)=>Math.abs(value-boundsInput()[i])<1e-9);
  const layer=r.layer||{};
  text+='Общая карта РГИС Крыма, слой «Территориальные зоны ПЗЗ». ';
  if(layer.received_at)text+=`Ответ ${layer.http_status?`HTTP ${layer.http_status} · `:''}${reconDate(layer.received_at)}: ${layer.count??'число не установлено'} объектов. `;
  text+=layer.count===0?'Источник подключён, зоны не получены. ':layer.count>0?'Полученные зоны доступны для сопоставления. ':'Зоны выбранной области ещё не получены. ';
  text+=same?'Область совпадает с выбранной. ':'Полученные сведения относятся к другой области; для выбранной области они не применяются. ';
  if(layer.error)text+='Ошибка получения: '+layer.error+'. ';
  if(layer.server_clock_warning)text+='Дата ответа сервера не принята как дата актуальности. ';
  text+='Полное покрытие и действующая редакция не подтверждены. Полученные границы требуют сверки с регламентами для выбранной цели. После обновления пересчитайте контуры; можно отключить обновления НСПД и торгов.';
 }else text+='Подключён официальный источник: слой «Территориальные зоны ПЗЗ» общей карты РГИС Крыма. Получите зоны выбранной области. Полное покрытие и действующая редакция не подтверждены.';
 el('pzz-status').textContent=text;
}
async function refreshPzz(){
 pzzStatusData=await api('/api/pzz');
 drawPzzStatus();
}
el('pzz-run').addEventListener('click',async()=>{
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 el('pzz-status').textContent='Получение территориальных зон ПЗЗ из официальной РГИС Крыма для выбранной области…';
 try{
  const job=await api('/api/pzz',{bounds:boundsInput()});let state;
  do{await new Promise(resolve=>setTimeout(resolve,1000));state=await api('/api/jobs/'+job.job_id);await refreshPzz()}while(state.state==='running');
  await refreshRecon();if(state.state==='error')throw Error(state.error);
 }catch(error){el('pzz-status').textContent+=' Не выполнено: '+error.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);if(reconStale||!sameReconBounds())document.querySelectorAll('[data-recon-save]').forEach(b=>b.disabled=true)}
});
refreshPzz().catch(error=>el('pzz-status').textContent='Проверка региональных ПЗЗ не выполнена: '+error.message);
