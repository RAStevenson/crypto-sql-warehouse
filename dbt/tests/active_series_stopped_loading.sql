-- No active series has stopped loading.
--
-- An active series (active asset at an active timeframe) whose newest
-- candle trails the newest candle of any active series by more than two
-- days has stopped loading. Two days, because a daily schedule plus a load
-- of a single product can legitimately leave the others a day behind. The
-- series end within an hour of each other, since each load stops at the
-- last closed bucket of its timeframe. The gap test cannot see this case,
-- because each series' expected buckets run only to its own last candle.
WITH newest AS (
    SELECT a.product_code, t.timeframe_code, max(o.ts_utc) AS last_ts
    FROM {{ source('core', 'assets') }} a
    JOIN {{ source('core', 'ohlcv') }} o USING (asset_id)
    JOIN {{ source('core', 'timeframes') }} t USING (granularity_seconds)
    WHERE a.is_active AND t.is_active
    GROUP BY a.product_code, t.timeframe_code
)
SELECT
    product_code,
    timeframe_code,
    last_ts AS newest_candle,
    'more than 48 hours behind the newest active series' AS reason
FROM newest
WHERE last_ts < (SELECT max(last_ts) FROM newest) - interval '48 hours'
