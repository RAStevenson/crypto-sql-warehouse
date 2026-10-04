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
    """Truncate to the top of the UTC hour. A naive datetime is truncated as given.

    The conversion to UTC matters for a session time zone with a half-hour
    offset: psycopg2 hands back timestamps in the session's zone, and flooring
    10:45+05:30 in place gives 10:00+05:30, which is 04:30 UTC, not a bucket.
    """
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
    longest = run = 0
    previous = None
    for bucket in buckets:
        run = run + 1 if previous is not None and bucket - previous == HOUR else 1
        longest = max(longest, run)
        previous = bucket
    return longest


def iter_windows(
    start: datetime,
    current_hour: datetime,
    max_candles: int = MAX_CANDLES_PER_REQUEST,
) -> Iterator[tuple[datetime, datetime]]:
    """Yield (window_start, window_end) pairs covering every hourly bucket in
    [floor_hour(start), current_hour).

    Both bounds are inclusive, matching the API: a window from 00:00 to 02:00
    asks for three candles. A window therefore spans at most max_candles - 1
    hours, and consecutive windows are adjacent rather than overlapping - the
    next one begins on the hour after the previous one ended. The old code
    overlapped by one bucket and relied on the upsert to absorb the duplicate;
    exact tiling is easier to reason about and one fewer row per window.

    current_hour is the still-forming bucket and is never requested, so the
    last bucket any window can cover is current_hour - 1h. That also makes the
    generator empty when the watermark has caught up (start == current_hour)
    or when clock skew puts it in the future (start > current_hour).

    A start that is not on the hour is floored, which can pull in one bucket
    already held. The upsert dedupes it and the alternative, ceiling, would
    skip an hour.
    """
    if max_candles < 1:
        raise ValueError(f"max_candles must be at least 1, got {max_candles}")

    cursor = floor_hour(start)
    last_bucket = floor_hour(current_hour) - HOUR
    span = (max_candles - 1) * HOUR

    while cursor <= last_bucket:
        window_end = min(cursor + span, last_bucket)
        yield cursor, window_end
        cursor = window_end + HOUR
