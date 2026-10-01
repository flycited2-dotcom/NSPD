"""Text evidence from bounded PDF / DOCX bodies, never executable Word content."""
import hashlib
import json
from pathlib import Path
import sys
from . import municipal, schemes, egrn_coordinates
from .docx_text import docx_text, read_docx, ALGORITHM as DOCX_ALGORITHM
from . import image_evidence


def extract(raw,fmt):
    if fmt in image_evidence.FORMATS:
        png,info=image_evidence.preview(raw,fmt)
        return {'unit':'image','image':dict(info,preview_sha256=hashlib.sha256(png).hexdigest()),
                'mentions':[],'tables':[],'geometry_confirmed':False,'georeferenced':False,
                'text_layer_complete':False},[]
    if fmt=='pdf':
        details,pages=municipal.pdf_text(raw)
        result=schemes.apply_page_limit(schemes.extract(pages),details['processed_pages'],details['unread_pages'])
        egrn, consumed = egrn_coordinates.extract(pages)
        unparsed = [n for n,text in pages if any(schemes.PAIR.search(line) and (n,line.strip()) not in consumed
                    for line in text.splitlines()) and n in result['unparsed_coordinate_pages']]
        return {**details,'tables':result['tables'],
                'unreadable_tables':result['unreadable_tables'],'crs_status':result['crs_status'],
                'egrn_tables':egrn,'unparsed_coordinate_pages':unparsed,
                'unit':'pdf_page','georeferenced':False},pages
    if fmt!='docx':raise ValueError('Формат не поддержан')
    text,count,details=read_docx(raw)
    # DOCX pagination cannot be recovered from body XML. Never call this a PDF page.
    evidence=municipal.evidence([(1,text)])
    for mention in evidence['mentions']:
        mention['sections']=mention.pop('pages')
    return {**evidence,**details,'unit':'docx_body','paragraph_count':count,'tables':[], 'text_layer_complete':False,
            'scope_note':'Прочитан основной XML DOCX; страницы, колонтитулы, изображения и отношения номера к лоту не установлены.'},[(1,text)]


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        path,digest,fmt=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('SHA-256 файла изменился')
        result,pages=extract(raw,fmt)
        if fmt in image_evidence.FORMATS:
            png,_=image_evidence.preview(raw,fmt)
            path.with_suffix('.preview.png').write_bytes(png)
        else:path.with_suffix('.txt').write_text('\n'.join(f'UNIT {n}\n{text}' for n,text in pages),encoding='utf-8')
        algorithm=DOCX_ALGORITHM if fmt=='docx' else image_evidence.ALGORITHM if fmt in image_evidence.FORMATS else 'torgi-file-text-v2'
        print(json.dumps({**result,'sha256':digest,'algorithm':algorithm},ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error':str(exc)[:500]},ensure_ascii=False));raise SystemExit(1)


if __name__=='__main__':main()
