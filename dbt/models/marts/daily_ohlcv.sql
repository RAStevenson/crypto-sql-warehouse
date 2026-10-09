{{ config(
    indexes=[{'columns': ['asset_id', 'trade_date'], 'unique': True}]
) }}

-- marts.daily_ohlcv: the 1-hour series rolled up to trading days (UTC).
--
-- The marts layer, of which this is the first model
-- -------------------------------------------------
-- Materialized views, not tables maintained by the pipeline: at this data
-- volume a full refresh takes seconds, so incremental mart maintenance
-- would be complexity with no payoff. Each view declares a UNIQUE index so
-- REFRESH MATERIALIZED VIEW CONCURRENTLY works (readers, e.g. a live
-- Tableau connection, are never blocked mid-refresh).
--
-- Dependency chain: daily_ohlcv -> daily_metrics -> (correlations, extract).
-- dbt builds and refreshes them in that order, from the ref() calls.
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
--
-- This model
-- ----------
-- Daily bars are derived from hourly rather than fetched separately so both
-- granularities come from one source; a discrepancy between them can then
-- only be a rollup bug, never a data-source mismatch. Every mart builds
-- from the 1-hour series alone. The finer timeframes in core.ohlcv are for
-- intraday questions and the CSV export, and Q21 in sql/04_queries.sql
-- compares them with the hourly bars.
--
-- open/close use ordered array_agg: aggregates cannot say "first row by
-- time" directly, and this idiom avoids a second pass with window functions.
-- ts_utc is unique within a (asset, day) group, so the ordering is total and
-- the result is deterministic.
--
-- The bucket expression is (ts_utc AT TIME ZONE 'UTC')::date, not
-- ts_utc::date. The AT TIME ZONE conversion pins the day boundary to UTC
-- regardless of the reader's session timezone; a plain cast would silently
-- re-bucket the whole mart for a client connecting from Chicago. The
-- timezone check in sql/05_verify.sql re-runs this view's definition under
-- two session timezones and fails if any row differs.
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
FROM {{ source('core', 'ohlcv') }}
WHERE granularity_seconds = 3600
GROUP BY asset_id, (ts_utc AT TIME ZONE 'UTC')::date
