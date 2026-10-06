"""Bounded district document version observations, independent of land availability."""
import copy
import hashlib
import json
import re
import subprocess
import sys
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from . import store
from .network import fetch

ALGORITHM = 'district-planning-v1'
HOST = 'simfmo-rk.ru'
MAX_LINKS, MAX_SESSIONS, MAX_DOCUMENTS, BATCH = 1000, 16, 100, 3
MAX_BYTES = 32 * 1024 * 1024
ROOT = Path(__file__).resolve().parent.parent
SOURCES = (
    {'id': 'administration2025', 'url': 'https://simfmo-rk.ru/postanovleniya-administratsii-2024-god-2/'},
    {'id': 'administration2026', 'url': 'https://simfmo-rk.ru/postanovleniya-administratsii-2026-god/'},
    {'id': 'planning', 'url': 'https://simfmo-rk.ru/dokumentatsiya-po-planirovke-territorii/'},
    {'id': 'pzz2026', 'url': 'https://simfmo-rk.ru/2026-2/'},
)
LIMITATION = ('Проверяются только четыре указанных перечня района: проекты «Коммунальник»/СНТ «Труд» '
              'и ссылки ПЗЗ Трудовского поселения в разделе 2026 года. Это не полная история, '
              'не сводная действующая редакция и не проверка прав. Реквизиты и связи извлечены из текста; '
              'подлинность, вступление в силу и применимость к контуру требуют сверки. '
              'Сканы не распознаются. Отсутствие документа не подтверждает свободность земли.')


def safe_link(base, href):
    url = urljoin(base, href)
    p = urlparse(url)
    return url if p.scheme == 'https' and p.hostname == HOST and p.port in (None, 443) and not (p.username or p.password or p.query or p.fragment) else None


class Listing(HTMLParser):
    """Keep publication dates inside the same list item as their links."""
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url, self.article, self.articles, self.ignore = url, 0, 0, 0
        self.anchor, self.blocks, self.links = None, [], []

    def handle_starttag(self, tag, attrs):
        if tag == 'article':
            self.article += 1
            self.articles += 1
        if self.article and tag in ('script', 'style', 'nav'):
            self.ignore += 1
        if self.article and not self.ignore:
            if tag == 'li':
                self.blocks.append({'texts': [], 'links': []})
            if tag == 'a':
                self.anchor = [dict(attrs).get('href', ''), []]

    def handle_data(self, data):
        if self.article and not self.ignore:
            if self.blocks:
                self.blocks[-1]['texts'].append(data)
            if self.anchor:
                self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor:
            href, texts = self.anchor
            self.anchor = None
            url = safe_link(self.url, href)
            title = ' '.join(' '.join(texts).split())
            if url and title:
                row = {'url': url, 'title': title[:2000], 'publication_date': None}
                if self.blocks:
                    self.blocks[-1]['links'].append(row)
                else:
                    self.links.append(row)
        if tag == 'li' and self.blocks:
            block = self.blocks.pop()
            text = ' '.join(' '.join(block['texts']).split())
            dates = re.findall(r'Дата\s+(?:опубликования|публикации)\s*:?\s*(\d{2}\.\d{2}\.\d{4})', text, re.I)
            for row in block['links']:
                row['publication_date'] = dates[0] if len(set(dates)) == 1 else None
                self.links.append(row)
        if tag in ('script', 'style', 'nav') and self.ignore:
            self.ignore -= 1
        if tag == 'article' and self.article:
            self.article -= 1


def parse_links(raw, url):
    parser = Listing(url)
    parser.feed(raw.decode('utf-8-sig', errors='strict'))
    if not parser.articles or parser.blocks:
        raise ValueError('Структура официального перечня не подтверждена')
    if len(parser.links) > MAX_LINKS:
        raise ValueError('Перечень превышает лимит ссылок')
    return list({(x['url'], x['title'], x['publication_date']): x for x in parser.links}.values())


def subject(text):
    low = text.lower()
    result = []
    if 'коммунальник' in low:
        result.append('Коммунальник')
    if re.search(r'снт\s*[«"„]?\s*труд\b', low):
        result.append('СНТ «Труд»')
    return result


def eligible(row, pzz=False):
    return urlparse(row['url']).path.lower().endswith('.pdf') and (
        ('трудов' in row['title'].lower() and ('пзз' in row['title'].lower() or re.search(r'правил\w*\s+землепользован', row['title'].lower())))
        if pzz else bool(subject(row['title'])))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def save(project, result):
    result['updated_at'] = store.now()
    result['id'] = digest(result)[:20]
    folder = store.DATA / 'planning_watch'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (result['id'] + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    store.set_setting('planning_watch_' + project, result)


def catalog(project):
    if project != 'trudovoe':
        raise ValueError('Перечни района настроены только для проекта Трудовое')
    old = store.get_setting('planning_watch_' + project) or {}
    result = {'algorithm': ALGORITHM, 'checked_at': store.now(), 'sources': [], 'items': [],
              'complete': False, 'geometry_confirmed': False, 'legal_status_confirmed': False, 'limitation': LIMITATION,
              'previous_report_id': old.get('id')}
    found = {}

    def receive(source, pzz=False, sessions=False):
        record = dict(source, checked_at=store.now(), state='error')
        result['sources'].append(record)
        try:
            raw, ct, code = fetch(source['url'], max_bytes=4 * 1024 * 1024)
            if 'text/html' not in ct.lower():
                raise ValueError('Перечень не вернул HTML')
            links = parse_links(raw, source['url'])
            sha = hashlib.sha256(raw).hexdigest()
            folder = store.DATA / 'planning_watch'
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (sha + '.html')).write_bytes(raw)
            record.update(state='received', sha256=sha, bytes=len(raw), http_status=code, link_count=len(links))
            selected = [x for x in links if re.search(r'сессия\b', x['title'], re.I) and '2026' in x['title'] and not urlparse(x['url']).path.lower().endswith('.pdf')] if sessions else [x for x in links if eligible(x, pzz)]
            if sessions:
                selected = list({x['url']: x for x in selected}.values())
                if len(selected) > MAX_SESSIONS:
                    raise ValueError('Число сессий превышает лимит; перечень не обходился частично')
                for entry in selected:
                    receive({'id': 'session_' + digest(entry['url'])[:12], 'url': entry['url'], 'parent_url': source['url']}, pzz=True)
            else:
                for entry in selected:
                    ref = dict(entry, parent_url=source['url'], root_url=source.get('parent_url', source['url']), listing_sha256=sha, listed_at=record['checked_at'])
                    found.setdefault(entry['url'], []).append(ref)
        except Exception as exc:
            record.update(state='error', error=str(exc)[:500])
            previous = next((x for x in old.get('sources', []) if x['url'] == source['url']), None)
            if previous:
                record['previous_observation'] = {k: previous[k] for k in ('checked_at', 'state', 'sha256') if k in previous}

    for source in SOURCES:
        receive(source, sessions=source['id'] == 'pzz2026')
    if len(found) > MAX_DOCUMENTS:
        raise ValueError('Число документов превышает лимит; прежний каталог сохранён')
    previous = {r['url']: r for r in old.get('items', [])}
    for url, references in sorted(found.items()):
        row = copy.deepcopy(previous.get(url, {}))
        row.update(id=digest(url)[:20], url=url, title=references[0]['title'], listing_references=references,
                   listing_state='listed', currently_listed=True)
        row.setdefault('state', 'pending')
        result['items'].append(row)
    errors = {x['url'] for x in result['sources'] if x['state'] == 'error'}
    for url, old_row in previous.items():
        if url not in found:
            row = copy.deepcopy(old_row)
            row.update(currently_listed=False, listing_state='unknown' if any(x['parent_url'] in errors or x.get('root_url') in errors for x in row['listing_references']) else 'not_seen')
            result['items'].append(row)
    if len(result['items']) > MAX_DOCUMENTS:
        raise ValueError('История перечня превышает лимит; прежний каталог сохранён')
    result['listing_changes'] = {'added': sorted(set(found) - set(previous)),
                                 'not_seen': [r['url'] for r in result['items'] if r['listing_state'] == 'not_seen']}
    result['catalog_revision'] = digest({'checked_at': result['checked_at'], 'sources': result['sources'], 'links': found,
                                         'previous_report_id': old.get('id')})[:20]
    with store.LOCK:
        current = store.get_setting('planning_watch_' + project) or {}
        if current.get('id') != old.get('id'):
            raise ValueError('Каталог района изменился во время получения; прежний результат не заменён')
        save(project, result)
    with store.connect() as db:
        store.event(db, project, 'planning_catalog', {'id': result['id'], 'documents': len(result['items']), 'source_errors': len(errors)})
    return {'id': result['id'], 'documents': len(result['items']), 'source_errors': len(errors)}


MONTHS = {'января': '01', 'февраля': '02', 'марта': '03', 'апреля': '04', 'мая': '05', 'июня': '06',
          'июля': '07', 'августа': '08', 'сентября': '09', 'октября': '10', 'ноября': '11', 'декабря': '12'}
DATE = r'(\d{1,2}\.\d{2}\.\d{4}|\d{1,2}\s+(?:' + '|'.join(MONTHS) + r')\s+\d{4})'
NUMBER = r'(\d+(?:\s*[-–]\s*[а-яё0-9]+)?)'


def identity(text):
    match = re.search(DATE + r'.{0,70}?№\s*' + NUMBER, ' '.join(text.split()), re.I)
    if not match:
        return None
    date, number = match.group(1).lower(), re.sub(r'\s+', '', match.group(2).lower()).replace('–', '-')
    if '.' in date:
        day, month, year = date.split('.')
    else:
        day, name, year = date.split()
        month = MONTHS[name]
    from datetime import date as Date
    return {'date': Date(int(year), int(month), int(day)).isoformat(), 'number': number}


def text_evidence(pages):
    first = pages[0][1] if pages else ''
    head = re.search(r'\b(ПОСТАНОВЛЕНИЕ|РЕШЕНИЕ)\b', first[:1000], re.I)
    own = identity(first[head.end():head.end() + 250]) if head else None
    draft = bool(head and re.search(r'\bпроект\b', first[:head.start()], re.I))
    full = '\n'.join(t for _, t in pages)
    operative = re.search(r'(?:ПОСТАНОВЛЯЕТ|решил[аи]?)\s*:', full, re.I)
    actions, refs = [], []
    if own and operative:
        body = re.split(r'\n\s*(?:Приложение|Председатель|Временно исполняющий|Глава администрации)\b', full[operative.end():], maxsplit=1)[0][:12000]
        paragraphs = re.findall(r'(?:^|\n)\s*\d{1,2}\.(?!\d)\s*(.*?)(?=\n\s*\d{1,2}\.(?!\d)|\Z)', body, re.S)
        for paragraph in paragraphs:
            text = ' '.join(paragraph.split())
            action = next((value for word, value in [('Отменить', 'cancel_reference'), ('Утвердить', 'approve_text'), ('Внести', 'amend_reference')]
                           if re.match(word + r'\b', text, re.I)), None)
            if action:
                actions.append(action)
            if action in ('cancel_reference', 'amend_reference'):
                target = identity(text)
                if target:
                    refs.append(dict(target, relation=action, page=next((n for n, t in pages if target['number'] in t and ('Отменить' in t or 'Внести' in t)), None),
                                     scope_requires_review=True))
    action = actions[0] if len(set(actions)) == 1 else ('multiple_text_actions' if actions else 'unknown')
    return {'document_role': 'draft_text' if draft else ('act_text' if own and operative else 'planning_document'),
            'act_identity': own, 'action': action, 'references': refs,
            'subjects': subject(full[:10000]), 'legal_status_confirmed': False, 'geometry_confirmed': False}


def listing_identity(title):
    text = ' '.join(title.split())
    # A heading about changes to an older act cites that act, not its own identity.
    if re.match(r'^(?:постановление|решение)\b', text, re.I):
        return identity(text.split('«')[0])
    reverse = re.match(r'^№\s*' + NUMBER + r'\s+от\s+' + DATE, text, re.I)
    return identity(reverse.group(2) + ' № ' + reverse.group(1)) if reverse else None


def compare_listing(row):
    conflicts = []
    actual = row.get('act_identity')
    for ref in row['listing_references']:
        listed = listing_identity(ref['title'])
        if listed and actual and listed != actual:
            conflicts.append({'kind': 'act_identity', 'listed': listed, 'pdf': actual, 'parent_url': ref['parent_url']})
        if ref['title'].lower().startswith('проект') and row.get('document_role') == 'act_text':
            conflicts.append({'kind': 'draft_title_act_text', 'parent_url': ref['parent_url']})
        named = subject(ref['title'])
        if named and row.get('subjects') and set(named).isdisjoint(row['subjects']):
            conflicts.append({'kind': 'subject', 'listed': named, 'pdf': row['subjects'], 'parent_url': ref['parent_url']})
    row['listing_conflicts'] = conflicts


def read_pdf(sha):
    path = store.DATA / 'planning_watch' / (sha + '.pdf')
    opts = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    try:
        output = subprocess.run([sys.executable, '-m', 'land.planning_pdf_worker', str(path.resolve()), sha],
                                cwd=ROOT, capture_output=True, encoding='utf-8', timeout=60, **opts)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Чтение PDF превысило 60 секунд; процесс остановлен') from exc
    if len(output.stdout) > 4 * 1024 * 1024:
        raise ValueError('Вывод процесса превышает лимит')
    result = json.loads(output.stdout)
    if output.returncode or result.get('error'):
        raise ValueError(result.get('error') or 'Ошибка чтения PDF')
    if result.get('source_sha256') != sha or result.get('algorithm') != ALGORITHM:
        raise ValueError('Чтение относится к другому PDF/алгоритму')
    return result


def read(project, params):
    old = store.get_setting('planning_watch_' + project)
    if not old or params.get('id') != old['id']:
        raise ValueError('Каталог района изменился; обновите отчёт')
    retry = params.get('retry', False)
    if not isinstance(retry, bool):
        raise ValueError('Некорректный параметр повтора')
    result = copy.deepcopy(old)
    result['previous_report_id'] = old['id']
    available = [r for r in result['items'] if r['currently_listed'] and (r.get('read_revision') != result['catalog_revision'] or (retry and r.get('read_attempt', {}).get('state') == 'error'))]
    selected = sorted(available, key=lambda r: r.get('read_revision') == result['catalog_revision'])[:BATCH]
    if not selected:
        return {'processed': 0, 'remaining': 0}
    for row in selected:
        attempt = {'checked_at': store.now(), 'state': 'error'}
        try:
            if not safe_link(row['url'], row['url']) or not row['url'].lower().endswith('.pdf'):
                raise ValueError('Ссылка PDF не принадлежит официальному перечню')
            raw, ct, code = fetch(row['url'], max_bytes=MAX_BYTES)
            if not raw.startswith(b'%PDF-'):
                raise ValueError('Вместо PDF получен другой ответ')
            sha = hashlib.sha256(raw).hexdigest()
            folder = store.DATA / 'planning_watch'
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (sha + '.pdf')).write_bytes(raw)
            details = read_pdf(sha)
            if row.get('sha256') and row['sha256'] != sha:
                if len(row.get('previous_versions', [])) >= 50:
                    raise ValueError('История документа достигла лимита; прежние версии сохранены')
                row.setdefault('previous_versions', []).append({k: copy.deepcopy(v) for k, v in row.items() if k not in ('previous_versions', 'read_attempt')})
            received_at = row.get('received_at') if row.get('sha256') == sha else attempt['checked_at']
            row.update(details, sha256=sha, bytes=len(raw), content_type=ct, http_status=code, received_at=received_at, parsed_at=store.now(), state='read')
            compare_listing(row)
            attempt.update(state='received', sha256=sha)
        except Exception as exc:
            attempt['error'] = str(exc)[:500]
            if row['state'] != 'read':
                row['state'] = 'error'
        row['read_attempt'] = attempt
        row['read_revision'] = result['catalog_revision']
    # Do not replace a catalog refreshed by another process while downloads were running.
    with store.LOCK:
        current = store.get_setting('planning_watch_' + project)
        if (current or {}).get('id') != old['id']:
            raise ValueError('Каталог района изменился во время чтения; прежний результат не заменён')
        save(project, result)
        with store.connect() as db:
            store.event(db, project, 'planning_read', {'id': result['id'], 'processed': len(selected), 'errors': sum(r['read_attempt']['state'] == 'error' for r in selected)})
    return {'processed': len(selected), 'remaining': sum(r.get('read_revision') != result['catalog_revision'] and r['currently_listed'] for r in result['items'])}


def report(project):
    result = store.get_setting('planning_watch_' + project)
    if not result:
        return {'result': None, 'limitation': LIMITATION}
    result = copy.deepcopy(result)
    timelines = {}
    pzz_root = next((s['url'] for s in SOURCES if s['id'] == 'pzz2026'), None)
    for row in result['items']:
        compare_listing(row)
        row['current_read_state'] = 'unchecked' if row.get('read_revision') != result['catalog_revision'] else row.get('read_attempt', {}).get('state', 'unchecked')
        row['same_bytes_municipal'] = [{'url': r['url'], 'received_at': r.get('received_at')} for r in (store.get_setting('municipal_' + project) or {}).get('items', []) if row.get('sha256') and r.get('sha256') == row['sha256']]
        row['reference_matches'] = [dict(ref, observed_documents=[r['id'] for r in result['items'] if r.get('act_identity') == {k: ref[k] for k in ('date', 'number')}]) for ref in row.get('references', [])]
        if row.get('act_identity') and row.get('document_role') == 'act_text':
            groups = list(row.get('subjects', []))
            if pzz_root and any(r.get('root_url') == pzz_root for r in row['listing_references']):
                groups.append('ПЗЗ Трудовского — отбор по перечню')
            for group in groups:
                key = digest([row['act_identity'], row.get('sha256')])
                entry = timelines.setdefault(group, {}).setdefault(key, {**row['act_identity'], 'action': row.get('action'),
                    'references': row['reference_matches'], 'urls': [], 'listing_conflicts': [], 'current_read_state': row['current_read_state']})
                entry['urls'].append(row['url'])
                entry['listing_conflicts'].extend(row['listing_conflicts'])
    chronological = [{'subject': name, 'entries': sorted(entries.values(), key=lambda x: (x['date'], x['number'])),
                       'legal_status_confirmed': False} for name, entries in timelines.items()]
    return {'result': result, 'timelines': chronological, 'limitation': LIMITATION}


def html_report(project):
    data = report(project)
    result = data['result']
    esc = lambda value: escape(str(value or '—'), quote=True)
    sections = []
    for row in (result or {}).get('items', []):
        act = row.get('act_identity')
        sections.append('<section><h2>' + esc(row['title']) + '</h2><p><a href="' + esc(row['url']) + '">Официальный PDF</a></p><p>'
                        + esc(row['state']) + ' · ' + esc(row.get('document_role')) + ' · ' + esc(row.get('action')) + '</p><p>Реквизиты в PDF: '
                        + esc(f"{act['date']} № {act['number']}" if act else None) + '</p><p>Получен: ' + esc(row.get('received_at'))
                        + '; SHA-256: <code>' + esc(row.get('sha256')) + '</code></p><p>Страницы текста: '
                        + esc(f"{row.get('processed_pages', 0)}/{row.get('total_pages', '?')}; без текста: {row.get('image_or_sparse_pages', [])}")
                        + '</p><pre>' + esc(json.dumps({'публикации': row['listing_references'], 'конфликты': row.get('listing_conflicts', []),
                                                      'ссылки_на_акты': row.get('reference_matches', []), 'совпадения_PDF': row.get('same_bytes_municipal', []),
                                                      'последняя_попытка': row.get('read_attempt')}, ensure_ascii=False, indent=2)) + '</pre></section>')
    return ('<!doctype html><html lang="ru"><meta charset="utf-8"><title>История документов района</title>'
            '<style>body{font:16px/1.5 sans-serif;max-width:1100px;margin:32px auto;padding:0 20px;color:#253125}'
            'section{border-top:1px solid #bbc6b7;margin-top:28px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5ef;padding:16px}'
            'code,a{overflow-wrap:anywhere}h2{font-size:20px}</style><h1>Документы района: версии и реквизиты</h1><p>'
            + esc(LIMITATION) + '</p><p>Дата проверки перечней: ' + esc((result or {}).get('checked_at')) + '</p>'
            + '<h2>Последовательность наблюдаемых актов</h2><p>Группировка помогает сверке документов. Поздняя дата сама по себе не устанавливает действие акта или применимость к участку.</p><pre>'
            + esc(json.dumps(data.get('timelines', []), ensure_ascii=False, indent=2)) + '</pre>'
            + '<pre>' + esc(json.dumps((result or {}).get('sources', []), ensure_ascii=False, indent=2)) + '</pre>'
            + (''.join(sections) or '<p>Перечни ещё не проверены.</p>') + '</html>').encode('utf-8')
