"""Decode one explicitly selected auxiliary MPO frame with a separate scope.

The declared container frame count is not a claim that other frames were read.
Callers must bind the returned scope and preview to the original source hash.
"""
import io

from PIL import Image, ImageOps

from land import image_evidence

ALGORITHM = 'torgi-image-aux-frame-v1'
MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = image_evidence.MAX_PIXELS
MAX_SIDE = image_evidence.MAX_SIDE
MAX_SOURCE_FRAMES = image_evidence.MAX_SOURCE_FRAMES
SCOPE_FIELDS = ('algorithm', 'source_format', 'source_frame_count',
                'source_frame_index', 'complete_frame', 'complete_source')


def scope(info):
    """Return a strictly typed, explicit auxiliary-frame marker for a cache."""
    if not isinstance(info, dict):
        raise ValueError('Не получена область чтения вспомогательного кадра')
    marker = info.get('frame_scope', info)
    if not isinstance(marker, dict):
        raise ValueError('Не получена область чтения вспомогательного кадра')
    count, index = marker.get('source_frame_count'), marker.get('source_frame_index')
    expected = {'algorithm': ALGORITHM, 'source_format': 'MPO',
                'source_frame_count': count, 'source_frame_index': index,
                'complete_frame': True, 'complete_source': False}
    if (type(count) is not int or not 2 <= count <= MAX_SOURCE_FRAMES
            or type(index) is not int or not 1 <= index < count
            or any(type(marker.get(key)) is not type(value) or marker.get(key) != value
                   for key, value in expected.items())):
        raise ValueError('Область чтения не соответствует вспомогательному кадру MPO')
    return {key: marker[key] for key in SCOPE_FIELDS}


def preview(raw, fmt, index, side=2000):
    """Return a metadata-free PNG and scope of one non-primary MPO frame.

    ``index`` is zero-based and must be explicitly selected from 1 through 15.
    No other frame is decoded or marked complete by this function.
    """
    if (type(raw) not in (bytes, bytearray) or not raw or len(raw) > MAX_BYTES
            or type(fmt) is not str or fmt not in ('jpg', 'jpeg')):
        raise ValueError('Ожидался контейнер MPO в JPEG до 8 МБ')
    if type(index) is not int or not 1 <= index < MAX_SOURCE_FRAMES:
        raise ValueError('Нужен явный индекс вспомогательного кадра от 1 до 15')
    if type(side) is not int or not 1 <= side <= MAX_SIDE:
        raise ValueError('Размер предпросмотра должен быть целым от 1 до 10000')
    with io.BytesIO(raw) as stream:
        with Image.open(stream, formats=['JPEG']) as source:
            count = getattr(source, 'n_frames', 1)
            if (source.format != 'MPO' or type(count) is not int
                    or not 2 <= count <= MAX_SOURCE_FRAMES or index >= count):
                raise ValueError('Формат или число кадров MPO не соответствует выбранному кадру')
            source.seek(index)
            width, height = source.size
            if (min(width, height) < 1 or max(width, height) > MAX_SIDE
                    or width * height > MAX_PIXELS):
                raise ValueError('Размер вспомогательного кадра MPO превышает лимит')
            source.load()  # A corrupt selected frame must never become successful evidence.
            orientation = source.getexif().get(274, 1)
            with ImageOps.exif_transpose(source) as oriented:
                with oriented.convert('RGB') as image:
                    image.thumbnail((side, side), Image.Resampling.LANCZOS)
                    image.info.clear()
                    with io.BytesIO() as output:
                        image.save(output, format='PNG')
                        png = output.getvalue()
                    marker = {'algorithm': ALGORITHM, 'source_format': 'MPO',
                              'source_frame_count': count, 'source_frame_index': index,
                              'complete_frame': True, 'complete_source': False}
                    return png, {'frame_scope': scope(marker),
                                 'source_frame_count_basis': 'container_header',
                                 'primary_preview_algorithm': image_evidence.ALGORITHM,
                                 'source_width': width, 'source_height': height,
                                 'preview_width': image.width, 'preview_height': image.height,
                                 'orientation_applied': orientation in (2, 3, 4, 5, 6, 7, 8)}
