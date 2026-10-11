"""Explicit single-file large PDF lane; ordinary download limits stay unchanged."""
import copy
import hashlib
import json
import re
import subprocess
import sys

from . import store, torgi, torgi_docs as docs
from .network import fetch
from .torgi_large_pdf_worker import ALGORITHM, MAX_BYTES, MAX_OUTPUT, MAX_PAGES, TEXT_SUFFIX, scope

BATCH = 1
MIN_BYTES = 16 * 1024 * 1024
WARNING = ('Явное чтение одного крупного PDF до 32 МБ и первых 120 страниц, без OCR. '
           'Дата получения источника отделена от даты чтения; оставшиеся страницы и сканы '
           'не считаются прочитанными. Упоминания и таблицы не подтверждают принадлежность '
           'к лоту, геопривязку, права или свободность земли.')
EVIDENCE_FIELDS = ('algorithm', 'unit', 'mentions', 'coordinate_label_pages', 'crs_mentions',
                   'total_pages', 'processed_pages', 'unread_pages', 'image_or_sparse_pages',
                   'text_layer_complete', 'tables', 'unreadable_tables', 'crs_status', 'egrn_tables',
                   'unparsed_coordinate_pages', 'text_engine', 'geometry_confirmed', 'georeferenced')


def _eligible(file):
    return (file.get('format') == 'pdf' and file.get('state') == 'oversized'
            and file.get('eligible') is True and not file.get('metadata_conflict')
            and type(file.get('declared_size')) is int and MIN_BYTES < file['declared_size'] <= MAX_BYTES
            and isinstance(file.get('file_id'), str) and bool(re.fullmatch(r'[0-9a-f]{24}', file['file_id']))
            and isinstance(file.get('key'), str) and bool(re.fullmatch(r'[0-9a-f]{24}', file['key'])))


def queue(result, retry=False):
    if not isinstance(retry, bool):
        raise ValueError('Параметр повтора должен быть логическим')
    pending = []
    for file in (result or {}).get('files', {}).values():
        if not _eligible(file):
            continue
        prior = file.get('large_pdf_attempt') or {}
        if not retry and prior.get('algorithm') == ALGORITHM and prior.get('state') == 'error':
            continue
        pending.append(file)
    return sorted(pending, key=lambda file: (file['declared_size'], file['key']))


def summary(result, lot_id=None):
    files = docs.files_for_lot(list((result or {}).get('files', {}).values()), lot_id)
    pending = docs.files_for_lot(queue(result), lot_id)
    return {'remaining': len(pending), 'retry_remaining': len(docs.files_for_lot(queue(result, True), lot_id)),
            'read_files': sum(scope(file) for file in files),
            'unread_pages': sum(file['unread_pages'] for file in files if scope(file)),
            'max_bytes': MAX_BYTES, 'max_pages': MAX_PAGES, 'batch': BATCH}


def report(project, active=False):
    catalog = store.get_setting(docs.setting_key(project, active))
    search = store.get_setting(torgi.setting_key(project, active))
    return {**summary(catalog), 'catalog_id': (catalog or {}).get('id'),
            'attempt': store.get_setting(docs.setting_key(project, active, '_large_pdf_attempt')),
            'stale': bool(catalog and not docs.same_search(search, catalog)), 'warning': WARNING}


def read_saved(digest):
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Некорректный SHA-256 сохранённого крупного PDF')
    path = store.DATA / 'torgi_documents' / (digest + '.pdf')
    if (not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
        raise ValueError('Сохранённый крупный PDF отсутствует, превышает 32 МБ или изменился')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    try:
        output = subprocess.run([sys.executable, '-m', 'land.torgi_large_pdf_worker', str(path.resolve()), digest],
                                cwd=docs.ROOT, capture_output=True, encoding='utf-8', timeout=60, **options)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Чтение крупного PDF превысило 60 секунд; процесс остановлен') from exc
    if len(output.stdout.encode('utf-8')) > MAX_OUTPUT:
        raise ValueError('Результат чтения крупного PDF превышает 4 МБ')
    details = json.loads(output.stdout)
    if not isinstance(details, dict) or output.returncode or details.get('error'):
        raise ValueError((details.get('error') if isinstance(details, dict) else None) or 'Ошибка чтения крупного PDF')
    if (details.get('source_sha256') != digest or details.get('algorithm') != docs.ALGORITHM
            or details.get('large_pdf_algorithm') != ALGORITHM
            or type(details.get('large_pdf_max_bytes')) is not int or details['large_pdf_max_bytes'] != MAX_BYTES
            or type(details.get('large_pdf_max_pages')) is not int or details['large_pdf_max_pages'] != MAX_PAGES):
        raise ValueError('Чтение крупного PDF относится к другому источнику или алгоритму')
    total, processed, unread = (details.get(key) for key in ('total_pages', 'processed_pages', 'unread_pages'))
    if (any(type(value) is not int for value in (total, processed, unread)) or total < 1
            or processed != min(total, MAX_PAGES) or unread != total - processed
            or not isinstance(details.get('page_range'), list)
            or any(type(value) is not int for value in details['page_range'])
            or details['page_range'] != [1, processed]):
        raise ValueError('Неверное число или нумерация страниц крупного PDF')
    text = path.with_suffix(TEXT_SUFFIX)
    if (not text.is_file() or text.stat().st_size > MAX_OUTPUT
            or hashlib.sha256(text.read_bytes()).hexdigest() != details.get('continued_text_sha256')):
        raise ValueError('Текст крупного PDF отсутствует или изменился')
    if not path.is_file() or path.stat().st_size > MAX_BYTES or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('Сохранённый крупный PDF изменился во время чтения')
    return details


def _assert_snapshot(project, search, expected, active, lot_id):
    docs.assert_current(project, search, expected['id'], active)
    current_search = store.get_setting(torgi.setting_key(project, active))
    current_catalog = store.get_setting(docs.setting_key(project, active))
    if current_search != search or current_catalog != expected:
        raise ValueError('Поиск, каталог или сведения лота изменились; результат не записан')
    if lot_id is not None:
        docs.selected_lot({'lot_id': lot_id}, current_search, current_catalog, active)


def _source(file):
    """Return exact cached bytes or one public download; never substitute stale bytes."""
    digest, received = file.get('sha256'), file.get('received_at')
    folder = store.DATA / 'torgi_documents'
    folder.mkdir(parents=True, exist_ok=True)
    if digest is not None or received is not None:
        if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or not isinstance(received, str) or not received):
            raise ValueError('Неполные SHA-256 или дата сохранённого крупного PDF')
        path = folder / (digest + '.pdf')
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError('Сохранённый крупный PDF отсутствует или превышает 32 МБ')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Сохранённый крупный PDF изменился; повторная загрузка не выполнена')
        metadata = {key: file[key] for key in ('sha256', 'received_at', 'actual_size', 'http_status', 'content_type') if key in file}
        network_requests = 0
    else:
        raw, content_type, status = fetch(docs.FILE + file['file_id'], max_bytes=MAX_BYTES)
        metadata = {'sha256': hashlib.sha256(raw).hexdigest(), 'received_at': store.now(),
                    'actual_size': len(raw), 'http_status': status, 'content_type': content_type}
        network_requests = 1
        path = folder / (metadata['sha256'] + '.pdf')
    if (len(raw) != file['declared_size'] or len(raw) > MAX_BYTES or not raw.startswith(b'%PDF-')
            or metadata.get('http_status') != 200 or metadata.get('actual_size') != len(raw)):
        raise ValueError('Размер, HTTP-статус или сигнатура крупного PDF не соответствуют карточке')
    return path, raw, metadata, network_requests


def run(project, params, active=False):
    old = store.get_setting(docs.setting_key(project, active))
    search = store.get_setting(torgi.setting_key(project, active))
    if not old or params.get('id') != old.get('id') or not docs.same_search(search, old):
        raise ValueError('Каталог документов или поиск изменились; обновите страницу')
    retry = params.get('retry_errors', False)
    if not isinstance(retry, bool):
        raise ValueError('Параметр повтора должен быть логическим')
    lot_id = docs.selected_lot(params, search, old, active)
    result, expected = copy.deepcopy(old), copy.deepcopy(old)
    pending = docs.files_for_lot(queue(result, retry), lot_id)
    if 'file_key' in params:
        key = params['file_key']
        if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{24}', key):
            raise ValueError('Некорректный ключ крупного PDF')
        pending = [file for file in pending if file['key'] == key]
        if not pending:
            raise ValueError('Крупный PDF отсутствует в выбранной очереди или требует явного повтора')
    selected = pending[:BATCH]
    attempt = {'state': 'running', 'started_at': store.now(), 'processed': 0, 'errors': 0,
               'requested': len(selected), 'network_requests': 0, 'cached_files': 0}
    if lot_id is not None:
        attempt['lot_id'] = lot_id
    attempt_key = docs.setting_key(project, active, '_large_pdf_attempt')
    store.set_setting(attempt_key, attempt)
    try:
        for file in selected:
            _assert_snapshot(project, search, expected, active, lot_id)
            attempt['file_key'] = file['key']
            store.set_setting(attempt_key, attempt)
            observed = {'algorithm': ALGORITHM, 'checked_at': store.now(), 'state': 'error',
                        'file_key': file['key'], 'declared_size': file['declared_size']}
            previous = {key: copy.deepcopy(file[key]) for key in EVIDENCE_FIELDS if key in file}
            previous.update({key: file[key] for key in ('sha256', 'received_at') if key in file})
            try:
                # Count an attempted HTTP call even if it times out before yielding bytes.
                cached = file.get('sha256') is not None or file.get('received_at') is not None
                attempt['network_requests'] += 0 if cached else 1
                path, raw, receipt, requests = _source(file)
                _assert_snapshot(project, search, expected, active, lot_id)
                if requests:
                    store.atomic_write(path, raw)
                else:
                    attempt['cached_files'] += 1
                file.update(receipt)
                observed.update(source_sha256=file['sha256'], received_at=file['received_at'])
                details = read_saved(file['sha256'])
                if type(file.get('total_pages')) is int and file['total_pages'] != details['total_pages']:
                    raise ValueError('Число страниц сохранённого крупного PDF изменилось')
                update = {key: copy.deepcopy(details[key]) for key in EVIDENCE_FIELDS if key in details}
                update.update(state='read', large_pdf_previous=previous, large_pdf_algorithm=ALGORITHM,
                              large_pdf_max_bytes=MAX_BYTES, large_pdf_max_pages=MAX_PAGES,
                              large_pdf_source_sha256=file['sha256'], large_pdf_received_at=file['received_at'],
                              large_pdf_read_at=store.now(), continued_text_sha256=details['continued_text_sha256'],
                              large_pdf_text_suffix=TEXT_SUFFIX, geometry_confirmed=False, georeferenced=False)
                file.update(update)
                file.pop('error', None)
                observed.update(state='read', read_at=file['large_pdf_read_at'])
            except Exception as exc:
                observed['error'] = str(exc)[:500]
                attempt['errors'] += 1
            file['large_pdf_attempt'] = observed
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
                store.event(db, project, 'torgi_large_pdf', {'id': result['id'], 'files': attempt['processed'],
                            'errors': attempt['errors'], 'network_requests': attempt['network_requests']})
        return {key: attempt[key] for key in ('processed', 'errors', 'remaining', 'unread_pages',
                                            'network_requests', 'cached_files', 'state')} | ({'lot_id': lot_id} if lot_id is not None else {})
    except Exception as exc:
        attempt.update(state='error', error=str(exc)[:500], finished_at=store.now())
        raise
    finally:
        store.set_setting(attempt_key, attempt)
