"""Window arithmetic for the 300-candle-per-request cap.

Pure functions, no clock and no network, because the pagination is the part
of the pipeline where an off-by-one silently drops or duplicates a bucket and
the only cheap way to be sure is to test every start offset exhaustively.

Every function takes the timeframe as granularity_seconds, the bucket width
the Coinbase candles endpoint takes. The warehouse supports widths that
divide an hour into whole minutes (60, 300, 900, 3600), so a bucket always
starts on a whole minute and every hour boundary is also a bucket boundary.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import UTC, datetime, timedelta

HOURLY_GRANULARITY_SECONDS = 3600

# The candles endpoint refuses a request whose start/end span more than this
# many buckets. Both bounds are inclusive, so 300 buckets is a span of 299
# bucket widths.
MAX_CANDLES_PER_REQUEST = 300


def bucket_width(granularity_seconds: int) -> timedelta:
    """Return one bucket as a timedelta, raising ValueError for a width that
    does not divide an hour into whole minutes."""
    if (
        granularity_seconds < 60
        or granularity_seconds % 60
        or HOURLY_GRANULARITY_SECONDS % granularity_seconds
    ):
        raise ValueError(
            f"granularity_seconds must divide an hour into whole minutes, got {granularity_seconds}"
        )
    return timedelta(seconds=granularity_seconds)


def floor_to_bucket(moment: datetime, granularity_seconds: int) -> datetime:
    """Return the moment truncated to the start of its UTC bucket.

    A naive datetime is truncated as given, with no conversion.
    """
    bucket_minutes = bucket_width(granularity_seconds) // timedelta(minutes=1)
    # Converted before truncating because psycopg2 returns timestamps in the
    # session's zone, and flooring 10:45+05:30 to the hour in place gives
    # 10:00+05:30, which is 04:30 UTC and not a bucket.
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.replace(
        minute=moment.minute - moment.minute % bucket_minutes, second=0, microsecond=0
    )


def bucket_count(window_start: datetime, window_end: datetime, granularity_seconds: int) -> int:
    """How many buckets a window asks for, both bounds inclusive."""
    if window_end < window_start:
        return 0
    return int((window_end - window_start) // bucket_width(granularity_seconds)) + 1


def missing_buckets(
    window_start: datetime,
    window_end: datetime,
    present: Collection[datetime],
    granularity_seconds: int,
) -> list[datetime]:
    """The buckets of the inclusive window that present lacks, in time order."""
    width = bucket_width(granularity_seconds)
    held = set(present)
    wanted = (
        window_start + index * width
        for index in range(bucket_count(window_start, window_end, granularity_seconds))
    )
    return [bucket for bucket in wanted if bucket not in held]


def longest_consecutive_run(buckets: list[datetime], granularity_seconds: int) -> int:
    """Length of the longest run of back-to-back buckets in a time-ordered bucket list."""
    width = bucket_width(granularity_seconds)
    longest = current_run = 0
    previous = None
    for bucket in buckets:
        current_run = current_run + 1 if previous is not None and bucket - previous == width else 1
        longest = max(longest, current_run)
        previous = bucket
    return longest


def windows_covering(
    buckets: list[datetime],
    granularity_seconds: int,
    max_candles: int = MAX_CANDLES_PER_REQUEST,
) -> list[tuple[datetime, datetime]]:
    """Return inclusive (window_start, window_end) pairs, in time order, that
    together cover every bucket in buckets and nothing past the last one.

    Each window starts at the earliest bucket not yet covered and ends at the
    last bucket that fits within max_candles of it, so scattered buckets
    share a request when they are close and get their own when they are not.
    """
    if max_candles < 1:
        raise ValueError(f"max_candles must be at least 1, got {max_candles}")
    span = (max_candles - 1) * bucket_width(granularity_seconds)
    windows: list[tuple[datetime, datetime]] = []
    for bucket in sorted(buckets):
        if windows and bucket <= windows[-1][0] + span:
            windows[-1] = (windows[-1][0], bucket)
        else:
            windows.append((bucket, bucket))
    return windows


def iter_windows(
    start: datetime,
    current_time: datetime,
    granularity_seconds: int,
    max_candles: int = MAX_CANDLES_PER_REQUEST,
) -> Iterator[tuple[datetime, datetime]]:
    """Yield inclusive (window_start, window_end) pairs that together cover every
    bucket from the one holding start up to but not including the bucket
    still forming at current_time.

    Consecutive windows share no bucket, each holds at most max_candles
    buckets, and nothing is yielded when start is at or after the forming
    bucket.
    """
    if max_candles < 1:
        raise ValueError(f"max_candles must be at least 1, got {max_candles}")
    width = bucket_width(granularity_seconds)

    # The start is floored, not ceiled, because flooring can request one
    # bucket already held, which the upsert absorbs, while a ceiling would
    # skip one.
    window_start = floor_to_bucket(start, granularity_seconds)
    # The bucket holding current_time is still forming, so the last bucket
    # requested is the one before it.
    last_bucket = floor_to_bucket(current_time, granularity_seconds) - width
    span = (max_candles - 1) * width

    while window_start <= last_bucket:
        window_end = min(window_start + span, last_bucket)
        yield window_start, window_end
        window_start = window_end + width
