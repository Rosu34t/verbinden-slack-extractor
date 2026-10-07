from datetime import datetime

from verbinden.models import Post
from verbinden.stats import build_active_hours


def post(instant):
    return Post(id="1704067200.000001", channel="general", user_id="U123",
                user_name="架空の人", text="架空本文", posted_at=instant)


def test_empty_posts_returns_twenty_four_zeros():
    assert build_active_hours([]) == [0] * 24


def test_counts_jst_midnight_boundary_and_total():
    posts = [post(datetime.fromisoformat(value)) for value in [
        "2024-01-01T23:59:59+09:00", "2024-01-02T00:00:00+09:00",
        "2024-01-02T00:30:00+09:00", "2024-01-02T12:00:00+09:00",
    ]]
    result = build_active_hours(posts)
    assert len(result) == 24
    assert result[23] == 1
    assert result[0] == 2
    assert result[12] == 1
    assert sum(result) == len(posts)
    assert all(type(value) is int and value >= 0 for value in result)
