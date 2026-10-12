# OpenAI pause CLI workflow ultra review

Reviewed: 2026-09-05  
Status: `IMPLEMENTING`; deterministic fixture-only evidence, not live validation.

## Scope

This review covers `src/adhelm/pause_workflow.py`, `src/adhelm/plan_cli.py`, the
pause request schema/discovery wiring, the workspace identity in
`src/adhelm/plan_views.py`, and the installed-wheel smoke additions in
`scripts/validate_development.py`. The independent regressions are in
`tests/test_pause_cli_workflow_ultra_review.py`.

Every provider interaction in the tests uses the existing `httpx.MockTransport`
fixture. Credential and sharing-key functions are replaced with in-memory test
values. The review does not access a provider, credential store, DNS service,
approval host, or execution command.

## Finding fixed

| Severity | Reproduction | Fix |
| --- | --- | --- |
| P2 | `plan catalog` emitted the default `OK` result outcome while `plan openai-pause` and `plan show` explicitly reported `PLAN_REVIEW_NO_EXECUTION_AUTHORITY`. Its fit probe measured the shorter default envelope. A caller consuming only the envelope could therefore miss the command family's non-authority boundary. | Catalog now emits `PLAN_REVIEW_NO_EXECUTION_AUTHORITY` and measures that exact envelope before selecting a bounded page. |

## Verified boundaries

- A `model_construct()` request with an arbitrary nested value is rejected
  before configuration, credential, binding-key, or HTTP construction. Its
  arbitrary `repr` does not appear in a diagnostic or process output.
- Configuration, a verified checkpoint, and unresolved-history recovery checks
  precede every private-key read and provider request. An unresolved start
  returns `RECOVERY_REQUIRED` without touching either boundary.
- Planning captures the account key once. Every fixed account, inventory, and
  campaign GET uses that same in-memory credential snapshot. A configured
  currency mismatch stops after the account GET and before child inventory or
  campaign reads.
- If time passes while the private binding key is read, planning rechecks expiry
  before its first provider GET. If time passes during bounded GETs, the
  workflow returns `PLAN_EXPIRED` and provides no action for retention.
- The complete redacted review is sized with a fixed test key before the real
  sharing key is read or a plan is appended. An over-bound output cannot touch
  either later step. A storage `OSError` becomes
  `PLAN_RETENTION_UNVERIFIED`; it never returns a false persisted receipt or
  exposes the raw storage error.
- Catalog uses verified local history and no sharing key. Show verifies the
  selected digest, sizes its redacted review before the sharing key, and emits
  the same explicit non-authority outcome. Account, campaign, credential
  profile, workspace profile, native IDs, provider key, and credential-binding
  HMAC are absent from catalog and show output.
- A new action displays separate opaque credential-profile and workspace-profile
  references when those identities differ. A legacy action retains its prior
  routing relation: its opaque workspace and credential profile references are
  equal when the original plan used the credential profile as its workspace
  profile.
- Installed development smokes block socket connect, `connect_ex`, address
  lookup, forward/reverse hostname lookup, and the credential backend. Their
  credential-lock root is isolated under the smoke state directory. The new
  schema and empty plan-catalog checks therefore do not exercise a real key
  store or network path.

## Limits

The retained action is historical local evidence. It does not establish current
provider state, provider-enforced scopes, an execution grant, an approval-host
decision, a durable mutation start, a pause result, or live provider
conformance. No execution command is installed. The v1 completion gate remains
open.

## Validation

```text
.venv/bin/pytest -q tests/test_pause_cli_workflow.py tests/test_pause_cli_workflow_ultra_review.py tests/test_openai_pause_view.py tests/test_pause_plan_view_ultra_review.py tests/test_openai_pause_workspace_binding.py tests/test_pause_workspace_ultra_review.py tests/test_validate_development.py tests/test_openai_pause_binding_key_ultra_review.py
# 122 passed

.venv/bin/ruff check src/adhelm/pause_workflow.py src/adhelm/plan_cli.py src/adhelm/plan_views.py tests/test_pause_cli_workflow_ultra_review.py tests/test_pause_plan_view_ultra_review.py tests/test_validate_development.py
.venv/bin/ruff format --check src/adhelm/pause_workflow.py src/adhelm/plan_cli.py src/adhelm/plan_views.py tests/test_pause_cli_workflow_ultra_review.py tests/test_pause_plan_view_ultra_review.py tests/test_validate_development.py
.venv/bin/mypy src/adhelm/pause_workflow.py src/adhelm/plan_cli.py src/adhelm/plan_views.py tests/test_pause_cli_workflow_ultra_review.py tests/test_pause_plan_view_ultra_review.py tests/test_validate_development.py
# clean
```
