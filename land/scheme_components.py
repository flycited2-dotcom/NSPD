"""Named component tables and project boundaries, with no inferred cadastral identity."""
import re
from shapely.geometry import Polygon
from shapely.validation import explain_validity

COMPONENT = re.compile(r'^Номер контура многоконтурного земельного участка\s+(:ЗУ\d{1,4})\((\d{1,3})\)\s*$', re.I | re.M)
AREA = re.compile(r'\s*Площадь контура многоконтурного земельного участка\s+(\d{1,9}(?:[,.]\d{1,2})?)\s+кв\.\s*м\s*', re.I)
HEADER = re.compile(r'Обозначение характерных\s+точек границ\s+Координаты,\s*м\s+(Система координат условная 1963 г\.)\s+X\s+Y\s*\n', re.I)
BOUNDARY = re.compile(r'^Координаты границ территории проектирования\s*\nТаблица\s+(\d{1,3})\s*\n', re.I | re.M)
POINT = re.compile(r'^(н?\d+)\s+(-?\d{1,8}(?:[,.]\d{1,3})?)\s+(-?\d{1,8}(?:[,.]\d{1,3})?)$', re.I)
POINT_ROW = re.compile(r'^(?:н\S*|\d+)\s+-?[\dОOоo]\S*', re.I)
FOOTER = re.compile(r'^(?:\d{4}-ПМТ|(?:Изм )?Лист № докум\. Подп\. Дата(?: Изм)?|Лист|\d{1,3})$', re.I)


def read_ring(text, start, end, page_at, allow_next_ring=False):
    points, issues, covered, cursor = [], [], set(), start
    closed = False
    previous_page = page_at(max(0, start - 1))
    for line in text[start:end].splitlines(keepends=True):
        line_start = cursor; cursor += len(line)
        value = line.strip()
        if not value or FOOTER.fullmatch(value):
            continue
        match = POINT.fullmatch(value)
        if not match:
            if POINT_ROW.match(value):
                issues.append({'reason':'Строка точки не соответствует трём столбцам', 'page':page_at(line_start), 'row':value[:200]})
            break
        x, y = (float(v.replace(',', '.')) for v in match.groups()[1:])
        current_page = page_at(line_start)
        if current_page > previous_page + 1:
            issues.append({'reason':'Между строками точек пропущена страница; полнота кольца не подтверждена', 'page':current_page})
        previous_page = current_page
        points.append({'label':match[1], 'x':x, 'y':y, 'page':page_at(line_start), 'source_row':value})
        covered.add(line_start)
        if len(points)>5000:
            raise ValueError('Кольцо превышает лимит 5000 точек')
        if len(points)>1 and points[-1]['label']==points[0]['label']:
            closed=(x,y)==(points[0]['x'],points[0]['y'])
            if not closed:
                issues.append({'reason':'Замыкающая точка изменила координаты', 'page':page_at(line_start), 'row':value})
            break
    if not closed:
        issues.append({'reason':'Нет явного замыкания; граница не достраивается'})
    vertices=points[:-1] if closed else points
    labels, coords = set(), set()
    for point in vertices:
        if point['label'] in labels:
            issues.append({'reason':'Повтор обозначения точки внутри кольца', 'page':point['page'], 'row':point['source_row']})
        if (point['x'],point['y']) in coords:
            issues.append({'reason':'Повтор координат внутри кольца', 'page':point['page'], 'row':point['source_row']})
        labels.add(point['label']); coords.add((point['x'],point['y']))
    if len(vertices)<3:
        issues.append({'reason':'Меньше трёх вершин'})
    polygon=Polygon([(p['x'],p['y']) for p in points]) if closed and len(vertices)>=3 else None
    if polygon is not None and (not polygon.is_valid or polygon.area<=0):
        issues.append({'reason':explain_validity(polygon) if not polygon.is_valid else 'Нулевая площадь'})
    # Another point row after closure is not silently attached to, or dropped from, this ring.
    for line in text[cursor:end].splitlines():
        value=line.strip()
        if not value or FOOTER.fullmatch(value):
            continue
        if POINT_ROW.match(value) and not allow_next_ring:
            issues.append({'reason':'Строки точек после замыкания кольца', 'row':value})
        break
    return points, issues, covered, cursor, polygon, closed


def extract(text, page_at):
    tables, boundaries, covered = [], [], set()
    headings=list(COMPONENT.finditer(text))
    if len(headings)>200:raise ValueError('Количество компонентов превышает лимит')
    for index, heading in enumerate(headings):
        end=headings[index+1].start() if index+1<len(headings) else len(text)
        number=int(heading[2]); area=AREA.match(text,heading.end(),end)
        header=HEADER.match(text,area.end(),end) if area else None
        if not header or number<1 or number>200:
            # Unsupported headings still appear as rejected components, never as trusted rings.
            points=[]; issues=[{'reason':'Заголовок, единицы или порядок X/Y компонента не подтверждены'}]
            polygon=None; stated=None; closed=False
        else:
            points,issues,found,_,polygon,closed=read_ring(text,header.end(),end,page_at)
            covered.update(found); stated=float(area[1].replace(',','.'))
        parent=heading[1].upper()
        tables.append({'label':f'{parent}({number})','heading_page':page_at(heading.start()),'source_heading':heading[0].strip(),
                       'pages':sorted({p['page'] for p in points}) or [page_at(heading.start())],
                       'table_kind':'parcel_component','parcel_label':parent, 'component_number':number,
                       'purpose_hint':'multi_contour_context','context_page':page_at(heading.start()),
                       'crs_label':header[1] if header else None,'crs_parameters_confirmed':False,
                       'stated_area_m2':stated,'points':points,'closure':'explicit' if closed else 'missing',
                       'state':'rejected' if issues else 'review_required','issues':issues,
                       'local_area_m2':round(polygon.area,2) if polygon is not None and not issues else None,
                       'outline_xy':list(map(list,polygon.exterior.coords)) if polygon is not None and not issues else None,
                       'georeferenced':False,'geometry_confirmed':False})
        if polygon is not None and not issues:
            decimals=len(area[1].replace(',','.').split('.')[1]) if '.' in area[1].replace(',','.') else 0
            tolerance=.5*10**(-decimals)
            tables[-1].update(area_difference_m2=round(polygon.area-stated,4),area_rounding_tolerance_m2=tolerance,
                              stated_area_disagrees=abs(polygon.area-stated)>tolerance+0.000001)
        if sum(len(t['points']) for t in tables)>5000:raise ValueError('Количество точек превышает лимит')
    for heading in BOUNDARY.finditer(text):
        # Stop at the next section/table. Only explicitly closed rings are supported here.
        cursor=heading.end(); rings=[]; all_issues=[]
        while cursor<len(text):
            points,issues,found,next_cursor,polygon,closed=read_ring(text,cursor,len(text),page_at,allow_next_ring=True)
            if not points:
                if not rings: all_issues.extend(issues)
                else:all_issues.extend(x for x in issues if x.get('row'))
                break
            covered.update(found)
            rings.append({'points':[{'label':p['label'],'column_1':p['x'],'column_2':p['y'],
                                     'page':p['page'],'source_row':p['source_row']} for p in points],
                          'closure':'explicit' if closed else 'missing',
                          'state':'rejected' if issues else 'review_required','issues':issues,
                          'outline_columns':list(map(list,polygon.exterior.coords)) if polygon is not None and not issues else None})
            if len(rings)>200 or sum(len(r['points']) for r in rings)>5000:
                raise ValueError('Количество колец/точек границы проекта превышает лимит')
            if polygon is None or next_cursor<=cursor:break
            cursor=next_cursor
        if not rings and not all_issues:
            all_issues.append({'reason':'В таблице границы не найдены строки точек', 'page':page_at(heading.start())})
        boundaries.append({'label':'Территория проектирования · таблица '+heading[1],
                           'heading_page':page_at(heading.start()),'rings':rings,'issues':all_issues,
                           'axes_status':'unlabelled_columns','units_confirmed':False,
                           'georeferenced':False,'geometry_confirmed':False,
                           'warning':'Граница территории проекта не является участком. X/Y и единицы в таблице не подписаны; исходные столбцы не переводятся на карту и не включаются в площадь участков.'})
        if len(boundaries)>200 or sum(len(r['points']) for b in boundaries for r in b['rings'])>5000:
            raise ValueError('Количество границ/точек проекта превышает лимит')
    return tables,boundaries,covered


def groups(tables):
    parents={t['parcel_label'] for t in tables if t.get('table_kind')=='parcel_component'}
    result=[]
    for label in sorted(parents):
        indices=[i for i,t in enumerate(tables) if t.get('parcel_label')==label]
        selected=[tables[i] for i in indices]; numbers={t['component_number'] for t in selected}
        highest=max(numbers); accepted=[t for t in selected if t['state']=='review_required']
        result.append({'parcel_label':label,'table_indices':indices,'observed_components':sorted(numbers),
                       'missing_numbers_before_highest':sorted(set(range(1,min(highest,200)+1))-numbers),
                       'accepted_components':len(accepted),'rejected_components':len(selected)-len(accepted),
                       'accepted_area_sum_m2':round(sum(t['local_area_m2'] for t in accepted),2),
                       'completeness_confirmed':False,'geometry_confirmed':False,'georeferenced':False,
                       'warning':'Части относятся к условному обозначению в документе. Пропущенные и отклонённые части не достраиваются; полнота участка, кадастровая идентичность и права не установлены.'})
    return result
