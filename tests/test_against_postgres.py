"""The loader's SQL, run against a real PostgreSQL.

The rest of the suite needs no database. It stubs the connection and checks
the statements the code sends. These tests send them. Each one starts from an
empty warehouse built from sql/01_schema.sql. All but the migration test load
candles through the loader's own functions from a stand-in for the Coinbase
candles endpoint and read back what landed in raw, core, and raw.fetch_windows.
The migration test builds an older warehouse instead and upgrades it. The last
test runs dbt over what the loader wrote.

They run only when CRYPTO_WAREHOUSE_TEST_DATABASE_URL is set, which the GitHub
Actions warehouse job does. The tests drop and recreate that database, so its
name must end in _test, which keeps them away from a warehouse holding real
history. To run them by hand against a local server, in PowerShell:

    $env:CRYPTO_WAREHOUSE_TEST_DATABASE_URL = "postgresql://postgres@localhost:5432/crypto_warehouse_test"
    pytest tests/test_against_postgres.py
"""

from __future__ import annotations

import os
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from psycopg2 import sql as pgsql

from crypto_warehouse import candle_ingest, database_setup, dbt_project, sql_file_runner
from crypto_warehouse.candle_ingest import Timeframe
from crypto_warehouse.database_connection import REPO_ROOT, connect, split_database_name

TEST_DATABASE_URL = os.environ.get("CRYPTO_WAREHOUSE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="CRYPTO_WAREHOUSE_TEST_DATABASE_URL is not set",
)

# The loader reads the clock to decide which bucket is still forming. A fixed
# clock keeps every load in these tests the same, even one that runs across
# the turn of an hour.
FIXED_NOW = datetime(2026, 3, 10, 12, 30, tzinfo=UTC)
HOUR = Timeframe("1h", 3600)
FIFTEEN_MINUTES = Timeframe("15m", 900)
DAY = Timeframe("1d", 86400)
OLD_SCHEMA_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "schema_before_timeframes.sql"


class FixedClock(datetime):
    """A datetime whose now() is always FIXED_NOW."""

    @classmethod
    def now(cls, timezone=None):
        return FIXED_NOW


class FakeResponse:
    """Holds the rows one request returns, in the shape fetch_window reads."""

    status_code = 200

    def __init__(self, rows: list[list]):
        self.rows = rows

    def raise_for_status(self) -> None:
        return None

    def json(self, parse_float=None):
        return self.rows


class FakeCoinbase:
    """Stands in for requests.Session against the candles endpoint.

    Every bucket in a requested window gets a candle whose prices follow from
    its timestamp, newest first, as Coinbase returns them. A bucket in missing
    is left out, as the exchange leaves out an hour with no trades. A bucket
    in revised returns a different close, as a late correction would. A
    bucket in invalid returns a high below its low.
    """

    def __init__(self):
        self.headers: dict[str, str] = {}
        self.missing: set[tuple[str, int, datetime]] = set()
        self.revised: set[tuple[str, int, datetime]] = set()
        self.invalid: set[tuple[str, int, datetime]] = set()
        self.requested_windows: list[tuple[str, int, datetime, datetime]] = []

    def get(self, url: str, params: dict, timeout: float) -> FakeResponse:
        product_code = url.split("/products/")[1].split("/")[0]
        granularity_seconds = int(params["granularity"])
        window_start = datetime.fromisoformat(params["start"])
        window_end = datetime.fromisoformat(params["end"])
        self.requested_windows.append((product_code, granularity_seconds, window_start, window_end))
        rows = []
        bucket = window_start
        while bucket <= window_end:
            key = (product_code, granularity_seconds, bucket)
            if key not in self.missing:
                rows.append(self.candle_row(key))
            bucket += timedelta(seconds=granularity_seconds)
        return FakeResponse(list(reversed(rows)))

    def candle_row(self, key: tuple[str, int, datetime]) -> list:
        """The row for one bucket, as [time, low, high, open, close, volume]."""
        product_code, granularity_seconds, bucket = key
        epoch = int(bucket.timestamp())
        base = Decimal(100 + (epoch // granularity_seconds) % 40) + Decimal(len(product_code))
        low, high, open_price, close = base - 1, base + 2, base, base + 1
        if key in self.revised:
            close = high
        if key in self.invalid:
            high = low - 1
        volume = Decimal(epoch % 997) / 10
        return [epoch, low, high, open_price, close, volume]


def bucket_hours_before(hours: int) -> datetime:
    """The start of the hourly bucket the given number of hours before the
    bucket still forming at FIXED_NOW."""
    return FIXED_NOW.replace(minute=0) - timedelta(hours=hours)


@pytest.fixture(scope="module")
def test_database_url() -> str:
    """Create the test database afresh once for this module and return its URL."""
    split_url = split_database_name(TEST_DATABASE_URL)
    assert split_url is not None, "CRYPTO_WAREHOUSE_TEST_DATABASE_URL must be a postgresql:// URL"
    maintenance_url, database = split_url
    assert database.endswith("_test"), (
        f"refusing to drop {database!r}: these tests only run against a database "
        "whose name ends in _test"
    )
    with (
        closing(connect(maintenance_url, autocommit=True)) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            pgsql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(pgsql.Identifier(database))
        )
    database_setup.create_database(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest.fixture
def warehouse(test_database_url, monkeypatch):
    """An open connection to an empty warehouse at the current schema, with the
    loader's clock fixed and its pause between requests removed."""
    monkeypatch.setattr(candle_ingest, "datetime", FixedClock)
    monkeypatch.setattr(candle_ingest, "REQUEST_PAUSE_SECONDS", 0)
    with closing(connect(test_database_url)) as connection:
        database_setup.drop_schemas(connection)
        sql_file_runner.run_sql_file(connection, sql_file_runner.SCHEMA_FILE)
        connection.commit()
        yield connection


def query(connection, statement: str, parameters=None) -> list[tuple]:
    """Run one SELECT and return its rows, leaving no transaction open."""
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        rows = cursor.fetchall()
    connection.commit()
    return rows


def core_rows(connection, product_code: str, granularity_seconds: int) -> dict:
    """Map each stored bucket of one series to its (open, high, low, close, volume)."""
    rows = query(
        connection,
        """
        SELECT o.ts_utc, o.open, o.high, o.low, o.close, o.volume
        FROM core.ohlcv o JOIN core.assets a USING (asset_id)
        WHERE a.product_code = %s AND o.granularity_seconds = %s
        """,
        (product_code, granularity_seconds),
    )
    return {row[0]: tuple(row[1:]) for row in rows}


def unrecorded_gaps(connection, product_code: str, granularity_seconds: int) -> list:
    """The buckets of one series that core lacks and no succeeded request covered."""
    rows = query(
        connection,
        candle_ingest.UNRECORDED_GAPS_SQL,
        {"product_code": product_code, "granularity_seconds": granularity_seconds},
    )
    return [row[0] for row in rows]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def test_a_first_load_writes_every_closed_candle_to_raw_core_and_the_request_log(warehouse):
    coinbase = FakeCoinbase()

    result = candle_ingest.load_product(warehouse, coinbase, "BTC-USD", HOUR, 2)

    stored = core_rows(warehouse, "BTC-USD", 3600)
    expected_buckets = [bucket_hours_before(hours) for hours in range(48, 0, -1)]
    assert sorted(stored) == expected_buckets
    for bucket in expected_buckets:
        row = coinbase.candle_row(("BTC-USD", 3600, bucket))
        assert stored[bucket] == (row[3], row[2], row[1], row[4], row[5])
    assert result.rows_upserted == 48
    assert query(warehouse, "SELECT status FROM raw.ingest_runs") == [("succeeded",)]
    assert query(warehouse, "SELECT count(*) FROM raw.fetch_windows") == [(1,)]
    assert unrecorded_gaps(warehouse, "BTC-USD", 3600) == []


def test_a_second_load_rewrites_no_core_row_and_keeps_the_overlap_copies_in_raw(warehouse):
    coinbase = FakeCoinbase()
    candle_ingest.load_product(warehouse, coinbase, "BTC-USD", HOUR, 2)
    before = query(warehouse, "SELECT ts_utc, source_run_id, updated_at FROM core.ohlcv")

    result = candle_ingest.load_product(warehouse, coinbase, "BTC-USD", HOUR, 2)

    # The resume point is the newest stored candle minus the overlap, so the
    # second load fetches the overlap's hours and the newest candle again.
    assert result.rows_upserted == 0
    assert result.rows_fetched == candle_ingest.OVERLAP_HOURS + 1
    assert query(warehouse, "SELECT ts_utc, source_run_id, updated_at FROM core.ohlcv") == before
    copies = query(
        warehouse,
        "SELECT count(*) FROM raw.candles GROUP BY bucket_start HAVING count(*) > 1",
    )
    assert len(copies) == candle_ingest.OVERLAP_HOURS + 1


def test_a_revised_candle_in_the_overlap_replaces_the_core_row(warehouse):
    coinbase = FakeCoinbase()
    first = candle_ingest.load_product(warehouse, coinbase, "BTC-USD", HOUR, 2)
    revised_bucket = bucket_hours_before(2)
    coinbase.revised.add(("BTC-USD", 3600, revised_bucket))

    second = candle_ingest.load_product(warehouse, coinbase, "BTC-USD", HOUR, 2)

    assert second.rows_upserted == 1
    stored = core_rows(warehouse, "BTC-USD", 3600)
    revised_row = coinbase.candle_row(("BTC-USD", 3600, revised_bucket))
    assert stored[revised_bucket][3] == revised_row[4]
    sources = dict(query(warehouse, "SELECT ts_utc, source_run_id FROM core.ohlcv"))
    assert sources[revised_bucket] == second.run_id
    assert sources[bucket_hours_before(1)] == first.run_id


def test_a_candle_core_would_reject_stays_in_raw_and_the_run_still_succeeds(warehouse):
    coinbase = FakeCoinbase()
    bad_bucket = bucket_hours_before(10)
    coinbase.invalid.add(("BTC-USD", 3600, bad_bucket))

    result = candle_ingest.load_product(warehouse, coinbase, "BTC-USD", HOUR, 2)

    assert bad_bucket not in core_rows(warehouse, "BTC-USD", 3600)
    assert result.rows_upserted == 47
    in_raw = query(
        warehouse, "SELECT count(*) FROM raw.candles WHERE bucket_start = %s", (bad_bucket,)
    )
    assert in_raw == [(1,)]
    assert query(warehouse, "SELECT status FROM raw.ingest_runs") == [("succeeded",)]


def test_one_timeframe_never_overwrites_or_relabels_another(warehouse):
    coinbase = FakeCoinbase()

    candle_ingest.load_product(warehouse, coinbase, "ETH-USD", HOUR, 4)
    candle_ingest.load_product(warehouse, coinbase, "ETH-USD", FIFTEEN_MINUTES, 4)

    hourly = core_rows(warehouse, "ETH-USD", 3600)
    quarter_hourly = core_rows(warehouse, "ETH-USD", 900)
    assert len(hourly) == 96
    assert len(quarter_hourly) == 4 * 96 + 2
    for bucket, values in hourly.items():
        row = coinbase.candle_row(("ETH-USD", 3600, bucket))
        assert values == (row[3], row[2], row[1], row[4], row[5])
    # Four days at 15 minutes is more than 300 buckets, so it took two requests.
    windows = query(
        warehouse,
        """
        SELECT count(*) FROM raw.fetch_windows w JOIN raw.ingest_runs r USING (run_id)
        WHERE r.granularity_seconds = 900
        """,
    )
    assert windows == [(2,)]


# --------------------------------------------------------------------------
# Gaps and the request log
# --------------------------------------------------------------------------
def test_a_bucket_the_exchange_left_out_is_on_record_and_not_an_unrecorded_gap(warehouse):
    coinbase = FakeCoinbase()
    left_out = bucket_hours_before(20)
    coinbase.missing.add(("BTC-USD", 3600, left_out))

    candle_ingest.load_product(warehouse, coinbase, "BTC-USD", HOUR, 2)

    assert left_out not in core_rows(warehouse, "BTC-USD", 3600)
    assert unrecorded_gaps(warehouse, "BTC-USD", 3600) == []


def test_recheck_gaps_requests_a_gap_no_window_covers_and_records_the_request(warehouse):
    coinbase = FakeCoinbase()
    left_out = bucket_hours_before(20)
    coinbase.missing.add(("BTC-USD", 3600, left_out))
    candle_ingest.load_product(warehouse, coinbase, "BTC-USD", HOUR, 2)
    # A warehouse loaded before raw.fetch_windows existed has no window on
    # record, which is what deleting the rows reproduces.
    query(warehouse, "DELETE FROM raw.fetch_windows RETURNING fetch_id")
    assert unrecorded_gaps(warehouse, "BTC-USD", 3600) == [left_out]
    coinbase.missing.clear()

    result = candle_ingest.recheck_gaps(warehouse, coinbase, "BTC-USD", HOUR)

    assert result is not None
    assert coinbase.requested_windows[-1][2] <= left_out <= coinbase.requested_windows[-1][3]
    assert left_out in core_rows(warehouse, "BTC-USD", 3600)
    assert unrecorded_gaps(warehouse, "BTC-USD", 3600) == []
    assert candle_ingest.recheck_gaps(warehouse, coinbase, "BTC-USD", HOUR) is None


# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------
def test_migrations_bring_a_pre_timeframes_warehouse_up_to_date_and_keep_its_candles(
    warehouse, test_database_url
):
    database_setup.drop_schemas(warehouse)
    with warehouse.cursor() as cursor:
        cursor.execute(OLD_SCHEMA_FIXTURE.read_text(encoding="utf-8"))
        cursor.execute(
            """
            INSERT INTO raw.ingest_runs
                (product_code, granularity_seconds, window_start, window_end,
                 rows_fetched, rows_upserted, status)
            VALUES ('BTC-USD', 3600, '2026-03-01', '2026-03-01 02:00', 2, 2, 'succeeded')
            RETURNING run_id
            """
        )
        run_id = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO core.ohlcv_hourly
                (asset_id, ts_utc, open, high, low, close, volume, source_run_id)
            SELECT asset_id, ts, 100, 102, 99, 101, 5, %s
            FROM core.assets,
                 (VALUES ('2026-03-01 00:00+00'::timestamptz),
                         ('2026-03-01 01:00+00'::timestamptz)) AS hours(ts)
            WHERE product_code = 'BTC-USD'
            """,
            (run_id,),
        )
    warehouse.commit()
    assert database_setup.pending_migrations(warehouse) == [
        sql_file_runner.TIMEFRAMES_MIGRATION_FILE,
        sql_file_runner.FETCH_WINDOWS_MIGRATION_FILE,
        sql_file_runner.DAILY_TIMEFRAME_MIGRATION_FILE,
    ]
    warehouse.commit()

    assert database_setup.run_migrations(test_database_url) == 0

    assert database_setup.pending_migrations(warehouse) == []
    stored = core_rows(warehouse, "BTC-USD", 3600)
    assert sorted(stored) == [
        datetime(2026, 3, 1, 0, tzinfo=UTC),
        datetime(2026, 3, 1, 1, tzinfo=UTC),
    ]
    assert query(warehouse, "SELECT to_regclass('core.ohlcv_hourly') IS NULL") == [(True,)]
    assert query(
        warehouse, "SELECT is_active FROM core.timeframes WHERE timeframe_code = '1d'"
    ) == [(True,)]
    assert query(warehouse, "SELECT count(*) FROM marts.daily_ohlcv") == [(1,)]
    assert database_setup.run_migrations(test_database_url) == 0


# --------------------------------------------------------------------------
# The marts and the dbt tests over what the loader wrote
# --------------------------------------------------------------------------
def test_loaded_candles_pass_every_dbt_test_and_a_day_after_a_short_day_has_no_return(
    warehouse, test_database_url
):
    coinbase = FakeCoinbase()
    # Five hours left out of one day in the middle make that day incomplete
    # for BTC-USD only. The day after it is complete but has no complete day
    # directly before it, so the mart must give it no return.
    short_day = (FIXED_NOW - timedelta(days=4)).replace(hour=0, minute=0)
    for hour in range(8, 13):
        coinbase.missing.add(("BTC-USD", 3600, short_day + timedelta(hours=hour)))
    query(
        warehouse,
        "UPDATE core.assets SET is_active = (product_code IN ('BTC-USD', 'ETH-USD')) "
        "RETURNING asset_id",
    )
    for product_code in ("BTC-USD", "ETH-USD"):
        for timeframe in (HOUR, DAY):
            candle_ingest.load_product(warehouse, coinbase, product_code, timeframe, 8)
            candle_ingest.load_product(warehouse, coinbase, product_code, timeframe, 8)

    assert dbt_project.run_dbt(["run"], test_database_url) == 0
    assert dbt_project.run_dbt(["test"], test_database_url) == 0

    returns = dict(
        query(
            warehouse,
            """
            SELECT m.trade_date, m.simple_return
            FROM marts.daily_metrics m JOIN core.assets a USING (asset_id)
            WHERE a.product_code = %s
            """,
            ("BTC-USD",),
        )
    )
    short_date = short_day.date()
    assert returns[short_date] is None
    assert returns[short_date + timedelta(days=1)] is None
    assert returns[short_date + timedelta(days=2)] is not None
    eth_returns = dict(
        query(
            warehouse,
            """
            SELECT m.trade_date, m.simple_return
            FROM marts.daily_metrics m JOIN core.assets a USING (asset_id)
            WHERE a.product_code = 'ETH-USD'
            """,
        )
    )
    assert eth_returns[short_date + timedelta(days=1)] is not None


# --------------------------------------------------------------------------
# The 1-day timeframe and the timeframe comparison
# --------------------------------------------------------------------------
def test_a_day_series_loads_whole_utc_days_and_stops_before_the_day_still_forming(warehouse):
    coinbase = FakeCoinbase()

    candle_ingest.load_product(warehouse, coinbase, "BTC-USD", DAY, 3)

    assert sorted(core_rows(warehouse, "BTC-USD", 86400)) == [
        datetime(2026, 3, 7, tzinfo=UTC),
        datetime(2026, 3, 8, tzinfo=UTC),
        datetime(2026, 3, 9, tzinfo=UTC),
    ]
    assert unrecorded_gaps(warehouse, "BTC-USD", 86400) == []

    coinbase.requested_windows.clear()
    candle_ingest.load_product(warehouse, coinbase, "BTC-USD", DAY, 3)

    # The overlap of three hours floors to the midnight before the newest
    # candle, so a repeat load asks for the last two days again and no more.
    assert coinbase.requested_windows == [
        (
            "BTC-USD",
            86400,
            datetime(2026, 3, 8, tzinfo=UTC),
            datetime(2026, 3, 9, tzinfo=UTC),
        )
    ]


def run_timeframe_comparison(connection) -> dict:
    """Run Q21 and map (finer, coarser) to its summed counts."""
    queries = (REPO_ROOT / "sql" / "04_queries.sql").read_text(encoding="utf-8")
    rows = query(connection, queries[queries.index("-- Q21 ") :])
    counts: dict = {}
    for finer, coarser, _year, *columns in rows:
        totals = counts.setdefault((finer, coarser), [0] * len(columns))
        counts[(finer, coarser)] = [
            total + column for total, column in zip(totals, columns, strict=True)
        ]
    return counts


def test_q21_compares_every_timeframe_with_each_coarser_one_it_divides(warehouse):
    coinbase = FakeCoinbase()
    for timeframe in (FIFTEEN_MINUTES, HOUR, DAY):
        candle_ingest.load_product(warehouse, coinbase, "BTC-USD", timeframe, 3)
    query(
        warehouse,
        """
        WITH rollup AS (
            SELECT asset_id,
                   date_bin('1 day', ts_utc, TIMESTAMPTZ '2000-01-01 00:00:00+00') AS day,
                   (array_agg(open ORDER BY ts_utc ASC))[1] AS open,
                   max(high) AS high,
                   min(low) AS low,
                   (array_agg(close ORDER BY ts_utc DESC))[1] AS close,
                   sum(volume) AS volume
            FROM core.ohlcv
            WHERE granularity_seconds = 3600
            GROUP BY asset_id, day
        )
        UPDATE core.ohlcv o
        SET open = r.open, high = r.high, low = r.low, close = r.close, volume = r.volume
        FROM rollup r
        WHERE o.asset_id = r.asset_id
          AND o.granularity_seconds = 86400
          AND o.ts_utc = r.day
          AND o.ts_utc = '2026-03-08 00:00:00+00'
        RETURNING o.ts_utc
        """,
    )

    counts = run_timeframe_comparison(warehouse)

    assert set(counts) == {("15m", "1h"), ("15m", "1d"), ("1h", "1d")}
    # The hourly load starts at 12:00 on the 7th, so that day is partial. Of the
    # two complete days, the 8th now equals its rollup and the 9th does not,
    # because the fake exchange prices each timeframe differently.
    compared, complete, prices_differ, volume_less, volume_more = counts[("1h", "1d")]
    assert (compared, complete, prices_differ) == (3, 2, 1)
    # The volume counts leave the partial 7th out as well.
    assert volume_less + volume_more == 1
    assert counts[("15m", "1d")][:2] == [3, 2]
    compared, complete, *_ = counts[("15m", "1h")]
    assert compared == complete > 0

    with warehouse.cursor() as cursor:
        cursor.execute("SET TIME ZONE 'America/Chicago'")
    warehouse.commit()
    assert run_timeframe_comparison(warehouse) == counts


def test_the_stopped_loading_test_allows_a_day_behind_and_fails_a_stopped_day_series(
    warehouse, test_database_url
):
    coinbase = FakeCoinbase()
    query(
        warehouse,
        "UPDATE core.assets SET is_active = (product_code = 'BTC-USD') RETURNING asset_id",
    )
    for timeframe in (HOUR, DAY):
        candle_ingest.load_product(warehouse, coinbase, "BTC-USD", timeframe, 8)
    stopped_loading_test = ["test", "--select", "active_series_stopped_loading"]

    # The newest 1-day candle closes on the 8th, 60 hours before the newest
    # hourly close at noon on the 10th. That is a daily run missed and one
    # candle width, which the test allows.
    keep_daily_candles_through(warehouse, datetime(2026, 3, 7, tzinfo=UTC))
    assert dbt_project.run_dbt(stopped_loading_test, test_database_url) == 0

    # One day more and the series has stopped.
    keep_daily_candles_through(warehouse, datetime(2026, 3, 6, tzinfo=UTC))
    assert dbt_project.run_dbt(stopped_loading_test, test_database_url) != 0


def keep_daily_candles_through(connection, last_kept: datetime) -> None:
    """Delete the 1-day candles newer than last_kept."""
    query(
        connection,
        "DELETE FROM core.ohlcv WHERE granularity_seconds = 86400 AND ts_utc > %s RETURNING ts_utc",
        (last_kept,),
    )


def test_the_daily_timeframe_migration_applies_alone_and_keeps_the_candles(
    warehouse, test_database_url
):
    candle_ingest.load_product(warehouse, FakeCoinbase(), "BTC-USD", HOUR, 1)
    hourly_before = core_rows(warehouse, "BTC-USD", 3600)
    with warehouse.cursor() as cursor:
        cursor.execute("DELETE FROM core.timeframes WHERE granularity_seconds = 86400")
        cursor.execute(
            "ALTER TABLE core.timeframes DROP CONSTRAINT timeframes_granularity_seconds_check"
        )
        cursor.execute(
            "ALTER TABLE core.timeframes ADD CONSTRAINT timeframes_granularity_seconds_check "
            "CHECK (granularity_seconds IN (60, 300, 900, 3600))"
        )
    warehouse.commit()
    assert database_setup.pending_migrations(warehouse) == [
        sql_file_runner.DAILY_TIMEFRAME_MIGRATION_FILE
    ]
    warehouse.commit()

    assert database_setup.run_migrations(test_database_url) == 0

    assert database_setup.pending_migrations(warehouse) == []
    assert core_rows(warehouse, "BTC-USD", 3600) == hourly_before
    assert query(
        warehouse, "SELECT is_active FROM core.timeframes WHERE granularity_seconds = 86400"
    ) == [(True,)]
