from datetime import date, datetime, time

import pytest

from verbinden.models import Event
from verbinden.calendar_format import _calendar_bounds


@pytest.mark.parametrize("changes,start,end", [
    ({}, "2026-10-08", "2026-10-09"),
    ({"end_date": date(2026, 10, 11)}, "2026-10-08", "2026-10-12"),
    ({"start_time": time(18), "end_time": time(19)}, "2026-10-08T18:00:00+09:00", "2026-10-08T19:00:00+09:00"),
    ({"start_time": time(23, 30)}, "2026-10-08T23:30:00+09:00", "2026-10-09T00:30:00+09:00"),
    ({"end_date": date(2026, 10, 9), "start_time": time(18), "end_time": time(10)}, "2026-10-08T18:00:00+09:00", "2026-10-09T10:00:00+09:00"),
])
def test_calendar_time_bounds_follow_specification(changes, start, end):
    event = Event(id="evt-1728300000.000001", source_post_id="1728300000.000001", source_channel="general", title="架空展示", date=date(2026, 10, 8),
                  posted_at=datetime.fromisoformat("2026-10-05T12:00:00+09:00"), **changes)
    actual_start, actual_end = _calendar_bounds(event)
    key = "dateTime" if event.start_time else "date"
    assert actual_start[key] == start
    assert actual_end[key] == end
    assert actual_end[key] > actual_start[key]
    if event.start_time:
        assert actual_start["timeZone"] == actual_end["timeZone"] == "Asia/Tokyo"
