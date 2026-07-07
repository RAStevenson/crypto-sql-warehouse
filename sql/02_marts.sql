-- =============================================================================
-- crypto-sql-warehouse: marts layer
--
-- Materialized views, not tables maintained by the pipeline: at this data
-- volume a full refresh takes well under a second, so incremental mart
-- maintenance would be complexity with no payoff. Each matview gets a
-- UNIQUE index so REFRESH MATERIALIZED VIEW CONCURRENTLY works (readers,
-- e.g. a live Tableau connection, are never blocked mid-refresh).
--
-- Dependency chain: daily_ohlcv -> daily_metrics -> (correlations, extract).
-- Refresh order in 03_refresh_marts.sql follows that chain.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- marts.daily_ohlcv: hourly facts rolled up to trading days (UTC).
--
-- Daily bars are derived from hourly rather than fetched separately so the
-- warehouse has one source of truth; a discrepancy between granularities
-- can then only be a rollup bug, never a data-source mismatch.
--
-- open/close use ordered array_agg: aggregates cannot say "first row by
-- time" directly, and this idiom avoids a second pass with window functions.
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW marts.daily_ohlcv AS
SELECT
    asset_id,
    (ts_utc AT TIME ZONE 'UTC')::date            AS trade_date,
    (array_agg(open  ORDER BY ts_utc ASC ))[1]   AS open,
    max(high)                                    AS high,
    min(low)                                     AS low,
    (array_agg(close ORDER BY ts_utc DESC))[1]   AS close,
    sum(volume)                                  AS volume,
    count(*)                                     AS hours_present  -- < 24 flags gaps; queries can exclude partial days
FROM core.ohlcv_hourly
GROUP BY asset_id, (ts_utc AT TIME ZONE 'UTC')::date;

CREATE UNIQUE INDEX daily_ohlcv_pk ON marts.daily_ohlcv (asset_id, trade_date);

-- -----------------------------------------------------------------------------
-- marts.daily_metrics: returns, moving averages, rolling volatility, drawdown.
--
-- Log returns rather than simple returns: they sum across periods and are
-- the standard input to volatility estimates. Volatility annualizes with
-- sqrt(365) because crypto trades every day, unlike the sqrt(252) used for
-- equities.
--
-- ROWS frames, not RANGE: with one row per (asset, day) they are equivalent
-- in result, but ROWS makes the "30 observations" intent explicit and is
-- what an interviewer expects you to reach for.
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW marts.daily_metrics AS
WITH returns AS (
    SELECT
        asset_id,
        trade_date,
        open, high, low, close, volume,
        hours_present,
        close / NULLIF(lag(close) OVER w, 0) - 1        AS simple_return,
        ln(close / NULLIF(lag(close) OVER w, 0))        AS log_return
    FROM marts.daily_ohlcv
    WINDOW w AS (PARTITION BY asset_id ORDER BY trade_date)
)
SELECT
    asset_id,
    trade_date,
    open, high, low, close, volume,
    hours_present,
    simple_return,
    log_return,
    avg(close) OVER (PARTITION BY asset_id ORDER BY trade_date
                     ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)    AS ma_7d,
    avg(close) OVER (PARTITION BY asset_id ORDER BY trade_date
                     ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)   AS ma_30d,
    stddev_samp(log_return) OVER (PARTITION BY asset_id ORDER BY trade_date
                     ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
        * sqrt(365)                                               AS volatility_30d_ann,
    close / max(close) OVER (PARTITION BY asset_id ORDER BY trade_date) - 1
                                                                  AS drawdown_from_peak,
    volume * close                                                AS dollar_volume_approx
FROM returns;

CREATE UNIQUE INDEX daily_metrics_pk ON marts.daily_metrics (asset_id, trade_date);

-- -----------------------------------------------------------------------------
-- marts.pairwise_correlations_90d: cross-asset return correlations.
--
-- Self-join on trade_date with asset_id_a < asset_id_b yields each unordered
-- pair once. corr() is Postgres's built-in Pearson aggregate; n_days is kept
-- because a correlation over 12 shared days deserves less trust than one
-- over 90.
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW marts.pairwise_correlations_90d AS
SELECT
    ra.asset_id                       AS asset_id_a,
    rb.asset_id                       AS asset_id_b,
    aa.symbol                         AS symbol_a,
    ab.symbol                         AS symbol_b,
    corr(ra.log_return, rb.log_return) AS return_correlation,
    count(*)                          AS n_days
FROM marts.daily_metrics ra
JOIN marts.daily_metrics rb
  ON rb.trade_date = ra.trade_date
 AND rb.asset_id   > ra.asset_id
JOIN core.assets aa ON aa.asset_id = ra.asset_id
JOIN core.assets ab ON ab.asset_id = rb.asset_id
WHERE ra.trade_date >= (SELECT max(trade_date) FROM marts.daily_metrics) - 90
  AND ra.log_return IS NOT NULL
  AND rb.log_return IS NOT NULL
GROUP BY ra.asset_id, rb.asset_id, aa.symbol, ab.symbol;

CREATE UNIQUE INDEX pairwise_correlations_pk
    ON marts.pairwise_correlations_90d (asset_id_a, asset_id_b);

-- -----------------------------------------------------------------------------
-- marts.tableau_daily_extract: the single wide object Tableau connects to.
--
-- A plain view over daily_metrics: no extra materialization needed since the
-- matview underneath is already computed. Column names are human-readable
-- because they become Tableau field names. One row per asset per day --
-- long format, which Tableau prefers over one-column-per-asset wide format.
-- -----------------------------------------------------------------------------
CREATE VIEW marts.tableau_daily_extract AS
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
    m.hours_present
FROM marts.daily_metrics m
JOIN core.assets a USING (asset_id)
WHERE m.hours_present >= 20;  -- drop badly incomplete days rather than ship misleading bars
