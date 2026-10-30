# crypto-sql-warehouse

This is a PostgreSQL warehouse of crypto market data, loaded daily from the
public Coinbase Exchange API. A set of checks fails when the loaded data
breaks one of its rules. A candle is the open, high, low, close, and volume
for one interval of time. The warehouse keeps candles for six assets at up to
five intervals, called timeframes, from 1 minute to 1 day. The candles land in
three schemas (named groups of tables), raw, core, and marts, described under
the section "How it works". The marts are the tables built for analysis. They
are built and tested by DBT (data build tool), which runs SQL files against a
database in dependency order. A CSV export writes stored candles, or the daily
figures built from them, to a file.

The CSV export makes quick calculations on stored prices practical without a
fresh download from the exchange. The warehouse is published for anyone who
wants hourly or finer crypto candles in a database or a CSV without writing a
loader.

## Quick start

Requires git, Python 3.11 or 3.12, and a PostgreSQL server to connect to, such
as one from the postgresql.org installer or a Docker container. Verified on
PostgreSQL 17.10 and Python 3.11. `run_setup.py` creates the database if it is
missing, so the connecting user needs permission to create one, or the
database is created first by hand. DBT installs with the other dependencies
and needs no setup of its own, because the scripts hand it the connection from
`.env`.

1. Clone the repository and enter it:

   ```
   git clone https://github.com/RAStevenson/crypto-sql-warehouse.git
   cd crypto-sql-warehouse
   ```

2. Create a virtual environment and activate it (on Mac or Linux the second
   line is `source .venv/bin/activate`):

   ```
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. Install the dependencies:

   ```
   pip install -r requirements.txt
   ```

4. Copy the example config (on Mac or Linux, `cp .env.example .env`), then
   set `DATABASE_URL` in `.env` to your server:

   ```
   copy .env.example .env
   ```

5. Create the database and its three schemas, have DBT build the marts, load
   a year of history, and refresh the marts:

   ```
   python scripts/run_setup.py
   python scripts/run_ingest.py
   ```

The first `run_ingest.py` run fetches every active product at every active
timeframe. A product is a Coinbase trading pair such as BTC-USD.
`run_setup.py` adds six products, all active, and five timeframes, of which 1
hour and 1 day are active. A run with no options therefore loads 1-hour and
1-day candles for those six products. The API caps a request at 300 candles,
so a year of hourly candles is about 30 requests per product. The first run
for all six took about 70 seconds on 2026-10-30, mart refresh included. Later
runs resume from the last loaded candle of each product and timeframe, called
the watermark, and need one request per product per day at each of those two
timeframes. The loader refreshes the marts when it finishes.

To keep finer candles too, switch their timeframe on and the next run
backfills it:

```
UPDATE core.timeframes SET is_active = true WHERE timeframe_code = '15m';
```

A year at 15 minutes is about 120 requests per product, and a year at 5
minutes about 350. To load a timeframe once without switching it on, name it:
`python scripts/run_ingest.py --timeframes 15m`. Loading 5 minutes, 15
minutes, and 1 hour for all six products from 2020 took about two hours on
2026-10-09.

The default first load is one year. To reach further back, pass the number
of days with `--backfill-days`:

```
python scripts/run_ingest.py --backfill-days 2500
```

Other entry points, with every argument listed under [Script
arguments](#script-arguments) at the end:

```
python scripts/run_verify.py                    # the DBT tests and the timezone check, nonzero exit on failure
python scripts/run_dbt.py run                   # refresh the marts only
python scripts/run_dbt.py docs generate         # DBT's dependency map and column docs, under dbt/target/
python scripts/run_add_asset.py XRP-USD --name Ripple   # register and backfill a new asset
python scripts/run_export.py prices --products BTC-USD --timeframe 15m --start 2024-01-01
python scripts/run_export.py daily --start 2025-01-01   # daily metrics, every active asset
python scripts/run_setup.py --migrate           # bring a warehouse built on an older schema up to date
pytest                                          # the test suite
```

The prices export writes one row per candle, with the columns listed under
Script arguments.

The daily export writes the rows of the view `marts.tableau_daily_extract`,
one per asset per day, with the daily candle, returns, moving averages,
30-day volatility, and drawdown, the fall from an earlier peak. Its columns
are described one by one in `dbt/models/marts/marts.yml`. The view is shaped
for a direct Tableau connection. No workbook is included in the repo.

## How it works

Candles arrive from the Coinbase API, land in raw as they came, are promoted
to core, and are rolled up into the marts, which DBT builds and tests.

```
Coinbase Exchange API (public, no key)
        |
        v  scripts/run_ingest.py  (windowed fetch, 300 candles/request,
        |                          per asset per active timeframe)
+-------------------+
| raw               |  as-landed candles, append-only, plus run and request records
|  raw.candles      |
|  raw.ingest_runs  |  each run records its timeframe
|  raw.fetch_windows|  every window requested, and how many candles came back
+-------------------+
        |
        v  one INSERT ... ON CONFLICT per run
+-------------------+
| core              |  typed, deduplicated, constrained facts
|  core.assets      |
|  core.timeframes  |  1m, 5m, 15m, 1h, 1d, each switched on or off
|  core.ohlcv       |  PK (asset_id, granularity_seconds, ts_utc), OHLC CHECKs
+-------------------+
        |
        v  dbt run: REFRESH MATERIALIZED VIEW CONCURRENTLY, from the 1-hour series
+-------------------+
| marts             |  DBT models: daily rollups, returns, rolling volatility,
|  daily_ohlcv      |  drawdowns, pairwise correlations
|  daily_metrics    |
|  pairwise_correlations_90d  |
|  tableau_daily_extract      |  <- shaped for Tableau
+-------------------+
```

Raw keeps every candle as it arrived and never updates a candle, so a bug
downstream is recoverable without calling the API again. Core holds one row
per candle, keyed by asset, timeframe, and start time, under row-level checks:
the high is the highest of the four prices, the low is the lowest and above
zero, and volume is not negative. The marts are rebuilt from core's 1-hour
series. The other timeframes feed the export and a few of the queries. The
loader's own record is two tables in raw: `ingest_runs`, one row per product
and timeframe per load with its status and row counts, and `fetch_windows`,
one row per request with how many candles came back.

The marts are a DBT project in `dbt/`. Each mart is one SELECT statement in
its own file under `dbt/models/marts/`, and each names the tables it reads,
so DBT knows to build `daily_ohlcv` before `daily_metrics`. All but one of
the warehouse checks are DBT tests, either a column rule in a yml file, such
as `not_null`, or a SELECT under `dbt/tests/` that returns the rows breaking
a rule and passes when it returns nothing. `scripts/run_verify.py` runs them
all and then the one check that is not a DBT test, and exits nonzero if
anything fails. `scripts/run_dbt.py` passes any other DBT command through,
such as `docs generate`, which writes a dependency map with every column
described under `dbt/target/`.

`scripts/scheduled_ingest.cmd` runs daily under Windows Task Scheduler on the
author's machine. It has run at 12:10 UTC on 64 days from 2026-08-11 to
2026-10-21. It was paused from 2026-10-06 to 2026-10-08, and five other days
in that span had no run. The script loads, then runs `run_verify.py`, then
DBT's freshness check, which warns when the newest 1-hour candle is more than
two days old and fails past four. All three append to one log, and the script
exits with the first failure, so the task history shows it. A day the machine
was off is made up in full by the next run, because every run resumes from the
watermark.

Every push to main runs two GitHub Actions jobs. One runs the linter, the
type checker, and the test suite with no database. The other installs
PostgreSQL 17 and runs `run_setup.py`, then loads the fixture
`tests/fixtures/warehouse_sample.sql` in place of a call to Coinbase. That
fixture is a dump of 45 days of hourly and two days of 15-minute candles for
two products, with 1-day candles built from the hourly ones. The job then
refreshes the marts and runs `run_verify.py` and both exports. Forty-five
days is enough for the marts to fill in returns and 30-day averages, so a
mart that breaks a rule on those columns fails there before it reaches a
loaded warehouse.

In a database of its own, the same job then runs the loader's own tests in
`tests/test_against_postgres.py`. Those tests load candles through the
loader's own functions from a stand-in for the Coinbase API and check what
landed in raw, core, and `raw.fetch_windows`. They also check that a second
load rewrites nothing, that the migrations upgrade an older warehouse without
losing a candle, and that every DBT test passes on what the loader wrote.
When `CRYPTO_WAREHOUSE_TEST_DATABASE_URL` is unset they are skipped, so a
plain `pytest` needs no database and no network.

## Design decisions

**The load is idempotent.** Running it twice leaves core as one run would. The
loader writes core with an upsert, an insert that becomes an update when the
row exists, keyed by asset, timeframe, and start time. The update is skipped
when the values are unchanged, so replaying any window changes nothing unless
the source changed. A DBT test holds the loader to this. It takes the rows the
latest run updated and fails if any of them holds the same values an earlier
run had already loaded. Each run also re-fetches the three hours before its
watermark, in case Coinbase corrects a recent candle. On a daily run at 1 hour
or 15 minutes the overlap adds no request, since the day's candles and the
overlap fit in one. At 5 minutes they come to 325 candles, past the 300-candle
cap, so the run makes a second request. The overlap adds four duplicate raw
rows at 1 hour (the three hours and the watermark candle itself) and 37 at 5
minutes. At 1 day the re-fetch begins at the midnight before the watermark,
which adds two. Three hours is a guess at how late a correction might come. As
of 2026-10-10, in 457 successful runs since 2026-07-07, one per product and
timeframe per load, no later run had changed a candle in core, so the overlap
has not yet caught one.

**Gaps are judged against a record of what was asked for.** Coinbase leaves a
candle out of its answer when nothing traded in that interval and when the
exchange itself was down, so core has holes the pipeline did not make.
`raw.fetch_windows` records every window the loader requested. The gap test
returns a candle missing from core only when no successful request covered
it, which means the loader never asked. A candle missing from a window that
was requested is Coinbase's gap, not the pipeline's. The trade-off is that a
request the exchange answered with too few candles is treated like an
interval with no trades, so a candle it failed to serve once is not asked for
again. An earlier version guessed instead, by comparing assets, and called an
hour missing for some assets but not all a pipeline bug. At 17:00 and 21:00
UTC on 2023-03-04 Coinbase has hourly candles for BTC and ETH and none for
the other four, so that guess failed on two hours where the exchange itself
left four assets out. Judging each asset by its own requests holds for any
asset added later and for every timeframe.

**A candle core would reject stays in raw.** A candle that fails one of
core's checks, such as a low of zero, is left in raw and logged, and the rest
of the run loads. Otherwise that one candle would abort the upsert for its
product and timeframe on every later run too, because the watermark could
never move past it. The cost is one missing candle in core, and
`run_verify.py` fails on it until a valid copy loads.

**Queries do not depend on the session timezone.** `date_trunc()` on a
`timestamptz` buckets by the session's timezone, so on a server defaulting to
America/Chicago the monthly query (Q02 in `sql/04_queries.sql`) split its
months at 05:00 UTC. The first fix, `ALTER DATABASE ... SET timezone =
'UTC'`, was insufficient, because it only sets a default and any client can
override it at connect time, as Java database drivers (JDBC) commonly do. The
queries now use `ts_utc AT TIME ZONE 'UTC'`, which returns a plain timestamp
that no session setting can move. The regression check re-runs the daily
rollup's defining query, read live from the catalog, once under
America/Chicago and once under UTC, and fails if any row differs. It has to
change the session timezone partway through, which a single SELECT cannot, so
it is the one check that stays outside DBT, in `sql/05_verify.sql`.

**Prices are exact from the API to the table.** Prices are stored as
`numeric(20,8)` and volumes as `numeric(30,8)`. The API returns JSON numbers,
which Python would ordinarily parse as binary floating point, which cannot
hold most decimal prices exactly. The client parses them straight to Python's
exact `Decimal` type instead, and the database driver passes that on as
`numeric`, so exactness holds end to end rather than starting at the database
boundary.

**Each timeframe is fetched on its own, not built from 1-minute candles.**
Coinbase leaves out any candle with no trades, and nothing in the answer says
whether a missing minute was quiet or unrecorded. A 15-minute bar built from
1-minute candles would treat every hole as quiet without saying so. The price
of fetching separately is that the timeframes can disagree, and on Coinbase
they do. Q21 in `sql/04_queries.sql` rolls each timeframe up to every coarser
one it divides evenly and compares the result with the stored candles, one
coarser interval, or bucket, at a time. It counts only complete buckets, those
with every finer candle present. Over the data loaded back to 2020, prices
differ in under 0.1 percent of those buckets outside 2023 to 2025. Comparing
the pairs shows which series disagree with the others. In 2023 the 5-minute
series differs from the 15-minute in 18.3 percent of buckets and from the
1-hour in 18.9 percent. The 15-minute, 1-hour, and 1-day series agree with one
another in all but 0.08 percent, so the 5-minute series is the one that
disagrees. In 2024 the series split in two. The 5-minute and 15-minute differ
from each other in 0.02 percent of buckets, the 1-hour and 1-day differ on
0.68 percent of days, and each of the first pair differs from each of the
second in 1.25 to 1.86 percent. In 2025 the 1-hour and 1-day agree on every
complete day, and the disagreement sits in the 5-minute and 15-minute, which
differ from them in 3.3 to 4.9 percent of buckets. The warehouse holds no
1-minute candles, so Q21 skips that timeframe until they are loaded. A one-off
comparison on 2026-10-09 put the differences at a median of 0.015 percent on
the open and at most 1.8 percent on any price. Fetching sample hours again
that day returned exactly the stored values, so the disagreement is in
Coinbase's data.

**Daily bars are rolled up from the hourly series and compared with Coinbase's
1-day candles.** Coinbase's 1-day candle cannot show that hours are missing
from a day. On 2026-05-08 Coinbase has 19 hourly candles for each of the six
products, and its 1-day candle equals the rollup of those 19 and looks like
any other day. The rollup counts its hours and marks a day with fewer than 24
as incomplete, so the marts use it and the 1-day series is stored for
comparison. A bar built here from finer candles treats a missing candle as a
quiet one unless the candles are counted, which is why the other timeframes
are fetched on their own. Daily bars are the exception because missing hours
are rare and the count shows them. As of 2026-10-30, under 0.1 percent of the
hours across the 13,476 daily bars are missing. The same count on finer
candles would mark too many bars incomplete. In the week of 2026-10-01, about
a fifth of DOGE's 5-minute bars were missing at least one 1-minute candle. Q21
compares the rollup with the 1-day series, and they match exactly on open,
high, low, and close on 13,422 of the 13,437 complete daily bars from
2020-01-02 to 2026-10-29. The other 15 are ADA, DOGE, and LINK bars, 14 of
them in August 2024, and they differ by at most 0.69 percent on any price. The
cause is unknown.

**Incomplete days keep their row and lose their derived columns.** A day with
fewer than 24 hourly candles appears in `marts.daily_ohlcv` with its candle
and an `is_complete` flag of false, and in `marts.daily_metrics` its return,
moving averages, volatility, and drawdown are NULL. An earlier version
filtered incomplete days out at the presentation layer, which hid the rows
while leaving their effect inside every rolling metric computed around them.
A trailing partial day's fraction of a day's volume lowered the baseline the
volume-anomaly query (Q17 in `sql/04_queries.sql`) compares against. Rolling
metrics are also NULL until their window is full, so `ma_7d`, `ma_30d`, and
`volatility_30d_ann` wait for 7, 30, and 30 observations. Before that guard,
roughly half the mart's 30-day volatility values were warm-up values. The
first for BTC was 0.2076 against 0.4043 for its first full window.

**Three marts are materialized views, refreshed without blocking a reader.**
Three of the marts are materialized views, queries whose results PostgreSQL
stores and recomputes on refresh. Each has a unique index so the refresh can
run `CONCURRENTLY` and a connected Tableau session never blocks. DBT's
PostgreSQL adapter refreshes without `CONCURRENTLY`, so the project overrides
that one statement in `dbt/macros/`. DBT refreshes the three in dependency
order, each in its own transaction, so a session can briefly see a refreshed
`daily_ohlcv` beside a `daily_metrics` from the refresh before.

**No partitioning, and no 1-minute candles by default.** Six assets at 5
minutes, 15 minutes, and 1 hour from 2020 came to about 5.4 million rows,
measured 2026-10-09. Partitioning usually earns its cost past tens of
millions of rows, and until then it complicates the upsert and PostgreSQL's
routine cleanup for nothing. Loading 1-minute candles too would add about 19
million rows, and a year of them is about 1,750 requests per product, so the
1-minute timeframe is supported but off until someone needs it.

## Limitations and what comes next

Every price comes from one exchange in one quote currency, so there is no
second exchange to check a suspicious print against. The correlation mart is
a fixed 90 days ending on the latest day that has a return, not a rolling
series. The column `dollar_volume_approx` multiplies daily volume by the
daily close rather than a traded average price. Measured against volume times
`(H+L+C)/3` over the data loaded as of 2026-08-04, the error is -0.04 percent
on average and ranges from -4.16 to +3.14 percent.

Next would be a data-quality report built from `raw.ingest_runs`,
`raw.fetch_windows`, and the gap query, since the warehouse records every
short API response but nothing summarizes them. After that would come a
second exchange, so a suspicious print has something to be checked against.

## Layout

```
scripts/run_setup.py          create the database, apply the DDL, and have DBT build the marts
scripts/run_ingest.py         incremental loader, safe to rerun; refreshes the marts
scripts/run_add_asset.py      validate, register, and backfill a new asset
scripts/run_export.py         write candles or daily metrics to a CSV file
scripts/scheduled_ingest.cmd  the Task Scheduler entry point, logs to logs/
scripts/run_verify.py         the DBT tests, then the timezone check; nonzero exit on failure
scripts/run_dbt.py            any DBT command, with the connection set from .env
src/crypto_warehouse/         the package the scripts import
dbt/dbt_project.yml           the DBT project; profiles.yml beside it reads the connection from .env
dbt/models/marts/             the marts, one DBT model each, with the design rationale in comments
dbt/models/marts/marts.yml    every mart column described, with its tests
dbt/models/sources.yml        the raw and core tables DBT reads, every column described, with their tests and the freshness rule
dbt/tests/                    the warehouse checks, one SELECT each, returning the rows that break a rule
dbt/macros/                   the concurrent-refresh override
sql/01_schema.sql             schema DDL with design rationale in comments
sql/04_queries.sql            annotated analytical queries, each standalone
sql/05_verify.sql             the timezone check, the one assertion that is not a DBT test
sql/migrations/               in-place upgrades for a warehouse built earlier
tests/                        the test suite; all but test_against_postgres.py need no database
tests/fixtures/               a small loaded warehouse for the GitHub Actions job, and an older schema for the migration test
.github/workflows/tests.yml   the GitHub Actions jobs: the test suite, and the warehouse against PostgreSQL
requirements.txt              exact pins the project was last verified against
```

## Script arguments

Every argument of every script, mirroring `--help`:

- `run_setup.py --reset` drops the raw, core, and marts schemas before
  applying the DDL. It deletes all stored data.
- `run_setup.py --rebuild-marts` drops the marts schema and has DBT build it
  again from `dbt/models/marts/`. Use it to apply a changed mart definition to
  a warehouse that already holds data. Raw and core keep their data. It cannot
  be combined with `--reset`.
- `run_setup.py --migrate` brings a warehouse built before the current schema
  up to date in place, by applying whichever files in `sql/migrations/` it
  still needs. Every stored candle and run record is kept. A warehouse whose
  price table is still `core.ohlcv_hourly` gets its candles labeled 1h and its
  marts rebuilt. It cannot be combined with `--reset` or `--rebuild-marts`.
- `run_ingest.py --backfill-days N` sets how far back to reach, in days
  (default 365). For a product and timeframe with no rows yet it is the depth
  of the first load. Passing it explicitly also makes it a floor on an
  existing resume point, so `--backfill-days 365` against a 90-day warehouse
  extends it back to a year. Omitting it leaves the run purely incremental.
- `run_ingest.py --products PRODUCT [PRODUCT ...]` names the product codes to
  load (default: every asset with `is_active` in `core.assets`). Naming a
  product explicitly loads it even when `is_active` is false, and the override
  is logged.
- `run_ingest.py --timeframes TIMEFRAME [TIMEFRAME ...]` names the timeframes
  to load, from 1m, 5m, 15m, 1h, and 1d (default: every timeframe with
  `is_active` in `core.timeframes`). Naming a timeframe explicitly loads it
  even when `is_active` is false, and the override is logged.
- `run_ingest.py --recheck-gaps` replaces the usual load, which runs forward
  from the newest stored candle. It asks Coinbase again for every candle
  missing from core that no recorded request covers, as described under
  Design decisions, and loads any that come back. It records each window it
  asks for as it goes. A warehouse loaded before `raw.fetch_windows` existed
  needs it once after `--migrate`, because until then verify fails on every
  gap the earlier runs left. It cannot be combined with `--backfill-days`.
- `run_ingest.py --no-refresh-marts` skips the mart refresh (`dbt run`) that
  otherwise runs after loading.
- `run_add_asset.py PRODUCT` takes the Coinbase product code as its one
  required argument, e.g. `XRP-USD`.
- `run_add_asset.py --symbol SYMBOL` sets the ticker symbol to store
  (default: the part of the product code before the dash).
- `run_add_asset.py --name NAME` sets the human-readable asset name
  (default: the symbol).
- `run_add_asset.py --backfill-days N` sets the depth of the initial load in
  days (default 365).
- `run_add_asset.py --no-backfill` registers the asset only, and the next
  ingest run loads it. It cannot be combined with `--backfill-days`.
- `run_export.py prices` writes the stored candles for the chosen assets at
  one timeframe, with the symbol, product code, timeframe, the candle's start
  time in UTC, open, high, low, close, and volume in units of the coin on
  each row.
- `run_export.py prices --timeframe TIMEFRAME` picks that timeframe from 1m,
  5m, 15m, 1h, and 1d (default 1h).
- `run_export.py daily` writes the rows of `marts.tableau_daily_extract`, one
  per asset per day, with daily prices, returns, moving averages, 30-day
  volatility, and drawdown.
- `run_export.py --products PRODUCT [PRODUCT ...]`, on either export, names
  the products to export (default: every active asset).
- `run_export.py --start DATE` and `--end DATE`, on either export, limit it to
  UTC dates written as YYYY-MM-DD, both ends included (default: all stored
  history).
- `run_export.py --output PATH`, on either export, names the CSV file
  (default: a name built from what the export holds, under `exports/`, which
  git ignores).
- `run_dbt.py DBT_COMMAND` passes everything after the script's own options
  to DBT as given, with the connection set from `DATABASE_URL`, so
  `run_dbt.py test`, `run_dbt.py run --select daily_ohlcv`, and
  `run_dbt.py docs generate` all work. It is for the DBT commands the other
  scripts do not run for you.
- Every Python script accepts `--verbose` for debug-level logging, and
  `run_verify.py` takes no other arguments.
