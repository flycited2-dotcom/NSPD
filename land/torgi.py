"""Read-only lot search observed in the official public registry on 2026-09-30."""
import copy
import hashlib
import json
import re
import time
from datetime import datetime
from urllib.parse import urlencode
from shapely.geometry import box, shape
from . import store, nspd
from .network import fetch
from .geometry import convert

SEARCH = 'https://torgi.gov.ru/new/api/public/lotcards/search'
PAGE_SIZE, MAX_PAGES, MAX_GEOMETRIES = 10, 20, 20
CAD = re.compile(r'(?<![\d:])\d{1,2}:\d{1,2}:\d{1,10}:\d{1,10}(?![\d:])')
WARNING = 'Поиск по тексту, региону Крым и земельной категории; не полный реестр земли выбранной области. История процедуры не подтверждает сегодняшнюю доступность. Заявления, не опубликованные в источнике, не видны. Геометрия берётся отдельно из НСПД и не заменяет схему лота.'


def canonical(value):
    value = str(value or '').strip()
    return ':'.join(str(int(x)) for x in value.split(':')) if CAD.fullmatch(value) else None


def text(value, limit=10000):
    return str(value or '')[:limit]


def normalize(row):
    if not isinstance(row, dict) or not re.fullmatch(r'\d{10,30}_\d{1,6}', str(row.get('id', ''))):
        raise ValueError('Неожиданный идентификатор лота')
    if str(row.get('subjectRFCode')) != '91':
        raise ValueError('Ответ содержит другой регион; результат не принят')
    numbers = set()
    areas = []
    for c in row.get('characteristics') or []:
        if c.get('code') == 'CadastralNumber':
            numbers.update(canonical(m.group()) for m in CAD.finditer(text(c.get('characteristicValue'))))
        if c.get('code') == 'SquareZU':
            areas.append({'value': c.get('characteristicValue'), 'unit': (c.get('unit') or {}).get('symbol')})
    form = row.get('biddForm') or {}
    return {'id': row['id'], 'notice_number': text(row.get('noticeNumber'), 100), 'lot_number': row.get('lotNumber'),
            'url': 'https://torgi.gov.ru/new/public/lots/lot/' + row['id'] + '/(lotInfo:info)',
            'title': text(row.get('lotName')), 'description': text(row.get('lotDescription')),
            'status': text(row.get('lotStatus'), 100), 'procedure': {'code': text(form.get('code'), 100), 'name': text(form.get('name'), 500)},
            'type': {'code': text((row.get('biddType') or {}).get('code'), 100), 'name': text((row.get('biddType') or {}).get('name'), 500)},
            'category': {'code': text((row.get('category') or {}).get('code'), 100), 'name': text((row.get('category') or {}).get('name'), 500)},
            'transaction': text(row.get('typeTransaction'), 100), 'price_min': row.get('priceMinExact') if row.get('priceMinExact') is not None else row.get('priceMin'),
            'currency': row.get('currencyCode'), 'deadline': row.get('biddEndTime'),
            'published_at': row.get('noticeFirstVersionPublicationDate'), 'timezone': row.get('timeZoneName'),
            'stopped': row.get('isStopped'), 'annulled': row.get('isAnnulled'),
            'cadastral_numbers': sorted(numbers), 'areas': areas, 'geometry_confirmed': False}


def deadline_state(lot, checked_at):
    if not lot.get('deadline'):
        return 'unknown'
    try:
        end = datetime.fromisoformat(lot['deadline'].replace('Z', '+00:00'))
        checked = datetime.fromisoformat(checked_at.replace('Z', '+00:00'))
        if end.tzinfo is None or checked.tzinfo is None:
            return 'unknown'
        return 'expired' if end <= checked else 'future'
    except (ValueError, TypeError):
        return 'unknown'


def survey_geometries(result):
    geometries = {}
    for mode in ('parcels', 'free', 'auction'):
        layer = (result or {}).get('layers', {}).get(mode, {})
        for f in layer.get('geojson', {}).get('features', []):
            p = f.get('properties') or {}
            number = canonical((p.get('options') or {}).get('cad_num') or p.get('label'))
            if number:
                geometries.setdefault(number, {'features': [], 'source': layer.get('source'), 'received_at': layer.get('received_at'), 'sha256': layer.get('sha256')})['features'].append(f)
    return geometries


def source_id(result):
    return (result or {}).get('search_source_id') or (result or {}).get('id')


def combined_geometries(previous,survey,lots):
    numbers={n for lot in lots for n in lot['cadastral_numbers']}
    observations={n:copy.deepcopy(o) for n,o in (previous or {}).get('geometries',{}).items() if n in numbers and o.get('lookup')}
    # The current surveyed geometry takes precedence over a dated number lookup.
    observations.update(survey_geometries(survey))
    return observations


def rematch(project,params):
    previous=store.get_setting('torgi_'+project)
    survey=store.get_setting('survey_'+project)
    if not previous or params.get('id')!=previous['id']:
        raise ValueError('Поиск изменился; обновите страницу')
    if not survey or params.get('survey_id')!=survey['id']:
        raise ValueError('Обследование изменилось; обновите страницу')
    attempt={'state':'running','started_at':store.now(),'network_requests':0}
    store.set_setting('torgi_rematch_attempt_'+project,attempt)
    try:
        result=copy.deepcopy(previous)
        observations=combined_geometries(previous,survey,result['lots'])
        numbers={n for lot in result['lots'] for n in lot['cadastral_numbers']}
        missing=sorted(n for n in numbers if not observations.get(n,{}).get('features'))
        result.update(parent_id=previous['id'],search_source_id=source_id(previous),rematched_at=store.now(),
                      survey_id=survey['id'],survey_bounds=survey['bounds'],geometries=observations,
                      lots=relate(result['lots'],observations,survey),rematch_network_requests=0,rematch_unlocated_numbers=missing)
        with store.LOCK:
            if (store.get_setting('torgi_'+project) or {}).get('id')!=previous['id'] or (store.get_setting('survey_'+project) or {}).get('id')!=survey['id']:
                raise ValueError('Поиск или обследование изменились во время сопоставления; результат не записан')
            persist(project,result)
            with store.connect() as db:store.event(db,project,'torgi_rematch',{'id':result['id'],'survey_id':survey['id'],'network_requests':0})
        attempt.update(state='done',finished_at=store.now())
        return {'id':result['id'],'count':sum(lot['in_survey'] for lot in result['lots']),
                'unlocated_numbers':len(missing),'lots_without_number':sum(not lot['cadastral_numbers'] for lot in result['lots']),'network_requests':0}
    except Exception as exc:
        attempt.update(state='error',error=str(exc)[:500],finished_at=store.now());raise
    finally:store.set_setting('torgi_rematch_attempt_'+project,attempt)


def relate(lots, geometries, survey):
    boundary = box(*survey['bounds']) if survey else None
    metric = f'+proj=laea +lat_0={boundary.centroid.y} +lon_0={boundary.centroid.x} +datum=WGS84 +units=m +no_defs' if boundary else None
    for lot in lots:
        lot['spatial_matches'] = []
        lot['geometry_lookups'] = []
        lot['geometry_confirmed'] = False
        for number in lot['cadastral_numbers']:
            observation = geometries.get(number)
            if not observation:
                continue
            lot['geometry_lookups'].append({'cadastral_number': number, **{k:v for k,v in observation.items() if k != 'features'}})
            for f in observation.get('features', []):
                if boundary is None:
                    continue
                g = shape(f['geometry'])
                intersection = g.intersection(boundary)
                if intersection.is_empty or intersection.area <= 0:
                    continue
                overlaps = []
                for gap in survey['gaps']['features']:
                    area = convert(g.intersection(shape(gap['geometry'])), 4326, metric).area
                    if area > .01:
                        overlaps.append({'gap_id': gap['id'], 'area_m2': round(area, 2)})
                lot['spatial_matches'].append({'cadastral_number': number, 'feature': f, 'gap_intersections': overlaps,
                                             'survey_intersection_m2': round(convert(intersection, 4326, metric).area, 2)})
        lot['in_survey'] = bool(lot['spatial_matches'])
    return lots


def persist(project, result):
    result['id'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:20]
    folder = store.DATA / 'torgi'
    folder.mkdir(exist_ok=True)
    (folder / (result['id'] + '.json')).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    store.set_setting('torgi_' + project, result)


def run(project, params):
    previous=store.get_setting('torgi_'+project)
    query = str(params.get('query', 'Трудовое')).strip()
    if not 2 <= len(query) <= 120 or any(ord(c) < 32 for c in query):
        raise ValueError('Поисковый текст должен содержать от 2 до 120 символов')
    history = params.get('history', True)
    if not isinstance(history, bool):
        raise ValueError('Некорректный фильтр истории')
    attempt = {'state': 'running', 'started_at': store.now(), 'query': query, 'pages_received': 0}
    store.set_setting('torgi_attempt_' + project, attempt)
    rows, pages, total, seen = [], [], None, set()
    try:
        for page in range(MAX_PAGES):
            if page:
                time.sleep(1)
            args = {'dynSubjRF': '12', 'catCode': '2', 'text': query, 'matchPhrase': 'false', 'byFirstVersion': 'true',
                    'withFacets': 'true' if page == 0 else 'false', 'size': PAGE_SIZE, 'sort': 'firstVersionPublicationDate,desc'}
            if page:
                args['page'] = page
            if not history:
                args['lotStatus'] = 'PUBLISHED,APPLICATIONS_SUBMISSION'
            url = SEARCH + '?' + urlencode(args)
            raw, ct, code = fetch(url)
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise ValueError('ГИС Торги вернула не JSON') from exc
            content = data.get('content') if isinstance(data, dict) else None
            if not isinstance(content, list) or data.get('number') != page or data.get('size') != PAGE_SIZE or not isinstance(data.get('last'), bool):
                raise ValueError('Структура страницы ГИС Торги изменилась')
            count = data.get('totalElements')
            if isinstance(count, bool) or not isinstance(count, int) or count < 0 or len(content) > PAGE_SIZE:
                raise ValueError('Некорректное количество лотов')
            if total is not None and count != total:
                raise ValueError('Количество результатов изменилось между страницами; предыдущий результат сохранён')
            total = count
            for row in content:
                item = normalize(row)
                if item['id'] in seen:
                    raise ValueError('Повтор лота между страницами; полнота не подтверждена')
                seen.add(item['id'])
                item['deadline_state'] = deadline_state(item, store.now())
                rows.append(item)
            pages.append({'url': url, 'received_at': store.now(), 'sha256': hashlib.sha256(raw).hexdigest(), 'count': len(content)})
            folder = store.DATA / 'torgi'
            folder.mkdir(exist_ok=True)
            (folder / (pages[-1]['sha256'] + '.source.json')).write_bytes(raw)
            attempt.update(pages_received=len(pages), total=total)
            store.set_setting('torgi_attempt_' + project, attempt)
            if data['last']:
                if len(rows) != total:
                    raise ValueError('Число полученных лотов не совпадает с итогом поиска')
                break
            if not content:
                raise ValueError('Пустая промежуточная страница; результат не принят')
        else:
            raise ValueError('Поиск превышает 200 лотов; уточните текст. Предыдущий результат сохранён.')
        survey = store.get_setting('survey_' + project, None)
        observations=combined_geometries(previous,survey,rows)
        source_digest=hashlib.sha256(json.dumps({'query':query,'history':history,'pages':pages,'previous_search_source_id':source_id(previous)},sort_keys=True).encode()).hexdigest()[:20]
        result = {'created_at': store.now(), 'query': query, 'history': history, 'region': 'Крым', 'category': 'Земельные участки',
                  'pages': pages, 'reported_total': total, 'query_pages_received': True, 'complete': False, 'warning': WARNING,
                  'survey_id': survey['id'] if survey else None, 'survey_bounds': survey['bounds'] if survey else None,
                  'lots': relate(rows, observations, survey), 'geometries': observations,
                  'search_source_id':source_digest,'parent_id':(previous or {}).get('id')}
        with store.LOCK:
            if (store.get_setting('torgi_'+project) or {}).get('id')!=(previous or {}).get('id') or (store.get_setting('survey_'+project) or {}).get('id')!=(survey or {}).get('id'):
                raise ValueError('Поиск или обследование изменились во время загрузки; результат не записан')
            persist(project, result)
        store.set_setting('torgi_geometry_attempt_' + project, None)
        attempt.update(state='done', finished_at=store.now())
        with store.connect() as db:
            store.event(db, project, 'torgi_search', {'id': result['id'], 'query': query, 'count': len(rows), 'pages': len(pages)})
        return {'id': result['id'], 'count': len(rows)}
    except Exception as exc:
        attempt.update(state='error', error=str(exc), finished_at=store.now())
        raise
    finally:
        store.set_setting('torgi_attempt_' + project, attempt)


def locate(project, params):
    previous = store.get_setting('torgi_' + project, None)
    survey = store.get_setting('survey_' + project, None)
    if not previous or params.get('id') != previous['id']:
        raise ValueError('Поиск изменился; обновите страницу')
    if not survey:
        raise ValueError('Сначала выполните обследование области')
    retry = params.get('retry_missing', False)
    if not isinstance(retry, bool):
        raise ValueError('Параметр повтора должен быть логическим')
    result = copy.deepcopy(previous)
    observations = combined_geometries(previous,survey,result['lots'])
    retry_states = {'not_returned', 'not_found', 'rejected'}
    numbers = sorted({n for lot in result['lots'] for n in lot['cadastral_numbers']
                      if n not in observations or observations[n].get('state') == 'error'
                      or (retry and observations[n].get('state') in retry_states)})
    checked = set()
    attempt = {'state': 'running', 'started_at': store.now(), 'processed': 0,
               'requested': min(len(numbers), MAX_GEOMETRIES), 'retry_missing': retry}
    store.set_setting('torgi_geometry_attempt_' + project, attempt)
    try:
        for number in numbers[:MAX_GEOMETRIES]:
            url = nspd.BASE + '/api/geoportal/v2/search/geoportal?' + urlencode({'thematicSearchId': 1, 'query': number})
            checked.add(number)
            try:
                data, digest = nspd.request_json(url)
                try:
                    fc = nspd.normalize(data)
                except (ValueError, TypeError, KeyError) as exc:
                    observations[number] = {'features': [], 'source': url, 'sha256': digest, 'received_at': store.now(),
                                            'lookup': True, 'state': 'rejected', 'error': str(exc)}
                    attempt['processed'] += 1
                    store.set_setting('torgi_geometry_attempt_' + project, attempt)
                    continue  # bad geometry is local to this number, never repaired or accepted
                # Neighbouring numbers mentioned in the text are never accepted as lot geometry.
                fs = [f for f in fc['features'] if f['properties'].get('category') == nspd.catalog()['parcels']['categoryId'] and canonical(f['properties'].get('options', {}).get('cad_num') or f['properties'].get('label')) == number]
                observations[number] = {'features': fs, 'source': url, 'sha256': digest, 'received_at': store.now(),
                                        'lookup': True, 'state': 'received' if fs else 'not_found'}
            except Exception as exc:
                if isinstance(exc, nspd.StatusError) and exc.status_code == 404:
                    observations[number] = {'features': [], 'source': url, 'received_at': store.now(), 'lookup': True,
                                            'state': 'not_returned', 'http_status': 404, 'error': 'HTTP 404: геометрия не получена; существование и права не установлены'}
                    attempt['processed'] += 1
                    store.set_setting('torgi_geometry_attempt_' + project, attempt)
                    continue
                observations[number] = {'features': [], 'source': url, 'received_at': store.now(), 'lookup': True, 'state': 'error', 'error': str(exc)}
                attempt.update(error=str(exc), state='partial')
                break  # no further requests after an access or transport error
            attempt['processed'] += 1
            store.set_setting('torgi_geometry_attempt_' + project, attempt)
        result.update(parent_id=previous['id'], search_source_id=source_id(previous), geometry_checked_at=store.now(), survey_id=survey['id'], survey_bounds=survey['bounds'],
                      geometries=observations, lots=relate(result['lots'], observations, survey), geometry_limit=MAX_GEOMETRIES,
                      geometry_unchecked_numbers=[n for n in numbers if n not in checked])
        persist(project, result)
        if attempt['state'] == 'running':
            attempt['state'] = 'done'
        attempt['finished_at'] = store.now()
        with store.connect() as db:
            store.event(db, project, 'torgi_geometry', {'id': result['id'], 'survey_id': survey['id'], 'state': attempt['state']})
        return {'count': sum(bool(lot['spatial_matches']) for lot in result['lots']), 'id': result['id']}
    except Exception as exc:
        attempt.update(state='error', error=str(exc), finished_at=store.now())
        raise
    finally:
        store.set_setting('torgi_geometry_attempt_' + project, attempt)
