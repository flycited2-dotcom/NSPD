import errno
import pytest
from land import store


def test_success_replaces_file_and_leaves_no_temporary_files(tmp_path):
    path = tmp_path / 'observation.json'; path.write_bytes(b'old')
    store.atomic_write(path, b'new')
    assert path.read_bytes() == b'new' and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('exists', [False, True])
def test_disk_full_during_flush_preserves_old_source(tmp_path, monkeypatch, exists):
    path = tmp_path / 'source.pdf'
    if exists: path.write_bytes(b'old verified bytes')
    def full(*args): raise OSError(errno.ENOSPC, 'No space left on device')
    monkeypatch.setattr(store.os, 'fsync', full)
    with pytest.raises(OSError): store.atomic_write(path, b'partial new bytes')
    if exists: assert path.read_bytes() == b'old verified bytes'
    else: assert not path.exists()
    assert not list(tmp_path.glob('*.tmp'))


def test_failed_replace_keeps_prior_file_and_cleans_temporary(tmp_path, monkeypatch):
    path = tmp_path / 'cache.json'; path.write_bytes(b'old')
    def fail(*args): raise PermissionError('File is in use')
    monkeypatch.setattr(store.os, 'replace', fail)
    with pytest.raises(PermissionError): store.atomic_write(path, b'new')
    assert path.read_bytes() == b'old' and list(tmp_path.iterdir()) == [path]
