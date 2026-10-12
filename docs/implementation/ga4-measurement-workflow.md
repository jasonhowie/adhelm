# GA4 Admin measurement read workflow

`adhelm.ga4_measurement_workflow.read_ga4_admin` is the read-only workflow
component used by `measurement ga4-admin` for the existing fixed GA4 Admin inventory. It accepts one explicit
`ga4_admin` profile with purpose `READ`, exactly
`https://www.googleapis.com/auth/analytics.readonly`, a configured native GA4
property ID, expected timezone/currency, and an expected numeric web-stream ID.

`Ga4AdminReadRequest` is the public, schema-versioned (`schema_version: 1`)
strict request model for those profile, stream, and CI inputs. Its defaults
preserve a disabled CI opt-in; callers can obtain its JSON Schema without
loading configuration, a credential, or a key.

`validate_ga4_admin_read(root, profile_id, stream_id=..., ci_enabled=...)`
is the public preflight for callers that need to validate those inputs before
obtaining a local fingerprint key or credential. It returns only a detached,
revalidated selected profile and performs no credential-store access or HTTP.

Before credential-store access or HTTP, the public preflight validates public
arguments including the strict Boolean `ci_enabled` opt-in, detaches and
revalidates the local configuration/profile, and checks the exact scope, native
property identifier, expected currency, and timezone. `read_ga4_admin` then
validates its local HMAC fingerprint-key length before it accesses a credential.
Its credential getter receives only the selected profile. The workflow resolves
that credential once after preflight and uses the same snapshot for identity
discovery and every fixed inventory page, preventing a local credential reread
from selecting a different credential between those reads. The snapshot must be
a bounded printable-ASCII bearer token; malformed, whitespace, DEL, and
non-ASCII values fail before the HTTP client is created. The direct GA4 Data and
Admin read clients apply the same token range for callers using those libraries
outside this workflow.

The workflow uses one `httpx.Client(trust_env=False)` and one fixed-host
`SafeHttpClient` for both reads. It first calls `Ga4DataClient.discover_identity`
against GA4 Admin v1beta, binding the configured property and expected web stream
to the property timezone and currency returned in that session. It then derives a
canonical v1alpha `Ga4AdminIdentity` and passes a verifier that accepts only that
same observed identity to `Ga4AdminReadClient`. The inventory reader remains
bounded to its fixed key-event, event-create-rule, and event-edit-rule lists.

The workflow performs no CLI registration, ledger append, artifact generation,
remote mutation, retry of writes, or persistence of raw provider responses.
The caller must create any observation interval and durable measurement receipt.
Rule source, names, URLs, and other raw response content remain inside the
provider parser; normalized output uses the existing keyed fingerprints and
opaque rule summaries.

The Admin property/data-stream endpoints establish only the configured property,
web stream, timezone, and currency for this read. They do not return an OAuth
principal or client-application identity. `principal_ref` and `client_ref` are
therefore strict local profile declarations revalidated before credential access;
they are not reported as provider-verified identity evidence.

`tests/test_ga4_measurement_workflow.py` uses mocked HTTP to cover the session
call sequence, profile purpose/scope rejection before credential access, property,
currency, and web-stream mismatches, failed Admin pagination, and raw-rule
redaction. The adversarial review also verifies the public request schema,
credential snapshot, strict token boundary, and direct-client failure paths.
This is fixture evidence only. It does not establish OAuth lifecycle, live GA4
access, UI reconciliation, GA4 Admin writes, Realtime behavior, or
processed-report verification.

The installed command adds canonical retention and redacted review through
[measurement history](measurement-history.md). The component itself performs
no ledger or artifact writes.
