"""Targeted OCR of existing source pixels, separate from visual transcription."""
import math
import copy,hashlib,json,subprocess,sys
from . import scan_layout

ALGORITHM='cell-ru-2x-v1'
BATCH_ROWS=6


def bands(table, row, view):
    columns=[]
    for index in range(3):
        boxes=[r['views'][view]['cells'][index]['bbox'] for r in table['rows']
               if r['views'][view] and r['views'][view]['cells'][index]]
        if len(boxes)<2:raise ValueError('Недостаточно положений ячеек для выделения столбца')
        left=max(0,min(b[0] for b in boxes)-.012);right=min(1,max(b[2] for b in boxes)+.012)
        if not .005<right-left<.25:raise ValueError('Неоднозначная ширина столбца')
        columns.append((left,right))
    if any(a[1]>=b[0] for a,b in zip(columns,columns[1:])):raise ValueError('Поля столбцов пересекаются')
    box=(row['views'][view] or row)['bbox']
    top=max(0,box[1]-.0025);bottom=min(1,box[3]+.0025)
    if not .002<bottom-top<.05:raise ValueError('Неоднозначная высота строки')
    return [[left,top,right,bottom] for left,right in columns]


def summarize(views):
    if len(views)!=2:raise ValueError('Не получены два чтения ячеек')
    values=[]
    for i,view in enumerate(views):
        if view.get('view')!=i or len(view.get('cells',[]))!=3:raise ValueError('Неверный порядок ячеек')
        current=[]
        for j,cell in enumerate(view['cells']):
            text=cell.get('text');box=cell.get('bbox')
            if not isinstance(text,str) or len(text)>500 or not isinstance(box,list) or len(box)!=4:
                raise ValueError('Некорректный результат чтения ячейки')
            if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in box):
                raise ValueError('Некорректное положение ячейки')
            if not 0<=box[0]<box[2]<=1 or not 0<=box[1]<box[3]<=1:raise ValueError('Ячейка вне изображения')
            current.append(scan_layout.literal(text,j==0))
        values.append(current)
    issues=[]
    for i,label in enumerate(('Точка','Столбец 1','Столбец 2')):
        if any(v[i] is None for v in values):issues.append(label+': пропуск или неподдержанное чтение')
        elif values[0][i]!=values[1][i]:issues.append(label+': чтения различаются')
    return {'views':views,'values':values,'issues':issues,'agreement':'same_literal_values' if not issues else 'review_required',
            'verification_required':True,'geometry_confirmed':False}


def requests(page):
    from . import scan_source
    fp=scan_source.fingerprint(page);result=[]
    for table in page['tables']:
        for row in table['rows']:
            if not row['issues']:continue
            item={'table':table['ordinal'],'row':row['ordinal'],'bands':[bands(table,row,v) for v in (0,1)],'fingerprint':fp}
            item['key']=hashlib.sha256(json.dumps({'algorithm':ALGORITHM,**item},sort_keys=True).encode()).hexdigest()
            result.append(item)
    return result


def run(project,params):
    from . import store,scan_source,ocr
    result,page,catalog=scan_source.current(project,params)
    old=store.get_setting('scan_cells_'+project) or {'rows':[]};fp=scan_source.fingerprint(page)
    prior={r['key']:r for r in old['rows'] if r.get('algorithm')==ALGORITHM}
    selected=[r for r in requests(page) if r['key'] not in prior][:BATCH_ROWS]
    attempt={'state':'running','started_at':store.now(),'network_requests':0,'requested':len(selected),'processed':0}
    store.set_setting('scan_cells_attempt_'+project,attempt)
    try:
        if not selected:
            attempt.update(state='done',finished_at=store.now(),remaining=0);return {'processed':0,'remaining':0}
        folder=ocr.page_folder(page['source_sha256']);cache=folder/'cells';cache.mkdir(exist_ok=True)
        payload={k:page[k] for k in ('source_sha256','page','source_views')};payload['rows']=selected
        path=cache/(hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()+'.request.json')
        path.write_text(json.dumps(payload),encoding='utf-8')
        options={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
        try:output=subprocess.run([sys.executable,'-m','land.scan_cell_worker',str(path.resolve()),str(folder.resolve())],cwd=ocr.ROOT,capture_output=True,encoding='utf-8',timeout=60,**options)
        except subprocess.TimeoutExpired as exc:raise ValueError('Прицельное OCR превысило время; процесс остановлен') from exc
        if len(output.stdout)>1024*1024:raise ValueError('Прицельное OCR превышает лимит вывода')
        data=json.loads(output.stdout)
        if output.returncode or data.get('error'):raise ValueError(data.get('error') or 'Прицельное OCR не завершено')
        if (data.get('algorithm')!=ALGORITHM or data.get('source_sha256')!=page['source_sha256'] or data.get('page')!=page['page']
            or data.get('source_views')!=page['source_views'] or len(data.get('rows',[]))!=len(selected)):
            raise ValueError('Прицельное OCR относится к другому источнику')
        if not isinstance(data.get('processed_at'),str) or not data['processed_at']:
            raise ValueError('У прицельного OCR нет даты чтения')
        fresh=[]
        for requested,observed in zip(selected,data['rows']):
            if any(observed.get(k)!=requested[k] for k in ('key','table','row','bands')):raise ValueError('Прицельное OCR относится к другой ячейке')
            record=summarize(observed['views'])
            if any(c['bbox']!=requested['bands'][v][ci] for v,view in enumerate(observed['views']) for ci,c in enumerate(view['cells'])):raise ValueError('Границы прочитанных ячеек изменились')
            record.update(**{k:observed[k] for k in ('key','table','row')},document_id=page['document_id'],page=page['page'],
                          source_sha256=page['source_sha256'],source_views=page['source_views'],fingerprint=fp,
                          algorithm=ALGORITHM,processed_at=data['processed_at'],confidence_available=False)
            fresh.append(record)
        scan_source.ensure_current(project,result,catalog)
        saved=copy.deepcopy(old);saved['rows']+=fresh
        scan_source.persist(project,'scan_cells',saved)
        remaining=len([r for r in requests(page) if r['key'] not in {x['key'] for x in saved['rows']}])
        attempt.update(state='done',finished_at=store.now(),processed=len(fresh),remaining=remaining)
        with store.connect() as db:store.event(db,project,'scan_cell_ocr',{'document_id':page['document_id'],'page':page['page'],'rows':len(fresh),'network_requests':0})
        return {'processed':len(fresh),'remaining':remaining}
    except Exception as exc:
        attempt.update(state='error',error=str(exc)[:500],finished_at=store.now());raise
    finally:store.set_setting('scan_cells_attempt_'+project,attempt)
