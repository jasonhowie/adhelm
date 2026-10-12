# ADR 0012: provider-neutral composite-execution approval

**Status:** Accepted for the offline Python protocol implementation on
2026-09-08. This amends the accepted scope of
[ADR 0004](0004-installed-approval-bridge.md). It does not authorize a runtime
bridge for the new kinds, a host change, client signing, key enrollment, a
CLI command, or any provider operation.

## Context

ADR 0004 accepted a bounded offline protocol whose direct retained-plan subset
is exactly two kinds: `OPENAI_PAUSE_PLAN` and `GA4_KEY_EVENT_CREATE_PLAN`. Every
other approval subject was left on its existing path.

That subset has not held. `META_CAMPAIGN_PAUSE_PLAN` was added as a third
provider-specific kind with its own Swift decoder, its own review-facts
projection, its own review renderer, its own receipt-binding branch, and -- once
the V1 bridge's `_DIRECT_KINDS` allowlist refused it -- an entire second bridge
codec, `NativeMetaCampaignPauseApprovalBridgeProtocolV2`, in both languages. One
provider's one operation cost a kind, two decoders, two renderers, two codecs,
and its own vector set.

The kernel generalization recorded in
`docs/implementation/kernel-generalization.md` removed the provider-specific
branches from the write path: every mutating subplan on every provider now needs
guarded egress, and `OperationKind.RECOVER` became a grant subject with no
approval material at all. `SafetyKernel.record_attempt_recovery` therefore has
no host that can prove its grant, and its own document names S1 as the owner of
that gap.

The remaining work is six providers times CREATE, ACTIVATE, MAINTAIN, and
RECOVER, plus GA4 Admin edits. Continuing one kind per operation would mean
roughly two dozen Swift decoders and renderers, each a place for the review a
person actually reads to drift from the digests a receipt actually binds.

## Decision

Add two provider-neutral approval challenge kinds, and admit both as inputs to
the installed approval bridge *contract*:

- `COMPOSITE_EXECUTION_PLAN` -- one compiled `CompositePlan`.
- `ATTEMPT_RECOVERY` -- recording the observed state of exact unresolved
  attempts of one compiled plan.

Their subject binds digests, bounded counts, an integer spend summary, and a
closed ordered list of short redacted `review_lines`. It carries no provider
payload, no resource name, no credential reference, and no other free-form text.
The host renders `review_lines` verbatim and needs **no provider-specific
schema**, so a new guarded write on any provider reuses this kind rather than
adding one.

The full subject schema, wire shape, validation rules, and derivation helpers
are in
[`docs/implementation/composite-execution-plan-approval.md`](../implementation/composite-execution-plan-approval.md).

### Why two sibling kinds rather than one kind with a subject variant

The receipt signing domain is per kind:
`adhelm.macos-approval-host.receipt.v1\0<kind>\0<canonical unsigned receipt>`.
Two kinds therefore make a composite receipt cryptographically unusable as a
recovery receipt, and vice versa, without relying on a discriminator inside the
subject body. That is worth more than the small duplication, because the two
subjects bind different things: a composite receipt's `plan_digest` and
`subplan_digests` are the compiled plan's own digests, while a recovery
receipt's are `attempt_recovery_subject` values that name durable
`APPLY_STARTED` records. A person reviewing them is also answering two
different questions -- "may this plan run?" versus "may the engine record what
it now sees after an attempt it never finished?" -- and a host must not be able
to present one as the other.

They remain one schema, one decoder, one review projection, one renderer, and
one acceptance branch, so the cost of the second kind is one enum case and one
signing-domain string.

### What this amends in ADR 0004

ADR 0004's sentence "The accepted offline protocol scope is only the current
direct retained-plan subset: `OPENAI_PAUSE_PLAN` and
`GA4_KEY_EVENT_CREATE_PLAN`" is amended to admit `COMPOSITE_EXECUTION_PLAN` and
`ATTEMPT_RECOVERY` as bridge inputs once a reviewed host renders them.

Everything else in ADR 0004 is unchanged and is deliberately restated as still
binding:

- **No Python-to-native acknowledgement.** The one-way durable ordering, its
  failure table, and its reasoning are untouched. These kinds add no cross-store
  acknowledgement and no new ordering.
- **One signed helper.** No second bridge codec, helper, Mach service, XPC
  selector, or protocol version is created for these kinds. When a reviewed host
  renders them, they must travel the existing V1 request and candidate bytes.
  Making Meta's V2 codec unnecessary for future operations is the point of this
  ADR; V2 itself stays where it is.
- **No runtime metadata loader.** Host configuration, metadata digests, code
  requirements, key selectors, and the helper path continue to come only from
  reviewed build constants.
- **Prospective revocation, same-UID limits, and the point-in-time nature of
  native current verification** are unchanged.

### This adds no installed capability

Nothing about adding a Python kind lets a person review anything. Until the
Mac-side A01 ticket changes, signs, and installs a host that renders these
subjects:

- `_native_approval_bridge_protocol._DIRECT_KINDS` still refuses them, so no
  bridge request can be built;
- `_installed_native_approval_bridge._require_installed_bridge_kind` refuses
  them with `NATIVE_APPROVAL_BRIDGE_KIND_NOT_INSTALLED` before any helper
  launch, credential, or egress;
- the compiled-binding resolver still returns
  `NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED` for every kind; and
- no CLI command reaches any of it.

A Python-verified receipt for these kinds is, exactly as for the existing kinds,
evidence that some key signed the exact reviewed challenge. It is not a grant,
an installed host, an enrollment, a provider authorization, or proof that a
person saw anything.

### Approval lifetime differs between the two kinds

A `COMPOSITE_EXECUTION_PLAN` challenge may not outlive the plan it authorizes.

An `ATTEMPT_RECOVERY` challenge deliberately may. The kind exists for an attempt
this kernel never finished journaling, which is discovered on a later run after
the short-lived compiled plan has expired; `record_attempt_recovery` checks only
the recovery grant's own expiry, and the plan stays one-use regardless. Clamping
it to the plan would leave a workspace blocked by an unresolved `APPLY_STARTED`
with no obtainable approval for the one authorized exit the kernel provides.

A recovery approval remains bounded by the ceiling every kind shares --
`maximum_challenge_lifetime_seconds`, which is 300 and cannot be configured
higher -- and the resulting direct grant expires with the challenge. The wire
names the recovery subject's field `source_plan_expires_at` rather than
`expires_at`, so a port cannot apply the composite clamp by reading a field of
that name.

## Consequences

- A future guarded write needs a compiler, a contract, an adapter, and
  evidence. It does **not** need a new approval kind, a new native decoder, a
  new review renderer, or a second bridge.
- The host's rendering surface becomes a fixed list of validated short strings.
  That is a smaller attack surface than a provider schema, and it moves the
  responsibility for *what* a reviewer sees into Python, where the compiled plan
  and its redaction rules already live. The trade is real: a host can no longer
  independently re-derive a semantic summary from provider fields, so a
  Python-side derivation bug produces a misleading review that the host cannot
  catch. The receipt still binds the plan and subplan digests the kernel checks,
  so such a bug cannot widen what executes -- only what a person was told.
- `review_lines` are covered by the challenge digest, so a line changed after
  issue invalidates any receipt for it. The lines that state a spend allowance
  or an authorized operation are **derived** from the structured subject and
  recomputed on decode, so the authoritative figure always leads the list and
  no supplied line may use the reserved spend or authorized-operation phrasings.
  The reserved-marker guard is a phrase check, not a semantic one: a reworded
  supplied line can still state a different figure later in the list.
- The two-entry `_SUPPORTED_PAUSE_TIMEZONES` allowlist is retired in favour of
  validating the IANA name the plan carries. The Swift host still hard-codes the
  pair, so Python now accepts a third zone the current host would refuse. That
  is a fail-closed divergence, not a widened capability, and closing it is on
  the A01 change list.
- `ATTEMPT_RECOVERY` gives `SafetyKernel.record_attempt_recovery` the host proof
  it has been missing. Recording an observation still settles nothing,
  attributes nothing, and permits no retry.

## Alternatives considered

### 1. One provider-neutral kind pair with rendered review lines -- chosen

It bounds the native surface, keeps one review projection, and makes the next
twenty operations free of native work.

### 2. One bespoke kind per operation -- rejected

This is the status quo extended. It multiplies decoders and renderers, and the
Meta V2 codec shows the second-order cost: a kind outside the bridge allowlist
grows a whole parallel protocol.

### 3. Send the whole `CompositePlan` and let the host render it -- rejected

`ApprovalChallenge.swift` already records why: a generic plan has opaque
before/after fingerprints and cannot produce a meaningful semantic review. It
would also put every provider's schema, and every future schema change, inside
the signed host.

### 4. `ATTEMPT_RECOVERY` as a subject variant of the composite kind -- rejected

It would put the only separation between "authorize this plan" and "record what
happened" inside the subject body, below one signing domain. See above.

## Required later implementation evidence

Beyond ADR 0004's list, which still applies, a promoted implementation must
prove that:

- the Swift decoder, review projection, and unsigned receipt produce bytes
  identical to `tests/fixtures/composite-execution-plan-approval-v1.json`;
- the host refuses any `review_lines` list that Python's validator refuses,
  including a derived spend or authorization line that does not restate the
  structured subject, and renders accepted lines verbatim, in order, without
  interpreting markup, concatenating lines, or resolving anything in them;
- the host renders an `ATTEMPT_RECOVERY` operation set as
  `RECOVER (attempt of …)` rather than as a bare operation list, so a recovery
  review cannot read as an execution approval;
- the host applies the plan-expiry clamp to `COMPOSITE_EXECUTION_PLAN` only,
  and still bounds a recovery challenge by the shared lifetime ceiling;
- a receipt of one of the two kinds cannot be redeemed as the other;
- the V1 bridge route and the raw receipt XPC route continue to refuse both
  kinds until their own reviewed slice; and
- the Swift time-zone check matches Python's IANA validation.
