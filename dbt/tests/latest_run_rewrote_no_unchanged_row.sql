-- Idempotency. The latest succeeded run per product and timeframe must not
-- have rewritten a row whose values did not change.
--
-- The upsert's IS DISTINCT FROM guard is what makes a rerun leave core
-- alone, and this is the state a broken guard would leave behind: a core
-- row whose source_run_id is the latest run, that was updated rather than
-- inserted (updated_at later than inserted_at), and whose values equal the
-- copy an earlier succeeded run had already promoted. Raw keeps every copy,
-- so the comparison needs no second run and holds after an ordinary daily
-- load.
--
-- A count-based form (the latest run upserted at most one row) is valid
-- only straight after two back-to-back runs. On any other day it fails on a
-- healthy warehouse.
--
-- A succeeded run that recorded no upsert count is returned as well. A run
-- record that never said how many rows it moved is a broken run record.
WITH latest AS (
    SELECT DISTINCT ON (product_code, granularity_seconds)
        product_code, granularity_seconds, run_id, rows_upserted
    FROM {{ source('raw', 'ingest_runs') }}
    WHERE status = 'succeeded'
    ORDER BY product_code, granularity_seconds, run_id DESC
)
SELECT l.product_code, l.granularity_seconds, c.ts_utc AS bucket_start,
       'the latest run rewrote this row although its values had not changed' AS reason
FROM latest l
JOIN {{ source('core', 'assets') }} a ON a.product_code = l.product_code
JOIN {{ source('core', 'ohlcv') }} c
  ON c.asset_id = a.asset_id
 AND c.granularity_seconds = l.granularity_seconds
 AND c.source_run_id = l.run_id
JOIN LATERAL (
    SELECT r.open, r.high, r.low, r.close, r.volume
    FROM {{ source('raw', 'candles') }} r
    JOIN {{ source('raw', 'ingest_runs') }} pr
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
UNION ALL
SELECT product_code, granularity_seconds, NULL::timestamptz,
       'succeeded run recorded no upsert count' AS reason
FROM latest
WHERE rows_upserted IS NULL
