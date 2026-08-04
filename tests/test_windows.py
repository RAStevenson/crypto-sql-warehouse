"""Pagination arithmetic against the 300-candle cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from crypto_warehouse.windows import HOUR, MAX_CANDLES_PER_REQUEST, floor_hour, iter_windows

BASE = datetime(2026, 3, 14, 7, tzinfo=UTC)

SPANS = [
    timedelta(hours=1),
    timedelta(hours=299),
    timedelta(hours=300),
    timedelta(hours=301),
    timedelta(days=365),
]


def inclusive_bucket_count(window_start: datetime, window_end: datetime) -> int:
    """How many epoch-aligned hourly buckets the API would return for an
    inclusive [start, end] request. Computed here independently of the code
    under test on purpose."""
    first = window_start.replace(minute=0, second=0, microsecond=0)
    if window_start != first:  # a start mid-hour cannot claim its own bucket
        first += HOUR
    last = window_end.replace(minute=0, second=0, microsecond=0)
    if last < first:
        return 0
    return int((last - first) // HOUR) + 1


@pytest.mark.parametrize("span", SPANS, ids=lambda s: str(s))
@pytest.mark.parametrize("offset_minutes", range(60))
def test_iter_windows_never_exceeds_candle_cap(span, offset_minutes):
    start = BASE + timedelta(minutes=offset_minutes)
    current_hour = BASE + span

    windows = list(iter_windows(start, current_hour))

    for window_start, window_end in windows:
        assert (window_start.minute, window_start.second, window_start.microsecond) == (0, 0, 0)
        assert (window_end.minute, window_end.second, window_end.microsecond) == (0, 0, 0)
        assert window_end >= window_start
        count = inclusive_bucket_count(window_start, window_end)
        assert 1 <= count <= MAX_CANDLES_PER_REQUEST


@pytest.mark.parametrize("span", SPANS, ids=lambda s: str(s))
@pytest.mark.parametrize("offset_minutes", [0, 1, 37, 59])
def test_iter_windows_covers_range_without_gaps(span, offset_minutes):
    start = BASE + timedelta(minutes=offset_minutes)
    current_hour = BASE + span

    covered: list[datetime] = []
    for window_start, window_end in iter_windows(start, current_hour):
        bucket = window_start
        while bucket <= window_end:
            covered.append(bucket)
            bucket += HOUR

    expected = []
    bucket = floor_hour(start)
    while bucket < current_hour:
        expected.append(bucket)
        bucket += HOUR

    assert covered == expected  # ordered, complete, and no bucket fetched twice


def test_iter_windows_stops_before_the_forming_hour():
    current_hour = BASE + timedelta(hours=5)

    windows = list(iter_windows(BASE, current_hour))

    assert windows == [(BASE, current_hour - HOUR)]


def test_iter_windows_splits_at_the_cap():
    current_hour = BASE + timedelta(hours=301)

    windows = list(iter_windows(BASE, current_hour))

    assert len(windows) == 2
    assert windows[0] == (BASE, BASE + timedelta(hours=299))
    # Adjacent, not overlapping: the second window starts the hour after.
    assert windows[1] == (BASE + timedelta(hours=300), BASE + timedelta(hours=300))


@pytest.mark.parametrize(
    "start",
    [
        BASE,  # watermark exactly at the current hour
        BASE + timedelta(minutes=30),
        BASE + timedelta(hours=1),  # clock skew: watermark ahead of the clock
        BASE + timedelta(days=30),
    ],
)
def test_iter_windows_terminates_and_is_empty_past_the_current_hour(start):
    assert list(iter_windows(start, BASE)) == []


def test_iter_windows_terminates_on_a_long_backfill():
    current_hour = BASE + timedelta(days=365)

    windows = list(iter_windows(BASE, current_hour))

    assert len(windows) == 30  # 8760 hours / 300 per request, rounded up
    assert windows[-1][1] == current_hour - HOUR


def test_iter_windows_rejects_a_nonsense_cap():
    with pytest.raises(ValueError):
        list(iter_windows(BASE, BASE + timedelta(hours=5), max_candles=0))
