"""Bounded streaming of Word body text, without building an XML tree."""
import io
import zipfile
from xml.parsers import expat

ALGORITHM = 'torgi-docx-stream-v1'
WORD = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MAX_XML = 8 * 1024 * 1024
MAX_TEXT = 500000
MAX_ELEMENTS = 300000
MAX_DEPTH = 64
MAX_PARAGRAPHS = 10000
CHUNK = 65536


def read_docx(raw):
    if len(raw) > 8 * 1024 * 1024 or not raw.startswith(b'PK\x03\x04'):
        raise ValueError('Ожидался DOCX до 8 МБ')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        names = [part.filename for part in members]
        if len(members) > 500 or len(set(names)) != len(names) or sum(p.file_size for p in members) > 20 * 1024 * 1024:
            raise ValueError('Архив DOCX превышает лимиты или содержит повторные части')
        if any(p.flag_bits & 1 for p in members) or any('vbaproject' in n.lower() for n in names):
            raise ValueError('Шифрование или макросы DOCX не поддержаны')
        info = archive.getinfo('word/document.xml')
        if info.file_size > MAX_XML:
            raise ValueError('Основной XML DOCX превышает лимит 8 МБ')
        parser = expat.ParserCreate(namespace_separator='}')
        stack, frames, paragraphs = [], [], []
        stats = {'main_xml_bytes': 0, 'xml_elements': 0, 'max_xml_depth': 0, 'body_text_characters': 0}
        body_seen = False

        def reject_declaration(*args):
            raise ValueError('DTD/ENTITY DOCX запрещены')

        def start(tag, attrs):
            nonlocal body_seen
            if not stack and tag != WORD + 'document':
                raise ValueError('Основной XML не является документом Word')
            stack.append(tag)
            stats['xml_elements'] += 1
            stats['max_xml_depth'] = max(stats['max_xml_depth'], len(stack))
            if len(stack) > MAX_DEPTH or stats['xml_elements'] > MAX_ELEMENTS:
                raise ValueError('Структура XML DOCX превышает лимит')
            if tag == WORD + 'body':
                if body_seen or stack != [WORD + 'document', WORD + 'body']:
                    raise ValueError('Основная часть Word неоднозначна')
                body_seen = True
            if tag == WORD + 'p' and WORD + 'body' in stack:
                if len(paragraphs) >= MAX_PARAGRAPHS:
                    raise ValueError('Число абзацев DOCX превышает лимит')
                if paragraphs:
                    stats['body_text_characters'] += 1  # paragraph separator
                paragraphs.append(None)
                frames.append((len(paragraphs) - 1, []))
                if stats['body_text_characters'] > MAX_TEXT:
                    raise ValueError('Текст DOCX превышает лимит')

        def end(tag):
            if tag == WORD + 'p' and frames:
                index, parts = frames.pop()
                paragraphs[index] = ''.join(parts)
            stack.pop()

        def characters(text):
            if frames and stack[-1] == WORD + 't':
                # Match the previous reader's paragraph.iter(w:t), including nested paragraphs.
                stats['body_text_characters'] += len(text) * len(frames)
                if stats['body_text_characters'] > MAX_TEXT:
                    raise ValueError('Текст DOCX превышает лимит')
                for _, parts in frames:
                    parts.append(text)

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.CharacterDataHandler = characters
        parser.StartDoctypeDeclHandler = reject_declaration
        parser.EntityDeclHandler = reject_declaration
        parser.ExternalEntityRefHandler = reject_declaration
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        try:
            with archive.open(info) as source:
                while chunk := source.read(CHUNK):
                    stats['main_xml_bytes'] += len(chunk)
                    if stats['main_xml_bytes'] > MAX_XML:
                        raise ValueError('Основной XML DOCX превышает лимит 8 МБ')
                    parser.Parse(chunk, False)
                parser.Parse(b'', True)
        except expat.ExpatError as exc:
            raise ValueError('Некорректный XML DOCX') from exc
        if not body_seen:
            raise ValueError('Основная часть Word отсутствует')
        if stats['main_xml_bytes'] != info.file_size:
            raise ValueError('Размер основного XML отличается от архива')
        return '\n'.join(paragraphs), len(paragraphs), {**stats, 'body_xml_complete': True, 'text_reader': 'streaming'}


def docx_text(raw):
    text, count, _ = read_docx(raw)
    return text, count
