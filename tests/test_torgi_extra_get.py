"""A restarted app reports interrupted large-PDF work without rewriting evidence."""
import pytest

import app
from land import store, torgi_docs


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()


def get(path):
    # Exercise the production route without opening a socket in restricted hosts.
    handler = object.__new__(app.Handler)
    handler.path = path
    handler.allowed = lambda: True
    handler.send = lambda value, *args, **kwargs: value
    return handler.do_GET()


@pytest.mark.parametrize('active', [False, True])
def test_large_pdf_get_marks_abandoned_attempt_and_preserves_live_attempt(db, monkeypatch, active):
    monkeypatch.setattr(app, 'JOBS', {})
    key = torgi_docs.setting_key('trudovoe', active, '_large_pdf_attempt')
    attempt = {'state': 'running', 'processed': 0, 'requested': 1, 'network_requests': 1}
    store.set_setting(key, attempt)
    path = '/api/torgi/' + ('active/' if active else '') + 'documents'
    data = get(path)
    assert data['large_pdf_attempt'] == {**attempt, 'state': 'interrupted'}
    assert data['large_pdf_summary']['remaining'] == 0
    assert store.get_setting(key) == attempt  # GET does not alter stored source history.
    app.JOBS['live'] = {'state': 'running', 'project': 'trudovoe'}
    data = get(path)
    assert data['large_pdf_attempt'] == attempt
