"""Add an asset to the warehouse and backfill its history.

    python scripts/run_add_asset.py XRP-USD                  # symbol XRP, name XRP
    python scripts/run_add_asset.py XRP-USD --name Ripple
    python scripts/run_add_asset.py XRP-USD --no-backfill    # register only

The product code is validated against Coinbase before anything is written; it
has to answer with at least one candle in the trailing probe window. The
default run then backfills the standard year of history and refreshes the
marts, so the new asset appears in marts.tableau_daily_extract in the same
run. The committed report's numbers do not move until
run_diversification_analysis.py is rerun.

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
from crypto_warehouse.database_connection import ConfigError, configure_logging, connect

log = logging.getLogger("run_add_asset")


def positive_int(value: str) -> int:
    """Return a command-line value as a whole number of days, raising an argparse
    error when it is below 1."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1 day, got {value}")
    return number


def main() -> int:
    """Validate the product on Coinbase, register it in core.assets, backfill it
    unless --no-backfill is passed, and return a process exit code."""
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
        type=positive_int,
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
        base, quote = parse_product_code(args.product_code)
        code = f"{base}-{quote}"
        symbol = (args.symbol or base).strip().upper()
        name = args.name or symbol

        with requests.Session() as session:
            session.headers["User-Agent"] = USER_AGENT
            probe_product(session, code)

        with closing(connect()) as conn:
            with conn.cursor() as cur:
                asset_id = insert_asset(
                    cur,
                    symbol=symbol,
                    product_code=code,
                    asset_name=name,
                    quote_currency=quote,
                )
            conn.commit()
        log.info("%s registered as asset_id %d (symbol %s)", code, asset_id, symbol)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except (requests.RequestException, RuntimeError, CandlePayloadError) as exc:
        log.error("%s: Coinbase did not answer for this product: %s", args.product_code, exc)
        return 2

    if args.no_backfill:
        log.info("registered without backfill; the next ingest run loads %s", code)
        return 0

    exit_code = run_ingest(
        products=[code],
        backfill_days=backfill_days,
        apply_floor=False,
        refresh=True,
    )
    if exit_code != 0:
        log.error(
            "%s is registered but its backfill did not finish. Finish it with: "
            "python scripts/run_ingest.py --products %s --backfill-days %d",
            code,
            code,
            backfill_days,
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
