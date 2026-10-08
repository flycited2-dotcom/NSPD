"""Bounded observations of the official regional PZZ layer, without legal clearance."""
import copy
import hashlib
import json
import math
from datetime import datetime
from urllib.parse import urlencode

from shapely.geometry import box, mapping, shape

from . import network, nspd, store
from .geometry import convert

VERSION = 1
WFS = 'https://rgis.rk.gov.ru/geoserver/wfs'
MAP = 'https://rgis.rk.gov.ru/map/Map.aspx#/maps/34f88ad7-6208-4f45-b29a-daabbb7119fe'
TITLE = 'Территориальные зоны ПЗЗ РГИС Крыма'
TYPE_NAME = 'rgis:territorialzone_polygon'
FEATURE_PREFIX = 'territorialzone_polygon.'
FIELDS = ('symbol', 'territorialzonename', 'territory', 'status', 'dataobjectkey')
MAX_FEATURES = 1000
MAX_COORDINATES = 200000
WARNING = (
    'Подключён опубликованный слой «Территориальные зоны ПЗЗ» общей карты РГИС Крыма. '
    'Сервис проверяется только в выбранной области; полная карта ПЗЗ Крыма не получена. '
    'Пустой ответ не подтверждает отсутствие территориальных зон. '
    'Полнота покрытия, действующая редакция ПЗЗ, утверждающие акты и точность исходной '
    'системы координат не установлены. Геометрическое пересечение не подтверждает '
    'допустимость ИЖС или другого использования, права, свободность земли и законный подъезд. '
    'Этот слой не исключает землю из подбора.'
)
CATALOG = {
    'checked_at': '2026-10-08T18:11:33+00:00',
    'map_url': MAP,
    'map_name': 'Общая карта',
    'map_id': '34f88ad7-6208-4f45-b29a-daabbb7119fe',
    'layer_id': '8d18d817-1aa5-4d55-8afc-d7a93a19a4c7',
    'layer_name': 'Территориальные зоны ПЗЗ',
    'type_name': TYPE_NAME,
    'metadata_sha256': 'bcd3a4a480b4180bbdafd3ab9df7c224911e02cd054f4f7b007fea233f8c380c',
    'capabilities_sha256': 'e91ec0d01809888e1c333592340d4d1f9462a9871d1a6313587164090622396f',
    'schema_sha256': '06592f1cdb090699f3c242d0a2ae308c67c8e21bcc42a369d81fda587bacc511',
    'default_srs_observed': 'urn:x-ogc:def:crs:EPSG:19635',
    'requested_srs': 'EPSG:3857',
    'advertised_bounds': [32.43530849137439, 44.36111440295335, 36.66438048832009, 46.245001334503414],
    'advertised_bounds_meaning': 'Declared layer extent; not confirmed coverage or obtained zone geometries',
    'provenance': 'Official OData MapLayer Settings, WFS GetCapabilities and DescribeFeatureType; not inferred layer identifiers',
    'global_observation': {
        'checked_at': '2026-10-08T18:09:58+00:00',
        'request_kind': 'WFS GetFeature resultType=hits without a spatial filter',
        'count': 0,
        'sha256': 'b201189d13233ac40488913d40d8b006fb7d452f15a03e86855eecfc5f1d203b',
        'coverage_confirmed': False,
        'currentness_confirmed': False,
        'meaning': 'Observed zero objects in this service; full or current PZZ coverage is not established',
    },
}


def parameters(bounds):
    nspd.spatial_body(bounds, 36368)  # Shared bounded WGS84 validation; no NSPD request.
    projected_bounds = convert(box(*bounds), 4326, 3857).bounds
    return {
        'service': 'WFS', 'version': '1.1.0', 'request': 'GetFeature',
        'typeName': TYPE_NAME, 'srsName': 'EPSG:3857',
        'bbox': ','.join(map(str, projected_bounds)) + ',EPSG:3857',
        'outputFormat': 'application/json', 'maxFeatures': MAX_FEATURES,
    }


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('Не подтверждено число объектов слоя ПЗЗ РГИС')
    return value


def normalize(data, bounds):
    if not isinstance(data, dict) or data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list):
        raise ValueError('Слой ПЗЗ РГИС не вернул GeoJSON FeatureCollection')
    features = data['features']
    count = len(features)
    if count >= MAX_FEATURES:
        raise ValueError('Достигнут предел ответа ПЗЗ РГИС; уменьшите область')
    if any(_count(data.get(key)) != count for key in ('totalFeatures', 'numberMatched', 'numberReturned')):
        raise ValueError('Неполный или изменившийся ответ слоя ПЗЗ РГИС')
    if data.get('links') or data.get('next') or data.get('@odata.nextLink'):
        raise ValueError('Ответ слоя ПЗЗ РГИС содержит продолжение; уменьшите область')
    crs = data.get('crs')
    if crs is not None and crs != {'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:EPSG::3857'}}:
        raise ValueError('Система координат ответа ПЗЗ РГИС отличается от EPSG:3857')
    if count and crs is None:
        raise ValueError('Для геометрии ПЗЗ РГИС не указана система координат')
    query = convert(box(*bounds), 4326, 3857)
    seen, result, budget = set(), [], 0
    for feature in features:
        if (not isinstance(feature, dict) or feature.get('type') != 'Feature'
                or not isinstance(feature.get('id'), str) or not feature['id']
                or len(feature['id']) > 200 or feature['id'] in seen):
            raise ValueError('Некорректный или повторный ID объекта ПЗЗ РГИС')
        if not feature['id'].startswith(FEATURE_PREFIX) or len(feature['id']) == len(FEATURE_PREFIX):
            raise ValueError('Ответ ПЗЗ РГИС относится к другому слою')
        seen.add(feature['id'])
        geometry = feature.get('geometry')
        if not isinstance(geometry, dict) or geometry.get('type') not in ('Polygon', 'MultiPolygon'):
            raise ValueError('Ожидалась полигональная геометрия ПЗЗ РГИС')
        stack = [geometry.get('coordinates')]
        while stack:
            item = stack.pop()
            budget += 1
            if budget > MAX_COORDINATES:
                raise ValueError('Превышен предел координат ПЗЗ РГИС')
            if not isinstance(item, (list, tuple)) or not item:
                raise ValueError('Некорректные координаты ПЗЗ РГИС')
            if isinstance(item[0], (list, tuple)):
                stack.extend(item)
                continue
            if (len(item) != 2 or any(isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or abs(value) > 20040000 for value in item)):
                raise ValueError('Некорректные координаты EPSG:3857 ПЗЗ РГИС')
        polygon = shape(geometry)
        if polygon.is_empty or not polygon.is_valid or not polygon.intersects(query):
            raise ValueError('Невалидная или не относящаяся к области геометрия ПЗЗ РГИС')
        properties = feature.get('properties')
        if not isinstance(properties, dict):
            raise ValueError('Некорректные свойства ПЗЗ РГИС')
        fields = {key: copy.deepcopy(properties[key]) for key in FIELDS if key in properties}
        if any(value is not None and (not isinstance(value, str) or len(value) > 2000) for value in fields.values()):
            raise ValueError('Некорректное значение свойства ПЗЗ РГИС')
        world = convert(polygon, 3857, 4326)
        if not world.is_valid:
            raise ValueError('Не удалось преобразовать геометрию ПЗЗ РГИС')
        result.append({'type': 'Feature', 'id': feature['id'], 'geometry': mapping(world), 'properties': fields})
    return {'type': 'FeatureCollection', 'features': result}


def collect(bounds):
    params = parameters(bounds)
    url = WFS + '?' + urlencode(params)
    raw, content_type, status = network.fetch(url, max_bytes=5 * 1024 * 1024)
    if status != 200:
        raise ValueError('Слой ПЗЗ РГИС не вернул полный HTTP 200 ответ')
    digest = hashlib.sha256(raw).hexdigest()
    folder = store.DATA / 'pzz' / 'raw'
    folder.mkdir(parents=True, exist_ok=True)
    store.atomic_write(folder / (digest + '.json'), raw)
    received_at = store.now()
    data = json.loads(raw)
    geojson = normalize(data, bounds)
    server_time = data.get('timeStamp')
    clock_warning = False
    try:
        clock_warning = datetime.fromisoformat(server_time.replace('Z', '+00:00')) > datetime.fromisoformat(received_at)
    except (ValueError, TypeError, AttributeError):
        clock_warning = bool(server_time)
    layer = {
        'title': TITLE, 'source': WFS, 'request_url': url, 'request': params,
        'sha256': digest, 'received_at': received_at, 'http_status': status,
        'content_type': content_type, 'count': len(geojson['features']), 'geojson': geojson,
        'response_counts': {key: data[key] for key in ('totalFeatures', 'numberMatched', 'numberReturned')},
        'server_timestamp': server_time, 'server_clock_warning': clock_warning,
        'response_count_consistent': True, 'source_crs_confirmed': data.get('crs') is not None,
        'requested_crs': 'EPSG:3857', 'normalized_crs': 'EPSG:4326',
        'coverage_confirmed': False, 'currentness_confirmed': False,
        'territorial_zone_confirmed': False, 'used_for_exclusion': False,
    }
    result = {
        'version': VERSION, 'created_at': store.now(), 'bounds': list(bounds),
        'catalog': copy.deepcopy(CATALOG), 'layer': layer, 'warning': WARNING,
        'coverage_confirmed': False, 'currentness_confirmed': False,
        'territorial_zone_confirmed': False,
    }
    result['id'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:20]
    return result


def run(project, params):
    bounds = list(params.get('bounds', []))
    parameters(bounds)
    old = store.get_setting('pzz_context_' + project)
    attempt = {'state': 'running', 'started_at': store.now(), 'bounds': bounds}
    store.set_setting('pzz_attempt_' + project, attempt)
    try:
        result = collect(bounds)
        with store.LOCK:
            if store.get_setting('pzz_context_' + project) != old:
                raise ValueError('Контекст ПЗЗ РГИС изменился во время обновления')
            folder = store.DATA / 'pzz'
            folder.mkdir(parents=True, exist_ok=True)
            store.atomic_write(folder / (result['id'] + '.json'), json.dumps(result, ensure_ascii=False).encode('utf-8'))
            # Context and its event are one transaction; a failed event cannot replace old evidence.
            with store.connect() as db:
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                           ('pzz_context_' + project, json.dumps(result, ensure_ascii=False)))
                store.event(db, project, 'pzz_context',
                            {'id': result['id'], 'bounds': bounds, 'count': result['layer']['count']})
        attempt.update(state='done', finished_at=store.now(), result_id=result['id'])
        return {'id': result['id'], 'count': result['layer']['count']}
    except Exception as exc:
        attempt.update(state='error', finished_at=store.now(), error=str(exc)[:500])
        raise
    finally:
        store.set_setting('pzz_attempt_' + project, attempt)


def applicable(context, bounds):
    return bool(context and context.get('version') == VERSION and context.get('bounds') == bounds)


def projected(context, bounds, metric):
    return [(convert(shape(feature['geometry']), 4326, metric), feature)
            for feature in context['layer']['geojson']['features']] if applicable(context, bounds) else []


def relate(geometry, entries, context):
    result = []
    for other, feature in entries:
        area = geometry.intersection(other).area
        if area <= .01:
            continue
        layer = context['layer']
        result.append({
            'id': feature['id'], 'fields': copy.deepcopy(feature['properties']), 'area_m2': round(area, 2),
            'spatially_covers': other.covers(geometry), 'source': WFS, 'map_url': MAP,
            'received_at': layer['received_at'], 'sha256': layer['sha256'],
            'currentness_confirmed': False, 'territorial_zone_confirmed': False,
            'coverage_confirmed': False, 'used_for_exclusion': False,
        })
    return result


def sources(context, bounds):
    return {
        'id': (context or {}).get('id'), 'applied': applicable(context, bounds),
        'bounds': copy.deepcopy((context or {}).get('bounds')), 'warning': WARNING,
        'catalog': copy.deepcopy((context or {}).get('catalog', CATALOG)),
        'layer': {key: copy.deepcopy(value) for key, value in (context or {}).get('layer', {}).items() if key != 'geojson'},
        'coverage_confirmed': False, 'currentness_confirmed': False, 'territorial_zone_confirmed': False,
    }
