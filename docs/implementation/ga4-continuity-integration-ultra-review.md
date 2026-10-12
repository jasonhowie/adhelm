# GA4 continuity integration ultra review

This independent review exercised the M18 measurement-comparison integration
against physical, self-consistent JSONL rewrites.  It did not use a provider,
credential store, export key, network request, or filesystem artifact outside
pytest's temporary local directory.

The review added `tests/test_ga4_continuity_integration_ultra_review.py`.
It verifies that a final GA4 observation can be physically rehashed after its
recorded key marker changes, yet comparison stops with
`MEASUREMENT_COMPARISON_FINGERPRINT_KEY_MISMATCH` before normalized facts are
constructed.  The same mismatch through a matching saved artifact rule fails
before sharing-key access and before creating a file.

It also rewrites the physical final JSONL record with malformed marker values
and original JSON scalar aliases, including `schema_version: 1.0` and
`execution_authority: 0.0`.  The scan rejects these as
`INVALID_LEDGER_PAYLOAD`; canonicalization cannot turn them into accepted
integer or Boolean controls.  A markerless legacy pair keeps the field absent
from its stored and reloaded wire shape and reports `NOT_EVIDENCED`.

The artifact boundary was checked with two syntactically valid, constructed
comparison objects: a matching pair relabeled `NOT_EVIDENCED`, and a legacy
pair relabeled `MATCHING_RECORDED_KEY_MARKERS`.  Both fail source
reconstruction before the supplied key or output directory can be used.

The marker status records only that two historical GA4 inventories were made
with matching locally configured fingerprint keys.  It does not prove current
key configuration, remote identity, collection semantics, duplicate status,
permissions, consent, browser firing, processed receipt, or authorization.
The JSONL digest chain detects ordinary corruption but is not a claim that a
party able to rewrite and rehash local history cannot do so.  No artifact or
comparison output exposes the marker or its private key.
