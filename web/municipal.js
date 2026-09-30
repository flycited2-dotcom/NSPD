let currentMunicipal=null, municipalStale=false;
const municipalKinds={regulation:'Общий регламент',restriction:'Сервитут / ограничение',notice_or_auction:'Извещение / торги — по заголовку',planning:'Планировка',land_document:'Земельный документ'};
const municipalStates={pending:'Ещё не прочитан',read:'Файл обработан',rejected:'Чтение отклонено',unsupported:'Формат не поддержан'};
function municipalTable(){
 const r=currentMunicipal;if(!r)return;
 const only=el('municipal-area-only').checked;
 const rows=r.items.filter(x=>!only||(!municipalStale&&x.mentions_in_area?.length));
 el('municipal-results').innerHTML=rows.length?rows.map(x=>`<details><summary>${escapeHtml(x.title)} · ${municipalStates[x.state]||escapeHtml(x.state)}</summary>${x.content_conflicts?.length?'<p><b>По другой ссылке с другим заголовком получен побайтно такой же PDF. Нужна сверка содержания.</b></p><ul>'+x.content_conflicts.map(r=>'<li><a href="'+escapeHtml(r.url)+'" target="_blank" rel="noopener noreferrer">'+escapeHtml(r.title)+'</a></li>').join('')+'</ul>':''}${x.listing_conflict?'<p><b>Один файл связан в источнике с разными заголовками. Нужна сверка содержания.</b></p><ul>'+x.listing_references.map(r=>'<li>'+escapeHtml(r.title)+'</li>').join('')+'</ul>':''}<p>${municipalKinds[x.kind]||'Документ'} · ${escapeHtml(x.format.toUpperCase())}</p><p><a href="${escapeHtml(x.url)}" target="_blank" rel="noopener noreferrer">Официальный документ</a> · <a href="${escapeHtml(x.parent_url)}" target="_blank" rel="noopener noreferrer">Перечень / публикация</a></p><p>В перечне: ${escapeHtml(x.listed_at)}${x.received_at?'<br>Файл получен: '+escapeHtml(x.received_at):''}${x.date_mentions?.length?'<br>Даты в заголовке: '+escapeHtml(x.date_mentions.join(', ')):''}</p>${x.error||x.last_error?`<p>Ошибка: ${escapeHtml(x.error||x.last_error)} ${escapeHtml(x.last_error_at||'')}</p>`:''}${x.state==='read'?`<p>${x.total_pages?'PDF: '+x.processed_pages+' из '+x.total_pages+' страниц. ':''}${x.text_layer_complete?'Текстовый слой обработан.':'Текст неполный; сканы и оставшиеся страницы требуют чтения.'}${x.image_or_sparse_pages?.length?'<br>Без достаточного текста: страницы '+escapeHtml(x.image_or_sparse_pages.join(', ')):''}${x.unread_pages?'<br>Осталось страниц сверх лимита: '+x.unread_pages:''}</p><details><summary>Кадастровые упоминания: ${x.mentions?.length||0}</summary><p>${(x.mentions||[]).map(n=>escapeHtml(n.cadastral_number)+' ('+[n.in_title?'заголовок':'',n.pages.length?'стр. '+escapeHtml(n.pages.join(', ')):''].filter(Boolean).join('; ')+')').join('; ')||'в полученном тексте не найдены'}</p></details>${x.coordinate_label_pages?.length?'<p>Обозначение координат: страницы '+escapeHtml(x.coordinate_label_pages.join(', '))+'. Координаты не преобразованы; схема на карту не нанесена.</p>':''}${x.attachments?.length?'<p>Обнаружено вложений: '+x.attachments.length+'. Они добавлены в очередь.</p>':''}`:''}${x.mentions_in_area?.length&&!municipalStale?'<p><b>Упоминание номера участка из обследуемой области.</b> Это может быть сосед или пример; предмет документа и применимость не подтверждены.</p>':''}<p>Соответствие содержания заголовку, геометрия схемы и права не подтверждены.</p></details>`).join(''):'<p>Документов по фильтру нет. Отсутствие публикаций или заявлений этим не установлено.</p>';
}
async function refreshMunicipal(){
 const {result:r,attempt:a,current_survey_id:s}=await api('/api/municipal');
 currentMunicipal=r;
 if(!r){el('municipal-status').textContent=a?.error||'Каталог ещё не загружен.';return;}
 municipalStale=r.survey_id!==s;
 const pending=r.items.filter(x=>x.state==='pending').length,read=r.items.filter(x=>x.state==='read').length,full=r.items.filter(x=>x.text_layer_complete).length,rejected=r.items.filter(x=>x.state==='rejected').length;
 el('municipal-status').textContent=`Перечни от ${r.catalog_at}. Документов: ${r.items.length}; обработано: ${read}; текст без отмеченных пропусков: ${full}; отклонено: ${rejected}; в очереди: ${pending}.\n${r.warning}${municipalStale?'\nОбласть изменилась; прочитайте следующую порцию для пересопоставления упоминаний.':''}${a?.state==='running'?'\nОбработка: '+a.processed+' / '+a.requested:''}${a?.state==='error'?'\nПоследняя попытка не завершена: '+a.error:''}${a?.state==='interrupted'?'\nПоследняя попытка прервана.':''}`;
 el('municipal-export').classList.remove('hidden');municipalTable();
}
async function runMunicipal(read=false){
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{const j=await api(read?'/api/municipal/read':'/api/municipal',read?{id:currentMunicipal?.id}:{});let s;
 do{await new Promise(r=>setTimeout(r,1000));s=await api('/api/jobs/'+j.job_id);await refreshMunicipal()}while(s.state==='running');
 if(s.state==='error')throw Error(s.error);
 }catch(e){el('municipal-status').textContent+='\nНе завершено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false)}
}
el('municipal-catalog').addEventListener('click',()=>runMunicipal());
el('municipal-read').addEventListener('click',()=>runMunicipal(true));
el('municipal-area-only').addEventListener('change',municipalTable);
refreshMunicipal().catch(e=>el('municipal-status').textContent=e.message);
