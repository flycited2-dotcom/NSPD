import pytest
import app
from land import store


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()


def get(path):
    handler=object.__new__(app.Handler);handler.path=path;handler.allowed=lambda:True
    sent=[]
    handler.send=lambda value,*args,**kwargs:sent.append(value)
    handler.do_GET()
    assert len(sent)==1
    return sent[0]


@pytest.mark.parametrize('base',['/api/torgi/documents','/api/torgi/active/documents'])
def test_html_reports_are_html_bytes_and_empty_queues_do_not_claim_reading(db,base):
    for kind,title in [('archives','ZIP'),('frames','MPO')]:
        report=get(base+'/'+kind+'/report')
        assert isinstance(report,bytes) and report.startswith(b'<!doctype html>')
        assert title in report.decode('utf-8')
        exported=get(base+'/'+kind+'/export')
        assert exported.get('geometry_confirmed') is not True
    data=get(base)
    assert data['archive_summary']['remaining']==data['archive_summary']['retry_remaining']==0
    assert data['image_frames_summary']['received']==data['image_frames_summary']['remaining']==0
    assert data['image_frames_queue']==data['image_frames_retry_queue']==[]


@pytest.mark.parametrize('kind',['archives','frames'])
def test_selected_lot_reports_reject_unaccepted_or_stale_catalog(db,kind):
    for ending in ('/export','/report'):
        result=get('/api/torgi/active/documents/'+kind+ending+'?lot_id=1234567890_1')
        assert 'error' in result and 'Поиск изменился' in result['error']
