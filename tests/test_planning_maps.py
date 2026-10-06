import copy
import hashlib
import io
import json
import subprocess
from pathlib import Path
import pytest
from PIL import Image
from shapely.geometry import box, mapping
from land import planning_maps as pm, planning_watch as pw, store


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    store.init()
    return tmp_path


def index(sha, cad='90:12:1:10'):
    result=pm.text_index([(1,f'Фрагмент карты градостроительного зонирования\n{cad} ОД1.4.1\nстарая редакция\nновая редакция')],[])
    return dict(result, source_sha256=sha,algorithm=pm.ALGORITHM,total_pages=1,processed_pages=1,unread_pages=0)


def test_only_supported_map_pair_is_rendered_not_textual_reference():
    pages=[(1,'Внести изменения согласно приложению «Фрагмент карты градостроительного зонирования», 90:12:1:10.'),
           (2,'Фрагмент карты градостроительногозонирования ОД1.4.1\nстарая редакция\nновая редакция'),
           (3,'Фрагмент карты градостроительного зонирования\nновая редакция')]
    result=pm.text_index(pages,[])
    assert [x['page'] for x in result['map_pages']]==[2]
    assert [x['page'] for x in result['map_reference_pages']]==[1,3]
    assert result['map_reference_pages'][0]['number_mentions']==['90:12:1:10']
    assert result['map_pages'][0]['zone_hints']==['ОД1.4.1']
    assert not result['map_geometry_confirmed'] and not result['coordinate_georeferencing_confirmed']


def survey(shape_in=box(0,0,1,1), cad='90:12:1:10'):
    feature={'type':'Feature','geometry':mapping(shape_in),'properties':{'options':{'cad_num':cad}}}
    return {'id':'s1','bounds':[0,0,1,1],'layers':{'parcels':{'geojson':{'type':'FeatureCollection','features':[feature]},
             'source':'https://nspd.gov.ru/observed','received_at':'old-date','sha256':'parcel-hash'}}}


def test_cadastral_match_keeps_parcel_source_and_is_not_zone_geometry():
    data={'documents':[index('0'*64)]}
    result=pm.relate(data,survey())
    match=result['documents'][0]['map_pages'][0]['parcel_number_matches'][0]
    assert match['cadastral_number']=='90:12:1:10' and match['parcel_intersects_survey']
    assert match['received_at']=='old-date' and match['geometry_is_parcel_only'] and not match['zone_geometry_confirmed']
    assert 'parcel_number_matches' not in data['documents'][0]['map_pages'][0]


@pytest.mark.parametrize('bounds',[(1,0,2,1),(2,2,3,3)])
def test_touch_or_distant_parcel_is_not_area_overlap(bounds):
    result=pm.relate({'documents':[index('0'*64)]},survey(box(*bounds)))
    assert not result['documents'][0]['map_pages'][0]['parcel_number_matches'][0]['parcel_intersects_survey']


def test_no_number_match_is_unknown_not_no_zone():
    result=pm.relate({'documents':[index('0'*64)]},survey(cad='90:12:1:11'))
    page=result['documents'][0]['map_pages'][0]
    assert page['unmatched_numbers']==['90:12:1:10'] and page['parcel_number_matches']==[] and not page['geometry_confirmed']


def test_current_year_read_documents_only():
    root=next(s['url'] for s in pw.SOURCES if s['id']=='pzz2026')
    rows=[{'state':s,'currently_listed':listed,'id':str(i),'listing_references':[{'root_url':source}]} for i,(s,listed,source) in enumerate([
        ('read',True,root),('pending',True,root),('read',False,root),('read',True,'other')])]
    assert [r['id'] for r in pm.selected({'items':rows})]==['0']


def source(workspace):
    raw=b'%PDF-synthetic'
    sha=hashlib.sha256(raw).hexdigest()
    folder=workspace/'planning_watch';folder.mkdir(exist_ok=True)
    (folder/(sha+'.pdf')).write_bytes(raw)
    root=next(s['url'] for s in pw.SOURCES if s['id']=='pzz2026')
    row={'id':'doc','sha256':sha,'state':'read','currently_listed':True,'total_pages':1,'url':'https://simfmo-rk.ru/a.pdf',
         'title':'PZZ','received_at':'original-date','listing_references':[{'root_url':root}]}
    store.set_setting('planning_watch_trudovoe',{'id':'p1','items':[row]})
    store.set_setting('survey_trudovoe',survey())
    return sha


def png(sha,page):
    target=pm.image_path(sha,page);target.parent.mkdir(parents=True,exist_ok=True)
    im=Image.new('RGB',(100,200),'white');im.save(target);im.close()
    return {'source_sha256':sha,'page':page,'algorithm':pm.RENDER_ALGORITHM,'width':100,'height':200,
            'image_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'rendered_at':'original-render'}


def test_pdf_and_index_tamper_cannot_be_reused(workspace,monkeypatch):
    sha=source(workspace)
    monkeypatch.setattr(pm,'worker',lambda mode,sha,page=None:index(sha))
    first=pm.inspect_pdf(sha)
    assert not first['cached'] and pm.inspect_pdf(sha)['cached']
    cache=workspace/'planning_maps'/sha/(pm.ALGORITHM+'.json')
    body=json.loads(cache.read_text(encoding='utf-8'));body['map_pages'][0]['page']=20
    cache.write_text(json.dumps(body),encoding='utf-8')
    with pytest.raises(ValueError,match='Кеш'):pm.inspect_pdf(sha)
    (workspace/'planning_watch'/(sha+'.pdf')).write_bytes(b'changed')
    with pytest.raises(ValueError,match='изменился'):pm.inspect_pdf(sha)


def test_render_cache_dates_and_image_tamper(workspace,monkeypatch):
    sha=source(workspace)
    calls=[]
    def worker(mode,sha,page=None):calls.append(mode);return png(sha,page)
    monkeypatch.setattr(pm,'worker',worker)
    first=pm.render_page(sha,1);second=pm.render_page(sha,1)
    assert calls==['render'] and second['cached'] and first['rendered_at']==second['rendered_at']
    pm.image_path(sha,1).write_bytes(b'other')
    with pytest.raises(ValueError,match='SHA-256'):pm.render_page(sha,1)


def test_run_preserves_other_work_and_stale_source_hides_preview(workspace,monkeypatch):
    sha=source(workspace)
    monkeypatch.setattr(pm,'inspect_pdf',lambda sha:index(sha))
    monkeypatch.setattr(pm,'render_page',png)
    saved_survey=store.get_setting('survey_trudovoe')
    store.set_setting('municipal_trudovoe',{'id':'untouched'})
    result=pm.run('trudovoe',{'planning_id':'p1','survey_id':'s1'})
    assert result=={'documents':1,'map_pages':1,'errors':0,'image_errors':0,'network_requests':0}
    assert store.get_setting('survey_trudovoe')==saved_survey and store.get_setting('municipal_trudovoe')=={'id':'untouched'}
    assert not store.candidates('trudovoe') and not pm.report('trudovoe')['stale']
    assert pm.preview('trudovoe','doc',1).startswith(b'\x89PNG')
    with pytest.raises(ValueError,match='Страница'):pm.preview('trudovoe','doc',2)
    changed=copy.deepcopy(saved_survey);changed['id']='s2';store.set_setting('survey_trudovoe',changed)
    assert pm.report('trudovoe')['stale']
    with pytest.raises(ValueError,match='прежним'):pm.preview('trudovoe','doc',1)
    assert b'<img' not in pm.html_report('trudovoe')


def test_concurrent_source_change_preserves_previous_result(workspace,monkeypatch):
    sha=source(workspace)
    store.set_setting('planning_maps_trudovoe',{'id':'old','planning_id':'p1','survey_id':'s1','documents':[]})
    def inspect(sha):
        store.set_setting('planning_watch_trudovoe',{'id':'p2','items':[]})
        return index(sha)
    monkeypatch.setattr(pm,'inspect_pdf',inspect)
    monkeypatch.setattr(pm,'render_page',png)
    with pytest.raises(ValueError,match='во время'):pm.run('trudovoe',{'planning_id':'p1','survey_id':'s1'})
    assert store.get_setting('planning_maps_trudovoe')['id']=='old'
    assert store.get_setting('planning_maps_attempt_trudovoe')['state']=='error'


def test_all_document_errors_preserve_previous_result(workspace,monkeypatch):
    source(workspace)
    store.set_setting('planning_maps_trudovoe',{'id':'old'})
    monkeypatch.setattr(pm,'inspect_pdf',lambda sha:(_ for _ in ()).throw(ValueError('PDF changed')))
    with pytest.raises(ValueError,match='Ни один'):pm.run('trudovoe',{'planning_id':'p1','survey_id':'s1'})
    assert store.get_setting('planning_maps_trudovoe')=={'id':'old'}


def test_render_error_is_visible_without_confirming_geometry(workspace,monkeypatch):
    sha=source(workspace)
    monkeypatch.setattr(pm,'inspect_pdf',lambda sha:index(sha))
    monkeypatch.setattr(pm,'render_page',lambda *a:(_ for _ in ()).throw(ValueError('Poppler unavailable')))
    result=pm.run('trudovoe',{'planning_id':'p1','survey_id':'s1'})
    assert result['image_errors']==1 and result['errors']==0
    saved=pm.report('trudovoe')['result']
    assert not saved['geometry_confirmed'] and not saved['legal_status_confirmed']
    assert saved['documents'][0]['map_pages'][0]['image_error']=='Poppler unavailable'


def test_map_count_limit_is_not_silent_truncation():
    result=index('0'*64)
    result.update(total_pages=30,processed_pages=30)
    result['map_pages']=[dict(result['map_pages'][0],page=n) for n in range(1,26)]
    with pytest.raises(ValueError,match='предел'):pm.validate_index(result)


@pytest.mark.parametrize('change',[{'page':True},{'page':0},{'page':201},{'geometry_confirmed':True}])
def test_index_rejects_invalid_pages_and_geometry_status(change):
    result=index('0'*64);result['map_pages'][0].update(change)
    with pytest.raises(ValueError):pm.validate_index(result)


def test_worker_source_binding_and_timeout(workspace,monkeypatch):
    sha=source(workspace)
    monkeypatch.setattr(pm.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a,0,json.dumps(index('0'*64)),''))
    with pytest.raises(ValueError,match='другому'):pm.worker('inspect',sha)
    def timeout(*a,**k):raise subprocess.TimeoutExpired(a,60)
    monkeypatch.setattr(pm.subprocess,'run',timeout)
    with pytest.raises(ValueError,match='60 секунд'):pm.worker('inspect',sha)


def test_actual_isolated_worker_reads_pdf(workspace):
    from pypdf import PdfWriter
    folder=workspace/'planning_watch';folder.mkdir()
    buffer=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=200,height=200);writer.write(buffer)
    raw=buffer.getvalue();sha=hashlib.sha256(raw).hexdigest();(folder/(sha+'.pdf')).write_bytes(raw)
    result=pm.worker('inspect',sha)
    assert result['total_pages']==1 and result['map_pages']==[] and not result['map_geometry_confirmed']
    import shutil
    if shutil.which('pdftoppm'):
        rendered=pm.worker('render',sha,1)
        assert max(rendered['width'],rendered['height'])==2400
        assert pm.checked_image(sha,1,rendered).startswith(b'\x89PNG')
