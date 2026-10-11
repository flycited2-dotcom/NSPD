import copy
import hashlib
import io

import pytest
from PIL import Image

from land import image_evidence as images, torgi_file_worker as reader


def raster(fmt='MPO', count=2, size=(80, 50), orientation=None):
    frames = [Image.new('RGB', size, 'red')]
    frames.extend(Image.new('RGB', size, 'blue') for _ in range(count - 1))
    output = io.BytesIO()
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
        exif[270] = 'private description to remove'
        exif[315] = 'private author to remove'
        exif[34853] = {1: 'N', 2: (44.0, 59.0, 0.0), 3: 'E', 4: (34.0, 12.0, 0.0)}
    try:
        options = {'save_all': True, 'append_images': frames[1:]} if count > 1 else {}
        if exif:
            options['exif'] = exif
        frames[0].save(output, format=fmt, **options)
        return output.getvalue()
    finally:
        for frame in frames:
            frame.close()


@pytest.mark.parametrize('fmt', ['jpg', 'jpeg'])
def test_real_two_frame_mpo_decodes_primary_and_keeps_auxiliary_unread(fmt):
    raw = raster()
    with Image.open(io.BytesIO(raw)) as source:
        assert source.format == 'MPO' and source.n_frames == 2
    png, info = images.preview(raw, fmt)
    assert images.scope(info) == {'preview_algorithm': 'torgi-image-preview-v2', 'source_format': 'MPO',
                                 'source_frame_count': 2, 'preview_frame_index': 0, 'processed_frame_count': 1,
                                 'unprocessed_frame_count': 1, 'complete_frame': True, 'complete_source': False}
    assert info['source_frame_count_basis'] == 'container_header'
    with Image.open(io.BytesIO(png)) as preview:
        assert preview.format == 'PNG' and getattr(preview, 'n_frames', 1) == 1
        red, green, blue = preview.getpixel((1, 1))
        assert red > 240 and green < 10 and blue < 10  # The blue auxiliary frame was not selected.


def test_mpo_orientation_and_private_metadata_are_removed_from_preview():
    png, info = images.preview(raster(orientation=6), 'jpg', 60)
    assert info['orientation_applied'] and info['source_width'] == 80 and info['source_height'] == 50
    assert info['preview_height'] == 60 and abs(info['preview_width'] - 37.5) <= 1
    assert b'private description' not in png and b'private author' not in png
    with Image.open(io.BytesIO(png)) as preview:
        assert not preview.getexif() and not preview.info


@pytest.mark.parametrize('fmt, extension', [('JPEG', 'jpg'), ('PNG', 'png')])
def test_single_frame_sources_are_complete_frames_and_complete_sources(fmt, extension):
    _, info = images.preview(raster(fmt=fmt, count=1), extension)
    marker = images.scope(info)
    assert marker['source_frame_count'] == marker['processed_frame_count'] == 1
    assert marker['unprocessed_frame_count'] == marker['preview_frame_index'] == 0
    assert marker['complete_frame'] and marker['complete_source']


def test_mpo_reader_makes_no_text_or_geometry_claim():
    raw = raster()
    evidence, pages = reader.extract(raw, 'jpeg')
    png, _ = images.preview(raw, 'jpeg')
    assert evidence['unit'] == 'image' and evidence['image']['preview_sha256'] == hashlib.sha256(png).hexdigest()
    assert evidence['image']['processed_frame_count'] == 1 and not evidence['image']['complete_source']
    assert pages == evidence['mentions'] == evidence['tables'] == []
    assert not evidence['geometry_confirmed'] and not evidence['georeferenced'] and not evidence['text_layer_complete']


def test_primary_frame_bounds_and_declared_frame_count_are_enforced():
    _, info = images.preview(raster(count=16), 'jpg')
    assert images.scope(info)['unprocessed_frame_count'] == 15
    for raw in (raster(count=17), raster(size=(10001, 1)), raster(size=(5100, 5100)), b'X' * (8 * 1024 * 1024 + 1)):
        with pytest.raises((ValueError, OSError)):
            images.preview(raw, 'jpg')


def test_other_multi_frame_containers_and_corrupt_primary_are_rejected():
    with pytest.raises(ValueError):
        images.preview(raster(fmt='PNG'), 'png')
    with pytest.raises((ValueError, OSError)):
        images.preview(raster()[:100], 'jpg')
    with pytest.raises((ValueError, OSError)):
        images.preview(raster(fmt='PNG', count=1), 'jpg')


@pytest.mark.parametrize('change', [{'preview_algorithm': 'torgi-image-preview-v1'}, {'preview_frame_index': 1},
                                  {'preview_frame_index': False}, {'source_frame_count': True},
                                  {'source_frame_count': 17}, {'processed_frame_count': 2},
                                  {'unprocessed_frame_count': 0}, {'complete_source': True},
                                  {'complete_frame': False}, {'source_format': 'PNG'}])
def test_scope_marker_rejects_changed_or_ambiguous_frame_claims(change):
    _, original = images.preview(raster(), 'jpg')
    info = copy.deepcopy(original)
    info.update(change)
    with pytest.raises(ValueError, match='Область чтения'):
        images.scope(info)
