# GA4 key-event CREATE workflow ultra review

This independent review covers the configured local planning and retained
history boundary in `ga4_create_workflow.py`, `ga4_create_plan_records.py`,
`ga4_create_plan_history.py`, `saved_ga4_create_plans.py`,
`ga4_create_plan_views.py`, and their ledger/export integration. It uses only
fake OAuth state and `httpx.MockTransport`; it does not contact Google, open an
OS credential store, or authorize or send a provider mutation.

## Boundary verified

The workflow accepts fresh source input plus a configured GA4 Admin EDIT
profile, resolves its own protected local binding, and completes its fixed
GET sequence before acquiring a configuration or ledger retention lock. It
retains an EDIT observation followed by a zero-spend, non-authorizing CREATE
plan. The plan records the exact source candidate, latest effective conversion
contract, retained inventory, workspace/configuration/profile bindings, local
OAuth association facts, read interval, and bounded expiry. It has no installed
execution command and keeps its capability status at
`DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`.

The sharing path removes the nested retained execution observation before
export. It replaces it with
`RETAINED_LOCAL_OAUTH_BINDING_NOT_AUTHENTICATED_BY_EXPORT`; native request
material, event names, resource identifiers, raw OAuth facts, and keyed
markers remain behind the existing redaction boundary.

## Findings repaired during review

- A copied action could originally alter source configuration or broad
  inventory provenance while retaining the same reconstructed `CompositePlan`
  digest. The action contract now commits the complete closed candidate,
  selection, observation, execution, native-request, semantic-state,
  workspace, and expiry surfaces to its plan identifier.
- A self-consistent rebuilt action could otherwise claim a different OAuth
  principal than the observation that supplied the inventory. The retained
  EDIT observation now optionally carries the complete sealed execution
  observation; the supported plan retention path current-key revalidates it,
  and physical plan history compares its profile, principal, client, scope,
  and target to the action. Legacy records preserve omitted proof bytes;
  explicit or constructed `null` proof values fail closed.
- The plan helper checked its selected checkpoint before calling `Ledger.append`,
  leaving a race for another stream to append before the plan’s durable write.
  `Ledger` now acquires all stream locks in checkpoint order for this plan kind
  and compares the full current checkpoint vector under those locks. Both an
  unrelated changes record and an unrelated creative record now block plan
  retention with `GA4_CREATE_PLAN_HISTORY_CHANGED`.

## Independent coverage

`tests/test_ga4_create_workflow_ultra_review.py` adds ten adversarial cases:

- rehashed copied-action provenance cannot preserve an original plan;
- a rebuilt action with an unobserved OAuth principal is rejected by retained
  source history;
- legacy proof omission remains byte-compatible while explicit null and
  constructed null fail;
- a newer selected EDIT observation blocks a stale action;
- changes-stream and creative-stream append races both invalidate the full
  selected checkpoint before durable plan append;
- provider reads occur with no changes-stream lock held;
- the final under-lock callback performs no ledger I/O;
- configuration drift at the final handoff leaves the observation but prevents
  plan retention; and
- export does not serialize the retained execution-proof object or its marker.

The review deliberately does not treat a local keyed marker as provider proof.
It proves only current-key integrity at the keyed retention/reuse boundary.
It does not prove Google origin, current remote state, granted scope,
CREATE permission, name reservation, atomicity of the sequential GETs, or a
mutation outcome. A later guarded adapter still needs a fresh protected OAuth
binding, reread, exact authorization, one guarded POST, ambiguity reconciliation
without blind retry, and verified readback.

## Validation

```sh
.venv/bin/pytest -q \
  tests/test_ga4_create_workflow.py \
  tests/test_ga4_create_workflow_ultra_review.py \
  tests/test_ga4_create_contract.py \
  tests/test_ga4_create_contract_ultra_review.py \
  tests/test_ga4_admin_execution_observation.py \
  tests/test_ga4_admin_execution_observation_ultra_review.py \
  tests/test_ga4_create_selection.py \
  tests/test_ga4_create_selection_ultra_review.py
# 114 passed

.venv/bin/ruff check [reviewed sources and independent test]
.venv/bin/ruff format --check [reviewed sources and independent test]
.venv/bin/mypy [reviewed source modules]
```

All listed scoped static checks passed.
