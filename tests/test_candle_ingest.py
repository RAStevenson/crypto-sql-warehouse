"""Watermark arithmetic and work-list resolution against a stub cursor, and
the load path against a fake connection. No database and no network."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_warehouse import candle_ingest
from crypto_warehouse.candle_ingest import (
    OVERLAP_HOURS,
    LoadResult,
    get_watermark,
    resolve_products,
)
from crypto_warehouse.coinbase_candles import Candle
from crypto_warehouse.database_connection import ConfigError
from crypto_warehouse.fetch_windows import HOUR

NOW = datetime(2026, 8, 3, 14, 27, 41, 123456, tzinfo=UTC)
NOW_HOUR = NOW.replace(minute=0, second=0, microsecond=0)


class StubCursor:
    """Enough of a psycopg2 cursor to check the query-shaped logic without a
    database: it records what was executed and hands back canned rows."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


def watermark(latest_timestamp, backfill_days=365, *, apply_floor=False):
    cursor = StubCursor([(latest_timestamp,)])
    return get_watermark(cursor, "BTC-USD", backfill_days, apply_floor=apply_floor, now_utc=NOW)


def test_get_watermark_cold_start_uses_backfill_days():
    assert watermark(None, backfill_days=90) == NOW_HOUR - timedelta(days=90)


def test_get_watermark_cold_start_is_hour_aligned():
    resume_point = watermark(None, backfill_days=7)

    assert (resume_point.minute, resume_point.second, resume_point.microsecond) == (0, 0, 0)
    assert resume_point.tzinfo is UTC


def test_get_watermark_populated_subtracts_the_overlap():
    latest_timestamp = datetime(2026, 8, 3, 11, tzinfo=UTC)

    assert watermark(latest_timestamp) == latest_timestamp - timedelta(hours=OVERLAP_HOURS)


def test_get_watermark_returns_the_resume_point_in_utc_when_max_ts_carries_an_offset():
    # psycopg2 hands a timestamp back in the session's time zone. This one is
    # 11:00 UTC written with a +05:30 offset.
    latest_timestamp = datetime(2026, 8, 3, 16, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    resume_point = watermark(latest_timestamp)

    assert resume_point == datetime(2026, 8, 3, 11, tzinfo=UTC) - timedelta(hours=OVERLAP_HOURS)
    assert resume_point.tzinfo is UTC


@pytest.mark.parametrize("backfill_days", [1, 90, 365, 3650])
def test_get_watermark_ignores_backfill_days_on_an_ordinary_run(backfill_days):
    """With no --backfill-days on the command line there is no floor, so a
    routine incremental run re-fetches the overlap and nothing more. If the
    floor were always applied, the 365-day default would send every run back a
    full year."""
    latest_timestamp = NOW_HOUR - timedelta(hours=1)

    assert watermark(latest_timestamp, backfill_days=backfill_days) == latest_timestamp - timedelta(
        hours=OVERLAP_HOURS
    )


def test_get_watermark_honors_the_backfill_floor():
    """Regression: --backfill-days used to be consulted only when a product had
    no rows at all, so --backfill-days 365 against a 90-day warehouse fetched
    nothing extra and exited 0."""
    latest_timestamp = NOW_HOUR - timedelta(days=90)

    resume_point = watermark(latest_timestamp, backfill_days=365, apply_floor=True)

    assert resume_point == NOW_HOUR - timedelta(days=365)


def test_get_watermark_floor_reaches_back_from_a_current_watermark():
    # An up-to-date warehouse plus an explicit --backfill-days 30 still reaches
    # back 30 days: the flag says "cover at least this much", not "cold start
    # only".
    latest_timestamp = NOW_HOUR - timedelta(hours=1)

    resume_point = watermark(latest_timestamp, backfill_days=30, apply_floor=True)

    assert resume_point == NOW_HOUR - timedelta(days=30)


def test_get_watermark_floor_does_not_skip_a_stale_watermark():
    # Newest row is 500 days old. The floor is 365 days back, but resuming
    # there would leave a 135-day hole, so the older resume point is used.
    latest_timestamp = NOW_HOUR - timedelta(days=500)

    resume_point = watermark(latest_timestamp, backfill_days=365, apply_floor=True)

    assert resume_point == latest_timestamp - timedelta(hours=OVERLAP_HOURS)


def test_get_watermark_queries_the_named_product():
    cursor = StubCursor([(None,)])

    get_watermark(cursor, "SOL-USD", 30, now_utc=NOW)

    (_, params) = cursor.executed[0]
    assert params == ("SOL-USD",)


def test_resolve_products_defaults_to_active_assets():
    cursor = StubCursor([("ADA-USD",), ("BTC-USD",)])

    assert resolve_products(cursor, None) == ["ADA-USD", "BTC-USD"]
    assert "is_active" in cursor.executed[0][0]


def test_resolve_products_named_explicitly_overrides_is_active(caplog):
    cursor = StubCursor([("BTC-USD", True), ("DOGE-USD", False)])

    with caplog.at_level("WARNING"):
        products = resolve_products(cursor, ["BTC-USD", "DOGE-USD"])

    assert products == ["BTC-USD", "DOGE-USD"]
    assert "DOGE-USD" in caplog.text  # the override is never silent


def test_resolve_products_rejects_unknown_products():
    cursor = StubCursor([("BTC-USD", True)])

    with pytest.raises(ConfigError) as caught:
        resolve_products(cursor, ["BTC-USD", "XYZ-USD"])

    assert "XYZ-USD" in str(caught.value)


# --------------------------------------------------------------------------
# The load path. fetch_window, the pause between windows, and execute_values
# are replaced, and the connection is a fake that records what it was asked.
# --------------------------------------------------------------------------
WINDOW_START = datetime(2026, 7, 1, 0, tzinfo=UTC)


class FakeCursor:
    """Reports every execute to its connection and hands back the connection's
    canned rows."""

    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        statement = " ".join(sql.split())
        self.connection.executed.append((statement, params))
        self.connection.log.append(statement)

    def fetchone(self):
        return self.connection.rows.pop(0)

    def fetchall(self):
        return self.connection.batches.pop(0) if self.connection.batches else []


class FakeConnection:
    """Enough of a psycopg2 connection for the loader: it counts commits and
    rollbacks, records every statement its cursors execute, and keeps one
    ordered log of all three so a test can check what came first."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.batches: list[list[tuple]] = []
        self.executed: list[tuple[str, object]] = []
        self.log: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.log.append("commit")

    def rollback(self):
        self.rollbacks += 1
        self.log.append("rollback")

    def close(self):
        self.closed = True


@pytest.fixture()
def raw_writes(monkeypatch):
    """Skip the pause between windows, and capture the rows handed to
    execute_values instead of sending them."""
    captured: list[tuple[str, list[tuple]]] = []

    def record(cursor, sql, rows):
        captured.append((" ".join(sql.split()), list(rows)))

    monkeypatch.setattr(candle_ingest.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(candle_ingest.psycopg2.extras, "execute_values", record)
    return captured


def serve(monkeypatch, candles):
    """Make fetch_window answer every window with the same candles."""

    def fake_fetch_window(session, product_code, window_start, window_end):
        return list(candles)

    monkeypatch.setattr(candle_ingest, "fetch_window", fake_fetch_window)


def flat_candle(bucket_start):
    one = Decimal(1)
    return Candle(bucket_start, low=one, high=one, open=one, close=one, volume=one)


def window_of_300_without(missing_offsets):
    """A full 300-hour window from WINDOW_START, less the hours at the given
    offsets."""
    return [flat_candle(WINDOW_START + i * HOUR) for i in range(300) if i not in missing_offsets]


def fetch_300_hour_window(connection):
    return candle_ingest._fetch_into_raw(
        connection, None, "BTC-USD", 42, WINDOW_START, WINDOW_START + 300 * HOUR
    )


def test_fetch_into_raw_writes_each_candle_to_raw_in_ddl_column_order(monkeypatch, raw_writes):
    # Five distinct values, so any two fields trading places changes the row.
    candle = Candle(
        bucket_start=WINDOW_START,
        low=Decimal("1.1"),
        high=Decimal("4.4"),
        open=Decimal("2.2"),
        close=Decimal("3.3"),
        volume=Decimal("5.5"),
    )
    serve(monkeypatch, [candle])
    connection = FakeConnection()

    rows_fetched, _ = candle_ingest._fetch_into_raw(
        connection, None, "BTC-USD", 42, WINDOW_START, WINDOW_START + HOUR
    )

    ((sql, rows),) = raw_writes
    assert "(run_id, product_code, bucket_start, open, high, low, close, volume)" in sql
    assert rows == [
        (
            42,
            "BTC-USD",
            WINDOW_START,
            Decimal("2.2"),  # open
            Decimal("4.4"),  # high
            Decimal("1.1"),  # low
            Decimal("3.3"),  # close
            Decimal("5.5"),  # volume
        )
    ]
    assert rows_fetched == 1
    assert connection.commits == 1


def test_fetch_into_raw_counts_a_window_missing_six_consecutive_hours_of_300_as_short(
    monkeypatch, raw_writes, caplog
):
    serve(monkeypatch, window_of_300_without(range(100, 106)))

    with caplog.at_level("WARNING"):
        rows_fetched, short_windows = fetch_300_hour_window(FakeConnection())

    assert short_windows == 1
    assert rows_fetched == 294
    (warning,) = [record for record in caplog.records if record.levelname == "WARNING"]
    assert "missing 6 of 300 buckets" in warning.getMessage()
    assert (WINDOW_START + 100 * HOUR).isoformat() in warning.getMessage()


def test_fetch_into_raw_does_not_count_a_window_missing_two_isolated_hours_of_300_as_short(
    monkeypatch, raw_writes, caplog
):
    serve(monkeypatch, window_of_300_without({50, 200}))

    with caplog.at_level("WARNING"):
        rows_fetched, short_windows = fetch_300_hour_window(FakeConnection())

    assert short_windows == 0
    assert rows_fetched == 298
    assert [record for record in caplog.records if record.levelname == "WARNING"] == []


def test_fetch_into_raw_drops_candles_outside_the_requested_window(monkeypatch, raw_writes, caplog):
    current_hour = WINDOW_START + 3 * HOUR
    inside = [flat_candle(WINDOW_START + i * HOUR) for i in range(3)]
    before_the_window = flat_candle(WINDOW_START - HOUR)
    forming_hour = flat_candle(current_hour)
    serve(monkeypatch, [before_the_window, *inside, forming_hour])

    with caplog.at_level("WARNING"):
        rows_fetched, short_windows = candle_ingest._fetch_into_raw(
            FakeConnection(), None, "BTC-USD", 42, WINDOW_START, current_hour
        )

    ((_, rows),) = raw_writes
    assert [row[2] for row in rows] == [candle.bucket_start for candle in inside]
    assert rows_fetched == 3
    assert short_windows == 0
    assert "dropped 2 candle(s) outside the requested window" in caplog.text


@pytest.mark.parametrize(
    "error",
    [RuntimeError("gave up fetching"), KeyboardInterrupt()],
    ids=["RuntimeError", "KeyboardInterrupt"],
)
def test_load_product_marks_the_run_failed_and_reraises_when_the_fetch_raises(monkeypatch, error):
    def failing_fetch_window(session, product_code, window_start, window_end):
        raise error

    monkeypatch.setattr(candle_ingest, "fetch_window", failing_fetch_window)
    # The product has no rows yet, so the one-day cold start gives the fetch a
    # window to fail on. 17 is the id of the run row the load inserts.
    connection = FakeConnection(rows=[(None,), (17,)])

    with pytest.raises(type(error)) as caught:
        candle_ingest.load_product(connection, None, "BTC-USD", backfill_days=1)

    assert caught.value is error
    assert connection.rollbacks == 1
    ((sql, params),) = [entry for entry in connection.executed if "status = 'failed'" in entry[0]]
    assert sql.startswith("UPDATE raw.ingest_runs SET status = 'failed'")
    assert params == (17,)
    # Rolled back first, then the failed status written and committed.
    assert connection.log.index("rollback") < connection.log.index(sql)
    assert connection.log[connection.log.index(sql) + 1] == "commit"


def test_run_ingest_keeps_going_after_a_failed_product_and_rolls_back_before_the_next(
    monkeypatch,
):
    connection = FakeConnection()
    # Each attempt, with the rollback count the connection showed as it began.
    attempts: list[tuple[str, int]] = []

    def fake_load_product(connection, session, product_code, backfill_days, *, apply_floor=False):
        attempts.append((product_code, connection.rollbacks))
        if product_code == "AAA-USD":
            raise RuntimeError("the first product fails")
        return LoadResult(
            product_code, run_id=5, rows_fetched=10, rows_upserted=10, short_windows=0
        )

    monkeypatch.setattr(candle_ingest.database_connection, "connect", lambda: connection)
    monkeypatch.setattr(
        candle_ingest, "resolve_products", lambda cursor, requested: ["AAA-USD", "BBB-USD"]
    )
    monkeypatch.setattr(candle_ingest, "load_product", fake_load_product)

    exit_code = candle_ingest.run_ingest()

    assert exit_code == 1
    assert attempts == [("AAA-USD", 0), ("BBB-USD", 1)]
    assert connection.closed


# --------------------------------------------------------------------------
# promotion to core, and the rows it leaves in raw
# --------------------------------------------------------------------------
REPO = Path(__file__).resolve().parents[1]


def conditions(sql_text: str) -> set[str]:
    """The individual comparisons in a block of AND-ed conditions, with the
    raw alias and repeated spaces removed so two spellings compare equal."""
    flat_text = " ".join(sql_text.replace("r.", "").split())
    return {part.strip() for part in re.split(r"\bAND\b", flat_text) if part.strip()}


def core_check_constraints() -> set[str]:
    """Every CHECK comparison the DDL declares on core.ohlcv_hourly."""
    ddl = (REPO / "sql" / "01_schema.sql").read_text(encoding="utf-8")
    table = ddl[ddl.index("CREATE TABLE core.ohlcv_hourly") :]
    table = table[: table.index(");")]
    found: set[str] = set()
    for body in re.findall(r"CHECK \(([^()]*)\)", table):
        found |= conditions(body)
    return found


def test_core_row_checks_repeat_every_check_constraint_on_the_core_table():
    assert core_check_constraints() == {
        "volume >= 0",
        "high >= low",
        "high >= open",
        "high >= close",
        "low <= open",
        "low <= close",
        "low > 0",
    }
    assert conditions(candle_ingest.CORE_ROW_CHECKS) == core_check_constraints()


def test_the_upsert_shown_in_the_query_file_carries_the_same_row_checks():
    queries = (REPO / "sql" / "04_queries.sql").read_text(encoding="utf-8")
    query_20 = queries[queries.index("-- Q20 ") :]
    statement = "\n".join(line for line in query_20.splitlines() if not line.startswith("--"))
    where_clause = statement[statement.index("status = 'succeeded')") : statement.index("ORDER BY")]
    shown = conditions(where_clause.split("AND", 1)[1])
    assert shown == conditions(candle_ingest.CORE_ROW_CHECKS)


def test_both_promotion_statements_filter_on_the_row_checks():
    checks = " ".join(candle_ingest.CORE_ROW_CHECKS.split())
    assert f"AND ( {checks} )" in " ".join(candle_ingest.UPSERT_SQL.split())
    assert f"AND NOT ( {checks} )" in " ".join(candle_ingest.REJECTED_ROWS_SQL.split())


def test_promote_to_core_warns_about_rows_core_would_reject_and_still_promotes(caplog):
    connection = FakeConnection()
    connection.batches = [[(WINDOW_START,), (WINDOW_START + HOUR,)]]

    with caplog.at_level("WARNING"):
        candle_ingest._promote_to_core(connection, run_id=7, rows_fetched=5)

    statements = [statement for statement, _ in connection.executed]
    assert statements[0].startswith("SELECT r.bucket_start FROM raw.candles r")
    assert statements[1].startswith("INSERT INTO core.ohlcv_hourly")
    assert "status = 'succeeded'" in statements[2]
    assert connection.commits == 1
    assert "2 raw candle(s) fail core's constraints" in caplog.text
    assert WINDOW_START.isoformat() in caplog.text


def test_promote_to_core_is_silent_when_every_row_passes(caplog):
    connection = FakeConnection()

    with caplog.at_level("WARNING"):
        candle_ingest._promote_to_core(connection, run_id=7, rows_fetched=5)

    assert caplog.text == ""
    assert connection.commits == 1
