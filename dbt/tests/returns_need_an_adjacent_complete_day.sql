-- A daily return exists exactly when the complete day directly before it
-- exists.
--
-- Incomplete days are excluded from the return series, so a return on the
-- day after a gap would be a multi-day move labeled as one day. The mart's
-- adjacency guard suppresses it. This holds the mart to that rule in both
-- directions: no return across a gap, and no missing return where the day
-- before is complete.
SELECT m.asset_id, m.trade_date,
       'return published with no complete day directly before it' AS reason
FROM {{ ref('daily_metrics') }} m
WHERE m.simple_return IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM {{ ref('daily_metrics') }} previous
      WHERE previous.asset_id = m.asset_id
        AND previous.trade_date = m.trade_date - 1
        AND previous.is_complete)
UNION ALL
SELECT m.asset_id, m.trade_date,
       'complete day with a complete day before it but no return' AS reason
FROM {{ ref('daily_metrics') }} m
WHERE m.is_complete
  AND m.simple_return IS NULL
  AND EXISTS (
      SELECT 1 FROM {{ ref('daily_metrics') }} previous
      WHERE previous.asset_id = m.asset_id
        AND previous.trade_date = m.trade_date - 1
        AND previous.is_complete)
