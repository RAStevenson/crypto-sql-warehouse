"""Registering a new asset in the warehouse's work list.

core.assets IS the ingest work list (is_active = true), so adding an asset is
an INSERT. What earns this a module is the validation in front of the INSERT.
A typo'd product code would otherwise sit in the work list failing every
scheduled run from then on, so the code has to prove it answers on Coinbase
with at least one candle before it is allowed in.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import requests
from psycopg2 import errors as pg_errors

from .api import fetch_window
from .config import ConfigError
from .windows import floor_hour

log = logging.getLogger(__name__)

# How far back the validation probe looks. Wide enough that a real but thinly
# traded product with some zero-trade hours still shows a candle; an empty
# answer over the whole span rejects the product, because "the endpoint did
# not error" is not evidence the work list gained something ingestible.
PROBE_HOURS = 72


def parse_product_code(product_code: str) -> tuple[str, str]:
    """Split 'XRP-USD' into ('XRP', 'USD'), rejecting anything else early.

    The API would reject a malformed code too, but with a 404 that reads as
    "no such product", and the operator's actual mistake (wrong separator, a
    stray space) deserves a message that names it. Lowercase is normalized
    rather than rejected because Coinbase treats codes case-insensitively and
    the schema seeds are upper.
    """
    code = product_code.strip().upper()
    parts = code.split("-")
    if len(parts) != 2 or not all(part.isalnum() and part for part in parts):
        raise ConfigError(
            f"{product_code!r} is not a Coinbase product code of the form BASE-QUOTE, e.g. XRP-USD"
        )
    return parts[0], parts[1]


def probe_product(session: requests.Session, product_code: str) -> int:
    """Positive evidence that the product exists and trades. Returns the
    candle count seen.

    Fetches the trailing PROBE_HOURS as one window. An unknown product
    surfaces as the client's HTTPError (a 404 is fatal on the first try
    there); a 200 with no candles at all is rejected here, since a product
    that has not traded for three days straight is either de-listed or a code
    that resolves to the wrong thing.
    """
    end = floor_hour(datetime.now(UTC))
    start = end - timedelta(hours=PROBE_HOURS)
    candles = fetch_window(session, product_code, start, end)
    if not candles:
        raise ConfigError(
            f"{product_code}: Coinbase answered but returned no candles in "
            f"the last {PROBE_HOURS} hours; refusing to add a product with "
            "no ingestible history"
        )
    log.info(
        "%s: probe returned %d candle(s) in the trailing %d hours",
        product_code,
        len(candles),
        PROBE_HOURS,
    )
    return len(candles)


def insert_asset(
    cur,
    *,
    symbol: str,
    product_code: str,
    asset_name: str,
    quote_currency: str,
) -> int:
    """INSERT the asset row and return its asset_id.

    No upsert on purpose. Re-adding an existing asset is either an operator
    mistake or a soft-disabled asset coming back, and both deserve a look at
    the existing row rather than a silent merge, so the unique collision is
    translated into a plain instruction instead of a traceback.
    """
    try:
        cur.execute(
            """
            INSERT INTO core.assets (symbol, product_code, asset_name, quote_currency)
            VALUES (%s, %s, %s, %s)
            RETURNING asset_id
            """,
            (symbol, product_code, asset_name, quote_currency),
        )
    except pg_errors.UniqueViolation as exc:
        raise ConfigError(
            f"{symbol} / {product_code} collides with an existing row in "
            "core.assets. If the asset was soft-disabled, set "
            "is_active = true instead of re-adding it."
        ) from exc
    return cur.fetchone()[0]
