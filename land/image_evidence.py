"""Decode the first bounded image frame; previews never expose source metadata."""
import io
from PIL import Image, ImageOps

FORMATS = {'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG'}
ALGORITHM = 'torgi-image-preview-v2'
MAX_PIXELS = 25000000
MAX_SIDE = 10000
MAX_SOURCE_FRAMES = 16
SCOPE_FIELDS = ('preview_algorithm', 'source_format', 'source_frame_count', 'preview_frame_index',
                'processed_frame_count', 'unprocessed_frame_count', 'complete_frame', 'complete_source')


def scope(info):
    """A source-bound caller can use this exact first-frame marker for OCR caches."""
    if not isinstance(info, dict):
        raise ValueError('Не получены сведения области чтения изображения')
    frames = info.get('source_frame_count')
    expected = {'preview_algorithm': ALGORITHM, 'source_format': info.get('source_format'),
                'source_frame_count': frames, 'preview_frame_index': 0, 'processed_frame_count': 1,
                'unprocessed_frame_count': frames - 1 if type(frames) is int else None,
                'complete_frame': True, 'complete_source': frames == 1}
    if (type(frames) is not int or not 1 <= frames <= MAX_SOURCE_FRAMES
            or expected['source_format'] not in ('JPEG', 'MPO', 'PNG')
            or (expected['source_format'] != 'MPO' and frames != 1)
            or any(type(info.get(key)) is not type(value) or info.get(key) != value
                   for key, value in expected.items())):
        raise ValueError('Область чтения изображения не соответствует первому кадру источника')
    return {key: info[key] for key in SCOPE_FIELDS}


def preview(raw, fmt, side=2000):
    if fmt not in FORMATS or len(raw) > 8 * 1024 * 1024:
        raise ValueError('Ожидалось изображение JPEG/PNG до 8 МБ')
    with Image.open(io.BytesIO(raw), formats=[FORMATS[fmt]]) as source:
        frames = getattr(source, 'n_frames', 1)
        allowed = ('JPEG', 'MPO') if fmt in ('jpg', 'jpeg') else ('PNG',)
        if (source.format not in allowed or type(frames) is not int or not 1 <= frames <= MAX_SOURCE_FRAMES
                or (source.format != 'MPO' and frames != 1)
                or min(source.size) < 1 or max(source.size) > MAX_SIDE or source.width * source.height > MAX_PIXELS):
            raise ValueError('Формат, число кадров или размер изображения превышает лимит')
        # An MPO container may include auxiliary/depth frames. Only the primary
        # frame is decoded; its declared frame count does not validate the rest.
        source.seek(0)
        source.load()  # truncated/corrupt pixels cannot become a successful preview
        orientation = source.getexif().get(274, 1)
        with ImageOps.exif_transpose(source) as oriented:
            with oriented.convert('RGB') as image:
                image.thumbnail((side, side), Image.Resampling.LANCZOS)
                image.info.clear()  # do not expose EXIF, coordinates or other embedded metadata
                output = io.BytesIO()
                image.save(output, format='PNG')
                return output.getvalue(), {'preview_algorithm': ALGORITHM,
                    'source_format': source.format, 'source_frame_count': frames,
                    'source_frame_count_basis': 'container_header', 'preview_frame_index': 0,
                    'processed_frame_count': 1, 'unprocessed_frame_count': frames - 1,
                    'complete_source': frames == 1, 'source_width': source.width,
                    'source_height': source.height, 'preview_width': image.width, 'preview_height': image.height,
                    'orientation_applied': orientation in (2, 3, 4, 5, 6, 7, 8), 'complete_frame': True}
