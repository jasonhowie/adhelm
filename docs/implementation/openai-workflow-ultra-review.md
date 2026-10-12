# OpenAI conversion measurement workflow ultra review

Reviewed: 2026-09-04. This was a fixture-only review of the read-only workflow,
the account-discovery cache it composes, and the fixed conversion-setting reader.
It made no live provider request, credential-store read, mutation, ledger append,
or CLI registration.

## Findings and remediation

| Severity | Finding | Remediation and regression |
| --- | --- | --- |
| P1 | Account discovery and each conversion page called the local credential getter independently, so a rotation could bind discovery and inventory to different bearers. | The workflow resolves one printable-ASCII credential snapshot after preflight and uses it for the whole `httpx` session. `test_whole_session_uses_one_credential_snapshot` rejects a second getter call across identity plus two conversion pages. |
| P1 | A structurally valid but semantically invalid profile account ID or timezone could reach credential access and account discovery. | Profile preflight now requires the same bounded opaque account-ID grammar used for provider IDs and an IANA timezone. Parameterized regressions prove both fail before HTTP-client construction or credential access. |
| P1 | `is_current_identity` kept only the caller-visible identity object. An in-place mutation of that cached object could retain object identity and start a child read under altered identity fields. | Discovery now retains an independent revalidated snapshot and all direct child reads use `is_current_identity`. `test_mutated_cached_identity_is_not_current_and_cannot_start_child_egress` proves a changed cached object cannot reach `/campaigns`. |
| P1 | The direct OpenAI Ads client allowed DEL and non-ASCII bearer characters to reach shared header handling instead of returning the adapter's sanitized access error. | The adapter now accepts only bounded printable-ASCII tokens. The direct-client parameterized regression covers whitespace, DEL, and non-ASCII values with zero HTTP calls. |
| P2 | A bounded but non-IANA account timezone from provider data could become a cached identity and be compared as a bare string. | Account discovery now rejects it as `PROVIDER_RESPONSE_INVALID` before caching; `test_invalid_provider_timezone_stops_identity_before_conversion_egress` covers the boundary. |

## Verified boundary

`OpenAIConversionsReadRequest` is a public strict schema with
`schema_version: 1`; it keeps the existing record/page limits and strict Boolean
CI opt-in. Before egress, the workflow detaches and revalidates configuration,
the selected `openai_ads`/`READ` profile, local principal/client declarations,
declared scope structure, account ID, currency, timezone, and one credential
snapshot. The conversion reader still receives `OpenAIAdsClient.is_current_identity`,
which proves the exact discovery object and its unchanged snapshot before token
retrieval or the fixed current-account settings route.

Only `GET /v1/ad_account` and then fixed, forward-paginated
`GET /v1/conversions/event_settings` can occur. The complete settings list is
buffered in memory; a late page error returns no tuple. Account and setting
display names, source IDs/names, campaign association values, tokens, and raw
provider response bodies remain transient. The raw-account display-name
regression verifies the identity/result models do not retain that value.

The adjacent shared `SafeHttpClient` header check was independently exercised:
it rejects malformed names, duplicate case-insensitive names, non-string values,
CR/LF, controls, DEL, non-ASCII, and overlong explicit values before mock HTTP.
It permits the documented ASCII provider headers unchanged. This hardens header
construction; it does not establish live provider authentication.

## Validation

```text
.venv/bin/pytest -q tests/test_openai_ads.py tests/test_openai_conversions.py \
  tests/test_openai_measurement_workflow.py tests/test_openai_workflow_ultra_review.py
# 69 passed

.venv/bin/pytest -q tests/test_transport.py tests/test_transport_headers.py
# 13 passed

.venv/bin/ruff check src/adhelm/openai_measurement_workflow.py \
  src/adhelm/providers/openai_ads.py src/adhelm/transport.py \
  tests/test_openai_measurement_workflow.py tests/test_openai_workflow_ultra_review.py \
  tests/test_transport.py tests/test_transport_headers.py
.venv/bin/ruff format --check src/adhelm/openai_measurement_workflow.py \
  src/adhelm/providers/openai_ads.py src/adhelm/transport.py \
  tests/test_openai_measurement_workflow.py tests/test_openai_workflow_ultra_review.py \
  tests/test_transport.py tests/test_transport_headers.py
.venv/bin/mypy src/adhelm/openai_measurement_workflow.py \
  src/adhelm/providers/openai_ads.py src/adhelm/transport.py
.venv/bin/python -m py_compile src/adhelm/openai_measurement_workflow.py \
  src/adhelm/providers/openai_ads.py src/adhelm/transport.py
# clean
```

## Remaining limits

The profile's scope, principal, and client values are strict local declarations.
The reviewed account route does not return a provider-verified OAuth principal,
client application, granted scope, or API-key lifecycle proof. The provider's
Conversion Setup reference documents the fixed route and request parameters but
does not publish a list-response example, so the closed response envelope and
setting shape remain fixture contracts. Identity discovery and settings pages are
sequential reads rather than an atomic provider snapshot.

Live access, least-privilege verification, conversion delivery, browser firing,
processed reporting, UI reconciliation, writes, and release evidence remain
unresolved. This review does not establish product completion; the workflow
remains `IMPLEMENTING`.
