-- =============================================================================
-- Migration 003: add the 1-day timeframe to a warehouse built before it existed.
--
-- The marts keep building daily bars from the 1-hour series. The 1-day candles
-- are stored so that Q21 in sql/04_queries.sql can compare the two. The new
-- timeframe starts out active, as it does in sql/01_schema.sql, so the next
-- load fetches it.
--
-- Apply it with:
--   python scripts/run_setup.py --migrate
--
-- The first load after this migration reaches back the default 365 days. To
-- match the rest of the history, name the timeframe and the depth:
--   python scripts/run_ingest.py --timeframes 1d --backfill-days 2500
-- Until a load has run, the dbt test active_series_stopped_loading.sql reports
-- the empty series.
-- =============================================================================

ALTER TABLE core.timeframes
    DROP CONSTRAINT timeframes_granularity_seconds_check;
ALTER TABLE core.timeframes
    ADD CONSTRAINT timeframes_granularity_seconds_check
    CHECK (granularity_seconds IN (60, 300, 900, 3600, 86400));

INSERT INTO core.timeframes (timeframe_code, granularity_seconds, is_active)
VALUES ('1d', 86400, true);
