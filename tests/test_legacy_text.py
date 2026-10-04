import copy
import hashlib
import io
import struct
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from land import store, torgi_docs as docs, torgi_visual as visual, municipal
from land.legacy_text import doc_text, rtf_text, doc_stream_text, OLE_MAGIC, ALGORITHM
from land.torgi_file_worker import extract


def word_streams():
    # Logical order differs from byte order. The last story is outside the main body.
    pieces = [
        ('Участок 90:12:172101:',False,2048),
        ('420\r',True,3500),
        ('\x13HYPERLINK "90:12:172101:999"\x14Ссылка\x15\r',False,2500),
        ('“Text”\r',True,3400),
        ('Колонтитул 90:12:172101:888\r\x00',False,3100),
    ]
    body = sum(len(x[0]) for x in pieces[:-1])
    extra = len(pieces[-1][0])-1
    word = bytearray(4096)
    struct.pack_into('<HH',word,0,0xa5ec,0xc1)
    struct.pack_into('<H',word,10,0x1200)
    struct.pack_into('<H',word,32,14)
    struct.pack_into('<H',word,62,22)
    struct.pack_into('<I',word,64,4096)
    struct.pack_into('<ii',word,76,body,0)
    struct.pack_into('<i',word,84,extra)
    struct.pack_into('<H',word,152,93)
    struct.pack_into('<H',word,898,0)
    cps = [0]; descriptors = []
    for text, compressed, start in pieces:
        raw = text.encode('cp1252' if compressed else 'utf-16-le')
        word[start:start+len(raw)] = raw
        cps.append(cps[-1]+len(text))
        descriptors.append(struct.pack('<HIH',0,(start*2|0x40000000) if compressed else start,0))
    plc = struct.pack('<'+'I'*len(cps),*cps)+b''.join(descriptors)
    clx = b'\x01\x02\x00\x00\x00\x02'+struct.pack('<I',len(plc))+plc
    struct.pack_into('<II',word,154+33*8,0,len(clx))
    return bytes(word),clx.ljust(4096,b'\0')


def ole_doc(word=None,table=None,table_name='1Table',macro=False):
    original_word,original_table = word_streams()
    word = word if word is not None else original_word
    table = table if table is not None else original_table
    assert len(word)==len(table)==4096
    free,end,fat_id = 0xffffffff,0xfffffffe,0xfffffffd
    header = bytearray(512);header[:8]=OLE_MAGIC
    struct.pack_into('<HHHH',header,24,0x3e,3,0xfffe,9)
    struct.pack_into('<H',header,32,6)
    struct.pack_into('<IIIIIIIII',header,40,0,1,1,0,4096,end,0,end,0)
    struct.pack_into('<109I',header,76,0,*([free]*108))
    fat = [free]*128;fat[0]=fat_id;fat[1]=end
    for start in (2,10):
        for i in range(start,start+7):fat[i]=i+1
        fat[start+7]=end
    def entry(name,kind,child=free,right=free,start=end,size=0):
        value=bytearray(128);encoded=(name+'\0').encode('utf-16-le');value[:len(encoded)]=encoded
        struct.pack_into('<HBBIII',value,64,len(encoded),kind,1,free,right,child)
        struct.pack_into('<IQ',value,116,start,size)
        return bytes(value)
    directory = entry('Root Entry',5,child=1)+entry('WordDocument',2,right=2,start=2,size=4096)
    directory += entry(table_name,2,right=3 if macro else free,start=10,size=4096)
    directory += entry('VBA',1) if macro else bytes(128)
    return bytes(header)+struct.pack('<128I',*fat)+directory+word+table


def rtf(content):
    return (r'{\rtf1\ansi\ansicpg1251 '+content+'}').encode('ascii')


def test_doc_piece_order_unicode_compression_and_main_story_only():
    text,info=doc_text(ole_doc())
    assert text=='Участок 90:12:172101:420\nСсылка\n“Text”\n'
    assert info['piece_count']==5 and info['compressed_body_pieces']==2
    assert info['other_story_characters']>0 and info['field_instruction_characters_skipped']>0
    data,units=extract(ole_doc(),'doc')
    assert [m['cadastral_number'] for m in data['mentions']]==['90:12:172101:420']
    assert data['unit']=='doc_body' and not data['text_layer_complete'] and not data['geometry_confirmed']
    assert data['tables']==[] and data['mentions'][0]['sections']==[1] and units==[(1,text)]


def test_doc_selected_table_and_container_guards():
    word,table=word_streams();word=bytearray(word)
    struct.pack_into('<H',word,10,0x1000)
    assert doc_text(ole_doc(bytes(word),table,'0Table'))[1]['table_stream']=='0Table'
    with pytest.raises(ValueError):doc_text(ole_doc(bytes(word),table,'1Table'))
    with pytest.raises(ValueError,match='макрос'):doc_text(ole_doc(macro=True))
    struct.pack_into('<H',word,10,0x1100)
    with pytest.raises(ValueError,match='Зашифрован'):doc_text(ole_doc(bytes(word),table,'0Table'))
    with pytest.raises((ValueError,OSError)):doc_text(ole_doc()[:900])
    with pytest.raises(ValueError):doc_text(b'<html>90:12:172101:420</html>')


@pytest.mark.parametrize('problem',['version','range','offset','clx_size','invalid_unicode','oversized_body','field'])
def test_doc_damaged_pieces_never_publish_partial_numbers(problem):
    word,table=map(bytearray,word_streams())
    if problem=='version':struct.pack_into('<H',word,2,0xb0)
    if problem=='range':struct.pack_into('<I',table,10+4,0)
    if problem=='offset':struct.pack_into('<I',table,10+6*4+2,50000)
    if problem=='clx_size':struct.pack_into('<I',word,154+33*8+4,999999)
    if problem=='invalid_unicode':struct.pack_into('<H',word,2048,0xd800)
    if problem=='oversized_body':struct.pack_into('<i',word,76,500001)
    if problem=='field':struct.pack_into('<H',word,2500,0x15)
    with pytest.raises((ValueError,UnicodeError)):doc_stream_text(bytes(word),bytes(table))


def test_rtf_cyrillic_unicode_tables_and_ignored_destinations():
    source=rtf(r"{\fonttbl{\f0\fcharset204 Arial;}}\f0 \'cf\'f0\'e8\'e2\'e5\'f2 \u1046?\par "
               r"{\*\unknown 90:12:172101:999}{\header 90:12:172101:888}{\pict 001122}"
               r"{\field{\*\fldinst HYPERLINK \"90:12:172101:777\"}{\fldrslt Ссылка}}"
               r"90:12:172101:420\cell 2\row".replace('Ссылка','Link'))
    text,info=rtf_text(source)
    assert 'Привет Ж\n' in text and '90:12:172101:420|2\n' in text
    assert not any(n in text for n in (':999',':888',':777'))
    assert info['encoding']=='cp1251'
    data,_=extract(source,'rtf')
    assert [m['cadastral_number'] for m in data['mentions']]==['90:12:172101:420']
    assert data['unit']=='rtf_body' and not data['geometry_confirmed'] and not data['text_layer_complete']


def test_rtf_word_math_and_table_controls_keep_destination_scope():
    source=rtf(r'{\mmathPr\mmathFont34\mbrkBin0\mbrkBinSub0\msmallFrac0\mdispDef1'
               r'\mlMargin0\mrMargin0\mdefJc1\mwrapIndent1440\mintLim0\mnaryLim1 90:12:172101:999}'
               r'\trftsWidthB3 90:12:172101:420\par')
    text,_=rtf_text(source)
    assert ':420' in text and ':999' not in text
    # Case is significant: neither unsupported names nor differently cased
    # binary/image destinations may silently expose their contents as evidence.
    for word in ('Unknown','Bin','Pict','MmathPr'):
        with pytest.raises(ValueError,match='Регистр'):rtf_text(rtf('\\'+word+' 90:12:172101:999'))


@pytest.mark.parametrize('source',[b'{\\rtf1 incomplete',b'{\\rtf1 a}}',b'{\\rtf1 a}extra',
    rtf(r'\bin10 abc'),rtf(r"\'zz"),rtf(r'\uc999 x'),rtf(r'\u999999 x'),rtf(r'\u-10240?'),
    rtf(r'\ansicpg999999 x'),rtf('{'*129+'x'+'}'*129),b'<html>fake</html>'])
def test_invalid_rtf_never_publishes_partial_evidence(source):
    with pytest.raises((ValueError,LookupError,UnicodeError)):rtf_text(source)


def big_pdf():
    writer=PdfWriter();writer.add_blank_page(100,100)
    stream=DecodedStreamObject();stream.set_data(b'x'*(9*1024*1024))
    writer._root_object[NameObject('/UnusedTestPayload')]=writer._add_object(stream)
    result=io.BytesIO();writer.write(result);return result.getvalue()


def test_extended_pdf_limit_is_only_for_torgi_and_keeps_page_scope():
    raw=big_pdf()
    with pytest.raises(ValueError,match='8 МБ'):municipal.pdf_text(raw)
    evidence,_=extract(raw,'pdf')
    assert evidence['processed_pages']==1 and evidence['total_pages']==1
    assert evidence['image_or_sparse_pages']==[1] and not evidence['text_layer_complete']
    with pytest.raises(ValueError):municipal.pdf_text(raw,max_bytes=32*1024*1024)


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    monkeypatch.setattr(docs.time,'sleep',lambda *a:None)
    store.set_setting('torgi_trudovoe',{'id':'search'})
    return tmp_path


def seed(raw,fmt='doc',state='unsupported',cached=False):
    key='file';digest=hashlib.sha256(raw).hexdigest()
    file={'key':key,'file_id':'a'*24,'file_name':'Извещение.'+fmt,'format':fmt,
          'declared_size':len(raw),'eligible':True,'state':state,'associations':[], 'geometry_confirmed':False}
    if cached:
        folder=store.DATA/'torgi_documents';folder.mkdir(exist_ok=True)
        (folder/(digest+'.'+fmt)).write_bytes(raw)
        file.update(sha256=digest,received_at='original-download',content_type='application/msword',http_status=200,actual_size=len(raw))
    result={'id':'catalog','search_id':'search','cards':[],'files':{key:file}}
    store.set_setting('torgi_documents_trudovoe',result)
    return file


def test_real_child_read_reuses_verified_source_dates_and_never_creates_geometry(db,monkeypatch):
    original=copy.deepcopy(seed(ole_doc(),cached=True))
    monkeypatch.setattr(docs,'fetch',lambda *a,**k:pytest.fail('Source was already downloaded'))
    result=docs.read('trudovoe',{'id':'catalog'})
    file=store.get_setting('torgi_documents_trudovoe')['files']['file']
    assert result['network_requests']==0 and result['cached_files']==1 and result['remaining']==0
    assert file['state']=='read' and file['algorithm']==ALGORITHM
    for field in ('received_at','sha256','associations'):assert file[field]==original[field]
    assert store.candidates('trudovoe')==[] and not file['geometry_confirmed'] and not file['georeferenced']
    assert len(file['mentions'])==1


def test_changed_cached_source_stops_without_redownload_or_partial_text(db,monkeypatch):
    file=seed(ole_doc(),cached=True)
    (db/'torgi_documents'/(file['sha256']+'.doc')).write_bytes(b'changed')
    monkeypatch.setattr(docs,'fetch',lambda *a,**k:pytest.fail('Do not replace source'))
    assert docs.read('trudovoe',{'id':'catalog'})['state']=='partial'
    result=store.get_setting('torgi_documents_trudovoe')['files']['file']
    assert result['state']=='error' and result['sha256']==file['sha256'] and 'mentions' not in result


def test_rtf_reprocess_uses_saved_source_and_preserves_receipt(db,monkeypatch):
    file=seed(rtf(r'{\mmathPr\mbrkBin0 hidden}90:12:172101:420'),'rtf','rejected',True)
    file['error']='Регистр управляющего слова RTF не поддержан'
    store.set_setting('torgi_documents_trudovoe',{'id':'catalog','search_id':'search','cards':[],'files':{'file':file}})
    monkeypatch.setattr(docs,'fetch',lambda *a,**k:pytest.fail('Local reprocess must not fetch'))
    result=docs.reprocess('trudovoe',{'id':'catalog'})
    updated=store.get_setting('torgi_documents_trudovoe')['files']['file']
    assert result=={'processed':1,'remaining':0,'network_requests':0}
    for field in ('received_at','sha256','actual_size','associations'):assert updated[field]==file[field]
    assert updated['state']=='read' and updated['reprocess_algorithm']==ALGORITHM
    assert len(updated['mentions'])==1 and not updated['geometry_confirmed']
    assert store.candidates('trudovoe')==[]


def test_failed_rtf_reprocess_is_bounded_and_other_rejections_stay_out(db,monkeypatch):
    file=seed(rtf(r'\Unknown text'),'rtf','rejected',True)
    file['error']='Регистр управляющего слова RTF не поддержан'
    result={'id':'catalog','search_id':'search','cards':[],'files':{'file':file}}
    for changed in ({'eligible':False},{'error':'Неполный RTF'},{'format':'doc'}):
        candidate={**file,**changed}
        assert docs.reprocess_queue({'files':{'file':candidate}})==[]
    store.set_setting('torgi_documents_trudovoe',result)
    monkeypatch.setattr(docs,'fetch',lambda *a,**k:pytest.fail('Do not redownload rejected source'))
    assert docs.reprocess('trudovoe',{'id':'catalog'})['remaining']==0
    updated=store.get_setting('torgi_documents_trudovoe')['files']['file']
    assert updated['state']=='rejected' and updated['reprocess_algorithm']==ALGORITHM
    assert updated['received_at']==file['received_at'] and 'mentions' not in updated


def test_old_states_queue_formats_and_pdf_visual_worker_size(db,monkeypatch):
    seed(b'example','doc')
    assert len(docs.file_queue(store.get_setting('torgi_documents_trudovoe')))==1
    for fmt,size,eligible in [('rtf',600,True),('pdf',12*1024*1024,True),('pdf',17*1024*1024,False),('doc',9*1024*1024,False)]:
        f={'file_name':'file.'+fmt,'format':fmt,'declared_size':size,'eligible':True,'state':'oversized','associations':[]}
        assert bool(docs.file_queue({'files':{'file':f}}))==eligible
    raw=big_pdf();file=seed(raw,'pdf','read',True)
    file.update(processed_pages=1,image_or_sparse_pages=[1])
    assert visual.source_path(file).stat().st_size>8*1024*1024
    calls=[]
    def worker(args,*unused):
        calls.append(args);raise ValueError('Test stops before recognition')
    monkeypatch.setattr(visual.ocr,'worker',worker)
    with pytest.raises(ValueError):visual.read_unit(file,1)
    assert calls[0][calls[0].index('--max-pdf-mib')+1]=='16'
