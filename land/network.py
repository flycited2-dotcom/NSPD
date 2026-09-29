"""Read-only remote adapters. No stored credentials, no bypasses or guessed layer IDs."""
import base64
import hashlib
import ipaddress
import json
import socket
import time
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
import truststore
# Use the operating system's trusted certificate authorities, with full TLS
# validation. This does not install certificates or disable verification.
truststore.inject_into_ssl()
import requests
from .store import DATA, now

MAX_BYTES = 25 * 1024 * 1024
SENSITIVE = ('token', 'key', 'auth', 'cookie', 'password', 'secret', 'session', 'signature')


def public_url(url):
    p = urlparse(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Разрешены публичные HTTPS-адреса без логина и пароля')
    if any(any(s in key.lower() for s in SENSITIVE) for key, _ in parse_qsl(p.query)):
        raise ValueError('Ссылки с токенами и ключами не поддерживаются. Используйте публичную выгрузку или локальный файл.')
    for info in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ValueError('Внутренние и локальные адреса запрещены')
    return url


def fetch(url):
    public_url(url)
    with requests.get(url, timeout=(8, 18), stream=True, allow_redirects=False,
                      headers={'User-Agent': 'LandRecon/1.0 (local read-only research)', 'Accept': 'application/geo+json,application/json,text/html;q=0.8'}) as r:
        if r.status_code in [401, 403, 429]:
            raise ValueError(f'HTTP {r.status_code}: доступ ограничен. Повторы и обход защиты не выполняются.')
        if 300 <= r.status_code < 400:
            raise ValueError('Источник перенаправляет запрос. Проверьте и укажите конечный HTTPS-адрес.')
        r.raise_for_status()
        chunks, size = [], 0
        for chunk in r.iter_content(65536):
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError('Ответ превышает 25 МБ')
            chunks.append(chunk)
        return b''.join(chunks), r.headers.get('Content-Type', ''), r.status_code


def fetch_geojson(url):
    raw, content_type, _ = fetch(url)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError('Источник не вернул JSON; возможно, требуется вход или CAPTCHA') from exc
    if data.get('type') != 'FeatureCollection':
        raise ValueError('Источник не вернул GeoJSON FeatureCollection')
    if any(x.get('rel') == 'next' for x in data.get('links', [])):
        raise ValueError('Это только страница выгрузки: обнаружена следующая страница. Импортируйте полную коллекцию.')
    total = data.get('numberMatched', data.get('totalFeatures'))
    if total is not None and str(total).isdigit() and int(total) != len(data.get('features', [])):
        raise ValueError('Неполная выгрузка: количество объектов не совпадает с общим числом')
    digest = hashlib.sha256(raw).hexdigest()
    folder = DATA / 'evidence'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{digest}.geojson').write_bytes(raw)
    return data, digest


def audit_nspd():
    row = {'id': 'NSPD-HOME', 'purpose': 'Доступность портала', 'type': 'HTML', 'url': 'https://nspd.gov.ru/',
           'method': 'GET', 'parameters': [], 'crs': 'не установлен', 'format': 'не установлен', 'geometry': 'не установлена',
           'auth': 'не установлена', 'limitations': 'Массовый доступ и API не проверены', 'checked_at': now(),
           'note': 'Доступность главной страницы не подтверждает доступность векторов или API.'}
    try:
        raw, ct, code = fetch(row['url'])
        row.update(status='Портал отвечает; API не проверен', http_status=code, format=ct, sha256=hashlib.sha256(raw).hexdigest())
    except Exception as exc:
        row.update(status='Не подключён', error=str(exc)[:500])
    return row


def inspect_har(har):
    """Store only sanitized schemas; never persist HAR, tokens, cookies or request bodies."""
    rows = {}
    for e in har.get('log', {}).get('entries', []):
        req, resp = e.get('request', {}), e.get('response', {})
        p = urlparse(req.get('url', ''))
        if p.hostname not in ['nspd.gov.ru', 'nspd.rosreestr.gov.ru']:
            continue
        if not any(s in p.path.lower() for s in ['api', 'geoportal', 'aeggis', 'wms', 'wfs', 'wmts', 'feature', 'layer', 'search', 'map']):
            continue
        # Parameter VALUES are never retained, including innocuously named credentials.
        names = [k for k, _ in parse_qsl(p.query)]
        safe_path = '/'.join('{id}' if re.fullmatch(r'[A-Za-z0-9_.=-]{24,}', part) else part for part in p.path.split('/'))
        url = urlunparse(('https', p.hostname, safe_path, '', '', ''))
        key = (req.get('method'), url)
        content = resp.get('content', {})
        schema, crs, geometry = [], 'не установлен', 'не подтверждена'
        try:
            raw = content.get('text', '')
            if content.get('encoding') == 'base64':
                raw = base64.b64decode(raw).decode('utf-8')
            body = json.loads(raw)
            if isinstance(body, dict):
                schema = sorted(body.keys())
                if body.get('type') == 'FeatureCollection':
                    geometry = 'GeoJSON FeatureCollection — CRS требует проверки'
        except (ValueError, TypeError, UnicodeError):
            pass
        rows[key] = {'id': hashlib.sha256((str(key)).encode()).hexdigest()[:12], 'purpose': 'Уточнить по действию в интерфейсе',
                     'type': 'XHR веб-клиента', 'url': url, 'method': req.get('method'), 'parameters': names,
                     'crs': crs, 'format': content.get('mimeType', ''), 'geometry': geometry,
                     'auth': 'Есть заголовок авторизации/cookie' if any(h.get('name', '').lower() in ['authorization', 'cookie'] for h in req.get('headers', [])) else 'Не установлена',
                     'limitations': 'Стабильность и разрешение массового использования не подтверждены',
                     'status': 'Наблюдался в HAR; не проверен повторно', 'http_status': resp.get('status'),
                     'checked_at': now(), 'observed_at': e.get('startedDateTime'), 'schema': schema,
                     'note': 'Внутренний endpoint веб-клиента; не документированный публичный API. Значения параметров удалены.'}
    return list(rows.values())
