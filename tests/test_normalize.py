from copy import deepcopy
from datetime import timedelta

import pytest

from verbinden.normalize import normalize_messages, resolve_display_name


def message(**changes):
    return {"ts": "1704067200.123456", "user": "U123", "text": " 架空の投稿 ", **changes}


@pytest.mark.parametrize("changes", [
    {"bot_id": "B123"}, {"bot_id": ""}, {"subtype": "channel_join"},
    {"subtype": "channel_leave"}, {"subtype": "bot_message"},
    {"subtype": "message_changed"}, {"text": " \n\t "},
])
def test_filters_bots_notifications_and_blank_text(changes):
    assert normalize_messages([message(**changes)], "general", {}) == []


def test_normalizes_mentions_links_jst_and_preserves_input():
    raw = [message(text=" <@U123> <@U999> <https://example.invalid/x|架空ページ> <https://example.invalid> ")]
    before = deepcopy(raw)
    posts = normalize_messages(raw, "general", {"U123": "架空の人"})
    assert posts[0].text == "@架空の人 @U999 架空ページ https://example.invalid"
    assert posts[0].id == "1704067200.123456"
    assert posts[0].channel == "general"
    assert posts[0].user_id == "U123"
    assert posts[0].user_name == "架空の人"
    assert posts[0].posted_at.isoformat() == "2024-01-01T09:00:00.123456+09:00"
    assert raw == before


def test_keeps_broadcast_and_sorts_by_exact_timestamp():
    raw = [message(ts="1704067200.123457", subtype="thread_broadcast"),
           message(ts="1704067200.123456")]
    posts = normalize_messages(raw, "general", {})
    assert [post.id for post in posts] == ["1704067200.123456", "1704067200.123457"]
    assert posts[0].user_name == "U123"
    assert posts[0].posted_at.utcoffset() == timedelta(hours=9)


@pytest.mark.parametrize("user, expected", [
    ({"id": "U123", "profile": {"display_name": "架空表示", "real_name": "架空姓名"}}, "架空表示"),
    ({"id": "U123", "profile": {"display_name": " ", "real_name": "架空姓名"}}, "架空姓名"),
    ({"id": "U123", "profile": {"display_name": "", "real_name": ""}}, "U123"),
    ({"id": "U123"}, "U123"),
])
def test_resolves_first_nonblank_display_name(user, expected):
    assert resolve_display_name(user) == expected


def test_empty_messages_returns_empty_list():
    assert normalize_messages([], "general", {}) == []


@pytest.mark.parametrize("changes, field", [
    ({"ts": "not-a-timestamp"}, "ts"), ({"ts": "NaN"}, "ts"),
    ({"ts": None}, "ts"), ({"ts": "1e999"}, "ts"),
    ({"user": None}, "user"), ({"text": None}, "text"),
])
def test_rejects_malformed_human_post_with_field_name(changes, field):
    with pytest.raises(ValueError, match="message\\." + field):
        normalize_messages([message(**changes)], "general", {})


def test_missing_display_names_and_id_raises_clear_error():
    with pytest.raises(ValueError, match="user.id"):
        resolve_display_name({})


def test_file_share_keeps_text_and_ignores_file_contents():
    message = {"ts": "1728300000.000001", "user": "U_FAKE", "subtype": "file_share",
               "text": "架空の展示会の告知", "files": [{"name": "poster.png", "title": "画像内の告知"}]}
    result = normalize_messages([message], "general", {"U_FAKE": "架空メンバー"})
    assert len(result) == 1 and result[0].text == "架空の展示会の告知"
    assert "画像内" not in result[0].text


def test_file_share_without_text_is_removed_without_using_file_title():
    message = {"ts": "1728300000.000001", "user": "U_FAKE", "subtype": "file_share",
               "files": [{"name": "poster.png", "title": "文章の代わりには使わない"}]}
    assert normalize_messages([message], "general", {}) == []


def test_bot_file_share_is_removed_even_with_text():
    message = {"ts": "1728300000.000001", "user": "U_FAKE", "bot_id": "B_FAKE",
               "subtype": "file_share", "text": "Bot告知"}
    assert normalize_messages([message], "general", {}) == []
