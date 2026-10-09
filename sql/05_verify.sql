-- The one warehouse check that is not a dbt test.
--
-- Usage:
--   python scripts/run_verify.py          (runs `dbt test` first, then this file)
--   psql -d crypto_warehouse -v ON_ERROR_STOP=1 -f sql/05_verify.sql
--
-- Every other check on the warehouse is a dbt test under dbt/tests/ or in
-- the dbt yml files, one SELECT each, returning the rows that break a rule.
-- This check cannot be written that way, because it has to run the same
-- query twice under two session timezones, and a session setting cannot
-- change partway through one SELECT. So it stays a DO block, which raises an
-- exception on failure and prints a NOTICE with its numbers on success.
--
-- The design rule the dbt tests and this block share is that an assertion
-- is only worth having if it fails on the bad state it claims to detect.

-- The timezone check: the daily rollup buckets identically under any session
-- timezone.
--
-- Regression test for a bug this warehouse actually had: bucketing a
-- timestamptz with a plain ::date cast (or date_trunc()) uses the SESSION
-- timezone, so a client connecting from America/Chicago got day boundaries at
-- 05:00/06:00 UTC and different daily volumes. The fix pins the boundary in
-- the rollup expression itself, (ts_utc AT TIME ZONE 'UTC')::date in
-- dbt/models/marts/daily_ohlcv.sql, with the database default of UTC set in
-- 01_schema.sql only as a second layer, since any client can override it
-- with SET timezone.
--
-- The check reads the mart's defining query live from the catalog with
-- pg_get_viewdef, so it tests the definition the database holds now. It
-- runs that query once under America/Chicago and once under UTC and requires
-- the two results to be identical row for row. Failure case constructed per the
-- design rule above: with marts.daily_ohlcv redefined to bucket by ts_utc::date
-- inside a rolled-back transaction, this check reported all 708 rows differing
-- on each side (moving the boundary by 5 hours touches nearly every day's
-- hours_present and volume) while the other checks still passed, because the
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
