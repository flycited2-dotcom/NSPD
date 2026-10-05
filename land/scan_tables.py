"""Auditable drafts from selected scanned pages, separate from accepted PDF tables."""
import copy
import hashlib
import json
import re
import subprocess
import sys
from . import store, municipal, ocr, scan_layout
from .scan_worker import ALGORITHM


def choices(catalog):
    result=[]
    for row in (catalog or {}).get('items',[]):
        prior=row.get('ocr') or {}
        if row['state']!='read' or row['format']!='pdf' or prior.get('source_sha256')!=row.get('sha256'):continue
        _,limit=municipal.pdf_scope(row)
        pages=[p['page'] for p in prior.get('pages',[]) if p['state']=='received' and p['page'] in row.get('image_or_sparse_pages',[])
               and 1<=p['page']<=min(limit,row['processed_pages'])]
        if pages:result.append({'document_id':row['id'],'title':row['title'],'source_sha256':row['sha256'],'pages':sorted(pages)})
    return result


def validate(data, observation):
    if data.get('algorithm')!=ALGORITHM or data.get('page')!=observation['page']:
        raise ValueError('Расположение слов относится к другому алгоритму или странице')
    if not isinstance(data.get('processed_at'),str) or not data['processed_at']:
        raise ValueError('У распознавания расположения слов нет даты')
    views=data.get('views')
    if not isinstance(views,list) or len(views)!=2:raise ValueError('Не получены два чтения расположения слов')
    for i,view in enumerate(views):
        source=observation['views'][i]
        if any(view.get(k)!=source[k] for k in ('view','width','height','image_sha256')):
            raise ValueError('Расположение слов относится к другому изображению')
        scan_layout.words(view)


def read_words(digest, observation):
    folder=ocr.page_folder(digest);number=observation['page']
    cache=folder/f'p{number}-{ALGORITHM}.json';cached=cache.exists()
    if cached:
        if cache.stat().st_size>4*1024*1024:raise ValueError('Сохранённое расположение слов превышает лимит')
        data=json.loads(cache.read_text(encoding='utf-8'))
    else:
        args=[sys.executable,'-m','land.scan_worker','--folder',str(folder.resolve()),'--page',str(number)]
        for view in observation['views']:args.extend(['--hash',view['image_sha256']])
        options={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
        try:
            output=subprocess.run(args,cwd=ocr.ROOT,capture_output=True,encoding='utf-8',timeout=60,**options)
        except subprocess.TimeoutExpired as exc:raise ValueError('Распознавание расположения слов превысило время; процесс остановлен') from exc
        if len(output.stdout)>4*1024*1024:raise ValueError('Расположение слов превышает лимит')
        data=json.loads(output.stdout)
        if output.returncode or data.get('error'):raise ValueError(data.get('error') or 'Распознавание расположения слов не завершено')
        data['source_sha256']=digest
    if data.get('source_sha256')!=digest:raise ValueError('Расположение слов относится к другому PDF')
    validate(data,observation)
    if not cached:cache.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    return data,cached


def run(project,params):
    catalog=store.get_setting('municipal_'+project) or {}
    if params.get('catalog_id')!=catalog.get('id') or not catalog.get('id'):raise ValueError('Каталог изменился; обновите страницу')
    number=params.get('page')
    if isinstance(number,bool) or not isinstance(number,int):raise ValueError('Номер страницы должен быть целым')
    choice=next((r for r in choices(catalog) if r['document_id']==params.get('document_id') and number in r['pages']),None)
    if not choice:raise ValueError('Выберите обработанную страницу скана текущего PDF')
    row=next(r for r in catalog['items'] if r['id']==choice['document_id'])
    digest=row['sha256']
    if not re.fullmatch(r'[0-9a-f]{64}',digest):raise ValueError('Некорректный SHA-256 источника')
    attempt={'state':'running','started_at':store.now(),'document_id':row['id'],'page':number,'network_requests':0}
    store.set_setting('scan_tables_attempt_'+project,attempt)
    try:
        source=store.DATA/'municipal'/(digest+'.pdf');max_mib,_=municipal.pdf_scope(row)
        if not source.is_file() or source.stat().st_size>max_mib*1024*1024 or hashlib.sha256(source.read_bytes()).hexdigest()!=digest:
            raise ValueError('Сохранённый PDF отсутствует, слишком велик или изменился')
        observation=next(p for p in row['ocr']['pages'] if p['page']==number)
        # Verify the current original images even when the word cache is reused.
        for v in (0,1):ocr.preview(project,row['id'],number,v)
        data,cached=read_words(digest,observation)
        draft=scan_layout.combine(data['views'])
        draft.update(document_id=row['id'],title=row['title'],url=row['url'],page=number,source_sha256=digest,
                     source_received_at=row['received_at'],original_ocr_at=observation['processed_at'],
                     word_ocr_at=data['processed_at'],processed_at=store.now(),word_algorithm=ALGORITHM,cached=cached,
                     ocr_engine=data.get('engine'),confidence_available=False,
                     source_views=[{k:v[k] for k in ('view','width','height','image_sha256')} for v in data['views']],
                     state='ocr_review_required',identity_confirmed=False,axes_confirmed=False,units_confirmed=False,
                     crs_parameters_confirmed=False,completeness_confirmed=False)
        if store.get_setting('municipal_'+project,{}).get('id')!=catalog['id']:
            raise ValueError('Каталог изменился во время чтения; повторите для текущего источника')
        result=copy.deepcopy(store.get_setting('scan_tables_'+project) or {'pages':[]})
        result['pages']=[p for p in result['pages'] if not (p['document_id']==row['id'] and p['page']==number)]+[draft]
        result.update(catalog_id=catalog['id'],updated_at=store.now(),warning=scan_layout.WARNING)
        result['id']=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]
        folder=store.DATA/'scan_tables';folder.mkdir(exist_ok=True)
        (folder/(result['id']+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        store.set_setting('scan_tables_'+project,result)
        count=sum(len(t['rows']) for t in draft['tables'])
        attempt.update(state='done',finished_at=store.now(),tables=len(draft['tables']),rows=count,cached=cached)
        with store.connect() as db:store.event(db,project,'scan_table_draft',{'document_id':row['id'],'page':number,'rows':count,'network_requests':0})
        return {'tables':len(draft['tables']),'rows':count,'cached':cached}
    except Exception as exc:
        attempt.update(state='error',error=str(exc)[:500],finished_at=store.now());raise
    finally:store.set_setting('scan_tables_attempt_'+project,attempt)
