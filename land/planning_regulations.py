"""Dated full-page text index of observed PZZ PDFs, never a consolidated regulation."""
import copy
import hashlib
import html
import json
import re
import subprocess
import sys
from urllib.parse import urlencode
from . import store, planning_watch
from .planning_text_worker import ALGORITHM, MAX_OUTPUT, MAX_PAGES, WINDOW

MAX_WINDOWS = 20
MAX_TOTAL_PAGES = 4000
ZONE = r'(?:Ж|ОД|СХ|П|Т|Р|С)\d+(?:\.\d+)*|И|ТО'
ZONE_RE = re.compile(r'(?<![\w.])((?:Ж|ОД|СХ|П|Т|Р|С)\d+(?:\.\d+)*)(?!\w|\.\d)')
SHORT_ZONE_RE = re.compile(r'(?<![\w.])(И|ТО)(?=\s*[)»"]|\.\s*(?:Зона|Территория))')
LIMITATION = ('Поиск по текстовому слою сохранённых PDF ПЗЗ, включая последующие изменения. '
              'Страницы указаны по порядку в PDF; оглавление и печатная нумерация могут отличаться. '
              'Упоминание зоны или ВРИ не устанавливает положение контура, действие нормы или разрешение на использование. '
              'Табличные колонки могут смешиваться при извлечении; параметры сверяют с изображением оригинала. '
              'Сканы не распознаются, отсутствие текста не означает отсутствие нормы. Это не сводная действующая редакция.')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def documents(catalog):
    rows = []
    for row in (catalog or {}).get('items', []):
        if row.get('state') != 'read' or not planning_watch.is_pzz(row):
            continue
        sha, count = row.get('sha256'), row.get('total_pages')
        if (not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha)
                or type(count) is not int or not 1 <= count <= MAX_PAGES
                or not planning_watch.safe_link(row['url'], row['url'])):
            raise ValueError('Некорректный сохранённый источник ПЗЗ')
        rows.append({k: copy.deepcopy(row.get(k)) for k in ('id', 'url', 'title', 'sha256', 'received_at',
                     'act_identity', 'document_role', 'total_pages', 'listing_references', 'listing_conflicts', 'currently_listed')})
    if len(rows) > planning_watch.MAX_DOCUMENTS or sum(r['total_pages'] for r in rows) > MAX_TOTAL_PAGES:
        raise ValueError('Превышен предел документов/страниц ПЗЗ')
    return sorted(rows, key=lambda r: ((r['act_identity'] or {}).get('date', ''), r['id']))


def save(project, result):
    result = copy.deepcopy(result)
    result.pop('id', None)
    result['updated_at'] = store.now()
    result['id'] = digest(result)[:20]
    folder = store.DATA / 'planning_regulations'
    folder.mkdir(parents=True, exist_ok=True)
    store.atomic_write(folder / (result['id'] + '.json'), json.dumps(result, ensure_ascii=False, indent=2).encode())
    store.set_setting('planning_regulations_' + project, result)
    return result


def checked(result):
    content = copy.deepcopy(result)
    claimed = content.pop('id', None)
    if claimed != digest(content)[:20] or result.get('algorithm') != ALGORITHM:
        raise ValueError('Сохранённый индекс текста изменился или требует нового алгоритма')
    rows = result.get('documents', [])
    if len(rows) > planning_watch.MAX_DOCUMENTS or sum(r['total_pages'] for r in rows) > MAX_TOTAL_PAGES:
        raise ValueError('Превышен предел индекса текста')
    for row in rows:
        start = 1
        for w in row['windows']:
            count = min(WINDOW, row['total_pages'] - start + 1)
            if w.get('start') != start or w.get('count') != count or count < 1 or not re.fullmatch(r'[0-9a-f]{64}', w.get('sha256', '')):
                raise ValueError('Порции текста перекрываются или пропускают страницы')
            start += count
    return result


def load(project, version=None):
    if version is None:
        result = store.get_setting('planning_regulations_' + project)
    else:
        if not isinstance(version, str) or not re.fullmatch(r'[0-9a-f]{20}', version):
            raise ValueError('Некорректная версия индекса текста')
        path = store.DATA / 'planning_regulations' / (version + '.json')
        if not path.is_file() or path.stat().st_size > MAX_OUTPUT:
            raise ValueError('Версия индекса текста не найдена или превышает предел')
        result = json.loads(path.read_text(encoding='utf-8'))
        if result.get('id') != version:
            raise ValueError('Версия индекса текста изменилась')
    if result:
        checked(result)
        if result.get('project') != project:
            raise ValueError('Индекс относится к другому проекту')
    return result


def counts(result):
    rows = (result or {}).get('documents', [])
    total = sum(r['total_pages'] for r in rows)
    processed = sum(sum(w['count'] for w in r['windows']) for r in rows)
    return {'documents': len(rows), 'total_pages': total, 'processed_pages': processed,
            'remaining_pages': total - processed,
            'errors': sum(r.get('attempt', {}).get('state') == 'error' for r in rows),
            'sparse_pages': sum(len(w['sparse_pages']) for r in rows for w in r['windows']),
            'complete_documents': sum(sum(w['count'] for w in r['windows']) == r['total_pages'] for r in rows)}


def report(project, version=None):
    result = load(project, version)
    catalog = store.get_setting('planning_watch_' + project)
    return {'result': result, 'counts': counts(result), 'catalog_id': (catalog or {}).get('id'),
            'stale': bool(result and result['planning_id'] != (catalog or {}).get('id')), 'limitation': LIMITATION}


def cached_pdf(project, version, document):
    result = load(project, version)
    row = next((r for r in (result or {}).get('documents', []) if r['id'] == document), None)
    if not row or not re.fullmatch(r'[0-9a-f]{64}', row['sha256']):
        raise ValueError('Документ не найден в этой версии индекса')
    path = store.DATA / 'planning_watch' / (row['sha256'] + '.pdf')
    if not path.is_file() or path.stat().st_size > planning_watch.MAX_BYTES:
        raise ValueError('Сохранённый PDF отсутствует или превышает 32 МБ')
    raw = path.read_bytes()
    if not raw.startswith(b'%PDF-') or hashlib.sha256(raw).hexdigest() != row['sha256']:
        raise ValueError('Сохранённый PDF изменился')
    return raw


def validate_window(value, sha, start, total):
    expected = list(range(start, min(total + 1, start + WINDOW)))
    if (value.get('algorithm') != ALGORITHM or value.get('source_sha256') != sha
            or value.get('start') != start or value.get('total_pages') != total
            or [p.get('page') for p in value.get('pages', [])] != expected
            or any(not isinstance(p.get('text'), str) or len(p['text']) > 500000 for p in value.get('pages', []))):
        raise ValueError('Порция текста не совпадает с PDF/страницами/алгоритмом')
    return value


def read_window(sha, start, total):
    path = store.DATA / 'planning_watch' / (sha + '.pdf')
    opts = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    try:
        output = subprocess.run([sys.executable, '-m', 'land.planning_text_worker', str(path.resolve()), sha, str(start)],
                                cwd=planning_watch.ROOT, capture_output=True, encoding='utf-8', timeout=60, **opts)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Чтение порции превысило 60 секунд; процесс остановлен') from exc
    if len(output.stdout.encode('utf-8')) > MAX_OUTPUT:
        raise ValueError('Вывод процесса превышает 4 МБ')
    value = json.loads(output.stdout)
    if output.returncode or value.get('error'):
        raise ValueError(value.get('error') or 'Ошибка чтения порции PDF')
    return validate_window(value, sha, start, total)


def cached_window(row, window):
    # Paths are constructed from validated identifiers, never from a client-supplied filename.
    sha = row['sha256']
    if not re.fullmatch(r'[0-9a-f]{64}', sha) or type(window.get('start')) is not int:
        raise ValueError('Некорректные реквизиты порции текста')
    path = store.DATA / 'planning_regulations' / ALGORITHM / sha / (str(window['start']) + '.json')
    if not path.is_file() or path.stat().st_size > MAX_OUTPUT:
        raise ValueError('Сохранённая порция текста отсутствует или превышает предел')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != window['sha256']:
        raise ValueError('Сохранённая порция текста изменилась')
    return validate_window(json.loads(raw), sha, window['start'], row['total_pages'])


def read(project, params):
    """At most 20 isolated windows; resume from committed windows, without HTTP calls."""
    old = load(project)
    catalog = store.get_setting('planning_watch_' + project)
    if not catalog or params.get('planning_id') != catalog['id'] or params.get('id') != (old or {}).get('id'):
        raise ValueError('Каталог или индекс изменился; обновите отчёт')
    retry = params.get('retry', False)
    if not isinstance(retry, bool):
        raise ValueError('Некорректный параметр повтора')
    source_rows = documents(catalog)
    if not source_rows:
        raise ValueError('Нет сохранённых прочитанных PDF ПЗЗ')
    initialize = not old or old['planning_id'] != catalog['id']
    if not initialize:
        result = copy.deepcopy(old)
    else:
        previous = {(r['sha256'], r['total_pages']): r for r in (old or {}).get('documents', [])}
        result = {'project': project, 'planning_id': catalog['id'], 'algorithm': ALGORITHM,
                  'documents': [dict(r, windows=copy.deepcopy(previous.get((r['sha256'], r['total_pages']), {}).get('windows', []))) for r in source_rows],
                  'legal_status_confirmed': False, 'candidate_zone_confirmed': False}
    expected_id = (old or {}).get('id')
    if initialize:
        for row in result['documents']:
            for window in row['windows']:
                cached_window(row, window)
        with store.LOCK:
            if ((store.get_setting('planning_watch_' + project) or {}).get('id') != catalog['id']
                    or (store.get_setting('planning_regulations_' + project) or {}).get('id') != expected_id):
                raise ValueError('Источники изменились во время подготовки; прежняя версия сохранена')
            result = save(project, result)
            expected_id = result['id']
    processed, errors, failed = 0, 0, set()
    for _ in range(MAX_WINDOWS):
        selected = next((r for r in result['documents'] if r['id'] not in failed and sum(w['count'] for w in r['windows']) < r['total_pages']
                         and (retry or r.get('attempt', {}).get('state') != 'error')), None)
        if selected is None:
            break
        # An explicit retry authorizes one attempt per failed document in this run.
        start = 1 + sum(w['count'] for w in selected['windows'])
        attempt = {'checked_at': store.now(), 'start': start, 'state': 'error'}
        try:
            for w in selected['windows']:
                cached_window(selected, w)
            value = validate_window(read_window(selected['sha256'], start, selected['total_pages']), selected['sha256'], start, selected['total_pages'])
            raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
            folder = store.DATA / 'planning_regulations' / ALGORITHM / selected['sha256']
            folder.mkdir(parents=True, exist_ok=True)
            store.atomic_write(folder / (str(start) + '.json'), raw)
            selected['windows'].append({'start': start, 'count': len(value['pages']),
                'sha256': hashlib.sha256(raw).hexdigest(), 'parsed_at': attempt['checked_at'],
                'sparse_pages': [p['page'] for p in value['pages'] if len(''.join(p['text'].split())) < 20]})
            attempt['state'] = 'read'
            processed += len(value['pages'])
        except Exception as exc:
            attempt['error'] = str(exc)[:500]
            errors += 1
        selected['attempt'] = attempt
        with store.LOCK:
            if ((store.get_setting('planning_watch_' + project) or {}).get('id') != catalog['id']
                    or (store.get_setting('planning_regulations_' + project) or {}).get('id') != expected_id):
                raise ValueError('Источники изменились во время чтения; прежняя версия сохранена')
            result = save(project, result)
            expected_id = result['id']
        if attempt['state'] == 'error':
            failed.add(selected['id'])
    return {'new_pages': processed, 'run_errors': errors, **counts(result)}


def search(project, zone='', query='', offset=0, version=None):
    if not isinstance(zone, str) or not isinstance(query, str):
        raise ValueError('Некорректный запрос поиска')
    if any(ord(c) < 32 for c in zone + query):
        raise ValueError('Пределы запроса поиска превышены')
    zone, query = zone.strip().upper(), ' '.join(query.split()).casefold()
    if zone and not re.fullmatch(ZONE, zone):
        raise ValueError('Код зоны: например Ж1, Ж1.1, СХ2, ОД1 или ТО')
    if len(query) > 120 or any(ord(c) < 32 for c in query) or type(offset) is not int or not 0 <= offset <= 10000:
        raise ValueError('Пределы запроса поиска превышены')
    data = report(project, version)
    matches, total_matches = [], 0
    # Empty query is a status request, not an accidental full-text dump.
    if zone or query:
        for row in (data['result'] or {}).get('documents', []):
            for window in row['windows']:
                for page in cached_window(row, window)['pages']:
                    text = page['text']
                    zones = sorted({m.group(1) for pattern in (ZONE_RE, SHORT_ZONE_RE) for m in pattern.finditer(text)})
                    if zone and zone not in zones:
                        continue
                    folded = ' '.join(text.split()).casefold()
                    if query and query not in folded:
                        continue
                    total_matches += 1
                    if not offset <= total_matches - 1 < offset + 50:
                        continue
                    matches.append({'document': row['id'], 'title': row['title'], 'url': row['url'],
                                    'sha256': row['sha256'], 'received_at': row['received_at'],
                                    'act_identity': row['act_identity'], 'document_role': row['document_role'],
                                    'listing_conflicts': row['listing_conflicts'], 'page': page['page'],
                                    'zones': zones, 'text': text})
    return {k: v for k, v in data.items() if k != 'result'} | {
        'id': (data['result'] or {}).get('id'), 'updated_at': (data['result'] or {}).get('updated_at'),
        'zone': zone, 'query': query, 'offset': offset, 'total_matches': total_matches,
        'matches': matches, 'next_offset': offset + 50 if total_matches > offset + 50 else None}


def html_report(project, zone='', query='', offset=0, version=None):
    data = search(project, zone, query, offset, version)
    esc = lambda v: html.escape(str(v if v is not None else '—'), quote=True)
    sections = []
    for m in data['matches']:
        act = m['act_identity']
        local_url = '/api/planning/regulations/pdf?' + urlencode({'version': data['id'], 'document': m['document']}) + '#page=' + str(m['page'])
        sections.append('<section><h2>' + esc(m['title']) + '</h2><p>Реквизиты PDF: '
            + esc(f"{act['date']} № {act['number']}" if act else 'не извлечены')
            + '; тип текста: ' + esc({'act_text': 'текст акта', 'draft_text': 'проект', 'planning_document': 'документация'}.get(m['document_role'], 'не установлен'))
            + '. Страница PDF ' + esc(m['page']) + ' · <a href="' + esc(local_url) + '">Сохранённый PDF на этой странице</a>'
            + ' · <a href="' + esc(m['url'] + '#page=' + str(m['page']))
            + '">Официальная ссылка</a></p><p>Получен ' + esc(m['received_at']) + ' · SHA-256 '
            + esc(m['sha256']) + '</p><p>Расхождения с заголовками перечней: ' + esc(len(m['listing_conflicts'] or []))
            + '</p><pre>' + esc(m['text']) + '</pre></section>')
    next_link = ''
    if data['next_offset'] is not None:
        args = {'zone': data['zone'], 'q': data['query'], 'offset': data['next_offset'], 'version': data['id']}
        next_link = '<p><a href="?' + esc(urlencode(args)) + '">Следующие совпадения</a></p>'
    return ('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Поиск по тексту ПЗЗ</title>'
        '<style>body{font:16px/1.5 system-ui;max-width:1080px;margin:32px auto;padding:0 20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5ef;padding:16px}section{border-top:1px solid #b8c4b7;margin-top:24px}a{overflow-wrap:anywhere}input{padding:8px}</style>'
        '<h1>Поиск по тексту ПЗЗ</h1><p>' + esc(LIMITATION) + '</p>'
        + '<p>Версия ' + esc(data['id']) + ' · обработка ' + esc(data['updated_at'])
        + (' · каталог изменился; показана датированная версия.' if data['stale'] else '') + '</p>'
        + '<p>Прочитано страниц ' + esc(data['counts']['processed_pages']) + '/' + esc(data['counts']['total_pages'])
        + '; страниц без достаточного текста ' + esc(data['counts']['sparse_pages']) + '; ошибок ' + esc(data['counts']['errors']) + '.</p>'
        + '<form><input name="zone" aria-label="Упоминание зоны" placeholder="Зона, например Ж1" value="' + esc(data['zone'])
        + '"><input name="q" aria-label="Слова в тексте" placeholder="Слова в тексте" value="' + esc(data['query'])
        + '">' + ('<input type="hidden" name="version" value="' + esc(data['id']) + '">' if data['id'] else '') + '<button>Найти</button></form>'
        + '<p>Найдено страниц: ' + esc(data['total_matches']) + '. Показано ' + esc(len(data['matches'])) + '.</p>'
        + ''.join(sections) + next_link + '</html>').encode('utf-8')


def dossier_section(result):
    source = result.get('sources', {}).get('regulations') or {}
    if not source.get('id'):
        return '<h3>Текст ПЗЗ</h3><p>Полный индекс текста ПЗЗ к этой версии расчёта не подключён.</p>'
    esc = lambda v: html.escape(str(v), quote=True)
    purpose = result.get('parameters', {}).get('purpose')
    words = {'housing': 'индивидуальн', 'personal_farm': 'подсобного', 'agriculture': 'сельскохозяйствен'}.get(purpose, '')
    url = '/api/planning/regulations/report?' + urlencode({'version': source['id'], 'q': words})
    c = source['counts']
    return ('<h3>Текст ПЗЗ</h3><p><a href="' + esc(url) + '">Поиск регламентов и изменений по сохранённым документам</a>'
            ' · страниц ' + esc(c['processed_pages']) + '/' + esc(c['total_pages']) + '. Версия ' + esc(source['id'])
            + '.</p>' + ('<p>Индекс относится к прежней версии каталога документов.</p>' if not source.get('catalog_matches') else '')
            + '<p>Зона этого контура не установлена. Поиск по цели использования показывает упоминания; '
            'нормы из разных редакций не объединяются автоматически.</p>')
