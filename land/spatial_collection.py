"""Compare a parent spatial answer with four children; never certify legal coverage."""
import hashlib
import json
from shapely.geometry import box, shape
from . import nspd, store

WARNING = ('Сверка ответов по четырём частям проверяет только наблюдаемые объекты. '
           'Она не устанавливает полноту ЕГРН, положение объектов без границ или права.')


def collect(bounds, category):
    parent_request = nspd.spatial_body(bounds, category)
    w, s, e, n = bounds
    x, y = (w + e) / 2, (s + n) / 2
    children = [[w, s, x, y], [x, s, e, y], [w, y, x, n], [x, y, e, n]]
    observations, merged, parent_ids, child_ids = [], {}, set(), set()
    for index, area in enumerate([bounds, *children]):
        body = nspd.spatial_body(area, category)
        payload, digest = nspd.request_json(nspd.INTERSECTS, body)
        fc = nspd.normalize(payload)
        if any(f['properties'].get('category') != category for f in fc['features']):
            raise ValueError('Категория контрольного ответа не соответствует слою')
        if len(fc['features']) > 5000:
            raise ValueError('Контрольный ответ слишком велик; уменьшите область')
        selected = [f for f in fc['features'] if shape(f['geometry']).intersection(box(*area)).area > 0]
        ids = {f['id'] for f in selected}
        if index == 0:
            parent_ids = ids
        else:
            child_ids.update(ids)
        raw_features = payload.get('data', payload).get('features', [])
        observations.append({'part': 'parent' if index == 0 else str(index), 'bounds': area,
                             'request': body, 'source': nspd.INTERSECTS, 'sha256': digest,
                             'received_at': store.now(), 'raw_count': len(raw_features),
                             'normalized_count': len(fc['features']), 'count': len(selected)})
        for feature in selected:
            prior = merged.get(feature['id'])
            if prior and not shape(prior['geometry']).equals(shape(feature['geometry'])):
                raise ValueError('Граница одного объекта различается между ответами; прежнее обследование сохранено')
            merged.setdefault(feature['id'], feature)
        if len(merged) > 5000:
            raise ValueError('Суммарный ответ слишком велик; уменьшите область')
    geojson = {'type': 'FeatureCollection', 'features': [merged[key] for key in sorted(merged)]}
    coverage = {'method': 'parent-and-four-children-v1', 'parent_count': len(parent_ids),
                'children_unique_count': len(child_ids), 'merged_count': len(merged),
                'additional_ids': sorted(child_ids - parent_ids), 'parent_only_ids': sorted(parent_ids - child_ids),
                'responses_consistent': parent_ids == child_ids, 'requests': len(observations),
                'complete': False, 'warning': WARNING}
    # This is a manifest hash; individual remote-response hashes remain in observations.
    digest = hashlib.sha256(json.dumps({'geojson': geojson, 'observations': observations}, sort_keys=True).encode()).hexdigest()
    return {'geojson': geojson, 'sha256': digest, 'sha256_kind': 'observation_manifest',
            'received_at': observations[-1]['received_at'], 'request': parent_request,
            'source': nspd.INTERSECTS, 'observations': observations, 'coverage': coverage}
