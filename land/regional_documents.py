"""Bounded collection of current land-provision cards and their supporting files."""
from . import store, torgi, torgi_docs

MAX_CARD_BATCHES = 5
MAX_FILE_BATCHES = 4


def collect(project, params):
    retry = params.get('retry_errors', False)
    if not isinstance(retry, bool):
        raise ValueError('Параметр повтора должен быть логическим')
    search = store.get_setting(torgi.setting_key(project, True))
    if not search or params.get('search_id') != search['id']:
        raise ValueError('Региональный поиск изменился; обновите страницу')
    totals = {'cards_processed': 0, 'files_processed': 0, 'network_files': 0, 'state': 'done'}
    for _ in range(MAX_CARD_BATCHES):
        catalog = store.get_setting(torgi_docs.setting_key(project, True)) or {}
        if torgi_docs.same_search(search, catalog) and not torgi_docs.metadata_queue(search, catalog, retry=retry, active=True, survey=store.get_setting('survey_' + project)):
            break
        report = torgi_docs.metadata(project, {'search_id': search['id'], 'retry_errors': retry}, active=True)
        totals['cards_processed'] += report['processed']
        if report['state'] != 'done':
            totals['state'] = report['state']
            break
        if not report['remaining']:
            break
    if totals['state'] == 'done':
        for _ in range(MAX_FILE_BATCHES):
            catalog = store.get_setting(torgi_docs.setting_key(project, True))
            if not torgi_docs.same_search(search, catalog):
                raise ValueError('Карточки не соответствуют региональному поиску')
            if not torgi_docs.file_queue(catalog, retry=retry):
                break
            report = torgi_docs.read(project, {'id': catalog['id'], 'retry_errors': retry}, active=True)
            totals['files_processed'] += report['processed']
            totals['network_files'] += report['network_requests']
            if report['state'] != 'done':
                totals['state'] = report['state']
                break
            if not report['remaining']:
                break
    catalog = store.get_setting(torgi_docs.setting_key(project, True)) or {}
    if (store.get_setting(torgi.setting_key(project, True)) or {}).get('id') != search['id']:
        raise ValueError('Региональный поиск изменился во время получения документов')
    totals.update(id=catalog.get('id'),
                  cards_remaining=len(torgi_docs.metadata_queue(search, catalog, active=True, survey=store.get_setting('survey_' + project))),
                  files_remaining=len(torgi_docs.file_queue(catalog)),
                  geometry_confirmed=False)
    return totals
