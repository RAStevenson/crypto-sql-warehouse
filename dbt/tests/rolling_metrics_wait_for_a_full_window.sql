-- A rolling column is NULL until its window is full, and never after.
--
-- ma_7d needs 7 complete days, ma_30d needs 30, and volatility_30d_ann
-- needs 30 complete days that each have a return. The frames count complete
-- days only, because daily_metrics computes its windows over complete days
-- alone. Both directions are tested: a value published early is the
-- warm-up bug the mart's comment describes, and a NULL once the window is
-- full would mean the guard is wrong the other way.
WITH complete_rows AS (
    SELECT
        asset_id,
        trade_date,
        ma_7d,
        ma_30d,
        volatility_30d_ann,
        count(*) OVER w7             AS closes_in_frame_7,
        count(*) OVER w30            AS closes_in_frame_30,
        count(log_return) OVER w30   AS returns_in_frame_30
    FROM {{ ref('daily_metrics') }}
    WHERE is_complete
    WINDOW
        w7  AS (PARTITION BY asset_id ORDER BY trade_date
                ROWS BETWEEN 6 PRECEDING AND CURRENT ROW),
        w30 AS (PARTITION BY asset_id ORDER BY trade_date
                ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
)
SELECT asset_id, trade_date, 'ma_7d published with fewer than 7 complete days' AS reason
FROM complete_rows WHERE closes_in_frame_7 < 7 AND ma_7d IS NOT NULL
UNION ALL
SELECT asset_id, trade_date, 'ma_7d missing with 7 complete days in frame' AS reason
FROM complete_rows WHERE closes_in_frame_7 = 7 AND ma_7d IS NULL
UNION ALL
SELECT asset_id, trade_date, 'ma_30d published with fewer than 30 complete days' AS reason
FROM complete_rows WHERE closes_in_frame_30 < 30 AND ma_30d IS NOT NULL
UNION ALL
SELECT asset_id, trade_date, 'ma_30d missing with 30 complete days in frame' AS reason
FROM complete_rows WHERE closes_in_frame_30 = 30 AND ma_30d IS NULL
UNION ALL
SELECT asset_id, trade_date, 'volatility_30d_ann published with fewer than 30 returns' AS reason
FROM complete_rows WHERE returns_in_frame_30 < 30 AND volatility_30d_ann IS NOT NULL
UNION ALL
SELECT asset_id, trade_date, 'volatility_30d_ann missing with 30 returns in frame' AS reason
FROM complete_rows WHERE returns_in_frame_30 = 30 AND volatility_30d_ann IS NULL
