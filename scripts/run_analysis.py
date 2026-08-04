"""Measure cross-asset diversification and write reports/metrics.json.

    python scripts/run_ingest.py --refresh-marts
    python scripts/run_analysis.py
    python scripts/run_build_report.py

Reads marts.daily_metrics, keeps the dates where every asset has a non-null
log return, and writes the panel description, per-asset volatility, the
correlation matrix and the equal-weight diversification result to
reports/metrics.json, plus four figures under reports/figures/. The report
builder reads that file; nothing is transcribed by hand between the two.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging

import psycopg2

from crypto_warehouse.analysis import FIGURES_DIR, METRICS_PATH, run_analysis
from crypto_warehouse.config import ConfigError, configure_logging

log = logging.getLogger("run_analysis")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run_analysis.py",
        description="Compute the diversification metrics and figures from marts.daily_metrics.",
    )
    parser.add_argument(
        "--metrics", default=str(METRICS_PATH), help="output path for metrics.json"
    )
    parser.add_argument(
        "--figures", default=str(FIGURES_DIR), help="output directory for the PNG figures"
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    args = parser.parse_args()

    configure_logging(args.verbose)
    try:
        metrics = run_analysis(Path(args.metrics), Path(args.figures))
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except psycopg2.Error as exc:
        log.error("database error: %s", str(exc).strip())
        log.error("run scripts/run_setup.py and scripts/run_ingest.py --refresh-marts first")
        return 1
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    div = metrics["diversification"]
    log.info(
        "equal-weight portfolio vol %.4f against average single-asset vol %.4f",
        div["equal_weight_portfolio_vol"],
        div["average_single_asset_vol"],
    )
    log.info(
        "risk removed %.1f%% of the %.1f%% removable at zero correlation (%.1f%% captured)",
        100 * div["risk_removed_actual"],
        100 * div["risk_removed_if_uncorrelated"],
        100 * div["diversification_captured"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
