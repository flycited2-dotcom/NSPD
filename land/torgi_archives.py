"""Explicit, source-bound ZIP observations separate from primary file/OCR state."""
import copy
from datetime import datetime
import hashlib
from html import escape
import json
from pathlib import Path
import re
import subprocess
import sys
from . import archive_evidence, store, torgi, torgi_docs
from .network import fetch
from .torgi_archive_worker import ALGORITHM, MAX_OUTPUT, MAX_TEXT, algorithm as member_algorithm


ROOT = Path(__file__).resolve().parent.parent
WARNING = ('Архив прочитан в пределах 64 записей, 16 МиБ на запись, 64 МиБ суммарно и 40 страниц PDF. '
           'Неподдержанные записи и пропуски остаются видимыми. OCR, подписи, действительность условий, '
           'геометрия и принадлежность упомянутых объектов предмету лота не подтверждены.')
HEX = re.compile(r'[0-9a-f]{64}')
MAX_SNAPSHOT = 8 * 1024 * 1024


def setting_key(project, active=True, suffix=''):
    return torgi.setting_key(project, active, '_archives' + suffix)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _cached_bytes(path, limit):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Сохранённое доказательство отсутствует или превышает лимит')
    with path.open('rb') as cached:
        raw = cached.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Сохранённое доказательство превышает лимит')
    return raw


def _snapshot(folder, record):
    """Keep prior successful manifests immutable when a later retry fails."""
    body = {key: value for key, value in record.items() if key != 'evidence_sha256'}
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_SNAPSHOT:
        raise ValueError('Снимок архивного доказательства превышает 8 МиБ')
    digest = hashlib.sha256(raw).hexdigest()
    destination = Path(folder) / 'observations'
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / (digest + '.json')
    if path.exists():
        if hashlib.sha256(_cached_bytes(path, MAX_SNAPSHOT)).hexdigest() != digest:
            raise ValueError('Неизменяемый снимок архивного доказательства повреждён')
    else:
        store.atomic_write(path, raw)
    record['evidence_sha256'] = digest
    return {'binding_id': record['binding_id'], 'observation_id': record['observation_id'],
            'archive_sha256': record['archive_sha256'], 'file_key': record['file_key'],
            'source_receipt_sha256': _hash(record['source_receipt']), 'checked_at': record['checked_at']}


def _assert_source(path, digest, size):
    raw = _cached_bytes(path, archive_evidence.MAX_BYTES)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Исходный ZIP изменился после чтения; результат не записан')


def _receipt_digest(receipt, binding):
    if not isinstance(receipt, dict):
        raise ValueError('Отсутствует квитанция сохранённого ZIP')
    digest = receipt.get('sha256')
    if (not isinstance(digest, str) or not HEX.fullmatch(digest)
            or receipt.get('source') != torgi_docs.FILE + binding['parent_file']['file_id']
            or receipt.get('http_status') != 200 or not _date(receipt.get('received_at'))
            or receipt.get('actual_size') != binding['parent_file']['declared_size']):
        raise ValueError('Некорректная квитанция или SHA-256 сохранённого ZIP')
    return digest


def _date(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def source_binding(file, search, catalog, active=True):
    """Use exact accepted card associations and receipts, never a filename guess."""
    if not torgi_docs.same_search(search, catalog):
        raise ValueError('Каталог не относится к текущему поиску')
    cards = [card for card in catalog.get('cards', []) if card.get('state') == 'received']
    fresh = torgi_docs.files_for(cards, {})
    observed = fresh.get(file.get('key'))
    fields = ('key', 'file_id', 'file_name', 'declared_size', 'source_hash', 'format', 'url')
    if (observed is None or observed.get('format') != 'zip' or not observed.get('eligible')
            or observed.get('metadata_conflict')
            or any(file.get(key) != observed.get(key) for key in fields)
            or file.get('associations') != observed.get('associations')):
        raise ValueError('ZIP или точные связи вложения отсутствуют в принятых карточках')
    if not 0 < observed['declared_size'] <= archive_evidence.MAX_BYTES:
        raise ValueError('ZIP превышает лимит 32 МиБ')
    receipts = []
    for card in cards:
        attachments = [row for row in card.get('attachments', []) if row.get('key') == file['key']]
        if not attachments:
            continue
        lots = [lot for lot in search.get('lots', []) if lot.get('id') == card.get('lot_id')
                and lot.get('notice_number') == card.get('notice_number')
                and (not active or (lot.get('type') or {}).get('code') == 'ZK')]
        if (len(lots) != 1 or card.get('source') != torgi_docs.CARD + card['lot_id']
                or card.get('http_status') != 200 or not _date(card.get('received_at'))
                or not isinstance(card.get('sha256'), str) or not HEX.fullmatch(card['sha256'])):
            raise ValueError('Нет подтверждённой квитанции текущей публичной карточки ZIP')
        normalized = []
        for row in attachments:
            if row.get('scope') not in ('lot', 'notice'):
                raise ValueError('Неизвестная область связи вложения')
            checked = torgi_docs.descriptor({'fileId': row.get('file_id'), 'fileName': row.get('file_name'),
                                             'fileSize': row.get('declared_size'), 'hash': row.get('source_hash'),
                                             'inactive': row.get('inactive')}, row['scope'])
            if any(checked.get(key) != row.get(key) for key in fields):
                raise ValueError('Описание ZIP изменилось относительно карточки')
            normalized.append({key: row.get(key) for key in (*fields, 'scope', 'inactive')})
        receipts.append({'lot_id': card['lot_id'], 'notice_number': card['notice_number'],
                         'source': card['source'], 'received_at': card['received_at'],
                         'sha256': card['sha256'], 'http_status': card['http_status'],
                         'attachments': normalized})
    if not receipts or len({row['lot_id'] for row in receipts}) != len(receipts):
        raise ValueError('Отсутствует или повторяется квитанция карточки ZIP')
    identity = {'search_source_id': torgi.source_id(search),
                'parent_file': {key: file.get(key) for key in fields},
                'associations': copy.deepcopy(observed['associations']), 'card_receipts': receipts}
    return {'binding_id': _hash(identity), **identity}


def _sources(search, catalog, active=True):
    output = []
    for file in catalog.get('files', {}).values():
        if file.get('format') != 'zip':
            continue
        item = {'file_key': file.get('key'), 'file_id': file.get('file_id'),
                'declared_size': file.get('declared_size'), 'file_name': file.get('file_name'),
                'associations': copy.deepcopy(file.get('associations', []))}
        try:
            binding = source_binding(file, search, catalog, active)
            item.update(binding_id=binding['binding_id'], source_binding=binding, eligible=True)
        except (ValueError, KeyError, TypeError) as exc:
            item.update(eligible=False, state='blocked', error=str(exc)[:500])
        output.append(item)
    return output


def queue(search, catalog, records, retry_errors=False, active=True, lot_id=None):
    output = []
    for item in _sources(search, catalog, active):
        if not item['eligible']:
            continue
        if lot_id is not None and not any(row.get('lot_id') == lot_id and not row.get('inactive')
                                          for row in item['associations']):
            continue
        previous = records.get(item['binding_id'])
        if previous is None or (retry_errors and previous.get('state') in ('error', 'partial')
                                and previous.get('retryable') is True):
            output.append(item)
    return output


def _assert_current(project, search, catalog_id, binding, archive_id, active):
    torgi_docs.assert_current(project, search, catalog_id, active)
    current_search = store.get_setting(torgi.setting_key(project, active))
    if current_search != search:
        raise ValueError('Состав текущего поиска изменился; результат не записан')
    current = store.get_setting(torgi_docs.setting_key(project, active))
    file = (current or {}).get('files', {}).get(binding['parent_file']['key'])
    if file is None or source_binding(file, current_search, current, active) != binding:
        raise ValueError('Карточка, квитанция или связи ZIP изменились; результат не записан')
    collection = store.get_setting(setting_key(project, active), {}) or {}
    if collection.get('id') != archive_id:
        raise ValueError('Каталог архивных наблюдений изменился; результат не записан')


def _persist(project, collection, search, catalog_id, binding, previous_id, active):
    with store.LOCK:
        _assert_current(project, search, catalog_id, binding, previous_id, active)
        record = collection['records'][binding['binding_id']]
        if record.get('state') in ('read', 'partial') and record.get('archive_sha256'):
            _assert_source(store.DATA / 'torgi_archives' / (record['archive_sha256'] + '.zip'),
                           record['archive_sha256'], binding['parent_file']['declared_size'])
        collection['updated_at'] = store.now()
        collection['id'] = _hash({key: value for key, value in collection.items() if key != 'id'})[:20]
        with store.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings(key,body) VALUES(?,?)',
                       (setting_key(project, active), json.dumps(collection, ensure_ascii=False)))
            store.event(db, project, 'torgi_archive', {'id': collection['id'],
                        'binding_id': binding['binding_id'], 'file_key': binding['parent_file']['key'],
                        'state': collection['records'][binding['binding_id']]['state']})
    return collection['id']


def extract_archive(path, digest, folder):
    """One isolated process; parsing never executes an attached document."""
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    try:
        result = subprocess.run([sys.executable, '-m', 'land.torgi_archive_worker',
                                 str(Path(path).resolve()), digest, str(Path(folder).resolve())],
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=60, **options)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Чтение ZIP превысило 60 секунд; процесс остановлен') from exc
    if len(result.stdout) > MAX_OUTPUT:
        raise ValueError('Результат чтения ZIP превышает 4 МиБ')
    try:
        output = json.loads(result.stdout.decode('utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Процесс чтения ZIP не вернул допустимый результат') from exc
    if result.returncode or not isinstance(output, dict) or output.get('error'):
        raise ValueError(output.get('error', 'Ошибка чтения ZIP') if isinstance(output, dict) else 'Ошибка чтения ZIP')
    if output.get('algorithm') != ALGORITHM or output.get('archive_sha256') != digest:
        raise ValueError('Чтение относится к другому ZIP или алгоритму')
    return output


def _validate_worker(output, raw, folder):
    manifest = archive_evidence.inspect(raw)
    allowed = {'algorithm', 'archive_sha256', 'manifest', 'entries', 'archive_complete',
               'geometry_confirmed', 'georeferenced', 'ocr_performed', 'pdf_max_pages',
               'member_max_bytes', 'counts', 'all_supported_members_read', 'state'}
    if (not isinstance(output, dict) or output.get('algorithm') != ALGORITHM
            or set(output) != allowed
            or output.get('archive_sha256') != manifest['archive_sha256']
            or output.get('manifest') != manifest or output.get('archive_complete') is not False
            or output.get('geometry_confirmed') is not False or output.get('georeferenced') is not False
            or output.get('ocr_performed') is not False or output.get('pdf_max_pages') != 40
            or output.get('member_max_bytes') != archive_evidence.MAX_MEMBER_BYTES
            or not isinstance(output.get('entries'), list)
            or len(output['entries']) != len(manifest['entries'])):
        raise ValueError('Манифест или область чтения процесса не соответствуют исходному ZIP')
    for original, row in zip(manifest['entries'], output['entries']):
        if (not isinstance(row, dict) or any(row.get(key) != value for key, value in original.items()
                                             if key not in ('state', 'reason'))
                or row.get('geometry_confirmed') is not False or row.get('georeferenced') is not False
                or row.get('ocr_performed') is not False or not isinstance(row.get('reading_complete'), bool)):
            raise ValueError('Наблюдение относится к другой записи ZIP')
        if original['state'] != 'ready':
            if row.get('state') != original['state'] or 'sha256' in row or 'evidence' in row:
                raise ValueError('Неподдержанная запись ZIP ошибочно объявлена прочитанной')
        elif row.get('state') not in ('read', 'error'):
            raise ValueError('Неизвестный результат чтения записи ZIP')
        if row.get('sha256') is not None:
            identifier = archive_evidence.member_identifier(manifest, original)
            identifier['sha256'] = row['sha256']
            _, checked = archive_evidence.read_member(raw, identifier)
            if row.get('actual_size') != checked['actual_size'] or row.get('crc_verified') is not True:
                raise ValueError('Размер или CRC прочитанной записи ZIP не подтверждены')
            cache = Path(folder) / (checked['sha256'] + '.' + original['format'])
            cached_member = _cached_bytes(cache, archive_evidence.MAX_MEMBER_BYTES)
            if (len(cached_member) != checked['actual_size']
                    or hashlib.sha256(cached_member).hexdigest() != checked['sha256']):
                raise ValueError('Сохранённая запись ZIP отсутствует или изменена')
        if row.get('state') == 'read' and (not row.get('sha256') or not isinstance(row.get('evidence'), dict)):
            raise ValueError('Прочитанная запись ZIP не содержит связанного доказательства')
        if row.get('state') == 'read':
            evidence = row['evidence']
            details = evidence.get('details')
            if (evidence.get('algorithm') != member_algorithm(original['format'])
                    or evidence.get('geometry_confirmed') is not False or evidence.get('georeferenced') is not False
                    or not isinstance(details, dict) or details.get('geometry_confirmed') is not False
                    or details.get('georeferenced') is not False
                    or row['reading_complete'] != (details.get('text_layer_complete') is True
                        and not details.get('unread_pages') and not details.get('image_or_sparse_pages'))):
                raise ValueError('Область доказательства записи ZIP не подтверждена')
            if evidence.get('text_cache_sha256') is not None:
                text_sha = evidence['text_cache_sha256']
                text = Path(folder) / (row['sha256'] + '.txt')
                cached_text = _cached_bytes(text, MAX_TEXT)
                if (not isinstance(text_sha, str) or not HEX.fullmatch(text_sha)
                        or len(cached_text) != evidence.get('text_cache_size')
                        or hashlib.sha256(cached_text).hexdigest() != text_sha):
                    raise ValueError('Сохранённый текст записи ZIP отсутствует или изменён')
    rows = output['entries']
    expected = {'entries': len(rows), 'supported': sum(row['state'] == 'ready' for row in manifest['entries']),
                'read': sum(row['state'] == 'read' for row in rows),
                'errors': sum(row['state'] == 'error' for row in rows),
                'unsupported': sum(row['state'] == 'unsupported' for row in rows),
                'directories': sum(row['state'] == 'directory' for row in rows),
                'incomplete': sum(row['state'] == 'read' and row.get('reading_complete') is not True for row in rows)}
    if output.get('counts') != expected:
        raise ValueError('Счётчики чтения ZIP не соответствуют записям')
    expected_state = ('partial' if any(expected[key] for key in ('errors', 'unsupported', 'incomplete'))
                       or not expected['supported'] else 'read')
    if (output.get('state') != expected_state or output.get('all_supported_members_read')
            != (expected['supported'] > 0 and expected['read'] == expected['supported'])):
        raise ValueError('Состояние чтения ZIP не соответствует пропускам')
    return output


def run(project, params, active=True):
    """Explicitly process at most one eligible ZIP; never retry errors implicitly."""
    if not isinstance(params, dict):
        raise ValueError('Требуются параметры запуска чтения ZIP')
    catalog = store.get_setting(torgi_docs.setting_key(project, active))
    search = store.get_setting(torgi.setting_key(project, active))
    if not catalog or params.get('id') != catalog.get('id') or not torgi_docs.same_search(search, catalog):
        raise ValueError('Каталог документов или поиск изменились; обновите страницу')
    retry = params.get('retry_errors', False)
    if not isinstance(retry, bool):
        raise ValueError('Параметр повтора должен быть логическим')
    reprocess = params.get('reprocess_cached', False)
    if not isinstance(reprocess, bool):
        raise ValueError('Параметр чтения кэша должен быть логическим')
    if reprocess and params.get('file_key') is None:
        raise ValueError('Для чтения кэша требуется явный file_key')
    lot_id = torgi_docs.selected_lot(params, search, catalog, active)
    collection = copy.deepcopy(store.get_setting(setting_key(project, active), {}) or {})
    previous_id = collection.get('id')
    records = collection.setdefault('records', {})
    selected = queue(search, catalog, records, retry, active, lot_id)
    key = params.get('file_key')
    if key is not None:
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{24}', key):
            raise ValueError('Некорректный ключ ZIP')
        sources = [item for item in _sources(search, catalog, active) if item['file_key'] == key]
        if len(sources) != 1 or not sources[0]['eligible']:
            raise ValueError('Выбранный ZIP отсутствует или заблокирован в текущем каталоге')
        selected = [item for item in selected if item['file_key'] == key]
        if reprocess:
            item = sources[0]
            binding = item['source_binding']
            if lot_id is not None and not any(a.get('lot_id') == lot_id and not a.get('inactive')
                                              for a in item['associations']):
                raise ValueError('ZIP не связан с выбранным лотом')
            prior = records.get(binding['binding_id'])
            if (not isinstance(prior, dict) or prior.get('binding_id') != binding['binding_id']
                    or prior.get('source_binding') != binding):
                raise ValueError('Для текущей привязки отсутствует сохранённое наблюдение ZIP')
            digest = _receipt_digest(prior.get('source_receipt'), binding)
            if prior.get('archive_sha256') not in (None, digest):
                raise ValueError('Квитанция относится к другому сохранённому ZIP')
            _assert_current(project, search, catalog['id'], binding, previous_id, active)
            _assert_source(store.DATA / 'torgi_archives' / (digest + '.zip'), digest,
                           binding['parent_file']['declared_size'])
            selected = [item]
    selected = selected[:1]
    attempt = {'state': 'running', 'started_at': store.now(), 'requested': len(selected),
               'processed': 0, 'network_requests': 0, 'cached_files': 0,
               'counts': {'read': 0, 'errors': 0, 'unsupported': 0, 'incomplete': 0}}
    if reprocess:
        attempt['reprocess_cached'] = True
    if lot_id is not None:
        attempt['lot_id'] = lot_id
    store.set_setting(setting_key(project, active, '_attempt'), attempt)
    folder = store.DATA / 'torgi_archives'
    try:
        for item in selected:
            binding = item['source_binding']
            prior = records.get(binding['binding_id'], {})
            record = {'binding_id': binding['binding_id'], 'source_binding': binding,
                      'file_key': item['file_key'], 'checked_at': store.now(),
                      'state': 'error', 'retryable': True, 'geometry_confirmed': False,
                      'georeferenced': False, 'archive_complete': False, 'ocr_performed': False}
            previous_evidence = prior.get('evidence_sha256') or prior.get('previous_evidence_sha256')
            if previous_evidence:
                record['previous_evidence_sha256'] = previous_evidence
            if prior.get('source_receipt'):
                record['source_receipt'] = copy.deepcopy(prior['source_receipt'])
            attempt['file_key'] = item['file_key']
            _assert_current(project, search, catalog['id'], binding, previous_id, active)
            folder.mkdir(parents=True, exist_ok=True)
            phase = 'download'
            try:
                receipt = prior.get('source_receipt')
                if receipt:
                    phase = 'cached_source'
                    digest = _receipt_digest(receipt, binding)
                    path = folder / (digest + '.zip')
                    raw = _cached_bytes(path, archive_evidence.MAX_BYTES)
                    if hashlib.sha256(raw).hexdigest() != digest:
                        raise ValueError('Сохранённый ZIP изменился')
                    record['source_receipt'] = copy.deepcopy(receipt)
                    attempt['cached_files'] += 1
                else:
                    if reprocess:
                        raise ValueError('В режиме чтения кэша скачивание ZIP запрещено')
                    attempt['network_requests'] += 1
                    raw, content_type, code = fetch(torgi_docs.FILE + item['file_id'],
                                                    max_bytes=archive_evidence.MAX_BYTES)
                    if code != 200:
                        raise ValueError('Источник ZIP не подтвердил HTTP 200')
                    digest = hashlib.sha256(raw).hexdigest()
                    path = folder / (digest + '.zip')
                    record['source_receipt'] = {'source': torgi_docs.FILE + item['file_id'],
                            'received_at': store.now(), 'sha256': digest, 'http_status': code,
                            'content_type': str(content_type)[:500], 'actual_size': len(raw)}
                _assert_current(project, search, catalog['id'], binding, previous_id, active)
                phase = 'validate_source'
                if (len(raw) != binding['parent_file']['declared_size']
                        or len(raw) > archive_evidence.MAX_BYTES):
                    raise ValueError('Размер ZIP отличается от карточки или превышает 32 МиБ')
                # Structural failures reject the entire ZIP before any member is read.
                archive_evidence.inspect(raw)
                if not receipt:
                    store.atomic_write(path, raw)
                phase = 'worker'
                output = _validate_worker(extract_archive(path, digest, folder / 'members'), raw, folder / 'members')
                _assert_source(path, digest, binding['parent_file']['declared_size'])
                _assert_current(project, search, catalog['id'], binding, previous_id, active)
                receipt = record['source_receipt']
                observation_id = _hash({'binding_id': binding['binding_id'], 'source_receipt': receipt,
                                        'algorithm': ALGORITHM})
                for row in output['entries']:
                    row['association_reference'] = {'binding_id': binding['binding_id'],
                            'observation_id': observation_id, 'parent_file_key': item['file_key'],
                            'archive_sha256': digest, 'source_receipt_sha256': _hash(receipt),
                            'entry_id': row['id'], 'member_sha256': row.get('sha256')}
                record.update(output, observation_id=observation_id,
                              retryable=output['counts']['errors'] > 0)
                record.pop('error', None)
                snapshot = _snapshot(folder, record)
                collection.setdefault('observations', {})[record['evidence_sha256']] = snapshot
                for count in attempt['counts']:
                    attempt['counts'][count] += output['counts'][count]
                if output['state'] == 'partial':
                    attempt['state'] = 'partial'
            except Exception as exc:
                _assert_current(project, search, catalog['id'], binding, previous_id, active)
                record.update(state='error' if phase in ('download', 'worker') else 'rejected',
                              error=str(exc)[:500], retryable=phase in ('download', 'worker'))
                attempt['counts']['errors'] += 1
                attempt.update(state='partial', error=str(exc)[:500])
            records[binding['binding_id']] = record
            collection.update(algorithm=ALGORITHM, warning=WARNING, geometry_confirmed=False,
                              georeferenced=False, archive_complete=False)
            previous_id = _persist(project, collection, search, catalog['id'], binding, previous_id, active)
            attempt['processed'] += 1
        if attempt['state'] == 'running':
            attempt['state'] = 'done'
        attempt.update(finished_at=store.now(),
                       remaining=len(queue(search, catalog, records, False, active, lot_id)))
        return {key: value for key, value in attempt.items() if key not in ('started_at', 'finished_at')}
    except Exception as exc:
        attempt['counts']['errors'] = max(1, attempt['counts']['errors'])
        attempt.update(state='error', error=str(exc)[:500], finished_at=store.now())
        raise
    finally:
        store.set_setting(setting_key(project, active, '_attempt'), attempt)


def summary(project, active=True, lot_id=None):
    search = store.get_setting(torgi.setting_key(project, active))
    catalog = store.get_setting(torgi_docs.setting_key(project, active), {}) or {}
    collection = store.get_setting(setting_key(project, active), {}) or {}
    current = torgi_docs.same_search(search, catalog)
    sources = _sources(search, catalog, active) if current else []
    records = collection.get('records', {})
    current_ids = {item.get('binding_id') for item in sources if item.get('eligible')}
    if lot_id is not None:
        sources=[item for item in sources if any(a.get('lot_id')==lot_id and not a.get('inactive') for a in item['associations'])]
    for item in sources:
        record = records.get(item.get('binding_id'))
        if record:
            item['observation'] = record
            item['state'] = record['state']
        elif item['eligible']:
            item['state'] = 'pending'
    return {'id': collection.get('id'), 'catalog_id': catalog.get('id'), 'current': current,
            'algorithm': ALGORITHM, 'warning': WARNING, 'archives': sources,
            'archive_count': len(sources), 'pending': len(queue(search, catalog, records, active=active,lot_id=lot_id)) if current else 0,
            'remaining': len(queue(search, catalog, records, active=active,lot_id=lot_id)) if current else 0,
            'retry_remaining': len(queue(search, catalog, records, True,active,lot_id)) if current else 0,
            'blocked_count': sum(not item['eligible'] for item in sources),
            'retryable_count': sum(item.get('observation', {}).get('retryable') is True for item in sources),
            'retained_count': sum(key not in current_ids for key in records),
            'attempt': store.get_setting(setting_key(project, active, '_attempt')),
            'archive_complete': False, 'geometry_confirmed': False, 'georeferenced': False,
            'ocr_performed': False}


def html_report(project, active=True, lot_id=None):
    result = summary(project, active,lot_id)
    rows = []
    for archive in result['archives']:
        record = archive.get('observation', {})
        counts = record.get('counts', {})
        rows.append('<tr><td>' + escape(str(archive.get('file_key') or '')) + '</td><td>'
                    + escape(archive['state']) + '</td><td>' + escape(str(counts.get('read', 0)))
                    + '</td><td>' + escape(str(counts.get('unsupported', 0))) + '</td><td>'
                    + escape(str(counts.get('errors', 0))) + '</td><td>'
                    + escape(str(counts.get('incomplete', 0))) + '</td></tr>')
        for entry in record.get('entries', []):
            rows.append('<tr><td colspan="2">' + escape(entry['name']) + '</td><td colspan="4">'
                        + escape(entry['state'] + ': ' + (entry.get('error') or entry.get('reason') or '')) + '</td></tr>')
    return ('<!doctype html><meta charset="utf-8"><title>ZIP: наблюдения вложений</title>'
            '<h1>ZIP: наблюдения вложений</h1><p>' + escape(WARNING) + '</p>'
            '<table><thead><tr><th>Ключ</th><th>Состояние</th><th>Прочитано</th><th>Не поддержано</th>'
            '<th>Ошибки</th><th>Частичное чтение</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')


def cached_observation(project, evidence_sha256, active=True):
    """Read a registered immutable observation only; no network or extraction."""
    if not isinstance(evidence_sha256, str) or not HEX.fullmatch(evidence_sha256):
        raise ValueError('Некорректный SHA-256 архивного доказательства')
    collection = store.get_setting(setting_key(project, active), {}) or {}
    reference = collection.get('observations', {}).get(evidence_sha256)
    if not reference:
        raise ValueError('Архивное доказательство отсутствует в каталоге проекта')
    path = store.DATA / 'torgi_archives' / 'observations' / (evidence_sha256 + '.json')
    raw = _cached_bytes(path, MAX_SNAPSHOT)
    if hashlib.sha256(raw).hexdigest() != evidence_sha256:
        raise ValueError('Снимок архивного доказательства изменился')
    record = json.loads(raw)
    if (record.get('binding_id') != reference.get('binding_id')
            or record.get('observation_id') != reference.get('observation_id')
            or record.get('archive_sha256') != reference.get('archive_sha256')
            or record.get('file_key') != reference.get('file_key')
            or _hash(record.get('source_receipt')) != reference.get('source_receipt_sha256')):
        raise ValueError('Снимок относится к другой привязке ZIP')
    return {**record, 'evidence_sha256': evidence_sha256, 'historical': True}
