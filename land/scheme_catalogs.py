"""Observed multi-column PDF catalogues. Stages never join parcel tables or GIS."""
import math
import re
from shapely.geometry import Polygon
from shapely.validation import explain_validity

NUMBER = r'-?(?:\d{1,8}(?:[,.]\d{1,3})?|\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{1,3})?)'
POINT = re.compile(r'^(\d{1,4})\s+('+NUMBER+r')\s+('+NUMBER+r')$')
LABEL = re.compile(r'^(?::ЗУ\s*\d{1,4}(?::ЗУ\s*\d{1,4})?|Ст\d{2}_\d{1,3})$', re.I)
CONTOUR = re.compile(r'^Контур\s+(\d{1,3})$', re.I)
AREA = re.compile(r'^Площад\s*ь\s*:\s*('+NUMBER+r')\s+кв\.\s*м\.?$', re.I)
SECTION = re.compile(r'^\d{1,2}(?:\.\d{1,2})?\.?\s+[А-ЯЁ]', re.M)
PAIR = re.compile(r'\d{5,8}[,.]\d+\s+\d{5,8}[,.]\d+')
PREFIX = 'Каталог координат образуемых земельных участков'
PROFILES = {
 '2.1': ('Каталог координат границы территории, в отношении которой утвержден проект межевания',0,'project_territory'),
 '2.2': (PREFIX+' (ПЕРВЫЙ ЭТАП МЕЖЕВАНИЯ)',1,'formed_context'),
 '2.3': (PREFIX+' (ВТОРОЙ ЭТАП МЕЖЕВАНИЯ)',2,'formed_context'),
 '2.4': (PREFIX+' территории общего назначения (ВТОРОЙ ЭТАП МЕЖЕВАНИЯ)',2,'public_use_context'),
 '2.5': ('Каталог координат поворотных точек земельных участков постороннего землепользователя (электросетевого хозяйства) (ВТОРОЙ ЭТАП МЕЖЕВАНИЯ)',2,'other_land_user_context'),
 '2.6': (PREFIX+' (ТРЕТИЙ ЭТАП МЕЖЕВАНИЯ)',3,'formed_context'),
 '2.7': (PREFIX+' (ЧЕТВЕРТЫЙ ЭТАП МЕЖЕВАНИЯ)',4,'formed_context'),
}
WARNING = ('Каталоги этапов хранятся отдельно от таблиц участков. Этапы, условные обозначения и кольца '
           'не объединяются для расчёта свободной земли. СК, кадастровая идентичность, полнота и права не установлены.')


def value(number):
    return float(number.replace(' ','').replace('\u00a0','').replace(',','.'))


def pdf_lines(page, number):
    textpage=page.get_textpage()
    try:
        raw=textpage.get_text_range(errors='strict')
        if len(raw)>500000 or len(raw)!=textpage.count_chars():
            raise ValueError('Текст/индексы PDF не подходят для сверки расположения строк')
        width,height=page.get_size(); lines=[]; offset=0
        for line in raw.splitlines(keepends=True):
            indices=[offset+i for i,c in enumerate(line) if not c.isspace()]
            if indices:
                first,last=(textpage.get_charbox(i) for i in (indices[0],indices[-1]))
                bbox=[min(first[0],last[0]),min(first[1],last[1]),max(first[2],last[2]),max(first[3],last[3])]
                if not all(math.isfinite(x) for x in bbox) or bbox[0]<-1 or bbox[2]>width+1 or bbox[1]<-1 or bbox[3]>height+1:
                    raise ValueError('Расположение строки вне страницы PDF')
                lines.append({'page':number,'offset':offset,'end_offset':offset+len(line),'text':line.strip(),'pdf_bbox':bbox})
            offset+=len(line)
        return {'page':number,'width':width,'height':height,'raw':raw,'lines':lines}
    finally:
        textpage.close()


def markers(pages):
    result=[]
    for page in pages:
        for match in SECTION.finditer(page['raw']):
            line=next(x for x in page['lines'] if x['offset']<=match.start()<x['end_offset'])
            number=match[0].split()[0].rstrip('.')
            profile=PROFILES.get(number); heading=None
            if profile:
                pattern=re.escape(number)+r'\s+'+r'\s+'.join(re.escape(w) for w in profile[0].split())
                found=re.match(pattern,page['raw'][match.start():],re.I)
                if found:heading=found[0]
            result.append({'page':page['page'],'line':line,'number':number,'heading':heading})
    return result


def ordered_lines(pages, start, end):
    selected=[]
    for page in pages:
        if not start['page']<=page['page']<=end['page']:continue
        for line in page['lines']:
            middle=(line['pdf_bbox'][1]+line['pdf_bbox'][3])/2
            if page['page']==start['page'] and middle>=start['line']['pdf_bbox'][1]:continue
            if page['page']==end['page'] and middle<=end['line']['pdf_bbox'][3]:continue
            if line['pdf_bbox'][3]<40:continue  # PDF page footer, never a vertex.
            selected.append({**line,'width':page['width']})
    points=[line for line in selected if POINT.fullmatch(line['text'])]
    anchors=[]
    for x in sorted(line['pdf_bbox'][0]/line['width'] for line in points):
        if not anchors or x-anchors[-1][-1]>.09:anchors.append([x])
        else:anchors[-1].append(x)
    if len(anchors)>3:raise ValueError('Больше трёх столбцов; расположение каталога не поддержано')
    centers=[sum(xs)/len(xs) for xs in anchors]
    for line in selected:
        line['column']=1+min(range(len(centers)),key=lambda i:abs(line['pdf_bbox'][0]/line['width']-centers[i])) if centers else 1
        line['pdf_bbox']=[round(x,3) for x in line['pdf_bbox']]
    return sorted(selected,key=lambda x:(x['column'],x['page'],-x['pdf_bbox'][3],x['offset'])),len(anchors)


def ring(points, axes_confirmed):
    issues=[]
    closed=len(points)>1 and points[-1]['label']==points[0]['label'] and (points[-1]['column_1'],points[-1]['column_2'])==(points[0]['column_1'],points[0]['column_2'])
    vertices=points[:-1] if len(points)>1 and points[-1]['label']==points[0]['label'] else points
    if not closed:issues.append({'reason':'Нет явного замыкания; кольцо не достраивается'})
    labels=[int(p['label']) for p in vertices]
    if labels and labels!=list(range(labels[0],labels[0]+len(labels))):
        issues.append({'reason':'Нарушена последовательность номеров строк; точки не переставляются'})
    if any(right['column']==left['column'] and right['page']>left['page']+1 for left,right in zip(points,points[1:])):
        issues.append({'reason':'Между точками в столбце пропущена страница; полнота кольца не подтверждена'})
    coords=[(p['column_1'],p['column_2']) for p in vertices]
    if len(vertices)<3:issues.append({'reason':'Меньше трёх вершин'})
    if len(set(coords))!=len(coords):issues.append({'reason':'Повтор координат внутри кольца'})
    polygon=Polygon(coords) if closed and len(vertices)>=3 else None
    if polygon is not None and (not polygon.is_valid or polygon.area<=0):
        issues.append({'reason':explain_validity(polygon) if not polygon.is_valid else 'Нулевая площадь'})
    return {'points':points,'state':'rejected' if issues else 'review_required','issues':issues,
            'closure':'explicit' if closed else 'missing',
            'outline_columns':list(map(list,polygon.exterior.coords)) if polygon is not None and not issues else None,
            'local_area_m2':round(polygon.area,2) if polygon is not None and not issues and axes_confirmed else None,
            'geometry_confirmed':False,'georeferenced':False}


def parts(lines, catalog):
    result=[]; current=None; parent=None; handled=set(); total=0; pending=[]
    def finish():
        nonlocal pending
        if current is not None and pending:
            current['rings'].append(ring(pending,catalog['axes_status']=='explicit_xy_m'));pending=[]
    def begin(label, line, contour=None):
        nonlocal current
        finish()
        current={'label':label,'contour_number':contour,'heading_page':line['page'],'heading_bbox':line['pdf_bbox'],
                 'rings':[],'source_areas':[],'issues':[],'completeness_confirmed':False,'geometry_confirmed':False,'georeferenced':False}
        result.append(current)
        if len(result)>200:raise ValueError('Каталог превышает лимит частей')
    if catalog['kind']=='project_territory':
        begin('Граница территории проекта',catalog['heading_source'])
    index=0
    while index<len(lines):
        line=lines[index]; text=line['text']; index+=1
        if LABEL.fullmatch(text):
            parent=re.sub(r'\s+','',text).upper();begin(parent,line);continue
        contour=CONTOUR.fullmatch(text)
        if contour:
            if parent is None:catalog['issues'].append({'reason':'Контур без условного обозначения','page':line['page']})
            else:
                if current and not current['rings'] and not pending and current['contour_number'] is None:result.pop()
                begin(parent+' · Контур '+contour[1],line,int(contour[1]))
            continue
        if text.lower().startswith('площад'):
            fragments=[text]; origin=[line]
            while not AREA.fullmatch(' '.join(fragments)) and index<len(lines) and len(fragments)<3:
                following=lines[index]
                if following['column']!=line['column'] or following['page']!=line['page'] or POINT.fullmatch(following['text']) or LABEL.fullmatch(following['text']):break
                fragments.append(following['text']);origin.append(following);index+=1
            match=AREA.fullmatch(' '.join(fragments))
            if match:
                area={'stated_area_m2':value(match[1]),'page':line['page'],'source_rows':[x['text'] for x in origin],
                      'scope':'all_catalogue_contours' if catalog['kind']=='public_use_context' else 'named_part'}
                if catalog['kind']=='public_use_context':catalog['source_areas'].append(area)
                elif current:current['source_areas'].append(area)
            else:catalog['issues'].append({'reason':'Строка площади не разобрана','page':line['page'],'row':' '.join(fragments)[:200]})
            continue
        match=POINT.fullmatch(text)
        if match:
            if current is None:
                catalog['issues'].append({'reason':'Строка точки без обозначения части','page':line['page'],'row':text});continue
            point={'label':match[1],'column_1':value(match[2]),'column_2':value(match[3]),'page':line['page'],
                   'column':line['column'],'source_row':text,'pdf_bbox':line['pdf_bbox']}
            pending.append(point);handled.add((line['page'],line['offset']));total+=1
            if total>5000:raise ValueError('Каталог превышает лимит 5000 точек')
            if len(pending)>1 and pending[-1]['label']==pending[0]['label']:finish()
        elif re.match(r'^\d+\s+[-\dОO]',text,re.I):
            catalog['issues'].append({'reason':'Повреждённая строка точки','page':line['page'],'row':text[:200]})
            if current:current['issues'].append({'reason':'Повреждённая строка точки','page':line['page'],'row':text[:200]})
        elif pending and not re.fullmatch(r'(?:Имя|точки|№|(?:п/п\s+)?X,\s*м\s+Y,\s*м)',text,re.I):
            current['issues'].append({'reason':'Неподдержанная строка внутри кольца','page':line['page'],'row':text[:200]})
    finish()
    identities={}
    for part in result:
        identities.setdefault((part['label'],part['contour_number']),[]).append(part)
        if not part['rings']:part['issues'].append({'reason':'Нет строк точек'})
        if len(part['rings'])>1:
            part['issues'].append({'reason':'Несколько колец под одним обозначением; роль дополнительного кольца (включая отверстие) не установлена'})
    for duplicates in identities.values():
        if len(duplicates)>1:
            for part in duplicates:part['issues'].append({'reason':'Повтор обозначения части; части не объединены'})
    for part in result:
        part['state']='rejected' if part['issues'] or any(r['state']=='rejected' for r in part['rings']) else 'review_required'
        if part['issues']:
            for r in part['rings']:r.update(state='rejected',outline_columns=None,local_area_m2=None)
    return result,handled,total


def extract_layout(pages):
    found=markers(pages); catalogs=[]; handled=set(); total=0
    for index,start in enumerate(found):
        if not start['heading']:continue
        end=found[index+1] if index+1<len(found) else {'page':pages[-1]['page']+1,'line':{'pdf_bbox':[0,0,0,0]}}
        profile=PROFILES[start['number']];lines,columns=ordered_lines(pages,start,end)
        headers=[x for x in lines if re.search(r'(?:^|\s)X,\s*м\s+Y,\s*м$',x['text'],re.I)]
        catalog={'section':start['number'],'source_heading':start['heading'],'heading_page':start['page'],
                 'heading_source':start['line'],'phase':profile[1],'kind':profile[2],'column_count':columns,
                 'axes_status':'explicit_xy_m' if headers else 'unlabelled_columns','axis_sources':headers,
                 'crs_parameters_confirmed':False,'geometry_confirmed':False,'georeferenced':False,
                 'completeness_confirmed':False,'source_areas':[],'issues':[],'warning':WARNING}
        catalog['parts'],done,count=parts(lines,catalog);handled.update(done);total+=count
        catalogs.append(catalog)
        if len(catalogs)>32 or total>5000:raise ValueError('Документ превышает лимит каталогов/точек')
    covered=[]
    for page in pages:
        numeric={(page['page'],x['offset']) for x in page['lines'] if PAIR.search(x['text'])}
        if numeric and numeric.issubset(handled):covered.append(page['page'])
    return {'coordinate_catalogs':catalogs,'catalogue_covered_coordinate_pages':covered}


def extract_pdf(path, processed_pages):
    import pypdfium2 as pdfium
    document=pdfium.PdfDocument(path)
    pages=[]
    try:
        if not 0<processed_pages<=200:raise ValueError('Недопустимый диапазон страниц каталогов')
        # Read layout only when a supported title exists. Normal files avoid character-box work.
        relevant=[]
        for index in range(min(len(document),processed_pages)):
            page=document[index]
            try:
                textpage=page.get_textpage()
                try:raw=textpage.get_text_range(errors='strict')
                finally:textpage.close()
                if len(raw)>500000:raise ValueError('Текст страницы превышает лимит')
                if re.search(r'^2\.[1-7]\s+Каталог координат',raw,re.M):relevant.append(index)
            finally:page.close()
        if not relevant:return {'coordinate_catalogs':[],'catalogue_covered_coordinate_pages':[]}
        first,last=min(relevant),max(relevant)
        # One following page supplies the next section marker; no unknown appendix is swallowed.
        for index in range(first,min(last+2,processed_pages,len(document))):
            page=document[index]
            try:pages.append(pdf_lines(page,index+1))
            finally:page.close()
        return extract_layout(pages)
    finally:document.close()
