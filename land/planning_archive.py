"""Resumable public council archive observations, never current zoning proof."""
import copy
import hashlib
import json
import re
from urllib.parse import urlparse
from . import planning_watch as pw, store
from .network import fetch

VERSION = 1
ROOT_URL = 'https://simfmo-rk.ru/resheniya-rajonnogo-soveta-ii-soveta/'
MAX_PAGES, MAX_BYTES, BATCH = 100, 4 * 1024 * 1024, 10
LIMITATION = ('Архив II созыва проверяется порциями по десять страниц. '
              'Он содержит наблюдаемые ссылки на сессии 2019–2024 годов; '
              'исходное решение от 26.06.2019 № 1239 относится к I созыву. '
              'Завершение очереди не подтверждает полноту истории, действие ПЗЗ '
              'или отсутствие других документов. PDF читаются отдельно в каталоге района.')


def sessions(raw):
    return list({r['url']: r for r in pw.parse_links(raw, ROOT_URL)
                 if re.search(r'сесси[яи]\b', r['title'], re.I)
                 and not urlparse(r['url']).path.lower().endswith('.pdf')}.values())


def cached(record):
    sha = record.get('sha256')
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
        raise ValueError('Некорректный SHA-256 страницы архива')
    path = store.DATA / 'planning_archive' / (sha + '.html')
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Сохранённая страница архива отсутствует или превышает предел')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError('Сохранённая страница архива изменилась')
    return raw


def verified(source):
    if not source:
        return []
    content = copy.deepcopy(source); claimed = content.pop('id', None)
    if source.get('version') != VERSION or claimed != pw.digest(content)[:20]:
        raise ValueError('Сохранённая проверка архива изменилась; обновите её')
    root = source.get('root', {})
    if root.get('url') != ROOT_URL:
        raise ValueError('Неизвестный корневой перечень архива')
    allowed = sessions(cached(root))
    pages = source.get('pages')
    if (not isinstance(pages, list) or len(pages) > MAX_PAGES
            or len({p.get('url') for p in pages}) != len(pages)
            or [(p.get('url'), p.get('title')) for p in pages]
               != [(r['url'], r['title']) for r in allowed]):
        raise ValueError('Страницы архива не совпадают с исходным перечнем')
    for page in pages:
        if page.get('state') not in ('pending', 'received', 'retained', 'error'):
            raise ValueError('Неизвестное состояние страницы архива')
        if page.get('sha256'):
            links = pw.parse_links(cached(page), page['url'])
            if links != page.get('links'):
                raise ValueError('Ссылки архива не совпадают с сохранённой страницей')
        elif page.get('state') in ('received', 'retained'):
            raise ValueError('Полученная страница архива не связана с исходными байтами')
    return pages


def receive(url):
    raw, ct, status = fetch(url, max_bytes=MAX_BYTES)
    if 'text/html' not in ct.lower():
        raise ValueError('Страница архива не вернула HTML')
    links = pw.parse_links(raw, url)
    sha = hashlib.sha256(raw).hexdigest()
    folder = store.DATA / 'planning_archive'; folder.mkdir(parents=True, exist_ok=True)
    store.atomic_write(folder / (sha + '.html'), raw)
    return {'url': url, 'state': 'received', 'checked_at': store.now(), 'received_at': store.now(),
            'sha256': sha, 'bytes': len(raw), 'http_status': status, 'links': links}


def counts(source):
    pages = (source or {}).get('pages', [])
    return {'total': len(pages), 'received': sum(p['state'] == 'received' for p in pages),
            'remaining': sum(p['state'] in ('pending', 'retained') for p in pages),
            'errors': sum(p['state'] == 'error' for p in pages),
            'all_observed_pages_received': bool(pages) and all(p['state'] == 'received' for p in pages)}


def scan(project, params):
    if project != 'trudovoe':
        raise ValueError('Архив района настроен только для Трудового')
    old = store.get_setting('planning_archive_' + project)
    if params.get('id') != (old or {}).get('id'):
        raise ValueError('Проверка архива изменилась; обновите отчёт')
    refresh, retry = params.get('refresh', False), params.get('retry', False)
    if not isinstance(refresh, bool) or not isinstance(retry, bool):
        raise ValueError('Некорректные параметры проверки архива')
    if old:
        verified(old)
    result = copy.deepcopy(old) if old else {'version': VERSION, 'pages': []}
    result.update(complete=False, legal_status_confirmed=False,
                  geometry_confirmed=False, limitation=LIMITATION,
                  previous_report_id=(old or {}).get('id'))
    if not old or refresh:
        # A failed root request leaves the last successful snapshot intact.
        root = receive(ROOT_URL)
        rows = sessions(cached(root))
        if not rows or len(rows) > MAX_PAGES:
            raise ValueError('Предел или структура перечня сессий архива не подтверждены')
        previous = {p['url']: p for p in verified(old)} if old else {}
        pages = []
        for row in rows:
            page = copy.deepcopy(previous.get(row['url'], {}))
            page.update(url=row['url'], title=row['title'])
            if page.get('state') == 'received':
                page['state'] = 'retained'
            page.setdefault('state', 'pending')
            pages.append(page)
        result.update(root=root, pages=pages)
    processed = 0
    # Retry failures only when explicitly requested; pending work goes first.
    pending = [p for p in result['pages'] if p['state'] in ('pending', 'retained')]
    errors = [p for p in result['pages'] if p['state'] == 'error'] if retry else []
    for page in (pending + errors)[:BATCH]:
        attempt = {'checked_at': store.now(), 'state': 'error'}
        try:
            observation = receive(page['url'])
            page.update(observation)
            attempt.update(state='received', sha256=page['sha256'])
            page.pop('error', None)
        except Exception as exc:
            page.update(state='error', error=str(exc)[:500])
            attempt['error'] = page['error']
        page['attempt'] = attempt
        processed += 1
        # Don't continue a batch against a site that denies or limits access.
        if page['state'] == 'error':
            break
    if old and not refresh and not processed:
        return {'id': old['id'], 'processed': 0, **counts(old)}
    result['updated_at'] = store.now()
    result.pop('id', None); result['id'] = pw.digest(result)[:20]
    verified(result)
    with store.LOCK:
        if (store.get_setting('planning_archive_' + project) or {}).get('id') != (old or {}).get('id'):
            raise ValueError('Архив изменился во время проверки; прежний результат не заменён')
        folder = store.DATA / 'planning_archive'; folder.mkdir(parents=True, exist_ok=True)
        store.atomic_write(folder / (result['id'] + '.json'), json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8'))
        store.set_setting('planning_archive_' + project, result)
        with store.connect() as db:
            store.event(db, project, 'planning_archive', {'id': result['id'], 'processed': processed, **counts(result)})
    return {'id': result['id'], 'processed': processed, **counts(result)}


def links(project):
    source = store.get_setting('planning_archive_' + project)
    pages = verified(source)
    records, found = [], {}
    if source:
        records.append(dict(source['root'], id='archive_root', archive_source_id=source['id']))
        records[-1].pop('links', None)
    for page in pages:
        records.append({k: page.get(k) for k in ('url', 'state', 'received_at', 'sha256', 'bytes')})
        records[-1].update(id='archive_' + pw.digest(page['url'])[:12],
                          checked_at=page.get('attempt', {}).get('checked_at'),
                          error=page.get('error'), archive_source_id=source['id'])
        for row in page.get('links', []):
            if pw.eligible(row, pzz=True):
                found.setdefault(row['url'], []).append(dict(row, parent_url=page['url'],
                    root_url=ROOT_URL, listing_sha256=page['sha256'], listed_at=page['received_at'],
                    observation_state=page['state'], planning_kind='pzz', archive_source_id=source['id']))
    return (source or {}).get('id'), records, found


def report(project):
    source = store.get_setting('planning_archive_' + project)
    _, _, found = links(project)
    return {'result': source, 'counts': counts(source), 'document_links': len(found),
            'limitation': LIMITATION}
