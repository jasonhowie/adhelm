# GA4 CREATE execution with private installed approval

`execute_retained_ga4_key_event_create_with_installed_approval(directory, request)`
is an internal library sibling of the legacy pinned-receipt GA4 CREATE
workflow. It accepts only the exact retained-plan digest and fresh typed
candidate input in `Ga4KeyEventCreateExecutionRequest`. The explicit
`execute ga4-key-event-create --input` CLI leaf is documented separately and
supplies only this closed request. It does not make an unsigned development
artifact an installed, signed, enrolled, or live approval capability.

The sibling accepts no receipt, challenge, trust pin, public key, candidate,
grant, native transport, OAuth identity, bearer, inventory, adapter, or
provider request. Its fixed logical challenge grantor reference is
`logical:adhelm-installed-approval-bridge-ga4-create-v1`. That is a protocol
label for the private bridge, not a person or a claim of human identity. The
native host review and fresh authentication remain the proposed evidence of
user-originated approval.

## Ordering

Before a bridge request, the workflow strictly rebuilds the request, loads the
exact retained `GA4_KEY_EVENT_CREATE_PLAN`, checks the configured OAuth EDIT
profile/target/configuration digests, rejects unresolved execution history,
obtains the current local fingerprint key under a short configuration
continuity transaction, and recompiles/authenticates the fresh GA4 source
facts. No configuration or ledger lock spans native review, OAuth resolution,
or provider HTTP.

It then calls only the private retained-plan coordinator. The coordinator owns
the compiled-binding gate, canonical expected challenge, request nonce,
candidate exchange, current receipt verification, retained-source reload, and
durable local receipt claim/direct-grant recording. The checked-in generated
binding is deliberately unconfigured, so it returns
`NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED` before any helper exchange. This path
does not fall back to the legacy `PinnedApprovalTrust` route.

After an exact GA4 direct grant is recorded, the workflow requires that the
grant has no policy, and that its plan and complete subplan digest tuple match
the retained action. It also requires the complete returned grant to match its
durable `APPROVAL_GRANTED` authorization digest before importing the configured
adapter; a copied grant with a different authorization or grantor identity is
rejected before OAuth. It then reloads the plan and repeats configuration,
fingerprint-key, unresolved-history, and current-source checks. A changed key,
source, configuration, plan, or approval identity blocks OAuth and egress. The
adapter independently repeats its own current
configuration/source/key checks before fixed provider reads and immediately
before the one kernel permit is consumed.

`SafetyKernel` remains the sole owner of durable `APPLY_STARTED`, permit use,
and provider egress. The native bridge records no provider effect. The legacy
`execute_retained_ga4_key_event_create(..., receipt, expected_challenge,
trust)` API remains intact and shares only the ordinary preflight/post-approval
helpers.

## Failure and recovery limits

The bridge consumes a native nonce before its private candidate returns. A
candidate/claim/reload failure after that point can leave native consumption or
a local approval record durable; it must not be retried automatically. The
coordinator returns its fixed ambiguity/recovery diagnostic. A later GA4
source, configuration, or key drift after a successful local claim is a
no-egress failure; it never turns the native result into a reusable grant or
replays the bridge exchange.

The workflow has synthetic fake HTTP, credential-store, session, and bridge
coverage only. It has no real helper launch, OS authentication, Keychain
enrollment, provider credential, or network evidence. It does not prove
current provider permission, remote success, browser firing, native conversion
linkage, installation, signing, or live validation.

## Focused validation

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_ga4_create_execution_workflow.py \
  tests/test_ga4_create_execution_workflow_ultra_review.py \
  tests/test_ga4_create_execution_with_installed_approval.py
```

The target tests cover default-binding refusal before helper/OAuth/HTTP, one
synthetic retained-plan approval through the existing kernel, fixed logical
grantor labeling, post-claim source/configuration/key drift, and rejection of a
mismatched plan or copied direct-grant identity before OAuth or adapter construction.
