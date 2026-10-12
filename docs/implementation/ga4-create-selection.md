# GA4 CREATE history selection

`select_ga4_key_event_create_candidate` selects a retained GA4 candidate and
its latest retained conversion definition from one verified ledger checkpoint.
The caller supplies the full source input again. The helper recompiles that
input and requires byte-for-byte equality with the retained compilation.
Changed event semantics, destination, stream or contract cannot reuse an older
candidate. Source event prose is never reconstructed from history or retained
in the selection result.

The latest definition must be effective at the explicit evaluation instant.
A newer future-effective definition still blocks an older candidate; scheduling
and an earlier-version fallback are not inferred. The eventual configured plan
workflow obtains the evaluation instant from its own clock and rechecks history
before retention and execution. Selection releases checkpoint locks normally;
it does not reserve the contract or establish continued currentness.

The detached result is internal local data containing the normalized native
request. It must pass the existing redacted sharing boundary before exposure.
There is no CLI command, append, provider call, credential lookup, authorization,
semantic-accuracy claim, or execution path in this module.

Tests use real temporary JSONL ledgers with synthetic declarations. They cover
one-checkpoint selection, supersession including future declarations, exact
effective-time boundaries, fresh input changes and invalid pre-read inputs.
