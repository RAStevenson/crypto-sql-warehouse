"""The CSV export's date handling, file naming, product resolution, and query
text, against a stub cursor. No database."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from crypto_warehouse import csv_export
from crypto_warehouse.csv_export import (
    DAILY_QUERY,
    PRICES_QUERY,
    check_timeframe,
    day_bounds,
    default_output_path,
    resolve_export_products,
)
from crypto_warehouse.database_connection import OperatorError


class StubCursor:
    """Records each query and answers it with the next canned batch of rows."""

    def __init__(self, *batches):
        self.batches = list(batches)
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.batches.pop(0)


def test_day_bounds_include_the_whole_last_day():
    start_instant, end_instant = day_bounds(date(2024, 1, 1), date(2024, 6, 30))

    assert start_instant == datetime(2024, 1, 1, tzinfo=UTC)
    # Half-open: the query keeps ts_utc < end, so 23:55 on June 30 is in and
    # midnight July 1 is out.
    assert end_instant == datetime(2024, 7, 1, tzinfo=UTC)


def test_day_bounds_leave_an_open_end_open():
    assert day_bounds(None, None) == (None, None)
    assert day_bounds(date(2024, 1, 1), None) == (datetime(2024, 1, 1, tzinfo=UTC), None)


def test_default_output_path_names_one_product_and_its_timeframe():
    path = default_output_path("prices", ["BTC-USD"], "15m", date(2024, 1, 1), date(2024, 6, 30))

    assert path == csv_export.EXPORT_DIR / "prices_BTC-USD_15m_2024-01-01_to_2024-06-30.csv"


def test_default_output_path_counts_several_products_and_names_open_ends():
    path = default_output_path("daily", ["BTC-USD", "ETH-USD", "SOL-USD"], None, None, None)

    assert path.name == "daily_3-assets_first_to_last.csv"


def test_resolve_export_products_defaults_to_active_assets():
    cursor = StubCursor([("BTC-USD",), ("ETH-USD",)])

    assert resolve_export_products(cursor, None) == ["BTC-USD", "ETH-USD"]
    assert "is_active" in cursor.executed[0][0]


def test_resolve_export_products_rejects_an_unknown_code():
    cursor = StubCursor([("BTC-USD",)])

    with pytest.raises(OperatorError, match="XYZ-USD"):
        resolve_export_products(cursor, ["BTC-USD", "XYZ-USD"])


def test_check_timeframe_rejects_an_unknown_code_and_lists_the_known_ones():
    cursor = StubCursor([("1m",), ("5m",), ("15m",), ("1h",)])

    with pytest.raises(OperatorError) as caught:
        check_timeframe(cursor, "4h")

    assert "4h" in str(caught.value)
    assert "15m" in str(caught.value)


def test_check_timeframe_accepts_a_known_code():
    check_timeframe(StubCursor([("1m",), ("1h",)]), "1h")


def test_the_price_export_writes_times_in_utc_whatever_the_session_timezone():
    assert "o.ts_utc AT TIME ZONE 'UTC' AS time_utc" in PRICES_QUERY


def test_both_exports_filter_on_the_resolved_products_and_order_their_rows():
    for query in (PRICES_QUERY, DAILY_QUERY):
        assert "ANY(%(product_codes)s)" in query
        assert "ORDER BY" in query
