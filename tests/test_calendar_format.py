from datetime import date, datetime, time, timedelta
from copy import deepcopy

import pytest

from verbinden.models import Event
from verbinden.calendar_format import to_calendar_event


def event(**changes):
    data = dict(id="evt-1728300000.001234", title="架空展示", date=date(2026, 10, 8),
                source_post_id="1728300000.001234", source_channel="general", posted_at=datetime.fromisoformat("2026-10-05T12:00:00+09:00"))
    return Event(**{**data, **changes})


@pytest.mark.parametrize("changes,start,end", [
    ({}, {"date": "2026-10-08"}, {"date": "2026-10-09"}),
    ({"end_date": date(2026, 10, 11)}, {"date": "2026-10-08"}, {"date": "2026-10-12"}),
    ({"start_time": time(18), "end_time": time(19, 30)},
     {"dateTime": "2026-10-08T18:00:00+09:00", "timeZone": "Asia/Tokyo"},
     {"dateTime": "2026-10-08T19:30:00+09:00", "timeZone": "Asia/Tokyo"}),
    ({"start_time": time(23, 30)},
     {"dateTime": "2026-10-08T23:30:00+09:00", "timeZone": "Asia/Tokyo"},
     {"dateTime": "2026-10-09T00:30:00+09:00", "timeZone": "Asia/Tokyo"}),
    ({"end_date": date(2026, 10, 9), "start_time": time(18), "end_time": time(10)},
     {"dateTime": "2026-10-08T18:00:00+09:00", "timeZone": "Asia/Tokyo"},
     {"dateTime": "2026-10-09T10:00:00+09:00", "timeZone": "Asia/Tokyo"}),
])
def test_calendar_start_end_and_positive_duration(changes, start, end):
    result = to_calendar_event(event(**changes))
    assert result["start"] == start
    assert result["end"] == end
    key = "dateTime" if "dateTime" in start else "date"
    parse = datetime.fromisoformat if key == "dateTime" else date.fromisoformat
    assert parse(end[key]) > parse(start[key])


def test_conversion_is_deterministic_preserves_source_and_omits_absent_place():
    source = event()
    original = deepcopy(source)
    result = to_calendar_event(source)
    assert result == to_calendar_event(source)
    assert source == original
    assert result["id"] == "slk1728300000001234"
    assert result["summary"] == "架空展示"
    assert "location" not in result


def test_location_and_description_include_optional_summary():
    result = to_calendar_event(event(place="架空部室", summary="架空の紹介"))
    assert result["location"] == "架空部室"
    assert result["description"].startswith("架空の紹介\n")


def test_description_uses_source_channel_even_without_summary():
    result = to_calendar_event(event(source_channel="times-fiction"))
    assert result["description"] == "（Slack #times-fiction の投稿より）"


def test_description_places_summary_before_source_channel():
    result = to_calendar_event(event(summary="架空の紹介", source_channel="general"))
    assert result["description"] == "架空の紹介\n（Slack #general の投稿より）"
