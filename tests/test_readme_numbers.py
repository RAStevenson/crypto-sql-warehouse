"""The README's diversification numbers match the committed results/metrics.json.

In this repo the README is the only place those numbers are written in prose,
and each was typed from the metrics file. These tests read the file as git
last committed it, so a reader who reruns scripts/run_diversification_analysis.py
on their own load does not fail the suite, while a commit that updates the
metrics file without the README does. Outside a git checkout the working file
is read instead.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from crypto_warehouse.database_connection import REPO_ROOT
from crypto_warehouse.diversification_metrics import METRICS_PATH

README_PATH = REPO_ROOT / "README.md"


def _readme_text() -> str:
    """The README with its line breaks folded to spaces, so a phrase that
    wraps across two lines still matches."""
    return " ".join(README_PATH.read_text(encoding="utf-8").split())


def _committed_metrics() -> dict:
    """The metrics file as git last committed it, or the working file when
    git cannot answer, such as outside a checkout."""
    relative = METRICS_PATH.relative_to(REPO_ROOT).as_posix()
    try:
        committed = subprocess.run(
            ["git", "show", f"HEAD:{relative}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        committed = METRICS_PATH.read_text(encoding="utf-8")
    return json.loads(committed)


def _expected_phrases() -> list[str]:
    """Every README phrase that carries a number from metrics.json, built from
    the file in the form the README prints it."""
    metrics = _committed_metrics()
    panel = metrics["panel"]
    correlation = metrics["correlation"]
    diversification = metrics["diversification"]
    assets = {asset["symbol"]: asset for asset in metrics["assets"]}
    smallest_drawdown = min(abs(asset["max_drawdown"]) for asset in metrics["assets"])
    return [
        f"{panel['n_days']:,} complete trading days from {panel['first_date']} "
        f"to {panel['last_date']}",
        f"{panel['n_days']:,} days from {panel['first_date']} to {panel['last_date']}",
        f"only {100 * diversification['risk_removed_actual']:.1f} percent of the volatility",
        f"it would remove {100 * diversification['risk_removed_if_uncorrelated']:.1f} percent",
        f"between {correlation['off_diagonal_min']:.2f} and "
        f"{correlation['off_diagonal_max']:.2f}, averaging {correlation['off_diagonal_mean']:.2f}",
        f"volatility is {diversification['equal_weight_portfolio_vol']:.3f}, against "
        f"{diversification['average_single_asset_vol']:.3f} for the average single asset",
        f"With zero correlation it would be {diversification['zero_correlation_vol']:.3f}",
        f"deliver {100 * diversification['diversification_captured']:.1f} percent of the reduction",
        f"Bitcoin alone, at {assets['BTC']['annualized_volatility']:.3f}",
        f"at least {100 * smallest_drawdown:.1f} percent below its highest close "
        f"since {metrics['close_path']['first_date']}",
        f"the {100 * diversification['risk_removed_actual']:.1f} percent is more likely too high",
        f"near the {correlation['off_diagonal_mean']:.2f} average has a 95 percent confidence "
        f"interval from {correlation['mean_ci95_low']:.2f} to {correlation['mean_ci95_high']:.2f}",
        f"differ by less than about "
        f"{correlation['mean_ci95_high'] - correlation['mean_ci95_low']:.2f} have overlapping",
        f"the {100 * diversification['risk_removed_actual']:.2f} percent the six actually remove "
        f"divided by the {100 * diversification['risk_removed_if_uncorrelated']:.2f} percent",
        f"reaches back past {metrics['close_path']['first_date']}",
        f"published one, which starts {panel['first_date']}",
    ]


@pytest.mark.parametrize("phrase", _expected_phrases())
def test_the_readme_prints_this_number_as_metrics_json_holds_it(phrase):
    assert phrase in _readme_text()


def test_the_readme_chart_is_the_one_the_analysis_writes():
    assert "](results/figures/diversification.png)" in README_PATH.read_text(encoding="utf-8")
    assert (REPO_ROOT / "results" / "figures" / "diversification.png").is_file()
