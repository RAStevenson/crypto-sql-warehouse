-- No active series has stopped loading.
--
-- An active series (active asset at an active timeframe) whose newest
-- candle trails the newest candle of any active series by more than two
-- days has stopped loading. Two days, because a daily schedule plus a load
-- of a single product can legitimately leave the others a day behind. The
-- series end within an hour of each other, since each load stops at the
-- last closed bucket of its timeframe. The gap test cannot see this case,
-- because each series' expected buckets run only to its own last candle.
--
-- An active series that holds no candle at all is reported as well, such as
-- an asset registered with run_add_asset.py --no-backfill and not yet
-- loaded, or a timeframe switched on and never loaded. The query builds its
-- list of series from the active assets and timeframes, not from
-- core.ohlcv, so an empty series cannot drop out of the comparison.
WITH active_series AS (
    SELECT a.asset_id, a.product_code, t.granularity_seconds, t.timeframe_code
    FROM {{ source('core', 'assets') }} a
    CROSS JOIN {{ source('core', 'timeframes') }} t
    WHERE a.is_active AND t.is_active
),
newest AS (
    SELECT s.product_code, s.timeframe_code, max(o.ts_utc) AS last_ts
    FROM active_series s
    LEFT JOIN {{ source('core', 'ohlcv') }} o
      ON o.asset_id = s.asset_id
     AND o.granularity_seconds = s.granularity_seconds
    GROUP BY s.product_code, s.timeframe_code
)
SELECT
    product_code,
    timeframe_code,
    last_ts AS newest_candle,
    'more than 48 hours behind the newest active series' AS reason
FROM newest
WHERE last_ts < (SELECT max(last_ts) FROM newest) - interval '48 hours'
UNION ALL
SELECT
    product_code,
    timeframe_code,
    last_ts AS newest_candle,
    'active but holds no candles; load it or switch it off' AS reason
FROM newest
WHERE last_ts IS NULL
