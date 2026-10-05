import copy,pytest
from land import store,scan_review,scan_source,ocr
from scan_fixtures import source,review_params


def test_visual_contour_has_explicit_source_closure_and_never_legal_or_map_approval(tmp_path,monkeypatch):
    draft,page,table,params=source(tmp_path,monkeypatch)
    originals={k:{'sentinel':k} for k in ('schemes_trudovoe','survey_trudovoe','georeference_trudovoe','torgi_trudovoe')}
    for k,v in originals.items():store.set_setting(k,v)
    catalog=store.get_setting('municipal_trudovoe')
    result=scan_review.run('trudovoe',review_params(params))
    assert result=={'state':'local_preview','table':1,'points':5}
    r=store.get_setting('scan_reviews_trudovoe')['tables'][0]
    assert r['closure']=='explicit' and r['local_area_m2']==400 and len(r['outline_columns'])==5
    assert r['reviewer_kind']=='assistant' and r['fingerprint']==scan_source.fingerprint(page)
    assert r['points'][0]['source_bbox']==table['rows'][0]['bbox'] and r['source_received_at']=='source-date'
    assert not any(r[k] for k in ('geometry_confirmed','georeferenced','crs_parameters_confirmed','cadastral_identity_confirmed','completeness_confirmed'))
    assert store.get_setting('scan_tables_trudovoe')==draft and store.get_setting('municipal_trudovoe')==catalog
    assert all(store.get_setting(k)==v for k,v in originals.items()) and not store.candidates('trudovoe')


@pytest.mark.parametrize('change',['confirm','reviewer','kind','axes','row_count','number','point','numeric_type'])
def test_invalid_input_cannot_overwrite_saved_review(tmp_path,monkeypatch,change):
    _,_,table,p=source(tmp_path,monkeypatch);p=review_params(p)
    scan_review.run('trudovoe',p);old=store.get_setting('scan_reviews_trudovoe')
    if change=='confirm':p['visual_checked']=False
    elif change=='reviewer':p['reviewer']=' '
    elif change=='kind':p['reviewer_kind']='OCR'
    elif change=='axes':p['axes']='EPSG:4326'
    elif change=='row_count':p['rows'].pop()
    elif change=='number':p['rows'][0][1]='10000O0.00'
    elif change=='point':p['rows'][0][0]='H1'
    else:p['rows'][0][1]=1000000.
    with pytest.raises(ValueError):scan_review.run('trudovoe',p)
    assert store.get_setting('scan_reviews_trudovoe')==old


@pytest.mark.parametrize('change,reason',[('open','замыкания'),('labels','Повтор обозначения'),('gap','последовательность'),('duplicate','Повтор координат'),('cross','Self-intersection')])
def test_bad_contour_is_saved_as_rejected_without_repairs(tmp_path,monkeypatch,change,reason):
    _,_,t,p=source(tmp_path,monkeypatch);p=review_params(p)
    if change=='open':p['rows'][-1][2]='2000001.00'
    elif change=='labels':p['rows'][2][0]='н2'
    elif change=='gap':p['rows'][2][0]='н8'
    elif change=='duplicate':p['rows'][2][1:]=p['rows'][1][1:]
    else:p['rows'][1][1:],p['rows'][2][1:]=p['rows'][2][1:],p['rows'][1][1:]
    r=scan_review.validate(p,t)
    assert r['state']=='rejected' and r['outline_columns'] is None and r['local_area_m2'] is None
    assert reason in ';'.join(r['issues']) and [x['transcribed_cells'] for x in r['points']]==p['rows']


def test_unconfirmed_axes_keep_preview_but_no_square_meters_and_precision_warning_is_only_hint(tmp_path,monkeypatch):
    _,_,t,p=source(tmp_path,monkeypatch);p=review_params(p);p['axes']='unconfirmed'
    r=scan_review.validate(p,t)
    assert r['state']=='local_preview' and r['outline_columns'] and r['local_area_m2'] is None
    p['axes']='xy_m';p['stated_area']='399'
    r=scan_review.validate(p,t)
    assert r['area_difference_m2']==1 and r['area_rounding_tolerance_m2']==.5 and r['stated_area_disagrees']
    assert r['state']=='local_preview' and not r['geometry_confirmed']


def test_corrections_preserve_history_and_cached_reassembly_does_not_stale(tmp_path,monkeypatch):
    d,page,t,p=source(tmp_path,monkeypatch);p=review_params(p);scan_review.run('trudovoe',p)
    p['label']='Corrected caption';scan_review.run('trudovoe',p)
    saved=store.get_setting('scan_reviews_trudovoe');r=saved['tables'][0]
    assert len(r['history'])==1 and r['history'][0]['label']=='Synthetic part'
    page.update(processed_at='later-cache-date',cached=True,word_ocr_at='original-word-date');d['id']='reassembled'
    choices=[{'document_id':page['document_id'],'source_sha256':page['source_sha256'],'pages':[2]}]
    shown=scan_review.present(saved,d,choices,'catalog')['tables'][0]
    assert not shown['stale'] and shown['local_area_m2']==400
    page['tables'][0]['rows'][0]['views'][0]['cells'][1]['text']='1000001.00'
    shown=scan_review.present(saved,d,choices,'catalog')['tables'][0]
    assert shown['stale'] and shown['outline_columns'] is None and shown['local_area_m2'] is None
    assert saved['tables'][0]['outline_columns'] and store.get_setting('scan_reviews_trudovoe')==saved


@pytest.mark.parametrize('change',['catalog','page','sha'])
def test_review_hides_if_catalog_source_or_processed_page_changes(tmp_path,monkeypatch,change):
    d,page,_,p=source(tmp_path,monkeypatch);scan_review.run('trudovoe',review_params(p))
    choices=[{'document_id':page['document_id'],'source_sha256':page['source_sha256'],'pages':[2]}];catalog='catalog'
    if change=='catalog':catalog='new-catalog'
    elif change=='page':choices[0]['pages']=[1]
    else:choices[0]['source_sha256']='0'*64
    shown=scan_review.present(store.get_setting('scan_reviews_trudovoe'),d,choices,catalog)['tables'][0]
    assert shown['stale'] and shown['outline_columns'] is None


@pytest.mark.parametrize('change',['pdf','png','draft','catalog','source_view'])
def test_source_guard_rejects_even_saved_source_substitution(tmp_path,monkeypatch,change):
    d,page,_,p=source(tmp_path,monkeypatch);scan_review.run('trudovoe',review_params(p));old=store.get_setting('scan_reviews_trudovoe')
    if change=='pdf':(tmp_path/'municipal'/(page['source_sha256']+'.pdf')).write_bytes(b'changed')
    elif change=='png':(ocr.page_folder(page['source_sha256'])/'p2-v1.png').write_bytes(b'changed')
    elif change=='draft':p['draft_id']='old'
    else:
        c=store.get_setting('municipal_trudovoe')
        if change=='catalog':c['id']='changed'
        else:c['items'][0]['ocr']['pages'][0]['views'][1]['width']=900
        store.set_setting('municipal_trudovoe',c)
    with pytest.raises(ValueError):scan_review.run('trudovoe',review_params(p))
    assert store.get_setting('scan_reviews_trudovoe')==old
