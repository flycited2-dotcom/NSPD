import hashlib
import math
from types import SimpleNamespace

import pytest
from shapely.geometry import box, mapping
from land import planning_boundary as boundary, recon, store


def table_pages(split_columns=False):
    """Synthetic two-part boundary matching the table's count/area controls."""
    width = (12684 - math.sqrt(12684 ** 2 - 32 * 3312172)) / 8
    total_height = 3312172 / width
    lines = ['ОПИСАНИЕ МЕСТОПОЛОЖЕНИЯ ГРАНИЦ С. ТРУДОВОЕ',
             'Пулково 1963, зона 5', '3312172 кв.м. +/- 63420 кв.м.']
    for part, first, counts, y0, height in (
        (1, 1, [37]*4, 5195000, total_height * .8),
        (2, 149, [12, 12, 13, 13], 5200000, total_height * .2),
    ):
        lines.append('Часть N ' + str(part))
        corners = [(4975000, y0), (4975000 + height, y0),
                   (4975000 + height, y0 + width), (4975000, y0 + width)]
        points = []
        for edge, count in enumerate(counts):
            a, b = corners[edge], corners[(edge + 1) % 4]
            points += [(a[0] + (b[0]-a[0])*i/count, a[1] + (b[1]-a[1])*i/count) for i in range(count)]
        for number, (x, y) in list(enumerate(points, first)) + [(first, points[0])]:
            text = f'{number} {x:.2f} {y:.2f} К 5 -'.replace('.', ',')
            lines.append(text.replace(' К 5 -', '\nК\n5\n-') if split_columns else text)
    return [(60, '\n'.join(lines))] + [(page, '') for page in range(61, 65)]


def observation():
    return {'id': 'boundary-v1', 'version': boundary.VERSION,
            'source': {'sha256': boundary.SHA, 'url': boundary.URL, 'received_at': '2026-01-01T00:00:00+00:00'},
            'analysis': {'crs_confirmed': False, 'current_boundary_confirmed': False,
                         'hypotheses': [{'geometry': mapping(box(34.1,44.9,34.3,45.1))}]},
            'history': {'pages': [], 'items': []}}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path); store.init()
    return tmp_path


def test_two_reading_layouts_preserve_all_points_and_explicit_closures():
    first, g = boundary.parse_tables(table_pages())
    second, other = boundary.parse_tables(table_pages(True))
    assert first == second and g.equals_exact(other, 0)
    assert [len(x) for x in first] == [149,51]
    assert g.is_valid and len(g.geoms)==2 and not g.geoms[0].intersects(g.geoms[1])
    assert abs(g.length*5-63420)<1


@pytest.mark.parametrize('before,after', [
    ('Часть N 2','Часть N 1'), ('148 ','147 '), ('2 497', '3 497'),
    ('1 4975000,00 5195000,00 К 5 -', '1 4975000,01 5195000,00 К 5 -'),
    ('Пулково 1963, зона 5','МСК неизвестная'), ('К 5 -','К 4 -'),
])
def test_inconsistent_table_is_rejected_without_repair(before, after):
    pages=table_pages();text=pages[0][1]
    # Change only the closing row for the closure case.
    if before.startswith('1 4975000'):
        start=text.rfind(before);assert start>=0
        text=text[:start]+text[start:].replace(before,after,1)
    else:text=text.replace(before,after,1)
    with pytest.raises(ValueError):boundary.parse_tables([(60,text)]+pages[1:])


def mock_readers(monkeypatch, second=None):
    raw=b'synthetic PDF for controlled reader tests'
    monkeypatch.setattr(boundary,'SHA',hashlib.sha256(raw).hexdigest())
    texts=dict(table_pages())
    monkeypatch.setattr(boundary,'PdfReader',lambda _:SimpleNamespace(is_encrypted=False,pages=[SimpleNamespace(extract_text=lambda i=i:texts.get(i+1,'')) for i in range(66)]))
    monkeypatch.setattr(boundary.municipal,'pdfium_text',lambda *_:({'processed_pages':66},second or table_pages(True)))
    return raw


def test_replaced_pdf_rejected_before_text_reading(monkeypatch):
    monkeypatch.setattr(boundary,'PdfReader',lambda _:pytest.fail('changed PDF must not be parsed'))
    with pytest.raises(ValueError,match='PDF изменился'):boundary.analyze_pdf(b'other PDF')


def test_independent_reading_disagreement_blocks_geometry(monkeypatch):
    # Change a known coordinate token without inventing the row/order.
    normal=table_pages(True);match=boundary.ROW.search(normal[0][1]);assert match
    rows=list(boundary.ROW.finditer(normal[0][1]));target=rows[1]
    token=target.group(2);changed=f"{float(token.replace(',','.'))+.01:.2f}".replace('.',',')
    text=normal[0][1][:target.start()]+normal[0][1][target.start():].replace(token,changed,1)
    raw=mock_readers(monkeypatch,[(60,text)]+normal[1:])
    with pytest.raises(ValueError,match='Независимые чтения'):boundary.analyze_pdf(raw)


def test_missing_transformation_blocks_hypothesis(monkeypatch):
    raw=mock_readers(monkeypatch)
    monkeypatch.setattr(boundary,'TransformerGroup',lambda *a,**k:SimpleNamespace(best_available=False,transformers=[]))
    with pytest.raises(ValueError,match='операции'):boundary.analyze_pdf(raw)


@pytest.mark.parametrize('g,expected', [(box(1,1,2,2),'inside'),(box(11,11,12,12),'outside'),(box(9,9,11,11),'crosses')])
def test_relations_are_hypotheses_and_never_legal_exclusions(g,expected):
    result=boundary.relate(g,[box(0,0,10,10)]*3,observation())
    assert result['relation']==expected and result['operations_agree']
    assert all(result[k] is False for k in ('current_boundary_confirmed','crs_confirmed','legal_status_confirmed','used_for_exclusion'))


def test_disagreeing_transformations_are_explicit():
    result=boundary.relate(box(9,1,10,2),[box(0,0,10,10),box(0,0,9.5,10)],observation())
    assert result['relation']=='varies' and not result['operations_agree']


@pytest.mark.parametrize('date',['2026-01-01','2099-01-01T00:00:00+00:00','unknown'])
def test_undated_or_future_observation_is_not_applied(date):
    source=observation();source['source']['received_at']=date
    assert not boundary.applicable(source)


def test_failed_refresh_retains_source_and_marks_attempt(db,monkeypatch):
    old=observation();store.set_setting('planning_boundary_trudovoe',old)
    old=store.get_setting('planning_boundary_trudovoe')
    monkeypatch.setattr(boundary,'fetch',lambda *a,**k:(_ for _ in ()).throw(ValueError('HTTP 403')))
    with pytest.raises(ValueError,match='403'):boundary.run('trudovoe',{'expected_id':old['id']})
    assert store.get_setting('planning_boundary_trudovoe')==old
    assert store.get_setting('planning_boundary_attempt_trudovoe')['state']=='error'


def test_source_change_before_refresh_blocks_network(db,monkeypatch):
    store.set_setting('planning_boundary_trudovoe',observation())
    monkeypatch.setattr(boundary,'fetch',lambda *a,**k:pytest.fail('stale update must not download'))
    with pytest.raises(ValueError,match='изменилось'):boundary.run('trudovoe',{'expected_id':'other'})


def test_source_changed_during_refresh_is_not_overwritten(db,monkeypatch):
    old=observation();store.set_setting('planning_boundary_trudovoe',old)
    monkeypatch.setattr(boundary,'fetch',lambda *a,**k:(b'pdf','application/pdf',200))
    monkeypatch.setattr(boundary,'analyze_pdf',lambda _:old['analysis'])
    def history(*args):
        store.set_setting('planning_boundary_trudovoe',dict(old,id='another-version'))
        return {'items':[],'pages':[],'all_observed_pages_received':True}
    monkeypatch.setattr(boundary,'collect_history',history)
    with pytest.raises(ValueError,match='во время проверки'):boundary.run('trudovoe',{'expected_id':old['id']})
    assert store.get_setting('planning_boundary_trudovoe')['id']=='another-version'


def test_observed_links_only_and_failed_page_retains_original_date(db,monkeypatch):
    root='https://simfmo-rk.ru'+boundary.ROOT_PATHS['pzz'];year='https://simfmo-rk.ru/2025-2/'
    document='https://simfmo-rk.ru/wp-content/uploads/2025/11/Trudovoe.pdf'
    htmls={boundary.ARCHITECTURE:f'<article><a href="{root}">ПЗЗ</a></article>'.encode(),
           root:f'<article><a href="{year}">2025</a></article>'.encode()}
    calls=[]
    def fetch(url,**kwargs):
        calls.append(url)
        if url==year:raise ValueError('HTTP 403')
        return htmls[url],'text/html',200
    monkeypatch.setattr(boundary,'fetch',fetch)
    old={'pages':[{'url':year,'sha256':'old-hash','received_at':'2025-12-01T00:00:00+00:00',
                   'links':[{'url':document,'title':'ПЗЗ Трудовского поселения','publication_date':None}]}]}
    result=boundary.collect_history(old)
    assert calls==[boundary.ARCHITECTURE,root,year]
    assert not result['all_observed_pages_received'] and not result['complete']
    item=result['items'][0];ref=item['references'][0]
    assert ref['state']=='retained' and ref['received_at']=='2025-12-01T00:00:00+00:00'
    assert not item['document_read'] and not item['legal_status_confirmed']


def test_boundary_observation_changes_dossier_but_not_candidates_or_checks():
    from test_recon import fixture
    values=fixture();params=recon.options({});before=recon.build(values,params)
    values['planning_boundary']=observation()
    after=recon.build(values,params)
    assert [c['geometry'] for c in before['candidates']]==[c['geometry'] for c in after['candidates']]
    for c in after['candidates']:
        assert c['boundary_observation']['relation']=='inside'
        assert c['required_checks'] and not c['rights_confirmed'] and not c['srzu_ready']
    dossier=recon.html_report({'result':after})
    assert 'Гипотеза границы Трудового'.encode() in dossier and boundary.URL.encode() in dossier
    assert after['source_inputs']['planning_boundary']=='boundary-v1'
    assert after['map_layers']['historical_boundary']['features'][0]['properties']['used_for_exclusion'] is False


def test_boundary_input_change_invalidates_search(db):
    result={'version':recon.VERSION,'source_inputs':recon.identities(recon.inputs('trudovoe'))}
    assert not recon.is_stale(result,recon.identities(recon.inputs('trudovoe')))
    store.set_setting('planning_boundary_trudovoe',observation())
    assert recon.is_stale(result,recon.identities(recon.inputs('trudovoe')))
