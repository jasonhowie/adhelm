# Google OAuth browser boundary: independent ultra review

## Scope

This review covers the bounded local browser handoff and loopback callback in
`src/adhelm/google_oauth_browser.py`, together with the narrow protocol handoff
that gives it an authorization attempt. It does not perform OAuth enrollment,
open a browser, bind a real socket, call a provider, access a credential store,
or read an installed client file.

The independent cases use in-memory listener, connection, clock, randomness,
and launcher fakes only.

## Findings resolved during review

| Finding | Resolution verified |
| --- | --- |
| A raw Google authorization URL allowed a caller to select arbitrary query fields while retaining the fixed host. | `open_system_browser` now accepts only `GoogleOAuthAuthorizationAttempt`; `google_oauth_authorization_url` reconstructs, revalidates, checks expiry, and builds the sole exact URL before the launcher is reachable. |
| Public `now` arguments allowed a caller to backdate an expired authorization attempt. | Public protocol and browser APIs no longer accept caller time. The module-private `_utc_now` seam is used only by local tests. |
| Nonfinite, Boolean, raising, or precision-rounded monotonic clocks could reach listener setup or leak a raw exception. | Clock samples are exact finite numbers; the computed deadline must be finite and strictly later than the start before a listener is created. Clock failures use fixed diagnostics. |
| A raw trailing `#` could pass an empty-fragment check. | Authorization and callback targets reject a raw fragment marker. |
| The callback could become overdue while the static HTTP reply was sent, then return a code without a final deadline check. | The receiver samples the bounded monotonic clock after replying and before returning the raw query. |
| Callback-path randomness failed outside the sanitized listener boundary. | Randomness is generated and checked as an exact 32-character URL-safe nonce before socket creation; malformed or raising sources return a fixed error. |

## Verified behavior

- A forged, malformed, or expired typed attempt cannot reach the fake OS
  launcher. The launcher receives the exact fixed macOS executable and argv,
  with `shell=False`, a fixed `PATH`, and no ambient `BROWSER` selection.
- The callback listener is IPv4 loopback only, chooses a kernel port, uses an
  unguessable fixed-length path, closes before any token exchange, and is
  single-use.
- The receiver bounds total lifetime, individual connections, header bytes,
  request count, and request-target length. It accepts only origin-form
  `GET /path?... HTTP/1.1` with the exact Host; bodies, transfer encoding,
  `Origin`, duplicate headers, alternate peers, and fragments are rejected.
- Callback responses are static, have no-store/referrer/CSP protections, and
  do not reflect the authorization code, state, or raw request data.
- HTTP acceptance is deliberately not OAuth acceptance. A syntactically valid
  callback with duplicate `state` receives the same static page but is rejected
  by the subsequent strict protocol parser; it cannot establish authority.

The loopback handoff is only local transport. State, authorization code, PKCE,
nonce-bound OIDC verification, and protected credential installation remain
separate later checks. A successful launcher return is not an authorization or
identity claim.

The fixed launcher still passes the authorization URL to an OS launcher as an
argument. This slice makes no secrecy guarantee against a compromised desktop
or another sufficiently privileged same-user process that can inspect process
arguments; it avoids ambient browser selection and arbitrary shell execution.

## Validation

```text
PYTHONPATH=src:tests .venv/bin/pytest -q tests/test_google_oauth_browser_ultra_review.py
15 passed

PYTHONPATH=src:tests .venv/bin/pytest -q \
  tests/test_google_oauth_browser_ultra_review.py \
  tests/test_google_oauth_protocol.py \
  tests/test_google_oauth_setup.py
54 passed

PYTHONPATH=src:tests .venv/bin/ruff check \
  src/adhelm/google_oauth_browser.py tests/test_google_oauth_browser_ultra_review.py
PYTHONPATH=src:tests .venv/bin/ruff format --check \
  src/adhelm/google_oauth_browser.py tests/test_google_oauth_browser_ultra_review.py
```

No real socket, browser, process, network, keychain, provider, or credential
operation was used by these checks.
