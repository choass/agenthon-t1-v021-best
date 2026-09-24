# Filing and XML extraction
Implement a reusable parser after a small structural probe. Repeatedly printing full filings is not an extraction algorithm.

- Inventory root tags, namespaces and a few records. Use namespace-aware queries or explicitly strip namespaces once. XML Element truthiness is not an existence test: use 'node is None' and separately inspect text/children.
- Normalize dates, identifiers, units and transaction signs into a table with provenance (document, record index, source tag). Preserve amendment and original-record relationships according to the task.
- Many financial XML fields nest text under a value child; absent elements, empty strings and genuine numeric zero must remain distinguishable.
- Build a table of classification rules from the instruction. Use bounded footnote/tag queries, and apply the rules over every record. Do not manually transcribe a few filings or infer labels from ticker names.
- Parse long documents locally and print only matched excerpts, counts and ambiguity diagnostics. Save parsed records to disk so subsequent calls do not reread full documents.
- Assert reconciliation identities, date coverage, identifier joins and count changes at each stage. Check default/excluded categories, rating order and sale/buy direction with a small explicit example.
- Complete the parser and emit the requested tables early; add corner-case handling incrementally. Never fabricate missing fields or silently discard unparsed records.
