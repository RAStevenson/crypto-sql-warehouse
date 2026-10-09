-- Every candle missing from core was asked for and left out of the
-- exchange's answer.
--
-- Coinbase leaves a bucket out of its answer when nothing traded in it and
-- when the exchange itself was down, so core has holes the pipeline did not
-- make. The question that separates those from holes the pipeline did make
-- is whether the bucket was ever requested. raw.fetch_windows records every
-- window the loader asked for. A bucket missing from core that sits inside
-- a window of a succeeded run was asked for and left out of the answer, so
-- it is the exchange's gap and passes. A bucket inside no such window was
-- never asked for. Once every run has its windows on record, only a
-- pipeline bug leaves a bucket unasked, and this test returns it.
--
-- An earlier version guessed instead, by comparing assets. It called an
-- hour missing for every asset an outage and an hour missing for only some
-- a pipeline bug. At 17:00 and 21:00 UTC on 2023-03-04, Coinbase has hourly
-- candles for BTC and ETH but none for the four other assets, so that guess
-- failed on hours the exchange itself has no candles for, as it would again
-- for any asset added later with gaps of its own. It also could not judge
-- the finer timeframes at all, where a quiet coin legitimately misses many
-- 5-minute buckets. This version needs no list of known outages and no
-- comparison between assets, and it covers every asset at every timeframe.
--
-- What the test cannot see, stated so nobody assumes otherwise. A bucket
-- inside a requested window that the exchange did send but the pipeline
-- then dropped would pass here. core_matches_raw.sql catches the case where
-- it reached raw, and the window arithmetic that decides which buckets a
-- request covers is tested at every start offset and timeframe in the
-- repo's tests/test_fetch_windows.py. Each series' expected buckets run from
-- its own first candle to its own last, so a series that stops loading
-- produces no gap; active_series_stopped_loading.sql covers that case.
--
-- A warehouse loaded before raw.fetch_windows existed has no windows on
-- record for its older runs and fails here until
-- scripts/run_ingest.py --recheck-gaps has run.
WITH series AS (
    SELECT o.asset_id, a.product_code, o.granularity_seconds,
           min(o.ts_utc) AS first_ts, max(o.ts_utc) AS last_ts
    FROM {{ source('core', 'ohlcv') }} o
    JOIN {{ source('core', 'assets') }} a USING (asset_id)
    GROUP BY o.asset_id, a.product_code, o.granularity_seconds
),
missing AS (
    SELECT s.asset_id, s.product_code, s.granularity_seconds, expected.ts
    FROM series s
    CROSS JOIN LATERAL generate_series(
        s.first_ts, s.last_ts, make_interval(secs => s.granularity_seconds)
    ) AS expected(ts)
    WHERE NOT EXISTS (
        SELECT 1 FROM {{ source('core', 'ohlcv') }} o
        WHERE o.asset_id = s.asset_id
          AND o.granularity_seconds = s.granularity_seconds
          AND o.ts_utc = expected.ts)
)
SELECT
    m.product_code,
    m.granularity_seconds,
    m.ts AS bucket_start,
    'no succeeded run requested this bucket' AS reason
FROM missing m
WHERE NOT EXISTS (
    SELECT 1
    FROM {{ source('raw', 'fetch_windows') }} w
    JOIN {{ source('raw', 'ingest_runs') }} r USING (run_id)
    WHERE r.product_code = m.product_code
      AND r.granularity_seconds = m.granularity_seconds
      AND r.status = 'succeeded'
      AND m.ts BETWEEN w.window_start AND w.window_end
)
