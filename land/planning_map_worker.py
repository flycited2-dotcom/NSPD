"""Isolated PDF map index and full-page Poppler rendering; no georeferencing."""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from . import municipal, planning_maps


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        mode, path, sha = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
        if path.stat().st_size > planning_maps.MAX_BYTES:
            raise ValueError('PDF карты превышает 32 МБ')
        raw = path.read_bytes()
        if not raw.startswith(b'%PDF-') or hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('Формат/SHA-256 PDF карты изменился')
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError('Зашифрованный PDF карты не читается')
        count = len(reader.pages)
        if mode == 'inspect':
            markers = [{'page': i + 1, 'keys': [key for key in ('/VP', '/LGIDict', '/Measure') if key in page]}
                       for i, page in enumerate(reader.pages[:200]) if any(key in page for key in ('/VP', '/LGIDict', '/Measure'))]
            metadata, pages = municipal.pdfium_text(raw, 200)
            result = dict(metadata, **planning_maps.text_index(pages, markers), algorithm=planning_maps.ALGORITHM)
        elif mode == 'markers':
            if not 1 <= count <= planning_maps.planning_regulations.MAX_PAGES:
                raise ValueError('Число страниц PDF карты вне предела 1–2000')
            markers = [{'page': i + 1, 'keys': [key for key in ('/VP', '/LGIDict', '/Measure') if key in page]}
                       for i, page in enumerate(reader.pages) if any(key in page for key in ('/VP', '/LGIDict', '/Measure'))]
            result = {'algorithm': planning_maps.FULL_ALGORITHM, 'total_pages': count,
                      'markers_processed_pages': count, 'standard_geopdf_markers': markers}
        elif mode == 'render':
            page, folder = int(sys.argv[4]), Path(sys.argv[5])
            if not 1 <= page <= min(count, planning_maps.planning_regulations.MAX_PAGES):
                raise ValueError('Страница вне предела 1–2000')
            binary = shutil.which('pdftoppm')
            if not binary:
                raise ValueError('Poppler pdftoppm не найден; карта не отрисована')
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f'p{page}-{planning_maps.RENDER_ALGORITHM}.png'
            prefix = target.with_suffix('.partial')
            temporary = Path(str(prefix) + '.png')
            opts = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
            subprocess.run([binary, '-f', str(page), '-singlefile', '-scale-to', '2400', '-png', str(path), str(prefix)],
                           check=True, capture_output=True, timeout=30, **opts)
            if temporary.stat().st_size > 16 * 1024 * 1024:
                raise ValueError('Изображение карты превышает 16 МБ')
            from PIL import Image
            with Image.open(temporary) as image:
                if image.format != 'PNG' or max(image.size) > 2400 or min(image.size) < 1:
                    raise ValueError('Неверный формат/размер карты')
                width, height = image.size
                image.verify()
            temporary.replace(target)
            from .store import now
            result = {'page': page, 'algorithm': planning_maps.RENDER_ALGORITHM, 'image_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                      'width': width, 'height': height, 'rendered_at': now()}
        else:
            raise ValueError('Недопустимая операция карты')
        result.update(source_sha256=sha)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': str(exc)[:500]}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
