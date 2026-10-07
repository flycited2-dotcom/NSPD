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
    changed=copy.deepcopy(saved_survey);changed['id']='s2';changed['layers']['parcels']['received_at']='new-date';store.set_setting('survey_trudovoe',changed)
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


def complete_late_text(workspace, monkeypatch):
    from land import planning_regulations as pr
    sha = source(workspace)
    catalog = store.get_setting('planning_watch_trudovoe')
    catalog['items'][0]['total_pages'] = 507
    store.set_setting('planning_watch_trudovoe', catalog)
    def text_window(sha, start, total):
        return {'algorithm': pr.ALGORITHM, 'source_sha256': sha, 'start': start, 'total_pages': total,
                'pages': [{'page': p, 'text': 'Фрагмент карты градостроительного зонирования\nстарая редакция\nновая редакция\nЖ2'
                          if p in (506,507) else 'Обычный текст правил землепользования'}
                         for p in range(start, min(start + 40, total + 1))]}
    monkeypatch.setattr(pr, 'read_window', text_window)
    pr.read('trudovoe', {'id': None, 'planning_id': 'p1'})
    full = pr.report('trudovoe')['result']
    return sha, full['documents'][0], full['id']


def marker_result(sha):
    return {'algorithm': pm.FULL_ALGORITHM, 'source_sha256': sha, 'total_pages': 507,
            'markers_processed_pages': 507, 'standard_geopdf_markers': [{'page': 507, 'keys': ['/VP']}]}


def test_full_verified_text_discovers_late_maps_and_preserves_prefix_cache(workspace, monkeypatch):
    sha, row, text_id = complete_late_text(workspace, monkeypatch)
    folder = store.DATA / 'planning_maps' / sha; folder.mkdir(parents=True, exist_ok=True)
    prefix_path = folder / (pm.ALGORITHM + '.json'); prefix_path.write_bytes(b'old-prefix-cache')
    calls = []
    def markers(mode, source_sha, page=None):
        calls.append(mode); assert mode == 'markers'; return marker_result(source_sha)
    monkeypatch.setattr(pm, 'worker', markers)
    result = pm.inspect_full_text(sha, row)
    assert result['processed_pages'] == 507 and result['unread_pages'] == 0
    assert [p['page'] for p in result['map_pages']] == [506,507]
    assert result['standard_geopdf_markers'][0]['page'] == 507
    assert not result['map_geometry_confirmed'] and not result['coordinate_georeferencing_confirmed']
    assert pm.inspect_full_text(sha, row)['cached'] and calls == ['markers']
    assert prefix_path.read_bytes() == b'old-prefix-cache'


def test_full_text_window_tamper_rejects_cached_map_analysis(workspace, monkeypatch):
    from land import planning_regulations as pr
    sha, row, text_id = complete_late_text(workspace, monkeypatch)
    monkeypatch.setattr(pm, 'worker', lambda *args: marker_result(sha))
    pm.inspect_full_text(sha, row)
    path = store.DATA / 'planning_regulations' / pr.ALGORITHM / sha / '481.json'
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='изменилась'):
        pm.inspect_full_text(sha, row)


def test_full_map_snapshot_depends_on_text_version_and_reports_complete_coverage(workspace, monkeypatch):
    from land import planning_regulations as pr
    sha, row, text_id = complete_late_text(workspace, monkeypatch)
    monkeypatch.setattr(pm, 'worker', lambda *args: marker_result(sha))
    monkeypatch.setattr(pm, 'render_page', png)
    pm.run('trudovoe', {'planning_id': 'p1', 'survey_id': 's1'})
    report = pm.report('trudovoe')
    assert report['result']['text_source_id'] == text_id and not report['stale']
    assert report['coverage'] == {'processed_pages':507,'total_pages':507,'unread_pages':0,'failed_documents':0}
    assert report['image_summary']['rendered'] == 2
    assert not report['result']['geometry_confirmed']
    full = pr.load('trudovoe'); full['observation_note'] = 'new-index-version'; pr.save('trudovoe', full)
    assert pm.report('trudovoe')['stale']
    with pytest.raises(ValueError, match='прежним'):
        pm.preview('trudovoe', 'doc', 506)


def test_full_text_changed_during_render_cannot_replace_map_snapshot(workspace, monkeypatch):
    from land import planning_regulations as pr
    sha, row, text_id = complete_late_text(workspace, monkeypatch)
    store.set_setting('planning_maps_trudovoe', {'id':'previous','documents':[]})
    monkeypatch.setattr(pm, 'worker', lambda *args: marker_result(sha))
    def changed(sha, page):
        full = pr.load('trudovoe'); full['note'] = str(page); pr.save('trudovoe', full)
        return png(sha,page)
    monkeypatch.setattr(pm, 'render_page', changed)
    with pytest.raises(ValueError, match='Полный текст изменился'):
        pm.run('trudovoe', {'planning_id':'p1','survey_id':'s1'})
    assert store.get_setting('planning_maps_trudovoe')['id'] == 'previous'


def test_prefix_without_complete_full_text_is_explicitly_incomplete(workspace, monkeypatch):
    sha = source(workspace)
    catalog = store.get_setting('planning_watch_trudovoe'); catalog['items'][0]['total_pages'] = 507
    store.set_setting('planning_watch_trudovoe', catalog)
    prefix = index(sha); prefix.update(total_pages=507,processed_pages=200,unread_pages=307)
    monkeypatch.setattr(pm, 'inspect_pdf', lambda sha: prefix)
    monkeypatch.setattr(pm, 'render_page', png)
    pm.run('trudovoe', {'planning_id':'p1','survey_id':'s1'})
    report = pm.report('trudovoe')
    assert report['coverage']['unread_pages'] == 307 and report['result']['documents'][0]['coverage'] == 'prefix_only'
    assert report['result']['text_source_id'] is None


def test_full_marker_coverage_and_page_limits_are_verified():
    value = index('a' * 64)
    value.update(algorithm=pm.FULL_ALGORITHM,total_pages=507,processed_pages=507,unread_pages=0,markers_processed_pages=200)
    with pytest.raises(ValueError, match='не охватывает'):
        pm.validate_index(value)
    value['markers_processed_pages'] = 507
    value['standard_geopdf_markers'] = [{'page':508,'keys':['/VP']}]
    with pytest.raises(ValueError, match='GeoPDF'):
        pm.validate_index(value)


def test_real_worker_checks_late_geopdf_fields_and_renders_late_page(workspace):
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, DictionaryObject
    writer = PdfWriter()
    for _ in range(205): writer.add_blank_page(width=100,height=100)
    writer.pages[204][NameObject('/LGIDict')] = DictionaryObject()
    buffer = io.BytesIO(); writer.write(buffer); raw = buffer.getvalue(); sha = hashlib.sha256(raw).hexdigest()
    folder = workspace/'planning_watch'; folder.mkdir(); (folder/(sha+'.pdf')).write_bytes(raw)
    markers = pm.worker('markers',sha)
    assert markers['markers_processed_pages'] == 205 and markers['standard_geopdf_markers'] == [{'page':205,'keys':['/LGIDict']}]
    import shutil
    if shutil.which('pdftoppm'):
        image = pm.worker('render',sha,205)
        assert pm.checked_image(sha,205,image).startswith(b'\x89PNG')


def many_maps(workspace,monkeypatch):
    sha=source(workspace);catalog=store.get_setting('planning_watch_trudovoe')
    original=catalog['items'][0];catalog['items']=[dict(original,id='doc'+str(n),total_pages=12) for n in range(3)]
    store.set_setting('planning_watch_trudovoe',catalog)
    def inspect(sha):
        result=index(sha);result.update(total_pages=12,processed_pages=12)
        result['map_pages']=[dict(result['map_pages'][0],page=n) for n in range(1,13)]
        return result
    monkeypatch.setattr(pm,'inspect_pdf',inspect);monkeypatch.setattr(pm,'render_page',png)
    return sha


def test_more_than_24_total_maps_are_indexed_with_only_three_new_images(workspace,monkeypatch):
    many_maps(workspace,monkeypatch)
    summary=pm.run('trudovoe',{'planning_id':'p1','survey_id':'s1'})
    report=pm.report('trudovoe')
    assert summary['map_pages']==36 and summary['errors']==0 and summary['image_errors']==0
    assert report['image_summary']=={'total':36,'rendered':3,'remaining':33,'errors':0}
    assert not report['stale'] and not report['result']['geometry_confirmed'] and not report['result']['legal_status_confirmed']
    assert all(d['received_at']=='original-date' for d in report['result']['documents'])


def test_render_continues_three_pages_and_preserves_images_sources_and_index_dates(workspace,monkeypatch):
    many_maps(workspace,monkeypatch);pm.run('trudovoe',{'planning_id':'p1','survey_id':'s1'})
    before=copy.deepcopy(pm.report('trudovoe')['result'])
    summary=pm.render('trudovoe',{'id':before['id']});after=pm.report('trudovoe')['result']
    assert summary=={'processed':3,'remaining':30,'network_requests':0}
    assert after['previous_result_id']==before['id'] and after['created_at']==before['created_at']
    assert after['documents'][0]['map_pages'][:3]==before['documents'][0]['map_pages'][:3]
    assert all(a['received_at']==b['received_at'] and a['sha256']==b['sha256'] for a,b in zip(before['documents'],after['documents']))
    assert pm.preview('trudovoe','doc0',4).startswith(b'\x89PNG')
    with pytest.raises(ValueError,match='изменились'):pm.render('trudovoe',{'id':before['id']})


def test_render_failures_remain_visible_without_automatic_retry(workspace,monkeypatch):
    source(workspace);monkeypatch.setattr(pm,'inspect_pdf',lambda sha:index(sha))
    calls=[]
    def fail(*a):calls.append(a);raise ValueError('Poppler unavailable')
    monkeypatch.setattr(pm,'render_page',fail)
    pm.run('trudovoe',{'planning_id':'p1','survey_id':'s1'})
    before=pm.report('trudovoe')
    assert before['image_summary']=={'total':1,'rendered':0,'remaining':0,'errors':1}
    assert pm.render('trudovoe',{'id':before['result']['id']})['processed']==0 and len(calls)==1
    assert pm.render('trudovoe',{'id':before['result']['id'],'retry':True})['processed']==1 and len(calls)==2


def test_new_source_during_render_cannot_overwrite_result(workspace,monkeypatch):
    many_maps(workspace,monkeypatch);pm.run('trudovoe',{'planning_id':'p1','survey_id':'s1'})
    before=store.get_setting('planning_maps_trudovoe')
    def change(sha,page):
        store.set_setting('planning_watch_trudovoe',{'id':'changed','items':[]})
        return png(sha,page)
    monkeypatch.setattr(pm,'render_page',change)
    with pytest.raises(ValueError,match='во время отрисовки'):pm.render('trudovoe',{'id':before['id']})
    assert store.get_setting('planning_maps_trudovoe')==before


def test_concurrent_index_result_is_not_replaced(workspace,monkeypatch):
    sha=source(workspace)
    def inspect(sha):
        store.set_setting('planning_maps_trudovoe',{'id':'other','planning_id':'p1','survey_id':'s1'})
        return index(sha)
    monkeypatch.setattr(pm,'inspect_pdf',inspect);monkeypatch.setattr(pm,'render_page',png)
    with pytest.raises(ValueError,match='Другая версия'):pm.run('trudovoe',{'planning_id':'p1','survey_id':'s1'})
    assert store.get_setting('planning_maps_trudovoe')['id']=='other'


def test_matched_page_is_rendered_before_unmatched_pages():
    data={'documents':[{'id':'a','act_identity':{'date':'2026-01-01'},'map_pages':[
        {'page':1,'parcel_number_matches':[]},
        {'page':2,'parcel_number_matches':[{'parcel_intersects_survey':True}]},
        {'page':3,'parcel_number_matches':[{'parcel_intersects_survey':False}]}]}]}
    assert [page['page'] for _,page in pm.render_queue(data)]==[2,3,1]


def test_previous_index_algorithm_is_stale_even_with_same_sources(workspace):
    source(workspace)
    store.set_setting('planning_maps_trudovoe',{'id':'old','algorithm':pm.ALGORITHM,'planning_id':'p1','survey_id':'s1','documents':[]})
    assert pm.report('trudovoe')['stale']
    with pytest.raises(ValueError,match='изменились'):pm.render('trudovoe',{'id':'old'})


def test_recalculated_gaps_keep_maps_current_but_changed_observations_invalidate(workspace,monkeypatch):
    sha=source(workspace);monkeypatch.setattr(pm,'inspect_pdf',lambda sha:index(sha));monkeypatch.setattr(pm,'render_page',png)
    pm.run('trudovoe',{'planning_id':'p1','survey_id':'s1'})
    observation=store.get_setting('survey_trudovoe');changed=copy.deepcopy(observation)
    changed.update(id='gap-recalculation',gaps={'new-filter-result':True},created_at='new-calculation-date')
    store.set_setting('survey_trudovoe',changed)
    assert not pm.report('trudovoe')['stale'] and pm.preview('trudovoe','doc',1).startswith(b'\x89PNG')
    for key,value in [('bounds',[0,0,2,2]),('layers',{})]:
        store.set_setting('survey_trudovoe',dict(changed,**{key:value}))
        assert pm.report('trudovoe')['stale']
    changed['layers']['parcels']['sha256']='new-observation-hash'
    store.set_setting('survey_trudovoe',changed)
    assert pm.report('trudovoe')['stale']


def test_reindex_does_not_retry_failed_images_of_the_same_pdf(workspace,monkeypatch):
    source(workspace);monkeypatch.setattr(pm,'inspect_pdf',lambda sha:index(sha))
    calls=[]
    def fail(*args):calls.append(args);raise ValueError('Renderer failed')
    monkeypatch.setattr(pm,'render_page',fail)
    pm.run('trudovoe',{'planning_id':'p1','survey_id':'s1'})
    before=pm.report('trudovoe')['result']['documents'][0]['map_pages'][0]
    pm.run('trudovoe',{'planning_id':'p1','survey_id':'s1'})
    after=pm.report('trudovoe')['result']['documents'][0]['map_pages'][0]
    assert len(calls)==1 and after['image_error']==before['image_error'] and after['image_attempt']==before['image_attempt']
