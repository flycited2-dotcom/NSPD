import copy
import json

import pytest

from land import candidate_checks, recon, store
from test_recon import BOUNDS, fixture


def test_progress_is_frozen_with_candidate_in_export_dossier_and_watch(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    values = fixture()
    for key, value in values.items():
        store.set_setting(key + '_trudovoe', value)
    monkeypatch.setattr(recon.nspd, 'request_json', lambda *a: pytest.fail('Unexpected source request'))
    recon.run('trudovoe', {'bounds': BOUNDS, 'purpose': 'housing', 'refresh_nspd': False})
    data = recon.report('trudovoe')
    result = data['result']
    candidate = result['candidates'][0]
    progress = copy.deepcopy(candidate['check_progress'])
    assert progress['algorithm'] == candidate_checks.ALGORITHM
    assert not data['stale']
    assert recon.collection(data)['features'][0]['properties']['check_progress'] == progress
    assert 'Ход проверки контура' in recon.html_report(data, candidate['id']).decode()
    assert recon.load('trudovoe', result['id'])['result']['candidates'][0]['check_progress'] == progress
    recon.watch('trudovoe', {'result_id': result['id'], 'id': candidate['id']})
    store.set_setting('pzz_context_trudovoe', {'id': 'different-source'})
    report = recon.report('trudovoe')
    assert report['stale'] and report['watchlist'][0]['stale']
    assert report['watchlist'][0]['candidate']['check_progress'] == progress
    assert recon.load('trudovoe', result['id'])['result']['candidates'][0]['check_progress'] == progress
    assert 'Это снимок прежнего расчёта' in recon.html_report(report, candidate['id']).decode()
    assert not candidate['rights_confirmed'] and not candidate['srzu_ready']
    assert result['summary']['ready_to_submit'] == result['summary']['confirmed_free'] == 0


def test_older_versions_stay_readable_without_reconstructing_evidence_from_current_sources():
    result = recon.build(fixture(), recon.options({'purpose': 'housing'}))
    result.update(id='previous', operation_warnings=[], version=12)
    for candidate in result['candidates']:
        candidate.pop('check_progress')
    before = copy.deepcopy(result)
    dossier = recon.html_report({'result': result, 'stale': True}).decode()
    assert 'В этой версии таблица проверок не рассчитывалась' in dossier
    assert result == before
    assert all(f['properties']['check_progress'] is None for f in recon.collection({'result': result})['features'])


def test_checks_dossier_escapes_strings_and_rejects_unsafe_links():
    candidate = recon.build(fixture(), recon.options({}))['candidates'][0]
    candidate['check_progress']['next_actions'][0] = '<script>alert(1)</script>'
    row = candidate['check_progress']['rows'][0]
    row.update(title='<img src=x>', observation='"<iframe>', remaining='<script>next</script>')
    row['evidence'] = [{'label': '<img src=x>', 'source': 'javascript:alert(2)', 'received_at': '<script>date</script>',
                        'sha256': '"<img src=x>', 'state': '<iframe>'}]
    section = recon.checks_section(candidate)
    assert '<script>' not in section and '<img' not in section and '<iframe>' not in section
    assert 'href="javascript:' not in section and '&lt;script&gt;date' in section
    assert '&lt;img src=x&gt;' in section


def test_unrecognized_progress_version_does_not_render_as_supported_checks():
    candidate = recon.build(fixture(), recon.options({}))['candidates'][0]
    candidate['check_progress']['algorithm'] = 'future-algorithm'
    section = recon.checks_section(candidate)
    assert 'не рассчитывалась' in section and '<table>' not in section


def test_progress_does_not_change_candidate_identity_or_input_snapshot():
    values = fixture()
    before = copy.deepcopy(values)
    result = recon.build(values, recon.options({}))
    assert values == before
    for candidate in result['candidates']:
        progress = candidate['check_progress']
        assert len(progress['rows']) == 8
        assert not progress['coverage_confirmed'] and not progress['rights_confirmed'] and not progress['ready_to_submit']
        assert all(not row['confirmed'] for row in progress['rows'])
    # The public export remains ordinary serializable GeoJSON, with the same vertices.
    exported = json.loads(json.dumps(recon.collection({'result': result})))
    assert [f['geometry'] for f in exported['features']] == json.loads(json.dumps([c['geometry'] for c in result['candidates']]))
