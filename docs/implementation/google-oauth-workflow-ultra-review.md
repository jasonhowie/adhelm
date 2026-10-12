# Google OAuth workflow ultra review

> Note (2026-10-12): Google retired Google Ads developer tokens on 2026-09-09, and AdHelm no longer stores or sends one (G1). The developer-token text below is a historical record of the state when it was written.

This independent review covers the local configuration transaction, draft and
private-client intake, enrollment/reconnect workflow, CLI registration, and
the protected install-slot boundary. It uses only fake stores, synthetic
grants, temporary configuration roots, and patched browser handoffs. It does
not open a browser, bind a socket, contact Google, use an OS credential store,
or create workflow artifacts.

The review verifies that invalid original JSON stops before a client-file read,
credential-slot read, or browser handoff. Both absent `--interactive` and an
explicit request without a local TTY fail before configuration or file work.
The CLI emits a fixed, bounded failure envelope rather than reflecting draft,
token, client-secret, or raw-subject values.

Enrollment snapshots configuration and the credential slot before the browser
handoff. A changed configuration rejects before profile or bundle storage. A
slot removed during reconnect rejects at the final locked comparison and is
not recreated. A failed config-first bundle write leaves the nonsecret profile
in place with an ambiguous local-state outcome; recovery requires a deliberate
same-identity reconnect. The review also covers recovery after an explicitly
removed local bundle. It does not claim that manual edits or another process
outside the local coordination locks are atomically controlled.

The install snapshot contains a per-process, domain-separated HMAC of the
observed protected slot, never the raw bundle. Independent checks cover
`repr`, `dataclasses.asdict`, field access, `vars`, and a bare constructed
snapshot. Reconnect now requires the configuration-bound snapshot helper;
the prior path that accepted a configless `replace=True` snapshot was repaired
before this review froze. This is an in-process capability boundary, not a
defense against arbitrary Python code in the same process.

OAuth primary status and local removal use the dedicated protected-bundle
service under the same credential-reference lock. The independent workflow
test verifies that a successful primary reports local presence from that
service, then reports missing after source-aware local removal. Google Ads'
secondary developer-token slot remains a separate generic credential fact.

For Google Ads, the completed receipt fits the minimum 1,024-byte output
budget and says only that the OAuth primary is ready while the separate
developer token remains unevaluated. It retains `resource_access_verified:
false` and `execution_authority: false`; no account access, mutation authority,
or remote revocation is inferred. No enrollment or reconnect test creates a
history record, artifact, or provider operation.

The independent suite is
[`tests/test_google_oauth_workflow_ultra_review.py`](../../tests/test_google_oauth_workflow_ultra_review.py).
It currently contains 13 adversarial cases. Focused validation was run with:

```sh
PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_configuration_transactions.py \
  tests/test_credential_setup.py \
  tests/test_google_oauth_setup.py \
  tests/test_google_oauth_protocol.py \
  tests/test_google_oauth_transport.py \
  tests/test_google_oauth_workflow.py \
  tests/test_google_oauth_credentials.py \
  tests/test_google_oauth_browser_ultra_review.py \
  tests/test_google_oauth_workflow_ultra_review.py
.venv/bin/ruff check src/adhelm/config.py src/adhelm/google_oauth_setup.py \
  src/adhelm/google_oauth_workflow.py src/adhelm/google_oauth_cli.py \
  tests/test_google_oauth_workflow_ultra_review.py
.venv/bin/ruff format --check src/adhelm/config.py src/adhelm/google_oauth_setup.py \
  src/adhelm/google_oauth_workflow.py src/adhelm/google_oauth_cli.py \
  tests/test_google_oauth_workflow_ultra_review.py
.venv/bin/mypy src/adhelm/config.py src/adhelm/google_oauth_setup.py \
  src/adhelm/google_oauth_workflow.py src/adhelm/google_oauth_cli.py
git diff --check
```

The combined fake-only group passed 138 tests; Ruff, format, scoped mypy, and
the diff check passed.
