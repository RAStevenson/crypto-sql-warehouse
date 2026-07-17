-- Refresh the marts layer in dependency order. Run after each ingestion
-- (pipeline/ingest.py --refresh-marts runs these same three statements).
--
-- CONCURRENTLY so a connected Tableau session never reads a half-refreshed
-- view; it requires the unique indexes created in 02_marts.sql and cannot
-- run inside a transaction block, which is why this lives in a script
-- instead of a stored function.

REFRESH MATERIALIZED VIEW CONCURRENTLY marts.daily_ohlcv;
REFRESH MATERIALIZED VIEW CONCURRENTLY marts.daily_metrics;
REFRESH MATERIALIZED VIEW CONCURRENTLY marts.pairwise_correlations_90d;
