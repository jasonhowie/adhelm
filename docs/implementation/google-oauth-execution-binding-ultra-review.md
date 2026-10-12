# Google OAuth GA4 Admin EDIT execution binding: independent ultra review

## Scope

This review covers the one-snapshot local handoff in
`src/adhelm/google_oauth_execution_binding.py` and its narrow factor in
`src/adhelm/google_oauth_credentials.py`. The independent tests use only an
in-memory protected-store fake, patched local clocks, and fake refresh results.
They do not open a browser, access an OS credential store, contact Google, or
make any provider request.

## Verified boundary

- The public resolver accepts only an exactly revalidated `Profile` with
  `credential_source=OS_OAUTH_GOOGLE`, `provider=ga4_admin`, `purpose=EDIT`,
  and the one `analytics.edit` scope. Static OS-store, explicit-CI, READ, and
  constructed/widened profiles stop before protected-bundle resolution.
- The lifecycle takes one existing credential-reference lock across private
  bundle decoding, exact profile, derived principal, client, and scope binding,
  and cache-or-refresh readback. The execution handoff does not take a
  second lock or re-read a second bearer.
- A binding has only bounded nonsecret configuration facts. Its bearer remains
  in a private single-use registry. Reuse, a different or drifted profile, a
  constructed binding, or a constructed paired snapshot cannot release a
  bearer.
- Missing, malformed, non-READY, expired, and uncertain-refresh bundle states
  fail before a usable handoff. An uncertain refresh becomes the durable
  reconnect-required state and is not retried automatically.
- A failed post-match consume, including a local clock failure, destroys its
  matching handoff before returning the fixed diagnostic.
- The snapshot, binding, and registry record are slots-only, redacted, and
  nonserializable. `repr`, `vars`, dataclass serialization, and pickle do not
  expose access tokens, refresh tokens, raw OIDC subjects, or backend error
  detail.
- The old `resolve_google_oauth_access_token(profile) -> str` adapter remains
  available for existing OAuth-primary credential callers. The new binding
  establishes local protected-grant association only; it does not claim remote
  permission, resource currentness, reservation, authorization, or CREATE
  authority.

## Findings repaired during review

| Finding | Repair verified |
| --- | --- |
| A dataclass execution snapshot and registry record allowed generic dataclass serialization to expose a bearer token. | Both are now non-dataclass slots-only classes that reject generic serialization and copying. |
| A binding could be resolved with a cached token and consumed after its expiry. | The private handoff retains token expiry, checks a fresh local clock before release, and consumes the failed binding. |
| A clock rollback between lifecycle resolution and binding registration was not visible to the later consume-time check. | The locked lifecycle core carries its final checked timestamp into the private snapshot; registration and consumption reject rollback or expiry. |

The clock checks are local process safeguards. They do not establish provider
token revocation, remote resource currentness, Google CREATE permission, or a
cross-process atomic revocation barrier. Local credential removal also does not
claim remote OAuth revocation.

## Validation

```text
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_google_oauth_execution_binding_ultra_review.py
23 passed

PYTHONPATH=src:tests .venv/bin/ruff check \
  tests/test_google_oauth_execution_binding_ultra_review.py
PYTHONPATH=src:tests .venv/bin/ruff format --check \
  tests/test_google_oauth_execution_binding_ultra_review.py
```

The broader OAuth protocol, transport, setup, browser, lifecycle, workflow,
and GA4 Admin EDIT workflow group passed 233 tests. Scoped Ruff, formatting,
mypy for the two source modules, and `git diff --check` passed.
