"""Reproducible batch runs against the local workspace."""
import argparse
import json
from pathlib import Path
from land import store
from land import planning_watch
from land.geometry import validate_layer
from land.exports import bundle
from app import run_analysis, SOURCES

def main():
    p=argparse.ArgumentParser(description='Земельная разведка: воспроизводимый поиск')
    p.add_argument('--project',choices=['trudovoe','demo'],default='trudovoe')
    sub=p.add_subparsers(dest='command',required=True)
    imp=sub.add_parser('import',help='Импорт пакета с role, metadata, geojson')
    imp.add_argument('file',type=Path)
    run=sub.add_parser('search')
    run.add_argument('--min-area',type=float,default=400)
    run.add_argument('--max-area',type=float,default=2500)
    run.add_argument('--min-width',type=float,default=12)
    exp=sub.add_parser('export')
    exp.add_argument('file',type=Path)
    district=sub.add_parser('planning',help='Районные проекты и ПЗЗ: ограниченные перечни и версии')
    district.add_argument('action',choices=['catalog','read','report'])
    district.add_argument('--retry',action='store_true')
    district.add_argument('--output',type=Path,help='HTML-отчёт с источниками, датами и конфликтами')
    args=p.parse_args()
    store.init()
    if args.command=='import':
        layer=validate_layer(json.loads(args.file.read_text(encoding='utf-8-sig')))
        store.save_layer(args.project,layer)
        print('Импортировано объектов:',len(layer['geojson']['features']))
    elif args.command=='search':
        print(json.dumps(run_analysis(args.project,{'min_area':args.min_area,'max_area':args.max_area,'min_width':args.min_width}),ensure_ascii=False,indent=2))
    elif args.command=='export':
        args.file.write_bytes(bundle(args.project,store.candidates(args.project),store.events(args.project),store.get_setting('endpoints',[]),SOURCES))
        print(args.file.resolve())
    elif args.command=='planning':
        if args.action=='catalog':
            result=planning_watch.catalog(args.project)
        elif args.action=='read':
            current=store.get_setting('planning_watch_'+args.project) or {}
            result=planning_watch.read(args.project,{'id':current.get('id'),'retry':args.retry})
        else:
            result=planning_watch.report(args.project)
        if args.output:
            args.output.write_bytes(planning_watch.html_report(args.project))
        print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
