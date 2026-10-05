"""Word positions from the two existing, verified OCR images; no new PDF rendering."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from .ocr_worker import engine, recognize
from . import store

ALGORITHM='word-boxes-v1'


async def extract(folder, number, hashes):
    from PIL import Image
    ocr,info=engine();views=[]
    for view,digest in enumerate(hashes):
        path=folder/f'p{number}-v{view}.png'
        if path.stat().st_size>16*1024*1024:raise ValueError('Изображение OCR превышает лимит')
        raw=path.read_bytes()
        if len(raw)>16*1024*1024 or hashlib.sha256(raw).hexdigest()!=digest:
            raise ValueError('Изображение OCR изменилось или превышает лимит')
        with Image.open(path) as image:
            if image.format!='PNG' or image.n_frames!=1 or min(image.size)<=0 or max(image.size)>10000 or image.width*image.height>25000000:
                raise ValueError('Размер или формат изображения OCR не поддержан')
            image.verify()
        result=await asyncio.wait_for(recognize(ocr,path,word_boxes=True),timeout=20)
        result.update(view=view,image_sha256=digest)
        views.append(result)
    return {'algorithm':ALGORITHM,'page':number,'engine':info,'processed_at':store.now(),'views':views}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('--folder',type=Path,required=True)
    parser.add_argument('--page',type=int,required=True)
    parser.add_argument('--hash',action='append',required=True)
    args=parser.parse_args()
    try:
        if not 1<=args.page<=200 or len(args.hash)!=2:raise ValueError('Недопустимая страница или число чтений')
        print(json.dumps(asyncio.run(extract(args.folder,args.page,args.hash)),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error':str(exc)[:500]},ensure_ascii=False));raise SystemExit(1)


if __name__=='__main__':main()
