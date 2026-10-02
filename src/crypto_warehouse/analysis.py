"""Cross-asset diversification analysis over marts.daily_metrics.

The question is how much of an equal-weight holder's volatility the six loaded
assets actually remove, measured against two references: the average volatility
of holding one name, and the volatility the same six variances would produce if
the assets were uncorrelated.

Everything here splits into two halves. The functions that touch the database
pull one long table of (date, symbol, log_return) and count rows for
provenance. The functions that do arithmetic take arrays and return arrays, so
the math is tested without a server.

Panel rule: only dates where all six assets carry a non-null log_return enter
the matrix. marts.daily_metrics already nulls log_return on incomplete days and
on days that are not adjacent to the previous observation, so this step drops
whole dates rather than filtering rows asset by asset. A covariance matrix
assembled from pairwise-available data is not a covariance matrix of any single
portfolio, and it can fail to be positive semidefinite.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import REPO_ROOT, connect

log = logging.getLogger(__name__)

REPORTS_DIR = REPO_ROOT / "reports"
FIGURES_DIR = REPORTS_DIR / "figures"
METRICS_PATH = REPORTS_DIR / "metrics.json"

# Crypto trades every calendar day, so a year is 365 observations rather than
# the 252 exchange sessions an equity volatility is annualized with.
DAYS_PER_YEAR = 365

RETURNS_QUERY = """
SELECT m.trade_date, a.symbol, m.log_return
FROM marts.daily_metrics m
JOIN core.assets a USING (asset_id)
WHERE m.log_return IS NOT NULL
ORDER BY m.trade_date, a.symbol
"""

PROVENANCE_QUERY = """
SELECT
    (SELECT count(*) FROM core.ohlcv_hourly)                             AS hourly_rows,
    (SELECT count(*) FROM marts.daily_ohlcv)                             AS daily_rows,
    (SELECT count(*) FROM marts.daily_ohlcv WHERE NOT is_complete)       AS incomplete_asset_days,
    (SELECT count(DISTINCT trade_date) FROM marts.daily_ohlcv)           AS observed_dates,
    (SELECT count(*) FROM core.assets)                                   AS assets_loaded,
    current_setting('server_version')                                    AS server_version
"""

# Dates carrying at least one partial bar, and every date the rollup observed.
# Both are needed to account for the gap between the observed calendar and the
# panel: an excluded date is either incomplete itself or the first observation
# after one, which the mart's adjacency guard nulls.
INCOMPLETE_DATES_QUERY = """
SELECT DISTINCT trade_date FROM marts.daily_ohlcv WHERE NOT is_complete ORDER BY trade_date
"""

OBSERVED_DATES_QUERY = """
SELECT DISTINCT trade_date FROM marts.daily_ohlcv ORDER BY trade_date
"""

# The mart's own correlation figures, kept for one comparison sentence in the
# report. It is a trailing 90-day window, which is not the panel this analysis
# uses, and quoting it without saying so would mix two bases.
MART_CORRELATION_QUERY = """
SELECT min(return_correlation), max(return_correlation), avg(return_correlation),
       count(*), min(n_days), max(n_days)
FROM marts.pairwise_correlations_90d
"""


@dataclass(frozen=True)
class Panel:
    """A rectangular block of daily log returns: one row per date, one column
    per symbol, no missing entries anywhere."""

    dates: list[dt.date]
    symbols: list[str]
    returns: np.ndarray

    @property
    def n_days(self) -> int:
        return len(self.dates)

    @property
    def n_assets(self) -> int:
        return len(self.symbols)


def build_panel(rows: list[tuple[dt.date, str, float]]) -> Panel:
    """Turn the long (date, symbol, log_return) table into a complete panel.

    A date survives only when every symbol seen anywhere in the input has a
    return on it. Dropping a date costs one observation for every asset;
    keeping it with a hole costs the covariance matrix its meaning.
    """
    by_date: dict[dt.date, dict[str, float]] = defaultdict(dict)
    for trade_date, symbol, value in rows:
        by_date[trade_date][symbol] = float(value)
    symbols = sorted({symbol for _, symbol, _ in rows})
    if not symbols:
        raise ValueError("no non-null log returns found; load the warehouse first")
    dates = sorted(d for d, obs in by_date.items() if len(obs) == len(symbols))
    if not dates:
        raise ValueError(f"no date carries all {len(symbols)} symbols; the panel would be empty")
    returns = np.array([[by_date[d][s] for s in symbols] for d in dates], dtype=float)
    return Panel(dates=dates, symbols=symbols, returns=returns)


def annualized_volatility(returns: np.ndarray) -> np.ndarray:
    """Per-column sample standard deviation of daily log returns, annualized."""
    return returns.std(axis=0, ddof=1) * np.sqrt(DAYS_PER_YEAR)


def annualized_covariance(returns: np.ndarray) -> np.ndarray:
    """Sample covariance of daily log returns, scaled to a one-year horizon."""
    return np.cov(returns, rowvar=False, ddof=1) * DAYS_PER_YEAR


def correlation_matrix(returns: np.ndarray) -> np.ndarray:
    return np.corrcoef(returns, rowvar=False)


def off_diagonal(matrix: np.ndarray) -> np.ndarray:
    """The entries of a square matrix that are not on its diagonal."""
    mask = ~np.eye(matrix.shape[0], dtype=bool)
    return matrix[mask]


def scale_bounds(values: np.ndarray, step: float = 0.05) -> tuple[float, float]:
    """Round a data range outward to the nearest step.

    Lives here rather than in plots.py because the heatmap's color axis is
    quoted in the report's caption, and a caption that says one range while the
    image shows another is worse than no caption.
    """
    lo = float(np.floor(values.min() / step) * step)
    hi = float(np.ceil(values.max() / step) * step)
    return lo, hi


def fisher_interval(r: float, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Two-sided 95% interval for one Pearson correlation from n observations.

    Fisher's transform, so the interval is asymmetric near the ends of the
    range instead of running past 1. This describes a single pair; it is not a
    joint statement about all pairs at once.
    """
    if n <= 3:
        raise ValueError("a Fisher interval needs more than 3 observations")
    half_width = z / np.sqrt(n - 3)
    center = np.arctanh(r)
    return float(np.tanh(center - half_width)), float(np.tanh(center + half_width))


def max_drawdown(returns: np.ndarray) -> np.ndarray:
    """Deepest peak-to-trough fall of each column's cumulative return path.

    The path starts at 1.0 on the day before the first return, so an asset that
    falls from the first observation onward records that fall. The peak is the
    running maximum within the panel, not an all-time high.
    """
    level = np.exp(np.vstack([np.zeros(returns.shape[1]), np.cumsum(returns, axis=0)]))
    return (level / np.maximum.accumulate(level, axis=0) - 1.0).min(axis=0)


def drawdown_paths(returns: np.ndarray) -> np.ndarray:
    """The full drawdown series behind max_drawdown, one column per asset.

    Length is one row longer than the return matrix, because the path includes
    its own starting point at zero drawdown.
    """
    level = np.exp(np.vstack([np.zeros(returns.shape[1]), np.cumsum(returns, axis=0)]))
    return level / np.maximum.accumulate(level, axis=0) - 1.0


def portfolio_volatility(covariance: np.ndarray, weights: np.ndarray) -> float:
    """sqrt(w' C w). Volatility on whatever horizon the covariance carries."""
    return float(np.sqrt(weights @ covariance @ weights))


def equal_weights(n: int) -> np.ndarray:
    return np.full(n, 1.0 / n)


def diversification(covariance: np.ndarray) -> dict[str, float]:
    """Compare an equal-weight portfolio against two references.

    The first reference is the average volatility of the individual assets,
    which is what an investor gives up single-name risk to improve on. The
    second holds every variance fixed and sets all correlations to zero. That
    is a benchmark, not a floor: negative covariances put a portfolio below it,
    which is what a hedge is for, and test_analysis.py checks that the
    arithmetic behaves that way. Six long spot positions in one sector will not
    supply negative covariance, so independence is the right reference here.
    Reporting the ratio of the two says how much of the available benefit the
    real correlations leave on the table.
    """
    n = covariance.shape[0]
    weights = equal_weights(n)
    asset_vols = np.sqrt(np.diag(covariance))
    portfolio_vol = portfolio_volatility(covariance, weights)
    average_vol = float(asset_vols.mean())
    independent_vol = portfolio_volatility(np.diag(np.diag(covariance)), weights)
    risk_removed_actual = 1.0 - portfolio_vol / average_vol
    risk_removed_if_uncorrelated = 1.0 - independent_vol / average_vol

    # Where an equal-weight basket of similar assets is headed as more names are
    # added. For equal volatilities and a shared correlation rho, portfolio
    # variance is sigma^2 * (1/n + (1 - 1/n) * rho), so as n grows the 1/n terms
    # vanish and volatility approaches sigma * sqrt(rho). Adding names past that
    # buys nothing; only a fall in rho does. Quoted so the report does not have
    # to assert "a seventh name would not help" without arithmetic behind it.
    correlation = covariance / np.outer(asset_vols, asset_vols)
    off_diagonal = correlation[np.triu_indices(n, k=1)]
    mean_correlation = float(off_diagonal.mean())
    equicorrelation_limit = average_vol * float(np.sqrt(max(mean_correlation, 0.0)))

    return {
        "weight_per_asset": float(weights[0]),
        "equal_weight_portfolio_vol": portfolio_vol,
        "average_single_asset_vol": average_vol,
        "zero_correlation_vol": independent_vol,
        "risk_removed_actual": risk_removed_actual,
        "risk_removed_if_uncorrelated": risk_removed_if_uncorrelated,
        "diversification_captured": risk_removed_actual / risk_removed_if_uncorrelated,
        "equicorrelation_limit_vol": equicorrelation_limit,
    }


def panel_metrics(panel: Panel) -> dict:
    """Everything the report needs that comes out of the return matrix alone."""
    returns = panel.returns
    vols = annualized_volatility(returns)
    drawdowns = max_drawdown(returns)
    totals = returns.sum(axis=0)
    corr = correlation_matrix(returns)
    off = off_diagonal(corr)

    # Which pair is least and which is most correlated, named rather than left as
    # bare numbers the reader has to hunt for in the matrix.
    upper = np.triu_indices(panel.n_assets, k=1)
    pair_values = corr[upper]
    least_correlated = int(np.argmin(pair_values))
    most_correlated = int(np.argmax(pair_values))

    def pair_name(index: int) -> list[str]:
        return [panel.symbols[upper[0][index]], panel.symbols[upper[1][index]]]

    mean_low, mean_high = fisher_interval(float(off.mean()), panel.n_days)
    scale_low, scale_high = scale_bounds(off)

    return {
        "panel": {
            "first_date": panel.dates[0].isoformat(),
            "last_date": panel.dates[-1].isoformat(),
            "n_days": panel.n_days,
            "n_assets": panel.n_assets,
            "symbols": list(panel.symbols),
        },
        "assets": [
            {
                "symbol": symbol,
                "annualized_volatility": float(vols[i]),
                "max_drawdown": float(drawdowns[i]),
                "total_log_return": float(totals[i]),
            }
            for i, symbol in enumerate(panel.symbols)
        ],
        "correlation": {
            "symbols": list(panel.symbols),
            "matrix": [[float(v) for v in row] for row in corr],
            "off_diagonal_min": float(off.min()),
            "off_diagonal_max": float(off.max()),
            "off_diagonal_mean": float(off.mean()),
            "mean_ci95_low": mean_low,
            "mean_ci95_high": mean_high,
            "heatmap_scale_low": scale_low,
            "heatmap_scale_high": scale_high,
            "least_correlated_pair": pair_name(least_correlated),
            "most_correlated_pair": pair_name(most_correlated),
            "n_pairs": len(pair_values),
        },
        "diversification": diversification(annualized_covariance(returns)),
    }


def fetch_returns(conn) -> list[tuple[dt.date, str, float]]:
    with conn.cursor() as cur:
        cur.execute(RETURNS_QUERY)
        return [(row[0], row[1], float(row[2])) for row in cur.fetchall()]


def fetch_provenance(conn, panel: Panel) -> dict:
    with conn.cursor() as cur:
        cur.execute(PROVENANCE_QUERY)
        hourly, daily, incomplete, observed_dates, assets, server_version = cur.fetchone()
        cur.execute(INCOMPLETE_DATES_QUERY)
        incomplete_dates = [row[0] for row in cur.fetchall()]
        cur.execute(OBSERVED_DATES_QUERY)
        all_dates = [row[0] for row in cur.fetchall()]
        cur.execute(MART_CORRELATION_QUERY)
        c_min, c_max, c_mean, n_pairs, days_min, days_max = cur.fetchone()
    in_panel = set(panel.dates)
    excluded = [d for d in all_dates if d not in in_panel]
    return {
        "ohlcv_hourly_rows": int(hourly),
        "daily_ohlcv_rows": int(daily),
        "incomplete_asset_days": int(incomplete),
        "observed_dates": int(observed_dates),
        "dates_dropped_from_panel": int(observed_dates) - panel.n_days,
        "incomplete_dates": [d.isoformat() for d in incomplete_dates],
        "excluded_dates": [d.isoformat() for d in excluded],
        "assets_loaded": int(assets),
        "server_version": str(server_version),
        "mart_correlations_90d": {
            "note": "trailing 90-day window from marts.pairwise_correlations_90d, "
            "not the complete-day panel this report is built on",
            "min": float(c_min),
            "max": float(c_max),
            "mean": float(c_mean),
            "n_pairs": int(n_pairs),
            "n_days_min": int(days_min),
            "n_days_max": int(days_max),
        },
    }


def run_analysis(metrics_path: Path = METRICS_PATH, figures_dir: Path = FIGURES_DIR) -> dict:
    """Query the warehouse, write metrics.json and the figures, return metrics."""
    from . import plots

    with closing(connect()) as conn:
        rows = fetch_returns(conn)
        panel = build_panel(rows)
        metrics = panel_metrics(panel)
        metrics["provenance"] = fetch_provenance(conn, panel)

    metrics["generated_at_utc"] = (
        dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    log.info(
        "panel: %d complete days, %s to %s, %d assets",
        panel.n_days,
        metrics["panel"]["first_date"],
        metrics["panel"]["last_date"],
        panel.n_assets,
    )

    figures_dir.mkdir(parents=True, exist_ok=True)
    written = plots.write_all(panel, metrics, figures_dir)
    for path in written:
        log.info("wrote %s", path)

    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    log.info("wrote %s", metrics_path)
    return metrics
