# Account-key identity in the safety kernel

The OpenAI Ads account-key variant represents current account access without
inventing OAuth scopes, a named remote principal, or an OAuth application.
It binds the planned account reference, native stable account ID, selected
authentication profile and a private credential HMAC reference. Its fixed scope
status is `NOT_DOCUMENTED`. The reviewed public account-key documentation does
not establish a named scope vocabulary; this status does not assert that a
provider mechanism cannot exist.

A runtime adapter must load the selected credential, compute its domain-separated
HMAC binding using a private local key, and prove the current account through an
authenticated account read. A local profile name or caller-supplied binding is
not that proof. The binding is neither a raw token nor a provider identity. A
credential rotation, selected-profile change, account change or identity-kind
change invalidates the approved plan. Future provider scope discovery requires
an explicitly reviewed model and adapter change.

The kernel compares this closed variant separately from scoped identity. It
cannot satisfy an OAuth identity, contain fabricated scope/principal/application
fields, or identify another provider. Legacy scoped identity serialization and
existing plan digests remain byte-identical. New account-key fields participate
in the complete plan digest. Adapter-returned identity objects are revalidated
before comparison, including the immediate pre-write read and reconciliation.

An account-key mutation also requires a `GuardedAdapter`. After the matching
fsynced `APPLY_STARTED` record, the kernel gives its guarded dispatch a
nonserializable, one-use permit bound to the exact composite digest, subplan
digest, and idempotency attempt. The permit is invalidated as soon as guarded
dispatch returns or raises, including in copied execution contexts, before
readback or verification. A reviewed guarded adapter must consume it
immediately at its fixed mutation dispatch. This is an in-process misuse
boundary for reviewed adapter code. It does not defend against hostile code
running with the same Python-process or operating-system privileges, and it
does not itself grant provider permission or make a mutation safe.

At consumption, the kernel also rechecks the current plan, direct grant or
delegation policy expiry under the permit's one-use lock. Slow adapter reads
cannot extend an expired authorization. A failed check permanently invalidates
the permit. If a durable start already exists, the conservative recovery barrier
remains even when the reviewed adapter sent no POST; no successful effect is
inferred from that rejection.

This model provides no mutation capability, permissions, direct approval,
delegation policy or trusted-host proof. The adapter still has to establish its
documented operation and enforceable preconditions. Exact durable authority,
expiry, all-provider preflight, drift rejection, fsynced starts, ambiguity
blocking and one-use execution remain required. The tests use synthetic
adapters and a synthetic approval host; they establish no live OpenAI access.
