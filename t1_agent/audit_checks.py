"""Optional generic assertions for agent-authored self-tests. No task-specific values.

Use only identities justified by the task. These do not replace the official grader.
"""

import numpy as np


def assert_close(name, actual, expected, *, rtol=1e-7, atol=1e-10):
    a, b = np.asarray(actual, dtype=float), np.asarray(expected, dtype=float)
    assert a.shape == b.shape, f"{name}: shape {a.shape} != {b.shape}"
    assert np.isfinite(a).all() and np.isfinite(b).all(), f"{name}: non-finite values"
    np.testing.assert_allclose(a, b, rtol=rtol, atol=atol, err_msg=name)


def check_gross_leverage(reported, weights, *, atol=1e-8):
    """Gross leverage includes both long and short exposures by magnitude."""
    assert_close("gross leverage", reported, np.abs(weights).sum(), atol=atol)


def check_variance_units(
    reported, variance_of_scaled_returns, return_multiplier, *, rtol=1e-7
):
    """If the estimator sees raw returns * multiplier, restore variance / multiplier**2."""
    assert return_multiplier != 0
    assert_close(
        "variance units",
        reported,
        np.asarray(variance_of_scaled_returns) / return_multiplier**2,
        rtol=rtol,
    )


def check_cash_account(
    before,
    after,
    signed_trade_quantities,
    trade_prices,
    fees,
    *,
    external_cashflow=0,
    atol=1e-8,
):
    """Positive trade quantities are buys; fees positive means a cash cost."""
    trades = np.asarray(signed_trade_quantities) * np.asarray(trade_prices)
    expected = before - trades.sum() - np.asarray(fees).sum() + external_cashflow
    assert_close("cash reconciliation", after, expected, atol=atol)


def check_table(
    frame, *, required_columns=(), unique_by=None, expected_rows=None, finite_columns=()
):
    missing = set(required_columns) - set(frame.columns)
    assert not missing, f"Missing columns: {sorted(missing)}"
    if expected_rows is not None:
        assert len(frame) == expected_rows, (
            f"Rows {len(frame)} != independently derived {expected_rows}"
        )
    if unique_by:
        assert not frame.duplicated(subset=unique_by).any(), (
            f"Duplicate keys: {unique_by}"
        )
    for column in finite_columns:
        assert np.isfinite(frame[column].to_numpy(dtype=float)).all(), (
            f"Non-finite {column}"
        )


def check_finite_difference(
    function, x, reported_derivative, *, step, rtol=1e-3, atol=1e-6
):
    """Check a scalar derivative at two step sizes, in the function's original units."""
    d1 = (function(x + step) - function(x - step)) / (2 * step)
    d2 = (function(x + step / 2) - function(x - step / 2)) / step
    assert_close("finite difference convergence", d1, d2, rtol=rtol, atol=atol)
    assert_close("derivative", reported_derivative, d2, rtol=rtol, atol=atol)
