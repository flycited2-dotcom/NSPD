"""Pure, dated attachment metadata for frozen dossiers; never legal exclusion data."""
from datetime import datetime
import re

from . import archive_evidence, image_frames, torgi, torgi_docs
from . import torgi_archives as archives, torgi_image_frames as frames

ALGORITHM = 'attachment-context-v1'
MAX_FILES = 16
MAX_MEMBERS = archive_evidence.MAX_ENTRIES
MAX_HINTS = 4
MAX_LINKS = 16
HEX = re.compile(r'[0-9a-f]{64}')
FILE_ID = re.compile(r'[0-9a-f]{24}')
LOT_ID = re.compile(r'\d{10,30}_\d{1,6}')
CAD = re.compile(r'\d{1,2}:\d{1,2}:\d{1,10}:\d{1,10}')
WARNING = ('Снимок ранее записанных сведений вложений. Текущие файлы, CRC и кэш '
           'здесь не перечитываются. Номера являются подсказками; права, геопривязка '
           'и принадлежность объекта предмету лота не подтверждены.')
FLAGS = {'used_for_exclusion': False, 'rights_confirmed': False,
         'geometry_confirmed': False, 'georeferenced': False, 'coverage_confirmed': False}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _hex(value):
    return value if isinstance(value, str) and HEX.fullmatch(value) else None


def _id(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value) else None


def _date(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        return value if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None else None
    except ValueError:
        return None


def _number(value, maximum=archive_evidence.MAX_TOTAL_BYTES):
    return value if type(value) is int and 0 <= value <= maximum else None


def _source(file):
    file_id = file.get('file_id')
    return torgi_docs.FILE + file_id if isinstance(file_id, str) and FILE_ID.fullmatch(file_id) else None


def _receipt(record, binding):
    receipt = _dict(record.get('source_receipt'))
    if (receipt.get('source') != _source(binding['parent_file']) or receipt.get('http_status') != 200
            or not _hex(receipt.get('sha256')) or not _date(receipt.get('received_at'))
            or type(receipt.get('actual_size')) is not int
            or receipt['actual_size'] != binding['parent_file']['declared_size']):
        return None
    return {'source': receipt['source'], 'source_sha256': receipt['sha256'],
            'received_at': receipt['received_at'], 'actual_size': receipt['actual_size'],
            'http_status': 200, 'receipt_sha256': archives._hash(receipt)}


def _archive_valid(record, binding, receipt):
    """Validate recorded identities/scopes without opening any source or cache."""
    if (not receipt or record.get('algorithm') != archives.ALGORITHM
            or record.get('state') not in ('read', 'partial')
            or record.get('archive_sha256') != receipt['source_sha256']
            or record.get('archive_complete') is not False or record.get('ocr_performed') is not False
            or record.get('geometry_confirmed') is not False or record.get('georeferenced') is not False
            or record.get('pdf_max_pages') != 40
            or record.get('member_max_bytes') != archive_evidence.MAX_MEMBER_BYTES
            or not _date(record.get('checked_at')) or not _hex(record.get('evidence_sha256'))
            or archives._hash({key: value for key, value in record.items() if key != 'evidence_sha256'})
               != record['evidence_sha256']):
        return False
    observation_id = archives._hash({'binding_id': binding['binding_id'],
                                     'source_receipt': record['source_receipt'], 'algorithm': archives.ALGORITHM})
    manifest = _dict(record.get('manifest'))
    original, entries = manifest.get('entries'), record.get('entries')
    if (record.get('observation_id') != observation_id or manifest.get('algorithm') != archive_evidence.ALGORITHM
            or manifest.get('archive_sha256') != receipt['source_sha256']
            or manifest.get('size') != receipt['actual_size'] or not isinstance(original, list)
            or not isinstance(entries, list) or not len(entries) == len(original) <= MAX_MEMBERS
            or type(manifest.get('entry_count')) is not int or manifest['entry_count'] != len(original)):
        return False
    for before, row in zip(original, entries):
        if not isinstance(before, dict) or not isinstance(row, dict):
            return False
        if (before.get('state') not in ('ready', 'unsupported', 'directory')
                or _number(before.get('size'), archive_evidence.MAX_MEMBER_BYTES) is None
                or _number(before.get('compressed_size'), archive_evidence.MAX_BYTES) is None
                or not isinstance(before.get('format'), str)):
            return False
        if (not _hex(row.get('id')) or any(type(row.get(key)) is not type(value) or row.get(key) != value
                for key, value in before.items() if key not in ('state', 'reason'))
                or row.get('geometry_confirmed') is not False or row.get('georeferenced') is not False
                or row.get('ocr_performed') is not False or type(row.get('reading_complete')) is not bool):
            return False
        expected_state = ('read', 'error') if before.get('state') == 'ready' else (before.get('state'),)
        if row.get('state') not in expected_state:
            return False
        if row['state'] != 'read' and row['reading_complete'] is not False:
            return False
        reference = {'binding_id': binding['binding_id'], 'observation_id': observation_id,
                     'parent_file_key': binding['parent_file']['key'], 'archive_sha256': receipt['source_sha256'],
                     'source_receipt_sha256': receipt['receipt_sha256'], 'entry_id': row['id'],
                     'member_sha256': row.get('sha256')}
        if row.get('association_reference') != reference:
            return False
        if row.get('sha256') is not None and (not _hex(row['sha256']) or row.get('crc_verified') is not True
                or type(row.get('actual_size')) is not int or row['actual_size'] != before.get('size')):
            return False
        if row.get('state') == 'read':
            evidence = _dict(row.get('evidence'))
            details = _dict(evidence.get('details'))
            if (not _hex(row.get('sha256')) or evidence.get('algorithm') != archives.member_algorithm(before.get('format'))
                    or evidence.get('geometry_confirmed') is not False or evidence.get('georeferenced') is not False
                    or details.get('geometry_confirmed') is not False or details.get('georeferenced') is not False
                    or row['reading_complete'] != (details.get('text_layer_complete') is True
                        and not details.get('unread_pages') and not details.get('image_or_sparse_pages'))):
                return False
        elif before.get('state') != 'ready' and ('sha256' in row or 'evidence' in row):
            return False
    counts = {'entries': len(entries), 'supported': sum(row.get('state') == 'ready' for row in original),
              'read': sum(row['state'] == 'read' for row in entries),
              'errors': sum(row['state'] == 'error' for row in entries),
              'unsupported': sum(row['state'] == 'unsupported' for row in entries),
              'directories': sum(row['state'] == 'directory' for row in entries),
              'incomplete': sum(row['state'] == 'read' and not row['reading_complete'] for row in entries)}
    observed_counts = _dict(record.get('counts'))
    if any(type(observed_counts.get(key)) is not int or observed_counts[key] != value for key, value in counts.items()):
        return False
    state = ('partial' if any(counts[key] for key in ('errors', 'unsupported', 'incomplete'))
             or not counts['supported'] else 'read')
    return record['state'] == state


def _member(row):
    evidence = _dict(row.get('evidence'))
    details = _dict(evidence.get('details'))
    candidates = set()
    mentions = details.get('mentions')
    for item in mentions if isinstance(mentions, list) else []:
        number = item.get('cadastral_number') if isinstance(item, dict) else None
        if isinstance(number, str) and CAD.fullmatch(number):
            candidates.add(torgi.canonical(number))
    hints = sorted(candidates)
    fmt = row.get('format')
    return {'entry_id': row['id'], 'format': fmt if isinstance(fmt, str) and re.fullmatch('[a-z0-9]{1,12}', fmt) else None,
            'state': row['state'], 'declared_size': _number(row.get('size'), archive_evidence.MAX_MEMBER_BYTES),
            'sha256': _hex(row.get('sha256')), 'crc32': row.get('crc32') if isinstance(row.get('crc32'), str)
                and re.fullmatch('[0-9a-f]{8}', row['crc32']) else None,
            'crc_verified_observed': row.get('crc_verified') is True,
            'evidence_algorithm': evidence.get('algorithm') if row['state'] == 'read' else None,
            'evidence_sha256': archives._hash(evidence) if evidence else None,
            'text_cache_sha256': _hex(evidence.get('text_cache_sha256')),
            'text_layer_complete_observed': details.get('text_layer_complete') is True,
            'processed_pages': _number(details.get('processed_pages'), 40),
            'unread_pages': _number(details.get('unread_pages')),
            'reading_complete_observed': row['state'] == 'read' and row['reading_complete'],
            'number_hints': [{'cadastral_number': number, 'verification_required': True} for number in hints[:MAX_HINTS]],
            'omitted_number_hints': max(0, len(hints) - MAX_HINTS), 'ocr_performed': False, **FLAGS}


def _archive_file(file, search, catalog, collection, active):
    result = {'file_key': _id(file.get('key')), 'source': _source(file), 'state': 'unaccepted',
              'source_bound': False, 'accepted_observation': False, 'members': [], **FLAGS}
    try:
        identity = archives.source_binding(file, search, catalog, active)
    except (ValueError, TypeError, KeyError, AttributeError):
        return result
    result.update(binding_id=identity['binding_id'], source_bound=True, state='pending')
    record = _dict(_dict(collection.get('records')).get(identity['binding_id']))
    if not record:
        return result
    if record.get('binding_id') != identity['binding_id'] or record.get('source_binding') != identity:
        result['state'] = 'unaccepted'
        return result
    receipt = _receipt(record, identity)
    if receipt:
        result['source_receipt'] = receipt
    if record.get('state') in ('error', 'rejected'):
        result['state'] = record['state']
        return result
    if not _archive_valid(record, identity, receipt):
        result['state'] = 'unaccepted'
        return result
    links = identity['card_receipts']
    result.update(state=record['state'], accepted_observation=True, algorithm=archives.ALGORITHM,
                  evidence_sha256=record['evidence_sha256'], observation_id=record['observation_id'],
                  checked_at=record['checked_at'], counts={key: value for key, value in record['counts'].items()
                      if key in ('entries', 'supported', 'read', 'errors', 'unsupported', 'directories', 'incomplete')},
                  card_receipts=[{'lot_id': row['lot_id'] if LOT_ID.fullmatch(row['lot_id']) else None,
                                  'sha256': row['sha256'], 'received_at': _date(row['received_at'])} for row in links[:MAX_LINKS]],
                  omitted_card_receipts=max(0, len(links) - MAX_LINKS),
                  members=[_member(row) for row in record['entries'][:MAX_MEMBERS]],
                  omitted_members=max(0, len(record['entries']) - MAX_MEMBERS),
                  archive_complete=False, ocr_performed=False)
    return result


def _archive_group(search, catalog, collection, active):
    files = sorted([file for file in _dict(catalog.get('files')).values()
                    if isinstance(file, dict) and file.get('format') == 'zip'], key=lambda file: str(file.get('key')))
    rows = [_archive_file(file, search, catalog, collection, active) for file in files[:MAX_FILES]]
    ids = {row.get('binding_id') for row in rows if row.get('source_bound')}
    records = _dict(collection.get('records'))
    states = ('read', 'partial', 'pending', 'error', 'rejected', 'unaccepted')
    return {'collection_id': _id(collection.get('id')), 'files': rows,
            'counts': {state: sum(row['state'] == state for row in rows) for state in states}
                | {'accepted_observations': sum(row['accepted_observation'] for row in rows),
                   'members_read': sum(row.get('counts', {}).get('read', 0) for row in rows),
                   'members_errors': sum(row.get('counts', {}).get('errors', 0) for row in rows),
                   'members_unsupported': sum(row.get('counts', {}).get('unsupported', 0) for row in rows),
                   'members_incomplete': sum(row.get('counts', {}).get('incomplete', 0) for row in rows)},
            'declared_files': len(files), 'omitted_files': max(0, len(files) - MAX_FILES),
            'retained_or_omitted_observations': sum(key not in ids for key in records),
            'cache_verified_now': False, **FLAGS}


def _frame_file(file, search, catalog, state):
    result = {'file_key': _id(file.get('key')), 'source': _source(file), 'state': 'unaccepted',
              'source_bound': False, 'frames': [], **FLAGS}
    try:
        identity = frames.binding(catalog, search, file)
    except (ValueError, TypeError, KeyError, AttributeError):
        return result
    if not _date(file.get('received_at')):
        return result
    result.update(binding_id=frames._hash(identity), source_bound=True, state='observed',
                  source_sha256=identity['source_sha256'], source_received_at=identity['source_received_at'],
                  source_format=identity['source_format'], declared_frame_count=identity['primary_scope']['source_frame_count'],
                  source_frame_count_basis='container_header', ocr_read=False, complete_source=False)
    for index in range(1, identity['primary_scope']['source_frame_count']):
        observed = frames._observation(state, identity, file, index)
        old = _dict(_dict(_dict(state.get('entries')).get(frames._hash(identity))).get('frames')).get(str(index))
        frame = {'frame_index': index, 'frame_scope': None,
                 'state': 'unaccepted' if old is not None else 'pending', 'accepted_observation': False,
                 'complete_frame_observed': False, 'ocr_read': False, **FLAGS}
        if observed and _date(observed.get('processed_at')):
            frame.update(state=observed['state'], accepted_observation=True, processed_at=observed['processed_at'],
                         observation_sha256=frames._hash(observed), png_sha256=_hex(observed.get('png_sha256')),
                         frame_scope=image_frames.scope(observed['frame_scope']),
                         complete_frame_observed=observed['state'] == 'received')
        result['frames'].append(frame)
    return result


def _frame_group(search, catalog, state):
    files = sorted([file for file in _dict(catalog.get('files')).values()
                    if isinstance(file, dict) and _dict(file.get('image')).get('source_format') == 'MPO'],
                   key=lambda file: str(file.get('key')))
    rows = [_frame_file(file, search, catalog, state) for file in files[:MAX_FILES]]
    units = [unit for file in rows for unit in file['frames']]
    ids = {row.get('binding_id') for row in rows if row.get('source_bound')}
    return {'files': rows, 'declared_files': len(files), 'omitted_files': max(0, len(files) - MAX_FILES),
            'counts': {name: sum(unit['state'] == name for unit in units) for name in ('received', 'error', 'pending', 'unaccepted')}
                | {'declared_auxiliary_frames': len(units), 'unaccepted_files': sum(not row['source_bound'] for row in rows)},
            'retained_or_omitted_observations': sum(key not in ids for key in _dict(state.get('entries'))),
            'cache_verified_now': False, 'primary_not_modified': True, 'ocr_read': False, **FLAGS}


def snapshot(values):
    """Project supplied frozen inputs into bounded metadata; never access live state."""
    if not isinstance(values, dict):
        raise ValueError('Нужны сохранённые входные сведения источников')
    groups = {}
    for label, active, prefix in (('historical', False, 'torgi'), ('regional_active', True, 'torgi_active')):
        search = _dict(values.get(prefix))
        catalog = _dict(values.get(prefix + '_documents'))
        current = torgi_docs.same_search(search, catalog)
        groups[label] = {'search_id': _id(search.get('id')), 'search_source_id': _id(torgi.source_id(search)),
                         'documents_id': _id(catalog.get('id')), 'documents_current': current,
                         'state': 'current' if current else 'legacy_or_mismatched' if search or catalog else 'missing',
                         'archives': _archive_group(search, catalog, _dict(values.get(prefix + '_archives')), active),
                         'auxiliary_frames': _frame_group(search, catalog, _dict(values.get(prefix + '_documents_image_frames'))),
                         **FLAGS}
    return {'algorithm': ALGORITHM, 'groups': groups, 'warning': WARNING, 'cache_verified_now': False,
            'number_hints_confirm_lot': False, 'archive_complete': False, 'ocr_performed': False,
            'limits': {'files_per_kind_per_group': MAX_FILES, 'members_per_archive': MAX_MEMBERS,
                       'number_hints_per_member': MAX_HINTS, 'card_receipts_per_archive': MAX_LINKS}, **FLAGS}
