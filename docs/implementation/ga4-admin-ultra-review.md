# GA4 Admin ultra review

Reviewed 2026-09-04 against the fixed Google Analytics Admin API `v1alpha`
list contracts for key events, event-create rules, and event-edit rules. This
review used only fixture transports and official public reference material; it
made no provider request.

## Fixed findings

- **P1 — documented edit rules were parsed as create rules.** `EventEditRule`
  responses carry `displayName`, `eventConditions`, `parameterMutations`, and
  signed-int64 `processingOrder`; they do not carry `destinationEvent` or
  `sourceCopyParameters`. The adapter now has distinct closed shapes, retains
  no display name, represents no source-copy fact for edit rules, and rejects
  missing or malformed ordering metadata.
- **P1 — native IDs could be labeled with unrelated local references.** The
  adapter now verifies both property and web-stream references against the
  configured deterministic reference function before the verifier, token
  callback, or HTTP request.
- **P1 — public SHA-256 digests of raw rule content were exposed.** Conditions,
  mutation values, display names, and URLs can contain sensitive customer data.
  The adapter now requires an injected private local key and produces
  domain-separated HMAC-SHA-256 fingerprints, marked
  `HMAC_SHA256_LOCAL_V1`; it never keeps the raw fields or a guessable raw
  digest.
- **P2 — rule metadata accepted unsupported semantics.** Empty condition lists,
  arbitrary comparison types, duplicate or malformed parameter mutations,
  oversize fields, invalid create destinations, and noncanonical/out-of-range
  signed processing-order values now fail before an inventory is returned.
- **P2 — default-value magnitude was not actually bounded.** The prior
  significant-digit check accepted values such as `1e1000000`. The retained
  Decimal now has bounded precision and exponent.

## Validation

`tests/test_ga4_admin.py` and
`tests/test_ga4_admin_ultra_review.py` passed: **12 tests**. The latter adds
documented edit-rule parsing, raw-content privacy, keyed-fingerprint variance,
identity mismatch before token/egress, custom reference mapping, unsupported
rule semantics, and missing-key checks. Ruff and mypy passed for the changed
adapter and tests.

## Remaining boundary

This is a fixture-tested, read-only adapter. A caller must provide a real
current-session identity proof, a stable private HMAC key, and a reference
mapping compatible with the discovered GA4 identity. It has no Admin mutation,
plan, authorization, ledger persistence, or live-provider evidence yet; those
remain separate release gates.
