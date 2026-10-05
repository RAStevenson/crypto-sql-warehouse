"""Payload mapping and retry behavior, with no network."""

from __future__ import annotations

import decimal
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import requests

from crypto_warehouse import coinbase_candles
from crypto_warehouse.coinbase_candles import (
    Candle,
    CandlePayloadError,
    fetch_window,
    parse_candles,
)

# One window of BTC-USD as the endpoint actually returned it, kept verbatim as
# a JSON string so the test exercises the same decode path the client uses.
LIVE_PAYLOAD_TEXT = "[[1782871200, 58566.2, 59077.93, 58668.85, 58991.99, 350.09989392]]"


def decode(text: str):
    """The client's decode: parse_float=Decimal, never a binary double."""
    return json.loads(text, parse_float=decimal.Decimal)


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
    """Replays a scripted list of outcomes. An Exception instance in the list
    is raised instead of returned, which is how transport failures are
    simulated. It subclasses requests.Session only so the type checker accepts
    it where a Session is declared; get never reaches the network."""

    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        outcome = self.outcomes.pop(0) if self.outcomes else FakeResponse()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture()
def no_sleep(monkeypatch):
    """Record backoff sleeps instead of taking them, and pin the jitter to 0
    so the sequence is exact."""
    slept: list[float] = []
    monkeypatch.setattr(coinbase_candles.time, "sleep", slept.append)
    monkeypatch.setattr(coinbase_candles.random, "uniform", lambda low, high: 0.0)
    return slept


def window():
    start = datetime(2026, 7, 1, 0, tzinfo=UTC)
    return start, datetime(2026, 7, 1, 5, tzinfo=UTC)


# --------------------------------------------------------------------------
# The mapping. This is the test that matters most in the repo: the wire order
# is [time, low, high, open, close, volume] and the DDL declares
# open/high/low/close, so a silent transposition here would put the high in
# the open column of every row in the warehouse and nothing downstream would
# complain.
# --------------------------------------------------------------------------
def test_parse_candles_column_mapping():
    (candle,) = parse_candles(decode(LIVE_PAYLOAD_TEXT), "BTC-USD")

    assert candle.bucket_start == datetime(2026, 7, 1, 2, 0, tzinfo=UTC)
    assert candle.low == Decimal("58566.2")
    assert candle.high == Decimal("59077.93")
    assert candle.open == Decimal("58668.85")
    assert candle.close == Decimal("58991.99")
    assert candle.volume == Decimal("350.09989392")


def test_parse_candles_returns_decimals_not_floats():
    (candle,) = parse_candles(decode(LIVE_PAYLOAD_TEXT), "BTC-USD")

    for field in ("low", "high", "open", "close", "volume"):
        value = getattr(candle, field)
        assert isinstance(value, Decimal), f"{field} is {type(value).__name__}"
        assert not isinstance(value, float)


def test_parse_candles_keeps_full_numeric_precision():
    # A price with more significant digits than a binary double can hold. Via
    # float this arrives as 10000000000.0, which is what made the schema's
    # "numeric, not double precision" comment untrue end to end.
    text = "[[1782871200, 1.0, 9999999999.99999999, 1.0, 1.0, 0.00000001]]"

    (candle,) = parse_candles(decode(text), "BTC-USD")

    assert candle.high == Decimal("9999999999.99999999")
    assert str(candle.high) == "9999999999.99999999"
    assert candle.volume == Decimal("0.00000001")
    assert float(candle.high) != candle.high


def test_parse_candles_is_a_named_tuple_in_wire_order():
    (candle,) = parse_candles(decode(LIVE_PAYLOAD_TEXT), "BTC-USD")

    assert isinstance(candle, Candle)
    assert candle[1] is candle.low and candle[3] is candle.open


def test_parse_candles_accepts_integer_prices():
    (candle,) = parse_candles(decode("[[1782871200, 1, 2, 1, 2, 0]]"), "ADA-USD")

    assert candle.high == Decimal(2)
    assert isinstance(candle.high, Decimal)


# --------------------------------------------------------------------------
# Error bodies
# --------------------------------------------------------------------------
def test_parse_candles_rejects_error_body():
    with pytest.raises(CandlePayloadError) as caught:
        parse_candles({"message": "NotFound"}, "NOPE-USD")

    assert "NOPE-USD" in str(caught.value)
    assert "NotFound" in str(caught.value)
    assert not isinstance(caught.value, TypeError)


def test_parse_candles_rejects_empty_dict_but_accepts_empty_list():
    # {} is an error body with no message; [] is a window in which nothing
    # traded, which is legitimate and must not raise.
    with pytest.raises(CandlePayloadError):
        parse_candles({}, "BTC-USD")

    assert parse_candles([], "BTC-USD") == []


@pytest.mark.parametrize(
    "payload",
    [
        "a string",
        123,
        None,
        [[1782871200, 1.0, 2.0]],  # short row
        [{"time": 1782871200}],  # row is not a sequence
        [[1782871200, "not-a-number", 2.0, 1.5, 1.5, 1.0]],
    ],
)
def test_parse_candles_rejects_malformed_payloads(payload):
    with pytest.raises(CandlePayloadError) as caught:
        parse_candles(payload, "BTC-USD")

    assert "BTC-USD" in str(caught.value)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize(
    "position", [1, 2, 3, 4, 5], ids=["low", "high", "open", "close", "volume"]
)
def test_parse_candles_rejects_nan_or_infinity_in_any_price_or_volume_field(position, literal):
    # JSON text may carry these literals, the decoder accepts them, and a NaN
    # would pass every CHECK on the candle tables.
    fields = ["1782871200", "1.0", "2.0", "1.5", "1.5", "1.0"]
    fields[position] = literal
    payload = decode(f"[[{', '.join(fields)}]]")

    with pytest.raises(CandlePayloadError) as caught:
        parse_candles(payload, "BTC-USD")

    assert "BTC-USD" in str(caught.value)


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------
def test_fetch_window_sends_the_window_start_and_end_and_hourly_granularity_as_params(no_sleep):
    session = FakeSession([FakeResponse(200, LIVE_PAYLOAD_TEXT)])
    window_start, window_end = window()
    assert window_start != window_end  # a swap of the two has to be visible

    fetch_window(session, "BTC-USD", window_start, window_end)

    (call,) = session.calls
    assert "/products/BTC-USD/candles" in call["url"]
    assert call["params"] == {
        "granularity": 3600,
        "start": window_start.isoformat(),
        "end": window_end.isoformat(),
    }


# --------------------------------------------------------------------------
# Retries
# --------------------------------------------------------------------------
def test_fetch_window_retries_429_then_succeeds(no_sleep):
    session = FakeSession(
        [
            FakeResponse(429),
            FakeResponse(429),
            FakeResponse(200, LIVE_PAYLOAD_TEXT),
        ]
    )

    candles = fetch_window(session, "BTC-USD", *window())

    assert len(candles) == 1
    assert len(session.calls) == 3
    assert no_sleep == [1.0, 2.0]


def test_fetch_window_honors_retry_after_on_429(no_sleep):
    session = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": "7"}),
            FakeResponse(200, LIVE_PAYLOAD_TEXT),
        ]
    )

    fetch_window(session, "BTC-USD", *window())

    assert no_sleep == [7.0]


def test_fetch_window_ignores_unparseable_retry_after(no_sleep):
    session = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            FakeResponse(200, LIVE_PAYLOAD_TEXT),
        ]
    )

    fetch_window(session, "BTC-USD", *window())

    assert no_sleep == [1.0]  # fell back to the exponential schedule


@pytest.mark.parametrize("header", ["0", "nan"])
def test_fetch_window_falls_back_to_its_own_backoff_on_a_zero_or_nan_retry_after(no_sleep, header):
    session = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": header}),
            FakeResponse(200, LIVE_PAYLOAD_TEXT),
        ]
    )

    fetch_window(session, "BTC-USD", *window())

    assert no_sleep == [1.0]  # the first backoff step, not 0 and not nan


def test_fetch_window_caps_a_retry_after_larger_than_the_maximum(no_sleep):
    session = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": "86400"}),
            FakeResponse(200, LIVE_PAYLOAD_TEXT),
        ]
    )

    fetch_window(session, "BTC-USD", *window())

    assert no_sleep == [coinbase_candles.MAX_RETRY_AFTER_SECONDS]
    assert coinbase_candles.MAX_RETRY_AFTER_SECONDS < 86400


def test_fetch_window_gives_up(no_sleep):
    session = FakeSession([FakeResponse(429) for _ in range(coinbase_candles.MAX_ATTEMPTS)])

    with pytest.raises(RuntimeError) as caught:
        fetch_window(session, "BTC-USD", *window())

    assert "BTC-USD" in str(caught.value)
    assert len(session.calls) == coinbase_candles.MAX_ATTEMPTS
    # One sleep fewer than attempts: backing off after the last attempt only
    # makes the failure slower.
    assert no_sleep == [1.0, 2.0, 4.0, 8.0]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_fetch_window_retries_5xx(no_sleep, status):
    session = FakeSession([FakeResponse(status), FakeResponse(200, LIVE_PAYLOAD_TEXT)])

    candles = fetch_window(session, "BTC-USD", *window())

    assert len(candles) == 1
    assert len(session.calls) == 2
    assert no_sleep == [1.0]


@pytest.mark.parametrize("status", [408, 524])
def test_fetch_window_retries_http_408_and_the_proxy_timeout_524(no_sleep, status):
    session = FakeSession([FakeResponse(status), FakeResponse(200, LIVE_PAYLOAD_TEXT)])

    candles = fetch_window(session, "BTC-USD", *window())

    assert len(candles) == 1
    assert len(session.calls) == 2
    assert no_sleep == [1.0]


def test_fetch_window_retries_a_chunked_encoding_error_and_returns_the_second_attempt(no_sleep):
    cut_off = requests.exceptions.ChunkedEncodingError("response ended mid-body")
    session = FakeSession([cut_off, FakeResponse(200, LIVE_PAYLOAD_TEXT)])

    candles = fetch_window(session, "BTC-USD", *window())

    assert [candle.close for candle in candles] == [Decimal("58991.99")]
    assert len(session.calls) == 2
    assert no_sleep == [1.0]


@pytest.mark.parametrize(
    "error",
    [
        requests.ConnectionError("connection reset"),
        requests.Timeout("read timed out"),
        requests.exceptions.ConnectTimeout("connect timed out"),
    ],
)
def test_fetch_window_retries_transport_failures(no_sleep, error):
    session = FakeSession([error, FakeResponse(200, LIVE_PAYLOAD_TEXT)])

    candles = fetch_window(session, "BTC-USD", *window())

    assert len(candles) == 1
    assert len(session.calls) == 2
    assert no_sleep == [1.0]


def test_fetch_window_gives_up_on_repeated_timeouts(no_sleep):
    session = FakeSession([requests.Timeout("read timed out")] * coinbase_candles.MAX_ATTEMPTS)

    with pytest.raises(RuntimeError):
        fetch_window(session, "BTC-USD", *window())

    assert len(session.calls) == coinbase_candles.MAX_ATTEMPTS
    assert no_sleep == [1.0, 2.0, 4.0, 8.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_fetch_window_does_not_retry_other_4xx(no_sleep, status):
    session = FakeSession([FakeResponse(status), FakeResponse(200, LIVE_PAYLOAD_TEXT)])

    with pytest.raises(requests.HTTPError):
        fetch_window(session, "BTC-USD", *window())

    assert len(session.calls) == 1
    assert no_sleep == []


def test_fetch_window_decodes_with_decimal(no_sleep):
    """The retry path must not lose parse_float=Decimal on the way through."""
    session = FakeSession([FakeResponse(503), FakeResponse(200, LIVE_PAYLOAD_TEXT)])

    (candle,) = fetch_window(session, "BTC-USD", *window())

    assert isinstance(candle.close, Decimal)
    assert candle.close == Decimal("58991.99")
