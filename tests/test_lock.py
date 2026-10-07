from datetime import datetime, timedelta, timezone
import json
import os
from multiprocessing import get_context

import pytest

from verbinden.batch_lock import BatchLock, LockBusy

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone(timedelta(hours=9)))


def attempt_lock(root, output):
    try:
        with BatchLock(root, 'test'):
            output.put('acquired')
    except LockBusy:
        output.put('busy')


def crash_with_lock(root):
    BatchLock(root, 'test').acquire()
    os._exit(0)


def test_exclusive_metadata_release_on_failure(tmp_path):
    lock = BatchLock(tmp_path, 'test', clock=lambda: NOW)
    with pytest.raises(RuntimeError):
        with lock:
            assert lock.held
            metadata = json.loads((tmp_path/'test/.batch.lock').read_text())
            assert metadata['pid'] > 0
            assert metadata['createdAt'] == NOW.isoformat()
            with pytest.raises(LockBusy):
                BatchLock(tmp_path, 'test').acquire()
            raise RuntimeError('failure')
    assert not lock.held
    with BatchLock(tmp_path, 'test'):
        pass


def test_stale_file_reclaimed_but_active_lock_never_stolen(tmp_path, caplog):
    path = tmp_path/'test/.batch.lock'
    path.parent.mkdir()
    path.write_text(json.dumps({'pid': 123, 'createdAt': (NOW-timedelta(minutes=31)).isoformat()}))
    with BatchLock(tmp_path, 'test', clock=lambda: NOW):
        assert 'stale' in caplog.text
        with pytest.raises(LockBusy):
            BatchLock(tmp_path, 'test', clock=lambda: NOW+timedelta(hours=1)).acquire()


def test_cross_process_exclusion(tmp_path):
    context = get_context('spawn')
    output = context.Queue()
    with BatchLock(tmp_path, 'test'):
        process = context.Process(target=attempt_lock, args=(tmp_path, output))
        process.start()
        process.join(10)
        assert process.exitcode == 0
        assert output.get(timeout=2) == 'busy'
    output.close()


def test_process_exit_releases_lock_without_deleting_inode(tmp_path):
    context = get_context('spawn')
    process = context.Process(target=crash_with_lock, args=(tmp_path,))
    process.start()
    process.join(10)
    assert process.exitcode == 0
    assert (tmp_path/'test/.batch.lock').exists()
    with BatchLock(tmp_path, 'test'):
        pass


@pytest.mark.parametrize('env', ['../outside', 'staging', ''])
def test_invalid_environment(tmp_path, env):
    with pytest.raises(ValueError):
        BatchLock(tmp_path, env)


def test_symlink_rejected_without_touching_target(tmp_path):
    target = tmp_path/'private'
    target.write_text('KEEP')
    (tmp_path/'test').mkdir()
    (tmp_path/'test/.batch.lock').symlink_to(target)
    with pytest.raises(RuntimeError):
        BatchLock(tmp_path, 'test').acquire()
    assert target.read_text() == 'KEEP'
