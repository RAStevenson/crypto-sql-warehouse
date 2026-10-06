"""Tests of asset registration, covering product code parsing, the existence
probe, and the insert's handling of a unique collision. They use no database
and no network, like the rest of the suite. The probe runs against a scripted
fake session and the insert against a fake cursor."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import requests
from psycopg2 import errors as pg_errors

from crypto_warehouse.asset_registration import (
    PROBE_HOURS,
    insert_asset,
    parse_product_code,
    probe_product,
)
from crypto_warehouse.database_connection import ConfigError
from crypto_warehouse.fetch_windows import floor_hour

# One real candle row in wire order, enough for the probe to count.
CANDLE_PAYLOAD = "[[1782871200, 58566.2, 59077.93, 58668.85, 58991.99, 350.09989392]]"


class FakeResponse:
    def __init__(self, status_code=200, text="[]", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self, **kwargs):
        return json.loads(self.text, **kwargs)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession(requests.Session):
    """Answers every get with one scripted response. It subclasses
    requests.Session only so the type checker accepts it where a Session is
    declared; get never reaches the network."""

    def __init__(self, response):
        super().__init__()
        self.response = response
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self.response


# --------------------------------------------------------------------------
# parse_product_code
# --------------------------------------------------------------------------
def test_parse_splits_and_uppercases():
    assert parse_product_code("xrp-usd") == ("XRP", "USD")


def test_parse_strips_whitespace():
    assert parse_product_code(" XRP-USD ") == ("XRP", "USD")


@pytest.mark.parametrize(
    "bad",
    ["XRPUSD", "XRP-USD-PERP", "-USD", "XRP-", "XRP/USD", "", "XR P-USD"],
)
def test_parse_rejects_malformed_codes(bad):
    with pytest.raises(ConfigError):
        parse_product_code(bad)


# --------------------------------------------------------------------------
# probe_product
# --------------------------------------------------------------------------
def test_probe_counts_candles_and_names_the_product_in_the_url():
    session = FakeSession(FakeResponse(text=CANDLE_PAYLOAD))
    assert probe_product(session, "XRP-USD") == 1
    (call,) = session.calls
    assert "XRP-USD" in call["url"]


def test_probe_asks_for_exactly_the_trailing_probe_hours_ending_on_the_current_hour():
    session = FakeSession(FakeResponse(text=CANDLE_PAYLOAD))
    hour_before = floor_hour(datetime.now(UTC))

    probe_product(session, "XRP-USD")

    hour_after = floor_hour(datetime.now(UTC))
    (call,) = session.calls
    start = datetime.fromisoformat(call["params"]["start"])
    end = datetime.fromisoformat(call["params"]["end"])
    assert end - start == timedelta(hours=PROBE_HOURS)
    assert PROBE_HOURS > 0
    assert (end.minute, end.second, end.microsecond) == (0, 0, 0)
    # Trailing means the window ends at the current hour. The two readings
    # bracket the call, so the check holds even across an hour rollover.
    assert hour_before <= end <= hour_after


def test_probe_rejects_a_200_with_no_candles():
    session = FakeSession(FakeResponse(text="[]"))
    with pytest.raises(ConfigError, match=f"last {PROBE_HOURS} hours"):
        probe_product(session, "XRP-USD")


def test_probe_lets_an_unknown_product_404_surface():
    session = FakeSession(FakeResponse(status_code=404, text='{"message": "NotFound"}'))
    with pytest.raises(requests.HTTPError):
        probe_product(session, "NOPE-USD")


# --------------------------------------------------------------------------
# insert_asset
# --------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, error=None, asset_id=7):
        self.error = error
        self.asset_id = asset_id
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql, params=None):
        if self.error is not None:
            raise self.error
        self.executed.append((sql, params))

    def fetchone(self):
        return (self.asset_id,)


def test_insert_passes_the_row_and_returns_the_id():
    cursor = FakeCursor(asset_id=9)
    asset_id = insert_asset(
        cursor,
        symbol="XRP",
        product_code="XRP-USD",
        asset_name="Ripple",
        quote_currency="USD",
    )
    assert asset_id == 9
    ((sql, params),) = cursor.executed
    assert "INSERT INTO core.assets" in sql
    assert params == ("XRP", "XRP-USD", "Ripple", "USD")


def test_insert_translates_a_unique_collision_into_plain_instructions():
    cursor = FakeCursor(error=pg_errors.UniqueViolation("duplicate key"))
    with pytest.raises(ConfigError, match="is_active = true"):
        insert_asset(
            cursor,
            symbol="BTC",
            product_code="BTC-USD",
            asset_name="Bitcoin",
            quote_currency="USD",
        )
