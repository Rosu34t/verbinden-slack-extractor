"""Refresh integration tests use only offline collaborators."""
import json
from pathlib import Path

import pytest

from test_batch import runtime, Gateway, message, NOW
from verbinden import batch
from verbinden.batch_lock import BatchLock, LockBusy
from verbinden.llm import FakeLlm
from verbinden.storage import Storage, StorageError


def seed(runtime):
    storage = Storage(runtime.root / 'data', clock=lambda: NOW)
    storage.save_extracted('test', [], [])
    storage.save_out('test', [], [])
    return storage


def snapshot(storage):
    return {str(path.relative_to(storage.base_dir)): path.read_bytes()
            for stage in ('extracted', 'out') for path in (storage.base_dir / stage).rglob('*.json')}


def test_refresh_events_changes_only_events(runtime):
    storage = seed(runtime)
    before = snapshot(storage)
    gateway = Gateway({'general': [message(text='明日工作会')]})
    llm = FakeLlm(json.dumps({'events': [{'sourcePostId': '1791320000.000001',
                                        'title': '工作会', 'date': '2026-10-08'}]}))
    result = batch.refresh_events(runtime, gateway=gateway, llm=llm, storage=storage, clock=lambda: NOW)
    after = snapshot(storage)
    assert result.target == 'events' and result.count == 1
    assert all(before[key] == after[key] for key in before if 'profiles' in key)
    assert all(before[key] != after[key] for key in before if 'events' in key)
    assert [call[1] for call in gateway.calls if call[0] == 'history'] == ['general']
    assert (storage.base_dir / 'raw/test/20261007-120000-events/general.json').exists()


def test_refresh_members_merge_filter_mask_and_leave_events(runtime):
    storage = seed(runtime)
    before = snapshot(storage)
    gateway = Gateway({'times-a': [message(text='@Alice Smith 感謝。工作しました'),
                                  message(user='FRIEND', text='友人の投稿'),
                                  message(thread_ts='parent', text='返信')],
                       'times-b': [message(text='@Alice Smith 感謝。工作しました')], 'times-c': []})
    llm = FakeLlm('{"intro":"工作が好きです"}')
    result = batch.refresh_members(runtime, gateway=gateway, llm=llm, storage=storage, clock=lambda: NOW)
    assert result.target == 'members' and result.count == 2
    assert all(snapshot(storage)[key] == value for key, value in before.items() if 'events' in key)
    profiles = storage.load_profiles('test')
    assert profiles[0].post_count == sum(profiles[0].active_hours) == 1
    assert all(word not in llm.prompts[0] for word in ('Alice', 'Smith', '友人の投稿', '返信'))
    assert not any(call[1] == 'general' for call in gateway.calls)
    assert (storage.base_dir / 'raw/test/20261007-120000-members/times-a.json').exists()


@pytest.mark.parametrize('target', ['events', 'members'])
@pytest.mark.parametrize('failure', ['fetch', 'llm'])
def test_refresh_failure_preserves_all_published_files(runtime, target, failure):
    storage = seed(runtime)
    before = snapshot(storage)
    class BrokenGateway(Gateway):
        def fetch_history(self, *args):
            if failure == 'fetch':
                raise RuntimeError('PRIVATE secret')
            return [message()]
    class BrokenLlm:
        def generate_json(self, prompt):
            raise RuntimeError('PRIVATE secret')
    with pytest.raises(RuntimeError):
        getattr(batch, 'refresh_' + target)(runtime, gateway=BrokenGateway(), llm=BrokenLlm(),
                                          storage=storage, clock=lambda: NOW)
    assert snapshot(storage) == before
    with BatchLock(storage.base_dir, 'test', clock=lambda: NOW):
        pass


def test_refresh_lock_rejects_cli_and_other_refresh(runtime):
    storage = seed(runtime)
    with BatchLock(storage.base_dir, 'test', clock=lambda: NOW):
        with pytest.raises(LockBusy):
            batch.run_batch(runtime, 'output', storage=storage, clock=lambda: NOW)
        with pytest.raises(LockBusy):
            batch.refresh_events(runtime, gateway=Gateway(), llm=FakeLlm(), storage=storage, clock=lambda: NOW)


def test_refresh_reuses_held_lease_and_does_not_release_it(runtime):
    storage = seed(runtime)
    with BatchLock(storage.base_dir, 'test', clock=lambda: NOW) as lease:
        batch.refresh_events(runtime, gateway=Gateway(), llm=FakeLlm(), storage=storage,
                             clock=lambda: NOW, _lease=lease)
        assert lease.held
    assert not lease.held


@pytest.mark.parametrize('held', [False, True])
def test_wrong_lease_is_rejected_before_fetch(runtime, tmp_path, held):
    storage = seed(runtime)
    lease = BatchLock(tmp_path / 'wrong', 'test', clock=lambda: NOW)
    if held:
        lease.acquire()
    try:
        with pytest.raises(ValueError):
            batch.refresh_events(runtime, gateway=Gateway(), llm=FakeLlm(), storage=storage,
                                 clock=lambda: NOW, _lease=lease)
    finally:
        lease.release()


def test_latest_raw_ignores_refresh_folders(runtime):
    storage = seed(runtime)
    storage.save_raw('test', 'general', [{'text': 'full'}])
    storage.save_refresh_raw('test', 'events', 'general', [{'text': 'partial'}])
    assert storage.load_latest_raw('test') == {'general': [{'text': 'full'}]}


@pytest.mark.parametrize('existing', [False, True])
def test_publish_rolls_back_second_replace_failure(runtime, monkeypatch, existing):
    storage = seed(runtime)
    if not existing:
        for path in (storage.base_dir / 'extracted/test/events.json', storage.base_dir / 'out/test/events.json'):
            path.unlink()
    before = snapshot(storage)
    import verbinden.storage as module
    replace = module.os.replace
    def fail_second(source, destination):
        if Path(destination) == storage.base_dir / 'out/test/events.json':
            raise OSError('PRIVATE token')
        return replace(source, destination)
    monkeypatch.setattr(module.os, 'replace', fail_second)
    with pytest.raises(StorageError, match='could not publish'):
        storage.save_refresh_events('test', [], [{'id': 'new'}])
    assert snapshot(storage) == before
    assert not list(storage.base_dir.rglob('.tmp-*'))


def test_publish_serialization_failure_changes_neither_file(runtime):
    storage = seed(runtime)
    before = snapshot(storage)
    with pytest.raises(StorageError):
        storage.save_refresh_events('test', [], [{'bad': object()}])
    assert snapshot(storage) == before
    assert not list(storage.base_dir.rglob('.tmp-*'))


def test_repeated_refresh_with_same_storage_has_distinct_raw_runs(runtime):
    from datetime import timedelta
    storage = seed(runtime)
    for moment in (NOW, NOW + timedelta(minutes=10)):
        batch.refresh_events(runtime, gateway=Gateway(), llm=FakeLlm(), storage=storage, clock=lambda: moment)
    assert sorted(path.name for path in (storage.base_dir / 'raw/test').iterdir()) == [
        '20261007-120000-events', '20261007-121000-events']


def test_partial_member_failure_does_not_publish_successful_owner(runtime, caplog):
    storage = seed(runtime)
    before = snapshot(storage)
    class Llm:
        calls = 0
        def generate_json(self, prompt):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError('PRIVATE token U2')
            return '{"intro":"制作が好きです"}'
    gateway = Gateway({'times-a': [message()], 'times-b': [], 'times-c': [message(user='U2')]})
    with pytest.raises(RuntimeError):
        batch.refresh_members(runtime, gateway=gateway, llm=Llm(), storage=storage, clock=lambda: NOW)
    assert snapshot(storage) == before
    assert not any(word in caplog.text for word in ('PRIVATE', 'token', 'U2'))


def test_refresh_save_failure_releases_lock_and_preserves_files(runtime, monkeypatch):
    storage = seed(runtime)
    before = snapshot(storage)
    def fail(*args):
        raise StorageError('could not publish refresh data')
    monkeypatch.setattr(storage, 'save_refresh_events', fail)
    with pytest.raises(StorageError):
        batch.refresh_events(runtime, gateway=Gateway(), llm=FakeLlm(), storage=storage, clock=lambda: NOW)
    assert snapshot(storage) == before
    with BatchLock(storage.base_dir, 'test', clock=lambda: NOW):
        pass


def test_api_lease_is_retained_when_refresh_fails(runtime):
    storage = seed(runtime)
    class GatewayFailure(Gateway):
        def find_channel(self, name):
            raise RuntimeError('PRIVATE')
    with BatchLock(storage.base_dir, 'test', clock=lambda: NOW) as lease:
        with pytest.raises(RuntimeError):
            batch.refresh_events(runtime, gateway=GatewayFailure(), llm=FakeLlm(), storage=storage,
                                 clock=lambda: NOW, _lease=lease)
        assert lease.held


def test_committed_refresh_cleanup_failure_keeps_success(runtime, monkeypatch, caplog):
    storage = seed(runtime)
    unlink = Path.unlink
    def fail_temporary(self, *args, **kwargs):
        if self.name.startswith('.tmp-'):
            raise OSError('PRIVATE token')
        return unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_temporary)
    storage.save_refresh_events('test', [], [{'id': 'published'}])
    assert storage.load_calendar_events('test') == [{'id': 'published'}]
    assert 'cleanup count=' in caplog.text
    assert 'PRIVATE' not in caplog.text and 'token' not in caplog.text


def test_member_publish_second_replace_failure_restores_extraction(runtime, monkeypatch):
    storage = seed(runtime)
    before = snapshot(storage)
    import verbinden.storage as module
    replace = module.os.replace
    def fail_second(source, destination):
        if Path(destination) == storage.base_dir / 'out/test/profiles.json':
            raise OSError('PRIVATE')
        return replace(source, destination)
    monkeypatch.setattr(module.os, 'replace', fail_second)
    gateway = Gateway({'times-a': [message()], 'times-b': [], 'times-c': []})
    with pytest.raises(StorageError):
        batch.refresh_members(runtime, gateway=gateway, llm=FakeLlm('{"intro":"制作が好きです"}'),
                              storage=storage, clock=lambda: NOW)
    assert snapshot(storage) == before


def test_only_partial_raw_is_not_eligible_for_cli_extract(runtime):
    storage = seed(runtime)
    storage.save_refresh_raw('test', 'events', 'general', [])
    with pytest.raises(StorageError, match='no raw run'):
        storage.load_latest_raw('test')


def test_invalid_refresh_clock_is_rejected_before_lock_or_fetch(runtime):
    storage = seed(runtime)
    gateway = Gateway()
    with pytest.raises(ValueError, match='JST'):
        batch.refresh_events(runtime, gateway=gateway, llm=FakeLlm(), storage=storage,
                             clock=lambda: NOW.replace(tzinfo=None))
    assert not gateway.calls
