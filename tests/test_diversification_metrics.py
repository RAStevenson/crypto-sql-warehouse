"""Tests for the diversification math in crypto_warehouse.diversification_metrics.

Nothing here touches a database or a network. Every input is either a matrix
written out by hand or a panel constructed so the right answer is known in
closed form, which is the only way a test of a statistic can fail informatively.
"""

from __future__ import annotations

import datetime

import numpy as np
import pytest

from crypto_warehouse import diversification_metrics

DAY = datetime.timedelta(days=1)
START = datetime.date(2026, 1, 1)


def consecutive_dates(n_days: int) -> list[datetime.date]:
    return [START + i * DAY for i in range(n_days)]


# Three Walsh vectors: zero mean, mutually orthogonal, so their sample
# covariance is exactly diagonal with no appeal to a random seed.
WALSH_VECTORS = np.array(
    [
        [1, -1, 1, -1, 1, -1, 1, -1],
        [1, 1, -1, -1, 1, 1, -1, -1],
        [1, -1, -1, 1, 1, -1, -1, 1],
    ],
    dtype=float,
).T


def orthogonal_panel(scales=(0.01, 0.02, 0.03)) -> np.ndarray:
    return WALSH_VECTORS * np.array(scales)


def collinear_panel(scales=(1.0, 2.0, 3.0)) -> np.ndarray:
    base_returns = np.array([0.01, -0.02, 0.015, -0.005, 0.03, -0.01, 0.02, -0.04])
    return np.outer(base_returns, np.array(scales))


# --------------------------------------------------------------------------
# portfolio_volatility
# --------------------------------------------------------------------------


def test_portfolio_volatility_matches_hand_computed_two_asset_case():
    """w'Cw for C = [[0.04, 0.01], [0.01, 0.09]] and w = (0.5, 0.5) is
    0.25 * (0.04 + 0.01 + 0.01 + 0.09) = 0.0375, so the volatility is
    sqrt(0.0375) = 0.1936491673103708."""
    covariance = np.array([[0.04, 0.01], [0.01, 0.09]])
    weights = np.array([0.5, 0.5])
    assert diversification_metrics.portfolio_volatility(covariance, weights) == pytest.approx(
        0.0375**0.5
    )
    assert diversification_metrics.portfolio_volatility(covariance, weights) == pytest.approx(
        0.1936491673103708
    )


def test_portfolio_volatility_uses_the_weights_it_is_given():
    covariance = np.array([[0.04, 0.01], [0.01, 0.09]])
    # All weight on the first asset returns that asset's own volatility.
    assert (
        diversification_metrics.portfolio_volatility(covariance, np.array([1.0, 0.0]))
        == pytest.approx(0.2)
    )
    assert (
        diversification_metrics.portfolio_volatility(covariance, np.array([0.0, 1.0]))
        == pytest.approx(0.3)
    )


def test_portfolio_volatility_falls_when_covariance_is_negative():
    variances = np.array([[0.04, 0.0], [0.0, 0.09]])
    weights = diversification_metrics.equal_weights(2)
    hedged = variances.copy()
    hedged[0, 1] = hedged[1, 0] = -0.02
    assert diversification_metrics.portfolio_volatility(
        hedged, weights
    ) < diversification_metrics.portfolio_volatility(variances, weights)


def equicorrelated_covariance(
    n_assets: int, volatility: float, correlation: float
) -> np.ndarray:
    """n_assets assets with the same volatility and the same correlation
    between every pair."""
    covariance = np.full((n_assets, n_assets), correlation * volatility * volatility)
    np.fill_diagonal(covariance, volatility * volatility)
    return covariance


def test_equicorrelation_limit_matches_the_closed_form():
    # With equal volatilities and a shared correlation, an equal-weight basket
    # tends to volatility * sqrt(correlation) as the asset count grows. The
    # report quotes this to say what another name of the same kind would add,
    # so pin it to the algebra.
    volatility, correlation = 0.5, 0.64
    diversification_result = diversification_metrics.equal_weight_diversification(
        equicorrelated_covariance(6, volatility, correlation)
    )
    assert diversification_result["equicorrelation_limit_vol"] == pytest.approx(
        volatility * 0.8
    )


def test_equicorrelation_limit_is_approached_from_above_as_n_grows():
    volatility, correlation = 0.5, 0.64
    limit = volatility * np.sqrt(correlation)
    previous = None
    for n_assets in (2, 4, 8, 32, 256):
        vol = diversification_metrics.equal_weight_diversification(
            equicorrelated_covariance(n_assets, volatility, correlation)
        )["equal_weight_portfolio_vol"]
        assert vol > limit
        if previous is not None:
            assert vol < previous
        previous = vol
    assert previous == pytest.approx(limit, abs=1e-3)


def test_equicorrelation_limit_is_the_asset_itself_when_everything_moves_together():
    # A correlation of 1 means the basket never beats one name, so the limit is
    # the single-asset volatility.
    diversification_result = diversification_metrics.equal_weight_diversification(
        equicorrelated_covariance(5, 0.4, 1.0)
    )
    assert diversification_result["equicorrelation_limit_vol"] == pytest.approx(0.4)


def test_equicorrelation_limit_is_zero_when_assets_are_independent():
    diversification_result = diversification_metrics.equal_weight_diversification(
        equicorrelated_covariance(5, 0.4, 0.0)
    )
    assert diversification_result["equicorrelation_limit_vol"] == pytest.approx(0.0)


def test_equal_weights_sum_to_one():
    for n_assets in (1, 2, 6, 17):
        weights = diversification_metrics.equal_weights(n_assets)
        assert len(weights) == n_assets
        assert weights.sum() == pytest.approx(1.0)


# --------------------------------------------------------------------------
# equal_weight_diversification
# --------------------------------------------------------------------------


def test_perfectly_correlated_panel_captures_no_diversification():
    """Columns that are scalar multiples of one another correlate at 1, so the
    equal-weight portfolio volatility equals the weighted average of the single
    asset volatilities and nothing is removed."""
    covariance = diversification_metrics.annualized_covariance(collinear_panel())
    diversification_result = diversification_metrics.equal_weight_diversification(covariance)
    assert diversification_result["equal_weight_portfolio_vol"] == pytest.approx(
        diversification_result["average_single_asset_vol"]
    )
    assert diversification_result["risk_removed_actual"] == pytest.approx(0.0, abs=1e-12)
    assert diversification_result["diversification_captured"] == pytest.approx(0.0, abs=1e-12)
    # The counterfactual still has room to move, so the ratio is a real zero
    # rather than zero divided by zero.
    assert diversification_result["risk_removed_if_uncorrelated"] > 0.3


def test_uncorrelated_panel_captures_all_available_diversification():
    covariance = diversification_metrics.annualized_covariance(orthogonal_panel())
    off_diagonal_values = diversification_metrics.off_diagonal_entries(covariance)
    assert np.allclose(off_diagonal_values, 0.0)
    diversification_result = diversification_metrics.equal_weight_diversification(covariance)
    assert diversification_result["equal_weight_portfolio_vol"] == pytest.approx(
        diversification_result["zero_correlation_vol"]
    )
    assert diversification_result["diversification_captured"] == pytest.approx(1.0, abs=1e-12)


def test_diversification_captured_sits_between_the_two_extremes():
    """A panel that is neither collinear nor orthogonal lands strictly inside."""
    returns = orthogonal_panel() + collinear_panel(scales=(0.01, 0.01, 0.01))
    diversification_result = diversification_metrics.equal_weight_diversification(
        diversification_metrics.annualized_covariance(returns)
    )
    assert 0.0 < diversification_result["diversification_captured"] < 1.0
    assert (
        diversification_result["zero_correlation_vol"]
        < diversification_result["equal_weight_portfolio_vol"]
        < diversification_result["average_single_asset_vol"]
    )


def test_equal_weight_diversification_reports_equal_weights():
    covariance = diversification_metrics.annualized_covariance(orthogonal_panel())
    assert diversification_metrics.equal_weight_diversification(covariance)[
        "weight_per_asset"
    ] == pytest.approx(1 / 3)


def test_zero_correlation_volatility_matches_the_closed_form():
    """For equal weights the zero-correlation portfolio volatility is
    sqrt(sum(sigma_i^2)) / n."""
    covariance = np.diag([0.04, 0.09, 0.16])
    expected = np.sqrt(0.04 + 0.09 + 0.16) / 3
    assert (
        diversification_metrics.equal_weight_diversification(covariance)["zero_correlation_vol"]
        == pytest.approx(expected)
    )


def test_identical_assets_leave_the_portfolio_where_it_started():
    covariance = np.full((4, 4), 0.09)
    diversification_result = diversification_metrics.equal_weight_diversification(covariance)
    assert diversification_result["equal_weight_portfolio_vol"] == pytest.approx(0.3)
    assert diversification_result["average_single_asset_vol"] == pytest.approx(0.3)
    assert diversification_result["risk_removed_actual"] == pytest.approx(0.0, abs=1e-12)


# --------------------------------------------------------------------------
# volatility, covariance, correlation
# --------------------------------------------------------------------------


def test_annualized_volatility_is_sample_stdev_times_root_365():
    returns = orthogonal_panel()
    expected = returns.std(axis=0, ddof=1) * np.sqrt(365)
    assert np.allclose(diversification_metrics.annualized_volatility(returns), expected)


def test_annualized_volatility_uses_the_n_minus_one_divisor():
    returns = np.array([[0.01], [-0.01], [0.01], [-0.01]])
    # Population stdev is 0.01; the sample stdev over 4 points is larger.
    assert diversification_metrics.annualized_volatility(returns)[0] > 0.01 * np.sqrt(365)


def test_annualized_covariance_diagonal_matches_annualized_variance():
    returns = orthogonal_panel() + collinear_panel(scales=(0.01, 0.02, 0.005))
    covariance = diversification_metrics.annualized_covariance(returns)
    volatilities = diversification_metrics.annualized_volatility(returns)
    assert np.allclose(np.sqrt(np.diag(covariance)), volatilities)


def test_correlation_matrix_has_a_unit_diagonal_and_is_symmetric():
    correlations = diversification_metrics.correlation_matrix(
        orthogonal_panel() + collinear_panel((0.01, 0.02, 0.005))
    )
    assert np.allclose(np.diag(correlations), 1.0)
    assert np.allclose(correlations, correlations.T)


def test_off_diagonal_entries_returns_every_entry_but_the_diagonal():
    matrix = np.arange(16, dtype=float).reshape(4, 4)
    off_diagonal_values = diversification_metrics.off_diagonal_entries(matrix)
    assert len(off_diagonal_values) == 12
    assert (
        0.0 not in off_diagonal_values
        and 5.0 not in off_diagonal_values
        and 10.0 not in off_diagonal_values
        and 15.0 not in off_diagonal_values
    )


def test_correlation_of_collinear_columns_is_one():
    correlations = diversification_metrics.correlation_matrix(collinear_panel())
    assert np.allclose(correlations, 1.0)


# --------------------------------------------------------------------------
# drawdown
# --------------------------------------------------------------------------


def test_max_drawdown_on_a_hand_computed_path():
    """A column that doubles then halves twice ends 50% below its peak."""
    returns = np.array([[np.log(2.0)], [np.log(0.5)], [np.log(0.5)]])
    assert diversification_metrics.max_drawdown(returns)[0] == pytest.approx(-0.75)


def test_max_drawdown_is_zero_for_a_monotonically_rising_path():
    returns = np.full((5, 1), 0.01)
    assert diversification_metrics.max_drawdown(returns)[0] == pytest.approx(0.0)


def test_max_drawdown_counts_a_fall_from_the_very_first_day():
    """The path starts at its own opening level, so an asset that only ever
    falls records the whole fall rather than zero."""
    returns = np.array([[np.log(0.5)], [np.log(0.5)]])
    assert diversification_metrics.max_drawdown(returns)[0] == pytest.approx(-0.75)


def test_drawdown_paths_start_at_zero_and_agree_with_max_drawdown():
    returns = orthogonal_panel()
    drawdowns = diversification_metrics.drawdown_paths(returns)
    assert drawdowns.shape == (returns.shape[0] + 1, returns.shape[1])
    assert np.allclose(drawdowns[0], 0.0)
    assert np.allclose(drawdowns.min(axis=0), diversification_metrics.max_drawdown(returns))
    assert (drawdowns <= 1e-15).all()


# --------------------------------------------------------------------------
# panel construction
# --------------------------------------------------------------------------


def test_build_return_panel_keeps_only_dates_where_every_symbol_reports():
    day_list = consecutive_dates(3)
    rows = [
        (day_list[0], "AAA", 0.01),
        (day_list[0], "BBB", 0.02),
        (day_list[1], "AAA", 0.03),  # BBB missing, so this date is dropped
        (day_list[2], "AAA", 0.04),
        (day_list[2], "BBB", 0.05),
    ]
    panel = diversification_metrics.build_return_panel(rows)
    assert panel.dates == [day_list[0], day_list[2]]
    assert panel.symbols == ["AAA", "BBB"]
    assert panel.n_days == 2
    assert panel.n_assets == 2
    assert np.allclose(panel.returns, [[0.01, 0.02], [0.04, 0.05]])


def test_build_return_panel_orders_columns_by_symbol_and_rows_by_date():
    day_list = consecutive_dates(2)
    rows = [
        (day_list[1], "ZZZ", 0.04),
        (day_list[0], "ZZZ", 0.02),
        (day_list[1], "AAA", 0.03),
        (day_list[0], "AAA", 0.01),
    ]
    panel = diversification_metrics.build_return_panel(rows)
    assert panel.symbols == ["AAA", "ZZZ"]
    assert panel.dates == [day_list[0], day_list[1]]
    assert np.allclose(panel.returns, [[0.01, 0.02], [0.03, 0.04]])


def test_build_return_panel_rejects_an_empty_input():
    with pytest.raises(ValueError, match="no non-null log returns"):
        diversification_metrics.build_return_panel([])


def test_build_return_panel_rejects_a_panel_with_no_common_date():
    day_list = consecutive_dates(2)
    rows = [(day_list[0], "AAA", 0.01), (day_list[1], "BBB", 0.02)]
    with pytest.raises(ValueError, match="would be empty"):
        diversification_metrics.build_return_panel(rows)


def close_rows(day_list, symbols, returns, start_level=100.0):
    """Closes consistent with a gap-free return matrix: one base close on the
    day before the first return, then each day's close compounded from it."""
    levels = start_level * np.exp(np.vstack([np.zeros(len(symbols)), np.cumsum(returns, axis=0)]))
    all_days = [day_list[0] - DAY, *day_list]
    return [
        (all_days[i], symbols[j], levels[i, j])
        for i in range(len(all_days))
        for j in range(len(symbols))
    ]


def panel_and_path(day_list, symbols, returns):
    rows = [
        (day_list[i], symbols[j], returns[i, j])
        for i in range(len(day_list))
        for j in range(len(symbols))
    ]
    panel = diversification_metrics.build_return_panel(rows)
    return panel, diversification_metrics.build_close_path(
        close_rows(day_list, symbols, returns), panel
    )


def test_build_return_panel_rejects_a_single_asset():
    day_list = consecutive_dates(3)
    rows = [(day_list[i], "AAA", 0.01 * (i + 1)) for i in range(3)]
    with pytest.raises(ValueError, match="at least two active assets"):
        diversification_metrics.build_return_panel(rows)


def test_build_return_panel_warns_when_one_asset_shortens_the_panel(caplog):
    day_list = consecutive_dates(4)
    rows = [(day_list[i], "AAA", 0.01) for i in range(4)] + [
        (day_list[i], "BBB", 0.02) for i in range(2, 4)
    ]
    with caplog.at_level("WARNING"):
        panel = diversification_metrics.build_return_panel(rows)
    assert panel.dates == [day_list[2], day_list[3]]
    assert "do not share one history" in caplog.text
    assert "BBB" in caplog.text


def test_build_return_panel_is_silent_when_every_asset_shares_one_history(caplog):
    day_list = consecutive_dates(3)
    rows = [(day_list[i], symbol, 0.01) for i in range(3) for symbol in ("AAA", "BBB")]
    with caplog.at_level("WARNING"):
        diversification_metrics.build_return_panel(rows)
    assert caplog.text == ""


def test_build_close_path_starts_at_the_close_before_the_first_return():
    day_list = consecutive_dates(3)
    symbols = ["AAA", "BBB"]
    returns = np.array([[0.01, 0.02], [0.03, -0.01], [-0.02, 0.04]])
    _, close_path = panel_and_path(day_list, symbols, returns)
    assert close_path.dates == [day_list[0] - DAY, *day_list]
    assert np.allclose(np.diff(np.log(close_path.closes), axis=0), returns)


def test_build_close_path_keeps_complete_days_the_panel_excludes():
    """A day after a gap has no return, so the panel drops it, but its close
    is real and the path has to pass through it."""
    day_list = consecutive_dates(5)
    symbols = ["AAA", "BBB"]
    # returns exist on day_list[0], day_list[1], day_list[4]; day_list[2] is a
    # partial day (no close row at all here) and day_list[3] is the complete day
    # after it (a close, but no return).
    return_rows = [
        (day, symbol, 0.01)
        for day in (day_list[0], day_list[1], day_list[4])
        for symbol in symbols
    ]
    panel = diversification_metrics.build_return_panel(return_rows)
    levels = {
        day_list[0] - DAY: 100.0,
        day_list[0]: 101.0,
        day_list[1]: 102.0,
        day_list[3]: 80.0,
        day_list[4]: 81.0,
    }
    closes = [(day, symbol, level) for day, level in levels.items() for symbol in symbols]
    close_path = diversification_metrics.build_close_path(closes, panel)
    assert close_path.dates == [
        day_list[0] - DAY,
        day_list[0],
        day_list[1],
        day_list[3],
        day_list[4],
    ]


def test_build_close_path_needs_a_close_before_the_first_return():
    day_list = consecutive_dates(2)
    symbols = ["AAA", "BBB"]
    panel = diversification_metrics.build_return_panel(
        [(day, symbol, 0.01) for day in day_list for symbol in symbols]
    )
    closes = [(day, symbol, 100.0) for day in day_list for symbol in symbols]
    with pytest.raises(ValueError, match="no complete close precedes"):
        diversification_metrics.build_close_path(closes, panel)


def test_panel_metrics_drawdown_and_total_return_follow_the_closes_across_an_excluded_date():
    """The defect this pins: summing only the panel's returns cuts the move
    across an excluded date out of the path. Here both assets halve across the
    excluded stretch, and that fall has to show up."""
    day_list = consecutive_dates(7)
    symbols = ["AAA", "BBB"]
    panel_days = (day_list[0], day_list[1], day_list[4], day_list[5], day_list[6])
    pairs = [(0.01, 0.02), (0.02, -0.01), (0.03, 0.01), (-0.01, 0.02), (0.02, 0.03)]
    return_rows = [
        (day, symbol, log_return)
        for day, pair in zip(panel_days, pairs, strict=True)
        for symbol, log_return in zip(symbols, pair, strict=True)
    ]
    panel = diversification_metrics.build_return_panel(return_rows)
    levels = {
        day_list[0] - DAY: 100.0, day_list[0]: 110.0, day_list[1]: 120.0,  # the peak
        day_list[3]: 60.0,  # complete day after the excluded day_list[2]: the trough
        day_list[4]: 66.0, day_list[5]: 70.0, day_list[6]: 75.0,
    }  # fmt: skip
    closes = [(day, symbol, level) for day, level in levels.items() for symbol in symbols]
    metrics = diversification_metrics.panel_metrics(
        panel, diversification_metrics.build_close_path(closes, panel)
    )
    for asset in metrics["assets"]:
        assert asset["total_log_return"] == pytest.approx(np.log(75.0 / 100.0))
        assert asset["max_drawdown"] == pytest.approx(60.0 / 120.0 - 1.0)
    assert metrics["close_path"] == {
        "first_date": (day_list[0] - DAY).isoformat(),
        "last_date": day_list[6].isoformat(),
        "n_closes": 7,
    }


def test_panel_metrics_rejects_an_asset_that_never_moved():
    day_list = consecutive_dates(6)
    symbols = ["AAA", "BBB"]
    returns = np.column_stack([np.array([0.01, -0.02, 0.03, 0.01, -0.01, 0.02]), np.zeros(6)])
    panel, close_path = panel_and_path(day_list, symbols, returns)
    with pytest.raises(ValueError, match="BBB did not move"):
        diversification_metrics.panel_metrics(panel, close_path)


def test_classify_excluded_dates_sorts_each_date_into_its_reason():
    day_list = consecutive_dates(6)
    panel_days = (day_list[1], day_list[4], day_list[5])
    panel = diversification_metrics.build_return_panel(
        [(day, symbol, 0.01) for day in panel_days for symbol in ("AAA", "BBB")]
    )
    observed = [
        (day_list[0], 1, 0),  # only one asset has a bar
        (day_list[1], 2, 0),  # in the panel
        (day_list[2], 2, 1),  # a partial bar
        (day_list[3], 2, 0),  # complete for both, excluded: first observation after day_list[2]
        (day_list[4], 2, 0),
        (day_list[5], 2, 0),
    ]
    assert diversification_metrics.classify_excluded_dates(observed, panel) == {
        "partial_bar": [day_list[2]],
        "not_every_asset": [day_list[0]],
        "after_excluded_day": [day_list[3]],
    }


def test_classify_excluded_dates_counts_a_partial_bar_first_when_an_asset_is_also_absent():
    day_list = consecutive_dates(2)
    panel = diversification_metrics.build_return_panel(
        [(day_list[1], symbol, 0.01) for symbol in ("AAA", "BBB")]
    )
    assert diversification_metrics.classify_excluded_dates(
        [(day_list[0], 1, 1), (day_list[1], 2, 0)], panel
    ) == {
        "partial_bar": [day_list[0]],
        "not_every_asset": [],
        "after_excluded_day": [],
    }


def test_panel_metrics_reports_the_panel_it_was_given():
    day_list = consecutive_dates(8)
    returns = orthogonal_panel()
    symbols = ["AAA", "BBB", "CCC"]
    metrics = diversification_metrics.panel_metrics(*panel_and_path(day_list, symbols, returns))
    assert metrics["panel"]["n_days"] == 8
    assert metrics["panel"]["n_assets"] == 3
    assert metrics["panel"]["symbols"] == symbols
    assert metrics["panel"]["first_date"] == day_list[0].isoformat()
    assert metrics["panel"]["last_date"] == day_list[7].isoformat()
    assert [asset["symbol"] for asset in metrics["assets"]] == symbols
    assert metrics["correlation"]["n_pairs"] == 3
    assert metrics["diversification"]["diversification_captured"] == pytest.approx(1.0, abs=1e-12)


def test_panel_metrics_output_is_json_serializable():
    """metrics.json is the contract with the report builder, so nothing in it
    may be a numpy scalar that json refuses to write."""
    import json

    day_list = consecutive_dates(8)
    returns = orthogonal_panel()
    symbols = ["AAA", "BBB", "CCC"]
    serialized_metrics = json.dumps(
        diversification_metrics.panel_metrics(*panel_and_path(day_list, symbols, returns))
    )
    assert "NaN" not in serialized_metrics
    assert "Infinity" not in serialized_metrics


# --------------------------------------------------------------------------
# fisher_interval and round_range_outward
# --------------------------------------------------------------------------


def test_fisher_interval_brackets_the_estimate():
    low, high = diversification_metrics.fisher_interval(0.8, 113)
    assert low < 0.8 < high
    assert low == pytest.approx(0.7215, abs=5e-4)
    assert high == pytest.approx(0.8583, abs=5e-4)


def test_fisher_interval_narrows_as_the_sample_grows():
    narrow = diversification_metrics.fisher_interval(0.8, 1000)
    wide = diversification_metrics.fisher_interval(0.8, 30)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_fisher_interval_stays_inside_the_correlation_range():
    low, high = diversification_metrics.fisher_interval(0.99, 10)
    assert -1.0 < low < high < 1.0


def test_fisher_interval_needs_more_than_three_observations():
    with pytest.raises(ValueError, match="more than 3"):
        diversification_metrics.fisher_interval(0.8, 3)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([0.7258, 0.8953], (0.70, 0.90)),
        ([0.75, 0.75], (0.75, 0.75)),
        ([0.31, 0.66], (0.30, 0.70)),
    ],
)
def test_round_range_outward_rounds_to_the_step(values, expected):
    low, high = diversification_metrics.round_range_outward(np.array(values))
    assert low == pytest.approx(expected[0])
    assert high == pytest.approx(expected[1])


def test_build_return_panel_raises_a_panel_data_error_that_is_still_a_value_error():
    with pytest.raises(diversification_metrics.PanelDataError) as raised:
        diversification_metrics.build_return_panel([])
    assert isinstance(raised.value, ValueError)


def test_panel_metrics_refuses_a_panel_too_short_for_a_correlation_interval():
    day_list = consecutive_dates(3)
    symbols = ["AAA", "BBB"]
    returns = np.array([[0.01, 0.02], [0.03, -0.01], [-0.02, 0.04]])
    panel, close_path = panel_and_path(day_list, symbols, returns)
    with pytest.raises(diversification_metrics.PanelDataError, match="needs at least 4"):
        diversification_metrics.panel_metrics(panel, close_path)
