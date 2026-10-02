"""Coinbase Exchange candles client.

Split in two halves on purpose. fetch_window does the I/O and the retrying;
parse_candles is pure and does the column mapping. The mapping is the one
piece whose silent failure would poison every downstream number in the
warehouse, so it is the piece that gets tested without a network.
"""

from __future__ import annotations

import decimal
import logging
import random
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import NamedTuple

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.exchange.coinbase.com"
GRANULARITY_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 30

MAX_ATTEMPTS = 5
# 429 is rate limiting and 5xx is the exchange having a moment; both are worth
# another try. Every other 4xx (404 on an unknown product, 400 on a bad window)
# is a bug in the request and retrying it just wastes time.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
BACKOFF_JITTER_SECONDS = 0.5
# A Retry-After long enough to be a typo should not hang the run all day.
MAX_RETRY_AFTER_SECONDS = 120.0


class CandlePayloadError(ValueError):
    """The endpoint answered 200 with something that is not a candle array."""


class Candle(NamedTuple):
    """One hourly bar, with the fields in the order Coinbase puts them on the
    wire: [time, low, high, open, close, volume].

    That is deliberately not the open/high/low/close order the DDL declares,
    and the mismatch used to live in an INSERT whose column list quietly
    reordered itself against the table definition. Naming the fields is the
    fix: callers write candle.open rather than row[3], and the INSERT can list
    its columns in DDL order without anyone having to hold both orders in
    their head at once.
    """

    bucket_start: datetime
    low: Decimal
    high: Decimal
    open: Decimal
    close: Decimal
    volume: Decimal


def _to_decimal(value: object) -> Decimal:
    """Coerce one JSON scalar to Decimal without ever routing through float.

    The response is decoded with parse_float=Decimal, so prices normally
    arrive here already exact. The float branch exists only for a caller that
    decoded some other way, and goes via str() so it recovers the shortest
    representation rather than the binary expansion.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError(f"{value!r} is a bool, not a price")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(f"cannot read {value!r} as a decimal")


def parse_candles(payload: object, product_code: str) -> list[Candle]:
    """Map a decoded candles response onto Candle rows.

    Pure: no network, no clock. Anything that is not a list of at least
    6-element rows raises CandlePayloadError naming the product, so an error
    body like {"message": "NotFound"} surfaces as a typed error instead of a
    TypeError thrown while iterating a dict's keys three frames away. An empty
    list is not an error: an hour in which nothing traded is legitimately
    absent from the response.
    """
    if isinstance(payload, dict):
        message = payload.get("message", payload)
        raise CandlePayloadError(
            f"{product_code}: candles endpoint returned an error body: {message!r}"
        )
    if not isinstance(payload, list):
        raise CandlePayloadError(
            f"{product_code}: expected a list of candles, got {type(payload).__name__}"
        )

    candles: list[Candle] = []
    for index, row in enumerate(payload):
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise CandlePayloadError(
                f"{product_code}: candle {index} is not a 6-element row: {row!r}"
            )
        try:
            candles.append(
                Candle(
                    bucket_start=datetime.fromtimestamp(int(row[0]), tz=UTC),
                    low=_to_decimal(row[1]),
                    high=_to_decimal(row[2]),
                    open=_to_decimal(row[3]),
                    close=_to_decimal(row[4]),
                    volume=_to_decimal(row[5]),
                )
            )
        except (TypeError, ValueError, OverflowError, decimal.InvalidOperation) as exc:
            raise CandlePayloadError(
                f"{product_code}: candle {index} has unreadable values {row!r}: {exc}"
            ) from exc
    return candles


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with jitter, so a whole fleet of retries does not
    come back in lockstep."""
    base = min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_CAP_SECONDS)
    return base + random.uniform(0.0, BACKOFF_JITTER_SECONDS)


def _retry_after_seconds(response: requests.Response) -> float | None:
    """The server's own Retry-After, in seconds, when it gave a usable one.

    Only the delta-seconds form is honored. The HTTP-date form is legal but
    Coinbase does not send it, and parsing it to save a request is not worth
    the code."""
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        seconds = float(header.strip())
    except (AttributeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def fetch_window(
    session: requests.Session,
    product_code: str,
    window_start: datetime,
    window_end: datetime,
) -> list[Candle]:
    """Fetch one inclusive [window_start, window_end] candle window.

    Retries rate limiting, 5xx, and the transport-level failures that a long
    backfill actually hits (a dropped connection, a read timeout). Other 4xx
    responses are fatal on the first try.
    """
    url = f"{API_BASE}/products/{product_code}/candles"
    params = {
        "granularity": GRANULARITY_SECONDS,
        "start": window_start.isoformat(),
        "end": window_end.isoformat(),
    }

    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            delay = _backoff_seconds(attempt)
            log.warning(
                "%s: %s on attempt %d/%d, retrying in %.1fs",
                product_code,
                type(exc).__name__,
                attempt + 1,
                MAX_ATTEMPTS,
                delay,
            )
        else:
            if response.status_code not in RETRY_STATUSES:
                response.raise_for_status()
                # parse_float=Decimal is what makes the numeric columns honest.
                # Without it every price would round-trip through a binary
                # double before psycopg2 ever saw it, and 9999999999.99999999
                # would land in the warehouse as 10000000000.0.
                return parse_candles(
                    response.json(parse_float=decimal.Decimal), product_code
                )

            last_error = requests.HTTPError(
                f"HTTP {response.status_code} from {url}", response=response
            )
            delay = None
            if response.status_code == 429:
                delay = _retry_after_seconds(response)
            if delay is None:
                delay = _backoff_seconds(attempt)
            log.warning(
                "%s: HTTP %d on attempt %d/%d, retrying in %.1fs",
                product_code,
                response.status_code,
                attempt + 1,
                MAX_ATTEMPTS,
                delay,
            )

        # Sleeping after the final attempt buys nothing but a slower failure.
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(delay)

    raise RuntimeError(
        f"{product_code}: gave up fetching {window_start.isoformat()}..."
        f"{window_end.isoformat()} after {MAX_ATTEMPTS} attempts"
    ) from last_error
