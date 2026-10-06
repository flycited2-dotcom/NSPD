"""Local OCR and previews for downloaded Torgi scans; hints never become geometry."""
import copy
import hashlib
import json
import re
from . import store, torgi_docs, ocr, image_evidence

BATCH = 6


def source_path(file):
    digest, fmt = file.get('sha256'), file.get('format')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or fmt not in ('pdf', *image_evidence.FORMATS):
        raise ValueError('Некорректный источник визуального документа')
    path = store.DATA / 'torgi_documents' / (digest + '.' + fmt)
    if not path.is_file() or path.stat().st_size > torgi_docs.file_limit(fmt) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('Сохранённый источник отсутствует, слишком велик или изменился')
    return path


def folder(file):
    digest, fmt = file['sha256'], file['format']
    if not re.fullmatch(r'[0-9a-f]{64}', digest) or fmt not in ('pdf', *image_evidence.FORMATS):
        raise ValueError('Некорректный идентификатор OCR')
    return store.DATA / 'torgi_ocr' / (digest + '-' + fmt)


def queue(result, retry=False):
    def priority(file):
        return 0 if any(word in file.get('file_name', '').lower() for word in ('схем', 'егрн', 'выписк', 'кадастр')) else 1
    pending = []
    for file in sorted(result.get('files', {}).values(), key=priority):
        if file['state'] != 'read' or not file.get('eligible') or not file.get('sha256'):
            continue
        units = [1] if file.get('format') in image_evidence.FORMATS else sorted(set(file.get('image_or_sparse_pages', []))) if file.get('format') == 'pdf' else []
        prior = file.get('visual_ocr') or {}
        old = {p['page']: p for p in prior.get('pages', [])} if prior.get('source_sha256') == file['sha256'] else {}
        for number in units:
            limit = min(file.get('processed_pages', 1), 40) if file['format'] == 'pdf' else 1
            if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= limit:
                raise ValueError('Недопустимая страница OCR торгов')
            item = old.get(number)
            if not item or item.get('algorithm') != ocr.ALGORITHM or (retry and item['state'] == 'error'):
                pending.append((file, number))
    return pending


def read_unit(file, number):
    path = source_path(file)  # validate even when using cached recognition
    target_folder = folder(file)
    target = target_folder / f'p{number}-{ocr.ALGORITHM}.json'
    cached = target.exists()
    if not cached:
        args = ['--pdf', str(path.resolve()), '--max-pdf-mib', '16'] if file['format'] == 'pdf' else ['--image', str(path.resolve()), '--source-format', file['format']]
        ocr.worker([*args, '--sha256', file['sha256'], '--page', str(number), '--folder', str(target_folder.resolve())], 60)
    if not target.is_file() or target.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Результат OCR отсутствует или слишком велик')
    data = json.loads(target.read_text(encoding='utf-8'))
    if data.get('source_sha256') != file['sha256'] or data.get('source_format') != file['format'] or data.get('page') != number:
        raise ValueError('OCR относится к другому источнику/странице')
    observation = ocr.summarize(data)
    for view in observation['views']:
        image = target_folder / f'p{number}-v{view["view"]}.png'
        if not image.is_file() or image.stat().st_size > 16 * 1024 * 1024 or hashlib.sha256(image.read_bytes()).hexdigest() != view['image_sha256']:
            raise ValueError('Изображение OCR отсутствует или изменилось')
    observation.update(processed_at=data['processed_at'], cached=cached, unit='image' if file['format'] in image_evidence.FORMATS else 'pdf_page')
    return observation


def run(project, params):
    old = store.get_setting('torgi_documents_' + project)
    search = store.get_setting('torgi_' + project)
    if not old or params.get('id') != old['id'] or not torgi_docs.same_search(search,old):
        raise ValueError('Каталог/поиск изменился; обновите страницу')
    retry = params.get('retry_errors', False)
    if not isinstance(retry, bool):
        raise ValueError('Параметр повтора должен быть логическим')
    result = copy.deepcopy(old)
    selected = queue(result, retry)[:BATCH]
    attempt = {'state': 'running', 'started_at': store.now(), 'processed': 0, 'requested': len(selected), 'network_requests': 0}
    store.set_setting('torgi_visual_attempt_' + project, attempt)
    try:
        info = ocr.worker(['--probe'], 15) if selected else old.get('visual_engine', {})
        result['visual_engine'] = info
        for file, number in selected:
            source_path(file)  # a changed source stops the job, preserving prior observations
            attempt.update(file_key=file['key'], page=number)
            store.set_setting('torgi_visual_attempt_' + project, attempt)
            prior = file.get('visual_ocr') or {}
            if prior.get('source_sha256') != file['sha256']:
                prior = {'source_sha256': file['sha256'], 'pages': [], 'verification_required': True}
            try:
                observation = read_unit(file, number)
            except Exception as exc:
                observation = {'page': number, 'state': 'error', 'processed_at': store.now(), 'algorithm': ocr.ALGORITHM, 'error': str(exc)[:500]}
            prior['pages'] = sorted([p for p in prior['pages'] if p['page'] != number] + [observation], key=lambda p: p['page'])
            file['visual_ocr'] = prior
            attempt['processed'] += 1
            torgi_docs.persist(project, result)
            store.set_setting('torgi_visual_attempt_' + project, attempt)
        torgi_docs.persist(project, result)
        attempt.update(state='done', finished_at=store.now(), remaining=len(queue(result)))
        with store.connect() as db:
            store.event(db, project, 'torgi_visual_ocr', {'id': result['id'], 'units': attempt['processed'], 'network_requests': 0})
        return {'processed': attempt['processed'], 'remaining': attempt['remaining'], 'network_requests': 0}
    except Exception as exc:
        attempt.update(state='error', error=str(exc)[:500], finished_at=store.now())
        raise
    finally:
        store.set_setting('torgi_visual_attempt_' + project, attempt)


def preview(project, key, number=None, view=None):
    result = store.get_setting('torgi_documents_' + project, {}) or {}
    file = result.get('files', {}).get(key)
    if not file or file['state'] != 'read':
        raise ValueError('Файл не принадлежит текущему каталогу')
    source_path(file)
    if number is None:
        if file['format'] not in image_evidence.FORMATS:
            raise ValueError('Ожидалось изображение')
        path = store.DATA / 'torgi_documents' / (file['sha256'] + '.preview.png')
        digest = file['image']['preview_sha256']
    else:
        if isinstance(number, bool) or not isinstance(number, int) or view not in (0, 1) or isinstance(view, bool):
            raise ValueError('Некорректная страница/версия OCR')
        prior = file.get('visual_ocr') or {}
        observation = next((p for p in prior.get('pages', []) if p['page'] == number and p['state'] == 'received'), None)
        if prior.get('source_sha256') != file['sha256'] or not observation:
            raise ValueError('Страница не принадлежит текущему OCR')
        path = folder(file) / f'p{number}-v{view}.png'
        digest = observation['views'][view]['image_sha256']
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Изображение отсутствует или слишком велико')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Изображение изменилось')
    return raw
