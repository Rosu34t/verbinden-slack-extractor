from copy import deepcopy
from datetime import date

from verbinden.models import LiveStatus, Profile
from verbinden.present import select_fields, to_member_response


def profile():
    return Profile(id="mem-U_FAKE", user_id="U_FAKE", display_name="架空メンバー", icon_url=None,
                   intro="制作を楽しんでいます", hobbies=[], recent_work=[],
                   active_hours=[0] * 24, post_count=0, updated_at=date(2026, 10, 5))


def test_select_fields_renames_and_omits_unconfigured_fields_without_mutating_input():
    source = {"id": "1", "name": "架空メンバー", "intro": "紹介"}
    original = deepcopy(source)
    assert select_fields(source, {"id": "memberId", "name": "displayName"}) == {
        "memberId": "1", "displayName": "架空メンバー"}
    assert source == original


def test_missing_status_uses_away_and_null_fields():
    result = to_member_response(profile(), None, {"name": "name", "status": "status", "intro": "intro"})
    assert result == {"name": "架空メンバー", "intro": "制作を楽しんでいます",
                      "status": {"emoji": None, "text": None, "presence": "away"}}


def test_live_status_and_icon_are_exposed_with_configured_keys():
    result = to_member_response(profile(), LiveStatus(emoji="💻", text="制作中", presence="active"),
                                {"id": "id", "iconUrl": "avatar", "status": "current"})
    assert result == {"id": "mem-U_FAKE", "avatar": None,
                      "current": {"emoji": "💻", "text": "制作中", "presence": "active"}}


def test_empty_field_map_returns_empty_object():
    assert select_fields({"id": "1"}, {}) == {}
    assert to_member_response(profile(), None, {}) == {}
