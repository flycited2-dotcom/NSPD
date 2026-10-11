"""Explicit bounded text reading of one source-bound large public Torgi PDF."""
import hashlib
import json
from pathlib import Path
import re
import sys

from . import egrn_coordinates, municipal, schemes, store

ALGORITHM = 'torgi-large-pdf-v1'
MAX_BYTES = 32 * 1024 * 1024
MAX_PAGES = 120
MAX_OUTPUT = 4 * 1024 * 1024
TEXT_SUFFIX = '.large.continued.txt'


def scope(file):
    """A successful source/receipt-bound marker alone permits extended local use."""
    digest = file.get('sha256')
    total, processed, unread = (file.get(key) for key in ('total_pages', 'processed_pages', 'unread_pages'))
    return bool(file.get('format') == 'pdf' and file.get('state') == 'read'
                and isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest)
                and isinstance(file.get('received_at'), str) and file['received_at']
                and file.get('large_pdf_algorithm') == ALGORITHM
                and type(file.get('large_pdf_max_bytes')) is int and file['large_pdf_max_bytes'] == MAX_BYTES
                and type(file.get('large_pdf_max_pages')) is int and file['large_pdf_max_pages'] == MAX_PAGES
                and file.get('large_pdf_source_sha256') == digest
                and file.get('large_pdf_received_at') == file['received_at']
                and file.get('algorithm') == egrn_coordinates.PDF_ALGORITHM
                and all(type(value) is int for value in (total, processed, unread))
                and total > 0 and processed == min(total, MAX_PAGES) and unread == total - processed
                and type(file.get('actual_size')) is int and 0 < file['actual_size'] <= MAX_BYTES
                and type(file.get('declared_size')) is int and file['declared_size'] == file['actual_size'])


def extract(path, digest):
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Некорректный SHA-256 крупного PDF')
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError('Сохранённый крупный PDF отсутствует или превышает 32 МБ')
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('SHA-256 или размер крупного PDF изменился')
    details, pages = municipal.pdf_text(raw, max_bytes=MAX_BYTES, max_pages=MAX_PAGES)
    tables = schemes.apply_page_limit(schemes.extract(pages), details['processed_pages'], details['unread_pages'])
    egrn, consumed = egrn_coordinates.extract(pages)
    unparsed = [number for number, body in pages
                if number in tables['unparsed_coordinate_pages'] and any(
                    schemes.PAIR.search(line) and (number, line.strip()) not in consumed for line in body.splitlines())]
    text = '\n'.join(f'UNIT {number}\n{body}' for number, body in pages).encode('utf-8')
    if len(text) > MAX_OUTPUT:
        raise ValueError('Текст крупного PDF превышает 4 МБ')
    result = {**details, 'algorithm': egrn_coordinates.PDF_ALGORITHM, 'unit': 'pdf_page',
              'tables': tables['tables'], 'unreadable_tables': tables['unreadable_tables'],
              'crs_status': tables['crs_status'], 'egrn_tables': egrn, 'unparsed_coordinate_pages': unparsed,
              'source_sha256': digest, 'large_pdf_algorithm': ALGORITHM,
              'large_pdf_max_bytes': MAX_BYTES, 'large_pdf_max_pages': MAX_PAGES,
              'page_range': [1, len(pages)], 'continued_text_sha256': hashlib.sha256(text).hexdigest(),
              'geometry_confirmed': False, 'georeferenced': False}
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > MAX_OUTPUT:
        raise ValueError('Результат чтения крупного PDF превышает 4 МБ')
    if not path.is_file() or path.stat().st_size != len(raw) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('Крупный PDF изменился во время чтения')
    store.atomic_write(path.with_suffix(TEXT_SUFFIX), text)
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        print(json.dumps(extract(Path(sys.argv[1]), sys.argv[2]), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': str(exc)[:500]}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
