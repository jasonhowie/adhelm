# Google Ads campaign PAUSE planning CLI

The public planning surface is limited to local proposal creation and historical
review:

```text
plan google-ads-campaign-pause create --input REQUEST.json [--no-artifacts]
plan google-ads-campaign-pause catalog
plan google-ads-campaign-pause show RECORD_DIGEST
```

`create` accepts only `google-ads-campaign-pause-plan-request`: schema version
1, a configured Google Ads OAuth `EDIT` profile, one canonical positive
signed-int64 campaign ID, and an expiry of 60 through 900 seconds. The regular
input-file boundary completes before configuration, artifact policy, protected
credentials, HTTP, or ledger activity.

The command reserves its complete compact receipt before opening the configured
planner. It then invokes `plan_configured_google_ads_campaign_pause`, which
performs the fixed customer/campaign reads, configuration and credential
continuity checks, expiry and clock checks, and one append-only retained plan.
The command reloads the exact retained action before emitting its receipt. A
reload or append uncertainty returns an inspect-before-repeat ambiguous result.
No command path adds approval, execution authority, provider writes, activation,
or retry of an ambiguous operation.

Receipt and historical output contain only digests, expiry, fixed capability and
zero-incremental-spend facts, and HMAC-redacted review references. Profile,
customer and campaign identifiers, principal, OAuth/developer-token binding,
configuration digests, raw provider responses, and reusable authority material
are excluded. `catalog` reads verified local history only. `show` first proves a
complete synthetic-key review fits the saved output limit, then obtains the
local sharing key for its redacted review. Expired proposals remain historical
evidence and still require fresh checks before any future egress.

`create` resolves the saved or one-task artifact policy before the protected
planner, credential access, HTTP, or retention, and reserves a full receipt plus
the longest allowed partial artifact result at the same point. `ON_REQUEST`,
`OFF`, a nonmatching rule and `--no-artifacts` keep planning compact and
file-free. A matching rule or `--artifact-format` request that names no complete
existing local destination fails with `ARTIFACT_CONFIGURATION_REQUIRED` before
the planner; its `next_action` names `artifacts google-ads-campaign-pause` as the
retained-history alternative. An unsafe explicit destination is rejected by the
common policy boundary before planner activity as well.

When the policy resolves to generation, `create` renders the same static
redacted JSON/CSV/HTML from the plan it just retained, through the separate
renderer described in
[retained-plan artifacts](google-ads-pause-plan-artifacts.md). It reloads the
retained record after the durable append, binds it to the receipt digests, runs
the synthetic-key preflight, re-reads the source immediately before the real
sharing key and writer, and downgrades only the result — never plan retention —
to `PARTIAL` if a file fails. The same document can be re-rendered or reused
later with `artifacts google-ads-campaign-pause`.

Tests cover public discovery/schema, strict input ordering, incomplete artifact
policy refusal, inline saved-rule generation and partial-write behavior,
no-artifact receipt composition from retained history, catalog/show behavior,
and sharing-key preflight. Existing Google PAUSE workflow and private-session
integration tests cover the fake-store/fake-HTTP fixed reads, expiry,
configuration/token drift, clock rollback, and uncertain append paths.
