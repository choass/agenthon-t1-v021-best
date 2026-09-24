# Pricing, calibration and numerical verification
Begin with the exact requested model and estimator. A more exact price is not a valid substitute for a specified approximation.

- Separate data preparation, parameter estimation, calibration, valuation, sensitivities and summary generation. Save the completed intermediate deliverables before starting expensive calculations.
- Check parameter bounds, convergence flags and residuals on original units. Reported optimizer success is insufficient; inspect the largest residual and active constraints.
- Test interpolation at input knots, enforce strictly ordered distinct abscissae, and state extrapolation policy. Vectorize queries and cache model-independent quantities.
- For finite differences, use two bump sizes and central differences where appropriate. Check whether vega/rho mean sensitivity per unit or per percentage point, and theta per year or per day.
- For Monte Carlo, use the required seed and method, compute standard error from path payoffs, and use common random numbers for comparisons. Antithetic/control-variate estimators change effective sample counting; do not silently change the requested estimator.
- Test zero maturity/volatility limits, parity or replication identities, monotonicity and mesh/sample convergence. Apply identities only where their assumptions hold (exercise style, dividends, barriers and timing matter).
- Verify lognormal pricing terms from the actual distribution of the chosen average/payoff. Arithmetic and geometric averages, discrete and continuous monitoring are different contracts.
- Write one independent small-case calculation. Comparing two functions copied from the same formula only checks transcription, not correctness.
- Rerun the original selfcheck after a reviewer changes formulas. Preserve a known executable solution and make evidence-based local changes.
- For discrete-loss tail statistics, strict exceedance, inclusive exceedance and a fixed-mass quantile tail are different estimators when losses tie at VaR. Explicitly identify the requested estimator and test a sample with ties; do not assume these formulas are interchangeable or silently change the boundary during review.
