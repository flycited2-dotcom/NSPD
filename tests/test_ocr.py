import copy
import hashlib
import json
import subprocess
import pytest
from shapely.geometry import box, mapping
from land import ocr, municipal, store


def digest(raw=b'%PDF-test'):
    return hashlib.sha256(raw).hexdigest()


def page(n=1, text='90:12:172101:420', other=None):
    return {'page':n,'algorithm':ocr.ALGORITHM,'source_sha256':digest(), 'processed_at':'2026-09-01T00:00:00+00:00',
            'views':[{'view':i,'text':t,'image_sha256':digest(b'image'),'width':100+i,'height':200+i,
                      'characters':len(t),'line_count':1,'text_angle':None} for i,t in enumerate([text, text if other is None else other])]}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    return tmp_path


def seed(db, pages=(1,2)):
    folder=db/'municipal';folder.mkdir(exist_ok=True)
    (folder/(digest()+'.pdf')).write_bytes(b'%PDF-test')
    row=municipal.item({'title':'Сервитут','url':'https://trudovskoe-rk.ru/a.pdf'},municipal.SOURCES[0]['url'],'old')
    row.update(state='read',sha256=digest(),received_at='source-date',processed_pages=2,total_pages=2,
               image_or_sparse_pages=list(pages),mentions=[],text_layer_complete=False)
    store.set_setting('municipal_trudovoe',{'id':'old','complete':False,'items':[row]})
    return row


def test_ocr_candidates_agreement_spaces_no_digit_guessing():
    data=page(text='90 : 12 : 0172101 : 0420 and 90:12:172101:999',other='90:12:172101:420')
    r=ocr.summarize(data)
    assert r['mentions']==[{'cadastral_number':'90:12:172101:420','agreement':'both','verification_required':True},
                           {'cadastral_number':'90:12:172101:999','agreement':'one','verification_required':True}]
    assert not r['confidence_available']
    assert ocr.numbers('9О:12:172101:420, 90.12.172101.420')==[]
    sparse=ocr.summarize(page(text=''))
    assert sparse['sparse'] and not sparse['mentions']
    bad=page();bad['views'].pop()
    with pytest.raises(ValueError):ocr.summarize(bad)


def test_run_keeps_source_dates_and_text_mentions_separate(db,monkeypatch):
    row=seed(db)
    monkeypatch.setattr(ocr,'worker',lambda *a:{'name':'Windows.Media.Ocr','language':'ru'})
    monkeypatch.setattr(ocr,'read_page',lambda d,n:ocr.summarize(page(n)))
    f={'type':'Feature','geometry':mapping(box(34.2,44.9,34.3,45)),'properties':{'label':'90:12:172101:420'}}
    store.set_setting('survey_trudovoe',{'id':'s1','bounds':[34.2,44.9,34.3,45],'layers':{'parcels':{'geojson':{'features':[f]}}}})
    out=ocr.run('trudovoe',{'id':'old'})
    r=store.get_setting('municipal_trudovoe');x=r['items'][0]
    assert out=={'processed':2,'remaining':0}
    assert x['received_at']=='source-date' and x['mentions']==[] and x['mentions_in_area']==[]
    assert x['ocr_mentions_in_area'][0]['verification_required'] and not x['geometry_confirmed']
    assert r['complete'] is False and store.candidates('trudovoe')==[]
    assert store.get_setting('municipal_ocr_attempt_trudovoe')['network_requests']==0
    with pytest.raises(ValueError,match='изменился'):ocr.run('trudovoe',{'id':'old'})


def test_errors_continue_and_retry_is_explicit(db,monkeypatch):
    seed(db)
    monkeypatch.setattr(ocr,'worker',lambda *a:{'name':'test'})
    calls=[]
    def read(d,n):
        calls.append(n)
        if n==1:raise ValueError('page error')
        return ocr.summarize(page(n))
    monkeypatch.setattr(ocr,'read_page',read)
    ocr.run('trudovoe',{'id':'old'})
    r=store.get_setting('municipal_trudovoe')
    assert [x['state'] for x in r['items'][0]['ocr']['pages']]==['error','received']
    assert not ocr.queue(r) and len(ocr.queue(r,True))==1
    assert store.get_setting('municipal_ocr_attempt_trudovoe')['state']=='done'
    monkeypatch.setattr(ocr,'read_page',lambda d,n:ocr.summarize(page(n)))
    assert ocr.run('trudovoe',{'id':r['id'],'retry_errors':True})['processed']==1
    assert not ocr.queue(store.get_setting('municipal_trudovoe'),True)


def test_batch_limit_and_continuation_no_repeated_pages(db,monkeypatch):
    seed(db)
    monkeypatch.setattr(ocr,'BATCH_PAGES',1)
    monkeypatch.setattr(ocr,'worker',lambda *a:{'name':'test'})
    calls=[]
    def read(d,n):calls.append(n);return ocr.summarize(page(n))
    monkeypatch.setattr(ocr,'read_page',read)
    assert ocr.run('trudovoe',{'id':'old'})['remaining']==1
    r=store.get_setting('municipal_trudovoe')
    assert ocr.run('trudovoe',{'id':r['id']})['remaining']==0
    assert calls==[1,2]


def test_tampered_source_stops_before_page_and_keeps_prior(db,monkeypatch):
    seed(db)
    (db/'municipal'/(digest()+'.pdf')).write_bytes(b'changed')
    monkeypatch.setattr(ocr,'worker',lambda *a:{'name':'test'})
    monkeypatch.setattr(ocr,'read_page',lambda *a:pytest.fail('must not recognize'))
    with pytest.raises(ValueError,match='изменился'):ocr.run('trudovoe',{'id':'old'})
    assert store.get_setting('municipal_trudovoe')['id']=='old'
    assert store.get_setting('municipal_ocr_attempt_trudovoe')['state']=='error'


def test_worker_timeout_and_unavailable_engine(db,monkeypatch):
    def timeout(*a,**kw):raise subprocess.TimeoutExpired('worker',1)
    monkeypatch.setattr(ocr.subprocess,'run',timeout)
    with pytest.raises(ValueError,match='время'):ocr.worker(['--probe'],1)
    seed(db)
    monkeypatch.setattr(ocr,'worker',lambda *a:(_ for _ in ()).throw(ValueError('Russian language unavailable')))
    with pytest.raises(ValueError):ocr.run('trudovoe',{'id':'old'})
    assert store.get_setting('municipal_trudovoe')['id']=='old'


def test_cached_page_preserves_recognition_date_and_validates_identity(db,monkeypatch):
    folder=ocr.page_folder(digest());folder.mkdir(parents=True)
    target=folder/('p1-'+ocr.ALGORITHM+'.json');target.write_text(json.dumps(page()),encoding='utf-8')
    monkeypatch.setattr(ocr,'worker',lambda *a:pytest.fail('cached page'))
    r=ocr.read_page(digest(),1)
    assert r['cached'] and r['processed_at']=='2026-09-01T00:00:00+00:00'
    bad=page(2);target.write_text(json.dumps(bad),encoding='utf-8')
    with pytest.raises(ValueError,match='другому'):ocr.read_page(digest(),1)
    with pytest.raises(ValueError):ocr.page_folder('../../escape')


def test_preview_matches_current_source_image_hash_and_rejects_unknown(db):
    row=seed(db,pages=(1,))
    observation=ocr.summarize(page())
    row['ocr']={'source_sha256':digest(),'pages':[observation]}
    store.set_setting('municipal_trudovoe',{'id':'new','items':[row]})
    folder=ocr.page_folder(digest());folder.mkdir(parents=True)
    path=folder/'p1-v0.png';path.write_bytes(b'image')
    assert ocr.preview('trudovoe',row['id'],1,0)==b'image'
    path.write_bytes(b'tampered')
    with pytest.raises(ValueError,match='изменилось'):ocr.preview('trudovoe',row['id'],1,0)
    with pytest.raises(ValueError):ocr.preview('trudovoe','unknown',1,0)
    with pytest.raises(ValueError):ocr.preview('trudovoe',row['id'],2,0)
    with pytest.raises(ValueError):ocr.preview('trudovoe',row['id'],1,2)
