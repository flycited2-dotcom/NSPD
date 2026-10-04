"""Explicit bounded retrieval of known municipal PDFs rejected by the 8 MiB cap."""
import copy
import time
from . import municipal, municipal_local, store
from .network import fetch, ResponseTooLarge

ALGORITHM = municipal.LARGE_PDF_ALGORITHM
MAX_BYTES = 32 * 1024 * 1024
BATCH = 3
OLD_ERROR = 'Ответ превышает 8 МБ'
PARSE_SCOPE = [32, 200, 'PDFium']


def queue(result, retry=False):
    def eligible(row):
        if row.get('format') != 'pdf' or (row.get('previous_read_error') or row.get('error')) != OLD_ERROR:
            return False
        # A received result from the first pypdf attempt can be upgraded locally.
        if row.get('state') == 'read' and row.get('large_pdf_algorithm') == ALGORITHM:
            needs_upgrade=(row.get('text_engine') != 'PDFium'
                           or row.get('processed_pages',0) < min(row.get('total_pages',0),200))
            return needs_upgrade and (row.get('large_pdf_attempt_scope') != PARSE_SCOPE or retry)
        return (row.get('state') == 'rejected' and row.get('large_pdf_algorithm') != ALGORITHM
                and (row.get('large_pdf_attempt_algorithm') != ALGORITHM or retry))
    return sorted((row for row in (result or {}).get('items', []) if eligible(row)), key=lambda row: row['id'])


def run(project, params):
    old = store.get_setting('municipal_' + project)
    if not old or params.get('id') != old['id']:
        raise ValueError('Каталог изменился; обновите страницу')
    retry = params.get('retry_errors', False)
    if not isinstance(retry, bool):
        raise ValueError('Параметр повтора должен быть логическим')
    result = copy.deepcopy(old)
    selected = queue(result, retry)[:BATCH]
    attempt = {'state': 'running', 'started_at': store.now(), 'requested': len(selected),
               'processed': 0, 'network_requests': 0, 'retry_errors': retry}
    store.set_setting('municipal_large_attempt_' + project, attempt)
    try:
        for row in selected:
            attempt['document_id'] = row['id']
            store.set_setting('municipal_large_attempt_' + project, attempt)
            if municipal.safe_link(row['parent_url'], row['url']) != row['url']:
                raise ValueError('Недопустимая ссылка документа')
            row.setdefault('previous_read_error', row.get('error') or OLD_ERROR)
            if row.get('checked_at'):
                row.setdefault('previous_checked_at', row['checked_at'])
            if row.get('last_error'):
                row.setdefault('previous_last_error', row['last_error'])
                row.setdefault('previous_last_error_at', row.get('last_error_at'))
            row['large_pdf_attempt_algorithm'] = ALGORITHM
            row['large_pdf_attempt_scope'] = PARSE_SCOPE.copy()
            try:
                if not row.get('sha256'):
                    attempt['network_requests'] += 1
                    store.set_setting('municipal_large_attempt_' + project, attempt)
                    raw, content_type, code = fetch(row['url'], max_bytes=MAX_BYTES)
                    row.update(sha256=municipal.save_raw(raw, 'pdf'), received_at=store.now(),
                               http_status=code, source_bytes=len(raw), source_content_type=content_type)
                try:
                    details = municipal_local.read_saved(row['sha256'], max_mib=32)
                    row.update(details, state='read', locally_read_at=store.now())
                    row.pop('source_sha256', None)
                    for key in ('error', 'last_error', 'last_error_at', 'large_pdf_error'):
                        row.pop(key, None)
                except Exception as exc:
                    row.update(large_pdf_error=str(exc)[:500], locally_read_at=store.now())
                    if row['state'] != 'read':
                        row['error'] = str(exc)[:500]
            except ResponseTooLarge as exc:
                row.update(error=str(exc)[:500], large_pdf_error=str(exc)[:500], checked_at=store.now())
            except Exception as exc:
                row.update(last_error=str(exc)[:500], last_error_at=store.now())
                municipal.persist(project, municipal.relate(result, store.get_setting('survey_' + project)))
                raise
            attempt['processed'] += 1
            municipal.persist(project, municipal.relate(result, store.get_setting('survey_' + project)))
            store.set_setting('municipal_large_attempt_' + project, attempt)
            if row is not selected[-1]:
                time.sleep(1)
        attempt.update(state='done', finished_at=store.now(), remaining=len(queue(result)),
                       failed=sum(row['state'] != 'read' or bool(row.get('large_pdf_error')) for row in selected))
        with store.connect() as db:
            store.event(db, project, 'municipal_large_pdf', {'id': result['id'], 'documents': attempt['processed'],
                                                          'network_requests': attempt['network_requests']})
        return {key: attempt[key] for key in ('processed', 'remaining', 'failed', 'network_requests')}
    except Exception as exc:
        attempt.update(state='error', error=str(exc)[:500], finished_at=store.now())
        raise
    finally:
        store.set_setting('municipal_large_attempt_' + project, attempt)
