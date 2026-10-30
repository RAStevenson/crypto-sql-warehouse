"""Resume-point arithmetic and work-list resolution against a stub cursor, and
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
    Timeframe,
    find_resume_point,
    resolve_products,
    resolve_timeframes,
)
from crypto_warehouse.coinbase_candles import Candle
from crypto_warehouse.database_connection import OperatorError

ONE_HOUR = timedelta(hours=1)
HOURLY = Timeframe("1h", 3600)

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


def resume_point(latest_timestamp, backfill_days=365, *, extend_to_backfill_days=False):
    cursor = StubCursor([(latest_timestamp,)])
    return find_resume_point(
        cursor,
        "BTC-USD",
        3600,
        backfill_days,
        extend_to_backfill_days=extend_to_backfill_days,
        now_utc=NOW,
    )


def test_find_resume_point_cold_start_uses_backfill_days():
    assert resume_point(None, backfill_days=90) == NOW_HOUR - timedelta(days=90)


def test_find_resume_point_cold_start_is_hour_aligned():
    fetch_start = resume_point(None, backfill_days=7)

    assert (fetch_start.minute, fetch_start.second, fetch_start.microsecond) == (0, 0, 0)
    assert fetch_start.tzinfo is UTC


def test_find_resume_point_populated_subtracts_the_overlap():
    latest_timestamp = datetime(2026, 8, 3, 11, tzinfo=UTC)

    assert resume_point(latest_timestamp) == latest_timestamp - timedelta(hours=OVERLAP_HOURS)


def test_find_resume_point_converts_an_offset_timestamp_to_utc():
    # psycopg2 hands a timestamp back in the session's time zone. This one is
    # 11:00 UTC written with a +05:30 offset.
    latest_timestamp = datetime(2026, 8, 3, 16, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    fetch_start = resume_point(latest_timestamp)

    assert fetch_start == datetime(2026, 8, 3, 11, tzinfo=UTC) - timedelta(hours=OVERLAP_HOURS)
    assert fetch_start.tzinfo is UTC


@pytest.mark.parametrize("backfill_days", [1, 90, 365, 3650])
def test_find_resume_point_ignores_backfill_days_on_an_ordinary_run(backfill_days):
    """With no --backfill-days on the command line there is no floor, so a
    routine incremental run re-fetches the overlap and nothing more. If the
    floor were always applied, the 365-day default would send every run back a
    full year."""
    latest_timestamp = NOW_HOUR - timedelta(hours=1)

    assert resume_point(
        latest_timestamp, backfill_days=backfill_days
    ) == latest_timestamp - timedelta(hours=OVERLAP_HOURS)


def test_find_resume_point_honors_the_backfill_floor():
    """Regression: --backfill-days used to be consulted only when a product had
    no rows at all, so --backfill-days 365 against a 90-day warehouse fetched
    nothing extra and exited 0."""
    latest_timestamp = NOW_HOUR - timedelta(days=90)

    fetch_start = resume_point(latest_timestamp, backfill_days=365, extend_to_backfill_days=True)

    assert fetch_start == NOW_HOUR - timedelta(days=365)


def test_find_resume_point_floor_reaches_back_from_a_current_watermark():
    # An up-to-date warehouse plus an explicit --backfill-days 30 still reaches
    # back 30 days: the flag says "cover at least this much", not "cold start
    # only".
    latest_timestamp = NOW_HOUR - timedelta(hours=1)

    fetch_start = resume_point(latest_timestamp, backfill_days=30, extend_to_backfill_days=True)

    assert fetch_start == NOW_HOUR - timedelta(days=30)


def test_find_resume_point_floor_does_not_skip_a_stale_watermark():
    # Newest row is 500 days old. The floor is 365 days back, but resuming
    # there would leave a 135-day hole, so the older resume point is used.
    latest_timestamp = NOW_HOUR - timedelta(days=500)

    fetch_start = resume_point(latest_timestamp, backfill_days=365, extend_to_backfill_days=True)

    assert fetch_start == latest_timestamp - timedelta(hours=OVERLAP_HOURS)


def day_series_resume_point(latest_timestamp, *, extend_to_backfill_days=False):
    cursor = StubCursor([(latest_timestamp,)])
    return find_resume_point(
        cursor,
        "BTC-USD",
        86400,
        30,
        extend_to_backfill_days=extend_to_backfill_days,
        now_utc=NOW,
    )


def test_find_resume_point_puts_a_day_series_on_utc_midnight():
    midnight_30_days_back = (NOW_HOUR - timedelta(days=30)).replace(hour=0)
    latest_timestamp = datetime(2026, 8, 2, tzinfo=UTC)

    assert day_series_resume_point(None) == midnight_30_days_back
    # Three hours before the newest candle is 21:00 the day before it.
    assert day_series_resume_point(latest_timestamp) == latest_timestamp - timedelta(days=1)
    assert (
        day_series_resume_point(latest_timestamp, extend_to_backfill_days=True)
        == midnight_30_days_back
    )


def test_find_resume_point_queries_the_named_series():
    cursor = StubCursor([(None,)])

    find_resume_point(cursor, "SOL-USD", 900, 30, now_utc=NOW)

    (sql, params) = cursor.executed[0]
    assert params == ("SOL-USD", 900)
    assert "granularity_seconds = %s" in sql


def test_find_resume_point_cold_start_is_hour_aligned_at_a_finer_timeframe():
    cursor = StubCursor([(None,)])

    fetch_start = find_resume_point(cursor, "SOL-USD", 300, 7, now_utc=NOW)

    # Every timeframe of a backfill starts on the same hour.
    assert fetch_start == NOW_HOUR - timedelta(days=7)


def test_resolve_timeframes_defaults_to_active_timeframes_finest_first():
    cursor = StubCursor([("5m", 300), ("1h", 3600)])

    assert resolve_timeframes(cursor, None) == [Timeframe("5m", 300), Timeframe("1h", 3600)]
    assert "is_active" in cursor.executed[0][0]
    assert "ORDER BY granularity_seconds" in cursor.executed[0][0]


def test_resolve_timeframes_named_explicitly_overrides_is_active(caplog):
    cursor = StubCursor([("1h", 3600, True), ("1m", 60, False)])

    with caplog.at_level("WARNING"):
        timeframes = resolve_timeframes(cursor, ["1h", "1m"])

    assert timeframes == [Timeframe("1m", 60), Timeframe("1h", 3600)]
    assert "1m" in caplog.text  # the override is never silent


def test_resolve_timeframes_rejects_unknown_codes_and_lists_the_known_ones():
    cursor = StubCursor([("1h", 3600, True)])

    def known_codes_after_the_first_query(sql, params=None):
        cursor.executed.append((sql, params))
        if len(cursor.executed) == 2:
            cursor.rows = [("1m",), ("5m",), ("15m",), ("1h",)]

    cursor.execute = known_codes_after_the_first_query

    with pytest.raises(OperatorError) as caught:
        resolve_timeframes(cursor, ["1h", "4h"])

    assert "4h" in str(caught.value)
    assert "15m" in str(caught.value)


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

    with pytest.raises(OperatorError) as caught:
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

    def record_execute_values(cursor, sql, rows):
        captured.append((" ".join(sql.split()), list(rows)))

    monkeypatch.setattr(candle_ingest.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(candle_ingest.psycopg2.extras, "execute_values", record_execute_values)
    return captured


def serve_candles(monkeypatch, candles):
    """Make fetch_window answer every window with the same candles."""

    def fake_fetch_window(session, product_code, window_start, window_end, granularity_seconds):
        return list(candles)

    monkeypatch.setattr(candle_ingest, "fetch_window", fake_fetch_window)


def flat_candle(bucket_start):
    one = Decimal(1)
    return Candle(bucket_start, low=one, high=one, open=one, close=one, volume=one)


def window_of_300_without(missing_offsets):
    """A full 300-hour window from WINDOW_START, less the hours at the given
    offsets."""
    return [
        flat_candle(WINDOW_START + i * ONE_HOUR) for i in range(300) if i not in missing_offsets
    ]


def fetch_300_hour_window(connection):
    return candle_ingest._fetch_into_raw(
        connection, None, "BTC-USD", 3600, 42, WINDOW_START, WINDOW_START + 300 * ONE_HOUR
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
    serve_candles(monkeypatch, [candle])
    connection = FakeConnection()

    rows_fetched, _ = candle_ingest._fetch_into_raw(
        connection, None, "BTC-USD", 3600, 42, WINDOW_START, WINDOW_START + ONE_HOUR
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
    serve_candles(monkeypatch, window_of_300_without(range(100, 106)))

    with caplog.at_level("WARNING"):
        rows_fetched, short_windows = fetch_300_hour_window(FakeConnection())

    assert short_windows == 1
    assert rows_fetched == 294
    (warning,) = [record for record in caplog.records if record.levelname == "WARNING"]
    assert "missing 6 of 300 buckets" in warning.getMessage()
    assert (WINDOW_START + 100 * ONE_HOUR).isoformat() in warning.getMessage()


def test_fetch_into_raw_does_not_count_a_window_missing_two_isolated_hours_of_300_as_short(
    monkeypatch, raw_writes, caplog
):
    serve_candles(monkeypatch, window_of_300_without({50, 200}))

    with caplog.at_level("WARNING"):
        rows_fetched, short_windows = fetch_300_hour_window(FakeConnection())

    assert short_windows == 0
    assert rows_fetched == 298
    assert [record for record in caplog.records if record.levelname == "WARNING"] == []


def test_fetch_into_raw_flags_a_single_missing_day_at_one_day(monkeypatch, raw_writes):
    # One day of 30 is under the three the 10 percent rule needs, so only the
    # gap line can flag this window, and one candle is already past three hours.
    one_day = timedelta(days=1)
    candles = [flat_candle(WINDOW_START + index * one_day) for index in range(30) if index != 10]
    serve_candles(monkeypatch, candles)

    _, short_windows = candle_ingest._fetch_into_raw(
        FakeConnection(), None, "BTC-USD", 86400, 42, WINDOW_START, WINDOW_START + 30 * one_day
    )

    assert short_windows == 1


@pytest.mark.parametrize(("missing_in_a_row", "expected_short"), [(11, 0), (12, 1)])
def test_fetch_into_raw_measures_the_gap_line_in_hours_at_fifteen_minutes(
    monkeypatch, raw_writes, missing_in_a_row, expected_short
):
    # Three hours is twelve 15-minute buckets, well under the 30 that the
    # 10 percent rule would need in a 300-bucket window, so only the gap line
    # can flag this window.
    fifteen_minutes = timedelta(minutes=15)
    candles = [
        flat_candle(WINDOW_START + index * fifteen_minutes)
        for index in range(300)
        if not 100 <= index < 100 + missing_in_a_row
    ]
    serve_candles(monkeypatch, candles)

    window_end = WINDOW_START + 300 * fifteen_minutes
    _, short_windows = candle_ingest._fetch_into_raw(
        FakeConnection(), None, "BTC-USD", 900, 42, WINDOW_START, window_end
    )

    assert short_windows == expected_short


def test_fetch_into_raw_logs_empty_windows_before_the_first_candle_once(
    monkeypatch, raw_writes, caplog
):
    # A deep backfill of a product listed later: the first two windows come
    # back empty, the third holds the first candles.
    answers = [[], [], [flat_candle(WINDOW_START + 600 * ONE_HOUR + index * ONE_HOUR)
                        for index in range(300)]]

    def fake_fetch_window(session, product_code, window_start, window_end, granularity_seconds):
        return answers.pop(0)

    monkeypatch.setattr(candle_ingest, "fetch_window", fake_fetch_window)

    with caplog.at_level("INFO"):
        rows_fetched, short_windows = candle_ingest._fetch_into_raw(
            FakeConnection(), None, "SOL-USD", 3600, 42, WINDOW_START, WINDOW_START + 900 * ONE_HOUR
        )

    assert rows_fetched == 300
    assert short_windows == 0
    assert [record for record in caplog.records if record.levelname == "WARNING"] == []
    assert "(2 empty window(s)); history starts there" in caplog.text


def test_fetch_into_raw_flags_a_run_that_finds_no_candle_at_all(monkeypatch, raw_writes, caplog):
    serve_candles(monkeypatch, [])

    with caplog.at_level("WARNING"):
        rows_fetched, short_windows = fetch_300_hour_window(FakeConnection())

    assert rows_fetched == 0
    assert short_windows == 1
    assert "came back empty" in caplog.text


def test_fetch_into_raw_drops_candles_outside_the_requested_window(monkeypatch, raw_writes, caplog):
    current_hour = WINDOW_START + 3 * ONE_HOUR
    inside = [flat_candle(WINDOW_START + i * ONE_HOUR) for i in range(3)]
    before_the_window = flat_candle(WINDOW_START - ONE_HOUR)
    forming_hour = flat_candle(current_hour)
    serve_candles(monkeypatch, [before_the_window, *inside, forming_hour])

    with caplog.at_level("WARNING"):
        rows_fetched, short_windows = candle_ingest._fetch_into_raw(
            FakeConnection(), None, "BTC-USD", 3600, 42, WINDOW_START, current_hour
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
    def failing_fetch_window(session, product_code, window_start, window_end, granularity_seconds):
        raise error

    monkeypatch.setattr(candle_ingest, "fetch_window", failing_fetch_window)
    # The product has no rows yet, so the one-day cold start gives the fetch a
    # window to fail on. 17 is the id of the run row the load inserts.
    connection = FakeConnection(rows=[(None,), (17,)])

    with pytest.raises(type(error)) as caught:
        candle_ingest.load_product(connection, None, "BTC-USD", HOURLY, backfill_days=1)

    assert caught.value is error
    assert connection.rollbacks == 1
    ((sql, params),) = [entry for entry in connection.executed if "status = 'failed'" in entry[0]]
    assert sql.startswith("UPDATE raw.ingest_runs SET status = 'failed'")
    assert params == (17,)
    # Rolled back first, then the failed status written and committed.
    assert connection.log.index("rollback") < connection.log.index(sql)
    assert connection.log[connection.log.index(sql) + 1] == "commit"


def test_run_ingest_keeps_going_after_a_failed_series_and_rolls_back_before_the_next(
    monkeypatch,
):
    connection = FakeConnection()
    # Each attempt, with the rollback count the connection showed as it began.
    attempts: list[tuple[str, str, int]] = []

    def fake_load_product(
        connection,
        session,
        product_code,
        timeframe,
        backfill_days,
        *,
        extend_to_backfill_days=False,
    ):
        attempts.append((product_code, timeframe.timeframe_code, connection.rollbacks))
        if (product_code, timeframe.timeframe_code) == ("AAA-USD", "15m"):
            raise RuntimeError("one series fails")
        return LoadResult(
            product_code,
            timeframe.timeframe_code,
            run_id=5,
            rows_fetched=10,
            rows_upserted=10,
            short_windows=0,
        )

    monkeypatch.setattr(candle_ingest.database_connection, "connect", lambda: connection)
    monkeypatch.setattr(
        candle_ingest, "resolve_products", lambda cursor, requested: ["AAA-USD", "BBB-USD"]
    )
    monkeypatch.setattr(
        candle_ingest,
        "resolve_timeframes",
        lambda cursor, requested: [Timeframe("15m", 900), Timeframe("1h", 3600)],
    )
    monkeypatch.setattr(candle_ingest, "load_product", fake_load_product)

    exit_code = candle_ingest.run_ingest()

    assert exit_code == 1
    assert attempts == [
        ("AAA-USD", "15m", 0),
        ("AAA-USD", "1h", 1),
        ("BBB-USD", "15m", 1),
        ("BBB-USD", "1h", 1),
    ]
    assert connection.closed


# --------------------------------------------------------------------------
# promotion to core, and the rows it leaves in raw
# --------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]


def split_sql_conditions(sql_text: str) -> set[str]:
    """The individual comparisons in a block of AND-ed conditions, with the
    raw alias and repeated spaces removed so two spellings compare equal."""
    flat_text = " ".join(sql_text.replace("r.", "").split())
    return {part.strip() for part in re.split(r"\bAND\b", flat_text) if part.strip()}


def core_check_constraints() -> set[str]:
    """Every CHECK comparison the DDL declares on core.ohlcv."""
    schema_sql = (REPO_ROOT / "sql" / "01_schema.sql").read_text(encoding="utf-8")
    table = schema_sql[schema_sql.index("CREATE TABLE core.ohlcv (") :]
    table = table[: table.index(");")]
    found: set[str] = set()
    for body in re.findall(r"CHECK \(([^()]*)\)", table):
        found |= split_sql_conditions(body)
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
    assert split_sql_conditions(candle_ingest.CORE_ROW_CHECKS) == core_check_constraints()


def test_the_upsert_shown_in_the_query_file_carries_the_same_row_checks():
    queries = (REPO_ROOT / "sql" / "04_queries.sql").read_text(encoding="utf-8")
    query_20 = queries[queries.index("-- Q20 ") :]
    statement = "\n".join(line for line in query_20.splitlines() if not line.startswith("--"))
    where_clause = statement[statement.index("status = 'succeeded')") : statement.index("ORDER BY")]
    shown = split_sql_conditions(where_clause.split("AND", 1)[1])
    assert shown == split_sql_conditions(candle_ingest.CORE_ROW_CHECKS)


def test_both_promotion_statements_filter_on_the_row_checks():
    checks = " ".join(candle_ingest.CORE_ROW_CHECKS.split())
    assert f"AND ( {checks} )" in " ".join(candle_ingest.UPSERT_CORE_SQL.split())
    assert f"AND NOT ( {checks} )" in " ".join(candle_ingest.REJECTED_ROWS_SQL.split())


def test_promote_to_core_warns_about_rows_core_would_reject_and_still_promotes(caplog):
    connection = FakeConnection()
    connection.batches = [[(WINDOW_START,), (WINDOW_START + ONE_HOUR,)]]

    with caplog.at_level("WARNING"):
        candle_ingest._promote_to_core(connection, run_id=7, rows_fetched=5)

    statements = [statement for statement, _ in connection.executed]
    assert statements[0].startswith("SELECT r.bucket_start FROM raw.candles r")
    assert statements[1].startswith("INSERT INTO core.ohlcv ")
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


# --------------------------------------------------------------------------
# The request log in raw.fetch_windows, and re-checking gaps against it.
# --------------------------------------------------------------------------
def fetch_window_records(connection):
    """The parameters of every raw.fetch_windows insert the connection saw."""
    return [
        params
        for sql, params in connection.executed
        if sql.startswith("INSERT INTO raw.fetch_windows")
    ]


def test_every_window_is_recorded_with_its_count_including_an_empty_one(monkeypatch, raw_writes):
    answers = [[], [flat_candle(WINDOW_START + 300 * ONE_HOUR)]]

    def fake_fetch_window(session, product_code, window_start, window_end, granularity_seconds):
        return answers.pop(0)

    monkeypatch.setattr(candle_ingest, "fetch_window", fake_fetch_window)
    connection = FakeConnection()

    candle_ingest._fetch_into_raw(
        connection, None, "BTC-USD", 3600, 42, WINDOW_START, WINDOW_START + 301 * ONE_HOUR
    )

    assert fetch_window_records(connection) == [
        (42, WINDOW_START, WINDOW_START + 299 * ONE_HOUR, 0),
        (42, WINDOW_START + 300 * ONE_HOUR, WINDOW_START + 300 * ONE_HOUR, 1),
    ]
    # One commit per window, the empty one included, so the record of an
    # empty answer is never lost to a later failure.
    assert connection.commits == 2


def test_the_first_window_with_candles_is_judged_from_its_first_candle(
    monkeypatch, raw_writes, caplog
):
    # A listing at hour 120 of the first window: the 120 hours before it are
    # not holes in a series that had not started.
    serve_candles(
        monkeypatch, [flat_candle(WINDOW_START + offset * ONE_HOUR) for offset in range(120, 300)]
    )

    with caplog.at_level("WARNING"):
        rows_fetched, short_windows = fetch_300_hour_window(FakeConnection())

    assert rows_fetched == 180
    assert short_windows == 0


def test_recheck_gaps_requests_only_the_unrecorded_gaps_and_records_the_windows(
    monkeypatch, raw_writes, caplog
):
    gaps = [
        WINDOW_START + 10 * ONE_HOUR,
        WINDOW_START + 11 * ONE_HOUR,
        WINDOW_START + 900 * ONE_HOUR,
    ]
    requested: list[tuple] = []

    def fake_fetch_window(session, product_code, window_start, window_end, granularity_seconds):
        requested.append((window_start, window_end))
        # The exchange now has one of the three.
        return [flat_candle(gap) for gap in gaps[:1] if window_start <= gap <= window_end]

    monkeypatch.setattr(candle_ingest, "fetch_window", fake_fetch_window)
    # Rows: the run id the insert returns. Batches: the gap query's answer,
    # then the rejected-rows query inside the promotion.
    connection = FakeConnection(rows=[(77,)])
    connection.batches = [[(gap,) for gap in gaps], []]

    with caplog.at_level("INFO"):
        load_result = candle_ingest.recheck_gaps(connection, None, "SOL-USD", HOURLY)

    assert requested == [(gaps[0], gaps[1]), (gaps[2], gaps[2])]
    assert fetch_window_records(connection) == [
        (77, gaps[0], gaps[1], 1),
        (77, gaps[2], gaps[2], 0),
    ]
    assert load_result is not None
    assert (load_result.run_id, load_result.rows_fetched) == (77, 1)
    assert "re-requested 3 gap bucket(s) in 2 window(s); the exchange returned 1" in caplog.text


def test_recheck_gaps_does_nothing_when_every_gap_is_on_record(monkeypatch):
    def unexpected_fetch(*args):
        raise AssertionError("no request should be made")

    monkeypatch.setattr(candle_ingest, "fetch_window", unexpected_fetch)
    connection = FakeConnection()

    assert candle_ingest.recheck_gaps(connection, None, "SOL-USD", HOURLY) is None
    assert not any(sql.startswith("INSERT INTO raw.ingest_runs") for sql, _ in connection.executed)
