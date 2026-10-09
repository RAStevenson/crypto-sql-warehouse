"""Run the warehouse checks: the dbt tests, then sql/05_verify.sql.

    python scripts/run_verify.py

`dbt test` runs every test under dbt/ and lists the failing ones; the
timezone check in sql/05_verify.sql is a DO block that raises on failure.
This exits nonzero when anything failed. The checks hold after an ordinary
daily load. They fail when a series' load failed, when a candle was left out
of core, or when the marts were not refreshed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging

from crypto_warehouse.database_connection import OperatorError, configure_logging
from crypto_warehouse.warehouse_verification import run_verify


def main() -> None:
    """Parse the command line, run the verify script, and exit with a process exit code."""
    logger = logging.getLogger("run_verify")
    parser = argparse.ArgumentParser(
        prog="run_verify.py",
        description="Run the dbt tests and sql/05_verify.sql; nonzero exit if anything fails.",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    args = parser.parse_args()

    configure_logging(args.verbose)
    try:
        sys.exit(run_verify())
    except (OperatorError, FileNotFoundError) as error:
        logger.error("%s", error)
        sys.exit(2)


if __name__ == "__main__":
    main()
