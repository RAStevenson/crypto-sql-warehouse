-- The newest run per product and timeframe actually succeeded.
--
-- Kept apart from the idempotency test on purpose. That test filters to
-- status = 'succeeded' inside its DISTINCT ON, so if the most recent run for
-- a series failed, it quietly judges the last run that worked and passes.
-- That is the worst behavior a pipeline gate can have: it passes precisely
-- when the pipeline is broken. This test looks at the newest run per series
-- with no status filter at all.
--
-- Only active series are judged (an active asset at an active timeframe).
-- When a series is switched off after its newest run failed, the daily load
-- skips it, so judging it would keep this test failing until someone loaded
-- it by hand. Any bucket that run fetched and core never received is still
-- caught by core_matches_raw.sql.
WITH newest_run AS (
    SELECT DISTINCT ON (product_code, granularity_seconds)
        product_code, granularity_seconds, run_id, status
    FROM {{ source('raw', 'ingest_runs') }}
    ORDER BY product_code, granularity_seconds, run_id DESC
)
SELECT n.product_code, n.granularity_seconds, n.run_id, n.status,
       'the newest run for this series did not succeed' AS reason
FROM newest_run n
JOIN {{ source('core', 'assets') }} a
  ON a.product_code = n.product_code AND a.is_active
JOIN {{ source('core', 'timeframes') }} t
  ON t.granularity_seconds = n.granularity_seconds AND t.is_active
WHERE n.status <> 'succeeded'
