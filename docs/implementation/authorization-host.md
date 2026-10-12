# Authorization host boundary

The safety kernel accepts a direct grant only when a host integration creates it
after a user-originated approval interaction. The grant records an opaque user
interaction reference, the host reference, grantor reference, the exact composite
digest and ordered subplan digests, and an expiry. An agent message, model output,
provider text, document checkbox, command argument, or opened action link cannot
be converted into this grant. `AGENT_ASSERTION` is rejected by the schema.

The initial local CLI host must document how it distinguishes a user action from
untrusted agent input before it exposes direct approval. Same-user files and
arguments are not asserted to be a boundary against a hostile process with the
same operating-system permissions. Until that integration exists, callers should
use only a previously user-configured delegation policy or return a configuration
need; they must not fabricate grant provenance.

The host integration calls `record_direct_grant` only after its trusted
user-origin check succeeds. Execution rejects a caller-supplied grant unless the
canonical `changes` ledger contains that recorded approval for the same plan
digest. Likewise, policy setup calls `install_delegation_policy` through the
trusted host; execution rejects policies absent from the durable ledger or whose
installed digest differs. `revoke_delegation_policy` leaves a durable local
revocation record that prevents subsequent execution.

Delegation policies are themselves user-controlled configuration. They name the
configured agent or host, stable accounts, allowed operation kinds, finite
per-operation and cumulative currency limits, effective window, allowed egress
hosts, and a source authorization reference. `ACTIVATE` and `PUBLISH` occur only
when included explicitly. The kernel serializes stable account and policy locks,
consults canonical `changes` JSONL records for prior reservations, and records a
full composite reservation with the first durable `APPLY_STARTED`. That makes
cumulative limits survive restarts and concurrent local processes on the one
configured coordinating host.

Each policy carries `USER_CONTROLLED_CONFIGURATION`, its configuration host, and
an opaque user-interaction reference. The schema rejects `AGENT_PROPOSAL`; the
installing host must match the recorded configuration host and verify that
interaction before a durable policy record is appended.

Direct or delegated authority is one-use: the first `APPLY_STARTED` consumes the
composite. A failed preflight appends no start and consumes nothing. After a
partial or ambiguous run, reconciliation and a new composite plan are required;
the original authorization is never replayed.

The [macOS implementation preparation](macos-approval-host-design.md) describes
a concrete signed local host and protected receipt boundary researched from
the installed Apple SDK. It is a design for the missing integration, not an
installed approval mechanism or evidence of a user authentication ceremony.
