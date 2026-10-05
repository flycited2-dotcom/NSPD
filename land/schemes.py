"""Strict extraction of observed XY tables; no guessed CRS or legal clearance."""
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from shapely.geometry import Polygon
from shapely.validation import explain_validity
from . import municipal, store, scheme_review, scheme_components

ALGORITHM = 'designation-catalogues-v4'
BATCH = 5
ROOT = Path(__file__).resolve().parent.parent
DESIGNATION = re.compile(r'Обозначение земельного\s+участка\s+(:ЗУ\d+|\d{1,2}:\d{1,2}:\d{1,10}:\d{1,10}(?:\(\d+\))?)', re.I)
HEADER = re.compile(r'Координаты,\s*м\s+X\s+Y\s+1\s+2\s+3', re.I)
POINT = re.compile(r'^(н?\d+)\s+(-?\d{1,8}(?:[,.]\d{1,3})?)\s+(-?\d{1,8}(?:[,.]\d{1,3})?)$', re.I)
PAIR = re.compile(r'\d{5,8}[,.]\d+\s+\d{5,8}[,.]\d+')
FOOTER = re.compile(r'^(?:\d{4}-ПМТ|Лист № докум\. Подп\. Дата Изм|Лист)$', re.I)
SECTION = re.compile(r'^\s*\d{1,2}(?:\.\d{1,2})?\.?\s+[А-ЯЁ]{4}', re.M)
WARNING = 'Контуры извлечены в X/Y таблицы, не в WGS84. Система координат, назначение и полнота таблиц требуют сверки. На географическую карту и в расчёт свободной земли они не добавлены.'


def extract(pages):
    chunks, offsets, cursor = [], [], 0
    for n, text in pages:
        if len(text) > 500000:
            raise ValueError('Текст страницы превышает лимит')
        chunk = text + '\n'
        offsets.append((cursor, cursor + len(chunk), n))
        chunks.append(chunk)
        cursor += len(chunk)
    text = ''.join(chunks)
    def page_at(pos):
        return next(n for a, b, n in offsets if a <= pos < b)
    headings = list(DESIGNATION.finditer(text))
    tables, covered, issues = [], set(), []
    for index, heading in enumerate(headings):
        start, end = heading.end(), headings[index+1].start() if index+1 < len(headings) else len(text)
        header = HEADER.search(text, start, min(end, start + 450))
        if not header:
            continue
        points, errors = [], []
        pos = header.end()
        for line in text[pos:end].splitlines(keepends=True):
            line_start = pos
            pos += len(line)
            value = line.strip()
            if not value or FOOTER.fullmatch(value):
                continue
            match = POINT.fullmatch(value)
            if not match:
                if re.match(r'^(?:н\S*\s+\S+|\d+\s+(?:[-\d]|\S+\s+[-\d]))', value, re.I):
                    errors.append({'page': page_at(line_start), 'reason': 'Строка точки не соответствует трём столбцам X/Y', 'row': value[:200]})
                break
            x, y = [float(v.replace(',', '.')) for v in match.groups()[1:]]
            points.append({'label': match[1], 'x': x, 'y': y, 'page': page_at(line_start), 'source_row': value})
            covered.add(line_start)
            if len(points) > 5000:
                raise ValueError('Таблица превышает лимит 5000 точек')
            if len(points) > 1 and points[-1]['label'] == points[0]['label']:
                if (x, y) != (points[0]['x'], points[0]['y']):
                    errors.append({'page': page_at(line_start), 'reason': 'Замыкающая точка изменила координаты'})
                break
        if not points:
            if errors:
                issues.append({'label':heading[1], 'heading_page':page_at(heading.start()), 'issues':errors})
            continue
        coords = [(p['x'], p['y']) for p in points]
        closed = len(coords) > 1 and coords[-1] == coords[0] and points[-1]['label'] == points[0]['label']
        vertices = points[:-1] if closed else points
        if len(vertices) < 3:
            errors.append({'reason': 'Меньше трёх вершин'})
        if len({p['label'] for p in vertices}) != len(vertices):
            errors.append({'reason': 'Повтор обозначения точки внутри кольца'})
        if len(set(coords[:-1] if closed else coords)) != len(vertices):
            errors.append({'reason': 'Повтор координат внутри кольца'})
        polygon = Polygon(coords) if len(vertices) >= 3 else None
        if polygon is not None and (not polygon.is_valid or polygon.area <= 0):
            errors.append({'reason': explain_validity(polygon) if not polygon.is_valid else 'Нулевая площадь'})
        sections = list(SECTION.finditer(text, 0, heading.start()))
        context_start = sections[-1].start() if sections else max(0, heading.start()-900)
        before = text[context_start:heading.start()]
        role = 'public_use_context' if re.search(r'ОБЩЕГО\s+ПОЛЬЗОВАНИЯ', before, re.I) else 'formed_label' if heading[1].upper().startswith(':ЗУ') else 'unknown'
        tables.append({'label': heading[1], 'heading_page': page_at(heading.start()), 'pages': sorted({p['page'] for p in points}),
                       'purpose_hint': role, 'context_page': page_at(context_start), 'points': points, 'closure': 'explicit' if closed else 'implicit_for_preview',
                       'state': 'rejected' if errors else 'review_required', 'issues': errors,
                       'local_area_m2': round(polygon.area, 2) if polygon is not None and not errors else None,
                       'outline_xy': list(map(list, polygon.exterior.coords)) if polygon is not None and not errors else None,
                       'georeferenced': False, 'geometry_confirmed': False})
        if len(tables) > 200 or sum(len(t['points']) for t in tables) > 5000:
            raise ValueError('Количество таблиц/точек превышает лимит')
    components,boundaries,component_covered=scheme_components.extract(text,page_at)
    tables.extend(components); covered.update(component_covered)
    if len(tables)>200 or sum(len(t['points']) for t in tables)>5000:
        raise ValueError('Количество таблиц/точек превышает лимит')
    labels = {}
    for table in tables:
        labels.setdefault(table['label'], []).append(table)
    for group in labels.values():
        if len(group) > 1:
            for table in group:
                table['issues'].append({'reason': 'Повтор обозначения участка; таблицы не объединены'})
                table.update(state='rejected', outline_xy=None, local_area_m2=None)
    # Every unmatched numeric pair is a visible limitation, including unnamed boundary rings.
    unparsed = set()
    cursor = 0
    for line in text.splitlines(keepends=True):
        if PAIR.search(line) and cursor not in covered:
            unparsed.add(page_at(cursor))
        cursor += len(line)
    crs = municipal.evidence(pages)['crs_mentions']
    crs += [{'page':t['heading_page'],'label':t['crs_label']} for t in components if t.get('crs_label')]
    crs=list({(r['page'],r['label']):r for r in crs}.values())
    names = {re.sub(r'[\s-]+', '', x['label']).upper() for x in crs}
    return {'algorithm': ALGORITHM, 'tables': tables, 'component_groups':scheme_components.groups(tables),
            'project_boundaries':boundaries,'unparsed_coordinate_pages': sorted(unparsed), 'unreadable_tables':issues,
            'crs_mentions': crs, 'crs_status': 'ambiguous_labels' if len(names)>1 else 'parameters_missing' if names else 'label_missing',
            'georeferenced': False, 'geometry_confirmed': False, 'warning': WARNING,
            'review': scheme_review.build(tables, scheme_review.context(pages))}


def apply_page_limit(result, processed_pages, unread_pages):
    if unread_pages:
        for table in result['tables']:
            if table['closure'] != 'explicit' and table['pages'][-1] == processed_pages:
                table['issues'].append({'reason':'Таблица достигает лимита страниц; продолжение не прочитано'})
                table.update(state='rejected', outline_xy=None, local_area_m2=None)
    result['review'] = scheme_review.build(result['tables'], {
        key:result['review'][key] for key in ('origin_statements','division_statements','quarter_statements')})
    result['component_groups']=scheme_components.groups(result['tables'])
    return result


def read_document(digest, extended=False, large=False):
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Некорректный SHA-256')
    source = store.DATA / 'municipal' / (digest + '.pdf')
    if not source.is_file() or source.stat().st_size > (32 if large else 16 if extended else 8)*1024*1024 or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise ValueError('Сохранённый PDF отсутствует, слишком велик или изменился')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    try:
        output = subprocess.run([sys.executable, '-m', 'land.scheme_worker', str(source.resolve()), digest,*(['large'] if large else ['extended'] if extended else [])],
                                cwd=ROOT, capture_output=True, encoding='utf-8', timeout=60, **options)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Извлечение таблиц превысило время; процесс остановлен') from exc
    if len(output.stdout) > 4*1024*1024:
        raise ValueError('Результат превышает лимит')
    result = json.loads(output.stdout)
    if output.returncode or result.get('error'):
        raise ValueError(result.get('error') or 'Ошибка извлечения таблиц')
    if result.get('source_sha256') != digest or result.get('algorithm') != ALGORITHM:
        raise ValueError('Таблицы относятся к другому файлу или алгоритму')
    return result


def pending(catalog, previous, retry=False):
    prior = {r['document_id']: r for r in previous.get('documents', [])}
    eligible = sorted((r for r in catalog['items'] if r['state']=='read' and r['format']=='pdf'),
                      key=lambda r: (r['kind'] != 'planning', not bool(r.get('coordinate_label_pages'))))
    return [r for r in eligible if r['id'] not in prior or prior[r['id']].get('source_sha256') != r['sha256']
            or prior[r['id']].get('algorithm') != ALGORITHM or (retry and (prior[r['id']]['state']=='error' or prior[r['id']].get('coordinate_catalog_error')))
            or (municipal.pdf_scope(r)[1]>40 and prior[r['id']].get('catalog_processed_pages')!=r['processed_pages'])]


def persist(project, result):
    result['updated_at'] = store.now()
    result['id'] = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
    folder = store.DATA / 'schemes'
    folder.mkdir(exist_ok=True)
    (folder / (result['id'] + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    store.set_setting('schemes_' + project, result)


def run(project, params):
    catalog = store.get_setting('municipal_' + project)
    if not catalog or params.get('catalog_id') != catalog['id']:
        raise ValueError('Каталог изменился; обновите страницу')
    retry = params.get('retry_errors', False)
    if not isinstance(retry, bool):
        raise ValueError('Параметр повтора должен быть логическим')
    result = copy.deepcopy(store.get_setting('schemes_' + project, {}) or {})
    known = {r['id']:r for r in catalog['items']}
    result['documents'] = [r for r in result.get('documents',[]) if r['document_id'] in known and r['source_sha256']==known[r['document_id']].get('sha256')]
    result.update(catalog_id=catalog['id'], warning=WARNING, georeferenced=False, geometry_confirmed=False)
    selected = pending(catalog, result, retry)[:BATCH]
    attempt = {'state':'running','started_at':store.now(),'requested':len(selected),'processed':0,'network_requests':0}
    store.set_setting('schemes_attempt_' + project, attempt)
    try:
        for row in selected:
            attempt['document_id'] = row['id']
            store.set_setting('schemes_attempt_' + project, attempt)
            document = {'document_id':row['id'],'source_sha256':row['sha256'],'source_received_at':row['received_at'],
                        'title':row['title'],'url':row['url'],'algorithm':ALGORITHM,'processed_at':store.now(),
                        'content_conflicts':row.get('content_conflicts',[]), 'listing_conflict':row.get('listing_conflict',False)}
            if municipal.pdf_scope(row)[1]>40:document['catalog_processed_pages']=row['processed_pages']
            try:
                details=(read_document(row['sha256'],large=True) if municipal.pdf_scope(row)[0]==32
                         else read_document(row['sha256'],extended=True) if 'catalog_processed_pages' in document
                         else read_document(row['sha256']))
                document.update(details, state='extracted')
            except Exception as exc:
                document.update(state='error', error=str(exc)[:500], tables=[])
            result['documents'] = [r for r in result['documents'] if r['document_id'] != row['id']] + [document]
            attempt['processed'] += 1
            persist(project, result)
            store.set_setting('schemes_attempt_' + project, attempt)
        persist(project, result)
        attempt.update(state='done', finished_at=store.now(), remaining=len(pending(catalog, result)))
        with store.connect() as db:
            store.event(db, project, 'scheme_tables', {'id':result['id'],'documents':len(selected),'network_requests':0})
        return {'processed':len(selected),'remaining':attempt['remaining']}
    except Exception as exc:
        attempt.update(state='error',finished_at=store.now(),error=str(exc)[:500])
        raise
    finally:
        store.set_setting('schemes_attempt_' + project, attempt)
