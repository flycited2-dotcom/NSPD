"""A source-bound historical boundary hypothesis, never a legal exclusion."""
import copy
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from pypdf import PdfReader
from pyproj import CRS
from pyproj.aoi import AreaOfInterest
from pyproj.transformer import TransformerGroup
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.ops import transform
from . import municipal, planning_watch, store
from .geometry import convert
from .network import fetch

VERSION = 1
URL = 'https://simfmo-rk.ru/wp-content/uploads/2024/11/396-ot-17.02.2021.pdf'
SHA = '0c8a85aa135c50f8f8f959296e3afc18cbb51dee699595b23f8261e3be18426e'
ARCHITECTURE = 'https://simfmo-rk.ru/arhitektura-i-gradostroitelstvo/'
ROOT_PATHS = {'gp': '/vnesenie-izmenenij-v-gp-reshenie-sessij/',
              'pzz': '/vnesenie-izmenenij-v-pzz-reshenie-sessij/'}
LIMITATION = ('Граница из решения № 396 от 17.02.2021. EPSG:7829 — гипотеза трактовки '
              '«Пулково 1963, зона 5»; паспорт системы координат не получен. '
              'Действующая граница и применимость редакции не подтверждены. '
              'Перечни ГП/ПЗЗ показывают опубликованные ссылки, а не полную историю '
              'или сводную действующую редакцию. Контур не используется для исключения земли.')
ROW = re.compile(r'^\s*(\d+)\s+(\d+,\d{2})\s+(\d+,\d{2})\s+К\s+5\s+-\s*$', re.M)


def parse_tables(pages):
    """Require the observed order, closures and two outer parts; never repair."""
    texts = dict(pages)
    heading = texts.get(60, '')
    if not all(s in heading for s in ('ГРАНИЦ С. ТРУДОВОЕ', 'Пулково 1963, зона 5',
                                       '3312172 кв.м. +/- 63420 кв.м.')):
        raise ValueError('Заголовок, площадь или система координат таблицы не совпадают')
    parts = {1: [], 2: []}
    part = None
    for page in range(60, 65):
        if page not in texts:
            raise ValueError('Не прочитаны все страницы таблицы границы')
        # PDFium places method/accuracy in separate lines; preserve tokens and
        # order, allowing only whitespace between the observed table columns.
        events = [(m.start(), 'part', m) for m in re.finditer(r'^\s*Часть N ([12])\s*$', texts[page], re.M)]
        events += [(m.start(), 'row', m) for m in ROW.finditer(texts[page])]
        for _, kind, match in sorted(events, key=lambda event: event[0]):
            if kind == 'part':
                part = int(match.group(1))
                continue
            if part is None:
                raise ValueError('Координата вне части таблицы')
            n, x, y = match.groups()
            parts[part].append({'number': int(n), 'x': float(x.replace(',', '.')),
                                'y': float(y.replace(',', '.')), 'page': page})
    polygons = []
    for part, first, last in ((1, 1, 148), (2, 149, 198)):
        rows = parts[part]
        if [r['number'] for r in rows] != list(range(first, last + 1)) + [first]:
            raise ValueError('Число или порядок точек границы не совпадают; восстановление запрещено')
        if (rows[0]['x'], rows[0]['y']) != (rows[-1]['x'], rows[-1]['y']):
            raise ValueError('Часть границы не замкнута исходной строкой')
        if any(not (4900000 < r['x'] < 5100000 and 5100000 < r['y'] < 5400000) for r in rows):
            raise ValueError('Координаты выходят за диапазон исходной таблицы')
        g = Polygon([(r['y'], r['x']) for r in rows])
        if not g.is_valid or g.is_empty or g.area <= 0:
            raise ValueError('Некорректная исходная геометрия границы')
        polygons.append(g)
    raw = MultiPolygon(polygons)
    if not raw.is_valid or polygons[0].intersects(polygons[1]):
        raise ValueError('Две внешние части границы пересекаются')
    if abs(raw.area - 3312172) > 63420 or abs(raw.length * 5 - 63420) > 1:
        raise ValueError('Контроль площади или периметра не согласуется с таблицей')
    return [parts[1], parts[2]], raw


def analyze_pdf(raw):
    if hashlib.sha256(raw).hexdigest() != SHA:
        raise ValueError('PDF изменился: требуется проверка нового документа и формата таблиц')
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted or len(reader.pages) != 66:
        raise ValueError('Структура исходного PDF не совпадает')
    first = [(i + 1, reader.pages[i].extract_text() or '') for i in range(59, 64)]
    metadata, second = municipal.pdfium_text(raw, 66)
    parts, raw_geometry = parse_tables(first)
    independent, other = parse_tables(second)
    if parts != independent or not raw_geometry.equals_exact(other, 0):
        raise ValueError('Независимые чтения координат расходятся')
    group = TransformerGroup(CRS.from_epsg(7829), CRS.from_epsg(4326), always_xy=True,
                             area_of_interest=AreaOfInterest(34.17, 44.98, 34.22, 45.02),
                             allow_ballpark=False)
    if not group.best_available or not 1 <= len(group.transformers) <= 8:
        raise ValueError('Подходящие операции преобразования координат недоступны')
    hypotheses = []
    for operation in group.transformers:
        world = transform(operation.transform, raw_geometry)
        w, s, e, n = world.bounds
        if not world.is_valid or not (34 < w < e < 35 and 44 < s < n < 46):
            raise ValueError('Преобразование координат не согласуется с районом источника')
        hypotheses.append({'geometry': mapping(world), 'operation': operation.description,
                           'pipeline': operation.definition, 'operation_accuracy_m':
                           operation.accuracy if operation.accuracy >= 0 else None})
    return {'parts': parts, 'raw_area_m2': raw_geometry.area,
            'declared_area_m2': 3312172, 'area_difference_m2': raw_geometry.area - 3312172,
            'declared_area_uncertainty_m2': 63420, 'raw_perimeter_m': raw_geometry.length,
            'point_accuracy_in_source_m': 5, 'point_method_in_source': 'К (картометрический)',
            'coordinate_order': 'X — север, Y — восток; преобразование получает (Y, X)',
            'source_crs_label': 'Пулково 1963, зона 5', 'hypothesis_epsg': 7829,
            'crs_confirmed': False, 'current_boundary_confirmed': False,
            'legal_status_confirmed': False, 'independent_text_readings_agree': True,
            'table_pages': list(range(60, 65)), 'map_page': 66,
            'text_read_pages': metadata['processed_pages'], 'hypotheses': hypotheses}


def collect_history(previous=None, progress=None):
    """Follow observed official links with fixed limits; retain dated failed pages."""
    previous = previous or {}
    old_pages = {p['url']: p for p in previous.get('pages', [])}
    pages, items = [], {}

    def receive(url, kind):
        if len(pages) >= 40:
            raise ValueError('Предел проверки: 40 страниц официальных перечней')
        record = {'url': url, 'kind': kind, 'checked_at': store.now()}
        if progress:
            progress('Проверка перечней ГП/ПЗЗ: страница ' + str(len(pages) + 1))
        try:
            raw, content_type, status = fetch(url, max_bytes=4 * 1024 * 1024)
            if 'text/html' not in content_type.lower():
                raise ValueError('Официальный перечень не вернул HTML')
            links = planning_watch.parse_links(raw, url)
            digest = hashlib.sha256(raw).hexdigest()
            folder = store.DATA / 'planning_boundary'; folder.mkdir(parents=True, exist_ok=True)
            (folder / (digest + '.html')).write_bytes(raw)
            record.update(state='received', received_at=store.now(), sha256=digest,
                          http_status=status, links=links)
        except Exception as exc:
            old = old_pages.get(url)
            record.update(state='error', error=str(exc)[:500], links=[])
            if old and old.get('sha256') and old.get('received_at'):
                record.update(state='retained', received_at=old['received_at'],
                              sha256=old['sha256'], links=copy.deepcopy(old.get('links', [])))
        pages.append(record)
        return record

    architecture = receive(ARCHITECTURE, 'architecture')
    for kind, path in ROOT_PATHS.items():
        root = next((x['url'] for x in architecture['links'] if urlparse(x['url']).path == path), None)
        if not root:
            pages.append({'url': 'https://simfmo-rk.ru' + path, 'kind': kind, 'state': 'error',
                          'checked_at': store.now(), 'error': 'Ссылка раздела не найдена на странице архитектуры', 'links': []})
            continue
        listing = receive(root, kind)
        years = [x for x in listing['links'] if re.fullmatch(r'20\d{2}', x['title']) and not x['url'].endswith('.pdf')]
        if len(years) > 5:
            raise ValueError('Раздел содержит более пяти годовых перечней; нужен новый профиль проверки')
        for year in years:
            year_page = receive(year['url'], kind + '_' + year['title'])
            publications = [year_page]
            if kind == 'pzz':
                sessions = [x for x in year_page['links'] if 'сессия' in x['title'].lower() and not x['url'].endswith('.pdf')]
                if len(sessions) > 16:
                    raise ValueError('Годовой перечень содержит более 16 сессий; проверка остановлена')
                publications += [receive(x['url'], kind + '_' + year['title']) for x in sessions]
            for page in publications:
                for link in page['links']:
                    if not urlparse(link['url']).path.lower().endswith('.pdf') or 'трудов' not in link['title'].lower():
                        continue
                    ref = {key: page.get(key) for key in ('url', 'state', 'received_at', 'sha256')}
                    ref.update(title=link['title'], publication_date=link['publication_date'])
                    item = items.setdefault(link['url'], {'url': link['url'], 'kind': kind,
                            'title': link['title'], 'references': [], 'document_read': False,
                            'spatial_applicability_confirmed': False, 'legal_status_confirmed': False})
                    if ref not in item['references']:
                        item['references'].append(ref)
                    if len(items) > 100:
                        raise ValueError('Перечень превышает 100 документов Трудовского поселения')
    return {'pages': pages, 'items': list(items.values()), 'complete': False,
            'all_observed_pages_received': all(p['state'] == 'received' for p in pages),
            'scope': 'Годовые перечни, прямо указанные в разделах ГП/ПЗЗ сайта района; прежние годы, ФГИС ТП и полнота истории не подтверждены'}


def run(project, params):
    if project != 'trudovoe':
        raise ValueError('Профиль границы и перечней настроен только для Трудового')
    old = store.get_setting('planning_boundary_' + project)
    if params.get('expected_id') != (old or {}).get('id'):
        raise ValueError('Наблюдение границы изменилось; обновите страницу')
    attempt = {'state': 'running', 'started_at': store.now(), 'step': 'Получение исходного PDF границы'}
    def progress(step):
        attempt['step'] = step
        store.set_setting('planning_boundary_attempt_' + project, attempt)
    try:
        progress(attempt['step'])
        raw, content_type, code = fetch(URL, max_bytes=4 * 1024 * 1024)
        received_at = store.now()
        analysis = analyze_pdf(raw)
        folder = store.DATA / 'planning_boundary'; folder.mkdir(parents=True, exist_ok=True)
        (folder / (SHA + '.pdf')).write_bytes(raw)
        result = {'version': VERSION, 'created_at': store.now(), 'limitation': LIMITATION,
                  'source': {'url': URL, 'sha256': SHA, 'received_at': received_at, 'http_status': code,
                             'bytes': len(raw), 'decision_number': '396', 'decision_date': '2021-02-17',
                             'base_decision_number': '1082', 'base_decision_date': '2018-12-06'},
                  'analysis': analysis, 'history': collect_history((old or {}).get('history'), progress)}
        result['id'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:20]
        with store.LOCK:
            if (store.get_setting('planning_boundary_' + project) or {}).get('id') != (old or {}).get('id'):
                raise ValueError('Наблюдение изменилось во время проверки; результат не записан')
            (folder / (result['id'] + '.json')).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            store.set_setting('planning_boundary_' + project, result)
            with store.connect() as db:
                store.event(db, project, 'planning_boundary', {'id': result['id'], 'document_links': len(result['history']['items']), 'legal_status_confirmed': False})
        attempt.update(state='done', finished_at=store.now(), step='Историческая граница и перечни сохранены')
        return {'id': result['id'], 'points': 198, 'parts': 2, 'document_links': len(result['history']['items']),
                'all_observed_pages_received': result['history']['all_observed_pages_received'], 'legal_status_confirmed': False}
    except Exception as exc:
        attempt.update(state='error', error=str(exc)[:500], finished_at=store.now())
        raise
    finally:
        store.set_setting('planning_boundary_attempt_' + project, attempt)


def applicable(result):
    if not result or result.get('version') != VERSION or result.get('source', {}).get('sha256') != SHA:
        return False
    try:
        received = datetime.fromisoformat(result['source']['received_at'])
        return (received.tzinfo is not None and received <= datetime.now(timezone.utc)
                and result['analysis'].get('crs_confirmed') is False
                and result['analysis'].get('current_boundary_confirmed') is False
                and bool(result['analysis'].get('hypotheses')))
    except (KeyError, TypeError, ValueError):
        return False


def projected(result, metric):
    if not applicable(result):
        return []
    return [convert(shape(h['geometry']), 4326, metric) for h in result['analysis']['hypotheses']]


def relate(g, boundaries, result):
    if not boundaries:
        return None
    observations = []
    for boundary in boundaries:
        inside = g.intersection(boundary).area
        relation = 'inside' if boundary.covers(g) else 'outside' if inside <= .01 else 'crosses'
        observations.append({'relation': relation, 'inside_area_m2': round(inside, 2),
                             'outside_area_m2': round(max(0, g.area - inside), 2),
                             'distance_to_boundary_m': round(g.distance(boundary.boundary), 2)})
    relations = {x['relation'] for x in observations}
    return {'relation': next(iter(relations)) if len(relations) == 1 else 'varies',
            'operations_agree': len(relations) == 1, 'observations': observations,
            'source': copy.deepcopy(result['source']), 'hypothesis_epsg': 7829,
            'decision_date': '2021-02-17', 'current_boundary_confirmed': False,
            'crs_confirmed': False, 'legal_status_confirmed': False, 'used_for_exclusion': False,
            'limitation': LIMITATION}


def report(project):
    return {'result': store.get_setting('planning_boundary_' + project),
            'attempt': store.get_setting('planning_boundary_attempt_' + project)}
