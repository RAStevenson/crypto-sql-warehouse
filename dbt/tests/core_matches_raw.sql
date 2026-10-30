-- Core matches raw, in value and in membership.
--
-- Three assertions in one test, because the value comparison is an INNER
-- JOIN and an inner join can only see rows that exist on both sides. A core
-- row with no raw backing (hand-edited, or promoted from a run whose raw
-- rows were later removed) and a raw bucket that was never promoted are
-- both invisible to it, so a membership check runs in each direction beside
-- it. A bucket the loader left in raw because core's constraints reject it
-- fails here too, as never promoted or, when core holds an earlier copy, as
-- a mismatch. A raw row's timeframe comes from its run, so every comparison
-- is per (asset, timeframe, bucket).
WITH newest_raw AS (
    SELECT DISTINCT ON (a.asset_id, ir.granularity_seconds, r.bucket_start)
        a.asset_id, ir.granularity_seconds, r.bucket_start,
        r.open, r.high, r.low, r.close, r.volume
    FROM {{ source('raw', 'candles') }} r
    JOIN {{ source('raw', 'ingest_runs') }} ir ON ir.run_id = r.run_id
    JOIN {{ source('core', 'assets') }} a ON a.product_code = r.product_code
    ORDER BY a.asset_id, ir.granularity_seconds, r.bucket_start, r.raw_id DESC
)
SELECT o.asset_id, o.granularity_seconds, o.ts_utc AS bucket_start,
       'core row disagrees with the newest raw copy; a load whose window covers the bucket promotes it' AS reason
FROM {{ source('core', 'ohlcv') }} o
JOIN newest_raw nr
  ON nr.asset_id = o.asset_id
 AND nr.granularity_seconds = o.granularity_seconds
 AND nr.bucket_start = o.ts_utc
WHERE (o.open, o.high, o.low, o.close, o.volume)
      IS DISTINCT FROM
      (nr.open, nr.high, nr.low, nr.close, nr.volume)
UNION ALL
SELECT o.asset_id, o.granularity_seconds, o.ts_utc,
       'core row has no raw backing at all' AS reason
FROM {{ source('core', 'ohlcv') }} o
LEFT JOIN newest_raw nr
  ON nr.asset_id = o.asset_id
 AND nr.granularity_seconds = o.granularity_seconds
 AND nr.bucket_start = o.ts_utc
WHERE nr.asset_id IS NULL
UNION ALL
SELECT nr.asset_id, nr.granularity_seconds, nr.bucket_start,
       'raw bucket was never promoted into core; re-run the load that fetched it' AS reason
FROM newest_raw nr
LEFT JOIN {{ source('core', 'ohlcv') }} o
  ON o.asset_id = nr.asset_id
 AND o.granularity_seconds = nr.granularity_seconds
 AND o.ts_utc = nr.bucket_start
WHERE o.asset_id IS NULL
