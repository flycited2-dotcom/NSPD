"""Bounded official publication catalog and PDF text evidence, never legal clearance."""
import copy
import hashlib
import io
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from pypdf import PdfReader
from shapely.geometry import box, shape
from . import store, torgi
from .network import fetch, ResponseTooLarge

HOST = 'trudovskoe-rk.ru'
SOURCES = (
    {'title': 'Постановления 2026', 'url': 'https://trudovskoe-rk.ru/postanovleniya-2026/'},
    {'title': 'Постановления 2025', 'url': 'https://trudovskoe-rk.ru/postanovleniya-2025/'},
    {'title': 'Планировка территории', 'url': 'https://trudovskoe-rk.ru/stroitelstvo-i-arhitektura/'},
)
MAX_ITEMS, BATCH, MAX_PAGES = 500, 5, 40
WORDS = ('земель', 'сервитут', 'планиров', 'межеван', 'пзз', 'аукцион', 'торги', 'извещение', 'обсужд')
WARNING = 'Это ограниченный каталог трёх перечней, не полная история заявлений. Номер в тексте — упоминание, а не доказательство предмета извещения. Схемы без проверенных координат не размещаются на карте. Правовой статус, действующая редакция и сроки требуют отдельной проверки.'
LARGE_PDF_ALGORITHM = 'municipal-large-pdf-v1'


def pdf_scope(row):
    if row.get('large_pdf_algorithm') == LARGE_PDF_ALGORITHM:
        return 32, 200
    if row.get('local_pdf_algorithm') == 'municipal-local-pdf-v1':
        return 16, 120
    return 8, 40


def safe_link(base, href):
    url = urljoin(base, href)
    p = urlparse(url)
    return url if p.scheme == 'https' and p.hostname == HOST and p.port in (None, 443) and not (p.username or p.password or p.query or p.fragment) else None


class Article(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url, self.depth, self.articles, self.ignore = url, 0, 0, 0
        self.anchor, self.links, self.texts = None, [], []

    def handle_starttag(self, tag, attrs):
        if tag == 'article':
            self.depth += 1
            self.articles += 1
        if self.depth and tag in ('script', 'style', 'nav'):
            self.ignore += 1
        if self.depth and not self.ignore and tag == 'a':
            self.anchor = [dict(attrs).get('href', ''), []]

    def handle_data(self, data):
        if self.depth and not self.ignore:
            self.texts.append(data)
            if self.anchor:
                self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor:
            href, words = self.anchor
            self.anchor = None
            url = safe_link(self.url, href)
            title = ' '.join(' '.join(words).split())
            if url and title:
                self.links.append({'url': url, 'title': title[:2000]})
        if tag in ('script', 'style', 'nav') and self.ignore:
            self.ignore -= 1
        if tag == 'article' and self.depth:
            self.depth -= 1


def parse_html(raw, url):
    p = Article(url)
    p.feed(raw.decode('utf-8-sig', errors='strict'))
    if not p.articles:
        raise ValueError('Раздел article не найден; ответ не принят за публикацию')
    links = list({(r['url'], r['title']): r for r in p.links}.values())
    if len(links) > MAX_ITEMS:
        raise ValueError('Структура страницы превышает лимит ссылок')
    return links, ' '.join(' '.join(p.texts).split())


def evidence(pages):
    numbers = {}
    coordinate_pages = []
    crs_mentions = []
    for number, text in pages:
        for m in torgi.CAD.finditer(text):
            numbers.setdefault(torgi.canonical(m.group()), set()).add(number)
        if re.search(r'мск[\s-]|ск[\s-]*63|систем\w*\s+координат|координат(?:ы характерных|ное описание)|пулково', text.lower()):
            coordinate_pages.append(number)
        for match in re.finditer(r'\b(?:МСК[\s-]*\d+|СК[\s-]*63|Пулково[\s-]*\d+)', text, flags=re.IGNORECASE):
            crs_mentions.append({'page': number, 'label': match.group()[:100]})
    return {'mentions': [{'cadastral_number': n, 'pages': sorted(v)} for n, v in sorted(numbers.items())],
            'coordinate_label_pages': coordinate_pages, 'crs_mentions': crs_mentions, 'geometry_confirmed': False}


def pdf_text(raw, max_bytes=8 * 1024 * 1024, max_pages=None):
    if max_bytes not in (8 * 1024 * 1024,16 * 1024 * 1024,32 * 1024 * 1024):
        raise ValueError('Недопустимый лимит PDF')
    if max_pages is not None and max_pages not in (40,120,200):raise ValueError('Недопустимый лимит страниц PDF')
    if max_pages==200 and max_bytes!=32*1024*1024:raise ValueError('200 страниц разрешены только для крупных PDF')
    limit=MAX_PAGES if max_pages is None else max_pages
    if not raw.startswith(b'%PDF-') or len(raw) > max_bytes:
        raise ValueError(f'Ожидался PDF не более {max_bytes // (1024 * 1024)} МБ')
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError('Зашифрованный PDF не читается')
    if max_bytes == 32 * 1024 * 1024:
        return pdfium_text(raw, limit)
    count = len(reader.pages)
    if not count:
        raise ValueError('PDF без страниц')
    pages, image_pages = [], []
    for i in range(min(count, limit)):
        page = reader.pages[i]
        stream = page.get_contents()
        if stream and len(stream.get_data()) > 8 * 1024 * 1024:
            raise ValueError('Слишком большой поток страницы PDF')
        text = page.extract_text() or ''
        if len(text) > 500000:
            raise ValueError('Текст страницы превышает лимит')
        pages.append((i + 1, text))
        if len(''.join(text.split())) < 20:
            image_pages.append(i + 1)
    return {**evidence(pages), 'total_pages': count, 'processed_pages': len(pages),
            'unread_pages': count - len(pages), 'image_or_sparse_pages': image_pages,
            'text_layer_complete': count == len(pages) and not image_pages}, pages


def pdfium_text(raw, limit):
    """The large-file child uses PDFium; dense vector drawings exhaust pypdf's text timer."""
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(raw)
    try:
        count = len(document)
        if not count:
            raise ValueError('PDF без страниц')
        pages, image_pages = [], []
        for index in range(min(count, limit)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    if textpage.count_chars() > 500000:
                        raise ValueError('Текст страницы превышает лимит')
                    text = textpage.get_text_range(errors='strict').replace('\r\n', '\n').replace('\r', '\n')
                    if len(text) > 500000:
                        raise ValueError('Текст страницы превышает лимит')
                finally:
                    textpage.close()
            finally:
                page.close()
            pages.append((index + 1, text))
            if len(''.join(text.split())) < 20:
                image_pages.append(index + 1)
        return {**evidence(pages), 'total_pages': count, 'processed_pages': len(pages),
                'unread_pages': count - len(pages), 'image_or_sparse_pages': image_pages,
                'text_layer_complete': count == len(pages) and not image_pages,
                'text_engine': 'PDFium'}, pages
    finally:
        document.close()


def kind(title):
    title = title.lower()
    if 'регламент' in title:
        return 'regulation'
    if 'сервитут' in title:
        return 'restriction'
    if any(w in title for w in ('аукцион', 'извещение', 'торги')):
        return 'notice_or_auction'
    if any(w in title for w in ('планиров', 'межеван', 'пзз', 'обсужд')):
        return 'planning'
    return 'land_document'


def item(link, parent, date):
    ext = urlparse(link['url']).path.lower().rsplit('.', 1)[-1]
    fmt = ext if ext in ('pdf', 'doc', 'docx', 'zip') else 'html'
    dates = re.findall(r'\b\d{2}\.\d{2}\.\d{4}\b', link['title'])
    return dict(link, id=hashlib.sha256(link['url'].encode()).hexdigest()[:20], kind=kind(link['title']),
                format=fmt, listed_at=date, parent_url=parent, date_mentions=dates,
                state='pending' if fmt in ('pdf', 'html') else 'unsupported', geometry_confirmed=False)


def relate(result, survey):
    result['survey_id'] = (survey or {}).get('id')
    observations = torgi.survey_geometries(survey)
    boundary = box(*survey['bounds']) if survey else None
    hashes = {}
    for row in result['items']:
        if row['format'] == 'pdf' and row.get('sha256'):
            hashes.setdefault(row['sha256'], []).append(row)
    for row in result['items']:
        row['content_conflicts'] = [{'title': x['title'], 'url': x['url']}
                                    for x in hashes.get(row.get('sha256'), [])
                                    if x['url'] != row['url'] and x['title'] != row['title']]
        mentions = {m['cadastral_number']: {'cadastral_number': m['cadastral_number'], 'pages': m['pages']}
                    for m in row.get('mentions', []) if m['pages']}
        titles = ' '.join(x['title'] for x in row.get('listing_references', [])) or row['title']
        for m in torgi.CAD.finditer(titles):
            number = torgi.canonical(m.group())
            mentions.setdefault(number, {'cadastral_number': number, 'pages': []})['in_title'] = True
        row['mentions'] = list(mentions.values())
        row['mentions_in_area'] = []
        for mention in row.get('mentions', []):
            obs = observations.get(mention['cadastral_number'], {})
            # Even an exact mention can be a neighbour or an example in a regulation.
            if boundary and any(shape(f['geometry']).intersection(boundary).area > 0 for f in obs.get('features', [])):
                row['mentions_in_area'].append(mention)
        candidates = {}
        ocr = row.get('ocr') or {}
        if ocr.get('source_sha256') == row.get('sha256'):
            for page in ocr.get('pages', []):
                if page['state'] != 'received':
                    continue
                for mention in page.get('mentions', []):
                    n = mention['cadastral_number']
                    record = candidates.setdefault(n, {'cadastral_number': n, 'pages': [], 'agreed_pages': [], 'single_pages': [], 'verification_required': True})
                    record['pages'].append(page['page'])
                    record['agreed_pages' if mention['agreement'] == 'both' else 'single_pages'].append(page['page'])
        row['ocr_mentions'] = list(candidates.values())
        row['ocr_mentions_in_area'] = []
        for mention in row['ocr_mentions']:
            obs = observations.get(mention['cadastral_number'], {})
            if boundary and any(shape(f['geometry']).intersection(boundary).area > 0 for f in obs.get('features', [])):
                row['ocr_mentions_in_area'].append(mention)
    return result


def persist(project, result):
    result['updated_at'] = store.now()
    result['id'] = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
    folder = store.DATA / 'municipal'
    folder.mkdir(exist_ok=True)
    (folder / (result['id'] + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    store.set_setting('municipal_' + project, result)


def catalog(project):
    old = store.get_setting('municipal_' + project, {}) or {}
    old_items = {x['url']: x for x in old.get('items', [])}
    result = {'catalog_at': store.now(), 'sources': [], 'items': [], 'complete': False, 'warning': WARNING}
    rows = {}
    attempt = {'state': 'running', 'started_at': store.now(), 'processed': 0, 'requested': len(SOURCES)}
    store.set_setting('municipal_attempt_' + project, attempt)
    try:
        for source in SOURCES:
            attempt['current_url'] = source['url']
            store.set_setting('municipal_attempt_' + project, attempt)
            raw, ct, code = fetch(source['url'])
            if 'text/html' not in ct.lower():
                raise ValueError('Перечень не вернул HTML')
            links, _ = parse_html(raw, source['url'])
            date = store.now()
            digest = save_raw(raw, 'html')
            selected = [l for l in links if any(w in l['title'].lower() for w in WORDS)]
            result['sources'].append(dict(source, received_at=date, sha256=digest, count=len(selected), http_status=code))
            for link in selected:
                row = item(link, source['url'], date)
                reference = {'title': link['title'], 'parent_url': source['url'], 'listed_at': date}
                if row['url'] in rows:
                    other = rows[row['url']]
                    other.setdefault('listing_references', [])
                    if reference not in other['listing_references']:
                        other['listing_references'].append(reference)
                    other['listing_conflict'] = len({r['title'] for r in other['listing_references']}) > 1
                    continue
                previous = old_items.get(row['url'])
                if previous and previous['title'] == row['title']:
                    row = dict(previous, listed_at=date, parent_url=source['url'])
                    for child in old.get('items', []):
                        if child.get('parent_url') == row['url']:
                            rows.setdefault(child['url'], copy.deepcopy(child))
                row['listing_references'] = [reference]
                row['listing_conflict'] = False
                rows[row['url']] = row
            if len(rows) > MAX_ITEMS:
                raise ValueError('Каталог превышает 500 документов')
            attempt['processed'] += 1
            store.set_setting('municipal_attempt_' + project, attempt)
            time.sleep(1)
        result['items'] = list(rows.values())
        persist(project, relate(result, store.get_setting('survey_' + project)))
        attempt.update(state='done', finished_at=store.now())
        with store.connect() as db:
            store.event(db, project, 'municipal_catalog', {'id': result['id'], 'count': len(rows)})
        return {'count': len(rows)}
    except Exception as exc:
        attempt.update(state='error', error=str(exc)[:500], finished_at=store.now())
        raise
    finally:
        store.set_setting('municipal_attempt_' + project, attempt)


def save_raw(raw, fmt):
    digest = hashlib.sha256(raw).hexdigest()
    folder = store.DATA / 'municipal'
    folder.mkdir(exist_ok=True)
    (folder / (digest + '.' + fmt)).write_bytes(raw)
    return digest


def read(project, params):
    old = store.get_setting('municipal_' + project)
    if not old or params.get('id') != old['id']:
        raise ValueError('Каталог изменился; обновите страницу')
    result = copy.deepcopy(old)
    priority = {'restriction': 0, 'notice_or_auction': 1, 'planning': 2, 'land_document': 3, 'regulation': 4}
    pending = sorted((r for r in result['items'] if r['state'] == 'pending'),
                     key=lambda r: priority[r['kind']])[:BATCH]
    attempt = {'state': 'running', 'started_at': store.now(), 'processed': 0, 'requested': len(pending)}
    store.set_setting('municipal_attempt_' + project, attempt)
    try:
        for row in pending:
            attempt['current_url'] = row['url']
            store.set_setting('municipal_attempt_' + project, attempt)
            # Only stored, validated links from the known official pages can be fetched.
            if safe_link(row['parent_url'], row['url']) != row['url']:
                raise ValueError('Недопустимая ссылка документа')
            try:
                raw, ct, code = fetch(row['url'], max_bytes=8 * 1024 * 1024 if row['format'] == 'pdf' else 25 * 1024 * 1024)
            except ResponseTooLarge as exc:
                row.update(state='rejected', checked_at=store.now(), error=str(exc)[:500])
                attempt['processed'] += 1
                persist(project, relate(result, store.get_setting('survey_' + project)))
                store.set_setting('municipal_attempt_' + project, attempt)
                time.sleep(1)
                continue
            except Exception as exc:
                row.update(last_error=str(exc)[:500], last_error_at=store.now())
                persist(project, relate(result, store.get_setting('survey_' + project)))
                raise
            date = store.now()
            digest = save_raw(raw, row['format'])
            try:
                if row['format'] == 'pdf':
                    details, pages = pdf_text(raw)
                    row.update(details)
                    (store.DATA / 'municipal' / (digest + '.txt')).write_text('\n'.join(f'PAGE {n}\n{t}' for n, t in pages), encoding='utf-8')
                else:
                    if 'text/html' not in ct.lower():
                        raise ValueError('Публикация не вернула HTML')
                    links, text = parse_html(raw, row['url'])
                    row.update(evidence([(1, text)]), text_layer_complete=True)
                    attachments = [l for l in links if urlparse(l['url']).path.lower().endswith(('.pdf', '.doc', '.docx', '.zip'))]
                    known = {r['url'] for r in result['items']}
                    if len(attachments) + len(known) > MAX_ITEMS:
                        raise ValueError('Вложения превышают лимит каталога')
                    for link in attachments:
                        if link['url'] not in known:
                            child = item(link, row['url'], date)
                            child['kind'] = row['kind']
                            result['items'].append(child)
                            known.add(link['url'])
                    row['attachments'] = [l['url'] for l in attachments]
                row.update(state='read', received_at=date, sha256=digest, http_status=code)
                row.pop('last_error', None)
                row.pop('last_error_at', None)
            except Exception as exc:
                row.update(state='rejected', received_at=date, sha256=digest, error=str(exc)[:500])
            attempt['processed'] += 1
            persist(project, relate(result, store.get_setting('survey_' + project)))
            store.set_setting('municipal_attempt_' + project, attempt)
            time.sleep(1)
        attempt.update(state='done', finished_at=store.now())
        persist(project, relate(result, store.get_setting('survey_' + project)))
        with store.connect() as db:
            store.event(db, project, 'municipal_read', {'id': result['id'], 'processed': len(pending)})
        return {'processed': len(pending), 'remaining': sum(r['state'] == 'pending' for r in result['items'])}
    except Exception as exc:
        attempt.update(state='error', error=str(exc)[:500], finished_at=store.now())
        # Successfully read earlier documents remain dated; failed/remaining ones remain pending.
        raise
    finally:
        store.set_setting('municipal_attempt_' + project, attempt)
