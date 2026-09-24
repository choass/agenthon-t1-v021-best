# Curves, bonds and swaps
Build and validate a reusable curve before pricing the entire portfolio. Follow the task's specified bootstrap and interpolation, not a library default.

- Separate valuation, settlement and payment dates; use the exact day-count, business-day rule, coupon schedule, accrual convention, compounding and clean/dirty price definitions.
- Bootstrap sequentially when instruments depend only on earlier solved nodes; use a constrained coupled solve if the specified interpolation couples nodes. Verify by repricing every calibration instrument.
- An interpolator requires strictly increasing, distinct maturities. Do not insert the origin or a trial knot twice; handle the first interval without inventing a duplicate node. Log-discount, discount, zero-rate and forward-rate interpolation are not equivalent.
- Check discount-factor positivity and instrument residuals. Do not impose globally decreasing discount factors if the supplied rate regime does not justify it.
- For sensitivities, distinguish parallel curve bumps from individual par/key-rate quote bumps. Rebuild the curve when the definition calls for quote bumps. Check per-bp versus per-unit scaling and one-sided/central schemes.
- Price liabilities and hedge instruments with consistent dates, units and multipliers. Verify dollar PV/duration/convexity constraints directly after solving weights; a solver's status alone is insufficient.
- Save each completed curve/valuation output before implementing a harder callable or risk calculation. Keep the curve code in one module and edit the last failing function rather than restarting the whole solution.
