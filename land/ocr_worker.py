"""Local PDF rendering and Windows OCR in an isolated, time-bounded process."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

ALGORITHM = 'winrt-dual-v1'
LONG_SIDES = (2000, 3000)


def engine():
    if sys.platform != 'win32':
        raise ValueError('OCR требует Windows и установленный русский пакет распознавания')
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    value = OcrEngine.try_create_from_language(Language('ru'))
    if value is None:
        raise ValueError('Русский язык OCR Windows недоступен')
    return value, {'name': 'Windows.Media.Ocr', 'language': value.recognizer_language.language_tag,
                   'languages': [x.language_tag for x in OcrEngine.available_recognizer_languages],
                   'max_image_dimension': OcrEngine.max_image_dimension, 'confidence_available': False}


async def recognize(ocr, path):
    from winrt.windows.storage import StorageFile, FileAccessMode
    from winrt.windows.graphics.imaging import BitmapDecoder
    stream = bitmap = None
    try:
        file = await StorageFile.get_file_from_path_async(str(path.resolve()))
        stream = await file.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        result = await ocr.recognize_async(bitmap)
        lines = [x.text for x in result.lines]
        text = '\n'.join(lines)
        if len(text) > 500000:
            raise ValueError('Распознанный текст превышает лимит')
        return {'text': text, 'line_count': len(lines), 'characters': len(text),
                'text_angle': result.text_angle, 'width': bitmap.pixel_width, 'height': bitmap.pixel_height}
    finally:
        if bitmap is not None:
            bitmap.close()
        if stream is not None:
            stream.close()


async def process(path, digest, number, folder, max_pdf_mib=8, max_pdf_pages=40):
    import pypdfium2 as pdfium
    ocr, info = engine()
    raw = path.read_bytes()
    if max_pdf_mib not in (8,16) or len(raw) > max_pdf_mib * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Размер или SHA-256 сохранённого PDF не соответствует источнику')
    doc = pdfium.PdfDocument(raw)
    try:
        if max_pdf_pages not in (40,120) or not 1 <= number <= min(len(doc), max_pdf_pages):
            raise ValueError(f'Страница вне разрешённых границ 1–{max_pdf_pages}')
        page = doc[number - 1]
        try:
            width, height = page.get_size()
            if min(width, height) <= 0:
                raise ValueError('Неверный размер страницы')
            views = []
            for index, side in enumerate(LONG_SIDES):
                if side > info['max_image_dimension']:
                    raise ValueError('Разрешение превышает лимит OCR Windows')
                bitmap = page.render(scale=side / max(width, height))
                try:
                    image = bitmap.to_pil()
                    image_path = folder / f'p{number}-v{index}.png'
                    try:
                        image.save(image_path)
                    finally:
                        image.close()
                finally:
                    bitmap.close()
                result = await asyncio.wait_for(recognize(ocr, image_path), timeout=20)
                result.update(view=index, image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest())
                views.append(result)
            return {'algorithm': ALGORITHM, 'source_sha256': digest, 'page': number, 'engine': info, 'views': views,
                    'source_format': 'pdf',
                    'processed_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        finally:
            page.close()
    finally:
        doc.close()


async def process_image(path, digest, fmt, folder):
    from .image_evidence import preview
    ocr, info = engine()
    raw = path.read_bytes()
    if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Размер или SHA-256 изображения не соответствует источнику')
    views = []
    for index, side in enumerate(LONG_SIDES):
        if side > info['max_image_dimension']:
            raise ValueError('Разрешение превышает лимит OCR Windows')
        png, _ = preview(raw, fmt, side)
        target = folder / f'p1-v{index}.png'
        target.write_bytes(png)
        result = await asyncio.wait_for(recognize(ocr, target), timeout=20)
        result.update(view=index, image_sha256=hashlib.sha256(png).hexdigest())
        views.append(result)
    return {'algorithm': ALGORITHM, 'source_sha256': digest, 'source_format': fmt, 'page': 1,
            'engine': info, 'views': views, 'processed_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--pdf', type=Path)
    parser.add_argument('--max-pdf-mib',type=int,choices=(8,16),default=8)
    parser.add_argument('--max-pdf-pages',type=int,choices=(40,120),default=40)
    parser.add_argument('--image', type=Path)
    parser.add_argument('--source-format', choices=('jpg','jpeg','png'))
    parser.add_argument('--sha256')
    parser.add_argument('--page', type=int)
    parser.add_argument('--folder', type=Path)
    args = parser.parse_args()
    try:
        if args.probe:
            _, info = engine()
            print(json.dumps(info, ensure_ascii=False))
        else:
            args.folder.mkdir(parents=True, exist_ok=True)
            result = asyncio.run(process_image(args.image, args.sha256, args.source_format, args.folder)
                                 if args.image else process(args.pdf, args.sha256, args.page, args.folder,args.max_pdf_mib,args.max_pdf_pages))
            target = args.folder / f'p{args.page}-{ALGORITHM}.json'
            target.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            print(json.dumps({'page': args.page, 'algorithm': ALGORITHM}))
    except Exception as exc:
        print(json.dumps({'error': str(exc)[:500]}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
