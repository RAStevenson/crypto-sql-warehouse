"""Watermark arithmetic and work-list resolution, against a stub cursor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from crypto_warehouse.config import ConfigError
from crypto_warehouse.ingest import OVERLAP_HOURS, get_watermark, resolve_products

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


@pytest.mark.parametrize("backfill_days", [1, 90, 365, 3650])
def test_get_watermark_ignores_backfill_days_on_an_ordinary_run(backfill_days):
    """With no --backfill-days on the command line there is no floor, so a
    routine incremental run re-fetches the overlap and nothing more. If the
    floor were always applied, the 365-day default would drag every run back a
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
    # there would leave a 135-day hole, so the older resume point wins.
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
