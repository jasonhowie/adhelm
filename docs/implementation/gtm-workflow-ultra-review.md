# GTM identity and measurement-read ultra review

Reviewed: 2026-09-04  
Status: fixture-tested local library composition; not live capability evidence.

## Scope

This review covers the fixed GTM v2 identity proof in
`src/adhelm/providers/gtm_identity.py` and its composition with the bounded
inventory reader in `src/adhelm/gtm_measurement_workflow.py`. It also covers the
adversarial fixture cases in `tests/test_gtm_workflow_ultra_review.py`.

The reviewed path is read-only and library-only. It adds no `adhelm` command,
ledger append, artifact, workspace action, dependency claim, staging decision,
preview, browser request, credential configuration, or remote mutation. Tests
use `httpx.MockTransport`; no provider request, credential-store access, or
customer GTM data was used.

## Source check

The review rechecked the primary GTM v2 documentation for
[account reads](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts/get),
[container reads](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers/get),
[the live-version read](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.versions/live),
[the latest-version header](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.version_headers/latest),
and [workspace pagination](https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces/list).
Those sources support the fixed `GET` paths and readonly OAuth scope recorded in
[`gtm-identity-read-contract.md`](../research/gtm-identity-read-contract.md) and
[`gtm-read-contract.md`](../research/gtm-read-contract.md).

## Findings fixed

| Severity | Reproduced issue | Fix |
| --- | --- | --- |
| P1 | `SafeHttpClient` exposes mutable response-cap and retry settings. A client that had passed construction could later buffer over 8 MiB or exceed the three-read-attempt bound before identity discovery noticed. | Revalidate fixed-host, response-cap, and retry bounds at the beginning of every discovery, after clearing prior proof and before token access or HTTP. |
| P1 | Local reference construction happened after the two provider identity reads. A malformed custom reference mapper could therefore make authorized GTM reads before the local identity model was known to be valid. | Construct and validate the bound `GtmReadIdentity` from validated public IDs before obtaining the token or issuing either request. |
| P1 | GTM container, workspace, and container-version IDs are parent-scoped. ID-only workflow references could collide when one local ledger retains observations from more than one container. | The workflow now passes one account/container-scoped mapper to both identity and inventory clients: `gtm.container.{account}.{container}`, `gtm.workspace.{account}.{container}.{workspace}`, and `gtm.container_version.{account}.{container}.{version}`. Standalone reader defaults remain unchanged. |
| P2 | The stale-proof test only covered a failed response refresh. It did not prove that a malformed refresh argument or a mutable transport failure invalidated the earlier exact-object proof. | Added regression coverage that both local failure modes clear the prior identity and make no new credential or HTTP call. |
| P2 | A Pydantic `model_copy()` can bypass validation on an in-memory `Profile`; the workflow needed direct proof that forged principal/client references fail before credential or transport construction. | Added regression cases for forged principal and client references through the detached configuration revalidation boundary. |
| P2 | An unexpected token, custom-reference, or workflow credential callback exception could expose arbitrary local text through its message or chained cause. That risks putting a caller-supplied secret or customer detail into diagnostics. | Unexpected callback failures now become sanitized `PROVIDER_ACCESS_REQUIRED` or `IDENTITY_BINDING_INVALID` diagnostics with the original exception context suppressed before HTTP. |

## Verified boundaries

- `validate_gtm_read()` validates the selected profile before credential access;
  an absent selected GTM profile reports `NOT_CONFIGURED_OPTIONAL` without
  changing unrelated profiles.
- `read_gtm()` uses one printable-ASCII credential snapshot in one
  `httpx.Client(trust_env=False)` session, limited to
  `tagmanager.googleapis.com`.
- `GtmReadRequest` is the versioned public request contract. Workflow-local
  references scope all parent-scoped GTM resources, while direct library users
  retain the standalone defaults documented by the fixed readers.
- Account and container responses bind native IDs and exact paths before any
  inventory route. Only exactly `usageContext: ["web"]` is accepted.
- The inventory reader receives the exact discovery identity and the discovery
  client's object-identity verifier. Cloned, constructed, stale, and
  failed-refresh identities cannot authorize its follow-up reads.
- Feature absence remains `UNKNOWN`; it is never rewritten to `FALSE`.
- Names, URLs, domains, notes, provider fingerprints, custom-code source, and
  credential values remain transient and are absent from normalized results and
  diagnostics.
- The inventory result remains explicitly
  `SEQUENTIALLY_REREAD_NONATOMIC` and `NOT_EVALUATED`. It proves neither
  workspace completeness, dependencies, staging eligibility, compiler/preview
  success, browser behavior, consent, GA4 receipt, nor publish readiness.

## Validation

```text
uv run pytest -q tests/test_gtm_identity.py tests/test_gtm_measurement_workflow.py tests/test_gtm_workflow_ultra_review.py tests/test_gtm_read.py tests/test_gtm_read_ultra_review.py tests/test_gtm_counts.py
# 69 passed

uv run ruff check src/adhelm/providers/gtm_identity.py src/adhelm/gtm_measurement_workflow.py tests/test_gtm_identity.py tests/test_gtm_measurement_workflow.py tests/test_gtm_workflow_ultra_review.py
uv run ruff format --check src/adhelm/providers/gtm_identity.py src/adhelm/gtm_measurement_workflow.py tests/test_gtm_identity.py tests/test_gtm_measurement_workflow.py tests/test_gtm_workflow_ultra_review.py
uv run mypy src/adhelm/providers/gtm_identity.py src/adhelm/gtm_measurement_workflow.py
# clean
```

## Remaining limits

This is fixture evidence only. Live validation of GTM read profiles, edit/version
and publish profiles; complete inventory; workspace-content and dependency analysis; safe staging;
`quick_preview`; real-browser/consent validation; GA4 receipt and processed
reporting; version creation; publish; restore; and live-account verification
remain separate required work under the measurement contract.

The installed command and retained-history composition are reviewed separately
in [GTM history review](gtm-history-ultra-review.md).
