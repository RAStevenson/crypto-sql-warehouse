"""Coinbase Exchange candles client.

The client is split in two. fetch_window does the I/O and the retrying, and
parse_candles is pure and does the column mapping. A mapping error would put
wrong values into every downstream number without raising, so the mapping is
kept free of I/O and tested without a network.
"""

from __future__ import annotations

import decimal
import logging
import math
import random
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import NamedTuple

import requests

logger = logging.getLogger(__name__)

COINBASE_EXCHANGE_URL = "https://api.exchange.coinbase.com"
REQUEST_TIMEOUT_SECONDS = 30

MAX_ATTEMPTS = 5
# 429 is rate limiting, 408 is a server-side timeout, and every 5xx is a
# failure on the server's side, including the 520 to 524 codes a proxy in
# front of the exchange returns when the origin is slow. All are worth another
# try. Every other 4xx (404 on an unknown product, 400 on a bad window) is a
# bug in the request and retrying it just wastes time.
RETRY_HTTP_STATUSES = frozenset({408, 429}) | frozenset(range(500, 600))
# A response cut off mid-body raises ChunkedEncodingError, which is neither a
# ConnectionError nor a Timeout in requests' hierarchy and has to be named.
RETRY_EXCEPTIONS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
BACKOFF_JITTER_SECONDS = 0.5
# A Retry-After long enough to be a typo should not hang the run all day.
MAX_RETRY_AFTER_SECONDS = 120.0


class CandlePayloadError(ValueError):
    """Raised when the endpoint answers 200 with a body that is not a candle array."""


class Candle(NamedTuple):
    """Holds one bar, with its fields in the order Coinbase sends them
    (time, low, high, open, close, volume).

    That order differs from the open/high/low/close order of the table
    columns, so callers read fields by name and never by position.
    """

    bucket_start: datetime
    low: Decimal
    high: Decimal
    open: Decimal
    close: Decimal
    volume: Decimal


def _to_decimal(value: object) -> Decimal:
    """Return one JSON scalar as a finite Decimal.

    A bool or an unsupported type raises TypeError, and NaN or Infinity raises
    ValueError.
    """
    if isinstance(value, bool):
        raise TypeError(f"{value!r} is a bool, not a price")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, (float, str)):
        # A float arrives only when the caller decoded without
        # parse_float=Decimal. str() gives its shortest decimal form, where
        # Decimal(float) would give the full binary expansion.
        number = Decimal(str(value))
    else:
        raise TypeError(f"cannot read {value!r} as a decimal")
    # NaN and Infinity are legal Decimals and legal numeric values in Postgres,
    # where NaN passes every CHECK on the candle tables, so they stop here.
    if not number.is_finite():
        raise ValueError(f"{value!r} is not a finite number")
    return number


def parse_candles(payload: object, product_code: str) -> list[Candle]:
    """Convert a decoded candles response into a list of Candle rows.

    Anything other than a list of rows with at least 6 readable elements
    raises CandlePayloadError naming the product, and an empty list is valid
    because an hour with no trades is absent from the response.
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
        except (TypeError, ValueError, OverflowError, decimal.InvalidOperation) as error:
            raise CandlePayloadError(
                f"{product_code}: candle {index} has unreadable values {row!r}: {error}"
            ) from error
    return candles


def _backoff_seconds(attempt: int) -> float:
    """Return the seconds to wait before the next attempt, which double per
    attempt up to a cap and carry a small random jitter so that retries do not
    repeat at fixed intervals."""
    base_delay = min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_CAP_SECONDS)
    return base_delay + random.uniform(0.0, BACKOFF_JITTER_SECONDS)


def _retry_after_seconds(response: requests.Response) -> float | None:
    """Return the server's Retry-After delay in seconds, capped at
    MAX_RETRY_AFTER_SECONDS, or None when it gave no usable one.

    Only the delta-seconds form is read, so an HTTP-date value returns None.
    """
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    # The HTTP-date form is legal, but Coinbase does not send it, so it is
    # not parsed and returns None.
    try:
        seconds = float(header.strip())
    except (AttributeError, ValueError):
        return None
    # Zero, a negative, or a non-finite value is not a usable delay; the
    # caller falls back to its own backoff.
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def fetch_window(
    session: requests.Session,
    product_code: str,
    window_start: datetime,
    window_end: datetime,
    granularity_seconds: int,
) -> list[Candle]:
    """Fetch one inclusive [window_start, window_end] window of candles of
    width granularity_seconds and return it as a list of Candle rows.

    Rate limiting, 408, 5xx, and transport failures are retried up to
    MAX_ATTEMPTS times before a RuntimeError, and any other 4xx raises on the
    first try.
    """
    url = f"{COINBASE_EXCHANGE_URL}/products/{product_code}/candles"
    query_parameters = {
        "granularity": granularity_seconds,
        "start": window_start.isoformat(),
        "end": window_end.isoformat(),
    }

    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = session.get(url, params=query_parameters, timeout=REQUEST_TIMEOUT_SECONDS)
        except RETRY_EXCEPTIONS as error:
            last_error = error
            delay = _backoff_seconds(attempt)
            logger.warning(
                "%s: %s on attempt %d/%d, retrying in %.1fs",
                product_code,
                type(error).__name__,
                attempt + 1,
                MAX_ATTEMPTS,
                delay,
            )
        else:
            if response.status_code not in RETRY_HTTP_STATUSES:
                response.raise_for_status()
                # parse_float=Decimal is what keeps the numeric columns exact.
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
            logger.warning(
                "%s: HTTP %d on attempt %d/%d, retrying in %.1fs",
                product_code,
                response.status_code,
                attempt + 1,
                MAX_ATTEMPTS,
                delay,
            )

        # A sleep after the final attempt would only delay the failure.
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(delay)

    raise RuntimeError(
        f"{product_code}: gave up fetching {window_start.isoformat()}..."
        f"{window_end.isoformat()} after {MAX_ATTEMPTS} attempts"
    ) from last_error
