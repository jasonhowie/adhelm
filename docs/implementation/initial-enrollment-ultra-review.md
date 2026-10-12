# Initial enrollment provenance ultra review

Terra independently reviewed the native enrollment implementation using fake
code-identity, provenance, key, clock and cancellation seams. Root completed
the final fixes and validation after temporary Terra capacity failures. No
real Keychain, UI, authentication or XPC operation was exercised.

The review reproduced cancellation handoff gaps after enrollment identifier
generation and revocation timestamp generation. Both operations now recheck
cancellation immediately before persisting PENDING or REVOKED. The independent
suite also rejects trailing line terminators in canonical timestamps.

The builder found separate stores created by the listener and enrollment
factories, which gave overlapping operations separate locks. A process-local
registry now shares one coordinator for an exact fixed configuration. Root's
follow-up found that logical host/key aliases and overlap of only one protected
selector could still obtain distinct coordinators. The registry now rejects
incompatible configurations whenever either the key selector or provenance
selector can overlap, treating an omitted access group conservatively. Tests
cover aliases, different key/provenance selectors and access-group overlap.

The complete native suite passed 64 tests after these repairs. The exact
committed build and transcript are recorded separately by native validation.
Documentation now explicitly limits serialization to this process: a process
with equivalent entitlement can change an item after local observation. ACTIVE
creation provenance does not prove the current opaque ACL or establish a
cross-process revocation barrier. Installed pinning, key lifecycle integration,
witnessed OS behavior and release signing remain unevidenced.
