"""Isolated, bounded observations of ZIP members; never unpack archive paths."""
import hashlib
import json
from pathlib import Path
import sys
from . import archive_evidence, egrn_xml, image_evidence, store, torgi_file_worker
from .docx_text import ALGORITHM as DOCX_ALGORITHM
from .legacy_text import ALGORITHM as LEGACY_ALGORITHM
from .egrn_coordinates import PDF_ALGORITHM


ALGORITHM = 'torgi-archive-members-v2'
MAX_OUTPUT = 4 * 1024 * 1024
MAX_TEXT = 8 * 1024 * 1024


def algorithm(fmt):
    return (egrn_xml.ALGORITHM if fmt == 'xml' else DOCX_ALGORITHM if fmt == 'docx' else LEGACY_ALGORITHM if fmt in ('doc', 'rtf')
            else image_evidence.ALGORITHM if fmt in image_evidence.FORMATS else PDF_ALGORITHM)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def xml_whitelist_bytes(details):
    """Canonical selected fields only; raw XML never enters the member cache."""
    return json.dumps(details, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def process(raw, expected_sha256, folder):
    """Read supported members into a separate flat, content-addressed cache.

    Unsupported entries remain metadata only. Failed CRC, parsing, page/image
    limits and omitted content remain visible, rather than a claim that the
    archive or the legal lot has been checked completely.
    """
    manifest = archive_evidence.inspect(raw)
    if manifest['archive_sha256'] != expected_sha256:
        raise ValueError('SHA-256 исходного ZIP изменился')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    output = {'algorithm': ALGORITHM, 'archive_sha256': expected_sha256,
              'manifest': manifest, 'entries': [], 'archive_complete': False,
              'geometry_confirmed': False, 'georeferenced': False, 'ocr_performed': False,
              'pdf_max_pages': 40, 'member_max_bytes': archive_evidence.MAX_MEMBER_BYTES,
              'xml_max_bytes': egrn_xml.MAX_BYTES}
    for entry in manifest['entries']:
        item = {**entry, 'geometry_confirmed': False, 'georeferenced': False,
                'reading_complete': False, 'ocr_performed': False}
        if entry['state'] == 'ready':
            try:
                member, checked = archive_evidence.read_member(
                    raw, archive_evidence.member_identifier(manifest, entry))
                item.update(checked)
                digest, fmt = checked['sha256'], entry['format']
                if fmt == 'xml':
                    details, pages = egrn_xml.extract(member), []
                else:
                    # Raw XML can contain owner/recipient fields and is never copied.
                    # Other formats retain the existing checked, flat cache contract.
                    store.atomic_write(folder / (digest + '.' + fmt), member)
                    details, pages = torgi_file_worker.extract(member, fmt)
                details = {**details, 'geometry_confirmed': False, 'georeferenced': False}
                evidence = {'algorithm': algorithm(fmt), 'details': details,
                            'geometry_confirmed': False, 'georeferenced': False}
                # The output ceiling also bounds retained metadata per member.
                if len(_json(evidence)) > MAX_OUTPUT - len(_json(output)) - 65536:
                    raise ValueError('Результат чтения записи превышает общий лимит 4 МиБ')
                if fmt == 'xml':
                    selected = xml_whitelist_bytes(details)
                    selected_sha = hashlib.sha256(selected).hexdigest()
                    safe_path = folder / (selected_sha + '.xml.whitelist.json')
                    if safe_path.exists():
                        with safe_path.open('rb') as cached:
                            cached_selected = cached.read(len(selected) + 1)
                        if safe_path.stat().st_size != len(selected) or cached_selected != selected:
                            raise ValueError('Безопасный неизменяемый кэш XML повреждён')
                    else:
                        store.atomic_write(safe_path, selected)
                    evidence.update(whitelist_cache_sha256=selected_sha,
                                    whitelist_cache_size=len(selected))
                if pages:
                    text = '\n'.join(f'UNIT {number}\n{value}' for number, value in pages).encode('utf-8')
                    if len(text) > MAX_TEXT:
                        raise ValueError('Текст записи превышает лимит 8 МиБ')
                    store.atomic_write(folder / (digest + '.txt'), text)
                    evidence.update(text_cache_sha256=hashlib.sha256(text).hexdigest(),
                                    text_cache_size=len(text))
                item.update(state='read', evidence=evidence,
                            reading_complete=fmt != 'xml' and details.get('text_layer_complete') is True
                            and not details.get('unread_pages')
                            and not details.get('image_or_sparse_pages'))
            except Exception as exc:
                item.update(state='error', error=('ЕГРН XML: чтение выбранных полей не удалось'
                            if entry['format'] == 'xml' else str(exc)[:500]), reading_complete=False)
                item.pop('evidence', None)
        output['entries'].append(item)
    entries = output['entries']
    output['counts'] = {'entries': len(entries),
                        'supported': sum(row['state'] == 'ready' for row in manifest['entries']),
                        'read': sum(row['state'] == 'read' for row in entries),
                        'errors': sum(row['state'] == 'error' for row in entries),
                        'unsupported': sum(row['state'] == 'unsupported' for row in entries),
                        'directories': sum(row['state'] == 'directory' for row in entries),
                        'incomplete': sum(row['state'] == 'read' and not row['reading_complete'] for row in entries)}
    output['all_supported_members_read'] = (output['counts']['supported'] > 0
                                            and output['counts']['read'] == output['counts']['supported'])
    output['state'] = ('partial' if any(output['counts'][key] for key in ('errors', 'unsupported', 'incomplete'))
                       or not output['counts']['supported'] else 'read')
    if len(_json(output)) > MAX_OUTPUT:
        raise ValueError('Результат чтения архива превышает лимит 4 МиБ')
    return output


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        path, digest, folder = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
        if not path.is_file() or path.stat().st_size > archive_evidence.MAX_BYTES:
            raise ValueError('Исходный ZIP отсутствует или превышает лимит 32 МиБ')
        with path.open('rb') as source:
            raw = source.read(archive_evidence.MAX_BYTES + 1)
        result = process(raw, digest, folder)
        print(_json(result).decode('utf-8'))
    except Exception as exc:
        print(json.dumps({'error': str(exc)[:500]}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
