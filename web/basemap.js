'use strict';
// OSM's web policy requires an actual page Referer. Only tile images send the
// origin; the rest of the application keeps its no-referrer response policy.
window.LandBasemap={bind(map,checkbox){
 const notice=document.createElement('p');notice.className='hint basemap-status';notice.setAttribute('role','status');
 map.getContainer().insertAdjacentElement('afterend',notice);
 let current=null;
 function off(){
  const old=current;current=null;if(old)map.removeLayer(old);
 }
 function update(){
  off();
  if(!checkbox.checked){notice.textContent='Фоновая карта отключена. Загруженные контуры и результаты поиска отображаются.';return;}
  notice.textContent='Загружается подложка OpenStreetMap…';
  const layer=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{
   maxZoom:19,referrerPolicy:'origin',keepBuffer:0,updateWhenIdle:true,
   attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  });
  current=layer;
  layer.on('tileerror',()=>{
   if(current!==layer)return;
   off();checkbox.checked=false;
   notice.textContent='Подложка OpenStreetMap не загрузилась и отключена. Контуры и поиск доступны. Автоматические повторы не выполняются.';
  });
  layer.on('load',()=>{if(current===layer)notice.textContent='Подложка OpenStreetMap загружена. Кадастровые сведения показываются отдельными слоями.';});
  layer.addTo(map);
 }
 checkbox.addEventListener('change',update);update();
 return {disable(){checkbox.checked=false;update();}};
}};
