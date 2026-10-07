from datetime import datetime, timezone, timedelta
from pathlib import Path
import json

import pytest

from verbinden.models import Event, Profile
from verbinden.storage import Storage, StorageError

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=JST)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_read_rejects_nonstandard_json_numbers(tmp_path, constant):
    target = tmp_path / "out/test/events.json"
    target.parent.mkdir(parents=True)
    target.write_text('[{"unknown": ' + constant + '}]', encoding="utf-8")
    with pytest.raises(StorageError, match="valid JSON"):
        Storage(tmp_path).load_calendar_events("test")


def event():
    return Event(id="evt-p1", title="展示", date="2026-10-08", source_post_id="p1", source_channel="general", posted_at=NOW)


def test_calendar_loader_preserves_mixed_items_for_api_validation(tmp_path):
    target = tmp_path / "out/test/events.json"
    target.parent.mkdir(parents=True)
    target.write_text('[{}, null, "invalid"]', encoding="utf-8")
    assert Storage(tmp_path).load_calendar_events("test") == [{}, None, "invalid"]


@pytest.mark.parametrize("content", ['{}', 'null', '"invalid"'])
def test_calendar_loader_rejects_non_array(tmp_path, content):
    target = tmp_path / "out/test/events.json"
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")
    with pytest.raises(StorageError):
        Storage(tmp_path).load_calendar_events("test")


def profile():
    return Profile(id="mem-u1", user_id="u1", display_name="架空さん", hobbies=["読書"], recent_work=[], active_hours=[0]*24, post_count=0, updated_at="2026-10-07")


def test_raw_roundtrip_shared_run_and_encoding(tmp_path):
    storage = Storage(tmp_path, clock=lambda: NOW)
    first = storage.save_raw("test", "general", [{"text": "架空の文章"}])
    second = storage.save_raw("test", "times-test1", [])
    assert first.parent == second.parent
    assert first == tmp_path / "raw/test/20261007-120000/general.json"
    assert "架空の文章" in first.read_text()
    assert '\n  {' in first.read_text()
    assert storage.load_latest_raw("test") == {"general": [{"text": "架空の文章"}], "times-test1": []}


def test_latest_raw_ignores_unrelated_folders(tmp_path):
    Storage(tmp_path, clock=lambda: NOW).save_raw("test", "general", [{"text": "old"}])
    later = Storage(tmp_path, clock=lambda: NOW + timedelta(seconds=1))
    later.save_raw("test", "general", [{"text": "new"}])
    (tmp_path / "raw/test/zzz").mkdir()
    assert later.load_latest_raw("test") == {"general": [{"text": "new"}]}


def test_raw_collision_preserves_previous_run(tmp_path):
    Storage(tmp_path, clock=lambda: NOW).save_raw("test", "general", [{"text": "old"}])
    with pytest.raises(StorageError):
        Storage(tmp_path, clock=lambda: NOW).save_raw("test", "general", [])
    assert Storage(tmp_path).load_latest_raw("test")["general"] == [{"text": "old"}]


@pytest.mark.parametrize("env", ["../test", "/tmp", "", "staging"])
def test_environment_boundary(tmp_path, env):
    storage = Storage(tmp_path)
    with pytest.raises(StorageError):
        storage.save_raw(env, "general", [])
    with pytest.raises(StorageError):
        storage.load_extracted(env)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("channel", ["../outside", "/tmp", "a/b", "a\\b", ".", "", "#general"])
def test_channel_boundary(tmp_path, channel):
    with pytest.raises(StorageError):
        Storage(tmp_path).save_raw("test", channel, [])
    assert not list(tmp_path.iterdir())


def test_extracted_models_roundtrip(tmp_path):
    storage = Storage(tmp_path)
    storage.save_extracted("test", [event()], [profile()])
    assert storage.load_extracted("test") == ([event()], [profile()])
    assert json.loads((tmp_path / "extracted/test/events.json").read_text())[0]["sourceChannel"] == "general"


def test_output_roundtrip(tmp_path):
    storage = Storage(tmp_path)
    calendar = [{"id": "evt-p1", "summary": "展示", "start": {"date": "2026-10-08"}}]
    storage.save_out("test", [profile()], calendar)
    assert storage.load_out("test") == ([profile()], calendar)


@pytest.mark.parametrize("loader", ["load_latest_raw", "load_extracted", "load_out"])
def test_missing_data_safe_error(tmp_path, loader):
    with pytest.raises(StorageError):
        getattr(Storage(tmp_path), loader)("test")


def test_corruption_error_does_not_include_content(tmp_path):
    storage = Storage(tmp_path)
    path = storage.save_raw("test", "general", [])
    path.write_text("private-secret invalid json")
    with pytest.raises(StorageError) as error:
        storage.load_latest_raw("test")
    assert "private-secret" not in str(error.value)


def test_corrupt_model_safe_error(tmp_path):
    storage = Storage(tmp_path)
    storage.save_extracted("test", [event()], [profile()])
    (tmp_path / "extracted/test/events.json").write_text('[{"private-secret": true}]')
    with pytest.raises(StorageError) as error:
        storage.load_extracted("test")
    assert "private-secret" not in str(error.value)


def test_atomic_replace_failure_preserves_file_and_cleans_temp(tmp_path, monkeypatch):
    storage = Storage(tmp_path)
    storage.save_out("test", [profile()], [])
    path = tmp_path / "out/test/profiles.json"
    before = path.read_bytes()
    def fail(*args):
        raise OSError("private-secret")
    monkeypatch.setattr("verbinden.storage.os.replace", fail)
    with pytest.raises(StorageError) as error:
        storage.save_out("test", [], [])
    assert "private-secret" not in str(error.value)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["events.json", "profiles.json"]


def test_symlink_path_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "raw").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageError):
        Storage(tmp_path).save_raw("test", "general", [])
    assert list(outside.iterdir()) == []


def test_unicode_channel(tmp_path):
    storage = Storage(tmp_path)
    storage.save_raw("test", "展示案内", [])
    assert storage.load_latest_raw("test") == {"展示案内": []}


@pytest.mark.parametrize("data", [{"text": "oops"}, ["oops"]])
def test_invalid_raw_shape(tmp_path, data):
    with pytest.raises(StorageError):
        Storage(tmp_path).save_raw("test", "general", data)
    assert list(tmp_path.iterdir()) == []


def test_serialization_failure_preserves_old_output(tmp_path):
    storage = Storage(tmp_path)
    storage.save_out("test", [], [{"title": "old"}])
    with pytest.raises(StorageError):
        storage.save_out("test", [], [{"value": float("nan")}])
    assert storage.load_out("test") == ([], [{"title": "old"}])
    assert not list((tmp_path / "out/test").glob(".tmp-*"))


def test_load_profiles_does_not_require_event_file(tmp_path):
    store = Storage(tmp_path)
    store.save_out('test', [], [])
    (tmp_path / 'out' / 'test' / 'events.json').unlink()
    assert store.load_profiles('test') == []


def test_load_calendar_events_does_not_require_profile_file(tmp_path):
    store = Storage(tmp_path)
    store.save_out('test', [], [{'id': 'slk1001', 'summary': '架空イベント',
                                'start': {'date': '2026-10-08'}, 'end': {'date': '2026-10-09'}}])
    (tmp_path / 'out' / 'test' / 'profiles.json').write_text('broken', encoding='utf-8')
    assert len(store.load_calendar_events('test')) == 1
