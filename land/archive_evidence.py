"""Bounded, source-bound ZIP evidence; never extracts paths or executes members.

``inspect(raw)`` validates the complete archive metadata without decompressing
members. ``read_member(raw, identifier)`` revalidates it and reads one supported
member into bounded memory. A caller should run these helpers in its existing
time-limited worker. Unsafe archive structure rejects the whole archive; an
ordinary unsupported file is retained in the manifest without being opened.
"""
import hashlib
import re
import stat
import struct
import unicodedata
import zipfile
import zlib


ALGORITHM = 'archive-evidence-v1'
MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 64
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_RATIO = 100
MAX_NAME = 1024
READ_FORMATS = ('pdf', 'docx', 'doc', 'rtf', 'jpg', 'jpeg', 'png')
COMPRESSION = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_DEVICE = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', re.I)
_SIGNATURES = {
    'pdf': b'%PDF-', 'docx': b'PK\x03\x04',
    'doc': b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', 'rtf': b'{\\rtf1',
    'jpg': b'\xff\xd8\xff', 'jpeg': b'\xff\xd8\xff', 'png': b'\x89PNG\r\n\x1a\n',
}


def _reject(message):
    raise ValueError('ZIP: ' + message)


def _raw(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        _reject('ожидались bytes архива размером до 32 МиБ')


def _directory(raw):
    """Preflight EOCD before ZipFile can allocate an unbounded ZipInfo list."""
    _raw(raw)
    start = raw.rfind(b'PK\x05\x06', max(0, len(raw) - 65557))
    if start < 0 or start + 22 > len(raw):
        _reject('не найден конец центрального каталога')
    _, disk, cd_disk, on_disk, count, size, offset, comment = struct.unpack_from('<4s4H2LH', raw, start)
    if start + 22 + comment != len(raw):
        _reject('неожиданные данные после архива или повреждённый комментарий')
    if disk or cd_disk or on_disk != count:
        _reject('многотомный архив не поддержан')
    if count == 65535 or size == 0xffffffff or offset == 0xffffffff:
        _reject('ZIP64 не поддержан')
    if count > MAX_ENTRIES:
        _reject('превышен предел 64 записей')
    if offset + size != start or offset > start:
        _reject('некорректные границы центрального каталога')
    return count, offset, start


def _extra(raw):
    position = 0
    while position < len(raw):
        if position + 4 > len(raw):
            _reject('повреждён дополнительный заголовок')
        tag, size = struct.unpack_from('<HH', raw, position)
        position += 4
        if position + size > len(raw):
            _reject('повреждён дополнительный заголовок')
        if tag in (0x0001, 0x0017, 0x9901):
            _reject('ZIP64 или шифрование не поддержаны')
        position += size


def _name(value, directory):
    if (not isinstance(value, str) or not value or len(value) > MAX_NAME
            or '\\' in value or ':' in value or value.startswith('/')
            or any(unicodedata.category(character) in ('Cc', 'Cf', 'Cs') for character in value)):
        _reject('небезопасное имя записи')
    name = unicodedata.normalize('NFC', value[:-1] if directory else value)
    parts = name.split('/')
    if any(not part or part in ('.', '..') or part.endswith((' ', '.')) or _DEVICE.match(part)
           for part in parts):
        _reject('неканонический относительный путь или имя устройства')
    return name


def _headers(raw, count, offset, end):
    """Cross-check central and local headers, data descriptors and byte ranges."""
    position, headers, ranges = offset, [], []
    for _ in range(count):
        if position + 46 > end or raw[position:position + 4] != b'PK\x01\x02':
            _reject('повреждена запись центрального каталога')
        fields = struct.unpack_from('<4s6H3L5H2L', raw, position)
        (_, made, needed, flags, method, _, _, crc, compressed, size,
         name_size, extra_size, comment_size, disk, _, external, local) = fields
        next_position = position + 46 + name_size + extra_size + comment_size
        if next_position > end or disk or needed > 20:
            _reject('неподдерживаемая или повреждённая запись каталога')
        if flags & ~0x080e or flags & 1 or flags & 0x40:
            _reject('шифрование или неизвестные флаги записи')
        if method not in COMPRESSION:
            _reject('поддержаны только Stored и Deflate')
        if method == zipfile.ZIP_STORED and flags & 6:
            _reject('неожиданные флаги Stored')
        encoded_name = raw[position + 46:position + 46 + name_size]
        try:
            decoded_name = encoded_name.decode('utf-8' if flags & 0x800 else 'cp437')
        except UnicodeError:
            _reject('некорректная кодировка имени')
        _extra(raw[position + 46 + name_size:position + 46 + name_size + extra_size])
        mode = external >> 16
        kind = stat.S_IFMT(mode)
        directory = decoded_name.endswith('/')
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR) != directory and kind:
            _reject('ссылки, устройства или противоречивый тип записи не поддержаны')
        if external & 8:  # DOS volume-label attribute, never a document.
            _reject('метка устройства не поддержана')
        name = _name(decoded_name, directory)
        if size > MAX_MEMBER_BYTES or (size and (not compressed or size > compressed * MAX_RATIO)):
            _reject('превышен размер записи или коэффициент сжатия 100')
        if directory and (size or crc or compressed > 2):
            _reject('каталог содержит данные')
        if method == zipfile.ZIP_STORED and size != compressed:
            _reject('размеры Stored не согласованы')
        if local + 30 > offset or raw[local:local + 4] != b'PK\x03\x04':
            _reject('некорректное положение локального заголовка')
        (_, local_needed, local_flags, local_method, _, _, local_crc,
         local_compressed, local_size, local_name_size, local_extra_size) = struct.unpack_from('<4s5H3L2H', raw, local)
        data_start = local + 30 + local_name_size + local_extra_size
        if data_start > offset or data_start + compressed > offset:
            _reject('данные записи выходят за границы архива')
        if (local_needed != needed or local_flags != flags or local_method != method
                or raw[local + 30:local + 30 + local_name_size] != encoded_name):
            _reject('локальный заголовок не соответствует каталогу')
        _extra(raw[local + 30 + local_name_size:data_start])
        if flags & 8:
            if (local_crc not in (0, crc) or local_compressed not in (0, compressed)
                    or local_size not in (0, size)):
                _reject('размеры локального заголовка не согласованы')
            descriptor = data_start + compressed
            if raw[descriptor:descriptor + 4] == b'PK\x07\x08':
                descriptor += 4
            if descriptor + 12 > offset or struct.unpack_from('<3L', raw, descriptor) != (crc, compressed, size):
                _reject('повреждён дескриптор данных')
            data_end = descriptor + 12
        else:
            if (local_crc, local_compressed, local_size) != (crc, compressed, size):
                _reject('размеры или CRC локального заголовка не согласованы')
            data_end = data_start + compressed
        ranges.append((local, data_end))
        headers.append({'name': name, 'original_name': decoded_name, 'size': size,
                        'compressed_size': compressed, 'crc32': f'{crc:08x}',
                        'compression': method, 'directory': directory, 'header_offset': local,
                        'data_offset': data_start})
        position = next_position
    if position != end:
        _reject('число записей или размер каталога не согласованы')
    cursor = 0
    for start, finish in sorted(ranges):
        if start != cursor or finish < start:
            _reject('перекрывающиеся записи или неожиданные данные между записями')
        cursor = finish
    if cursor != offset:
        _reject('неожиданные данные перед центральным каталогом')
    if sum(header['size'] for header in headers) > MAX_TOTAL_BYTES:
        _reject('превышен общий распакованный размер 64 МиБ')
    names = [unicodedata.normalize('NFC', header['name'].casefold()) for header in headers]
    if len(names) != len(set(names)):
        _reject('повтор имени после Unicode NFC/casefold')
    return headers


def inspect(raw):
    """Return metadata-only manifest; no member content SHA is invented."""
    count, offset, end = _directory(raw)
    headers = _headers(raw, count, offset, end)
    archive_sha256 = hashlib.sha256(raw).hexdigest()
    entries = []
    for header in headers:
        name = header['name']
        fmt = name.rsplit('/', 1)[-1].rsplit('.', 1)[-1].lower() if '.' in name.rsplit('/', 1)[-1] else ''
        state = 'directory' if header['directory'] else 'ready' if fmt in READ_FORMATS else 'unsupported'
        reason = ('Каталог; содержимое не читается' if header['directory'] else '' if state == 'ready'
                  else 'Вложенные архивы не читаются' if fmt in ('zip', 'zipx', '7z', 'rar', 'tar', 'gz')
                  else 'Формат не поддержан; содержимое не читается и не исполняется')
        identity = archive_sha256 + '\0' + header['original_name'] + '\0' + str(header['header_offset'])
        entries.append({'id': hashlib.sha256(identity.encode('utf-8')).hexdigest(),
                        'name': name, 'size': header['size'], 'compressed_size': header['compressed_size'],
                        'format': fmt, 'state': state, 'reason': reason, 'crc32': header['crc32'],
                        'compression': header['compression']})
    return {'algorithm': ALGORITHM, 'archive_sha256': archive_sha256, 'size': len(raw),
            'state': 'inspected', 'entry_count': len(entries),
            'total_uncompressed_size': sum(entry['size'] for entry in entries),
            'entries': entries, 'member_contents_read': False, 'crc_verified': False,
            'geometry_confirmed': False, 'georeferenced': False}


def member_identifier(manifest, entry):
    """Build a small identifier bound to the inspected source and descriptor."""
    if (not isinstance(manifest, dict) or not isinstance(entry, dict)
            or manifest.get('algorithm') != ALGORITHM
            or not any(entry == row for row in manifest.get('entries', []))):
        _reject('запись не принадлежит манифесту')
    return {'archive_sha256': manifest['archive_sha256'],
            **{key: entry[key] for key in ('id', 'name', 'size', 'compressed_size', 'format', 'crc32')}}


def _read_content(raw, header):
    """Bound actual output, including bytes beyond a forged declared size.

    ZipExtFile truncates output to its declared size. Read the compressed stream
    directly so a forged size/CRC for only its prefix cannot hide extra output.
    """
    start, length = header['data_offset'], header['compressed_size']
    chunks, size, crc = [], 0, 0
    decoder = zlib.decompressobj(-15) if header['compression'] == zipfile.ZIP_DEFLATED else None

    def accept(chunk):
        nonlocal size, crc
        size += len(chunk)
        if size > header['size'] or size > MAX_MEMBER_BYTES:
            _reject('фактический размер записи превышает объявленный или лимит')
        crc = zlib.crc32(chunk, crc)
        chunks.append(chunk)

    for offset in range(0, length, 65536):
        block = raw[start + offset:start + min(offset + 65536, length)]
        if decoder is None:
            accept(block)
            continue
        while block:
            output = decoder.decompress(block, min(65536, header['size'] - size + 1))
            accept(output)
            if decoder.eof:
                if decoder.unused_data or offset + min(65536, length - offset) != length:
                    _reject('неожиданные данные после потока Deflate')
                break
            tail = decoder.unconsumed_tail
            if tail == block and not output:
                _reject('поток Deflate не продвигается')
            block = tail
    if decoder is not None and not decoder.eof:
        _reject('неполный поток Deflate')
    if size != header['size'] or f'{crc & 0xffffffff:08x}' != header['crc32']:
        _reject('фактический размер или CRC записи отличается от каталога')
    return b''.join(chunks)


def read_member(raw, identifier):
    """Return ``(bytes, read_entry)`` after repeated metadata, CRC, size and magic checks.

    The identifier must come from ``member_identifier``. It may additionally
    include ``sha256`` from a previous successful read; that digest is checked.
    No content is returned on any error and nothing is written to the filesystem.
    """
    manifest = inspect(raw)
    if not isinstance(identifier, dict):
        _reject('требуется идентификатор из манифеста')
    entry = next((row for row in manifest['entries'] if row['id'] == identifier.get('id')), None)
    if entry is None or member_identifier(manifest, entry) != {key: identifier.get(key)
            for key in ('archive_sha256', 'id', 'name', 'size', 'compressed_size', 'format', 'crc32')}:
        _reject('архив или описание записи изменились')
    expected = identifier.get('sha256')
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)):
        _reject('некорректный ожидаемый SHA-256 записи')
    if entry['state'] != 'ready':
        _reject('чтение каталога, вложенного архива или неподдержанного файла запрещено')
    count, offset, end = _directory(raw)
    headers = _headers(raw, count, offset, end)
    index = next(index for index, row in enumerate(manifest['entries']) if row['id'] == entry['id'])
    try:
        content = _read_content(raw, headers[index])
    except zlib.error as exc:
        raise ValueError('ZIP: не удалось проверить данные записи: ' + str(exc)[:200]) from exc
    if not content.startswith(_SIGNATURES[entry['format']]):
        _reject('содержимое не соответствует формату записи')
    digest = hashlib.sha256(content).hexdigest()
    if expected is not None and digest != expected:
        _reject('SHA-256 записи изменился')
    return content, {**entry, 'state': 'read', 'sha256': digest, 'actual_size': len(content),
                     'crc_verified': True, 'archive_sha256': manifest['archive_sha256'],
                     'geometry_confirmed': False, 'georeferenced': False}
