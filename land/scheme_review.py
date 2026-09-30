"""Joint local-coordinate review; reconstructed project outlines are not EGRN."""
import re
from shapely.geometry import Polygon
from shapely.ops import unary_union

CAD = r'\d{1,2}:\d{1,2}:\d{1,10}:\d{1,10}'
ORIGIN = re.compile(r'в\s+границах\s+земельного\s+участка\s+с\s+кадастровым\s+номером\s+('
                    + CAD + r')\s+площадью\s+(\d{1,9}(?:[,.]\d{1,2})?)\s+кв\.?\s*м(?!\w)', re.I)
DIVISION = re.compile(r'Путем\s+раздела\s+земельного\s+участка\s+с\s+кадастровым\s+номе[\s-]*ром\s+('
                      + CAD + r')\s+с\s+сохранением\s+исходного', re.I)
QUARTER = re.compile(r'кадастров[\s-]*[а-яё]*\s+квартал[а-яё]*\s+(\d{1,2}:\d{1,2}:\d{1,10})(?![:\d])', re.I)
WARNING = 'Общий контур составлен из таблиц проекта в исходных X/Y. Это не актуальная граница ЕГРН. Обозначение :ЗУ и совпадение площадей не подтверждают свободную землю или полноту проекта.'


def context(pages):
    text = ''; offsets = []
    for page, body in pages:
        offsets.append((len(text), len(text)+len(body)+1, page))
        text += body+'\n'
    def at(pos):
        return next(page for start, end, page in offsets if start <= pos < end)
    return {
        'origin_statements': [{'cadastral_number':m[1], 'stated_area_m2':float(m[2].replace(',', '.')),
                               'page':at(m.start()), 'source_excerpt':m[0]} for m in ORIGIN.finditer(text)],
        'division_statements': [{'cadastral_number':m[1], 'page':at(m.start()),
                                 'source_excerpt':m[0]} for m in DIVISION.finditer(text)],
        'quarter_statements': [{'cadastral_quarter':m[1], 'page':at(m.start()),
                                'source_excerpt':m[0]} for m in QUARTER.finditer(text)]}


def build(tables, evidence):
    accepted = [(t, Polygon(t['outline_xy'])) for t in tables if t['state']=='review_required' and t.get('outline_xy')]
    result = {**evidence, 'warning':WARNING, 'table_count':len(tables), 'accepted_count':len(accepted),
              'excluded_count':len(tables)-len(accepted), 'georeferenced':False, 'geometry_confirmed':False,
              'comparison_scope':'all_accepted_tables_not_verified_as_parent', 'area_comparisons':[],
              'union_rings_xy':[], 'overlap_pairs':[], 'overlap_pair_count':0, 'role_totals':[]}
    if not accepted:
        result.update(sum_area_m2=None, union_area_m2=None, overlap_area_m2=None, component_count=0)
        return result
    polygons = [p for _, p in accepted]
    union = unary_union(polygons)
    components = list(union.geoms) if union.geom_type=='MultiPolygon' else [union]
    total = sum(p.area for p in polygons)
    result.update(sum_area_m2=round(total,2), union_area_m2=round(union.area,2),
                  overlap_area_m2=round(max(0,total-union.area),6), component_count=len(components))
    for polygon in sorted(components,key=lambda p:p.bounds):
        result['union_rings_xy'].append({'exterior_xy':list(map(list,polygon.exterior.coords)),
                                        'holes_xy':[list(map(list,r.coords)) for r in polygon.interiors],
                                        'table_indices':[tables.index(t) for t,p in accepted if polygon.covers(p)]})
    for i, (left, p) in enumerate(accepted):
        for right, q in accepted[i+1:]:
            area = p.intersection(q).area
            if area > 0.000001:
                result['overlap_pair_count'] += 1
                if len(result['overlap_pairs']) < 200:
                    result['overlap_pairs'].append({'left':left['label'], 'right':right['label'], 'area_m2':round(area,6)})
    result['overlap_pairs_truncated'] = result['overlap_pair_count']>len(result['overlap_pairs'])
    for role in sorted({t['purpose_hint'] for t,_ in accepted}):
        selected = [p for t,p in accepted if t['purpose_hint']==role]
        result['role_totals'].append({'purpose_hint':role,'count':len(selected), 'sum_area_m2':round(sum(p.area for p in selected),2)})
    # Multiple distinct parent statements must never be reconciled with one global union.
    origins = {(x['cadastral_number'],x['stated_area_m2']) for x in evidence['origin_statements']}
    result['origin_ambiguous'] = len(origins)>1
    if len(origins)==1:
        cad, area = next(iter(origins))
        result['area_comparisons'] = [{'cadastral_number':cad,'stated_area_m2':area,
                                      'union_area_m2':round(union.area,2), 'difference_m2':round(union.area-area,2),
                                      'applicability':'review_required'}]
    parents = {x['cadastral_number'].rsplit(':',1)[0] for x in evidence['origin_statements']}
    quarters = {x['cadastral_quarter'] for x in evidence['quarter_statements']}
    result['quarter_disagreement'] = bool(parents and quarters and not parents.issubset(quarters))
    return result
