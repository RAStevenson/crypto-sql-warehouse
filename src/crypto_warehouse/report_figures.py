"""Report figures.

Four plots, all on a light surface at print-legible sizes. The categorical
palette is Okabe-Ito, which stays distinguishable under the common forms of
color vision deficiency, and every bar carries its own value as text so a
reader who cannot separate two hues can still read the number.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.figure import Figure

# Okabe-Ito, in a fixed order so an asset keeps its color across regenerations.
OKABE_ITO_PALETTE = ["#0072b2", "#d55e00", "#009e73", "#e69f00", "#cc79a7", "#56b4e9"]
TEXT_COLOR = "#0b0b0b"
LABEL_COLOR = "#52514e"
MUTED_COLOR = "#898781"
GRID_COLOR = "#e1e0d9"
AXIS_COLOR = "#c3c2b7"
BACKGROUND_COLOR = "#fcfcfb"
MASKED_CELL_COLOR = "#e8e7e1"

CORRELATION_HEATMAP_FILENAME = "correlation_heatmap.png"
ASSET_VOLATILITY_FILENAME = "asset_volatility.png"
DRAWDOWN_CURVES_FILENAME = "drawdown_curves.png"
DIVERSIFICATION_FILENAME = "diversification.png"


def apply_style() -> None:
    """Set the matplotlib defaults that every figure shares."""
    plt.rcParams.update(
        {
            "figure.facecolor": BACKGROUND_COLOR,
            "axes.facecolor": BACKGROUND_COLOR,
            "savefig.facecolor": BACKGROUND_COLOR,
            "axes.edgecolor": AXIS_COLOR,
            "axes.linewidth": 1.0,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID_COLOR,
            "grid.linewidth": 0.8,
            "axes.axisbelow": True,
            "text.color": TEXT_COLOR,
            "axes.labelcolor": LABEL_COLOR,
            "xtick.color": MUTED_COLOR,
            "ytick.color": MUTED_COLOR,
            "xtick.labelcolor": LABEL_COLOR,
            "ytick.labelcolor": LABEL_COLOR,
            "font.family": "sans-serif",
            "font.sans-serif": ["Segoe UI", "DejaVu Sans", "sans-serif"],
            "font.size": 10,
            "legend.frameon": False,
            "figure.dpi": 150,
        }
    )


def _save_figure(fig: Figure, path: Path) -> Path:
    """Save the figure to path, creating its directory if needed, close the
    figure, and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def correlation_heatmap(
    symbols: list[str],
    correlations: np.ndarray,
    scale_low: float,
    scale_high: float,
    path: Path,
) -> Path:
    """Draw the correlation matrix as an annotated heatmap, save it, and return
    the path.

    The color axis runs from scale_low to scale_high, which the caller takes
    from the metrics so that the image and the report's caption state the same
    range.
    """
    apply_style()
    n_symbols = len(symbols)
    # The scale covers the observed off-diagonal range, because on -1 to 1
    # every cell would render in nearly the same tone. The diagonal is masked
    # because a self-correlation of 1 would otherwise dominate the scale.
    off_diagonal_mask = ~np.eye(n_symbols, dtype=bool)
    masked_correlations = np.ma.masked_array(correlations, mask=~off_diagonal_mask)

    colormap = matplotlib.colormaps["RdBu_r"].copy()
    colormap.set_bad(MASKED_CELL_COLOR)

    fig, ax = plt.subplots(figsize=(6.4, 5.4))
    heatmap_image = ax.imshow(
        masked_correlations, cmap=colormap, vmin=scale_low, vmax=scale_high
    )
    ax.set_xticks(range(n_symbols), symbols)
    ax.set_yticks(range(n_symbols), symbols)
    ax.grid(visible=False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(length=0)

    for i in range(n_symbols):
        for j in range(n_symbols):
            if i == j:
                ax.text(j, i, "1.00", ha="center", va="center", fontsize=9, color=MUTED_COLOR)
                continue
            rgba = colormap((correlations[i, j] - scale_low) / (scale_high - scale_low))
            luminance = 0.299 * rgba[0] + 0.587 * rgba[1] + 0.114 * rgba[2]
            ax.text(
                j,
                i,
                f"{correlations[i, j]:.2f}",
                ha="center",
                va="center",
                fontsize=9.5,
                color="#ffffff" if luminance < 0.55 else TEXT_COLOR,
            )

    colorbar = fig.colorbar(heatmap_image, ax=ax, fraction=0.046, pad=0.04)
    colorbar.set_label(
        f"daily log-return correlation ({scale_low:.2f} to {scale_high:.2f})"
    )
    colorbar.outline.set_visible(False)
    return _save_figure(fig, path)


def volatility_bars(
    symbols: list[str], volatilities: np.ndarray, average: float, path: Path
) -> Path:
    """Draw each asset's annualized volatility as sorted bars with the average
    marked, save the figure, and return the path."""
    apply_style()
    order = np.argsort(volatilities)
    labels = [symbols[i] for i in order]
    values = volatilities[order]

    fig, ax = plt.subplots(figsize=(7.0, 3.8))
    bars = ax.barh(labels, values, color=OKABE_ITO_PALETTE[0], height=0.62, zorder=3)
    for bar, value in zip(bars, values, strict=True):
        ax.annotate(
            f"{value:.3f}",
            (value, bar.get_y() + bar.get_height() / 2),
            xytext=(5, 0),
            textcoords="offset points",
            va="center",
            fontsize=9,
            color=LABEL_COLOR,
            # An opaque patch above the average line, so the line passes
            # behind a label it would otherwise cut through.
            bbox={"facecolor": BACKGROUND_COLOR, "edgecolor": "none", "pad": 1.5},
            zorder=5,
        )
    ax.axvline(average, color=OKABE_ITO_PALETTE[1], linewidth=1.4, linestyle=(0, (4, 3)), zorder=4)
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
        color=OKABE_ITO_PALETTE[1],
        va="top",
    )
    ax.set_xlabel("annualized volatility (sample stdev of daily log returns x sqrt(365))")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    ax.margins(x=0.14)
    return _save_figure(fig, path)


def drawdown_curves(
    dates: list[datetime.date], symbols: list[str], drawdowns: np.ndarray, path: Path
) -> Path:
    """Draw every asset's drawdown from its running peak on one axis, save the
    figure, and return the path."""
    apply_style()
    # The dates are the close path's own, starting at the close the first
    # return is measured from, so they line up with the path row for row.
    plot_dates = np.array(dates, dtype="datetime64[D]")

    # One axis for all assets, so the reader can compare when each one falls
    # and recovers.
    fig, ax = plt.subplots(figsize=(8.4, 4.6))
    for i, symbol in enumerate(symbols):
        ax.plot(
            plot_dates,
            drawdowns[:, i],
            color=OKABE_ITO_PALETTE[i % len(OKABE_ITO_PALETTE)],
            linewidth=1.6,
            zorder=3,
            label=symbol,
        )
    ax.axhline(0.0, color=MUTED_COLOR, linewidth=1.0, zorder=2)
    ax.set_ylabel("drawdown from running peak")
    ax.yaxis.set_major_formatter(lambda drawdown, _: f"{100 * drawdown:.0f}%")
    # The legend sits above the axes. The curves cross the whole plot area
    # over the window, so no interior space could hold a key without covering
    # data.
    ax.legend(loc="lower left", bbox_to_anchor=(0.0, 1.01), ncols=6, fontsize=9.5)
    ax.grid(axis="y")
    ax.grid(axis="x", visible=False)
    fig.autofmt_xdate()
    return _save_figure(fig, path)


def diversification_bars(diversification: dict, path: Path) -> Path:
    """Draw the average single-asset, equal-weight, and zero-correlation
    volatilities as three bars, save the figure, and return the path."""
    apply_style()
    labels = [
        "average single asset",
        "equal-weight portfolio",
        "same variances,\nzero correlation",
    ]
    values = [
        diversification["average_single_asset_vol"],
        diversification["equal_weight_portfolio_vol"],
        diversification["zero_correlation_vol"],
    ]
    colors = [MUTED_COLOR, OKABE_ITO_PALETTE[0], OKABE_ITO_PALETTE[2]]

    fig, ax = plt.subplots(figsize=(6.6, 4.4))
    bars = ax.bar(labels, values, color=colors, width=0.56, zorder=3)
    for bar, value in zip(bars, values, strict=True):
        ax.annotate(
            f"{value:.3f}",
            (bar.get_x() + bar.get_width() / 2, value),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            fontsize=10,
            color=LABEL_COLOR,
        )
    ax.annotate(
        f"{100 * diversification['risk_removed_actual']:.1f}% removed",
        (1, values[1]),
        xytext=(0, 22),
        textcoords="offset points",
        ha="center",
        fontsize=9.5,
        color=OKABE_ITO_PALETTE[0],
    )
    ax.annotate(
        f"{100 * diversification['risk_removed_if_uncorrelated']:.1f}% removable",
        (2, values[2]),
        xytext=(0, 22),
        textcoords="offset points",
        ha="center",
        fontsize=9.5,
        color=OKABE_ITO_PALETTE[2],
    )
    ax.set_ylabel("annualized volatility")
    ax.set_ylim(0, max(values) * 1.28)
    ax.grid(axis="y")
    ax.grid(axis="x", visible=False)
    return _save_figure(fig, path)


def write_figures(panel, close_path, metrics: dict, figures_dir: Path) -> list[Path]:
    """Render all four figures from a panel, its close path, and its metrics, and
    return the paths written."""
    from .diversification_metrics import drawdown_paths

    correlations = np.array(metrics["correlation"]["matrix"], dtype=float)
    volatilities = np.array(
        [asset["annualized_volatility"] for asset in metrics["assets"]], dtype=float
    )
    diversification = metrics["diversification"]
    return [
        correlation_heatmap(
            panel.symbols,
            correlations,
            metrics["correlation"]["heatmap_scale_low"],
            metrics["correlation"]["heatmap_scale_high"],
            figures_dir / CORRELATION_HEATMAP_FILENAME,
        ),
        volatility_bars(
            panel.symbols,
            volatilities,
            diversification["average_single_asset_vol"],
            figures_dir / ASSET_VOLATILITY_FILENAME,
        ),
        drawdown_curves(
            close_path.dates,
            panel.symbols,
            drawdown_paths(np.diff(np.log(close_path.closes), axis=0)),
            figures_dir / DRAWDOWN_CURVES_FILENAME,
        ),
        diversification_bars(diversification, figures_dir / DIVERSIFICATION_FILENAME),
    ]
