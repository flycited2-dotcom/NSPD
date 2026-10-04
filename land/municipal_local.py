"""Revisit only already-received municipal PDFs, preserving dated source receipts."""
import copy,hashlib,json,re,subprocess,sys
from pathlib import Path
from . import municipal,store
from .municipal_pdf_worker import ALGORITHM

MAX_BYTES=16*1024*1024
BATCH=3
ROOT=Path(__file__).resolve().parent.parent

def queue(result):
    return sorted((r for r in (result or {}).get('items',[]) if r.get('format')=='pdf' and r.get('sha256')
        and r.get('local_pdf_attempt_algorithm')!=ALGORITHM and r.get('local_pdf_algorithm')!=ALGORITHM
        and ((r['state']=='read' and r.get('unread_pages',0)>0)
             or (r['state']=='rejected' and r.get('error')=='Ожидался PDF не более 8 МБ'))),
        key=lambda r:(r['kind']!='planning',r['id']))

def read_saved(digest):
    if not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest):raise ValueError('Некорректный SHA-256 PDF')
    path=store.DATA/'municipal'/(digest+'.pdf')
    if not path.is_file() or path.stat().st_size>MAX_BYTES or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
        raise ValueError('Сохранённый PDF отсутствует, превышает 16 МБ или изменился')
    opts={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
    try:
        output=subprocess.run([sys.executable,'-m','land.municipal_pdf_worker',str(path.resolve()),digest],
            cwd=ROOT,capture_output=True,encoding='utf-8',timeout=60,**opts)
    except subprocess.TimeoutExpired as exc:raise ValueError('Локальное чтение PDF превысило время; процесс остановлен') from exc
    if len(output.stdout)>4*1024*1024:raise ValueError('Результат локального чтения превышает лимит')
    result=json.loads(output.stdout)
    if output.returncode or result.get('error'):raise ValueError(result.get('error') or 'Ошибка локального чтения PDF')
    if result.get('source_sha256')!=digest or result.get('local_pdf_algorithm')!=ALGORITHM:
        raise ValueError('Результат относится к другому источнику/алгоритму')
    return result

def run(project,params):
    old=store.get_setting('municipal_'+project)
    if not old or params.get('id')!=old['id']:raise ValueError('Каталог изменился; обновите страницу')
    result=copy.deepcopy(old);selected=queue(result)[:BATCH]
    attempt={'state':'running','started_at':store.now(),'processed':0,'requested':len(selected),'network_requests':0}
    store.set_setting('municipal_local_attempt_'+project,attempt)
    try:
        for row in selected:
            attempt['document_id']=row['id'];store.set_setting('municipal_local_attempt_'+project,attempt)
            try:
                details=read_saved(row['sha256'])
                if row.get('total_pages') and row['total_pages']!=details['total_pages']:
                    raise ValueError('Число страниц сохранённого PDF изменилось')
                row.update(details,state='read',locally_read_at=store.now())
                row.pop('source_sha256',None);row.pop('error',None);row.pop('local_pdf_error',None)
            except Exception as exc:
                row.update(local_pdf_error=str(exc)[:500],locally_read_at=store.now())
            row['local_pdf_attempt_algorithm']=ALGORITHM
            attempt['processed']+=1
            municipal.persist(project,municipal.relate(result,store.get_setting('survey_'+project)))
            store.set_setting('municipal_local_attempt_'+project,attempt)
        attempt.update(state='done',finished_at=store.now(),remaining=len(queue(result)))
        with store.connect() as db:store.event(db,project,'municipal_local_pdf',{'id':result['id'],'documents':attempt['processed'],'network_requests':0})
        return {'processed':attempt['processed'],'remaining':attempt['remaining'],'network_requests':0}
    except Exception as exc:
        attempt.update(state='error',error=str(exc)[:500],finished_at=store.now());raise
    finally:store.set_setting('municipal_local_attempt_'+project,attempt)
