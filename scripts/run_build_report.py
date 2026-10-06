"""Render the stakeholder report from reports/metrics.json and its figures.

    python scripts/run_build_report.py

scripts/run_diversification_analysis.py renders the report itself after
writing the metrics, so a standalone run is only needed to re-render after a
wording change. The builder is crypto_warehouse.report_generator, and this
script only calls it. It takes no arguments, and it stops with a message when
reports/metrics.json is missing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse

from crypto_warehouse.report_generator import build_report


def main() -> None:
    """Parse the command line and render the report."""
    parser = argparse.ArgumentParser(
        prog="run_build_report.py",
        description="Render the HTML and PDF report from reports/metrics.json and its figures.",
    )
    parser.parse_args()

    build_report()


if __name__ == "__main__":
    main()
