# ADR 0004: macOS installed approval bridge

**Status:** Accepted for bounded offline protocol implementation on 2026-09-05.
Review is complete. This does not authorize a runtime bridge, host installation,
client signing, key enrollment, or an execution command.

## Context

The native host has bounded pieces of an approval boundary: static build
metadata, mutually constrained XPC review requests, deterministic native review,
current `ACTIVE` enrollment/key verification, and fixed-slot receipt-redemption
state. The Python engine separately has the canonical plan, local JSONL ledger,
receipt parser, exact source-plan checks, authorization recording, OAuth
lifecycle, and provider execution.

They are deliberately not connected. `AuthenticatedApprovalXPCClient` can
request a receipt from the fixed Mach service, but it returns only a receipt
candidate. The fixed-slot coordinator resolves the current `ACTIVE` key and
then invokes `PinnedReceiptVerifier`; the pure verifier itself accepts a
supplied pin. Python's `PinnedApprovalTrust` and receipt-acceptance inputs
remain development seams; they are not an installed pin resolver. The M31
redemption permit is process-local and non-Codable, so it cannot be passed to
Python as authority.

The product contract does not claim to defend ordinary same-UID files,
arguments, or processes from hostile code with equivalent operating-system
permissions. It does require that model, provider, document, and command
assertions cannot create authority; direct authorization needs an actual
host-mediated user-origin interaction. It also requires durable one-use
authorization and `APPLY_STARTED` before provider egress.

The accepted offline protocol scope is only the current direct retained-plan
subset: `OPENAI_PAUSE_PLAN` and `GA4_KEY_EVENT_CREATE_PLAN`. Existing v1
delegation-policy installation and revocation, plus every other approval kind,
remain on their existing policy paths and are not bridge inputs or results.

## Proposed decision

Keep Python as the sole plan, ledger, credential, and provider-execution owner.
Do not introduce a second protected provider executor.

The smallest proposed installed bridge is one compiled, signed native client
helper that communicates with one compiled, signed native approval host over a
fixed mutually authenticated XPC connection. The Python bridge launches that
helper at its one reviewed absolute installed path with **no arguments**, using
`shell=False`, `close_fds=True` except its two pipe descriptors, and no shell.
Its fixed sanitized environment has no `PATH` or runtime-selection variable.
Both identities, the Mach service, host/key references, limits, code-signing
requirements, and the helper path come from reviewed build constants. There is
no runtime metadata loader, caller-selected service, code requirement, key
selector, public-key pin, endpoint, shell command, or generic XPC method.

The helper's only Python transport is one inherited anonymous `stdin` pipe and
one inherited anonymous `stdout` pipe. The parent retains the other ends; they
are never a terminal, CLI stream, socket, or file. The helper rejects non-pipe
standard input or output and every runtime selector. It accepts exactly one
bounded canonical request frame, returns at most one bounded canonical response
frame followed by EOF, and then exits. The launcher applies a fixed timeout and
cancel cleanup, closes both parent handles on every result or failure, and never
retries the request automatically. `stderr` is fixed sanitized diagnostics, not
a candidate channel. This is a disciplined privacy and API boundary under the
stated same-UID limitation; it is not parent authentication against hostile
same-UID code.

Before spawn, the Python bridge creates a private, one-use pending entry keyed
by a fresh 32-byte random request ID. It retains the source plan record digest,
verified workspace/checkpoint context, and canonical action bytes or digest,
then derives an opaque, domain-separated source-context digest from the exact
source plan record digest, workspace profile, and verified ledger checkpoint.
It also retains the expected canonical challenge and its digest, plus the
compiled full installed-metadata and protocol-configuration digests. It derives
one canonical private request digest that binds the request ID to those
challenge, source-context, metadata, and protocol facts. No digest, trust input,
or source-context fact comes from the caller or a returned candidate.

The helper accepts only that bounded canonical **expected challenge**, the
fresh request ID, opaque source-context digest, and canonical private request
digest. It derives the fixed metadata and protocol facts from its reviewed build
rather than taking them from a request. It does not accept a user-provided
receipt, a trust pin, a grant, a plan object, or a provider request. The host
performs its own canonical challenge validation. It presents the deterministic
challenge-bound review, requires the user to explicitly approve that review,
then requires fresh LocalAuthentication and a protected signature.
LocalAuthentication success alone is not approval. The host then performs
current receipt verification and one-use native nonce consumption, including
durable readback, before a result is returned.

The existing receipt-only public `AuthenticatedApprovalXPCClient.requestApproval`
route is forbidden for installed acceptance. It may not feed a Python
acceptance path or be upgraded into a candidate. The bridge instead requires a
separate internal post-consumption candidate response whose host implementation
cannot reply before current verification and durable nonce consumption succeed.

The helper returns a private, bounded candidate only to the installed Python
bridge library during the original live request exchange. It crosses only that
fixed private pipe boundary; it is never persisted, imported, emitted to an
agent/public API, or replayed. Its closed fields are proposed to bind:

- the fresh request ID and canonical private request digest, which binds it to
  the opaque Python source-context digest and exact challenge digest;
- the canonical expected challenge bytes and exact challenge digest;
- canonical receipt bytes and their digest, identifier, kind, and expiry;
- the fixed seven-field protocol-configuration digest;
- the exact full compiled installed-metadata digest;
- the current `ACTIVE` public-key X9.63 bytes and fingerprint, as
  host-observed evidence rather than a Python-selected or reusable trust pin;
- the native observation time and a fixed result status.

Receipt bytes and the public key are not secrets, but neither belongs in CLI
output, agent output, a document, a file, a generic API, or a reusable trust
object. The Python bridge consumes its pending entry once, reparses the
candidate's canonical challenge and receipt. It requires the candidate challenge
bytes to equal its private expected challenge byte-for-byte before parsing, then
independently verifies every exact binding and signature. It rejects every
result status except the one fixed post-consumption success value. It validates
a canonical native observation timestamp sampled after the final native
`VERIFIED` and global-state readback, with
`receipt.issued_at <= native_observed_at < receipt.expires_at`. Python still
applies its own trusted clock to receipt expiry and currentness. It rejects a
native observation later than Python's current time and a Python-clock rollback
during the exchange; native time alone is not a currentness certificate. Before
a claim, Python reloads the source plan through a verified ledger checkpoint and
requires the retained plan record, workspace context, and canonical action to
equal its private pending entry. The native side only echoes the opaque
source-context digest and private request digest; it makes no ledger-validation
claim. Python also checks the request ID, full installed metadata,
protocol-configuration, and key facts, then calls the existing exact local
ledger acceptance path. The bridge's expected full-metadata and protocol digests
are generated into its reviewed installed build, never loaded from an artifact,
manifest, or configuration file at runtime. The full-metadata digest closes
static limit, bundle, service, and selector drift beyond the seven-field
protocol projection; a digest alone never attests code identity. A production
execution surface must obtain this candidate from the bridge itself; it must not
accept `PinnedApprovalTrust`, receipt bytes, or a caller-built direct grant as
public inputs. Injectable equivalents remain test-only seams.

The native candidate is evidence only of completed native current verification
and native nonce consumption. It is not a Python grant, provider authorization,
provider credential, execution permit, or proof that egress occurred.

## One-way durable ordering

The proposed ordering intentionally has no Python-to-native acknowledgement:

```text
exact retained Python plan
  -> canonical expected challenge
  -> one-frame private pipe to fixed signed helper
  -> mutually authenticated XPC / host review, explicit user approval,
     fresh authentication, protected signature
  -> native current-key verification + durable nonce consumption
  -> private exact-bound candidate
  -> Python source reload/recomparison + receipt verification + exact JSONL claim/grant
  -> existing kernel durable APPLY_STARTED
  -> Python provider adapter egress
```

Before returning a candidate, the native side must finish its fixed-slot
verification and durable replay transition, including exact readback. A
native `PENDING`, `INTENT_PENDING`, missing initialized global state, changed
readback, clock rollback, expiry, or uncertainty returns recovery-required and
does not return a candidate.

This order fails closed without a cross-store acknowledgement:

| Failure point | Required result |
| --- | --- |
| Native consumption succeeds; helper return is lost or Python exits | The nonce is burned, no Python grant exists, and a new challenge/user review is required. |
| Candidate is stale, swapped, tampered, or expires before Python records it | Python rejects its correlation, challenge, receipt, configuration, or time binding; the native nonce remains consumed. |
| Python claim/grant write is uncertain after native consumption | Existing ledger ambiguity handling applies; inspect history before another review or execution. |
| Python successfully records a claim/grant but loses its result | Native and JSONL replay records each reject duplicate use; no blind retry follows. |

An acknowledgement would create another non-atomic native-state/JSONL failure
edge without restoring a transaction across those stores. It is not proposed.
The native `VERIFIED` state must never be labeled as Python acceptance, direct
authorization, or provider execution.

## Revocation and currentness assumptions

The host checks fixed host/key/storage intent, `ACTIVE` provenance, and the
currently observed public-key fingerprint while it holds its in-process
fixed-slot lease. It verifies the exact receipt at the checked native time
before native consumption. This is a point-in-time local check; it does not
create cross-process atomicity against another entitled process changing a
protected item after observation.

For this proposed bridge, signing-key revocation is **prospective**. Native
current verification plus durable nonce consumption is the acceptance
observation. A revocation observed before that point prevents a candidate. A
revocation that occurs after that point cannot retroactively withdraw an already
returned private candidate, including when Python records the exact claim later
within its existing expiry. It does stop later signing/current-acceptance
observations. An already recorded direct grant likewise continues under its
existing exact expiry and ledger-consumption semantics unless an authorization
mechanism explicitly revokes that grant. If product policy later requires key
revocation to invalidate unstarted direct grants, the kernel needs a new fixed
native current-proof or approval-epoch check at each `APPLY_STARTED` boundary.
That is a separate authorization-contract change, not a bridge acknowledgement.

The host's review and fresh authentication are the sole proposed source of a
user-originated direct approval. XPC code-signing requirements establish peer
code identity, not human intent. Fixed installation, host code identity, and
peer requirements remain mandatory and fail closed when unconfigured or wrong.
The bridge does not claim to resist hostile same-UID code, an equally entitled
process, a compromised approved signer, hardware-attestation failure, or a
cross-process revocation race.

## Alternatives considered

### 1. Fixed signed client helper with exact request-bound result — proposed

This preserves Python's existing execution and ledger ownership while binding
the native review/current-key decision to one exact retained-plan challenge.
It is narrow enough to avoid a generic subprocess or XPC passthrough and makes
failed native consumption safely expensive rather than replayable.

### 2. Separate public enrollment-pin/currentness resolver — rejected alone

A resolver that returns an active public key or pin without an exact receipt
binding permits stale/cross-request use, encourages caching and caller-created
`PinnedApprovalTrust` values, and does not resolve replay. A receipt-specific
currentness result may be an internal part of the proposed helper response, but
there must be no standalone caller-selectable pin or generic key-lookup API.

### 3. Embed Python under the signed native host — rejected

Embedding the engine would require an interpreter/module-distribution trust
chain, credential and ledger ownership changes, new provider-egress lifecycle
controls, and a larger native execution boundary. That complexity is not
justified by the selected v1 threat model and would not itself establish a human
approval or cross-process atomicity guarantee.

## Required later implementation evidence

Any promoted implementation must prove, with fake-only tests first, that it:

- rejects every noncanonical, oversized, expired, mismatched, replayed,
  pending, or uncertain input before a Python grant or provider operation;
- rejects a non-pipe descriptor, extra or trailing frame, early EOF, timeout,
  cancellation after native consumption, helper-path substitution, environment
  selection, or a candidate/source-context mismatch before a ledger claim;
- cannot select trust, XPC routing, or key material from a request or command;
- binds a returned candidate to one exact request, challenge, receipt, full
  installed metadata, protocol configuration, key fingerprint, kind, expiry,
  and local plan source;
- leaves a native consumed nonce unusable when delivery or Python recording is
  lost, and reports recovery rather than retrying;
- preserves existing JSONL claim and `APPLY_STARTED` ordering; and
- keeps receipt/key/intermediate facts out of agent-visible output and artifacts.

Real installation evidence remains separate: reviewed host/client identities,
signed and registered artifacts, fixed enrollment/provenance state, actual XPC
peer checks, actual AppKit/LocalAuthentication behavior, Keychain behavior,
and an authorized end-to-end review. None is supplied by fixture tests or an
unsigned development artifact.
