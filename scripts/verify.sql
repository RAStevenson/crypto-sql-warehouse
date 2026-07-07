-- Smoke test: structural invariants the warehouse must satisfy after any load.
--
-- Usage (check 4 assumes the pipeline just ran twice back to back):
--   python pipeline/ingest.py --refresh-marts
--   python pipeline/ingest.py
--   psql -d crypto_warehouse -v ON_ERROR_STOP=1 -f scripts/verify.sql
--
-- Each check is a DO block that raises an exception on failure, so a failing
-- run exits nonzero under ON_ERROR_STOP and works as a CI gate. Passing
-- checks print a NOTICE with the numbers they saw.

-- Check 1: no asset-specific gaps in the hourly series.
-- Hours missing for EVERY active asset at once are exchange outages and
-- allowed; an hour missing for only some assets would mean the pipeline
-- dropped data, which is a bug.
DO $$
DECLARE
    n_assets bigint;
    partial_gap_hours bigint;
    outage_hours bigint;
BEGIN
    SELECT count(*) INTO n_assets FROM core.assets WHERE is_active;

    WITH bounds AS (
        SELECT asset_id, min(ts_utc) AS first_ts, max(ts_utc) AS last_ts
        FROM core.ohlcv_hourly
        GROUP BY asset_id
    ),
    missing AS (
        SELECT e.ts
        FROM bounds b
        CROSS JOIN LATERAL generate_series(b.first_ts, b.last_ts, interval '1 hour') AS e(ts)
        LEFT JOIN core.ohlcv_hourly o
               ON o.asset_id = b.asset_id AND o.ts_utc = e.ts
        WHERE o.asset_id IS NULL
    ),
    by_hour AS (
        SELECT ts, count(*) AS assets_missing
        FROM missing
        GROUP BY ts
    )
    SELECT
        count(*) FILTER (WHERE assets_missing < n_assets),
        count(*) FILTER (WHERE assets_missing = n_assets)
    INTO partial_gap_hours, outage_hours
    FROM by_hour;

    IF partial_gap_hours > 0 THEN
        RAISE EXCEPTION 'gap check failed: % hour(s) missing for only some assets (pipeline bug, not outage)',
            partial_gap_hours;
    END IF;
    RAISE NOTICE 'gap check passed: 0 asset-specific gaps, % exchange-wide outage hour(s) tolerated', outage_hours;
END $$;

-- Check 2: the daily rollup covers exactly the (asset, day) pairs present
-- in the hourly facts -- no dropped days, no invented ones.
DO $$
DECLARE
    mart_rows bigint;
    hourly_pairs bigint;
BEGIN
    SELECT count(*) INTO mart_rows FROM marts.daily_ohlcv;

    SELECT count(DISTINCT (asset_id, (ts_utc AT TIME ZONE 'UTC')::date))
    INTO hourly_pairs
    FROM core.ohlcv_hourly;

    IF mart_rows <> hourly_pairs THEN
        RAISE EXCEPTION 'rollup check failed: daily_ohlcv has % rows, hourly has % distinct (asset, day) pairs',
            mart_rows, hourly_pairs;
    END IF;
    RAISE NOTICE 'rollup check passed: % rows on both sides', mart_rows;
END $$;

-- Check 3: core matches raw. Every core row's values equal the newest raw
-- copy of that bucket -- the upsert promoted the right data and nothing
-- got mangled in between.
DO $$
DECLARE
    mismatched bigint;
BEGIN
    WITH newest_raw AS (
        SELECT DISTINCT ON (a.asset_id, r.bucket_start)
            a.asset_id, r.bucket_start,
            r.open, r.high, r.low, r.close, r.volume
        FROM raw.candles r
        JOIN core.assets a ON a.product_code = r.product_code
        ORDER BY a.asset_id, r.bucket_start, r.raw_id DESC
    )
    SELECT count(*) INTO mismatched
    FROM core.ohlcv_hourly o
    JOIN newest_raw nr
      ON nr.asset_id = o.asset_id AND nr.bucket_start = o.ts_utc
    WHERE (o.open, o.high, o.low, o.close, o.volume)
          IS DISTINCT FROM
          (nr.open, nr.high, nr.low, nr.close, nr.volume);

    IF mismatched > 0 THEN
        RAISE EXCEPTION 'raw/core check failed: % core row(s) disagree with newest raw copy', mismatched;
    END IF;
    RAISE NOTICE 'raw/core check passed';
END $$;

-- Check 4: idempotency. The latest succeeded run per product must have
-- upserted zero rows, which is what a rerun over unchanged data does.
-- Valid immediately after two back-to-back runs; if an hour boundary
-- crossed between them, one new candle per product is legitimate, so the
-- threshold is 1, not 0.
DO $$
DECLARE
    offending bigint;
BEGIN
    WITH latest AS (
        SELECT DISTINCT ON (product_code)
            product_code, run_id, rows_upserted
        FROM raw.ingest_runs
        WHERE status = 'succeeded'
        ORDER BY product_code, run_id DESC
    )
    SELECT count(*) INTO offending FROM latest WHERE rows_upserted > 1;

    IF offending > 0 THEN
        RAISE EXCEPTION 'idempotency check failed: % product(s) upserted more than 1 row on rerun (see raw.ingest_runs)',
            offending;
    END IF;
    RAISE NOTICE 'idempotency check passed: latest run per product upserted <= 1 row';
END $$;
