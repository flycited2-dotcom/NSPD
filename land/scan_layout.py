"""Strict draft rows in two original OCR views. No geometry or inferred identities."""
import math
import re
from statistics import median

ALGORITHM='dual-position-rows-v1'
COORDINATE=re.compile(r'-?\d(?:[ \u00a0]*\d){4,7}[.,]\d{1,3}')
POINT=re.compile(r'н\s*\d{1,4}',re.I)
WARNING='Строки распознаны автоматически. Даже совпадение двух чтений требует визуальной сверки. Обозначение участка, оси, единицы, СК и полнота не подтверждены; геометрия не создаётся.'


def literal(text, label=False):
    value=''.join(text.split())
    if label:return value.lower() if POINT.fullmatch(text.strip()) else None
    return float(value.replace(',','.')) if COORDINATE.fullmatch(text.strip()) else None


def words(view):
    width,height=view.get('width'),view.get('height')
    if any(isinstance(n,bool) or not isinstance(n,int) or not 0<n<=10000 for n in (width,height)) or width*height>25000000:
        raise ValueError('Неверные размеры OCR')
    raw=view.get('words')
    if not isinstance(raw,list) or len(raw)>10000:raise ValueError('Неверный список слов OCR')
    result=[]
    for word in raw:
        box=word.get('bbox');text=word.get('text')
        if not isinstance(text,str) or not 0<len(text)<=200 or not isinstance(box,list) or len(box)!=4:
            raise ValueError('Неверная структура слова OCR')
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in box):
            raise ValueError('Неверное положение слова OCR')
        x,y,w,h=box
        if min(x,y)<0 or min(w,h)<=0 or x+w>width+1 or y+h>height+1:
            raise ValueError('Слово OCR вне изображения')
        result.append({'text':text,'bbox':[x/width,y/height,(x+w)/width,(y+h)/height],
                       'line':word.get('line'),'word':word.get('word')})
    return sorted(result,key=lambda w:((w['bbox'][1]+w['bbox'][3])/2,w['bbox'][0]))


def visual_rows(items):
    if not items:return []
    tolerance=median(w['bbox'][3]-w['bbox'][1] for w in items)*.55
    rows=[]
    for word in items:
        cy=(word['bbox'][1]+word['bbox'][3])/2
        if not rows or cy-rows[-1]['cy']>tolerance:rows.append({'cy':cy,'words':[]})
        rows[-1]['words'].append(word)
    return rows


def cells(items):
    result=[]
    for word in sorted(items,key=lambda w:w['bbox'][0]):
        if not result or word['bbox'][0]-result[-1]['bbox'][2]>.018:
            result.append({'text':word['text'],'bbox':list(word['bbox']),'words':[word]})
        else:
            c=result[-1];c['text']+=' '+word['text'];c['words'].append(word)
            c['bbox']=[min(c['bbox'][0],word['bbox'][0]),min(c['bbox'][1],word['bbox'][1]),
                       max(c['bbox'][2],word['bbox'][2]),max(c['bbox'][3],word['bbox'][3])]
    return result


def view_rows(view):
    all_words=words(view);rows=visual_rows(all_words);candidates=[];anchors=[]
    for row in rows:
        row['cells']=cells(row['words'])
        for cell in row['cells']:
            if len(re.sub(r'\D','',cell['text']))>=5 and re.fullmatch(r'[-\d\s.,ОOоo]+',cell['text']):
                candidates.append(cell)
    for cell in sorted(candidates,key=lambda c:c['bbox'][0]):
        if not anchors or cell['bbox'][0]-anchors[-1][-1]['bbox'][0]>.04:anchors.append([cell])
        else:anchors[-1].append(cell)
    centers=[median(c['bbox'][0] for c in a) for a in anchors if len(a)>=2]
    if len(centers)!=2 or centers[1]-centers[0]<.05:
        return [],[{'reason':'Не найдена однозначная пара столбцов координат; формат требует отдельной сверки'}]
    result=[]
    for row in rows:
        selected=[]
        for center in centers:
            near=[c for c in row['cells'] if abs(c['bbox'][0]-center)<.04]
            selected.append(near[0] if len(near)==1 else None)
        if not any(c and c in candidates for c in selected):continue
        left=[c for c in row['cells'] if c['bbox'][2]<centers[0]-.04]
        label=left[0] if len(left)==1 else None
        result.append({'cy':row['cy'],'cells':[label,*selected],
                       'bbox':[min(w['bbox'][0] for w in row['words']),min(w['bbox'][1] for w in row['words']),
                               max(w['bbox'][2] for w in row['words']),max(w['bbox'][3] for w in row['words'])]})
    return result,[]


def combine(views):
    if not isinstance(views,list) or len(views)!=2:raise ValueError('Не получены два чтения OCR')
    parsed=[];issues=[]
    for v,view in enumerate(views):
        if view.get('view')!=v:raise ValueError('Порядок чтений OCR изменился')
        rows,errors=view_rows(view);parsed.append(rows);issues.extend({'view':v,**e} for e in errors)
    aligned=[]
    for v,rows in enumerate(parsed):
        for row in rows:
            matches=[r for r in aligned if abs(r['cy']-row['cy'])<.006]
            if len(matches)>1 or (matches and matches[0]['views'][v] is not None):
                raise ValueError('Неоднозначное совпадение строк двух чтений OCR')
            if matches:matches[0]['views'][v]=row
            else:aligned.append({'cy':row['cy'],'views':[row if v==0 else None,row if v==1 else None]})
    aligned.sort(key=lambda r:r['cy'])
    if len(aligned)>500:raise ValueError('Страница превышает лимит 500 строк координат')
    gaps=[b['cy']-a['cy'] for a,b in zip(aligned,aligned[1:])]
    spacing=median(gaps) if gaps else .02
    tables=[]
    for index,row in enumerate(aligned):
        if not tables or row['cy']-aligned[index-1]['cy']>spacing*2.5:
            tables.append({'ordinal':len(tables)+1,'rows':[],'identity_confirmed':False,'completeness_confirmed':False})
        values=[[literal(c['text'],i==0) if c else None for i,c in enumerate(v['cells'])] if v else [None]*3 for v in row['views']]
        reasons=[]
        for i,name in enumerate(('Точка','Столбец 1','Столбец 2')):
            if any(v[i] is None for v in values):reasons.append(name+': пропуск или неподдержанное чтение')
            elif values[0][i]!=values[1][i]:reasons.append(name+': чтения различаются')
        bounds=[v['bbox'] for v in row['views'] if v]
        row.update(ordinal=len(tables[-1]['rows'])+1,agreement='same_literal_values' if not reasons else 'review_required',
                   issues=reasons,values=values,
                   bbox=[min(b[0] for b in bounds),min(b[1] for b in bounds),max(b[2] for b in bounds),max(b[3] for b in bounds)],
                   verification_required=True)
        tables[-1]['rows'].append(row)
    if len(tables)>32:raise ValueError('Слишком много блоков координат на странице')
    return {'tables':tables,'issues':issues,'warning':WARNING,'algorithm':ALGORITHM,
            'geometry_confirmed':False,'georeferenced':False}
