"""Cross-asset diversification analysis over marts.daily_metrics.

The question is how much of an equal-weight holder's volatility the active
assets actually remove, measured against two references: the average volatility
of holding one name, and the volatility the same variances would produce if
the assets were uncorrelated.

Everything here splits into two halves. The functions that touch the database
pull one long table of (date, symbol, log_return), one of daily closes, and
count rows for provenance. The functions that do arithmetic take arrays and
return arrays, so the math is tested without a server.

Only active assets enter. A deactivated asset keeps its history in the
warehouse but stops gaining dates, and one stopped asset would otherwise
freeze the panel's end date for all of them.

Panel rule: only dates where every active asset has a non-null log_return enter
the matrix. marts.daily_metrics already nulls log_return on incomplete days and
on days that are not adjacent to the previous observation, so this step drops
whole dates rather than filtering rows asset by asset. A covariance matrix
assembled from pairwise-available data is not a covariance matrix of any single
portfolio, and it can fail to be positive semidefinite.
"""

from __future__ import annotations

import datetime
import json
import logging
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .database_connection import REPO_ROOT, connect

logger = logging.getLogger(__name__)

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
  AND a.is_active
ORDER BY m.trade_date, a.symbol
"""

# Closes on complete days, for the drawdown and total-return path. That path
# has to follow every close the assets printed, including the days whose
# returns the panel excludes, or it describes a price history that never
# happened.
CLOSES_QUERY = """
SELECT m.trade_date, a.symbol, m.close
FROM marts.daily_metrics m
JOIN core.assets a USING (asset_id)
WHERE m.is_complete
  AND a.is_active
ORDER BY m.trade_date, a.symbol
"""

PROVENANCE_QUERY = """
WITH active AS (SELECT asset_id FROM core.assets WHERE is_active)
SELECT
    (SELECT count(*) FROM core.ohlcv JOIN active USING (asset_id)
      WHERE granularity_seconds = 3600)                                    AS hourly_rows,
    (SELECT count(*) FROM marts.daily_ohlcv JOIN active USING (asset_id)) AS daily_rows,
    (SELECT count(*) FROM marts.daily_ohlcv JOIN active USING (asset_id)
      WHERE NOT is_complete)                                              AS incomplete_asset_days,
    (SELECT count(*) FROM active)                                         AS assets_loaded,
    current_setting('server_version')                                     AS server_version
"""

# One row per date on which any active asset has a daily bar: how many assets
# have a bar that day, and how many of those bars are partial. This is what
# accounts for the gap between the observed calendar and the panel.
OBSERVED_DATES_QUERY = """
SELECT d.trade_date,
       count(*)                                  AS assets_present,
       count(*) FILTER (WHERE NOT d.is_complete) AS partial_bars
FROM marts.daily_ohlcv d
JOIN core.assets a USING (asset_id)
WHERE a.is_active
GROUP BY d.trade_date
ORDER BY d.trade_date
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
class ReturnPanel:
    """Holds daily log returns as a matrix with one row per date, one column
    per symbol, and no missing entries."""

    dates: list[datetime.date]
    symbols: list[str]
    returns: np.ndarray

    @property
    def n_days(self) -> int:
        """Return the number of dates in the panel."""
        return len(self.dates)

    @property
    def n_assets(self) -> int:
        """Return the number of symbols in the panel."""
        return len(self.symbols)


class PanelDataError(ValueError):
    """Raised when the loaded data cannot support the analysis, with a message
    saying what is missing."""


@dataclass(frozen=True)
class ClosePath:
    """Holds the daily closes on every day of the panel's window that is complete
    for all panel symbols, with one row per date and one column per symbol.

    The first row is the close the panel's first return is measured from.
    """

    dates: list[datetime.date]
    closes: np.ndarray


def build_return_panel(rows: list[tuple[datetime.date, str, float]]) -> ReturnPanel:
    """Build a ReturnPanel from (date, symbol, log_return) rows, keeping only the dates
    on which every symbol in the input has a return.

    Raises ValueError when fewer than two symbols have returns or when no date
    carries all of them.
    """
    by_date: dict[datetime.date, dict[str, float]] = defaultdict(dict)
    for trade_date, symbol, log_return in rows:
        by_date[trade_date][symbol] = float(log_return)
    symbols = sorted({symbol for _, symbol, _ in rows})
    if not symbols:
        raise PanelDataError("no non-null log returns found; load the warehouse first")
    if len(symbols) < 2:
        raise PanelDataError(
            f"only {symbols[0]} has returns; a diversification analysis needs at "
            "least two active assets"
        )
    dates = sorted(
        trade_date
        for trade_date, observations in by_date.items()
        if len(observations) == len(symbols)
    )
    if not dates:
        raise PanelDataError(
            f"no date carries all {len(symbols)} symbols; the panel would be empty"
        )

    # The panel is the intersection of every asset's dates, so one asset with
    # a shorter history shortens it for all. Warn when that happens.
    starts = {
        symbol: min(
            trade_date for trade_date, observations in by_date.items() if symbol in observations
        )
        for symbol in symbols
    }
    ends = {
        symbol: max(
            trade_date for trade_date, observations in by_date.items() if symbol in observations
        )
        for symbol in symbols
    }
    if len(set(starts.values())) > 1 or len(set(ends.values())) > 1:
        latest_start = max(starts, key=lambda symbol: starts[symbol])
        earliest_end = min(ends, key=lambda symbol: ends[symbol])
        logger.warning(
            "assets do not share one history: returns start as late as %s (%s) and "
            "end as early as %s (%s); the panel keeps only the dates all %d share",
            starts[latest_start],
            latest_start,
            ends[earliest_end],
            earliest_end,
            len(symbols),
        )

    returns = np.array(
        [[by_date[trade_date][symbol] for symbol in symbols] for trade_date in dates],
        dtype=float,
    )
    return ReturnPanel(dates=dates, symbols=symbols, returns=returns)


def build_close_path(rows: list[tuple[datetime.date, str, float]], panel: ReturnPanel) -> ClosePath:
    """Build the panel's ClosePath from (date, symbol, close) rows, keeping every
    date on which all panel symbols have a close, from the last such date
    before the panel's first return through the panel's last date.

    That includes days the panel excludes, such as the first day after a gap,
    and a ValueError is raised when no close precedes the panel's first return.
    """
    by_date: dict[datetime.date, dict[str, float]] = defaultdict(dict)
    for trade_date, symbol, close in rows:
        by_date[trade_date][symbol] = float(close)
    shared_dates = sorted(
        trade_date
        for trade_date, observations in by_date.items()
        if trade_date <= panel.dates[-1]
        and all(symbol in observations for symbol in panel.symbols)
    )
    dates_before_panel = [trade_date for trade_date in shared_dates if trade_date < panel.dates[0]]
    if not dates_before_panel:
        raise PanelDataError("no complete close precedes the panel's first return")
    dates = [
        dates_before_panel[-1],
        *(trade_date for trade_date in shared_dates if trade_date >= panel.dates[0]),
    ]
    closes = np.array(
        [[by_date[trade_date][symbol] for symbol in panel.symbols] for trade_date in dates],
        dtype=float,
    )
    return ClosePath(dates=dates, closes=closes)


def classify_excluded_dates(
    observed: list[tuple[datetime.date, int, int]], panel: ReturnPanel
) -> dict[str, list[datetime.date]]:
    """Return the observed dates the panel leaves out, grouped under partial_bar,
    not_every_asset, or after_excluded_day.

    Each observed row is (date, assets_present, partial_bars), and a date goes
    under the first reason that applies, so the last group holds days with
    every bar complete that still lack a return.
    """
    panel_dates = set(panel.dates)
    reasons: dict[str, list[datetime.date]] = {
        "partial_bar": [],
        "not_every_asset": [],
        "after_excluded_day": [],
    }
    for trade_date, assets_present, partial_bars in observed:
        if trade_date in panel_dates:
            continue
        if partial_bars > 0:
            reasons["partial_bar"].append(trade_date)
        elif assets_present < panel.n_assets:
            reasons["not_every_asset"].append(trade_date)
        else:
            reasons["after_excluded_day"].append(trade_date)
    return reasons


def annualized_volatility(returns: np.ndarray) -> np.ndarray:
    """Per-column sample standard deviation of daily log returns, annualized."""
    return returns.std(axis=0, ddof=1) * np.sqrt(DAYS_PER_YEAR)


def annualized_covariance(returns: np.ndarray) -> np.ndarray:
    """Sample covariance of daily log returns, scaled to a one-year horizon."""
    return np.cov(returns, rowvar=False, ddof=1) * DAYS_PER_YEAR


def correlation_matrix(returns: np.ndarray) -> np.ndarray:
    """Return the Pearson correlation matrix of the columns of a return matrix."""
    return np.corrcoef(returns, rowvar=False)


def off_diagonal_entries(matrix: np.ndarray) -> np.ndarray:
    """The entries of a square matrix that are not on its diagonal."""
    mask = ~np.eye(matrix.shape[0], dtype=bool)
    return matrix[mask]


# Defined here rather than in report_figures.py because the report's caption quotes the
# heatmap's color range. The bounds are computed once and stored in the
# metrics, so the caption and the image cannot state different ranges.
def round_range_outward(values: np.ndarray, step: float = 0.05) -> tuple[float, float]:
    """Return the (low, high) range of the values, rounded outward to multiples of step."""
    low = float(np.floor(values.min() / step) * step)
    high = float(np.ceil(values.max() / step) * step)
    return low, high


def fisher_interval(
    correlation: float, n_observations: int, z_score: float = 1.959963984540054
) -> tuple[float, float]:
    """Return the two-sided Fisher interval, 95% at the default z_score, for one
    Pearson correlation estimated from n_observations observations.

    The interval describes a single pair and is not a joint statement about
    several pairs, and a ValueError is raised when n_observations is 3 or less.
    """
    if n_observations <= 3:
        raise ValueError("a Fisher interval needs more than 3 observations")
    half_width = z_score / np.sqrt(n_observations - 3)
    center = np.arctanh(correlation)
    return float(np.tanh(center - half_width)), float(np.tanh(center + half_width))


def max_drawdown(returns: np.ndarray) -> np.ndarray:
    """Return the deepest peak-to-trough fall of each column's cumulative return path.

    The path starts at 1.0 before the first return, and the peak is the running
    maximum within the returns given, not an all-time high.
    """
    cumulative_path = np.exp(
        np.vstack([np.zeros(returns.shape[1]), np.cumsum(returns, axis=0)])
    )
    return (cumulative_path / np.maximum.accumulate(cumulative_path, axis=0) - 1.0).min(axis=0)


def drawdown_paths(returns: np.ndarray) -> np.ndarray:
    """The full drawdown series behind max_drawdown, one column per asset.

    Length is one row longer than the return matrix, because the path includes
    its own starting point at zero drawdown.
    """
    cumulative_path = np.exp(
        np.vstack([np.zeros(returns.shape[1]), np.cumsum(returns, axis=0)])
    )
    return cumulative_path / np.maximum.accumulate(cumulative_path, axis=0) - 1.0


def portfolio_volatility(covariance: np.ndarray, weights: np.ndarray) -> float:
    """Return the portfolio volatility sqrt(w' C w), on the same horizon as the covariance."""
    return float(np.sqrt(weights @ covariance @ weights))


def equal_weights(n_assets: int) -> np.ndarray:
    """Return a vector of n_assets weights that each equal 1/n_assets."""
    return np.full(n_assets, 1.0 / n_assets)


def equal_weight_diversification(covariance: np.ndarray) -> dict[str, float]:
    """Compare an equal-weight portfolio's volatility with two references and
    return the resulting figures as a dict.

    The references are the average single-asset volatility and the volatility
    of the same variances with every correlation set to zero.
    """
    n_assets = covariance.shape[0]
    weights = equal_weights(n_assets)
    asset_vols = np.sqrt(np.diag(covariance))
    portfolio_vol = portfolio_volatility(covariance, weights)
    average_vol = float(asset_vols.mean())
    # Zero correlation is the reference because long spot positions in one
    # sector do not supply negative covariance. A portfolio with negative
    # covariances falls below it, which test_diversification_metrics.py checks.
    independent_vol = portfolio_volatility(np.diag(np.diag(covariance)), weights)
    risk_removed_actual = 1.0 - portfolio_vol / average_vol
    risk_removed_if_uncorrelated = 1.0 - independent_vol / average_vol

    # For equal volatilities sigma and a shared correlation rho, equal-weight
    # variance is sigma^2 * (1/n_assets + (1 - 1/n_assets) * rho), so volatility
    # tends to sigma * sqrt(rho) as n_assets grows. The report quotes this limit
    # to show what adding more names of the same kind could remove.
    correlations = covariance / np.outer(asset_vols, asset_vols)
    off_diagonal_values = correlations[np.triu_indices(n_assets, k=1)]
    mean_correlation = float(off_diagonal_values.mean())
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


def panel_metrics(panel: ReturnPanel, close_path: ClosePath) -> dict:
    """Compute the panel, close-path, per-asset, correlation, and diversification
    figures the report reads and return them as a dict.

    Raises PanelDataError when the panel is too short for a correlation
    interval, or when an asset's returns do not vary over it.
    """
    if panel.n_days <= 3:
        raise PanelDataError(
            f"the panel has {panel.n_days} day(s) where every asset has a return; "
            "the analysis needs at least 4, so load more history first"
        )
    returns = panel.returns
    volatilities = annualized_volatility(returns)
    constant_symbols = [
        symbol
        for symbol, volatility in zip(panel.symbols, volatilities, strict=True)
        if volatility == 0.0
    ]
    if constant_symbols:
        raise PanelDataError(
            f"{', '.join(constant_symbols)} did not move over the panel; a correlation with "
            "a constant price is undefined"
        )
    # Drawdown and total return follow the closes, not the panel's returns.
    # Summing panel returns would cut the move across every excluded date out
    # of the path.
    close_changes = np.diff(np.log(close_path.closes), axis=0)
    drawdowns = max_drawdown(close_changes)
    total_log_returns = close_changes.sum(axis=0)
    correlations = correlation_matrix(returns)
    off_diagonal_values = off_diagonal_entries(correlations)

    # Which pair is least and which is most correlated, named rather than left as
    # bare numbers the reader has to look for in the matrix.
    upper_triangle = np.triu_indices(panel.n_assets, k=1)
    pair_correlations = correlations[upper_triangle]
    least_correlated_index = int(np.argmin(pair_correlations))
    most_correlated_index = int(np.argmax(pair_correlations))

    def pair_name(index: int) -> list[str]:
        """Return the two symbols of the pair at a position in the upper triangle."""
        return [panel.symbols[upper_triangle[0][index]], panel.symbols[upper_triangle[1][index]]]

    mean_interval_low, mean_interval_high = fisher_interval(
        float(off_diagonal_values.mean()), panel.n_days
    )
    scale_low, scale_high = round_range_outward(off_diagonal_values)

    return {
        "panel": {
            "first_date": panel.dates[0].isoformat(),
            "last_date": panel.dates[-1].isoformat(),
            "n_days": panel.n_days,
            "n_assets": panel.n_assets,
            "symbols": list(panel.symbols),
        },
        "close_path": {
            "first_date": close_path.dates[0].isoformat(),
            "last_date": close_path.dates[-1].isoformat(),
            "n_closes": len(close_path.dates),
        },
        "assets": [
            {
                "symbol": symbol,
                "annualized_volatility": float(volatilities[i]),
                "max_drawdown": float(drawdowns[i]),
                "total_log_return": float(total_log_returns[i]),
            }
            for i, symbol in enumerate(panel.symbols)
        ],
        "correlation": {
            "symbols": list(panel.symbols),
            "matrix": [[float(coefficient) for coefficient in row] for row in correlations],
            "off_diagonal_min": float(off_diagonal_values.min()),
            "off_diagonal_max": float(off_diagonal_values.max()),
            "off_diagonal_mean": float(off_diagonal_values.mean()),
            "mean_ci95_low": mean_interval_low,
            "mean_ci95_high": mean_interval_high,
            "heatmap_scale_low": scale_low,
            "heatmap_scale_high": scale_high,
            "least_correlated_pair": pair_name(least_correlated_index),
            "most_correlated_pair": pair_name(most_correlated_index),
            "n_pairs": len(pair_correlations),
        },
        "diversification": equal_weight_diversification(annualized_covariance(returns)),
    }


def fetch_returns(connection) -> list[tuple[datetime.date, str, float]]:
    """Every non-null daily log return of every active asset, as (date, symbol, value)."""
    with connection.cursor() as cursor:
        cursor.execute(RETURNS_QUERY)
        return [(row[0], row[1], float(row[2])) for row in cursor.fetchall()]


def fetch_closes(connection) -> list[tuple[datetime.date, str, float]]:
    """Every complete-day close of every active asset, as (date, symbol, value)."""
    with connection.cursor() as cursor:
        cursor.execute(CLOSES_QUERY)
        return [(row[0], row[1], float(row[2])) for row in cursor.fetchall()]


def fetch_provenance(connection, panel: ReturnPanel) -> dict:
    """Row counts, the excluded dates with their reasons, and the mart's own correlations."""
    with connection.cursor() as cursor:
        cursor.execute(PROVENANCE_QUERY)
        hourly_rows, daily_rows, incomplete_asset_days, assets_loaded, server_version = (
            cursor.fetchone()
        )
        cursor.execute(OBSERVED_DATES_QUERY)
        observed = [(row[0], int(row[1]), int(row[2])) for row in cursor.fetchall()]
        cursor.execute(MART_CORRELATION_QUERY)
        (
            mart_min_correlation,
            mart_max_correlation,
            mart_mean_correlation,
            mart_n_pairs,
            mart_n_days_min,
            mart_n_days_max,
        ) = cursor.fetchone()
    reasons = classify_excluded_dates(observed, panel)
    excluded_dates = sorted(trade_date for dates in reasons.values() for trade_date in dates)
    return {
        "hourly_candle_rows": int(hourly_rows),
        "daily_ohlcv_rows": int(daily_rows),
        "incomplete_asset_days": int(incomplete_asset_days),
        "observed_dates": len(observed),
        "dates_dropped_from_panel": len(excluded_dates),
        "incomplete_dates": [trade_date.isoformat() for trade_date in reasons["partial_bar"]],
        "dates_without_every_asset": [
            trade_date.isoformat() for trade_date in reasons["not_every_asset"]
        ],
        "dates_after_an_excluded_day": [
            trade_date.isoformat() for trade_date in reasons["after_excluded_day"]
        ],
        "excluded_dates": [trade_date.isoformat() for trade_date in excluded_dates],
        "assets_loaded": int(assets_loaded),
        "server_version": str(server_version),
        "mart_correlations_90d": {
            "note": "trailing 90-day window from marts.pairwise_correlations_90d, "
            "not the complete-day panel this report is built on",
            "min": float(mart_min_correlation),
            "max": float(mart_max_correlation),
            "mean": float(mart_mean_correlation),
            "n_pairs": int(mart_n_pairs),
            "n_days_min": int(mart_n_days_min),
            "n_days_max": int(mart_n_days_max),
        },
    }


def run_diversification_analysis(
    metrics_path: Path = METRICS_PATH, figures_dir: Path = FIGURES_DIR
) -> dict:
    """Query the warehouse, write metrics.json and the figures, return metrics."""
    from . import report_figures

    with closing(connect()) as connection:
        panel = build_return_panel(fetch_returns(connection))
        close_path = build_close_path(fetch_closes(connection), panel)
        metrics = panel_metrics(panel, close_path)
        metrics["provenance"] = fetch_provenance(connection, panel)

    metrics["generated_at_utc"] = (
        datetime.datetime.now(datetime.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    logger.info(
        "panel: %d complete days, %s to %s, %d assets",
        panel.n_days,
        metrics["panel"]["first_date"],
        metrics["panel"]["last_date"],
        panel.n_assets,
    )

    figures_dir.mkdir(parents=True, exist_ok=True)
    figure_paths = report_figures.write_figures(panel, close_path, metrics, figures_dir)
    for path in figure_paths:
        logger.info("wrote %s", path)

    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s", metrics_path)
    return metrics
