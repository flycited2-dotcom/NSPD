import copy,json,subprocess,asyncio
from types import SimpleNamespace
import pytest
from land import store,scan_cells,scan_cell_worker,scan_source
from scan_fixtures import source


def worker(monkeypatch,mutation=None):
    calls=[]
    def run(args,**kw):
        assert kw['timeout']==60
        request=json.loads(open(args[-2],encoding='utf-8').read());calls.append(request)
        result=dict(request,algorithm=scan_cells.ALGORITHM,processed_at='cell-ocr-date')
        result['rows']=copy.deepcopy(request['rows'])
        for row in result['rows']:
            row['views']=[{'view':i,'cells':[{'text':text,'bbox':b} for text,b in zip(('н1','1000000.00','2000000.00'),row['bands'][i])]} for i in (0,1)]
        if mutation:mutation(result)
        return SimpleNamespace(returncode=0,stdout=json.dumps(result))
    monkeypatch.setattr(scan_cells.subprocess,'run',run);return calls


def test_bounded_batches_continue_without_duplicate_ocr_or_source_changes(tmp_path,monkeypatch):
    d,page,_,p=source(tmp_path,monkeypatch,8);catalog=store.get_setting('municipal_trudovoe');calls=worker(monkeypatch)
    assert scan_cells.run('trudovoe',p)=={'processed':6,'remaining':2}
    assert scan_cells.run('trudovoe',p)=={'processed':2,'remaining':0}
    assert scan_cells.run('trudovoe',p)=={'processed':0,'remaining':0}
    saved=store.get_setting('scan_cells_trudovoe')['rows']
    assert len(calls)==2 and len(saved)==len({r['key'] for r in saved})==8
    assert all(r['processed_at']=='cell-ocr-date' and r['fingerprint']==scan_source.fingerprint(page) and not r['geometry_confirmed'] for r in saved)
    assert store.get_setting('scan_tables_trudovoe')==d and store.get_setting('municipal_trudovoe')==catalog
    assert not store.candidates('trudovoe') and store.get_setting('scan_reviews_trudovoe') is None
    assert store.get_setting('scan_cells_attempt_trudovoe')['network_requests']==0


@pytest.mark.parametrize('change',['page','hash','box','views','text','time','date'])
def test_worker_failure_keeps_prior_result(tmp_path,monkeypatch,change):
    _,_,_,p=source(tmp_path,monkeypatch);old={'rows':[],'id':'prior'};store.set_setting('scan_cells_trudovoe',old)
    def mutate(r):
        if change=='page':r['page']=1
        elif change=='hash':r['source_sha256']='0'*64
        elif change=='box':r['rows'][0]['views'][0]['cells'][0]['bbox'][0]=.01
        elif change=='views':r['rows'][0]['views'].pop()
        elif change=='text':r['rows'][0]['views'][0]['cells'][0]['text']='x'*501
        elif change=='date':r['processed_at']=None
    worker(monkeypatch,mutate)
    if change=='time':
        def timeout(*a,**kw):raise subprocess.TimeoutExpired('cells',60)
        monkeypatch.setattr(scan_cells.subprocess,'run',timeout)
    with pytest.raises(ValueError):scan_cells.run('trudovoe',p)
    assert store.get_setting('scan_cells_trudovoe')==old
    assert store.get_setting('scan_cells_attempt_trudovoe')['state']=='error'


def test_cell_ocr_retains_letters_missing_or_conflicting_values(tmp_path,monkeypatch):
    _,_,t,_=source(tmp_path,monkeypatch);views=[]
    for i in (0,1):views.append({'view':i,'cells':[{'text':x,'bbox':b} for x,b in zip(('H1','100000O.00','2000000.00'),scan_cells.bands(t,t['rows'][0],i))]})
    views[1]['cells'][0]['text']='н1';views[1]['cells'][1]['text']='1000000.00';views[1]['cells'][2]['text']='2000001.00'
    r=scan_cells.summarize(views)
    assert r['values'][0]==[None,None,2000000.] and r['views'][0]['cells'][0]['text']=='H1'
    assert len(r['issues'])==3 and r['verification_required'] and not r['geometry_confirmed']


def test_overlapping_columns_cannot_be_cropped_as_valid_cells(tmp_path,monkeypatch):
    _,_,t,_=source(tmp_path,monkeypatch)
    for r in t['rows']:r['views'][0]['cells'][1]['bbox'][0]=.12;r['views'][0]['cells'][1]['bbox'][2]=.22
    with pytest.raises(ValueError,match='пересекаются'):scan_cells.bands(t,t['rows'][0],0)


@pytest.mark.parametrize('change',['page','key','count','bands','view'])
def test_worker_rejects_bad_limits_before_loading_ocr(tmp_path,monkeypatch,change):
    _,page,_,_=source(tmp_path,monkeypatch)
    req={k:page[k] for k in ('source_sha256','page','source_views')};req['rows']=scan_cells.requests(page)[:1]
    if change=='page':req['page']=True
    elif change=='key':req['rows'][0]['key']='../../file'
    elif change=='count':req['rows']*=7
    elif change=='bands':req['rows'][0]['bands'][0].pop()
    else:req['source_views'][0]['view']=1
    monkeypatch.setattr(scan_cell_worker,'engine',lambda:pytest.fail('bad request must fail before OCR'))
    with pytest.raises(ValueError):asyncio.run(scan_cell_worker.extract(tmp_path,req))
