"""Window arithmetic for the 300-candle-per-request cap.

Pure functions, no clock and no network, because the pagination is the part
of the pipeline where an off-by-one silently drops or duplicates an hour and
the only cheap way to be sure is to test every start offset exhaustively.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import UTC, datetime, timedelta

HOUR = timedelta(hours=1)

# The candles endpoint refuses a request whose start/end span more than this
# many buckets. Both bounds are inclusive, so 300 buckets is a 299-hour span.
MAX_CANDLES_PER_REQUEST = 300


def floor_hour(moment: datetime) -> datetime:
    """Return the moment truncated to the top of its UTC hour.

    A naive datetime is truncated as given, with no conversion.
    """
    # Converted before truncating because psycopg2 returns timestamps in the
    # session's zone, and flooring 10:45+05:30 in place gives 10:00+05:30,
    # which is 04:30 UTC and not a bucket.
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.replace(minute=0, second=0, microsecond=0)


def bucket_count(window_start: datetime, window_end: datetime) -> int:
    """How many hourly buckets a window asks for, both bounds inclusive."""
    if window_end < window_start:
        return 0
    return int((window_end - window_start) // HOUR) + 1


def missing_buckets(
    window_start: datetime, window_end: datetime, present: Collection[datetime]
) -> list[datetime]:
    """The hourly buckets of the inclusive window that present lacks, in time order."""
    held = set(present)
    wanted = (window_start + i * HOUR for i in range(bucket_count(window_start, window_end)))
    return [bucket for bucket in wanted if bucket not in held]


def longest_consecutive_run(buckets: list[datetime]) -> int:
    """Length of the longest run of back-to-back hours in a time-ordered bucket list."""
    longest = current_run = 0
    previous = None
    for bucket in buckets:
        current_run = current_run + 1 if previous is not None and bucket - previous == HOUR else 1
        longest = max(longest, current_run)
        previous = bucket
    return longest


def iter_windows(
    start: datetime,
    current_hour: datetime,
    max_candles: int = MAX_CANDLES_PER_REQUEST,
) -> Iterator[tuple[datetime, datetime]]:
    """Yield inclusive (window_start, window_end) pairs that together cover every
    hourly bucket from floor_hour(start) up to but not including current_hour.

    Consecutive windows share no bucket, each holds at most max_candles
    buckets, and nothing is yielded when start is at or after current_hour.
    """
    if max_candles < 1:
        raise ValueError(f"max_candles must be at least 1, got {max_candles}")

    # The start is floored, not ceiled, because flooring can request one
    # bucket already held, which the upsert absorbs, while a ceiling would
    # skip an hour.
    window_start = floor_hour(start)
    # current_hour is still forming, so the last bucket requested is the hour
    # before it.
    last_bucket = floor_hour(current_hour) - HOUR
    span = (max_candles - 1) * HOUR

    while window_start <= last_bucket:
        window_end = min(window_start + span, last_bucket)
        yield window_start, window_end
        window_start = window_end + HOUR
