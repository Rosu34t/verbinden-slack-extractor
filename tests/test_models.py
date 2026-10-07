from datetime import date, datetime, time, timedelta, timezone

import pytest
from pydantic import ValidationError

from verbinden.models import ApiResponse, Event, LiveStatus, Post, Profile


JST = timezone(timedelta(hours=9))
STAMP = datetime(2026, 10, 5, 12, tzinfo=JST)


def post_data(**changes):
    return dict(id="123.000001", channel="general", user_id="U_FAKE",
                user_name="架空太郎", text="架空の投稿", posted_at=STAMP) | changes


def event_data(**changes):
    return dict(id="evt-123.000001", title="架空展示", date=date(2026, 10, 8),
                source_post_id="123.000001", source_channel="fiction", posted_at=STAMP) | changes


def profile_data(**changes):
    return dict(id="mem-U_FAKE", user_id="U_FAKE", display_name="架空太郎",
                hobbies=[], recent_work=[], active_hours=[0] * 24,
                post_count=0, updated_at=date(2026, 10, 5)) | changes


def test_post_trims_text_and_serializes_camel_case():
    post = Post(**post_data(text="  投稿 \n"))
    assert post.text == "投稿"
    assert post.model_dump(by_alias=True)["userId"] == "U_FAKE"
    assert "+09:00" in post.model_dump_json(by_alias=True)
    assert Post.model_validate(post.model_dump(by_alias=True)) == post


@pytest.mark.parametrize("changes", [dict(user_name=""), dict(user_name="  "),
    dict(text=" \n"), dict(posted_at=STAMP.replace(tzinfo=None)),
    dict(posted_at=STAMP.astimezone(timezone.utc))])
def test_post_rejects_invalid_values(changes):
    with pytest.raises(ValidationError):
        Post(**post_data(**changes))


@pytest.mark.parametrize("model,data", [(Post, post_data()), (Event, event_data()),
    (Profile, profile_data()), (LiveStatus, dict(presence="active")),
    (ApiResponse, dict(success=True, data=None, error=None))])
def test_models_are_frozen(model, data):
    instance = model(**data)
    key = next(iter(data))
    with pytest.raises(ValidationError):
        setattr(instance, key, data[key])


def test_event_accepts_multi_day_and_limits():
    event = Event(**event_data(title="あ" * 100, summary="あ" * 200,
        end_date=date(2026, 10, 11), start_time=time(9), end_time=time(17)))
    assert event.end_date == date(2026, 10, 11)
    assert event.end_time == time(17)
    assert "sourcePostId" in event.model_dump(by_alias=True)


@pytest.mark.parametrize("end", [time(8), time(9)])
def test_event_clears_non_increasing_end_time(end):
    assert Event(**event_data(start_time=time(9), end_time=end)).end_time is None


def test_event_keeps_end_time_without_start():
    assert Event(**event_data(end_time=time(17))).end_time == time(17)


def test_event_rejects_mixed_time_offsets():
    with pytest.raises(ValidationError):
        Event(**event_data(start_time=time(9, tzinfo=JST), end_time=time(17)))


@pytest.mark.parametrize("changes", [dict(title=""), dict(title=" "),
    dict(title="あ" * 101), dict(summary="あ" * 201), dict(id="bad"),
    dict(end_date=date(2026, 10, 7)), dict(posted_at=STAMP.replace(tzinfo=None))])
def test_event_rejects_invalid_values(changes):
    with pytest.raises(ValidationError):
        Event(**event_data(**changes))


def test_profile_accepts_limits_and_matching_histogram():
    profile = Profile(**profile_data(intro="あ" * 80, hobbies=["趣味"] * 5,
        recent_work=["制作"] * 5, active_hours=[1] * 24, post_count=24))
    assert sum(profile.active_hours) == profile.post_count
    assert Profile.model_validate(profile.model_dump(by_alias=True)) == profile


@pytest.mark.parametrize("changes", [dict(id="bad"), dict(display_name=" "),
    dict(intro="あ" * 81), dict(hobbies=["趣味"] * 6),
    dict(recent_work=["制作"] * 6), dict(active_hours=[0] * 23),
    dict(active_hours=[0] * 25), dict(active_hours=[-1] + [0] * 23),
    dict(active_hours=[0.5] + [0] * 23), dict(post_count=-1), dict(post_count=1)])
def test_profile_rejects_invalid_values(changes):
    with pytest.raises(ValidationError):
        Profile(**profile_data(**changes))


def test_live_status_and_generic_response():
    status = LiveStatus(emoji=":fake:", text="架空作業", presence="away")
    response = ApiResponse[LiveStatus](success=True, data=status)
    assert response.model_dump(by_alias=True) == {
        "success": True, "data": {"emoji": ":fake:", "text": "架空作業", "presence": "away"}, "error": None}
    assert ApiResponse[str](success=False, error="架空エラー").data is None
    with pytest.raises(ValidationError):
        LiveStatus(presence="offline")
    with pytest.raises(ValidationError):
        ApiResponse[LiveStatus](success=True, data="invalid")


@pytest.mark.parametrize("field", ["hobbies", "recent_work", "active_hours"])
def test_profile_collections_cannot_change_after_validation(field):
    data = profile_data()
    profile = Profile(**data)
    with pytest.raises(TypeError):
        getattr(profile, field)[0:0] = ("changed",)
    data[field].append("changed")
    assert "changed" not in getattr(profile, field)


def test_profile_serializes_immutable_collections_as_json_arrays():
    profile = Profile(**profile_data())
    data = profile.model_dump(mode="json", by_alias=True)
    assert isinstance(data["activeHours"], list)
    assert isinstance(data["hobbies"], list)
    assert isinstance(data["recentWork"], list)


@pytest.mark.parametrize("end", [time(8), time(18)])
def test_multi_day_event_keeps_non_increasing_end_time(end):
    event = Event(**event_data(end_date=date(2026, 10, 9), start_time=time(18), end_time=end))
    assert event.end_time == end
    assert Event.model_validate(event.model_dump(by_alias=True)).end_time == end


@pytest.mark.parametrize("end", [time(8), time(18)])
def test_explicit_same_day_event_clears_non_increasing_end_time(end):
    event = Event(**event_data(end_date=date(2026, 10, 8), start_time=time(18), end_time=end))
    assert event.end_time is None



def test_event_normalization_copies_camel_case_input():
    data = event_data(startTime="18:00", endTime="10:00", endDate="2026-10-08")
    event = Event.model_validate(data)
    assert event.end_time is None
    assert data["endTime"] == "10:00"
    assert Event.model_validate(event) == event


@pytest.mark.parametrize("changes", [
    {"date": "invalid"}, {"end_date": "invalid"},
    {"start_time": "invalid"}, {"end_time": "invalid"},
])
def test_event_normalization_preserves_field_validation(changes):
    with pytest.raises(ValidationError):
        Event(**(event_data(start_time=time(18), end_time=time(10)) | changes))


def test_event_source_channel_is_required_and_serializes_as_camel_case():
    data = event_data()
    assert Event(**data).model_dump(mode="json", by_alias=True)["sourceChannel"] == "fiction"
    del data["source_channel"]
    with pytest.raises(ValidationError):
        Event(**data)


def test_refresh_status_serializes_camel_case_and_is_frozen():
    from verbinden.models import RefreshStatus
    status = RefreshStatus(state="running", started_at=STAMP, finished_at=None,
                           next_available_at=STAMP + timedelta(minutes=10), message="更新中です")
    serialized = status.model_dump(mode="json", by_alias=True)
    assert serialized == {"state": "running", "startedAt": STAMP.isoformat(), "finishedAt": None,
                          "nextAvailableAt": (STAMP + timedelta(minutes=10)).isoformat(), "message": "更新中です"}
    assert RefreshStatus.model_validate(serialized) == status
    with pytest.raises(ValidationError):
        status.state = "failed"


@pytest.mark.parametrize("state", ["idle", "running", "succeeded", "failed"])
def test_refresh_status_accepts_all_declared_states(state):
    from verbinden.models import RefreshStatus
    assert RefreshStatus(state=state, started_at=None, finished_at=None,
                         next_available_at=None, message=None).state == state


def test_refresh_aliases_and_status_reject_unknown_values():
    from pydantic import TypeAdapter
    from verbinden.models import RefreshState, RefreshStatus, RefreshTarget
    assert TypeAdapter(RefreshTarget).validate_python("events") == "events"
    assert TypeAdapter(RefreshTarget).validate_python("members") == "members"
    with pytest.raises(ValidationError):
        TypeAdapter(RefreshTarget).validate_python("all")
    with pytest.raises(ValidationError):
        TypeAdapter(RefreshState).validate_python("pending")
    with pytest.raises(ValidationError):
        RefreshStatus(state="pending", started_at=None, finished_at=None,
                      next_available_at=None, message=None)
