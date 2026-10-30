"""Incremental, idempotent OHLCV ingestion from Coinbase Exchange into Postgres.

The data source is the public Coinbase Exchange candles endpoint. It needs no
API key and reports real traded volume. It caps a request at 300 candles, so
any load longer than 300 buckets has to be fetched as a series of windows.

The work list is every active asset at every active timeframe, from
core.assets and core.timeframes. Flow per (product, timeframe) series:
  1. Watermark: MAX(ts_utc) of the series in core.ohlcv, minus a re-fetch
     overlap.
  2. Fetch [watermark, current bucket) in 300-bucket windows into
     raw.candles, committing each window.
  3. One INSERT ... ON CONFLICT statement promotes the run's raw rows to core,
     leaving out any row core's constraints would reject.
  4. The run row in raw.ingest_runs records what happened, including the
     timeframe, which is how every raw row of the run is labeled, and
     raw.fetch_windows records every window requested and how many candles
     came back, which is how verify tells an exchange gap from a lost one.

Idempotency comes from two properties: core's primary key (asset_id,
granularity_seconds, ts_utc) is the upsert conflict target, and raw tolerates
duplicates by design. Re-running any window changes nothing in core unless the
source data actually changed.
"""

from __future__ import annotations

import logging
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import psycopg2.extras
import requests

from . import database_connection, dbt_project
from .coinbase_candles import Candle, fetch_window
from .database_connection import OperatorError
from .fetch_windows import (
    HOURLY_GRANULARITY_SECONDS,
    bucket_count,
    bucket_width,
    floor_to_bucket,
    iter_windows,
    longest_consecutive_run,
    missing_buckets,
    windows_covering,
)

logger = logging.getLogger(__name__)

USER_AGENT = "crypto-sql-warehouse (portfolio project)"

# Re-fetch the trailing few hours on every run, at every timeframe: a
# completed candle can be revised shortly after its bucket closes, and the
# upsert leaves an unrevised row in core untouched. At 1 day the start floors
# to the midnight before, so the overlap is one whole candle.
OVERLAP_HOURS = 3
DEFAULT_BACKFILL_DAYS = 365
REQUEST_PAUSE_SECONDS = 0.15  # public endpoint allows ~10 req/s; stay well under

# A window is logged as short when it is missing this fraction of its buckets
# or more, or when it is missing this many consecutive hours or more. The
# fraction rounds down with a floor of one bucket, so a window under 20
# buckets is flagged by a single hole and an ordinary daily hourly window by
# two. Zero-trade buckets are legitimately absent from the response, so an
# isolated hole in a long backfill window on an illiquid pair is normal, and
# at 5 minutes a thin pair has many. Several hours missing in a row is what an
# exchange outage looks like, and a fraction alone would not see it: six hours
# is 2% of a 300-hour window. The gap line is a duration, so it means the same
# outage at every timeframe up to 1 hour, and at 1 day a single missing candle
# reaches it. At 5 minutes and finer, three hours is more buckets than 10
# percent of a window, so there the fraction fires first. The
# MAX() watermark makes any hole permanent unless somebody notices it. Three
# hours is a judgment line, not a measured bound.
SHORT_WINDOW_TOLERANCE = 0.10
GAP_WARNING_HOURS = 3

# Column list in DDL order. The Candle NamedTuple carries the wire order, and
# building the tuple by field name is what keeps the two from drifting.
INSERT_RAW_SQL = """
    INSERT INTO raw.candles
        (run_id, product_code, bucket_start, open, high, low, close, volume)
    VALUES %s
"""

INSERT_FETCH_WINDOW_SQL = """
    INSERT INTO raw.fetch_windows (run_id, window_start, window_end, candles_received)
    VALUES (%s, %s, %s, %s)
"""

# The buckets of one series missing from core that no window of a succeeded
# run requested. These are the rows dbt/tests/gaps_without_a_recorded_request.sql
# returns, and the ones --recheck-gaps asks the exchange for again.
UNRECORDED_GAPS_SQL = """
    WITH series AS (
        SELECT o.asset_id, min(o.ts_utc) AS first_ts, max(o.ts_utc) AS last_ts
        FROM core.ohlcv o
        JOIN core.assets a USING (asset_id)
        WHERE a.product_code = %(product_code)s
          AND o.granularity_seconds = %(granularity_seconds)s
        GROUP BY o.asset_id
    )
    SELECT expected.ts
    FROM series s
    CROSS JOIN LATERAL generate_series(
        s.first_ts, s.last_ts, make_interval(secs => %(granularity_seconds)s)
    ) AS expected(ts)
    WHERE NOT EXISTS (
            SELECT 1 FROM core.ohlcv o
            WHERE o.asset_id = s.asset_id
              AND o.granularity_seconds = %(granularity_seconds)s
              AND o.ts_utc = expected.ts)
      AND NOT EXISTS (
            SELECT 1
            FROM raw.fetch_windows w
            JOIN raw.ingest_runs r USING (run_id)
            WHERE r.product_code = %(product_code)s
              AND r.granularity_seconds = %(granularity_seconds)s
              AND r.status = 'succeeded'
              AND expected.ts BETWEEN w.window_start AND w.window_end)
    ORDER BY expected.ts
"""

# The conditions core.ohlcv enforces with CHECK constraints, written
# against r, the alias of raw.candles. One raw row that fails any of them
# would abort the whole upsert, and because the watermark would then never
# move, every later run for that product too. Such a row is left in raw and
# logged instead, and verify fails on its bucket until a valid copy loads.
# tests/test_candle_ingest.py holds this text against the DDL so the two cannot drift.
CORE_ROW_CHECKS = """
        r.volume >= 0
        AND r.high >= r.low
        AND r.high >= r.open AND r.high >= r.close
        AND r.low <= r.open AND r.low <= r.close
        AND r.low > 0
"""

REJECTED_ROWS_SQL = f"""
    SELECT r.bucket_start
    FROM raw.candles r
    WHERE r.run_id = %s
      AND NOT ({CORE_ROW_CHECKS}      )
    ORDER BY r.bucket_start
"""

# The timeframe comes from the run row, since every candle a run fetched
# shares its run's granularity.
UPSERT_CORE_SQL = f"""
    INSERT INTO core.ohlcv
        (asset_id, granularity_seconds, ts_utc, open, high, low, close, volume, source_run_id)
    SELECT DISTINCT ON (a.asset_id, r.bucket_start)
        a.asset_id, ir.granularity_seconds, r.bucket_start,
        r.open, r.high, r.low, r.close, r.volume, r.run_id
    FROM raw.candles r
    JOIN raw.ingest_runs ir ON ir.run_id = r.run_id
    JOIN core.assets a ON a.product_code = r.product_code
    WHERE r.run_id = %s
      AND ({CORE_ROW_CHECKS}      )
    ORDER BY a.asset_id, r.bucket_start, r.raw_id DESC
    ON CONFLICT (asset_id, granularity_seconds, ts_utc) DO UPDATE
    SET open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        source_run_id = EXCLUDED.source_run_id,
        updated_at = now()
    WHERE (core.ohlcv.open, core.ohlcv.high, core.ohlcv.low,
           core.ohlcv.close, core.ohlcv.volume)
          IS DISTINCT FROM
          (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume)
"""


class Timeframe(NamedTuple):
    """Holds one row of core.timeframes: its code, such as '15m', and its
    width in seconds."""

    timeframe_code: str
    granularity_seconds: int


class LoadResult(NamedTuple):
    """Holds the outcome of one series' load, with its run id, its row counts,
    and the number of windows that came back short."""

    product_code: str
    timeframe_code: str
    run_id: int
    rows_fetched: int
    rows_upserted: int
    short_windows: int


def resolve_products(cursor, requested: list[str] | None) -> list[str]:
    """Return the sorted product codes to load, which are the requested codes
    or, with none requested, every active asset.

    A requested code is loaded even when inactive, with a warning, and one
    that is missing from core.assets raises OperatorError.
    """
    if requested:
        cursor.execute(
            "SELECT product_code, is_active FROM core.assets WHERE product_code = ANY(%s)",
            (list(requested),),
        )
        found = dict(cursor.fetchall())
        missing = sorted(set(requested) - set(found))
        if missing:
            raise OperatorError(f"not in core.assets: {missing}. Add them there first.")
        # A named product is loaded even when inactive, because naming it is a
        # direct instruction and backfilling a delisted asset is a real use.
        inactive = sorted(code for code, active in found.items() if not active)
        if inactive:
            logger.warning(
                "loading inactive asset(s) %s because --products named them explicitly",
                inactive,
            )
        return sorted(found)

    cursor.execute("SELECT product_code FROM core.assets WHERE is_active ORDER BY product_code")
    return [row[0] for row in cursor.fetchall()]


def resolve_timeframes(cursor, requested: list[str] | None) -> list[Timeframe]:
    """Return the timeframes to load, finest first, which are the requested
    codes or, with none requested, every active timeframe.

    A requested code is loaded even when inactive, with a warning, and one
    that is missing from core.timeframes raises OperatorError.
    """
    if requested:
        cursor.execute(
            "SELECT timeframe_code, granularity_seconds, is_active FROM core.timeframes "
            "WHERE timeframe_code = ANY(%s)",
            (list(requested),),
        )
        found = {code: (granularity, active) for code, granularity, active in cursor.fetchall()}
        missing = sorted(set(requested) - set(found))
        if missing:
            cursor.execute(
                "SELECT timeframe_code FROM core.timeframes ORDER BY granularity_seconds"
            )
            known = [row[0] for row in cursor.fetchall()]
            raise OperatorError(f"not in core.timeframes: {missing}. Known timeframes: {known}.")
        # Same rule as products: naming a timeframe is a direct instruction.
        inactive = sorted(code for code, (_, active) in found.items() if not active)
        if inactive:
            logger.warning(
                "loading inactive timeframe(s) %s because --timeframes named them explicitly",
                inactive,
            )
        return sorted(
            (Timeframe(code, granularity) for code, (granularity, _) in found.items()),
            key=lambda timeframe: timeframe.granularity_seconds,
        )

    cursor.execute(
        "SELECT timeframe_code, granularity_seconds FROM core.timeframes "
        "WHERE is_active ORDER BY granularity_seconds"
    )
    return [Timeframe(code, granularity) for code, granularity in cursor.fetchall()]


def find_resume_point(
    cursor,
    product_code: str,
    granularity_seconds: int,
    backfill_days: int,
    *,
    extend_to_backfill_days: bool = False,
    now_utc: datetime | None = None,
) -> datetime:
    """Return the UTC time this series' fetch should start from.

    With no rows it is the current hour minus backfill_days, and with rows it
    is MAX(ts_utc) of the series minus the overlap, or the earlier of that and
    the backfill point when extend_to_backfill_days is set. Each result is
    floored to the series' own bucket.
    """
    # Floored to the hour, which is a bucket boundary at every timeframe up to
    # 1 hour, so those timeframes of a backfill start at the same instant. The
    # floor on each return puts a 1-day series on UTC midnight, so the
    # window_start stored in the run record is a bucket at every timeframe.
    now_hour = floor_to_bucket(now_utc or datetime.now(UTC), HOURLY_GRANULARITY_SECONDS)
    backfill_floor = now_hour - timedelta(days=backfill_days)

    cursor.execute(
        """
        SELECT max(o.ts_utc)
        FROM core.ohlcv o
        JOIN core.assets a USING (asset_id)
        WHERE a.product_code = %s AND o.granularity_seconds = %s
        """,
        (product_code, granularity_seconds),
    )
    latest_timestamp = cursor.fetchone()[0]
    if latest_timestamp is None:
        return floor_to_bucket(backfill_floor, granularity_seconds)

    resume_point = latest_timestamp.astimezone(UTC) - timedelta(hours=OVERLAP_HOURS)
    # extend_to_backfill_days is off unless the operator passes
    # --backfill-days. If it were always on, the default of 365 days would make
    # every ordinary incremental run re-fetch a full year.
    if extend_to_backfill_days:
        resume_point = min(resume_point, backfill_floor)
    return floor_to_bucket(resume_point, granularity_seconds)


def _mark_run_failed(connection, run_id: int) -> None:
    """Roll back the open transaction and mark the run row failed, logging a
    warning instead of raising if either step fails.

    When a step fails, the run row stays at 'running' in raw.ingest_runs.
    """
    try:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE raw.ingest_runs SET status = 'failed', finished_at = now() "
                "WHERE run_id = %s",
                (run_id,),
            )
        connection.commit()
    except Exception:
        # The error is not re-raised, because on a dead connection even the
        # rollback raises, and that error would replace the exception that
        # explains the failure.
        logger.warning(
            "could not mark run %s failed; it stays 'running' in raw.ingest_runs",
            run_id,
            exc_info=True,
        )


def _fetch_and_record_window(
    connection,
    session,
    product_code: str,
    granularity_seconds: int,
    run_id: int,
    window_start: datetime,
    window_end: datetime,
    current_bucket: datetime,
) -> list[Candle]:
    """Fetch one window, store its candles in raw.candles and the request in
    raw.fetch_windows, commit, and return the candles kept.

    A window that comes back empty is still recorded, because an empty answer
    is what lets verify call a missing bucket the exchange's and not the
    pipeline's.
    """
    fetched = fetch_window(session, product_code, window_start, window_end, granularity_seconds)
    # Keep only what was asked for. Every caller stops before the forming
    # bucket, and a candle outside the window would otherwise count toward
    # the completeness check and the recorded count.
    candles = [
        candle
        for candle in fetched
        if window_start <= candle.bucket_start <= window_end
        and candle.bucket_start < current_bucket
    ]
    if len(candles) != len(fetched):
        logger.warning(
            "%s: dropped %d candle(s) outside the requested window %s..%s",
            product_code,
            len(fetched) - len(candles),
            window_start.isoformat(),
            window_end.isoformat(),
        )

    with connection.cursor() as cursor:
        if candles:
            psycopg2.extras.execute_values(
                cursor,
                INSERT_RAW_SQL,
                [
                    (
                        run_id,
                        product_code,
                        candle.bucket_start,
                        candle.open,
                        candle.high,
                        candle.low,
                        candle.close,
                        candle.volume,
                    )
                    for candle in candles
                ],
            )
        cursor.execute(INSERT_FETCH_WINDOW_SQL, (run_id, window_start, window_end, len(candles)))
    # Committed per window, so a failure late in a long backfill keeps the
    # windows already fetched in raw, each with its row in raw.fetch_windows.
    # No later step promotes a failed run's rows. Their buckets reach core
    # only when another run fetches the same span and promotes its own copies.
    # Until then dbt/tests/core_matches_raw.sql lists any of them core does
    # not already hold as never promoted.
    connection.commit()
    return candles


def _fetch_into_raw(
    connection,
    session,
    product_code: str,
    granularity_seconds: int,
    run_id: int,
    start: datetime,
    current_bucket: datetime,
) -> tuple[int, int]:
    """Fetch every window from start up to the current bucket into raw.candles,
    recording each request, and return (rows_fetched, short_windows).

    Windows that come back empty before the first candle of the run are the
    span before the product's history starts, which a deep backfill of a
    later listing reaches. They are counted and logged once rather than each
    flagged as short, the first window with candles is judged from its first
    candle on, and a run that finds no candle at all is flagged.
    """
    rows_fetched = 0
    short_windows = 0
    leading_empty_windows = 0
    gap_warning_buckets = max(
        1, timedelta(hours=GAP_WARNING_HOURS) // bucket_width(granularity_seconds)
    )

    for window_start, window_end in iter_windows(start, current_bucket, granularity_seconds):
        candles = _fetch_and_record_window(
            connection,
            session,
            product_code,
            granularity_seconds,
            run_id,
            window_start,
            window_end,
            current_bucket,
        )
        time.sleep(REQUEST_PAUSE_SECONDS)

        if not candles and not rows_fetched:
            leading_empty_windows += 1
            continue
        # The first window with candles is judged from its first candle, so the
        # part of a listing day before trading opened is not counted as holes.
        judged_from = window_start
        if not rows_fetched:
            judged_from = min(candle.bucket_start for candle in candles)
            if leading_empty_windows:
                logger.info(
                    "%s at %d s: no candles before %s (%d empty window(s)); history starts there",
                    product_code,
                    granularity_seconds,
                    judged_from.isoformat(),
                    leading_empty_windows,
                )
        rows_fetched += len(candles)

        expected = bucket_count(judged_from, window_end, granularity_seconds)
        missing = missing_buckets(
            judged_from,
            window_end,
            {candle.bucket_start for candle in candles},
            granularity_seconds,
        )
        longest_gap = longest_consecutive_run(missing, granularity_seconds)
        if longest_gap >= gap_warning_buckets or (
            missing and len(missing) >= max(1, int(expected * SHORT_WINDOW_TOLERANCE))
        ):
            short_windows += 1
            logger.warning(
                "%s at %d s: window %s..%s is missing %d of %d buckets, longest gap %d "
                "bucket(s) starting %s",
                product_code,
                granularity_seconds,
                window_start.isoformat(),
                window_end.isoformat(),
                len(missing),
                expected,
                longest_gap,
                missing[0].isoformat(),
            )

    if leading_empty_windows and not rows_fetched:
        short_windows += 1
        logger.warning(
            "%s at %d s: all %d window(s) from %s came back empty",
            product_code,
            granularity_seconds,
            leading_empty_windows,
            start.isoformat(),
        )
    return rows_fetched, short_windows


def _promote_to_core(connection, run_id: int, rows_fetched: int) -> int:
    """Upsert this run's raw rows into core, mark the run succeeded, and return
    the number of rows inserted or updated.

    A raw row that core's constraints would reject is skipped with a warning.
    """
    with connection.cursor() as cursor:
        cursor.execute(REJECTED_ROWS_SQL, (run_id,))
        rejected = [row[0] for row in cursor.fetchall()]
        if rejected:
            logger.warning(
                "run %d: %d raw candle(s) fail core's constraints and stay in raw only, "
                "the first at %s; scripts/run_verify.py will fail on these buckets",
                run_id,
                len(rejected),
                rejected[0].isoformat(),
            )
        cursor.execute(UPSERT_CORE_SQL, (run_id,))
        rows_upserted = cursor.rowcount
        cursor.execute(
            """
            UPDATE raw.ingest_runs
            SET rows_fetched = %s, rows_upserted = %s,
                status = 'succeeded', finished_at = now()
            WHERE run_id = %s
            """,
            (rows_fetched, rows_upserted, run_id),
        )
    connection.commit()
    return rows_upserted


def load_product(
    connection,
    session,
    product_code: str,
    timeframe: Timeframe,
    backfill_days: int,
    *,
    extend_to_backfill_days: bool = False,
) -> LoadResult:
    """Load one product at one timeframe by finding its resume point, fetching
    into raw, and promoting to core, and return a LoadResult.

    On any failure, Ctrl+C included, the run row is marked failed and the
    exception is re-raised.
    """
    granularity_seconds = timeframe.granularity_seconds
    current_bucket = floor_to_bucket(datetime.now(UTC), granularity_seconds)

    with connection.cursor() as cursor:
        start = find_resume_point(
            cursor,
            product_code,
            granularity_seconds,
            backfill_days,
            extend_to_backfill_days=extend_to_backfill_days,
        )
        cursor.execute(
            """
            INSERT INTO raw.ingest_runs
                (product_code, granularity_seconds, window_start, window_end)
            VALUES (%s, %s, %s, %s)
            RETURNING run_id
            """,
            (product_code, granularity_seconds, start, current_bucket),
        )
        run_id = cursor.fetchone()[0]
    connection.commit()  # run row is visible even if the fetch below fails

    # Raw commits per window and the core upsert follows in its own
    # transaction, so core never references raw rows that are missing. A crash
    # between the two leaves unpromoted raw rows. A daily run re-fetches them
    # only when they fall after the watermark, so rows left by a failed
    # --backfill-days or --recheck-gaps run stay unpromoted until a run
    # fetches that span again, such as the same command run again.
    try:
        rows_fetched, short_windows = _fetch_into_raw(
            connection, session, product_code, granularity_seconds, run_id, start, current_bucket
        )
        rows_upserted = _promote_to_core(connection, run_id, rows_fetched)
    except BaseException:
        # BaseException, so that Ctrl+C also closes out the run row instead of
        # leaving it at 'running'.
        _mark_run_failed(connection, run_id)
        raise

    if short_windows:
        logger.warning(
            "%s %s: %d window(s) came back materially short; run %d may have holes",
            product_code,
            timeframe.timeframe_code,
            short_windows,
            run_id,
        )
    return LoadResult(
        product_code, timeframe.timeframe_code, run_id, rows_fetched, rows_upserted, short_windows
    )


def recheck_gaps(connection, session, product_code: str, timeframe: Timeframe) -> LoadResult | None:
    """Re-request every bucket of one series that is missing from core and
    covered by no recorded request, record the windows asked for, promote
    anything the exchange now returns, and return a LoadResult, or None when
    there is nothing to re-request.

    This is how a warehouse loaded before raw.fetch_windows existed gets a
    real request on record for each gap its older runs left. On any failure,
    Ctrl+C included, the run row is marked failed and the exception is
    re-raised.
    """
    granularity_seconds = timeframe.granularity_seconds
    with connection.cursor() as cursor:
        cursor.execute(
            UNRECORDED_GAPS_SQL,
            {"product_code": product_code, "granularity_seconds": granularity_seconds},
        )
        gaps = [row[0] for row in cursor.fetchall()]
    connection.commit()
    if not gaps:
        return None

    windows = windows_covering(gaps, granularity_seconds)
    current_bucket = floor_to_bucket(datetime.now(UTC), granularity_seconds)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO raw.ingest_runs
                (product_code, granularity_seconds, window_start, window_end)
            VALUES (%s, %s, %s, %s)
            RETURNING run_id
            """,
            (
                product_code,
                granularity_seconds,
                windows[0][0],
                windows[-1][1] + bucket_width(granularity_seconds),
            ),
        )
        run_id = cursor.fetchone()[0]
    connection.commit()

    try:
        rows_fetched = 0
        returned_for_gaps = 0
        gap_set = set(gaps)
        for window_start, window_end in windows:
            candles = _fetch_and_record_window(
                connection,
                session,
                product_code,
                granularity_seconds,
                run_id,
                window_start,
                window_end,
                current_bucket,
            )
            rows_fetched += len(candles)
            returned_for_gaps += sum(candle.bucket_start in gap_set for candle in candles)
            time.sleep(REQUEST_PAUSE_SECONDS)
        rows_upserted = _promote_to_core(connection, run_id, rows_fetched)
    except BaseException:
        _mark_run_failed(connection, run_id)
        raise

    logger.info(
        "%s %s: re-requested %d gap bucket(s) in %d window(s); the exchange returned "
        "%d of them, and the rest are now on record as answered without a candle",
        product_code,
        timeframe.timeframe_code,
        len(gaps),
        len(windows),
        returned_for_gaps,
    )
    return LoadResult(
        product_code, timeframe.timeframe_code, run_id, rows_fetched, rows_upserted, 0
    )


def _rollback_quietly(connection) -> None:
    """Roll back, logging instead of raising when the connection itself is gone."""
    try:
        connection.rollback()
    except Exception:
        logger.warning(
            "rollback after a failure also failed; the connection is likely gone, so "
            "the products after this one will fail too",
            exc_info=True,
        )


def refresh_marts() -> None:
    """Refresh the marts through dbt (`dbt run`), in dependency order, each
    view in its own transaction, raising RuntimeError when dbt reports a
    failure."""
    if dbt_project.build_marts() != 0:
        raise RuntimeError("dbt run failed; its output above and logs/dbt/dbt.log say why")


def run_ingest(
    products: list[str] | None = None,
    timeframes: list[str] | None = None,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
    *,
    extend_to_backfill_days: bool = False,
    refresh: bool = False,
    recheck: bool = False,
) -> int:
    """Load every requested product at every requested timeframe, refresh the
    marts when asked, and return a process exit code.

    With recheck, each series re-requests its unrecorded gaps (recheck_gaps)
    instead of loading new candles. A series that fails is logged and the
    run continues with the next one, and the exit code is 1 if any series or
    the refresh failed.
    """
    failures: list[str] = []

    with closing(database_connection.connect()) as connection, requests.Session() as session:
        session.headers["User-Agent"] = USER_AGENT

        with connection.cursor() as cursor:
            targets = resolve_products(cursor, products)
            target_timeframes = resolve_timeframes(cursor, timeframes)
        connection.commit()

        if not targets:
            logger.warning("no products to load; core.assets has no active rows")
            return 0
        if not target_timeframes:
            logger.warning("no timeframes to load; core.timeframes has no active rows")
            return 0

        logger.info(
            "loading %d product(s) at %d timeframe(s): %s at %s",
            len(targets),
            len(target_timeframes),
            ", ".join(targets),
            ", ".join(timeframe.timeframe_code for timeframe in target_timeframes),
        )
        for product_code in targets:
            for timeframe in target_timeframes:
                series = f"{product_code} {timeframe.timeframe_code}"
                try:
                    if recheck:
                        load_result = recheck_gaps(connection, session, product_code, timeframe)
                        if load_result is None:
                            logger.info("%s: no unrecorded gaps", series)
                            continue
                    else:
                        load_result = load_product(
                            connection,
                            session,
                            product_code,
                            timeframe,
                            backfill_days,
                            extend_to_backfill_days=extend_to_backfill_days,
                        )
                except Exception as error:
                    failures.append(series)
                    logger.error("%s: load failed: %s", series, error, exc_info=True)
                    # A failure before the run row exists never reaches the
                    # rollback in _mark_run_failed, and an aborted transaction
                    # would fail every series after this one.
                    _rollback_quietly(connection)
                    continue
                logger.info(
                    "%s: fetched %d, upserted %d (run %d)",
                    series,
                    load_result.rows_fetched,
                    load_result.rows_upserted,
                    load_result.run_id,
                )

        # The refresh runs even after a failed product, so the products that
        # did load reach the marts.
        if refresh:
            try:
                refresh_marts()
            except Exception as error:
                failures.append("refresh-marts")
                logger.error("mart refresh failed: %s", error, exc_info=True)
                _rollback_quietly(connection)

    if failures:
        logger.error("finished with %d failure(s): %s", len(failures), ", ".join(failures))
        return 1

    logger.info(
        "all %d series %s (%d product(s) at %d timeframe(s))",
        len(targets) * len(target_timeframes),
        "rechecked" if recheck else "loaded",
        len(targets),
        len(target_timeframes),
    )
    return 0
