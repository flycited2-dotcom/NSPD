"""Text evidence from bounded PDF / DOCX bodies, never executable Word content."""
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile
from xml.etree import ElementTree as ET
from . import municipal, schemes

NS='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def docx_text(raw):
    if len(raw)>8*1024*1024 or not raw.startswith(b'PK\x03\x04'):raise ValueError('Ожидался DOCX до 8 МБ')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        members=z.infolist();names=[x.filename for x in members]
        if len(members)>500 or len(set(names))!=len(names) or sum(x.file_size for x in members)>20*1024*1024:
            raise ValueError('Архив DOCX превышает лимиты или содержит повторные части')
        if any(x.flag_bits&1 for x in members) or any('vbaproject' in n.lower() for n in names):
            raise ValueError('Шифрование или макросы DOCX не поддержаны')
        info=z.getinfo('word/document.xml')
        if info.file_size>2*1024*1024:raise ValueError('Основной XML DOCX превышает лимит')
        xml=z.read(info)
        markup=xml.replace(b'\x00',b'').upper()
        if b'<!DOCTYPE' in markup or b'<!ENTITY' in markup:raise ValueError('DTD/ENTITY DOCX запрещены')
        root=ET.fromstring(xml)
        paragraphs=[''.join(x.text or '' for x in p.iter(NS+'t')) for p in root.iter(NS+'p')]
        text='\n'.join(paragraphs)
        if len(text)>500000:raise ValueError('Текст DOCX превышает лимит')
        return text,len(paragraphs)


def extract(raw,fmt):
    if fmt=='pdf':
        details,pages=municipal.pdf_text(raw)
        result=schemes.apply_page_limit(schemes.extract(pages),details['processed_pages'],details['unread_pages'])
        return {**details,'tables':result['tables'],'unparsed_coordinate_pages':result['unparsed_coordinate_pages'],
                'unreadable_tables':result['unreadable_tables'],'crs_status':result['crs_status'],
                'unit':'pdf_page','georeferenced':False},pages
    if fmt!='docx':raise ValueError('Формат не поддержан')
    text,count=docx_text(raw)
    # DOCX pagination cannot be recovered from body XML. Never call this a PDF page.
    evidence=municipal.evidence([(1,text)])
    for mention in evidence['mentions']:
        mention['sections']=mention.pop('pages')
    return {**evidence,'unit':'docx_body','paragraph_count':count,'tables':[], 'text_layer_complete':False,
            'scope_note':'Прочитан основной XML DOCX; страницы, колонтитулы, изображения и отношения номера к лоту не установлены.'},[(1,text)]


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        path,digest,fmt=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('SHA-256 файла изменился')
        result,pages=extract(raw,fmt)
        path.with_suffix('.txt').write_text('\n'.join(f'UNIT {n}\n{text}' for n,text in pages),encoding='utf-8')
        print(json.dumps({**result,'sha256':digest,'algorithm':'torgi-file-text-v1'},ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error':str(exc)[:500]},ensure_ascii=False));raise SystemExit(1)


if __name__=='__main__':main()
