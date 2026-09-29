from datetime import date

CHECKS = {
    'boundary': 'Официальная граница населённого пункта',
    'rights': 'Права третьих лиц и публичная принадлежность земли',
    'category': 'Категория земель',
    'general_plan': 'Функциональная зона по генплану',
    'pzz': 'ПЗЗ, допустимый ВРИ и предельные размеры',
    'red_lines': 'Красные линии и территории общего пользования',
    'restrictions': 'ЗОУИТ, вода, лес, ОКН и инженерные сети',
    'access': 'Законный подъезд и доступ',
    'visual': 'Фактическое использование и спутниковая проверка',
    'planning': 'Проекты планировки и межевания территории',
    'refusal': 'Основания отказа по статьям 39.15 и 39.16',
    'procedure': 'Применимая процедура и статус заявителя',
    'authority': 'Уполномоченный орган и актуальный регламент',
    'contour': 'Окончательный контур и расчёты',
}
PACKAGE_CHECKS = {
    'srzu': 'СРЗУ по актуальной форме или основание, почему она не требуется',
    'application': 'Заполненное заявление и обязательные приложения',
    'channel': 'Проверенный канал подачи и финальная правовая проверка',
}
WORKFLOWS = {'review': 'Проверка', 'working': 'В разработке', 'ready': 'ПОДАЧА',
             'submitted': 'ОЖИДАНИЕ', 'auction': 'АУКЦИОН', 'direct': 'БЕЗ ТОРГОВ', 'refused': 'ОТКАЗ'}


def passed(check):
    try:
        age = (date.today() - date.fromisoformat(check.get('date', ''))).days
        return check.get('result') == 'pass' and bool(check.get('source', '').strip()) and bool(check.get('note', '').strip()) and 0 <= age <= 30
    except (ValueError, TypeError, AttributeError):
        return False


def status(c):
    checks = c.get('checks') or {}
    if any(v.get('result') == 'fail' for v in checks.values()):
        return 'red'
    if c.get('stale') or not c.get('active'):
        return 'yellow'
    if all(passed(checks.get(k, {})) for k in CHECKS):
        return 'green'
    return 'yellow'


def blockers(c, package=False):
    needed = dict(CHECKS, **PACKAGE_CHECKS) if package else CHECKS
    result = [label for k, label in needed.items() if not passed((c.get('checks') or {}).get(k, {}))]
    if c.get('stale'):
        result.insert(0, 'Повторить поиск: изменились исходные слои')
    if not c.get('active'):
        result.insert(0, 'Кандидат отсутствует в последнем расчёте')
    return result


def validate_update(c, data):
    changes = {}
    for key in ['notes', 'reference', 'deadline']:
        if key in data:
            changes[key] = str(data[key])[:10000]
    if changes.get('deadline'):
        date.fromisoformat(changes['deadline'])
    if 'checks' in data:
        checks = data['checks']
        if not isinstance(checks, dict):
            raise ValueError('Некорректные проверки')
        for key, check in checks.items():
            if key not in {**CHECKS, **PACKAGE_CHECKS} or not isinstance(check, dict):
                raise ValueError('Неизвестная проверка')
            if check.get('result') not in ['unknown', 'pass', 'fail']:
                raise ValueError('Неизвестный результат проверки')
            if check['result'] != 'unknown':
                if not check.get('source', '').strip() or not check.get('note', '').strip():
                    raise ValueError('Для вывода нужны источник и обоснование')
                d = date.fromisoformat(check.get('date', ''))
                if d > date.today():
                    raise ValueError('Дата проверки не может быть в будущем')
        changes['checks'] = checks
    if 'workflow' in data:
        if data['workflow'] not in WORKFLOWS:
            raise ValueError('Неизвестная стадия')
        proposed = dict(c, **changes)
        if data['workflow'] == 'ready' and blockers(proposed, package=True):
            raise ValueError('Пакет не готов: ' + '; '.join(blockers(proposed, package=True)))
        if data['workflow'] in ['submitted', 'auction', 'direct', 'refused'] and not proposed.get('reference', '').strip():
            raise ValueError('Укажите номер и источник заявления, извещения или решения')
        changes['workflow'] = data['workflow']
    return changes


def present(c):
    result = dict(c, status=status(c), blockers=blockers(c), package_blockers=blockers(c, True))
    result['effective_workflow'] = c['workflow']
    if c['workflow'] == 'ready' and result['package_blockers']:
        result['effective_workflow'] = 'review'
    return result
