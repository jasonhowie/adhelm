# GA4 Admin execution-observation ultra review

Status: independent fake-only review of the M24 execution-bound inventory
observation boundary. No Google request, browser, OS credential store, keychain,
or live OAuth token was used.

## Boundary reviewed

`ga4_admin_execution_observation.py` accepts only a configured Google OAuth
GA4 Admin `EDIT` profile with the exact `analytics.edit` scope. It binds the
configuration and profile facts to the M23 one-use OAuth handoff, captures one
in-memory bearer, and uses it for the fixed property, selected web-stream, and
bounded paginated key-event GET sequence. Static-store, CI, malformed, wrong
provider, wrong-purpose, and widened-scope profiles are rejected before the
fingerprint key, protected lifecycle, or HTTP are reached.

The reviewed result is an internal typed planning input. It contains no bearer,
refresh token, raw OIDC subject, bundle bytes, fingerprint key, raw provider
response, or raw event name. It is not itself an authorization, a Google CREATE
permission, a grant, a reservation, or an agent-visible projection.

## Independent adversarial coverage

The review test uses a memory-only credential backend and `httpx.MockTransport`.
It verifies:

- all fixed GETs, including a second key-event page, use the same captured
  bearer and the closed request masks;
- non-OAuth sources and malformed request scalars stop before protected
  lifecycle or fingerprint-key access;
- a constructed binding with a mismatched profile cannot consume a bearer or
  reach HTTP;
- configuration drift after binding resolution prevents bearer consumption;
- unexpected provider response fields fail closed without disclosing private
  sentinels;
- direct constructed OAuth facts, copied invalid flags, reversed intervals,
  wrong fingerprint keys, and copied snapshot rewrites cannot pass the public
  reuse boundary; and
- the returned facts retain explicit limits: local sealed-bundle association,
  `NOT_EVALUATED` granted scopes, `NOT_PREPROVABLE` provider CREATE permission,
  `NOT_ESTABLISHED` current state and reservation, false authority/mutation
  controls, and `SEQUENTIAL_NONATOMIC` consistency.

During review, a valid-shape `model_copy()` could change the configuration
digest or a monotonic read interval because those fields had no observation-level
integrity binding. The source owner added a domain-separated current-fingerprint
keyed `observation_content_marker` over the complete closed observation surface.
`revalidate_ga4_admin_edit_execution_observation()` now recomputes that marker,
so those copied changes fail as
`INVALID_GA4_ADMIN_EDIT_EXECUTION_OBSERVATION` before reuse. The regression also
covers a direct marker overwrite.

`model_dump()` alone checks the closed structural shape but cannot independently
authenticate a caller-owned fingerprint key. Consumers that use an observation
for retention, planning, or any agent-visible result must first call the keyed
revalidator. This module does not claim atomicity with local configuration edits
or Google changes: configuration is checked at defined local boundaries, the
fixed GETs remain sequential, and a change that occurs and reverts between
checks is outside this local detection guarantee.

## Validation

After the observation-marker repair:

```sh
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_ga4_admin_execution_observation_ultra_review.py
# 15 passed

PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_ga4_admin_execution_observation.py \
  tests/test_ga4_admin_execution_observation_ultra_review.py \
  tests/test_google_oauth_execution_binding.py \
  tests/test_google_oauth_execution_binding_ultra_review.py \
  tests/test_ga4_admin_edit_inventory.py \
  tests/test_ga4_admin_edit_inventory_ultra_review.py \
  tests/test_ga4_admin_access_workflow.py \
  tests/test_ga4_admin_access_ultra_review.py \
  tests/test_ga4_admin_edit_workflow.py \
  tests/test_ga4_admin_edit_workflow_ultra_review.py
# 177 passed

.venv/bin/ruff check src/adhelm/ga4_admin_execution_observation.py \
  tests/test_ga4_admin_execution_observation.py \
  tests/test_ga4_admin_execution_observation_ultra_review.py
.venv/bin/ruff format --check src/adhelm/ga4_admin_execution_observation.py \
  tests/test_ga4_admin_execution_observation.py \
  tests/test_ga4_admin_execution_observation_ultra_review.py
.venv/bin/mypy src/adhelm/ga4_admin_execution_observation.py
git diff --check
```

All listed static checks passed. The independent test file is
`tests/test_ga4_admin_execution_observation_ultra_review.py`.
