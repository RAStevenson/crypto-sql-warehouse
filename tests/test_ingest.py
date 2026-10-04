"""Watermark arithmetic and work-list resolution against a stub cursor, and
the load path against a fake connection. No database and no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from crypto_warehouse import ingest
from crypto_warehouse.api import Candle
from crypto_warehouse.config import ConfigError
from crypto_warehouse.ingest import OVERLAP_HOURS, LoadResult, get_watermark, resolve_products
from crypto_warehouse.windows import HOUR

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


def watermark(max_ts, backfill_days=365, *, apply_floor=False):
    cur = StubCursor([(max_ts,)])
    return get_watermark(cur, "BTC-USD", backfill_days, apply_floor=apply_floor, now_utc=NOW)


def test_get_watermark_cold_start_uses_backfill_days():
    assert watermark(None, backfill_days=90) == NOW_HOUR - timedelta(days=90)


def test_get_watermark_cold_start_is_hour_aligned():
    result = watermark(None, backfill_days=7)

    assert (result.minute, result.second, result.microsecond) == (0, 0, 0)
    assert result.tzinfo is UTC


def test_get_watermark_populated_subtracts_the_overlap():
    max_ts = datetime(2026, 8, 3, 11, tzinfo=UTC)

    assert watermark(max_ts) == max_ts - timedelta(hours=OVERLAP_HOURS)


def test_get_watermark_returns_the_resume_point_in_utc_when_max_ts_carries_an_offset():
    # psycopg2 hands a timestamp back in the session's time zone. This one is
    # 11:00 UTC written with a +05:30 offset.
    max_ts = datetime(2026, 8, 3, 16, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    result = watermark(max_ts)

    assert result == datetime(2026, 8, 3, 11, tzinfo=UTC) - timedelta(hours=OVERLAP_HOURS)
    assert result.tzinfo is UTC


@pytest.mark.parametrize("backfill_days", [1, 90, 365, 3650])
def test_get_watermark_ignores_backfill_days_on_an_ordinary_run(backfill_days):
    """With no --backfill-days on the command line there is no floor, so a
    routine incremental run re-fetches the overlap and nothing more. If the
    floor were always applied, the 365-day default would send every run back a
    full year."""
    max_ts = NOW_HOUR - timedelta(hours=1)

    assert watermark(max_ts, backfill_days=backfill_days) == max_ts - timedelta(
        hours=OVERLAP_HOURS
    )


def test_get_watermark_honors_the_backfill_floor():
    """Regression: --backfill-days used to be consulted only when a product had
    no rows at all, so --backfill-days 365 against a 90-day warehouse fetched
    nothing extra and exited 0."""
    max_ts = NOW_HOUR - timedelta(days=90)

    result = watermark(max_ts, backfill_days=365, apply_floor=True)

    assert result == NOW_HOUR - timedelta(days=365)


def test_get_watermark_floor_reaches_back_from_a_current_watermark():
    # An up-to-date warehouse plus an explicit --backfill-days 30 still reaches
    # back 30 days: the flag says "cover at least this much", not "cold start
    # only".
    max_ts = NOW_HOUR - timedelta(hours=1)

    result = watermark(max_ts, backfill_days=30, apply_floor=True)

    assert result == NOW_HOUR - timedelta(days=30)


def test_get_watermark_floor_does_not_skip_a_stale_watermark():
    # Newest row is 500 days old. The floor is 365 days back, but resuming
    # there would leave a 135-day hole, so the older resume point is used.
    max_ts = NOW_HOUR - timedelta(days=500)

    result = watermark(max_ts, backfill_days=365, apply_floor=True)

    assert result == max_ts - timedelta(hours=OVERLAP_HOURS)


def test_get_watermark_queries_the_named_product():
    cur = StubCursor([(None,)])

    get_watermark(cur, "SOL-USD", 30, now_utc=NOW)

    (_, params) = cur.executed[0]
    assert params == ("SOL-USD",)


def test_resolve_products_defaults_to_active_assets():
    cur = StubCursor([("ADA-USD",), ("BTC-USD",)])

    assert resolve_products(cur, None) == ["ADA-USD", "BTC-USD"]
    assert "is_active" in cur.executed[0][0]


def test_resolve_products_named_explicitly_overrides_is_active(caplog):
    cur = StubCursor([("BTC-USD", True), ("DOGE-USD", False)])

    with caplog.at_level("WARNING"):
        products = resolve_products(cur, ["BTC-USD", "DOGE-USD"])

    assert products == ["BTC-USD", "DOGE-USD"]
    assert "DOGE-USD" in caplog.text  # the override is never silent


def test_resolve_products_rejects_unknown_products():
    cur = StubCursor([("BTC-USD", True)])

    with pytest.raises(ConfigError) as caught:
        resolve_products(cur, ["BTC-USD", "XYZ-USD"])

    assert "XYZ-USD" in str(caught.value)


# --------------------------------------------------------------------------
# The load path. fetch_window, the pause between windows, and execute_values
# are replaced, and the connection is a fake that records what it was asked.
# --------------------------------------------------------------------------
WINDOW_START = datetime(2026, 7, 1, 0, tzinfo=UTC)


class FakeCursor:
    """Reports every execute to its connection and hands back the connection's
    canned rows."""

    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        statement = " ".join(sql.split())
        self.conn.executed.append((statement, params))
        self.conn.log.append(statement)

    def fetchone(self):
        return self.conn.rows.pop(0)


class FakeConnection:
    """Enough of a psycopg2 connection for the loader: it counts commits and
    rollbacks, records every statement its cursors execute, and keeps one
    ordered log of all three so a test can check what came first."""

    def __init__(self, rows=()):
        self.rows = list(rows)
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

    def record(cur, sql, rows):
        captured.append((" ".join(sql.split()), list(rows)))

    monkeypatch.setattr(ingest.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(ingest.psycopg2.extras, "execute_values", record)
    return captured


def serve(monkeypatch, candles):
    """Make fetch_window answer every window with the same candles."""

    def fake_fetch_window(session, product_code, window_start, window_end):
        return list(candles)

    monkeypatch.setattr(ingest, "fetch_window", fake_fetch_window)


def flat_candle(bucket_start):
    one = Decimal(1)
    return Candle(bucket_start, low=one, high=one, open=one, close=one, volume=one)


def window_of_300_without(missing_offsets):
    """A full 300-hour window from WINDOW_START, less the hours at the given
    offsets."""
    return [flat_candle(WINDOW_START + i * HOUR) for i in range(300) if i not in missing_offsets]


def fetch_300_hour_window(conn):
    return ingest._fetch_into_raw(
        conn, None, "BTC-USD", 42, WINDOW_START, WINDOW_START + 300 * HOUR
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
    conn = FakeConnection()

    rows_fetched, _ = ingest._fetch_into_raw(
        conn, None, "BTC-USD", 42, WINDOW_START, WINDOW_START + HOUR
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
    assert conn.commits == 1


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
        rows_fetched, short_windows = ingest._fetch_into_raw(
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

    monkeypatch.setattr(ingest, "fetch_window", failing_fetch_window)
    # The product has no rows yet, so the one-day cold start gives the fetch a
    # window to fail on. 17 is the id of the run row the load inserts.
    conn = FakeConnection(rows=[(None,), (17,)])

    with pytest.raises(type(error)) as caught:
        ingest.load_product(conn, None, "BTC-USD", backfill_days=1)

    assert caught.value is error
    assert conn.rollbacks == 1
    ((sql, params),) = [entry for entry in conn.executed if "status = 'failed'" in entry[0]]
    assert sql.startswith("UPDATE raw.ingest_runs SET status = 'failed'")
    assert params == (17,)
    # Rolled back first, then the failed status written and committed.
    assert conn.log.index("rollback") < conn.log.index(sql)
    assert conn.log[conn.log.index(sql) + 1] == "commit"


def test_run_ingest_keeps_going_after_a_failed_product_and_rolls_back_before_the_next(
    monkeypatch,
):
    conn = FakeConnection()
    # Each attempt, with the rollback count the connection showed as it began.
    attempts: list[tuple[str, int]] = []

    def fake_load_product(conn, session, product_code, backfill_days, *, apply_floor=False):
        attempts.append((product_code, conn.rollbacks))
        if product_code == "AAA-USD":
            raise RuntimeError("the first product fails")
        return LoadResult(
            product_code, run_id=5, rows_fetched=10, rows_upserted=10, short_windows=0
        )

    monkeypatch.setattr(ingest.config, "connect", lambda: conn)
    monkeypatch.setattr(ingest, "resolve_products", lambda cur, requested: ["AAA-USD", "BBB-USD"])
    monkeypatch.setattr(ingest, "load_product", fake_load_product)

    exit_code = ingest.run_ingest()

    assert exit_code == 1
    assert attempts == [("AAA-USD", 0), ("BBB-USD", 1)]
    assert conn.closed
