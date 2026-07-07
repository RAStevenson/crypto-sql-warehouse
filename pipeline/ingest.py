"""Incremental, idempotent OHLCV ingestion from Coinbase Exchange into Postgres.

Data source: the public Coinbase Exchange candles endpoint. No API key needed,
real traded volume, and a hard cap of 300 candles per request -- which forces
the windowed pagination that makes this a worthwhile incremental-loading
exercise rather than a single bulk download.

Flow per product:
  1. Watermark: MAX(ts_utc) in core.ohlcv_hourly, minus a re-fetch overlap.
  2. Fetch [watermark, now) in 300-hour windows into raw.candles (append-only).
  3. One INSERT ... ON CONFLICT statement promotes the run's raw rows to core.
  4. The run row in raw.ingest_runs records what happened.

Idempotency comes from two properties: core's primary key (asset_id, ts_utc)
is the upsert conflict target, and raw tolerates duplicates by design. Re-running
any window changes nothing in core unless the source data actually changed.
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

API_BASE = "https://api.exchange.coinbase.com"
GRANULARITY_SECONDS = 3600
MAX_CANDLES_PER_REQUEST = 300
# Re-fetch the trailing few hours on every run: the most recent candle can be
# revised while its hour is in progress, and the upsert makes overlap free.
OVERLAP_HOURS = 3
REQUEST_PAUSE_SECONDS = 0.15  # public endpoint allows ~10 req/s; stay well under

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


def get_connection():
    load_dotenv()  # reads .env from the working directory if present; no-op otherwise
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set. Copy .env.example to .env or export it.")
    return psycopg2.connect(dsn)


def fetch_window(session, product_code, window_start, window_end):
    """Fetch one candle window. Returns rows as (bucket_start, low, high, open, close, volume)."""
    url = f"{API_BASE}/products/{product_code}/candles"
    params = {
        "granularity": GRANULARITY_SECONDS,
        "start": window_start.isoformat(),
        "end": window_end.isoformat(),
    }
    for attempt in range(5):
        response = session.get(url, params=params, timeout=30)
        if response.status_code == 429:
            # Explicit backoff on rate limiting; anything else is a hard error.
            time.sleep(2 ** attempt)
            continue
        response.raise_for_status()
        rows = response.json()
        return [
            (datetime.fromtimestamp(r[0], tz=timezone.utc), r[1], r[2], r[3], r[4], r[5])
            for r in rows
        ]
    raise RuntimeError(f"Rate-limited 5 times in a row fetching {product_code}")


def get_watermark(cur, product_code, backfill_days):
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
        return datetime.now(timezone.utc) - timedelta(days=backfill_days)
    return max_ts - timedelta(hours=OVERLAP_HOURS)


def load_product(conn, session, product_code, backfill_days):
    current_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    with conn.cursor() as cur:
        start = get_watermark(cur, product_code, backfill_days)
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
        rows_fetched = 0
        window_start = start
        with conn.cursor() as cur:
            while window_start < current_hour:
                # start/end are inclusive, so a full 300-hour span would be 301
                # buckets and the API rejects it. The boundary candle gets
                # fetched twice across windows; the upsert dedup absorbs that.
                window_end = min(
                    window_start + timedelta(hours=MAX_CANDLES_PER_REQUEST - 1),
                    current_hour,
                )
                candles = fetch_window(session, product_code, window_start, window_end)
                # The API includes the still-forming current hour; exclude it so
                # every stored candle is final (modulo rare upstream revisions).
                candles = [c for c in candles if c[0] < current_hour]
                if candles:
                    psycopg2.extras.execute_values(
                        cur,
                        """
                        INSERT INTO raw.candles
                            (run_id, product_code, bucket_start,
                             low, high, open, close, volume)
                        VALUES %s
                        """,
                        [(run_id, product_code, *c) for c in candles],
                    )
                    rows_fetched += len(candles)
                window_start = window_end
                time.sleep(REQUEST_PAUSE_SECONDS)

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
        print(f"{product_code}: fetched {rows_fetched}, upserted {rows_upserted} (run {run_id})")
    except Exception:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE raw.ingest_runs SET status = 'failed', finished_at = now() WHERE run_id = %s",
                (run_id,),
            )
        conn.commit()
        raise  # mark the run failed, then let the real error surface


def refresh_marts(conn):
    # REFRESH ... CONCURRENTLY cannot run inside a transaction block.
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for view in ("marts.daily_ohlcv", "marts.daily_metrics",
                         "marts.pairwise_correlations_90d"):
                cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
                print(f"refreshed {view}")
    finally:
        conn.autocommit = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backfill-days", type=int, default=365,
        help="History depth for a product's first load (default 365)",
    )
    parser.add_argument(
        "--products", nargs="*", default=None,
        help="Product codes to load (default: all active assets in core.assets)",
    )
    parser.add_argument(
        "--refresh-marts", action="store_true",
        help="Refresh materialized views after loading",
    )
    args = parser.parse_args()

    conn = get_connection()
    session = requests.Session()
    session.headers["User-Agent"] = "crypto-sql-warehouse (portfolio project)"

    with conn.cursor() as cur:
        if args.products:
            cur.execute(
                "SELECT product_code FROM core.assets WHERE product_code = ANY(%s)",
                (args.products,),
            )
            found = {row[0] for row in cur.fetchall()}
            missing = set(args.products) - found
            if missing:
                sys.exit(f"Not in core.assets: {sorted(missing)}. Add them there first.")
            products = sorted(found)
        else:
            cur.execute(
                "SELECT product_code FROM core.assets WHERE is_active ORDER BY product_code"
            )
            products = [row[0] for row in cur.fetchall()]
    conn.commit()  # close the read transaction so autocommit can be toggled later

    for product_code in products:
        load_product(conn, session, product_code, args.backfill_days)

    if args.refresh_marts:
        refresh_marts(conn)

    conn.close()


if __name__ == "__main__":
    main()
