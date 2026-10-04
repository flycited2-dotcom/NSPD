"""OCR hints stay separate from source text, legal checks and accepted geometry."""
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone
from . import store, municipal, torgi
from .ocr_worker import ALGORITHM

BATCH_PAGES = 6
ROOT = Path(__file__).resolve().parent.parent
SPACED_CAD = re.compile(r'(?<![\d:])(\d{1,2})\s*:\s*(\d{1,2})\s*:\s*(\d{1,10})\s*:\s*(\d{1,10})(?![\d:])')


def worker(args, timeout):
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    try:
        result = subprocess.run([sys.executable, '-m', 'land.ocr_worker', *args], cwd=ROOT,
                                capture_output=True, encoding='utf-8', timeout=timeout, **options)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('OCR превысил время обработки страницы; процесс остановлен') from exc
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise ValueError('Не получен корректный ответ локального OCR') from exc
    if result.returncode or data.get('error'):
        raise ValueError(data.get('error') or 'Локальный OCR завершился с ошибкой')
    return data


def page_folder(digest):
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Некорректный SHA-256 документа')
    return store.DATA / 'ocr' / digest


def numbers(text):
    # Spaces around colons are accepted; letters and punctuation are never guessed as digits.
    return sorted({torgi.canonical(':'.join(m.groups())) for m in SPACED_CAD.finditer(text)})


def summarize(data):
    if data.get('algorithm') != ALGORITHM or not isinstance(data.get('views'), list) or len(data['views']) != 2:
        raise ValueError('Не получены две версии распознавания')
    sets = []
    views = []
    for i, view in enumerate(data['views']):
        if view.get('view') != i or not isinstance(view.get('text'), str):
            raise ValueError('Структура OCR изменилась')
        if len(view['text']) > 500000 or not re.fullmatch(r'[0-9a-f]{64}', str(view.get('image_sha256', ''))):
            raise ValueError('Некорректный результат OCR')
        sets.append(set(numbers(view['text'])))
        views.append({k: view[k] for k in ('view', 'width', 'height', 'image_sha256', 'characters', 'line_count', 'text_angle')})
    candidates = sorted(sets[0] | sets[1])
    return {'page': data['page'], 'state': 'received', 'processed_at': store.now(), 'algorithm': ALGORITHM,
            'views': views, 'coordinate_label_detected': bool(municipal.evidence([(data['page'], v['text']) for v in data['views']])['coordinate_label_pages']),
            'mentions': [{'cadastral_number': n, 'agreement': 'both' if n in sets[0] & sets[1] else 'one',
                          'verification_required': True} for n in candidates],
            'sparse': any(len(''.join(v['text'].split())) < 20 for v in data['views']), 'confidence_available': False}


def read_page(digest, number, max_pdf_mib=8, max_pdf_pages=40):
    folder = page_folder(digest)
    target = folder / f'p{number}-{ALGORITHM}.json'
    cached = target.exists()
    if not cached:
        path = store.DATA / 'municipal' / (digest + '.pdf')
        worker(['--pdf', str(path.resolve()), '--sha256', digest, '--page', str(number), '--folder', str(folder.resolve()),
                '--max-pdf-mib',str(max_pdf_mib),'--max-pdf-pages',str(max_pdf_pages)], 60)
    data = json.loads(target.read_text(encoding='utf-8'))
    if data.get('source_sha256') != digest or data.get('page') != number:
        raise ValueError('OCR относится к другому документу или странице')
    result = summarize(data)
    result['processed_at'] = data.get('processed_at') or datetime.fromtimestamp(target.stat().st_mtime, timezone.utc).isoformat(timespec='seconds')
    result['cached'] = cached
    # A reused cache is not a new recognition or a new source download.
    if not cached:
        data['processed_at'] = result['processed_at']
        target.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return result


def queue(result, retry=False):
    priority = {'restriction': 0, 'notice_or_auction': 1, 'planning': 2, 'land_document': 3, 'regulation': 4}
    work = []
    for row in sorted(result['items'], key=lambda r: priority[r['kind']]):
        if row['state'] != 'read' or row['format'] != 'pdf' or not row.get('sha256'):
            continue
        prior = row.get('ocr') or {}
        pages = {p['page']: p for p in prior.get('pages', [])} if prior.get('source_sha256') == row['sha256'] else {}
        for n in sorted(set(row.get('image_or_sparse_pages', []))):
            _,limit=municipal.pdf_scope(row)
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= min(row['processed_pages'], limit):
                raise ValueError('Недопустимая страница OCR')
            old = pages.get(n)
            if not old or old.get('algorithm') != ALGORITHM or (retry and old['state'] == 'error'):
                work.append((row, n))
    return work


def run(project, params):
    old = store.get_setting('municipal_' + project)
    if not old or params.get('id') != old['id']:
        raise ValueError('Каталог изменился; обновите страницу')
    retry = params.get('retry_errors', False)
    if not isinstance(retry, bool):
        raise ValueError('Параметр повтора должен быть логическим')
    result = copy.deepcopy(old)
    selected = queue(result, retry)[:BATCH_PAGES]
    attempt = {'state': 'running', 'started_at': store.now(), 'processed': 0, 'requested': len(selected),
               'network_requests': 0, 'retry_errors': retry}
    store.set_setting('municipal_ocr_attempt_' + project, attempt)
    try:
        info = worker(['--probe'], 15) if selected else (old.get('ocr_engine') or {})
        result['ocr_engine'] = info
        attempt['engine'] = info
        for row, number in selected:
            digest = row['sha256']
            page_folder(digest)
            source = store.DATA / 'municipal' / (digest + '.pdf')
            max_mib,max_pages=municipal.pdf_scope(row)
            if not source.is_file() or source.stat().st_size > max_mib * 1024 * 1024 or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                raise ValueError('Сохранённый PDF отсутствует, слишком велик или изменился; OCR остановлен')
            attempt.update(document_id=row['id'], page=number)
            store.set_setting('municipal_ocr_attempt_' + project, attempt)
            prior = row.get('ocr') or {}
            if prior.get('source_sha256') != digest:
                prior = {'source_sha256': digest, 'pages': [], 'verification_required': True}
            try:
                observation = read_page(digest, number,max_mib,max_pages) if max_mib!=8 else read_page(digest, number)
            except Exception as exc:
                observation = {'page': number, 'state': 'error', 'processed_at': store.now(), 'algorithm': ALGORITHM, 'error': str(exc)[:500]}
            prior['pages'] = sorted([p for p in prior['pages'] if p['page'] != number] + [observation], key=lambda p: p['page'])
            row['ocr'] = prior
            attempt['processed'] += 1
            municipal.persist(project, municipal.relate(result, store.get_setting('survey_' + project)))
            store.set_setting('municipal_ocr_attempt_' + project, attempt)
        municipal.persist(project, municipal.relate(result, store.get_setting('survey_' + project)))
        attempt.update(state='done', finished_at=store.now(), remaining=len(queue(result)))
        with store.connect() as db:
            store.event(db, project, 'municipal_ocr', {'id': result['id'], 'pages': len(selected), 'network_requests': 0})
        return {'processed': len(selected), 'remaining': len(queue(result))}
    except Exception as exc:
        attempt.update(state='error', error=str(exc)[:500], finished_at=store.now())
        raise
    finally:
        store.set_setting('municipal_ocr_attempt_' + project, attempt)


def preview(project, document_id, number, view):
    if view not in (0, 1) or isinstance(number, bool) or not isinstance(number, int):
        raise ValueError('Некорректная страница')
    result = store.get_setting('municipal_' + project, {}) or {}
    row = next((r for r in result.get('items', []) if r['id'] == document_id), None)
    observation = next((p for p in (row or {}).get('ocr', {}).get('pages', []) if p['page'] == number and p['state'] == 'received'), None)
    if not row or not observation or row['ocr'].get('source_sha256') != row.get('sha256'):
        raise ValueError('Страница не принадлежит текущему документу OCR')
    path = page_folder(row['sha256']) / f'p{number}-v{view}.png'
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != observation['views'][view]['image_sha256']:
        raise ValueError('Изображение страницы изменилось')
    return raw
