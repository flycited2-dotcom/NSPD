import hashlib
import io
import json
import pytest
from PIL import Image
from land import store, torgi_docs as docs, torgi_visual as visual, image_evidence as images


def raster(fmt='JPEG', size=(80, 50), orientation=None):
    output = io.BytesIO()
    with Image.new('RGB', size, 'white') as im:
        exif = Image.Exif()
        if orientation:
            exif[274] = orientation
            exif[270] = 'private embedded metadata'
        im.save(output, format=fmt, exif=exif)
    return output.getvalue()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    store.set_setting('torgi_trudovoe', {'id':'search','created_at':'date','lots':[]})
    return tmp_path


def seed(db, fmt='jpg', pages=None):
    raw = raster() if fmt=='jpg' else b'%PDF-test'
    digest = hashlib.sha256(raw).hexdigest()
    source = db/'torgi_documents';source.mkdir(exist_ok=True)
    (source/(digest+'.'+fmt)).write_bytes(raw)
    file = {'key':'file','file_name':'Выписка','format':fmt,'state':'read','eligible':True,'received_at':'source-date',
            'sha256':digest,'tables':[],'mentions':[],'geometry_confirmed':False,'text_layer_complete':False,
            'image_or_sparse_pages':pages or [],'processed_pages':2,'total_pages':2}
    if fmt=='jpg':
        png,info=images.preview(raw,fmt)
        (source/(digest+'.preview.png')).write_bytes(png)
        file.update(unit='image',image=dict(info,preview_sha256=hashlib.sha256(png).hexdigest()))
    store.set_setting('torgi_documents_trudovoe',{'id':'docs','search_id':'search','cards':[], 'files':{'file':file}})
    return file


def cache(file, page=1, texts=('90:12:172101:420','90:12:172101:420')):
    target=visual.folder(file);target.mkdir(parents=True,exist_ok=True)
    views=[]
    for v,text in enumerate(texts):
        png=raster('PNG');(target/f'p{page}-v{v}.png').write_bytes(png)
        views.append({'view':v,'text':text,'image_sha256':hashlib.sha256(png).hexdigest(),'width':80,'height':50,
                      'characters':len(text),'line_count':1,'text_angle':None})
    data={'algorithm':visual.ocr.ALGORITHM,'source_sha256':file['sha256'],'source_format':file['format'],
          'page':page,'processed_at':'recognition-date','views':views}
    (target/f'p{page}-{visual.ocr.ALGORITHM}.json').write_text(json.dumps(data),encoding='utf-8')
    return data


def test_preview_complete_frame_orientation_and_no_metadata():
    png,info=images.preview(raster(size=(80,50),orientation=6),'jpg',60)
    assert info['source_width']==80 and info['source_height']==50 and info['complete_frame']
    assert info['orientation_applied'] and info['preview_height']==60 and abs(info['preview_width']-37.5)<=1
    with Image.open(io.BytesIO(png)) as image:
        assert image.format=='PNG' and not image.getexif() and 'exif' not in image.info


def test_visual_processing_tracks_search_source_through_local_revisions(db,monkeypatch):
    store.set_setting('torgi_documents_trudovoe',{'id':'docs','search_id':'search','cards':[],'files':{}})
    search=store.get_setting('torgi_trudovoe');search.update(id='local-revision',search_source_id='search')
    store.set_setting('torgi_trudovoe',search)
    monkeypatch.setattr(visual,'read_unit',lambda *a:pytest.fail('unexpected OCR'))
    assert visual.run('trudovoe',{'id':'docs'})['processed']==0
    catalog=store.get_setting('torgi_documents_trudovoe')
    search.update(id='new-search',search_source_id='new-source');store.set_setting('torgi_trudovoe',search)
    with pytest.raises(ValueError):visual.run('trudovoe',{'id':catalog['id']})
    assert store.get_setting('torgi_documents_trudovoe')==catalog


@pytest.mark.parametrize('raw,fmt',[(b'<html>login</html>','jpg'),(raster('PNG'),'jpg'),(raster()[:100],'jpg'),
                                  (raster(size=(10001,1)),'jpg'),(raster(size=(5100,5100)),'jpg')],
                         ids=['non_image','wrong_format','truncated','side_limit','pixel_limit'])
def test_corrupt_wrong_and_oversized_rasters_are_rejected(raw,fmt):
    with pytest.raises((ValueError,OSError)):
        images.preview(raw,fmt)


def test_animated_png_is_not_treated_as_single_source_frame():
    out=io.BytesIO()
    with Image.new('RGB',(10,10),'white') as first,Image.new('RGB',(10,10),'black') as second:
        first.save(out,format='PNG',save_all=True,append_images=[second],duration=10)
    with pytest.raises(ValueError):images.preview(out.getvalue(),'png')


def test_actual_image_reader_downloads_once_and_never_calls_ocr(db,monkeypatch):
    raw=raster();file={'key':'file','format':'jpg','state':'unsupported','eligible':True,'declared_size':len(raw),
                     'file_name':'Схема.jpg','file_id':'a'*24,'associations':[{'scope':'lot'}]}
    store.set_setting('torgi_documents_trudovoe',{'id':'docs','search_id':'search','cards':[], 'files':{'file':file}})
    calls=[]
    monkeypatch.setattr(docs,'fetch',lambda *a,**k:(calls.append(1) or (raw,'image/jpeg',200)))
    monkeypatch.setattr(docs.time,'sleep',lambda n:None)
    monkeypatch.setattr(visual.ocr,'worker',lambda *a:pytest.fail('Reading image is separate from OCR'))
    assert docs.read('trudovoe',{'id':'docs'})['processed']==1
    r=store.get_setting('torgi_documents_trudovoe');f=r['files']['file']
    assert f['state']=='read' and f['unit']=='image' and f['algorithm']==images.ALGORITHM
    assert not f['mentions'] and not f['tables'] and not f['geometry_confirmed']
    assert visual.preview('trudovoe','file').startswith(b'\x89PNG') and len(visual.queue(r))==1
    docs.read('trudovoe',{'id':r['id']});assert len(calls)==1


def test_ocr_keeps_source_dates_text_geometry_and_lot_fields_separate(db,monkeypatch):
    f=seed(db);cache(f,texts=('90 : 12 : 172101 : 420','90:12:172101:420 and 90:12:172101:999'))
    monkeypatch.setattr(visual.ocr,'worker',lambda *a:{'name':'test','language':'ru'})
    assert visual.run('trudovoe',{'id':'docs'})=={'processed':1,'remaining':0,'network_requests':0}
    r=store.get_setting('torgi_documents_trudovoe');a=r['files']['file']
    assert {k:v for k,v in a.items() if k!='visual_ocr'}==f
    page=a['visual_ocr']['pages'][0]
    assert page['processed_at']=='recognition-date' and page['cached'] and page['unit']=='image'
    assert [m['agreement'] for m in page['mentions']]==['both','one']
    assert all(m['verification_required'] for m in page['mentions'])
    assert store.candidates('trudovoe')==[] and store.get_setting('torgi_visual_attempt_trudovoe')['network_requests']==0
    with pytest.raises(ValueError):visual.run('trudovoe',{'id':'docs'})


def test_sparse_pdf_queue_shares_cache_across_metadata_versions_and_limits_batch(db,monkeypatch):
    f=seed(db,'pdf',[1,2]);cache(f,1);cache(f,2)
    result=store.get_setting('torgi_documents_trudovoe');result['files']['duplicate']=dict(f,key='duplicate')
    store.set_setting('torgi_documents_trudovoe',result)
    monkeypatch.setattr(visual,'BATCH',1)
    monkeypatch.setattr(visual.ocr,'worker',lambda *a:{'name':'test'})
    assert visual.run('trudovoe',{'id':'docs'})['remaining']==3
    result=store.get_setting('torgi_documents_trudovoe')
    assert result['files']['file']['visual_ocr']['pages'][0]['unit']=='pdf_page'
    assert len(result['files']['file']['visual_ocr']['pages'])==1 and 'visual_ocr' not in result['files']['duplicate']
    assert visual.read_unit(result['files']['duplicate'],1)['cached']


def test_retry_errors_explicit_and_corrupt_cache_never_becomes_success(db,monkeypatch):
    f=seed(db);cache(f)
    (visual.folder(f)/'p1-v0.png').write_bytes(b'tampered')
    monkeypatch.setattr(visual.ocr,'worker',lambda *a:{'name':'test'})
    visual.run('trudovoe',{'id':'docs'});r=store.get_setting('torgi_documents_trudovoe')
    assert r['files']['file']['visual_ocr']['pages'][0]['state']=='error'
    assert not visual.queue(r) and len(visual.queue(r,True))==1
    assert r['files']['file']['mentions']==[] and r['files']['file']['state']=='read'


def test_changed_source_stops_and_preserves_prior(db,monkeypatch):
    f=seed(db);cache(f)
    (db/'torgi_documents'/(f['sha256']+'.jpg')).write_bytes(b'changed')
    monkeypatch.setattr(visual.ocr,'worker',lambda *a:{'name':'test'})
    with pytest.raises(ValueError,match='изменился'):visual.run('trudovoe',{'id':'docs'})
    assert store.get_setting('torgi_documents_trudovoe')['id']=='docs'
    with pytest.raises(ValueError):visual.preview('trudovoe','file')


def test_cache_source_identity_language_failure_and_invalid_units(db,monkeypatch):
    f=seed(db);data=cache(f)
    data['source_format']='pdf'
    (visual.folder(f)/f'p1-{visual.ocr.ALGORITHM}.json').write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(ValueError,match='другому'):visual.read_unit(f,1)
    monkeypatch.setattr(visual.ocr,'worker',lambda *a:(_ for _ in ()).throw(ValueError('No Russian OCR')))
    with pytest.raises(ValueError):visual.run('trudovoe',{'id':'docs'})
    assert store.get_setting('torgi_documents_trudovoe')['id']=='docs'
    with pytest.raises(ValueError):visual.run('trudovoe',{'id':'docs','retry_errors':'yes'})
    f=seed(db,'pdf',[41])
    with pytest.raises(ValueError):visual.queue({'files':{'file':f}})


def test_preview_requires_current_catalog_identity_and_both_hashes(db,monkeypatch):
    f=seed(db);cache(f)
    monkeypatch.setattr(visual.ocr,'worker',lambda *a:{'name':'test'})
    visual.run('trudovoe',{'id':'docs'})
    assert visual.preview('trudovoe','file',1,0).startswith(b'\x89PNG')
    for key,page,view in [('unknown',1,0),('file',2,0),('file',1,2),('../../escape',None,None)]:
        with pytest.raises(ValueError):visual.preview('trudovoe',key,page,view)
    (db/'torgi_documents'/(f['sha256']+'.preview.png')).write_bytes(b'changed')
    with pytest.raises(ValueError):visual.preview('trudovoe','file')
