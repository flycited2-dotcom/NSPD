"""Local-only extended PDF text reading; inputs and resource limits are explicit."""
import hashlib,json,sys
from pathlib import Path
from . import municipal

ALGORITHM='municipal-local-pdf-v1'

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        path,digest=Path(sys.argv[1]),sys.argv[2]
        max_mib=int(sys.argv[3]) if len(sys.argv)>3 else 16
        if max_mib not in (16,32):raise ValueError('Недопустимый лимит PDF')
        if path.stat().st_size>max_mib*1024*1024:raise ValueError(f'Сохранённый PDF превышает {max_mib} МБ')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('SHA-256 PDF изменился')
        result,pages=municipal.pdf_text(raw,max_bytes=max_mib*1024*1024,max_pages=200 if max_mib==32 else 120)
        path.with_suffix('.continued.txt').write_text('\n'.join(f'PAGE {n}\n{t}' for n,t in pages),encoding='utf-8')
        marker={'large_pdf_algorithm':municipal.LARGE_PDF_ALGORITHM} if max_mib==32 else {'local_pdf_algorithm':ALGORITHM}
        print(json.dumps(dict(result,source_sha256=digest,**marker),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error':str(exc)[:500]},ensure_ascii=False));raise SystemExit(1)

if __name__=='__main__':main()
