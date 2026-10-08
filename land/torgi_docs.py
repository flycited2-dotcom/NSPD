"""Observed public lot cards and files, with notice/lot evidence kept separate."""
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from . import store, torgi
from .network import fetch, ResponseTooLarge
from .docx_text import ALGORITHM as DOCX_ALGORITHM
from . import image_evidence
from .legacy_text import ALGORITHM as LEGACY_ALGORITHM, OLE_MAGIC
from .egrn_coordinates import PDF_ALGORITHM as ALGORITHM

CARD = 'https://torgi.gov.ru/new/api/public/lotcards/'
FILE = 'https://torgi.gov.ru/new/file-store/v1/'
BATCH = 5
MAX_BYTES = 8*1024*1024
PDF_MAX_BYTES = 16*1024*1024
READ_FORMATS = ('pdf','docx','doc','rtf',*image_evidence.FORMATS)


def setting_key(project, active=False, suffix=''):
    return torgi.setting_key(project, active, '_documents' + suffix)


def assert_current(project, search, catalog_id, active=False):
    current = store.get_setting(torgi.setting_key(project, active))
    catalog = store.get_setting(setting_key(project, active))
    if ((current or {}).get('id') != (search or {}).get('id')
            or torgi.source_id(current) != torgi.source_id(search)
            or (catalog or {}).get('id') != catalog_id):
        raise ValueError('Поиск или каталог изменились во время обработки; результат не записан')


def persist_current(project, result, search, catalog_id, active=False):
    with store.LOCK:
        assert_current(project, search, catalog_id, active)
        persist(project, result, active)
    return result['id']


def same_search(search,catalog):
    if not search or not catalog:return False
    if search.get('search_source_id'):
        return (catalog.get('search_source_id') or catalog.get('search_id'))==search['search_source_id']
    # Legacy searches already retain this date through geometry-only revisions.
    return catalog.get('search_id')==search.get('id') or bool(catalog.get('search_created_at') and catalog['search_created_at']==search.get('created_at'))
ROOT = Path(__file__).resolve().parent.parent
WARNING = 'Файлы извещения могут относиться ко многим лотам. Номер в документе не подтверждает предмет лота, геометрию, действующие условия или доступность земли. Подписи и права не проверены.'


def file_limit(fmt):
    return PDF_MAX_BYTES if fmt=='pdf' else MAX_BYTES


def validate_download(file,raw):
    if len(raw)!=file['declared_size'] or len(raw)>file_limit(file['format']):
        raise ValueError('Размер скачанного файла отличается от карточки или превышает лимит')
    signature={'pdf':b'%PDF-','docx':b'PK\x03\x04','doc':OLE_MAGIC,'rtf':b'{\\rtf1',
               'jpg':b'\xff\xd8\xff','jpeg':b'\xff\xd8\xff','png':b'\x89PNG\r\n\x1a\n'}[file['format']]
    if not raw.startswith(signature):raise ValueError('Содержимое не соответствует формату файла')


def descriptor(row, scope):
    if not isinstance(row,dict) or not re.fullmatch(r'[0-9a-f]{24}',str(row.get('fileId',''))):
        raise ValueError('Неожиданный идентификатор файла')
    name=row.get('fileName');size=row.get('fileSize')
    if not isinstance(name,str) or not 1<=len(name)<=500 or any(ord(c)<32 for c in name):
        raise ValueError('Некорректное имя файла')
    if isinstance(size,bool) or not isinstance(size,int) or not 0<size<=1024*1024*1024:
        raise ValueError('Неожиданный размер файла')
    digest=row.get('hash')
    if digest is not None and (not isinstance(digest,str) or not re.fullmatch(r'[0-9a-fA-F]{64}',digest)):
        raise ValueError('Неожиданный хеш источника')
    inactive=row.get('inactive',False)
    if not isinstance(inactive,bool):raise ValueError('Неожиданный статус вложения')
    ext=Path(name).suffix.lower().lstrip('.')
    identity={'file_id':row['fileId'],'file_name':name,'declared_size':size,'source_hash':digest}
    key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:24]
    return {**identity,'key':key,'scope':scope,'format':ext,'url':FILE+row['fileId'],
            'source_hash_algorithm':'not_specified','source_upload_date':row.get('uploadDate'),
            'inactive':inactive,'type_code':str(row.get('attachmentTypeCode') or '')[:100],
            'type_name':str(row.get('attachmentTypeName') or '')[:500],
            'source_check_result':str(row.get('checkResult') or '')[:100]}


def normalize_card(data, lot):
    if not isinstance(data,dict) or data.get('id')!=lot['id'] or str(data.get('subjectRFCode'))!='91' or data.get('noticeNumber')!=lot['notice_number']:
        raise ValueError('Карточка не соответствует лоту/извещению/региону')
    attachments=[]
    for field,scope in [('lotAttachments','lot'),('noticeAttachments','notice')]:
        rows=data.get(field) or []
        if not isinstance(rows,list) or len(rows)>100:raise ValueError('Неожиданная структура вложений')
        seen=set()
        for row in rows:
            item=descriptor(row,scope)
            if item['key'] in seen:raise ValueError('Повтор файла внутри группы вложений')
            seen.add(item['key']);attachments.append(item)
    return {'lot_id':lot['id'],'notice_number':lot['notice_number'],'lot_url':lot['url'],
            'state':'received','card_status':str(data.get('lotStatus') or '')[:100],
            'attachments':attachments,'geometry_confirmed':False}


def files_for(cards, prior):
    files={};versions={}
    for card in cards:
        for attachment in card.get('attachments',[]):
            key=attachment['key']
            versions.setdefault(attachment['file_id'],set()).add(key)
            if key not in files:
                old=copy.deepcopy(prior.get(key) or {})
                initial='inactive' if attachment['inactive'] else 'oversized' if attachment['declared_size']>file_limit(attachment['format']) else 'pending' if attachment['format'] in READ_FORMATS else 'unsupported'
                files[key]={**old,**attachment,'associations':[]}
                files[key].setdefault('state',initial)
            f=files[key]
            f['associations'].append({'lot_id':card['lot_id'],'notice_number':card['notice_number'],'scope':attachment['scope'],'inactive':attachment['inactive']})
    for file in files.values():
        file['metadata_conflict']=len(versions[file['file_id']])>1
        file['eligible']=not file['metadata_conflict'] and any(not a['inactive'] for a in file['associations'])
        if file['state']=='inactive' and file['eligible']:
            file['state']='oversized' if file['declared_size']>file_limit(file['format']) else 'pending' if file['format'] in READ_FORMATS else 'unsupported'
    return files


def metadata_queue(search,result,retry=False,active=False,survey=None):
    prior={c['lot_id']:c for c in result.get('cards',[])}
    pending=[lot for lot in search['lots'] if (not active or (lot.get('type') or {}).get('code')=='ZK')
             and (lot['id'] not in prior or (retry and prior[lot['id']]['state']=='error'))]
    if active:
        districts={':'.join(number.split(':')[:2]) for number in torgi.survey_geometries(survey)}
        pending.sort(key=lambda lot: 0 if any(':'.join(number.split(':')[:2]) in districts
                     for number in lot.get('cadastral_numbers',[])) else 1)
    return pending


def refresh_files(result,prior):
    result['files']=files_for(result['cards'],prior)
    # Dated files of cards not yet refreshed remain reusable, outside active queues.
    result['retained_files']={key:copy.deepcopy(file) for key,file in prior.items() if key not in result['files']}


def file_queue(result,retry=False):
    def priority(f):
        name=f['file_name'].lower()
        if f.get('type_code')=='Notice_Document' or any(x in name for x in ('схем','кадастр','межеван','егрн')):return 0
        if f.get('type_code')=='Basis_for_sale' or any(a['scope']=='lot' for a in f['associations']):return 1
        if f.get('type_code') in ('Application_Form','Draft_Contract') or any(x in name for x in ('квитанц','задат','договор','заявк')):return 3
        return 2
    return sorted((f for f in result.get('files',{}).values() if f.get('eligible') and (f['state']=='pending' or (retry and f['state']=='error')
                  or (f['state'] in ('unsupported','oversized') and f['format'] in READ_FORMATS and f['declared_size']<=file_limit(f['format'])))),
                  key=lambda f: (0 if retry and f['state']=='error' else 1, priority(f)))


def selected_lot(params, search, result, active=False):
    if 'lot_id' not in params:return None
    lot_id=params['lot_id']
    if not active or not isinstance(lot_id,str) or not re.fullmatch(r'\d{10,30}_\d{1,6}',lot_id):
        raise ValueError('Выбор лота доступен только для регионального каталога; требуется корректный lot_id')
    lots=[lot for lot in search.get('lots',[]) if lot.get('id')==lot_id
          and (lot.get('type') or {}).get('code')=='ZK']
    if len(lots)!=1 or not any(card.get('lot_id')==lot_id and card.get('state')=='received'
                              and card.get('notice_number')==lots[0].get('notice_number')
                              for card in result.get('cards',[])):
        raise ValueError('Лот ЗК или его принятая карточка отсутствуют в текущем поиске/каталоге')
    return lot_id


def files_for_lot(files, lot_id):
    if lot_id is None:return files
    # Notice files are shared only through the explicit associations of accepted cards.
    return [file for file in files if file.get('eligible') and any(
        association.get('lot_id')==lot_id and association.get('scope') in ('lot','notice')
        and not association.get('inactive',False) for association in file.get('associations',[]))]


def persist(project,result,active=False):
    result['updated_at']=store.now()
    result['id']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()[:20]
    folder=store.DATA/'torgi_documents';folder.mkdir(exist_ok=True)
    (folder/(result['id']+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    store.set_setting(setting_key(project,active),result)


def reprocess_queue(result):
    def outdated(file):
        if file['state']=='read' and file.get('format')=='pdf':return file.get('algorithm')!=ALGORITHM
        if file['state']!='rejected' or not file.get('eligible') or not file.get('sha256'):return False
        if file.get('format')=='docx':
            return (file.get('error')=='Основной XML DOCX превышает лимит'
                    and file.get('reprocess_algorithm')!=DOCX_ALGORITHM)
        return (file.get('format')=='rtf' and file.get('error')=='Регистр управляющего слова RTF не поддержан'
                and file.get('reprocess_algorithm')!=LEGACY_ALGORITHM)
    return [f for f in result.get('files',{}).values() if outdated(f)]


def reprocess(project,params,active=False):
    old=store.get_setting(setting_key(project,active))
    search=store.get_setting(torgi.setting_key(project,active))
    if not old or params.get('id')!=old['id'] or not same_search(search,old):
        raise ValueError('Каталог документов/поиск изменился; обновите страницу')
    lot_id=selected_lot(params,search,old,active)
    result=copy.deepcopy(old);selected=files_for_lot(reprocess_queue(result),lot_id)[:BATCH];catalog_id=old['id']
    attempt={'state':'running','started_at':store.now(),'processed':0,'requested':len(selected),'network_requests':0}
    if lot_id is not None:attempt['lot_id']=lot_id
    attempt_key=torgi.setting_key(project,active,'_reprocess_attempt')
    store.set_setting(attempt_key,attempt)
    try:
        for file in selected:
            assert_current(project,search,catalog_id,active)
            fmt=file['format'];algorithm=LEGACY_ALGORITHM if fmt in ('doc','rtf') else DOCX_ALGORITHM if fmt=='docx' else ALGORITHM
            try:
                digest=file.get('sha256')
                if not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest):
                    raise ValueError('Некорректный SHA-256 сохранённого файла')
                path=store.DATA/'torgi_documents'/(digest+'.'+fmt)
                if not path.is_file() or path.stat().st_size>file_limit(fmt) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                    raise ValueError('Сохранённый файл отсутствует, слишком велик или изменился')
                file.update(extract_file(path,digest,fmt),state='read',reprocessed_at=store.now(),geometry_confirmed=False,
                            reprocess_algorithm=algorithm)
                file.pop('error',None)
            except Exception as exc:
                file.update(state='rejected',error=str(exc)[:500],reprocessed_at=store.now(),tables=[],egrn_tables=[],
                            reprocess_algorithm=algorithm)
            attempt['processed']+=1;catalog_id=persist_current(project,result,search,catalog_id,active)
            store.set_setting(attempt_key,attempt)
        catalog_id=persist_current(project,result,search,catalog_id,active)
        attempt.update(state='done',finished_at=store.now(),remaining=len(files_for_lot(reprocess_queue(result),lot_id)))
        with store.connect() as db:
            store.event(db,project,'torgi_reprocess',{'id':result['id'],'files':attempt['processed'],'network_requests':0})
        return {'processed':attempt['processed'],'remaining':attempt['remaining'],'network_requests':0,
                **({'lot_id':lot_id} if lot_id is not None else {})}
    except Exception as exc:
        attempt.update(state='error',error=str(exc)[:500],finished_at=store.now());raise
    finally:store.set_setting(attempt_key,attempt)


def metadata(project,params,active=False):
    search=store.get_setting(torgi.setting_key(project,active))
    if not search or params.get('search_id')!=search['id']:raise ValueError('Поиск изменился; обновите страницу')
    if params.get('search_source_id') is not None and params['search_source_id']!=torgi.source_id(search):
        raise ValueError('Исходный поиск изменился; обновите страницу')
    retry=params.get('retry_errors',False)
    if not isinstance(retry,bool):raise ValueError('Параметр повтора должен быть логическим')
    old=store.get_setting(setting_key(project,active),{}) or {}
    if params.get('id') is not None and params['id']!=old.get('id'):
        raise ValueError('Каталог изменился; обновите страницу')
    catalog_id=old.get('id')
    prior_files={**old.get('retained_files',{}),**old.get('files',{})}
    result=copy.deepcopy(old)
    # A new actual search requires fresh cards; geometry-only updates retain dated cards.
    same=same_search(search,result)
    known={lot['id'] for lot in search['lots'] if not active or (lot.get('type') or {}).get('code')=='ZK'}
    result.update(search_id=search['id'],search_source_id=torgi.source_id(search),search_created_at=search['created_at'],warning=WARNING,
                  cards=[c for c in old.get('cards',[]) if same and c['lot_id'] in known],geometry_confirmed=False)
    if active:result['scope']='regional_active_zk'
    survey=store.get_setting('survey_'+project)
    selected=metadata_queue(search,result,retry,active,survey)[:BATCH]
    attempt={'state':'running','started_at':store.now(),'processed':0,'requested':len(selected)}
    attempt_key=setting_key(project,active,'_attempt')
    store.set_setting(attempt_key,attempt)
    try:
        for lot in selected:
            assert_current(project,search,catalog_id,active)
            url=CARD+lot['id'];attempt['lot_id']=lot['id']
            store.set_setting(attempt_key,attempt)
            try:
                raw,ct,code=fetch(url,max_bytes=2*1024*1024)
            except Exception as exc:
                assert_current(project,search,catalog_id,active)
                result['cards']=[c for c in result['cards'] if c['lot_id']!=lot['id']]+[{'lot_id':lot['id'],'state':'error','error':str(exc)[:500],'checked_at':store.now()}]
                attempt.update(state='partial',error=str(exc)[:500]);break
            assert_current(project,search,catalog_id,active)
            digest=hashlib.sha256(raw).hexdigest()
            data=json.loads(raw)
            card=normalize_card(data,lot)
            if active and data.get('biddType') and data['biddType'].get('code')!='ZK':
                raise ValueError('Карточка относится к другому виду торгов; прежний каталог сохранён')
            card.update(source=url,received_at=store.now(),sha256=digest,http_status=code)
            # Only normalized fields are persisted; raw account/payment/owner fields are excluded.
            result['cards']=[c for c in result['cards'] if c['lot_id']!=lot['id']]+[card]
            refresh_files(result,prior_files)
            attempt['processed']+=1;catalog_id=persist_current(project,result,search,catalog_id,active)
            store.set_setting(attempt_key,attempt);time.sleep(2)
        refresh_files(result,prior_files)
        catalog_id=persist_current(project,result,search,catalog_id,active)
        if attempt['state']=='running':attempt['state']='done'
        attempt.update(finished_at=store.now(),remaining=len(metadata_queue(search,result,active=active,survey=survey)))
        with store.connect() as db:store.event(db,project,'torgi_document_cards',{'id':result['id'],'cards':attempt['processed'],'state':attempt['state']})
        return {'processed':attempt['processed'],'remaining':attempt['remaining'],'state':attempt['state']}
    except Exception as exc:
        attempt.update(state='error',error=str(exc)[:500],finished_at=store.now());raise
    finally:store.set_setting(attempt_key,attempt)


def extract_file(path,digest,fmt):
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
    try:
        r=subprocess.run([sys.executable,'-m','land.torgi_file_worker',str(path.resolve()),digest,fmt],cwd=ROOT,
                         capture_output=True,encoding='utf-8',timeout=60,**options)
    except subprocess.TimeoutExpired as exc:raise ValueError('Чтение файла превысило время; процесс остановлен') from exc
    if len(r.stdout)>4*1024*1024:raise ValueError('Результат чтения превышает лимит')
    result=json.loads(r.stdout)
    if r.returncode or result.get('error'):raise ValueError(result.get('error') or 'Ошибка чтения файла')
    algorithm=LEGACY_ALGORITHM if fmt in ('doc','rtf') else DOCX_ALGORITHM if fmt=='docx' else image_evidence.ALGORITHM if fmt in image_evidence.FORMATS else ALGORITHM
    if result.get('sha256')!=digest or result.get('algorithm')!=algorithm:raise ValueError('Чтение относится к другому файлу')
    return result


def read(project,params,active=False):
    old=store.get_setting(setting_key(project,active))
    search=store.get_setting(torgi.setting_key(project,active))
    if not old or params.get('id')!=old['id'] or not same_search(search,old):
        raise ValueError('Каталог документов/поиск изменился; сначала загрузите карточки')
    retry=params.get('retry_errors',False)
    if not isinstance(retry,bool):raise ValueError('Параметр повтора должен быть логическим')
    lot_id=selected_lot(params,search,old,active)
    result=copy.deepcopy(old);selected=files_for_lot(file_queue(result,retry),lot_id)[:BATCH];catalog_id=old['id']
    attempt={'state':'running','started_at':store.now(),'processed':0,'requested':len(selected),'network_requests':0,'cached_files':0}
    if lot_id is not None:attempt['lot_id']=lot_id
    attempt_key=torgi.setting_key(project,active,'_files_attempt')
    store.set_setting(attempt_key,attempt)
    folder=store.DATA/'torgi_documents';folder.mkdir(exist_ok=True)
    try:
        for file in selected:
            assert_current(project,search,catalog_id,active)
            attempt['file_key']=file['key'];store.set_setting(attempt_key,attempt)
            try:
                digest=file.get('sha256')
                cached=folder/(digest+'.'+file['format']) if isinstance(digest,str) and re.fullmatch(r'[0-9a-f]{64}',digest) else None
                if cached and file.get('received_at'):
                    if not cached.is_file():raise ValueError('Сохранённый файл отсутствует; чтение остановлено')
                    if cached.stat().st_size>file_limit(file['format']):raise ValueError('Сохранённый файл превышает лимит')
                    raw=cached.read_bytes()
                    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Сохранённый файл изменился; чтение остановлено')
                    validate_download(file,raw)
                    ct,code=file.get('content_type',''),file.get('http_status',200)
                    attempt['cached_files']+=1
                else:
                    attempt['network_requests']=attempt.get('network_requests',0)+1
                    raw,ct,code=fetch(FILE+file['file_id'],max_bytes=file_limit(file['format']))
                    file.update(received_at=store.now(),sha256=hashlib.sha256(raw).hexdigest(),http_status=code,content_type=ct,actual_size=len(raw))
            except ResponseTooLarge as exc:
                file.update(state='rejected',error=str(exc)[:500],checked_at=store.now())
                attempt['processed']+=1;catalog_id=persist_current(project,result,search,catalog_id,active);continue
            except Exception as exc:
                assert_current(project,search,catalog_id,active)
                file.update(state='error',error=str(exc)[:500],checked_at=store.now())
                attempt.update(state='partial',error=str(exc)[:500]);break
            assert_current(project,search,catalog_id,active)
            digest=hashlib.sha256(raw).hexdigest();path=folder/(digest+'.'+file['format'])
            try:
                validate_download(file,raw)
                path.write_bytes(raw)
                file.update(extract_file(path,digest,file['format']),state='read',geometry_confirmed=False)
                file.pop('error',None)
            except Exception as exc:file.update(state='rejected',error=str(exc)[:500])
            attempt['processed']+=1;catalog_id=persist_current(project,result,search,catalog_id,active)
            store.set_setting(attempt_key,attempt);time.sleep(2)
        catalog_id=persist_current(project,result,search,catalog_id,active)
        if attempt['state']=='running':attempt['state']='done'
        attempt.update(finished_at=store.now(),remaining=len(files_for_lot(file_queue(result),lot_id)))
        with store.connect() as db:store.event(db,project,'torgi_files',{'id':result['id'],'files':attempt['processed'],'state':attempt['state'],
                                                                  'network_requests':attempt['network_requests'],'cached_files':attempt['cached_files']})
        return {'processed':attempt['processed'],'remaining':attempt['remaining'],'state':attempt['state'],
                'network_requests':attempt['network_requests'],'cached_files':attempt['cached_files'],
                **({'lot_id':lot_id} if lot_id is not None else {})}
    except Exception as exc:
        attempt.update(state='error',error=str(exc)[:500],finished_at=store.now());raise
    finally:store.set_setting(attempt_key,attempt)
