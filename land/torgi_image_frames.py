"""Explicit source-bound auxiliary MPO previews, separate from primary files/OCR."""
import copy
import hashlib
import json
import re
import subprocess
import sys
from html import escape
from urllib.parse import quote

from . import image_evidence, image_frames, store, torgi, torgi_docs as docs
from .torgi_image_frame_worker import ALGORITHM, MAX_OUTPUT, MAX_PNG

MAX_FRAMES = image_frames.MAX_SOURCE_FRAMES - 1
WARNING = ('Локально декодируется только явно выбранный вспомогательный кадр MPO. '
           'Число кадров объявлено контейнером; каждый кадр проверяется отдельно. '
           'OCR, подписи, геопривязка, права и свободность земли не подтверждены. '
           'Основной кадр, его OCR и даты получения источника не изменяются; '
           'ошибки повторяются только по явному запросу.')


def setting_key(project, active=False, suffix=''):
    if type(active) is not bool:
        raise ValueError('Выбор регионального каталога должен быть логическим')
    return docs.setting_key(project, active, '_image_frames' + suffix)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _eligible(file):
    if (not isinstance(file, dict) or file.get('state') != 'read' or file.get('eligible') is not True
            or not isinstance(file.get('key'), str) or not re.fullmatch(r'[0-9a-f]{24}', file['key'])
            or file.get('metadata_conflict') or file.get('format') not in ('jpg', 'jpeg')
            or not isinstance(file.get('received_at'), str) or not file['received_at']
            or not isinstance(file.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', file['sha256'])
            or type(file.get('declared_size')) is not int or not 0 < file['declared_size'] <= image_frames.MAX_BYTES):
        return False
    try:
        primary = image_evidence.scope(file.get('image'))
        return primary['source_format'] == 'MPO' and primary['source_frame_count'] >= 2
    except ValueError:
        return False


def binding(catalog, search, file):
    """Full current catalog/search identity plus the exact dated source/frame scope."""
    if not docs.same_search(search, catalog) or not _eligible(file):
        raise ValueError('Каталог или принятый источник MPO не соответствует текущему поиску')
    return {'algorithm': ALGORITHM, 'catalog_id': catalog.get('id'), 'catalog_sha256': _hash(catalog),
            'search_id': search.get('id'), 'search_source_id': torgi.source_id(search),
            'search_sha256': _hash(search), 'file_key': file['key'], 'source_sha256': file['sha256'],
            'source_received_at': file['received_at'], 'source_format': file['format'],
            'primary_scope': image_evidence.scope(file['image'])}


def frame_scope(file, index):
    primary = image_evidence.scope(file['image'])
    return image_frames.scope({'algorithm': ALGORITHM, 'source_format': 'MPO',
                              'source_frame_count': primary['source_frame_count'],
                              'source_frame_index': index, 'complete_frame': True, 'complete_source': False})


def source_path(file):
    if not _eligible(file):
        raise ValueError('Нужен принятый текущий источник MPO без конфликта метаданных')
    path = store.DATA / 'torgi_documents' / (file['sha256'] + '.' + file['format'])
    if not path.is_file() or path.stat().st_size > image_frames.MAX_BYTES:
        raise ValueError('Сохранённый MPO отсутствует или превышает 8 МБ')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != file['sha256']:
        raise ValueError('SHA-256 сохранённого MPO изменился')
    docs.validate_download(file, raw)
    return path


def folder(identity):
    return store.DATA / 'torgi_image_frames' / _hash(identity)


def _observation(state, identity, file, index):
    entry = (state or {}).get('entries', {}).get(_hash(identity), {})
    if entry.get('binding') != identity:
        return None
    observed = entry.get('frames', {}).get(str(index))
    if (not isinstance(observed, dict) or observed.get('binding_id') != _hash(identity)
            or observed.get('algorithm') != ALGORITHM or observed.get('state') not in ('received', 'error')
            or observed.get('source_sha256') != file['sha256'] or observed.get('source_format') != file['format']
            or not isinstance(observed.get('processed_at'), str)):
        return None
    try:
        if image_frames.scope(observed.get('frame_scope')) != frame_scope(file, index):
            return None
    except ValueError:
        return None
    if observed['state'] == 'received' and (not isinstance(observed.get('png_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', observed['png_sha256']) or observed.get('ocr_read') is not False):
        return None
    return observed


def queue(catalog, search, state=None, retry=False, lot_id=None):
    """Describe explicit work only; this function performs no I/O or retries."""
    if type(retry) is not bool:
        raise ValueError('Параметр повтора должен быть логическим')
    if not docs.same_search(search, catalog):
        return []
    work = []
    files = docs.files_for_lot(list((catalog or {}).get('files', {}).values()), lot_id)
    for file in files:
        if not _eligible(file):
            continue
        identity = binding(catalog, search, file)
        frames = []
        for index in range(1, identity['primary_scope']['source_frame_count']):
            observed = _observation(state, identity, file, index)
            if observed is None or (retry and observed['state'] == 'error'):
                frames.append(index)
        if frames:
            work.append({'file_key': file['key'], 'frames': frames})
    return sorted(work, key=lambda item: item['file_key'])


def summary(catalog, search, state=None, lot_id=None):
    files = docs.files_for_lot(list((catalog or {}).get('files', {}).values()), lot_id)
    current = []
    if docs.same_search(search, catalog):
        for file in files:
            if _eligible(file):
                identity = binding(catalog, search, file)
                current.extend(_observation(state, identity, file, index)
                               for index in range(1, identity['primary_scope']['source_frame_count']))
    return {'declared_auxiliary_frames': len(current),
            'received': sum(bool(item and item['state'] == 'received') for item in current),
            'errors': sum(bool(item and item['state'] == 'error') for item in current),
            'remaining': sum(len(item['frames']) for item in queue(catalog, search, state, lot_id=lot_id)),
            'retry_remaining': sum(len(item['frames']) for item in queue(catalog, search, state, True, lot_id)),
            'max_frames': MAX_FRAMES, 'primary_not_modified': True, 'ocr_read': False}


def report(project, active=False,lot_id=None):
    catalog = store.get_setting(docs.setting_key(project, active)) or {}
    search = store.get_setting(torgi.setting_key(project, active)) or {}
    state = store.get_setting(setting_key(project, active)) or {}
    files = []
    if docs.same_search(search, catalog):
        for file in docs.files_for_lot(list(catalog.get('files', {}).values()),lot_id):
            if not _eligible(file):
                continue
            identity = binding(catalog, search, file)
            frames = [_observation(state, identity, file, index)
                      for index in range(1, identity['primary_scope']['source_frame_count'])]
            files.append({'file_key': file['key'], 'file_name': file.get('file_name'),
                          'source_sha256': file['sha256'], 'source_received_at': file['received_at'],
                          'declared_frame_count': identity['primary_scope']['source_frame_count'],
                          'frames': [copy.deepcopy(item) for item in frames if item]})
    attempt = store.get_setting(setting_key(project, active, '_attempt'))
    current_ids = {_hash(binding(catalog, search, file)) for file in catalog.get('files', {}).values()
                   if docs.same_search(search, catalog) and _eligible(file)}
    return {**summary(catalog, search, state,lot_id), 'catalog_id': catalog.get('id'),
            'search_id': search.get('id'), 'files': files, 'attempt': attempt,
            'attempt_stale': bool(attempt and attempt.get('binding_id') not in current_ids),
            'stale': bool(catalog and not docs.same_search(search, catalog)), 'warning': WARNING}


def _read_cache(identity, file, index):
    target = folder(identity)
    result_path, png_path = target / f'f{index}.json', target / f'f{index}.png'
    if not result_path.is_file() or result_path.stat().st_size > MAX_OUTPUT:
        raise ValueError('Сведения вспомогательного кадра отсутствуют или превышают 64 КБ')
    data = json.loads(result_path.read_text(encoding='utf-8'))
    if (not isinstance(data, dict) or data.get('algorithm') != ALGORITHM
            or data.get('source_sha256') != file['sha256'] or data.get('source_format') != file['format']
            or data.get('binding_id') != _hash(identity) or data.get('ocr_read') is not False
            or data.get('source_frame_count_basis') != 'container_header'
            or data.get('primary_preview_algorithm') != image_evidence.ALGORITHM
            or type(data.get('orientation_applied')) is not bool
            or not isinstance(data.get('processed_at'), str) or not data['processed_at']
            or image_frames.scope(data.get('frame_scope')) != frame_scope(file, index)):
        raise ValueError('Вспомогательный кадр относится к другому источнику, каталогу или области чтения')
    for key in ('source_width', 'source_height', 'preview_width', 'preview_height'):
        if type(data.get(key)) is not int or not 1 <= data[key] <= image_frames.MAX_SIDE:
            raise ValueError('Некорректный размер вспомогательного кадра')
    if (data['source_width'] * data['source_height'] > image_frames.MAX_PIXELS
            or max(data['preview_width'], data['preview_height']) > 2000):
        raise ValueError('Размер вспомогательного кадра превышает лимит')
    if not png_path.is_file() or png_path.stat().st_size > MAX_PNG:
        raise ValueError('Предпросмотр вспомогательного кадра отсутствует или превышает 16 МБ')
    raw = png_path.read_bytes()
    if (not raw.startswith(b'\x89PNG\r\n\x1a\n') or not isinstance(data.get('png_sha256'), str)
            or hashlib.sha256(raw).hexdigest() != data['png_sha256']):
        raise ValueError('Предпросмотр вспомогательного кадра изменился')
    return data, raw


def read_frame(file, index, identity, retry=False):
    frame_scope(file, index)
    path = source_path(file)
    target = folder(identity) / f'f{index}.json'
    cached = target.exists() and not retry
    if not cached:
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        try:
            output = subprocess.run([sys.executable, '-m', 'land.torgi_image_frame_worker', str(path.resolve()),
                                     file['sha256'], file['format'], str(index), str(folder(identity).resolve()),
                                     _hash(identity)], cwd=docs.ROOT, capture_output=True, encoding='utf-8',
                                    timeout=60, **options)
        except subprocess.TimeoutExpired as exc:
            raise ValueError('Декодирование вспомогательного кадра превысило 60 секунд; процесс остановлен') from exc
        if len(output.stdout.encode('utf-8')) > MAX_OUTPUT:
            raise ValueError('Ответ декодера вспомогательного кадра превышает 64 КБ')
        try:
            result = json.loads(output.stdout)
        except (ValueError, TypeError) as exc:
            raise ValueError('Не получен корректный ответ декодера вспомогательного кадра') from exc
        if not isinstance(result, dict) or output.returncode or result.get('error'):
            raise ValueError(result.get('error') if isinstance(result, dict) else 'Ошибка декодера кадра MPO')
    data, _ = _read_cache(identity, file, index)
    if not cached and data != result:
        raise ValueError('Ответ декодера отличается от сохранённых сведений кадра')
    source_path(file)  # A cached result also requires the exact source to still exist.
    return {**data, 'state': 'received', 'cached': cached}


def _assert_snapshot(project, catalog, search, expected_state, active):
    docs.assert_current(project, search, catalog['id'], active)
    if (store.get_setting(docs.setting_key(project, active)) != catalog
            or store.get_setting(torgi.setting_key(project, active)) != search
            or (store.get_setting(setting_key(project, active)) or {}) != expected_state):
        raise ValueError('Каталог, поиск или сведения вспомогательных кадров изменились; результат не записан')


def run(project, params, active=False):
    if not isinstance(params, dict):
        raise ValueError('Нужны параметры выбранного файла и кадров MPO')
    catalog = store.get_setting(docs.setting_key(project, active)) or {}
    search = store.get_setting(torgi.setting_key(project, active)) or {}
    if not catalog or params.get('id') != catalog.get('id') or not docs.same_search(search, catalog):
        raise ValueError('Каталог или поиск изменился; обновите страницу')
    key = params.get('file_key')
    if not isinstance(key, str) or key not in catalog.get('files', {}):
        raise ValueError('Файл не принадлежит текущему каталогу')
    file = catalog['files'][key]
    identity = binding(catalog, search, file)
    lot_id = docs.selected_lot(params, search, catalog, active)
    if not docs.files_for_lot([file], lot_id):
        raise ValueError('Выбранный файл не относится к текущему лоту')
    frames, retry = params.get('frames'), params.get('retry_errors', False)
    if type(retry) is not bool:
        raise ValueError('Параметр повтора должен быть логическим')
    if (not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES
            or any(type(index) is not int for index in frames) or len(set(frames)) != len(frames)):
        raise ValueError('Нужен явный список от 1 до 15 различных целых индексов кадров')
    for index in frames:
        frame_scope(file, index)
    source_path(file)  # Even an already-decoded selection must still have the exact cached source.
    expected = store.get_setting(setting_key(project, active)) or {}
    state = copy.deepcopy(expected)
    state.setdefault('entries', {})
    entry = state['entries'].setdefault(_hash(identity), {'binding': identity, 'frames': {}})
    selected = [index for index in sorted(frames)
                if (old := _observation(state, identity, file, index)) is None or (retry and old['state'] == 'error')]
    attempt = {'algorithm': ALGORITHM, 'binding_id': _hash(identity), 'catalog_id': catalog['id'],
               'file_key': key, 'state': 'running', 'started_at': store.now(),
               'requested': len(selected), 'processed': 0, 'received': 0, 'errors': 0, 'network_requests': 0,
               'retry_errors': retry}
    attempt_key = setting_key(project, active, '_attempt')
    store.set_setting(attempt_key, attempt)
    try:
        for index in selected:
            _assert_snapshot(project, catalog, search, expected, active)
            attempt['frame_index'] = index
            store.set_setting(attempt_key, attempt)
            try:
                observed = read_frame(file, index, identity, retry=retry)
            except Exception as exc:
                observed = {'algorithm': ALGORITHM, 'binding_id': _hash(identity),
                            'source_sha256': file['sha256'], 'source_format': file['format'],
                            'frame_scope': frame_scope(file, index), 'processed_at': store.now(),
                            'state': 'error', 'error': str(exc)[:500], 'ocr_read': False}
            prior = entry['frames'].get(str(index))
            if prior:
                observed['previous'] = [*prior.get('previous', []),
                                        {key: value for key, value in prior.items() if key != 'previous'}][-10:]
            entry['frames'][str(index)] = observed
            state.update(algorithm=ALGORITHM, updated_at=store.now())
            with store.LOCK:
                _assert_snapshot(project, catalog, search, expected, active)
                if observed['state'] == 'received':
                    source_path(file)
                store.set_setting(setting_key(project, active), state)
                expected = copy.deepcopy(state)
            attempt['processed'] += 1
            attempt['errors' if observed['state'] == 'error' else 'received'] += 1
            store.set_setting(attempt_key, attempt)
        counts = summary(catalog, search, state, lot_id)
        attempt.update(state='partial' if attempt['errors'] else 'done', finished_at=store.now(),
                       remaining=counts['remaining'], retry_remaining=counts['retry_remaining'])
        if selected:
            with store.connect() as db:
                store.event(db, project, 'torgi_image_aux_frames', {'catalog_id': catalog['id'], 'file_key': key,
                                                               'processed': attempt['processed'], 'errors': attempt['errors'],
                                                               'network_requests': 0})
        return {key: attempt[key] for key in ('processed', 'received', 'errors', 'remaining',
                                             'retry_remaining', 'network_requests', 'state')} | {'file_key': key}
    except Exception as exc:
        attempt.update(state='error', error=str(exc)[:500], finished_at=store.now())
        raise
    finally:
        store.set_setting(attempt_key, attempt)


def frame_preview(project, key, index, active=False):
    catalog = store.get_setting(docs.setting_key(project, active)) or {}
    search = store.get_setting(torgi.setting_key(project, active)) or {}
    if not docs.same_search(search, catalog):
        raise ValueError('Каталог относится к другому поиску; обновите страницу')
    if not isinstance(key, str) or key not in catalog.get('files', {}):
        raise ValueError('Файл не принадлежит текущему каталогу')
    file = catalog['files'][key]
    identity = binding(catalog, search, file)
    frame_scope(file, index)
    state = store.get_setting(setting_key(project, active)) or {}
    observed = _observation(state, identity, file, index)
    if not observed or observed['state'] != 'received':
        raise ValueError('Кадр не принадлежит текущему принятому источнику и каталогу')
    source_path(file)
    data, raw = _read_cache(identity, file, index)
    if any(observed.get(key) != data.get(key) for key in data):
        raise ValueError('Предпросмотр не соответствует принятому наблюдению кадра')
    with store.LOCK:
        _assert_snapshot(project, catalog, search, state, active)
        source_path(file)
    return raw


def html_report(project,active=False,lot_id=None):
    result=report(project,active,lot_id)
    base='/api/torgi/active/documents' if active else '/api/torgi/documents'
    rows=[]
    for file in result['files']:
        rows.append('<h2>'+escape(str(file.get('file_name') or file['file_key']))+'</h2><p>Источник SHA-256 '+escape(file['source_sha256'])+' · получен '+escape(file['source_received_at'])+'</p>')
        for frame in file['frames']:
            index=frame['frame_scope']['source_frame_index']
            rows.append('<h3>Кадр '+str(index+1)+' из '+str(file['declared_frame_count'])+'</h3><p>'+escape(frame['state'])+' · '+escape(frame['processed_at'])+' · OCR не выполнено</p>')
            if frame['state']=='received':
                rows.append('<p>PNG SHA-256 '+escape(frame['png_sha256'])+'</p><img style="max-width:100%" alt="Дополнительный кадр MPO" src="'+base+'/frames/image?file='+quote(file['file_key'],safe='')+'&frame='+str(index)+'">')
            else:rows.append('<p>'+escape(frame.get('error',''))+'</p>')
    return '<!doctype html><meta charset="utf-8"><title>Дополнительные кадры MPO</title><h1>Дополнительные кадры MPO</h1><p>'+escape(WARNING)+'</p>'+''.join(rows)
