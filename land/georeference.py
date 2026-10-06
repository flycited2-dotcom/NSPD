"""Reference-scoped geographic preview, never cadastral survey coordinates."""
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlencode
import pyproj
from pyproj import CRS, Transformer, datadir
from pyproj.aoi import AreaOfInterest
from pyproj.transformer import TransformerGroup
from shapely import hausdorff_distance, segmentize
from shapely.geometry import Polygon, Point, box, mapping, shape
from shapely.ops import transform, unary_union
from . import nspd, store

LABEL = re.compile(r'^(\d{1,2}:\d{1,2}:\d{1,10}:\d{1,10})(?:\(\d+\))?$')
WARNING = 'Предварительная привязка сверяется по одному объекту НСПД. Паспорт СК проекта и точность для кадастровых работ не подтверждены. Слой предназначен для исследования; свободность земли и права не установлены.'
TOLERANCE_M = 3.0


def choices(result):
    found=[]
    for d in (result or {}).get('documents',[]):
        names={re.sub(r'[\s-]+','',x['label']).upper() for x in d.get('crs_mentions',[])}
        controls={m[1] for t in d.get('tables',[]) if t['state']=='review_required' and t.get('outline_xy') and (m:=LABEL.fullmatch(t['label']))}
        if d['state']=='extracted' and names=={'СК63'} and len(controls)==1:
            found.append({'document_id':d['document_id'],'title':d['title'],'cadastral_number':next(iter(controls))})
    return found


def document(project,params):
    current=store.get_setting('schemes_'+project)
    if not current or params.get('schemes_id')!=current['id']:
        raise ValueError('Таблицы изменились; обновите страницу')
    eligible=next((x for x in choices(current) if x['document_id']==params.get('document_id')),None)
    if not eligible:raise ValueError('Нужны таблицы с одним подписанным кадастровым объектом и единственным указанием СК-63')
    doc=next(d for d in current['documents'] if d['document_id']==eligible['document_id'])
    digest=doc['source_sha256']
    if not re.fullmatch(r'[0-9a-f]{64}',digest):raise ValueError('Некорректный SHA-256 PDF')
    path=store.DATA/'municipal'/(digest+'.pdf')
    if not path.is_file() or path.stat().st_size>8*1024*1024 or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
        raise ValueError('Сохранённый исходный PDF отсутствует или изменился')
    return current,doc,eligible['cadastral_number']


def operation():
    group=TransformerGroup(CRS.from_epsg(7829),CRS.from_epsg(4326),always_xy=True,
                           allow_ballpark=False,area_of_interest=AreaOfInterest(34,44.33,37,46))
    # Chosen authority operation is fixed before observing residuals, never fitted to the parcel.
    selected=next((t for t in group.transformers if any(op.to_json_dict().get('id')=={'authority':'EPSG','code':5044} for op in t.operations)),None)
    if selected is None or selected.accuracy!=3:raise ValueError('Проверяемое преобразование EPSG:5044 недоступно или изменено')
    return selected


def receive(cad):
    url=nspd.BASE+'/api/geoportal/v2/search/geoportal?'+urlencode({'thematicSearchId':1,'query':cad})
    raw,digest=nspd.request_json(url)
    return {'source':url,'sha256':digest,'received_at':store.now(),'geojson':nspd.normalize(raw)}


def control_window(doc,cad):
    """Fixed transform only locates a bounded query; it does not validate the CRS."""
    tables=[t for t in doc['tables'] if t['state']=='review_required' and t.get('outline_xy')
            and (m:=LABEL.fullmatch(t['label'])) and m[1]==cad]
    if not tables:raise ValueError('Нет подписанных таблиц контрольного участка для запроса по области')
    selected=operation()
    polygons=[transform(lambda x,y:selected.transform(y,x,errcheck=True),Polygon(t['outline_xy'])) for t in tables]
    if any(g.is_empty or not g.is_valid or not box(34,44.33,37,46).covers(g) for g in polygons):
        raise ValueError('Область контрольного запроса некорректна или вне проверяемого варианта СК-63')
    candidate=unary_union(polygons)
    metric_crs=32600+int((candidate.centroid.x+180)//6)+1
    forward=Transformer.from_crs(4326,metric_crs,always_xy=True)
    inverse=Transformer.from_crs(metric_crs,4326,always_xy=True)
    metric=transform(forward.transform,candidate).envelope.buffer(25).envelope
    if metric.area>1_000_000:raise ValueError('Область контрольного запроса превышает 1 км²')
    bounds=list(transform(inverse.transform,metric).bounds)
    if not box(34,44.33,37,46).covers(box(*bounds)):
        raise ValueError('Область контрольного запроса вне проверяемого варианта СК-63')
    query_area=transform(forward.transform,box(*bounds)).area
    if query_area>1_000_000:raise ValueError('Итоговая область контрольного запроса превышает 1 км²')
    return bounds,{'basis':'signed_control_tables_fixed_transform','source_crs':'EPSG:7829',
                   'operation_code':'EPSG:5044','padding_m':25,'metric_crs':f'EPSG:{metric_crs}',
                   'window_area_m2':round(metric.area,2),'query_area_m2':round(query_area,2),'crs_confirmed':False}


def receive_spatial(doc,cad):
    bounds,basis=control_window(doc,cad)
    category=nspd.catalog()['parcels']['categoryId']
    body=nspd.spatial_body(bounds,category)
    raw,digest=nspd.request_json(nspd.INTERSECTS,body)
    fc=nspd.normalize(raw)
    if any(f['properties'].get('category')!=category for f in fc['features']):
        raise ValueError('Категория объектов не соответствует контрольному слою ЕГРН')
    matched=[f for f in fc['features'] if f['properties'].get('options',{}).get('cad_num')==cad]
    if len(matched)!=1:raise ValueError('Пространственный ответ не содержит единственный точный номер контрольного участка')
    reference={'source':nspd.INTERSECTS,'sha256':digest,'received_at':store.now(),'geojson':fc,
               'retrieval_method':'spatial_control_window','requested_cadastral_number':cad,
               'request':body,'query_bounds':bounds,'window_basis':basis,'response_count':len(fc['features'])}
    folder=store.DATA/'georeference_sources';folder.mkdir(exist_ok=True)
    snapshot=digest+'-'+hashlib.sha256(json.dumps(reference,sort_keys=True).encode()).hexdigest()[:20]+'.json'
    reference['source_snapshot']=snapshot
    (folder/snapshot).write_text(json.dumps({'reference':reference,'raw_response':raw},ensure_ascii=False,indent=2),encoding='utf-8')
    return reference


def parts(g):
    return list(g.geoms) if g.geom_type=='MultiPolygon' else [g]


def vertices(g):
    return [Point(p) for polygon in parts(g) for ring in [polygon.exterior,*polygon.interiors] for p in ring.coords]


def vertex_gap(a,b):
    return max([p.distance(b.boundary) for p in vertices(a)]+[p.distance(a.boundary) for p in vertices(b)])


def boundary_gap(a,b):
    return float(hausdorff_distance(segmentize(a.boundary,1),segmentize(b.boundary,1)))


def evaluate(doc,schemes_id,cad,reference,survey=None):
    features=reference['geojson']['features']
    matched=[f for f in features if f['properties'].get('options',{}).get('cad_num')==cad
             and f['properties'].get('category')==nspd.catalog()['parcels']['categoryId']]
    if len(matched)!=1:raise ValueError('Ответ не содержит единственную геометрию контрольного участка ЕГРН')
    control=shape(matched[0]['geometry'])
    if control.geom_type not in ('Polygon','MultiPolygon') or not control.is_valid or control.is_empty:
        raise ValueError('Некорректная контрольная геометрия')
    if not box(34,44.33,37,46).covers(control):raise ValueError('Контрольная геометрия вне области проверяемого варианта СК-63')
    accepted=[(i,t) for i,t in enumerate(doc['tables']) if t['state']=='review_required' and t.get('outline_xy')]
    ref_tables=[t for _,t in accepted if (m:=LABEL.fullmatch(t['label'])) and m[1]==cad]
    if not ref_tables:raise ValueError('Нет таблиц, подписанных именно контрольным номером')
    selected=operation()
    def convert(t):
        # PDF X is northing, Y is easting. always_xy expects easting/northing.
        return transform(lambda x,y:selected.transform(y,x,errcheck=True),Polygon(t['outline_xy']))
    converted=[(i,t,convert(t)) for i,t in accepted]
    if any(not g.is_valid or not box(34,44.33,37,46).covers(g) for _,_,g in converted):
        raise ValueError('Преобразованные таблицы некорректны или вне области СК')
    candidate=unary_union([g for _,t,g in converted if t in ref_tables])
    zone=int((control.centroid.x+180)//6)+1
    metric_crs=32600+zone
    metric=Transformer.from_crs(4326,metric_crs,always_xy=True)
    cg,rg=[transform(metric.transform,g) for g in (candidate,control)]
    left,right=parts(cg),parts(rg)
    if max(len(left),len(right))>20:raise ValueError('Слишком много частей контрольного объекта')
    if max(cg.boundary.length,rg.boundary.length)>5000 or max(len(vertices(cg)),len(vertices(rg)))>2000:
        raise ValueError('Контроль слишком велик для ограниченной проверки границ с шагом 1 м')
    distance=boundary_gap(cg,rg)
    vertex_distance=vertex_gap(cg,rg)
    area_delta=abs(cg.area-rg.area)/rg.area
    part_checks=[]
    for i,p in enumerate(left):
        fits=[(j,vertex_gap(p,q)) for j,q in enumerate(right)]
        within=[(j,delta) for j,delta in fits if delta<=TOLERANCE_M and abs(p.area-right[j].area)/right[j].area<=.01]
        part_checks.append({'preview_part':i+1,'matching_reference_parts':[j+1 for j,_ in within],
                            'nearest_vertex_distance_m':round(min(delta for _,delta in fits),4)})
    unique=[x['matching_reference_parts'][0] for x in part_checks if len(x['matching_reference_parts'])==1]
    coordinates_consistent=vertex_distance<=TOLERANCE_M and area_delta<=.01 and len(left)==len(right)==len(unique)==len(set(unique))
    boundary_consistent=coordinates_consistent and distance<=TOLERANCE_M
    with sqlite3.connect(Path(datadir.get_data_dir())/'proj.db') as db:
        epsg_metadata=dict(db.execute("SELECT key,value FROM metadata WHERE key IN ('EPSG.VERSION','EPSG.DATE')"))
    result={'document_id':doc['document_id'],'title':doc['title'],'url':doc['url'],
            'source_pdf_sha256':doc['source_sha256'],'source_pdf_received_at':doc['source_received_at'],
            'schemes_id':schemes_id,'cadastral_number':cad,'reference':{k:reference[k] for k in ('source','sha256','received_at','retrieval_method',
                'requested_cadastral_number','request','query_bounds','window_basis','response_count','source_snapshot') if k in reference},
            'reference_geojson':{'type':'FeatureCollection','features':matched},
            'state':'consistent_with_reference' if boundary_consistent else 'vertices_consistent_boundary_differs' if coordinates_consistent else 'reference_disagrees',
            'warning':WARNING,'source_crs':'EPSG:7829','source_axes':'PDF X=northing, Y=easting',
            'operation_code':'EPSG:5044','operation_description':selected.description,'pipeline':selected.definition,
            'operation_expected_accuracy_m':selected.accuracy,'check_tolerance_m':TOLERANCE_M,
            'metric_crs':f'EPSG:{metric_crs}','pyproj_version':pyproj.__version__,'proj_version':pyproj.proj_version_str,
            'epsg_database':epsg_metadata,'boundary_distance_m':round(distance,4),
            'boundary_sampling_m':1,'vertex_max_distance_m':round(vertex_distance,4),
            'coordinates_consistent':coordinates_consistent,'boundary_consistent':boundary_consistent,
            'preview_hole_count':sum(len(p.interiors) for p in left),'reference_hole_count':sum(len(p.interiors) for p in right),
            'area_difference_percent':round(area_delta*100,6),'centroid_distance_m':round(cg.centroid.distance(rg.centroid),4),
            'intersection_over_union':round(cg.intersection(rg).area/cg.union(rg).area,6),
            'reference_local_area_m2':round(unary_union([Polygon(t['outline_xy']) for t in ref_tables]).area,2),
            'reference_stated_area_m2':matched[0]['properties'].get('options',{}).get('specified_area'),
            'part_checks':part_checks,'preview_georeferenced':coordinates_consistent,'geometry_confirmed':False,
            'survey_id':(survey or {}).get('id'),'survey_bounds':(survey or {}).get('bounds'),
            'preview_geojson':{'type':'FeatureCollection','features':[]}}
    if coordinates_consistent:
        area=box(*survey['bounds']) if survey else None
        for i,t,g in converted:
            result['preview_geojson']['features'].append({'type':'Feature','geometry':mapping(g),
                'properties':{'table_index':i,'label':t['label'],'purpose_hint':t['purpose_hint'],
                              'reference_table':t in ref_tables,'geometry_confirmed':False,'usage':'research_preview_only',
                              'source_crs':'EPSG:7829','operation_code':'EPSG:5044','source_pdf_sha256':doc['source_sha256'],
                              'intersects_survey_area':g.intersects(area) if area is not None else None}})
        result['intersecting_tables']=sum(f['properties']['intersects_survey_area'] is True for f in result['preview_geojson']['features'])
    return result


def persist(project,result):
    result['previous_result_id']=(store.get_setting('georeference_'+project) or {}).get('id')
    result['checked_at']=store.now()
    result['id']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()[:20]
    folder=store.DATA/'georeference';folder.mkdir(exist_ok=True)
    (folder/(result['id']+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    store.set_setting('georeference_'+project,result)
    with store.connect() as db:store.event(db,project,'scheme_georeference',{'id':result['id'],'state':result['state'],'document_id':result['document_id']})


def run(project,params):
    current,doc,cad=document(project,params)
    mode=params.get('reference_mode','spatial')
    if mode not in ('spatial','number'):raise ValueError('Неизвестный способ получения контрольного объекта')
    attempt={'state':'running','started_at':store.now(),'document_id':doc['document_id'],'cadastral_number':cad,'reference_mode':mode}
    store.set_setting('georeference_attempt_'+project,attempt)
    try:
        reference=receive_spatial(doc,cad) if mode=='spatial' else receive(cad)
        survey=store.get_setting('survey_'+project)
        result=evaluate(doc,current['id'],cad,reference,survey)
        with store.LOCK:
            document(project,params)  # Recheck revision and source bytes after network/transform work.
            if (store.get_setting('survey_'+project) or {}).get('id')!=(survey or {}).get('id'):
                raise ValueError('Обследование изменилось во время проверки; прежняя геопривязка сохранена')
            persist(project,result)
        attempt.update(state='done',finished_at=store.now(),result_state=result['state'])
        return {'id':result['id'],'state':result['state']}
    except Exception as exc:
        attempt.update(state='error',error=str(exc)[:500],finished_at=store.now());raise
    finally:store.set_setting('georeference_attempt_'+project,attempt)
