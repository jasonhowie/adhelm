# Approval acceptance ultra review

Terra performed independent adversarial review of the root-built ledger and
acceptance integration. Root completed the reported fixes and regression runs
after the review agents encountered model-capacity errors. The saved independent
tests remain separate from the builder's functional tests.

Reproduced and repaired findings:

- A partially implemented typed policy host fell back to a generic proof. Any
  typed-method presence now requires both callable kind-specific methods.
- Host exceptions and truthy non-booleans could escape or establish a proof.
  Proof calls are bounded and require exact `True`.
- Expiry between proof and final authorization append could record a grant.
  A final proof/expiry callback runs under the changes-stream append lock.
- A still-current receipt could install an already expired policy. Subject
  eligibility is checked before claim and again at the durable handoff.
- Future requested revocation times were discarded into immediate revocation.
  Acceptance now rejects a future requested instant; the signed requested time
  remains in the claim and actual local effect begins at durable recording.
- Low-level constructed challenge instances and a raising clock escaped as raw
  exceptions. Both paths now return bounded diagnostics before consumption.
- Generic export projection leaked raw receipt/signature/nonce/public-key
  material. An explicit claim projection retains only compact redacted facts.

Independent tests also prove one concurrent winner, claim-before-grant order,
wrong-pin rejection before history reads, historical verification after expiry,
and no automatic replay following post-claim recording uncertainty. Root added
actual Swift-signed receipt acceptance and a policy-specific final-append expiry
regression. Counts for the exact committed snapshot are in the development
validation record; this review does not establish installed authority or live
execution.
