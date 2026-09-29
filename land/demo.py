"""Entirely synthetic geometry, kept in a separate project."""
from datetime import date
from shapely.geometry import box, mapping
from .geometry import validate_layer, convert


def fixture_layers():
    # Artificial metre grid; not cadastral or official data.
    crs = 'EPSG:32636'
    x, y = 608000, 4982000
    boundary = box(x, y, x+220, y+160)
    configs = {
        'boundary': [boundary],
        'parcels': [box(x, y, x+70, y+70), box(x+100, y, x+180, y+65), box(x, y+90, x+60, y+160), box(x+85, y+90, x+180, y+160)],
        'roads': [box(x, y+70, x+220, y+85)],
        'exclusions': [box(x+205, y, x+220, y+160)],
        'restrictions': [box(x+180, y+85, x+205, y+120)],
    }
    out = []
    for role, gs in configs.items():
        meta = {'title': 'Учебный слой: ' + role, 'source': 'Синтетический пример; не использовать для подачи',
                'checked_at': date.today().isoformat(), 'crs': crs, 'official': False}
        if role == 'parcels':
            meta.update(complete=True, coverage=mapping(boundary))
        out.append(validate_layer({'role': role, 'metadata': meta,
                                  'geojson': {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'geometry': mapping(g), 'properties': {'name': f'Учебный объект {i+1}'}} for i, g in enumerate(gs)]}}))
    return out
