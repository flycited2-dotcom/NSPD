"""Local-only extended PDF text reading; inputs and resource limits are explicit."""
import hashlib,json,sys
from pathlib import Path
from . import municipal

ALGORITHM='municipal-local-pdf-v1'

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        path,digest=Path(sys.argv[1]),sys.argv[2]
        if path.stat().st_size>16*1024*1024:raise ValueError('Сохранённый PDF превышает 16 МБ')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('SHA-256 PDF изменился')
        result,pages=municipal.pdf_text(raw,max_bytes=16*1024*1024,max_pages=120)
        path.with_suffix('.continued.txt').write_text('\n'.join(f'PAGE {n}\n{t}' for n,t in pages),encoding='utf-8')
        print(json.dumps(dict(result,source_sha256=digest,local_pdf_algorithm=ALGORITHM),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error':str(exc)[:500]},ensure_ascii=False));raise SystemExit(1)

if __name__=='__main__':main()
