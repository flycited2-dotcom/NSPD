import copy
import io
import struct

import pytest
from PIL import Image

from land import image_evidence, image_frames


def valid_mpo_offsets(raw):
    # Pillow's MPO writer in the bundled runtime accumulates offsets incorrectly
    # after two frames. Keep the fixture a real, correctly indexed MPO container.
    starts = []
    position = raw.find(b'\xff\xd8\xff')
    while position >= 0:
        starts.append(position)
        position = raw.find(b'\xff\xd8\xff', position + 3)
    tiff = raw.index(b'MPF\0') + 4
    assert raw[tiff:tiff + 4] == b'II\x2a\0'
    directory = tiff + struct.unpack_from('<I', raw, tiff + 4)[0]
    entries = struct.unpack_from('<H', raw, directory)[0]
    fixed = bytearray(raw)
    for offset in range(directory + 2, directory + 2 + 12 * entries, 12):
        if struct.unpack_from('<H', raw, offset)[0] == 0xB002:
            table = tiff + struct.unpack_from('<I', raw, offset + 8)[0]
            for index, start in enumerate(starts):
                end = starts[index + 1] if index + 1 < len(starts) else len(raw)
                struct.pack_into('<II', fixed, table + 16 * index + 4,
                                 end - start, 0 if index == 0 else start - tiff)
            return bytes(fixed)
    raise AssertionError('Fixture does not contain an MPF index')


def source_bytes(fmt='MPO', count=2, size=(80, 50), auxiliary_size=None, orientation=None):
    frames = [Image.new('RGB', size, 'red')]
    frames.extend(Image.new('RGB', auxiliary_size or size, 'blue' if index == 1 else 'green')
                  for index in range(1, count))
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
        exif[270] = 'private auxiliary description'
        exif[315] = 'private auxiliary author'
        exif[34853] = {1: 'N', 2: (44.0, 59.0, 0.0), 3: 'E', 4: (34.0, 12.0, 0.0)}
    try:
        with io.BytesIO() as output:
            options = {'save_all': True, 'append_images': frames[1:]} if count > 1 else {}
            if exif:
                options['exif'] = exif
            frames[0].save(output, format=fmt, **options)
            raw = output.getvalue()
            return valid_mpo_offsets(raw) if fmt == 'MPO' and count > 1 else raw
    finally:
        for frame in frames:
            frame.close()


@pytest.mark.parametrize('fmt', ['jpg', 'jpeg'])
def test_real_mpo_selects_blue_auxiliary_without_claiming_whole_source(fmt):
    raw = source_bytes()
    png, info = image_frames.preview(raw, fmt, 1)
    marker = image_frames.scope(info)
    assert marker == {'algorithm': image_frames.ALGORITHM, 'source_format': 'MPO',
                      'source_frame_count': 2, 'source_frame_index': 1,
                      'complete_frame': True, 'complete_source': False}
    assert info['source_frame_count_basis'] == 'container_header'
    assert info['primary_preview_algorithm'] == image_evidence.ALGORITHM
    assert image_frames.ALGORITHM != image_evidence.ALGORITHM
    with Image.open(io.BytesIO(png)) as preview:
        assert preview.format == 'PNG' and getattr(preview, 'n_frames', 1) == 1
        red, green, blue = preview.getpixel((1, 1))
        assert blue > 240 and red < 10 and green < 10


def test_selected_frame_orientation_is_applied_and_private_metadata_removed():
    png, info = image_frames.preview(source_bytes(orientation=6), 'jpg', 1, 60)
    assert info['orientation_applied']
    assert (info['source_width'], info['source_height']) == (80, 50)
    assert info['preview_height'] == 60 and abs(info['preview_width'] - 37.5) <= 1
    assert b'private auxiliary' not in png
    with Image.open(io.BytesIO(png)) as preview:
        assert not preview.getexif() and not preview.info
        assert preview.getpixel((1, 1))[2] > 240


@pytest.mark.parametrize('index', [0, -1, 16, 17, True, False, 1.0, '1', None])
def test_explicit_auxiliary_index_has_strict_integer_bounds(index):
    with pytest.raises(ValueError, match='индекс'):
        image_frames.preview(source_bytes(), 'jpg', index)


@pytest.mark.parametrize('side', [0, -1, 10001, True, False, 60.0, '60', None])
def test_preview_side_has_strict_integer_bounds(side):
    with pytest.raises(ValueError, match='Размер предпросмотра'):
        image_frames.preview(source_bytes(), 'jpg', 1, side)


def test_last_allowed_frame_and_preview_side_bounds_are_valid():
    png, info = image_frames.preview(source_bytes(count=16), 'jpg', 15, 1)
    assert image_frames.scope(info)['source_frame_index'] == 15
    assert not image_frames.scope(info)['complete_source']
    with Image.open(io.BytesIO(png)) as preview:
        assert max(preview.size) == 1
    png, info = image_frames.preview(source_bytes(), 'jpeg', 1, 10000)
    assert (info['preview_width'], info['preview_height']) == (80, 50)


@pytest.mark.parametrize('raw, fmt', [(b'x' * (8 * 1024 * 1024 + 1), 'jpg'),
                                    ('invalid', 'jpg'), (b'', 'jpg'),
                                    (b'invalid', 'png'), (b'invalid', True)],
                         ids=['oversized', 'wrong-raw-type', 'empty', 'wrong-extension', 'wrong-extension-type'])
def test_input_type_extension_and_byte_limit_are_bounded(raw, fmt):
    with pytest.raises(ValueError, match='MPO в JPEG'):
        image_frames.preview(raw, fmt, 1)


def test_declared_frame_count_and_selected_frame_availability_are_bounded():
    for raw, index in ((source_bytes(count=17), 1), (source_bytes(), 2)):
        with pytest.raises(ValueError, match='число кадров'):
            image_frames.preview(raw, 'jpg', index)


@pytest.mark.parametrize('auxiliary_size', [(10001, 1), (5100, 5100)])
def test_selected_frame_dimensions_are_checked_instead_of_primary_only(auxiliary_size):
    raw = source_bytes(auxiliary_size=auxiliary_size)
    # The ordinary primary decoder still has a bounded, small primary frame.
    _, primary = image_evidence.preview(raw, 'jpg')
    assert primary['source_width'] == 80 and primary['source_height'] == 50
    with pytest.raises(ValueError, match='вспомогательного кадра MPO превышает'):
        image_frames.preview(raw, 'jpg', 1)


@pytest.mark.parametrize('fmt, count', [('JPEG', 1), ('PNG', 1), ('PNG', 2)])
def test_single_frame_jpeg_png_and_apng_are_not_auxiliary_mpo(fmt, count):
    with pytest.raises((ValueError, OSError)):
        image_frames.preview(source_bytes(fmt=fmt, count=count), 'jpg', 1)


@pytest.mark.parametrize('truncation', ['header', 'pixels'])
def test_corrupt_selected_frame_is_rejected_without_relabeling_primary(truncation):
    raw = source_bytes()
    with Image.open(io.BytesIO(raw)) as source:
        source.seek(1)
        auxiliary_offset = source.offset
    broken = raw[:auxiliary_offset + 32] if truncation == 'header' else raw[:-2]
    _, primary = image_evidence.preview(broken, 'jpg')
    assert primary['preview_frame_index'] == 0 and not primary['complete_source']
    with pytest.raises((ValueError, OSError, SyntaxError)):
        image_frames.preview(broken, 'jpg', 1)


def test_unselected_corrupt_frame_is_not_decoded_or_marked_complete():
    raw = source_bytes(count=3)
    with Image.open(io.BytesIO(raw)) as source:
        source.seek(2)
        third_offset = source.offset
    broken = raw[:third_offset + 32]
    _, info = image_frames.preview(broken, 'jpg', 1)
    assert image_frames.scope(info)['source_frame_count'] == 3
    assert not image_frames.scope(info)['complete_source']
    with pytest.raises((ValueError, OSError, SyntaxError)):
        image_frames.preview(broken, 'jpg', 2)


@pytest.mark.parametrize('change', [{'algorithm': image_evidence.ALGORITHM}, {'source_format': 'JPEG'},
                                  {'source_frame_count': True}, {'source_frame_count': 2.0},
                                  {'source_frame_count': 17}, {'source_frame_index': 0},
                                  {'source_frame_index': True}, {'source_frame_index': 1.0},
                                  {'source_frame_index': 2}, {'complete_frame': 1},
                                  {'complete_frame': False}, {'complete_source': 0},
                                  {'complete_source': True}])
def test_scope_rejects_primary_ambiguous_types_and_whole_container_claims(change):
    _, info = image_frames.preview(source_bytes(), 'jpg', 1)
    changed = copy.deepcopy(info)
    changed['frame_scope'].update(change)
    with pytest.raises(ValueError, match='Область чтения'):
        image_frames.scope(changed)


@pytest.mark.parametrize('marker', [None, [], {'frame_scope': None}, {'frame_scope': []}])
def test_scope_requires_structured_frame_marker(marker):
    with pytest.raises(ValueError, match='область чтения'):
        image_frames.scope(marker)


def test_primary_scope_cannot_be_used_as_auxiliary_cache_marker():
    _, primary = image_evidence.preview(source_bytes(), 'jpg')
    with pytest.raises(ValueError, match='Область чтения'):
        image_frames.scope(image_evidence.scope(primary))
