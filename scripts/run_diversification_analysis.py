"""Measure cross-asset diversification and write results/metrics.json and
the chart the README shows.

    python scripts/run_ingest.py
    python scripts/run_diversification_analysis.py

Reads marts.daily_metrics, keeps the dates where every active asset has a
non-null log return, and writes the panel description, per-asset volatility,
drawdown and total return from the complete-day closes, the correlation
matrix, the equal-weight diversification result, and row counts for
provenance to results/metrics.json, and draws the diversification chart to
results/figures/diversification.png. The README quotes its numbers from that
file, and tests/test_readme_numbers.py fails when the two disagree.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging

import psycopg2
import psycopg2.errors

from crypto_warehouse.database_connection import OperatorError, configure_logging
from crypto_warehouse.diversification_metrics import (
    FIGURES_DIR,
    METRICS_PATH,
    PanelDataError,
    run_diversification_analysis,
)


def main() -> None:
    """Run the analysis, log the main results, and exit with a process exit code."""
    logger = logging.getLogger("run_diversification_analysis")
    parser = argparse.ArgumentParser(
        prog="run_diversification_analysis.py",
        description=(
            "Compute the diversification metrics and chart from marts.daily_metrics."
        ),
    )
    parser.add_argument(
        "--metrics", default=str(METRICS_PATH), help="output path for metrics.json"
    )
    parser.add_argument(
        "--figures", default=str(FIGURES_DIR), help="output directory for the PNG chart"
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    args = parser.parse_args()

    configure_logging(args.verbose)
    try:
        metrics = run_diversification_analysis(Path(args.metrics), Path(args.figures))
    except OperatorError as error:
        logger.error("%s", error)
        sys.exit(2)
    except psycopg2.OperationalError:
        logger.error("could not reach the database", exc_info=True)
        logger.error(
            "nothing was written; start the server or fix DATABASE_URL in .env, then rerun"
        )
        sys.exit(1)
    except psycopg2.errors.UndefinedTable:
        logger.error("a warehouse table is missing", exc_info=True)
        logger.error(
            "nothing was written; run scripts/run_setup.py and scripts/run_ingest.py first"
        )
        sys.exit(1)
    except PanelDataError as error:
        logger.error("%s", error)
        logger.error("nothing was written")
        sys.exit(1)

    diversification = metrics["diversification"]
    logger.info(
        "equal-weight portfolio vol %.4f against average single-asset vol %.4f",
        diversification["equal_weight_portfolio_vol"],
        diversification["average_single_asset_vol"],
    )
    logger.info(
        "risk removed %.1f%% of the %.1f%% removable at zero correlation (%.1f%% captured)",
        100 * diversification["risk_removed_actual"],
        100 * diversification["risk_removed_if_uncorrelated"],
        100 * diversification["diversification_captured"],
    )


if __name__ == "__main__":
    main()
