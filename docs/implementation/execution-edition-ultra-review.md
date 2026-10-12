# Execution edition ultra review

> Historical. The configured edition, its guard and the thirteen tests below
> were removed in O3.2; the [release-policy gate](release-policy-gate.md)
> replaced them.

This review uses only temporary directories, synthetic plans, fake adapters,
an ephemeral signed bridge fixture, and mock HTTP. It does not launch a helper,
access an operating-system credential store, authenticate through native UI, or
contact a provider.

The configured `auditor` edition rejects provider mutation before adapter
preflight, key reads, native approval, OAuth, or HTTP. The central
`SafetyKernel.execute` guard is necessary because a caller could otherwise use
the generic kernel or the OpenAI kernel wrapper without either installed
workflow. It is checked at entry, after a coordinator-lock wait, around each
provider preflight and mutation start, immediately before legacy adapter
invocation, and through guarded-permit consumption.

The review has thirteen test cases covering:

- Both installed CLI leaves reject Auditor mode before output preflight, native
  readiness, or workflow dispatch.
- Direct kernel execution in Auditor mode cannot reach an adapter.
- An explicit Operator configuration retains normal execution behavior.
- A configuration change while waiting for the coordinator lock fails before
   provider preflight.
- A change during the final provider preflight fails before `APPLY_STARTED`.
- A change after `APPLY_STARTED` invalidates the guarded permit before egress.
- The same post-start change stops a legacy unguarded adapter before `apply`.
- Read-only reconciliation remains available in Auditor mode.
- OpenAI installed execution in Auditor mode stops before ledger, key, or
   bridge access.
- GA4 installed execution in Auditor mode stops before ledger, key, or
   bridge access.
- An OpenAI edition change across native approval prevents the provider POST.
- A GA4 edition change across native approval prevents OAuth and provider
    access.

Post-approval configuration changes can report the existing configuration-drift
code before the edition-specific code because the retained plan requires its
exact configuration digest. Both outcomes fail closed before a provider write.
The configured mode is a runtime workflow restriction only. It does not prove
paid distribution edition, installed-product identity, licensing, or an
external enforcement mechanism.
