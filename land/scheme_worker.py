"""Bounded child process for PDF text table extraction."""
import hashlib
import json
from pathlib import Path
import sys
from . import municipal, schemes


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        path, digest = Path(sys.argv[1]), sys.argv[2]
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('SHA-256 PDF изменился')
        extended=len(sys.argv)>3 and sys.argv[3]=='extended'
        details, pages = municipal.pdf_text(raw,max_bytes=(16 if extended else 8)*1024*1024,max_pages=120 if extended else 40)
        result = schemes.apply_page_limit(schemes.extract(pages), details['processed_pages'], details['unread_pages'])
        result.update(source_sha256=digest, processed_pages=details['processed_pages'], total_pages=details['total_pages'],
                      unread_pages=details['unread_pages'], image_or_sparse_pages=details['image_or_sparse_pages'])
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error':str(exc)[:500]}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
