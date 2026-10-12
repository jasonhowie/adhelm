# GA4 CREATE plan artifact CLI

`artifacts ga4-key-event-plan RECORD_DIGEST --input REQUEST.json` creates an
explicit local static review from one retained GA4 key-event CREATE plan. The
closed request schema is `ga4-key-event-create-plan-artifact-render`; it accepts
only schema version 1, JSON/CSV/static-HTML formats, an existing local output
directory, and an explicit replacement choice. It accepts no action body,
provider response, OAuth value, event source text, native receipt, or generic
document payload.

The command loads the selected plan through the verified saved-plan reader,
revalidates the complete action and its closed eight-field retained provenance,
and requires the selected digest to match the returned source record. Artifact
provenance derives expiry and capability status from that revalidated action;
callers cannot supply either value. The renderer repeats this revalidation
before publication. These checks establish local retained-record integrity for
the selected historical source. They do not establish current remote state,
provider CREATE permission, a current authorization grant, or a remote
mutation.

The explicit command resolves only an on-request artifact decision. It has no
provider or native-approval path. Before export-key access or a writer call, it
builds every selected complete redacted document with a fixed synthetic HMAC
key and checks rows and bytes. It also fits the complete CLI result with the
worst allowed partial artifact references. An input, source, row, byte, or
preflight destination failure creates no file and does not access the sharing
key. The shared writer checks its opened destination again before publication;
a destination changed after preflight can therefore fail later as a bounded
partial result after export-key access, with no claim that a file was created.

The retained CREATE command has a separate saved-rule path. It evaluates the
exact `plan` / `before_approval` / `plan_review` / `ga4_admin` / profile context
before its EDIT inventory GETs, local sharing-key access, or plan retention.
`ON_REQUEST`, disabled purpose, and nonmatching rules leave the compact plan
receipt file-free. A matching incomplete rule returns
`ARTIFACT_CONFIGURATION_REQUIRED` before those operations. A matching complete
rule first fits the full plan receipt plus worst-case artifact envelope, then
retains the plan, reloads its verified source, and runs the same zero-key
complete-document preflight before obtaining the real export key or opening a
file.

If a post-retention artifact source, preflight, key, or per-format writer step
fails, `plan ga4-key-event create` returns `PARTIAL` with the durable plan
receipt and every completed artifact reference. It records a fixed public
failure code and does not retry preparation or publication. The plan remains
inspectable through `plan ga4-key-event show RECORD_DIGEST`; a caller must not
blindly create another plan after an ambiguous retained result.

Artifacts contain a redacted, historical review only. Account/profile,
property, stream, event, and destination references are HMAC pseudonyms before
the shared writer receives the document. Raw event-source prose, OAuth
principal or subject, access/refresh token, native request body, private HMAC
marker, and provider payload remain absent. Every command and artifact surface
sets `execution_authority=false`, `remote_mutations=false`,
`opening_or_editing_authorizes_execution=false`, and
`execution_command=UNRELEASED`. Static artifacts retain fixed recovery
limits: reconcile/export/stop, no remote compensation, and no blind retry.

The shared writer is descriptor-relative, no-follow, private-mode, fsynced,
versioned, and deterministic. It can reuse exact rendered output under the
resolved local policy. It never truncates a plan review. JSON, CSV, and static
HTML are the only supported formats; HTML carries no scripts or links.

## Validation

Focused tests cover verified-source rendering without new provider reads,
schema/discovery visibility, raw schema-version and input-size rejection before
history or key access, complete-document preflight before export-key access,
default file-free creation, saved-rule full-envelope preflight before GETs,
post-retention partial handling, and partial writer references:

```sh
uv run pytest -q \
  tests/test_ga4_create_plan_artifacts.py \
  tests/test_ga4_create_plan_artifact_cli.py \
  tests/test_ga4_create_plan_artifact_policy.py \
  tests/test_ga4_create_cli.py \
  tests/test_ga4_create_cli_ultra_review.py
```

These tests use local temporary ledgers, fake OAuth/HTTP fixtures, and test
keys only. They do not contact a provider, read an operating-system credential
store, invoke native approval, or execute a GA4 mutation.
