# Initial enrollment follow-up review

This bounded independent review examined the post-M15 native follow-up in
`InitialEnrollmentProvenance.swift`, `ApprovalHostError.swift`, and
`AppKitReview.swift`. It also reviewed the related fake-only XCTest coverage.
No Keychain, Security, LocalAuthentication, AppKit UI, XPC, provider, or
network operation was invoked.

Three concrete boundary defects were addressed.

- The former fingerprint regular expression could accept a final line
  terminator under ICU matching rules. Fingerprints now require exactly 64
  lowercase ASCII hex bytes.
- At far-future dates, Foundation `Date` cannot preserve every microsecond.
  A `REVOKED` timestamp one microsecond before `ACTIVE` could therefore compare
  equal. Calendar validation still validates each timestamp, while revocation
  ordering now compares fixed-width canonical UTC text after padding
  whole-second forms to six fractional digits. Fraction validation begins at
  the whole second so a valid `.999999Z` timestamp cannot round into the next
  second during validation.
- Cancellation after the protected-work handoff originally had a completion
  ordering race: a worker could observe cancellation, throw, and publish a
  generic signing failure before the cancellation callback delivered its
  required recovery diagnostic. `EnrollmentOperationState` now selects the
  outcome under its lock, and both enrollment and revocation worker catches use
  that selector. A post-handoff cancellation reports
  `APPROVAL_ENROLLMENT_STATE_UNVERIFIED`, requiring local state inspection
  before retry; an uncancelled worker failure remains
  `APPROVAL_SIGNING_UNAVAILABLE`.

`EnrollmentProvenanceFollowupTests` covers exact fingerprint acceptance,
far-future microsecond ordering, and fraction/calendar validation.
`EnrollmentCancellationPhaseTests` covers the pure handoff state. The
independent fake-only
`EnrollmentCancellationFollowupIndependentReviewTests` pins the critical
ordering: cancellation changes the post-handoff state before the terminal
callback, and a worker failure observed in that interval must retain the
unverified-state diagnostic.

The native XCTest transcript at
`/private/tmp/adhelm-enrollment-provenance/root-reviewed-tests-m16-r2.txt`
reports 71 tests passed, with 0 failures and 0 skips, including these new
cases. `git diff --check` also passed for the reviewed working tree.

This evidence establishes only source-level, fake-adapter behavior. It does
not prove an installed code identity, real Keychain access controls,
LocalAuthentication behavior, AppKit cancellation, XPC identity, cross-process
linearization, current ACL inspection, recovery, rotation, or authorization to
sign or execute a provider action. `APPROVAL_ENROLLMENT_STATE_UNVERIFIED` is an
honest recovery signal, not evidence that no protected state changed.
