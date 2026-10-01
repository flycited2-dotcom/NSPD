"""Decode bounded source images and produce complete, metadata-free previews."""
import io
from PIL import Image, ImageOps

FORMATS = {'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG'}
ALGORITHM = 'torgi-image-preview-v1'
MAX_PIXELS = 25000000
MAX_SIDE = 10000


def preview(raw, fmt, side=2000):
    if fmt not in FORMATS or len(raw) > 8 * 1024 * 1024:
        raise ValueError('Ожидалось изображение JPEG/PNG до 8 МБ')
    with Image.open(io.BytesIO(raw), formats=[FORMATS[fmt]]) as source:
        if (source.format != FORMATS[fmt] or getattr(source, 'n_frames', 1) != 1
                or min(source.size) < 1 or max(source.size) > MAX_SIDE or source.width * source.height > MAX_PIXELS):
            raise ValueError('Формат, число кадров или размер изображения превышает лимит')
        source.load()  # truncated/corrupt pixels cannot become a successful preview
        orientation = source.getexif().get(274, 1)
        with ImageOps.exif_transpose(source) as oriented:
            with oriented.convert('RGB') as image:
                image.thumbnail((side, side), Image.Resampling.LANCZOS)
                image.info.clear()  # do not expose EXIF, coordinates or other embedded metadata
                output = io.BytesIO()
                image.save(output, format='PNG')
                return output.getvalue(), {'source_format': source.format, 'source_width': source.width,
                    'source_height': source.height, 'preview_width': image.width, 'preview_height': image.height,
                    'orientation_applied': orientation in (2, 3, 4, 5, 6, 7, 8), 'complete_frame': True}
