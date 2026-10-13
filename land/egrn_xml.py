"""Bounded, privacy-preserving extraction from one supported EGRN XML shape.

Only a land record's cadastral number and original XY contours leave this
module. A readable/valid XY ring does not establish its CRS, a registered
right, current legal status, or availability of land. No coordinate transform,
geometry repair, XML resource access, or interpretation of other fields occurs.
"""
import hashlib
import math
import re
from decimal import Decimal
from xml.parsers import expat
from xml.etree import ElementTree as ET

from shapely.errors import GEOSException
from shapely.geometry import Polygon


ALGORITHM = 'egrn-xml-whitelist-v1'
MAX_BYTES = 2 * 1024 * 1024
MAX_NODES = 20000
MAX_DEPTH = 64
MAX_POINTS = 5000
MAX_CONTOURS = 100
MAX_ABS_COORDINATE = 1000000000
ROOT = 'extract_about_property_land'
_DECIMAL = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z')
_CADASTRAL = re.compile(r'[0-9]{1,2}:[0-9]{1,2}:[0-9]{1,8}:[0-9]{1,12}\Z')


def _reject(message):
    # Never include XML text, names, parser snippets, or source paths in errors.
    raise ValueError('ЕГРН XML: ' + message)


def _parse(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        _reject('ожидались bytes XML размером от 1 байта до 2 МиБ')
    if b'\x00' in raw:
        _reject('NUL и кодировки UTF-16/UTF-32 не поддержаны; требуется UTF-8')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        _reject('поддержана только кодировка UTF-8')
    parser = expat.ParserCreate(namespace_separator='}')
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    stack, buffers, roots = [], [], []
    count = 0

    def unsupported_declaration(*_):
        _reject('DTD, объявления и внешние сущности не поддержаны')

    def declaration(version, encoding, standalone):
        if version != '1.0' or (encoding is not None and encoding.upper() != 'UTF-8'):
            _reject('поддержаны XML 1.0 и кодировка UTF-8')

    def namespace(*_):
        _reject('пространства имён и schemaLocation не поддержаны этой версией')

    def start(name, attributes):
        nonlocal count
        count += 1
        if count > MAX_NODES:
            _reject('превышен предел 20000 узлов')
        if len(stack) >= MAX_DEPTH:
            _reject('превышен предел глубины 64')
        if any(character in name for character in (':', '{', '}')) or attributes:
            _reject('атрибуты, пространства имён и schemaLocation не поддержаны этой версией')
        node = ET.Element(name)
        if stack:
            stack[-1].append(node)
        else:
            roots.append(node)
        stack.append(node)
        buffers.append([])

    def characters(value):
        # The complete source is byte bounded; ignored fields remain only in
        # this short-lived tree and are never copied into the returned evidence.
        if stack:
            buffers[-1].append(value)

    def end(_):
        stack.pop().text = ''.join(buffers.pop())

    parser.XmlDeclHandler = declaration
    parser.StartNamespaceDeclHandler = namespace
    parser.StartDoctypeDeclHandler = unsupported_declaration
    parser.EntityDeclHandler = unsupported_declaration
    parser.ExternalEntityRefHandler = unsupported_declaration
    parser.ProcessingInstructionHandler = unsupported_declaration
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    try:
        parser.Parse(text, True)
    except expat.ExpatError:
        _reject('повреждённый или неподдерживаемый XML')
    if len(roots) != 1 or roots[0].tag != ROOT:
        _reject('поддержан только корень extract_about_property_land без пространства имён')
    return roots[0], count


def _one(parent, name, selected, required=True):
    values = [node for node in parent if node.tag == name]
    if len(values) > 1 or (required and not values):
        _reject('отсутствующее или повторённое обязательное поле ' + name)
    node = values[0] if values else None
    if node is not None:
        selected.add(id(node))
    return node


def _structure(node, allowed):
    if (node.text or '').strip() or any(child.tag not in allowed for child in node):
        _reject('неподдержанная структура выбранных координатных полей')


def _leaf(node, limit):
    if len(node) or node.text is None or len(node.text) > limit:
        _reject('неподдержанное содержимое выбранного поля')
    return node.text


def _coordinate(node):
    raw = _leaf(node, 64)
    stripped = raw.strip()
    if not _DECIMAL.fullmatch(stripped):
        return raw, None
    value = float(stripped)
    if not math.isfinite(value) or abs(value) > MAX_ABS_COORDINATE:
        return raw, None
    return raw, value


def extract(raw):
    """Return whitelist evidence; malformed/ambiguous source raises ValueError.

    Geometrically unusable contours are omitted with fixed reason codes. All
    source/node/depth/point/contour budgets and unknown coordinate structures
    reject the entire source. ``points_xy`` preserve source axis order and
    ``raw_points_xy`` preserve exact decimal text; neither is WGS84 geometry.
    """
    root, node_count = _parse(raw)
    selected = {id(root)}
    _structure(root, {'details_statement', 'details_request', 'land_record',
                      'right_records', 'recipient_statement', 'status'})
    record = _one(root, 'land_record', selected)
    obj = _one(record, 'object', selected)
    common = _one(obj, 'common_data', selected)
    cadastral_node = _one(common, 'cad_number', selected)
    cadastral_number = _leaf(cadastral_node, 80).strip()
    if not _CADASTRAL.fullmatch(cadastral_number):
        _reject('неподдержанный формат кадастрового номера выбранного land_record')
    location = _one(record, 'contours_location', selected)
    container = _one(location, 'contours', selected)
    _structure(container, {'contour'})
    contours = list(container)
    if not contours or len(contours) > MAX_CONTOURS:
        _reject('требуется от 1 до 100 контуров')
    point_count = sum(1 for contour in contours for node in contour.iter('ordinate'))
    if point_count > MAX_POINTS:
        _reject('превышен общий предел 5000 точек')
    accepted, errors = [], []
    for index, contour in enumerate(contours):
        selected.add(id(contour))
        _structure(contour, {'number_pp', 'cad_number', 'entity_spatial'})
        entity = _one(contour, 'entity_spatial', selected)
        _structure(entity, {'sk_id', 'spatials_elements'})
        crs_node = _one(entity, 'sk_id', selected)
        crs = _leaf(crs_node, 128)
        if not crs.strip() or any(ord(character) < 32 for character in crs):
            _reject('неподдержанный идентификатор исходной системы координат')
        elements = _one(entity, 'spatials_elements', selected)
        _structure(elements, {'spatial_element'})
        spatial = _one(elements, 'spatial_element', selected)
        _structure(spatial, {'ordinates'})
        ordinates = _one(spatial, 'ordinates', selected)
        _structure(ordinates, {'ordinate'})
        points, raw_points = [], []
        invalid_number = False
        invalid_order = False
        order_fields = 0
        repeated_closing_order = False
        for point_index, ordinate in enumerate(ordinates):
            selected.add(id(ordinate))
            _structure(ordinate, {'x', 'y', 'ord_nmb', 'num_geopoint', 'delta_geopoint'})
            # Never sort or infer the ring from ordinal metadata. If this
            # metadata is present, every point must identify its existing
            # document position unambiguously, starting at one. A final point
            # may repeat ordinal one only when its exact XY closes the ring.
            order = [node for node in ordinate if node.tag == 'ord_nmb']
            if order:
                order_fields += 1
                selected.update(id(node) for node in order)
                if len(order) != 1 or len(order[0]) or order[0].text is None or len(order[0].text) > 64:
                    invalid_order = True
                else:
                    number = order[0].text.strip()
                    if not re.fullmatch(r'\+?[0-9]+', number):
                        invalid_order = True
                    elif int(number) == 1 and point_index > 0 and point_index == len(ordinates) - 1:
                        repeated_closing_order = True
                    elif int(number) != point_index + 1:
                        invalid_order = True
            x_raw, x = _coordinate(_one(ordinate, 'x', selected))
            y_raw, y = _coordinate(_one(ordinate, 'y', selected))
            raw_points.append([x_raw, y_raw])
            points.append([x, y])
            invalid_number = invalid_number or x is None or y is None
        explicitly_closed = (not invalid_number and bool(points)
            and all(Decimal(raw_points[0][axis].strip()) == Decimal(raw_points[-1][axis].strip())
                    for axis in (0, 1)))
        code = None
        if invalid_number:
            code = 'invalid_coordinate'
        elif (invalid_order or (order_fields and order_fields != len(points))
              or (repeated_closing_order and not explicitly_closed)):
            code = 'invalid_point_order'
        elif len(points) < 4 or len({tuple(point) for point in points}) < 3:
            code = 'insufficient_points'
        elif not explicitly_closed:
            code = 'unclosed_contour'
        else:
            try:
                polygon = Polygon(points)
                if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
                    code = 'invalid_contour'
            except (GEOSException, ValueError, TypeError):
                code = 'invalid_contour'
        if code:
            errors.append({'contour_index': index, 'code': code})
            continue
        bounds = [min(point[0] for point in points), min(point[1] for point in points),
                  max(point[0] for point in points), max(point[1] for point in points)]
        accepted.append({'contour_index': index, 'source_crs_raw': crs,
                         'coordinate_system_confirmed': False,
                         'coordinate_order': 'source_x_y', 'points_xy': points,
                         'raw_points_xy': raw_points, 'bounds_xy': bounds,
                         'point_count': len(points), 'explicitly_closed': True})
    return {'algorithm': ALGORITHM, 'source_sha256': hashlib.sha256(raw).hexdigest(),
            'source_bytes': len(raw), 'root_type': ROOT, 'node_count': node_count,
            'omitted_node_count': node_count - len(selected),
            'cadastral_number': cadastral_number, 'contours': accepted,
            'received_contour_count': len(contours), 'accepted_contour_count': len(accepted),
            'omitted_contour_count': len(errors), 'received_point_count': point_count,
            'accepted_point_count': sum(contour['point_count'] for contour in accepted),
            'errors': errors, 'all_contours_accepted': not errors,
            'complete_source': False, 'coordinate_system_confirmed': False,
            'georeferenced': False, 'geometry_confirmed': False,
            'rights_confirmed': False, 'free_land_confirmed': False,
            'legal_claims_confirmed': False}
