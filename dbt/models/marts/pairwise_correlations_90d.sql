{{ config(
    indexes=[{'columns': ['asset_id_a', 'asset_id_b'], 'unique': True}]
) }}

-- marts.pairwise_correlations_90d: cross-asset return correlations.
--
-- Self-join on trade_date with asset_id_a < asset_id_b yields each unordered
-- pair once. corr() is Postgres's built-in Pearson aggregate; n_days is kept
-- because a correlation over 12 shared days deserves less trust than one
-- over 90.
--
-- The window predicate is trade_date > max - 90, not >= max - 90. The
-- inclusive form spans 91 distinct dates. Measured 2026-08-04 it still
-- reported 90, because the earliest of them had a NULL log_return; that is
-- an accident, not a design. Strict > gives exactly 90 candidate dates.
--
-- The window is anchored on the latest date that has a return, not on
-- max(trade_date). The newest row is normally the day still being loaded,
-- whose log_return is NULL, and a window anchored there spends one of its 90
-- dates on a day that cannot contribute, so n_days never exceeds 89.
--
-- Only complete days contribute, because log_return is NULL on incomplete
-- ones by construction, so n_days still comes in under 90 when the window
-- contains a gap. That is reported rather than hidden.
SELECT
    ra.asset_id                       AS asset_id_a,
    rb.asset_id                       AS asset_id_b,
    aa.symbol                         AS symbol_a,
    ab.symbol                         AS symbol_b,
    corr(ra.log_return, rb.log_return) AS return_correlation,
    count(*)                          AS n_days
FROM {{ ref('daily_metrics') }} ra
JOIN {{ ref('daily_metrics') }} rb
  ON rb.trade_date = ra.trade_date
 AND rb.asset_id   > ra.asset_id
JOIN {{ source('core', 'assets') }} aa ON aa.asset_id = ra.asset_id
JOIN {{ source('core', 'assets') }} ab ON ab.asset_id = rb.asset_id
WHERE ra.trade_date > (SELECT max(trade_date) FROM {{ ref('daily_metrics') }}
                       WHERE log_return IS NOT NULL) - 90
  AND ra.log_return IS NOT NULL
  AND rb.log_return IS NOT NULL
GROUP BY ra.asset_id, rb.asset_id, aa.symbol, ab.symbol
