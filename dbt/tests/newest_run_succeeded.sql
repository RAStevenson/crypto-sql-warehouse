-- The newest run per product and timeframe actually succeeded.
--
-- Kept apart from the idempotency test on purpose. That test filters to
-- status = 'succeeded' inside its DISTINCT ON, so if the most recent run for
-- a series failed, it quietly judges the last run that worked and passes.
-- That is the worst behavior a pipeline gate can have: it passes precisely
-- when the pipeline is broken. This test looks at the newest run per series
-- with no status filter at all.
WITH newest_run AS (
    SELECT DISTINCT ON (product_code, granularity_seconds)
        product_code, granularity_seconds, run_id, status
    FROM {{ source('raw', 'ingest_runs') }}
    ORDER BY product_code, granularity_seconds, run_id DESC
)
SELECT product_code, granularity_seconds, run_id, status,
       'the newest run for this series did not succeed' AS reason
FROM newest_run
WHERE status <> 'succeeded'
