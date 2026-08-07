-- Smoke test: structural invariants the warehouse must satisfy after any load.
--
-- Usage (checks 4 and 5 assume the loader just ran twice back to back):
--   psql -d crypto_warehouse -v ON_ERROR_STOP=1 -f sql/05_verify.sql
--
-- Each check is a DO block that raises an exception on failure, so a failing
-- run exits nonzero under ON_ERROR_STOP and works as a CI gate. Passing
-- checks print a NOTICE with the numbers they saw.
--
-- Design rule these checks now follow, learned the hard way: an assertion is
-- only worth having if it fails on the bad state it claims to detect. Every
-- check below was rewritten after constructing its failure case inside a
-- transaction and watching the previous version pass.

-- Check 1: no asset-specific gaps in the hourly series.
-- Hours missing for EVERY asset that has history at that hour are exchange
-- outages and allowed; an hour missing for only some of them would mean the
-- pipeline dropped data, which is a bug.
--
-- The denominator is computed PER HOUR from the same LATERAL expansion that
-- produces the numerator, rather than from a separate count of active
-- assets. The old version counted core.assets WHERE is_active and compared
-- that to a missing-count derived from core.ohlcv_hourly, so the two sides
-- described different populations:
--   - adding a new active asset with no candles yet raised the denominator
--     to 7 while no hour could ever reach 7 missing, turning 5 genuine
--     outage hours into a reported pipeline bug;
--   - marking an asset inactive while it kept its history lowered the
--     denominator to 5 while real outages showed 6 missing, so those hours
--     matched neither FILTER and vanished from both counters.
-- Counting expected and missing in the same GROUP BY makes both impossible,
-- and the >= in the outage filter means nothing can fall between the two.
DO $$
DECLARE
    partial_gap_hours bigint;
    outage_hours bigint;
BEGIN
    WITH bounds AS (
        SELECT asset_id, min(ts_utc) AS first_ts, max(ts_utc) AS last_ts
        FROM core.ohlcv_hourly
        GROUP BY asset_id
    ),
    by_hour AS (
        SELECT
            e.ts,
            count(*)                                        AS assets_expected,
            count(*) FILTER (WHERE o.asset_id IS NULL)      AS assets_missing
        FROM bounds b
        CROSS JOIN LATERAL generate_series(b.first_ts, b.last_ts, interval '1 hour') AS e(ts)
        LEFT JOIN core.ohlcv_hourly o
               ON o.asset_id = b.asset_id AND o.ts_utc = e.ts
        GROUP BY e.ts
    )
    SELECT
        count(*) FILTER (WHERE assets_missing > 0 AND assets_missing < assets_expected),
        count(*) FILTER (WHERE assets_missing >= assets_expected)
    INTO partial_gap_hours, outage_hours
    FROM by_hour;

    IF partial_gap_hours > 0 THEN
        RAISE EXCEPTION 'gap check failed: % hour(s) missing for only some assets (pipeline bug, not outage)',
            partial_gap_hours;
    END IF;
    RAISE NOTICE 'gap check passed: 0 asset-specific gaps, % exchange-wide outage hour(s) tolerated', outage_hours;
END $$;

-- Check 2: the daily rollup covers exactly the (asset, day) pairs present in
-- the hourly facts. No dropped days, no invented ones.
--
-- This compares SETS, not cardinalities. The old version compared
-- count(marts.daily_ohlcv) against count(DISTINCT (asset, day)) in core, so
-- deleting one real day and inventing one fake day left both integers equal
-- and the check passed on a mart that was wrong in two places at once. The
-- EXCEPT runs in both directions and reports which side is at fault.
DO $$
DECLARE
    missing_from_mart bigint;
    not_in_hourly bigint;
    example text;
BEGIN
    WITH hourly_pairs AS (
        SELECT DISTINCT asset_id, (ts_utc AT TIME ZONE 'UTC')::date AS trade_date
        FROM core.ohlcv_hourly
    ),
    mart_pairs AS (
        SELECT asset_id, trade_date FROM marts.daily_ohlcv
    ),
    only_hourly AS (SELECT * FROM hourly_pairs EXCEPT SELECT * FROM mart_pairs),
    only_mart   AS (SELECT * FROM mart_pairs   EXCEPT SELECT * FROM hourly_pairs)
    SELECT
        (SELECT count(*) FROM only_hourly),
        (SELECT count(*) FROM only_mart),
        coalesce(
            (SELECT format('missing from mart: asset %s on %s', asset_id, trade_date)
             FROM only_hourly ORDER BY asset_id, trade_date LIMIT 1),
            (SELECT format('not backed by hourly: asset %s on %s', asset_id, trade_date)
             FROM only_mart ORDER BY asset_id, trade_date LIMIT 1))
    INTO missing_from_mart, not_in_hourly, example;

    IF missing_from_mart > 0 OR not_in_hourly > 0 THEN
        RAISE EXCEPTION 'rollup check failed: % (asset, day) pair(s) missing from the mart, % with no hourly backing (e.g. %)',
            missing_from_mart, not_in_hourly, example;
    END IF;
    RAISE NOTICE 'rollup check passed: (asset, day) sets are identical';
END $$;

-- Check 3: core matches raw, in value and in membership.
--
-- Three assertions, because the value comparison alone was an INNER JOIN and
-- an inner join can only see rows that exist on both sides. A core row with
-- no raw backing (hand-edited, or promoted from a run whose raw rows were
-- later removed) and a raw bucket that was never promoted were both
-- invisible to it. The value check is kept as-is and the two membership
-- checks are added around it.
DO $$
DECLARE
    mismatched bigint;
    core_orphans bigint;
    raw_unpromoted bigint;
BEGIN
    WITH newest_raw AS (
        SELECT DISTINCT ON (a.asset_id, r.bucket_start)
            a.asset_id, r.bucket_start,
            r.open, r.high, r.low, r.close, r.volume
        FROM raw.candles r
        JOIN core.assets a ON a.product_code = r.product_code
        ORDER BY a.asset_id, r.bucket_start, r.raw_id DESC
    ),
    value_mismatch AS (
        SELECT count(*) AS c
        FROM core.ohlcv_hourly o
        JOIN newest_raw nr
          ON nr.asset_id = o.asset_id AND nr.bucket_start = o.ts_utc
        WHERE (o.open, o.high, o.low, o.close, o.volume)
              IS DISTINCT FROM
              (nr.open, nr.high, nr.low, nr.close, nr.volume)
    ),
    core_without_raw AS (
        SELECT count(*) AS c
        FROM core.ohlcv_hourly o
        LEFT JOIN newest_raw nr
          ON nr.asset_id = o.asset_id AND nr.bucket_start = o.ts_utc
        WHERE nr.asset_id IS NULL
    ),
    raw_without_core AS (
        SELECT count(*) AS c
        FROM newest_raw nr
        LEFT JOIN core.ohlcv_hourly o
          ON o.asset_id = nr.asset_id AND o.ts_utc = nr.bucket_start
        WHERE o.asset_id IS NULL
    )
    SELECT value_mismatch.c, core_without_raw.c, raw_without_core.c
    INTO mismatched, core_orphans, raw_unpromoted
    FROM value_mismatch, core_without_raw, raw_without_core;

    IF mismatched > 0 THEN
        RAISE EXCEPTION 'raw/core check failed: % core row(s) disagree with the newest raw copy', mismatched;
    END IF;
    IF core_orphans > 0 THEN
        RAISE EXCEPTION 'raw/core check failed: % core row(s) have no raw backing at all', core_orphans;
    END IF;
    IF raw_unpromoted > 0 THEN
        RAISE EXCEPTION 'raw/core check failed: % raw bucket(s) were never promoted into core', raw_unpromoted;
    END IF;
    RAISE NOTICE 'raw/core check passed: values agree, no core orphans, no unpromoted raw buckets';
END $$;

-- Check 4: idempotency. The latest succeeded run per product must have
-- upserted at most one row, which is what a rerun over unchanged data does.
-- Valid immediately after two back-to-back runs; if an hour boundary crossed
-- between them, one new candle per product is legitimate, so the threshold
-- is 1, not 0.
--
-- rows_upserted IS NULL is now an explicit failure. It used to slip through
-- because NULL > 1 evaluates to NULL, not true, so count(*) never counted
-- it. A succeeded run that never recorded how many rows it moved is exactly
-- the case this check exists to catch.
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
    SELECT count(*) INTO offending
    FROM latest
    WHERE rows_upserted IS NULL OR rows_upserted > 1;

    IF offending > 0 THEN
        RAISE EXCEPTION 'idempotency check failed: % product(s) upserted more than 1 row on rerun, or recorded no count at all (see raw.ingest_runs)',
            offending;
    END IF;
    RAISE NOTICE 'idempotency check passed: latest succeeded run per product upserted <= 1 row';
END $$;

-- Check 5: the newest run per product actually succeeded.
--
-- Split out from check 4 rather than folded into it. Check 4 filters to
-- status = 'succeeded' INSIDE its DISTINCT ON, which means that if the most
-- recent run for a product failed, check 4 quietly validates the last run
-- that worked and reports a pass. That is the worst possible behaviour for a
-- pipeline gate: it goes green precisely when the pipeline is broken. This
-- check looks at the newest run per product with no status filter at all.
DO $$
DECLARE
    offending bigint;
    detail text;
BEGIN
    WITH newest_run AS (
        SELECT DISTINCT ON (product_code)
            product_code, run_id, status
        FROM raw.ingest_runs
        ORDER BY product_code, run_id DESC
    )
    SELECT count(*), string_agg(format('%s (run %s: %s)', product_code, run_id, status), ', '
                                ORDER BY product_code)
    INTO offending, detail
    FROM newest_run
    WHERE status <> 'succeeded';

    IF offending > 0 THEN
        RAISE EXCEPTION 'freshness check failed: newest run did not succeed for % product(s): %',
            offending, detail;
    END IF;
    RAISE NOTICE 'freshness check passed: newest run per product succeeded';
END $$;

-- Check 6: the daily rollup buckets identically under any session timezone.
--
-- Regression test for a bug this warehouse actually had: bucketing a
-- timestamptz with a plain ::date cast (or date_trunc()) uses the SESSION
-- timezone, so a client connecting from America/Chicago got day boundaries at
-- 05:00/06:00 UTC and different daily volumes. The fix pins the boundary in
-- the rollup expression itself, (ts_utc AT TIME ZONE 'UTC')::date in
-- 02_marts.sql, with the database default of UTC set in 01_schema.sql only as
-- a second layer, since any client can override it with SET timezone.
--
-- The check reads the mart's defining query live from the catalog with
-- pg_get_viewdef, so a drifted definition cannot dodge it, then runs that
-- query once under America/Chicago and once under UTC and requires the two
-- results to be identical row for row. Failure case constructed per the
-- design rule above: with marts.daily_ohlcv redefined to bucket by ts_utc::date
-- inside a rolled-back transaction, this check reported all 708 rows differing
-- on each side (moving the boundary by 5 hours touches nearly every day's
-- hours_present and volume) while checks 1 through 5 still passed, because the
-- buggy mart had been materialized under a UTC session and its stored rows
-- looked fine.
DO $$
DECLARE
    rollup_sql text := pg_get_viewdef('marts.daily_ohlcv'::regclass);
    tz_before text := current_setting('timezone');
    only_chicago bigint;
    only_utc bigint;
    total_rows bigint;
    example text;
BEGIN
    -- set_config(..., is_local => true): both overrides die with the enclosing
    -- transaction, so a failure cannot leak a Chicago timezone into anything
    -- that runs after this block.
    PERFORM set_config('timezone', 'America/Chicago', true);
    EXECUTE format('CREATE TEMP TABLE rollup_run_chicago ON COMMIT DROP AS %s', rollup_sql);

    PERFORM set_config('timezone', 'UTC', true);
    EXECUTE format('CREATE TEMP TABLE rollup_run_utc ON COMMIT DROP AS %s', rollup_sql);

    PERFORM set_config('timezone', tz_before, true);

    WITH only_c AS (SELECT * FROM rollup_run_chicago EXCEPT SELECT * FROM rollup_run_utc),
         only_u AS (SELECT * FROM rollup_run_utc     EXCEPT SELECT * FROM rollup_run_chicago)
    SELECT
        (SELECT count(*) FROM only_c),
        (SELECT count(*) FROM only_u),
        (SELECT count(*) FROM rollup_run_utc),
        coalesce(
            (SELECT format('asset %s on %s has %s hour(s) under America/Chicago', asset_id, trade_date, hours_present)
             FROM only_c ORDER BY asset_id, trade_date LIMIT 1),
            (SELECT format('asset %s on %s exists only under UTC', asset_id, trade_date)
             FROM only_u ORDER BY asset_id, trade_date LIMIT 1))
    INTO only_chicago, only_utc, total_rows, example;

    IF only_chicago > 0 OR only_utc > 0 THEN
        RAISE EXCEPTION 'timezone check failed: rollup depends on session timezone, % row(s) only under America/Chicago and % only under UTC (e.g. %)',
            only_chicago, only_utc, example;
    END IF;
    RAISE NOTICE 'timezone check passed: % rollup rows identical under America/Chicago and UTC', total_rows;
END $$;
