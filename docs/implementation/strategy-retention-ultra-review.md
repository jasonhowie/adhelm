# Strategy retention ultra review

Root independently reviewed Terra's one-record strategy retention and verified
all six normalized child boundaries, exact per-currency totals, ledger envelope
semantics, and the bounded catalog/show paths.

The review reproduced numeric values being accepted as literal boolean control
flags: `1`/`1.0` for required review flags and `0`/`0.0` for remote side effects.
Exact original JSON boolean validation now rejects these forms both before
append and when verifying rehashed stored history. Six additional cases reject
false provider/account/run/source/observation/occurrence provenance.

The final review also found that expiry could pass while the ledger waited or
built its record after the caller's last check. Retention now checks expiry in
the ledger's private callback immediately before durable append. A regression
advances the injected clock at that handoff and proves no strategy observation
is retained.

The original strategy input and native specifications are deliberately omitted
from history. Their source digests identify the original compilation; they
cannot be independently recomputed from an export and never become planning
authority. Exact child reviews and spending remain self-validated. The 14
independent regressions supplement the builder's retention/CLI tests; exact
snapshot totals are recorded by the development validator.
