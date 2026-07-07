-- =============================================================================
-- crypto-sql-warehouse: core schema DDL
-- Target: PostgreSQL 15+
--
-- Layering rationale
-- ------------------
-- Three schemas model the standard ELT flow:
--   raw   : data exactly as the API returned it, append-only, plus load
--           bookkeeping. Never updated, never deduplicated. If a transform
--           bug is found downstream, core can be rebuilt from raw without
--           re-calling the API.
--   core  : cleaned, typed, deduplicated facts and dimensions. One row per
--           (asset, hour). This is the layer analytical queries trust.
--   marts : derived, presentation-shaped objects (daily rollups, rolling
--           metrics, correlations). Defined in 02_marts.sql.
--
-- This mirrors dbt's staging/intermediate/marts convention closely enough
-- that the project translates directly if I later port it to dbt.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS marts;

-- -----------------------------------------------------------------------------
-- core.assets: the asset dimension.
--
-- Surrogate smallint key rather than using the product code as the PK:
-- the fact table stores millions of rows and a 2-byte key beats a text key
-- for index size and join speed. product_code stays UNIQUE so the natural
-- key is still enforced.
-- -----------------------------------------------------------------------------
CREATE TABLE core.assets (
    asset_id      smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol        text NOT NULL UNIQUE,          -- 'BTC'
    product_code  text NOT NULL UNIQUE,          -- Coinbase product id, 'BTC-USD'
    asset_name    text NOT NULL,
    quote_currency text NOT NULL DEFAULT 'USD',
    is_active     boolean NOT NULL DEFAULT true, -- soft-disable ingestion without deleting history
    added_at      timestamptz NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- raw.ingest_runs: one row per (product, window) fetch attempt.
--
-- This table is the incremental-loading watermark and the audit trail in one.
-- The pipeline reads MAX(ts_utc) from core to decide where to resume, but
-- ingest_runs records what was attempted, what succeeded, and how many rows
-- moved -- the first thing I would query when a load looks wrong.
-- -----------------------------------------------------------------------------
CREATE TABLE raw.ingest_runs (
    run_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_code        text NOT NULL,
    granularity_seconds integer NOT NULL,
    window_start        timestamptz NOT NULL,
    window_end          timestamptz NOT NULL,
    rows_fetched        integer,
    rows_upserted       integer,
    status              text NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz
);

CREATE INDEX ingest_runs_product_started_idx
    ON raw.ingest_runs (product_code, started_at DESC);

-- -----------------------------------------------------------------------------
-- raw.candles: API responses as landed. Append-only by design.
--
-- No uniqueness constraint here on purpose: if the same bucket is fetched
-- twice (overlap re-fetch, retried run), both copies land. Deduplication is
-- the core layer's job, and keeping raw dumb makes idempotency reasoning
-- simple -- replaying any run can only ever add rows here, never corrupt.
--
-- numeric, not double precision: prices are exact decimal quantities and
-- float rounding artifacts in a warehouse invite "why doesn't this sum
-- match" questions. numeric(20,8) covers BTC at six figures down to satoshi
-- precision.
-- -----------------------------------------------------------------------------
CREATE TABLE raw.candles (
    raw_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id        bigint NOT NULL REFERENCES raw.ingest_runs (run_id),
    product_code  text NOT NULL,
    bucket_start  timestamptz NOT NULL,
    open          numeric(20,8) NOT NULL,
    high          numeric(20,8) NOT NULL,
    low           numeric(20,8) NOT NULL,
    close         numeric(20,8) NOT NULL,
    volume        numeric(30,8) NOT NULL,         -- base-currency units (BTC, not USD)
    fetched_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX candles_run_idx ON raw.candles (run_id);

-- -----------------------------------------------------------------------------
-- core.ohlcv_hourly: the fact table. One row per asset per hour, UTC.
--
-- Composite natural PK (asset_id, ts_utc) instead of a surrogate id:
-- the grain IS the identity, the PK doubles as the upsert conflict target,
-- and the implicit index serves the dominant access pattern
-- (per-asset time-range scans). A surrogate key would add a column and an
-- index while enforcing nothing.
--
-- CHECK constraints encode OHLC invariants. They have caught real API
-- glitches in similar pipelines; cheap insurance at insert time.
--
-- Not partitioned: ~6 assets x 24 x 365 is about 53k rows/year. Declarative
-- range partitioning on ts_utc becomes worth its operational cost somewhere
-- past tens of millions of rows; below that it only complicates the upsert
-- and vacuum story. The PK already leads with asset_id, so pruning would
-- buy little here. Documented so I can defend the decision, and reverse it,
-- in an interview.
-- -----------------------------------------------------------------------------
CREATE TABLE core.ohlcv_hourly (
    asset_id      smallint NOT NULL REFERENCES core.assets (asset_id),
    ts_utc        timestamptz NOT NULL,
    open          numeric(20,8) NOT NULL,
    high          numeric(20,8) NOT NULL,
    low           numeric(20,8) NOT NULL,
    close         numeric(20,8) NOT NULL,
    volume        numeric(30,8) NOT NULL CHECK (volume >= 0),
    source_run_id bigint NOT NULL REFERENCES raw.ingest_runs (run_id),
    inserted_at   timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, ts_utc),
    CHECK (high >= low),
    CHECK (high >= open AND high >= close),
    CHECK (low  <= open AND low  <= close)
);

-- Cross-asset queries filter on time first ("all assets on 2026-05-01");
-- the PK index leads with asset_id and cannot serve that efficiently.
CREATE INDEX ohlcv_hourly_ts_idx ON core.ohlcv_hourly (ts_utc);

-- -----------------------------------------------------------------------------
-- Seed the asset dimension. Product codes are Coinbase Exchange product ids;
-- the pipeline treats this table as its work list (is_active = true).
-- -----------------------------------------------------------------------------
INSERT INTO core.assets (symbol, product_code, asset_name)
VALUES
    ('BTC',  'BTC-USD',  'Bitcoin'),
    ('ETH',  'ETH-USD',  'Ethereum'),
    ('SOL',  'SOL-USD',  'Solana'),
    ('ADA',  'ADA-USD',  'Cardano'),
    ('LINK', 'LINK-USD', 'Chainlink'),
    ('DOGE', 'DOGE-USD', 'Dogecoin');
