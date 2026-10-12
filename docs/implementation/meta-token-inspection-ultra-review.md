# Meta token inspection independent review

This review exercised only `meta_token_inspection` with synthetic credentials,
fake clocks, and an in-memory fake HTTP client. It made no Meta request and
did not create a credential, configuration, OS-store entry, account read, or
mutation.

The review reproduced and the source now rejects these boundaries:

- an untrusted `AdhelmError` whose code starts with `META_TOKEN_` can no longer
  make its caller-controlled suffix public; the normalizer permits only its
  closed internal code set;
- a response must explicitly state `type: USER` before a principal or scope
  observation is derived;
- `required_scope` must be an exact string, so a string subclass or an object
  with matching equality and hash cannot manufacture a fixed scope input;
- the monotonic thirty-second deadline covers the credential recheck,
  final UTC sample, response parsing, and return boundary, rather than ending
  at response EOF; and
- a token that expires after the HTTP read but before the final current-time
  sample cannot be returned as current.

The independent fake transport also verifies the one fixed `GET` target,
query-only inspected token, authorization header, no request body, disabled
redirects, `trust_env=False`, and the thirty-second timeout. Oversized or
duplicate-key JSON responses and a supplier error containing a synthetic
secret return only bounded public errors; the secret is absent from their
codes and formatted tracebacks. The deterministic observation test checks the
domain-separated principal reference and HMAC credential-binding reference
without exposing the source user or tokens.

`tests/test_meta_token_inspection.py` plus
`tests/test_meta_token_inspection_ultra_review.py` passed 32 tests. Scoped
Ruff, Ruff format, and mypy for `src/adhelm/meta_token_inspection.py` also
passed.

The boundary remains a private prerequisite only. It does not prove an account
relationship, a target-specific grant, app review, mutation authority, current
provider state, or a configured Meta workflow.
