let scanChoices=[],scanCatalog=null,scanDrafts=null;
function scanPages(){
 const doc=scanChoices.find(c=>c.document_id===el('scan-document').value),old=el('scan-page').value;
 el('scan-page').innerHTML=(doc?.pages||[]).map(n=>`<option value="${n}">Страница ${n}</option>`).join('');
 if(doc?.pages.includes(Number(old)))el('scan-page').value=old;
 el('scan-run').disabled=running||!doc;
}
function scanCellText(view){return view?view.cells.map(c=>c?.text||'∅').join(' | '):'Строка не найдена в этом чтении';}
function scanEvidence(pageIndex,tableIndex,rowIndex){
 const p=scanDrafts.pages[pageIndex],t=p.tables[tableIndex],r=t.rows[rowIndex];
 const current=scanChoices.find(c=>c.document_id===p.document_id&&c.source_sha256===p.source_sha256&&c.pages.includes(p.page));
 if(!current){el('scan-evidence').textContent='Источник изменился; выполните чтение текущей страницы.';return;}
 el('scan-evidence').innerHTML=`<h3>Страница ${p.page} · таблица скана ${t.ordinal} · строка ${r.ordinal}</h3><p>Выделена строка на исходных изображениях. Обозначение части и все значения сверяются по странице; совпадение OCR не подтверждает границу.</p>${p.source_views.map((v,i)=>{
 const b=r.views[i]?.bbox||r.bbox;
 const left=Math.max(0,Math.min(...t.rows.map(row=>row.bbox[0]))-.025),right=Math.min(1,Math.max(...t.rows.map(row=>row.bbox[2]))+.025),top=Math.max(0,b[1]-.008),bottom=Math.min(1,b[3]+.008);
 const url=`/api/municipal/ocr/page?document=${encodeURIComponent(p.document_id)}&page=${p.page}&view=${i}`;
 return `<details open><summary>Чтение ${i+1} · ${escapeHtml(scanCellText(r.views[i]))}</summary><svg viewBox="${left*v.width} ${top*v.height} ${(right-left)*v.width} ${(bottom-top)*v.height}" style="width:100%;display:block" role="img" aria-label="Увеличенная строка исходного скана"><image href="${url}" width="${v.width}" height="${v.height}"/><rect x="${b[0]*v.width}" y="${Math.max(0,b[1]-.002)*v.height}" width="${(b[2]-b[0])*v.width}" height="${(b[3]-b[1]+.004)*v.height}" fill="#ffb020" fill-opacity=".18" stroke="#b54b00" stroke-width="1"/></svg><details><summary>Вся страница с заголовками</summary><div style="position:relative"><img src="${url}" alt="Исходный скан страницы ${p.page}, чтение ${i+1}" style="display:block;width:100%;height:auto"><svg viewBox="0 0 ${v.width} ${v.height}" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none" aria-label="Положение распознанной строки"><rect x="${b[0]*v.width}" y="${Math.max(0,b[1]-.003)*v.height}" width="${(b[2]-b[0])*v.width}" height="${(b[3]-b[1]+.006)*v.height}" fill="#ffb020" fill-opacity=".25" stroke="#b54b00" stroke-width="3"/></svg></div></details></details>`;
 }).join('')}`;
 el('scan-evidence').scrollIntoView({block:'start'});
}
async function refreshScanTables(){
 const a=await api('/api/scan-tables');
 const opened=scanDrafts?.id===a.result?.id?new Set([...el('scan-results').querySelectorAll('details[data-scan-open][open]')].map(d=>d.dataset.scanOpen)):new Set();
 if(scanDrafts?.id!==a.result?.id||scanCatalog!==a.current_catalog_id){el('scan-evidence').replaceChildren();el('scan-review-editor').replaceChildren()}
 scanChoices=a.choices;scanCatalog=a.current_catalog_id;scanDrafts=a.result;scanExtras=a;
 const old=el('scan-document').value;
 el('scan-document').innerHTML=scanChoices.map(c=>`<option value="${escapeHtml(c.document_id)}">${escapeHtml(c.title)}</option>`).join('');
 if(scanChoices.some(c=>c.document_id===old))el('scan-document').value=old;scanPages();
 const pages=a.result?.pages||[],rows=pages.flatMap(p=>p.tables.flatMap(t=>t.rows)),same=rows.filter(r=>r.agreement==='same_literal_values').length;
 const cells=(a.cells?.rows||[]).filter(c=>pages.some(p=>p.document_id===c.document_id&&p.page===c.page&&scanFingerprint(p)===c.fingerprint)),reviews=a.reviews?.tables||[];
 el('scan-status').textContent=`Сканов с черновиками: ${pages.length}; строк координат: ${rows.length}; совпало без пропусков в двух чтениях: ${same}; с пропусками/различиями: ${rows.length-same}. Прицельно прочитано проблемных строк: ${cells.length}. Сохранённых сверок: ${reviews.length}; исходных контуров для проверки: ${reviews.filter(r=>!r.stale&&r.state==='local_preview').length}; отклонено: ${reviews.filter(r=>!r.stale&&r.state==='rejected').length}; устарело: ${reviews.filter(r=>r.stale).length}. Автоматические чтения требуют визуальной сверки; геопривязки нет.${a.result&&a.result.catalog_id!==scanCatalog?' Каталог изменился; сохранённые черновики относятся к прежней версии.':''}${a.attempt?.state==='running'?' Выполняется чтение выбранной страницы.':''}${a.attempt?.state==='error'?' Последняя попытка: '+a.attempt.error:''}${a.attempt?.state==='interrupted'?' Последняя попытка прервана.':''}${a.cells_attempt?.state==='error'?' Прицельное OCR: '+a.cells_attempt.error:''}${a.cells_attempt?.state==='running'?' Чтение ячеек выполняется.':''}${a.cells_attempt?.state==='done'?' Последняя порция: обработано '+a.cells_attempt.processed+', осталось '+a.cells_attempt.remaining+'.':''}`;
 el('scan-results').innerHTML=pages.map((p,pi)=>`<details data-scan-open="${p.document_id}-${p.page}"><summary>${escapeHtml(p.title)} · стр. ${p.page} · таблиц скана: ${p.tables.length}</summary><p><a href="${escapeHtml(p.url)}" target="_blank" rel="noopener noreferrer">Официальный PDF</a><br>Источник получен: ${escapeHtml(p.source_received_at)}<br>Прежнее OCR: ${escapeHtml(p.original_ocr_at)}<br>OCR положения слов: ${escapeHtml(p.word_ocr_at)}${p.cached?' · сохранённый результат':''}<br>Строки собраны: ${escapeHtml(p.processed_at)}</p><p>${escapeHtml(p.warning)}</p><button class="button secondary" data-scan-cell-page="${pi}" ${running?'disabled':''}>Прицельно прочитать до 6 проблемных строк</button>${p.issues.map(x=>'<p>'+escapeHtml(x.reason)+' · чтение '+(x.view+1)+'</p>').join('')}${p.tables.map((t,ti)=>`<details data-scan-open="${p.document_id}-${p.page}-${t.ordinal}"><summary>Таблица скана ${t.ordinal} · строк: ${t.rows.length} · обозначение части не установлено</summary><p>Столбцы 1/2 определены по расположению распознанных чисел. X/Y и метры нужно сверить с заголовком. Полнота таблицы не подтверждена.</p><button class="button secondary" data-scan-edit="${pi},${ti}" ${running?'disabled':''}>Внести визуальную сверку</button>${scanReviewInfo(p,t)}<div style="overflow:auto"><table><thead><tr><th>Строка</th><th>OCR 1 · точка | столбец 1 | столбец 2</th><th>OCR 2 · точка | столбец 1 | столбец 2</th><th>Проверка</th></tr></thead><tbody>${t.rows.map((r,ri)=>`<tr><td><button class="button secondary" data-scan-row="${pi},${ti},${ri}">Сверить строку ${r.ordinal}</button></td><td>${escapeHtml(scanCellText(r.views[0]))}</td><td>${escapeHtml(scanCellText(r.views[1]))}</td><td>${r.issues.length?r.issues.map(escapeHtml).join('<br>'):'Значения совпали; нужна визуальная сверка'}${scanCellInfo(p,t,r)}</td></tr>`).join('')}</tbody></table></div></details>`).join('')}${!p.tables.length?'<p>Поддержанное расположение координат не найдено. Это не означает отсутствия таблиц на скане.</p>':''}</details>`).join('');
 el('scan-export').classList.toggle('hidden',!a.result);
 el('scan-results').querySelectorAll('details[data-scan-open]').forEach(d=>d.open=opened.has(d.dataset.scanOpen));
}
async function runScanTables(){
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{const j=await api('/api/scan-tables',{catalog_id:scanCatalog,document_id:el('scan-document').value,page:Number(el('scan-page').value)});let s;
 do{await new Promise(r=>setTimeout(r,1000));s=await api('/api/jobs/'+j.job_id);await refreshScanTables()}while(s.state==='running');
 if(s.state==='error')throw Error(s.error);
 }catch(e){el('scan-status').textContent+='\nНе завершено: '+e.message}
 finally{running=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);scanPages()}
}
el('scan-document').addEventListener('change',scanPages);
el('scan-run').addEventListener('click',runScanTables);
el('scan-results').addEventListener('click',e=>{
 const edit=e.target.closest('[data-scan-edit]');if(edit){scanEditReview(...edit.dataset.scanEdit.split(',').map(Number));return}
 const cell=e.target.closest('[data-scan-cell-page]');if(cell){const p=scanDrafts.pages[Number(cell.dataset.scanCellPage)];scanOperation('/api/scan-tables/cells',{draft_id:scanDrafts.id,document_id:p.document_id,page:p.page});return}
 const b=e.target.closest('[data-scan-row]');if(b)scanEvidence(...b.dataset.scanRow.split(',').map(Number))
});
refreshScanTables().catch(e=>el('scan-status').textContent=e.message);
