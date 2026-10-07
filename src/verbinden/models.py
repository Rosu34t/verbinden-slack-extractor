"""Immutable data contracts shared by extraction and presentation."""

from collections.abc import Mapping
from datetime import date as Date, datetime, time, timedelta
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator
from pydantic.alias_generators import to_camel


T = TypeVar("T")
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
RefreshTarget = Literal["events", "members"]
RefreshState = Literal["idle", "running", "succeeded", "failed"]


class FrozenModel(BaseModel):
    """Keep Python field names and allow camelCase JSON round trips."""

    model_config = ConfigDict(frozen=True, alias_generator=to_camel,
                              populate_by_name=True)


def require_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def require_jst(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(hours=9):
        raise ValueError("datetime must include the JST (+09:00) offset")
    return value


class Post(FrozenModel):
    id: str
    channel: str
    user_id: str
    user_name: str
    text: str
    posted_at: datetime

    _name_not_blank = field_validator("user_name")(require_nonblank)
    _jst = field_validator("posted_at")(require_jst)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return require_nonblank(value.strip())


class Event(FrozenModel):
    id: str
    title: str = Field(max_length=100)
    date: Date
    end_date: Date | None = None
    start_time: time | None = None
    end_time: time | None = None
    place: str | None = None
    summary: str | None = Field(default=None, max_length=200)
    source_post_id: str
    source_channel: str
    posted_at: datetime

    _title_not_blank = field_validator("title")(require_nonblank)
    _jst = field_validator("posted_at")(require_jst)

    @model_validator(mode="after")
    def validate_event(self) -> "Event":
        # Existence of source_post_id is checked against input posts in validate.py.
        if self.id != "evt-" + self.source_post_id:
            raise ValueError("id must equal 'evt-' + source_post_id")
        if self.end_date is not None and self.end_date < self.date:
            raise ValueError("end_date must not precede date")
        return self

    @model_validator(mode="before")
    @classmethod
    def normalize_end_time(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        end_date = value.get("end_date", value.get("endDate"))
        start_time = value.get("start_time", value.get("startTime"))
        end_time = value.get("end_time", value.get("endTime"))
        if start_time is None or end_time is None:
            return value
        try:
            start_date = TypeAdapter(Date).validate_python(value.get("date"))
            final_date = TypeAdapter(Date).validate_python(end_date) if end_date is not None else start_date
            start = TypeAdapter(time).validate_python(start_time)
            end = TypeAdapter(time).validate_python(end_time)
        except ValidationError:
            return value  # Field validation supplies the precise invalid-field error.
        if final_date != start_date:
            return value
        try:
            if end <= start:
                key = "endTime" if "endTime" in value else "end_time"
                return {**value, key: None}
        except TypeError as exc:
            raise ValueError("start_time and end_time must have compatible offsets") from exc
        return value


class Profile(FrozenModel):
    id: str
    user_id: str
    display_name: str
    icon_url: str | None = None
    intro: str | None = Field(default=None, max_length=80)
    hobbies: tuple[str, ...] = Field(max_length=5)
    recent_work: tuple[str, ...] = Field(max_length=5)
    tendency: str | None = None
    summary: str | None = None
    active_hours: tuple[NonNegativeInt, ...] = Field(min_length=24, max_length=24)
    post_count: NonNegativeInt
    updated_at: Date

    _name_not_blank = field_validator("display_name")(require_nonblank)

    @model_validator(mode="after")
    def validate_profile(self) -> "Profile":
        if self.id != "mem-" + self.user_id:
            raise ValueError("id must equal 'mem-' + user_id")
        if sum(self.active_hours) != self.post_count:
            raise ValueError("active_hours total must equal post_count")
        return self


class LiveStatus(FrozenModel):
    emoji: str | None = None
    text: str | None = None
    presence: Literal["active", "away"]


class ApiResponse(FrozenModel, Generic[T]):
    success: bool
    data: T | None = None
    error: str | None = None


class RefreshStatus(FrozenModel):
    state: RefreshState
    started_at: datetime | None
    finished_at: datetime | None
    next_available_at: datetime | None
    message: str | None
