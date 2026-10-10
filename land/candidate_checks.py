"""Pure candidate-specific progress from a frozen result; observations never pass legal checks."""
from itertools import chain, islice

from .review import CHECKS

ALGORITHM = 'candidate-checks-v1'
MAX_EVIDENCE = 20
ENVIRONMENT = ('water', 'forests', 'protected', 'heritage')
RESTRICTION_MODES = ('red_lines',) + ENVIRONMENT + ('schemes', 'planned_parcels')
LAYER_TITLES = {'red_lines': 'Красные линии НСПД', 'water': 'Водные объекты НСПД',
                'forests': 'Лесничества НСПД', 'protected': 'ООПТ НСПД', 'heritage': 'Территории ОКН НСПД',
                'schemes': 'Схемы расположения НСПД', 'planned_parcels': 'Участки по межеванию НСПД'}


def _text(value):
    return str(value)[:2000] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ''


def _rows(value):
    return value if isinstance(value, list) else []


def _evidence(row, state=None, count=None, label=None):
    count = row.get('count') if count is None else count
    return {'source': _text(row.get('source') or row.get('url') or row.get('map_url')),
            'received_at': _text(row.get('received_at') or row.get('search_date')),
            'sha256': _text(row.get('sha256')), 'id': _text(row.get('id') or row.get('feature_id')),
            'state': _text(state or row.get('observation_state') or row.get('state')) or ('observed' if row.get('received_at') or row.get('search_date') or row.get('id') else 'not_available'),
            'label': _text(label or row.get('title')),
            'count': count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None}


def _row(key, title, state, observation, remaining, check_keys, evidence=()):
    records = list(islice(evidence, MAX_EVIDENCE + 1))
    if len(records) > MAX_EVIDENCE:
        observation += ' Показаны первые 20 свидетельств; перечень сокращён.'
    return {'key': key, 'title': title, 'state': state, 'observation': observation,
            'remaining': remaining, 'check_keys': list(check_keys), 'evidence': records[:MAX_EVIDENCE],
            'confirmed': False}


def _date(row):
    return _text(row.get('received_at')) or 'дата получения не установлена'


def _layer(label, layer, applied=True):
    if not layer or not layer.get('received_at') and layer.get('state') == 'not_available':
        return label + ': датированный ответ не получен.'
    if not applied:
        return label + ': источник другой области или неподдержанной версии не применён; сохранённая дата ' + _date(layer) + '.'
    if layer.get('state') in ('error', 'not_requested', 'not_available'):
        return label + ': ответ не получен' + ('; ' + _text(layer['error']) if layer.get('error') else '') + '.'
    if not layer.get('received_at'):
        return label + ': датированный ответ не получен.'
    text = label + ': получено ' + (_text(layer.get('count')) or 'число не установлено') + ' объектов; ' + _date(layer) + '.'
    if layer.get('count') == 0:
        text += ' Пустой ответ не подтверждает отсутствие объектов.'
    if layer.get('state') == 'retained':
        text += ' Использовано прежнее наблюдение; обновление не удалось.'
    return text


def assessment(candidate, result):
    sources = result.get('sources') or {}
    parameters = result.get('parameters') or {}
    nspd = sources.get('nspd') or {}
    context = sources.get('context') or {}
    context_applied = context.get('applied') is True
    context_layers = context.get('layers') or {}
    context_matches = candidate.get('context_matches') or {}
    matches = candidate.get('matches') or {}
    rows = []
    parcels, buildings = nspd.get('parcels') or {}, nspd.get('buildings') or {}
    quarter_rows = _rows(context_matches.get('quarters')) if context_applied else []
    quarters_unknown = [row for row in quarter_rows if row.get('parcels_without_geometry')]
    cadastre = _layer('Кадастровые объекты', parcels) + ' ' + _layer('Здания', buildings)
    if quarters_unknown:
        cadastre += ' В пересекающих контур кварталах есть участки без геометрии; счётчики относятся ко всему кварталу, их положение неизвестно.'
    cadastre += ' Вычитание полученных участков и зданий создаёт поисковые промежутки; полнота ЕГРН не установлена.'
    category = (candidate.get('source_fields') or {}).get('category')
    if category is not None:
        cadastre += ' Поле категории опубликованного объекта: ' + _text(category) + '; это не результат правовой проверки.'
    rows.append(_row('cadastre', 'Кадастр и здания', 'attention' if quarters_unknown else 'observed' if parcels.get('received_at') and parcels.get('count', 0) else 'unknown',
                     cadastre, 'Проверить полноту кадастровых сведений и категорию земли именно этого контура; поля соседей его статус не устанавливают.',
                     ('category',), chain((_evidence(parcels, label='Кадастр НСПД'), _evidence(buildings, label='Здания НСПД')), map(_evidence, quarter_rows))))

    settlements = _rows(context_matches.get('settlements')) if context_applied else []
    historical = candidate.get('boundary_observation') or {}
    settlement_layer = context_layers.get('settlements') or {}
    boundary = _layer('Населённые пункты НСПД', settlement_layer, context_applied)
    if settlements:
        boundary += ' Пересечений с контуром: ' + str(len(settlements)) + '; целиком внутри одного полученного полигона: ' + ('да' if any(row.get('spatially_inside') for row in settlements) else 'не установлено') + '.'
    if historical:
        labels = {'inside': 'внутри', 'outside': 'вне', 'crosses': 'пересекает', 'varies': 'зависит от преобразования'}
        boundary += ' Гипотеза исторической границы: ' + labels.get(historical.get('relation'), 'положение не установлено') + '; действующая граница и система координат не подтверждены.'
    boundary_attention = (bool(historical) and historical.get('relation') in ('outside', 'crosses', 'varies')) or (context_applied and settlement_layer.get('state') in ('retained', 'error', 'not_requested')) or (bool(settlements) and not any(row.get('spatially_inside') for row in settlements))
    rows.append(_row('boundary', 'Граница населённого пункта', 'attention' if boundary_attention else 'observed' if settlements or historical else 'unknown',
                     boundary, 'Получить действующую официальную границу с подтверждённой системой координат и проверить весь контур.', ('boundary',),
                     chain((_evidence(settlement_layer, None if context_applied else 'not_applied', label='Населённые пункты НСПД'),), map(_evidence, settlements),
                           (_evidence(historical.get('source') or {}, 'historical_hypothesis'),) if historical else ())))

    gp_source = sources.get('rgis_context') or {}
    gp_matches = candidate.get('general_plan_matches') or {}
    gp = list(chain.from_iterable(_rows(gp_matches.get(mode)) for mode in ('functional', 'settlements', 'roads'))) if gp_source.get('applied') is True else []
    pzz_source = sources.get('pzz_context') or {}
    pzz_layer = pzz_source.get('layer') or {}
    regional = _rows(candidate.get('regional_pzz_matches')) if pzz_source.get('applied') is True else []
    nspd_pzz = nspd.get('pzz') or {}
    pzz_matches = _rows(matches.get('pzz'))
    regulations = sources.get('regulations') or {}
    counts = regulations.get('counts') or {}
    zoning = _layer('Территориальные зоны НСПД', nspd_pzz) + ' ' + _layer('Региональные ПЗЗ РГИС', pzz_layer, pzz_source.get('applied') is True)
    zoning += ' ' + _layer('Функциональные зоны генплана РГИС', (gp_source.get('layers') or {}).get('functional') or {}, gp_source.get('applied') is True)
    zoning += ' Пересечений контура: НСПД ' + str(len(pzz_matches)) + ', региональные ПЗЗ ' + str(len(regional)) + '; генплан РГИС ' + str(len(gp)) + '.'
    if gp_source.get('applied') is not True:
        zoning += ' Слои генплана не применены к этой области или не получены.'
    zoning += ' Генплан не заменяет ПЗЗ. '
    if regulations.get('id'):
        zoning += 'В сохранённом индексе ПЗЗ прочитано ' + _text(counts.get('processed_pages')) + '/' + _text(counts.get('total_pages')) + ' страниц; это документы поселения, не подтверждение зоны контура.'
        if not regulations.get('catalog_matches'):
            zoning += ' Индекс относится к прежнему каталогу.'
    else:
        zoning += 'Индекс текста ПЗЗ не подключён.'
    zoning_attention = bool(regulations.get('id') and not regulations.get('catalog_matches'))
    rows.append(_row('zoning', 'Генплан, ПЗЗ и использование', 'attention' if zoning_attention else 'observed' if gp or regional or pzz_matches or counts.get('processed_pages') else 'unknown',
                     zoning, 'Установить действующую зону и подзону, применимые ВРИ, размеры и параметры для выбранной цели; подтвердить акты и изменения.', ('general_plan', 'pzz'),
                     chain((_evidence(nspd_pzz, label='ПЗЗ НСПД'), _evidence(pzz_layer, None if pzz_source.get('applied') is True else 'not_applied', label='ПЗЗ РГИС')),
                           map(_evidence, chain(pzz_matches, regional, gp)),
                           ({'source': '', 'received_at': '', 'sha256': '', 'id': _text(regulations.get('id')), 'state': 'text_index', 'count': counts.get('processed_pages')},) if regulations.get('id') else ())))

    restriction_matches = _rows(matches.get('restrictions'))
    context_restrictions = [row for mode in RESTRICTION_MODES for row in _rows(context_matches.get(mode))] if context_applied else []
    relevant_layers = [(mode, context_layers.get(mode) or {}) for mode in RESTRICTION_MODES]
    errors = [mode for mode, layer in relevant_layers if context_applied and layer.get('state') in ('retained', 'error', 'not_requested')]
    restrictions = 'Пересечения контура: ЗОУИТ ' + str(len(restriction_matches)) + '; красные линии, природные территории, схемы и межевание ' + str(len(context_restrictions)) + '.'
    restrictions += ' ' + _layer('ЗОУИТ НСПД', nspd.get('restrictions') or {})
    if errors:
        restrictions += ' Часть источников не обновлена или не запрошена после ошибки; прежние наблюдения сохраняют свои даты.'
    if not context_applied:
        restrictions += ' Контекст схем, красных линий и природных территорий этой области не применён или не получен.'
    restrictions += ' Геометрические исключения при подборе: ЗОУИТ ' + ('включены' if parameters.get('avoid_restrictions', True) else 'отключены') + ', схемы/межевание ' + ('включены' if parameters.get('avoid_planned', True) else 'отключены') + ', природные территории ' + ('включены' if parameters.get('avoid_environment', True) else 'отключены') + '; это фильтры пробных контуров, не правовые выводы.'
    has_restriction_response = bool((nspd.get('restrictions') or {}).get('received_at')) or (context_applied and any(layer.get('received_at') for _, layer in relevant_layers))
    rows.append(_row('restrictions', 'Ограничения и планировка', 'attention' if restriction_matches or context_restrictions or errors else 'observed' if has_restriction_response else 'unknown',
                     restrictions, 'Проверить документы, действующие режимы пересечений, красные линии, сети и ранее согласованные схемы. Нулевые совпадения не доказывают отсутствие ограничений.', ('red_lines', 'restrictions', 'planning'),
                     chain((_evidence(nspd.get('restrictions') or {}, label='ЗОУИТ НСПД'),),
                           (_evidence(layer, None if context_applied else 'not_applied', label=layer.get('title') or LAYER_TITLES[mode]) for mode, layer in relevant_layers), map(_evidence, chain(restriction_matches, context_restrictions)))))

    access = candidate.get('access_evidence') or {}
    direct = access.get('direct_segment') or {}
    intersections = access.get('intersections') or {}
    access_count = sum(len(_rows(intersections.get(mode))) for mode in ('parcels', 'buildings'))
    access_text = 'Контур дорожного назначения не получен; законный доступ не установлен.'
    if access.get('road'):
        access_text = 'До полученного контура дорожного назначения ' + _text((access.get('road') or {}).get('distance_m')) + ' м.'
    if direct:
        access_text += ' Прямой отрезок ' + _text(direct.get('length_m')) + ' м; пересечений участков/зданий ' + str(access_count) + '. Это не маршрут и не полоса проезда.'
        if direct.get('within_survey_bounds') is False:
            access_text += ' Отрезок выходит за область обследования.'
    if access.get('state') == 'zero_distance':
        access_text += ' Нулевое расстояние не подтверждает право или возможность проезда.'
    rows.append(_row('access', 'Подъезд и фактическое использование', 'attention' if access_count or direct.get('within_survey_bounds') is False else 'observed' if access.get('road') else 'unknown',
                     access_text, 'Подтвердить законный подъезд и проверить фактический проезд и использование на местности или актуальном снимке.', ('access', 'visual'),
                     map(_evidence, chain((access[key] for key in ('nearest_parcel', 'nearest_building', 'road') if isinstance(access.get(key), dict)),
                                          *(_rows(intersections.get(mode)) for mode in ('parcels', 'buildings'))))))

    lots = _rows(candidate.get('lots'))
    regional_source = sources.get('torgi_active') or {}
    debt_count = sum(row.get('land_group') == 'debt_sale' for row in lots)
    publications = 'Связанных с контуром опубликованных процедур: ' + str(len(lots)) + '.'
    if debt_count:
        publications += ' Из них реализация имущества должников: ' + str(debt_count) + '; это не предоставление свободной земли.'
    if lots:
        publications += ' Статусы, сроки и признак действующей процедуры относятся к сохранённому наблюдению; текущий статус требует обновления.'
    else:
        publications += ' Отсутствие совпадения не доказывает отсутствие заявлений или торгов.'
    publications += ' Региональных кадастровых номеров без геометрии: ' + str(len(_rows(regional_source.get('unlocated_numbers')))) + '; лотов без номера: ' + _text(regional_source.get('lots_without_number')) + '. Полнота поиска по контуру не подтверждена.'
    rows.append(_row('publications', 'Публикации и процедуры', 'attention' if lots else 'unknown', publications,
                     'Сверить текущие статусы, сроки, вид процедуры, схему и документы именно связанных с контуром лотов; проверить непозиционированные публикации.', ('procedure',),
                     chain((_evidence(row, 'active_at_observation' if row.get('active_observed') else 'status_requires_check') for row in lots),
                           (_evidence(regional_source, 'regional_search_not_complete'),) if regional_source.get('id') else ())))

    land_status = candidate.get('land_status') or {}
    procedures = (sources.get('land_status') or {}).get('documents') or []
    rights_text = 'Принадлежность земли, права третьих лиц, прежние заявления и уполномоченный орган не установлены.'
    rights_text += ' Публичных документов, связанных по упоминаниям: ' + _text(land_status.get('related_public_documents', 0)) + '; регламентов в сохранённом перечне: ' + str(len(procedures)) + '. Их действие и применимость к контуру не подтверждены; поля соседей не переносятся на этот контур.'
    rows.append(_row('rights', 'Права, орган и основания предоставления', 'unknown', rights_text,
                     'Получить сведения о принадлежности, правах и прежних заявлениях, подтвердить компетентный орган, действующий регламент и основания отказа для заявителя.', ('rights', 'authority', 'refusal'),
                     map(_evidence, procedures)))

    layout = result.get('layout') or {}
    kind = {'draft': 'пробный контур', 'gap': 'промежуток для проектирования', 'offer': 'предложение НСПД', 'auction': 'объект торгов/слоя аукционов'}.get(candidate.get('kind'), 'тип контура не установлен')
    contour = 'Тип: ' + kind + '; расчётная площадь ' + _text(candidate.get('area_m2')) + ' м². Подбор ограничен: ' + _text(layout.get('layout_checks')) + ' проверок; полнота конфигураций не установлена.'
    area, maximum = candidate.get('area_m2'), parameters.get('max_area')
    needs_design = candidate.get('kind') == 'gap' or layout.get('layout_limit_reached') is True or (isinstance(area, (int, float)) and isinstance(maximum, (int, float)) and area > maximum)
    if needs_design:
        contour += ' Требуется дополнительное проектирование контура.'
    rows.append(_row('contour', 'Конфигурация и подготовка контура', 'attention' if needs_design else 'observed' if candidate.get('geometry') else 'unknown',
                     contour, 'Уточнить окончательный контур, размеры, систему координат и расчёты; подготовить надлежащую СРЗУ при необходимости. Поисковая геометрия не является СРЗУ.', ('contour',)))

    actions = []
    if parameters.get('purpose', 'unspecified') == 'unspecified':
        actions.append('Выбрать цель использования для проверки применимых требований ПЗЗ.')
    if quarters_unknown:
        actions.append('Проверить кадастровые сведения об участках без геометрии в пересекающих контур кварталах; их положение неизвестно.')
    if rows[3]['state'] == 'attention':
        actions.append('Проверить документы по пересечениям и не обновлённым источникам ограничений и планировки.')
    if rows[4]['state'] == 'attention':
        actions.append('Проверить пересечения прямого направления к дороге и найти законный вариант подъезда.')
    if lots:
        actions.append('Сверить текущий статус, сроки и схему связанных с контуром опубликованных процедур.')
    # Reserve room for the two unresolved legal prerequisites even when several
    # observations need inspection; no numerical readiness score is computed.
    actions = actions[:2] + ['Получить действующую зону ПЗЗ и применимые регламенты для выбранной цели.',
                            'Установить принадлежность земли, права третьих лиц и компетентный орган.']
    if len(actions) < 4:
        actions.append('Подтвердить законный подъезд и проверить фактическое использование территории.')
    return {'algorithm': ALGORITHM, 'rows': rows, 'next_actions': actions,
            'attention_keys': [row['key'] for row in rows if row['state'] == 'attention'],
            'coverage_confirmed': False, 'rights_confirmed': False, 'ready_to_submit': False}
