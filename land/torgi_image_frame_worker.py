"""Isolated decoding of one exact cached MPO auxiliary frame; no network or OCR."""
import hashlib
import json
from pathlib import Path
import re
import sys

from . import image_frames, store

ALGORITHM = image_frames.ALGORITHM
MAX_OUTPUT = 64 * 1024
MAX_PNG = 16 * 1024 * 1024
SIDE = 2000


def extract(path, digest, fmt, index, folder, binding_id):
    if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
            or not isinstance(binding_id, str) or not re.fullmatch(r'[0-9a-f]{64}', binding_id)):
        raise ValueError('Некорректная привязка SHA-256 вспомогательного кадра')
    if not path.is_file() or path.stat().st_size > image_frames.MAX_BYTES:
        raise ValueError('Сохранённый MPO отсутствует или превышает 8 МБ')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('SHA-256 сохранённого MPO изменился')
    png, info = image_frames.preview(raw, fmt, index, SIDE)
    if len(png) > MAX_PNG:
        raise ValueError('Предпросмотр кадра MPO превышает 16 МБ')
    if (not path.is_file() or path.stat().st_size > image_frames.MAX_BYTES
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
        raise ValueError('Сохранённый MPO изменился во время декодирования')
    result = {**info, 'algorithm': ALGORITHM, 'source_sha256': digest,
              'source_format': fmt, 'binding_id': binding_id,
              'png_sha256': hashlib.sha256(png).hexdigest(),
              'processed_at': store.now(), 'ocr_read': False}
    serialized = json.dumps(result, ensure_ascii=False).encode('utf-8')
    if len(serialized) > MAX_OUTPUT:
        raise ValueError('Сведения вспомогательного кадра превышают 64 КБ')
    folder.mkdir(parents=True, exist_ok=True)
    store.atomic_write(folder / f'f{index}.png', png)
    store.atomic_write(folder / f'f{index}.json', serialized)
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        result = extract(Path(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4]),
                         Path(sys.argv[5]), sys.argv[6])
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': str(exc)[:500]}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
