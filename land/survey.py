"""Preliminary gaps in observed geometry, separate from verified GIS candidates."""
import hashlib
import json
import math
from shapely.geometry import box, shape, mapping
from shapely.ops import unary_union
from . import store, nspd
from .geometry import convert, polygons

WARNING = 'Предварительный контур, не подтверждённый свободный участок. Полнота источников, права, дороги и допустимость использования не подтверждены. Полученные здания исключены; пересечения с зонами требуют проверки документов.'
MODES = ('parcels', 'free', 'auction', 'buildings', 'pzz', 'restrictions')


def parameters(params):
    values = {k: float(params.get(k, default)) for k, default in [('min_area', 400), ('max_area', 2500), ('min_width', 12)]}
    if not all(math.isfinite(v) for v in values.values()) or not (0 < values['min_area'] <= values['max_area'] <= 1e8 and 0 <= values['min_width'] <= 1000):
        raise ValueError('Некорректные фильтры площади или ширины')
    return values


def gaps(bounds, layers, params):
    params = parameters(params)
    nspd.spatial_body(bounds, 36368)
    if not layers['parcels']['geojson']['features']:
        raise ValueError('Кадастровый ответ пуст. Расчёт промежутков остановлен: нельзя объявлять всю область свободной.')
    boundary = box(*bounds)
    c = boundary.centroid
    metric = f'+proj=laea +lat_0={c.y} +lon_0={c.x} +datum=WGS84 +units=m +no_defs'
    project = lambda g: convert(g, 4326, metric)
    b = project(boundary)
    occupied_world = unary_union([shape(f['geometry']) for f in layers['parcels']['geojson']['features']])
    buildings_world = unary_union([shape(f['geometry']) for f in layers.get('buildings', {}).get('geojson', {}).get('features', [])])
    before_buildings = boundary.difference(occupied_world)
    # Preserve the input topology at the boundary; project only for metric filters.
    remainder_world = before_buildings.difference(buildings_world)
    remainder = project(remainder_world)
    features, filtered = [], {'small': 0, 'narrow': 0}
    overlays = {mode: [(project(shape(f['geometry'])), f['id']) for f in layers.get(mode, {}).get('geojson', {}).get('features', [])] for mode in ('free', 'auction', 'pzz', 'restrictions')}
    for g in sorted(polygons(remainder), key=lambda x: (-x.area, x.centroid.x, x.centroid.y)):
        if g.area < params['min_area']:
            filtered['small'] += 1
            continue
        if params['min_width'] and g.buffer(-params['min_width']/2).is_empty:
            filtered['narrow'] += 1
            continue
        if len(features) >= 500:
            raise ValueError('Слишком много промежутков. Уменьшите область или увеличьте минимальную площадь.')
        world = convert(g, metric).intersection(remainder_world)
        key = hashlib.sha256(world.normalize().wkb).hexdigest()[:16]
        features.append({'type': 'Feature', 'id': 'gap-' + key, 'geometry': mapping(world), 'properties': {
            'label': 'Промежуток ' + str(len(features)+1), 'area_m2': round(g.area, 2),
            'large_window': g.area > params['max_area'], 'touches_boundary': g.distance(b.boundary) < .1,
            'matches': {mode: [fid for other, fid in rows if g.intersection(other).area > .01] for mode, rows in overlays.items()},
            'zone_intersections': {mode: [{'id': fid, 'area_m2': round(g.intersection(other).area, 2)} for other, fid in overlays[mode] if g.intersection(other).area > .01] for mode in ('pzz', 'restrictions')},
            'status': 'unverified', 'warning': WARNING}})
    return {'type': 'FeatureCollection', 'features': features}, {
        'area_m2': round(b.area, 2), 'observed_parcels_m2': round(b.area - project(before_buildings).area, 2),
        'buildings_excluded_m2': round(project(before_buildings).area - remainder.area, 2),
        'layer_counts': {mode: len(layers.get(mode, {}).get('geojson', {}).get('features', [])) for mode in MODES},
        'pzz_coverage_confirmed': False, 'restrictions_coverage_confirmed': False,
        'remainder_m2': round(remainder.area, 2), 'filtered': filtered, 'parameters': params,
        'complete': False, 'warning': WARNING}


def run(project, params):
    bounds = list(map(float, params.get('bounds', [])))
    filters = parameters(params)
    categories = nspd.catalog()
    nspd.spatial_body(bounds, categories['parcels']['categoryId'])
    attempt = {'started_at': store.now(), 'state': 'running', 'bounds': bounds, 'completed_layers': []}
    store.set_setting('survey_attempt_' + project, attempt)
    layers = {}
    try:
        for mode in MODES:
            body = nspd.spatial_body(bounds, categories[mode]['categoryId'])
            data, digest = nspd.request_json(nspd.INTERSECTS, body)
            fc = nspd.normalize(data)
            # Reject wrong-category responses instead of subtracting unrelated objects.
            if any(f['properties'].get('category') != categories[mode]['categoryId'] for f in fc['features']):
                raise ValueError('Категория объектов не соответствует запрошенному слою')
            layers[mode] = {'geojson': fc, 'sha256': digest, 'received_at': store.now(), 'request': body, 'source': nspd.INTERSECTS}
            attempt['completed_layers'].append(mode)
            store.set_setting('survey_attempt_' + project, attempt)
        fc, summary = gaps(bounds, layers, filters)
        result = {'id': hashlib.sha256(json.dumps(layers, sort_keys=True).encode()).hexdigest()[:20],
                  'created_at': store.now(), 'bounds': bounds, 'layers': layers, 'gaps': fc, 'summary': summary}
        folder = store.DATA / 'surveys'
        folder.mkdir(exist_ok=True)
        (folder / (result['id'] + '.json')).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        store.set_setting('survey_' + project, result)
        attempt.update(state='done', finished_at=store.now(), count=len(fc['features']))
        with store.connect() as db:
            store.event(db, project, 'survey', {'id': result['id'], 'bounds': bounds, 'summary': summary, 'counts': {k: len(v['geojson']['features']) for k,v in layers.items()}})
    except Exception as exc:
        attempt.update(state='error', finished_at=store.now(), error=str(exc))
        raise
    finally:
        store.set_setting('survey_attempt_' + project, attempt)
    return {'count': len(fc['features']), 'id': result['id']}
