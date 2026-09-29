"""Metric, vector-only reconnaissance. No legal availability is inferred."""
import hashlib
import json
import math
from datetime import date

from pyproj import CRS, Transformer
from shapely import normalize, to_wkb, force_2d
from shapely.geometry import shape, mapping, GeometryCollection
from shapely.ops import transform, unary_union

ROLES = {
    'boundary': 'Граница исследования', 'parcels': 'Учтённые участки',
    'roads': 'Дороги и территории общего пользования',
    'exclusions': 'Подтверждённые исключения', 'restrictions': 'ЗОУИТ и ограничения',
    'buildings': 'Застройка', 'pzz': 'Территориальные зоны ПЗЗ',
    'quarters': 'Кадастровые кварталы',
}


def convert(g, source, target='EPSG:4326'):
    return transform(Transformer.from_crs(source, target, always_xy=True).transform, g)


def polygons(g):
    if g.geom_type == 'Polygon':
        yield g
    elif hasattr(g, 'geoms'):
        for part in g.geoms:
            yield from polygons(part)


def validate_layer(payload):
    role = payload.get('role')
    if role not in ROLES:
        raise ValueError('Неизвестная роль слоя')
    meta = payload.get('metadata', {})
    for field in ['title', 'source', 'checked_at', 'crs']:
        if not str(meta.get(field, '')).strip():
            raise ValueError('Для слоя обязательно поле: ' + field)
    checked = date.fromisoformat(meta['checked_at'])
    if checked > date.today():
        raise ValueError('Дата источника не может быть в будущем')
    crs = CRS.from_user_input(meta['crs'])
    raw = payload.get('geojson', {})
    if raw.get('type') != 'FeatureCollection':
        raise ValueError('Нужен GeoJSON FeatureCollection с явно заданным CRS')
    declared = raw.get('crs', {}).get('properties', {}).get('name')
    if declared and CRS.from_user_input(declared) != crs:
        raise ValueError('CRS в файле не совпадает с выбранным CRS')
    features = raw.get('features')
    if not isinstance(features, list) or len(features) > 100000:
        raise ValueError('Допустимо не более 100 000 объектов в слое')
    if role == 'boundary' and not features:
        raise ValueError('Пустая граница исследования недопустима')
    clean = []
    for i, f in enumerate(features):
        try:
            g = shape(f['geometry'])
        except Exception as exc:
            raise ValueError(f'Объект {i + 1}: отсутствует корректная геометрия') from exc
        if g.is_empty or not g.is_valid or g.geom_type not in ('Polygon', 'MultiPolygon'):
            raise ValueError(f'Объект {i + 1}: нужен валидный Polygon/MultiPolygon. Линии дорог предварительно буферизуйте в метрах в GIS.')
        g = force_2d(convert(g, crs))
        if not all(math.isfinite(x) for x in g.bounds):
            raise ValueError('Ошибка преобразования координат')
        x1, y1, x2, y2 = g.bounds
        if not (-180 <= x1 <= x2 <= 180 and -85 <= y1 <= y2 <= 85):
            raise ValueError('Координаты не соответствуют заявленному CRS')
        clean.append({'type': 'Feature', 'geometry': mapping(g), 'properties': f.get('properties') or {}})
    coverage = meta.get('coverage')
    if coverage:
        c = shape(coverage)
        if not c.is_valid or c.geom_type not in ('Polygon', 'MultiPolygon'):
            raise ValueError('Покрытие должно быть валидным полигоном в CRS слоя')
        meta = dict(meta, coverage=mapping(convert(c, crs)))
    return {'role': role, 'metadata': dict(meta, original_crs=str(crs), crs='EPSG:4326'),
            'geojson': {'type': 'FeatureCollection', 'features': clean}}


def analyse(layers, params):
    by_role = {x['role']: x for x in layers}
    if not all(r in by_role for r in ['boundary', 'parcels']):
        raise ValueError('Для поиска нужны граница исследования и слой учтённых участков')
    boundary = unary_union([shape(f['geometry']) for f in by_role['boundary']['geojson']['features']])
    if boundary.bounds[2] - boundary.bounds[0] > 5 or boundary.bounds[3] - boundary.bounds[1] > 5:
        raise ValueError('Выберите локальную область исследования, не более 5° по каждой оси')
    center = boundary.centroid
    metric = CRS.from_proj4(f'+proj=laea +lat_0={center.y} +lon_0={center.x} +datum=WGS84 +units=m +no_defs')
    project = lambda g: convert(g, 'EPSG:4326', metric)
    b = project(boundary)
    pm = by_role['parcels']['metadata']
    if pm.get('complete') is not True or not pm.get('coverage'):
        raise ValueError('Для кадастрового слоя нужны подтверждение полноты выгрузки и полигон покрытия. Неполные данные создают ложные окна.')
    coverage = project(shape(pm['coverage']))
    if b.difference(coverage).area > 0.1:
        raise ValueError('Кадастровые данные не покрывают всю границу исследования')
    min_area = float(params.get('min_area', 400))
    max_area = float(params.get('max_area', 2500))
    min_width = float(params.get('min_width', 12))
    clearance = float(params.get('clearance', 0))
    if not all(math.isfinite(x) for x in [min_area, max_area, min_width, clearance]) or not (0 < min_area <= max_area <= 1e9 and 0 <= min_width <= 1000 and 0 <= clearance <= 100):
        raise ValueError('Некорректные параметры площади, ширины или отступа')
    if b.area > 500_000_000:
        raise ValueError('За один запуск допускается не более 500 км²')
    geoms = {r: [(project(shape(f['geometry'])), f['properties']) for f in layer['geojson']['features']] for r, layer in by_role.items()}
    occupied = [g.buffer(clearance) if clearance else g for r in ['parcels', 'roads', 'exclusions', 'buildings'] for g, _ in geoms.get(r, [])]
    difference = b.difference(unary_union(occupied))
    warnings = []
    for r in ['boundary', 'parcels', 'roads', 'exclusions', 'buildings', 'restrictions', 'pzz', 'quarters']:
        if r not in by_role:
            warnings.append(f'{ROLES[r]}: слой не загружен')
        else:
            m = by_role[r]['metadata']
            if not m.get('official'):
                warnings.append(f'{ROLES[r]}: официальный источник не подтверждён')
            if (date.today() - date.fromisoformat(m['checked_at'])).days > 30:
                warnings.append(f'{ROLES[r]}: данные старше 30 дней, требуется актуализация')
    fingerprint = hashlib.sha256(json.dumps(layers, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    results, filtered = [], {'small': 0, 'narrow': 0}
    roads = unary_union([g for g, _ in geoms.get('roads', [])])
    for g in sorted(polygons(difference), key=lambda x: (-x.area, x.centroid.x, x.centroid.y)):
        if g.area < min_area:
            filtered['small'] += 1
            continue
        if min_width and g.buffer(-min_width / 2).is_empty:
            filtered['narrow'] += 1
            continue
        world = convert(g, metric)
        point = world.representative_point()
        matches = {}
        for role in ['restrictions', 'pzz', 'quarters']:
            matches[role] = [p for other, p in geoms.get(role, []) if g.intersection(other).area > .01]
        neighbours = [p for other, p in geoms['parcels'] if g.distance(other) <= max(clearance + 1, 5)]
        digest = hashlib.sha256(to_wkb(normalize(world))).hexdigest()
        results.append({'geometry_hash': digest, 'geometry': mapping(world), 'area_m2': round(g.area, 2),
                        'point': [round(point.x, 7), round(point.y, 7)], 'fingerprint': fingerprint,
                        'metric_crs': metric.to_wkt(), 'coordinates_metric': mapping(g),
                        'large_window': g.area > max_area, 'road_distance_m': None if roads.is_empty else round(g.distance(roads), 1),
                        'overlays': matches, 'neighbours': neighbours, 'warnings': warnings,
                        'sources': [{'role': r, **l['metadata']} for r, l in by_role.items()]})
        if len(results) > 5000:
            raise ValueError('Более 5000 кандидатов. Уменьшите область или измените фильтры')
    return results, {'area_m2': round(b.area, 2), 'remaining_m2': round(difference.area, 2), 'filtered': filtered,
                     'warnings': warnings, 'parameters': params, 'fingerprint': fingerprint,
                     'note': 'Найденные полигоны — пространственные кандидаты. Права и возможность предоставления не установлены.'}
