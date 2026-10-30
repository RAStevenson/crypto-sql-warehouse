"""Export a slice of the warehouse to a CSV file.

    python scripts/run_export.py prices --products BTC-USD --timeframe 15m --start 2024-01-01
    python scripts/run_export.py prices --timeframe 1h          # every active asset, all history
    python scripts/run_export.py daily --products BTC-USD ETH-USD --start 2025-01-01

prices writes the stored candles for the chosen assets at one timeframe, with
the symbol, product code, and timeframe on every row ahead of time_utc, open,
high, low, close, and volume. daily writes the rows of
marts.tableau_daily_extract: daily prices with returns, moving averages,
30-day volatility, and drawdown. Both default to every active asset and to
all stored history, and both write to exports/ unless --output names a file.
Dates are UTC and both ends are included.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging
from contextlib import closing
from datetime import date

from crypto_warehouse.csv_export import (
    DAILY_QUERY,
    PRICES_QUERY,
    check_timeframe,
    day_bounds,
    default_output_path,
    resolve_export_products,
    write_csv,
)
from crypto_warehouse.database_connection import OperatorError, configure_logging, connect


def iso_date(value: str) -> date:
    """Return a command-line value as a date, raising an argparse error when it
    is not YYYY-MM-DD."""
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected a date as YYYY-MM-DD, got {value!r}") from error


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the arguments both exports take."""
    parser.add_argument(
        "--products",
        nargs="+",
        default=None,
        metavar="PRODUCT",
        help="Product codes to export (default: every asset with is_active in core.assets)",
    )
    parser.add_argument(
        "--start", type=iso_date, default=None, help="First UTC date to include, YYYY-MM-DD"
    )
    parser.add_argument(
        "--end", type=iso_date, default=None, help="Last UTC date to include, YYYY-MM-DD"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="CSV file to write (default: a descriptive name under exports/)",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")


def main() -> None:
    """Parse the command line, write the requested export, and exit with a
    process exit code."""
    logger = logging.getLogger("run_export")
    parser = argparse.ArgumentParser(
        prog="run_export.py",
        description="Export candles or daily metrics from the warehouse to a CSV file.",
    )
    exports = parser.add_subparsers(dest="kind", required=True, metavar="{prices,daily}")
    prices_parser = exports.add_parser(
        "prices", help="Stored candles for chosen assets at one timeframe"
    )
    prices_parser.add_argument(
        "--timeframe",
        default="1h",
        help="Timeframe code: 1m, 5m, 15m, 1h, or 1d (default 1h)",
    )
    add_common_arguments(prices_parser)
    daily_parser = exports.add_parser(
        "daily", help="Daily prices with returns, volatility, and drawdown"
    )
    add_common_arguments(daily_parser)
    args = parser.parse_args()
    if args.start and args.end and args.start > args.end:
        parser.error(f"--start {args.start} is after --end {args.end}")

    configure_logging(args.verbose)
    timeframe_code = args.timeframe if args.kind == "prices" else None

    try:
        with closing(connect()) as connection:
            with connection.cursor() as cursor:
                product_codes = resolve_export_products(cursor, args.products)
                if timeframe_code:
                    check_timeframe(cursor, timeframe_code)
            if not product_codes:
                logger.error("no products to export; core.assets has no active rows")
                sys.exit(2)

            output_path = args.output or default_output_path(
                args.kind, product_codes, timeframe_code, args.start, args.end
            )
            if args.kind == "prices":
                start_instant, end_instant = day_bounds(args.start, args.end)
                rows_written = write_csv(
                    connection,
                    PRICES_QUERY,
                    {
                        "timeframe_code": timeframe_code,
                        "product_codes": product_codes,
                        "start": start_instant,
                        "end": end_instant,
                    },
                    output_path,
                )
            else:
                rows_written = write_csv(
                    connection,
                    DAILY_QUERY,
                    {
                        "product_codes": product_codes,
                        "start_date": args.start,
                        "end_date": args.end,
                    },
                    output_path,
                )
            # The export only reads, so its transaction is closed without a commit.
            connection.rollback()
    except OperatorError as error:
        logger.error("%s", error)
        sys.exit(2)

    if rows_written == 0:
        logger.warning(
            "no rows matched; %s holds only the header. Check the products, timeframe, "
            "and dates against what is loaded (sql/04_queries.sql Q03 lists it)",
            output_path,
        )
    else:
        logger.info("wrote %d row(s) to %s", rows_written, output_path)


if __name__ == "__main__":
    main()
