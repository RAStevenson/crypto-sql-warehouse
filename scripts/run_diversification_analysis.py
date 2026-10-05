"""Measure cross-asset diversification, write reports/metrics.json, and
render the report.

    python scripts/run_ingest.py
    python scripts/run_diversification_analysis.py

Reads marts.daily_metrics, keeps the dates where every active asset has a
non-null log return, and writes the panel description, per-asset volatility,
drawdown and total return from the complete-day closes, the correlation
matrix, the equal-weight diversification result, and row counts for
provenance to reports/metrics.json, plus four figures under reports/figures/. It then calls
the report builder in crypto_warehouse.report_generator, which reads that file
at render time; nothing is transcribed by hand between the two. --no-report
skips the render, and so does a --metrics or --figures path other than the
default. scripts/run_build_report.py runs the builder alone, for iterating on
the report's prose without touching the database.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse
import logging

import psycopg2
import psycopg2.errors

from crypto_warehouse.database_connection import ConfigError, configure_logging
from crypto_warehouse.diversification_metrics import (
    FIGURES_DIR,
    METRICS_PATH,
    PanelDataError,
    run_analysis,
)
from crypto_warehouse.report_generator import build_report

log = logging.getLogger("run_diversification_analysis")


def main() -> int:
    """Run the analysis, log the main results, render the report when both
    output paths are the defaults, and return a process exit code."""
    parser = argparse.ArgumentParser(
        prog="run_diversification_analysis.py",
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
    except psycopg2.OperationalError:
        log.error("could not reach the database", exc_info=True)
        log.error(
            "nothing was written; start the server or fix DATABASE_URL in .env, then rerun"
        )
        return 1
    except psycopg2.errors.UndefinedTable:
        log.error("a warehouse table is missing", exc_info=True)
        log.error(
            "nothing was written; run scripts/run_setup.py and scripts/run_ingest.py first"
        )
        return 1
    except PanelDataError as exc:
        log.error("%s", exc)
        log.error("nothing was written")
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
            "report not built: the report builder reads %s and %s, and this run wrote "
            "%s and %s",
            METRICS_PATH,
            FIGURES_DIR,
            args.metrics,
            args.figures,
        )
    else:
        # A report-build failure should not read as an analysis failure, so
        # it is caught here and logged as its own error. SystemExit is caught
        # too, because the builder exits with a message when a figure or table
        # is not cited.
        try:
            build_report()
        except (Exception, SystemExit):
            log.error("the report build failed", exc_info=True)
            log.error(
                "the metrics and figures were written, but the report was not; fix the "
                "cause above and rerun python scripts/run_build_report.py"
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
