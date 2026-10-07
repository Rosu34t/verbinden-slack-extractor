import json
from datetime import datetime, timezone, timedelta

import pytest
from pydantic import ValidationError

from verbinden.models import Post
from verbinden.validate import validate_events, validate_profile


def post(id='100.1', day=1):
    return Post(id=id, channel='fiction', user_id='U1', user_name='架空', text='架空の投稿',
                posted_at=datetime(2026, 10, day, tzinfo=timezone(timedelta(hours=9))))


def event(**changes):
    return dict(sourcePostId='100.1', title='架空の展示', date='2026-10-08', **changes)


def run(items, posts=None):
    return validate_events(json.dumps({'events': items}), posts or [post()])


@pytest.mark.parametrize('raw', ['{broken private text', 'null', '[]', '{}', '{"events":null}'])
def test_invalid_envelope_is_safe(raw):
    events, reasons = validate_events(raw, [post()])
    assert not events and reasons
    assert 'private text' not in str(reasons)


@pytest.mark.parametrize('changes', [dict(sourcePostId='missing'), dict(title=' '), dict(title=4),
    dict(date='2026-02-30'), dict(date='20261008'), dict(date='2026-1-08'),
    dict(startTime='9:00'), dict(endTime='24:00'), dict(place=42)])
def test_invalid_events_are_dropped(changes):
    item = event()
    item.update(changes)
    events, reasons = run([item])
    assert events == [] and reasons


def test_event_mapping_truncation_and_empty_optional():
    item = event(summary='s' * 201, place=' ', startTime='', endTime=None)
    item['title'] = 't' * 101
    events, reasons = run([item])
    assert not reasons
    result = events[0]
    assert result.id == 'evt-100.1' and result.posted_at == post().posted_at
    assert len(result.title) == 100 and len(result.summary) == 200
    assert result.place is None and result.start_time is None


@pytest.mark.parametrize('end_date,expected', [(None, None), ('2026-10-08', None),
    ('2026-10-07', None), ('2026-10-09', '10:00')])
def test_end_time_rule(end_date, expected):
    events, _ = run([event(endDate=end_date, startTime='18:00', endTime='10:00')])
    assert (events[0].end_time.isoformat(timespec='minutes') if events[0].end_time else None) == expected


def test_duplicate_keeps_oldest_source_even_if_llm_order_reversed():
    newer = event(); newer['sourcePostId'] = '200.1'
    events, reasons = run([newer, event()], [post('100.1', 1), post('200.1', 2)])
    assert len(events) == 1 and events[0].source_post_id == '100.1' and reasons


def test_bad_member_does_not_discard_valid_members():
    events, reasons = run([None, event()])
    assert len(events) == 1 and reasons


def test_profile_limits_and_immutable_lists():
    fields, reasons = validate_profile(json.dumps(dict(intro='a'*81, hobbies=['x']*6,
        recentWork=['y']*6, tendency=' ', summary='fiction')))
    assert len(fields.intro) == 80 and fields.hobbies == ('x',)*5
    assert fields.recent_work == ('y',)*5 and fields.tendency is None and not reasons
    assert isinstance(fields.model_dump(mode='json', by_alias=True)['recentWork'], list)
    with pytest.raises(ValidationError):
        fields.intro = 'changed'


@pytest.mark.parametrize('raw', ['secret bad json', 'null', '[]'])
def test_profile_invalid_root_safe_fallback(raw):
    fields, reasons = validate_profile(raw)
    assert fields.intro is None and fields.hobbies == () and reasons
    assert 'secret bad json' not in str(reasons)


def test_profile_bad_fields_fallback_individually():
    fields, reasons = validate_profile(json.dumps(dict(intro=2, hobbies=['ok', None],
        recentWork='bad', tendency=False, summary={'private': 'text'})))
    assert fields.intro is None and fields.hobbies == () and fields.recent_work == ()
    assert fields.tendency is None and fields.summary is None
    assert len(reasons) == 5 and 'private' not in str(reasons)


def test_profile_missing_fields_are_empty():
    fields, reasons = validate_profile('{}')
    assert fields == fields.__class__()
    assert reasons == []


@pytest.mark.parametrize('changes', [dict(endDate='2026-02-30'), dict(endDate=2),
    dict(startTime=False), dict(summary=['invalid'])])
def test_invalid_optional_event_fields_drop_event(changes):
    events, reasons = run([event(**changes)])
    assert not events and reasons


def test_equal_end_time_same_day_is_removed():
    events, _ = run([event(startTime='10:00', endTime='10:00')])
    assert events[0].end_time is None


def test_event_provenance_comes_from_post_ignoring_ai_channel_and_timestamp():
    item = event(sourceChannel="forged-channel", source_channel="also-forged",
                 postedAt="2099-01-01T00:00:00+09:00", posted_at="forged-timestamp")
    events, reasons = run([item])
    assert not reasons
    assert events[0].source_channel == post().channel
    assert events[0].posted_at == post().posted_at
    serialized = events[0].model_dump(mode="json", by_alias=True)
    assert serialized["sourceChannel"] == "fiction"
    assert "forged" not in str(serialized)
