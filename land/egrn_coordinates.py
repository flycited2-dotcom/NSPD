"""Observed EGRN section 3.2 tables, in source XY only, without OCR or CRS guesses."""
import re
from shapely.geometry import Polygon
from shapely.validation import explain_validity
from .torgi import canonical

PDF_ALGORITHM = 'torgi-file-text-v3'
SHEET = re.compile(r'Лист\s*№\s*(\d+)\s+раздела\s+3\.2\s+Всего\s+листов\s+раздела\s+3\.2:\s*(\d+)', re.I)
CAD = re.compile(r'^Кадастровый\s+номер:\s*(\d{1,2}:\d{1,2}:\d{1,10}:\d{1,10})', re.I | re.M)
TITLE = 'Сведения о характерных точках границы земельного участка'
HEADER = re.compile(r'Координаты,\s*м\b[\s\S]*?X\s+Y\s*\n\s*1\s+2\s+3\s+4\s+5\s*\n', re.I)
VALUE = r'-?\d{1,8}(?:[,.]\d{1,3})?'
ROW = re.compile(rf'^(\d+)\s+({VALUE})\s+({VALUE})\s+(закрепление\s+отсутствует|-)\s+(\d+(?:[,.]\d{{1,3}})?)$', re.I)


def extract(pages):
    result = []
    consumed = set()
    for position, (page, text) in enumerate(pages):
        if len(text) > 500000:
            raise ValueError('Текст страницы превышает лимит')
        sheet = SHEET.search(text)
        if TITLE not in text or not sheet or int(sheet[1]) != 1:
            continue
        total = int(sheet[2])
        if not 1 <= total <= 40:
            raise ValueError('Раздел координат ЕГРН превышает лимит листов')
        issues, points, source_pages, cad_numbers, crs_labels = [], [], [], set(), set()
        closure_seen = False
        for offset in range(total):
            if position + offset >= len(pages):
                issues.append({'reason': 'Листы раздела 3.2 получены не полностью'})
                break
            number, body = pages[position + offset]
            marker = SHEET.search(body)
            if not marker or int(marker[1]) != offset + 1 or int(marker[2]) != total or number != page + offset:
                issues.append({'page': number, 'reason': 'Нарушен порядок или число листов раздела 3.2'})
                break
            source_pages.append(number)
            cads = {canonical(m[1]) for m in CAD.finditer(body)}
            if len(cads) != 1:
                issues.append({'page': number, 'reason': 'Кадастровый номер листа не однозначен'})
            cad_numbers.update(cads)
            crs_labels.update(m[1].strip() for m in re.finditer(r'Система координат\s+([^\n]+)', body))
            if offset == 0:
                header = HEADER.search(body, body.find(TITLE))
                if not header:
                    issues.append({'page': number, 'reason': 'Не подтверждены пять столбцов с осями X/Y и метрами'})
                    break
                body = body[header.end():]
            rows_on_page = 0
            started = False
            rows_ended = False
            for line in body.splitlines():
                value = line.strip()
                if not value:
                    continue
                match = ROW.fullmatch(value)
                if not match:
                    if (re.match(r'^\d+\s+[-\d]', value)
                            or re.match(rf'^\S+\s+{VALUE}\s+{VALUE}(?:\s|$)', value)):
                        issues.append({'page': number, 'reason': 'Неподдержанная строка пятистолбцовой таблицы', 'row': value[:200]})
                    if started:
                        rows_ended = True
                    continue
                if rows_ended:
                    issues.append({'page': number, 'reason': 'Координаты после разделителя таблицы; продолжение не установлено', 'row': value[:200]})
                    continue
                started = True
                x, y, accuracy = [float(v.replace(',', '.')) for v in (match[2], match[3], match[5])]
                repeat_first = (len(points) >= 3 and match[1] == points[0]['label']
                                and (x, y) == (points[0]['x'], points[0]['y']))
                if closure_seen:
                    issues.append({'page': number, 'reason': 'Строки после явного замыкания; части не объединены'})
                if int(match[1]) != len(points) + 1 and not repeat_first:
                    issues.append({'page': number, 'reason': 'Нарушен последовательный номер точки'})
                points.append({'label': match[1], 'x': x, 'y': y, 'page': number,
                               'mark_description': match[4], 'accuracy_stated_m': accuracy, 'source_row': value})
                if len(points) >= 4 and (x, y) == (points[0]['x'], points[0]['y']):
                    closure_seen = True
                consumed.add((number, value))
                rows_on_page += 1
                if len(points) > 5000:
                    raise ValueError('Таблица ЕГРН превышает лимит 5000 точек')
            if not rows_on_page:
                issues.append({'page': number, 'reason': 'На листе не прочитаны строки координат'})
        if len(cad_numbers) != 1:
            issues.append({'reason': 'Кадастровый номер различается или отсутствует на листах'})
        if len(crs_labels) != 1:
            issues.append({'reason': 'Обозначение системы координат отсутствует или неоднозначно'})
        coords = [(p['x'], p['y']) for p in points]
        closed = len(coords) >= 4 and coords[0] == coords[-1]
        if not closed:
            issues.append({'reason': 'Нет явного замыкания исходными координатами'})
        if closed and len(set(coords[:-1])) != len(coords) - 1:
            issues.append({'reason': 'Повтор координат внутри кольца; части не объединены'})
        polygon = Polygon(coords) if len(coords) >= 4 else None
        if polygon is not None and (not polygon.is_valid or polygon.area <= 0):
            issues.append({'reason': explain_validity(polygon) if not polygon.is_valid else 'Нулевая площадь'})
        cad = next(iter(cad_numbers)) if len(cad_numbers) == 1 else None
        result.append({'label': cad or 'Номер не подтверждён', 'cadastral_number': cad,
                       'format': 'egrn_section_3_2_xy_5_columns', 'heading_page': page, 'pages': source_pages,
                       'section_sheets_expected': total, 'crs_label': next(iter(crs_labels)) if len(crs_labels) == 1 else None,
                       'crs_status': 'parameters_missing', 'axes': 'X/Y as printed', 'units': 'm',
                       'points': points, 'closure': 'explicit_coordinate_repeat' if closed else 'missing',
                       'state': 'rejected' if issues else 'review_required', 'issues': issues,
                       'outline_xy': list(map(list, polygon.exterior.coords)) if polygon is not None and not issues else None,
                       'local_area_m2': round(polygon.area, 2) if polygon is not None and not issues else None,
                       'purpose_hint': 'recorded_parcel_boundary', 'geometry_confirmed': False, 'georeferenced': False})
    if len(result) > 200 or sum(len(t['points']) for t in result) > 5000:
        raise ValueError('Слишком много таблиц или точек ЕГРН')
    for table in result:
        if table['cadastral_number'] and sum(t['cadastral_number'] == table['cadastral_number'] for t in result) > 1:
            table['issues'].append({'reason': 'Повтор раздела для одного номера; контуры не объединены'})
            table.update(state='rejected', outline_xy=None, local_area_m2=None)
    return result, consumed
