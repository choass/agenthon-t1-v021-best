# Library migrations must preserve semantics
First execute a small probe against the installed version. Then migrate the full supplied pipeline and compare intermediate results on diagnostic inputs.

- A renamed method can have different defaults. Check unmatched mapping behavior (keep original, null or explicit default), null/NaN treatment, group ordering, join keys, rolling-window boundaries and output dtypes.
- A successful run does not establish equivalence. Include an unknown category, a null, repeated keys, out-of-order timestamps and a boundary value in the probe.
- Distinguish replace from mapping: test both a known and an unmapped value. Record the old operation's intended result before choosing the replacement.
- Preserve the original operation order and all intermediate deliverables. Sorting or filling missing values to make an output look neat may change the specified result.
- Use installed help/signatures and a tiny executable example rather than repeatedly guessing API names. If an expression fails, fix that expression with edit_file, then rerun the complete pipeline.
- Make review assertions about values, null masks, column names, dtypes and row order. Checking only file existence or number of CSVs is insufficient.
