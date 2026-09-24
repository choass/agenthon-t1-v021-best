# Portfolios, backtests and hedging
Translate the specification into a dated cash-and-position ledger before computing aggregate metrics.

- Define signal observation, portfolio formation, execution, fees, return accrual and rebalance time explicitly. Use a small rising/falling two-asset example to reveal off-by-one execution or future information.
- Store signed quantities separately from absolute exposures. Gross leverage is the sum of absolute exposures; net exposure is the signed sum. Whether weights use equity, gross NAV or another denominator must come from the task.
- Check equity equals cash plus marked holdings and that each trade's cash movement includes sign, multiplier and fees. Reconcile cash dividends, corporate actions and financing consistently.
- Distinguish target from realized weights, pre-trade from post-trade NAV, turnover conventions and whether costs apply once or twice. Do not overwrite a stale hedge book unless the task calls for rehedging.
- Currency pairs have a direction: derive the conversion and hedging P&L with a one-unit example. Keep spot/forward quotation, foreign/domestic rates and discounting consistent.
- Risk metrics require an explicit return/loss sign, window, frequency and quantile convention. Backtest metrics must use the specified population of dates and assets.
- Save positions, intermediate checkpoints and summaries along with final returns when requested. Independently recompute selected dates from raw trades rather than from the final P&L vector.
