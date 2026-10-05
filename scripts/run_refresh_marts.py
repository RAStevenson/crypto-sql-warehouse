"""Rebuild the materialized views in sql/03_refresh_marts.sql.

    python scripts/run_refresh_marts.py

The same mart refresh statements that run_ingest.py issues after a load, for
when the marts need rebuilding without a load. They refresh CONCURRENTLY, so
readers are never blocked, and the three refreshes commit together.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging
from contextlib import closing

from crypto_warehouse.candle_ingest import refresh_marts
from crypto_warehouse.database_connection import ConfigError, configure_logging, connect

log = logging.getLogger("run_refresh_marts")


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
        with closing(connect()) as conn:
            refresh_marts(conn)
    except (ConfigError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return 2
    log.info("marts refreshed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
