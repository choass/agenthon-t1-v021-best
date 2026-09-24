# Statistics, time and data contracts
Treat the task's stated definitions as executable specifications, even if they differ from a usual convention.
Before writing formulas, map each requested output to: input column, filtering order, units, time grid, estimator, denominator and null policy.
Use a tiny dataset where competing choices give different answers, and calculate the expected result by hand.

- Track separate counts for raw rows, deduplicated observations, usable prices and returns. N prices normally produce N-1 simple returns; an output named n_prices need not mean n_returns. Quote the task's definition before choosing.
- Record timestamps' source zone, conversion zone, day boundary and cutoff inclusivity. Do not localize and convert interchangeably. For as-of joins prove the information was available at the decision time.
- Preserve requested order: deduplication, resampling, session filtering, differencing, missing-value removal. These operations generally do not commute.
- Variance units square the input scaling. Volatility, variance and standard error are different quantities. Track per-day versus annualized statistics independently.
- Read ddof, geometric/arithmetic return and simple/log return requirements explicitly. Include zero/negative returns, missing observations and unequal group sizes in small tests.
- A high-frequency noise correction depends on the task's chosen estimator and sampling grid. Do not replace it with another familiar estimator merely because both are mathematically reasonable.
- Reconcile each dropped row and every reported count using the actual input. Never delete rows to match an assumed count.
