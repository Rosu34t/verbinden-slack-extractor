"""Pure Google Calendar time-bound conversion."""
from datetime import datetime, timedelta, timezone

from verbinden.models import Event

JST = timezone(timedelta(hours=9))


def _calendar_bounds(event: Event) -> tuple[dict, dict]:
    if event.start_time is None:
        return ({"date": event.date.isoformat()},
                {"date": ((event.end_date or event.date) + timedelta(days=1)).isoformat()})
    start = datetime.combine(event.date, event.start_time.replace(tzinfo=None), tzinfo=JST)
    end = (datetime.combine(event.end_date or event.date, event.end_time.replace(tzinfo=None), tzinfo=JST)
           if event.end_time is not None else start + timedelta(hours=1))
    if end <= start:
        raise ValueError("event.end: must follow event.start")
    return ({"dateTime": start.isoformat(), "timeZone": "Asia/Tokyo"},
            {"dateTime": end.isoformat(), "timeZone": "Asia/Tokyo"})


def to_calendar_event(event: Event) -> dict:
    """Convert a validated event using provenance supplied by the source post."""
    start, end = _calendar_bounds(event)
    provenance = f"（Slack #{event.source_channel} の投稿より）"
    description = f"{event.summary}\n{provenance}" if event.summary else provenance
    result = {
        "id": "slk" + event.source_post_id.replace(".", ""),
        "summary": event.title, "description": description,
        "start": start, "end": end,
    }
    return {**result, "location": event.place} if event.place is not None else result
