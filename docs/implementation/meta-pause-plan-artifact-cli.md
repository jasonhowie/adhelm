# Meta campaign PAUSE plan artifact CLI

`plan meta-campaign-pause create` can apply the existing saved artifact policy
or a one-task override. The only explicit historical command is:

```text
artifacts meta-campaign-pause RECORD_DIGEST --input REQUEST.json
```

`REQUEST.json` uses the closed
`meta-campaign-pause-plan-artifact-render` schema. It contains only schema
version 1, a nonempty unique set of `json`, `csv`, and/or `html` formats, an
optional local output directory (otherwise the saved destination is used), and
an optional explicit replacement choice. It cannot contain an action, a
source/provenance mapping, a provider response,
a credential, an approval, or a generic document body. The installed document
schema is `meta-campaign-pause-plan-artifact-document`.

The artifacts group rejects the request file before it reads configuration,
local history, a sharing key, or a destination. It then loads exactly the
selected retained plan, revalidates its closed M40 action and loader
provenance, and requires the selected record digest to match. The explicit
command resolves an on-request artifact decision and renders every complete
document with a fixed synthetic HMAC key before it reads the real sharing key
or opens the output directory. It also fits the full result with the largest
allowed partial artifact reference set under the saved output bound.

For `plan meta-campaign-pause create`, policy resolution and the same
worst-case receipt/result sizing happen before the configured Meta read,
credential/session access, or durable plan append. `ON_REQUEST`, disabled
purpose, a nonmatching rule, and `--no-artifacts` leave the receipt file-free.
A matching incomplete rule fails before those operations. A matching saved
rule or an explicit format/output override can request only this static
history view.

After a plan is retained, both routes reload its selected source, reconstruct
and validate the M40 action, and verify that the returned record/action/plan
digests match the durable receipt or selected record. They create a private
canonical comparison snapshot of the action, loader provenance, and derived
artifact provenance. The complete zero-key document preflight runs from that
held source. Immediately before the real export-key and writer handoff, the
CLI reloads the same record and requires the complete snapshot, including the
checkpoint digest, to match. A changed source or checkpoint does not reach the
writer. This is an exact local retained-history check; it does not claim a
cross-process ledger transaction or current provider state.

The renderer produces only `meta_campaign_pause_plan_review`, a redacted,
historical M40 view. The CLI result retains safe digests, historical status,
false authority and mutation controls, and selected artifact metadata. It
excludes native account/campaign IDs, profile/application/principal values,
credential binding, configuration digests, scopes, raw actions, receipts,
challenges, provider payloads, and keys. Files remain historical and state
that configuration-only `PAUSED` does not predict effective delivery or prove
that residual spend is zero.

A file write can partially complete. The result then remains `PARTIAL`, keeps
the retained plan receipt and every verified completed artifact reference, and
uses a fixed public failure code. It does not retry planning or publication,
open a provider path, grant execution authority, or treat the artifact as an
approval. A later explicit regeneration must again select and reload the exact
retained record.

## Validation

Focused tests use temporary ledgers, fake credential/session/HTTP seams, and
local output directories only:

```sh
uv run pytest -q \
  tests/test_meta_pause_cli.py \
  tests/test_meta_pause_cli_ultra_review.py \
  tests/test_meta_pause_plan_artifacts.py \
  tests/test_meta_pause_plan_artifact_cli.py \
  tests/test_meta_pause_plan_artifact_policy.py \
  tests/test_meta_pause_plan_artifact_integration.py
```

They do not contact Meta, invoke a real operating-system credential store,
launch the native host, request approval, or execute a campaign change.
