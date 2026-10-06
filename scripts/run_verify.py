"""Run the warehouse smoke tests in sql/05_verify.sql.

    python scripts/run_verify.py

Each check is a DO block that raises on failure, so this exits nonzero when
one fails and prints the message that names it. The checks hold after an
ordinary daily load. They fail when a product's load failed, when a candle
was left out of core, or when the marts were not refreshed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging

from crypto_warehouse.database_connection import ConfigError, configure_logging
from crypto_warehouse.warehouse_verification import run_verify

log = logging.getLogger("run_verify")


def main() -> int:
    """Parse the command line, run the verify script, and return a process exit code."""
    parser = argparse.ArgumentParser(
        prog="run_verify.py",
        description="Execute sql/05_verify.sql; nonzero exit if any check fails.",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    args = parser.parse_args()

    configure_logging(args.verbose)
    try:
        return run_verify()
    except (ConfigError, FileNotFoundError) as error:
        log.error("%s", error)
        return 2


if __name__ == "__main__":
    sys.exit(main())
