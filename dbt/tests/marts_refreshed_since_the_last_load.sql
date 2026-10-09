-- The stored marts were refreshed after the newest load.
--
-- A load run with --no-refresh-marts, or a refresh that failed, leaves the
-- marts holding what core held before, and `dbt test` on its own refreshes
-- nothing. A check on hour counts would miss a load that changed a candle's
-- values without adding an hour, such as a revised candle in the three-hour
-- overlap. A check on the newest day would miss a --recheck-gaps run that
-- fills an hour on an older day. So daily_ohlcv must hold exactly the rollup
-- of core as it is now, day for day in both directions and in every column
-- but is_complete, which follows from hours_present. daily_metrics must
-- match daily_ohlcv row for row in those same columns, since the two refresh
-- in separate transactions and the second could have failed alone. The
-- rollup here repeats the one in daily_ohlcv.sql on purpose, because the
-- question is whether the stored mart is current, not whether its query is
-- right. A change to that rollup has to be made here too, or this test fails
-- on a freshly refreshed mart.
WITH rollup_in_core AS (
    SELECT
        asset_id,
        (ts_utc AT TIME ZONE 'UTC')::date            AS trade_date,
        (array_agg(open  ORDER BY ts_utc ASC ))[1]   AS open,
        max(high)                                    AS high,
        min(low)                                     AS low,
        (array_agg(close ORDER BY ts_utc DESC))[1]   AS close,
        sum(volume)                                  AS volume,
        count(*)                                     AS hours_present
    FROM {{ source('core', 'ohlcv') }}
    WHERE granularity_seconds = 3600
    GROUP BY asset_id, (ts_utc AT TIME ZONE 'UTC')::date
),
stored_ohlcv AS (
    SELECT asset_id, trade_date, open, high, low, close, volume, hours_present
    FROM {{ ref('daily_ohlcv') }}
),
stored_metrics AS (
    SELECT asset_id, trade_date, open, high, low, close, volume, hours_present
    FROM {{ ref('daily_metrics') }}
)
SELECT asset_id, trade_date,
       'daily_ohlcv does not match core for this day; refresh the marts' AS reason
FROM (
    (SELECT * FROM rollup_in_core EXCEPT SELECT * FROM stored_ohlcv)
    UNION
    (SELECT * FROM stored_ohlcv EXCEPT SELECT * FROM rollup_in_core)
) AS differs_from_core
UNION ALL
SELECT asset_id, trade_date,
       'daily_metrics and daily_ohlcv disagree; refresh the marts' AS reason
FROM (
    (SELECT * FROM stored_ohlcv EXCEPT SELECT * FROM stored_metrics)
    UNION
    (SELECT * FROM stored_metrics EXCEPT SELECT * FROM stored_ohlcv)
) AS differs_between_marts
