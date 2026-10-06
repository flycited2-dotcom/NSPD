"""Source-bound PZZ map pages and parcel-number hints, never zoning polygons."""
import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode
from shapely.geometry import box, shape
from . import store, planning_watch, torgi

ALGORITHM = 'pzz-map-pairs-v2'
RENDER_ALGORITHM = 'pzz-map-poppler-2400-v1'
MAX_DOCUMENTS, MAX_BYTES = 16, 32 * 1024 * 1024
MAX_MAP_PAGES = 24
ROOT = Path(__file__).resolve().parent.parent
MAP_HEADING = re.compile(r'фрагмент\s+карты\s+градостроительного\s*зонирования', re.I)
ZONE = re.compile(r'(?<![А-Яа-яЁё0-9])(?:ОД|СХ|ИТ|Ж|О|П|Р|С|Т)\d+(?:\.\d+){0,2}(?![А-Яа-яЁё0-9])')
WARNING = ('Это карты из ограниченного каталога изменений ПЗЗ, не действующая сводная карта. '
           'Номера и коды зон — упоминания текста. Совпадение номера сопоставляет документ с кадастровым объектом, '
           'но не устанавливает границу зоны. Отсутствие совпадений не исключает действие регламентов или ограничений. '
           'Растровые фрагменты без проверенной привязки не участвуют в фильтрации свободной земли.')


def selected(catalog):
    rows = [r for r in (catalog or {}).get('items', []) if r.get('currently_listed') and r.get('state') == 'read'
            and planning_watch.is_pzz(r)]
    if len(rows) > MAX_DOCUMENTS:
        raise ValueError('Слишком много PDF ПЗЗ для ограниченного анализа')
    return rows


def pdf_path(sha):
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
        raise ValueError('Некорректный SHA-256 PDF ПЗЗ')
    path = store.DATA / 'planning_watch' / (sha + '.pdf')
    if not path.is_file() or path.stat().st_size > MAX_BYTES or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise ValueError('PDF ПЗЗ отсутствует, превышает 32 МБ или изменился')
    return path


def worker(mode, sha, page=None):
    path = pdf_path(sha)
    args = [sys.executable, '-m', 'land.planning_map_worker', mode, str(path.resolve()), sha]
    if mode == 'render':
        args += [str(page), str((store.DATA / 'planning_maps' / sha).resolve())]
    opts = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    try:
        output = subprocess.run(args, cwd=ROOT, capture_output=True, encoding='utf-8', timeout=60, **opts)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Обработка карты ПЗЗ превысила 60 секунд; процесс остановлен') from exc
    if len(output.stdout) > 4 * 1024 * 1024:
        raise ValueError('Вывод обработки карты превышает лимит')
    result = json.loads(output.stdout)
    if output.returncode or result.get('error'):
        raise ValueError(result.get('error') or 'Ошибка обработки карты ПЗЗ')
    algorithm = ALGORITHM if mode == 'inspect' else RENDER_ALGORITHM
    if result.get('source_sha256') != sha or result.get('algorithm') != algorithm or (mode == 'render' and result.get('page') != page):
        raise ValueError('Результат относится к другому PDF/странице/алгоритму')
    return result


def inspect_pdf(sha):
    pdf_path(sha)
    folder = store.DATA / 'planning_maps' / sha
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (ALGORITHM + '.json')
    if path.exists():
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('Кеш карт ПЗЗ превышает 4 МБ')
        result = json.loads(path.read_text(encoding='utf-8'))
        saved_hash = result.pop('content_sha256', None)
        if result.get('source_sha256') != sha or result.get('algorithm') != ALGORITHM or saved_hash != planning_watch.digest(result):
            raise ValueError('Кеш карт ПЗЗ относится к другому источнику')
        validate_index(result)
        return dict(result, cached=True)
    result = worker('inspect', sha)
    validate_index(result)
    path.write_text(json.dumps(dict(result, content_sha256=planning_watch.digest(result)), ensure_ascii=False, indent=2), encoding='utf-8')
    return dict(result, cached=False)


def validate_index(result):
    total, processed = result.get('total_pages'), result.get('processed_pages')
    if type(total) is not int or type(processed) is not int or not 1 <= processed <= min(total, 200) or result.get('unread_pages') != total - processed:
        raise ValueError('Некорректный предел страниц карты ПЗЗ')
    pages = result.get('map_pages')
    if not isinstance(pages, list) or len(pages) > MAX_MAP_PAGES:
        raise ValueError('Картографических страниц больше разрешённого предела')
    values = [p.get('page') for p in pages]
    if any(type(n) is not int or not 1 <= n <= processed for n in values) or len(set(values)) != len(values):
        raise ValueError('Некорректные/повторённые страницы карты ПЗЗ')
    refs = result.get('map_reference_pages', [])
    if not isinstance(refs, list) or len(refs) > 200:
        raise ValueError('Некорректные текстовые ссылки на карты')
    all_pages = pages + refs
    numbers = [p.get('page') for p in all_pages]
    if any(type(n) is not int or not 1 <= n <= processed for n in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError('Некорректные/повторённые ссылки на страницы')
    for page in all_pages:
        if page.get('geometry_confirmed') is not False or any(type(page.get(k)) is not bool for k in ('old_edition_label', 'new_edition_label', 'coordinate_label_present')):
            raise ValueError('Не подтверждена структура наблюдения карты')
        if not isinstance(page.get('number_mentions'), list) or len(page['number_mentions']) > 200 or any(not isinstance(n, str) or not torgi.CAD.fullmatch(n) for n in page['number_mentions']):
            raise ValueError('Некорректные упоминания номеров карты')
        if not isinstance(page.get('zone_hints'), list) or len(page['zone_hints']) > 100 or any(not isinstance(n, str) or not ZONE.fullmatch(n) for n in page['zone_hints']):
            raise ValueError('Некорректные подсказки зон карты')


def image_path(sha, page):
    return store.DATA / 'planning_maps' / sha / f'p{page}-{RENDER_ALGORITHM}.png'


def checked_image(sha, page, details):
    path = image_path(sha, page)
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Изображение карты отсутствует или превышает лимит')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != details.get('image_sha256'):
        raise ValueError('SHA-256 изображения карты изменился')
    from PIL import Image
    import io
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != 'PNG' or getattr(image, 'n_frames', 1) != 1 or image.size != (details.get('width'), details.get('height')) or min(image.size) < 1 or max(image.size) > 2400:
            raise ValueError('Формат/размер изображения карты не соответствует источнику')
        image.verify()
    return raw


def render_page(sha, page):
    pdf_path(sha)
    target = image_path(sha, page).with_suffix('.json')
    if target.exists():
        if target.stat().st_size > 65536:
            raise ValueError('Метаданные изображения карты превышают лимит')
        details = json.loads(target.read_text(encoding='utf-8'))
        if details.get('source_sha256') != sha or details.get('page') != page or details.get('algorithm') != RENDER_ALGORITHM:
            raise ValueError('Изображение относится к другому источнику/странице')
        checked_image(sha, page, details)
        return dict(details, cached=True)
    details = worker('render', sha, page)
    checked_image(sha, page, details)
    target.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding='utf-8')
    return dict(details, cached=False)


def text_index(pages, geopdf_markers):
    drawings, references = [], []
    for number, text in pages:
        if MAP_HEADING.search(text):
            row = {'page': number, 'number_mentions': sorted({torgi.canonical(m.group()) for m in torgi.CAD.finditer(text)}),
                             'zone_hints': sorted(set(ZONE.findall(text))),
                             'old_edition_label': bool(re.search(r'старая\s+редакция', text, re.I)),
                             'new_edition_label': bool(re.search(r'новая\s+редакция', text, re.I)),
                             'coordinate_label_present': bool(re.search(r'систем\w*\s+координат|\bМСК[\s-]*\d|\bСК[\s-]*63', text, re.I)),
                             'geometry_confirmed': False}
            (drawings if row['old_edition_label'] and row['new_edition_label'] else references).append(row)
    return {'map_pages': drawings, 'map_reference_pages': references, 'standard_geopdf_markers': geopdf_markers,
            'map_geometry_confirmed': False, 'coordinate_georeferencing_confirmed': False}


def relate(result, survey):
    result = copy.deepcopy(result)
    boundary = box(*survey['bounds']) if survey else None
    parcels = (survey or {}).get('layers', {}).get('parcels', {})
    geometries = torgi.survey_geometries({'layers': {'parcels': parcels}})
    for doc in result['documents']:
        for page in doc.get('map_pages', []) + doc.get('map_reference_pages', []):
            matches = []
            for number in page['number_mentions']:
                observation = geometries.get(number)
                if not observation:
                    continue
                # Parcel geometry is a lookup aid, never an inferred PZZ boundary.
                intersect = bool(boundary is not None and any(shape(f['geometry']).intersection(boundary).area > 0 for f in observation['features']))
                matches.append({'cadastral_number': number, 'kind': 'number_mention_match', 'parcel_intersects_survey': bool(intersect),
                                'source': observation['source'], 'received_at': observation['received_at'], 'sha256': observation['sha256'],
                                'geometry_is_parcel_only': True, 'zone_geometry_confirmed': False})
            page['parcel_number_matches'] = matches
            page['unmatched_numbers'] = sorted(set(page['number_mentions']) - {m['cadastral_number'] for m in matches})
    return result


def run(project, params):
    catalog = store.get_setting('planning_watch_' + project)
    survey = store.get_setting('survey_' + project)
    if not catalog or params.get('planning_id') != catalog['id'] or params.get('survey_id') != (survey or {}).get('id'):
        raise ValueError('Документы или область изменились; обновите страницу')
    rows = selected(catalog)
    if not rows:
        raise ValueError('Нет прочитанных PDF из раздела ПЗЗ за 2026 год')
    result = {'planning_id': catalog['id'], 'survey_id': (survey or {}).get('id'), 'created_at': store.now(), 'documents': [],
              'algorithm': ALGORITHM, 'geometry_confirmed': False, 'legal_status_confirmed': False, 'network_requests': 0, 'warning': WARNING}
    result['previous_result_id'] = (store.get_setting('planning_maps_' + project) or {}).get('id')
    attempt = {'state': 'running', 'started_at': store.now(), 'requested': len(rows), 'processed': 0, 'network_requests': 0}
    store.set_setting('planning_maps_attempt_' + project, attempt)
    try:
        for row in rows:
            entry = {k: copy.deepcopy(row.get(k)) for k in ('id', 'url', 'title', 'sha256', 'received_at', 'act_identity', 'listing_references')}
            try:
                details = inspect_pdf(row['sha256'])
                if details['total_pages'] != row['total_pages']:
                    raise ValueError('Число страниц PDF изменилось')
                entry.update(details, state='indexed')
                if sum(len(x['map_pages']) for x in result['documents']) + len(entry['map_pages']) > MAX_MAP_PAGES:
                    raise ValueError('Общее число карт превышает предел 24 листа')
                for page in entry['map_pages']:
                    try:
                        page['image'] = render_page(row['sha256'], page['page'])
                    except Exception as exc:
                        page['image_error'] = str(exc)[:500]
            except Exception as exc:
                entry.update(state='error', error=str(exc)[:500], map_pages=[])
            result['documents'].append(entry)
            attempt['processed'] += 1
            store.set_setting('planning_maps_attempt_' + project, attempt)
        result = relate(result, survey)
        if not any(d['state'] == 'indexed' for d in result['documents']):
            raise ValueError('Ни один PDF не проверен; прежний результат карт сохранён')
        with store.LOCK:
            current = store.get_setting('planning_watch_' + project) or {}
            current_survey = store.get_setting('survey_' + project) or {}
            if current.get('id') != catalog['id'] or current_survey.get('id') != (survey or {}).get('id'):
                raise ValueError('Документы или область изменились во время анализа; прежний результат сохранён')
            result['id'] = planning_watch.digest(result)[:20]
            store.set_setting('planning_maps_' + project, result)
            folder = store.DATA / 'planning_maps'
            folder.mkdir(exist_ok=True)
            (folder / (result['id'] + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            with store.connect() as db:
                store.event(db, project, 'planning_maps', {'id': result['id'], 'documents': len(rows), 'network_requests': 0})
        attempt.update(state='done', finished_at=store.now())
        return {'documents': len(rows), 'map_pages': sum(len(x['map_pages']) for x in result['documents']),
                'errors': sum(x['state'] == 'error' for x in result['documents']),
                'image_errors': sum(bool(p.get('image_error')) for d in result['documents'] for p in d['map_pages']), 'network_requests': 0}
    except Exception as exc:
        attempt.update(state='error', error=str(exc)[:500], finished_at=store.now())
        raise
    finally:
        store.set_setting('planning_maps_attempt_' + project, attempt)


def report(project):
    result = store.get_setting('planning_maps_' + project)
    catalog = store.get_setting('planning_watch_' + project) or {}
    survey = store.get_setting('survey_' + project) or {}
    attempt = store.get_setting('planning_maps_attempt_' + project)
    stale = bool(result and (result['planning_id'] != catalog.get('id') or result['survey_id'] != survey.get('id')))
    return {'result': result, 'stale': stale, 'current_planning_id': catalog.get('id'), 'current_survey_id': survey.get('id'),
            'attempt': attempt, 'warning': WARNING}


def preview(project, document, page):
    data = report(project)
    if data['stale'] or not data['result']:
        raise ValueError('Карты относятся к прежним документам/области; повторите локальный анализ')
    doc = next((x for x in data['result']['documents'] if x['id'] == document), None)
    if not doc or not isinstance(page, int) or isinstance(page, bool):
        raise ValueError('Недопустимый документ/страница карты')
    sheet = next((x for x in doc['map_pages'] if x['page'] == page), None)
    if not sheet or not sheet.get('image'):
        raise ValueError('Страница не принята как обработанная карта ПЗЗ')
    pdf_path(doc['sha256'])
    return checked_image(doc['sha256'], page, sheet['image'])


def html_report(project):
    from html import escape
    data = report(project)
    esc = lambda value: escape('—' if value is None or value == '' else str(value), quote=True)
    content = []
    for doc in (data['result'] or {}).get('documents', []):
        act = doc.get('act_identity') or {}
        content.append('<h2>' + esc(f"{act.get('date', '')} № {act.get('number', '')}") + '</h2><p><a href="' + esc(doc['url']) + '">Официальный PDF</a></p>')
        if not doc['map_pages']:
            content.append('<p>Поддержанные картографические заголовки не найдены. Это не вывод об отсутствии ограничений.</p>')
        for page in doc['map_pages']:
            url = '/api/planning/maps/page?' + urlencode({'project': project, 'document': doc['id'], 'page': page['page']})
            content.append('<h3>PDF-страница ' + esc(page['page']) + '</h3><p>Коды зон в тексте: ' + esc(', '.join(page['zone_hints'])) + '</p><p>Кадастровые упоминания: '
                           + esc(', '.join(page['number_mentions'])) + '</p><p>Совпавших номеров сохранённых участков: ' + esc(len(page.get('parcel_number_matches', [])))
                           + '. Это не пространственная проверка зоны.</p>')
            for match in page.get('parcel_number_matches', []):
                content.append('<p>Совпавший номер: ' + esc(match['cadastral_number']) + '; кадастровая геометрия получена '
                               + esc(match['received_at']) + '. Площадь участка пересекает область: ' + ('да' if match['parcel_intersects_survey'] else 'нет')
                               + '. Эта геометрия относится к участку ЕГРН; граница зоны не установлена.</p>')
            if page.get('image') and not data['stale']:
                content.append('<a href="' + esc(url) + '"><img alt="Полный картографический лист, страница ' + esc(page['page']) + '" src="' + esc(url) + '"></a>')
            else:
                content.append('<p>' + esc(page.get('image_error') or 'Предыдущая версия карты скрыта; повторите локальный анализ.') + '</p>')
    return ('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Карты изменений ПЗЗ</title><style>body{font:16px/1.5 sans-serif;max-width:1100px;margin:30px auto;padding:0 20px;color:#253125}img{width:100%;height:auto}h2{margin-top:40px;border-top:1px solid #bbc6b7;padding-top:20px}a,p{overflow-wrap:anywhere}</style>'
            '<h1>Карты изменений ПЗЗ</h1><p>' + esc(WARNING) + '</p><p>' + ('Предыдущий результат: документы или область изменились.' if data['stale'] else 'Локальный анализ сохранённых PDF.')
            + '</p>' + ''.join(content) + '</html>').encode('utf-8')
