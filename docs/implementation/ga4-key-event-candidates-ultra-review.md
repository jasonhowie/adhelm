# GA4 key-event candidate ultra review

This review covered the retained offline GA4 key-event candidate boundary:
the closed compiler record, conversion-definition binding, `changes` ledger
envelope/history checks, catalog/show CLI paths, and the export projection. It
did not contact GA4, read credentials, invoke a native host, or perform a
provider mutation.

| Severity | Finding | Resolution and regression |
| --- | --- | --- |
| P1 | A constructed, fully rehashed candidate could replace the local property or web-stream reference while retaining its native client ID. The prior durable revalidation checked the target route against the client ID but did not reconstruct the two local references. A matching forged envelope account reference would then be accepted. | Durable candidate revalidation now requires `ga4_admin.property.<property_client_ref>` and `ga4_admin.web_stream.<web_stream_client_ref>` on every typed/raw reparse. The new independent regression rejects both a direct ledger append and a physically rehashed final JSONL record. The new durable APIs do not accept a caller-supplied reference mapper. |

The final implementation accepts only the declared structural subset: each
qualifying occurrence, no monetary value, `ONCE_PER_EVENT`, no default value,
and an exact Boolean field subset. Required Boolean parameters must be bound;
required non-Boolean parameters and any mismatched field type or requiredness
fail closed. The conversion definition's effective time remains
`DECLARED_NOT_EVALUATED`; no candidate claims it is current, active, or
scheduled.

The ledger checks the selected definition at the candidate's historical append
point under the `changes` lock. A later definition prevents another stale
candidate append, while a candidate retained before that later definition stays
valid historical evidence. Rehashed raw `true`, `1.0`, `0`, and `0.0` aliases
for conversion schema/authority controls are rejected before RFC 8785 can
normalize them. Rehashed candidate field-requiredness mismatches likewise fail
the verified history scan.

The CLI's explicit `--retain` receipt and bounded catalog require no sharing
key. Full preview/show and `history export` use the closed review projection:
they omit native property/stream IDs, targets, request bodies, event and field
names, source-event prose, and local references in cleartext. The projection
preserves candidate and source digests, mapping/effective-time uncertainty,
prospective effects, and every unresolved planning gate. This remains an
offline candidate; it proves neither source-event semantics nor current
identity, consent enforcement, authorization, readback, browser behavior,
receipt, processed reporting, or a native advertising conversion mapping.

Validation completed on the frozen source with:

```text
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_ga4_key_event_candidates.py \
  tests/test_saved_ga4_key_event_candidates.py \
  tests/test_ga4_key_event_candidate_cli.py \
  tests/test_ga4_key_event_candidates_ultra_review.py \
  tests/test_conversion_history.py \
  tests/test_conversion_ultra_review.py
# 83 passed

.venv/bin/ruff check src/adhelm/ga4_key_event_candidates.py \
  src/adhelm/saved_ga4_key_event_candidates.py \
  src/adhelm/ga4_key_event_candidate_cli.py \
  src/adhelm/ga4_key_event_candidate_views.py \
  src/adhelm/ledger.py src/adhelm/projections.py \
  tests/test_ga4_key_event_candidates_ultra_review.py

.venv/bin/ruff format --check src/adhelm/ga4_key_event_candidates.py \
  src/adhelm/saved_ga4_key_event_candidates.py \
  src/adhelm/ga4_key_event_candidate_cli.py \
  src/adhelm/ga4_key_event_candidate_views.py \
  tests/test_ga4_key_event_candidates_ultra_review.py

.venv/bin/mypy src/adhelm/ga4_key_event_candidates.py \
  src/adhelm/saved_ga4_key_event_candidates.py \
  src/adhelm/ga4_key_event_candidate_cli.py \
  src/adhelm/ga4_key_event_candidate_views.py src/adhelm/ledger.py src/adhelm/projections.py
```

The independent suite contains 14 passing cases. No open scoped authority,
replay, raw-type, stale-binding, output-redaction, or key-access defect remains
after the P1 repair.
