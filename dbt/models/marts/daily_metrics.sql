{{ config(
    indexes=[{'columns': ['asset_id', 'trade_date'], 'unique': True}]
) }}

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
-- full, and warm-up rows are NULL. dbt/tests/rolling_metrics_wait_for_a_full_window.sql
-- holds the mart to that rule.
--
-- Adjacency guard on the return. Incomplete days are excluded from the
-- return series, so lag() on the day after a gap would reach back two or
-- more calendar days and label the result a daily return. The
-- trade_date = prev_date + 1 test suppresses that; one extra NULL return per
-- gap is cheaper than one wrong number. For volatility_30d_ann the cost is
-- thirty rows, not one: that NULL return stays inside the 30-row frame for
-- the day after the gap and the 29 complete days that follow, and no
-- volatility is published until it has left the frame.
-- dbt/tests/returns_need_an_adjacent_complete_day.sql holds the mart to that rule.
--
-- drawdown_from_peak is measured from the running peak WITHIN THE LOADED
-- WINDOW, not from an all-time high. A drawdown of -0.49 means "49% below
-- the highest close seen since the first day of loaded history", which is
-- not the statistic a trading desk means by max drawdown. Backfilling
-- earlier history would change every value in this column.
WITH complete_days AS (
    -- The single point where incompleteness is filtered. Everything
    -- downstream in this view is computed over clean bars only.
    SELECT asset_id, trade_date, close
    FROM {{ ref('daily_ohlcv') }}
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
-- dbt/tests/incomplete_days_carry_no_derived_metrics.sql holds the mart to that.
FROM {{ ref('daily_ohlcv') }} d
LEFT JOIN metrics m ON m.asset_id = d.asset_id AND m.trade_date = d.trade_date
