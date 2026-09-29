import csv
import html
import io
import json
import zipfile
from .review import CHECKS, PACKAGE_CHECKS, WORKFLOWS, present
from .store import now


def safe_cell(value):
    value = str(value if value is not None else '')
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def csv_bytes(rows):
    out = io.StringIO(newline='')
    w = csv.writer(out, delimiter=';')
    for row in rows:
        w.writerow([safe_cell(x) for x in row])
    return out.getvalue().encode('utf-8-sig')


def collection(cs):
    return {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'geometry': c['geometry'], 'properties': {'id': c['id'], 'area_m2': c['area_m2'], 'status': present(c)['status'], 'note': 'Кандидат на проверку, правовая свобода не установлена', 'active': c['active']}} for c in cs]}


def dossier(c, project):
    c = present(c)
    esc = lambda v: html.escape(str(v))
    rows = ''.join(f'<tr><td>{esc(label)}</td><td>{esc(c["checks"].get(k, {}).get("result", "unknown"))}</td><td>{esc(c["checks"].get(k, {}).get("note", "Не подтверждено"))}</td><td>{esc(c["checks"].get(k, {}).get("source", ""))}<br>{esc(c["checks"].get(k, {}).get("date", ""))}</td></tr>' for k, label in {**CHECKS, **PACKAGE_CHECKS}.items())
    sources = ''.join(f'<li>{esc(s["role"])}: {esc(s["title"])} — {esc(s["source"])}; дата {esc(s["checked_at"])}</li>' for s in c['sources'])
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Досье {esc(c['id'])}</title>
    <style>body{{font:16px/1.5 system-ui;max-width:1100px;margin:40px auto;color:#172d2b;padding:0 24px}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border:1px solid #ccc;padding:10px;text-align:left}}th{{background:#e8efeb}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}@media print{{body{{margin:0}}}}</style>
    <h1>Земельное досье {esc(c['id'])}</h1><p>{'УЧЕБНЫЙ ПРИМЕР. Синтетическая территория.' if project == 'demo' else 'Трудовое · пространственный кандидат'}</p>
    <p>Площадь: {c['area_m2']} м². Координаты точки внутри контура (долгота, широта): {esc(c['point'])}.</p>
    <p>Статус проверки: {esc(c['status'])}. Стадия: {esc(WORKFLOWS[c['effective_workflow']])}. Расчёт актуален: {not c['stale'] and c['active']}.</p>
    <p>Досье не является СРЗУ, выпиской ЕГРН или заявлением. Отсутствие пересечений с загруженными контурами не подтверждает отсутствие прав третьих лиц.</p>
    <h2>Что требуется до подачи</h2><ul>{''.join('<li>'+esc(x)+'</li>' for x in c['package_blockers']) or '<li>Проверки отмечены пользователем; финальное подписание и отправка выполняются в официальном сервисе.</li>'}</ul>
    <h2>Проверки и доказательства</h2><table><thead><tr><th>Проверка</th><th>Вывод</th><th>Обоснование</th><th>Источник и дата</th></tr></thead><tbody>{rows}</tbody></table>
    <h2>Пространственные сведения</h2><p>Расстояние до загруженного полигона дороги: {esc(c['road_distance_m'])} м; законность доступа требует отдельной проверки.</p>
    <p>Требуется проектирование меньшего контура: {c['large_window']}.</p><pre>{esc(json.dumps(c['overlays'], ensure_ascii=False, indent=2))}</pre>
    <h2>Источники геометрии</h2><ul>{sources}</ul><h2>Рабочие заметки</h2><pre>{esc(c['notes'])}</pre>
    <p>Номер/источник процедуры: {esc(c['reference'])}. Следующая проверка: {esc(c['deadline'])}.</p>
    <h2>Контур в WGS84</h2><pre>{esc(json.dumps(c['geometry'], ensure_ascii=False))}</pre><p>Выгрузка: {now()}</p></html>'''


def bundle(project, cs, events, endpoints, sources):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        def js(name, obj):
            z.writestr(name, json.dumps(obj, ensure_ascii=False, indent=2))
        js('candidates.geojson', collection(cs))
        js('dossiers.json', [present(c) for c in cs])
        js('audit_log.json', events)
        js('sources.json', sources)
        js('NSPD_ENDPOINT_REGISTRY.json', endpoints)
        fields = ['id', 'purpose', 'type', 'url', 'method', 'parameters', 'crs', 'format', 'geometry', 'auth', 'limitations', 'status', 'checked_at', 'note']
        z.writestr('NSPD_ENDPOINT_REGISTRY.csv', csv_bytes([fields] + [[r.get(k, '') for k in fields] for r in endpoints]))
        z.writestr('registry.csv', csv_bytes([['ID', 'Площадь м²', 'Долгота', 'Широта', 'Статус проверки', 'Стадия', 'Актуален', 'Следующая проверка', 'Причина/заметки']] + [[c['id'], c['area_m2'], *c['point'], present(c)['status'], WORKFLOWS[present(c)['effective_workflow']], c['active'] and not c['stale'], c['deadline'], c['notes']] for c in cs]))
        for c in cs:
            z.writestr(f'dossiers/{c["id"]}.html', dossier(c, project))
            rows = [['Часть', 'Кольцо', 'Вершина', 'Долгота WGS84', 'Широта WGS84']]
            gs = c['geometry']['coordinates'] if c['geometry']['type'] == 'MultiPolygon' else [c['geometry']['coordinates']]
            for pi, poly in enumerate(gs, 1):
                for ri, ring in enumerate(poly, 1):
                    for vi, point in enumerate(ring, 1):
                        rows.append([pi, ri, vi, *point])
            z.writestr(f'coordinates/{c["id"]}_WGS84.csv', csv_bytes(rows))
            js(f'coordinates/{c["id"]}_analysis_projection.json', {'crs_wkt': c['metric_crs'], 'geometry': c['coordinates_metric'], 'note': 'Расчётная равновеликая проекция; не подменяет кадастровую МСК и координаты СРЗУ.'})
        z.writestr('READ_ME.txt', 'УЧЕБНЫЙ ПРОЕКТ — синтетические данные\n' if project == 'demo' else 'Реестр поиска по загруженным данным\n')
        z.writestr('NEXT_STEPS.txt', '1. Подтвердить официальную границу, полноту кадастровой выгрузки, ПЗЗ, ЗОУИТ и права.\n2. Для каждого кандидата заполнить проверки с источником, датой и обоснованием.\n3. Проверить применимость процедуры и уполномоченный орган.\n4. Подготовить окончательный контур, СРЗУ (если требуется) и заявление по актуальной форме.\n5. Войти в официальный сервис, проверить карточку услуги и приложения, подписать и отправить лично.\n6. Сохранить номер, контролировать извещение и решение; при назначении торгов изучить лот и условия.\n7. Регистрация права следует после предусмотренных законом оснований; найденное окно само по себе не является таким основанием.\n')
    return buffer.getvalue()
