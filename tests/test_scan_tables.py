import copy
import hashlib
import json
import subprocess
import pytest
from land import store, scan_layout, scan_tables, municipal, ocr


def view(index=0, rows=None, missing=()):
    if rows is None:rows=[('н1','4978000.00','5190000.00'),('н2','4978010.00','5190000.00'),('н3','4978010.00','5190010.00')]
    words=[]
    for ri,row in enumerate(rows):
        for ci,text in enumerate(row):
            if (ri,ci) in missing:continue
            # Reverse OCR column order deliberately: geometry must govern the reading.
            words.append({'text':text,'bbox':[100+ci*240,200+ri*30,130 if ci else 35,14],'line':ci,'word':ri})
    return {'view':index,'width':1000,'height':1400,'image_sha256':hashlib.sha256(b'image').hexdigest(),'words':list(reversed(words))}


def test_position_order_matches_the_row_and_keeps_raw_words():
    r=scan_layout.combine([view(),view(1)])
    assert len(r['tables'])==1 and len(r['tables'][0]['rows'])==3
    first=r['tables'][0]['rows'][0]
    assert first['values']==[['н1',4978000.,5190000.]]*2
    assert first['agreement']=='same_literal_values' and first['verification_required']
    assert first['views'][0]['cells'][1]['text']=='4978000.00'
    assert first['views'][0]['cells'][1]['words'][0]['bbox']==[.34,200/1400,.47,214/1400]
    assert not r['geometry_confirmed'] and not r['georeferenced']
    assert not r['tables'][0]['identity_confirmed'] and not r['tables'][0]['completeness_confirmed']


def test_missing_y_and_point_never_shift_remaining_rows():
    r=scan_layout.combine([view(missing=((0,2),(1,0))),view(1)])['tables'][0]['rows']
    assert len(r)==3 and r[0]['values'][0][2] is None
    assert r[1]['values'][0][2]==5190000. and r[1]['values'][0][0] is None
    assert r[2]['agreement']=='same_literal_values'
    assert all(x['agreement']=='review_required' for x in r[:2])


@pytest.mark.parametrize('value',['519000O.00','5190000.OO','51e90000.00','5190000.00 3'])
def test_ocr_letters_and_extra_tokens_are_not_corrected(value):
    v=view();v['words'][0]['text']=value  # first is the last Y cell in reversed OCR order
    r=scan_layout.combine([v,view(1)])['tables'][0]['rows'][-1]
    assert r['values'][0][2] is None and r['issues']


def test_disagreement_is_visible_and_comma_spaces_remain_raw():
    v=view(1);v['words'][0]['text']='5190011,00'
    r=scan_layout.combine([view(),v])['tables'][0]['rows'][-1]
    assert r['values'][1][2]==5190011. and 'Столбец 2: чтения различаются' in r['issues']
    assert scan_layout.literal('5 190 000,00')==5190000.
    assert scan_layout.literal('H1',True) is None
    assert scan_layout.literal('н 10',True)=='н10'


def test_large_vertical_gap_keeps_tables_separate_and_row_omission_visible():
    a=view();b=view(1)
    for v in (a,b):
        second=copy.deepcopy(v['words'])
        for w in second:w['bbox'][1]+=400
        v['words']+=second
    b['words']=[w for w in b['words'] if w['bbox'][1]!=230]
    r=scan_layout.combine([a,b])
    assert [len(t['rows']) for t in r['tables']]==[3,3]
    assert r['tables'][0]['rows'][1]['views'][1] is None
    assert r['tables'][0]['rows'][1]['issues']


def test_three_coordinate_columns_are_not_silently_selected():
    a=view()
    for i in range(3):a['words'].append({'text':'6190000.00','bbox':[800,200+30*i,130,14]})
    r=scan_layout.combine([a,dict(copy.deepcopy(a),view=1)])
    assert not r['tables'] and len(r['issues'])==2


@pytest.mark.parametrize('box',[[float('nan'),0,4,4],[1000,100,200,10],[-1,100,200,10],[True,100,2,10],[1,2,0,3]])
def test_invalid_bounds_rejected(box):
    a=view();a['words'][0]['bbox']=box
    with pytest.raises(ValueError):scan_layout.combine([a,view(1)])


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init();return tmp_path


def seed(db):
    raw=b'%PDF source';digest=hashlib.sha256(raw).hexdigest()
    folder=db/'municipal';folder.mkdir();(folder/(digest+'.pdf')).write_bytes(raw)
    row=municipal.item({'title':'Planning scan','url':'https://trudovskoe-rk.ru/scan.pdf'},municipal.SOURCES[0]['url'],'listed')
    row.update(state='read',sha256=digest,received_at='old-source-date',processed_pages=3,image_or_sparse_pages=[2])
    source_views=[{k:v[k] for k in ('view','width','height','image_sha256')} for v in (view(),view(1))]
    observation={'page':2,'state':'received','processed_at':'old-ocr-date','views':source_views}
    row['ocr']={'source_sha256':digest,'pages':[observation]}
    store.set_setting('municipal_trudovoe',{'id':'catalog','items':[row]})
    folder=ocr.page_folder(digest);folder.mkdir(parents=True)
    for i in (0,1):(folder/f'p2-v{i}.png').write_bytes(b'image')
    data={'algorithm':scan_tables.ALGORITHM,'source_sha256':digest,'page':2,'processed_at':'word-ocr-date','views':[view(),view(1)]}
    (folder/f'p2-{scan_tables.ALGORITHM}.json').write_text(json.dumps(data),encoding='utf-8')
    return row,observation,data


def test_cached_pilot_separate_from_all_source_and_geometry_settings(db,monkeypatch):
    row,_,_=seed(db);before=store.get_setting('municipal_trudovoe')
    settings={key:{'id':key,'sentinel':'original'} for key in ('schemes_trudovoe','survey_trudovoe','torgi_trudovoe','georeference_trudovoe')}
    for key,data in settings.items():store.set_setting(key,data)
    monkeypatch.setattr(scan_tables.subprocess,'run',lambda *a,**k:pytest.fail('no new OCR'))
    result=scan_tables.run('trudovoe',{'catalog_id':'catalog','document_id':row['id'],'page':2})
    assert result=={'tables':1,'rows':3,'cached':True}
    draft=store.get_setting('scan_tables_trudovoe')['pages'][0]
    assert draft['source_received_at']=='old-source-date' and draft['original_ocr_at']=='old-ocr-date' and draft['word_ocr_at']=='word-ocr-date'
    assert draft['algorithm']==scan_layout.ALGORITHM and draft['word_algorithm']==scan_tables.ALGORITHM
    assert not draft['axes_confirmed'] and draft['state']=='ocr_review_required'
    assert store.get_setting('municipal_trudovoe')==before
    assert all(store.get_setting(k)==v for k,v in settings.items())
    assert not store.candidates('trudovoe')
    assert store.get_setting('scan_tables_attempt_trudovoe')['network_requests']==0


@pytest.mark.parametrize('change',['pdf','image','cached_hash','cached_page','cached_algorithm'])
def test_failed_source_or_cache_check_keeps_previous_result(db,change):
    row,_,data=seed(db);digest=row['sha256'];old={'id':'prior','pages':[{'sentinel':'old'}]}
    store.set_setting('scan_tables_trudovoe',old)
    folder=ocr.page_folder(digest)
    if change=='pdf':(db/'municipal'/(digest+'.pdf')).write_bytes(b'changed')
    elif change=='image':(folder/'p2-v1.png').write_bytes(b'changed')
    else:
        if change=='cached_hash':data['views'][1]['image_sha256']='0'*64
        elif change=='cached_page':data['page']=1
        else:data['algorithm']='old'
        (folder/f'p2-{scan_tables.ALGORITHM}.json').write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(ValueError):scan_tables.run('trudovoe',{'catalog_id':'catalog','document_id':row['id'],'page':2})
    assert store.get_setting('scan_tables_trudovoe')==old
    assert store.get_setting('scan_tables_attempt_trudovoe')['state']=='error'


@pytest.mark.parametrize('page',[True,0,1,4,'2',2.0])
def test_only_current_processed_scan_page_allowed(db,page):
    row,_,_=seed(db)
    with pytest.raises(ValueError):scan_tables.run('trudovoe',{'catalog_id':'catalog','document_id':row['id'],'page':page})
    assert store.get_setting('scan_tables_trudovoe') is None


def test_worker_timeout_keeps_previous_page(db,monkeypatch):
    row,observation,_=seed(db);folder=ocr.page_folder(row['sha256'])
    (folder/f'p2-{scan_tables.ALGORITHM}.json').unlink()
    def timeout(*a,**kw):raise subprocess.TimeoutExpired('worker',60)
    monkeypatch.setattr(scan_tables.subprocess,'run',timeout)
    with pytest.raises(ValueError,match='время'):scan_tables.read_words(row['sha256'],observation)
    assert not (folder/f'p2-{scan_tables.ALGORITHM}.json').exists()
