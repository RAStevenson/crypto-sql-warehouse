"""Rebuild the materialized views in sql/03_refresh_marts.sql.

    python scripts/run_refresh_marts.py

The same mart refresh statements that run_ingest.py issues after a load, for
when the marts need rebuilding without a load. They refresh CONCURRENTLY, so
readers are never blocked and the connection runs with autocommit.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging
from contextlib import closing

from crypto_warehouse.config import ConfigError, configure_logging, connect
from crypto_warehouse.ingest import refresh_marts

log = logging.getLogger("run_refresh_marts")


def main() -> int:
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
