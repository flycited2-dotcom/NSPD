import hashlib
import json
import pytest
from shapely.geometry import box, mapping
from land import planning_watch as pw, planning_boundary as pb, planning_maps, recon, store

PARENT='https://simfmo-rk.ru/session-history/'
DOCUMENT='https://simfmo-rk.ru/wp-content/uploads/2025/pzz.pdf'
DATE='2026-01-01T00:00:00+00:00'


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init();return tmp_path


def seal(source):
    source.pop('id',None)
    source['id']=hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest()[:20]
    return source


def history(db,state='received'):
    title='Правила землепользования Трудовского поселения'
    raw=f'<article><a href="{DOCUMENT}">{title}</a></article>'.encode()
    sha=hashlib.sha256(raw).hexdigest();folder=db/'planning_boundary';folder.mkdir(exist_ok=True)
    (folder/(sha+'.html')).write_bytes(raw)
    source={'version':pb.VERSION,'source':{'sha256':pb.SHA,'received_at':DATE},
            'analysis':{'crs_confirmed':False,'current_boundary_confirmed':False,'hypotheses':[{'geometry':mapping(box(34.1,44.9,34.3,45.1))}]},
            'history':{'pages':[{'url':PARENT,'state':state,'checked_at':DATE,'received_at':DATE,'sha256':sha}],
                       'items':[{'url':DOCUMENT,'kind':'pzz','title':title,'references':[{'url':PARENT,'state':state,'received_at':DATE,'sha256':sha,'title':title,'publication_date':None}]}]}}
    seal(source);store.set_setting('planning_boundary_trudovoe',source);return source


def test_cached_observed_history_is_imported_with_original_dates_without_network(db,monkeypatch):
    source=history(db);monkeypatch.setattr(pw,'SOURCES',())
    monkeypatch.setattr(pw,'fetch',lambda *a,**k:pytest.fail('cached history does not download'))
    result=pw.catalog('trudovoe');catalog=store.get_setting('planning_watch_trudovoe')
    assert result['documents']==1 and catalog['history_source_id']==source['id']
    ref=catalog['items'][0]['listing_references'][0]
    assert ref['listed_at']==DATE and ref['planning_kind']=='pzz' and pw.is_pzz(catalog['items'][0])
    assert catalog['sources'][0]['received_at']==DATE and catalog['items'][0]['state']=='pending'
    assert not catalog['complete'] and not catalog['geometry_confirmed']


def test_modified_cached_listing_blocks_catalog_replacement(db,monkeypatch):
    source=history(db);old={'id':'old-catalog'};store.set_setting('planning_watch_trudovoe',old)
    page=source['history']['pages'][0];(db/'planning_boundary'/(page['sha256']+'.html')).write_bytes(b'changed')
    with pytest.raises(ValueError,match='страница перечня изменилась'):pw.catalog('trudovoe')
    assert store.get_setting('planning_watch_trudovoe')==old


def test_unobserved_document_cannot_be_added_even_with_new_snapshot_hash(db):
    source=history(db);source['history']['items'][0]['url']='https://simfmo-rk.ru/invented.pdf'
    store.set_setting('planning_boundary_trudovoe',seal(source))
    with pytest.raises(ValueError,match='не совпадают'):pw.history_links('trudovoe')


def test_modified_snapshot_body_is_rejected(db):
    source=history(db);source['history']['items'][0]['title']='changed'
    store.set_setting('planning_boundary_trudovoe',source)
    with pytest.raises(ValueError,match='проверка границы'):pw.history_links('trudovoe')


def test_retained_listing_has_unknown_current_presence(db,monkeypatch):
    history(db,'retained');monkeypatch.setattr(pw,'SOURCES',())
    result=pw.catalog('trudovoe');row=store.get_setting('planning_watch_trudovoe')['items'][0]
    assert result['source_errors']==1
    assert not row['currently_listed'] and row['listing_state']=='unknown'
    assert row['listing_references'][0]['listed_at']==DATE


def test_changed_history_during_catalog_refresh_blocks_save(db,monkeypatch):
    source=history(db);old={'id':'old-catalog','items':[]};store.set_setting('planning_watch_trudovoe',old)
    monkeypatch.setattr(pw,'SOURCES',({'id':'test','url':'https://simfmo-rk.ru/test/','title':'test'},))
    def fetch(*a,**k):
        store.set_setting('planning_boundary_trudovoe',dict(source,id='new-history'))
        return b'<article></article>','text/html',200
    monkeypatch.setattr(pw,'fetch',fetch)
    with pytest.raises(ValueError,match='ГП/ПЗЗ изменились'):pw.catalog('trudovoe')
    assert store.get_setting('planning_watch_trudovoe')==old


def test_pzz_scope_excludes_other_documents_and_does_not_claim_current_regulations(db,monkeypatch):
    catalog={'id':'catalog','catalog_revision':'revision','items':[
        {'id':'pzz','url':DOCUMENT,'state':'pending','currently_listed':True,'listing_references':[{'planning_kind':'pzz','title':'ПЗЗ','parent_url':PARENT}]},
        {'id':'other','url':'https://simfmo-rk.ru/other.pdf','state':'pending','currently_listed':True,'listing_references':[]} ]}
    store.set_setting('planning_watch_trudovoe',catalog);calls=[]
    def fetch(url,**kw):calls.append(url);return b'%PDF-test','application/pdf',200
    monkeypatch.setattr(pw,'fetch',fetch)
    monkeypatch.setattr(pw,'read_pdf',lambda sha:{'act_identity':None,'geometry_confirmed':False,'legal_status_confirmed':False,'references':[]})
    result=pw.read('trudovoe',{'id':'catalog','scope':'pzz'})
    assert calls==[DOCUMENT] and result['remaining']==0 and result['processed']==1
    saved=store.get_setting('planning_watch_trudovoe');assert saved['items'][1]==catalog['items'][1]
    assert not saved['items'][0]['legal_status_confirmed']
    assert [r['id'] for r in planning_maps.selected(saved)]==['pzz']
    with pytest.raises(ValueError,match='область чтения'):pw.read('trudovoe',{'id':saved['id'],'scope':'bad'})


def test_pzz_text_mentions_are_dossier_context_without_zone_assignment():
    from test_recon import fixture
    values=fixture()
    doc={'url':DOCUMENT,'title':'ПЗЗ Трудовского','state':'read','received_at':DATE,'sha256':'sha',
         'listing_references':[{'planning_kind':'pzz'}], 'mentions':[{'cadastral_number':'90:12:1:1','pages':[1,4]}]}
    values['planning_watch']={'items':[doc]}
    rows=recon.document_context(values,{'90:12:1:1'})
    assert rows[0]['pages']==[1,4] and rows[0]['sha256']=='sha'
    assert 'не установлены' in rows[0]['scope']
    before=recon.build(fixture(),recon.options({}));after=recon.build(values,recon.options({}))
    assert [c['geometry'] for c in before['candidates']]==[c['geometry'] for c in after['candidates']]
    assert all(not c['rights_confirmed'] and not c['srzu_ready'] for c in after['candidates'])


def test_word_date_with_dot_uses_actual_header_not_base_act():
    result=pw.text_evidence([(1,'РЕШЕНИЕ\n19.ноября 2025 г. Симферополь № 290\nО внесении изменений в решение\nот 26.06.2019 № 1239\nрайонный совет решил:\n1. Внести в решение от 26.06.2019 № 1239 изменения.')])
    assert result['act_identity']=={'date':'2025-11-19','number':'290'}
    assert result['references'][0]['number']=='1239'


def test_unknown_header_date_never_inherits_base_act_identity():
    result=pw.text_evidence([(1,'РЕШЕНИЕ\nнечитаемая дата № 290\nО внесении изменений в решение\nот 26.06.2019 № 1239\nрайонный совет решил:\n1. Внести изменения.')])
    assert result['act_identity'] is None and result['document_role']=='planning_document'


def cached_catalog(db, count=1):
    result={'id':'catalog','catalog_revision':'revision','items':[]}
    for n in range(count):
        result['items'].append({'id':str(n),'url':DOCUMENT+str(n),'title':'ПЗЗ','state':'read','currently_listed':True,
            'listing_references':[{'planning_kind':'pzz','title':'ПЗЗ','parent_url':PARENT}],
            'algorithm':'district-planning-v1','sha256':'a'*64,'received_at':DATE,'read_revision':'revision',
            'read_attempt':{'state':'received','checked_at':DATE},'act_identity':{'date':'2019-06-26','number':'1239'}})
    store.set_setting('planning_watch_trudovoe',result);return result


def test_local_reprocess_is_bounded_and_preserves_download_dates(db,monkeypatch):
    before=cached_catalog(db,4);calls=[]
    monkeypatch.setattr(pw,'fetch',lambda *a,**kw:pytest.fail('local reprocess must not download'))
    def parse(sha):
        calls.append(sha);return {'algorithm':pw.ALGORITHM,'source_sha256':sha,'act_identity':{'date':'2025-11-19','number':'290'}}
    monkeypatch.setattr(pw,'read_pdf',parse)
    assert pw.report('trudovoe')['reprocess_remaining']==4
    result=pw.reprocess('trudovoe',{'id':'catalog','scope':'pzz'})
    assert result=={'processed':3,'remaining':1} and len(calls)==3
    after=store.get_setting('planning_watch_trudovoe')
    assert after['catalog_revision']==before['catalog_revision']
    for old,row in zip(before['items'],after['items']):
        assert all(row[k]==old[k] for k in ('received_at','sha256','read_attempt','read_revision'))
    assert after['items'][3]==before['items'][3]
    assert all(r['parser_current'] for r in pw.report('trudovoe')['result']['items'][:3])


def test_reprocess_failure_keeps_previous_result_and_requires_explicit_retry(db,monkeypatch):
    before=cached_catalog(db)
    monkeypatch.setattr(pw,'read_pdf',lambda sha:(_ for _ in ()).throw(ValueError('SHA-256 изменился')))
    assert pw.reprocess('trudovoe',{'id':'catalog'})=={'processed':1,'remaining':0}
    after=store.get_setting('planning_watch_trudovoe')
    assert all(after['items'][0][k]==v for k,v in before['items'][0].items())
    assert after['items'][0]['reprocess_attempt']['state']=='error'
    assert not pw.report('trudovoe')['result']['items'][0]['parser_current']
    assert pw.reprocess('trudovoe',{'id':after['id']})['processed']==0
    assert pw.reprocess('trudovoe',{'id':after['id'],'retry':True})['processed']==1


def test_reprocess_cannot_replace_a_concurrent_catalog(db,monkeypatch):
    cached_catalog(db)
    def parse(sha):
        store.set_setting('planning_watch_trudovoe',{'id':'new','items':[]})
        return {'algorithm':pw.ALGORITHM,'act_identity':None}
    monkeypatch.setattr(pw,'read_pdf',parse)
    with pytest.raises(ValueError,match='во время перечтения'):pw.reprocess('trudovoe',{'id':'catalog'})
    assert store.get_setting('planning_watch_trudovoe')['id']=='new'


def test_cached_pdf_integrity_and_sha_path_are_checked(db):
    with pytest.raises(ValueError,match='Некорректный SHA'):pw.read_pdf('../wrong')
    folder=db/'planning_watch';folder.mkdir()
    raw=b'%PDF-changed';sha='b'*64;(folder/(sha+'.pdf')).write_bytes(raw)
    # The subprocess normally uses the project DATA; exercise the worker against this explicit temporary path.
    import subprocess,sys
    result=subprocess.run([sys.executable,'-m','land.planning_pdf_worker',str(folder/(sha+'.pdf')),sha],capture_output=True,encoding='utf-8')
    assert result.returncode==1 and 'SHA-256 PDF изменился' in json.loads(result.stdout)['error']


def test_dossier_links_actual_read_metadata_without_changing_listing_history(db):
    source=history(db);catalog=cached_catalog(db)
    catalog['history_source_id']=source['id'];catalog['items'][0]['url']=DOCUMENT;catalog['items'][0]['algorithm']=pw.ALGORITHM
    result=recon.history_documents({'planning_boundary':source,'planning_watch':catalog})
    assert not source['history']['items'][0].get('document_read')
    assert result[0]['document_read'] and result[0]['text_observation']['received_at']==DATE
    assert result[0]['text_observation']['parser_current'] and result[0]['text_observation']['catalog_matches_history']
    assert not result[0]['text_observation']['legal_status_confirmed'] and not result[0]['text_observation']['geometry_confirmed']
