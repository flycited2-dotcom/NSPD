let currentSchemes=null;
const schemePurpose={formed_label:'Обозначение образуемого участка',public_use_context:'Раздел общего пользования — по тексту',unknown:'Назначение не определено'};
function unreadableInfo(d){return (d.unreadable_tables||[]).map(t=>'<p>Не прочитана таблица '+escapeHtml(t.label)+' · стр. '+t.heading_page+': '+t.issues.map(x=>escapeHtml(x.reason)).join('; ')+'</p>').join('');}
function schemePreview(t){
 if(!t.outline_xy)return '';
 const pts=t.outline_xy,xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]);
 const loX=Math.min(...xs),hiX=Math.max(...xs),loY=Math.min(...ys),hiY=Math.max(...ys),scale=300/Math.max(hiX-loX,hiY-loY,1);
 const offsetX=(360-(hiX-loX)*scale)/2,offsetY=(360-(hiY-loY)*scale)/2;
 const points=pts.map(p=>`${offsetX+(p[0]-loX)*scale},${360-offsetY-(p[1]-loY)*scale}`).join(' ');
 return `<svg viewBox="0 0 360 360" style="width:360px;max-width:100%;background:#f2f5ef" role="img" aria-label="Контур ${escapeHtml(t.label)} в координатах таблицы"><polygon points="${points}" fill="#d5e4d3" stroke="#315c46" stroke-width="2"/><text x="12" y="20" fill="#315c46">Y ↑ · X →</text><text x="12" y="344" fill="#315c46">Контур в X/Y · геопривязки нет</text></svg>`;
}
async function refreshSchemes(){
 const {result:r,attempt:a,current_catalog_id:c,remaining:n}=await api('/api/schemes');currentSchemes=r;
 if(!r){el('scheme-status').textContent=a?.error||'Таблицы ещё не извлекались.';return;}
 const tables=r.documents.flatMap(d=>d.tables||[]),errors=r.documents.filter(d=>d.state==='error').length;
 el('scheme-status').textContent=`Документов обработано: ${r.documents.length}; таблиц: ${tables.length}; контуров для сверки: ${tables.filter(t=>t.state==='review_required').length}; ошибок файлов: ${errors}; в очереди: ${n}.\n${r.warning}${r.catalog_id!==c?'\nКаталог изменился; запустите следующую порцию для актуализации.':''}${a?.state==='running'?'\nОбработка порции: '+a.processed+' / '+a.requested:''}${a?.state==='error'?'\nНе завершено: '+a.error:''}${a?.state==='interrupted'?'\nПоследняя попытка прервана.':''}`;
 el('scheme-export').classList.remove('hidden');
 el('scheme-results').innerHTML=r.documents.filter(d=>d.tables?.length||d.unparsed_coordinate_pages?.length||d.unreadable_tables?.length||d.state==='error').map(d=>`<details><summary>${escapeHtml(d.title)} · таблиц: ${d.tables?.length||0}</summary><p><a href="${escapeHtml(d.url)}" target="_blank" rel="noopener noreferrer">Официальный PDF</a><br>PDF получен: ${escapeHtml(d.source_received_at)}<br>Таблицы извлечены: ${escapeHtml(d.processed_at)}</p>${d.state==='error'?'<p>'+escapeHtml(d.error)+'</p>':`<p>Текст: ${d.processed_pages} из ${d.total_pages} страниц; сканы: ${escapeHtml((d.image_or_sparse_pages||[]).join(', ')||'нет отмеченных')}. Сканы не используются для автоматического извлечения координат.</p><p>Указания СК: ${d.crs_mentions?.map(x=>escapeHtml(x.label)+' (стр. '+x.page+')').join('; ')||'не найдены'}. Параметры преобразования не подтверждены.</p>${d.unparsed_coordinate_pages?.length?'<p>Числовые пары вне поддержанного формата: страницы '+escapeHtml(d.unparsed_coordinate_pages.join(', '))+'. Требуют отдельного чтения.</p>':''}`}${unreadableInfo(d)}${d.content_conflicts?.length||d.listing_conflict?'<p><b>Конфликт заголовков источника. Применимость документа требует сверки.</b></p>':''}${(d.tables||[]).map(t=>`<details><summary>${escapeHtml(t.label)} · стр. ${escapeHtml(t.pages.join(', '))} · ${t.state==='rejected'?'контур отклонён':Number(t.local_area_m2).toLocaleString('ru-RU')+' м² по таблице'}</summary><p>${schemePurpose[t.purpose_hint]||'Назначение не определено'} · контекст: стр. ${t.context_page}. Это признак текста, а не правовая классификация.<br>${t.closure==='explicit'?'Замыкающая точка есть в таблице.':'Для предпросмотра последняя вершина соединена с первой; полноту кольца нужно сверить.'}</p>${t.issues.map(x=>'<p>'+escapeHtml(x.reason)+(x.page?' · стр. '+x.page:'')+(x.row?'<br>'+escapeHtml(x.row):'')+'</p>').join('')}${schemePreview(t)}<p>Оси соответствуют столбцам таблицы. Север и положение на местности не установлены. Площадь вычислена по исходным координатам, не подтверждает площадь или доступность участка.</p><details><summary>Исходные строки точек: ${t.points.length}</summary><table><thead><tr><th>Точка</th><th>X</th><th>Y</th><th>Страница</th></tr></thead><tbody>${t.points.map(p=>`<tr><td>${escapeHtml(p.label)}</td><td>${p.x}</td><td>${p.y}</td><td>${p.page}</td></tr>`).join('')}</tbody></table></details></details>`).join('')}</details>`).join('')||'<p>Поддержанные таблицы не найдены. Это не отсутствие схем или координат в документах.</p>';
}
async function runSchemes(){
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{
  const c=await api('/api/municipal');
  const j=await api('/api/schemes',{catalog_id:c.result?.id,retry_errors:el('scheme-retry').checked});let s;
  do{await new Promise(r=>setTimeout(r,1000));s=await api('/api/jobs/'+j.job_id);await refreshSchemes()}while(s.state==='running');
  if(s.state==='error')throw Error(s.error);
 }catch(e){el('scheme-status').textContent+='\nНе завершено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false)}
}
el('scheme-run').addEventListener('click',runSchemes);
refreshSchemes().catch(e=>el('scheme-status').textContent=e.message);
