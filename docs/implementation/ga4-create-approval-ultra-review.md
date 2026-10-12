# GA4 CREATE approval ultra review

## Scope

This independent review covers the closed GA4 key-event CREATE approval
challenge, receipt, local trusted-host review, and Python receipt acceptance.
It uses only canonical fixtures, temporary ledgers, ephemeral P-256 keys, and
the native Core package. It does not initialize Keychain, LocalAuthentication,
XPC, a provider client, or a network transport.

## Findings and repairs

Two native parser parity gaps were reproduced while reconstructing complete,
rehashing action fixtures rather than changing an isolated hash value:

1. The native request validator accepted `ONCE_PER_SESSION`. It now requires
   exactly `ONCE_PER_EVENT`, matching the Python challenge builder and the
   GA4 candidate binding.
2. The native decoder did not bind the compiler's ordered
   `declared_field_names` to the conversion contract's ordered Boolean fields.
   It now requires exact equality.

Both repairs reject the malformed action before local review or receipt
creation.

The review also confirmed that trusted local native review displays the safe
configured OAuth profile, property and web-stream identifiers, event name, and
fixed counting/default-value semantics. It does not include a raw request
path/body, customer source prose, OAuth principal/client facts, or tokens.
Portable ledger export remains a separately redacted boundary: receipt claims
contain only the allowlisted digests, kind, time, and opaque references.

## Adversarial coverage

The independent Python cases verify:

- exact original JSON scalar handling for schema version, `false`, and null;
  missing and extra nested request fields fail closed;
- constructed semantic-state changes cannot be promoted into a challenge;
- cross-kind receipt substitution, a valid non-plan source digest, expiry, and
  replay create no additional durable claim;
- an accepted GA4 claim exports only the closed projection.

The native cases exercise production decoding and review with fully rebuilt
canonical action/plan content. They reject a rehashed `ONCE_PER_SESSION`
request and a rehashed divergent Boolean-field binding, reject malformed raw
values, and verify the native review/receipt surfaces and millisecond receipt
time format.

## Limits

Native approval establishes the exact reviewed plan/receipt binding; it does
not authenticate the keyed GA4 inventory marker, prove a current remote
permission, or perform a provider mutation. The configured guarded execution
path rechecks those facts before CREATE. A native review result is a
point-in-time local display, not a reusable authorization or cross-process
revocation barrier.
