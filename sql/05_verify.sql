-- Smoke test: structural invariants the warehouse must satisfy after a load
-- in which every product succeeded, no candle was left out of core, and the
-- marts were refreshed.
--
-- Usage:
--   psql -d crypto_warehouse -v ON_ERROR_STOP=1 -f sql/05_verify.sql
--
-- Each check is a DO block that raises an exception on failure, so a failing
-- run exits nonzero under ON_ERROR_STOP and works as a CI gate. Passing
-- checks print a NOTICE with the numbers they saw.
--
-- The design rule these checks follow is that an assertion is only worth
-- having if it fails on the bad state it claims to detect. Where a check
-- has a simpler form, the comment below names it and what that form gets
-- wrong.

-- Check 1: no asset-specific gaps in the 1-hour series.
-- Hours missing for EVERY asset that has history at that hour are exchange
-- outages and allowed; an hour missing for only some of them means the
-- pipeline dropped data, the loader left a candle in raw because core's
-- constraints reject it, or the product had no trades that hour (see below).
--
-- The denominator is computed PER HOUR from the same LATERAL expansion that
-- produces the numerator, rather than from a separate count of active
-- assets. Counting core.assets WHERE is_active and comparing that to a
-- missing-count derived from the 1-hour series puts two different
-- populations on the two sides:
--   - a new active asset with no candles yet raises the denominator to 7
--     while no hour can ever reach 7 missing, which turns 5 genuine outage
--     hours into a reported pipeline bug;
--   - an asset marked inactive that keeps its history lowers the
--     denominator to 5 while real outages show 6 missing, so those hours
--     match neither FILTER and vanish from both counters.
-- Counting expected and missing in the same GROUP BY makes both impossible,
-- and the >= in the outage filter means nothing can fall between the two.
--
-- The check reads the 1-hour series only. The marts build from it, and a
-- finer series legitimately skips every bucket with no trades, which on a
-- thin product at 5 minutes is a large share of them, so a hole there cannot
-- be told apart from lost data. For the finer series the guard is the
-- loader's own warning on windows that come back short, not an assertion.
--
-- What the gap count cannot see, stated so nobody assumes otherwise. Each
-- asset's expected hours run from its own first candle to its own last, so
-- an asset that stops loading produces no gap; the second assertion below
-- covers that case for active assets. Where only one asset has history, an
-- hour it lacks counts as an outage, because there is nothing to compare it
-- with. And a thinly traded product with zero-trade hours fails this check
-- by design: the six seeded assets trade every hour, and a product that does
-- not would need a tolerance this check does not have.
DO $$
DECLARE
    partial_gap_hours bigint;
    outage_hours bigint;
    stale_assets bigint;
    stale_detail text;
BEGIN
    WITH bounds AS (
        SELECT asset_id, min(ts_utc) AS first_ts, max(ts_utc) AS last_ts
        FROM core.ohlcv
        WHERE granularity_seconds = 3600
        GROUP BY asset_id
    ),
    by_hour AS (
        SELECT
            e.ts,
            count(*)                                        AS assets_expected,
            count(*) FILTER (WHERE o.asset_id IS NULL)      AS assets_missing
        FROM bounds b
        CROSS JOIN LATERAL generate_series(b.first_ts, b.last_ts, interval '1 hour') AS e(ts)
        LEFT JOIN core.ohlcv o
               ON o.asset_id = b.asset_id
              AND o.granularity_seconds = 3600
              AND o.ts_utc = e.ts
        GROUP BY e.ts
    )
    SELECT
        count(*) FILTER (WHERE assets_missing > 0 AND assets_missing < assets_expected),
        count(*) FILTER (WHERE assets_missing >= assets_expected)
    INTO partial_gap_hours, outage_hours
    FROM by_hour;

    IF partial_gap_hours > 0 THEN
        RAISE EXCEPTION 'gap check failed: % hour(s) missing for only some assets (dropped by the pipeline, rejected by core constraints, or not traded; not an outage)',
            partial_gap_hours;
    END IF;

    -- An active series (active asset at an active timeframe) whose newest
    -- candle trails the newest candle of any active series by more than two
    -- days has stopped loading. Two days, because a daily schedule plus a
    -- load of a single product can legitimately leave the others a day
    -- behind. The series end within an hour of each other, since each load
    -- stops at the last closed bucket of its timeframe.
    WITH newest AS (
        SELECT a.product_code, t.timeframe_code, max(o.ts_utc) AS last_ts
        FROM core.assets a
        JOIN core.ohlcv o USING (asset_id)
        JOIN core.timeframes t USING (granularity_seconds)
        WHERE a.is_active AND t.is_active
        GROUP BY a.product_code, t.timeframe_code
    )
    SELECT count(*),
           string_agg(format('%s %s (last candle %s)', product_code, timeframe_code, last_ts),
                      ', ' ORDER BY product_code, timeframe_code)
    INTO stale_assets, stale_detail
    FROM newest
    WHERE last_ts < (SELECT max(last_ts) FROM newest) - interval '48 hours';

    IF stale_assets > 0 THEN
        RAISE EXCEPTION 'gap check failed: % active series stopped loading more than 48 hours before the newest candle: %',
            stale_assets, stale_detail;
    END IF;
    RAISE NOTICE 'gap check passed: 0 asset-specific gaps in the 1-hour series, % exchange-wide outage hour(s) tolerated, no active series more than 48 hours behind', outage_hours;
END $$;

-- Check 2: the daily rollup covers exactly the (asset, day) pairs present in
-- the 1-hour series. No dropped days, no invented ones.
--
-- This compares SETS, not cardinalities. Comparing count(marts.daily_ohlcv)
-- against count(DISTINCT (asset, day)) in core leaves both integers equal
-- when one real day is deleted and one fake day invented, so that form
-- passes on a mart that is wrong in two places at once. The
-- EXCEPT runs in both directions and reports which side is at fault.
DO $$
DECLARE
    missing_from_mart bigint;
    not_in_hourly bigint;
    example text;
BEGIN
    WITH hourly_pairs AS (
        SELECT DISTINCT asset_id, (ts_utc AT TIME ZONE 'UTC')::date AS trade_date
        FROM core.ohlcv
        WHERE granularity_seconds = 3600
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
-- Three assertions, because the value comparison is an INNER JOIN and an
-- inner join can only see rows that exist on both sides. A core row with no
-- raw backing (hand-edited, or promoted from a run whose raw rows were later
-- removed) and a raw bucket that was never promoted are both invisible to
-- it, so a membership check runs in each direction beside it. A bucket the
-- loader left in raw because core's constraints reject it fails here too,
-- as never promoted or, when core holds an earlier copy, as a mismatch.
-- A raw row's timeframe comes from its run, so every comparison here is
-- per (asset, timeframe, bucket).
DO $$
DECLARE
    mismatched bigint;
    core_orphans bigint;
    raw_unpromoted bigint;
BEGIN
    WITH newest_raw AS (
        SELECT DISTINCT ON (a.asset_id, ir.granularity_seconds, r.bucket_start)
            a.asset_id, ir.granularity_seconds, r.bucket_start,
            r.open, r.high, r.low, r.close, r.volume
        FROM raw.candles r
        JOIN raw.ingest_runs ir ON ir.run_id = r.run_id
        JOIN core.assets a ON a.product_code = r.product_code
        ORDER BY a.asset_id, ir.granularity_seconds, r.bucket_start, r.raw_id DESC
    ),
    value_mismatch AS (
        SELECT count(*) AS c
        FROM core.ohlcv o
        JOIN newest_raw nr
          ON nr.asset_id = o.asset_id
         AND nr.granularity_seconds = o.granularity_seconds
         AND nr.bucket_start = o.ts_utc
        WHERE (o.open, o.high, o.low, o.close, o.volume)
              IS DISTINCT FROM
              (nr.open, nr.high, nr.low, nr.close, nr.volume)
    ),
    core_without_raw AS (
        SELECT count(*) AS c
        FROM core.ohlcv o
        LEFT JOIN newest_raw nr
          ON nr.asset_id = o.asset_id
         AND nr.granularity_seconds = o.granularity_seconds
         AND nr.bucket_start = o.ts_utc
        WHERE nr.asset_id IS NULL
    ),
    raw_without_core AS (
        SELECT count(*) AS c
        FROM newest_raw nr
        LEFT JOIN core.ohlcv o
          ON o.asset_id = nr.asset_id
         AND o.granularity_seconds = nr.granularity_seconds
         AND o.ts_utc = nr.bucket_start
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

-- Check 4: idempotency. The latest succeeded run per product and timeframe
-- must not have rewritten a row whose values did not change.
--
-- The upsert's IS DISTINCT FROM guard is what makes a rerun leave core alone,
-- and this is the state a broken guard would leave behind: a core row whose
-- source_run_id is the latest run, that was updated rather than inserted
-- (updated_at later than inserted_at), and whose values equal the copy an
-- earlier succeeded run had already promoted. Raw keeps every copy, so the
-- comparison needs no second run and holds after an ordinary daily load.
--
-- A count-based form (the latest run upserted at most one row) is valid only
-- straight after two back-to-back runs. On any other day it fails on a
-- healthy warehouse, and because a failed check aborts the script, the
-- checks after it never run.
--
-- rows_upserted IS NULL is an explicit failure. A succeeded run that never
-- recorded how many rows it moved is a broken run record.
DO $$
DECLARE
    rewritten bigint;
    unrecorded bigint;
    example text;
BEGIN
    WITH latest AS (
        SELECT DISTINCT ON (product_code, granularity_seconds)
            product_code, granularity_seconds, run_id, rows_upserted
        FROM raw.ingest_runs
        WHERE status = 'succeeded'
        ORDER BY product_code, granularity_seconds, run_id DESC
    ),
    rewritten_rows AS (
        SELECT l.product_code, l.granularity_seconds, c.ts_utc
        FROM latest l
        JOIN core.assets a ON a.product_code = l.product_code
        JOIN core.ohlcv c
          ON c.asset_id = a.asset_id
         AND c.granularity_seconds = l.granularity_seconds
         AND c.source_run_id = l.run_id
        JOIN LATERAL (
            SELECT r.open, r.high, r.low, r.close, r.volume
            FROM raw.candles r
            JOIN raw.ingest_runs pr
              ON pr.run_id = r.run_id
             AND pr.status = 'succeeded'
             AND pr.granularity_seconds = l.granularity_seconds
            WHERE r.product_code = l.product_code
              AND r.bucket_start = c.ts_utc
              AND r.run_id < l.run_id
            ORDER BY r.raw_id DESC
            LIMIT 1
        ) previous ON true
        WHERE c.updated_at > c.inserted_at
          AND (previous.open, previous.high, previous.low, previous.close, previous.volume)
              IS NOT DISTINCT FROM
              (c.open, c.high, c.low, c.close, c.volume)
    )
    SELECT
        (SELECT count(*) FROM rewritten_rows),
        (SELECT count(*) FROM latest WHERE rows_upserted IS NULL),
        (SELECT format('%s at %s (granularity %s s)', product_code, ts_utc, granularity_seconds)
         FROM rewritten_rows ORDER BY product_code, granularity_seconds, ts_utc LIMIT 1)
    INTO rewritten, unrecorded, example;

    IF unrecorded > 0 THEN
        RAISE EXCEPTION 'idempotency check failed: % succeeded run(s) recorded no upsert count (see raw.ingest_runs)',
            unrecorded;
    END IF;
    IF rewritten > 0 THEN
        RAISE EXCEPTION 'idempotency check failed: the latest run rewrote % row(s) whose values had not changed (e.g. %)',
            rewritten, example;
    END IF;
    RAISE NOTICE 'idempotency check passed: the latest run per product and timeframe rewrote no unchanged row';
END $$;

-- Check 5: the newest run per product and timeframe actually succeeded.
--
-- Split out from check 4 rather than folded into it. Check 4 filters to
-- status = 'succeeded' INSIDE its DISTINCT ON, which means that if the most
-- recent run for a product failed, check 4 quietly validates the last run
-- that worked and reports a pass. That is the worst possible behavior for a
-- pipeline gate: it passes precisely when the pipeline is broken. This
-- check looks at the newest run per product with no status filter at all.
DO $$
DECLARE
    offending bigint;
    detail text;
BEGIN
    WITH newest_run AS (
        SELECT DISTINCT ON (product_code, granularity_seconds)
            product_code, granularity_seconds, run_id, status
        FROM raw.ingest_runs
        ORDER BY product_code, granularity_seconds, run_id DESC
    )
    SELECT count(*),
           string_agg(format('%s at %s s (run %s: %s)', product_code, granularity_seconds,
                             run_id, status),
                      ', ' ORDER BY product_code, granularity_seconds)
    INTO offending, detail
    FROM newest_run
    WHERE status <> 'succeeded';

    IF offending > 0 THEN
        RAISE EXCEPTION 'freshness check failed: newest run did not succeed for % series: %',
            offending, detail;
    END IF;
    RAISE NOTICE 'freshness check passed: newest run per product and timeframe succeeded';
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
-- pg_get_viewdef, so it tests the definition the database holds now. It
-- runs that query once under America/Chicago and once under UTC and requires
-- the two results to be identical row for row. Failure case constructed per the
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
    -- set_config(..., is_local => true): both overrides end with the enclosing
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

-- Check 7: the stored marts equal their own defining queries run now.
--
-- Checks 2 and 6 both pass on a stale mart. Check 2 compares only which
-- (asset, day) pairs exist, and check 6 compares two fresh runs with each
-- other. A load with --no-refresh-marts, or a refresh that failed, leaves
-- stored rows that no longer match the hourly facts on a day the mart
-- already holds. Here each matview's stored rows are compared with a fresh
-- run of its definition, read from the catalog. daily_ohlcv and
-- daily_metrics are compared row for row. The correlation mart is refreshed
-- in the same transaction as daily_metrics and is left out, because its
-- float aggregate is not guaranteed to reproduce bit for bit.
DO $$
DECLARE
    mart text;
    differing bigint;
BEGIN
    FOREACH mart IN ARRAY ARRAY['marts.daily_ohlcv', 'marts.daily_metrics'] LOOP
        EXECUTE format(
            'WITH fresh AS (%s) '
            'SELECT (SELECT count(*) FROM (SELECT * FROM %s EXCEPT SELECT * FROM fresh) s) '
            '     + (SELECT count(*) FROM (SELECT * FROM fresh EXCEPT SELECT * FROM %s) f)',
            rtrim(pg_get_viewdef(mart::regclass), '; '), mart, mart)
        INTO differing;
        IF differing > 0 THEN
            RAISE EXCEPTION 'stale-mart check failed: % stored row(s) of % differ from its definition run now; refresh the marts',
                differing, mart;
        END IF;
    END LOOP;
    RAISE NOTICE 'stale-mart check passed: daily_ohlcv and daily_metrics match their definitions run now';
END $$;
