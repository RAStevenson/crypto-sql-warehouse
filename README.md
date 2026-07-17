# crypto-sql-warehouse

A PostgreSQL warehouse for multi-asset crypto market data: an incremental
ingestion pipeline, a layered schema, derived analytical marts, and a set of
twenty annotated queries. I built it to demonstrate the SQL that data science
roles actually screen for -- window functions, CTEs, aggregation across time
granularities, idempotent incremental loading, and schema design -- against
real data rather than a toy dataset.

I'm a licensed mechanical PE moving into data science. My analytical
background is real; this project exists to make the SQL depth verifiable.

## Architecture

```
Coinbase Exchange API (public, no key)
        |
        v  pipeline/ingest.py  (windowed fetch, 300 candles/request)
+-------------------+
| raw               |  as-landed candles, append-only + run audit table
|  raw.candles      |
|  raw.ingest_runs  |
+-------------------+
        |
        v  single INSERT ... ON CONFLICT per run
+-------------------+
| core              |  typed, deduplicated, constrained facts
|  core.assets      |
|  core.ohlcv_hourly|  PK (asset_id, ts_utc), OHLC CHECK constraints
+-------------------+
        |
        v  REFRESH MATERIALIZED VIEW CONCURRENTLY
+-------------------+
| marts             |  daily rollups, returns, rolling volatility,
|  daily_ohlcv      |  drawdowns, pairwise correlations
|  daily_metrics    |
|  pairwise_corr... |
|  tableau_daily_extract  <- Tableau connects here
+-------------------+
```

The raw/core/marts split follows the standard ELT convention: raw is never
mutated, so any transform bug downstream is recoverable without touching the
API again. Core is the layer queries trust. Marts are presentation-shaped
and rebuilt from core.

## Design decisions worth defending

Idempotency lives in the database, not the pipeline. The upsert targets the
natural primary key `(asset_id, ts_utc)`, so replaying any time window is a
no-op unless the source data changed. The pipeline deliberately re-fetches a
three-hour overlap on every run to pick up late candle revisions, and that
costs nothing because of the upsert. The `DO UPDATE` clause carries an
`IS DISTINCT FROM` guard so unchanged rows are skipped entirely -- no dead
tuples, and `updated_at` only moves when data actually moved.

Prices are `numeric`, not floating point. A warehouse where `SUM(volume)`
drifts by rounding is a warehouse people stop trusting.

The schema pins the database timezone to UTC, and that line exists because
of a bug. `date_trunc()` on a `timestamptz` buckets by the session timezone,
so on a server defaulting to America/Chicago the monthly aggregates were
splitting at 05:00 UTC. I found it by running the full query set against a
live instance; the `-05` display offsets in the output were the visible
symptom. The pin and the reasoning are at the top of `sql/01_schema.sql`.

Daily bars are derived from hourly ones in SQL rather than fetched at daily
granularity. One source of truth: any daily/hourly disagreement can only be
a rollup bug, which is findable, never a source discrepancy, which isn't.

The fact table is not partitioned. Six assets at hourly grain is roughly 53k
rows per year; partitioning pays off orders of magnitude later, and until
then it complicates upserts and maintenance for nothing. The DDL comments
say where the crossover is.

Marts are materialized views with unique indexes so refreshes can run
`CONCURRENTLY` -- a connected Tableau session never blocks or reads a
half-refreshed state.

## Running it

Requires PostgreSQL 15+ and Python 3.10+.

```
createdb crypto_warehouse
psql -d crypto_warehouse -f sql/01_schema.sql
psql -d crypto_warehouse -f sql/02_marts.sql

pip install -r requirements.txt
copy .env.example .env        # edit DATABASE_URL if needed
```

The example connection string is the stock `postgres:postgres` on localhost.
That is a deliberate choice for a local demo, not a habit I would carry to a
shared environment: nothing here listens beyond localhost or holds data
worth protecting.

```
python pipeline/ingest.py --backfill-days 365 --refresh-marts
```

The first run backfills a year of hourly candles for six assets (about 180
API requests, a couple of minutes with rate-limit pauses). Every later run
resumes from the watermark and finishes in seconds. Rerunning is always
safe; that is the point.

`sql/04_interview_queries.sql` holds the twenty analytical queries. Each one
runs standalone against a loaded database.

To smoke-test a load, run the pipeline twice back to back and then:

```
psql -d crypto_warehouse -v ON_ERROR_STOP=1 -f scripts/verify.sql
```

Four assertions, each fatal on failure: no hourly gaps except exchange-wide
outages, daily rollup row counts match the distinct (asset, day) pairs in
hourly, every core row equals the newest raw copy of its bucket, and the
rerun upserted zero rows (one is tolerated in case an hour boundary crossed
between the two runs).

## Tableau

Connect Tableau to Postgres and point it at `marts.tableau_daily_extract`:
one row per asset per day with OHLCV, returns, 7/30-day moving averages,
annualized 30-day volatility, and drawdown from peak. Long format, since
that is what Tableau's shelf model wants. For a file-based extract instead
of a live connection:

```
psql -d crypto_warehouse -c "\copy (SELECT * FROM marts.tableau_daily_extract ORDER BY symbol, trade_date) TO 'tableau_extract.csv' CSV HEADER"
```

Refresh cadence: rerun the pipeline with `--refresh-marts`, or execute
`sql/03_refresh_marts.sql`.

## What I would build next

Scheduling (Task Scheduler or cron wrapping the pipeline), a dbt port of the
marts layer with tests, more granularities, and a data-quality report built
from `raw.ingest_runs` and the gap-detection query (Q15). The open questions
section of my study guide tracks the judgment calls I want to revisit.

## Layout

```
sql/01_schema.sql            schema DDL with design rationale in comments
sql/02_marts.sql             derived layers (rollups, volatility, correlations)
sql/03_refresh_marts.sql     mart refresh script, dependency-ordered
sql/04_interview_queries.sql twenty annotated analytical queries
pipeline/ingest.py           incremental, idempotent loader
scripts/verify.sql           smoke-test assertions, fatal on failure
docs/STUDY_GUIDE.md          my interview preparation map for every query
```
