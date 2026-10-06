"""Refresh the materialized views named in sql/03_refresh_marts.sql.

    python scripts/run_refresh_marts.py

The same mart refresh statements that run_ingest.py issues after a load, for
when the marts need refreshing without a load. They refresh CONCURRENTLY, so
readers are never blocked, and the three refreshes commit together. A refresh
does not apply a changed mart definition; scripts/run_setup.py --rebuild-marts
does.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging
from contextlib import closing

from crypto_warehouse.candle_ingest import refresh_marts
from crypto_warehouse.database_connection import OperatorError, configure_logging, connect

logger = logging.getLogger("run_refresh_marts")


def main() -> int:
    """Parse the command line, refresh the marts, and return a process exit code."""
    parser = argparse.ArgumentParser(
        prog="run_refresh_marts.py",
        description="Refresh the marts layer.",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    args = parser.parse_args()

    configure_logging(args.verbose)
    try:
        with closing(connect()) as connection:
            refresh_marts(connection)
    except (OperatorError, FileNotFoundError) as error:
        logger.error("%s", error)
        return 2
    logger.info("marts refreshed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
