# Meta Ads token READ session: independent review

This review exercised the private configured Meta READ session and its
inventory/report composition with synthetic token text, an in-memory protected
store, tracked local lock seams, and an in-memory HTTP transport. It did not
open an operating-system credential store, prompt a user, contact Meta, or
write a provider resource.

## Reviewed boundary

`tests/test_meta_ads_token_read_session_ultra_review.py` adds fifteen
fake-only regressions. The focused session, integration, and independent group
passes 44 tests with the frozen source.

The review verifies that:

- a malformed composition digest stops before profile preparation, inspection,
  account discovery, or a provider read;
- the inspector token is used only by the fixed `debug_token` inspection and
  every Meta Ads account request receives only the subject token;
- a post-inspection full-bundle replacement stops before account identity
  discovery, and configuration drift between fixed inventory edges stops the
  next edge before it is sent;
- every fixed provider GET obtains the subject token through the guarded
  configuration, canonical-bundle, continuity-marker, and expiry check;
- remote account ID, currency, and timezone must match the declared Meta READ
  profile before inventory or report reads can proceed;
- the retention context holds configuration state before the credential slot,
  supports repeated callbacks only on its owning thread, and rejects escaped
  or cross-thread callbacks before local state verification;
- provider reads cannot overlap the retention window, and the retention
  callback performs no provider request, ledger read, or additional lock
  acquisition;
- inventory invokes the callback once before an ephemeral return and once
  before each durable append; report invokes it once before an ephemeral
  return and once before its durable observation append;
- canonical Meta report provenance binds the discovered canonical account and
  declared currency; and
- report artifact projection happens only after the session and local
  configuration/credential locks have closed.

## Scope limits

The customer-declared Meta account ID, currency, and timezone are checked
against the fixed account identity response for this session. They are not
evidence of app review, provider-side account authorization beyond that
response, mutation permission, or an execution grant.

This review does not provide a generic credential route, a CLI token export,
an account mutation path, token refresh or exchange, live Keychain evidence,
or live Meta evidence. The session remains a private composition boundary for
the fixed READ inventory and aggregate-report workflows.
