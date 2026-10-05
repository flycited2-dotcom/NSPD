"""Explicit visual transcriptions produce local previews, never legal/GIS approval."""
import copy
import re
from shapely.geometry import Polygon
from shapely.validation import explain_validity
from . import store,scan_source,scan_layout

ALGORITHM='visual-scan-review-v1'
LABEL=re.compile(r'н?\d{1,4}',re.I)
AREA=re.compile(r'\d{1,9}(?:[.,]\d{1,3})?')
WARNING='Визуальная сверка и исходный контур не подтверждают параметры СК, кадастровую идентичность, полноту участка, права или доступность земли. На географическую карту контур не добавлен.'


def validate(params,table):
    if params.get('visual_checked') is not True:raise ValueError('Нужна явная отметка визуальной сверки всех строк и замыкания')
    reviewer=params.get('reviewer');kind=params.get('reviewer_kind')
    if not isinstance(reviewer,str) or not 1<=len(reviewer.strip())<=100 or kind not in ('person','assistant'):raise ValueError('Укажите автора и способ визуальной сверки')
    label=params.get('label','');crs=params.get('crs_label','');axes=params.get('axes')
    if not isinstance(label,str) or len(label)>100 or not isinstance(crs,str) or len(crs)>200 or axes not in ('unconfirmed','xy_m'):
        raise ValueError('Неверные сведения заголовка таблицы')
    rows=params.get('rows')
    if not isinstance(rows,list) or len(rows)!=len(table['rows']) or not 4<=len(rows)<=500:raise ValueError('Число строк должно совпадать с черновиком, включая явное замыкание')
    points=[]
    for index,row in enumerate(rows):
        if not isinstance(row,list) or len(row)!=3 or any(not isinstance(x,str) or len(x)>40 for x in row):raise ValueError('Нужны три текстовых столбца каждой строки')
        text=[x.strip() for x in row]
        if not LABEL.fullmatch(text[0]):raise ValueError('Обозначение точки содержит неподдержанные символы')
        values=[scan_layout.literal(x) for x in text[1:]]
        if any(x is None for x in values):raise ValueError('Координаты содержат пропуск или неподдержанные символы')
        points.append({'label':text[0].lower(),'column_1':values[0],'column_2':values[1],
                       'transcribed_cells':text,'source_row_ordinal':index+1,'source_bbox':table['rows'][index]['bbox']})
    area_text=params.get('stated_area','')
    if not isinstance(area_text,str) or (area_text and (not AREA.fullmatch(area_text) or float(area_text.replace(',','.'))<=0)):raise ValueError('Неверная площадь по источнику')
    issues=[]
    closed=points[-1]['label']==points[0]['label'] and (points[-1]['column_1'],points[-1]['column_2'])==(points[0]['column_1'],points[0]['column_2'])
    if not closed:issues.append('Нет явного замыкания; контур не достраивается')
    vertices=points[:-1] if closed else points
    labels=[p['label'] for p in vertices];numbers=[int(re.sub(r'^н','',x)) for x in labels]
    if len(set(labels))!=len(labels):issues.append('Повтор обозначения точки внутри кольца')
    if numbers!=list(range(numbers[0],numbers[0]+len(numbers))):issues.append('Нарушена последовательность номеров; точки не переставляются')
    coords=[(p['column_1'],p['column_2']) for p in vertices]
    if len(set(coords))!=len(coords):issues.append('Повтор координат внутри кольца')
    polygon=Polygon(coords) if closed and len(coords)>=3 else None
    if polygon is not None and (not polygon.is_valid or polygon.area<=0):issues.append(explain_validity(polygon) if not polygon.is_valid else 'Нулевая площадь')
    area=round(polygon.area,2) if polygon is not None and not issues and axes=='xy_m' else None
    result={'algorithm':ALGORITHM,'label':label.strip() or 'Таблица скана '+str(table['ordinal']),
            'reviewer':reviewer.strip(),'reviewer_kind':kind,'visual_checked':True,'points':points,'axes':axes,'crs_label':crs.strip(),
            'stated_area_m2':float(area_text.replace(',','.')) if area_text else None,'stated_area_text':area_text,
            'closure':'explicit' if closed else 'missing','state':'rejected' if issues else 'local_preview',
            'issues':issues,'outline_columns':list(map(list,polygon.exterior.coords)) if polygon is not None and not issues else None,
            'local_area_m2':area,'georeferenced':False,'geometry_confirmed':False,'crs_parameters_confirmed':False,
            'cadastral_identity_confirmed':False,'completeness_confirmed':False,'warning':WARNING}
    if area is not None and area_text:
        decimals=len(area_text.replace(',','.').partition('.')[2]);tolerance=.5*10**(-decimals)
        difference=polygon.area-result['stated_area_m2']
        result.update(area_difference_m2=round(difference,4),area_rounding_tolerance_m2=tolerance,
                      stated_area_disagrees=abs(difference)>tolerance+.000001)
    return result


def run(project,params):
    result,page,catalog=scan_source.current(project,params)
    ordinal=params.get('table')
    if isinstance(ordinal,bool) or not isinstance(ordinal,int):raise ValueError('Недопустимый номер таблицы')
    table=next((t for t in page['tables'] if t['ordinal']==ordinal),None)
    if not table:raise ValueError('Таблица не принадлежит странице')
    review=validate(params,table)
    review.update(document_id=page['document_id'],page=page['page'],table=ordinal,
                  source_sha256=page['source_sha256'],source_views=page['source_views'],
                  source_received_at=page['source_received_at'],url=page['url'],title=page['title'],
                  fingerprint=scan_source.fingerprint(page),reviewed_at=store.now())
    old=copy.deepcopy(store.get_setting('scan_reviews_'+project) or {'tables':[]})
    prior=next((t for t in old['tables'] if t['document_id']==page['document_id'] and t['page']==page['page'] and t['table']==ordinal),None)
    history=copy.deepcopy(prior.get('history',[]) if prior else [])
    if prior:history.append({k:v for k,v in prior.items() if k!='history'})
    if len(history)>50:raise ValueError('Достигнут предел 50 исправлений одной таблицы; история сохранена')
    review['history']=history
    old['tables']=[t for t in old['tables'] if not (t['document_id']==page['document_id'] and t['page']==page['page'] and t['table']==ordinal)]+[review]
    scan_source.ensure_current(project,result,catalog)
    scan_source.persist(project,'scan_reviews',old)
    with store.connect() as db:store.event(db,project,'scan_visual_review',{'document_id':page['document_id'],'page':page['page'],'table':ordinal,'state':review['state'],'reviewer_kind':review['reviewer_kind']})
    return {'state':review['state'],'table':ordinal,'points':len(review['points'])}


def present(saved,drafts,choices,catalog_id=None):
    if not saved:return None
    result=copy.deepcopy(saved);fps={(p['document_id'],p['page']):scan_source.fingerprint(p) for p in (drafts or {}).get('pages',[])}
    current={(c['document_id'],n):c['source_sha256'] for c in choices for n in c['pages']}
    for table in result['tables']:
        identity=(table['document_id'],table['page'])
        table['stale']=(table['fingerprint']!=fps.get(identity) or table['source_sha256']!=current.get(identity)
                        or catalog_id is not None and (drafts or {}).get('catalog_id')!=catalog_id)
        if table['stale']:table['outline_columns']=None;table['local_area_m2']=None
    return result
