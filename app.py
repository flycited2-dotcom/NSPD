"""Launch: python app.py. Local-only HTTP workspace with auditable GIS jobs."""
import argparse
import json
import mimetypes
import os
import secrets
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from shapely.geometry import mapping, shape
from shapely.ops import unary_union
from land import store
from land.geometry import ROLES, analyse, validate_layer
from land.review import CHECKS, PACKAGE_CHECKS, WORKFLOWS, present, validate_update
from land.network import audit_nspd, fetch_geojson, inspect_har
from land.demo import fixture_layers
from land.exports import bundle, dossier
from land import nspd
from land import survey
from land import publications
from land import torgi
from land import municipal
from land import municipal_local
from land import ocr
from land import schemes
from land import torgi_docs
from land import georeference
from land import torgi_visual

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
JOBS = {}
JOB_LOCK = threading.RLock()
SOURCES = json.loads((ROOT / 'config/sources.json').read_text(encoding='utf-8'))


def project_name(value):
    if value not in ['trudovoe', 'demo']:
        raise ValueError('Неизвестный проект')
    return value


def state(project):
    ls = store.layers(project)
    with JOB_LOCK:
        jobs = [dict(id=k, **v) for k, v in JOBS.items() if v['project'] == project]
    return {'project': project, 'layers': ls, 'candidates': [present(c) for c in store.candidates(project)],
            'events': store.events(project), 'endpoints': store.get_setting('endpoints', []), 'sources': SOURCES,
            'roles': ROLES, 'checks': CHECKS, 'package_checks': PACKAGE_CHECKS, 'workflows': WORKFLOWS,
            'jobs': jobs, 'nspd_last': {k: v for k, v in (store.get_setting('nspd_' + project, {}) or {}).items() if k in ('received_at', 'snapshot', 'count')}}


def launch_job(project, kind, work):
    with JOB_LOCK:
        if any(j['state'] == 'running' and j['project'] == project for j in JOBS.values()):
            raise ValueError('Дождитесь завершения текущей операции проекта')
        job_id = uuid.uuid4().hex
        JOBS[job_id] = {'project': project, 'kind': kind, 'state': 'running', 'started_at': store.now()}
    def run():
        try:
            result = work()
            with JOB_LOCK:
                JOBS[job_id].update(state='done', result=result, finished_at=store.now())
        except Exception as exc:
            with JOB_LOCK:
                JOBS[job_id].update(state='error', error=str(exc), finished_at=store.now())
            with store.connect() as db:
                store.event(db, project, 'job_error', {'kind': kind, 'error': str(exc)})
    threading.Thread(target=run, daemon=True).start()
    return {'job_id': job_id}


def run_analysis(project, params):
    # Lock input changes until result persistence, so revisions cannot be marked fresh accidentally.
    with store.LOCK:
        results, summary = analyse(store.layers(project), params)
        store.save_results(project, results, summary, expected_fingerprint=summary['fingerprint'])
    return dict(summary, count=len(results))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args)

    def send(self, value, code=200, content_type='application/json; charset=utf-8', filename=None):
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(raw)

    def allowed(self):
        return self.headers.get('Host') in [f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}']

    def do_GET(self):
        if not self.allowed():
            return self.send({'error': 'Local access only'}, 403)
        try:
            p = urlparse(self.path)
            query = parse_qs(p.query)
            project = project_name(query.get('project', ['trudovoe'])[0])
            if p.path == '/api/state':
                return self.send(state(project))
            if p.path == '/api/session':
                return self.send({'token': TOKEN, 'app': 'land-recon'})
            if p.path == '/api/municipal/ocr/page':
                raw = ocr.preview(project, query.get('document', [''])[0], int(query.get('page', ['0'])[0]), int(query.get('view', ['1'])[0]))
                return self.send(raw, content_type='image/png')
            if p.path == '/api/torgi/documents/image':
                number = int(query['page'][0]) if 'page' in query else None
                view = int(query.get('view', ['0'])[0]) if number is not None else None
                return self.send(torgi_visual.preview(project, query.get('file', [''])[0], number, view), content_type='image/png')
            if p.path in ('/api/schemes', '/api/schemes/export'):
                result = store.get_setting('schemes_' + project, None)
                attempt = store.get_setting('schemes_attempt_' + project, None)
                with JOB_LOCK:
                    active = any(j['state'] == 'running' and j['project'] == project for j in JOBS.values())
                if attempt and attempt['state'] == 'running' and not active:
                    attempt = dict(attempt, state='interrupted')
                catalog = store.get_setting('municipal_' + project, {}) or {}
                remaining = len(schemes.pending(catalog, result or {})) if catalog else 0
                return self.send({'result':result, 'attempt':attempt, 'current_catalog_id':catalog.get('id'), 'remaining':remaining},
                                 filename='scheme-coordinates.json' if p.path.endswith('/export') else None)
            if p.path in ('/api/georeference','/api/georeference/export'):
                result=store.get_setting('georeference_'+project)
                attempt=store.get_setting('georeference_attempt_'+project)
                current=store.get_setting('schemes_'+project,{}) or {}
                survey_result=store.get_setting('survey_'+project,{}) or {}
                with JOB_LOCK:
                    active=any(j['state']=='running' and j['project']==project for j in JOBS.values())
                if attempt and attempt['state']=='running' and not active:attempt=dict(attempt,state='interrupted')
                return self.send({'result':result,'attempt':attempt,'choices':georeference.choices(current),
                                  'current_schemes_id':current.get('id'),'current_survey_id':survey_result.get('id')},
                                 filename='scheme-georeference-preview.json' if p.path.endswith('/export') else None)
            if p.path in ('/api/torgi/documents', '/api/torgi/documents/export'):
                result = store.get_setting('torgi_documents_' + project, None)
                attempt = store.get_setting('torgi_documents_attempt_' + project, None)
                reading = store.get_setting('torgi_files_attempt_' + project, None)
                reprocessing = store.get_setting('torgi_reprocess_attempt_' + project, None)
                visual = store.get_setting('torgi_visual_attempt_' + project, None)
                search = store.get_setting('torgi_' + project, None)
                with JOB_LOCK:
                    active = any(j['state']=='running' and j['project']==project for j in JOBS.values())
                if attempt and attempt['state']=='running' and not active:attempt=dict(attempt,state='interrupted')
                if reading and reading['state']=='running' and not active:reading=dict(reading,state='interrupted')
                if reprocessing and reprocessing['state']=='running' and not active:reprocessing=dict(reprocessing,state='interrupted')
                if visual and visual['state']=='running' and not active:visual=dict(visual,state='interrupted')
                same = result and search and result.get('search_created_at')==search['created_at']
                return self.send({'result':result,'attempt':attempt,'reading_attempt':reading,'reprocess_attempt':reprocessing,'current_search_id':(search or {}).get('id'),
                                  'visual_attempt':visual,'visual_remaining':len(torgi_visual.queue(result or {})),
                                  'cards_remaining':len(torgi_docs.metadata_queue(search,result if same else {})) if search else 0,
                                  'files_to_reprocess':len(torgi_docs.reprocess_queue(result or {})),
                                  'files_remaining':len(torgi_docs.file_queue(result or {}))},
                                 filename='torgi-documents.json' if p.path.endswith('/export') else None)
            if p.path in ('/api/municipal', '/api/municipal/export'):
                result = store.get_setting('municipal_' + project, None)
                attempt = store.get_setting('municipal_attempt_' + project, None)
                ocr_attempt = store.get_setting('municipal_ocr_attempt_' + project, None)
                local_attempt = store.get_setting('municipal_local_attempt_' + project, None)
                with JOB_LOCK:
                    active = any(j['state'] == 'running' and j['project'] == project for j in JOBS.values())
                if attempt and attempt['state'] == 'running' and not active:
                    attempt = dict(attempt, state='interrupted')
                if ocr_attempt and ocr_attempt['state'] == 'running' and not active:
                    ocr_attempt = dict(ocr_attempt, state='interrupted')
                if local_attempt and local_attempt['state']=='running' and not active:
                    local_attempt=dict(local_attempt,state='interrupted')
                return self.send({'result': result, 'attempt': attempt, 'ocr_attempt': ocr_attempt,
                                  'local_attempt':local_attempt,'local_remaining':len(municipal_local.queue(result)),
                                  'current_survey_id': (store.get_setting('survey_' + project, {}) or {}).get('id')},
                                 filename='municipal-evidence.json' if p.path.endswith('/export') else None)
            if p.path in ('/api/publications', '/api/publications/export'):
                result = store.get_setting('publications_' + project, None)
                return self.send({'result': result}, filename='official-publications-check.json' if p.path.endswith('/export') else None)
            if p.path in ('/api/torgi', '/api/torgi/export'):
                result = store.get_setting('torgi_' + project, None)
                attempt = store.get_setting('torgi_attempt_' + project, None)
                geometry_attempt = store.get_setting('torgi_geometry_attempt_' + project, None)
                with JOB_LOCK:
                    active = any(j['state'] == 'running' and j['project'] == project for j in JOBS.values())
                if not active:
                    if attempt and attempt['state'] == 'running':
                        attempt = dict(attempt, state='interrupted')
                    if geometry_attempt and geometry_attempt['state'] == 'running':
                        geometry_attempt = dict(geometry_attempt, state='interrupted')
                return self.send({'result': result, 'attempt': attempt, 'geometry_attempt': geometry_attempt,
                                  'current_survey_id': (store.get_setting('survey_' + project, {}) or {}).get('id')},
                                 filename='torgi-observed.json' if p.path.endswith('/export') else None)
            if p.path in ('/api/survey', '/api/survey/export'):
                result = store.get_setting('survey_' + project, None)
                attempt = store.get_setting('survey_attempt_' + project, None)
                with JOB_LOCK:
                    active = any(j['state'] == 'running' and j['project'] == project for j in JOBS.values())
                if attempt and attempt['state'] == 'running' and not active:
                    attempt = dict(attempt, state='interrupted', error='Обследование прервано; предыдущий результат не обновлён.')
                if p.path.endswith('/export'):
                    if not result:
                        raise ValueError('Сначала выполните обследование')
                    return self.send(result, filename='survey-unverified.json')
                return self.send({'result': result, 'attempt': attempt, 'watchlist': store.get_setting('survey_watch_' + project, [])})
            if p.path == '/api/nspd':
                attempt = store.get_setting('nspd_attempt_' + project, None)
                with JOB_LOCK:
                    active = any(j['state'] == 'running' and j['project'] == project for j in JOBS.values())
                if attempt and attempt['state'] == 'running' and not active:
                    attempt = dict(attempt, state='interrupted', error='Приложение перезапущено или выполнение прервано; результат не подтверждён.')
                return self.send({'result': store.get_setting('nspd_' + project, None), 'watchlist': store.get_setting('nspd_watch_' + project, []),
                                  'area': store.get_setting('nspd_area_' + project, None), 'attempt': attempt})
            if p.path == '/api/nspd/export':
                result = store.get_setting('nspd_' + project, None)
                if not result:
                    raise ValueError('Сначала выполните поиск')
                return self.send(dict(result['geojson'], metadata={k: v for k, v in result.items() if k != 'geojson'}), filename='nspd-observed.geojson')
            if p.path == '/api/export':
                raw = bundle(project, store.candidates(project), store.events(project), store.get_setting('endpoints', []), SOURCES)
                return self.send(raw, content_type='application/zip', filename=f'land-{project}.zip')
            if p.path == '/api/dossier':
                c = next((c for c in store.candidates(project) if c['id'] == query.get('id', [''])[0]), None)
                if c is None:
                    return self.send({'error': 'Кандидат не найден'}, 404)
                return self.send(dossier(c, project).encode('utf-8'), content_type='text/html; charset=utf-8')
            if p.path.startswith('/api/jobs/'):
                with JOB_LOCK:
                    job = JOBS.get(p.path.rsplit('/', 1)[-1])
                    return self.send(job or {'error': 'Операция не найдена'}, 200 if job else 404)
            rel = 'index.html' if p.path == '/' else p.path.lstrip('/')
            path = (ROOT / 'web' / rel).resolve()
            if not path.is_relative_to((ROOT / 'web').resolve()) or not path.is_file():
                return self.send({'error': 'Не найдено'}, 404)
            ct = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
            if ct.startswith('text/') or path.suffix == '.js':
                ct += '; charset=utf-8'
            return self.send(path.read_bytes(), content_type=ct)
        except (ValueError, TypeError) as exc:
            self.send({'error': str(exc)}, 400)

    def reject_post(self, message):
        # On Windows, closing a socket with an unread body can erase the 403 response.
        # Drain only small bodies without parsing, using a one-second socket timeout.
        timeout = self.connection.gettimeout()
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if 0 < size <= 65536:
                self.connection.settimeout(1)
                self.rfile.read(size)
        except (ValueError, OSError):
            pass
        finally:
            self.connection.settimeout(timeout)
        return self.send({'error': message}, 403)

    def do_POST(self):
        if not self.allowed() or self.headers.get('X-Local-Token') != TOKEN:
            return self.reject_post('Обновите страницу приложения')
        origin = self.headers.get('Origin')
        if origin and origin not in [f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}']:
            return self.reject_post('Недопустимый источник запроса')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0 or size > 30 * 1024 * 1024:
                raise ValueError('Размер запроса должен быть от 1 байта до 30 МБ')
            data = json.loads(self.rfile.read(size))
            project = project_name(data.get('project', 'trudovoe'))
            path = urlparse(self.path).path
            if path == '/api/survey':
                return self.send(launch_job(project, 'Обследование шести слоёв', lambda: survey.run(project, data)))
            if path == '/api/survey/recalculate':
                return self.send(launch_job(project, 'Пересчёт сохранённой геометрии', lambda: survey.recalculate(project, data)))
            if path == '/api/municipal':
                return self.send(launch_job(project, 'Каталог муниципальных документов', lambda: municipal.catalog(project)))
            if path == '/api/municipal/read':
                return self.send(launch_job(project, 'Чтение муниципальных документов', lambda: municipal.read(project, data)))
            if path == '/api/municipal/local':
                return self.send(launch_job(project,'Перечтение сохранённых муниципальных PDF',lambda:municipal_local.run(project,data)))
            if path == '/api/municipal/ocr':
                return self.send(launch_job(project, 'Локальное распознавание сканов', lambda: ocr.run(project, data)))
            if path == '/api/schemes':
                return self.send(launch_job(project, 'Извлечение таблиц координат', lambda: schemes.run(project, data)))
            if path == '/api/georeference':
                return self.send(launch_job(project, 'Проверка предварительной геопривязки', lambda: georeference.run(project,data)))
            if path == '/api/torgi/documents':
                return self.send(launch_job(project, 'Карточки и вложения ГИС Торги', lambda: torgi_docs.metadata(project, data)))
            if path == '/api/torgi/documents/read':
                return self.send(launch_job(project, 'Чтение вложений ГИС Торги', lambda: torgi_docs.read(project, data)))
            if path == '/api/torgi/documents/reprocess':
                return self.send(launch_job(project, 'Локальное перечтение документов торгов', lambda: torgi_docs.reprocess(project, data)))
            if path == '/api/torgi/documents/ocr':
                return self.send(launch_job(project, 'Локальное OCR изображений и PDF торгов', lambda: torgi_visual.run(project, data)))
            if path == '/api/publications':
                return self.send(launch_job(project, 'Проверка официальных публикаций', lambda: publications.run(project)))
            if path == '/api/torgi':
                return self.send(launch_job(project, 'Поиск лотов ГИС Торги', lambda: torgi.run(project, data)))
            if path == '/api/torgi/geometry':
                return self.send(launch_job(project, 'Сопоставление лотов с областью', lambda: torgi.locate(project, data)))
            if path == '/api/survey/watch':
                with store.LOCK:
                    result = store.get_setting('survey_' + project, {})
                    if data.get('survey_id') != result.get('id'):
                        raise ValueError('Результат изменился. Обновите страницу.')
                    feature = next((f for f in result.get('gaps', {}).get('features', []) if f['id'] == data.get('id')), None)
                    if not feature:
                        raise ValueError('Промежуток не найден')
                    items = store.get_setting('survey_watch_' + project, [])
                    if not any(x['survey_id'] == result['id'] and x['feature']['id'] == feature['id'] for x in items):
                        items.append({'survey_id': result['id'], 'feature': feature, 'added_at': store.now(), 'status': 'Требуется проверка', 'source_date': result['created_at']})
                        store.set_setting('survey_watch_' + project, items)
                return self.send({'count': len(items)})
            if path == '/api/nspd/search':
                return self.send(launch_job(project, 'Получение объектов НСПД', lambda: nspd.search(project, data)))
            if path == '/api/nspd/area':
                nspd.spatial_body(data.get('bounds'), nspd.catalog()['parcels']['categoryId'])
                area = {'bounds': list(map(float, data['bounds'])), 'updated_at': store.now(), 'official_boundary': False}
                store.set_setting('nspd_area_' + project, area)
                with store.connect() as db:
                    store.event(db, project, 'nspd_area', area)
                return self.send(area)
            if path == '/api/nspd/watch':
                with store.LOCK:
                    result = store.get_setting('nspd_' + project, {})
                    feature = next((f for f in result.get('geojson', {}).get('features', []) if f['id'] == data.get('id')), None)
                    if not feature:
                        raise ValueError('Объект не найден в текущем результате')
                    watch = store.get_setting('nspd_watch_' + project, [])
                    if not any(x['feature']['id'] == feature['id'] for x in watch):
                        watch.append({'feature': feature, 'added_at': store.now(), 'source_date': result['source_date'], 'source': result['source'], 'status': 'В разработке — требуется проверка прав и ограничений'})
                        store.set_setting('nspd_watch_' + project, watch)
                return self.send({'count': len(watch)})
            if path == '/api/shutdown':
                self.send({'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == '/api/layers':
                layer = validate_layer(data)
                if data.get('coverage_current_boundary'):
                    bs = [l for l in store.layers(project) if l['role'] == 'boundary']
                    if not bs:
                        raise ValueError('Сначала загрузите границу исследования')
                    layer['metadata']['coverage'] = mapping(unary_union([shape(f['geometry']) for f in bs[0]['geojson']['features']]))
                    layer['metadata']['coverage_basis'] = 'Пользователь подтвердил полное покрытие текущей границы исследования'
                store.save_layer(project, layer)
                return self.send({'ok': True, 'count': len(layer['geojson']['features'])})
            if path == '/api/import-url':
                def work():
                    raw, digest = fetch_geojson(data['url'])
                    layer = validate_layer(dict(data, geojson=raw))
                    if data.get('coverage_current_boundary'):
                        bs = [l for l in store.layers(project) if l['role'] == 'boundary']
                        if not bs:
                            raise ValueError('Сначала загрузите границу исследования')
                        layer['metadata']['coverage'] = mapping(unary_union([shape(f['geometry']) for f in bs[0]['geojson']['features']]))
                        layer['metadata']['coverage_basis'] = 'Подтверждено пользователем'
                    layer['metadata']['sha256'] = digest
                    layer['metadata']['fetch_url'] = data['url']
                    store.save_layer(project, layer)
                    return {'count': len(raw['features'])}
                return self.send(launch_job(project, 'Загрузка GeoJSON', work))
            if path == '/api/search':
                return self.send(launch_job(project, 'Поиск кандидатов', lambda: run_analysis(project, data.get('parameters', {}))))
            if path == '/api/demo':
                if project != 'demo':
                    raise ValueError('Учебные данные допускаются только в учебном проекте')
                for layer in fixture_layers():
                    store.save_layer(project, layer)
                return self.send(run_analysis(project, {'min_area': 300, 'max_area': 2500, 'min_width': 10, 'clearance': 0}))
            if path == '/api/audit':
                def work():
                    row = audit_nspd()
                    previous = store.get_setting('endpoints', [])
                    store.set_setting('endpoints', [row] + [x for x in previous if x['id'] != row['id']])
                    with store.connect() as db:
                        store.event(db, project, 'endpoint_audit', row)
                    return row
                return self.send(launch_job(project, 'Проверка НСПД', work))
            if path == '/api/har':
                rows = inspect_har(data['har'])
                if not rows:
                    raise ValueError('В HAR не найдены подходящие запросы НСПД')
                prev = {x['id']: x for x in store.get_setting('endpoints', [])}
                changes = [{'id': x['id'], 'before': prev.get(x['id']), 'after': x} for x in rows]
                prev.update({x['id']: x for x in rows})
                store.set_setting('endpoints', list(prev.values()))
                with store.connect() as db:
                    store.event(db, project, 'endpoint_registry_update', changes)
                return self.send({'count': len(rows)})
            if path == '/api/candidate':
                with store.LOCK:
                    c = next((c for c in store.candidates(project) if c['id'] == data.get('id')), None)
                    if not c:
                        raise ValueError('Кандидат не найден')
                    result = store.update_candidate(project, c['id'], validate_update(c, data))
                return self.send(present(result))
            return self.send({'error': 'Не найдено'}, 404)
        except (ValueError, KeyError, TypeError) as exc:
            return self.send({'error': str(exc)}, 400)
        except Exception:
            traceback.print_exc()
            return self.send({'error': 'Ошибка сервера. Подробности сохранены в локальном журнале.'}, 500)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    store.init()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    (store.DATA / 'server.pid').write_text(str(os.getpid()), encoding='ascii')
    print(f'Land Recon: http://127.0.0.1:{args.port}', flush=True)
    server.serve_forever()
    server.server_close()


if __name__ == '__main__':
    main()
