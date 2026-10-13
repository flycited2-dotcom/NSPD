"""Sanitized projection fixture mirrors the observed, namespace-free XML shape."""
import hashlib
import json
from xml.sax.saxutils import escape

import pytest
from shapely.errors import GEOSException

from land import egrn_xml


PROJECTED = [
    ('5026078.18', '5205201.02'), ('5026078.21', '5205202.12'),
    ('5026079.53', '5205254.45'), ('5026077.53', '5205254.45'),
    ('5026053.97', '5205254.46'), ('5026054.71', '5205251.19'),
    ('5026053.52', '5205201.38'), ('5026078.18', '5205201.02'),
]
SQUARE = [('0', '0'), ('10', '0'), ('10', '10'), ('0', '10'), ('0', '0')]


def contour(points=PROJECTED, crs='МСК-63, зона 5', metadata=True):
    ordinates = ''.join(
        '<ordinate><x>'+escape(x)+'</x><y>'+escape(y)+'</y>'
        + ('<ord_nmb>'+str(index+1)+'</ord_nmb><num_geopoint>0</num_geopoint>'
           '<delta_geopoint>0.10</delta_geopoint>' if metadata else '') + '</ordinate>'
        for index, (x, y) in enumerate(points))
    return ('<contour><number_pp>1</number_pp><cad_number>99:99:1234567:8</cad_number>'
            '<entity_spatial><sk_id>'+escape(crs)+'</sk_id><spatials_elements><spatial_element>'
            '<ordinates>'+ordinates+'</ordinates></spatial_element></spatials_elements>'
            '</entity_spatial></contour>')


def document(contours=None, extra='', declaration='<?xml version="1.0" encoding="UTF-8"?>'):
    if contours is None:
        contours = contour()
    return (declaration+'<extract_about_property_land>'+extra+'<land_record>'
            '<object><common_data><cad_number>99:99:1234567:8</cad_number>'
            '<quarter_cad_number>99:99:1234567</quarter_cad_number>'
            '<type><code>002001001000</code><value>Синтетический участок</value></type>'
            '</common_data><subtype><code>001</code></subtype></object>'
            '<contours_location><contours>'+contours+'</contours></contours_location>'
            '</land_record></extract_about_property_land>').encode('utf-8')


def test_projected_source_is_whitelisted_without_transform_or_repair():
    raw = document()
    result = egrn_xml.extract(raw)
    assert result['algorithm'] == 'egrn-xml-whitelist-v1'
    assert result['source_sha256'] == hashlib.sha256(raw).hexdigest()
    assert result['source_bytes'] == len(raw)
    assert result['cadastral_number'] == '99:99:1234567:8'
    assert result['received_contour_count'] == result['accepted_contour_count'] == 1
    assert result['received_point_count'] == result['accepted_point_count'] == 8
    assert result['omitted_contour_count'] == 0
    assert result['errors'] == []
    assert result['all_contours_accepted'] is True
    item = result['contours'][0]
    assert item['source_crs_raw'] == 'МСК-63, зона 5'
    assert item['coordinate_order'] == 'source_x_y'
    assert item['raw_points_xy'] == [list(point) for point in PROJECTED]
    assert item['points_xy'] == [[float(x), float(y)] for x, y in PROJECTED]
    assert item['bounds_xy'] == [5026053.52, 5205201.02, 5026079.53, 5205254.46]
    assert all(type(value) is float for value in item['bounds_xy'])
    assert item['point_count'] == 8 and item['explicitly_closed'] is True
    for key in ('complete_source', 'coordinate_system_confirmed', 'georeferenced',
                'geometry_confirmed', 'rights_confirmed', 'free_land_confirmed',
                'legal_claims_confirmed'):
        assert result[key] is False
    assert item['coordinate_system_confirmed'] is False
    assert result['omitted_node_count'] > 0


def test_privacy_does_not_copy_nonwhitelisted_text_fields_or_attributes():
    marker = 'SYNTHETIC_PRIVATE_NEVER_COPY'
    extra = ('<details_request><address>'+marker+'</address></details_request>'
             '<right_records><owner>'+marker+'</owner><cad_number>11:11:1111111:1</cad_number>'
             '</right_records><recipient_statement><recipient>'+marker+'</recipient>'
             '</recipient_statement>')
    result = egrn_xml.extract(document(extra=extra))
    output = json.dumps(result, ensure_ascii=False)
    assert marker not in output
    assert '11:11:1111111:1' not in output
    assert all(key not in output for key in ('owner', 'address', 'recipient', 'right_records'))
    assert result['omitted_node_count'] >= 8


def test_decimal_text_and_source_order_are_preserved_exactly():
    points = [(' +000.000 ', '0.00'), ('10.0000', '+000.0'), ('10', '10.0'),
              ('0.000', '+10'), ('0', '-0.00')]
    result = egrn_xml.extract(document(contour(points, 'UNKNOWN LOCAL SYSTEM')))
    item = result['contours'][0]
    assert item['raw_points_xy'] == [list(point) for point in points]
    assert item['points_xy'] == [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, -0.0]]
    assert item['source_crs_raw'] == 'UNKNOWN LOCAL SYSTEM'
    assert item['coordinate_system_confirmed'] is False


@pytest.mark.parametrize('points,code', [
    (SQUARE[:-1], 'unclosed_contour'),
    ([('0', '0'), ('10', '10'), ('0', '10'), ('10', '0'), ('0', '0')], 'invalid_contour'),
    ([('0', '0'), ('1', '0'), ('0', '0')], 'insufficient_points'),
    ([('0', '0'), ('1', '0'), ('2', '0'), ('0', '0')], 'invalid_contour'),
    ([('NaN', '0')]+SQUARE[1:], 'invalid_coordinate'),
    ([('Infinity', '0')]+SQUARE[1:], 'invalid_coordinate'),
    ([('1e309', '0')]+SQUARE[1:], 'invalid_coordinate'),
    ([('1000000001', '0')]+SQUARE[1:], 'invalid_coordinate'),
    ([('PRIVATE_SENTINEL', '0')]+SQUARE[1:], 'invalid_coordinate'),
])
def test_invalid_contours_are_omitted_without_repair_or_raw_error_echo(points, code):
    result = egrn_xml.extract(document(contour(points)))
    assert result['contours'] == []
    assert result['errors'] == [{'contour_index': 0, 'code': code}]
    assert result['received_contour_count'] == result['omitted_contour_count'] == 1
    assert result['accepted_contour_count'] == result['accepted_point_count'] == 0
    assert result['all_contours_accepted'] is False
    assert result['geometry_confirmed'] is False
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)


def test_float_rounding_cannot_close_an_unclosed_decimal_ring():
    points = [('5000000.0000000000001', '0'), ('5000010', '0'),
              ('5000010', '10'), ('5000000', '10'), ('5000000.0000000000002', '0')]
    assert float(points[0][0]) == float(points[-1][0])
    result = egrn_xml.extract(document(contour(points)))
    assert result['errors'] == [{'contour_index': 0, 'code': 'unclosed_contour'}]


@pytest.mark.parametrize('replacement', [
    '<ord_nmb>2</ord_nmb>', '<ord_nmb>0</ord_nmb>', '<ord_nmb>-1</ord_nmb>',
    '<ord_nmb>1.0</ord_nmb>', '<ord_nmb>PRIVATE_SENTINEL</ord_nmb>',
    '<ord_nmb>1</ord_nmb><ord_nmb>1</ord_nmb>',
    '<ord_nmb><number>1</number></ord_nmb>', '',
])
def test_conflicting_or_partial_point_order_is_omitted_without_sorting(replacement):
    raw = document(contour(SQUARE)).replace(b'<ord_nmb>1</ord_nmb>', replacement.encode(), 1)
    result = egrn_xml.extract(raw)
    assert result['contours'] == []
    assert result['errors'] == [{'contour_index': 0, 'code': 'invalid_point_order'}]
    assert result['accepted_point_count'] == 0
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)


def test_point_order_accepts_exact_document_order_or_no_ordinal_metadata():
    raw = document(contour(SQUARE)).replace(b'<ord_nmb>1</ord_nmb>', b'<ord_nmb> +001 </ord_nmb>', 1)
    assert egrn_xml.extract(raw)['accepted_contour_count'] == 1
    assert egrn_xml.extract(document(contour(SQUARE, metadata=False)))['accepted_contour_count'] == 1


def test_conflicting_point_order_does_not_discard_an_independent_valid_contour():
    bad = contour(SQUARE).replace('<ord_nmb>1</ord_nmb>', '<ord_nmb>2</ord_nmb>', 1)
    result = egrn_xml.extract(document(bad + contour(SQUARE)))
    assert result['accepted_contour_count'] == result['omitted_contour_count'] == 1
    assert result['contours'][0]['contour_index'] == 1
    assert result['errors'] == [{'contour_index': 0, 'code': 'invalid_point_order'}]


@pytest.mark.parametrize('points', [PROJECTED, SQUARE])
def test_closing_point_may_repeat_first_ordinal_only_with_exact_source_xy(points):
    text = contour(points)
    text = text.replace('<ord_nmb>'+str(len(points))+'</ord_nmb>', '<ord_nmb>1</ord_nmb>')
    result = egrn_xml.extract(document(text))
    assert result['accepted_contour_count'] == 1 and result['errors'] == []
    assert result['contours'][0]['raw_points_xy'] == [list(point) for point in points]
    assert result['accepted_point_count'] == len(points)


def test_repeated_closing_ordinal_cannot_close_different_decimal_xy():
    points = [('5000000.0000000000001', '0'), ('5000010', '0'), ('5000010', '10'),
              ('5000000', '10'), ('5000000.0000000000002', '0')]
    assert float(points[0][0]) == float(points[-1][0])
    text = contour(points).replace('<ord_nmb>5</ord_nmb>', '<ord_nmb>1</ord_nmb>')
    result = egrn_xml.extract(document(text))
    assert result['contours'] == []
    assert result['errors'] == [{'contour_index': 0, 'code': 'invalid_point_order'}]


def test_first_ordinal_repeated_inside_ring_is_rejected_even_with_valid_closure():
    text = contour(SQUARE).replace('<ord_nmb>3</ord_nmb>', '<ord_nmb>1</ord_nmb>')
    text = text.replace('<ord_nmb>5</ord_nmb>', '<ord_nmb>1</ord_nmb>')
    result = egrn_xml.extract(document(text))
    assert result['contours'] == []
    assert result['errors'] == [{'contour_index': 0, 'code': 'invalid_point_order'}]


def test_valid_contour_is_retained_beside_an_invalid_contour():
    result = egrn_xml.extract(document(contour(SQUARE[:-1])+contour(SQUARE)))
    assert result['received_contour_count'] == 2
    assert result['accepted_contour_count'] == result['omitted_contour_count'] == 1
    assert result['received_point_count'] == 9
    assert result['accepted_point_count'] == 5
    assert result['contours'][0]['contour_index'] == 1
    assert result['errors'] == [{'contour_index': 0, 'code': 'unclosed_contour'}]


@pytest.mark.parametrize('declaration', [
    '<!DOCTYPE extract_about_property_land [<!ENTITY x "SYNTHETIC_PRIVATE">]>',
    '<!DOCTYPE extract_about_property_land SYSTEM "file:///SYNTHETIC_PRIVATE">',
    '<!DOCTYPE extract_about_property_land [<!ENTITY x SYSTEM "https://invalid.example/private">]>',
    '<!DOCTYPE extract_about_property_land [<!ENTITY % x SYSTEM "file:///PRIVATE">%x;]>',
    '<?xml-stylesheet href="file:///PRIVATE"?>',
])
def test_dtd_entities_external_resources_and_processing_instructions_are_blocked(declaration):
    with pytest.raises(ValueError, match='DTD') as caught:
        egrn_xml.extract(document(declaration=declaration))
    assert 'PRIVATE' not in str(caught.value)
    assert 'file:' not in str(caught.value)


@pytest.mark.parametrize('raw', [
    document().decode('utf-8').encode('utf-16'),
    document().decode('utf-8').encode('utf-32'),
    document(declaration='<?xml version="1.0" encoding="windows-1251"?>'),
    document(declaration='<?xml version="1.0" encoding="UTF-16"?>'),
    document(declaration='<?xml version="1.1" encoding="UTF-8"?>'),
    document().replace(b'<land_record>', b'<land_record>\x00'),
    b'\xff'+document(),
])
def test_unsupported_encoding_xml_version_and_nul_are_rejected(raw):
    with pytest.raises(ValueError, match='UTF|XML 1.0'):
        egrn_xml.extract(raw)


@pytest.mark.parametrize('mutation', [
    lambda raw: raw.replace(b'<extract_about_property_land>', b'<extract_about_property_land xmlns="urn:future">'),
    lambda raw: raw.replace(b'<extract_about_property_land>', b'<extract_about_property_land xmlns:x="urn:unused">'),
    lambda raw: raw.replace(b'<extract_about_property_land>', b'<extract_about_property_land schemaLocation="PRIVATE">'),
    lambda raw: raw.replace(b'<x>', b'<x custom="PRIVATE">', 1),
])
def test_namespaces_and_attributes_are_strictly_rejected_without_value_echo(mutation):
    with pytest.raises(ValueError, match='пространства|атрибуты') as caught:
        egrn_xml.extract(mutation(document()))
    assert 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('mutation', [
    lambda raw: raw.replace(b'<land_record>', b'<land_record/><land_record>', 1),
    lambda raw: raw.replace(b'<cad_number>99:99:1234567:8</cad_number>', b'<cad_number>99:99:1234567:8</cad_number><cad_number>99:99:1234567:9</cad_number>', 1),
    lambda raw: raw.replace(b'<x>5026078.18</x>', b'<x>5026078.18</x><x>0</x>', 1),
    lambda raw: raw.replace(b'<y>5205201.02</y>', b'<y>5205201.02</y><y>0</y>', 1),
    lambda raw: raw.replace(b'</sk_id>', b'</sk_id><sk_id>OTHER</sk_id>', 1),
    lambda raw: raw.replace(b'<entity_spatial>', b'<entity_spatial/><entity_spatial>', 1),
    lambda raw: raw.replace(b'<spatial_element>', b'<spatial_element/><spatial_element>', 1),
    lambda raw: raw.replace(b'<x>5026078.18</x>', b'', 1),
    lambda raw: raw.replace(b'<sk_id>', b'<missing>', 1).replace(b'</sk_id>', b'</missing>', 1),
])
def test_missing_or_repeated_critical_fields_reject_whole_source(mutation):
    with pytest.raises(ValueError):
        egrn_xml.extract(mutation(document()))


@pytest.mark.parametrize('mutation', [
    lambda raw: raw.replace(b'extract_about_property_land', b'extract_about_property_building'),
    lambda raw: raw.replace(b'<ordinate>', b'<ordinate><z>100</z>', 1),
    lambda raw: raw.replace(b'<entity_spatial>', b'<entity_spatial><circle/>', 1),
    lambda raw: raw.replace(b'<spatial_element>', b'<spatial_element><arc/>', 1),
    lambda raw: raw.replace(b'<extract_about_property_land>', b'<extract_about_property_land><future_geometry/>', 1),
])
def test_future_document_and_geometry_types_are_not_silently_projected(mutation):
    with pytest.raises(ValueError, match='поддержан|структура'):
        egrn_xml.extract(mutation(document()))


@pytest.mark.parametrize('raw', [
    document()[:-10], b'<', b'<extract_about_property_land>',
    document()+b'<extra/>', document().replace(b'</ordinate>', b'</wrong>', 1),
    document().replace(b'<x>5026078.18</x>', b'<x>&unknown;</x>', 1),
])
def test_malformed_xml_is_rejected_without_source_snippets(raw):
    with pytest.raises(ValueError, match='повреждённый'):
        egrn_xml.extract(raw)


def test_all_budgets_include_ignored_data_and_invalid_contours():
    with pytest.raises(ValueError, match='2 МиБ'):
        egrn_xml.extract(b' '*(egrn_xml.MAX_BYTES+1))
    with pytest.raises(ValueError, match='20000'):
        egrn_xml.extract(document(extra='<details_request>'+'<unused/>'*20000+'</details_request>'))
    with pytest.raises(ValueError, match='глубины 64'):
        egrn_xml.extract(document(extra='<details_request>'+'<unused>'*64+'</unused>'*64+'</details_request>'))
    with pytest.raises(ValueError, match='5000'):
        egrn_xml.extract(document(contour([('NaN', '0')]*5001, metadata=False)))
    with pytest.raises(ValueError, match='100 контуров'):
        egrn_xml.extract(document(contour(SQUARE)*101))


def test_limits_are_inclusive_for_node_depth_and_contour_budgets(monkeypatch):
    raw = document(contour(SQUARE)*2)
    baseline = egrn_xml.extract(raw)
    monkeypatch.setattr(egrn_xml, 'MAX_BYTES', len(raw))
    monkeypatch.setattr(egrn_xml, 'MAX_NODES', baseline['node_count'])
    monkeypatch.setattr(egrn_xml, 'MAX_POINTS', 10)
    monkeypatch.setattr(egrn_xml, 'MAX_CONTOURS', 2)
    monkeypatch.setattr(egrn_xml, 'MAX_DEPTH', 11)
    assert egrn_xml.extract(raw)['accepted_contour_count'] == 2
    monkeypatch.setattr(egrn_xml, 'MAX_DEPTH', 10)
    with pytest.raises(ValueError, match='глубины'):
        egrn_xml.extract(raw)
    monkeypatch.setattr(egrn_xml, 'MAX_DEPTH', 11)
    monkeypatch.setattr(egrn_xml, 'MAX_NODES', baseline['node_count']-1)
    with pytest.raises(ValueError, match='узлов'):
        egrn_xml.extract(raw)


def test_5000_points_are_counted_even_when_contour_is_omitted():
    result = egrn_xml.extract(document(contour([('NaN', '0')]*5000, metadata=False)))
    assert result['received_point_count'] == 5000
    assert result['accepted_point_count'] == 0
    assert result['errors'] == [{'contour_index': 0, 'code': 'invalid_coordinate'}]


def test_empty_contour_and_geometry_engine_failure_never_become_empty_land_claim(monkeypatch):
    empty = egrn_xml.extract(document(contour([])))
    assert empty['errors'] == [{'contour_index': 0, 'code': 'insufficient_points'}]
    assert empty['free_land_confirmed'] is False

    def fail(_):
        raise GEOSException('SYNTHETIC_PRIVATE_NATIVE_DETAIL')

    monkeypatch.setattr(egrn_xml, 'Polygon', fail)
    result = egrn_xml.extract(document())
    assert result['errors'] == [{'contour_index': 0, 'code': 'invalid_contour'}]
    assert result['accepted_contour_count'] == 0
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(result)


def test_actual_contour_depth_and_size_limits_are_inclusive():
    raw = document(contour(SQUARE)*100,
                   extra='<details_request>'+'<unused>'*62+'</unused>'*62+'</details_request>')
    result = egrn_xml.extract(raw)
    assert result['received_contour_count'] == result['accepted_contour_count'] == 100
    padded = document()+b' '*(egrn_xml.MAX_BYTES-len(document()))
    assert len(padded) == egrn_xml.MAX_BYTES
    assert egrn_xml.extract(padded)['accepted_contour_count'] == 1


def test_utf8_bom_and_no_declaration_are_supported():
    assert egrn_xml.extract(b'\xef\xbb\xbf'+document())['accepted_contour_count'] == 1
    assert egrn_xml.extract(document(declaration=''))['accepted_contour_count'] == 1


def test_nonbytes_empty_and_invalid_identifier_never_become_evidence():
    for value in ('<xml/>', None, bytearray(document()), b''):
        with pytest.raises(ValueError, match='bytes'):
            egrn_xml.extract(value)
    raw = document().replace(b'<cad_number>99:99:1234567:8</cad_number>',
                             b'<cad_number>SYNTHETIC_PRIVATE</cad_number>', 1)
    with pytest.raises(ValueError, match='кадастрового') as caught:
        egrn_xml.extract(raw)
    assert 'SYNTHETIC_PRIVATE' not in str(caught.value)
