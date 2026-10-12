# Provider-neutral composite-execution approval

Last updated: 2026-09-08

This is the Python side of [ADR 0012](../adr/0012-composite-execution-plan-approval.md).
It adds two approval challenge kinds, their subject schema, their builders,
their acceptance branch, and their offline vectors.

It adds **no provider capability, no CLI command, no credential, no scope, no
installed host, and no live evidence**. No command reaches any of it. The
installed bridge refuses both kinds before a helper launch, and the compiled
binding resolver still returns `NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED` for every
kind. A verified receipt here is evidence that a supplied key signed the exact
reviewed challenge; it is not a grant, a host, an enrollment, a provider
authorization, or proof that a person saw anything.

## The two kinds

```python
class ApprovalChallengeKind(StrEnum):
    ...
    COMPOSITE_EXECUTION_PLAN = "COMPOSITE_EXECUTION_PLAN"
    ATTEMPT_RECOVERY = "ATTEMPT_RECOVERY"


COMPOSITE_APPROVAL_KINDS = frozenset({COMPOSITE_EXECUTION_PLAN, ATTEMPT_RECOVERY})
```

They share one dataclass (`CompositeExecutionApprovalSubject`), one wire shape,
one review projection, one receipt binding, and one acceptance branch. They are
separate kinds because the receipt signing domain is per kind; ADR 0012 records
that reasoning.

## Challenge wire shape

```json
{
  "schema_version": 1,
  "kind": "COMPOSITE_EXECUTION_PLAN",
  "nonce": "<43 char base64url of 32 bytes>",
  "issued_at": "2030-01-01T00:00:00Z",
  "expires_at": "2030-01-01T00:04:00Z",
  "grantor_ref": "customer-grantor-1",
  "subject": {
    "expires_at": "2030-01-01T01:00:00Z",
    "operations": ["CREATE", "PAUSE"],
    "plan_digest": "<64 hex>",
    "review_lines": [
      "Maximum incremental spend 1000 minor units EUR",
      "Maximum incremental spend 25000 minor units USD",
      "Provider google_ads operation CREATE",
      "..."
    ],
    "spend_bounds_by_currency": [
      {"currency": "EUR", "maximum_minor_units": 1000},
      {"currency": "USD", "maximum_minor_units": 25000}
    ],
    "subplan_digests": ["<64 hex>", "..."],
    "subplans": [
      {
        "expected_child_egress_count": 3,
        "operation": "CREATE",
        "provider": "google_ads",
        "resource_ref": "opaque:sha256:<64 hex>",
        "spend_bound": {"currency": "USD", "minor_units": 25000},
        "subplan_digest": "<64 hex>"
      }
    ]
  }
}
```

An `ATTEMPT_RECOVERY` challenge has the same top-level shape with
`"kind": "ATTEMPT_RECOVERY"`. Its subject replaces `expires_at` with
`source_plan_expires_at`, adds two fields, and each entry adds two:

```json
{
  "authorized_operation": "RECOVER",
  "source_plan_digest": "<64 hex, the composite this attempt belongs to>",
  "source_plan_expires_at": "2030-01-01T01:00:00Z",
  "subplans": [
    {
      "attempt_subject_digest": "<64 hex>",
      "started_record_digest": "<64 hex APPLY_STARTED record digest>",
      "...": "the six composite entry fields"
    }
  ]
}
```

### Field meanings

| Field | Meaning |
| --- | --- |
| `plan_digest` | Exactly what the resulting `DirectGrant.plan_digest` binds. For a composite it is `CompositePlan.plan_digest`. For a recovery it is `digest(list(subjects))`, matching `SafetyKernel._authorize_attempt_recovery`. |
| `subplan_digests` | Exactly what `DirectGrant.subplan_digests` binds, in order. For a composite, the plan's subplan digests. For a recovery, the `attempt_recovery_subject` values. |
| `subplans[].subplan_digest` | The compiled subplan's own digest, in both kinds. |
| `subplans[].provider` | The subplan's provider string. |
| `subplans[].operation` | The subplan's own `OperationKind`. For a recovery this is the operation that was *attempted*, not the operation being authorized; `authorized_operation` is always `RECOVER`. |
| `subplans[].resource_ref` | `"opaque:sha256:" + sha256(canonical({domain, provider, resource_ref, stable_account_id}))`, domain `adhelm.approval.composite-resource-ref.v1`. A stable pseudonym of the subplan identifier the ledger records as `resource_ref`, so the same resource is recognisable across plans without displaying its name. |
| `subplans[].expected_child_egress_count` | The subplan's declared number of provider requests, `1..MAX_CHILD_EGRESS_COUNT`. |
| `subplans[].spend_bound` | `{"currency","minor_units"}`, or `{"spend_capability": "NOT_SPEND_CAPABLE"}`. |
| `spend_bounds_by_currency` | For each currency among spend-capable entries, the **maximum** declared cap of that currency, sorted by currency. It is not a total; a composite has no cross-subplan cumulative claim to make. Empty when no entry is spend-capable. |
| `operations` | The sorted unique set of `subplans[].operation`. For a recovery these are the operations that were *attempted*; `authorized_operation` is always `RECOVER`. |
| `expires_at` (composite only) | The compiled plan's expiry. The challenge's own `expires_at` may not exceed it. |
| `source_plan_expires_at` (recovery only) | The same value, as review context. It does **not** bound the challenge. |
| `review_lines` | The bounded redacted strings the host renders verbatim, led by the derived lines below. |

### Approval lifetime

A `COMPOSITE_EXECUTION_PLAN` challenge may not outlive the plan it authorizes:
approving a plan past its expiry would authorize work `SafetyKernel._check_expiry`
will refuse anyway.

An `ATTEMPT_RECOVERY` challenge deliberately **may** outlive it. That kind
exists for an attempt this kernel never finished journaling -- a process that
died inside the write path -- which is discovered on a later run, after the
short-lived compiled plan has expired. `record_attempt_recovery` checks only the
recovery grant's own expiry, never the plan's, and the plan stays one-use
regardless. Clamping the recovery challenge to the plan would leave a workspace
blocked by an unresolved `APPLY_STARTED` with no obtainable approval for the one
authorized exit the kernel provides.

The recovery challenge is still bounded by the ordinary ceiling every kind
shares: `expires_at - issued_at` may not exceed
`ApprovalHostConfiguration.maximum_challenge_lifetime_seconds`, which is 300 and
may not be configured higher. The resulting `DirectGrant.expires_at` is that
challenge expiry, so the recovery grant is short-lived even when the plan it
observes is ancient.

The wire names the field `source_plan_expires_at` for the recovery kind
precisely so a port cannot apply the composite clamp by reading a field named
`expires_at`.

### `NOT_SPEND_CAPABLE`

An entry summarizes as `NOT_SPEND_CAPABLE` when, and only when, its operation is
in the closed set `{PAUSE, ARCHIVE, RECOVER}` **and** its declared cap is exactly
zero minor units. Those three cannot increase provider spend, and the existing
Meta PAUSE subject already asserts a zero cap.

Every other operation always shows its integer cap, including a zero cap: a
`CREATE` with a zero cap means "the cap is zero", which is a different statement
from "this cannot spend", and conflating them would let a rendering choice
understate an operation's nature.

Every `ATTEMPT_RECOVERY` entry is `NOT_SPEND_CAPABLE` and its
`spend_bounds_by_currency` is empty, because recording an observation issues no
provider request. The decoder enforces both.

## `review_lines`

A closed, ordered list of at most **24** lines of at most **120 UTF-8 bytes**
each. `validate_review_lines` refuses a line that:

- does not match `^[A-Za-z0-9][A-Za-z0-9 .,:;()\[\]/+%_-]*$` (so: printable
  ASCII from a fixed set only, first character alphanumeric -- no control
  characters, no bidirectional overrides, no homoglyphs, no `@`, no `<`);
- has a trailing space or two consecutive spaces;
- contains `://`;
- contains a 64-hex run at a token boundary, upper or lower case; or
- fails `adhelm.privacy.validate_data`, which screens e-mail addresses, telephone
  numbers, and secret-shaped text.

All refusals raise one `ValueError`, which the builders convert to the single
`INVALID_APPROVAL_CHALLENGE` code. Which screen refused a line is not a fact a
caller needs.

A digest is refused deliberately: digests belong in structured fields where the
receipt binds them, and a rendered digest is unreadable in a review anyway.

This is a bounded screen, not a claim that every sensitive meaning is detectable
in free text.

### Derived lines lead the list

Some claims are decided by the structured subject, not by whoever assembled the
lines. Those are **derived** and are recomputed on decode, so the
authoritative sentence always leads the list. Supplied lines may not use the
reserved phrasings below; the guard is a phrase check and does not prove that a
reworded later line agrees with the summary:

| Kind | Derived lines, in order |
| --- | --- |
| both | one `Maximum incremental spend <n> minor units <CUR>` per entry of `spend_bounds_by_currency`, in its order; or the single line `Maximum incremental spend NOT_SPEND_CAPABLE` when that list is empty |
| `ATTEMPT_RECOVERY` | before those: `Authorized operation RECOVER (attempt of <operations>)` and `This records the observed state and applies no provider change` |

`validate_review_lines(value, derived=...)` requires the list to begin with
exactly those lines, in order, and requires that **no later line** contains a
reserved marker -- `incremental spend` or `authorized operation`, matched
case-insensitively. A supplied line making either claim is refused.

Prepending is idempotent: the builder accepts either a caller's own lines or a
complete previously validated list, so acceptance can rebuild an expected
subject from a verified one through the same derivation.

The recovery lead line is why a recovery review cannot be mistaken for an
execution approval: it names `RECOVER` first and the attempted operation only
as parenthetical context.

### Deriving the rest of the lines

Two helpers produce lines that already satisfy the validator.

`composite_subplan_review_lines(subplan)` derives five lines from a compiled
subplan's own intent -- provider and operation, planned request count, declared
egress hosts, recovery actions, provider API and capability version. This is the
source for PAUSE, ARCHIVE, and recovery subjects, which have no compiler
candidate to summarize. It restates only facts the subplan already contains, and
it deliberately makes no spend statement: the authoritative one is derived from
the per-currency summary above.

`compiler_review_lines(provider, redacted_review)` renders any provider
compiler's existing `redacted_review` model -- `google_ads_compiler.py:746`,
`meta_compiler.py:498`, `linkedin_compiler.py:620`, `tiktok_compiler.py:634`,
`reddit_compiler.py:599`, `openai_compiler.py:491` -- as ordered
`label: value` lines, sorted by field name, after a fixed
`Provider <name> compiled candidate review` header. Rendering rules:

| Value | Rendered as |
| --- | --- |
| `None` | `NOT_SUPPLIED` |
| `bool` | `true` / `false` |
| `int` | decimal |
| `str` matching a 64-hex digest or `<scheme>:sha256:<64 hex>` | **omitted** |
| other `str` | verbatim |
| `{"minor_units","currency"}` | `<n> minor units <CUR>` |
| any other mapping | **refused** |
| list or tuple | `<n> values` |

A digest is the only value omitted, because the receipt binds it structurally
and a rendered digest is what the line validator refuses anyway. Every other
unrecognised shape raises: a nested field a future compiler adds must fail
loudly rather than disappear from the review with no signal. All six current
compilers are pinned against this by
`test_every_compiler_review_fits_the_bounded_line_budget`.

All six current compilers fit inside the 24-line and 120-byte budgets; a review
that does not fit is refused rather than truncated. The declared egress
allowlist **is** shown -- a marketer needs it and the ledger already records it
-- while the account's stable identifier, principal, and the planned resource
name are not.

## Receipt binding

The receipt schema is **unchanged**. Both kinds join the existing plan-bound
family:

```json
{
  "action_digest": "<sha256 of the canonical subject object>",
  "plan_digest": "<subject.plan_digest>",
  "subplan_digests": ["<subject.subplan_digests, in order>"],
  "grantor_ref": "...",
  "...": "the twelve common receipt fields, unchanged"
}
```

Signing input is the existing domain separation with the new kind string:

```text
adhelm.macos-approval-host.receipt.v1\0COMPOSITE_EXECUTION_PLAN\0<canonical unsigned receipt>
adhelm.macos-approval-host.receipt.v1\0ATTEMPT_RECOVERY\0<canonical unsigned receipt>
```

Review facts gain one closed branch:

```json
{
  "approval_kind": "...",
  "challenge_digest": "...",
  "challenge_expires_at": "...",
  "challenge_nonce": "...",
  "grantor_ref": "...",
  "composite_execution": {
    "binding_kind": "COMPOSITE_EXECUTION_PLAN",
    "subject": { "...the complete subject object..." },
    "subject_digest": "<64 hex>"
  }
}
```

The subject is already digests, bounded counts, integer caps, and validated
lines, so there is nothing further to redact in the review projection.

## Validation the decoder performs

`_composite_subject_from_data` proves the summary describes *itself*. It does
not prove the summarized plan exists, is retained, or is current; acceptance
does that separately. It requires:

- an exactly closed field set, per kind, at the subject and at every entry;
- `subplan_digests` equal, element for element and in order, to the entries'
  `subplan_digest` (composite) or `attempt_subject_digest` (recovery);
- unique entry digests, `1..maximum_subplans` entries;
- `operations` equal to the sorted unique set of entry operations;
- `spend_bounds_by_currency` equal to the per-currency maxima recomputed from
  the entries, sorted, with no extra or missing currency;
- per entry, one canonical spend encoding: `NOT_SPEND_CAPABLE` only when the
  operation is in `{PAUSE, ARCHIVE, RECOVER}`, and *required* when such an
  operation carries a zero cap. Without the first half a spend-capable `CREATE`
  could be shown to a reviewer as incapable and dropped from the currency
  summary while every restatement check still passed; the second half keeps one
  set of bytes per plan so a port cannot disagree. Every recovery entry must be
  `NOT_SPEND_CAPABLE` whatever its attempted operation was;
- `review_lines` beginning with the recomputed derived lines and containing no
  other reserved claim;
- for a recovery: `authorized_operation == "RECOVER"`, a hex-64
  `source_plan_digest`, `plan_digest == sha256(canonical(subplan_digests))`,
  every entry `NOT_SPEND_CAPABLE`, and an empty currency summary;
- the remaining `review_lines` passing the validator; and
- byte-for-byte equality between the re-encoded subject and the wire bytes.

The challenge additionally requires `challenge.expires_at <= subject.expires_at`
**for `COMPOSITE_EXECUTION_PLAN` only**; see "Approval lifetime" above.

## Acceptance

`accept_composite_execution_plan_receipt(kernel, ledger, *, receipt,
expected_challenge, trust, plan)` verifies the receipt, rebuilds the subject the
caller's exact `CompositePlan` must produce through
`expected_composite_approval_subject`, requires byte equality, appends and
fsyncs `APPROVAL_RECEIPT_CLAIMED`, and then records a `DirectGrant` bound to the
composite digest and **every** subplan digest through the existing
`SafetyKernel.record_direct_grant`. Replay, nonce, expiry, and one-use semantics
are the existing ones: the ledger refuses a reused receipt identifier, signed
receipt digest, or challenge nonce.

`accept_attempt_recovery_receipt(kernel, ledger, *, receipt,
expected_challenge, trust, plan, attempts)` takes the same path, where `attempts`
pairs each unresolved subplan with the durable `APPLY_STARTED` record digest it
never resolved. It differs in one respect, deliberately: it appends **no**
`APPROVAL_GRANTED` event. A recovery grant's `plan_digest` is a recovery subject
digest rather than the plan's own, so `record_direct_grant` would correctly
refuse it with `AUTHORIZATION_PLAN_MISMATCH`. The durable one-use consumption is
the claim record, and `SafetyKernel.record_attempt_recovery` applies its own
host proof, expiry, subject-match, and one-use checks before it records
anything.

Because the caller must then supply a `UserApprovalHost` to
`record_attempt_recovery`, `AcceptedNativeApproval.approval_host` is populated
for that kind only. It is the same private receipt-bound host acceptance used
internally: it proves that one grant digest and nothing else, and it re-verifies
the receipt on every proof, so it stops proving the grant once the receipt
expires. Provider reads stay with the caller and the kernel; the acceptance
library still performs no provider work.

Neither route accepts a `source_plan_record_digest`. Retaining a compiled
composite as a ledger record is S3's persistence work; until it exists the
caller supplies the exact plan object, and the generic
`accept_approval_receipt` route fails closed with
`APPROVAL_SOURCE_PLAN_REQUIRED` because it has no plan to compare.

## Retiring `_SUPPORTED_PAUSE_TIMEZONES`

`_SUPPORTED_PAUSE_TIMEZONES = frozenset({"UTC", "America/Chicago"})` is gone.
`_validated_iana_timezone` now validates the name the plan carries: at most 64
bytes, matching `^[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,2}$`, not
prefixed `posix/` or `right/`, not `Factory` or `localtime`, and resolvable by
`zoneinfo.ZoneInfo`. The regex also excludes `.` and `..`, so the name cannot
traverse a path.

Existing behaviour is byte-identical for the checked-in vectors, which all use
`UTC` or `America/Chicago`;
`test_composite_execution_plan_approval.py::test_every_pre_adr_0012_receipt_vector_still_verifies_unchanged`
re-verifies every pre-ADR-0012 Swift receipt vector, and the GA4 and Meta
suites are unchanged.

**Two known asymmetries, both fail-closed:**

1. `_SUPPORTED_GA4_TIMEZONES` is deliberately left in place. It belongs to
   `_validate_ga4_native_subset`, which is documented as a narrower fixed
   subject than the GA4 contract, and widening it is a GA4 decision rather than
   an approval-protocol one.
2. The Swift host still hard-codes the pair at
   `ApprovalChallenge.swift:502`. Python now builds a challenge the current host
   would refuse. Nothing unsafe follows -- the host fails closed -- but closing
   the gap is on the A01 list below.

## Offline vectors

`tests/fixtures/composite-execution-plan-approval-v1.json` is written by
`tests/generate_composite_execution_plan_approval_vectors.py`.
`test_checked_in_vectors_regenerate_byte_for_byte` reruns the generator and
requires byte equality, so the Swift port can regenerate and cross-check.

The file contains, for both kinds: the canonical challenge, its digest, the
canonical review facts, the review digest, the receipt identifier and issue
time, the canonical **unsigned** receipt, its digest, and the exact
base64url-encoded domain-separated signing bytes. It also contains the source
`CompositePlan`, its digests, and a worked `compiler_review_lines` derivation
including two digest-shaped fields that the derivation omits.

The composite vector is four providers, two currencies, one subplan with three
expected child requests, and one zero-cap PAUSE that renders
`NOT_SPEND_CAPABLE`. The recovery vector is one unresolved attempt, and shows
the derived recovery lead lines and `source_plan_expires_at`.

**The file deliberately contains no signature and no key material.** Committing
a fixed private key to make an ECDSA signature reproducible would violate
`AGENTS.md` rule 1, and committing a signature without its key would be a value
nothing can verify. The signed path is covered instead by ephemeral in-memory
P-256 keys in `tests/test_composite_execution_plan_approval.py` and
`tests/test_composite_execution_plan_acceptance.py`. What a Swift port needs
from the file is the exact canonical challenge, review-facts, and unsigned
receipt bytes it must produce; the signature is the host's own concern.

## Swift changes required for A01

Recorded here because the native sources cannot be built or edited from this
host. Line numbers are as of base `5eb153c`.

### `Sources/ApprovalHostCore/ApprovalChallenge.swift`

1. **`ApprovalKind`** (lines 3-18): add
   `case compositeExecutionPlan = "COMPOSITE_EXECUTION_PLAN"` and
   `case attemptRecovery = "ATTEMPT_RECOVERY"`. The enum is `CaseIterable`, so
   check every exhaustive `switch` over it.
2. **`ApprovalOperation`** (lines 20-32): add `case recover = "RECOVER"`. The
   Python `OperationKind` already has it; the Swift enum has eleven cases and
   would otherwise reject a subject whose `operations` contains `RECOVER`.
3. **`ApprovalChallenge.decode`** (from line 74): add `"subject"` to the
   `ClosedObject` field list and add
   `["schema_version", "kind", "nonce", "issued_at", "expires_at", "grantor_ref", "subject"]`
   to `allowingOneOf`. Add two `case` branches that decode a new
   `ExactCompositeApprovalSubject` bounded by `limits.maximumSubplans` and build
   `.compositeExecutionPlan(grantorRef:subject:)` /
   `.attemptRecovery(grantorRef:subject:)`. Apply
   `expiresAt <= subject.expiresAt` **only** for the composite case; the
   recovery subject has no `expires_at` field to clamp against, only
   `source_plan_expires_at`, and clamping it would make the kernel's one
   authorized exit from the write barrier unobtainable. The existing
   `expiresAt.timeIntervalSince(issuedAt) <= limits.maximumChallengeLifetime`
   check already bounds a recovery challenge.
4. **New `ExactCompositeApprovalSubject`**: implement every rule in
   "Validation the decoder performs" above, including the byte-equality
   re-encode check the existing exact subjects already perform, the per-entry
   canonical spend-encoding rule, and the derived-line recomputation. It must
   also implement the `review_lines` validator exactly: the ASCII allowlist
   regex, the 24-line and 120-byte bounds, the space rules, `://`, the 64-hex
   run check, the derived-line prefix, and the reserved markers
   `incremental spend` and `authorized operation` (lowercased substring match).
   Swift's `String` is not byte-oriented by default; validate over `utf8` as
   `isExactApprovalReceiptIdentifier` already does.
5. **`isSupportedOpenAIPauseTimezone`** (lines 502-503): replace
   `["UTC", "America/Chicago"].contains(value)` with the same validation Python
   now performs -- the syntactic guard plus `TimeZone(identifier: value) != nil`
   -- so the two sides agree. Leave `Ga4KeyEventCreateApproval.swift:670`
   unchanged; GA4 keeps its narrower subset.

### `Sources/ApprovalHostCore/ApprovalReview.swift`

6. **`reviewFacts(for:)`** (from line 25): add the two cases emitting
   `root["grantor_ref"]` and
   `root["composite_execution"] = .object(["binding_kind": …, "subject": …, "subject_digest": …])`.
   `subject` must be the identical canonical object the challenge carried, and
   `subject_digest` its SHA-256, so the review digest matches Python's.
7. **`render(challenge:facts:)`** (from line 321): add a summary builder. It
   emits host-authored fixed context first -- what kind this is, the grantor,
   the plan digest, the subplan count, the per-currency spend summary, the
   operation set, and the expiries -- and then the `review_lines` **verbatim,
   in order, one per output line**. Rules the host must follow:
   - for `ATTEMPT_RECOVERY`, render the operation set as
     `RECOVER (attempt of CREATE, PAUSE)` -- the authorized operation first and
     the attempted operations only as parenthetical context -- never as a bare
     operation list, which would read like an execution approval. State the
     plan expiry as *the source plan's* expiry and the challenge expiry
     separately, because the two are unrelated for this kind;
   - never concatenate, reorder, deduplicate, truncate, or wrap a line;
   - never interpret a line as markup, a URL, a path, a command, or an
     identifier, and never make one actionable;
   - never render a line before the closed validator has accepted the whole
     list; a challenge whose lines fail validation is `malformedChallenge`;
   - keep the existing "Technical detail -- canonical facts bound by the review
     digest" block, which already appends the canonical facts JSON; and
   - keep the existing 16,384-byte total-text bound.

### `Sources/ApprovalHostCore/ApprovalReceipt.swift`

8. **`ApprovalReceiptFactory.makeUnsigned`** (from line 80): add two cases
   setting `grantor_ref`, `action_digest` (SHA-256 of the canonical subject
   object), `plan_digest` (the subject's `plan_digest`) and `subplan_digests`
   (the subject's `subplan_digests`, in order). No receipt field is added,
   removed, or renamed. `ReceiptSigningPayload` already derives its domain from
   `kind.rawValue`, so the new signing domains follow with no change.

### `Sources/ApprovalHostCore/ApprovalStateMachine.swift`

9. **`validateHostBinding`** (line 373): add the two cases. They carry no host
   reference of their own -- exactly like the three plan kinds -- so they
   `return`.
10. **`isDirectRetainedPlanKind`** (line 388): the two new kinds must return
    **`false`** until their own reviewed bridge slice exists, keeping them out
    of the V1 direct-retained-plan route.
11. Re-check the Meta-only guard at line 270 and every other `switch` over
    `ApprovalKind` for exhaustiveness after step 1.

### `Sources/ApprovalHostCore/NativeApprovalBridgeProtocol.swift`

12. The V1 request and candidate codecs must continue to refuse both kinds,
    mirroring Python's `_DIRECT_KINDS`. Add a regression that proves it, as the
    Meta V2 work did.

### Tests and fixtures

13. Add `Tests/ApprovalHostCoreTests/Fixtures/CompositeExecutionPlanApproval/`
    holding a copy of `tests/fixtures/composite-execution-plan-approval-v1.json`
    (or a consumer that reads the Python path), plus a test that decodes each
    challenge, rebuilds the review facts, rebuilds the unsigned receipt, and
    asserts byte equality with the checked-in `challenge`, `review_facts`, and
    `unsigned_receipt` values and their digests.
14. Add adversarial Swift cases mirroring
    `tests/test_composite_execution_plan_approval.py` and
    `tests/test_composite_execution_plan_approval_review.py`: each refused
    review line, line count and length overflow, a `subplan_digests` list that
    does not restate the entries, a spend summary that disagrees with an entry,
    a spend-capable operation claiming `NOT_SPEND_CAPABLE`, a zero-cap
    non-spend operation not claiming it, a derived spend line that disagrees
    with the summary, a supplied line making a reserved claim, a recovery
    subject carrying a spend allowance or a non-`RECOVER`
    `authorized_operation`, a recovery challenge that outlives its source plan
    (must be **accepted**) and one that exceeds the challenge lifetime (must be
    refused), an unknown subject field, and cross-kind receipt redemption.

## Evidence and limits

- Everything here is hermetic and offline. No provider transport, credential,
  network call, live account, host, Keychain, LocalAuthentication, or XPC peer
  was involved.
- No Swift code was built, run, or changed; this host cannot compile Swift. The
  A01 list above is a specification, not evidence.
- The vectors carry no signature or key material, so they prove canonical byte
  agreement and nothing about signing.
- 50 tests in `tests/test_composite_execution_plan_approval.py`, 11 in
  `tests/test_composite_execution_plan_acceptance.py`, and the 9 independent
  review regressions in
  `tests/test_composite_execution_plan_approval_review.py`, plus the existing
  approval suites, pass unchanged.
- The kinds add no installed capability. Both bridge gates refuse them, no CLI
  command reaches them, and no adapter, compiler, or contract changed.

## Known follow-ups

- S3/S4 own aliasing the OpenAI, Meta, and GA4 pause/create paths onto this
  kind, retaining a compiled composite as a ledger record so acceptance can
  reload it from a verified checkpoint instead of taking a plan object, and any
  CLI surface.
- The Swift host still hard-codes the two pause time zones; see the asymmetry
  note above.
- `_SUPPORTED_GA4_TIMEZONES` remains, by design, for the GA4 owner to revisit.
- A reviewed bridge slice for these kinds is A01's, and must keep V1 bytes,
  routing, and the raw receipt XPC refusal intact.
