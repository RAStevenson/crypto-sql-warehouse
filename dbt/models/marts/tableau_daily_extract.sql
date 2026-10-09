{{ config(materialized='view') }}

-- marts.tableau_daily_extract: the single wide object Tableau connects to.
--
-- A plain view over daily_metrics: no extra materialization needed since the
-- matview underneath is already computed. Column names are human-readable
-- because they become Tableau field names. Long format, one row per asset
-- per day, which Tableau prefers over one-column-per-asset wide format.
--
-- No completeness filter here. Dropping partial days (a filter such as
-- hours_present < 20) would let a day vanish from a dashboard with nothing to
-- indicate it had ever existed. Completeness is a property of the row
-- (is_complete, hours_present) rather than a filter buried in a view:
-- incomplete days arrive with their observed OHLCV and NULL derived metrics,
-- so a line chart of ma_30d or simple_return breaks visibly at the gap
-- instead of interpolating over it. Dashboards that want bars only for clean
-- days filter on is_complete, and that choice is then visible in the workbook
-- rather than invisible in the warehouse.
--
-- drawdown_from_peak here inherits the caveat from daily_metrics: the peak
-- is the highest close in the loaded window, not an all-time high.
SELECT
    a.symbol,
    a.asset_name,
    m.trade_date,
    m.open,
    m.high,
    m.low,
    m.close,
    m.volume                 AS volume_base_units,
    m.dollar_volume_approx,
    m.simple_return,
    m.ma_7d,
    m.ma_30d,
    m.volatility_30d_ann,
    m.drawdown_from_peak,
    m.hours_present,
    m.is_complete
FROM {{ ref('daily_metrics') }} m
JOIN {{ source('core', 'assets') }} a USING (asset_id)
