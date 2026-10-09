"""Create the warehouse database if it is missing, then apply the DDL.

    pip install -r requirements.txt
    python scripts/run_setup.py

Reads DATABASE_URL from the environment or from .env. Everything runs through
psycopg2, so psql does not have to be installed or on PATH.

The DDL files use plain CREATE, so they cannot be applied twice. Re-running
against a database that already has core.assets stops with an explanation.
To rebuild from scratch, which deletes every stored candle:

    python scripts/run_setup.py --reset

To apply a changed mart definition without touching the stored candles:

    python scripts/run_setup.py --rebuild-marts

To convert a warehouse built before timeframes existed (one whose fact table
is core.ohlcv_hourly), keeping every stored candle and run record:

    python scripts/run_setup.py --migrate
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging

from crypto_warehouse.database_connection import OperatorError, configure_logging
from crypto_warehouse.database_setup import run_setup


def main() -> None:
    """Parse the command line, run the setup, and exit with a process exit code."""
    logger = logging.getLogger("run_setup")
    parser = argparse.ArgumentParser(
        prog="run_setup.py",
        description="Create the database and apply sql/01_schema.sql and sql/02_marts.sql.",
    )
    rebuild_options = parser.add_mutually_exclusive_group()
    rebuild_options.add_argument(
        "--reset",
        action="store_true",
        help="Drop schemas raw, core and marts first. Deletes all stored data.",
    )
    rebuild_options.add_argument(
        "--rebuild-marts",
        action="store_true",
        help=(
            "Drop and re-create only the marts schema from sql/02_marts.sql. "
            "Raw and core keep their data"
        ),
    )
    rebuild_options.add_argument(
        "--migrate",
        action="store_true",
        help=(
            "Convert a warehouse built before timeframes existed (fact table "
            "core.ohlcv_hourly) in place, keeping every stored candle"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    args = parser.parse_args()

    configure_logging(args.verbose)
    try:
        sys.exit(
            run_setup(reset=args.reset, marts_only=args.rebuild_marts, migrate=args.migrate)
        )
    except (OperatorError, FileNotFoundError) as error:
        logger.error("%s", error)
        sys.exit(2)


if __name__ == "__main__":
    main()
