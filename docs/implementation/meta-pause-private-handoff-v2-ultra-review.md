# Meta PAUSE private handoff V2 independent review

Status: `REVIEWED`.

This review covers M46's private retained-source preparation, V2 candidate
acceptance, and package-only native post-signing handoff. It uses temporary
ledgers, synthetic P-256 receipt material, in-memory native fixed-slot state,
and fake review/authentication collaborators. It does not start a native host,
open XPC or a helper, access an operating-system credential store, invoke a
provider, or claim that a synthetic candidate has native origin.

## Boundaries verified

`_prepare_installed_meta_campaign_pause_approval_bridge_v2()` accepts only an
exact retained `META_CAMPAIGN_PAUSE_PLAN`, records all three verified ledger
stream prefixes, derives its logical grantor from the retained record digest,
and creates fresh private nonce/request-ID values. The caller-held reviewed
metadata value detaches and rechecks the seven protocol configuration facts;
its full metadata digest remains an expected fact only. It is not an installed
binding, host-provenance proof, or provider authority claim.

The Python pending value checks its module issuer, CPython `LockType`, exact
source/action bytes, V2 request, challenge, and metadata before candidate
inspection. Corrupt pending data returns `PENDING_INVALID`; a previously
attempted pending value returns the non-retryable `PENDING_CONSUMED`. Once a
structurally valid pending request has been consumed for candidate handling,
coordinator mismatch, source-prefix drift, clock rollback/expiry, malformed
candidate material, receipt claim failure, and grant-recording failure collapse
to `ACCEPTANCE_UNVERIFIED`. That preserves the consumed exchange and prevents
a retry from treating native nonce consumption as absent.

The retained-source reload permits an append-only ledger suffix. It requires
the original stream identity, high-watermark, and head digest for `changes`,
`creative`, and `performance`, then reloads the exact selected record and
canonical action. A replacement in either non-`changes` stream therefore
blocks the claim after a candidate and leaves the pending request consumed.

The V2 acceptance wrapper reparses the candidate at every local current-time
sample, compares the candidate challenge to the held challenge, and keeps the
receipt and current X9.63 key internal until the shared exact receipt-claim
core uses them. The independent test makes the public legacy
`_checked_trust()` path fail if called; V2 candidate acceptance succeeds
without constructing `PinnedApprovalTrust`.

Native source review confirms the package-only V2 producer verifies the signed
receipt against the current ACTIVE key before its first global
`INTENT_PENDING` or nonce `PENDING` write. It verifies again before promotion
to `VERIFIED` and global `READY`, performs exact readback, then lets a one-use
permit construct the candidate. Cancellation, expiry, rollback, or candidate
construction failure after the durable boundary returns no candidate and keeps
the recovery state. The engine holds the fresh authenticated session only
during the synchronous lease callback and suppresses a candidate if
cancellation or expiry wins afterward.

V1 remains separately allowlisted to OpenAI PAUSE and GA4 CREATE. Its request
builder refuses the Meta challenge. The existing raw-receipt XPC method retains
its bounded Meta header refusal before it acquires the interaction lease; M46
adds no XPC selector, helper protocol, CLI command, resolver, or provider
route.

## Review repairs verified

The final source uses `_thread.LockType`, rather than treating the `Lock`
factory as a runtime type. It also leaves malformed pending values outside the
candidate boundary while moving coordinator/source/current/acceptance work
inside the post-candidate ambiguity boundary. The base and independent tests
exercise both distinctions.

No open finding remains within this review scope.

## Limits

The private metadata object does not seal itself against coherent arbitrary
same-process rewriting. That is outside the documented threat model; the
artifact/static-binding boundary must provide provenance evidence separately.

The root integration's three synthetic scenarios prove only Python composition:
configured retained plan to V2 pending/candidate to shared exact direct grant,
an append-only source extension, expiry after candidate with no claim, and
pending reuse refusal. They do not prove a native host ran, a Keychain slot was
used, or Meta accepted a PAUSE operation. A direct grant likewise remains only
local authorization evidence for the later guarded kernel path.

## Validation

The frozen Python focused group passed 16 tests:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-m46-uv-cache uv run pytest -q \
  tests/test_installed_meta_campaign_pause_approval_bridge_v2.py \
  tests/test_meta_pause_private_handoff_v2_integration.py \
  tests/test_installed_meta_campaign_pause_approval_bridge_v2_ultra_review.py
```

It includes seven owner cases, three root synthetic integration cases, and six
independent cases. The independent test file also passed strict mypy, Ruff,
and format checks. Root retained the combined transcript at
`/private/tmp/adhelm-m46-final-python-tests.txt`.

The native owner reported the fake-only producer/handoff focused group at 8/0
in `/private/tmp/adhelm-m46-focused-swift-tests.txt` and the sandboxed full
package suite at 263/0 in `/private/tmp/adhelm-m46-full-swift-tests.txt`. Those
cover current-key pre-reservation rejection, durable global/nonce ordering,
post-consumption cancellation, engine cancellation during a blocking consumer,
final post-consumer expiry suppression, protocol mismatch before UI, and V1
Meta refusal.
