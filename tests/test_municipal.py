import io
import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from shapely.geometry import box, mapping
from land import municipal, store


def pdf(text=None, pages=1, encrypted=False):
    w = PdfWriter()
    for _ in range(pages):
        p = w.add_blank_page(width=300, height=300)
        if text:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            p[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): w._add_object(font)})})
            content = DecodedStreamObject()
            content.set_data(('BT /F1 10 Tf 10 100 Td (' + text + ') Tj ET').encode())
            p[NameObject('/Contents')] = w._add_object(content)
    if encrypted:
        w.encrypt('test')
    out = io.BytesIO(); w.write(out)
    return out.getvalue()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path); store.init()
    monkeypatch.setattr(municipal.time, 'sleep', lambda *a: None)
    return tmp_path


def test_catalog_parser_only_article_known_host_and_land_titles(db, monkeypatch):
    raw = '''<nav><a href="/x.pdf">Земельный участок</a></nav><article>
    <a href="/wp-content/uploads/a.pdf">Регламент предоставления земельных участков</a>
    <a href="https://evil.invalid/b.pdf">Земля</a><a href="/private.pdf?token=x">Земля</a>
    <a href="/x.pdf#section">Извещение</a><script><a href="/bad.pdf">Земля</a></script>
    <a href="/other.pdf">Социальная выплата</a></article>'''.encode()
    monkeypatch.setattr(municipal, 'fetch', lambda u, **kwargs: (raw, 'text/html', 200))
    municipal.catalog('trudovoe')
    r = store.get_setting('municipal_trudovoe')
    assert len(r['items']) == 1 and r['items'][0]['kind'] == 'regulation'
    assert not r['complete'] and r['items'][0]['state'] == 'pending'
    assert store.candidates('trudovoe') == []
    with pytest.raises(ValueError): municipal.parse_html(b'<div>challenge</div>', municipal.SOURCES[0]['url'])


def test_catalog_failure_retains_previous_snapshot(db, monkeypatch):
    store.set_setting('municipal_trudovoe', {'id': 'old', 'items': []})
    calls = []
    def fail(u):
        calls.append(u)
        if len(calls) == 2: raise ValueError('HTTP 403')
        return '<article><a href="/a.pdf">Земельный участок</a></article>'.encode(), 'text/html', 200
    monkeypatch.setattr(municipal, 'fetch', fail)
    with pytest.raises(ValueError, match='403'): municipal.catalog('trudovoe')
    assert len(calls) == 2 and store.get_setting('municipal_trudovoe')['id'] == 'old'
    assert store.get_setting('municipal_attempt_trudovoe')['state'] == 'error'


def test_pdf_real_text_canonical_mentions_and_scans():
    r, pages = municipal.pdf_text(pdf('Land 90:12:0172101:0420 and 90:12:172101:999'))
    assert r['mentions'] == [{'cadastral_number':'90:12:172101:420','pages':[1]}, {'cadastral_number':'90:12:172101:999','pages':[1]}]
    assert r['text_layer_complete'] and not r['geometry_confirmed'] and pages
    blank, _ = municipal.pdf_text(pdf())
    assert blank['image_or_sparse_pages'] == [1] and not blank['text_layer_complete']
    with pytest.raises(ValueError, match='PDF'): municipal.pdf_text(b'<html>blocked</html>')
    with pytest.raises(ValueError, match='Зашифрован'): municipal.pdf_text(pdf(encrypted=True))


def test_pdf_page_limit_and_coordinate_label(monkeypatch):
    monkeypatch.setattr(municipal, 'MAX_PAGES', 1)
    r, pages = municipal.pdf_text(pdf('Land cadastral number 90:12:172101:420', pages=2))
    assert r['unread_pages'] == 1 and not r['text_layer_complete'] and len(pages) == 1
    e = municipal.evidence([(2, 'Система координат МСК-90; 90:12:172101:420')])
    assert e['coordinate_label_pages'] == [2] and not e['geometry_confirmed']
    e = municipal.evidence([(3, 'Выполнено в системе координат - СК-63')])
    assert e['coordinate_label_pages'] == [3] and e['crs_mentions'] == [{'page':3,'label':'СК-63'}]


def seed(rows):
    store.set_setting('municipal_trudovoe', {'id':'old','items':rows,'catalog_at':'old','sources':[],'complete':False})


def row(url='https://trudovskoe-rk.ru/a.pdf', title='Земельный участок'):
    return municipal.item({'url':url,'title':title}, municipal.SOURCES[0]['url'], 'old')


def test_read_discovers_attachments_but_never_fetches_external(db, monkeypatch):
    seed([row('https://trudovskoe-rk.ru/post/')])
    calls = []
    def fetch(u, **kwargs):
        calls.append(u)
        return '<article><a href="/scheme.pdf">Схема</a><a href="https://evil.invalid/a.pdf">Схема</a><a href="/word.doc">Документ</a>90:12:172101:420</article>'.encode(), 'text/html', 200
    monkeypatch.setattr(municipal, 'fetch', fetch)
    out = municipal.read('trudovoe', {'id':'old'})
    r = store.get_setting('municipal_trudovoe')
    assert len(calls) == 1 and out['remaining'] == 1 and len(r['items']) == 3
    assert r['items'][0]['mentions'] and r['items'][2]['state'] == 'unsupported'
    assert not r['items'][0]['geometry_confirmed']
    with pytest.raises(ValueError, match='изменился'): municipal.read('trudovoe', {'id':'old'})


def test_read_rejected_file_continues_and_transport_stops(db, monkeypatch):
    seed([row(), row('https://trudovskoe-rk.ru/b.pdf'), row('https://trudovskoe-rk.ru/c.pdf')])
    calls=[]
    def fetch(u, **kwargs):
        calls.append(u)
        if len(calls) == 2: raise ValueError('HTTP 429')
        return b'<html>challenge</html>', 'text/html', 200
    monkeypatch.setattr(municipal, 'fetch', fetch)
    with pytest.raises(ValueError, match='429'): municipal.read('trudovoe', {'id':'old'})
    r = store.get_setting('municipal_trudovoe')
    assert len(calls) == 2 and r['items'][0]['state'] == 'rejected'
    assert r['items'][1]['last_error'] == 'HTTP 429' and r['items'][2]['state'] == 'pending'
    assert store.get_setting('municipal_attempt_trudovoe')['state'] == 'error'


def test_mentions_are_not_spatial_proof_and_empty_queue_relate(db, monkeypatch):
    r = row(); r.update(state='read', received_at='old', mentions=[{'cadastral_number':'90:12:172101:420','pages':[1]}])
    seed([r])
    f = {'type':'Feature','geometry':mapping(box(34.2,44.9,34.3,45)), 'properties':{'label':'90:12:172101:420'}}
    store.set_setting('survey_trudovoe', {'id':'new','bounds':[34.2,44.9,34.3,45],'layers':{'parcels':{'geojson':{'features':[f]}}}})
    monkeypatch.setattr(municipal, 'fetch', lambda u, **kwargs: pytest.fail('no new requests'))
    assert municipal.read('trudovoe', {'id':'old'})['processed'] == 0
    result = store.get_setting('municipal_trudovoe')
    assert result['survey_id'] == 'new' and result['items'][0]['mentions_in_area']
    assert result['items'][0]['received_at'] == 'old' and not result['items'][0]['geometry_confirmed']


def test_catalog_retains_dated_reading_and_children(db, monkeypatch):
    parent = row('https://trudovskoe-rk.ru/post/')
    parent.update(state='read', received_at='yesterday')
    child = row('https://trudovskoe-rk.ru/child.pdf'); child['parent_url'] = parent['url']
    seed([parent,child])
    monkeypatch.setattr(municipal, 'fetch', lambda u, **kwargs: ('<article><a href="/post/">Земельный участок</a></article>'.encode(),'text/html',200))
    municipal.catalog('trudovoe')
    r = store.get_setting('municipal_trudovoe')
    p = next(x for x in r['items'] if x['url'] == parent['url'])
    assert p['received_at'] == 'yesterday' and len(r['items']) == 2


def test_title_mentions_marked_separately_even_when_pdf_is_scanned(db, monkeypatch):
    seed([row(title='Сервитут на 90:12:0172101:0420')])
    monkeypatch.setattr(municipal, 'fetch', lambda u, **kwargs: (pdf(), 'application/pdf', 200))
    municipal.read('trudovoe', {'id':'old'})
    r = store.get_setting('municipal_trudovoe')['items'][0]
    assert r['mentions'] == [{'cadastral_number':'90:12:172101:420','pages':[],'in_title':True}]
    assert not r['text_layer_complete'] and not r['geometry_confirmed']


def test_size_limit_does_not_stop_next_document(db, monkeypatch):
    seed([row(), row('https://trudovskoe-rk.ru/b.pdf')])
    calls=[]
    def fetch(u, **kwargs):
        calls.append((u, kwargs))
        if len(calls) == 1: raise municipal.ResponseTooLarge('Ответ превышает 8 МБ')
        return pdf('Land number 90:12:172101:420'), 'application/pdf', 200
    monkeypatch.setattr(municipal, 'fetch', fetch)
    municipal.read('trudovoe', {'id':'old'})
    r = store.get_setting('municipal_trudovoe')['items']
    assert r[0]['state'] == 'rejected' and 'sha256' not in r[0]
    assert r[1]['state'] == 'read' and len(calls) == 2
    assert calls[0][1]['max_bytes'] == 8 * 1024 * 1024


def test_one_file_with_conflicting_titles_preserves_both_references(db, monkeypatch):
    raw = '<article><a href="/a.pdf">Планировка Дельфин</a><a href="/a.pdf">Планировка Эфиронос</a></article>'.encode()
    monkeypatch.setattr(municipal, 'fetch', lambda u, **kwargs: (raw,'text/html',200))
    municipal.catalog('trudovoe')
    r = store.get_setting('municipal_trudovoe')['items']
    assert len(r) == 1 and r[0]['listing_conflict']
    assert {x['title'] for x in r[0]['listing_references']} == {'Планировка Дельфин','Планировка Эфиронос'}
    assert len(r[0]['listing_references']) == 6


def test_identical_pdf_under_different_urls_with_different_titles():
    a = row(title='Планировка Дельфин'); b = row('https://trudovskoe-rk.ru/b.pdf', 'Планировка Эфиронос')
    a.update(state='read',sha256='same'); b.update(state='read',sha256='same')
    result = municipal.relate({'items':[a,b]}, None)
    assert result['items'][0]['content_conflicts'] == [{'title':b['title'],'url':b['url']}]
    assert result['items'][1]['content_conflicts'] and not result['items'][0]['geometry_confirmed']
