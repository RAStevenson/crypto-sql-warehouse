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
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging

from crypto_warehouse.config import ConfigError, configure_logging
from crypto_warehouse.setup_db import run_setup

log = logging.getLogger("run_setup")


def main() -> int:
    """Parse the command line, run the setup, and return a process exit code."""
    parser = argparse.ArgumentParser(
        prog="run_setup.py",
        description="Create the database and apply sql/01_schema.sql and sql/02_marts.sql.",
    )
    rebuild = parser.add_mutually_exclusive_group()
    rebuild.add_argument(
        "--reset",
        action="store_true",
        help="Drop schemas raw, core and marts first. Deletes all stored data.",
    )
    rebuild.add_argument(
        "--rebuild-marts",
        action="store_true",
        help=(
            "Drop and re-create only the marts schema from sql/02_marts.sql. "
            "Raw and core keep their data"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    args = parser.parse_args()

    configure_logging(args.verbose)
    try:
        return run_setup(reset=args.reset, marts_only=args.rebuild_marts)
    except (ConfigError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
