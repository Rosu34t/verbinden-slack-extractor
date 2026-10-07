"""Offline integration of normalization, prompts, validation and presentation."""
import json
from datetime import date

from verbinden.models import Event, Profile
from verbinden.calendar_format import to_calendar_event
from verbinden.normalize import normalize_messages
from verbinden.prompts import build_event_prompt, build_profile_prompt
from verbinden.stats import build_active_hours
from verbinden.validate import validate_events, validate_profile
from verbinden.present import to_member_response


def test_human_posts_flow_through_extraction_and_member_presentation_without_services():
    raw = [{"ts": "1728300000.000001", "user": "U_FAKE", "text": "架空の制作を進めています"},
           {"ts": "1728300001.000001", "bot_id": "B_FAKE", "text": "Botの投稿"}]
    posts = normalize_messages(raw, "times-fiction", {"U_FAKE": "架空メンバー"})
    assert len(posts) == 1
    assert posts[0].id in build_profile_prompt("架空メンバー", posts, "架空メンバー")
    assert posts[0].id in build_event_prompt(posts, date(2026, 10, 5))
    llm_event_json = json.dumps({"events": [
        {"sourcePostId": posts[0].id, "title": "架空LT", "date": "2026-10-08", "sourceChannel": "forged"},
        {"sourcePostId": raw[1]["ts"], "title": "BotLT", "date": "2026-10-08"}]})
    events, reasons = validate_events(llm_event_json, posts)
    assert len(events) == 1 and len(reasons) == 1
    restored = Event.model_validate_json(events[0].model_dump_json(by_alias=True))
    calendar = to_calendar_event(restored)
    assert calendar["description"] == "（Slack #times-fiction の投稿より）"
    assert calendar["end"]["date"] == "2026-10-09"
    fields, reasons = validate_profile('{"intro":"制作が好きです","hobbies":["工作"]}')
    assert not reasons
    profile = Profile(id="mem-U_FAKE", user_id="U_FAKE", display_name="架空メンバー",
                      active_hours=build_active_hours(posts), post_count=len(posts),
                      updated_at=date(2026, 10, 5), **fields.model_dump())
    result = to_member_response(profile, None, {"name": "name", "intro": "intro", "status": "status"})
    assert result["intro"] == "制作が好きです"
    assert result["status"]["presence"] == "away"
    assert sum(profile.active_hours) == 1
