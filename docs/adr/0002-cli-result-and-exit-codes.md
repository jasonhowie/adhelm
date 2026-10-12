# ADR 0002: CLI, result, and exit-code contract

> Names superseded by [ADR 0013](0013-open-source-release.md): the product, package and command were renamed to AdHelm. The text below is the historical record.

- Status: Superseded for exit codes by 0002-machine-interface-and-editions.md, which matches src/adcp/errors.py and `adcp discover`.
- Date: 2026-09-04

Shipped exit codes are 0 success, 2 invalid input, 3 failed precondition,
4 authorization required, 5 partial, 6 ambiguous, 7 pending, 10 internal failure.

## Decision

The primary interface is noninteractive, versioned, and machine-readable.
Human tables and optional artifacts render the same typed result. Every command
supports structured output without waiting for terminal input and returns:

```text
schema_version, command, tool_version, outcome, data, warnings,
diagnostics, plan_ref, run_ref, status_ref, artifact_refs, next_actions
```

Results are bounded by explicit pagination or local references. Truncation is
never success. Stable process exit codes are:

| Code | Meaning |
| --- | --- |
| `0` | `COMPLETED` with the requested safe outcome |
| `2` | invalid command, schema, or user input |
| `3` | failed precondition or unsupported capability; no remote effect |
| `4` | authentication, scope, or account identity action required |
| `5` | direct authorization or covering delegation required |
| `6` | accepted/pending; use the returned `status_ref` |
| `7` | partial effect; reconcile before new writes |
| `8` | ambiguous remote state; never retry blindly |
| `9` | internal integrity or invariant failure |

Provider outages, rate limits, and policy errors use typed diagnostic codes and
map to the outcome proved by read-back, not merely the HTTP status. A composite
result carries one provider result per selected subplan.

## Consequences

- Agent hosts can branch on schema fields and exit codes without parsing prose.
- Exit `0` never hides warnings that invalidate completeness.
- Direct authorization may use an interactive local adapter, but execution
  still consumes the same digest-bound machine contract.
