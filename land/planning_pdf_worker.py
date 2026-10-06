"""Isolated text evidence; no OCR or assignment of land rights/territorial zones."""
import hashlib
import json
import sys
from pathlib import Path
from . import municipal, planning_watch


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        path, sha = Path(sys.argv[1]), sys.argv[2]
        if path.stat().st_size > planning_watch.MAX_BYTES:
            raise ValueError('PDF превышает 32 МБ')
        raw = path.read_bytes()
        if not raw.startswith(b'%PDF-') or hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('Формат/SHA-256 PDF изменился')
        from pypdf import PdfReader
        import io
        if PdfReader(io.BytesIO(raw)).is_encrypted:
            raise ValueError('Зашифрованный PDF не читается')
        result, pages = municipal.pdfium_text(raw, 200)
        path.with_suffix('.txt').write_text('\n'.join(f'PAGE {n}\n{t}' for n, t in pages), encoding='utf-8')
        print(json.dumps(dict(result, **planning_watch.text_evidence(pages), algorithm=planning_watch.ALGORITHM, source_sha256=sha), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': str(exc)[:500]}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
