-- =============================================================================
-- crypto-sql-warehouse: 21 analytical queries.
--
-- Each query is headed by the question it answers, then by the SQL technique
-- it uses. They are ordered roughly from the simpler techniques to the more
-- involved ones. Every query is self-contained and runnable once the
-- pipeline has loaded data and marts are refreshed.
--
-- Timezone note that applies file-wide: every date_trunc() over a timestamptz
-- is written as date_trunc(unit, ts_utc AT TIME ZONE 'UTC'). That yields a
-- plain timestamp and produces identical numbers no matter what timezone the
-- connecting session is set to. See the comment block in 01_schema.sql for
-- the bug that made this necessary.
--
-- Timeframe note: core.ohlcv holds one series per asset per timeframe (1h
-- always, plus whichever of 1m, 5m and 15m core.timeframes has active).
-- Each series covers the same trades, so a query over core.ohlcv picks one
-- timeframe with granularity_seconds. Summing volume across timeframes
-- would count every trade once per series. The queries below use the 1-hour
-- series (3600) unless the question is about the timeframes themselves.
--
-- Completeness note: marts.daily_metrics reports OHLCV for every day it
-- observed but leaves derived columns NULL on days built from fewer than 24
-- hourly candles. Queries below that consume derived columns therefore
-- filter out NULLs explicitly rather than letting a partial day pass as a
-- quiet trading day. See the header of dbt/models/marts/daily_ohlcv.sql.
-- =============================================================================


-- Q01 --------------------------------------------------------------------
-- What is the latest price on record for each asset at each timeframe, and
-- how fresh is each series?
-- Technique: DISTINCT ON, Postgres's top-1-per-group. ORDER BY must lead
-- with the DISTINCT ON columns; the trailing sort key decides which row of
-- each group is kept, here the newest. ts_utc is unique within an (asset,
-- timeframe) series, so no tie is possible. The finer series run later than
-- the hourly one, because a load stops at the last closed bucket of each.
SELECT DISTINCT ON (a.symbol, t.granularity_seconds)
    a.symbol,
    t.timeframe_code,
    o.ts_utc,
    o.close
FROM core.ohlcv o
JOIN core.assets a USING (asset_id)
JOIN core.timeframes t USING (granularity_seconds)
ORDER BY a.symbol, t.granularity_seconds, o.ts_utc DESC;


-- Q02 --------------------------------------------------------------------
-- Which asset-months saw more than 100k base units traded?
-- Technique: GROUP BY with HAVING. WHERE filters rows before aggregation,
-- HAVING filters groups after, so a threshold on sum() has to live in
-- HAVING. AT TIME ZONE 'UTC' keeps the month boundary fixed: without it the
-- same query returns 199952.70606718 for BTC in April from a UTC session and
-- 201147.88052508 from a Chicago one.
SELECT
    a.symbol,
    date_trunc('month', o.ts_utc AT TIME ZONE 'UTC') AS month,
    sum(o.volume)                                    AS monthly_volume
FROM core.ohlcv o
JOIN core.assets a USING (asset_id)
WHERE o.granularity_seconds = 3600
GROUP BY a.symbol, date_trunc('month', o.ts_utc AT TIME ZONE 'UTC')
HAVING sum(o.volume) > 100000
ORDER BY a.symbol, month;


-- Q03 --------------------------------------------------------------------
-- How much history do we actually hold per asset and timeframe, including
-- pairs that have never loaded a single candle?
-- Technique: INNER vs LEFT JOIN semantics. The CROSS JOIN lists every pair
-- the loader is meant to fill, every asset at every active timeframe. The
-- LEFT JOIN keeps pairs with no candles yet (an asset freshly added, a
-- timeframe just activated), and count(o.ts_utc), not count(*), makes them
-- show 0 instead of 1.
SELECT
    a.symbol,
    t.timeframe_code,
    a.is_active,
    count(o.ts_utc)  AS candle_rows,
    min(o.ts_utc)    AS first_candle,
    max(o.ts_utc)    AS last_candle
FROM core.assets a
CROSS JOIN core.timeframes t
LEFT JOIN core.ohlcv o
       ON o.asset_id = a.asset_id AND o.granularity_seconds = t.granularity_seconds
WHERE t.is_active
GROUP BY a.symbol, t.timeframe_code, t.granularity_seconds, a.is_active
ORDER BY a.symbol, t.granularity_seconds;


-- Q04 --------------------------------------------------------------------
-- What do weekly OHLCV bars look like, built from the 1-hour series?
-- Technique: aggregation across granularities with date_trunc; ordered
-- array_agg is the idiom for "first/last value within a group" in a plain
-- aggregate query. AT TIME ZONE 'UTC' again pins the boundary: under
-- America/Chicago one BTC week_close moved from 73823.14 to 74590.39 purely
-- because the week started five hours earlier.
SELECT
    a.symbol,
    date_trunc('week', o.ts_utc AT TIME ZONE 'UTC')  AS week,
    (array_agg(o.open  ORDER BY o.ts_utc ASC ))[1]   AS week_open,
    max(o.high)                                      AS week_high,
    min(o.low)                                       AS week_low,
    (array_agg(o.close ORDER BY o.ts_utc DESC))[1]   AS week_close,
    sum(o.volume)                                    AS week_volume
FROM core.ohlcv o
JOIN core.assets a USING (asset_id)
WHERE o.granularity_seconds = 3600
GROUP BY a.symbol, date_trunc('week', o.ts_utc AT TIME ZONE 'UTC')
ORDER BY a.symbol, week;


-- Q05 --------------------------------------------------------------------
-- How many up days versus down days did each asset have per month, and how
-- big was the average up move?
-- Technique: conditional aggregation, the SQL pivot. Both idioms shown, the
-- portable CASE-inside-aggregate and Postgres's cleaner FILTER clause.
-- date_trunc on a date resolves to the timestamptz overload, which groups
-- correctly but emits a session-dependent instant (2026-04-01 00:00:00-05
-- from Chicago). The ::date cast makes the label a plain date again.
SELECT
    a.symbol,
    date_trunc('month', m.trade_date)::date                      AS month,
    count(*) FILTER (WHERE m.simple_return > 0)                  AS up_days,
    count(*) FILTER (WHERE m.simple_return < 0)                  AS down_days,
    sum(CASE WHEN m.simple_return > 0 THEN 1 ELSE 0 END)         AS up_days_portable,
    round(avg(m.simple_return) FILTER (WHERE m.simple_return > 0), 4) AS avg_up_move
FROM marts.daily_metrics m
JOIN core.assets a USING (asset_id)
WHERE m.simple_return IS NOT NULL
GROUP BY a.symbol, date_trunc('month', m.trade_date)::date
ORDER BY a.symbol, month;


-- Q06 --------------------------------------------------------------------
-- Which assets trade more dollar volume per day than the average across all
-- assets?
-- Technique: scalar subquery in HAVING, comparing a group aggregate to a
-- global one. Both sides ignore NULLs, so incomplete days drop out of the
-- comparison on both sides automatically.
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
-- Which was each asset's best full calendar month, and by how much?
-- Technique: multi-step CTEs, each doing one transformation, readable top to
-- bottom. Monthly return from first and last daily close, then the best
-- month per asset.
--
-- The "full calendar month" wording matters, because the load starts and
-- ends mid-month, so the first and last calendar months are always partial.
-- Ranked against whole months, a partial month can rank first: on this data
-- a 7-day fragment came out as four of the six "best months", because a
-- 7-day window has far less room to give back a gain than a 31-day one.
-- days_present is carried through the CTEs and compared
-- against the calendar length of the month, and only months with every day
-- present are ranked. It counts complete days, so the current month does
-- not qualify on its last day while that day is still loading. Both columns
-- stay in the output so the reader can check the claim instead of trusting
-- it.
WITH monthly_bounds AS (
    SELECT
        asset_id,
        date_trunc('month', trade_date)::date                  AS month,
        count(*) FILTER (WHERE is_complete)                   AS days_present,
        (array_agg(close ORDER BY trade_date ASC ))[1]        AS first_close,
        (array_agg(close ORDER BY trade_date DESC))[1]        AS last_close
    FROM marts.daily_ohlcv
    GROUP BY asset_id, date_trunc('month', trade_date)::date
),
monthly_returns AS (
    SELECT
        asset_id,
        month,
        days_present,
        -- Days in that calendar month: step forward one month, back one day.
        extract(day FROM (month + interval '1 month - 1 day'))::int AS days_in_month,
        last_close / first_close - 1                                AS month_return
    FROM monthly_bounds
),
full_months AS (
    SELECT * FROM monthly_returns WHERE days_present = days_in_month
)
SELECT
    a.symbol,
    fm.month,
    fm.days_present,
    fm.days_in_month,
    round(fm.month_return, 4) AS month_return
FROM full_months fm
JOIN core.assets a USING (asset_id)
WHERE fm.month_return = (
    SELECT max(month_return)
    FROM full_months fm2
    WHERE fm2.asset_id = fm.asset_id
)
ORDER BY a.symbol, fm.month;


-- Q08 --------------------------------------------------------------------
-- What was each asset's day-over-day return, with the previous close shown
-- alongside so the arithmetic is checkable by eye?
-- Technique: LAG, the canonical period-over-period question. NULLIF guards
-- division by zero; the first row per partition is NULL by construction and
-- that is correct, not a bug to hide. This query computes the return on
-- every row, so three columns are selected to show when it should not be
-- trusted: hours_present and prev_hours_present expose a partial bar on
-- either side, and prev_date exposes a return that spans a gap.
-- marts.daily_metrics is where those returns are set to NULL.
SELECT
    a.symbol,
    d.trade_date,
    d.close,
    d.hours_present,
    lag(d.trade_date)    OVER w AS prev_date,
    lag(d.hours_present) OVER w AS prev_hours_present,
    lag(d.close)         OVER w AS prev_close,
    round(d.close / NULLIF(lag(d.close) OVER w, 0) - 1, 4) AS daily_return
FROM marts.daily_ohlcv d
JOIN core.assets a USING (asset_id)
WINDOW w AS (PARTITION BY d.asset_id ORDER BY d.trade_date)
ORDER BY a.symbol, d.trade_date;


-- Q09 --------------------------------------------------------------------
-- What were the three heaviest dollar-volume days for each asset?
-- Technique: ROW_NUMBER for top-N-per-group. Window functions cannot appear
-- in WHERE (they are evaluated after it), hence the CTE-then-filter shape.
-- The trade_date tiebreak returns the same three days on every run if two
-- days ever tie on volume, and the IS NOT NULL filter keeps partial days out
-- of a ranking they would only distort.
WITH ranked AS (
    SELECT
        asset_id,
        trade_date,
        dollar_volume_approx,
        row_number() OVER (PARTITION BY asset_id
                           ORDER BY dollar_volume_approx DESC, trade_date) AS rn
    FROM marts.daily_metrics
    WHERE dollar_volume_approx IS NOT NULL
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
-- On each asset's best days, how do ROW_NUMBER, RANK and DENSE_RANK differ
-- when the sort key has ties?
-- Technique: the three ranking functions side by side. Returns are rounded
-- to two decimals to force ties, so the divergence is visible: ROW_NUMBER
-- assigns distinct numbers, RANK leaves gaps after a tie, DENSE_RANK does
-- not.
--
-- Two windows, deliberately. w_row adds trade_date as a tiebreak so
-- ROW_NUMBER is reproducible and the top-6-per-asset cut always returns the
-- same rows; w_tie leaves the tie intact so RANK and DENSE_RANK still have
-- something to demonstrate. Filtering on row_num rather than a global LIMIT
-- is what makes all six assets appear: on this data a global LIMIT 40 was
-- consumed entirely by ADA, and without the tiebreak the cut landed inside a
-- tie group, so the rows returned changed between runs.
WITH ranked AS (
    SELECT
        m.asset_id,
        m.trade_date,
        round(m.simple_return, 2) AS return_bucket,
        row_number() OVER w_row AS row_num,
        rank()       OVER w_tie AS rnk,
        dense_rank() OVER w_tie AS dense_rnk
    FROM marts.daily_metrics m
    WHERE m.simple_return IS NOT NULL
    WINDOW
        w_row AS (PARTITION BY m.asset_id
                  ORDER BY round(m.simple_return, 2) DESC, m.trade_date),
        w_tie AS (PARTITION BY m.asset_id
                  ORDER BY round(m.simple_return, 2) DESC)
)
SELECT
    a.symbol,
    r.trade_date,
    r.return_bucket,
    r.row_num,
    r.rnk,
    r.dense_rnk
FROM ranked r
JOIN core.assets a USING (asset_id)
WHERE r.row_num <= 6
ORDER BY a.symbol, r.row_num;


-- Q11 --------------------------------------------------------------------
-- What do the 7-day and 30-day moving averages and the 30-day annualized
-- volatility look like day by day?
-- Technique: window frames. ROWS BETWEEN N PRECEDING AND CURRENT ROW for
-- moving averages and rolling volatility. The default frame is RANGE
-- UNBOUNDED PRECEDING TO CURRENT ROW, and an explicit ROWS frame is needed
-- as soon as the ORDER BY key can repeat.
--
-- The count() guards are the same ones marts.daily_metrics applies. Without
-- them the first row of each partition publishes a one-observation mean
-- under a column named ma_30d, and BTC's first volatility print comes out
-- at 0.2076 against 0.4043 for its first genuine 30-observation window.
-- NULL is the accurate value during warm-up.
SELECT
    a.symbol,
    m.trade_date,
    m.close,
    CASE WHEN count(m.close) OVER w7  >= 7
         THEN round(avg(m.close) OVER w7, 2)  END  AS ma_7d,
    CASE WHEN count(m.close) OVER w30 >= 30
         THEN round(avg(m.close) OVER w30, 2) END  AS ma_30d,
    -- sqrt() returns double precision; cast back because round(dp, int) does not exist
    CASE WHEN count(m.log_return) OVER w30 >= 30
         THEN round((stddev_samp(m.log_return) OVER w30 * sqrt(365))::numeric, 4) END
        AS vol_30d_annualized
FROM marts.daily_metrics m
JOIN core.assets a USING (asset_id)
WHERE m.is_complete
WINDOW
    w7  AS (PARTITION BY m.asset_id ORDER BY m.trade_date
            ROWS BETWEEN 6 PRECEDING AND CURRENT ROW),
    w30 AS (PARTITION BY m.asset_id ORDER BY m.trade_date
            ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
ORDER BY a.symbol, m.trade_date;


-- Q12 --------------------------------------------------------------------
-- How far did each asset fall from its running peak, and on what day?
-- Technique: cumulative windows. Running peak with ROWS BETWEEN UNBOUNDED
-- PRECEDING AND CURRENT ROW, the same frame a running total uses.
--
-- Two points need stating. First, the frame is spelled out rather than left
-- to the RANGE default; with one row per (asset, day) the results agree, but
-- the marts header claims ROWS frames and the code should match the claim.
-- Second, the peak is the highest close in the LOADED WINDOW,
-- measured from the first day of loaded history. This is not an
-- all-time-high drawdown, and backfilling earlier data would change every
-- number below.
--
-- The trade_date tiebreak in array_agg is needed, because ADA has an exact
-- tie, 2026-06-25 and 2026-06-28 both at -0.49203539823008849558, and
-- without the tiebreak the reported worst_day changes between runs.
WITH dd AS (
    SELECT
        asset_id,
        trade_date,
        close,
        max(close) OVER w             AS running_peak,
        close / max(close) OVER w - 1 AS drawdown
    FROM marts.daily_ohlcv
    WHERE is_complete
    WINDOW w AS (PARTITION BY asset_id ORDER BY trade_date
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
)
SELECT
    a.symbol,
    round(min(dd.drawdown), 4)                                                AS max_drawdown,
    (array_agg(dd.trade_date ORDER BY dd.drawdown ASC, dd.trade_date ASC))[1] AS worst_day
FROM dd
JOIN core.assets a USING (asset_id)
GROUP BY a.symbol
ORDER BY max_drawdown, a.symbol;


-- Q13 --------------------------------------------------------------------
-- Split each asset's days into volume quartiles: how much more does a heavy
-- day trade than a quiet one?
-- Technique: NTILE for bucketing plus CASE for labeling. NTILE(4) puts
-- near-equal ROW COUNTS in each bucket, which is not the same as quartiles
-- by value, and that distinction is the point of the query.
WITH tiled AS (
    SELECT
        asset_id,
        trade_date,
        dollar_volume_approx,
        ntile(4) OVER (PARTITION BY asset_id
                       ORDER BY dollar_volume_approx, trade_date) AS volume_quartile
    FROM marts.daily_metrics
    WHERE dollar_volume_approx IS NOT NULL
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
-- What is the longest run of consecutive up days any asset has put together?
-- Technique: gaps and islands. (date - row_number within the up-day
-- partition) is constant across a run of consecutive up days, so it works as
-- a group key. Days with no return (the first day, and days following a data
-- gap) are absent from the input, which correctly breaks a streak rather
-- than bridging one.
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
-- The tiebreaks matter because LIMIT cuts inside a group of equal-length
-- streaks; without them the tenth row varies between runs.
ORDER BY streak_days DESC, a.symbol, streak_start
LIMIT 10;


-- Q15 --------------------------------------------------------------------
-- Which hours should exist in the series but have no candle?
-- Technique: anti-join against generate_series, the data quality audit.
-- LEFT JOIN plus WHERE right-side IS NULL is the anti-join idiom; NOT
-- EXISTS is the equivalent form. Bounds are per asset, so an asset that
-- started loading later is not reported as missing the hours before its
-- first candle. The audit runs on the 1-hour series. A finer series skips
-- every bucket with no trades, so an audit of it would mostly list those
-- quiet buckets. The dbt test dbt/tests/gaps_without_a_recorded_request.sql
-- judges every series, finer ones included, against raw.fetch_windows, the
-- record of what the loader asked for.
WITH bounds AS (
    SELECT asset_id, min(ts_utc) AS first_ts, max(ts_utc) AS last_ts
    FROM core.ohlcv
    WHERE granularity_seconds = 3600
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
LEFT JOIN core.ohlcv o
       ON o.asset_id = e.asset_id
      AND o.granularity_seconds = 3600
      AND o.ts_utc = e.ts
WHERE o.asset_id IS NULL
ORDER BY a.symbol, e.ts;


-- Q16 --------------------------------------------------------------------
-- Which assets move together, and how much should the number be trusted?
-- Technique: self-join for pairwise comparison plus corr(), Postgres's
-- built-in Pearson aggregate. The a < b predicate emits each unordered pair
-- exactly once. shared_days is returned alongside because a correlation over
-- 12 days deserves less trust than one over 90, and no aggregate tells you
-- that on its own.
--
-- The bound is > max - 90, not >= max - 90. The inclusive form spans 91
-- distinct dates. Measured 2026-08-04 it still reported 90, because the earliest
-- of them had a NULL log_return, which is luck rather than logic. The
-- window is anchored on the latest date that has a return, because the
-- newest row is normally the day still being loaded and has none.
WITH r AS (
    SELECT asset_id, trade_date, log_return
    FROM marts.daily_metrics
    WHERE log_return IS NOT NULL
      AND trade_date > (SELECT max(trade_date) FROM marts.daily_metrics
                        WHERE log_return IS NOT NULL) - 90
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
ORDER BY correlation DESC, asset_a, asset_b;


-- Q17 --------------------------------------------------------------------
-- Which days traded at more than three times their own recent normal?
-- Technique: a frame that excludes the current row. The baseline runs
-- ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING so today's spike does not
-- inflate the average it is being measured against, a window-function
-- analog of avoiding data leakage.
--
-- The CTE reads complete days only. A partial day's volume is whatever
-- fraction of the session happened to be loaded, so letting one into the
-- trailing window moves the baseline by an amount that depends on when the
-- loader ran. That is enough to manufacture or suppress an anomaly on the
-- days that follow it.
--
-- No day is tested until its baseline holds 30 observations, the same rule
-- the marts apply to ma_30d. Without the count, the second day of history
-- would be compared against an average of one.
WITH baseline AS (
    SELECT
        asset_id,
        trade_date,
        dollar_volume_approx,
        avg(dollar_volume_approx)   OVER w AS trailing_avg_volume,
        count(dollar_volume_approx) OVER w AS baseline_days
    FROM marts.daily_metrics
    WHERE dollar_volume_approx IS NOT NULL
    WINDOW w AS (PARTITION BY asset_id ORDER BY trade_date
                 ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING)
)
SELECT
    a.symbol,
    b.trade_date,
    round(b.dollar_volume_approx)                            AS dollar_volume,
    round(b.trailing_avg_volume)                             AS trailing_avg,
    round(b.dollar_volume_approx / b.trailing_avg_volume, 1) AS volume_multiple
FROM baseline b
JOIN core.assets a USING (asset_id)
WHERE b.baseline_days = 30
  AND b.dollar_volume_approx > 3 * b.trailing_avg_volume
ORDER BY volume_multiple DESC, a.symbol, b.trade_date;


-- Q18 --------------------------------------------------------------------
-- What are the three most recent hourly candles for every asset?
-- Technique: LATERAL join, a per-row subquery that can reference the outer
-- row. Compare with Q09's ROW_NUMBER approach: LATERAL can walk the
-- (asset_id, granularity_seconds, ts_utc) index backwards and stop after 3
-- rows per asset instead of ranking every row first.
SELECT
    a.symbol,
    recent.ts_utc,
    recent.close,
    recent.volume
FROM core.assets a
CROSS JOIN LATERAL (
    SELECT o.ts_utc, o.close, o.volume
    FROM core.ohlcv o
    WHERE o.asset_id = a.asset_id
      AND o.granularity_seconds = 3600
    ORDER BY o.ts_utc DESC
    LIMIT 3
) AS recent
ORDER BY a.symbol, recent.ts_utc DESC;


-- Q19 --------------------------------------------------------------------
-- Does the average daily return describe a typical day, or is it moved by
-- a handful of outliers?
-- Technique: ordered-set aggregates. percentile_cont gives the median and
-- the tails; crypto returns are heavy-tailed, so mean and median visibly
-- disagree.
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
-- How does a rerun of the loader avoid duplicating or corrupting rows that
-- are already in the fact table?
-- Technique: idempotent incremental loading with INSERT ... ON CONFLICT.
-- This is the pipeline's core statement, shown on its own. The conflict
-- target must match a unique constraint. EXCLUDED refers to the incoming
-- row. The DO UPDATE ... WHERE clause skips no-op updates, which avoids dead
-- tuples and moves updated_at only when a value changed.
-- The timeframe comes from the run row, since every candle a run fetched
-- shares its run's granularity.
-- DISTINCT ON guards against the same bucket appearing twice in one run,
-- because ON CONFLICT cannot update the same row twice in one statement.
-- The ORDER BY ends on raw_id DESC, a unique column, so the newest copy of
-- each bucket is the one kept and the statement is deterministic.
-- The WHERE conditions after the run_id filter repeat core's CHECK
-- constraints, so a raw row they would reject is left out. Without them one
-- bad candle aborts the whole statement, and the same candle aborts every
-- later run for that product. The verify script fails on a bucket left out
-- this way.
--
-- (Illustrative: run against a real run_id, e.g. the latest succeeded run.)
INSERT INTO core.ohlcv
    (asset_id, granularity_seconds, ts_utc, open, high, low, close, volume, source_run_id)
SELECT DISTINCT ON (a.asset_id, r.bucket_start)
    a.asset_id,
    ir.granularity_seconds,
    r.bucket_start,
    r.open, r.high, r.low, r.close, r.volume,
    r.run_id
FROM raw.candles r
JOIN raw.ingest_runs ir ON ir.run_id = r.run_id
JOIN core.assets a ON a.product_code = r.product_code
WHERE r.run_id = (SELECT max(run_id) FROM raw.ingest_runs WHERE status = 'succeeded')
  AND r.volume >= 0
  AND r.high >= r.low
  AND r.high >= r.open AND r.high >= r.close
  AND r.low <= r.open AND r.low <= r.close
  AND r.low > 0
ORDER BY a.asset_id, r.bucket_start, r.raw_id DESC
ON CONFLICT (asset_id, granularity_seconds, ts_utc) DO UPDATE
SET open = EXCLUDED.open,
    high = EXCLUDED.high,
    low  = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    source_run_id = EXCLUDED.source_run_id,
    updated_at = now()
WHERE (core.ohlcv.open, core.ohlcv.high, core.ohlcv.low,
       core.ohlcv.close, core.ohlcv.volume)
      IS DISTINCT FROM
      (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume);

-- Q21 --------------------------------------------------------------------
-- How often do Coinbase's own timeframes disagree with each other?
-- Technique: aggregate one series up to the grain of another and join the
-- two. Each finer series is combined into hours (first open, highest high,
-- lowest low, last close, summed volume) and set against the stored 1-hour
-- bar for the same asset and hour. Only hours present in both series are
-- compared, and the first and last hour of a finer series can be partial.
--
-- This started as a verify check that failed on any price difference. It
-- could not stay one, because the disagreement is in the source. Measured
-- 2026-10-09 over the load from 2020: prices agree in nearly every hour of
-- 2020 to 2022 and of 2026, but the 5-minute series disagrees with the
-- hourly bar in about one hour in five from April to December 2023, and both
-- the 5-minute and 15-minute series disagree in stretches of August and
-- September 2024 and of January to May 2025. The smaller coins carry most of
-- it (June 2023 at 5 minutes: LINK 425 of 720 hours, BTC 2). The differences
-- are small, a median of 0.015 percent on the open and at most 1.8 percent
-- on any price. Fetching sample hours again returned exactly the stored
-- values at every timeframe, so the stored data matches what Coinbase
-- serves and the disagreement is Coinbase's. A check that failed on it would
-- fail on every warehouse and could never be fixed by the operator.
--
-- Volume differs in the same stretches, in both directions, and elsewhere
-- in a few dozen hours a year, except in 2026, where the 5-minute bars carry
-- more volume than the hourly bar in about one hour in fifty. One hour also
-- exists in only one series: on 2023-03-04 at 17:00 Coinbase has an hourly
-- BTC bar but no 5-minute or 15-minute bars.
WITH fine AS (
    SELECT
        asset_id,
        granularity_seconds,
        date_trunc('hour', ts_utc AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS hour_utc,
        (array_agg(open  ORDER BY ts_utc ASC ))[1]  AS open,
        max(high)                                   AS high,
        min(low)                                    AS low,
        (array_agg(close ORDER BY ts_utc DESC))[1]  AS close,
        sum(volume)                                 AS volume
    FROM core.ohlcv
    WHERE granularity_seconds < 3600
    GROUP BY asset_id, granularity_seconds, date_trunc('hour', ts_utc AT TIME ZONE 'UTC')
)
SELECT
    t.timeframe_code,
    extract(year FROM f.hour_utc AT TIME ZONE 'UTC')::int              AS year,
    count(*)                                                           AS hours_compared,
    count(*) FILTER (WHERE (f.open, f.high, f.low, f.close)
                           IS DISTINCT FROM (h.open, h.high, h.low, h.close))
                                                                       AS hours_prices_differ,
    count(*) FILTER (WHERE f.volume < h.volume)                        AS hours_finer_volume_less,
    count(*) FILTER (WHERE f.volume > h.volume)                        AS hours_finer_volume_more
FROM fine f
JOIN core.ohlcv h
  ON h.asset_id = f.asset_id
 AND h.granularity_seconds = 3600
 AND h.ts_utc = f.hour_utc
JOIN core.timeframes t ON t.granularity_seconds = f.granularity_seconds
GROUP BY t.timeframe_code, t.granularity_seconds, year
ORDER BY t.granularity_seconds, year;
