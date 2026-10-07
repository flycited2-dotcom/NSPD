"""Dated public evidence and an unsent enquiry; never a title/availability decision."""
import hashlib
import html
import json
import re
from . import municipal, planning_watch, store

ALGORITHM = 'land-status-v1'
LIMITATION = ('Принадлежность и права на контур не установлены. Поля соседних участков относятся только '
              'к соседям. Пустой слой и отсутствие упоминания не подтверждают отсутствие прав, '
              'заявлений или ранее согласованных схем. Применимость регламента и полномочия органа требуют проверки.')
QUESTIONS = (
    'Кому принадлежит земля в пределах приложенного контура: муниципальная, государственная '
    '(Российская Федерация или Республика Крым), неразграниченная либо иная собственность? '
    'Прошу указать основание, реквизиты и границы относящихся к контуру объектов.',
    'Имеются ли права третьих лиц, аренда, безвозмездное пользование, сервитуты, '
    'ранее возникшие права, предоставление, резервирование или фактическое использование земли? '
    'Прошу сообщить доступные сведения и документы без персональных данных правообладателей.',
    'Имеются ли пересекающие контур утверждённые СРЗУ, решения о предварительном согласовании, '
    'проекты межевания, рассматриваемые заявления, извещения о предоставлении или назначенные торги? '
    'Прошу указать номера, даты, статус, границы и официальные ссылки.',
    'Какой орган уполномочен распоряжаться этой землёй? Каковы применимый действующий регламент, '
    'процедура предоставления, условия для заявителя и проверенный канал подачи заявления, '
    'включая Госуслуги при наличии соответствующей услуги?',
)


def service(title):
    text = ' '.join(title.lower().split())
    if 'регламент' not in text or not re.search('земель|сервитут', text):
        return None
    for phrase, key in (
        ('предварительн', 'preapproval'), ('схемы расположения', 'layout'),
        ('на торгах', 'auction'), ('без проведения торгов', 'direct'),
        ('без торгов', 'direct'), ('переоформление прав', 'rights'),
        ('прекращение прав', 'rights'), ('договора аренды', 'lease'),
        ('вида разрешенного', 'use'), ('вида разрешённого', 'use'),
    ):
        if phrase in text:
            return key
    return 'other_land_service'


def first_pages(sha):
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
        raise ValueError('Некорректный SHA-256 PDF регламента')
    path = store.DATA / 'municipal' / (sha + '.pdf')
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Исходный PDF отсутствует или превышает 32 МБ')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha or not raw.startswith(b'%PDF-'):
        raise ValueError('Исходный PDF регламента изменился')
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(raw)
    pages = []
    try:
        for i in range(min(len(doc), 2)):
            page = doc[i]
            try:
                textpage = page.get_textpage()
                try:
                    if textpage.count_chars() > 500000:
                        raise ValueError('Текст страницы превышает лимит')
                    pages.append((i + 1, textpage.get_text_range(errors='strict').replace('\r\n', '\n')))
                finally:
                    textpage.close()
            finally:
                page.close()
    finally:
        doc.close()
    return pages


def text_relations(pages):
    """Only operative numbered paragraphs; a quoted cancellation/title is insufficient."""
    # Quoted calendar day is an observed header form; no body identity fallback.
    normalized = [(n, re.sub(r'[«"](\d{1,2})[»"](?=\s+(?:' + '|'.join(planning_watch.MONTHS) + r')\b)', r'\1', text, flags=re.I)) for n, text in pages]
    own = planning_watch.text_evidence(normalized)['act_identity']
    full = '\n'.join(text for _, text in pages)
    operative = re.search(r'ПОСТАНОВЛЯЮ\s*:', full, re.I)
    refs = []
    if own and operative:
        body = re.split(r'\n\s*(?:Глава администрации|Приложение|УТВЕРЖДЕН)\b', full[operative.end():], maxsplit=1, flags=re.I)[0]
        paragraphs = re.finditer(r'(?:^|\n)\s*\d{1,2}\.(?!\d)\s*(.*?)(?=\n\s*\d{1,2}\.(?!\d)|\Z)', body, re.S)
        for para in paragraphs:
            text = ' '.join(para.group(1).split())
            if not re.match(r'Признать\s+утратившим\s*и?\s+силу\b', text, re.I):
                continue
            # Only direct “от DATE № N” references. References inside a quoted title
            # are not promoted to additional cancellation targets.
            direct = re.split('[«"]', text, maxsplit=1)[0]
            bullets = re.findall(r'(?:^|\n)\s*[-–]\s*от\s+[^«"\n]+', para.group(1), re.I)
            for segment in bullets or [direct]:
                match = re.search(r'\bот\s+' + planning_watch.DATE + r'\s*(?:г\.?\s*)?№\s*' + planning_watch.NUMBER, segment, re.I)
                if not match:
                    continue
                try:
                    target = planning_watch.identity(match.group())
                except ValueError:
                    continue
                if not target or target == own or target in [r['target'] for r in refs]:
                    continue
                offset = operative.end() + para.start(1)
                consumed = 0
                page_number = None
                for n, original in pages:
                    if consumed <= offset < consumed + len(original) + 1:
                        page_number = n
                        break
                    consumed += len(original) + 1
                refs.append({'target': target, 'relation': 'cancellation_in_text', 'page': page_number,
                             'legal_effect_confirmed': False})
    return own, refs


def procedures(catalog):
    catalog = catalog or {}
    if len(catalog.get('items', [])) > municipal.MAX_ITEMS:
        raise ValueError('Каталог регламентов превышает предел документов')
    sources = {s['url']: s for s in catalog.get('sources', []) if s.get('url') in {x['url'] for x in municipal.SOURCES}}
    verified = {}
    documents = []
    for row in catalog.get('items', []):
        kind = service(row.get('title', ''))
        if not kind or row.get('format') != 'pdf':
            continue
        record = {k: row.get(k) for k in ('id', 'title', 'sha256', 'received_at', 'total_pages', 'processed_pages', 'text_layer_complete')}
        try:
            record['url'] = municipal.safe_link(row.get('url', ''), row.get('url', '')) or ''
        except ValueError:
            record['url'] = ''
        record.update(service=kind, state='unverified', act_identity=None, cancellation_references=[],
                      cancellation_observations=[], legal_status_confirmed=False, applicable_to_candidate=False)
        documents.append(record)
        try:
            refs = row.get('listing_references', [])
            observed = []
            for ref in refs:
                source = sources.get(ref.get('parent_url'))
                if not source:
                    continue
                sha = source.get('sha256')
                if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
                    raise ValueError('Не установлен хеш перечня')
                if source['url'] not in verified:
                    path = store.DATA / 'municipal' / (sha + '.html')
                    if not path.is_file() or path.stat().st_size > 25 * 1024 * 1024:
                        raise ValueError('Исходный перечень отсутствует или превышает предел')
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != sha:
                        raise ValueError('Исходный перечень изменился')
                    verified[source['url']] = municipal.parse_html(raw, source['url'])[0]
                if (ref.get('sha256') != sha or ref.get('listed_at') != source.get('received_at') or
                        {'url': row['url'], 'title': ref['title']} not in verified[source['url']]):
                    raise ValueError('Ссылка, заголовок или дата не совпадают с исходным перечнем')
                observed.append(dict(ref))
            if not observed or row.get('state') != 'read' or not row.get('received_at'):
                raise ValueError('Не подтверждена цепочка перечень → прочитанный PDF')
            pages = first_pages(row.get('sha256'))
            own, references = text_relations(pages)
            record.update(state='observed', listing_evidence=observed, act_identity=own,
                          cancellation_references=references, inspected_pages=[n for n, _ in pages],
                          sparse_header_pages=[n for n, text in pages if len(''.join(text.split())) < 20])
        except Exception as exc:
            record['error'] = str(exc)[:500]
    for row in documents:
        if row['state'] != 'observed' or not row['act_identity']:
            continue
        for other in documents:
            for ref in other['cancellation_references']:
                if ref['target'] == row['act_identity']:
                    row['cancellation_observations'].append({k: other[k] for k in ('id', 'url', 'title', 'sha256', 'received_at', 'act_identity')} | {'page': ref['page'], 'legal_effect_confirmed': False})
    return {'algorithm': ALGORITHM, 'catalog_id': catalog.get('id'), 'catalog_at': catalog.get('catalog_at'),
            'documents': documents, 'complete': False, 'limitation': LIMITATION}


def assessment(candidate, values):
    context = values.get('nspd_context') or {}
    survey = values.get('survey') or {}
    # Every context observation must explicitly belong to this same area.
    applicable = bool(survey.get('bounds') and context.get('bounds') == survey.get('bounds'))
    schemas = {}
    for mode in ('schemes', 'planned_parcels'):
        layer = context.get('layers', {}).get(mode, {}) if applicable else {}
        schemas[mode] = {k: layer.get(k) for k in ('state', 'received_at', 'sha256', 'count', 'source', 'error')}
        schemas[mode]['coverage_confirmed'] = False
        schemas[mode]['candidate_matches'] = len(candidate.get('context_matches', {}).get(mode, [])) if applicable else None
    return {'algorithm': ALGORITHM, 'ownership': 'unknown', 'third_party_rights': 'unknown',
            'prior_applications': 'unknown', 'prior_schemes_absent_confirmed': False,
            'authority': 'unknown', 'source_fields': {k: v for k, v in candidate.get('source_fields', {}).items() if k in ('cad_num', 'ownership_type', 'right_type')},
            'schema_observations': schemas, 'municipal_catalog_id': (values.get('municipal') or {}).get('id'),
            'related_public_documents': len(candidate.get('documents', [])),
            'limitation': LIMITATION}


def enquiry(data, candidate_id):
    result = data.get('result') or {}
    candidate = next((c for c in result.get('candidates', []) if c['id'] == candidate_id), None)
    if not candidate:
        raise ValueError('Контур не найден в этой версии')
    lines = ['ПРОЕКТ ЗАПРОСА СВЕДЕНИЙ О ЗЕМЛЕ — НЕ ОТПРАВЛЕН',
             'Адресат: [уточнить уполномоченный орган и официальный канал]',
             'Заявитель и обратный адрес: [заполнить перед отправкой]', '',
             'Прошу предоставить доступные сведения о территории, обозначенной в приложении GeoJSON.',
             f"Контур: {candidate['id']}. Версия расчёта: {result.get('id')} от {result.get('created_at')}.",
             f"Расчётная площадь: {candidate['area_m2']} м². Точка внутри (долгота, широта): {candidate['point']}.",
             f"Кадастровый номер контура: {candidate.get('cadastral_number') or 'не установлен; номер не присваивался'}.",
             'Координаты WGS84; контур поисковый, площадь расчётная. Требуется уточнение в надлежащей системе координат.',
             'Предполагаемая цель: [указать цель использования и основания/статус заявителя].', '',
             *[f'{i}. {text}' for i, text in enumerate(QUESTIONS, 1)], '',
             'Приложение: GeoJSON с этой же версией и точными вершинами поискового контура.',
             'Это запрос информации, не заявление о предоставлении, регистрации права или участии в торгах.',
             LIMITATION]
    if data.get('stale'):
        lines.append('ВНИМАНИЕ: эта версия расчёта устарела; перед отправкой обновите источники и приложение.')
    lines.extend(['', 'Геометрия WGS84:', json.dumps(candidate['geometry'], ensure_ascii=False)])
    return ('\n'.join(lines) + '\n').encode('utf-8')


def enquiry_geometry(data, candidate_id):
    enquiry(data, candidate_id)  # Validate the same candidate/version contract.
    result = data['result']
    candidate = next(c for c in result['candidates'] if c['id'] == candidate_id)
    return {'type': 'FeatureCollection', 'source_result_id': result['id'], 'stale': bool(data.get('stale')),
            'purpose': 'Приложение к неотправленному запросу сведений', 'warning': LIMITATION,
            'features': [{'type': 'Feature', 'id': candidate['id'], 'geometry': candidate['geometry'],
                          'properties': {k: candidate.get(k) for k in ('id', 'area_m2', 'cadastral_number', 'rights_confirmed', 'srzu_ready')} | {'calculated_at': result.get('created_at'), 'is_srzu': False}}]}


def dossier_section(result, candidate, stale=False):
    esc = lambda x: html.escape(str(x), quote=True)
    query = '?result_id=' + esc(result.get('id', '')) + '&amp;id=' + esc(candidate['id'])
    snapshot = result.get('sources', {}).get('land_status') or {}
    docs = snapshot.get('documents', [])
    rows = []
    for row in docs:
        warnings = (' · Есть текстовое указание об отмене; нужна проверка действия.' if row.get('cancellation_observations') else '')
        identity = row.get('act_identity')
        label = ('<a href="' + esc(row['url']) + '">' + esc(row['title']) + '</a>') if row['url'] else esc(row['title'])
        rows.append('<li>' + label + ' · PDF получен ' + esc(row.get('received_at')) +
                    ' · Реквизиты в текстовом заголовке: ' + (esc(identity['date'] + ' № ' + identity['number']) if identity else 'не установлены') +
                    warnings + (' · Цепочка источников не подтверждена: ' + esc(row.get('error')) if row['state'] != 'observed' else '') + '<details><summary>Основания и даты</summary><pre>' +
                    esc(json.dumps(row, ensure_ascii=False, indent=2)) + '</pre></details></li>')
    return ('<h3>Принадлежность, права и ранее согласованные схемы</h3><p>' + esc(LIMITATION) + '</p>' +
            '<p>Публичная принадлежность: неизвестна. Права третьих лиц: не проверены. Ранее поданные заявления: не установлены. Уполномоченный орган: требует уточнения.</p>' +
            '<p><a href="/api/recon/rights-request' + query + '">Скачать проект запроса сведений</a> · ' +
            '<a href="/api/recon/rights-request.geojson' + query + '">Скачать контур для запроса</a></p>' +
            ('<p>Расчёт устарел: обновите источники перед отправкой.</p>' if stale else '') +
            '<details><summary>Наблюдения для этого контура</summary><pre>' +
            esc(json.dumps(candidate.get('land_status') or {}, ensure_ascii=False, indent=2)) + '</pre></details>' +
            '<details><summary>Официальные регламенты и указания об отмене (' + str(len(docs)) + ')</summary>' +
            '<p>Документы перечня поселения. Применимость к этому контуру, действующая редакция и канал подачи не подтверждены.</p><ul>' +
            ''.join(rows) + '</ul></details>')
