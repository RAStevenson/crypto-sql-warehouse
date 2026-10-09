-- =============================================================================
-- Migration 001: add timeframes to a warehouse built before 2026-10-09.
--
-- Before this migration the fact table was core.ohlcv_hourly, keyed
-- (asset_id, ts_utc), and held only 1-hour candles. It becomes core.ohlcv,
-- keyed (asset_id, granularity_seconds, ts_utc), beside a new
-- core.timeframes dimension. Every stored candle is kept, labeled 1h.
--
-- Apply it with:
--   python scripts/run_setup.py --migrate
-- which runs this file in one transaction, so a failure in it leaves the old
-- warehouse exactly as it was. Once it has committed, dbt builds the marts
-- again, and if that step fails, scripts/run_setup.py --rebuild-marts
-- finishes the job.
--
-- The new table is created and filled rather than altered in place, so its
-- column order and constraint names come out identical to a fresh
-- sql/01_schema.sql. tests/test_database_setup.py holds the two CREATE
-- statements below against 01_schema.sql so they cannot drift.
--
-- raw is untouched. raw.ingest_runs already records granularity_seconds,
-- 3600 for every run so far, and raw.candles reaches it through run_id, so
-- the load history and every raw copy survive as they are.
-- =============================================================================

-- The marts read the old table. dbt rebuilds them from dbt/models/marts/
-- after this file runs.
DROP SCHEMA marts CASCADE;
CREATE SCHEMA marts;

CREATE TABLE core.timeframes (
    timeframe_code      text PRIMARY KEY,              -- '15m'
    granularity_seconds integer NOT NULL UNIQUE
                        CHECK (granularity_seconds IN (60, 300, 900, 3600)),
    is_active           boolean NOT NULL DEFAULT false
);

INSERT INTO core.timeframes (timeframe_code, granularity_seconds, is_active)
VALUES
    ('1m',    60, false),
    ('5m',   300, false),
    ('15m',  900, false),
    ('1h',  3600, true);

CREATE TABLE core.ohlcv (
    asset_id            smallint NOT NULL REFERENCES core.assets (asset_id),
    granularity_seconds integer NOT NULL
                        REFERENCES core.timeframes (granularity_seconds),
    ts_utc              timestamptz NOT NULL,
    open                numeric(20,8) NOT NULL,
    high                numeric(20,8) NOT NULL,
    low                 numeric(20,8) NOT NULL,
    close               numeric(20,8) NOT NULL,
    volume              numeric(30,8) NOT NULL CHECK (volume >= 0),
    source_run_id       bigint NOT NULL REFERENCES raw.ingest_runs (run_id),
    inserted_at         timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, granularity_seconds, ts_utc),
    CHECK (high >= low),
    CHECK (high >= open AND high >= close),
    CHECK (low  <= open AND low  <= close),
    CHECK (low  > 0)
);

-- Cross-asset queries filter on timeframe and time first ("all assets at
-- 1h on 2026-05-01"); the PK index leads with asset_id and cannot serve
-- that efficiently.
CREATE INDEX ohlcv_granularity_ts_idx ON core.ohlcv (granularity_seconds, ts_utc);

INSERT INTO core.ohlcv
    (asset_id, granularity_seconds, ts_utc, open, high, low, close, volume,
     source_run_id, inserted_at, updated_at)
SELECT
    asset_id, 3600, ts_utc, open, high, low, close, volume,
    source_run_id, inserted_at, updated_at
FROM core.ohlcv_hourly;

DROP TABLE core.ohlcv_hourly;
