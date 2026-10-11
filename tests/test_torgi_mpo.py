import asyncio
import copy
import hashlib
import json

import pytest

from land import store, torgi_docs as docs, torgi_visual as visual, image_evidence as images, ocr_worker
from test_mpo_evidence import raster
from test_torgi_visual import db, cache


def seed_mpo(db, rejected=False):
    raw=raster()
    digest=hashlib.sha256(raw).hexdigest()
    folder=db/'torgi_documents';folder.mkdir(exist_ok=True)
    (folder/(digest+'.jpg')).write_bytes(raw)
    descriptor=docs.descriptor({'fileId':'a'*24,'fileName':'Фото.jpg','fileSize':len(raw)},'lot')
    file={**descriptor,'sha256':digest,'received_at':'original-source-date','eligible':True,
          'state':'rejected' if rejected else 'read','tables':[],'egrn_tables':[],
          'associations':[{'lot_id':'22000000000000000000_1','scope':'lot','inactive':False}],
          'geometry_confirmed':False}
    if rejected:
        file['error']='Формат, число кадров или размер изображения превышает лимит'
    else:
        png,info=images.preview(raw,'jpg')
        (folder/(digest+'.preview.png')).write_bytes(png)
        file.update(unit='image',image={**info,'preview_sha256':hashlib.sha256(png).hexdigest()},mentions=[])
    store.set_setting('torgi_documents_trudovoe',{'id':'docs','search_id':'search','cards':[],'files':{file['key']:file}})
    return file


def test_reprocess_exact_cached_mpo_without_fetch_or_source_date_changes(db,monkeypatch):
    before=seed_mpo(db,True)
    monkeypatch.setattr(docs,'fetch',lambda *a,**k:pytest.fail('MPO processing must be local'))
    assert len(docs.reprocess_queue({'files':{before['key']:before}}))==1
    answer=docs.reprocess('trudovoe',{'id':'docs'})
    after=store.get_setting('torgi_documents_trudovoe')['files'][before['key']]
    assert answer['processed']==1 and answer['remaining']==0 and answer['network_requests']==0
    for key in ('sha256','received_at','declared_size','associations'):
        assert after[key]==before[key]
    assert after['state']=='read' and after['algorithm']==images.ALGORITHM
    assert not after['geometry_confirmed'] and not after['georeferenced'] and not after['text_layer_complete']
    assert after['image']['source_frame_count']==2 and after['image']['unprocessed_frame_count']==1
    assert not after['image']['complete_source']
    assert not docs.reprocess_queue({'files':{after['key']:after}})
    assert visual.preview('trudovoe',after['key']).startswith(b'\x89PNG')


def test_failed_mpo_reprocess_is_not_automatically_repeated(db,monkeypatch):
    before=seed_mpo(db,True)
    (db/'torgi_documents'/(before['sha256']+'.jpg')).write_bytes(b'changed')
    answer=docs.reprocess('trudovoe',{'id':'docs'})
    after=store.get_setting('torgi_documents_trudovoe')['files'][before['key']]
    assert answer['processed']==1 and after['state']=='rejected'
    assert after['sha256']==before['sha256'] and after['received_at']==before['received_at']
    assert not docs.reprocess_queue({'files':{after['key']:after}})


def test_mpo_ocr_cache_requires_exact_frame_scope_and_preview_rejects_other_frame(db,monkeypatch):
    file=seed_mpo(db)
    data=cache(file)
    with pytest.raises(ValueError,match='кадр'):
        visual.read_unit(file,1)
    data['image_scope']=images.scope(file['image'])
    target=visual.folder(file)/f'p1-{visual.ocr.ALGORITHM}.json'
    target.write_text(json.dumps(data),encoding='utf-8')
    observation=visual.read_unit(file,1)
    assert observation['cached'] and observation['image_scope']['preview_frame_index']==0
    monkeypatch.setattr(visual.ocr,'worker',lambda *a,**k:{'name':'test-engine'})
    outcome=visual.run('trudovoe',{'id':'docs'})
    assert outcome['received']==1 and outcome['errors']==0 and outcome['state']=='done'
    catalog=store.get_setting('torgi_documents_trudovoe')
    assert not visual.queue(catalog)
    current=catalog['files'][file['key']]
    assert visual.preview('trudovoe',file['key'],1,0).startswith(b'\x89PNG')
    current['visual_ocr']['pages'][0]['image_scope']['preview_frame_index']=1
    store.set_setting('torgi_documents_trudovoe',catalog)
    assert len(visual.queue(catalog))==1
    with pytest.raises(ValueError,match='кадр'):
        visual.preview('trudovoe',file['key'],1,0)


def test_mpo_ocr_error_has_scope_and_needs_explicit_retry(db,monkeypatch):
    file=seed_mpo(db)
    data=cache(file)
    monkeypatch.setattr(visual.ocr,'worker',lambda *a,**k:{'name':'test-engine'})
    outcome=visual.run('trudovoe',{'id':'docs'})
    catalog=store.get_setting('torgi_documents_trudovoe')
    item=catalog['files'][file['key']]['visual_ocr']['pages'][0]
    assert outcome['state']=='partial' and outcome['errors']==1 and outcome['received']==0
    assert item['image_scope']==images.scope(file['image'])
    assert not visual.queue(catalog) and len(visual.queue(catalog,True))==1


@pytest.mark.parametrize('change',[{'preview_frame_index':False},{'processed_frame_count':True},
                                   {'complete_frame':1},{'source_frame_count':2.0}])
def test_mpo_scope_rejects_equal_values_with_wrong_types(db,change):
    file=seed_mpo(db)
    data=cache(file)
    data['image_scope']={**images.scope(file['image']),**change}
    target=visual.folder(file)/f'p1-{visual.ocr.ALGORITHM}.json'
    target.write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(ValueError,match='кадр'):
        visual.read_unit(file,1)


def test_real_worker_propagates_primary_scope_without_using_windows_ocr(db,monkeypatch):
    file=seed_mpo(db)
    async def recognize(engine,path):
        return {'text':'90:12:172101:420','characters':17,'line_count':1,'text_angle':None,'width':80,'height':50}
    monkeypatch.setattr(ocr_worker,'engine',lambda:('test',{'max_image_dimension':10000}))
    monkeypatch.setattr(ocr_worker,'recognize',recognize)
    target=visual.folder(file);target.mkdir(parents=True)
    result=asyncio.run(ocr_worker.process_image(db/'torgi_documents'/(file['sha256']+'.jpg'),file['sha256'],'jpg',target))
    assert result['source_sha256']==file['sha256'] and result['image_scope']==images.scope(file['image'])
    assert len(result['views'])==2 and result['page']==1
    assert not result['image_scope']['complete_source']
