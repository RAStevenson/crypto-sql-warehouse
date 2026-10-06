"""Registering a new asset in the warehouse's work list.

core.assets is the ingest work list (is_active = true), so adding an asset is
an INSERT. This module holds the validation that runs before the INSERT. A
mistyped product code would otherwise stay in the work list and fail in every
scheduled run from then on, so a code must answer on Coinbase with at least
one candle before it is inserted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import requests
from psycopg2 import errors as pg_errors

from .coinbase_candles import fetch_window
from .database_connection import OperatorError
from .fetch_windows import floor_hour

logger = logging.getLogger(__name__)

# How far back the validation probe looks. Wide enough that a real but thinly
# traded product with some zero-trade hours still shows a candle; an empty
# answer over the whole span rejects the product, because "the endpoint did
# not error" is not evidence the work list gained something ingestible.
PROBE_HOURS = 72


def parse_product_code(product_code: str) -> tuple[str, str]:
    """Split a product code such as 'XRP-USD' into ('XRP', 'USD'), uppercased and
    with surrounding whitespace removed.

    Raises OperatorError for anything not of the form BASE-QUOTE.
    """
    # Lowercase is normalized, not rejected, because Coinbase treats codes
    # case-insensitively and the seeded codes are uppercase.
    normalized_code = product_code.strip().upper()
    parts = normalized_code.split("-")
    # Checked here because the API answers a malformed code with a 404, which
    # reads as "no such product" and does not name the actual mistake.
    if len(parts) != 2 or not all(part.isalnum() and part for part in parts):
        raise OperatorError(
            f"{product_code!r} is not a Coinbase product code of the form BASE-QUOTE, e.g. XRP-USD"
        )
    return parts[0], parts[1]


def probe_product(session: requests.Session, product_code: str) -> int:
    """Fetch the trailing PROBE_HOURS of candles for a product and return how
    many came back.

    Raises OperatorError when the answer holds no candles, and an unknown
    product raises the client's HTTPError.
    """
    end = floor_hour(datetime.now(UTC))
    start = end - timedelta(hours=PROBE_HOURS)
    candles = fetch_window(session, product_code, start, end)
    if not candles:
        raise OperatorError(
            f"{product_code}: Coinbase answered but returned no candles in "
            f"the last {PROBE_HOURS} hours; refusing to add a product with "
            "no ingestible history"
        )
    logger.info(
        "%s: probe returned %d candle(s) in the trailing %d hours",
        product_code,
        len(candles),
        PROBE_HOURS,
    )
    return len(candles)


def insert_asset(
    cursor,
    *,
    symbol: str,
    product_code: str,
    asset_name: str,
    quote_currency: str,
) -> int:
    """Insert the asset row into core.assets and return its asset_id.

    A symbol or product code that already exists raises OperatorError, because
    the insert never updates an existing row.
    """
    # The INSERT has no ON CONFLICT clause, because re-adding an existing
    # asset is either a mistake or a soft-disabled asset coming back, and both
    # call for a look at the existing row, not a silent merge.
    try:
        cursor.execute(
            """
            INSERT INTO core.assets (symbol, product_code, asset_name, quote_currency)
            VALUES (%s, %s, %s, %s)
            RETURNING asset_id
            """,
            (symbol, product_code, asset_name, quote_currency),
        )
    except pg_errors.UniqueViolation as error:
        raise OperatorError(
            f"{symbol} / {product_code} collides with an existing row in "
            "core.assets. If the asset was soft-disabled, set "
            "is_active = true instead of re-adding it."
        ) from error
    return cursor.fetchone()[0]
