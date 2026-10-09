-- On an incomplete day every derived column is NULL.
--
-- daily_metrics states this rule in its own comment: the OHLCV of a day
-- built from fewer than 24 hourly candles is reported as observed, and its
-- return, moving averages, volatility, drawdown, and dollar volume are
-- NULL, so a partial day cannot leak into any rolling figure. This holds
-- the mart to that rule.
SELECT asset_id, trade_date, hours_present,
       'a derived column has a value on an incomplete day' AS reason
FROM {{ ref('daily_metrics') }}
WHERE NOT is_complete
  AND (simple_return IS NOT NULL
       OR log_return IS NOT NULL
       OR ma_7d IS NOT NULL
       OR ma_30d IS NOT NULL
       OR volatility_30d_ann IS NOT NULL
       OR drawdown_from_peak IS NOT NULL
       OR dollar_volume_approx IS NOT NULL)
