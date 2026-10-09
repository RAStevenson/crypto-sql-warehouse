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
--
-- Day completeness
-- ----------------
-- The loaded window starts and ends mid-day, and the exchange has real
-- outages, so some (asset, day) pairs are built from fewer than 24 hourly
-- candles. At the time of writing that is the first day of history, the
-- trailing day (however far the last load got), and 2026-05-08, where an
-- exchange-wide gap left 19 hours.
--
-- A partial day is not a small day, it is a different measurement. Its
-- volume is the sum of however many hours happened to land, and its close
-- is whatever the last observed hour printed rather than the 23:00 candle.
-- Whether that produces an up day or a down day, a heavy day or a quiet
-- one, depends on when the load ran, not on the market. Rolling averages,
-- volatility and drawdown built on top of those bars inherit the
-- distortion and carry it forward for as many rows as the window is wide.
--
-- The rule this layer follows, stated once and applied everywhere:
--
--   daily_ohlcv carries every day it observed, plus hours_present and an
--   explicit is_complete flag. Nothing is hidden.
--
--   daily_metrics carries every one of those rows too, but computes its
--   derived columns (returns, moving averages, volatility, drawdown,
--   dollar volume) over complete days ONLY. On an incomplete day the OHLCV
--   is reported as observed and every derived column is NULL.
--
-- Tradeoff, stated plainly: skipping incomplete days means a rolling
-- 30-observation window can span more than 30 calendar days, and the day
-- after a gap would otherwise carry a multi-day return mislabeled as a
-- daily one. The second problem is handled explicitly (see the adjacency
-- guard in daily_metrics); the first is accepted, because a 30-observation
-- window of clean bars is more useful than a 30-calendar-day window with
-- contaminated ones. Consumers that want calendar-exact windows should
-- reject the data set until the gap is backfilled, not average through it.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- marts.daily_ohlcv: the 1-hour series rolled up to trading days (UTC).
--
-- Daily bars are derived from hourly rather than fetched separately so both
-- granularities come from one source; a discrepancy between them can then
-- only be a rollup bug, never a data-source mismatch. Every mart builds
-- from the 1-hour series alone. The finer timeframes in core.ohlcv are for
-- intraday questions and the CSV export, and check 8 in 05_verify.sql holds
-- them against the hourly bars.
--
-- open/close use ordered array_agg: aggregates cannot say "first row by
-- time" directly, and this idiom avoids a second pass with window functions.
-- ts_utc is unique within a (asset, day) group, so the ordering is total and
-- the result is deterministic.
--
-- The bucket expression is (ts_utc AT TIME ZONE 'UTC')::date, not
-- ts_utc::date. The AT TIME ZONE conversion pins the day boundary to UTC
-- regardless of the reader's session timezone; a plain cast would silently
-- re-bucket the whole mart for a client connecting from Chicago.
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
    count(*)                                     AS hours_present,
    count(*) = 24                                AS is_complete
FROM core.ohlcv
WHERE granularity_seconds = 3600
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
-- ROWS frames, not RANGE, and written out in full including the cumulative
-- frame under drawdown_from_peak. With one row per (asset, day) the two are
-- equivalent in result, but RANGE is the silent default and spelling ROWS
-- makes the "N observations" intent explicit.
--
-- Minimum-observation guards. A window frame of "29 PRECEDING AND CURRENT
-- ROW" emits a value from row 1 of every partition, computed over one
-- observation, under a column literally named ma_30d. Roughly half the
-- non-null volatility_30d_ann rows were warm-up values of that kind. BTC's
-- first published figure was 0.2076 against 0.4043 for its first genuine
-- 30-observation window: not a rounding difference, a different quantity
-- published under the same column name. The CASE clauses make the column
-- name true. No value is published until the window it claims is actually
-- full, and warm-up rows are NULL.
--
-- Adjacency guard on the return. Incomplete days are excluded from the
-- return series, so lag() on the day after a gap would reach back two or
-- more calendar days and label the result a daily return. The
-- trade_date = prev_date + 1 test suppresses that; one extra NULL return per
-- gap is cheaper than one wrong number. For volatility_30d_ann the cost is
-- thirty rows, not one: that NULL return stays inside the 30-row frame for
-- the day after the gap and the 29 complete days that follow, and no
-- volatility is published until it has left the frame.
--
-- drawdown_from_peak is measured from the running peak WITHIN THE LOADED
-- WINDOW, not from an all-time high. A drawdown of -0.49 means "49% below
-- the highest close seen since the first day of loaded history", which is
-- not the statistic a trading desk means by max drawdown. Backfilling
-- earlier history would change every value in this column.
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW marts.daily_metrics AS
WITH complete_days AS (
    -- The single point where incompleteness is filtered. Everything
    -- downstream in this view is computed over clean bars only.
    SELECT asset_id, trade_date, close
    FROM marts.daily_ohlcv
    WHERE is_complete
),
returns AS (
    SELECT
        asset_id,
        trade_date,
        close,
        CASE WHEN trade_date = lag(trade_date) OVER w + 1
             THEN close / NULLIF(lag(close) OVER w, 0) - 1 END  AS simple_return,
        CASE WHEN trade_date = lag(trade_date) OVER w + 1
             THEN ln(close / NULLIF(lag(close) OVER w, 0)) END  AS log_return
    FROM complete_days
    WINDOW w AS (PARTITION BY asset_id ORDER BY trade_date)
),
metrics AS (
    SELECT
        asset_id,
        trade_date,
        simple_return,
        log_return,
        CASE WHEN count(close) OVER w7  >= 7
             THEN avg(close) OVER w7  END                       AS ma_7d,
        CASE WHEN count(close) OVER w30 >= 30
             THEN avg(close) OVER w30 END                       AS ma_30d,
        CASE WHEN count(log_return) OVER w30 >= 30
             THEN stddev_samp(log_return) OVER w30 * sqrt(365) END
                                                                AS volatility_30d_ann,
        close / max(close) OVER wcum - 1                        AS drawdown_from_peak
    FROM returns
    WINDOW
        w7   AS (PARTITION BY asset_id ORDER BY trade_date
                 ROWS BETWEEN 6 PRECEDING AND CURRENT ROW),
        w30  AS (PARTITION BY asset_id ORDER BY trade_date
                 ROWS BETWEEN 29 PRECEDING AND CURRENT ROW),
        wcum AS (PARTITION BY asset_id ORDER BY trade_date
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
)
SELECT
    d.asset_id,
    d.trade_date,
    d.open, d.high, d.low, d.close, d.volume,
    d.hours_present,
    d.is_complete,
    m.simple_return,
    m.log_return,
    m.ma_7d,
    m.ma_30d,
    m.volatility_30d_ann,
    m.drawdown_from_peak,
    -- Notional traded, approximated as base volume x closing price. The
    -- exact figure would need per-trade prices, which the candle API does
    -- not return. Measured 2026-08-04 against volume x (high+low+close)/3
    -- over the loaded data set, the error is -0.04% on average with a
    -- per-day range of -4.16% to +3.14%: fine for ranking and anomaly
    -- detection, wrong for anything needing an accounting-grade notional.
    -- NULL on incomplete days, like every other derived column here.
    CASE WHEN d.is_complete THEN d.volume * d.close END AS dollar_volume_approx
-- LEFT JOIN, not a filter: incomplete days keep their row and their OHLCV,
-- with every derived column NULL. No day disappears without explanation.
FROM marts.daily_ohlcv d
LEFT JOIN metrics m ON m.asset_id = d.asset_id AND m.trade_date = d.trade_date;

CREATE UNIQUE INDEX daily_metrics_pk ON marts.daily_metrics (asset_id, trade_date);

-- -----------------------------------------------------------------------------
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
WHERE ra.trade_date > (SELECT max(trade_date) FROM marts.daily_metrics
                       WHERE log_return IS NOT NULL) - 90
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
    m.hours_present,
    m.is_complete
FROM marts.daily_metrics m
JOIN core.assets a USING (asset_id);
