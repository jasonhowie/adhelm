# Fixed-slot approval-key enrollment provenance

This document defines the bounded first enrollment protocol implemented in the
native macOS approval-host foundation. It is a local, uninstalled foundation:
its tests use fakes and no development command enrolls a Keychain key, opens an
authentication prompt, starts an XPC service, or signs a receipt.

It does not make an installed host a completed authorization boundary. In
particular, it does not install a verifier pin, create durable approval
authority, prove a customer reviewed a screen, or protect against a hostile
same-user process with equivalent code-signing and Keychain-access-group
privileges.

## Fixed configuration

TrustedHostConfiguration remains a reviewed build-configuration value. The
checked-in executable creates no instance, so it refuses launch rather than
reading a service name, requirement, key tag, access group, or selector from a
file, environment variable, command argument, XPC request, or challenge.

Initial enrollment is unavailable unless that configuration includes exactly one
InitialEnrollmentConfiguration:

~~~
provenance_service
provenance_account
~~~

The existing fixed configuration supplies the other facts: host reference, key
reference, key application tag, optional access group, P-256 storage policy,
and host code-signing requirement. There is no caller-selected slot, second
slot, rotation target, public-key pin, receipt configuration digest, or
replacement selector.

Before a local enrollment or revocation screen is displayed, the host validates
its own code against the fixed host requirement with SecCodeCopySelf,
SecRequirementCreateWithString, and SecCodeCheckValidity. The shared
coordinator repeats that validation immediately before every protected
operation. A failed validation reaches neither UI nor Keychain operations.

## Closed provenance record

The protected generic-password item's data is canonical JSON with a 4 KiB
maximum and no extension fields. The decoder rejects duplicate keys,
noncanonical JSON, non-safe integers, booleans in integer fields, unknown
fields, malformed base64url transaction IDs, noncanonical timestamps, and
state-inapplicable fields.

All forms contain:

~~~
schema_version: 1
state: PENDING | ACTIVE | REVOKED
enrollment_id: canonical 32-byte base64url
host_ref
key_ref
key_storage: KEYCHAIN_P256 | SECURE_ENCLAVE_P256
~~~

PENDING contains only those intent and transaction facts. It has no public
fingerprint or creation time.

ACTIVE additionally contains:

~~~
public_key_x963_sha256: lowercase SHA-256 hex
created_at: whole-second or six-fraction UTC timestamp ending Z
~~~

REVOKED preserves every ACTIVE fact and adds revoked_at. It can never be used
to sign. The initial protocol has no RETIRING, NEXT, replacement, or rotation
form.

The fingerprint is continuity evidence for the exact public key created during
the audited transaction. It is not a claim that the host can inspect the
opaque access-control policy of an existing Keychain item.

## Coordinator and ordering

One InitialEnrollmentCoordinator owns the fixed slot for sign, enroll, and
revoke within the host process. Its NSLock defines in-process linearization
only; this does not claim a cross-process mutex or protect against another
process that legitimately has the same access-group entitlement.

The coordinator holds its lease across each operation and re-reads the
protected record and observable key facts under that local lease. This avoids
stale interleavings among operations in this host process. It is not an atomic
cross-process transaction: an equivalently entitled process can modify a
record after this host reads it and before a later key operation. This
increment does not claim that such a change is observed before a signature,
nor that local revocation creates a cross-process signing barrier. It never
adopts, retries, deletes, or normalizes an ambiguity it does observe.

### Initial enrollment

1. The local AppKit settings controller validates fixed host code identity,
   renders an explicit fixed-key action, and obtains one fresh
   device-owner-authentication session.
2. Under the coordinator lease, code identity is checked again. The fixed
   provenance item and fixed key selector must both be absent. PENDING,
   ACTIVE, REVOKED, an orphaned key, selector mismatch, or any unreadable
   result stops before key creation.
3. The coordinator writes a protected PENDING item using that fresh
   LAContext, with Keychain interaction disabled so no unexpected second
   prompt can occur.
4. The Security adapter creates only a permanent EC P-256 private key with the
   fixed tag/access group, kSecAttrAccessibleWhenUnlockedThisDeviceOnly, and
   privateKeyUsage plus userPresence. SECURE_ENCLAVE_P256 requires the Secure
   Enclave token and never falls back to a software key.
5. It reads observable private-key attributes, obtains the public key, accepts
   only a 65-byte ANSI X9.63 uncompressed P-256 representation, hashes it, and
   updates the same record to ACTIVE using the same fresh context.

The public SecKeyCreateRandomKey declaration documents key attributes but does
not document an LAContext parameter. The coordinator requires the fresh
session for the transaction and writes PENDING through that exact context
before generation; reads, record updates, and signing reuse it with UI
disabled. This does not claim that the key-generation C API independently
attests the authentication session.

Any failure after PENDING is durable leaves recovery required. The host does
not retry generation, promote a key, delete a record/key, or silently adopt an
orphan. A later recovery workflow needs its own reviewed contract.

The local cancellation handle is rechecked under the coordinator lease before
PENDING persistence, key creation, and ACTIVE promotion. A cancellation before
PENDING has no protected effect. A cancellation observed after PENDING leaves
that non-signing recovery state rather than promoting an identity. If a
Security call already crossed its checked cancellation boundary, a created key
may remain beside PENDING; later completion still fails closed and is never
retried or deleted automatically.

### Signing

The typed receipt signer never exposes a SecKey or a generic sign-bytes
operation. After the existing review and fresh-authentication state machine
reaches signing, it acquires the fixed-slot coordinator lease and:

1. validates fixed host code identity and fixed enrollment configuration;
2. claims the one-use receipt permit immediately before the first protected
   provenance/key read;
3. loads the protected record through the same authenticated context with UI
   disabled;
4. requires ACTIVE, exact host/key/storage facts, and a matching observed
   X9.63 public-key fingerprint; and
5. reloads the exact configured private key and fingerprint immediately before
   SecKeyCreateSignature over the existing domain-separated typed receipt
   payload.

Once claimed, the permit remains consumed if any later read, pin check, or
signing call is unavailable or ambiguous. A PENDING, REVOKED, missing,
malformed, mismatched, or substituted record/key cannot sign.

### Revocation

Revocation is a separate explicit local AppKit action followed by fresh
authentication. Under the same coordinator, it rechecks code identity and
fixed ACTIVE provenance, then writes REVOKED before any hypothetical key
deletion. This increment intentionally has no deletion, replacement, rotation,
or automatic retry. A failed record update is unavailable/recovery work, not a
reason to touch the key.

## Security boundaries and remaining work

The creation record and exact public fingerprint establish a narrow
creation-time continuity claim. They do not provide:

- later inspection of privateKeyUsage and userPresence on an opaque existing
  SecAccessControl;
- hardware attestation, proof of a person's identity or comprehension, or a
  hostile-same-user protection boundary;
- a client-side pinned public key, a receipt-v2 enrollment binding, a pin
  registry, or a durable kernel grant;
- cross-process enrollment locking, automatic orphan cleanup, recovery,
  deletion, replacement, or rotation; or
- live evidence that a signed installed app, Keychain access controls,
  LocalAuthentication, XPC identity, receipt verifier, durable nonce handling,
  and provider execution all work together.

The generic-password provenance item uses the same configured access group as
the fixed key. A process with equivalent entitlement can potentially modify
both; the fingerprint does not repair that trust assumption. The design does
not claim otherwise.

## Offline evidence

The core codec and coordinator use fakeable, narrow adapters for code identity,
provenance storage, fixed-key observation/signing, timestamp generation, and
enrollment-ID generation. Offline tests cover state-specific record decoding,
base64url pad-bit rejection, timestamp/calendar checks, PENDING crash
retention, duplicate/orphan/revoked rejection before key creation, code-identity
ordering, same-session forwarding, permit-before-read ordering, fingerprint
mismatch, record-only revocation, and an unconfigured host.

The real adapters compile against the macOS 26.2 SDK declarations in
SecCode.h, SecAccessControl.h, SecItem.h, and SecKey.h; offline tests do not
execute those APIs. An installed-product validation and separate
customer-controlled pin/provenance bridge remain required before this can
support durable receipt verification or operational authorization.
