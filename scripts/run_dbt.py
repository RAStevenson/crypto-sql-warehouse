"""Run any dbt command against the project in dbt/, with the connection set.

    python scripts/run_dbt.py run                     # build or refresh the marts
    python scripts/run_dbt.py test                    # the warehouse checks
    python scripts/run_dbt.py docs generate           # lineage and column docs, under dbt/target/
    python scripts/run_dbt.py ls --resource-type test

Everything after the script's own options goes to dbt as given. The script
fills the connection variables dbt/profiles.yml reads from DATABASE_URL in
.env, so dbt needs no profile of its own on the machine. run_setup.py,
run_ingest.py, and run_verify.py call dbt the same way for the commands they
need; this script is for every other dbt command.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging

from crypto_warehouse.database_connection import OperatorError, configure_logging
from crypto_warehouse.dbt_project import run_dbt


def main() -> None:
    """Parse the command line, hand the rest to dbt, and exit with dbt's exit code."""
    logger = logging.getLogger("run_dbt")
    parser = argparse.ArgumentParser(
        prog="run_dbt.py",
        description="Run a dbt command against dbt/ with the connection taken from DATABASE_URL.",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    parser.add_argument(
        "dbt_arguments",
        nargs=argparse.REMAINDER,
        metavar="DBT_COMMAND",
        help="The dbt command and its options, e.g. run, test, or docs generate",
    )
    args = parser.parse_args()
    if not args.dbt_arguments:
        parser.error("give dbt a command, e.g. run, test, or docs generate")

    configure_logging(args.verbose)
    try:
        sys.exit(run_dbt(args.dbt_arguments))
    except OperatorError as error:
        logger.error("%s", error)
        sys.exit(2)


if __name__ == "__main__":
    main()
