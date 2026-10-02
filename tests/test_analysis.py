"""Tests for the diversification math in crypto_warehouse.analysis.

Nothing here touches a database or a network. Every input is either a matrix
written out by hand or a panel constructed so the right answer is known in
closed form, which is the only way a test of a statistic can fail informatively.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from crypto_warehouse import analysis

DAY = dt.timedelta(days=1)
START = dt.date(2026, 1, 1)


def dates(n: int) -> list[dt.date]:
    return [START + i * DAY for i in range(n)]


# Three Walsh vectors: zero mean, mutually orthogonal, so their sample
# covariance is exactly diagonal with no appeal to a random seed.
WALSH = np.array(
    [
        [1, -1, 1, -1, 1, -1, 1, -1],
        [1, 1, -1, -1, 1, 1, -1, -1],
        [1, -1, -1, 1, 1, -1, -1, 1],
    ],
    dtype=float,
).T


def orthogonal_panel(scales=(0.01, 0.02, 0.03)) -> np.ndarray:
    return WALSH * np.array(scales)


def collinear_panel(scales=(1.0, 2.0, 3.0)) -> np.ndarray:
    base = np.array([0.01, -0.02, 0.015, -0.005, 0.03, -0.01, 0.02, -0.04])
    return np.outer(base, np.array(scales))


# --------------------------------------------------------------------------
# portfolio_volatility
# --------------------------------------------------------------------------


def test_portfolio_volatility_matches_hand_computed_two_asset_case():
    """w'Cw for C = [[0.04, 0.01], [0.01, 0.09]] and w = (0.5, 0.5) is
    0.25 * (0.04 + 0.01 + 0.01 + 0.09) = 0.0375, so the volatility is
    sqrt(0.0375) = 0.1936491673103708."""
    cov = np.array([[0.04, 0.01], [0.01, 0.09]])
    weights = np.array([0.5, 0.5])
    assert analysis.portfolio_volatility(cov, weights) == pytest.approx(0.0375**0.5)
    assert analysis.portfolio_volatility(cov, weights) == pytest.approx(0.1936491673103708)


def test_portfolio_volatility_uses_the_weights_it_is_given():
    cov = np.array([[0.04, 0.01], [0.01, 0.09]])
    # All weight on the first asset returns that asset's own volatility.
    assert analysis.portfolio_volatility(cov, np.array([1.0, 0.0])) == pytest.approx(0.2)
    assert analysis.portfolio_volatility(cov, np.array([0.0, 1.0])) == pytest.approx(0.3)


def test_portfolio_volatility_falls_when_covariance_is_negative():
    variances = np.array([[0.04, 0.0], [0.0, 0.09]])
    weights = analysis.equal_weights(2)
    hedged = variances.copy()
    hedged[0, 1] = hedged[1, 0] = -0.02
    assert analysis.portfolio_volatility(hedged, weights) < analysis.portfolio_volatility(
        variances, weights
    )


def equicorrelated_covariance(n: int, sigma: float, rho: float) -> np.ndarray:
    """n assets, identical volatility sigma, every pair correlated rho."""
    cov = np.full((n, n), rho * sigma * sigma)
    np.fill_diagonal(cov, sigma * sigma)
    return cov


def test_equicorrelation_limit_matches_the_closed_form():
    # With equal volatilities and a shared rho, an equal-weight basket tends to
    # sigma * sqrt(rho) as the asset count grows. The report quotes this to say
    # what another name of the same kind would buy, so pin it to the algebra.
    sigma, rho = 0.5, 0.64
    result = analysis.diversification(equicorrelated_covariance(6, sigma, rho))
    assert result["equicorrelation_limit_vol"] == pytest.approx(sigma * 0.8)


def test_equicorrelation_limit_is_approached_from_above_as_n_grows():
    sigma, rho = 0.5, 0.64
    limit = sigma * np.sqrt(rho)
    previous = None
    for n in (2, 4, 8, 32, 256):
        vol = analysis.diversification(equicorrelated_covariance(n, sigma, rho))[
            "equal_weight_portfolio_vol"
        ]
        assert vol > limit
        if previous is not None:
            assert vol < previous
        previous = vol
    assert previous == pytest.approx(limit, abs=1e-3)


def test_equicorrelation_limit_is_the_asset_itself_when_everything_moves_together():
    # rho = 1 means the basket never beats one name, so the limit is sigma.
    result = analysis.diversification(equicorrelated_covariance(5, 0.4, 1.0))
    assert result["equicorrelation_limit_vol"] == pytest.approx(0.4)


def test_equicorrelation_limit_is_zero_when_assets_are_independent():
    result = analysis.diversification(equicorrelated_covariance(5, 0.4, 0.0))
    assert result["equicorrelation_limit_vol"] == pytest.approx(0.0)


def test_equal_weights_sum_to_one():
    for n in (1, 2, 6, 17):
        weights = analysis.equal_weights(n)
        assert len(weights) == n
        assert weights.sum() == pytest.approx(1.0)


# --------------------------------------------------------------------------
# diversification
# --------------------------------------------------------------------------


def test_perfectly_correlated_panel_captures_no_diversification():
    """Columns that are scalar multiples of one another correlate at 1, so the
    equal-weight portfolio volatility equals the weighted average of the single
    asset volatilities and nothing is removed."""
    cov = analysis.annualized_covariance(collinear_panel())
    result = analysis.diversification(cov)
    assert result["equal_weight_portfolio_vol"] == pytest.approx(
        result["average_single_asset_vol"]
    )
    assert result["risk_removed_actual"] == pytest.approx(0.0, abs=1e-12)
    assert result["diversification_captured"] == pytest.approx(0.0, abs=1e-12)
    # The counterfactual still has room to move, so the ratio is a real zero
    # rather than zero divided by zero.
    assert result["risk_removed_if_uncorrelated"] > 0.3


def test_uncorrelated_panel_captures_all_available_diversification():
    cov = analysis.annualized_covariance(orthogonal_panel())
    off = analysis.off_diagonal(cov)
    assert np.allclose(off, 0.0)
    result = analysis.diversification(cov)
    assert result["equal_weight_portfolio_vol"] == pytest.approx(result["zero_correlation_vol"])
    assert result["diversification_captured"] == pytest.approx(1.0, abs=1e-12)


def test_diversification_captured_sits_between_the_two_extremes():
    """A panel that is neither collinear nor orthogonal lands strictly inside."""
    returns = orthogonal_panel() + collinear_panel(scales=(0.01, 0.01, 0.01))
    result = analysis.diversification(analysis.annualized_covariance(returns))
    assert 0.0 < result["diversification_captured"] < 1.0
    assert (
        result["zero_correlation_vol"]
        < result["equal_weight_portfolio_vol"]
        < result["average_single_asset_vol"]
    )


def test_diversification_reports_equal_weights():
    cov = analysis.annualized_covariance(orthogonal_panel())
    assert analysis.diversification(cov)["weight_per_asset"] == pytest.approx(1 / 3)


def test_zero_correlation_volatility_matches_the_closed_form():
    """For equal weights the zero-correlation portfolio volatility is
    sqrt(sum(sigma_i^2)) / n."""
    cov = np.diag([0.04, 0.09, 0.16])
    expected = np.sqrt(0.04 + 0.09 + 0.16) / 3
    assert analysis.diversification(cov)["zero_correlation_vol"] == pytest.approx(expected)


def test_identical_assets_leave_the_portfolio_where_it_started():
    cov = np.full((4, 4), 0.09)
    result = analysis.diversification(cov)
    assert result["equal_weight_portfolio_vol"] == pytest.approx(0.3)
    assert result["average_single_asset_vol"] == pytest.approx(0.3)
    assert result["risk_removed_actual"] == pytest.approx(0.0, abs=1e-12)


# --------------------------------------------------------------------------
# volatility, covariance, correlation
# --------------------------------------------------------------------------


def test_annualized_volatility_is_sample_stdev_times_root_365():
    returns = orthogonal_panel()
    expected = returns.std(axis=0, ddof=1) * np.sqrt(365)
    assert np.allclose(analysis.annualized_volatility(returns), expected)


def test_annualized_volatility_uses_the_n_minus_one_divisor():
    returns = np.array([[0.01], [-0.01], [0.01], [-0.01]])
    # Population stdev is 0.01; the sample stdev over 4 points is larger.
    assert analysis.annualized_volatility(returns)[0] > 0.01 * np.sqrt(365)


def test_annualized_covariance_diagonal_matches_annualized_variance():
    returns = orthogonal_panel() + collinear_panel(scales=(0.01, 0.02, 0.005))
    cov = analysis.annualized_covariance(returns)
    vols = analysis.annualized_volatility(returns)
    assert np.allclose(np.sqrt(np.diag(cov)), vols)


def test_correlation_matrix_has_a_unit_diagonal_and_is_symmetric():
    corr = analysis.correlation_matrix(orthogonal_panel() + collinear_panel((0.01, 0.02, 0.005)))
    assert np.allclose(np.diag(corr), 1.0)
    assert np.allclose(corr, corr.T)


def test_off_diagonal_returns_every_entry_but_the_diagonal():
    matrix = np.arange(16, dtype=float).reshape(4, 4)
    off = analysis.off_diagonal(matrix)
    assert len(off) == 12
    assert 0.0 not in off and 5.0 not in off and 10.0 not in off and 15.0 not in off


def test_correlation_of_collinear_columns_is_one():
    corr = analysis.correlation_matrix(collinear_panel())
    assert np.allclose(corr, 1.0)


# --------------------------------------------------------------------------
# drawdown
# --------------------------------------------------------------------------


def test_max_drawdown_on_a_hand_computed_path():
    """A column that doubles then halves twice ends 50% below its peak."""
    returns = np.array([[np.log(2.0)], [np.log(0.5)], [np.log(0.5)]])
    assert analysis.max_drawdown(returns)[0] == pytest.approx(-0.75)


def test_max_drawdown_is_zero_for_a_monotonically_rising_path():
    returns = np.full((5, 1), 0.01)
    assert analysis.max_drawdown(returns)[0] == pytest.approx(0.0)


def test_max_drawdown_counts_a_fall_from_the_very_first_day():
    """The path starts at its own opening level, so an asset that only ever
    falls records the whole fall rather than zero."""
    returns = np.array([[np.log(0.5)], [np.log(0.5)]])
    assert analysis.max_drawdown(returns)[0] == pytest.approx(-0.75)


def test_drawdown_paths_start_at_zero_and_agree_with_max_drawdown():
    returns = orthogonal_panel()
    paths = analysis.drawdown_paths(returns)
    assert paths.shape == (returns.shape[0] + 1, returns.shape[1])
    assert np.allclose(paths[0], 0.0)
    assert np.allclose(paths.min(axis=0), analysis.max_drawdown(returns))
    assert (paths <= 1e-15).all()


# --------------------------------------------------------------------------
# panel construction
# --------------------------------------------------------------------------


def test_build_panel_keeps_only_dates_where_every_symbol_reports():
    d = dates(3)
    rows = [
        (d[0], "AAA", 0.01),
        (d[0], "BBB", 0.02),
        (d[1], "AAA", 0.03),  # BBB missing, so this date is dropped
        (d[2], "AAA", 0.04),
        (d[2], "BBB", 0.05),
    ]
    panel = analysis.build_panel(rows)
    assert panel.dates == [d[0], d[2]]
    assert panel.symbols == ["AAA", "BBB"]
    assert panel.n_days == 2
    assert panel.n_assets == 2
    assert np.allclose(panel.returns, [[0.01, 0.02], [0.04, 0.05]])


def test_build_panel_orders_columns_by_symbol_and_rows_by_date():
    d = dates(2)
    rows = [
        (d[1], "ZZZ", 0.04),
        (d[0], "ZZZ", 0.02),
        (d[1], "AAA", 0.03),
        (d[0], "AAA", 0.01),
    ]
    panel = analysis.build_panel(rows)
    assert panel.symbols == ["AAA", "ZZZ"]
    assert panel.dates == [d[0], d[1]]
    assert np.allclose(panel.returns, [[0.01, 0.02], [0.03, 0.04]])


def test_build_panel_rejects_an_empty_input():
    with pytest.raises(ValueError, match="no non-null log returns"):
        analysis.build_panel([])


def test_build_panel_rejects_a_panel_with_no_common_date():
    d = dates(2)
    rows = [(d[0], "AAA", 0.01), (d[1], "BBB", 0.02)]
    with pytest.raises(ValueError, match="would be empty"):
        analysis.build_panel(rows)


def test_panel_metrics_reports_the_panel_it_was_given():
    d = dates(8)
    returns = orthogonal_panel()
    symbols = ["AAA", "BBB", "CCC"]
    rows = [(d[i], symbols[j], returns[i, j]) for i in range(8) for j in range(3)]
    metrics = analysis.panel_metrics(analysis.build_panel(rows))
    assert metrics["panel"]["n_days"] == 8
    assert metrics["panel"]["n_assets"] == 3
    assert metrics["panel"]["symbols"] == symbols
    assert metrics["panel"]["first_date"] == d[0].isoformat()
    assert metrics["panel"]["last_date"] == d[7].isoformat()
    assert [a["symbol"] for a in metrics["assets"]] == symbols
    assert metrics["correlation"]["n_pairs"] == 3
    assert metrics["diversification"]["diversification_captured"] == pytest.approx(1.0, abs=1e-12)


def test_panel_metrics_output_is_json_serializable():
    """metrics.json is the contract with the report builder, so nothing in it
    may be a numpy scalar that json refuses to write."""
    import json

    d = dates(8)
    returns = orthogonal_panel()
    symbols = ["AAA", "BBB", "CCC"]
    rows = [(d[i], symbols[j], returns[i, j]) for i in range(8) for j in range(3)]
    text = json.dumps(analysis.panel_metrics(analysis.build_panel(rows)))
    assert "NaN" not in text
    assert "Infinity" not in text


# --------------------------------------------------------------------------
# helpers quoted in the report
# --------------------------------------------------------------------------


def test_fisher_interval_brackets_the_estimate():
    low, high = analysis.fisher_interval(0.8, 113)
    assert low < 0.8 < high
    assert low == pytest.approx(0.7215, abs=5e-4)
    assert high == pytest.approx(0.8583, abs=5e-4)


def test_fisher_interval_narrows_as_the_sample_grows():
    narrow = analysis.fisher_interval(0.8, 1000)
    wide = analysis.fisher_interval(0.8, 30)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_fisher_interval_stays_inside_the_correlation_range():
    low, high = analysis.fisher_interval(0.99, 10)
    assert -1.0 < low < high < 1.0


def test_fisher_interval_needs_more_than_three_observations():
    with pytest.raises(ValueError, match="more than 3"):
        analysis.fisher_interval(0.8, 3)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([0.7258, 0.8953], (0.70, 0.90)),
        ([0.75, 0.75], (0.75, 0.75)),
        ([0.31, 0.66], (0.30, 0.70)),
    ],
)
def test_scale_bounds_rounds_outward_to_the_step(values, expected):
    low, high = analysis.scale_bounds(np.array(values))
    assert low == pytest.approx(expected[0])
    assert high == pytest.approx(expected[1])
