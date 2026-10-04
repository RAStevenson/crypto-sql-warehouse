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
    (SELECT count(*) FROM core.ohlcv_hourly JOIN active USING (asset_id)) AS hourly_rows,
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


@dataclass(frozen=True)
class ClosePath:
    """Daily closes on every complete day of the panel's window: one row per
    date, one column per panel symbol. The first row is the close the panel's
    first return is measured from."""

    dates: list[dt.date]
    closes: np.ndarray


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
    if len(symbols) < 2:
        raise ValueError(
            f"only {symbols[0]} has returns; a diversification analysis needs at "
            "least two active assets"
        )
    dates = sorted(d for d, obs in by_date.items() if len(obs) == len(symbols))
    if not dates:
        raise ValueError(f"no date carries all {len(symbols)} symbols; the panel would be empty")

    # The panel is the intersection of every asset's dates, so one asset with
    # a shorter history shortens it for all. Say so when that happens.
    starts = {s: min(d for d, obs in by_date.items() if s in obs) for s in symbols}
    ends = {s: max(d for d, obs in by_date.items() if s in obs) for s in symbols}
    if len(set(starts.values())) > 1 or len(set(ends.values())) > 1:
        latest_start = max(starts, key=lambda s: starts[s])
        earliest_end = min(ends, key=lambda s: ends[s])
        log.warning(
            "assets do not share one history: returns start as late as %s (%s) and "
            "end as early as %s (%s); the panel keeps only the dates all %d share",
            starts[latest_start],
            latest_start,
            ends[earliest_end],
            earliest_end,
            len(symbols),
        )

    returns = np.array([[by_date[d][s] for s in symbols] for d in dates], dtype=float)
    return Panel(dates=dates, symbols=symbols, returns=returns)


def build_close_path(rows: list[tuple[dt.date, str, float]], panel: Panel) -> ClosePath:
    """Turn the long (date, symbol, close) table into the panel window's close path.

    Keeps every date on which all panel symbols have a complete close, from
    the last such date before the panel's first return through the panel's
    last date. That includes complete days the panel excludes, such as the
    first day after a gap, whose return is undefined but whose close is real.
    """
    by_date: dict[dt.date, dict[str, float]] = defaultdict(dict)
    for trade_date, symbol, value in rows:
        by_date[trade_date][symbol] = float(value)
    shared = sorted(
        d
        for d, obs in by_date.items()
        if d <= panel.dates[-1] and all(s in obs for s in panel.symbols)
    )
    before = [d for d in shared if d < panel.dates[0]]
    if not before:
        raise ValueError("no complete close precedes the panel's first return")
    dates = [before[-1], *(d for d in shared if d >= panel.dates[0])]
    closes = np.array([[by_date[d][s] for s in panel.symbols] for d in dates], dtype=float)
    return ClosePath(dates=dates, closes=closes)


def classify_excluded_dates(
    observed: list[tuple[dt.date, int, int]], panel: Panel
) -> dict[str, list[dt.date]]:
    """Sort every observed date the panel leaves out into the reason it is out.

    observed holds one (date, assets_present, partial_bars) row per date. A
    date with a partial bar is listed under partial_bar. A date on which not
    every panel asset has a bar is listed under not_every_asset. Whatever is
    left has every bar complete and still lacks a return for some asset, which
    happens on the first observation after an excluded day.
    """
    in_panel = set(panel.dates)
    reasons: dict[str, list[dt.date]] = {
        "partial_bar": [],
        "not_every_asset": [],
        "after_excluded_day": [],
    }
    for trade_date, assets_present, partial_bars in observed:
        if trade_date in in_panel:
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
    running maximum within the path it is given, not an all-time high.
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
    second holds every variance fixed and sets all correlations to zero.
    Negative covariances put a portfolio below that second reference, which is
    what a hedge is for, and test_analysis.py checks that the arithmetic
    behaves that way. Long spot positions in one sector do not supply negative
    covariance, so zero correlation is the right reference here.
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


def panel_metrics(panel: Panel, close_path: ClosePath) -> dict:
    """Everything the report needs that comes out of the returns and closes alone."""
    returns = panel.returns
    vols = annualized_volatility(returns)
    constant = [s for s, v in zip(panel.symbols, vols, strict=True) if v == 0.0]
    if constant:
        raise ValueError(
            f"{', '.join(constant)} did not move over the panel; a correlation with "
            "a constant price is undefined"
        )
    # Drawdown and total return follow the closes, not the panel's returns.
    # Summing panel returns would cut the move across every excluded date out
    # of the path.
    close_changes = np.diff(np.log(close_path.closes), axis=0)
    drawdowns = max_drawdown(close_changes)
    totals = close_changes.sum(axis=0)
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
        "close_path": {
            "first_date": close_path.dates[0].isoformat(),
            "last_date": close_path.dates[-1].isoformat(),
            "n_closes": len(close_path.dates),
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
    """Every non-null daily log return of every active asset, as (date, symbol, value)."""
    with conn.cursor() as cur:
        cur.execute(RETURNS_QUERY)
        return [(row[0], row[1], float(row[2])) for row in cur.fetchall()]


def fetch_closes(conn) -> list[tuple[dt.date, str, float]]:
    """Every complete-day close of every active asset, as (date, symbol, value)."""
    with conn.cursor() as cur:
        cur.execute(CLOSES_QUERY)
        return [(row[0], row[1], float(row[2])) for row in cur.fetchall()]


def fetch_provenance(conn, panel: Panel) -> dict:
    """Row counts, the excluded dates with their reasons, and the mart's own correlations."""
    with conn.cursor() as cur:
        cur.execute(PROVENANCE_QUERY)
        hourly, daily, incomplete, assets, server_version = cur.fetchone()
        cur.execute(OBSERVED_DATES_QUERY)
        observed = [(row[0], int(row[1]), int(row[2])) for row in cur.fetchall()]
        cur.execute(MART_CORRELATION_QUERY)
        c_min, c_max, c_mean, n_pairs, days_min, days_max = cur.fetchone()
    reasons = classify_excluded_dates(observed, panel)
    excluded = sorted(d for dates in reasons.values() for d in dates)
    return {
        "ohlcv_hourly_rows": int(hourly),
        "daily_ohlcv_rows": int(daily),
        "incomplete_asset_days": int(incomplete),
        "observed_dates": len(observed),
        "dates_dropped_from_panel": len(excluded),
        "incomplete_dates": [d.isoformat() for d in reasons["partial_bar"]],
        "dates_without_every_asset": [d.isoformat() for d in reasons["not_every_asset"]],
        "dates_after_an_excluded_day": [d.isoformat() for d in reasons["after_excluded_day"]],
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
        panel = build_panel(fetch_returns(conn))
        close_path = build_close_path(fetch_closes(conn), panel)
        metrics = panel_metrics(panel, close_path)
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
    written = plots.write_all(panel, close_path, metrics, figures_dir)
    for path in written:
        log.info("wrote %s", path)

    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    log.info("wrote %s", metrics_path)
    return metrics
