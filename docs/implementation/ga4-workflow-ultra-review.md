# GA4 Admin measurement workflow ultra review

Reviewed: 2026-09-04. This was a fixture-only review of the fixed read workflow,
its profile preflight, and its composition of the existing GA4 identity and
Admin inventory clients. It made no live GA4 request, accessed no actual
credential or OS key store, and performed no mutation, ledger append, or CLI
registration.

## Findings and remediation

| Severity | Finding | Remediation and regression |
| --- | --- | --- |
| P1 | `ci_enabled` was passed as an unvalidated Python value. A truthy non-Boolean caller could satisfy the credential boundary's explicit-CI branch. | `Ga4AdminReadRequest` now requires a strict Boolean. `test_nonboolean_ci_opt_in_fails_before_configuration_credentials_or_http` proves a string opt-in reaches neither configuration, credentials, nor HTTP. |
| P1 | The workflow could read a credential once for v1beta identity discovery and again for v1alpha inventory. A local rotation between those reads could select a different bearer credential mid-session. | The selected credential is resolved once after preflight and reused by every request in the one `httpx.Client` session. `test_all_session_requests_use_one_credential_snapshot` rejects a second credential read and verifies all five fixed requests use the same header. |
| P1 | A DEL or non-ASCII credential value passed the existing client token checks. DEL reached the mock HTTP handler; non-ASCII could escape as an unstructured header-encoding exception. | The workflow validates a bounded printable-ASCII snapshot before constructing `httpx.Client`, and both direct GA4 clients enforce the same range. `test_nonprintable_or_space_credential_fails_before_http` and the direct-client parameterized regressions cover space, DEL, and non-ASCII values with zero HTTP calls and a sanitized `PROVIDER_ACCESS_REQUIRED` error. |
| P1 | A programmatically forged `Configuration` or `Profile` made with `model_copy()` could bypass normal construction validation before selection. | The workflow detaches and revalidates the whole configuration and the selected profile before credential access. The principal/client and path-like native-property regressions prove malformed values fail before credentials or HTTP. |

## Verified boundary

`validate_ga4_admin_read` is a public, no-key, no-credential, no-HTTP preflight
for the selected GA4 Admin read profile. It strictly validates the request,
configuration, profile purpose, exact readonly scope, native property ID,
currency, timezone, and CI flag. `read_ga4_admin` separately verifies its HMAC
fingerprint key, captures one printable bearer credential, and then uses only
the fixed GA4 Admin host in a `trust_env=False` client.

The workflow first discovers the configured property and requested web stream
in that session, compares returned timezone and currency to the local profile,
then derives the v1alpha identity passed to the Admin reader. The reader accepts
only this observed identity and returns an inventory only after all bounded
key-event, event-create-rule, and event-edit-rule lists succeed. A failure on a
later list returns no partial inventory. Raw rule content remains inside the
parser and is represented only by the existing local keyed fingerprints and
opaque summaries.

## Validation

```text
.venv/bin/pytest -q tests/test_ga4.py tests/test_ga4_admin.py \
  tests/test_ga4_admin_ultra_review.py tests/test_ga4_measurement_workflow.py \
  tests/test_ga4_workflow_ultra_review.py
# 43 passed

.venv/bin/ruff check src/adhelm/ga4_measurement_workflow.py src/adhelm/providers/ga4.py \
  src/adhelm/providers/ga4_admin.py tests/test_ga4.py tests/test_ga4_admin.py \
  tests/test_ga4_admin_ultra_review.py tests/test_ga4_measurement_workflow.py \
  tests/test_ga4_workflow_ultra_review.py
.venv/bin/ruff format --check src/adhelm/ga4_measurement_workflow.py src/adhelm/providers/ga4.py \
  src/adhelm/providers/ga4_admin.py tests/test_ga4.py tests/test_ga4_admin.py \
  tests/test_ga4_admin_ultra_review.py tests/test_ga4_measurement_workflow.py \
  tests/test_ga4_workflow_ultra_review.py
.venv/bin/mypy src/adhelm/ga4_measurement_workflow.py src/adhelm/providers/ga4.py \
  src/adhelm/providers/ga4_admin.py
.venv/bin/python -m py_compile src/adhelm/ga4_measurement_workflow.py \
  src/adhelm/providers/ga4.py src/adhelm/providers/ga4_admin.py
# clean
```

## Remaining limits

The property/data-stream responses bind only the property, web stream, timezone,
and currency. GA4 does not return an OAuth principal or client-application
identity on these routes, so `principal_ref` and `client_ref` remain strict local
profile declarations rather than provider-verified identity or granted-scope
evidence. Discovery and the three inventory lists are sequential provider reads,
not an atomic end-of-read snapshot; downstream persistence must retain its
explicit non-atomic observation status.

This remains fixture evidence for a read-only library boundary. Live OAuth and
provider access, UI reconciliation, Admin writes, browser/Realtime behavior,
processed-report verification, and release evidence remain unresolved. The
review does not establish product completion; the state remains `IMPLEMENTING`.
