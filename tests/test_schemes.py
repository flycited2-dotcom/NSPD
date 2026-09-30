import hashlib
import json
import subprocess
import pytest
from pypdf import PdfWriter
from land import schemes, store, municipal

HEADER = 'Обозначение земельного участка :ЗУ1\nОбозначение характерных точек границы\nКоординаты, м\nX Y\n1 2 3\n'
ROWS = 'н1 4978800,00 5190000,00\nн2 4978820,00 5190000,00\nн3 4978820,00 5190020,00\nн4 4978800,00 5190020,00\n'


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    return tmp_path


def test_observed_table_preserves_points_axes_pages_and_unknown_crs():
    result=schemes.extract([(1,'5.2. КООРДИНАТНОЕ ОПИСАНИЕ\nСистема координат СК-63\n'+HEADER+ROWS)])
    t=result['tables'][0]
    assert t['local_area_m2']==400 and t['state']=='review_required'
    assert t['points'][0]=={'label':'н1','x':4978800.0,'y':5190000.0,'page':1,'source_row':'н1 4978800,00 5190000,00'}
    assert t['closure']=='implicit_for_preview' and t['outline_xy'][0]==t['outline_xy'][-1]
    assert not t['geometry_confirmed'] and not result['georeferenced']
    assert result['crs_status']=='parameters_missing' and result['unparsed_coordinate_pages']==[]
    assert t['purpose_hint']=='formed_label'


def test_split_page_public_ring_continues_without_becoming_available_land():
    heading=HEADER.replace(':ЗУ1','90:12:171301:1630(1)')
    r=schemes.extract([(12,'3.2 КООРДИНАТЫ ОБЩЕГО ПОЛЬЗОВАНИЯ\n'+heading+'1 4978800 5190000\n2 4978820 5190000\n'),
                      (13,'0123-ПМТ\nЛист № докум. Подп. Дата Изм\nЛист\n3 4978820 5190020\n4 4978800 5190020\n1 4978800 5190000\n')])
    t=r['tables'][0]
    assert t['pages']==[12,13] and t['closure']=='explicit'
    assert t['purpose_hint']=='public_use_context' and t['context_page']==12
    assert t['local_area_m2']==400 and not t['geometry_confirmed']


@pytest.mark.parametrize('rows',[
    'н1 4978800 5190000\nн2 4978820 5190020\nн3 4978800 5190020\nн4 4978820 5190000\n',
    ROWS+'н5 4978800 5190000\n',
    ROWS+'н2 4978890 5190090\n',
    ROWS+'н5 4978890 5190090 55\n',
    ROWS+'нЗ 4978890 5190090\n',
    ROWS+'н1 4978890 5190090\n',
    'н1 4978800 5190000\nн2 4978820 5190000\n',
])
def test_invalid_or_ambiguous_rows_rejected_without_geometry_repair(rows):
    t=schemes.extract([(1,HEADER+rows)])['tables'][0]
    assert t['state']=='rejected' and t['outline_xy'] is None and t['local_area_m2'] is None
    assert t['issues']


def test_unsupported_header_and_boundary_pairs_remain_visible():
    r=schemes.extract([(1,HEADER.replace('X Y','Y X')+ROWS),(2,'Координаты границ территории\n'+ROWS)])
    assert not r['tables'] and r['unparsed_coordinate_pages']==[1,2]
    assert r['crs_status']=='label_missing'
    r=schemes.extract([(1,HEADER.replace('Координаты, м','Координаты, см')+ROWS)])
    assert not r['tables']
    r=schemes.extract([(1,HEADER+'н1 49788О0 5190000\n')])
    assert not r['tables'] and r['unreadable_tables'][0]['issues']


def test_repeated_designation_not_silently_merged_and_limit_not_closed():
    r=schemes.extract([(1,HEADER+ROWS),(2,HEADER+ROWS)])
    assert all(t['state']=='rejected' for t in r['tables']) and len(r['tables'])==2
    r=schemes.apply_page_limit(schemes.extract([(40,HEADER+ROWS)]),40,2)
    assert r['tables'][0]['state']=='rejected' and r['tables'][0]['outline_xy'] is None
    r=schemes.extract([(1,'СК-63 и МСК-90\n'+HEADER+ROWS)])
    assert r['crs_status']=='ambiguous_labels'


def catalog(db):
    rows=[]
    for i in range(2):
        r=municipal.item({'title':'Планировка '+str(i),'url':f'https://trudovskoe-rk.ru/{i}.pdf'},municipal.SOURCES[0]['url'],'listed')
        r.update(state='read',sha256=hashlib.sha256(str(i).encode()).hexdigest(),received_at='source-date',geometry_confirmed=False)
        rows.append(r)
    store.set_setting('municipal_trudovoe',{'id':'catalog','items':rows,'complete':False})
    return rows


def test_batch_continuation_preserves_dates_catalog_and_review(db,monkeypatch):
    rows=catalog(db);monkeypatch.setattr(schemes,'BATCH',1)
    monkeypatch.setattr(schemes,'read_document',lambda d:dict(schemes.extract([(1,HEADER+ROWS)]),source_sha256=d))
    assert schemes.run('trudovoe',{'catalog_id':'catalog'})['remaining']==1
    first=store.get_setting('schemes_trudovoe')['documents'][0]
    assert schemes.run('trudovoe',{'catalog_id':'catalog'})['remaining']==0
    result=store.get_setting('schemes_trudovoe')
    assert result['documents'][0]==first
    assert all(r['source_received_at']=='source-date' for r in result['documents'])
    assert store.get_setting('municipal_trudovoe')['id']=='catalog' and store.candidates('trudovoe')==[]
    assert store.get_setting('schemes_attempt_trudovoe')['network_requests']==0
    with pytest.raises(ValueError):schemes.run('trudovoe',{'catalog_id':'old'})


def test_errors_continue_and_only_explicit_retry(db,monkeypatch):
    rows=catalog(db)
    def read(d):
        if d==rows[0]['sha256']:raise ValueError('changed PDF')
        return dict(schemes.extract([]),source_sha256=d)
    monkeypatch.setattr(schemes,'read_document',read)
    schemes.run('trudovoe',{'catalog_id':'catalog'})
    result=store.get_setting('schemes_trudovoe')
    assert [r['state'] for r in result['documents']]==['error','extracted']
    assert not schemes.pending(store.get_setting('municipal_trudovoe'),result)
    assert len(schemes.pending(store.get_setting('municipal_trudovoe'),result,True))==1
    monkeypatch.setattr(schemes,'read_document',lambda d:dict(schemes.extract([]),source_sha256=d))
    schemes.run('trudovoe',{'catalog_id':'catalog','retry_errors':True})
    assert all(r['state']=='extracted' for r in store.get_setting('schemes_trudovoe')['documents'])


def test_worker_actual_pdf_hash_and_timeout(db,monkeypatch):
    folder=db/'municipal';folder.mkdir()
    w=PdfWriter();w.add_blank_page(width=300,height=300)
    tmp=folder/'test.pdf';w.write(tmp)
    digest=hashlib.sha256(tmp.read_bytes()).hexdigest();path=folder/(digest+'.pdf');tmp.rename(path)
    r=schemes.read_document(digest)
    assert r['processed_pages']==1 and not r['tables'] and r['image_or_sparse_pages']==[1]
    def timeout(*args,**kw):raise subprocess.TimeoutExpired('worker',1)
    monkeypatch.setattr(schemes.subprocess,'run',timeout)
    with pytest.raises(ValueError,match='время'):schemes.read_document(digest)
    path.write_bytes(b'changed')
    with pytest.raises(ValueError,match='изменился'):schemes.read_document(digest)
    with pytest.raises(ValueError):schemes.read_document('../../escape')
