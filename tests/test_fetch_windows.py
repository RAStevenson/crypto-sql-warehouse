"""Pagination arithmetic against the 300-candle cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from crypto_warehouse.fetch_windows import (
    MAX_CANDLES_PER_REQUEST,
    ONE_HOUR,
    floor_hour,
    iter_windows,
    longest_consecutive_run,
    missing_buckets,
)

BASE_HOUR = datetime(2026, 3, 14, 7, tzinfo=UTC)

# A session time zone with a half-hour offset, the case where flooring in
# place and flooring in UTC give different instants.
HALF_HOUR_ZONE = timezone(timedelta(hours=5, minutes=30))

WINDOW_SPANS = [
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
    first_bucket = window_start.replace(minute=0, second=0, microsecond=0)
    if window_start != first_bucket:  # a start mid-hour cannot claim its own bucket
        first_bucket += ONE_HOUR
    last_bucket = window_end.replace(minute=0, second=0, microsecond=0)
    if last_bucket < first_bucket:
        return 0
    return int((last_bucket - first_bucket) // ONE_HOUR) + 1


@pytest.mark.parametrize("span", WINDOW_SPANS, ids=lambda span: str(span))
@pytest.mark.parametrize("offset_minutes", range(60))
def test_iter_windows_never_exceeds_candle_cap(span, offset_minutes):
    start = BASE_HOUR + timedelta(minutes=offset_minutes)
    current_hour = BASE_HOUR + span

    windows = list(iter_windows(start, current_hour))

    for window_start, window_end in windows:
        assert (window_start.minute, window_start.second, window_start.microsecond) == (0, 0, 0)
        assert (window_end.minute, window_end.second, window_end.microsecond) == (0, 0, 0)
        assert window_end >= window_start
        count = inclusive_bucket_count(window_start, window_end)
        assert 1 <= count <= MAX_CANDLES_PER_REQUEST


@pytest.mark.parametrize("span", WINDOW_SPANS, ids=lambda span: str(span))
@pytest.mark.parametrize("offset_minutes", [0, 1, 37, 59])
def test_iter_windows_covers_range_without_gaps(span, offset_minutes):
    start = BASE_HOUR + timedelta(minutes=offset_minutes)
    current_hour = BASE_HOUR + span

    covered: list[datetime] = []
    for window_start, window_end in iter_windows(start, current_hour):
        bucket = window_start
        while bucket <= window_end:
            covered.append(bucket)
            bucket += ONE_HOUR

    expected = []
    bucket = floor_hour(start)
    while bucket < current_hour:
        expected.append(bucket)
        bucket += ONE_HOUR

    assert covered == expected  # ordered, complete, and no bucket fetched twice


def test_iter_windows_stops_before_the_forming_hour():
    current_hour = BASE_HOUR + timedelta(hours=5)

    windows = list(iter_windows(BASE_HOUR, current_hour))

    assert windows == [(BASE_HOUR, current_hour - ONE_HOUR)]


def test_iter_windows_splits_at_the_cap():
    current_hour = BASE_HOUR + timedelta(hours=301)

    windows = list(iter_windows(BASE_HOUR, current_hour))

    assert len(windows) == 2
    assert windows[0] == (BASE_HOUR, BASE_HOUR + timedelta(hours=299))
    # Adjacent, not overlapping: the second window starts the hour after.
    assert windows[1] == (BASE_HOUR + timedelta(hours=300), BASE_HOUR + timedelta(hours=300))


@pytest.mark.parametrize(
    "start",
    [
        BASE_HOUR,  # watermark exactly at the current hour
        BASE_HOUR + timedelta(minutes=30),
        BASE_HOUR + timedelta(hours=1),  # clock skew: watermark ahead of the clock
        BASE_HOUR + timedelta(days=30),
    ],
)
def test_iter_windows_terminates_and_is_empty_past_the_current_hour(start):
    assert list(iter_windows(start, BASE_HOUR)) == []


def test_iter_windows_terminates_on_a_long_backfill():
    current_hour = BASE_HOUR + timedelta(days=365)

    windows = list(iter_windows(BASE_HOUR, current_hour))

    assert len(windows) == 30  # 8760 hours / 300 per request, rounded up
    assert windows[-1][1] == current_hour - ONE_HOUR


def test_iter_windows_rejects_a_nonsense_cap():
    with pytest.raises(ValueError):
        list(iter_windows(BASE_HOUR, BASE_HOUR + timedelta(hours=5), max_candles=0))


def test_floor_hour_converts_an_aware_datetime_to_utc_before_truncating():
    moment = datetime(2026, 10, 3, 13, 30, tzinfo=HALF_HOUR_ZONE)

    floored_hour = floor_hour(moment)

    # 13:30+05:30 is 08:00 UTC. Truncating in place would give 13:00+05:30,
    # which is 07:30 UTC and not a bucket.
    assert floored_hour == datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
    assert floored_hour.tzinfo is UTC


def test_iter_windows_from_a_half_hour_offset_start_tiles_on_the_utc_hour():
    start = datetime(2026, 10, 3, 13, 30, tzinfo=HALF_HOUR_ZONE)
    first_bucket = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
    current_hour = first_bucket + timedelta(hours=301)

    windows = list(iter_windows(start, current_hour))

    assert len(windows) == 2  # the tiling is checked across a window boundary
    covered: list[datetime] = []
    for window_start, window_end in windows:
        for bound in (window_start, window_end):
            bound_in_utc = bound.astimezone(UTC)
            assert (bound_in_utc.minute, bound_in_utc.second, bound_in_utc.microsecond) == (0, 0, 0)
        bucket = window_start
        while bucket <= window_end:
            covered.append(bucket)
            bucket += ONE_HOUR

    assert covered == [first_bucket + i * ONE_HOUR for i in range(301)]


# --------------------------------------------------------------------------
# missing_buckets and longest_consecutive_run, the two halves of the
# short-window check in the loader.
# --------------------------------------------------------------------------
def test_missing_buckets_returns_exactly_the_absent_hours_of_an_inclusive_window_in_order():
    window_end = BASE_HOUR + timedelta(hours=5)
    # The first and last buckets are among the absent ones, so an exclusive
    # bound at either end would change the answer.
    present = {
        BASE_HOUR + timedelta(hours=1),
        BASE_HOUR + timedelta(hours=2),
        BASE_HOUR + timedelta(hours=4),
    }

    missing = missing_buckets(BASE_HOUR, window_end, present)

    assert missing == [BASE_HOUR, BASE_HOUR + timedelta(hours=3), BASE_HOUR + timedelta(hours=5)]


def test_missing_buckets_ignores_present_buckets_outside_the_window():
    window_end = BASE_HOUR + timedelta(hours=2)
    present = {
        BASE_HOUR - ONE_HOUR,  # before the window
        BASE_HOUR + timedelta(hours=1),
        BASE_HOUR + timedelta(hours=3),  # after the window
        BASE_HOUR + timedelta(days=9),
    }

    missing = missing_buckets(BASE_HOUR, window_end, present)

    assert missing == [BASE_HOUR, BASE_HOUR + timedelta(hours=2)]


def test_missing_buckets_is_empty_when_every_bucket_is_present():
    window_end = BASE_HOUR + timedelta(hours=3)
    present = [BASE_HOUR + i * ONE_HOUR for i in range(4)]

    assert missing_buckets(BASE_HOUR, window_end, present) == []


def test_longest_consecutive_run_of_an_empty_list_is_zero():
    assert longest_consecutive_run([]) == 0


def test_longest_consecutive_run_of_isolated_hours_is_one():
    isolated = [BASE_HOUR, BASE_HOUR + timedelta(hours=2), BASE_HOUR + timedelta(hours=7)]

    assert longest_consecutive_run(isolated) == 1


def test_longest_consecutive_run_finds_a_six_hour_run_among_scattered_single_holes():
    # Single holes before and after the run, so neither the first nor the last
    # run in the list is the longest.
    buckets = [
        BASE_HOUR,
        BASE_HOUR + timedelta(hours=3),
        *(BASE_HOUR + timedelta(hours=10 + i) for i in range(6)),
        BASE_HOUR + timedelta(hours=20),
        BASE_HOUR + timedelta(hours=40),
    ]

    assert longest_consecutive_run(buckets) == 6
