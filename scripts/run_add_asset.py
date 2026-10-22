"""Add an asset to the warehouse and backfill its history.

    python scripts/run_add_asset.py XRP-USD                  # symbol XRP, name XRP
    python scripts/run_add_asset.py XRP-USD --name Ripple
    python scripts/run_add_asset.py XRP-USD --no-backfill    # register only

The product code is validated against Coinbase before anything is written; it
has to answer with at least one candle in the trailing probe window. The
default run then backfills the standard year of history at every active
timeframe and refreshes the marts, so the new asset appears in
marts.tableau_daily_extract in the same run.

Deactivating an asset is one statement and needs no script:

    UPDATE core.assets SET is_active = false WHERE product_code = 'XRP-USD';
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging
from contextlib import closing

import requests

from crypto_warehouse.asset_registration import insert_asset, parse_product_code, probe_product
from crypto_warehouse.candle_ingest import DEFAULT_BACKFILL_DAYS, USER_AGENT, run_ingest
from crypto_warehouse.coinbase_candles import CandlePayloadError
from crypto_warehouse.database_connection import OperatorError, configure_logging, connect


def positive_day_count(value: str) -> int:
    """Return a command-line value as a whole number of days, raising an argparse
    error when it is below 1."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1 day, got {value}")
    return number


def main() -> None:
    """Validate the product on Coinbase, register it in core.assets, backfill it
    unless --no-backfill is passed, and exit with a process exit code."""
    logger = logging.getLogger("run_add_asset")
    parser = argparse.ArgumentParser(
        prog="run_add_asset.py",
        description="Validate a Coinbase product, register it in core.assets, backfill it.",
    )
    parser.add_argument("product_code", help="Coinbase product code, e.g. XRP-USD")
    parser.add_argument(
        "--symbol",
        default=None,
        help="Ticker symbol to store (default: the part before the dash)",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Human-readable asset name (default: the symbol)",
    )
    parser.add_argument(
        "--backfill-days",
        type=positive_day_count,
        default=None,
        help=f"Depth of the initial load in days (default {DEFAULT_BACKFILL_DAYS})",
    )
    parser.add_argument(
        "--no-backfill",
        action="store_true",
        help=(
            "Register the asset only; the next ingest run loads it. Cannot be "
            "combined with --backfill-days"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    args = parser.parse_args()
    if args.no_backfill and args.backfill_days is not None:
        parser.error("--backfill-days has no effect with --no-backfill; pass one or the other")
    backfill_days = args.backfill_days or DEFAULT_BACKFILL_DAYS

    configure_logging(args.verbose)

    try:
        base_currency, quote_currency = parse_product_code(args.product_code)
        product_code = f"{base_currency}-{quote_currency}"
        symbol = (args.symbol or base_currency).strip().upper()
        asset_name = args.name or symbol

        # Only the probe touches the network, so only the probe's failures are
        # reported as a failed probe. OperatorError subclasses RuntimeError and
        # is re-raised to the outer handler, which prints its own message.
        try:
            with requests.Session() as session:
                session.headers["User-Agent"] = USER_AGENT
                probe_product(session, product_code)
        except OperatorError:
            raise
        except (requests.RequestException, RuntimeError, CandlePayloadError):
            logger.error("%s: the Coinbase probe failed", product_code, exc_info=True)
            logger.error(
                "%s was not registered and nothing was written; check the product code "
                "and rerun",
                product_code,
            )
            sys.exit(2)

        with closing(connect()) as connection:
            with connection.cursor() as cursor:
                asset_id = insert_asset(
                    cursor,
                    symbol=symbol,
                    product_code=product_code,
                    asset_name=asset_name,
                    quote_currency=quote_currency,
                )
            connection.commit()
        logger.info("%s registered as asset_id %d (symbol %s)", product_code, asset_id, symbol)
    except OperatorError as error:
        logger.error("%s", error)
        sys.exit(2)

    if args.no_backfill:
        logger.info("registered without backfill; the next ingest run loads %s", product_code)
        return

    exit_code = run_ingest(
        products=[product_code],
        backfill_days=backfill_days,
        extend_to_backfill_days=False,
        refresh=True,
    )
    if exit_code != 0:
        logger.error(
            "%s is registered but its backfill did not finish. Finish it with: "
            "python scripts/run_ingest.py --products %s --backfill-days %d",
            product_code,
            product_code,
            backfill_days,
        )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
