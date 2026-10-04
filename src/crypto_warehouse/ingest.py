"""Incremental, idempotent OHLCV ingestion from Coinbase Exchange into Postgres.

Data source: the public Coinbase Exchange candles endpoint. No API key needed,
real traded volume, and a hard cap of 300 candles per request, which forces the
windowed pagination that makes this a worthwhile incremental-loading exercise
rather than a single bulk download.

Flow per product:
  1. Watermark: MAX(ts_utc) in core.ohlcv_hourly, minus a re-fetch overlap.
  2. Fetch [watermark, current hour) in 300-hour windows into raw.candles,
     committing each window.
  3. One INSERT ... ON CONFLICT statement promotes the run's raw rows to core.
  4. The run row in raw.ingest_runs records what happened.

Idempotency comes from two properties: core's primary key (asset_id, ts_utc) is
the upsert conflict target, and raw tolerates duplicates by design. Re-running
any window changes nothing in core unless the source data actually changed.
"""

from __future__ import annotations

import logging
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import psycopg2.extras
import requests

from . import config, sqlfiles
from .api import GRANULARITY_SECONDS, fetch_window
from .config import ConfigError
from .windows import (
    bucket_count,
    floor_hour,
    iter_windows,
    longest_consecutive_run,
    missing_buckets,
)

log = logging.getLogger(__name__)

USER_AGENT = "crypto-sql-warehouse (portfolio project)"

# Re-fetch the trailing few hours on every run: a completed candle can be
# revised shortly after its hour closes, and the upsert leaves an unrevised
# row in core untouched.
OVERLAP_HOURS = 3
DEFAULT_BACKFILL_DAYS = 365
REQUEST_PAUSE_SECONDS = 0.15  # public endpoint allows ~10 req/s; stay well under

# A window is logged as short when it is missing this fraction of its buckets
# or more, or when it is missing this many consecutive hours or more. The
# fraction rounds down with a floor of one bucket, so a window under 20 hours
# is flagged by a single hole and an ordinary daily window by two.
# Zero-trade hours are legitimately absent from the response, so an isolated
# hole in a long backfill window on an illiquid pair is normal. Several hours missing in a row is
# what an exchange outage looks like, and a fraction alone would not see it:
# six hours is 2% of a 300-hour window. The MAX() watermark makes any hole
# permanent unless somebody notices it. Three hours is a judgment line, not a
# measured bound.
SHORT_WINDOW_TOLERANCE = 0.10
GAP_WARNING_HOURS = 3

# Column list in DDL order. The Candle NamedTuple carries the wire order, and
# building the tuple by field name is what keeps the two from drifting.
INSERT_RAW_SQL = """
    INSERT INTO raw.candles
        (run_id, product_code, bucket_start, open, high, low, close, volume)
    VALUES %s
"""

UPSERT_SQL = """
    INSERT INTO core.ohlcv_hourly
        (asset_id, ts_utc, open, high, low, close, volume, source_run_id)
    SELECT DISTINCT ON (a.asset_id, r.bucket_start)
        a.asset_id, r.bucket_start,
        r.open, r.high, r.low, r.close, r.volume, r.run_id
    FROM raw.candles r
    JOIN core.assets a ON a.product_code = r.product_code
    WHERE r.run_id = %s
    ORDER BY a.asset_id, r.bucket_start, r.raw_id DESC
    ON CONFLICT (asset_id, ts_utc) DO UPDATE
    SET open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        source_run_id = EXCLUDED.source_run_id,
        updated_at = now()
    WHERE (core.ohlcv_hourly.open, core.ohlcv_hourly.high, core.ohlcv_hourly.low,
           core.ohlcv_hourly.close, core.ohlcv_hourly.volume)
          IS DISTINCT FROM
          (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume)
"""


class LoadResult(NamedTuple):
    product_code: str
    run_id: int
    rows_fetched: int
    rows_upserted: int
    short_windows: int


def resolve_products(cur, requested: list[str] | None) -> list[str]:
    """The work list.

    With no --products, the work list is every asset flagged is_active: that
    flag exists so ingestion can be soft-disabled without deleting history.
    Naming products explicitly overrides the flag, because "load BTC-USD" is a
    direct instruction and silently doing nothing would be the worse
    surprise - backfilling a de-listed asset is a real reason to reach for the
    flag. The override is logged so it is never silent.
    """
    if requested:
        cur.execute(
            "SELECT product_code, is_active FROM core.assets WHERE product_code = ANY(%s)",
            (list(requested),),
        )
        found = dict(cur.fetchall())
        missing = sorted(set(requested) - set(found))
        if missing:
            raise ConfigError(f"not in core.assets: {missing}. Add them there first.")
        inactive = sorted(code for code, active in found.items() if not active)
        if inactive:
            log.warning(
                "loading inactive asset(s) %s because --products named them explicitly",
                inactive,
            )
        return sorted(found)

    cur.execute("SELECT product_code FROM core.assets WHERE is_active ORDER BY product_code")
    return [row[0] for row in cur.fetchall()]


def get_watermark(
    cur,
    product_code: str,
    backfill_days: int,
    *,
    apply_floor: bool = False,
    now_utc: datetime | None = None,
) -> datetime:
    """Where this product's fetch should resume.

    With no rows for the product, backfill_days sets the depth of the first
    load. With rows, the resume point is MAX(ts_utc) minus the overlap.

    apply_floor makes backfill_days a floor on that resume point rather than a
    cold-start-only setting, so it can actually extend an existing warehouse.
    It is off by default and scripts/run_ingest.py turns it on only when the
    operator passes --backfill-days explicitly; if it were always on, the
    default of 365 would drag every ordinary incremental run back a full year.

    The cold-start point is floored to the hour. Keeping now()'s minutes and
    microseconds served nothing: every bucket is on the hour, so the fractional
    part only made the first window's bounds look arbitrary in the run record.
    """
    now_hour = floor_hour(now_utc or datetime.now(UTC))
    floor = now_hour - timedelta(days=backfill_days)

    cur.execute(
        """
        SELECT max(o.ts_utc)
        FROM core.ohlcv_hourly o
        JOIN core.assets a USING (asset_id)
        WHERE a.product_code = %s
        """,
        (product_code,),
    )
    max_ts = cur.fetchone()[0]
    if max_ts is None:
        return floor

    resume = max_ts.astimezone(UTC) - timedelta(hours=OVERLAP_HOURS)
    return min(resume, floor) if apply_floor else resume


def _mark_run_failed(conn, run_id: int) -> None:
    """Best-effort bookkeeping after a failed load.

    Every statement here can itself fail - on a dead connection even
    conn.rollback() raises - and when it does, the InterfaceError from the
    bookkeeping used to replace the exception that actually explains the
    failure. So this swallows its own errors and the caller always re-raises
    the original. The cost of the swallow is a run row stranded at 'running',
    which the warning says out loud.
    """
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE raw.ingest_runs SET status = 'failed', finished_at = now() "
                "WHERE run_id = %s",
                (run_id,),
            )
        conn.commit()
    except Exception:
        log.warning(
            "could not mark run %s failed; it stays 'running' in raw.ingest_runs",
            run_id,
            exc_info=True,
        )


def _fetch_into_raw(
    conn, session, product_code: str, run_id: int, start: datetime, current_hour: datetime
) -> tuple[int, int]:
    """Fetch every window into raw.candles, committing as it goes.

    Returns (rows_fetched, short_windows).
    """
    rows_fetched = 0
    short_windows = 0

    for window_start, window_end in iter_windows(start, current_hour):
        fetched = fetch_window(session, product_code, window_start, window_end)
        # Keep only what was asked for. iter_windows already stops before the
        # forming hour, and a candle outside the window would otherwise count
        # toward the completeness check below.
        candles = [
            c
            for c in fetched
            if window_start <= c.bucket_start <= window_end and c.bucket_start < current_hour
        ]
        if len(candles) != len(fetched):
            log.warning(
                "%s: dropped %d candle(s) outside the requested window %s..%s",
                product_code,
                len(fetched) - len(candles),
                window_start.isoformat(),
                window_end.isoformat(),
            )

        expected = bucket_count(window_start, window_end)
        missing = missing_buckets(window_start, window_end, {c.bucket_start for c in candles})
        longest_gap = longest_consecutive_run(missing)
        if longest_gap >= GAP_WARNING_HOURS or (
            missing and len(missing) >= max(1, int(expected * SHORT_WINDOW_TOLERANCE))
        ):
            short_windows += 1
            log.warning(
                "%s: window %s..%s is missing %d of %d buckets, longest gap %d hour(s) "
                "starting %s",
                product_code,
                window_start.isoformat(),
                window_end.isoformat(),
                len(missing),
                expected,
                longest_gap,
                missing[0].isoformat(),
            )

        if candles:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    INSERT_RAW_SQL,
                    [
                        (
                            run_id,
                            product_code,
                            c.bucket_start,
                            c.open,
                            c.high,
                            c.low,
                            c.close,
                            c.volume,
                        )
                        for c in candles
                    ],
                )
            conn.commit()
            rows_fetched += len(candles)

        time.sleep(REQUEST_PAUSE_SECONDS)

    return rows_fetched, short_windows


def _promote_to_core(conn, run_id: int, rows_fetched: int) -> int:
    """Upsert this run's raw rows into core and close out the run row, in one
    transaction."""
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, (run_id,))
        rows_upserted = cur.rowcount
        cur.execute(
            """
            UPDATE raw.ingest_runs
            SET rows_fetched = %s, rows_upserted = %s,
                status = 'succeeded', finished_at = now()
            WHERE run_id = %s
            """,
            (rows_fetched, rows_upserted, run_id),
        )
    conn.commit()
    return rows_upserted


def load_product(
    conn,
    session,
    product_code: str,
    backfill_days: int,
    *,
    apply_floor: bool = False,
) -> LoadResult:
    """Load one product: watermark, fetch into raw, promote to core.

    Transaction shape, and why it changed
    -------------------------------------
    This used to be one transaction spanning every HTTP request for the
    product. That made the schema's "raw is append-only, so we never have to
    re-call the API" promise false exactly when it mattered: on a 365-day
    backfill, a kill at window 29 of 30 rolled back all ~8,600 candles already
    paid for and the next run started from nothing.

    Now each window commits as it lands, and the core upsert runs afterwards in
    its own transaction. The property the single transaction really bought was
    that core could never reference raw rows that were not there, and ordering
    preserves it: raw is durable before the upsert that reads it even begins,
    so the dangling direction is impossible by construction.

    What is now possible instead is the harmless direction: a crash between the
    last window and the upsert leaves raw rows that core has not promoted. The
    next run re-fetches that span from the watermark under a new run_id and
    promotes it then, and the stranded rows stay in raw as extra copies, which
    is precisely what raw is specified to tolerate. Recovering them without
    re-fetching is also just the UPSERT_SQL statement with the old run_id.
    """
    current_hour = floor_hour(datetime.now(UTC))

    with conn.cursor() as cur:
        start = get_watermark(cur, product_code, backfill_days, apply_floor=apply_floor)
        cur.execute(
            """
            INSERT INTO raw.ingest_runs
                (product_code, granularity_seconds, window_start, window_end)
            VALUES (%s, %s, %s, %s)
            RETURNING run_id
            """,
            (product_code, GRANULARITY_SECONDS, start, current_hour),
        )
        run_id = cur.fetchone()[0]
    conn.commit()  # run row is visible even if the fetch below dies

    try:
        rows_fetched, short_windows = _fetch_into_raw(
            conn, session, product_code, run_id, start, current_hour
        )
        rows_upserted = _promote_to_core(conn, run_id, rows_fetched)
    except BaseException:
        # BaseException, so that Ctrl+C also closes out the run row instead of
        # leaving it at 'running'.
        _mark_run_failed(conn, run_id)
        raise

    if short_windows:
        log.warning(
            "%s: %d window(s) came back materially short; run %d may have holes",
            product_code,
            short_windows,
            run_id,
        )
    return LoadResult(product_code, run_id, rows_fetched, rows_upserted, short_windows)


def _rollback_quietly(conn) -> None:
    """Roll back, logging instead of raising when the connection itself is gone."""
    try:
        conn.rollback()
    except Exception:
        log.warning("rollback after a failure also failed", exc_info=True)


def refresh_marts(conn) -> None:
    """Run sql/03_refresh_marts.sql in one transaction, so the three views change together."""
    sqlfiles.run_sql_file(conn, sqlfiles.REFRESH_MARTS_FILE)
    conn.commit()


def run_ingest(
    products: list[str] | None = None,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
    *,
    apply_floor: bool = False,
    refresh: bool = False,
) -> int:
    """Load every requested product. Returns a process exit code.

    Products are isolated from each other: one that fails is logged, recorded
    as failed, and the run moves on to the next. The mart refresh still
    happens, because stale marts help nobody. The exit code is what expresses
    partial success - nonzero if anything at all failed, with a summary line
    naming what.
    """
    failures: list[str] = []

    with closing(config.connect()) as conn, requests.Session() as session:
        session.headers["User-Agent"] = USER_AGENT

        with conn.cursor() as cur:
            targets = resolve_products(cur, products)
        conn.commit()

        if not targets:
            log.warning("no products to load; core.assets has no active rows")
            return 0

        log.info("loading %d product(s): %s", len(targets), ", ".join(targets))
        for product_code in targets:
            try:
                result = load_product(
                    conn, session, product_code, backfill_days, apply_floor=apply_floor
                )
            except Exception as exc:
                failures.append(product_code)
                log.error("%s: load failed: %s", product_code, exc, exc_info=True)
                # A failure before the run row exists never reaches the
                # rollback in _mark_run_failed, and an aborted transaction
                # would fail every product after this one.
                _rollback_quietly(conn)
                continue
            log.info(
                "%s: fetched %d, upserted %d (run %d)",
                result.product_code,
                result.rows_fetched,
                result.rows_upserted,
                result.run_id,
            )

        if refresh:
            try:
                refresh_marts(conn)
            except Exception as exc:
                failures.append("refresh-marts")
                log.error("mart refresh failed: %s", exc, exc_info=True)
                _rollback_quietly(conn)

    if failures:
        log.error("finished with %d failure(s): %s", len(failures), ", ".join(failures))
        return 1

    log.info("all %d product(s) loaded", len(targets))
    return 0
