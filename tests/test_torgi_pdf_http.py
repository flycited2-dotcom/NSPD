"""Local PDF jobs use the authenticated job API and separate document catalogs."""
import json
import time
import urllib.error

import pytest
import app
from land import store, torgi_docs
from test_http import server, request


@pytest.mark.parametrize('active', [False, True])
def test_pdf_continue_job_is_protected_and_forwards_catalog_and_scope(server, monkeypatch, active):
    calls = []
    def operation(project, params, active=False):
        calls.append((project, params, active))
        return {'processed': 2, 'network_requests': 0}
    monkeypatch.setattr(app.torgi_pdf_local, 'run', operation)
    path = '/api/torgi/' + ('active/' if active else '') + 'documents/continue'
    params = {'id': 'dated-catalog', 'retry_errors': False}
    if active:
        params['lot_id'] = '22000000000000000000_1'
    with pytest.raises(urllib.error.HTTPError) as error:
        request(server, path, params, token=False)
    assert error.value.code == 403 and calls == []
    with request(server, path, params) as response:
        job_id = json.load(response)['job_id']
    for _ in range(50):
        with request(server, '/api/jobs/' + job_id) as response:
            job = json.load(response)
        if job['state'] != 'running':
            break
        time.sleep(.01)
    assert job['state'] == 'done' and job['result']['network_requests'] == 0
    assert calls == [('trudovoe', params, active)]


@pytest.mark.parametrize('active', [False, True])
def test_pdf_continue_get_reports_interrupted_job_without_rewriting_observations(server, active):
    key = torgi_docs.setting_key('trudovoe', active)
    catalog = {'id': 'dated-catalog', 'search_id': 'search', 'cards': [], 'files': {}}
    store.set_setting(key, catalog)
    store.set_setting(torgi_docs.torgi.setting_key('trudovoe', active), {'id': 'search', 'lots': []})
    attempt = {'state': 'running', 'network_requests': 0, 'processed': 0}
    store.set_setting(torgi_docs.setting_key('trudovoe', active, '_pdf_local_attempt'), attempt)
    path = '/api/torgi/' + ('active/' if active else '') + 'documents/export'
    with request(server, path) as response:
        data = json.load(response)
    assert data['pdf_local_attempt']['state'] == 'interrupted'
    assert data['pdf_local_remaining'] == data['pdf_local_retry_remaining'] == 0
    assert data['pdf_local_summary']['unread_pages'] == 0
    assert data['pdf_local_summary']['max_pages'] == 120
    assert data['selected_pdf_local_summary'] is None
    assert store.get_setting(key) == catalog
    assert store.get_setting(torgi_docs.setting_key('trudovoe', active, '_pdf_local_attempt')) == attempt
