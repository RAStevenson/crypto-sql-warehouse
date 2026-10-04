"""Report figures.

Four plots, all on a light surface at print-legible sizes. The categorical
palette is Okabe-Ito, which stays distinguishable under the common forms of
color vision deficiency, and every bar carries its own value as text so a
reader who cannot separate two hues can still read the number.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.figure import Figure

# Okabe-Ito, in a fixed order so an asset keeps its color across regenerations.
SERIES = ["#0072b2", "#d55e00", "#009e73", "#e69f00", "#cc79a7", "#56b4e9"]
INK = "#0b0b0b"
SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#fcfcfb"
BLANK = "#e8e7e1"

CORRELATION_HEATMAP = "correlation_heatmap.png"
ASSET_VOLATILITY = "asset_volatility.png"
DRAWDOWN_CURVES = "drawdown_curves.png"
DIVERSIFICATION = "diversification.png"


def apply_style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "axes.edgecolor": AXIS,
            "axes.linewidth": 1.0,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "axes.axisbelow": True,
            "text.color": INK,
            "axes.labelcolor": SECONDARY,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.labelcolor": SECONDARY,
            "ytick.labelcolor": SECONDARY,
            "font.family": "sans-serif",
            "font.sans-serif": ["Segoe UI", "DejaVu Sans", "sans-serif"],
            "font.size": 10,
            "legend.frameon": False,
            "figure.dpi": 150,
        }
    )


def _save(fig: Figure, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def correlation_heatmap(
    symbols: list[str], corr: np.ndarray, vmin: float, vmax: float, path: Path
) -> Path:
    """Annotated heatmap on a scale fitted to the observed range.

    A -1 to 1 scale would render every cell here in nearly the same tone, so
    the color axis runs over the observed off-diagonal range rounded outward.
    The bounds arrive from metrics.json rather than being recomputed here, so
    the report's caption and the image cannot disagree about the scale. The
    diagonal is blanked because a variable's correlation with itself is 1 by
    construction and would otherwise dominate the scale.
    """
    apply_style()
    n = len(symbols)
    off_mask = ~np.eye(n, dtype=bool)
    shown = np.ma.masked_array(corr, mask=~off_mask)

    cmap = matplotlib.colormaps["RdBu_r"].copy()
    cmap.set_bad(BLANK)

    fig, ax = plt.subplots(figsize=(6.4, 5.4))
    mesh = ax.imshow(shown, cmap=cmap, vmin=vmin, vmax=vmax)
    ax.set_xticks(range(n), symbols)
    ax.set_yticks(range(n), symbols)
    ax.grid(visible=False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(length=0)

    for i in range(n):
        for j in range(n):
            if i == j:
                ax.text(j, i, "1.00", ha="center", va="center", fontsize=9, color=MUTED)
                continue
            rgba = cmap((corr[i, j] - vmin) / (vmax - vmin))
            luminance = 0.299 * rgba[0] + 0.587 * rgba[1] + 0.114 * rgba[2]
            ax.text(
                j,
                i,
                f"{corr[i, j]:.2f}",
                ha="center",
                va="center",
                fontsize=9.5,
                color="#ffffff" if luminance < 0.55 else INK,
            )

    bar = fig.colorbar(mesh, ax=ax, fraction=0.046, pad=0.04)
    bar.set_label(f"daily log-return correlation ({vmin:.2f} to {vmax:.2f})")
    bar.outline.set_visible(False)
    return _save(fig, path)


def volatility_bars(symbols: list[str], vols: np.ndarray, average: float, path: Path) -> Path:
    """Annualized volatility per asset, sorted, with the average marked."""
    apply_style()
    order = np.argsort(vols)
    labels = [symbols[i] for i in order]
    values = vols[order]

    fig, ax = plt.subplots(figsize=(7.0, 3.8))
    bars = ax.barh(labels, values, color=SERIES[0], height=0.62, zorder=3)
    for rect, value in zip(bars, values, strict=True):
        ax.annotate(
            f"{value:.3f}",
            (value, rect.get_y() + rect.get_height() / 2),
            xytext=(5, 0),
            textcoords="offset points",
            va="center",
            fontsize=9,
            color=SECONDARY,
            # An opaque patch above the average line, so the line passes
            # behind a label it would otherwise cut through.
            bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 1.5},
            zorder=5,
        )
    ax.axvline(average, color=SERIES[1], linewidth=1.4, linestyle=(0, (4, 3)), zorder=4)
    # Headroom above the top bar so the average label has clear background
    # instead of sitting on a filled bar in a second color.
    ax.set_ylim(-0.6, len(labels) - 0.05)
    ax.annotate(
        f"average {average:.3f}",
        (average, 1.0),
        xycoords=("data", "axes fraction"),
        xytext=(5, -12),
        textcoords="offset points",
        fontsize=9,
        color=SERIES[1],
        va="top",
    )
    ax.set_xlabel("annualized volatility (sample stdev of daily log returns x sqrt(365))")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    ax.margins(x=0.14)
    return _save(fig, path)


def drawdown_curves(
    dates: list[dt.date], symbols: list[str], paths_matrix: np.ndarray, out_path: Path
) -> Path:
    """Every asset's drawdown from its running peak, on one axis.

    The point of one axis is the co-movement: the assets fall together and
    rebound together, which is consistent with what the correlation matrix
    states numerically.
    """
    apply_style()
    # The dates are the close path's own, starting at the close the first
    # return is measured from, so they line up with the path row for row.
    x = np.array(dates, dtype="datetime64[D]")

    fig, ax = plt.subplots(figsize=(8.4, 4.6))
    for i, symbol in enumerate(symbols):
        ax.plot(
            x,
            paths_matrix[:, i],
            color=SERIES[i % len(SERIES)],
            linewidth=1.6,
            zorder=3,
            label=symbol,
        )
    ax.axhline(0.0, color=MUTED, linewidth=1.0, zorder=2)
    ax.set_ylabel("drawdown from running peak")
    ax.yaxis.set_major_formatter(lambda v, _: f"{100 * v:.0f}%")
    # The legend sits above the axes. The curves cross the whole plot area
    # over the window, so no interior space could hold a key without covering
    # data.
    ax.legend(loc="lower left", bbox_to_anchor=(0.0, 1.01), ncols=6, fontsize=9.5)
    ax.grid(axis="y")
    ax.grid(axis="x", visible=False)
    fig.autofmt_xdate()
    return _save(fig, out_path)


def diversification_bars(div: dict, path: Path) -> Path:
    """The result: what an equal-weight portfolio removes against what zero
    correlation would have removed."""
    apply_style()
    labels = [
        "average single asset",
        "equal-weight portfolio",
        "same variances,\nzero correlation",
    ]
    values = [
        div["average_single_asset_vol"],
        div["equal_weight_portfolio_vol"],
        div["zero_correlation_vol"],
    ]
    colors = [MUTED, SERIES[0], SERIES[2]]

    fig, ax = plt.subplots(figsize=(6.6, 4.4))
    bars = ax.bar(labels, values, color=colors, width=0.56, zorder=3)
    for rect, value in zip(bars, values, strict=True):
        ax.annotate(
            f"{value:.3f}",
            (rect.get_x() + rect.get_width() / 2, value),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            fontsize=10,
            color=SECONDARY,
        )
    ax.annotate(
        f"{100 * div['risk_removed_actual']:.1f}% removed",
        (1, values[1]),
        xytext=(0, 22),
        textcoords="offset points",
        ha="center",
        fontsize=9.5,
        color=SERIES[0],
    )
    ax.annotate(
        f"{100 * div['risk_removed_if_uncorrelated']:.1f}% removable",
        (2, values[2]),
        xytext=(0, 22),
        textcoords="offset points",
        ha="center",
        fontsize=9.5,
        color=SERIES[2],
    )
    ax.set_ylabel("annualized volatility")
    ax.set_ylim(0, max(values) * 1.28)
    ax.grid(axis="y")
    ax.grid(axis="x", visible=False)
    return _save(fig, path)


def write_all(panel, close_path, metrics: dict, figures_dir: Path) -> list[Path]:
    """Render all four figures from a panel, its close path, and its computed metrics."""
    from .analysis import drawdown_paths

    corr = np.array(metrics["correlation"]["matrix"], dtype=float)
    vols = np.array([a["annualized_volatility"] for a in metrics["assets"]], dtype=float)
    div = metrics["diversification"]
    return [
        correlation_heatmap(
            panel.symbols,
            corr,
            metrics["correlation"]["heatmap_scale_low"],
            metrics["correlation"]["heatmap_scale_high"],
            figures_dir / CORRELATION_HEATMAP,
        ),
        volatility_bars(
            panel.symbols, vols, div["average_single_asset_vol"], figures_dir / ASSET_VOLATILITY
        ),
        drawdown_curves(
            close_path.dates,
            panel.symbols,
            drawdown_paths(np.diff(np.log(close_path.closes), axis=0)),
            figures_dir / DRAWDOWN_CURVES,
        ),
        diversification_bars(div, figures_dir / DIVERSIFICATION),
    ]
