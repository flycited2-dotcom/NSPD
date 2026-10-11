let currentTorgiDocs=null;
let torgiPdfLocalData=null;
const torgiPdfLocalViews=new Map();
function installTorgiPdfLocalControls(prefix,run){
 const anchor=el(prefix+'-reprocess');
 if(!anchor?.parentElement||typeof document==='undefined'||!document.createElement)return;
 const row=document.createElement('div');row.className='row';
 const button=document.createElement('button');button.id=prefix+'-pdf-local';button.type='button';button.className='button secondary';button.textContent='Дочитать 3 сохранённых PDF';button.disabled=true;button.addEventListener('click',run);
 const label=document.createElement('label'),retry=document.createElement('input');retry.id=prefix+'-pdf-local-retry';retry.type='checkbox';retry.checked=false;
 label.append(retry,document.createTextNode(' Повторить ошибки локального дочитывания PDF'));
 const status=document.createElement('p');status.id=prefix+'-pdf-local-status';status.className='hint';status.setAttribute('role','status');status.style.whiteSpace='pre-line';
 status.textContent='Локальное дочитывание: до 3 сохранённых PDF, первые 120 страниц каждого файла. Новые файлы не скачиваются; дата получения и SHA-256 сохраняются. OCR сканов выполняется отдельно.';
 row.append(button,label);anchor.parentElement.insertAdjacentElement('afterend',row);row.insertAdjacentElement('afterend',status);
 retry.addEventListener('change',()=>restoreTorgiPdfLocalControls());
}
function torgiPdfLocalFiles(catalog,lotId=''){
 if(!lotId)return Object.values(catalog?.files||{});
 const keys=new Set((catalog?.cards||[]).filter(card=>card.lot_id===lotId&&card.state==='received').flatMap(card=>(card.attachments||[]).map(attachment=>attachment.key)));
 return Object.entries(catalog?.files||{}).filter(([key])=>keys.has(key)).map(([,file])=>file);
}
function drawTorgiPdfLocalStatus(prefix,data,files=[],selected=false){
 torgiPdfLocalViews.set(prefix,{data,files,selected});
 const status=el(prefix+'-pdf-local-status'),button=el(prefix+'-pdf-local'),retry=el(prefix+'-pdf-local-retry');
 if(!status||!button||!retry)return;
 const summary=(selected?data.selected_pdf_local_summary:data.pdf_local_summary)||{};
 const remaining=summary.remaining??(selected?data.selected_pdf_local_remaining:data.pdf_local_remaining)??0;
 const retryRemaining=summary.retry_remaining??(selected?data.selected_pdf_local_retry_remaining:data.pdf_local_retry_remaining)??remaining;
 const unread=summary.unread_pages??files.filter(file=>file.format==='pdf'&&file.state==='read').reduce((sum,file)=>sum+Math.max(0,(Number(file.total_pages)||0)-(Number(file.processed_pages)||0)),0);
 const limited=summary.limited_files??files.filter(file=>file.format==='pdf'&&file.state==='read'&&file.total_pages>120&&file.processed_pages>=120).length;
 const stale=Boolean(data.search_stale),attempt=data.pdf_local_attempt,busy=typeof running!=='undefined'&&running;
 let text=`${selected?'Выбранный лот: ':''}сохранённых PDF для дочитывания ${remaining}; с учётом явного повтора ошибок ${retryRemaining}. Непрочитанных страниц в сохранённых PDF: ${unread}. `;
 if(limited)text+=`Файлов с непрочитанными страницами за пределом 120: ${limited}; они требуют отдельного чтения. `;
 text+='Один запуск — до 3 PDF, первые 120 страниц каждого. Дата получения и SHA-256 сохраняются; новые файлы не скачиваются. Сканы требуют отдельного OCR. ';
 if(stale)text+='Поиск изменился: обновите карточки документов перед дочитыванием. ';
 if(attempt){const states={running:'выполняется',done:'завершена',partial:'завершена с ошибками',error:'ошибка',interrupted:'прервана'};text+=`Последнее локальное дочитывание: ${states[attempt.state]||attempt.state} · ${attempt.processed??0}/${attempt.requested??0}${attempt.lot_id?' · лот '+attempt.lot_id:''}${attempt.errors?' · ошибок '+attempt.errors:''}${attempt.error?' · '+docErrorText(attempt.error):''}${attempt.network_requests!==undefined?' · сетевых запросов '+attempt.network_requests:''}. `;}
 status.textContent=text;
 button.disabled=Boolean(busy||!data.result?.id||stale||(retry.checked?retryRemaining:remaining)<=0);
 retry.disabled=Boolean(busy);
}
function restoreTorgiPdfLocalControls(){
 for(const [prefix,view] of torgiPdfLocalViews)drawTorgiPdfLocalStatus(prefix,view.data,view.files,view.selected);
}
function torgiPdfUnreadInfo(file){
 if(file.format!=='pdf'||file.unit!=='pdf_page')return '';
 const unread=Math.max(0,(Number(file.total_pages)||0)-(Number(file.processed_pages)||0));
 const attempt=file.local_pdf_attempt,error=attempt?.state==='error'&&attempt.source_sha256===file.sha256?attempt.error:null;
 return `<p>Непрочитанных страниц: ${unread}.${file.total_pages>120&&file.processed_pages>=120?' Остаток за пределом 120 страниц требует отдельного чтения.':''}${file.local_pdf_read_at&&file.local_pdf_source_sha256===file.sha256?'<br>Локально дочитан: '+escapeHtml(file.local_pdf_read_at)+'; дата скачивания сохранена.':''}${error?'<br>Ошибка локального дочитывания: '+escapeHtml(docErrorText(error))+'. Прежний результат сохранён.':''}</p>`;
}
const docErrorText=x=>/timed out|Timeout/i.test(String(x||''))?'Источник не завершил загрузку за отведённое время. Полученные файлы сохранены; очередь можно продолжить отдельно.':String(x||'');
const fileState={pending:'В очереди',read:'Файл обработан',unsupported:'Формат не читается автоматически',inactive:'Неактивное вложение',oversized:'Превышает лимит размера',rejected:'Чтение отклонено',error:'Ошибка загрузки'};
function torgiFileState(file){
 const limit=(file.format==='pdf'?16:8)*1024*1024;
 if(['unsupported','oversized'].includes(file.state)&&file.eligible&&['pdf','docx','doc','rtf','jpg','jpeg','png'].includes(file.format)&&file.declared_size<=limit)return file.received_at?'Сохранён; ожидает чтения':'Ещё не загружено';
 return file.state==='oversized'?'Превышает '+(file.format==='pdf'?16:8)+' МБ':fileState[file.state]||escapeHtml(file.state);
}
function torgiBodyInfo(file){
 if(file.unit==='doc_body')return `Основной текст DOC: ${Number(file.body_text_characters).toLocaleString('ru-RU')} символов; частей текста: ${file.piece_count}. Страницы и изображения не восстановлены. Свойства правок и скрытого текста не проверены.`;
 if(file.unit==='rtf_body')return `Основной текст RTF: ${Number(file.body_text_characters).toLocaleString('ru-RU')} символов; кодировка: ${escapeHtml(file.encoding)}. Страницы, оформление и изображения не восстановлены.`;
 return 'Основной текст DOCX, абзацев: '+file.paragraph_count+(file.text_reader==='streaming'?' · чтение по частям, XML: '+Number(file.main_xml_bytes).toLocaleString('ru-RU')+' байт':'')+'. Страницы и изображения не восстановлены.';
}
function egrnTableInfo(file,card,lots=currentTorgi?.lots||[]){
 return (file.egrn_tables||[]).map(t=>{const lot=lots.find(l=>l.id===card.lot_id);const match=lot?.cadastral_numbers.includes(t.cadastral_number);return `<details><summary>Граница ЕГРН ${escapeHtml(t.label)} · строк: ${t.points.length} · ${t.state==='review_required'?'для сверки':'отклонена'}</summary><p>Раздел 3.2 · страницы PDF: ${escapeHtml(t.pages.join(', '))}; листов раздела по документу: ${t.section_sheets_expected}.<br>Система координат в источнике: ${escapeHtml(t.crs_label)}; X/Y, метры. Параметры геопривязки не установлены.</p><p>${match===true?'Номер таблицы совпадает со структурированным номером лота.':match===false?'Номер таблицы не совпадает со структурированным номером лота; граница ему не назначается.':'Номер лота не загружен для сравнения.'}</p>${file.associations.some(a=>a.lot_id===card.lot_id&&a.scope==='lot'&&!a.inactive)?'<p>Файл приложен к этому лоту. Принадлежность и актуальность границы требуют сверки с карточкой и текущими сведениями.</p>':'<p>Общий документ извещения: граница не назначается этому лоту автоматически.</p>'}${t.local_area_m2!=null?'<p>Площадь контура в исходных X/Y: '+Number(t.local_area_m2).toLocaleString('ru-RU')+' м².</p>':''}${t.issues.length?'<p>'+t.issues.map(i=>escapeHtml(i.reason)+(i.page?' · стр. '+i.page:'')).join('<br>')+'</p>':''}${typeof schemePreview==='function'?schemePreview(t):''}<div class="table-wrap"><table><thead><tr><th>Точка</th><th>X, м</th><th>Y, м</th><th>Погрешность по документу, м</th><th>Страница</th></tr></thead><tbody>${t.points.map(p=>`<tr><td>${escapeHtml(p.label)}</td><td>${p.x}</td><td>${p.y}</td><td>${p.accuracy_stated_m}</td><td>${p.page}</td></tr>`).join('')}</tbody></table></div><p>Исходные координаты из исторической выписки. Географическая граница, действующие права и свободность земли не подтверждены.</p></details>`;}).join('');
}
function torgiImageInfo(file){
 const i=file.image;if(!i)return '';
 return `<p>Изображение ${escapeHtml(i.source_format)}: ${i.source_width} × ${i.source_height} пикселей. Прочитан один полный кадр; результат распознавания показан отдельно.</p><a href="/api/torgi/documents/image?file=${encodeURIComponent(file.key)}" target="_blank" rel="noopener noreferrer">Открыть изображение для сверки</a><details><summary>Показать изображение</summary><img loading="lazy" style="max-width:100%;max-height:650px;object-fit:contain" alt="Изображение вложения для визуальной сверки" src="/api/torgi/documents/image?file=${encodeURIComponent(file.key)}"></details>`;
}
function torgiOcrInfo(file){
 const o=file.visual_ocr;if(!o||o.source_sha256!==file.sha256)return '';
 return `<details><summary>OCR: обработано ${o.pages.filter(p=>p.state==='received').length} страниц/изображений · подсказок номеров ${o.pages.reduce((n,p)=>n+(p.mentions?.length||0),0)}</summary><p>Номера распознаны локально; совпадение в двух чтениях не подтверждает точность, предмет лота или свободность земли. Координаты OCR не преобразуются в границу.</p>${o.pages.map(p=>`<p><b>${file.unit==='image'?'Изображение':'Страница '+p.page}</b> · ${escapeHtml(p.processed_at)}${p.cached?' · сохранённое распознавание':''}<br>${p.state==='error'?escapeHtml(p.error):`${p.sparse?'Текста мало; нужна визуальная проверка.<br>':''}${p.coordinate_label_detected?'Есть обозначение координат; граница не извлечена.<br>':''}${p.mentions.map(m=>escapeHtml(m.cadastral_number)+' · '+(m.agreement==='both'?'совпало в двух чтениях':'одно чтение')).join('<br>')||'Кадастровые номера не распознаны; их отсутствие не установлено.'}<br>${[0,1].map(v=>`<a href="/api/torgi/documents/image?file=${encodeURIComponent(file.key)}&page=${p.page}&view=${v}" target="_blank" rel="noopener noreferrer">Сверить скан · чтение ${v+1}</a>`).join(' · ')}`}</p>`).join('')}</details>`;
}
function torgiFileEvidence(file,card,lots=currentTorgi?.lots||[]){
 if(file.unit==='image')return torgiImageInfo(file)+torgiOcrInfo(file);
 return `<p>${file.unit==='pdf_page'?'PDF: '+file.processed_pages+' из '+file.total_pages+' страниц; без достаточного текста: '+escapeHtml((file.image_or_sparse_pages||[]).join(', ')||'нет отмеченных'):torgiBodyInfo(file)}</p>${torgiPdfUnreadInfo(file)}<details><summary>Номера из текстового слоя: ${file.mentions?.length||0}</summary><p>${(file.mentions||[]).map(m=>escapeHtml(m.cadastral_number)+(file.unit==='pdf_page'?' · стр. '+escapeHtml(m.pages.join(', ')):' · основной текст '+escapeHtml(file.format.toUpperCase()))).join('<br>')||'Не найдены; отсутствие номеров этим не установлено.'}</p></details><p>Таблиц планировки: ${file.tables?.length||0}; таблиц границы ЕГРН: ${file.egrn_tables?.length||0}. Геопривязки нет; номер из общего извещения не назначается этому лоту автоматически.</p>${egrnTableInfo(file,card,lots)}${torgiImageInfo(file)}${torgiOcrInfo(file)}`;
}
async function refreshTorgiDocs(){
 const d=await api('/api/torgi/documents');torgiPdfLocalData=d;
 const {result:r,attempt:a,reading_attempt:b,reprocess_attempt:p,files_to_reprocess:n,search_stale:stale,cards_remaining:c,files_remaining:f,visual_attempt:v,visual_remaining:q}=d;currentTorgiDocs=r;
 drawTorgiPdfLocalStatus('torgi-doc',d,torgiPdfLocalFiles(r));
 if(!r){el('torgi-doc-status').textContent='Карточки вложений ещё не загружены. В текущем поиске осталось карточек: '+c;return;}
 const files=Object.values(r.files||{});
 el('torgi-doc-status').textContent=`Карточек получено: ${r.cards.filter(x=>x.state==='received').length}; ошибок карточек: ${r.cards.filter(x=>x.state==='error').length}; файлов: ${files.length}; обработано: ${files.filter(x=>x.state==='read').length}; ошибок/отклонений файлов: ${files.filter(x=>x.state==='error'||x.state==='rejected').length}; карточек в очереди: ${c}; файлов в очереди: ${f}.\nСохранённых файлов для нового разбора: ${n??0}; сканов в очереди OCR: ${q??0}; таблиц границы ЕГРН: ${files.filter(x=>x.state==='read').reduce((sum,x)=>sum+(x.egrn_tables?.length||0),0)}.\n${r.warning}${stale?'\nПоиск изменился; загрузите карточки для актуализации.':''}${a?.state==='running'?'\nКарточки: '+a.processed+' / '+a.requested:''}${b?.state==='running'?'\nФайлы: '+b.processed+' / '+b.requested:''}${p?.state==='running'?'\nЛокальное перечтение: '+p.processed+' / '+p.requested:''}${a?.error?'\nПопытка карточек: '+docErrorText(a.error):''}${b?.error?'\nПопытка чтения: '+docErrorText(b.error):''}${p?.error?'\nЛокальное перечтение: '+docErrorText(p.error):''}${v?.state==='running'?'\nOCR: '+v.processed+' / '+v.requested:''}${v?.error?'\nOCR: '+docErrorText(v.error):''}${a?.state==='interrupted'||b?.state==='interrupted'||p?.state==='interrupted'||v?.state==='interrupted'?'\nПоследняя попытка прервана.':''}`;
 el('torgi-doc-export').classList.remove('hidden');
 el('torgi-doc-results').innerHTML=r.cards.map(card=>`<details><summary>Лот ${escapeHtml(card.lot_id)} · ${card.state==='received'?'вложений: '+card.attachments.length:'ошибка карточки'}</summary>${card.state==='error'?'<p>'+escapeHtml(docErrorText(card.error))+'</p>':`<p><a href="${escapeHtml(card.lot_url)}" target="_blank" rel="noopener noreferrer">Официальная карточка</a> · получена ${escapeHtml(card.received_at)}<br>Статус карточки: ${escapeHtml(statusName(card.card_status))}. Это дата проверки карточки, а не дата актуальности файла.</p>${card.attachments.map(x=>{
 const file=r.files[x.key];return `<details><summary>${escapeHtml(x.file_name)} · ${x.scope==='notice'?'извещение, общий файл':'вложение лота'} · ${torgiFileState(file)}</summary><p><a href="${escapeHtml(x.url)}" target="_blank" rel="noopener noreferrer">Официальный файл</a><br>Тип источника: ${escapeHtml(x.type_name||'не указан')} · размер карточки: ${x.declared_size} байт<br>Загружен в источник: ${escapeHtml(x.source_upload_date||'не указано')}${x.inactive?' · отмечен неактивным':''}<br>Файл связан с лотами: ${escapeHtml(file.associations.map(a=>a.lot_id+' ('+(a.scope==='notice'?'извещение':'лот')+')').join('; '))}</p>${file.metadata_conflict?'<p><b>Для одного fileId получены разные метаданные; автоматическое чтение остановлено.</b></p>':''}${file.error?'<p>'+escapeHtml(docErrorText(file.error))+'</p>':''}${file.received_at?'<p>Скачан: '+escapeHtml(file.received_at)+'<br>Локальный SHA-256: '+escapeHtml(file.sha256)+'</p>':''}<p>Алгоритм хеша карточки не указан; он не объявляется локальным SHA-256. Электронная подпись не проверена.</p>${file.state==='read'?torgiFileEvidence(file,card):''}</details>`;
 }).join('')}`}</details>`).join('')||'<p>Карточек нет. Отсутствие документов этим не установлено.</p>';
}
async function runTorgiDocs(read=false,local=false,visual=false){
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{
  const args={retry_errors:el('torgi-doc-retry').checked};
  if(read||local||visual)args.id=currentTorgiDocs?.id;else args.search_id=(await api('/api/torgi')).result?.id;
  const j=await api(visual?'/api/torgi/documents/ocr':local?'/api/torgi/documents/reprocess':read?'/api/torgi/documents/read':'/api/torgi/documents',args);let s;
  do{await new Promise(r=>setTimeout(r,1000));s=await api('/api/jobs/'+j.job_id);await refreshTorgiDocs()}while(s.state==='running');
  if(s.state==='error')throw Error(s.error);
 }catch(e){el('torgi-doc-status').textContent+='\nНе завершено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);restoreTorgiPdfLocalControls()}
}
async function runTorgiPdfLocal(){
 if(running)return;
 if(!currentTorgiDocs?.id||torgiPdfLocalData?.search_stale){el('torgi-doc-status').textContent+='\nДочитывание не начато: каталог или поиск изменился; обновите карточки.';return;}
 running=true;document.querySelectorAll('button').forEach(button=>button.disabled=true);restoreTorgiPdfLocalControls();
 try{
  const job=await api('/api/torgi/documents/continue',{id:currentTorgiDocs.id,retry_errors:el('torgi-doc-pdf-local-retry').checked});let state;
  do{await new Promise(resolve=>setTimeout(resolve,1000));state=await api('/api/jobs/'+job.job_id);await refreshTorgiDocs()}while(state.state==='running');
  if(typeof refreshRecon==='function')await refreshRecon();if(state.state==='error')throw Error(state.error);
 }catch(error){el('torgi-doc-status').textContent+='\nЛокальное дочитывание не завершено: '+docErrorText(error.message)}
 finally{running=false;document.querySelectorAll('button').forEach(button=>button.disabled=false);restoreTorgiPdfLocalControls();if(typeof reconStale!=='undefined'&&(reconStale||!sameReconBounds()))document.querySelectorAll('[data-recon-save]').forEach(button=>button.disabled=true)}
}
installTorgiPdfLocalControls('torgi-doc',runTorgiPdfLocal);
el('torgi-doc-cards').addEventListener('click',()=>runTorgiDocs());el('torgi-doc-read').addEventListener('click',()=>runTorgiDocs(true));
el('torgi-doc-reprocess').addEventListener('click',()=>runTorgiDocs(false,true));
el('torgi-doc-ocr').addEventListener('click',()=>runTorgiDocs(false,false,true));
refreshTorgiDocs().catch(e=>el('torgi-doc-status').textContent=e.message);
