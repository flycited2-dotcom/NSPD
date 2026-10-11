"""Local continuation of accepted Torgi PDFs; dated source evidence remains intact."""
import copy
import hashlib
import json
import re
import subprocess
import sys

from . import store, torgi, torgi_docs as docs
from .torgi_pdf_worker import ALGORITHM, MAX_BYTES, MAX_OUTPUT, MAX_PAGES

BATCH = 3
WARNING = ('Локальное чтение сохранённых PDF: не более 3 файлов и первых 120 страниц на файл, '
           'без скачивания и OCR. Даты получения и SHA-256 источников сохраняются; '
           'оставшиеся страницы и сканы не считаются прочитанными. Номера и таблицы '
           'не подтверждают принадлежность к лоту, геопривязку, права или свободность земли.')
EVIDENCE_FIELDS = ('algorithm', 'unit', 'mentions', 'coordinate_label_pages', 'crs_mentions',
                   'total_pages', 'processed_pages', 'unread_pages', 'image_or_sparse_pages',
                   'text_layer_complete', 'tables', 'unreadable_tables', 'crs_status', 'egrn_tables',
                   'unparsed_coordinate_pages', 'geometry_confirmed', 'georeferenced')


def _eligible(file):
    return (file.get('format') == 'pdf' and file.get('state') == 'read' and file.get('eligible') is True
            and not file.get('metadata_conflict') and bool(file.get('received_at'))
            and isinstance(file.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', file['sha256'])
            and type(file.get('total_pages')) is int and type(file.get('processed_pages')) is int
            and 0 < file['processed_pages'] < file['total_pages']
            and file.get('unread_pages') == file['total_pages'] - file['processed_pages'])


def queue(result, retry=False):
    if not isinstance(retry, bool):
        raise ValueError('Параметр повтора должен быть логическим')
    result = result or {}
    pending = []
    for file in result.get('files', {}).values():
        if not _eligible(file) or file['processed_pages'] >= MAX_PAGES:
            continue
        prior = file.get('local_pdf_attempt') or {}
        if (not retry and prior.get('algorithm') == ALGORITHM and prior.get('source_sha256') == file['sha256']
                and prior.get('state') == 'error'):
            continue
        pending.append(file)
    return sorted(pending, key=lambda file: (file['processed_pages'], file.get('key', '')))


def summary(result, lot_id=None):
    files = docs.files_for_lot([file for file in (result or {}).get('files', {}).values() if _eligible(file)], lot_id)
    pending = docs.files_for_lot(queue(result), lot_id)
    return {'remaining': len(pending), 'retry_remaining': len(docs.files_for_lot(queue(result, True), lot_id)),
            'unread_pages': sum(file['total_pages'] - file['processed_pages'] for file in files),
            'unread_pages_queued': sum(min(file['total_pages'], MAX_PAGES) - file['processed_pages'] for file in pending),
            'limited_files': sum(file['processed_pages'] >= MAX_PAGES for file in files),
            'max_pages': MAX_PAGES, 'batch': BATCH}


def report(project, active=False):
    catalog = store.get_setting(docs.setting_key(project, active))
    search = store.get_setting(torgi.setting_key(project, active))
    return {**summary(catalog), 'catalog_id': (catalog or {}).get('id'),
            'attempt': store.get_setting(docs.setting_key(project, active, '_pdf_local_attempt')),
            'stale': bool(catalog and not docs.same_search(search, catalog)), 'warning': WARNING}


def read_saved(digest):
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Некорректный SHA-256 сохранённого PDF')
    path = store.DATA / 'torgi_documents' / (digest + '.pdf')
    if not path.is_file() or path.stat().st_size > MAX_BYTES or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('Сохранённый PDF отсутствует, превышает 16 МБ или изменился')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    try:
        output = subprocess.run([sys.executable, '-m', 'land.torgi_pdf_worker', str(path.resolve()), digest],
                                cwd=docs.ROOT, capture_output=True, encoding='utf-8', timeout=60, **options)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Локальное продолжение PDF превысило 60 секунд; процесс остановлен') from exc
    if len(output.stdout.encode('utf-8')) > MAX_OUTPUT:
        raise ValueError('Результат локального продолжения PDF превышает 4 МБ')
    result = json.loads(output.stdout)
    if output.returncode or result.get('error'):
        raise ValueError(result.get('error') or 'Ошибка локального продолжения PDF')
    if (result.get('source_sha256') != digest or result.get('algorithm') != docs.ALGORITHM
            or result.get('local_pdf_algorithm') != ALGORITHM or result.get('local_pdf_max_pages') != MAX_PAGES):
        raise ValueError('Продолжение относится к другому источнику или алгоритму')
    total, processed, unread = (result.get(key) for key in ('total_pages', 'processed_pages', 'unread_pages'))
    if (any(type(value) is not int for value in (total, processed, unread)) or total < 1
            or processed != min(total, MAX_PAGES) or unread != total - processed
            or result.get('page_range') != [1, processed]):
        raise ValueError('Неверное число или нумерация страниц продолжения PDF')
    text = path.with_suffix('.continued.txt')
    if (not text.is_file() or text.stat().st_size > MAX_OUTPUT
            or hashlib.sha256(text.read_bytes()).hexdigest() != result.get('continued_text_sha256')):
        raise ValueError('Текст продолжения PDF отсутствует или изменился')
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('Сохранённый PDF изменился во время локального чтения')
    return result


def _assert_snapshot(project, search, expected, active, lot_id):
    docs.assert_current(project, search, expected['id'], active)
    current_search = store.get_setting(torgi.setting_key(project, active))
    current_catalog = store.get_setting(docs.setting_key(project, active))
    if current_search != search or current_catalog != expected:
        raise ValueError('Поиск, каталог или сведения лота изменились; результат не записан')
    if lot_id is not None:
        docs.selected_lot({'lot_id': lot_id}, current_search, current_catalog, active)


def run(project, params, active=False):
    old = store.get_setting(docs.setting_key(project, active))
    search = store.get_setting(torgi.setting_key(project, active))
    if not old or params.get('id') != old.get('id') or not docs.same_search(search, old):
        raise ValueError('Каталог документов или поиск изменился; обновите страницу')
    retry = params.get('retry_errors', False)
    if not isinstance(retry, bool):
        raise ValueError('Параметр повтора должен быть логическим')
    lot_id = docs.selected_lot(params, search, old, active)
    result = copy.deepcopy(old)
    expected = copy.deepcopy(old)
    selected = docs.files_for_lot(queue(result, retry), lot_id)[:BATCH]
    attempt = {'state': 'running', 'started_at': store.now(), 'processed': 0, 'errors': 0,
               'requested': len(selected), 'network_requests': 0}
    if lot_id is not None:
        attempt['lot_id'] = lot_id
    attempt_key = docs.setting_key(project, active, '_pdf_local_attempt')
    store.set_setting(attempt_key, attempt)
    try:
        for file in selected:
            _assert_snapshot(project, search, expected, active, lot_id)
            attempt['file_key'] = file['key']
            store.set_setting(attempt_key, attempt)
            observed = {'algorithm': ALGORITHM, 'source_sha256': file['sha256'], 'checked_at': store.now(), 'state': 'error'}
            try:
                details = read_saved(file['sha256'])
                if details['total_pages'] != file['total_pages'] or details['processed_pages'] <= file['processed_pages']:
                    raise ValueError('Число страниц PDF изменилось или продолжение не расширило чтение')
                # A new prefix can complete a table crossing page 40. Its former
                # partial observation stays attached, rather than being silently lost.
                previous = {key: copy.deepcopy(file[key]) for key in EVIDENCE_FIELDS if key in file}
                previous.update(sha256=file['sha256'], received_at=file['received_at'])
                update = {key: copy.deepcopy(details[key]) for key in EVIDENCE_FIELDS if key in details}
                update.update(local_pdf_previous=previous, local_pdf_algorithm=ALGORITHM,
                              local_pdf_max_pages=MAX_PAGES, local_pdf_source_sha256=file['sha256'],
                              local_pdf_read_at=observed['checked_at'], continued_text_sha256=details.get('continued_text_sha256'),
                              geometry_confirmed=False, georeferenced=False)
                file.update(update)
                observed['state'] = 'read'
            except Exception as exc:
                observed['error'] = str(exc)[:500]
                attempt['errors'] += 1
            file['local_pdf_attempt'] = observed
            with store.LOCK:
                _assert_snapshot(project, search, expected, active, lot_id)
                docs.persist_current(project, result, search, expected['id'], active)
                expected = copy.deepcopy(result)
            attempt['processed'] += 1
            store.set_setting(attempt_key, attempt)
        counts = summary(result, lot_id)
        attempt.update(state='partial' if attempt['errors'] else 'done', finished_at=store.now(),
                       remaining=counts['remaining'], unread_pages=counts['unread_pages'])
        if selected:
            with store.connect() as db:
                store.event(db, project, 'torgi_local_pdf', {'id': result['id'], 'files': attempt['processed'],
                                                          'errors': attempt['errors'], 'network_requests': 0})
        return {key: attempt[key] for key in ('processed', 'errors', 'remaining', 'unread_pages', 'network_requests', 'state')} | ({'lot_id': lot_id} if lot_id is not None else {})
    except Exception as exc:
        attempt.update(state='error', error=str(exc)[:500], finished_at=store.now())
        raise
    finally:
        store.set_setting(attempt_key, attempt)
