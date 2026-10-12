# Offline strategy compilation

`adhelm.strategy` implements a bounded local `StrategyBrief` aggregate. It is
not an executable plan, grant, ledger record, provider request, or a claim of
common semantic verification.

The brief has a versioned immutable content-addressed ID, customer-declared
intent statement, expected account bindings, ordered selected provider briefs,
per-provider exact maximum spends, typed per-currency total caps, assumptions,
provenance, approval owner, and expiry. It accepts one through six selected
providers. A provider is represented by a discriminated closed wrapper around
its own native brief, so generic payloads and provider extension passthroughs
are impossible.

The compiler accepts only each reviewed native allowance: Google
`advertising_channel_type=SEARCH` with the `target_spend` bidding field, Meta
`OUTCOME_TRAFFIC`, LinkedIn `ENGAGEMENT`, TikTok `TRAFFIC`, Reddit `CLICKS`,
and OpenAI `bidding_type` of `impressions` or `clicks`. These are separately
typed provider fields; this module does not create a common objective enum.
Customer intent, provenance, and approval owner remain customer declarations,
not proof of provider semantics or authorization. Required native conversion,
unreviewed targeting, and unreviewed tracking requirements reject before
compilation.

A `CompiledStrategy` is returned only after every selected child compiler
succeeds. A child failure returns no aggregate. The aggregate retains the
closed source brief along with its account bindings, caps, expiry, owner,
assumptions, and provenance. Result validation recompiles that source and
requires exact equality for every child native spec and typed redacted review.
This makes an altered serialized child, or context changed independently of
its source, fail even when an attacker recomputes child digests. A complete
replacement of the closed source is a distinct candidate and needs a later
digest-bound authorization; this offline compiler makes no claim to authorize
it. It sums `Money` only by identical ISO currency, preserves the original
amounts, and rejects a missing or exceeded currency cap. It has no FX
conversion or synthetic grand total.
OpenAI retains its activation boundary and lossy/unsupported mappings inside
the preserved child spec; its spend comes from that activation boundary.

The result is always `OFFLINE_CANDIDATE_NOT_EXECUTABLE` and
`FIXTURE_ONLY_NOT_LIVE_VALIDATED`. Account identity/currency, provider
eligibility, creative and native-conversion readback, tracking, drift,
authorization, execution, and verification remain each child’s unmet planning
boundary. In particular, this module cannot demonstrate that an intent is
provider-representable or that the six provider specs share a common result.

The public `StrategyBrief` and `CompiledStrategy` boundaries revalidate nested
business intent, expected accounts, provider-native briefs, `Money`, and child
candidates through their closed discriminated types. This prevents a nested
`model_copy()` or `model_construct()` value from relying on Pydantic's
already-built-instance fast path. Strategy expiry and the injected clock must
both use a zero UTC offset; assumptions and provenance entries must be
non-blank.

`CompiledStrategy` is an internal local integrity object: it retains the
closed source and native child specs to prove their exact relationship. It is
not an agent-output schema and must not be emitted directly. A separate
redacted CLI projection may expose only its approved review fields, digests,
typed totals, and explicit unmet boundaries. This module does not call a
provider, write history, authorize an action, or perform an HTTP, credential,
CLI, MCP, or website operation.

The installed `compile strategy --input <file>` command exposes a separate
agent-safe projection after all selected compilers finish. It preserves ordered
child spec digests, each complete typed review and planning boundary, OpenAI's
activation boundary and mapping statuses, exact currency totals/caps, expiry and
an opaque owner reference. Source intent, assumptions, provenance, native
requests, account IDs, copy and destinations remain outside agent output.

Optional `compile strategy --retain` creates one closed
`STRATEGY_CANDIDATE_COMPILED` history observation after every selected compiler
and normalized child review succeeds. It stores no source brief or native
specification and is neither a plan nor authorization. See
[strategy candidate history](strategy-candidate-history.md).

The CLI bounds input to 1 MiB, rejects duplicate/non-finite JSON, and validates
the original bytes so integer-looking floats cannot become typed integer money
through canonicalization. It checks the complete redacted output including its
newline against the saved limit before accessing the local sharing key. An
invalid/expired/over-limit brief returns no partial candidate or new history.
`schema strategy-brief --root-only`, `--definitions` and `--definition <name>`
support progressive loading; all fragments bind the same complete schema digest.
