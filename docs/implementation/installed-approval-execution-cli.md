# Installed-approval execution CLI

This contract covers the two OpenAI/GA4 development execution leaves:

```text
execute openai-pause --input REQUEST.json
execute ga4-key-event-create --input REQUEST.json
```

The separate [Meta campaign PAUSE command](meta-pause-execution-cli.md) uses
the same bounded result policy with its own Meta V2 readiness boundary.

Their request schemas are respectively
`openai-pause-installed-approval-execution-request` and
`ga4-key-event-create-installed-approval-execution-request`. The OpenAI request
selects one retained pause-plan record. The GA4 request selects one retained
CREATE plan and carries the typed fresh candidate source needed to recheck that
plan. Neither leaf accepts a receipt, challenge, trust pin, key, grant,
candidate override, adapter, transport, credential, provider request, artifact
option, or generic execution selector.

`discover` and `schema` are static reads. They advertise `UNRELEASED`,
`native_setup_prerequisite: REQUIRED`, and
`live_provider_execution_verified: false`; they do not resolve a binding, start
a helper, read a credential, or contact a provider. Both leaves are declared
remote-mutation leaves, so the 0.1.0 release-policy gate
([`release-policy-gate.md`](release-policy-gate.md)) refuses them with
`WRITES_NOT_RELEASED` (exit 3) before they read the input file, configuration,
output bound, native readiness, credential, helper, OAuth, or provider work.
The code below the gate is unchanged and still tested through the test-only
`simulate_released_writes` fixture. The ordinary checked-in binding is also
deliberately unconfigured: with the gate lifted, a valid bounded input and
complete result-envelope preflight precede the fixed
`NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED` refusal until separately evidenced native
setup is available. The earlier configured `auditor` edition and its
`OPERATOR_EDITION_REQUIRED` refusal are removed.

The CLI validates the regular JSON input file first. It rejects pipes, final
symlinks, duplicate keys, unsafe numeric aliases, oversized files, and request
schema violations before configuration or readiness work. It then reserves the
full closed `EXECUTION_RESULT` envelope against the saved output limit before
asking the private workflow to do anything. This preflight does not create an
artifact; planning and artifact commands remain historical review surfaces.

A completed workflow result is projected into a bounded safe result containing
only the plan digest, `authorization_consumed`, `recovery_required`, one provider
status/subplan digest, bounded opaque resource and evidence references, a fixed
recovery instruction, and `history status`. It omits authorization IDs, receipt
or key material, grants, approval-bridge details, credential facts, provider
payloads, adapter diagnostics, and raw recovery prose. `PARTIAL_EFFECT`,
`AMBIGUOUS_REMOTE_STATE`, `FAILED_NO_EFFECT`, and `NOT_STARTED` all produce a
recovery-required result. The fixed exit mapping is `APPLIED_VERIFIED` → 0,
`PARTIAL_EFFECT` → 5, `AMBIGUOUS_REMOTE_STATE` → 6, and
`FAILED_NO_EFFECT`/`NOT_STARTED` → 3. Every non-success outcome still emits the
safe result first. The command does not automatically retry an ambiguous result.

The private OpenAI and GA4 workflows still own retained-source revalidation,
current configuration and credential checks, native approval exchange, durable
approval claim, pre-egress `APPLY_STARTED`, provider operation, verification,
and recovery. `SafetyKernel` remains the only egress owner. A plan, artifact,
or this CLI registration does not create execution authority or prove native
installation, signing, enrollment, current provider permission, remote success,
or live validation.

## Validation

Focused tests use only temporary directories, fake bindings, and synthetic
workflow results. They do not launch the helper, access an operating-system
store, authenticate a user, or make a provider request.
