# Meta PAUSE approval ultra review

This review covers the M42 closed `META_CAMPAIGN_PAUSE_PLAN` subject only. It
uses synthetic M40 plans, ephemeral P-256 keys, canonical fixture bytes, and
temporary local history. It does not start a native host, open a credential
store, perform Local Authentication, contact Meta, or write a provider state.

The Python review confirms that a challenge reconstructs the entire M40 action
before it becomes reviewable: workspace, account, inspected EDIT association,
campaign baseline, postcondition, plan, subplan, zero spend bound, and expiry
all contribute to the closed action and plan reconstruction. Canonical but
inconsistent substitutions of local configuration, currency, or effective
status are rejected. A nested numeric alias is rejected before it can normalize
to the zero-spend value.

The local review projection deliberately shows the fixed reviewer facts:
workspace/profile/application references, fixed `ads_management` scope, Meta
account and campaign references, observed configured/effective status, bounded
read interval, the configuration-only `ACTIVE` to `PAUSED` transition, and
explicit no-authority limits. It omits the credential-binding reference,
principal, configuration and profile digests, tokens, raw provider data,
request bodies, and outcome claims. The postcondition retains
`effective_delivery: NOT_PREDICTED`; it does not claim zero delivery or a
successful write.

The shared synthetic fixture is unsigned and contains no private key, signature,
OS authentication result, or provider result. The independent Python test
rebuilds its exact challenge, review facts, unsigned receipt, and
domain-separated payload digest. It also checks the Python-generated fractional
vector: every M40 action timestamp has the nonzero six-digit `.123456Z`
spelling, while the receipt retains its distinct valid millisecond grammar.
Receipt verification binds the full action, plan, and sole subplan; an altered
receipt plan digest is rejected.

## Findings resolved before freeze

The shared Swift timestamp parser accepted `.000000Z` in Meta action fields,
while Python rejects that redundant fractional spelling. Because both action
and plan identifiers commit the original timestamp text, an otherwise rebuilt
action could have had different cross-language acceptance. The native code now
uses a Meta-local timestamp rule for all seven action fields (three inspected
association times, two observation times, and plan plus subplan expiry). It
rejects `.000000Z` without changing the general challenge timestamp grammar.
The independent Python regression coherently rebuilds the alternate action,
plan, and subplan identifiers before confirming Python rejects it; the Core
test reconstructs all seven spellings. The shared positive vector proves that
nonzero six-digit fractional timestamps round-trip with matching action, plan,
subplan, review, unsigned-receipt, and domain-payload digests.

The raw XPC client-side subject check did not protect the installed server,
whose service could previously call the generic Core engine after acquiring an
interaction lease. `ApprovalHostXPCService.requestApproval` now calls the
package-only `rawReceiptXPCRejectsMetaSubject` predicate before its lock or
interaction lease. It uses the immutable canonical parser with the configured
challenge-byte cap to inspect only a canonical top-level `kind`, rejecting
Meta. It deliberately does not turn into a generic challenge validator: all
non-Meta and malformed input retains the endpoint's established gate-first
error ordering. The fake service regression verifies the fixed public failure
with zero reviewer, authenticator, and signer calls and leaves the interaction
gate available. This does not change Core-only Meta review or receipt syntax.
An initial full decode-and-host-binding preflight changed that ordering for
generic malformed input and exposed three native regressions. It was replaced
by the final canonical-kind-only predicate before the frozen run.

Receipt consumption is separately protected by the retained-source and replay
history checks: the claim names an earlier exact retained Meta action in the
same workspace, and receipt digest, nonce digest, and receipt identifier remain
one-use. M42 produces no `APPLY_STARTED` or provider effect. Export carries
only the existing redacted receipt-claim allowlist and not a reusable
challenge, receipt, key, or full action.

The installed bridge remains intentionally closed. Python's private bridge
allowlist contains only `OPENAI_PAUSE_PLAN` and `GA4_KEY_EVENT_CREATE_PLAN`; a
Meta challenge returns the fixed bridge protocol refusal. In the reviewed Swift
source, `NativeApprovalBridgeWire.isDirectPlanKind` and
`ApprovalHostEngine.isDirectRetainedPlanKind` likewise exclude Meta. Both the
raw XPC client and the server-side bounded canonical-kind predicate refuse a
valid Meta subject before a transport, interaction lease, review,
authentication, signing, or bridge consumer can run. This permits Core-only
semantic review and receipt construction without enabling the raw XPC receipt
route, the installed bridge, an execution command, or a Graph mutation.

Validation completed so far:

- `UV_CACHE_DIR=/private/tmp/adhelm-m42-uv-cache uv run pytest -q tests/test_meta_pause_approval_ultra_review.py tests/test_meta_pause_approval_protocol.py tests/test_meta_pause_approval_acceptance.py` — 31 passed.
- `UV_CACHE_DIR=/private/tmp/adhelm-m42-uv-cache uv run ruff check tests/test_meta_pause_approval_ultra_review.py` — passed.
- `UV_CACHE_DIR=/private/tmp/adhelm-m42-uv-cache uv run ruff format --check tests/test_meta_pause_approval_ultra_review.py` — passed.
- `CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m42-clang-module-cache swift test --scratch-path /private/tmp/adhelm-m42-swift-build --filter MetaCampaignPause` from `native/macos-approval-host` — 7 passed, run by the native owner in the shared scratch tree.
- The restored-source full native suite in that same shared scratch tree — 250 passed, 0 failed. It includes five Core cases (whole-second and generated nonzero-six-digit vectors, rebuilt redundant-zero rejection, and bridge init/decode refusal) and two macOS cases (raw client and raw-service refusal before interactive or protected work). The owner recorded the final transcript at `/private/tmp/adhelm-m42-full-swift-test.log` and a clean diff check.

The native fixture-consumer and raw-service tests are owned by the Core and
macOS implementation owners. To avoid a duplicate Swift build while disk space
is constrained, this review used their shared-scratch results. A
passing synthetic fixture still does not establish an installed/signed host,
user-origin review, receipt redemption, provider permission, current campaign
state, or execution.
