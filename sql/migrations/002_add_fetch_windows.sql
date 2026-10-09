-- =============================================================================
-- Migration 002: add raw.fetch_windows to a warehouse built before it existed.
--
-- The table records every window the loader requests from Coinbase and how
-- many candles came back, which check 1 in 05_verify.sql uses to tell a
-- candle the loader never asked for from one the exchange did not return.
-- Its definition is copied from sql/01_schema.sql, and
-- tests/test_database_setup.py holds the two against each other.
--
-- Apply it with:
--   python scripts/run_setup.py --migrate
--
-- Runs loaded before this migration have no windows on record, so check 1
-- fails on every gap they left. Re-requesting those gaps records the
-- windows they sit in, from a real request to the exchange:
--   python scripts/run_ingest.py --recheck-gaps
-- =============================================================================

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
