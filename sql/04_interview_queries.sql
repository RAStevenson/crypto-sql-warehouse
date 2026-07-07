-- =============================================================================
-- crypto-sql-warehouse: 20 analytical queries, escalating difficulty.
--
-- Each query is self-contained and runnable once the pipeline has loaded
-- data and marts are refreshed. The header of each query names the SQL
-- interview concept it exercises; docs/STUDY_GUIDE.md maps every query to
-- what I should be able to explain without notes.
-- =============================================================================


-- Q01 --------------------------------------------------------------------
-- Concept: DISTINCT ON (Postgres-specific top-1-per-group) and why ORDER BY
-- must lead with the DISTINCT ON columns.
-- Question shape: "latest record per entity."
SELECT DISTINCT ON (a.symbol)
    a.symbol,
    o.ts_utc,
    o.close
FROM core.ohlcv_hourly o
JOIN core.assets a USING (asset_id)
ORDER BY a.symbol, o.ts_utc DESC;


-- Q02 --------------------------------------------------------------------
-- Concept: GROUP BY with HAVING; the difference between WHERE (filters rows
-- before aggregation) and HAVING (filters groups after).
-- Months where an asset traded more than 100k base units.
SELECT
    a.symbol,
    date_trunc('month', o.ts_utc) AS month,
    sum(o.volume)                 AS monthly_volume
FROM core.ohlcv_hourly o
JOIN core.assets a USING (asset_id)
GROUP BY a.symbol, date_trunc('month', o.ts_utc)
HAVING sum(o.volume) > 100000
ORDER BY a.symbol, month;


-- Q03 --------------------------------------------------------------------
-- Concept: INNER vs LEFT JOIN semantics. LEFT JOIN keeps assets with no
-- candles yet (freshly added, is_active toggled) and count(o.ts_utc) -- not
-- count(*) -- makes them show 0 instead of 1.
SELECT
    a.symbol,
    a.is_active,
    count(o.ts_utc)  AS hourly_rows,
    min(o.ts_utc)    AS first_candle,
    max(o.ts_utc)    AS last_candle
FROM core.assets a
LEFT JOIN core.ohlcv_hourly o USING (asset_id)
GROUP BY a.symbol, a.is_active
ORDER BY hourly_rows DESC;


-- Q04 --------------------------------------------------------------------
-- Concept: aggregation across granularities with date_trunc. Rolling hourly
-- facts up to weekly bars; ordered array_agg is the idiom for "first/last
-- value within a group" in a plain aggregate query.
SELECT
    a.symbol,
    date_trunc('week', o.ts_utc)                    AS week,
    (array_agg(o.open  ORDER BY o.ts_utc ASC ))[1]  AS week_open,
    max(o.high)                                     AS week_high,
    min(o.low)                                      AS week_low,
    (array_agg(o.close ORDER BY o.ts_utc DESC))[1]  AS week_close,
    sum(o.volume)                                   AS week_volume
FROM core.ohlcv_hourly o
JOIN core.assets a USING (asset_id)
GROUP BY a.symbol, date_trunc('week', o.ts_utc)
ORDER BY a.symbol, week;


-- Q05 --------------------------------------------------------------------
-- Concept: conditional aggregation -- the SQL pivot. Both idioms shown:
-- portable CASE-inside-aggregate and Postgres's cleaner FILTER clause.
SELECT
    a.symbol,
    date_trunc('month', m.trade_date)                            AS month,
    count(*) FILTER (WHERE m.simple_return > 0)                  AS up_days,
    count(*) FILTER (WHERE m.simple_return < 0)                  AS down_days,
    sum(CASE WHEN m.simple_return > 0 THEN 1 ELSE 0 END)         AS up_days_portable,
    round(avg(m.simple_return) FILTER (WHERE m.simple_return > 0), 4) AS avg_up_move
FROM marts.daily_metrics m
JOIN core.assets a USING (asset_id)
WHERE m.simple_return IS NOT NULL
GROUP BY a.symbol, date_trunc('month', m.trade_date)
ORDER BY a.symbol, month;


-- Q06 --------------------------------------------------------------------
-- Concept: scalar subquery in HAVING; comparing group aggregates to a
-- global aggregate. Assets whose average daily dollar volume beats the
-- all-asset average.
SELECT
    a.symbol,
    round(avg(m.dollar_volume_approx)) AS avg_daily_dollar_volume
FROM marts.daily_metrics m
JOIN core.assets a USING (asset_id)
GROUP BY a.symbol
HAVING avg(m.dollar_volume_approx) >
       (SELECT avg(dollar_volume_approx) FROM marts.daily_metrics)
ORDER BY avg_daily_dollar_volume DESC;


-- Q07 --------------------------------------------------------------------
-- Concept: multi-step CTEs -- each CTE does one transformation, readable
-- top to bottom. Monthly return computed from first and last daily close,
-- then the best month per asset selected.
WITH monthly_bounds AS (
    SELECT
        asset_id,
        date_trunc('month', trade_date)                       AS month,
        (array_agg(close ORDER BY trade_date ASC ))[1]        AS first_close,
        (array_agg(close ORDER BY trade_date DESC))[1]        AS last_close
    FROM marts.daily_ohlcv
    GROUP BY asset_id, date_trunc('month', trade_date)
),
monthly_returns AS (
    SELECT
        asset_id,
        month,
        last_close / first_close - 1 AS month_return
    FROM monthly_bounds
)
SELECT
    a.symbol,
    mr.month,
    round(mr.month_return, 4) AS month_return
FROM monthly_returns mr
JOIN core.assets a USING (asset_id)
WHERE mr.month_return = (
    SELECT max(month_return)
    FROM monthly_returns mr2
    WHERE mr2.asset_id = mr.asset_id
)
ORDER BY a.symbol;


-- Q08 --------------------------------------------------------------------
-- Concept: LAG. The canonical period-over-period question. NULLIF guards
-- division by zero; the first row per partition is NULL by construction and
-- that is correct, not a bug to paper over.
SELECT
    a.symbol,
    d.trade_date,
    d.close,
    lag(d.close) OVER (PARTITION BY d.asset_id ORDER BY d.trade_date) AS prev_close,
    round(d.close / NULLIF(lag(d.close) OVER (PARTITION BY d.asset_id
                                              ORDER BY d.trade_date), 0) - 1, 4)
        AS daily_return
FROM marts.daily_ohlcv d
JOIN core.assets a USING (asset_id)
ORDER BY a.symbol, d.trade_date;


-- Q09 --------------------------------------------------------------------
-- Concept: ROW_NUMBER for top-N-per-group. Window functions cannot appear
-- in WHERE (evaluated after), hence the CTE-then-filter shape.
WITH ranked AS (
    SELECT
        asset_id,
        trade_date,
        dollar_volume_approx,
        row_number() OVER (PARTITION BY asset_id
                           ORDER BY dollar_volume_approx DESC) AS rn
    FROM marts.daily_metrics
)
SELECT
    a.symbol,
    r.trade_date,
    round(r.dollar_volume_approx) AS dollar_volume
FROM ranked r
JOIN core.assets a USING (asset_id)
WHERE r.rn <= 3
ORDER BY a.symbol, r.rn;


-- Q10 --------------------------------------------------------------------
-- Concept: ROW_NUMBER vs RANK vs DENSE_RANK side by side. Returns are
-- bucketed to force ties so the three functions visibly diverge:
-- ROW_NUMBER breaks ties arbitrarily, RANK leaves gaps, DENSE_RANK doesn't.
SELECT
    a.symbol,
    m.trade_date,
    round(m.simple_return, 2) AS return_bucket,
    row_number() OVER (PARTITION BY m.asset_id ORDER BY round(m.simple_return, 2) DESC) AS row_num,
    rank()       OVER (PARTITION BY m.asset_id ORDER BY round(m.simple_return, 2) DESC) AS rnk,
    dense_rank() OVER (PARTITION BY m.asset_id ORDER BY round(m.simple_return, 2) DESC) AS dense_rnk
FROM marts.daily_metrics m
JOIN core.assets a USING (asset_id)
WHERE m.simple_return IS NOT NULL
ORDER BY a.symbol, rnk
LIMIT 40;


-- Q11 --------------------------------------------------------------------
-- Concept: window frames. ROWS BETWEEN N PRECEDING AND CURRENT ROW for
-- moving averages and rolling volatility; the key talking point is the
-- default frame (RANGE UNBOUNDED PRECEDING TO CURRENT ROW) and why an
-- explicit ROWS frame is safer with duplicate ORDER BY values.
SELECT
    a.symbol,
    m.trade_date,
    m.close,
    round(avg(m.close) OVER w7, 2)   AS ma_7d,
    round(avg(m.close) OVER w30, 2)  AS ma_30d,
    -- sqrt() returns double precision; cast back because round(dp, int) does not exist
    round((stddev_samp(m.log_return) OVER w30 * sqrt(365))::numeric, 4) AS vol_30d_annualized
FROM marts.daily_metrics m
JOIN core.assets a USING (asset_id)
WINDOW
    w7  AS (PARTITION BY m.asset_id ORDER BY m.trade_date
            ROWS BETWEEN 6 PRECEDING AND CURRENT ROW),
    w30 AS (PARTITION BY m.asset_id ORDER BY m.trade_date
            ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
ORDER BY a.symbol, m.trade_date;


-- Q12 --------------------------------------------------------------------
-- Concept: cumulative windows (UNBOUNDED PRECEDING). Running peak and
-- maximum drawdown -- a finance-flavored version of "running total."
WITH dd AS (
    SELECT
        asset_id,
        trade_date,
        close,
        max(close) OVER (PARTITION BY asset_id ORDER BY trade_date)      AS running_peak,
        close / max(close) OVER (PARTITION BY asset_id ORDER BY trade_date) - 1
                                                                          AS drawdown
    FROM marts.daily_ohlcv
)
SELECT
    a.symbol,
    round(min(dd.drawdown), 4)                            AS max_drawdown,
    (array_agg(dd.trade_date ORDER BY dd.drawdown ASC))[1] AS worst_day
FROM dd
JOIN core.assets a USING (asset_id)
GROUP BY a.symbol
ORDER BY max_drawdown;


-- Q13 --------------------------------------------------------------------
-- Concept: NTILE for bucketing, plus CASE for labeling. Quartiles of daily
-- dollar volume within each asset; NTILE(4) puts near-equal row counts in
-- each bucket, which is not the same as quartiles by value.
WITH tiled AS (
    SELECT
        asset_id,
        trade_date,
        dollar_volume_approx,
        ntile(4) OVER (PARTITION BY asset_id
                       ORDER BY dollar_volume_approx) AS volume_quartile
    FROM marts.daily_metrics
)
SELECT
    a.symbol,
    t.volume_quartile,
    CASE t.volume_quartile
        WHEN 1 THEN 'quiet'
        WHEN 2 THEN 'below average'
        WHEN 3 THEN 'above average'
        WHEN 4 THEN 'heavy'
    END                                   AS activity_label,
    count(*)                              AS days,
    round(avg(t.dollar_volume_approx))    AS avg_dollar_volume
FROM tiled t
JOIN core.assets a USING (asset_id)
GROUP BY a.symbol, t.volume_quartile
ORDER BY a.symbol, t.volume_quartile;


-- Q14 --------------------------------------------------------------------
-- Concept: gaps and islands. Longest streak of consecutive up days.
-- The trick: (date - row_number within the up-day partition) is constant
-- across a run of consecutive up days, so it works as a group key.
WITH flagged AS (
    SELECT
        asset_id,
        trade_date,
        CASE WHEN simple_return > 0 THEN 1 ELSE 0 END AS is_up
    FROM marts.daily_metrics
    WHERE simple_return IS NOT NULL
),
grouped AS (
    SELECT
        asset_id,
        trade_date,
        is_up,
        trade_date - (row_number() OVER (PARTITION BY asset_id, is_up
                                         ORDER BY trade_date))::int AS streak_key
    FROM flagged
)
SELECT
    a.symbol,
    min(g.trade_date) AS streak_start,
    max(g.trade_date) AS streak_end,
    count(*)          AS streak_days
FROM grouped g
JOIN core.assets a USING (asset_id)
WHERE g.is_up = 1
GROUP BY a.symbol, g.streak_key
ORDER BY streak_days DESC
LIMIT 10;


-- Q15 --------------------------------------------------------------------
-- Concept: anti-join against generate_series for gap detection -- data
-- quality auditing. Every hour that SHOULD exist per asset but has no
-- candle. LEFT JOIN ... WHERE right-side IS NULL is the anti-join idiom;
-- NOT EXISTS is the equivalent worth mentioning.
WITH bounds AS (
    SELECT asset_id, min(ts_utc) AS first_ts, max(ts_utc) AS last_ts
    FROM core.ohlcv_hourly
    GROUP BY asset_id
),
expected AS (
    SELECT b.asset_id, gs.ts
    FROM bounds b
    CROSS JOIN LATERAL generate_series(b.first_ts, b.last_ts, interval '1 hour') AS gs(ts)
)
SELECT
    a.symbol,
    e.ts AS missing_hour
FROM expected e
JOIN core.assets a USING (asset_id)
LEFT JOIN core.ohlcv_hourly o
       ON o.asset_id = e.asset_id AND o.ts_utc = e.ts
WHERE o.asset_id IS NULL
ORDER BY a.symbol, e.ts;


-- Q16 --------------------------------------------------------------------
-- Concept: self-join for pairwise comparison plus a statistical aggregate.
-- Cross-asset return correlation over the trailing 90 days; the a < b
-- predicate emits each unordered pair exactly once.
WITH r AS (
    SELECT asset_id, trade_date, log_return
    FROM marts.daily_metrics
    WHERE log_return IS NOT NULL
      AND trade_date >= (SELECT max(trade_date) FROM marts.daily_metrics) - 90
)
SELECT
    aa.symbol                             AS asset_a,
    ab.symbol                             AS asset_b,
    round(corr(ra.log_return, rb.log_return)::numeric, 3) AS correlation,
    count(*)                              AS shared_days
FROM r ra
JOIN r rb ON rb.trade_date = ra.trade_date AND rb.asset_id > ra.asset_id
JOIN core.assets aa ON aa.asset_id = ra.asset_id
JOIN core.assets ab ON ab.asset_id = rb.asset_id
GROUP BY aa.symbol, ab.symbol
ORDER BY correlation DESC;


-- Q17 --------------------------------------------------------------------
-- Concept: frames that exclude the current row. Volume anomaly detection:
-- days trading at 3x their own trailing 30-day average. The frame ends at
-- 1 PRECEDING so today's spike does not inflate its own baseline -- a
-- window-function analogue of avoiding data leakage.
WITH baseline AS (
    SELECT
        asset_id,
        trade_date,
        dollar_volume_approx,
        avg(dollar_volume_approx) OVER (PARTITION BY asset_id ORDER BY trade_date
                                        ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING)
            AS trailing_avg_volume
    FROM marts.daily_metrics
)
SELECT
    a.symbol,
    b.trade_date,
    round(b.dollar_volume_approx)                            AS dollar_volume,
    round(b.trailing_avg_volume)                             AS trailing_avg,
    round(b.dollar_volume_approx / b.trailing_avg_volume, 1) AS volume_multiple
FROM baseline b
JOIN core.assets a USING (asset_id)
WHERE b.trailing_avg_volume IS NOT NULL
  AND b.dollar_volume_approx > 3 * b.trailing_avg_volume
ORDER BY volume_multiple DESC;


-- Q18 --------------------------------------------------------------------
-- Concept: LATERAL join -- a per-row subquery that can use the outer row,
-- the clean way to fetch "N most recent records per entity." Compare with
-- Q09's ROW_NUMBER approach; LATERAL can use the (asset_id, ts_utc) index
-- and stop after 3 rows per asset instead of ranking everything.
SELECT
    a.symbol,
    recent.ts_utc,
    recent.close,
    recent.volume
FROM core.assets a
CROSS JOIN LATERAL (
    SELECT o.ts_utc, o.close, o.volume
    FROM core.ohlcv_hourly o
    WHERE o.asset_id = a.asset_id
    ORDER BY o.ts_utc DESC
    LIMIT 3
) AS recent
ORDER BY a.symbol, recent.ts_utc DESC;


-- Q19 --------------------------------------------------------------------
-- Concept: ordered-set aggregates. Median vs mean daily return per asset;
-- crypto returns are heavy-tailed, so the two visibly disagree, which is
-- the whole point of knowing percentile_cont exists.
SELECT
    a.symbol,
    round(avg(m.simple_return), 5)                                          AS mean_return,
    round((percentile_cont(0.5)  WITHIN GROUP (ORDER BY m.simple_return))::numeric, 5) AS median_return,
    round((percentile_cont(0.05) WITHIN GROUP (ORDER BY m.simple_return))::numeric, 5) AS p05_return,
    round((percentile_cont(0.95) WITHIN GROUP (ORDER BY m.simple_return))::numeric, 5) AS p95_return
FROM marts.daily_metrics m
JOIN core.assets a USING (asset_id)
WHERE m.simple_return IS NOT NULL
GROUP BY a.symbol
ORDER BY a.symbol;


-- Q20 --------------------------------------------------------------------
-- Concept: idempotent incremental loading with INSERT ... ON CONFLICT.
-- This is the pipeline's core statement, isolated so I can walk through it.
-- Talking points: the conflict target must match a unique constraint;
-- EXCLUDED refers to the incoming row; the DO UPDATE ... WHERE clause skips
-- no-op updates, which avoids dead tuples and keeps updated_at honest.
-- DISTINCT ON guards against the same bucket appearing twice in one run,
-- because ON CONFLICT cannot update the same row twice in one statement.
--
-- (Illustrative: run against a real run_id, e.g. the latest succeeded run.)
INSERT INTO core.ohlcv_hourly
    (asset_id, ts_utc, open, high, low, close, volume, source_run_id)
SELECT DISTINCT ON (a.asset_id, r.bucket_start)
    a.asset_id,
    r.bucket_start,
    r.open, r.high, r.low, r.close, r.volume,
    r.run_id
FROM raw.candles r
JOIN core.assets a ON a.product_code = r.product_code
WHERE r.run_id = (SELECT max(run_id) FROM raw.ingest_runs WHERE status = 'succeeded')
ORDER BY a.asset_id, r.bucket_start, r.raw_id DESC
ON CONFLICT (asset_id, ts_utc) DO UPDATE
SET open = EXCLUDED.open,
    high = EXCLUDED.high,
    low  = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    source_run_id = EXCLUDED.source_run_id,
    updated_at = now()
WHERE (core.ohlcv_hourly.open, core.ohlcv_hourly.high, core.ohlcv_hourly.low,
       core.ohlcv_hourly.close, core.ohlcv_hourly.volume)
      IS DISTINCT FROM
      (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume);
