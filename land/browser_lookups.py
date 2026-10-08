"""Import dated NSPD number-search responses without browser credentials or requests."""
import copy
import hashlib
import json
import math
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs, urlsplit

from . import nspd, store, torgi

MAX_RESPONSES = 5
MAX_BODY_BYTES = 1024 * 1024
WARNING = ('Импортированный ответ поиска НСПД из браузера. Происхождение и дата указаны '
           'в импортируемом файле; импорт не удостоверяет их подлинность. Кадастровая '
           'геометрия не заменяет схему лота и не подтверждает права или свободность земли.')


def validate_geometry(feature, fc):
    geometry = feature.get('geometry')
    if not isinstance(geometry, dict) or geometry.get('type') not in ('Polygon', 'MultiPolygon'):
        raise ValueError('Нужна полигональная геометрия участка')
    declared = []
    for value in (geometry, fc):
        if 'crs' not in value:
            continue
        crs = value['crs']
        name = crs.get('properties', {}).get('name') if isinstance(crs, dict) and isinstance(crs.get('properties'), dict) else None
        if name not in ('EPSG:3857', 'EPSG:4326'):
            raise ValueError('Система координат ответа не подтверждена')
        declared.append(name)
    if not declared or len(set(declared)) != 1:
        raise ValueError('Системы координат объекта и коллекции расходятся или не указаны')
    coordinates = geometry.get('coordinates')
    if not isinstance(coordinates, list) or not coordinates:
        raise ValueError('Координаты участка не получены')
    polygons = [coordinates] if geometry['type'] == 'Polygon' else coordinates
    count = 0
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise ValueError('Некорректные кольца участка')
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
                raise ValueError('Кольцо участка должно быть явно замкнуто')
            for point in ring:
                count += 1
                if (count > 20000 or not isinstance(point, list) or len(point) != 2
                        or any(type(n) not in (int, float) or not math.isfinite(n) for n in point)):
                    raise ValueError('Нужны конечные двумерные координаты в пределах лимита')


def observation(entry, allowed_numbers):
    if not isinstance(entry, dict):
        raise ValueError('Некорректная запись ответа НСПД')
    url = entry.get('url')
    if not isinstance(url, str) or len(url) > 300:
        raise ValueError('Нужен URL конкретного поиска номера НСПД')
    parts = urlsplit(url)
    query = parse_qs(parts.query, strict_parsing=True, keep_blank_values=True)
    if (parts.scheme != 'https' or parts.netloc != 'nspd.gov.ru' or parts.fragment
            or parts.path != '/api/geoportal/v2/search/geoportal'
            or set(query) != {'thematicSearchId', 'query'} or query['thematicSearchId'] != ['1']
            or len(query['query']) != 1):
        raise ValueError('Допускается только опубликованный поиск номера НСПД')
    number = torgi.canonical(query['query'][0])
    if not number or number not in allowed_numbers:
        raise ValueError('Номер ответа не относится к сохранённым лотам')
    if type(entry.get('status')) is not int or entry['status'] != 200:
        raise ValueError('Импортируется только успешный HTTP 200 ответ')
    received_at = entry.get('received_at')
    if not isinstance(received_at, str) or len(received_at) > 40:
        raise ValueError('Нужна дата получения ответа с часовым поясом')
    received = datetime.fromisoformat(received_at.replace('Z', '+00:00'))
    if (received.tzinfo is None or received < datetime(2000, 1, 1, tzinfo=timezone.utc)
            or received > datetime.now(timezone.utc) + timedelta(minutes=5)):
        raise ValueError('Дата получения ответа не подтверждена или находится в будущем')
    body = entry.get('body')
    if not isinstance(body, str):
        raise ValueError('Нужен исходный JSON-текст ответа')
    raw = body.encode('utf-8')
    if not raw or len(raw) > MAX_BODY_BYTES:
        raise ValueError('Ответ поиска номера должен быть не больше 1 МБ')
    digest = hashlib.sha256(raw).hexdigest()
    if entry.get('sha256') != digest:
        raise ValueError('SHA-256 не совпадает с исходным ответом')
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('data'), dict):
        raise ValueError('Неожиданный формат ответа поиска НСПД')
    if set(data) != {'data', 'meta'}:
        raise ValueError('Нужен только ответ поиска, без заголовков и сведений сессии')
    pending = [data]
    forbidden = {'authorization', 'cookie', 'cookies', 'headers', 'requestheaders', 'responseheaders',
                 'access_token', 'refresh_token', 'accesstoken', 'refreshtoken', 'password', 'token', 'set-cookie'}
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if any(key.lower() in forbidden for key in value):
                raise ValueError('Заголовки и сведения сессии не импортируются')
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    fc = data['data']
    for container in (data, fc):
        links = container.get('links', [])
        if not isinstance(links, list) or any(not isinstance(link, dict) or link.get('rel') == 'next' for link in links):
            raise ValueError('Продолжение ответа не допускается')
    for key in ('totalFeatures', 'numberMatched', 'numberReturned'):
        if key in fc and (type(fc[key]) is not int or fc[key] != 1):
            raise ValueError('Счётчики ответа не согласованы')
    features = fc.get('features')
    category = nspd.catalog()['parcels']['categoryId']
    # A bounded number lookup must contain exactly one parcel and an explicit total.
    if (fc.get('type') != 'FeatureCollection' or not isinstance(features, list) or len(features) != 1
            or data.get('meta') != [{'totalCount': 1, 'categoryId': category}]
            or type(data['meta'][0]['totalCount']) is not int
            or type(data['meta'][0]['categoryId']) is not int):
        raise ValueError('Нужен полный ответ с одним земельным участком и явным счётчиком')
    feature = features[0]
    if not isinstance(feature, dict) or feature.get('type') != 'Feature':
        raise ValueError('Некорректный объект поиска НСПД')
    properties = feature.get('properties')
    if not isinstance(properties, dict) or type(properties.get('category')) is not int or properties['category'] != category:
        raise ValueError('Ответ должен содержать земельный участок ЕГРН')
    options = properties.get('options')
    if not isinstance(options, dict) or torgi.canonical(options.get('cad_num')) != number:
        raise ValueError('Структурированный номер объекта не совпадает с запросом')
    for key in ('label', 'externalKey', 'descr'):
        if key in properties and torgi.canonical(properties[key]) != number:
            raise ValueError('Номера в полях объекта расходятся')
    validate_geometry(feature, fc)
    normalized = nspd.normalize(data)
    if len(normalized['features']) != 1:
        raise ValueError('Геометрия объекта не получена')
    imported = {'features': normalized['features'], 'source': url, 'sha256': digest,
                'received_at': received_at, 'lookup': True, 'state': 'received',
                'transport': 'browser_response_import', 'imported_at': store.now(),
                'provenance_verified': False, 'network_requests': 0, 'warning': WARNING}
    return number, imported, raw


def run(project, params, active=True):
    previous = store.get_setting(torgi.setting_key(project, active))
    survey = store.get_setting('survey_' + project)
    if not previous or params.get('id') != previous['id']:
        raise ValueError('Поиск изменился; обновите страницу')
    if not survey or params.get('survey_id') != survey['id']:
        raise ValueError('Обследование изменилось; обновите страницу')
    bundle = params.get('bundle')
    if not isinstance(bundle, dict) or type(bundle.get('version')) is not int or bundle['version'] != 1:
        raise ValueError('Неизвестная версия файла браузерных ответов')
    entries = bundle.get('observations')
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_RESPONSES:
        raise ValueError('Одна порция содержит от одного до пяти ответов поиска')
    allowed = {n for lot in previous['lots'] for n in lot['cadastral_numbers']}
    accepted = [observation(entry, allowed) for entry in entries]
    if len({number for number, _, _ in accepted}) != len(accepted):
        raise ValueError('Номер повторяется в импортируемой порции')
    result = copy.deepcopy(previous)
    observations = torgi.combined_geometries(previous, survey, result['lots'])
    for number, imported, _ in accepted:
        old = observations.get(number)
        if old and old.get('features'):
            # Do not replace surveyed or previously received geometry through an import.
            raise ValueError('Геометрия этого номера уже получена; импорт не заменяет её')
        observations[number] = imported
    imported_numbers = [number for number, _, _ in accepted]
    result.update(parent_id=previous['id'], search_source_id=torgi.source_id(previous),
                  browser_imported_at=store.now(), browser_imported_numbers=imported_numbers,
                  survey_id=survey['id'], survey_bounds=survey['bounds'],
                  survey_signature=torgi.survey_signature(survey), geometries=observations,
                  lots=torgi.relate(result['lots'], observations, survey))
    for key in ('geometry_unchecked_numbers', 'geometry_deferred_numbers'):
        if key in result:
            result[key] = [n for n in result[key] if n not in imported_numbers]
    with store.LOCK:
        if (store.get_setting(torgi.setting_key(project, active)) or {}).get('id') != previous['id'] or (store.get_setting('survey_' + project) or {}).get('id') != survey['id']:
            raise ValueError('Поиск или обследование изменились; импорт не записан')
        folder = store.DATA / 'nspd_browser'
        folder.mkdir(exist_ok=True)
        for _, imported, raw in accepted:
            store.atomic_write(folder / (imported['sha256'] + '.source.json'), raw)
            store.atomic_write(folder / (imported['sha256'] + '.observation.json'),
                               json.dumps({k: v for k, v in imported.items() if k != 'features'}, ensure_ascii=False).encode('utf-8'))
        torgi.persist(project, result, active)
        with store.connect() as db:
            store.event(db, project, 'torgi_browser_geometry',
                        {'id': result['id'], 'numbers': imported_numbers, 'network_requests': 0})
    return {'id': result['id'], 'imported': len(accepted), 'network_requests': 0}
