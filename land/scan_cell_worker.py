"""Small bounded batches of cell crops for local Windows OCR."""
import asyncio,hashlib,json,math,re,sys
from pathlib import Path
from PIL import Image
from .ocr_worker import engine,recognize
from .scan_cells import ALGORITHM,BATCH_ROWS
from . import store


async def extract(folder,request):
    records=[]
    page=request.get('page')
    if isinstance(page,bool) or not isinstance(page,int) or not 1<=page<=200:raise ValueError('Недопустимая страница ячеек')
    if not re.fullmatch(r'[a-f0-9]{64}',request.get('source_sha256','')):raise ValueError('Неверный источник ячеек')
    if not 1<=len(request['rows'])<=BATCH_ROWS or len(request['source_views'])!=2:raise ValueError('Неверный размер порции ячеек')
    for row in request['rows']:
        if not re.fullmatch(r'[a-f0-9]{64}',row.get('key','')) or len(row.get('bands',[]))!=2 or any(len(v)!=3 for v in row['bands']):raise ValueError('Неверные поля ячеек')
    for vi,v in enumerate(request['source_views']):
        if v.get('view')!=vi:raise ValueError('Неверный порядок изображений')
    ocr,info=engine()
    for row in request['rows']:
        views=[]
        for vi,v in enumerate(request['source_views']):
            path=folder/f"p{request['page']}-v{vi}.png"
            if path.stat().st_size>16*1024*1024 or hashlib.sha256(path.read_bytes()).hexdigest()!=v['image_sha256']:
                raise ValueError('Исходное изображение ячеек изменилось')
            with Image.open(path) as image:
                if image.format!='PNG' or image.n_frames!=1 or image.size!=(v['width'],v['height']) or image.width*image.height>25000000 or max(image.size)>10000:
                    raise ValueError('Размер или формат изображения ячеек изменился')
                cells=[]
                for ci,box in enumerate(row['bands'][vi]):
                    if len(box)!=4 or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in box):raise ValueError('Некорректные границы ячейки')
                    if not 0<=box[0]<box[2]<=1 or not 0<=box[1]<box[3]<=1:raise ValueError('Ячейка вне изображения')
                    px=[math.floor(box[0]*image.width),math.floor(box[1]*image.height),math.ceil(box[2]*image.width),math.ceil(box[3]*image.height)]
                    crop=image.crop(px)
                    try:
                        large=crop.resize((crop.width*2,crop.height*2))
                        try:
                            if max(large.size)>info['max_image_dimension'] or large.width*large.height>4000000:raise ValueError('Ячейка превышает предел OCR')
                            target=folder/'cells'/f"{row['key']}-v{vi}-c{ci}.png";large.save(target)
                        finally:large.close()
                    finally:crop.close()
                    read=await asyncio.wait_for(recognize(ocr,target),timeout=10)
                    if len(read['text'])>500:raise ValueError('Текст ячейки превышает лимит')
                    cells.append({'text':read['text'],'bbox':box,'pixel_bbox':px,'scale':2,
                                  'image_sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
                views.append({'view':vi,'cells':cells})
        records.append({'key':row['key'],'table':row['table'],'row':row['row'],'bands':row['bands'],'views':views})
    return {'algorithm':ALGORITHM,'source_sha256':request['source_sha256'],'page':request['page'],
            'source_views':request['source_views'],'processed_at':store.now(),'engine':info,'rows':records}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        path=Path(sys.argv[1]);folder=Path(sys.argv[2])
        if path.stat().st_size>65536:raise ValueError('Параметры ячеек превышают лимит')
        request=json.loads(path.read_text(encoding='utf-8'))
        print(json.dumps(asyncio.run(extract(folder,request)),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error':str(exc)[:500]},ensure_ascii=False));raise SystemExit(1)


if __name__=='__main__':main()
