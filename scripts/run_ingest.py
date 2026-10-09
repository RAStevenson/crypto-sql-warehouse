"""Load Coinbase candles into the warehouse, incrementally.

    python scripts/run_ingest.py                       # every active series, then the marts
    python scripts/run_ingest.py --no-refresh-marts    # load only, leave the marts alone
    python scripts/run_ingest.py --products BTC-USD ETH-USD
    python scripts/run_ingest.py --timeframes 15m --backfill-days 90

A load ends with the mart refresh in sql/03_refresh_marts.sql unless
--no-refresh-marts says otherwise, so the default run leaves nothing stale.

Each (product, timeframe) series resumes from its MAX(ts_utc) in core.ohlcv
minus a few hours of deliberate overlap, so running it twice in a row is a
no-op in core. One series failing does not stop the others; the exit code is
nonzero if anything failed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging

from crypto_warehouse.candle_ingest import DEFAULT_BACKFILL_DAYS, run_ingest
from crypto_warehouse.database_connection import OperatorError, configure_logging


def positive_day_count(value: str) -> int:
    """Return a command-line value as a whole number of days, raising an argparse
    error when it is below 1."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1 day, got {value}")
    return number


def main() -> None:
    """Parse the command line, run the ingest, and exit with a process exit code."""
    logger = logging.getLogger("run_ingest")
    parser = argparse.ArgumentParser(
        prog="run_ingest.py",
        description="Incremental, idempotent OHLCV ingestion from Coinbase Exchange.",
    )
    parser.add_argument(
        "--backfill-days",
        type=positive_day_count,
        default=None,
        help=(
            f"How far back to reach, in days (default {DEFAULT_BACKFILL_DAYS}). "
            "For a product and timeframe with no rows yet this is the depth of "
            "the first load. Passing the flag explicitly ALSO makes it a floor on "
            "an existing resume point, so --backfill-days 365 against a "
            "90-day warehouse extends it back to a year. Omitting the flag "
            "leaves an ordinary run purely incremental: the floor is not "
            "applied, and only the trailing overlap gets re-fetched."
        ),
    )
    parser.add_argument(
        "--products",
        nargs="+",
        default=None,
        metavar="PRODUCT",
        help=(
            "Product codes to load (default: every asset with is_active in "
            "core.assets). Naming products explicitly loads them even if "
            "is_active is false, which is how a soft-disabled asset gets "
            "backfilled; the override is logged."
        ),
    )
    parser.add_argument(
        "--timeframes",
        nargs="+",
        default=None,
        metavar="TIMEFRAME",
        help=(
            "Timeframe codes to load, from 1m, 5m, 15m and 1h (default: every "
            "timeframe with is_active in core.timeframes). Naming a timeframe "
            "explicitly loads it even if is_active is false; the override is logged."
        ),
    )
    parser.add_argument(
        "--recheck-gaps",
        action="store_true",
        help=(
            "Instead of loading forward from the newest candle, re-request every "
            "candle missing from core that no recorded request covers, load any "
            "that come back, and record the windows asked for. For a warehouse "
            "loaded before raw.fetch_windows existed. Cannot be combined with "
            "--backfill-days"
        ),
    )
    parser.add_argument(
        "--no-refresh-marts",
        action="store_true",
        help="Skip the mart refresh that otherwise runs after loading",
    )
    # --refresh-marts is accepted and does nothing, because refreshing is the
    # default. Report copies rendered before 2026-08-24 name the flag in their
    # reproduce section, and it can be removed once no published report does.
    parser.add_argument("--refresh-marts", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    args = parser.parse_args()

    if args.recheck_gaps and args.backfill_days is not None:
        parser.error("--recheck-gaps reads its span from the gaps; drop --backfill-days")

    configure_logging(args.verbose)
    backfill_days = args.backfill_days if args.backfill_days is not None else DEFAULT_BACKFILL_DAYS

    try:
        sys.exit(run_ingest(
            products=args.products,
            timeframes=args.timeframes,
            backfill_days=backfill_days,
            extend_to_backfill_days=args.backfill_days is not None,
            refresh=not args.no_refresh_marts,
            recheck=args.recheck_gaps,
        ))
    except (OperatorError, FileNotFoundError) as error:
        logger.error("%s", error)
        sys.exit(2)


if __name__ == "__main__":
    main()
