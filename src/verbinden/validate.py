"""Validate untrusted LLM JSON without exposing generated text in diagnostics."""

import json
import re
from datetime import date, time

from pydantic import ValidationError

from .models import Event, FrozenModel, Post


class ProfileFields(FrozenModel):
    intro: str | None = None
    hobbies: tuple[str, ...] = ()
    recent_work: tuple[str, ...] = ()
    tendency: str | None = None
    summary: str | None = None


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('must be a string or null')
    return value.strip() or None


def _date(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('must use YYYY-MM-DD')
    return date.fromisoformat(value)


def _time(value: object) -> time | None:
    value = _optional_string(value)
    if value is None:
        return None
    if not re.fullmatch(r'[0-9]{2}:[0-9]{2}', value):
        raise ValueError('must use HH:MM')
    return time.fromisoformat(value)


def _event(item: object, sources: dict[str, Post]) -> Event:
    if not isinstance(item, dict):
        raise ValueError('event must be an object')
    source = item.get('sourcePostId')
    if not isinstance(source, str) or source not in sources:
        raise ValueError('sourcePostId must refer to an input post')
    title = _optional_string(item.get('title'))
    if title is None:
        raise ValueError('title must not be empty')
    start_date = _date(item.get('date'))
    end_date_value = _optional_string(item.get('endDate'))
    end_date = _date(end_date_value) if end_date_value else None
    if end_date is not None and end_date < start_date:
        end_date = None
    start_time, end_time = _time(item.get('startTime')), _time(item.get('endTime'))
    if (end_date is None or end_date == start_date) and start_time is not None and end_time is not None:
        if end_time <= start_time:
            end_time = None
    summary = _optional_string(item.get('summary'))
    return Event(id='evt-' + source, source_post_id=source,
                 source_channel=sources[source].channel, posted_at=sources[source].posted_at,
                 title=title[:100], date=start_date, end_date=end_date,
                 start_time=start_time, end_time=end_time,
                 place=_optional_string(item.get('place')), summary=summary[:200] if summary else None)


def validate_events(llm_output: str, posts: list[Post]) -> tuple[list[Event], list[str]]:
    try:
        data = json.loads(llm_output)
    except (ValueError, TypeError, RecursionError):
        return [], ['events: invalid JSON']
    if not isinstance(data, dict) or not isinstance(data.get('events'), list):
        return [], ['events: expected an array in a JSON object']
    sources = {post.id: post for post in posts}
    accepted: dict[tuple[str, date], Event] = {}
    reasons: list[str] = []
    for index, item in enumerate(data['events']):
        try:
            event = _event(item, sources)
        except ValidationError:
            reasons.append(f'events[{index}]: invalid event fields')
            continue
        except (ValueError, TypeError):
            # Error messages from date parsers can contain raw values; never log them.
            reasons.append(f'events[{index}]: invalid source or field format')
            continue
        key = (event.title, event.date)
        previous = accepted.get(key)
        if previous is not None:
            reasons.append(f'events[{index}]: duplicate title and date')
        if previous is None or event.posted_at < previous.posted_at:
            accepted = {**accepted, key: event}
    return list(accepted.values()), reasons


def _profile_array(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError('expected string array')
    return tuple(item.strip() for item in value if item.strip())[:5]


def validate_profile(llm_output: str) -> tuple[ProfileFields, list[str]]:
    try:
        data = json.loads(llm_output)
    except (ValueError, TypeError, RecursionError):
        return ProfileFields(), ['profile: invalid JSON']
    if not isinstance(data, dict):
        return ProfileFields(), ['profile: expected a JSON object']
    values: dict = {}
    reasons: list[str] = []
    for key in ('intro', 'hobbies', 'recentWork', 'tendency', 'summary'):
        array = key in ('hobbies', 'recentWork')
        try:
            value = _profile_array(data.get(key)) if array else _optional_string(data.get(key))
            values = {**values, key: value[:80] if key == 'intro' and value else value}
        except ValueError:
            values = {**values, key: () if array else None}
            reasons.append(f'profile.{key}: invalid field type')
    return ProfileFields.model_validate(values), reasons
