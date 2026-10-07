"""Isolated, bounded reading of an exact cached PDF window; no network or OCR."""
import hashlib
import io
import json
import sys
from pathlib import Path

ALGORITHM = 'pzz-page-text-v1'
MAX_BYTES = 32 * 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
WINDOW = 40
MAX_PAGES = 2000


def extract(path, sha, start):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('PDF превышает 32 МБ')
    raw = path.read_bytes()
    if not raw.startswith(b'%PDF-') or hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError('Формат/SHA-256 PDF изменился')
    from pypdf import PdfReader
    if PdfReader(io.BytesIO(raw)).is_encrypted:
        raise ValueError('Зашифрованный PDF не читается')
    import pypdfium2 as pdfium
    pages = []
    with pdfium.PdfDocument(raw) as document:
        total = len(document)
        if not 1 <= total <= MAX_PAGES or not 1 <= start <= total:
            raise ValueError('Число страниц или начало порции вне пределов')
        for index in range(start - 1, min(total, start - 1 + WINDOW)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    if textpage.count_chars() > 500000:
                        raise ValueError('Текст страницы превышает лимит')
                    text = textpage.get_text_range(errors='strict').replace('\r\n', '\n').replace('\r', '\n')
                    if len(text) > 500000:
                        raise ValueError('Текст страницы превышает лимит')
                finally:
                    textpage.close()
            finally:
                page.close()
            pages.append({'page': index + 1, 'text': text})
    result = {'algorithm': ALGORITHM, 'source_sha256': sha, 'total_pages': total,
              'start': start, 'pages': pages}
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > MAX_OUTPUT:
        raise ValueError('Текст порции превышает 4 МБ')
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        print(json.dumps(extract(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': str(exc)[:500]}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
