"""Bounded published general-plan observations, separate from territorial PZZ."""
import copy
import hashlib
import json
import math
import time
from datetime import datetime
from urllib.parse import urlencode

from shapely.geometry import box, mapping, shape
from shapely.validation import explain_validity

from . import network, nspd, store
from .geometry import convert

VERSION=2
SUPPORTED_VERSIONS=(1,VERSION)
WFS='https://rgis.rk.gov.ru/geoserver/wfs'
MAP='https://rgis.rk.gov.ru/map/Map.aspx#/maps/00404cd9-ad64-4ab7-9ccc-bb3b2dda915e'
TITLES={'functional':'Функциональные зоны генплана','settlements':'Населённые пункты генплана','roads':'Дорожные контуры генплана'}
TYPES={'functional':'FunctionalZones_polygon','settlements':'NasPunkty_polygon','roads':'UDS_polygon'}
FIELDS=('OBJECTID','CLASS_ID','STAT_OBJ_U','TYPE_ZONE_','TYPE_ZONE','SUBTYPE_ZO','SUBSUBTYPE','NAME','NAME_MO','LOCATIONS','VALUE_ROAD','STATUS_ROA','CATEGORY_R','LOCATION_R','TYPE_ROAD_','TYPE_PAVEM')
MAX_FEATURES=1000
MAX_COORDINATES=200000
WARNING=('Это полученные слои карты «ГП 2019г. Симферопольский район» РГИС Крыма. '
         'Функциональные зоны генплана не являются территориальными зонами ПЗЗ. '
         'Коды сохранены без неподтверждённой расшифровки. Утверждающий акт, действующая редакция, '
         'актуальность границ и точность исходной системы координат не установлены. '
         'Слои не исключают землю из подбора и не подтверждают ИЖС, права, полноту сведений или законный подъезд.')
CATALOG={'checked_at':'2026-10-08T09:27:20+00:00','map_url':MAP,'map_name':'ГП 2019г. Симферопольский район',
         'map_id':'00404cd9-ad64-4ab7-9ccc-bb3b2dda915e',
         'metadata_sha256':'54ea03bfb7cf4212c61778923941263bdd3376630e8020f94c0f2ec030f920b0',
         'capabilities_sha256':'e91ec0d01809888e1c333592340d4d1f9462a9871d1a6313587164090622396f',
         'layer_ids':{'functional':'5215adaa-ddf6-400b-a500-06d56170833b','settlements':'c415dfef-26cd-44cc-b902-778be4f4a35a','roads':'0aea85f5-3deb-457d-abfe-f054adeef3d7'},
         'default_srs_observed':'urn:x-ogc:def:crs:EPSG:19635','requested_srs':'EPSG:3857',
         'provenance':'Official OData MapLayer Settings and WFS 1.1.0 GetCapabilities, not inferred names/IDs'}


def parameters(bounds,mode):
    nspd.spatial_body(bounds,36368)  # The same bounded WGS84 area validation, no network.
    if mode not in TYPES:raise ValueError('Неизвестный слой генплана РГИС')
    b=convert(box(*bounds),4326,3857).bounds
    return {'service':'WFS','version':'1.1.0','request':'GetFeature','typeName':'genplanrk:'+TYPES[mode],
            'srsName':'EPSG:3857','bbox':','.join(map(str,b))+',EPSG:3857',
            'outputFormat':'application/json','maxFeatures':MAX_FEATURES}


def _count(value):
    if isinstance(value,bool) or not isinstance(value,int) or value<0:
        raise ValueError('Не подтверждено число объектов ответа РГИС')
    return value


def _coordinates(geom,budget):
    """Validate polygon structure and all coordinates before any quarantine."""
    polygons=geom.get('coordinates')
    if geom['type']=='Polygon':polygons=[polygons]
    else:budget+=1  # Count the MultiPolygon root, preserving the prior coordinate budget.
    if not isinstance(polygons,(list,tuple)) or not polygons:
        raise ValueError('Некорректные координаты РГИС')
    for polygon in polygons:
        budget+=1
        if not isinstance(polygon,(list,tuple)) or not polygon:
            raise ValueError('Некорректные координаты РГИС')
        for ring in polygon:
            budget+=1
            if not isinstance(ring,(list,tuple)) or len(ring)<4:
                raise ValueError('Некорректное кольцо геометрии РГИС')
            for point in ring:
                budget+=1
                if budget>MAX_COORDINATES:
                    raise ValueError('Превышен предел координат РГИС')
                if (not isinstance(point,(list,tuple)) or len(point)!=2
                        or any(isinstance(value,bool) or not isinstance(value,(int,float))
                               or not math.isfinite(value) or abs(value)>20040000 for value in point)):
                    raise ValueError('Некорректные координаты EPSG:3857 РГИС')
            if ring[0]!=ring[-1]:
                raise ValueError('Незамкнутое кольцо геометрии РГИС')
    return budget


def _properties(props):
    if not isinstance(props,dict):
        raise ValueError('Некорректные свойства РГИС')
    # Validate every source property, including discarded fields. A damaged
    # record cannot hide a structural/size problem behind invalid topology.
    for key,value in props.items():
        if (not isinstance(key,str) or len(key)>2000
                or value is not None and (
                    not isinstance(value,(str,int,float,bool))
                    or isinstance(value,str) and len(value)>2000
                    or isinstance(value,float) and not math.isfinite(value))):
            raise ValueError('Некорректное значение свойства РГИС')
    return {key:copy.deepcopy(props[key]) for key in FIELDS if key in props}


def _normalize(data,bounds,mode,allow_invalid_geometry):
    parameters(bounds,mode)  # Shared bounded area validation; no network.
    if not isinstance(data,dict) or data.get('type')!='FeatureCollection' or not isinstance(data.get('features'),list):
        raise ValueError('РГИС не вернула GeoJSON FeatureCollection')
    features=data['features'];count=len(features)
    if count>MAX_FEATURES:raise ValueError('Слишком много объектов РГИС; уменьшите область')
    if count>=MAX_FEATURES:raise ValueError('Достигнут предел ответа РГИС; уменьшите область')
    if any(_count(data.get(key))!=count for key in ('totalFeatures','numberMatched','numberReturned')):
        raise ValueError('Неполный или изменившийся ответ РГИС')
    if data.get('links') or data.get('next') or data.get('@odata.nextLink'):
        raise ValueError('Ответ РГИС содержит продолжение; уменьшите область')
    crs=data.get('crs')
    if crs is not None and crs!={'type':'name','properties':{'name':'urn:ogc:def:crs:EPSG::3857'}}:
        raise ValueError('Система координат ответа РГИС отличается от EPSG:3857')
    if count and crs is None:raise ValueError('Для геометрии РГИС не указана система координат')
    query=convert(box(*bounds),4326,3857);seen=set();result=[];rejected=[];budget=0
    for f in features:
        if (not isinstance(f,dict) or f.get('type')!='Feature' or not isinstance(f.get('id'),str)
                or not f['id'] or len(f['id'])>200 or f['id'] in seen):
            raise ValueError('Некорректный/повторный ID объекта РГИС')
        prefix=TYPES[mode]+'.'
        if not f['id'].startswith(prefix) or len(f['id'])==len(prefix):
            raise ValueError('Ответ РГИС относится к другому слою')
        seen.add(f['id']);geom=f.get('geometry')
        if not isinstance(geom,dict) or geom.get('type') not in ('Polygon','MultiPolygon'):
            raise ValueError('Ожидалась полигональная геометрия РГИС')
        budget=_coordinates(geom,budget)
        fields=_properties(f.get('properties'))
        g=shape(geom)
        if g.is_empty or not g.intersects(query):
            raise ValueError('Невалидная или не относящаяся к области геометрия РГИС')
        if not g.is_valid:
            if not allow_invalid_geometry:
                raise ValueError('Невалидная или не относящаяся к области геометрия РГИС')
            digest=hashlib.sha256(json.dumps(geom,sort_keys=True,separators=(',',':'),
                                            ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()
            rejected.append({'id':f['id'],'reason':explain_validity(g),
                             'original_geometry_sha256':digest,'geometry_digest_encoding':'canonical-json',
                             'original_crs':'EPSG:3857','bounds':list(g.bounds),
                             'geometry_repaired':False})
            continue
        world=convert(g,3857,4326)
        if not world.is_valid:raise ValueError('Не удалось преобразовать геометрию РГИС')
        result.append({'type':'Feature','id':f['id'],'geometry':mapping(world),'properties':fields})
    warnings=[]
    if rejected:
        warnings.append(f'Из {count} полученных объектов РГИС принято {len(result)}, '
                        f'невалидная геометрия {len(rejected)} исключена из геометрических операций '
                        'без исправления. Наблюдение частичное; отсутствие совпадений не подтверждает отсутствие зон или дорог.')
        if not result:
            warnings.append('Пригодная геометрия не получена: все объекты отклонены. Это не пустой ответ источника.')
    return {'geojson':{'type':'FeatureCollection','features':result},
            'received_count':count,'accepted_count':len(result),'invalid_count':len(rejected),
            'rejected_geometries':rejected,'state':'partial' if rejected else 'done',
            'geometry_processing_complete':not rejected,'warnings':warnings}


def normalize(data,bounds,mode):
    """Strict compatibility API: any invalid source geometry rejects the response."""
    return _normalize(data,bounds,mode,False)['geojson']


def normalize_partial(data,bounds,mode):
    """Explicitly quarantine topology defects; structural/source errors still fail closed."""
    return _normalize(data,bounds,mode,True)


def collect(bounds):
    parameters(bounds,'functional');layers={}
    for mode in TYPES:
        params=parameters(bounds,mode);url=WFS+'?'+urlencode(params)
        raw,ct,status=network.fetch(url,max_bytes=5*1024*1024)
        if status!=200:raise ValueError('РГИС не вернула полный HTTP200 ответ')
        digest=hashlib.sha256(raw).hexdigest();folder=store.DATA/'rgis'/'raw';folder.mkdir(parents=True,exist_ok=True)
        store.atomic_write(folder/(digest+'.json'),raw)
        received=store.now();data=json.loads(raw);observation=normalize_partial(data,bounds,mode)
        fc=observation['geojson']
        server_time=data.get('timeStamp');clock_warning=False
        try:clock_warning=datetime.fromisoformat(server_time.replace('Z','+00:00'))>datetime.fromisoformat(received)
        except (ValueError,TypeError,AttributeError):clock_warning=bool(server_time)
        layers[mode]={'title':TITLES[mode],'source':WFS,'request_url':url,'request':params,'sha256':digest,
                      'received_at':received,'http_status':status,'content_type':ct,'count':len(fc['features']),
                      'geojson':fc,'response_counts':{key:data[key] for key in ('totalFeatures','numberMatched','numberReturned')},
                      'server_timestamp':server_time,'server_clock_warning':clock_warning,
                      'response_count_consistent':True,'coverage_confirmed':False,'currentness_confirmed':False,
                      'territorial_zone_confirmed':False,'used_for_exclusion':False,
                      'source_crs_confirmed':data.get('crs') is not None,'requested_crs':'EPSG:3857','normalized_crs':'EPSG:4326',
                      **{key:value for key,value in observation.items() if key!='geojson'}}
        if mode!='roads':time.sleep(.8)
    warnings=[TITLES[mode]+': '+warning for mode,layer in layers.items() for warning in layer['warnings']]
    result={'version':VERSION,'created_at':store.now(),'bounds':list(bounds),'catalog':copy.deepcopy(CATALOG),'layers':layers,
            'state':'partial' if warnings else 'done','warnings':warnings,
            'warning':WARNING,'territorial_zone_confirmed':False,'currentness_confirmed':False,
            'coverage_confirmed':False,'used_for_exclusion':False}
    result['id']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()[:20]
    return result


def run(project,params):
    bounds=list(params.get('bounds',[]));parameters(bounds,'functional')
    old=store.get_setting('rgis_context_'+project)
    attempt={'state':'running','started_at':store.now(),'bounds':bounds};store.set_setting('rgis_attempt_'+project,attempt)
    try:
        result=collect(bounds)
        with store.LOCK:
            if store.get_setting('rgis_context_'+project)!=old:raise ValueError('Контекст РГИС изменился во время обновления')
            folder=store.DATA/'rgis';folder.mkdir(parents=True,exist_ok=True)
            store.atomic_write(folder/(result['id']+'.json'),json.dumps(result,ensure_ascii=False).encode('utf-8'))
            store.set_setting('rgis_context_'+project,result)
            with store.connect() as db:store.event(db,project,'rgis_context',{'id':result['id'],'bounds':bounds,
                'state':result['state'],'counts':{mode:l['count'] for mode,l in result['layers'].items()},
                'invalid_counts':{mode:l['invalid_count'] for mode,l in result['layers'].items()}})
        attempt.update(state=result['state'],finished_at=store.now(),result_id=result['id'],warnings=result['warnings'])
        return {'id':result['id'],'state':result['state'],'warnings':result['warnings'],
                'counts':{mode:l['count'] for mode,l in result['layers'].items()},
                'invalid_counts':{mode:l['invalid_count'] for mode,l in result['layers'].items()}}
    except Exception as exc:
        attempt.update(state='error',finished_at=store.now(),error=str(exc)[:500]);raise
    finally:store.set_setting('rgis_attempt_'+project,attempt)


def applicable(context,bounds):
    return bool(context and isinstance(context.get('version'),int) and not isinstance(context.get('version'),bool)
                and context.get('version') in SUPPORTED_VERSIONS and context.get('bounds')==bounds)


def projected(context,bounds,metric):
    return {mode:[(convert(shape(f['geometry']),4326,metric),f) for f in context['layers'][mode]['geojson']['features']]
            if applicable(context,bounds) else [] for mode in TYPES}


def relate(g,entries,context):
    result={mode:[] for mode in TYPES}
    for mode,rows in entries.items():
        for other,f in rows:
            area=g.intersection(other).area
            if area<=.01:continue
            layer=context['layers'][mode]
            result[mode].append({'id':f['id'],'fields':copy.deepcopy(f['properties']),'area_m2':round(area,2),
                                 'spatially_covers':other.covers(g),'source':WFS,'received_at':layer['received_at'],
                                 'sha256':layer['sha256'],'currentness_confirmed':False,'territorial_zone_confirmed':False,
                                 'coverage_confirmed':False,'used_for_exclusion':False,
                                 'source_state':layer.get('state','unknown_legacy'),
                                 'source_invalid_count':layer.get('invalid_count')})
    return result


def sources(context,bounds):
    layers={}
    for mode,layer in (context or {}).get('layers',{}).items():
        observed={key:copy.deepcopy(value) for key,value in layer.items() if key!='geojson'}
        observed.setdefault('state','unknown_legacy')
        observed.setdefault('geometry_processing_complete',None)
        observed.setdefault('received_count',None)
        observed.setdefault('accepted_count',layer.get('count'))
        observed.setdefault('invalid_count',None)
        observed.setdefault('rejected_geometries',[])
        observed.setdefault('warnings',[])
        observed.update(coverage_confirmed=False,currentness_confirmed=False,
                        territorial_zone_confirmed=False,used_for_exclusion=False)
        layers[mode]=observed
    return {'id':(context or {}).get('id'),'version':(context or {}).get('version'),
            'applied':applicable(context,bounds),'warning':WARNING,
            'state':(context or {}).get('state','unknown_legacy'),
            'warnings':copy.deepcopy((context or {}).get('warnings',[])),
            'coverage_confirmed':False,'currentness_confirmed':False,
            'territorial_zone_confirmed':False,'used_for_exclusion':False,
            'catalog':copy.deepcopy((context or {}).get('catalog')),'layers':layers}
