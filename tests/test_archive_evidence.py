import copy
import hashlib
import io
import stat
import struct
import zipfile
import zlib

import pytest

from land import archive_evidence as archive


PDF = b'%PDF-1.7\npublic synthetic document\n'


def make_zip(rows=None, compression=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=compression) as target:
        for name, content in rows or [('scheme.pdf', PDF)]:
            target.writestr(name, content)
    return output.getvalue()


def identify(raw, index=0):
    manifest = archive.inspect(raw)
    return archive.member_identifier(manifest, manifest['entries'][index])


def patch_headers(raw, *, flags=None, method=None, crc=None, size=None, compressed=None):
    data = bytearray(raw)
    central = data.index(b'PK\x01\x02')
    if flags is not None:
        struct.pack_into('<H', data, 6, flags)
        struct.pack_into('<H', data, central + 8, flags)
    if method is not None:
        struct.pack_into('<H', data, 8, method)
        struct.pack_into('<H', data, central + 10, method)
    for value, local_at, central_at in ((crc, 14, 16), (compressed, 18, 20), (size, 22, 24)):
        if value is not None:
            struct.pack_into('<L', data, local_at, value)
            struct.pack_into('<L', data, central + central_at, value)
    return bytes(data)


@pytest.mark.parametrize('compression', [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_metadata_inspection_does_not_read_members_or_invent_content_hash(compression, monkeypatch):
    raw = make_zip([('folder/', b''), ('folder/scheme.pdf', PDF),
                    ('nested.zip', b'PK\x03\x04unread'), ('program.exe', b'MZunread')], compression)
    monkeypatch.setattr(zipfile.ZipFile, 'open', lambda *args, **kwargs: pytest.fail('No member reads'))
    monkeypatch.setattr(zlib, 'decompressobj', lambda *args, **kwargs: pytest.fail('No decompression'))
    manifest = archive.inspect(raw)
    assert manifest['entry_count'] == 4
    assert manifest['archive_sha256'] == hashlib.sha256(raw).hexdigest()
    assert manifest['state'] == 'inspected' and not manifest['member_contents_read']
    assert not manifest['crc_verified'] and not manifest['geometry_confirmed']
    entries = manifest['entries']
    assert [entry['state'] for entry in entries] == ['directory', 'ready', 'unsupported', 'unsupported']
    assert entries[0]['name'] == 'folder'
    assert 'Вложенные архивы' in entries[2]['reason']
    assert all('sha256' not in entry for entry in entries)


@pytest.mark.parametrize('fmt,content', [
    ('pdf', PDF), ('docx', make_zip([('word/document.xml', b'<document/>')])),
    ('doc', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1body'), ('rtf', b'{\\rtf1 body}'),
    ('jpg', b'\xff\xd8\xffbody'), ('jpeg', b'\xff\xd8\xffbody'),
    ('png', b'\x89PNG\r\n\x1a\nbody'),
])
@pytest.mark.parametrize('compression', [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_read_supported_member_returns_unchanged_bytes_with_verified_hash_and_crc(fmt, content, compression):
    raw = make_zip([('subfolder/sample.' + fmt, content)], compression)
    identifier = identify(raw)
    before = copy.deepcopy(identifier)
    value, entry = archive.read_member(raw, identifier)
    assert value == content and identifier == before
    assert entry['sha256'] == hashlib.sha256(content).hexdigest()
    assert entry['actual_size'] == len(content) and entry['crc_verified']
    assert entry['state'] == 'read' and not entry['geometry_confirmed'] and not entry['georeferenced']
    assert entry['archive_sha256'] == hashlib.sha256(raw).hexdigest()
    assert archive.read_member(raw, {**identifier, 'sha256': entry['sha256']})[0] == content


@pytest.mark.parametrize('name', [
    '../scheme.pdf', 'folder/../scheme.pdf', '/scheme.pdf', '\\server\\scheme.pdf',
    'folder\\scheme.pdf', 'C:/scheme.pdf', 'folder/name:stream.pdf', './scheme.pdf',
    'folder//scheme.pdf', '', 'NUL.pdf', 'con', 'aux.txt', 'COM1.pdf', 'LPT9.txt',
    'com¹.pdf', 'folder/PRN/name.pdf', 'folder./scheme.pdf', 'folder /scheme.pdf',
    'folder/\x00scheme.pdf', 'folder/\nname.pdf', 'folder/\u202ename.pdf',
])
def test_unsafe_names_reject_whole_archive(name):
    if not name:
        raw = make_zip([('scheme.pdf', PDF)])
        data = bytearray(raw);central = data.index(b'PK\x01\x02')
        # Empty name lengths are structurally inconsistent and fail closed too.
        struct.pack_into('<H', data, central + 28, 0)
        raw = bytes(data)
    elif '\x00' in name:
        # ZipInfo itself silently truncates NUL. Patch bytes to preserve malicious input.
        raw = make_zip([('folder/Xscheme.pdf', PDF)]).replace(b'folder/Xscheme.pdf', b'folder/\0scheme.pdf')
    elif '\\' in name:
        # Windows ZipInfo canonicalizes slashes on creation. Patch the wire names.
        safe = name.replace('\\', '/')
        raw = make_zip([(safe, PDF)]).replace(safe.encode(), name.encode())
    else:
        raw = make_zip([(name, PDF)])
    with pytest.raises(ValueError, match='ZIP:'):
        archive.inspect(raw)


@pytest.mark.parametrize('names', [
    ['scheme.pdf', 'SCHEME.PDF'], ['caf\u00e9.pdf', 'cafe\u0301.pdf'],
    ['Stra\u00dfe.pdf', 'STRASSE.pdf'], ['folder/', 'folder'],
])
def test_duplicate_unicode_casefold_names_reject_whole_archive(names):
    raw = make_zip([(name, b'' if name.endswith('/') else PDF) for name in names])
    with pytest.raises(ValueError, match='повтор имени'):
        archive.inspect(raw)


def test_nfc_canonical_name_still_selects_original_member():
    raw = make_zip([('cafe\u0301.pdf', PDF)])
    manifest = archive.inspect(raw)
    assert manifest['entries'][0]['name'] == 'caf\u00e9.pdf'
    assert archive.read_member(raw, identify(raw))[0] == PDF


@pytest.mark.parametrize('mode', [stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK])
def test_symlinks_and_special_devices_reject_whole_archive(mode):
    info = zipfile.ZipInfo('scheme.pdf')
    info.create_system = 3
    info.external_attr = (mode | 0o600) << 16
    with pytest.raises(ValueError, match='ссылки|устройства'):
        archive.inspect(make_zip([(info, PDF)]))


@pytest.mark.parametrize('flags', [1, 0x40, 0x2000, 0x8000])
def test_encryption_or_unknown_flags_rejected(flags):
    with pytest.raises(ValueError, match='шифрование|флаги'):
        archive.inspect(patch_headers(make_zip(), flags=flags))


@pytest.mark.parametrize('method', [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA, 99])
def test_unexpected_compression_rejects_whole_archive(method):
    with pytest.raises(ValueError, match='Stored и Deflate'):
        archive.inspect(patch_headers(make_zip(), method=method))


@pytest.mark.parametrize('tag', [0x0001, 0x0017, 0x9901])
def test_zip64_and_encryption_extra_fields_reject_whole_archive(tag):
    info = zipfile.ZipInfo('scheme.pdf')
    info.extra = struct.pack('<HH', tag, 0)
    with pytest.raises(ValueError, match='ZIP64|шифрование'):
        archive.inspect(make_zip([(info, PDF)]))


def test_limits_cover_compressed_input_entry_count_total_entry_size_and_ratio(monkeypatch):
    raw = make_zip()
    monkeypatch.setattr(archive, 'MAX_BYTES', len(raw) - 1)
    with pytest.raises(ValueError, match='32 МиБ'):
        archive.inspect(raw)
    monkeypatch.setattr(archive, 'MAX_BYTES', 32 * 1024 * 1024)
    monkeypatch.setattr(archive, 'MAX_ENTRIES', 1)
    with pytest.raises(ValueError, match='64 записей'):
        archive.inspect(make_zip([('a.pdf', PDF), ('b.pdf', PDF)]))
    monkeypatch.setattr(archive, 'MAX_ENTRIES', 64)
    monkeypatch.setattr(archive, 'MAX_MEMBER_BYTES', len(PDF) - 1)
    with pytest.raises(ValueError, match='размер записи'):
        archive.inspect(raw)
    monkeypatch.setattr(archive, 'MAX_MEMBER_BYTES', 16 * 1024 * 1024)
    monkeypatch.setattr(archive, 'MAX_TOTAL_BYTES', len(PDF))
    with pytest.raises(ValueError, match='64 МиБ'):
        archive.inspect(make_zip([('a.pdf', PDF), ('b.pdf', PDF)]))
    monkeypatch.setattr(archive, 'MAX_TOTAL_BYTES', 64 * 1024 * 1024)
    with pytest.raises(ValueError, match='коэффициент сжатия'):
        archive.inspect(make_zip([('bomb.pdf', b'%PDF-' + b'A' * 100000)]))


def test_pdf_member_size_limit_cannot_expand(monkeypatch):
    raw = patch_headers(make_zip(), size=16 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match='размер записи'):
        archive.inspect(raw)


@pytest.mark.parametrize('name,content', [('nested.zip', b'PK\x03\x04zip'), ('run.exe', b'MZexe'), ('x.txt', b'text'), ('folder/', b'')])
def test_unsupported_nested_archives_executables_and_directories_cannot_be_read(name, content):
    raw = make_zip([(name, content)])
    with pytest.raises(ValueError, match='неподдержанного'):
        archive.read_member(raw, identify(raw))


@pytest.mark.parametrize('field,value', [
    ('archive_sha256', '0' * 64), ('id', '0' * 64), ('name', 'other.pdf'),
    ('size', 1), ('compressed_size', 1), ('format', 'docx'), ('crc32', '00000000'),
])
def test_changed_source_bound_descriptor_rejected(field, value):
    raw = make_zip()
    with pytest.raises(ValueError, match='изменились'):
        archive.read_member(raw, {**identify(raw), field: value})


def test_different_archive_bytes_and_previous_content_hash_are_checked():
    raw = make_zip();identifier = identify(raw)
    with pytest.raises(ValueError, match='изменились'):
        archive.read_member(make_zip([('scheme.pdf', PDF + b'changed')]), identifier)
    with pytest.raises(ValueError, match='SHA-256 записи изменился'):
        archive.read_member(raw, {**identifier, 'sha256': '0' * 64})
    with pytest.raises(ValueError, match='ожидаемый SHA'):
        archive.read_member(raw, {**identifier, 'sha256': 'wrong'})


def test_actual_read_checks_crc_and_actual_size_not_just_headers():
    raw = patch_headers(make_zip(), crc=0)
    manifest = archive.inspect(raw)
    assert not manifest['crc_verified']
    with pytest.raises(ValueError, match='CRC'):
        archive.read_member(raw, identify(raw))
    # A forged declared length and CRC of only a prefix must not hide extra output.
    original = PDF + b'other bytes'
    raw = patch_headers(make_zip([('scheme.pdf', original)]), size=len(PDF), crc=zlib.crc32(PDF))
    archive.inspect(raw)
    with pytest.raises(ValueError, match='фактический размер'):
        archive.read_member(raw, identify(raw))
    raw = patch_headers(make_zip(), size=len(PDF) + 1)
    with pytest.raises(ValueError, match='фактический размер'):
        archive.read_member(raw, identify(raw))


def test_supported_extension_with_executable_or_nested_zip_bytes_rejected():
    for content in (b'MZexe', b'PK\x03\x04nested', b'not pdf'):
        raw = make_zip([('scheme.pdf', content)])
        with pytest.raises(ValueError, match='содержимое не соответствует'):
            archive.read_member(raw, identify(raw))


@pytest.mark.parametrize('change', ['local_name', 'local_size', 'overlap', 'multidisk', 'count', 'trailing', 'prefix', 'bad_eocd'])
def test_structural_inconsistency_rejects_archive_before_member_read(change):
    raw = bytearray(make_zip())
    central = raw.index(b'PK\x01\x02')
    eocd = raw.index(b'PK\x05\x06')
    if change == 'local_name':raw[30] ^= 1
    elif change == 'local_size':struct.pack_into('<L', raw, 22, 999)
    elif change == 'overlap':struct.pack_into('<L', raw, central + 42, 1)
    elif change == 'multidisk':struct.pack_into('<H', raw, eocd + 4, 1)
    elif change == 'count':struct.pack_into('<H', raw, eocd + 8, 2);struct.pack_into('<H', raw, eocd + 10, 2)
    elif change == 'trailing':raw.extend(b'trailing')
    elif change == 'prefix':raw = b'MZprefix' + raw
    elif change == 'bad_eocd':raw[eocd] ^= 1
    with pytest.raises(ValueError, match='ZIP:'):
        archive.inspect(bytes(raw))


def test_streamed_archive_data_descriptor_is_supported_and_validated():
    class Unseekable(io.BytesIO):
        def seekable(self):return False
        def seek(self, *args):raise OSError('unseekable')
    output = Unseekable()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as target:
        target.writestr('scheme.pdf', PDF)
    raw = output.getvalue()
    assert archive.read_member(raw, identify(raw))[0] == PDF
    corrupted = bytearray(raw)
    descriptor = corrupted.index(b'PK\x07\x08')
    corrupted[descriptor + 4] ^= 1
    with pytest.raises(ValueError, match='дескриптор'):
        archive.inspect(bytes(corrupted))


def test_empty_archive_is_a_metadata_observation_without_contents():
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w'):pass
    observed = archive.inspect(raw.getvalue())
    assert observed['entry_count'] == 0 and observed['entries'] == []
    assert observed['total_uncompressed_size'] == 0 and not observed['member_contents_read']


def test_no_filesystem_writes_or_zipfile_extract_calls(monkeypatch):
    raw = make_zip()
    monkeypatch.setattr(zipfile.ZipFile, 'extract', lambda *args, **kwargs: pytest.fail('No extraction'))
    monkeypatch.setattr(zipfile.ZipFile, 'extractall', lambda *args, **kwargs: pytest.fail('No extraction'))
    monkeypatch.setattr('builtins.open', lambda *args, **kwargs: pytest.fail('No filesystem'))
    assert archive.read_member(raw, identify(raw))[0] == PDF
