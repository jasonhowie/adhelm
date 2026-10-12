# OpenAI pause workspace binding

## Scope

This library-only change gives newly built fixed OpenAI campaign-pause actions
an explicit local workspace binding. It does not add a CLI command, credential
lookup, retention authority, provider request, authorization decision, or
execution path. The pause action remains
`DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED`.

## Wire compatibility and routing

Historical v1 actions omit `workspace_binding` entirely. Their
`CompositePlan.profile_ref` continues to equal the credential profile in the
action binding, so their serialized bytes and canonical digests remain
unchanged. Explicit `null` is not a historical form and is rejected, as are
booleans, scalar values, extra object fields, non-integer schema versions, and
unsafe workspace references.

New builders must supply a local workspace profile reference. They serialize a
closed binding:

```json
{
  "schema_version": 1,
  "binding_kind": "LOCAL_LEDGER_WORKSPACE",
  "workspace_profile_ref": "default"
}
```

For such an action, `plan.profile_ref` is exactly
`workspace_profile_ref`. `binding.profile_ref` remains the selected credential
profile, and the native subplan account's `auth_profile_ref` remains exactly
that credential profile. A workspace binding therefore routes local history;
it neither changes account authentication nor creates execution authority.

`build_openai_pause_plan(binding, *, expires_at, workspace_profile_ref=...)`
is the pure builder. Omitting `workspace_profile_ref` is retained solely for
legacy-compatible actions. A new caller must supply one exact nonempty safe
string; `None` and every non-string input fail rather than being normalized to
legacy routing. Invalid or naive expiry values and malformed constructed
bindings produce a bounded `INVALID_INPUT` diagnostic before plan
reconstruction.

## Planner metadata boundary

`plan_openai_campaign_pause` accepts the same optional workspace argument and
also accepts `expected_account_currency` with
`expected_account_timezone`. These metadata values are optional only as a pair.
When supplied, currency must be a three-letter uppercase ISO-shaped value and
timezone must be a valid IANA zone before the first provider identity read. The
single discovered identity must match both values before campaign inventory or
campaign reads begin. Omitting both values preserves the prior planner
behavior.

The installed pause workflow must explicitly pass its existing
`Ledger(root).profile_ref` as the workspace profile. It must still reject an
expired plan after remote reads before retention. Retention and any future
execution remain subject to their existing verified-ledger and current safety
kernel checks; this binding is not fresh provider evidence or authority.

## Offline validation

`tests/test_openai_pause_workspace_binding.py` covers pinned legacy digest and
wire compatibility, model-copy and constructed-model behavior, malformed
workspace forms, workspace-versus-auth profile separation, retention with the
matching ledger profile, early configured-metadata failures, identity mismatch
before inventory, and sanitized pure-builder input failures. It uses only the
repository's fixture HTTP transport.
