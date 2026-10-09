-- The daily rollup covers exactly the (asset, day) pairs present in the
-- 1-hour series. No dropped days, no invented ones.
--
-- This compares SETS, not cardinalities. Comparing count(daily_ohlcv)
-- against count(DISTINCT (asset, day)) in core leaves both integers equal
-- when one real day is deleted and one fake day invented, so that form
-- passes on a mart that is wrong in two places at once. The EXCEPT runs in
-- both directions and the reason column says which side is at fault.
WITH hourly_pairs AS (
    SELECT DISTINCT asset_id, (ts_utc AT TIME ZONE 'UTC')::date AS trade_date
    FROM {{ source('core', 'ohlcv') }}
    WHERE granularity_seconds = 3600
),
mart_pairs AS (
    SELECT asset_id, trade_date FROM {{ ref('daily_ohlcv') }}
)
SELECT asset_id, trade_date, 'in the 1-hour series but not in daily_ohlcv' AS reason
FROM (SELECT * FROM hourly_pairs EXCEPT SELECT * FROM mart_pairs) AS missing_from_mart
UNION ALL
SELECT asset_id, trade_date, 'in daily_ohlcv with no hourly candles behind it' AS reason
FROM (SELECT * FROM mart_pairs EXCEPT SELECT * FROM hourly_pairs) AS not_in_hourly
