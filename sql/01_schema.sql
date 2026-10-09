-- =============================================================================
-- crypto-sql-warehouse: core schema DDL
-- Verified on PostgreSQL 17. The newest feature used is identity columns,
-- so nothing here needs a version newer than PostgreSQL 10.
--
-- Layering rationale
-- ------------------
-- Three schemas model the standard ELT flow:
--   raw   : data exactly as the API returned it, append-only, plus load
--           bookkeeping. Never updated, never deduplicated. If a transform
--           bug is found downstream, core can be rebuilt from raw without
--           re-calling the API.
--   core  : cleaned, typed, deduplicated facts and dimensions. One row per
--           (asset, timeframe, bucket). This is the layer analytical queries
--           trust.
--   marts : derived, presentation-shaped objects (daily rollups, rolling
--           metrics, correlations). Defined in 02_marts.sql.
--
-- This mirrors dbt's staging/intermediate/marts convention closely enough
-- that the project translates directly if I later port it to dbt.
-- =============================================================================

-- Timezone. date_trunc() on a timestamptz buckets by the SESSION timezone,
-- so the same "monthly volume" query returns different numbers depending on
-- who is connected. Found by running the query set against a live instance
-- under America/Chicago: BTC's April volume moved from 199952.70606718 to
-- 201147.88052508 because the month boundary slid to 05:00 UTC. The -05
-- display offsets were the visible symptom.
--
-- The ALTER DATABASE below is defense in depth, not a guarantee. It only
-- sets the default for NEW sessions, and any client can override it with
-- SET TIME ZONE or PGOPTIONS='-c timezone=...', which is exactly what a
-- JDBC or Tableau driver does. The real fix lives in the queries: every
-- date_trunc() over ts_utc is written as
-- date_trunc('month', ts_utc AT TIME ZONE 'UTC'), which yields a plain
-- timestamp and is timezone-invariant by construction.
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET timezone = ''UTC''', current_database());
END $$;

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS marts;

-- -----------------------------------------------------------------------------
-- core.assets: the asset dimension.
--
-- Surrogate smallint key rather than using the product code as the PK: as
-- the fact table grows, a 2-byte key beats a text key for index size and
-- join speed. At a few million rows (see core.ohlcv below) the saving is
-- real but modest; the point is that it costs nothing now and the
-- choice does not have to be revisited later. product_code stays UNIQUE so
-- the natural key is still enforced.
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
-- core.timeframes: the timeframe dimension, and the second axis of the
-- ingest work list. A load covers every active asset at every active
-- timeframe.
--
-- granularity_seconds is the value the Coinbase candles endpoint takes and
-- the key the fact table carries. The CHECK lists the four the warehouse
-- supports. Coinbase also serves 6-hour and 1-day candles; daily bars are
-- built from hourly ones in the marts instead (see 02_marts.sql), and
-- nothing here uses 6 hours.
--
-- Only 1h is active in the seed, so a fresh clone's first load stays
-- small. Activating another timeframe is one statement:
--   UPDATE core.timeframes SET is_active = true WHERE timeframe_code = '15m';
-- -----------------------------------------------------------------------------
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

-- -----------------------------------------------------------------------------
-- raw.ingest_runs: one row per product and timeframe per run, covering every
-- window the run fetched.
--
-- This table is the audit trail of the incremental load.
-- The pipeline reads MAX(ts_utc) from core to decide where to resume, but
-- ingest_runs records what was attempted, what succeeded, and how many rows
-- moved. It is the first thing I would query when a load looks wrong.
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
-- raw.fetch_windows: one row per window the loader asked Coinbase for, with
-- how many candles came back inside it.
--
-- This is the record that tells a candle the loader never asked for apart
-- from one the exchange did not return. A bucket missing from core that
-- sits inside a window a succeeded run requested was asked for, and the
-- exchange left it out of its answer, because nothing traded or because
-- Coinbase itself had an outage. A bucket missing from core that sits
-- inside no such window was never asked for, which on a warehouse loaded
-- since this table existed means a pipeline bug. Check 1 in 05_verify.sql
-- draws exactly that line, so it needs no list of known outages and no
-- comparison between assets, and it holds for any asset at any timeframe.
--
-- Append-only like the rest of raw. A window that comes back empty still
-- gets its row, because that row is what lets check 1 pass every bucket
-- inside it.
-- -----------------------------------------------------------------------------
CREATE TABLE raw.fetch_windows (
    fetch_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id            bigint NOT NULL REFERENCES raw.ingest_runs (run_id),
    window_start      timestamptz NOT NULL,  -- first bucket requested
    window_end        timestamptz NOT NULL,  -- last bucket requested, inclusive
    candles_received  integer NOT NULL CHECK (candles_received >= 0),
    requested_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (window_end >= window_start)
);

CREATE INDEX fetch_windows_run_idx ON raw.fetch_windows (run_id);

-- -----------------------------------------------------------------------------
-- raw.candles: API responses as landed. Append-only by design.
--
-- A raw row's timeframe is its run's granularity_seconds, reached through
-- run_id. Every row of a run shares it, so storing it again per row would
-- only add a column that could disagree with its run.
--
-- No uniqueness constraint here on purpose: if the same bucket is fetched
-- twice (overlap re-fetch, retried run), both copies land. Deduplication is
-- the core layer's job, and a raw layer that applies no rules makes
-- idempotency simple to reason about. Replaying any run can only ever add
-- rows here, never corrupt.
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
-- core.ohlcv: the fact table. One row per asset per timeframe per bucket, UTC.
--
-- Composite natural PK (asset_id, granularity_seconds, ts_utc) instead of a
-- surrogate id: the grain IS the identity, the PK is also the upsert
-- conflict target, and the implicit index serves the dominant access
-- pattern (one asset at one timeframe over a time range). A surrogate key
-- would add a column and an index while enforcing nothing.
--
-- Each timeframe is fetched from Coinbase on its own rather than built from
-- 1-minute candles. Coinbase leaves out a bucket with no trades, so a
-- thinly traded product's 1-minute series is full of holes, and nothing in
-- the answer says whether a hole is a quiet minute or one the exchange
-- failed to record. Building 15-minute bars from it would treat every hole
-- as a quiet minute without saying so, and would cost fifteen times the
-- rows. The price of fetching separately is that the timeframes can
-- disagree, and on Coinbase they do; Q21 in 04_queries.sql measures how
-- often.
--
-- CHECK constraints encode OHLC invariants. They have caught real API
-- glitches in similar pipelines, and checking at insert time costs little.
--
-- low > 0 is not cosmetic. marts.daily_metrics computes ln(close/lag(close)),
-- and a single zero close makes that raise "cannot take logarithm of zero",
-- which aborts the whole matview refresh. Because low <= open, close and
-- high, one constraint on low makes all four prices positive. The check
-- lives here rather than on raw.candles on purpose: raw applies no rules
-- and accepts whatever the API sent, so a bad payload is still inspectable.
--
-- Not partitioned. Six assets at 5-minute, 15-minute and 1-hour grain from
-- 2020 is about 5.4 million rows (measured 2026-10-09), and range
-- partitioning on ts_utc usually becomes worth its operational cost only past
-- tens of millions.
-- The 1-minute timeframe is what would approach that line: six assets over
-- six years is about nineteen million rows on its own. Below the line partitioning only
-- complicates the upsert and vacuum story, and the PK already leads with
-- asset_id, so pruning would save little. Documented so the decision can be
-- defended, and reversed, on evidence rather than on habit.
-- -----------------------------------------------------------------------------
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
