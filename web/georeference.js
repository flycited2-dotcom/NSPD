let currentGeo=null,currentGeoSchemes=null,geoAreaStale=false;
const geoState={consistent_with_reference:'Проверка вершин и границ согласуется с контрольным объектом.',vertices_consistent_boundary_differs:'Положение вершин согласуется; границы различаются. Создан только предварительный слой для исследования.',reference_disagrees:'Положение контрольных вершин не согласуется; слой схемы не создан.'};
const gmap=L.map('georef-map').setView([44.994,34.205],14),gdraw=L.featureGroup().addTo(gmap);
function drawGeoreference(){
 gdraw.clearLayers();el('georef-count').textContent='';const r=currentGeo;if(!r||r.schemes_id!==currentGeoSchemes)return;
 const inside=el('georef-inside').checked&&!geoAreaStale;
 const features=r.preview_geojson.features.filter(f=>!inside||f.properties.intersects_survey_area===true);
 if(!inside)L.geoJSON(r.reference_geojson,{style:{color:'#555e66',weight:3,fillOpacity:.12},onEachFeature:(f,l)=>l.bindPopup('Контроль НСПД: '+escapeHtml(r.cadastral_number))}).addTo(gdraw);
 L.geoJSON({type:'FeatureCollection',features},{style:f=>({color:f.properties.reference_table?'#2b6b9a':'#b87b22',weight:2,fillOpacity:.2}),onEachFeature:(f,l)=>l.bindPopup(escapeHtml(f.properties.label)+'<br>Предварительная привязка; права и доступность не проверены.')}).addTo(gdraw);
 if(r.survey_bounds&&!geoAreaStale){const [w,s,e,n]=r.survey_bounds;L.rectangle([[s,w],[n,e]],{color:'#555e66',dashArray:'6 6',fill:false,weight:1}).addTo(gdraw)}
 if(inside&&r.survey_bounds){const [w,s,e,n]=r.survey_bounds;gmap.fitBounds([[s,w],[n,e]],{padding:[20,20]})}
 else if(features.length){const layer=L.geoJSON({type:'FeatureCollection',features});gmap.fitBounds(layer.getBounds(),{padding:[24,24]})}
 el('georef-count').textContent='Показано таблиц схемы: '+features.length+(inside?' · фильтр сохранённой области':'');
}
async function refreshGeoreference(){
 const {result:r,attempt:a,choices:c,current_schemes_id:s,current_survey_id:u}=await api('/api/georeference');currentGeo=r;currentGeoSchemes=s;
 const selected=el('georef-document').value;el('georef-document').innerHTML=c.map(d=>`<option value="${escapeHtml(d.document_id)}">${escapeHtml(d.title)}</option>`).join('');if(c.some(x=>x.document_id===selected))el('georef-document').value=selected;
 geoAreaStale=!!r&&r.survey_id!==u;el('georef-inside').disabled=!r?.survey_id||geoAreaStale;
 if(geoAreaStale)el('georef-inside').checked=false;
 const stale=!!r&&r.schemes_id!==s;
 el('georef-status').textContent=r?`${geoState[r.state]||r.state}\n${r.title}\nКонтроль: ${r.cadastral_number}, получен ${r.reference.received_at}; PDF получен ${r.source_pdf_received_at}.\nРасхождение исходных вершин до ближайшей контрольной границы: до ${r.vertex_max_distance_m} м. Расхождение границ при выборке с шагом до ${r.boundary_sampling_m} м: ${r.boundary_distance_m} м; проверены части: ${r.part_checks.length}. Внутренних колец: PDF ${r.preview_hole_count}, НСПД ${r.reference_hole_count}.\nЗаявленная точность операции преобразования: ${r.operation_expected_accuracy_m} м — это не установленная точность участка. СК-63: проверяемый вариант ${r.source_crs}; ${r.operation_code}. Сдвиг под геометрию не подбирался.\nТаблиц в сохранённой области: ${r.intersecting_tables??'не определено'}. ${r.warning}${!r.boundary_consistent?'\nЭквивалентность границ не подтверждена; схема не заменяет текущую геометрию НСПД.':''}${stale?'\nТаблицы изменились; повторите проверку.':''}${geoAreaStale?'\nОбласть обследования изменилась; прежние пространственные совпадения устарели.':''}`:'Подходящих документов: '+c.length+'. Проверка использует таблицы с подписанным кадастровым объектом и СК-63.';
 if(a?.state==='running')el('georef-status').textContent+='\nПроверка выполняется…';
 if(a?.state==='error')el('georef-status').textContent+='\nПоследняя попытка: '+a.error+' Предыдущий результат сохранён.';
 if(a?.state==='interrupted')el('georef-status').textContent+='\nПоследняя попытка прервана.';
 if(r)el('georef-export').classList.remove('hidden');drawGeoreference();
}
async function runGeoreference(){
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{const j=await api('/api/georeference',{schemes_id:currentGeoSchemes,document_id:el('georef-document').value});let s;
 do{await new Promise(r=>setTimeout(r,1000));s=await api('/api/jobs/'+j.job_id);await refreshGeoreference()}while(s.state==='running');if(s.state==='error')throw Error(s.error);
 }catch(e){el('georef-status').textContent+='\nНе завершено: '+e.message}finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false)}
}
el('georef-run').addEventListener('click',runGeoreference);el('georef-inside').addEventListener('change',drawGeoreference);
el('georef-fit-control').addEventListener('click',()=>{
 if(!currentGeo||currentGeo.schemes_id!==currentGeoSchemes)return;
 el('georef-inside').checked=false;drawGeoreference();
 gmap.fitBounds(L.geoJSON(currentGeo.reference_geojson).getBounds(),{padding:[24,24]});
});
el('georef-fit-scheme').addEventListener('click',()=>{el('georef-inside').checked=false;drawGeoreference()});
refreshGeoreference().catch(e=>el('georef-status').textContent=e.message);
