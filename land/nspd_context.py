"""Observed planning footprints and quarter counters; never a coverage certificate."""
import copy
import hashlib
import json
import math
from shapely.geometry import shape
from . import nspd, store
from .geometry import convert

TITLES={
    'settlements':'Населённые пункты (полигоны)',
    'quarters':'Кадастровые кварталы',
    'schemes':'Земельные участки, образуемые по схеме расположения земельного участка',
    'planned_parcels':'Земельные участки, образуемые по проекту межевания территории',
    'red_lines':'Красные линии',
    'water':'Береговые линии (границы водных объектов) (полигональный)',
    'forests':'Лесничества',
    'protected':'Особо охраняемые природные территории',
    'heritage':'Территории объектов культурного наследия',
}
ENVIRONMENT=('water','forests','protected','heritage')
COUNTERS=('cnt_land','cnt_land_geom','cnt_land_not_geom','cnt_oks','cnt_oks_geom','cnt_oks_not_geom')
FIELDS=COUNTERS+('is_actual','is_conditional','status','date_cr','reg_numb_border','name')
WARNING='Пустой ответ не подтверждает отсутствие схем, красных линий или границ. Счётчики квартала относятся ко всему кварталу, а не к выделенной области; положение объектов без геометрии неизвестно. Действующая редакция документов и права не установлены.'


def catalog():
    rows=json.loads((nspd.CAPTURE/'layers-catalog.json').read_text(encoding='utf-8'))['layers']
    return {mode:next(row for row in rows if row['title'].strip()==title) for mode,title in TITLES.items()}


def collect(bounds,previous=None):
    nspd.spatial_body(bounds,36368)
    layers={};stopped_reason=None
    for mode,entry in catalog().items():
        row={'title':TITLES[mode],'category_id':entry['categoryId'],'source':nspd.INTERSECTS,
             'state':'not_requested' if stopped_reason else 'error','requested':False,'complete':False}
        try:
            if stopped_reason:
                row.update(checked_at=store.now(),stopped_reason=stopped_reason,
                           error='Категория не запрошена: '+stopped_reason)
            else:
                row['requested']=True
                payload,digest=nspd.request_json(nspd.INTERSECTS,nspd.spatial_body(bounds,entry['categoryId']))
                row['http_status']=200
                kinds=('Polygon','MultiPolygon','LineString','MultiLineString') if mode=='red_lines' else ('Polygon','MultiPolygon')
                fc=nspd.normalize(payload,kinds,FIELDS)
                if any(f['properties'].get('category')!=entry['categoryId'] for f in fc['features']):
                    raise ValueError('Категория объектов не соответствует запрошенному слою контекста')
                if len(fc['features'])>5000:raise ValueError('Слишком много объектов контекста; уменьшите область')
                row.update(state='received',received_at=store.now(),sha256=digest,http_status=200,count=len(fc['features']),geojson=fc)
        except Exception as exc:
            row.update(checked_at=store.now(),error=str(exc)[:500])
            if isinstance(exc,nspd.StatusError):row['http_status']=exc.status_code
            stopped_reason='Остановлено после ошибки слоя «'+TITLES[mode]+'»: '+row['error']
            row['stopped_reason']=stopped_reason
        if row['state']!='received':
            prior=(previous or {}).get('layers',{}).get(mode,{}) if applicable(previous,bounds) else {}
            prior_fc=prior.get('geojson')
            if isinstance(prior_fc,dict) and prior_fc.get('features') and prior.get('received_at'):
                row.update({key:copy.deepcopy(prior[key]) for key in ('geojson','count','received_at','sha256') if key in prior})
                row.update(state='retained',retained_from_context_id=previous['id'])
        layers[mode]=row
    result={'created_at':store.now(),'bounds':list(bounds),'layers':layers,'complete':False,'warning':WARNING}
    result['id']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()[:20]
    return result


def save(project,result):
    folder=store.DATA/'nspd_context';folder.mkdir(parents=True,exist_ok=True)
    (folder/(result['id']+'.json')).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    store.set_setting('nspd_context_'+project,result)
    with store.connect() as db:store.event(db,project,'nspd_context',{'id':result['id'],'bounds':result['bounds'],'states':{k:v['state'] for k,v in result['layers'].items()}})


def applicable(context,bounds):
    return bool(context and context.get('bounds')==bounds)


def fields(feature):
    o=feature.get('properties',{}).get('options',{})
    return {key:o[key] for key in ('cad_num','reg_numb_border','name','name_by_doc','type_boundary_value','legal_act_document_name','legal_act_document_number','legal_act_document_date')+FIELDS if key in o}


def nonnegative_count(value):
    if isinstance(value,bool):return None
    try:
        number=float(value)
        return int(number) if math.isfinite(number) and number>=0 and number.is_integer() else None
    except (ValueError,TypeError):return None


def projected(context,bounds,metric):
    layers=context['layers'] if applicable(context,bounds) else {}
    return {mode:[(convert(shape(f['geometry']),4326,metric),f) for f in layers.get(mode,{}).get('geojson',{}).get('features',[])] for mode in TITLES}


def relate(g,entries,context):
    matches={mode:[] for mode in TITLES}
    for mode,rows in entries.items():
        layer=(context or {}).get('layers',{}).get(mode,{})
        for other,f in rows:
            inter=g.intersection(other)
            linear=other.geom_type in ('LineString','MultiLineString')
            if (not linear and inter.area<=.01) or (linear and not g.intersects(other)):continue
            item={'id':f['id'],'fields':fields(f),'received_at':layer.get('received_at'),'sha256':layer.get('sha256'),
                  'area_m2':round(inter.area,2),'length_m':round(inter.length,2) if linear else None,'observation_state':layer.get('state'),'applies_legally_confirmed':False}
            if mode=='settlements':item['spatially_inside']=other.covers(g)
            if mode=='quarters':
                number=str(item['fields'].get('cad_num',''))
                item.update(counter_scope='whole_quarter',aggregate_zero_quarter=bool(number and number.split(':')[-1].strip('0')==''),
                            parcels_without_geometry=nonnegative_count(item['fields'].get('cnt_land_not_geom')))
            matches[mode].append(item)
    return matches


def flags(matches):
    result=[]
    if not matches['settlements']:result.append('Граница населённого пункта для контура не получена')
    elif not any(row['spatially_inside'] for row in matches['settlements']):result.append('Контур не целиком внутри одного полученного населённого пункта')
    for row in matches['quarters']:
        count=row.get('parcels_without_geometry')
        if count:
            prefix='Сводный нулевой квартал' if row['aggregate_zero_quarter'] else 'Квартал'
            result.append(f"{prefix} {row['fields'].get('cad_num','')}: {count} участков без геометрии в целом квартале; положение неизвестно")
    if matches['schemes']:result.append('Есть пересечение с полученными схемами расположения участков; проверить опубликованную процедуру')
    if matches['planned_parcels']:result.append('Есть пересечение с участками по проекту межевания; проверить документ и статус')
    if matches['red_lines']:result.append('Есть пересечение с полученными красными линиями; требуется проверка документа')
    for mode in ENVIRONMENT:
        if matches[mode]:result.append('Есть пересечение: '+TITLES[mode]+'; режим территории и допустимость использования требуют проверки')
    for mode,rows in matches.items():
        if any(row.get('observation_state')=='retained' for row in rows):result.append(TITLES[mode]+': использовано прежнее датированное наблюдение; обновление не удалось')
    return result


def sources(context,bounds):
    used=applicable(context,bounds)
    return {'id':(context or {}).get('id'),'applied':used,'complete':False,'warning':WARNING,
            'layers':{mode:({k:row.get(k) for k in ('title','state','requested','received_at','checked_at','sha256','count','error','stopped_reason','http_status','source','category_id','retained_from_context_id')} if used and row else {'title':TITLES[mode],'state':'not_available'}) for mode,row in ((mode,(context or {}).get('layers',{}).get(mode,{})) for mode in TITLES)}}
