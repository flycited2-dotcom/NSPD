"""Read a bounded prefix of one exact cached Torgi PDF, without HTTP or OCR."""
import hashlib
import json
from pathlib import Path
import re
import sys

from . import store, torgi_file_worker
from .egrn_coordinates import PDF_ALGORITHM

ALGORITHM = 'torgi-local-pdf-v1'
MAX_PAGES = 120
MAX_BYTES = 16 * 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024


def extract(path, digest):
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Некорректный SHA-256 сохранённого PDF')
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Сохранённый PDF отсутствует или превышает 16 МБ')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('SHA-256 сохранённого PDF изменился')
    details, pages = torgi_file_worker.extract(raw, 'pdf', max_pages=MAX_PAGES)
    text = '\n'.join(f'UNIT {number}\n{body}' for number, body in pages).encode('utf-8')
    if len(text) > MAX_OUTPUT:
        raise ValueError('Текст локального продолжения PDF превышает 4 МБ')
    result = {**details, 'algorithm': PDF_ALGORITHM, 'source_sha256': digest,
              'local_pdf_algorithm': ALGORITHM, 'local_pdf_max_pages': MAX_PAGES,
              'page_range': [1, len(pages)], 'continued_text_sha256': hashlib.sha256(text).hexdigest()}
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > MAX_OUTPUT:
        raise ValueError('Результат локального продолжения PDF превышает 4 МБ')
    store.atomic_write(path.with_suffix('.continued.txt'), text)
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        result = extract(Path(sys.argv[1]), sys.argv[2])
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': str(exc)[:500]}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
