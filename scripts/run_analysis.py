"""Measure cross-asset diversification, write reports/metrics.json, and
render the report.

    python scripts/run_ingest.py
    python scripts/run_analysis.py

Reads marts.daily_metrics, keeps the dates where every active asset has a
non-null log return, and writes the panel description, per-asset volatility,
drawdown and total return from the complete-day closes, the correlation
matrix, the equal-weight diversification result, and row counts for
provenance to reports/metrics.json, plus four figures under reports/figures/. It then runs
scripts/run_build_report.py, which reads that file at render time; nothing is
transcribed by hand between the two. --no-report skips the render, and so does
a --metrics or --figures path other than the default. The builder also runs
standalone for iterating on the report's prose without
touching the database.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging
import subprocess

import psycopg2

from crypto_warehouse.analysis import FIGURES_DIR, METRICS_PATH, run_analysis
from crypto_warehouse.config import ConfigError, configure_logging

log = logging.getLogger("run_analysis")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run_analysis.py",
        description=(
            "Compute the diversification metrics and figures from marts.daily_metrics, "
            "then render the report."
        ),
    )
    parser.add_argument(
        "--metrics", default=str(METRICS_PATH), help="output path for metrics.json"
    )
    parser.add_argument(
        "--figures", default=str(FIGURES_DIR), help="output directory for the PNG figures"
    )
    parser.add_argument(
        "--no-report", action="store_true", help="skip rendering the HTML/PDF report"
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
        log.error("run scripts/run_setup.py and scripts/run_ingest.py first")
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

    if args.no_report:
        log.info("report skipped; render it with: python scripts/run_build_report.py")
    elif (
        Path(args.metrics).resolve() != METRICS_PATH.resolve()
        or Path(args.figures).resolve() != FIGURES_DIR.resolve()
    ):
        # The report builder reads the default metrics file and the default
        # figures directory. Rendering after writing either one elsewhere
        # would pair new numbers with old figures, or the reverse.
        log.info(
            "report not built: run_build_report.py reads %s and %s, and this run wrote "
            "%s and %s",
            METRICS_PATH,
            FIGURES_DIR,
            args.metrics,
            args.figures,
        )
    else:
        # Separate process on purpose: the report builder is its own entry
        # script, and a report-build failure should not read as an analysis
        # failure.
        report_script = Path(__file__).resolve().parent / "run_build_report.py"
        subprocess.run([sys.executable, str(report_script)], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
