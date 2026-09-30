"""Bounded checks of known public official pages; no claim of exhaustive search."""
import hashlib
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from . import store
from .network import fetch

SOURCES = (
    {'id': 'planning', 'title': 'Документация по планировке территории',
     'url': 'https://trudovskoe-rk.ru/stroitelstvo-i-arhitektura/', 'format': 'html'},
    {'id': 'news', 'title': 'Администрация информирует — первая страница',
     'url': 'https://trudovskoe-rk.ru/category/administratsiya-informiruet/', 'format': 'html'},
    {'id': 'pzz377', 'title': 'Изменения ПЗЗ № 377 от 13.05.2026',
     'url': 'https://simfmo-rk.ru/wp-content/uploads/2026/05/377_13.05.2026.pdf', 'format': 'pdf'},
)
LIMITATION = 'Проверены только указанные страницы и доступность документа. Содержание вложений, вся история публикаций, действующая сводная редакция ПЗЗ и совпадения с контурами не проверены. Отсутствие заголовка не означает отсутствие заявлений или торгов.'


class ArticleLinks(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.in_article = 0
        self.articles = 0
        self.anchor = None
        self.links = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'article':
            self.in_article += 1
            self.articles += 1
        if self.in_article and tag in ('script', 'style'):
            self.ignored += 1
        if self.in_article and not self.ignored and tag == 'a':
            self.anchor = [dict(attrs).get('href', ''), []]

    def handle_data(self, text):
        if self.anchor and not self.ignored:
            self.anchor[1].append(text)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor:
            href, texts = self.anchor
            self.anchor = None
            title = ' '.join(' '.join(texts).split())
            url = urljoin(self.url, href)
            p = urlparse(url)
            words = ('зем', 'планиров', 'пзз', 'извещ', 'аукцион', 'аренд', 'предостав', 'обсужд', 'дорог', 'межеван')
            if title and any(w in title.lower() for w in words) and p.scheme == 'https' and p.hostname == urlparse(self.url).hostname and not (p.username or p.password or p.query or p.fragment) and p.port in (None, 443):
                self.links.append({'title': title[:2000], 'url': url})
        if tag in ('script', 'style') and self.ignored:
            self.ignored -= 1
        if tag == 'article' and self.in_article:
            self.in_article -= 1


def parse_links(raw, url):
    parser = ArticleLinks(url)
    parser.feed(raw.decode('utf-8-sig', errors='strict'))
    if not parser.articles:
        raise ValueError('Не найден раздел публикаций; страница не принята за проверенный перечень')
    unique = {r['url']: r for r in parser.links}
    if len(unique) > 200:
        raise ValueError('Слишком много ссылок; структура источника требует проверки')
    return list(unique.values())


def run(project):
    previous = store.get_setting('publications_' + project, {})
    result = {'checked_at': store.now(), 'sources': [], 'complete': False,
              'candidate_matches_confirmed': False, 'limitation': LIMITATION}
    for source in SOURCES:
        row = dict(source, checked_at=store.now(), state='error', links=[])
        try:
            raw, ct, code = fetch(source['url'])
            if source['format'] == 'html':
                if 'text/html' not in ct.lower():
                    raise ValueError('Источник не вернул HTML')
                row['links'] = parse_links(raw, source['url'])
            elif not raw.startswith(b'%PDF-'):
                raise ValueError('Вместо PDF получен другой ответ')
            digest = hashlib.sha256(raw).hexdigest()
            folder = store.DATA / 'publications'
            folder.mkdir(exist_ok=True)
            (folder / (digest + '.' + source['format'])).write_bytes(raw)
            row.update(state='received', http_status=code, sha256=digest, bytes=len(raw), content_type=ct,
                       last_success_at=row['checked_at'])
        except Exception as exc:
            row['error'] = str(exc)[:500]
            old = next((x for x in previous.get('sources', []) if x['id'] == source['id']), {})
            # Prior observations stay separately labelled and never become a fresh success.
            if old.get('last_success_at'):
                row.update(last_success_at=old['last_success_at'], previous_links=old.get('links') or old.get('previous_links', []))
        result['sources'].append(row)
    store.set_setting('publications_' + project, result)
    with store.connect() as db:
        store.event(db, project, 'publications', result)
    return {'received': sum(r['state'] == 'received' for r in result['sources']), 'total': len(SOURCES)}
