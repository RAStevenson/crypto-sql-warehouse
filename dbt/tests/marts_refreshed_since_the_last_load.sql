-- The stored marts were refreshed after the newest load.
--
-- The rollup-coverage test passes on a stale mart as long as no whole day
-- is missing, and `dbt test` on its own refreshes nothing. A load run with
-- --no-refresh-marts, or a refresh that failed, leaves daily_ohlcv holding
-- yesterday's count of hours for the newest day. So for each asset, the
-- newest day in the 1-hour series must appear in daily_ohlcv with the same
-- number of hours core holds for it, and daily_metrics must carry every
-- daily_ohlcv row with the same hour count, since the two refresh in
-- separate transactions and the second could have failed alone.
WITH hours_in_core AS (
    SELECT asset_id, (ts_utc AT TIME ZONE 'UTC')::date AS trade_date, count(*) AS hours_present
    FROM {{ source('core', 'ohlcv') }}
    WHERE granularity_seconds = 3600
    GROUP BY asset_id, (ts_utc AT TIME ZONE 'UTC')::date
),
newest_day AS (
    SELECT DISTINCT ON (asset_id) asset_id, trade_date, hours_present
    FROM hours_in_core
    ORDER BY asset_id, trade_date DESC
)
SELECT n.asset_id, n.trade_date,
       'daily_ohlcv does not hold the newest day as core has it; refresh the marts' AS reason
FROM newest_day n
LEFT JOIN {{ ref('daily_ohlcv') }} d USING (asset_id, trade_date)
WHERE d.hours_present IS DISTINCT FROM n.hours_present
UNION ALL
SELECT asset_id, trade_date,
       'daily_metrics and daily_ohlcv disagree; refresh the marts' AS reason
FROM (
    SELECT asset_id, trade_date, hours_present FROM {{ ref('daily_ohlcv') }}
    EXCEPT
    SELECT asset_id, trade_date, hours_present FROM {{ ref('daily_metrics') }}
) AS only_in_ohlcv
