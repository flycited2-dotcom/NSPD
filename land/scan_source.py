"""Source and stable content identity shared by scan OCR and visual review."""
import hashlib
import json
from . import store,ocr,municipal,scan_tables


def fingerprint(page):
    body={k:page[k] for k in ('document_id','page','source_sha256','source_views','algorithm','tables')}
    return hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def fingerprints(result):
    return [{'document_id':p['document_id'],'page':p['page'],'fingerprint':fingerprint(p)} for p in (result or {}).get('pages',[])]


def current(project,params):
    result=store.get_setting('scan_tables_'+project) or {}
    if not result.get('id') or params.get('draft_id')!=result['id']:raise ValueError('Черновик изменился; обновите страницу')
    n=params.get('page')
    if isinstance(n,bool) or not isinstance(n,int):raise ValueError('Недопустимая страница скана')
    page=next((p for p in result['pages'] if p['document_id']==params.get('document_id') and p['page']==n),None)
    catalog=store.get_setting('municipal_'+project) or {}
    if not page or result['catalog_id']!=catalog.get('id'):raise ValueError('Нет черновика текущего каталога')
    choice=next((c for c in scan_tables.choices(catalog) if c['document_id']==page['document_id'] and n in c['pages']),None)
    if not choice or choice['source_sha256']!=page['source_sha256']:raise ValueError('Источник скана изменился')
    row=next(r for r in catalog['items'] if r['id']==page['document_id'])
    observation=next(p for p in row['ocr']['pages'] if p['page']==n)
    if page['source_views']!=[{k:v[k] for k in ('view','width','height','image_sha256')} for v in observation['views']]:raise ValueError('Изображения черновика изменились')
    ocr.page_folder(page['source_sha256'])
    source=store.DATA/'municipal'/(page['source_sha256']+'.pdf');max_mib,_=municipal.pdf_scope(row)
    if not source.is_file() or source.stat().st_size>max_mib*1024*1024 or hashlib.sha256(source.read_bytes()).hexdigest()!=page['source_sha256']:
        raise ValueError('Сохранённый PDF отсутствует или изменился')
    for v in (0,1):ocr.preview(project,page['document_id'],n,v)
    return result,page,catalog


def ensure_current(project,result,catalog):
    if (store.get_setting('scan_tables_'+project) or {}).get('id')!=result['id'] or (store.get_setting('municipal_'+project) or {}).get('id')!=catalog['id']:
        raise ValueError('Источник или черновик изменился во время обработки')


def persist(project,key,result):
    result['updated_at']=store.now()
    result['id']=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]
    folder=store.DATA/'scan_tables';folder.mkdir(exist_ok=True)
    (folder/(result['id']+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    store.set_setting(key+'_'+project,result)
