"""Synthetic source pixels and draft rows for source-bound scan operations."""
import copy,hashlib
from land import municipal,ocr,scan_source,store


def source(tmp_path,monkeypatch,count=5):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    raw=b'%PDF synthetic source';digest=hashlib.sha256(raw).hexdigest()
    folder=tmp_path/'municipal';folder.mkdir();(folder/(digest+'.pdf')).write_bytes(raw)
    row=municipal.item({'title':'Synthetic planning scan','url':'https://trudovskoe-rk.ru/synthetic.pdf'},municipal.SOURCES[0]['url'],'listed')
    views=[{'view':i,'width':1000,'height':1400,'image_sha256':hashlib.sha256(b'image').hexdigest()} for i in (0,1)]
    row.update(state='read',sha256=digest,received_at='source-date',processed_pages=2,image_or_sparse_pages=[2])
    row['ocr']={'source_sha256':digest,'pages':[{'page':2,'state':'received','processed_at':'old-ocr-date','views':copy.deepcopy(views)}]}
    row['ocr']['pages'][0]['views'][0]['characters']=100  # Original OCR has extra metadata.
    store.set_setting('municipal_trudovoe',{'id':'catalog','items':[row]})
    folder=ocr.page_folder(digest);folder.mkdir(parents=True)
    for i in (0,1):(folder/f'p2-v{i}.png').write_bytes(b'image')
    rows=[]
    for ri in range(count):
        box=[.1,.15+ri*.025,.71,.16+ri*.025];read=[]
        for vi in (0,1):
            cells=[{'text':text,'bbox':[.1+ci*.24,box[1],.135+ci*.24+(0 if ci==0 else .095),box[3]]}
                   for ci,text in enumerate((f'н{ri+1}','1000000.00','2000000.00'))]
            read.append({'bbox':box,'cells':cells,'cy':(box[1]+box[3])/2})
        rows.append({'ordinal':ri+1,'bbox':box,'views':read,'values':[[f'н{ri+1}',1000000.,2000000.]]*2,
                     'issues':['Synthetic disagreement'],'agreement':'review_required'})
    table={'ordinal':1,'rows':rows}
    page={'document_id':row['id'],'page':2,'source_sha256':digest,'source_views':views,'algorithm':'synthetic-rows-v1',
          'tables':[table],'source_received_at':'source-date','url':row['url'],'title':row['title'],'processed_at':'draft-date'}
    draft={'id':'draft','catalog_id':'catalog','pages':[page]};store.set_setting('scan_tables_trudovoe',draft)
    params={'draft_id':'draft','document_id':row['id'],'page':2,'table':1}
    return draft,page,table,params


def review_params(params):
    return dict(params,visual_checked=True,reviewer='Test visual reviewer',reviewer_kind='assistant',
                label='Synthetic part',axes='xy_m',crs_label='Source conditional coordinates',stated_area='400',
                rows=[['н1','1000000.00','2000000.00'],['н2','1000020.00','2000000.00'],
                      ['н3','1000020.00','2000020.00'],['н4','1000000.00','2000020.00'],['н1','1000000.00','2000000.00']])
