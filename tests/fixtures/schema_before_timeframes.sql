-- A warehouse as it stood before migration 001 (commit c2b8633), for the
-- migration test in tests/test_against_postgres.py. The DDL is
-- sql/01_schema.sql from that commit with its comments removed. The one
-- materialized view below stands in for the marts of that time, which read
-- core.ohlcv_hourly, so the test proves the migration clears them before it
-- drops the table they depend on.


DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET timezone = ''UTC''', current_database());
END $$;

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS marts;

CREATE TABLE core.assets (
    asset_id      smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol        text NOT NULL UNIQUE,
    product_code  text NOT NULL UNIQUE,
    asset_name    text NOT NULL,
    quote_currency text NOT NULL DEFAULT 'USD',
    is_active     boolean NOT NULL DEFAULT true,
    added_at      timestamptz NOT NULL DEFAULT now()
);

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

CREATE TABLE raw.candles (
    raw_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id        bigint NOT NULL REFERENCES raw.ingest_runs (run_id),
    product_code  text NOT NULL,
    bucket_start  timestamptz NOT NULL,
    open          numeric(20,8) NOT NULL,
    high          numeric(20,8) NOT NULL,
    low           numeric(20,8) NOT NULL,
    close         numeric(20,8) NOT NULL,
    volume        numeric(30,8) NOT NULL,
    fetched_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX candles_run_idx ON raw.candles (run_id);

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
    CHECK (low  <= open AND low  <= close),
    CHECK (low  > 0)
);

CREATE INDEX ohlcv_hourly_ts_idx ON core.ohlcv_hourly (ts_utc);

INSERT INTO core.assets (symbol, product_code, asset_name)
VALUES
    ('BTC',  'BTC-USD',  'Bitcoin'),
    ('ETH',  'ETH-USD',  'Ethereum'),
    ('SOL',  'SOL-USD',  'Solana'),
    ('ADA',  'ADA-USD',  'Cardano'),
    ('LINK', 'LINK-USD', 'Chainlink'),
    ('DOGE', 'DOGE-USD', 'Dogecoin');

CREATE MATERIALIZED VIEW marts.daily_ohlcv AS
SELECT asset_id, (ts_utc AT TIME ZONE 'UTC')::date AS trade_date, count(*) AS hours_present
FROM core.ohlcv_hourly
GROUP BY asset_id, (ts_utc AT TIME ZONE 'UTC')::date;
