import json
import time
import urllib.error

import pytest
from land import store
from test_http import server, request
from test_browser_lookups import capture, lot, NUMBER


def test_browser_geometry_requires_token_and_runs_locally_without_changing_survey(server):
    survey = {'id': 'survey', 'bounds': [34.20, 44.99, 34.21, 45.0],
              'gaps': {'features': []}, 'layers': {}}
    prior = {'id': 'lots', 'created_at': '2026-10-07T08:00:00+03:00', 'lots': [lot()], 'geometries': {}}
    store.set_setting('survey_trudovoe', survey)
    store.set_setting('torgi_active_trudovoe', prior)
    body = {'id': 'lots', 'survey_id': 'survey', 'bundle': {'version': 1, 'observations': [capture()]}}
    path = '/api/torgi/active/browser-geometry'
    with pytest.raises(urllib.error.HTTPError) as error:
        request(server, path, body, token=False)
    assert error.value.code == 403
    assert store.get_setting('torgi_active_trudovoe') == prior
    with request(server, path, body) as response:
        job_id = json.load(response)['job_id']
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with request(server, '/api/jobs/' + job_id) as response:
            job = json.load(response)
        if job['state'] != 'running':
            break
        time.sleep(.01)
    assert job['state'] == 'done', job
    assert job['result']['imported'] == 1 and job['result']['network_requests'] == 0
    with request(server, '/api/torgi/active/export') as response:
        result = json.load(response)['result']
    assert result['lots'][0]['spatial_state'] == 'inside'
    assert not result['lots'][0]['geometry_confirmed']
    assert result['geometries'][NUMBER]['transport'] == 'browser_response_import'
    assert result['geometries'][NUMBER]['received_at'] == body['bundle']['observations'][0]['received_at']
    assert store.get_setting('survey_trudovoe') == survey
