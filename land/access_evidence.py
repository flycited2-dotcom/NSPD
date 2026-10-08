"""Bounded geometry observations about a straight segment, never lawful access."""
import copy
import math
from dataclasses import dataclass

from pyproj import Transformer
from shapely.geometry import LineString, Polygon, MultiPolygon, box, mapping, shape
from shapely.ops import nearest_points, transform

from . import survey as survey_module

ALGORITHM='candidate-access-v1'
MAX_FEATURES=5000
MAX_COORDINATES=200000
WARNING=('Расстояния и пересечения рассчитаны только по полученным геометриям. '
         'Прямой отрезок не является маршрутом или коридором проезда. Назначение соседнего участка '
         'не устанавливает право доступа. Отсутствие полученных препятствий, пустой слой и нулевое '
         'расстояние не подтверждают фактический проезд, полноту сведений или законный доступ.')


def _polygon(geometry,budget):
    if not isinstance(geometry,dict) or geometry.get('type') not in ('Polygon','MultiPolygon'):
        raise ValueError('Для наблюдения доступа требуется Polygon/MultiPolygon WGS84')
    polygons=[geometry.get('coordinates')] if geometry['type']=='Polygon' else geometry.get('coordinates')
    if not isinstance(polygons,(list,tuple)):
        raise ValueError('Некорректные координаты геометрии')
    budget[0]+=len(polygons)
    if budget[0]>MAX_COORDINATES:raise ValueError('Превышен предел координат наблюдения доступа')
    for rings in polygons:
        if not isinstance(rings,(list,tuple)):
            raise ValueError('Некорректные кольца геометрии')
        budget[0]+=len(rings)
        if budget[0]>MAX_COORDINATES:raise ValueError('Превышен предел координат наблюдения доступа')
        for ring in rings:
            if not isinstance(ring,(list,tuple)):
                raise ValueError('Некорректное кольцо геометрии')
            budget[0]+=len(ring)
            if budget[0]>MAX_COORDINATES:
                raise ValueError('Превышен предел координат наблюдения доступа')
            for point in ring:
                if (not isinstance(point,(list,tuple)) or len(point)!=2
                        or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in point)
                        or not -180<=point[0]<=180 or not -85<=point[1]<=85):
                    raise ValueError('Координаты должны быть конечными долгота/широта WGS84')
    try:g=shape(geometry)
    except Exception as exc:raise ValueError('Некорректная геометрия наблюдения доступа') from exc
    if g.is_empty or not g.is_valid or not isinstance(g,(Polygon,MultiPolygon)):
        raise ValueError('Геометрия наблюдения доступа пуста или невалидна')
    return g


def _record(feature,layer,distance):
    properties=feature.get('properties') or {}
    return {'feature_id':feature['id'],
            'cadastral_number':(properties.get('options') or {}).get('cad_num') or properties.get('label'),
            'distance_m':round(distance,2),
            **{key:layer.get(key) for key in ('source','received_at','sha256','sha256_kind')}}


@dataclass(frozen=True)
class PreparedSurvey:
    """One calculation's source snapshot; never shared through a global cache."""
    survey_id: object
    boundary: Polygon
    metric_crs: str
    forward: object
    inverse: object
    layers: dict
    observations: dict
    road_ids: frozenset
    coordinate_count: int


def _projected(geometry,budget,forward):
    result=transform(forward,_polygon(geometry,budget))
    if not all(math.isfinite(v) for v in result.bounds) or not result.is_valid:
        raise ValueError('Не удалось спроецировать геометрию наблюдения доступа')
    return result


def prepare(survey):
    """Validate and project observed layers once for all candidates in this survey."""
    bounds=survey.get('bounds')
    if (not isinstance(bounds,(list,tuple)) or len(bounds)!=4
            or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in bounds)
            or not -180<=bounds[0]<bounds[2]<=180 or not -85<=bounds[1]<bounds[3]<=85):
        raise ValueError('Не установлена корректная область обследования WGS84')
    boundary=box(*bounds)
    metric=f'+proj=laea +lat_0={boundary.centroid.y} +lon_0={boundary.centroid.x} +datum=WGS84 +units=m +no_defs'
    forward=Transformer.from_crs(4326,metric,always_xy=True).transform
    inverse=Transformer.from_crs(metric,4326,always_xy=True).transform
    budget=[0]
    layers=survey.get('layers') or {}
    observations={};provenance={}
    for mode in ('parcels','buildings'):
        layer=layers.get(mode) or {}
        provenance[mode]=copy.deepcopy({key:layer.get(key) for key in ('source','received_at','sha256','sha256_kind')})
        features=(layer.get('geojson') or {}).get('features',[])
        if not isinstance(features,list) or len(features)>MAX_FEATURES:
            raise ValueError('Превышен предел объектов наблюдения доступа')
        seen=set();rows=[]
        for feature in features:
            if not isinstance(feature,dict) or not isinstance(feature.get('id'),(str,int)) or isinstance(feature['id'],bool):
                raise ValueError('Не установлен идентификатор наблюдаемого объекта')
            if feature['id'] in seen:raise ValueError('Повтор идентификатора наблюдаемого объекта')
            seen.add(feature['id'])
            if not isinstance(feature.get('properties') or {},dict) or not isinstance((feature.get('properties') or {}).get('options') or {},dict):
                raise ValueError('Некорректные свойства наблюдаемого объекта')
            g=_projected(feature.get('geometry'),budget,forward)
            # Keep only fields used by designation/provenance, independently of
            # subsequent changes to the caller's source dictionaries.
            properties=feature.get('properties') or {}
            options=properties.get('options') or {}
            snapshot={'id':feature['id'],'properties':copy.deepcopy({
                'label':properties.get('label'),'options':{key:options[key] for key in
                    ('cad_num','permitted_use_established_by_document') if key in options}})}
            rows.append((snapshot,g))
        observations[mode]=tuple(rows)
    road_ids=frozenset(feature['id'] for feature in survey_module.road_features(
        {'parcels':{'geojson':{'features':[feature for feature,_ in observations['parcels']]}}}))
    return PreparedSurvey(copy.deepcopy(survey.get('id')),boundary,metric,forward,inverse,
                          provenance,observations,road_ids,budget[0])


def assessment(candidate,survey):
    """Compatibility entry point for a single candidate."""
    return assessment_prepared(candidate,prepare(survey))


def assessment_prepared(candidate,prepared):
    """Observe a candidate using one prepared survey, with its own combined budget."""
    if not isinstance(prepared,PreparedSurvey):
        raise ValueError('Требуется подготовленное обследование для наблюдения доступа')
    budget=[prepared.coordinate_count]
    target=_projected(candidate.get('geometry'),budget,prepared.forward)
    layers=prepared.layers
    observations={mode:[(feature,g,target.distance(g)) for feature,g in rows]
                  for mode,rows in prepared.observations.items()}
    result={'algorithm':ALGORITHM,'candidate_id':candidate.get('id'),'survey_id':prepared.survey_id,
            'metric_crs':prepared.metric_crs,'state':'unknown_no_observed_road',
            'nearest_parcel':None,'nearest_building':None,'road':None,'direct_segment':None,
            'intersections':{'parcels':[],'buildings':[]},'touches':{'parcels':[],'buildings':[]},
            'observed_counts':{mode:len(rows) for mode,rows in observations.items()},
            'coverage_confirmed':False,'legal_access_confirmed':False,'warning':WARNING}
    for mode,name in (('parcels','nearest_parcel'),('buildings','nearest_building')):
        if observations[mode]:
            feature,g,distance=min(observations[mode],key=lambda row:(row[2],str(row[0]['id'])))
            result[name]=_record(feature,layers.get(mode) or {},distance)
    parcels=layers.get('parcels') or {}
    roads=[row for row in observations['parcels'] if row[0]['id'] in prepared.road_ids]
    result['observed_counts']['road_features']=len(roads)
    if not roads:return result
    feature,road,distance=min(roads,key=lambda row:(row[2],str(row[0]['id'])))
    result['road']=_record(feature,parcels,distance)
    start,end=nearest_points(target,road)
    segment=start if start.equals(end) else LineString([start,end])
    world=transform(prepared.inverse,segment)
    result['direct_segment']={'geometry':mapping(world),'length_m':round(segment.length,2),
                              'within_survey_bounds':prepared.boundary.covers(world)}
    for mode,rows in observations.items():
        for other,g,other_distance in rows:
            if mode=='parcels' and other['id']==feature['id']:continue
            intersection=segment.intersection(g)
            if intersection.is_empty:continue
            length=intersection.length
            record={**_record(other,layers.get(mode) or {},other_distance),
                    'intersection_length_m':round(length,6) or length}
            result['intersections' if length>0 else 'touches'][mode].append(record)
    for group in ('intersections','touches'):
        for mode in observations:result[group][mode].sort(key=lambda row:str(row['feature_id']))
    result['state']=('zero_distance' if segment.geom_type=='Point' else 'observed_intersections'
                     if any(result['intersections'].values()) else 'no_observed_intersections')
    return result
