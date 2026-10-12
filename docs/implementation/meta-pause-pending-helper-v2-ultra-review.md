# Meta campaign PAUSE pending/helper V2 ultra review

This independent M48 review covered the private Python composition from a
retained pending request to M47's sealed V2 pipe exchange, plus the distinct
native V2 helper product. The behavioral tests used temporary ledgers, locally signed
synthetic candidate frames, and fake pipe, clock, cancellation, process, and
native-client collaborators. It did not start a helper, connect to XPC, use an
operating-system store, authenticate a user, contact a provider, or execute a
Meta mutation.

## Pending-to-exchange ordering

`_exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2`
first structurally validates and consumes the module-issued pending value. It
then reloads the exact retained record and every captured stream prefix before
it enters M47. The held request, challenge, source, and reviewed metadata stay
inside the module; the entry accepts only the kernel, ledger, pending value,
sealed V2 adapter, and cancellation seam. It does not accept a request,
candidate, receipt, key, source digest, clock, path, grantor, or provider
input.

The second source/prefix check and receipt-claim continuation run only after a
validated M47 candidate returns. The continuation consumes held data directly,
so it does not call `_pending_data` a second time. Append-only extension of a
captured stream remains valid; replacement, truncation, or changed selected
record does not.

The fixed error boundary is consistent with the one-use ordering:

- A malformed pending fails as `..._PENDING_INVALID` before M47 and does not
  disclose an internal attempted-state bit.
- A valid consumed pending that fails local coordinator, source-prefix, or
  reviewed-metadata checks before M47 returns
  `..._PENDING_EXCHANGE_PRECONDITION`; it remains spent.
- M47's definite pre-launch cancellation, unavailable transport, protocol,
  and time errors retain their exact diagnostics, with the pending already
  spent. A possible launch or lost response is M47's ambiguous,
  no-retry `..._EXCHANGE_UNVERIFIED` outcome.
- A returned candidate followed by a source, clock, receipt-claim, or
  recording failure returns `..._ACCEPTANCE_UNVERIFIED`. Provider execution
  does not follow that failure; an interrupted durable append can leave local
  claim/grant state that must be inspected, so absence is not inferred.

My independent regressions add three boundaries not duplicated by the owner
success-path cases: a constructed malformed pending cannot call M47 or the
fake helper; a pre-launch cancellation consumes the pending without a launch;
and a default V1 adapter is rejected before it can launch its V1 role, while
the V2 pending remains consumed. The owner tests separately cover a concurrent
race, one launch at most, pre- and post-exchange source drift, a legitimate
history extension, lost reply, cancellation after launch, clock rollback, and
expiry. Root's integration test derives a synthetic candidate from the
complete framed stdin request rather than peeking at pending state, and covers
normal completion, append-only growth during exchange, and lost reply.

## Fixed native helper

`NativeMetaCampaignPauseApprovalBridgeHelperRunner` has a separate typed V2
request/client/response/candidate path. It shares only the bounded pipe,
current code-identity, monotonic/UTC clock, and peer-liveness mechanics with
the V1 helper. The common operations surface has no request role, selector,
path, client factory, receipt, or key input. V1 frames cannot reach the V2
client, and the typed V2 response cannot be a V1 candidate or raw receipt.

The V2 executable takes no arguments. Arguments produce the exact Meta client
diagnostic and exit 64. With zero arguments, it calls the default generated
metadata factory before constructing the runner, inspecting pipes, validating
identity, or creating an XPC client. The checked-in metadata remains
unconfigured, so that route exits with the existing host-not-installed
diagnostic and status 78. The root hash guard verified all 16 working-source
unconfigured product cases, including the four new Meta helper cases; this is
unconfigured smoke evidence, not configured-runtime or installed-host proof.

The runner requires the fixed three-variable environment, pipe-only standard
descriptors, code identity, a single bounded V2 request frame and clean EOF.
It validates the bound challenge before V2 client creation; checks the parent
peer before and during the response/write paths; rejects UTC rollback or
expiry; bounds partial writes; invalidates the typed client on every terminal
path after construction; and emits no acknowledgement or retry protocol. Its
final comment was corrected to make the boundary precise: the runner itself
does not implement or expose authentication, protected-store reads, signing,
nonce consumption, provider contact, or a generic pipe route. A future typed
client may call the separate V2 XPC path, which handles approval and nonce
consumption without contacting a provider.

## Validation

The independent Python cases are in
`tests/test_meta_campaign_pause_pending_exchange_v2_ultra_review.py`. The
combined focused Python group passed **68 tests**:

```text
UV_CACHE_DIR=/private/tmp/adhelm-m48-uv-cache uv run pytest -q \
  tests/test_meta_campaign_pause_pending_exchange_v2.py \
  tests/test_meta_campaign_pause_pending_exchange_v2_ultra_review.py \
  tests/test_installed_meta_campaign_pause_approval_bridge_v2.py \
  tests/test_meta_pause_pending_pipe_integration_v2.py \
  tests/test_meta_campaign_pause_approval_bridge_transport_v2.py \
  tests/test_meta_campaign_pause_approval_bridge_protocol_v2.py \
  tests/test_meta_pause_private_handoff_v2_integration.py
```

The native owner recorded fake-only V2 helper tests at **7/0**, the combined
V1/V2 helper and framing group at **20/0**, and the full native package at
**284/0**. Native strict formatting and diff checks passed. Python owner
source/base strict mypy, Ruff, format, and diff checks passed before this
review.

## Limits retained

M48 adds no installed-binding resolver, generated runtime binding, public CLI,
configured host setup, or automatic execution. It does not claim that the
fixed helper is installed or that a configured exchange has occurred. A
pre-launch failure still spends a valid pending value; a lost reply or any
post-launch uncertainty requires a fresh review and never triggers retry.
Receipt acceptance records a local claim and exact direct grant for the
guarded kernel. It does not itself issue an egress permit or perform a provider
request. The caller-held metadata remains an expected local fact, not
independent installed provenance.

## Exact committed follow-up validation

The archived M48 commit `f04f627be2c91634a3d2cac3a4b5bc9932f2e6ac`
passed **3,388 Python tests**, **1,261 targeted ultra regressions**, strict typing
for **154 source files**, and the exact native suite at **284/0**. The native
product build and all **16 artifact-target refusal smokes** also passed. The
Python validation completed at `2026-09-06T21:37:49.460784Z`; its raw report
SHA-256 is `2d684f52aae977ecf434de24a48041395094e639b56a66055be7efa3fd69b732`.
These checks preserve the offline and unconfigured-execution limits above.
