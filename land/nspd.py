"""Read-only endpoints observed in the public NSPD map on 2026-09-29."""
import hashlib
import json
import math
import re
import threading
import time
import ssl
import urllib.request
from urllib.parse import urlencode

import httpx
import truststore
from pyproj import Transformer
from shapely.geometry import shape, mapping, box
from shapely.ops import transform

from . import store
from .network import MAX_BYTES, public_url

CAPTURE = store.ROOT / 'fixtures' / 'nspd' / '2026-09-29'
BASE = 'https://nspd.gov.ru'
INTERSECTS = BASE + '/api/geoportal/v1/intersects?typeIntersect=fullObject'
TITLES = {'parcels': 'Земельные участки из ЕГРН',
          'free': 'Земельные участки, свободные от прав третьих лиц',
          'auction': 'Земельные участки, выставленные на аукцион'}
LOCK = threading.Lock()
LAST_REQUEST = 0.0
WARNING = 'Полнота пространственного покрытия не подтверждена. Пустой ответ не доказывает отсутствие прав. Объекты требуют проверки.'


def catalog():
    data = json.loads((CAPTURE / 'layers-catalog.json').read_text(encoding='utf-8'))
    return {key: next(x for x in data['layers'] if x['title'].strip() == title) for key, title in TITLES.items()}


def request_json(url, body=None):
    global LAST_REQUEST
    public_url(url)
    with LOCK:
        time.sleep(max(0, 1 - (time.monotonic() - LAST_REQUEST)))
        LAST_REQUEST = time.monotonic()
        try:
            # HTTPX does not discover Windows registry proxy settings by itself.
            # Preserve the same OS route as the earlier Requests client.
            proxy = None if urllib.request.proxy_bypass('nspd.gov.ru') else urllib.request.getproxies().get('https')
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            with httpx.Client(http2=True, verify=context, proxy=proxy,
                              timeout=httpx.Timeout(25, connect=8), follow_redirects=False,
                              headers={'Accept': 'application/json', 'User-Agent': 'LandRecon/1.0'}) as client:
                with client.stream('POST' if body is not None else 'GET', url, json=body) as r:
                    if r.status_code != 200:
                        raise ValueError(f'НСПД: HTTP {r.status_code}. Автоматические повторы не выполняются.')
                    chunks, size = [], 0
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError('Ответ НСПД превышает 25 МБ. Уменьшите область.')
                        chunks.append(chunk)
                    raw = b''.join(chunks)
        except httpx.HTTPError as exc:
            raise ValueError('НСПД не завершила запрос за отведённое время или разорвала соединение. Предыдущие результаты сохранены; можно открыть снимок браузера.') from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError('НСПД вернула не JSON; проверьте доступ в браузере.') from exc
    return data, hashlib.sha256(raw).hexdigest()


def normalize(data):
    fc = data.get('data', data)
    if not isinstance(fc, dict) or fc.get('type') != 'FeatureCollection' or not isinstance(fc.get('features'), list):
        raise ValueError('Неожиданный формат ответа НСПД')
    if any(x.get('rel') == 'next' for x in fc.get('links', [])):
        raise ValueError('НСПД вернула только страницу результатов')
    total = fc.get('numberMatched', fc.get('totalFeatures'))
    if total is not None and str(total).isdigit() and int(total) != len(fc['features']):
        raise ValueError('НСПД вернула неполную страницу')
    result, seen = [], set()
    for f in fc['features']:
        g = f.get('geometry') or {}
        crs = g.get('crs', fc.get('crs', {})).get('properties', {}).get('name')
        if crs not in ('EPSG:3857', 'EPSG:4326'):
            raise ValueError('Система координат ответа не подтверждена')
        geom = shape(g)
        if geom.is_empty or not geom.is_valid or geom.geom_type not in ('Polygon', 'MultiPolygon'):
            raise ValueError('Получена некорректная полигональная геометрия')
        if crs == 'EPSG:3857':
            geom = transform(Transformer.from_crs(3857, 4326, always_xy=True).transform, geom)
        if not all(math.isfinite(x) for x in geom.bounds) or not box(-180, -90, 180, 90).covers(geom):
            raise ValueError('Координаты вне допустимых границ')
        p = f.get('properties') or {}
        options = p.get('options') or {}
        key = f"{p.get('category')}:{f.get('id', hashlib.sha256(geom.wkb).hexdigest())}"
        if key in seen:
            continue
        seen.add(key)
        props = {k: p[k] for k in ('category', 'categoryName', 'label', 'externalKey', 'descr') if k in p}
        props['options'] = {k: options[k] for k in ('cad_num', 'specified_area', 'land_record_area', 'ownership_type',
            'right_type', 'permitted_use_established_by_document', 'land_record_category_type', 'readable_address', 'status') if k in options}
        result.append({'type': 'Feature', 'id': key, 'geometry': mapping(geom), 'properties': props})
    return {'type': 'FeatureCollection', 'features': result}


def spatial_body(bounds, category):
    if not isinstance(bounds, list) or len(bounds) != 4:
        raise ValueError('Укажите запад, юг, восток и север в WGS84')
    w, s, e, n = map(float, bounds)
    if not all(math.isfinite(x) for x in (w, s, e, n)) or not (-180 <= w < e <= 180 and -85 < s < n < 85):
        raise ValueError('Некорректная область поиска')
    metric = transform(Transformer.from_crs(4326, 3857, always_xy=True).transform, box(w, s, e, n))
    if metric.area > 10_000_000:
        raise ValueError('Уменьшите область: максимум 10 км² в проекции карты за один запрос')
    geometry = mapping(metric)
    geometry['crs'] = {'type': 'name', 'properties': {'name': 'EPSG:3857'}}
    return {'geom': {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'geometry': geometry, 'properties': {}}]}, 'categories': [{'id': category}]}


def search(project, params):
    attempt = {'started_at': store.now(), 'state': 'running', 'mode': params.get('mode', 'parcels'),
               'cadnum': str(params.get('cadnum', '')).strip(), 'bounds': params.get('bounds')}
    store.set_setting('nspd_attempt_' + project, attempt)
    started = time.monotonic()
    try:
        result = _search(project, params)
    except Exception as exc:
        attempt.update(state='error', error=str(exc), finished_at=store.now(), seconds=round(time.monotonic() - started, 2))
        store.set_setting('nspd_attempt_' + project, attempt)
        raise
    attempt.update(state='done', count=result['count'], finished_at=store.now(), seconds=round(time.monotonic() - started, 2))
    store.set_setting('nspd_attempt_' + project, attempt)
    return result


def _search(project, params):
    mode = params.get('mode', 'parcels')
    query = str(params.get('cadnum', '')).strip()
    if mode == 'snapshot':
        raw = (CAPTURE / 'intersects-control.browser.json').read_bytes()
        data, digest = json.loads(raw), hashlib.sha256(raw).hexdigest()
        source, requested = 'Снимок ответа Chrome · 29.09.2026 · контрольная область', json.loads((CAPTURE / 'intersects-request.json').read_text(encoding='utf-8'))
    elif query:
        if not re.fullmatch(r'\d{1,2}:\d{1,2}:\d{1,10}:\d{1,10}', query):
            raise ValueError('Введите кадастровый номер вида 90:12:172101:420')
        source = BASE + '/api/geoportal/v2/search/geoportal?' + urlencode({'thematicSearchId': 1, 'query': query})
        requested = {'cadnum': query}
        data, digest = request_json(source)
    else:
        if mode not in TITLES:
            raise ValueError('Неизвестный слой НСПД')
        requested = spatial_body(params.get('bounds'), catalog()[mode]['categoryId'])
        source = INTERSECTS
        data, digest = request_json(source, requested)
    fc = normalize(data)
    result = {'geojson': fc, 'source': source, 'received_at': store.now(), 'source_date': '2026-09-29' if mode == 'snapshot' else store.now()[:10],
              'snapshot': mode == 'snapshot', 'mode': mode, 'request': requested, 'sha256': digest,
              'complete': False, 'warning': WARNING, 'count': len(fc['features'])}
    folder = store.DATA / 'nspd_results'
    folder.mkdir(exist_ok=True)
    (folder / (digest + '.json')).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    store.set_setting('nspd_' + project, result)
    with store.connect() as db:
        store.event(db, project, 'nspd_search', {k: v for k, v in result.items() if k != 'geojson'})
    return {'count': result['count'], 'snapshot': result['snapshot']}
