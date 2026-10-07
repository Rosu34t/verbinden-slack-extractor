"""Pure normalization of human Slack posts without network access."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

from verbinden.models import Post


JST = timezone(timedelta(hours=9))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MENTION = re.compile(r"<@([^>|]+)>")
LINK = re.compile(r"<(https?://[^>|]+)(?:\|([^>]*))?>")


def resolve_display_name(user: dict) -> str:
    """Choose the first nonblank display name, real name, or Slack ID."""
    profile = user.get("profile") or {}
    for value in (profile.get("display_name"), profile.get("real_name"), user.get("id")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("user.id: a nonblank ID is required")


def _posted_at(ts: str) -> datetime:
    # Decimal avoids rounding six-digit Slack fractions through a float.
    try:
        seconds = Decimal(ts)
        if not seconds.is_finite():
            raise ValueError
        microseconds = int(seconds * 1_000_000)
        return (EPOCH + timedelta(microseconds=microseconds)).astimezone(JST)
    except (InvalidOperation, ValueError, OverflowError, TypeError) as exc:
        raise ValueError("message.ts: valid Unix timestamp required") from exc


def _normalize_post(message: dict, channel: str, user_names: dict[str, str]) -> Post | None:
    if "bot_id" in message or message.get("subtype") not in (None, "thread_broadcast", "file_share"):
        return None
    text = message.get("text", "")
    if not isinstance(text, str):
        raise ValueError("message.text: string required")
    text = MENTION.sub(lambda match: "@" + (user_names.get(match[1]) or match[1]), text)
    text = LINK.sub(lambda match: match[2] if match[2] is not None else match[1], text).strip()
    if not text:
        return None
    user_id = message.get("user")
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("message.user: nonblank user ID required")
    ts = message.get("ts")
    if not isinstance(ts, str):
        raise ValueError("message.ts: string timestamp required")
    return Post(id=ts, channel=channel, user_id=user_id,
                user_name=user_names.get(user_id) or user_id,
                text=text, posted_at=_posted_at(ts))


def normalize_messages(raw_messages: list[dict], channel: str, user_names: dict[str, str]) -> list[Post]:
    """Filter system posts, clean text, and return posts oldest first."""
    posts = [_normalize_post(message, channel, user_names) for message in raw_messages]
    return sorted((post for post in posts if post is not None), key=lambda post: Decimal(post.id))
