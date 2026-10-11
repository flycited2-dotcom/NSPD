import copy
import hashlib
import pytest
from land import store, torgi_docs as docs, torgi_visual as visual
from land.torgi_large_pdf_worker import ALGORITHM, MAX_BYTES, MAX_PAGES


def file(raw):
    digest=hashlib.sha256(raw).hexdigest()
    return {'key':'source','format':'pdf','state':'read','eligible':True,
            'sha256':digest,'received_at':'source-date','actual_size':len(raw),'declared_size':len(raw),
            'algorithm':docs.ALGORITHM,'total_pages':41,'processed_pages':41,'unread_pages':0,
            'image_or_sparse_pages':[41],'large_pdf_algorithm':ALGORITHM,
            'large_pdf_max_bytes':MAX_BYTES,'large_pdf_max_pages':MAX_PAGES,
            'large_pdf_source_sha256':digest,'large_pdf_received_at':'source-date'}


def test_large_source_requires_successful_exact_receipt_bound_marker(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    raw=b'%PDF-'+b' '*(16*1024*1024)
    item=file(raw)
    path=tmp_path/'torgi_documents'/(item['sha256']+'.pdf');path.parent.mkdir();path.write_bytes(raw)
    assert docs.pdf_scope(item)==(32,120) and visual.source_path(item)==path
    for key,value in [('received_at','other-date'),('large_pdf_source_sha256','0'*64),
                      ('large_pdf_max_bytes',True),('processed_pages',True),('state','oversized')]:
        invalid=copy.deepcopy(item);invalid[key]=value
        assert docs.pdf_scope(invalid)==(16,40)
        with pytest.raises(ValueError):visual.source_path(invalid)
    assert docs.file_limit('pdf')==16*1024*1024


def test_large_pdf_ocr_passes_only_its_source_bound_worker_limits(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    raw=b'%PDF-source';item=file(raw)
    path=tmp_path/'torgi_documents'/(item['sha256']+'.pdf');path.parent.mkdir();path.write_bytes(raw)
    calls=[]
    def worker(args,timeout):
        calls.append((args,timeout))
        raise ValueError('stop before OCR')
    monkeypatch.setattr(visual.ocr,'worker',worker)
    assert visual.queue({'files':{'source':item}})==[(item,41)]
    with pytest.raises(ValueError,match='stop before OCR'):visual.read_unit(item,41)
    args,timeout=calls[0]
    assert args[args.index('--max-pdf-mib')+1]=='32'
    assert args[args.index('--max-pdf-pages')+1]=='120' and timeout==60
