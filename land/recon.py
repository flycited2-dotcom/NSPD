"""One workflow from an observed area to dated, explicitly unverified candidates."""
import copy
import hashlib
import html
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from shapely.affinity import rotate
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union
from . import store, survey, nspd, nspd_context, torgi, torgi_docs, planning_boundary, planning_watch, land_status, planning_regulations, planning_enquiry
from .geometry import convert, polygons
from .review import CHECKS
from . import regional_documents, access_evidence, rgis_context, pzz_context

VERSION=12
KEYS=('survey','torgi','torgi_active','torgi_documents','torgi_active_documents','municipal','planning_watch','planning_maps','nspd_context','planning_boundary','planning_regulations','rgis_context','pzz_context')
PURPOSES={'unspecified':'Цель пока не выбрана','housing':'ИЖС','personal_farm':'ЛПХ','agriculture':'Сельскохозяйственное использование'}
WARNING='Контуры для проверки. Отсутствие полученного кадастрового объекта не подтверждает свободность земли. Права, полнота источников и допустимость использования не установлены.'
HOSTS={'nspd.gov.ru','torgi.gov.ru','trudovskoe-rk.ru','simf.rk.gov.ru','simfmo-rk.ru','rgis.rk.gov.ru'}


def options(params):
    result=survey.parameters(params)
    result.update(purpose=params.get('purpose','unspecified'),target_area=float(params.get('target_area',1000)),
                  limit=params.get('limit',10),refresh_nspd=params.get('refresh_nspd',True),
                  verify_nspd=params.get('verify_nspd',True),refresh_active_torgi=params.get('refresh_active_torgi',False),
                  refresh_active_documents=params.get('refresh_active_documents',False),
                  refresh_torgi=params.get('refresh_torgi',False),avoid_restrictions=params.get('avoid_restrictions',True),avoid_planned=params.get('avoid_planned',True),avoid_environment=params.get('avoid_environment',True),query=str(params.get('query','Трудовое')).strip())
    if result['purpose'] not in PURPOSES:raise ValueError('Неизвестная цель использования')
    if not math.isfinite(result['target_area']) or not result['min_area']<=result['target_area']<=result['max_area']:
        raise ValueError('Площадь пробного контура должна находиться между минимальной и максимальной')
    if math.sqrt(result['target_area'])<result['min_width']:raise ValueError('Выбранная площадь меньше квадрата заданной ширины')
    if isinstance(result['limit'],bool) or not isinstance(result['limit'],int) or not 1<=result['limit']<=20:raise ValueError('Допустимо от 1 до 20 контуров')
    if any(not isinstance(result[k],bool) for k in ('refresh_nspd','verify_nspd','refresh_torgi','refresh_active_torgi','refresh_active_documents','avoid_restrictions','avoid_planned','avoid_environment')):raise ValueError('Параметры обновления и исключения должны быть логическими')
    if not 2<=len(result['query'])<=120 or any(ord(c)<32 for c in result['query']):raise ValueError('Некорректный текст поиска торгов')
    return result


def inputs(project):
    return {key:store.get_setting(key+'_'+project) for key in KEYS}


def identities(values):
    return {key:(value.get('id') or hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()) if value else None for key,value in values.items()}


def safe_url(value):
    try:
        p=urlparse(str(value or ''))
        return str(value) if p.scheme=='https' and p.hostname in HOSTS and p.port in (None,443) and not (p.username or p.password) else ''
    except ValueError:return ''


def age_days(value):
    try:
        date=datetime.fromisoformat(value.replace('Z','+00:00'))
        if date.tzinfo is None:return None
        return (datetime.now(timezone.utc)-date).total_seconds()/86400
    except (ValueError,TypeError,AttributeError):return None


def metric_for(bounds):
    c=box(*bounds).centroid
    return f'+proj=laea +lat_0={c.y} +lon_0={c.x} +datum=WGS84 +units=m +no_defs'


def draft_plots(s,params,exclusions=()):
    """Bounded geometric hypotheses, without interpreting legal regimes."""
    metric=metric_for(s['bounds']);side=math.sqrt(params['target_area'])
    restricted=[convert(shape(f['geometry']),4326,metric) for f in s['layers'].get('restrictions',{}).get('geojson',{}).get('features',[])] if params['avoid_restrictions'] else []
    restricted.extend(exclusions)
    chosen=[];checks=0;limited=False;unplaced=[]
    gaps=s['gaps']['features'];per_gap=max(1,math.ceil(params['limit']/max(1,len(gaps))))
    for feature in gaps:
        if len(chosen)>=params['limit']:break
        original=shape(feature['geometry']);g=convert(original,4326,metric)
        centers=g.buffer(-side/2)
        accepted=[]
        if not centers.is_empty:
            seeds=[p.representative_point().coords[0] for p in polygons(centers)]
            left,bottom,right,top=centers.bounds;step=side/2
            nx=math.floor((right-left)/step)+1;ny=math.floor((top-bottom)/step)+1
            # Representatives first, then a bounded grid. Failure is not proof that no layout exists.
            def locations():
                yield from seeds
                for row in range(ny):
                    for col in range(nx):yield left+col*step,bottom+row*step
            for x,y in locations():
                if checks>=6000:limited=True;break
                for angle in (0,30,60):
                    if checks>=6000:limited=True;break
                    checks+=1
                    rectangle=rotate(box(x-side/2,y-side/2,x+side/2,y+side/2),angle,origin=(x,y))
                    if not g.covers(rectangle):continue
                    if any(rectangle.intersection(zone).area>.01 for zone in restricted):continue
                    world=convert(rectangle,metric)
                    if not original.covers(world):continue
                    if any(world.intersection(shape(p['geometry'])).area>0 for p in chosen):continue
                    accepted.append({'geometry':mapping(world),'parent_gap_id':feature['id'],'kind':'draft','rotation_degrees':angle})
                    chosen.append(accepted[-1]);break
                if len(accepted)>=per_gap or len(chosen)>=params['limit']:break
        if not accepted:unplaced.append(feature['id'])
        if limited:break
    return chosen,{'layout_checks':checks,'layout_limit_reached':limited,'layout_complete':False,
                   'unplaced_gaps':unplaced,'gap_count':len(gaps),'warning':'Подбор ограничен сеткой и числом контуров; отсутствие результата не исключает другой конфигурации.'}


def public_fields(f):
    o=f.get('properties',{}).get('options',{})
    return {key:str(o[key])[:2000] for key in ('cad_num','ownership_type','right_type','permitted_use_established_by_document','category','name_by_doc','content_restrict_encumbrances','legal_act_document_name','legal_act_document_number','legal_act_document_date') if o.get(key) is not None}


def document_context(values,numbers):
    found=[]
    for row in (values.get('municipal') or {}).get('items',[]):
        mentions=[m for m in row.get('mentions',[]) if torgi.canonical(m.get('cadastral_number')) in numbers]
        if mentions:
            found.append({'title':row.get('title','Документ'),'url':safe_url(row.get('url')),'received_at':row.get('received_at'),
                          'sha256':row.get('sha256'),'numbers':sorted({m['cadastral_number'] for m in mentions}),'scope':'Упоминание номера; применимость к контуру не установлена'})
    maps=values.get('planning_maps') or {};catalog=values.get('planning_watch') or {}
    for doc in catalog.get('items',[]):
        mentions=[m for m in doc.get('mentions',[]) if torgi.canonical(m.get('cadastral_number')) in numbers]
        if doc.get('state')=='read' and planning_watch.is_pzz(doc) and mentions:
            found.append({'title':'Изменение ПЗЗ: '+doc.get('title','Документ'),'url':safe_url(doc.get('url')),
                          'received_at':doc.get('received_at'),'sha256':doc.get('sha256'),
                          'numbers':sorted({m['cadastral_number'] for m in mentions}),
                          'pages':sorted({page for m in mentions for page in m.get('pages',[])}), 'act_identity':doc.get('act_identity'),
                          'parser_current':doc.get('algorithm')==planning_watch.ALGORITHM,
                          'scope':'Упоминание близкого кадастрового номера в тексте ПЗЗ; граница зоны и применимость к контуру не установлены'})
    if catalog.get('id') and maps.get('planning_id')==catalog['id']:
        for doc in maps.get('documents',[]):
            for page in doc.get('map_pages',[]):
                mentioned=sorted({n for n in page.get('number_mentions',[]) if torgi.canonical(n) in numbers})
                if mentioned:found.append({'title':'Карта изменения ПЗЗ','url':safe_url(doc.get('url')),'received_at':doc.get('received_at'),
                                          'sha256':doc.get('sha256'),'page':page['page'],'numbers':mentioned,'scope':'Кадастровое упоминание, не граница зоны'})
    return found[:30]


def history_documents(values):
    history=values.get('planning_boundary') or {};catalog=values.get('planning_watch') or {}
    documents={r['url']:r for r in catalog.get('items',[]) if r.get('state')=='read' and r.get('sha256') and r.get('received_at')}
    items=copy.deepcopy(history.get('history',{}).get('items',[]))
    for item in items:
        row=documents.get(item['url'])
        item['document_read']=bool(row)
        if row:
            item['text_observation']={k:copy.deepcopy(row.get(k)) for k in ('received_at','sha256','parsed_at','act_identity','total_pages','processed_pages','image_or_sparse_pages')}
            item['text_observation'].update(catalog_id=catalog.get('id'),parser_current=row.get('algorithm')==planning_watch.ALGORITHM,
                catalog_matches_history=catalog.get('history_source_id')==history.get('id'),
                listing_conflicts=copy.deepcopy(row.get('listing_conflicts',[])),legal_status_confirmed=False,geometry_confirmed=False)
    return items


def build(values,params):
    if values.get('planning_regulations'):
        planning_regulations.checked(values['planning_regulations'])
    s=values['survey'];metric=metric_for(s['bounds']);boundary=box(*s['bounds'])
    context=values.get('nspd_context');entries=nspd_context.projected(context,s['bounds'],metric)
    rgis=values.get('rgis_context');rgis_entries=rgis_context.projected(rgis,s['bounds'],metric)
    regional_pzz=values.get('pzz_context');pzz_entries=pzz_context.projected(regional_pzz,s['bounds'],metric)
    historical=values.get('planning_boundary');historical_geometries=planning_boundary.projected(historical,metric)
    excluded_modes=('schemes','planned_parcels') if params.get('avoid_planned',True) else ()
    if params.get('avoid_environment',True):excluded_modes+=nspd_context.ENVIRONMENT
    exclusions=[g for mode in excluded_modes for g,_ in entries[mode]]
    # Both independent switches select the geometric exclusions before layout.
    layers=s['layers'];rows,layout=draft_plots(s,params,exclusions)
    if not layers.get('parcels',{}).get('geojson',{}).get('features'):raise ValueError('Нет полученных кадастровых объектов; подбор остановлен')
    occupied=[shape(f['geometry']) for mode in ('parcels','buildings') for f in layers.get(mode,{}).get('geojson',{}).get('features',[])]
    for row in rows:
        if any(shape(row['geometry']).intersection(g).area>0 for g in occupied):raise ValueError('Пробный контур пересекает полученный участок или здание')
    source=values.get('torgi');active_source=values.get('torgi_active')
    indexed_lots={}
    for snapshot,regional in ((source,False),(active_source,True)):
        for original in (snapshot or {}).get('lots',[]):
            item=copy.deepcopy(original)
            item.update(search_date=snapshot.get('created_at'),search_id=snapshot.get('id'),regional_active=regional,
                        documents_current=torgi_docs.same_search(snapshot,values.get('torgi_active_documents' if regional else 'torgi_documents')))
            if regional:
                item['documents_current']=item['documents_current'] and any(c.get('lot_id')==item['id'] and c.get('state')=='received' for c in (values.get('torgi_active_documents') or {}).get('cards',[]))
            indexed_lots[item['id']]=item
    geometry_source={'geometries':{**(source or {}).get('geometries',{}),**(active_source or {}).get('geometries',{})}}
    observations=torgi.combined_geometries(geometry_source,s,list(indexed_lots.values()))
    lots=torgi.relate(list(indexed_lots.values()),observations,s)
    by_number={}
    for lot in lots:
        source_age=age_days(lot.get('search_date'))
        active=lot.get('status') in ('PUBLISHED','APPLICATIONS_SUBMISSION') and not lot.get('stopped') and not lot.get('annulled') and torgi.deadline_state(lot,store.now())=='future' and source_age is not None and 0<=source_age<=1
        lot['active_observed']=active
        if active and (not lot.get('regional_active') or torgi.land_group(lot)=='land_provision'):
            for match in lot['spatial_matches']:
                number=match['cadastral_number']
                if number not in by_number:
                    by_number[number]=len(rows)
                    rows.append({'geometry':match['feature']['geometry'],'kind':'auction','cadastral_number':number})
    for mode,kind in (('free','offer'),('auction','auction')):
        for f in layers.get(mode,{}).get('geojson',{}).get('features',[]):
            if shape(f['geometry']).intersection(boundary).area<=0:continue
            number=torgi.canonical(f.get('properties',{}).get('options',{}).get('cad_num'))
            if kind=='auction' and number in by_number:continue
            rows.append({'geometry':f['geometry'],'kind':kind,'source_feature_id':f['id'],'cadastral_number':number,'source_fields':public_fields(f)})
            if kind=='auction' and number:by_number[number]=len(rows)-1
    for f in s['gaps']['features']:
        if not any(r.get('parent_gap_id')==f['id'] for r in rows):
            rows.append({'geometry':f['geometry'],'kind':'gap','parent_gap_id':f['id']})
    parcels=[(convert(shape(f['geometry']),4326,metric),f) for f in layers['parcels']['geojson']['features']]
    roads=[(convert(shape(f['geometry']),4326,metric),f) for f in survey.road_features(layers)]
    overlays={mode:[(convert(shape(f['geometry']),4326,metric),f) for f in layers.get(mode,{}).get('geojson',{}).get('features',[])] for mode in ('free','auction','pzz','restrictions')}
    candidates=[];filtered={'small':0,'narrow':0}
    for row in rows[:520]:
        world=shape(row['geometry']);g=convert(world,4326,metric);point=world.representative_point()
        if g.area+.01<params['min_area']:filtered['small']+=1;continue
        if row['kind']!='draft' and params['min_width'] and g.buffer(-params['min_width']/2).is_empty:filtered['narrow']+=1;continue
        nearby=sorted(((g.distance(other),f) for other,f in parcels),key=lambda item:(item[0],str(item[1]['id'])))[:5]
        neighbours=[{'id':f['id'],'distance_m':round(distance,1),'fields':public_fields(f),'received_at':layers['parcels'].get('received_at'),'sha256':layers['parcels'].get('sha256')} for distance,f in nearby]
        matches={mode:[{'id':f['id'],'area_m2':round(g.intersection(other).area,2),'fields':public_fields(f),
                       'received_at':layers[mode].get('received_at'),'sha256':layers[mode].get('sha256')} for other,f in entries if g.intersection(other).area>.01] for mode,entries in overlays.items()}
        related=[]
        for lot in lots:
            used=[m for m in lot['spatial_matches'] if g.intersection(convert(shape(m['feature']['geometry']),4326,metric)).area>.01]
            if not used:continue
            overlap=g.intersection(unary_union([convert(shape(m['feature']['geometry']),4326,metric) for m in used])).area
            if overlap>.01:related.append({'id':lot['id'],'url':safe_url(lot['url']),'status':lot['status'],'deadline':lot.get('deadline'),
                                          'active_observed':lot['active_observed'],'overlap_m2':round(overlap,2),'search_date':lot.get('search_date'),
                                          'search_id':lot.get('search_id'),'regional_active':lot.get('regional_active',False),
                                          'land_group':torgi.land_group(lot),'type':copy.deepcopy(lot.get('type',{})),
                                          'geometry_sources':[{'cadastral_number':m['cadastral_number'],**{key:observations[m['cadastral_number']].get(key) for key in ('source','received_at','sha256')}} for m in used],
                                          'documents_current':lot['documents_current']})
        numbers={row.get('cadastral_number')} | {torgi.canonical(n['fields'].get('cad_num')) for n in neighbours if n['distance_m']<=10}
        numbers.discard(None)
        flags=[]
        boundary_observation=planning_boundary.relate(g,historical_geometries,historical)
        if boundary_observation:
            label={'inside':'внутри','outside':'вне','crosses':'пересекает','varies':'положение зависит от операции преобразования'}[boundary_observation['relation']]
            flags.append('Гипотеза границы Трудового по документу 2021 года: '+label+'; актуальность и система координат не подтверждены')
        context_matches=nspd_context.relate(g,entries,context)
        flags.extend(nspd_context.flags(context_matches))
        if matches['restrictions']:flags.append('Есть пересечение с полученными ЗОУИТ; нужен режим ограничения')
        if not matches['pzz']:flags.append('Территориальная зона для контура не установлена')
        if related:flags.append('Есть опубликованные процедуры; проверить статусы и схемы лотов')
        if any(lot.get('land_group')=='debt_sale' for lot in related):flags.append('Есть реализация имущества должника; это не подтверждает предоставление свободной земли')
        if g.area>params['max_area']:flags.append('Требуется проектирование меньшего контура')
        if world.distance(boundary.boundary)<1e-8:flags.append('Контур примыкает к границе обследования')
        digest=hashlib.sha256((row['kind']+world.normalize().wkb.hex()).encode()).hexdigest()[:20]
        road=min(roads,key=lambda r:g.distance(r[0])) if roads else None
        candidates.append({**row,'id':digest,'area_m2':round(g.area,2),'point':[round(point.x,7),round(point.y,7)],
                           'road_proximity':{'id':road[1]['id'],'distance_m':round(g.distance(road[0]),1),'fields':public_fields(road[1]),'received_at':layers['parcels'].get('received_at'),'legal_access_confirmed':False} if road else None,
                           'neighbours':neighbours,'matches':matches,'context_matches':context_matches,'boundary_observation':boundary_observation,'lots':related,'documents':document_context(values,numbers),
                           'flags':flags,'required_checks':list(CHECKS.values()),'status':'needs_review','rights_confirmed':False,'srzu_ready':False})
    access_source=access_evidence.prepare(s)
    for candidate in candidates:
        candidate['land_status']=land_status.assessment(candidate,values)
        candidate['access_evidence']=access_evidence.assessment_prepared(candidate,access_source)
        candidate['general_plan_matches']=rgis_context.relate(convert(shape(candidate['geometry']),4326,metric),rgis_entries,rgis)
        candidate['regional_pzz_matches']=pzz_context.relate(convert(shape(candidate['geometry']),4326,metric),pzz_entries,regional_pzz)
    source_numbers={n for lot in (source or {}).get('lots',[]) for n in lot['cadastral_numbers']}
    active_numbers={n for lot in (active_source or {}).get('lots',[]) for n in lot['cadastral_numbers']}
    sources={'nspd':{mode:{key:copy.deepcopy(layer.get(key)) for key in ('source','sha256','sha256_kind','received_at','coverage','observations')} | {'count':len(layer['geojson']['features']),'coverage_confirmed':False} for mode,layer in layers.items()},
             'torgi':{'id':(source or {}).get('id'),'received_at':(source or {}).get('created_at'),'query':(source or {}).get('query'),
                      'lots':len((source or {}).get('lots',[])),'unlocated_numbers':sorted(n for n in source_numbers if not observations.get(n,{}).get('features')),
                      'complete':False,'documents_current':torgi_docs.same_search(source,values.get('torgi_documents'))},
             'torgi_active':{'id':(active_source or {}).get('id'),'received_at':(active_source or {}).get('created_at'),
                             'lots':len((active_source or {}).get('lots',[])),
                             'groups':{group:sum(torgi.land_group(lot)==group for lot in (active_source or {}).get('lots',[])) for group in ('land_provision','debt_sale','other')},
                             'documents_id':(values.get('torgi_active_documents') or {}).get('id'),
                             'documents_current':torgi_docs.same_search(active_source,values.get('torgi_active_documents')),
                             'query_pages_received':bool((active_source or {}).get('query_pages_received')),
                             'unlocated_numbers':sorted(n for n in active_numbers if not observations.get(n,{}).get('features')),
                             'lots_without_number':sum(not lot['cadastral_numbers'] for lot in (active_source or {}).get('lots',[])),
                             'complete':False,'warning':torgi.ACTIVE_WARNING},
             'municipal':{'id':(values.get('municipal') or {}).get('id'),'catalog_at':(values.get('municipal') or {}).get('catalog_at'),'complete':False},
             'land_status':land_status.procedures(values.get('municipal')),
             'planning':{'id':(values.get('planning_watch') or {}).get('id'),
                         'archive_source_id':(values.get('planning_watch') or {}).get('archive_source_id'),
                         'archive_counts':copy.deepcopy((values.get('planning_watch') or {}).get('archive_counts')),
                         'geometry_confirmed':False,'complete':False},
             'regulations':{'id':(values.get('planning_regulations') or {}).get('id'),
                            'planning_id':(values.get('planning_regulations') or {}).get('planning_id'),
                            'updated_at':(values.get('planning_regulations') or {}).get('updated_at'),
                            'counts':planning_regulations.counts(values.get('planning_regulations')),
                            'catalog_matches':bool(values.get('planning_regulations') and values['planning_regulations']['planning_id']==(values.get('planning_watch') or {}).get('id')),
                            'legal_status_confirmed':False,'candidate_zone_confirmed':False},
             'historical_boundary':{'id':(historical or {}).get('id'),'applied':bool(historical_geometries),
                                    'source':copy.deepcopy((historical or {}).get('source')),'limitation':planning_boundary.LIMITATION,
                                    'current_boundary_confirmed':False,'crs_confirmed':False,
                                    'history':{'items':history_documents(values),
                                               'page_count':len((historical or {}).get('history',{}).get('pages',[])),
                                               'all_observed_pages_received':(historical or {}).get('history',{}).get('all_observed_pages_received',False),
                                               'scope':(historical or {}).get('history',{}).get('scope'),'complete':False}},
             'context':nspd_context.sources(context,s['bounds']),
             'rgis_context':rgis_context.sources(rgis,s['bounds']),
             'pzz_context':pzz_context.sources(regional_pzz,s['bounds'])}
    map_layers={mode:{'type':'FeatureCollection','features':[{'type':'Feature','id':f['id'],'geometry':f['geometry'],'properties':{'label':public_fields(f).get('cad_num',str(f['id']))}} for f in layers.get(mode,{}).get('geojson',{}).get('features',[])]} for mode in ('parcels','buildings','restrictions')}
    for mode,records in entries.items():
        map_layers[mode]={'type':'FeatureCollection','features':[{'type':'Feature','id':f['id'],'geometry':mapping(shape(f['geometry']).intersection(boundary)),
                     'properties':{'label':str(nspd_context.fields(f).get('cad_num') or nspd_context.fields(f).get('name') or nspd_context.TITLES.get(mode,mode))}} for _,f in records if not shape(f['geometry']).intersection(boundary).is_empty]}
    map_layers['historical_boundary']={'type':'FeatureCollection','features':[
        {'type':'Feature','geometry':historical['analysis']['hypotheses'][0]['geometry'],
         'properties':{'label':'Гипотеза границы Трудового · решение 17.02.2021 · CRS и актуальность не подтверждены',
                       'current_boundary_confirmed':False,'crs_confirmed':False,'used_for_exclusion':False}}
    ] if historical_geometries else []}
    for mode,records in rgis_entries.items():
        map_layers['rgis_'+mode]={'type':'FeatureCollection','features':[{'type':'Feature','id':f['id'],
            'geometry':mapping(shape(f['geometry']).intersection(boundary)),
            'properties':{'label':rgis_context.TITLES[mode]+' · '+str(f['properties'].get('SUBSUBTYPE') or f['id'])+' · карта ГП 2019; актуальность и ПЗЗ не подтверждены',
                          'currentness_confirmed':False,'used_for_exclusion':False}}
            for _,f in records if not shape(f['geometry']).intersection(boundary).is_empty]}
    map_layers['regional_pzz']={'type':'FeatureCollection','features':[{'type':'Feature','id':f['id'],
        'geometry':mapping(shape(f['geometry']).intersection(boundary)),
        'properties':{'label':'ПЗЗ РГИС · '+str(f['properties'].get('symbol') or f['id'])+' · '+str(f['properties'].get('territorialzonename') or '')+' · действующая редакция и допустимость использования не подтверждены',
                      'currentness_confirmed':False,'used_for_exclusion':False}}
        for _,f in pzz_entries if not shape(f['geometry']).intersection(boundary).is_empty]}
    return {'created_at':store.now(),'version':VERSION,'survey_id':s['id'],'source_inputs':identities(values),'bounds':s['bounds'],
            'parameters':params,'sources':sources,'candidates':candidates,'layout':layout,'warning':WARNING,
            'map_layers':map_layers,
            'summary':{'candidate_count':len(candidates),'drafts':sum(c['kind']=='draft' for c in candidates),
                       'offers':sum(c['kind']=='offer' for c in candidates),'auctions':sum(c['kind']=='auction' for c in candidates),
                       'large_gaps':sum(c['kind']=='gap' for c in candidates),'confirmed_free':0,'ready_to_submit':0,
                       'coverage_confirmed':False,'excluded_parcels':len(layers['parcels']['geojson']['features']),
                       'filtered':filtered,'candidate_rows_omitted':max(0,len(rows)-520),
                       'excluded_buildings':len(layers.get('buildings',{}).get('geojson',{}).get('features',[]))}}


def run(project,params):
    settings=options(params);bounds=list(map(float,params.get('bounds',[])))
    nspd.spatial_body(bounds,36368)
    old=store.get_setting('recon_'+project)
    attempt={'state':'running','started_at':store.now(),'step':'Получение обследования','warnings':[]}
    store.set_setting('recon_attempt_'+project,attempt)
    try:
        if settings['refresh_nspd']:
            survey.run(project,{**settings,'bounds':bounds})
            attempt['step']='Проверка кварталов, схем и границ';store.set_setting('recon_attempt_'+project,attempt)
            previous_context=store.get_setting('nspd_context_'+project)
            context=nspd_context.collect(bounds,previous_context)
            with store.LOCK:
                if store.get_setting('nspd_context_'+project)!=previous_context:raise ValueError('Контекст НСПД изменился во время обновления')
                nspd_context.save(project,context)
            for mode,layer in context['layers'].items():
                if layer['state']!='received':attempt['warnings'].append(nspd_context.TITLES[mode]+(': сохранено прежнее наблюдение от '+layer['received_at'] if layer['state']=='retained' else ': не получено')+'; '+layer.get('error','нет ответа'))
        else:
            previous=store.get_setting('survey_'+project)
            if not previous or previous['bounds']!=bounds:raise ValueError('Сохранённое обследование относится к другой области; включите обновление НСПД')
            survey.recalculate(project,settings)
            saved_context=store.get_setting('nspd_context_'+project)
            if not nspd_context.applicable(saved_context,bounds):attempt['warnings'].append('Кварталы, схемы и границы этой области не сохранены; включите обновление НСПД для их получения')
            else:
                missing=[title for mode,title in nspd_context.TITLES.items() if mode not in saved_context.get('layers',{})]
                if missing:attempt['warnings'].append('В сохранённом контексте отсутствуют слои: '+', '.join(missing)+'; включите обновление НСПД')
        if settings['refresh_torgi']:
            attempt['step']='Обновление поиска торгов';store.set_setting('recon_attempt_'+project,attempt)
            try:torgi.run(project,{'query':settings['query'],'history':True})
            except Exception as exc:attempt['warnings'].append('Торги не обновлены; используется датированный прежний поиск: '+str(exc)[:300])
        if settings['refresh_active_torgi']:
            attempt['step']='Земельные публикации по всему Крыму';store.set_setting('recon_attempt_'+project,attempt)
            try:torgi.run(project,{},active=True)
            except Exception as exc:attempt['warnings'].append('Региональные публикации не обновлены; прежняя выборка сохранена: '+str(exc)[:300])
        if settings['refresh_active_documents']:
            attempt['step']='Карточки и документы земельных лотов';store.set_setting('recon_attempt_'+project,attempt)
            active_search=store.get_setting('torgi_active_'+project) or {}
            try:
                documents=regional_documents.collect(project,{'search_id':active_search.get('id')})
                if documents['state']!='done' or documents['cards_remaining'] or documents['files_remaining']:
                    attempt['warnings'].append('Документы земельных лотов получены частично; оставшаяся очередь и ошибки показаны отдельно')
            except Exception as exc:attempt['warnings'].append('Документы земельных лотов не завершены; прежние датированные источники сохранены: '+str(exc)[:300])
        attempt['step']='Подбор контуров и сверка источников';store.set_setting('recon_attempt_'+project,attempt)
        values=inputs(project);result=build(values,settings)
        result.update(previous_result_id=(old or {}).get('id'),operation_warnings=attempt['warnings'])
        result['id']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()[:20]
        with store.LOCK:
            if identities(inputs(project))!=result['source_inputs']:raise ValueError('Источники изменились во время поиска; результат не записан')
            if (store.get_setting('recon_'+project) or {}).get('id')!=(old or {}).get('id'):raise ValueError('Другая версия поиска уже записана; обновите страницу')
            folder=store.DATA/'recon'/project;folder.mkdir(parents=True,exist_ok=True)
            store.atomic_write(folder/(result['id']+'.json'),json.dumps(result,ensure_ascii=False).encode('utf-8'))
            store.set_setting('recon_'+project,result)
            with store.connect() as db:store.event(db,project,'recon',{'id':result['id'],'summary':result['summary'],'warnings':attempt['warnings']})
        attempt.update(state='done',finished_at=store.now(),step='Готово')
        return {'id':result['id'],**result['summary']}
    except Exception as exc:
        attempt.update(state='error',finished_at=store.now(),error=str(exc)[:500]);raise
    finally:store.set_setting('recon_attempt_'+project,attempt)


def is_stale(result,current):
    return bool(result and (result.get('version')!=VERSION or result['source_inputs']!=current))


def report(project):
    result=store.get_setting('recon_'+project);current=identities(inputs(project))
    watch=store.get_setting('recon_watch_'+project,[])
    return {'result':result,'attempt':store.get_setting('recon_attempt_'+project),'stale':is_stale(result,current),
            'watchlist':[dict(row,stale=row.get('calculation_version')!=VERSION or row['source_inputs']!=current) for row in watch],'purposes':PURPOSES}


def watch(project,params):
    with store.LOCK:
        data=report(project);result=data['result']
        if not result or params.get('result_id')!=result['id'] or data['stale']:raise ValueError('Результат изменился; повторите поиск или локальный расчёт')
        candidate=next((c for c in result['candidates'] if c['id']==params.get('id')),None)
        if not candidate:raise ValueError('Контур не найден')
        rows=store.get_setting('recon_watch_'+project,[])
        old=next((row for row in rows if row['candidate']['id']==candidate['id']),None)
        if old and old['result_id']==result['id']:return {'count':len(rows)}
        row={'candidate':copy.deepcopy(candidate),'result_id':result['id'],'source_inputs':result['source_inputs'],'calculation_version':result['version'],'added_at':store.now(),
             'history':(old or {}).get('history',[])+([{'result_id':old['result_id'],'added_at':old['added_at']}] if old else [])}
        rows=[r for r in rows if r['candidate']['id']!=candidate['id']]+[row]
        store.set_setting('recon_watch_'+project,rows)
        with store.connect() as db:store.event(db,project,'recon_watch',{'id':candidate['id'],'result_id':result['id']})
        return {'count':len(rows)}


def load(project,result_id):
    if not isinstance(result_id,str) or not re.fullmatch(r'[0-9a-f]{20}',result_id):raise ValueError('Некорректная версия результата')
    path=store.DATA/'recon'/project/(result_id+'.json')
    if not path.is_file():raise ValueError('Версия результата не найдена')
    result=json.loads(path.read_text(encoding='utf-8'));claimed=result.pop('id',None)
    if claimed!=result_id or hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()[:20]!=result_id:raise ValueError('Сохранённый результат изменился')
    result['id']=claimed
    return {'result':result,'stale':is_stale(result,identities(inputs(project)))}


def collection(data):
    result=data.get('result') or {}
    return {'type':'FeatureCollection','source_result_id':result.get('id'),'stale':data.get('stale',False),'warning':WARNING,
            'features':[{'type':'Feature','id':c['id'],'geometry':c['geometry'],
                         'properties':{key:c.get(key) for key in ('id','kind','area_m2','status','rights_confirmed','srzu_ready','flags','parent_gap_id','boundary_observation','access_evidence','general_plan_matches','regional_pzz_matches')} | {'calculated_at':result.get('created_at'),'survey_id':result.get('survey_id')}} for c in result.get('candidates',[])]}


def regional_pzz_section(result,candidate):
    esc=lambda value:html.escape(str('—' if value is None else value),quote=True)
    source=result.get('sources',{}).get('pzz_context') or {}
    layer=source.get('layer') or {}
    status=('Получено '+esc(layer.get('count'))+' зон для области; дата получения '+esc(layer.get('received_at'))+'.'
            if source.get('applied') else 'Данные ПЗЗ РГИС для этой версии и области не получены.')
    if source.get('applied') and layer.get('count')==0:
        status+=' Источник ответил без зон. Это не подтверждает отсутствие ПЗЗ.'
    rows=[]
    for row in candidate.get('regional_pzz_matches',[]):
        rows.append('<li>'+esc(row['id'])+' · пересечение '+esc(row['area_m2'])+' м² · поля '+esc(json.dumps(row['fields'],ensure_ascii=False))
                    +' · получено '+esc(row['received_at'])+' · SHA-256 '+esc(row['sha256'])+'</li>')
    return ('<h3>Региональный источник ПЗЗ Крыма</h3><p><a href="'+esc(pzz_context.MAP)+'">Территориальные зоны ПЗЗ в РГИС Крыма</a></p>'
            +'<p>'+status+'</p><p>'+esc(pzz_context.WARNING)+'</p><ul>'+(''.join(rows) or '<li>Пересечения с полученными зонами не установлены.</li>')+'</ul>')


def access_section(candidate):
    observation=candidate.get('access_evidence')
    if not observation:return '<h3>Наблюдение по направлению к дороге</h3><p>В этой версии не рассчитывалось.</p>'
    esc=lambda x:html.escape(str('—' if x is None else x),quote=True)
    def source(row):
        return (' · источник <a href="'+esc(safe_url(row.get('source')))+'">НСПД</a> · получено '+esc(row.get('received_at'))
                +' · SHA-256 '+esc(row.get('sha256'))+' ('+esc(row.get('sha256_kind'))+')')
    rows=[]
    for key,title in (('nearest_parcel','Ближайший полученный участок'),('nearest_building','Ближайшее полученное здание'),('road','Ближайший полученный контур дорожного назначения')):
        row=observation.get(key)
        rows.append('<li>'+title+': '+(esc(row.get('cadastral_number') or row.get('feature_id'))+' · '+esc(row['distance_m'])+' м'+source(row) if row else 'не установлен по полученным геометриям')+'</li>')
    segment=observation.get('direct_segment')
    if segment:
        rows.append('<li>Кратчайший прямой отрезок: '+esc(segment['length_m'])+' м; в пределах обследования: '+('да' if segment['within_survey_bounds'] else 'нет')+'. Это не маршрут и не полоса проезда.</li>')
        for group,title in (('intersections','Пересечения положительной длины'),('touches','Касания без длины')):
            for mode,name in (('parcels','участки'),('buildings','здания')):
                records=observation[group][mode]
                rows.append('<li>'+title+', '+name+': '+str(len(records))+'<ul>'+''.join('<li>'+esc(row.get('cadastral_number') or row['feature_id'])+' · '+esc(row['intersection_length_m'])+' м'+source(row)+'</li>' for row in records)+'</ul></li>')
    return ('<h3>Наблюдение по направлению к дороге</h3><p>'+esc(observation['warning'])+'</p><ul>'+''.join(rows)+'</ul>'
            +'<p>Алгоритм '+esc(observation['algorithm'])+'; обследование '+esc(observation['survey_id'])+'. Законный доступ не подтверждён.</p>')


def html_report(data,candidate_id=None):
    result=data.get('result') or {};esc=lambda x:html.escape(str('—' if x is None else x),quote=True)
    candidates=result.get('candidates',[])
    if candidate_id is not None:
        candidates=[c for c in candidates if c['id']==candidate_id]
        if not candidates:raise ValueError('Контур не найден в этой версии')
    blocks=[]
    names={'draft':'Пробный контур','gap':'Промежуток для проектирования','offer':'Предложение НСПД','auction':'Торги / слой аукционов'}
    for c in candidates:
        evidence=[]
        observation=c.get('boundary_observation')
        if observation:
            label={'inside':'внутри','outside':'вне','crosses':'пересекает','varies':'зависит от преобразования'}[observation['relation']]
            evidence.append('<li>Гипотеза границы Трудового: '+esc(label)+' · <a href="'+esc(safe_url(observation['source']['url']))+'">Решение № 396 от 17.02.2021</a> · получено '+esc(observation['source']['received_at'])+' · SHA-256 '+esc(observation['source']['sha256'])+' · '+esc(observation['limitation'])+'<pre>'+esc(json.dumps(observation['observations'],ensure_ascii=False,indent=2))+'</pre></li>')
        for mode,rows in c['matches'].items():
            for row in rows:evidence.append('<li>'+esc(mode)+' · '+esc(row['area_m2'])+' м² · '+esc(json.dumps(row['fields'],ensure_ascii=False))+' · '+esc(row['received_at'])+'</li>')
        for mode,rows in c.get('context_matches',{}).items():
            for row in rows:
                detail=f"{row['length_m']} м линии" if row.get('length_m') is not None else f"{row['area_m2']} м² пересечения"
                scope=' · счётчики целого квартала; положение объектов без геометрии неизвестно' if mode=='quarters' else ''
                evidence.append('<li>'+esc(nspd_context.TITLES[mode])+' · '+esc(detail)+' · '+esc(json.dumps(row['fields'],ensure_ascii=False))+' · '+esc(row['received_at'])+esc(scope)+' · правовая применимость не подтверждена</li>')
        for lot in c['lots']:evidence.append('<li><a href="'+esc(lot['url'])+'">Лот '+esc(lot['id'])+'</a> · '+esc(lot['status'])+' · поиск '+esc(lot['search_date'])+' · действующая процедура по наблюдению: '+esc(lot['active_observed'])+' · геометрия: '+esc(json.dumps(lot['geometry_sources'],ensure_ascii=False))+'</li>')
        for doc in c['documents']:evidence.append('<li><a href="'+esc(doc['url'])+'">'+esc(doc['title'])+'</a> · '+esc(doc['scope'])+' · '+esc(doc.get('received_at'))+'</li>')
        gp_rows=[]
        for mode,rows in c.get('general_plan_matches',{}).items():
            for row in rows:
                gp_rows.append('<li>'+esc(rgis_context.TITLES[mode])+' · '+esc(row['id'])+' · пересечение '+esc(row['area_m2'])+' м² · поля '+esc(json.dumps(row['fields'],ensure_ascii=False))+' · получено '+esc(row['received_at'])+' · SHA-256 '+esc(row['sha256'])+'</li>')
        blocks.append('<section><h2>'+esc(names[c['kind']])+' '+esc(c['id'])+'</h2><p>Площадь '+esc(c['area_m2'])+' м²; точка внутри: '+esc(c['point'])+'. Права не подтверждены; к подаче не готов.</p>'
                      +'<p>До полученного кадастрового контура дорожного назначения: '+esc((c.get('road_proximity') or {}).get('distance_m'))+' м. Расстояние не подтверждает законный подъезд.</p>'
                      +access_section(c)
                      +regional_pzz_section(result,c)
                      +'<h3>Независимый контекст генплана РГИС</h3><p>'+esc(rgis_context.WARNING)+'</p><p><a href="'+esc(rgis_context.MAP)+'">Официальная карта РГИС</a></p><ul>'+(''.join(gp_rows) or '<li>Совпадения не получены для этой версии/области. Это не подтверждает отсутствие функциональных зон или дорог.</li>')+'</ul>'
                      +'<h3>Особенности</h3><ul>'+''.join('<li>'+esc(f)+'</li>' for f in c['flags'])+'</ul><h3>Полученные совпадения</h3><ul>'+(''.join(evidence) or '<li>Совпадения не установлены. Это не подтверждение отсутствия процедур или ограничений.</li>')+'</ul>'
                      +'<h3>Ближайшие полученные участки</h3><ul>'+''.join('<li>'+esc(n['fields'].get('cad_num',n['id']))+' · '+esc(n['distance_m'])+' м · '+esc(json.dumps(n['fields'],ensure_ascii=False))+'</li>' for n in c['neighbours'])+'</ul>'
                      +planning_regulations.dossier_section(result)
                      +planning_enquiry.dossier_section(result,c,data.get('stale',False))
                      +land_status.dossier_section(result,c,data.get('stale',False))
                      +'<h3>Недостающие проверки</h3><ul>'+''.join('<li>'+esc(x)+'</li>' for x in c['required_checks'])+'</ul><h3>Контур WGS84</h3><pre>'+esc(json.dumps(c['geometry'],ensure_ascii=False))+'</pre></section>')
    return ('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Поиск участков: рабочее досье</title><style>body{font:16px/1.5 system-ui;max-width:1000px;margin:30px auto;padding:0 20px;color:#21382b}section{border-top:1px solid #ccd8ce;margin-top:30px}pre{white-space:pre-wrap;overflow-wrap:anywhere}li{margin:8px 0}a{overflow-wrap:anywhere}@media print{section{break-before:page}}</style>'
            +'<h1>Поиск участков: рабочее досье</h1><p>'+esc(WARNING)+'</p><p>'+('Предыдущая версия: источники изменились.' if data.get('stale') else 'Датированная версия расчёта.')+'</p><p>Расчёт '+esc(result.get('created_at'))+'; цель '+esc(PURPOSES.get(result.get('parameters',{}).get('purpose'),'Не выбрана'))+'.</p>'
            +'<p>Подтверждённых свободных участков: 0. Досье не является СРЗУ или заявлением.</p>'
            +''.join('<p>'+esc(w)+'</p>' for w in result.get('operation_warnings',[]))+''.join(blocks)
            +'<h2>Ссылки из перечней ГП/ПЗЗ</h2><p>Документы относятся к поселению. Состояние чтения текста показано отдельно для каждого PDF. Применимость к этому контуру и вступление в силу не подтверждены.</p><ul>'
            +''.join('<li><a href="'+esc(safe_url(item['url']))+'">'+esc(item['title'])+'</a> · '+('текст прочитан' if item.get('document_read') else 'PDF не прочитан')+' · '+esc(json.dumps({'перечни':item['references'],'чтение':item.get('text_observation')},ensure_ascii=False))+'</li>' for item in result.get('sources',{}).get('historical_boundary',{}).get('history',{}).get('items',[]))
            +'</ul><h2>Источники и даты</h2><pre>'+esc(json.dumps(result.get('sources',{}),ensure_ascii=False,indent=2))+'</pre></html>').encode('utf-8')
