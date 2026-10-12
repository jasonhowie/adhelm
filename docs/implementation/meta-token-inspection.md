# Private Meta token inspection — M37 development

`meta_token_inspection.py` implements the fixed Graph v26.0 `debug_token` read
identified in [the provider reference](../research/meta-campaign-pause-contract.md).
It is a prerequisite for verified OAuth identity in future Meta mutation
workflows. It is not connected to configuration, the OS store, OAuth enrollment,
the CLI, account inventory, authorization or provider writes.

The caller is an internal configured integration that must own a private
credential supplier. The function constructs its own fixed-host HTTP client
with environment proxy/auth settings disabled. The inspector token is sent in
the authorization header; the subject token is sent in the provider-required
`input_token` parameter. The generic report transport and its privacy checks
are unchanged. There is one request, no redirects and no retry. Response bytes
are bounded to 64 KiB. A monotonic 30-second observation deadline is checked
before dispatch, after every body chunk and at completion; an already blocked
socket read has its separate at-most-30-second transport timeout. A late result
cannot become an accepted observation.

The closed response requires validity, an explicit USER token type, the exact
expected application, a numeric user identifier, the requested advertising
permission and future finite token/data-access expirations. This initial
protocol explicitly rejects nonexpiring or unknown token types. Actual granted
scopes are retained as a sorted unique tuple. A domain-separated digest replaces
the raw user identifier, and a separate private HMAC binds the subject credential
and profile. The full credential snapshot is re-read after inspection and must
match. UTC rollback or an expired observation fails closed. This observation
does not prove account access, required app review, authorization to mutate or
a native direct grant; future account operations must independently verify those.

No raw response, token, application name or token-bearing URL is returned or
persisted. Errors use a closed code/message set and suppress provider/supplier
exception detail in the public traceback. The independent review caught and
repaired an overly broad error-code prefix rule and missing token-type admission.
Tests use synthetic credentials and mock HTTP only; no real credential or Meta
request was used. Live endpoint behavior, credential lifecycle and configured
execution remain unverified.
