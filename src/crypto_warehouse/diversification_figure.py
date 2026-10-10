"""The chart of the diversification result, written to results/figures/.

Three bars: the average single asset, the equal-weight portfolio, and that
portfolio with the same variances and zero correlation. The two colored bars
use the Okabe-Ito palette, which stays distinguishable under the common forms
of color vision deficiency, and every bar is labeled with its value so a
reader who cannot separate two hues can still read the number.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.figure import Figure

OKABE_ITO_PALETTE = ["#0072b2", "#d55e00", "#009e73", "#e69f00", "#cc79a7", "#56b4e9"]
TEXT_COLOR = "#0b0b0b"
LABEL_COLOR = "#52514e"
MUTED_COLOR = "#898781"
GRID_COLOR = "#e1e0d9"
AXIS_COLOR = "#c3c2b7"
BACKGROUND_COLOR = "#fcfcfb"

DIVERSIFICATION_FILENAME = "diversification.png"


def apply_figure_style() -> None:
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


def diversification_bars(diversification: dict, path: Path) -> Path:
    """Draw the average single-asset, equal-weight, and zero-correlation
    volatilities as three bars, save the figure, and return the path."""
    apply_figure_style()
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
