"""Create the warehouse database if it is missing, apply the DDL, and have
dbt build the marts.

    pip install -r requirements.txt
    python scripts/run_setup.py

Reads DATABASE_URL from the environment or from .env. The DDL runs through
psycopg2, so psql does not have to be installed or on PATH, and the marts are
built by dbt from dbt/models/marts/, with the same connection.

The DDL file uses plain CREATE, so it cannot be applied twice. Re-running
against a database that already has core.assets stops with an explanation.
To rebuild from scratch, which deletes every stored candle:

    python scripts/run_setup.py --reset

To apply a changed mart definition without touching the stored candles:

    python scripts/run_setup.py --rebuild-marts

To bring a warehouse built before the current schema up to date in place,
keeping every stored candle and run record (the files in sql/migrations/ say
what each step changes):

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
        description="Create the database, apply sql/01_schema.sql, and build the marts with dbt.",
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
            "Drop the marts schema and have dbt build it again from "
            "dbt/models/marts/. Raw and core keep their data"
        ),
    )
    rebuild_options.add_argument(
        "--migrate",
        action="store_true",
        help=(
            "Apply the migrations in sql/migrations/ that this warehouse still "
            "needs, in place, keeping every stored candle"
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
