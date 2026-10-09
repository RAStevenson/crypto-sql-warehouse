"""Pagination arithmetic against the 300-candle cap, at every supported timeframe."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from crypto_warehouse.fetch_windows import (
    HOURLY_GRANULARITY_SECONDS,
    MAX_CANDLES_PER_REQUEST,
    bucket_count,
    floor_to_bucket,
    iter_windows,
    longest_consecutive_run,
    missing_buckets,
)

ONE_HOUR = timedelta(hours=1)
BASE_HOUR = datetime(2026, 3, 14, 7, tzinfo=UTC)
SUPPORTED_GRANULARITIES = [60, 300, 900, 3600]

# A session time zone with a half-hour offset, the case where flooring in
# place and flooring in UTC give different instants.
HALF_HOUR_ZONE = timezone(timedelta(hours=5, minutes=30))

# Window lengths in buckets, around the cap and well past it.
SPANS_IN_BUCKETS = [1, 299, 300, 301, 1000]


def inclusive_bucket_count(
    window_start: datetime, window_end: datetime, granularity_seconds: int
) -> int:
    """How many epoch-aligned buckets the API would return for an inclusive
    [start, end] request. Computed here independently of the code under test
    on purpose, from seconds since the epoch."""
    start_seconds = int(window_start.timestamp())
    end_seconds = int(window_end.timestamp())
    # A start mid-bucket cannot claim its own bucket.
    first_bucket = -(-start_seconds // granularity_seconds) * granularity_seconds
    last_bucket = end_seconds // granularity_seconds * granularity_seconds
    if last_bucket < first_bucket:
        return 0
    return (last_bucket - first_bucket) // granularity_seconds + 1


def is_on_bucket_boundary(moment: datetime, granularity_seconds: int) -> bool:
    """True when the moment is the start of a bucket, judged in UTC epoch seconds."""
    return moment.timestamp() % granularity_seconds == 0


@pytest.mark.parametrize("granularity_seconds", SUPPORTED_GRANULARITIES)
@pytest.mark.parametrize("span_buckets", SPANS_IN_BUCKETS)
@pytest.mark.parametrize("offset_minutes", range(60))
def test_iter_windows_never_exceeds_candle_cap(granularity_seconds, span_buckets, offset_minutes):
    start = BASE_HOUR + timedelta(minutes=offset_minutes)
    current_time = BASE_HOUR + span_buckets * timedelta(seconds=granularity_seconds)

    windows = list(iter_windows(start, current_time, granularity_seconds))

    for window_start, window_end in windows:
        assert is_on_bucket_boundary(window_start, granularity_seconds)
        assert is_on_bucket_boundary(window_end, granularity_seconds)
        assert window_end >= window_start
        count = inclusive_bucket_count(window_start, window_end, granularity_seconds)
        assert 1 <= count <= MAX_CANDLES_PER_REQUEST


@pytest.mark.parametrize("granularity_seconds", SUPPORTED_GRANULARITIES)
@pytest.mark.parametrize("span_buckets", SPANS_IN_BUCKETS)
@pytest.mark.parametrize("offset_minutes", [0, 1, 4, 14, 37, 59])
def test_iter_windows_covers_range_without_gaps(granularity_seconds, span_buckets, offset_minutes):
    width = timedelta(seconds=granularity_seconds)
    start = BASE_HOUR + timedelta(minutes=offset_minutes)
    current_time = BASE_HOUR + span_buckets * width

    covered: list[datetime] = []
    for window_start, window_end in iter_windows(start, current_time, granularity_seconds):
        bucket = window_start
        while bucket <= window_end:
            covered.append(bucket)
            bucket += width

    expected = []
    bucket = floor_to_bucket(start, granularity_seconds)
    while bucket + width <= current_time:
        expected.append(bucket)
        bucket += width

    assert covered == expected  # ordered, complete, and no bucket fetched twice


@pytest.mark.parametrize("granularity_seconds", SUPPORTED_GRANULARITIES)
def test_iter_windows_stops_before_the_forming_bucket(granularity_seconds):
    width = timedelta(seconds=granularity_seconds)
    # Mid-bucket: the bucket holding current_time is still forming.
    current_time = BASE_HOUR + 5 * width + width / 2

    windows = list(iter_windows(BASE_HOUR, current_time, granularity_seconds))

    assert windows == [(BASE_HOUR, BASE_HOUR + 4 * width)]


@pytest.mark.parametrize("granularity_seconds", SUPPORTED_GRANULARITIES)
def test_iter_windows_splits_at_the_cap(granularity_seconds):
    width = timedelta(seconds=granularity_seconds)
    current_time = BASE_HOUR + 301 * width

    windows = list(iter_windows(BASE_HOUR, current_time, granularity_seconds))

    assert len(windows) == 2
    assert windows[0] == (BASE_HOUR, BASE_HOUR + 299 * width)
    # Adjacent, not overlapping: the second window starts the bucket after.
    assert windows[1] == (BASE_HOUR + 300 * width, BASE_HOUR + 300 * width)


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
    assert list(iter_windows(start, BASE_HOUR, HOURLY_GRANULARITY_SECONDS)) == []


def test_iter_windows_terminates_on_a_long_backfill():
    current_hour = BASE_HOUR + timedelta(days=365)

    windows = list(iter_windows(BASE_HOUR, current_hour, HOURLY_GRANULARITY_SECONDS))

    assert len(windows) == 30  # 8760 hours / 300 per request, rounded up
    assert windows[-1][1] == current_hour - ONE_HOUR


def test_iter_windows_of_a_five_minute_year_takes_the_expected_request_count():
    current_hour = BASE_HOUR + timedelta(days=365)

    windows = list(iter_windows(BASE_HOUR, current_hour, 300))

    assert len(windows) == 351  # 105,120 buckets / 300 per request, rounded up


def test_iter_windows_rejects_a_nonsense_cap():
    with pytest.raises(ValueError):
        list(
            iter_windows(
                BASE_HOUR, BASE_HOUR + timedelta(hours=5), HOURLY_GRANULARITY_SECONDS, max_candles=0
            )
        )


@pytest.mark.parametrize("granularity_seconds", [0, 30, 90, 420, 7200, 21600])
def test_an_unsupported_granularity_is_rejected(granularity_seconds):
    with pytest.raises(ValueError, match="divide an hour"):
        floor_to_bucket(BASE_HOUR, granularity_seconds)


def test_floor_to_bucket_converts_an_aware_datetime_to_utc_before_truncating():
    moment = datetime(2026, 10, 3, 13, 30, tzinfo=HALF_HOUR_ZONE)

    floored_hour = floor_to_bucket(moment, HOURLY_GRANULARITY_SECONDS)

    # 13:30+05:30 is 08:00 UTC. Truncating in place would give 13:00+05:30,
    # which is 07:30 UTC and not a bucket.
    assert floored_hour == datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
    assert floored_hour.tzinfo is UTC


@pytest.mark.parametrize(
    ("granularity_seconds", "expected_minute"),
    [(60, 47), (300, 45), (900, 45), (3600, 0)],
)
def test_floor_to_bucket_truncates_to_the_bucket_start(granularity_seconds, expected_minute):
    moment = datetime(2026, 10, 3, 13, 47, 59, 999999, tzinfo=UTC)

    assert floor_to_bucket(moment, granularity_seconds) == datetime(
        2026, 10, 3, 13, expected_minute, tzinfo=UTC
    )


def test_iter_windows_from_a_half_hour_offset_start_tiles_on_the_utc_hour():
    start = datetime(2026, 10, 3, 13, 30, tzinfo=HALF_HOUR_ZONE)
    first_bucket = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
    current_hour = first_bucket + timedelta(hours=301)

    windows = list(iter_windows(start, current_hour, HOURLY_GRANULARITY_SECONDS))

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

    assert covered == [first_bucket + index * ONE_HOUR for index in range(301)]


def test_bucket_count_counts_both_bounds():
    assert bucket_count(BASE_HOUR, BASE_HOUR + timedelta(minutes=55), 300) == 12
    assert bucket_count(BASE_HOUR, BASE_HOUR, 300) == 1
    assert bucket_count(BASE_HOUR, BASE_HOUR - ONE_HOUR, 300) == 0


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

    missing = missing_buckets(BASE_HOUR, window_end, present, HOURLY_GRANULARITY_SECONDS)

    assert missing == [BASE_HOUR, BASE_HOUR + timedelta(hours=3), BASE_HOUR + timedelta(hours=5)]


def test_missing_buckets_steps_by_the_granularity():
    window_end = BASE_HOUR + timedelta(minutes=45)
    present = {BASE_HOUR, BASE_HOUR + timedelta(minutes=30)}

    missing = missing_buckets(BASE_HOUR, window_end, present, 900)

    assert missing == [BASE_HOUR + timedelta(minutes=15), BASE_HOUR + timedelta(minutes=45)]


def test_missing_buckets_ignores_present_buckets_outside_the_window():
    window_end = BASE_HOUR + timedelta(hours=2)
    present = {
        BASE_HOUR - ONE_HOUR,  # before the window
        BASE_HOUR + timedelta(hours=1),
        BASE_HOUR + timedelta(hours=3),  # after the window
        BASE_HOUR + timedelta(days=9),
    }

    missing = missing_buckets(BASE_HOUR, window_end, present, HOURLY_GRANULARITY_SECONDS)

    assert missing == [BASE_HOUR, BASE_HOUR + timedelta(hours=2)]


def test_missing_buckets_is_empty_when_every_bucket_is_present():
    window_end = BASE_HOUR + timedelta(hours=3)
    present = [BASE_HOUR + index * ONE_HOUR for index in range(4)]

    assert missing_buckets(BASE_HOUR, window_end, present, HOURLY_GRANULARITY_SECONDS) == []


def test_longest_consecutive_run_of_an_empty_list_is_zero():
    assert longest_consecutive_run([], HOURLY_GRANULARITY_SECONDS) == 0


def test_longest_consecutive_run_of_isolated_hours_is_one():
    isolated = [BASE_HOUR, BASE_HOUR + timedelta(hours=2), BASE_HOUR + timedelta(hours=7)]

    assert longest_consecutive_run(isolated, HOURLY_GRANULARITY_SECONDS) == 1


def test_longest_consecutive_run_finds_a_six_hour_run_among_scattered_single_holes():
    # Single holes before and after the run, so neither the first nor the last
    # run in the list is the longest.
    buckets = [
        BASE_HOUR,
        BASE_HOUR + timedelta(hours=3),
        *(BASE_HOUR + timedelta(hours=10 + index) for index in range(6)),
        BASE_HOUR + timedelta(hours=20),
        BASE_HOUR + timedelta(hours=40),
    ]

    assert longest_consecutive_run(buckets, HOURLY_GRANULARITY_SECONDS) == 6


def test_longest_consecutive_run_counts_in_buckets_of_the_given_width():
    # Back to back at 5 minutes, but not at an hour.
    buckets = [BASE_HOUR + index * timedelta(minutes=5) for index in range(4)]

    assert longest_consecutive_run(buckets, 300) == 4
    assert longest_consecutive_run(buckets, HOURLY_GRANULARITY_SECONDS) == 1
