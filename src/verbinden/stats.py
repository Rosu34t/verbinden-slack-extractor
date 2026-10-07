"""Deterministic activity statistics in JST."""

from datetime import timedelta, timezone

from verbinden.models import Post


JST = timezone(timedelta(hours=9))


def build_active_hours(posts: list[Post]) -> list[int]:
    """Count posts by JST hour with one independent bin per hour."""
    hours = tuple(post.posted_at.astimezone(JST).hour for post in posts)
    return [hours.count(hour) for hour in range(24)]
