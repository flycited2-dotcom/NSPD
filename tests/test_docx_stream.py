import io
import zipfile
import pytest
from land import docx_text as reader

OPEN = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
CLOSE = '</w:body></w:document>'


def archive(xml):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr('word/document.xml', xml)
    return raw.getvalue()


def paragraph(text):
    return '<w:p><w:r><w:t>' + text + '</w:t></w:r></w:p>'


def test_large_markup_small_text_streams_without_zip_read(monkeypatch):
    xml = OPEN + '<w:p>' + '<w:r><w:rPr><w:b/><w:color w:val="123456"/></w:rPr></w:r>' * 50000
    xml += '<w:r><w:t>90:12:172001:956</w:t></w:r></w:p>' + CLOSE
    assert len(xml.encode()) > 2 * 1024 * 1024
    raw = archive(xml)
    monkeypatch.setattr(zipfile.ZipFile, 'read', lambda *a, **k: pytest.fail('Main XML must be streamed'))
    text, count, stats = reader.read_docx(raw)
    assert text == '90:12:172001:956' and count == 1
    assert stats['main_xml_bytes'] == len(xml.encode()) and stats['body_xml_complete']


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16'])
def test_text_split_across_chunks_runs_tables_and_empty_paragraphs(encoding):
    body = paragraph('А' * 65520 + '90:12:172001:956') + '<w:p/>'
    body += '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>90:12:</w:t></w:r><w:r><w:t>172001:957</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    xml = ('<?xml version="1.0" encoding="' + encoding + '"?>' + OPEN + body + CLOSE).encode(encoding)
    text, count, stats = reader.read_docx(archive(xml))
    assert count == 3 and text.endswith('90:12:172001:956\n\n90:12:172001:957')
    assert stats['body_text_characters'] == len(text)


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16'])
def test_dtd_after_chunk_boundary_and_external_entities_are_rejected(encoding):
    xml = '<?xml version="1.0" encoding="' + encoding + '"?>'
    xml += '<!--' + ' ' * 65530 + '--><!DOCTYPE w:document [<!ENTITY e SYSTEM "file:///never-read">]>'
    xml += OPEN + paragraph('&e;') + CLOSE
    with pytest.raises(ValueError, match='DTD/ENTITY'):
        reader.read_docx(archive(xml.encode(encoding)))


@pytest.mark.parametrize('xml', [
    OPEN + paragraph('visible') + CLOSE[:-1],
    '<document><body><p>Other namespace</p></body></document>',
    OPEN + CLOSE + 'trailing corruption',
    OPEN + paragraph('first') + '</w:body><w:body>' + paragraph('second') + CLOSE,
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
], ids=['truncated', 'wrong_namespace', 'trailing_data', 'duplicate_body', 'missing_body'])
def test_partial_wrong_namespace_and_ambiguous_body_do_not_become_evidence(xml):
    with pytest.raises(ValueError):
        reader.read_docx(archive(xml))


def test_nested_paragraphs_match_previous_reader_without_unbounded_growth():
    body = '<w:p><w:r><w:t>outer</w:t><w:p><w:r><w:t>inner</w:t></w:r></w:p></w:r></w:p>'
    text, count, stats = reader.read_docx(archive(OPEN + body + CLOSE))
    assert text == 'outerinner\ninner' and count == 2
    assert stats['body_text_characters'] == len(text)


@pytest.mark.parametrize('body,reason', [
    ('<w:r>' * 65 + '</w:r>' * 65, 'Структура'),
    (paragraph('x' * 500001), 'Текст'),
    ('<w:p/>' * 10001, 'абзацев'),
    ('<w:r/>' * 300001, 'Структура'),
    ('<!--' + ' ' * (8 * 1024 * 1024) + '-->', 'XML'),
], ids=['depth', 'text', 'paragraphs', 'elements', 'xml_bytes'])
def test_resource_limits_reject_instead_of_returning_partial_text(body, reason):
    with pytest.raises(ValueError, match=reason):
        reader.read_docx(archive(OPEN + body + CLOSE))


def test_zip_crc_corruption_is_not_accepted_as_complete_body():
    raw = bytearray(archive(OPEN + paragraph('source') + CLOSE))
    central = raw.index(b'PK\x01\x02')
    raw[central + 16] ^= 1
    with pytest.raises(zipfile.BadZipFile):
        reader.read_docx(bytes(raw))
