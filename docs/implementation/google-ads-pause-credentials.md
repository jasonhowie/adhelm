# Google Ads PAUSE protected credentials

M56 adds the private credential-continuity boundary used by a later configured
Google Ads campaign PAUSE planner.  It lives in
`adhelm.google_oauth_credentials` and does not add a public credential API,
provider request, plan, ledger record, mutation, or CLI route.

The root-owned reserved credential reference
`google-ads-pause-credential-binding-v1` selects one fixed 32-byte hex key in
the ordinary local credential-store service.  It cannot be configured as a
profile credential reference.  It is separate from the OAuth bundle service.
The two slots are always locked in casefold-sorted order:

1. the configured Google OAuth bundle reference; and
2. the fixed PAUSE binding-key reference.

Google retired developer tokens on 2026-09-09, so there is no third slot (AdHelm 0.1.0
locked a developer-token reference as well).

The private initial helper is:

```python
_prepare_google_ads_pause_protected_snapshot(profile, configuration_digest)
```

It is called while the caller owns its configuration transaction.  It validates
the exact configured Google Ads `EDIT` OAuth profile, may use the existing
refresh-capable bundle resolver and creates the
fixed key only when absent with exact readback.  It returns a module-issued,
immutable, nonserializable `_GoogleAdsPauseProtectedSnapshot`.  The snapshot
has only the detached nonsecret M55 execution facts needed by the sealed
planning facade, plus a private bearer for its fixed header construction.

`credential_binding_ref` is a `credential-binding:sha256:` HMAC.  Its
domain-separated canonical input includes the exact configuration/profile,
target customer, optional login customer, declared currency/timezone, client,
derived principal, exact `adwords` scope, configured slot association,
and the complete canonical protected OAuth bundle.  The
bundle contribution includes refresh material, client secret, lifecycle state,
and expiry provenance privately.  The key is the HMAC key, so key rotation also
changes the resulting reference.  The ref deliberately excludes only the
volatile local `resolved_at` value and the M54 random per-session marker.
Neither raw value, a slot reference, nor a bundle representation enters M55
action data, history, output, or diagnostics.

Later checks use:

```python
_current_google_ads_pause_protected_snapshot(profile, configuration_digest, expected_binding)
```

and:

```python
with _google_ads_pause_retention_recheck(
    profile, configuration_digest, expected_binding
) as recheck:
    # retain one exact plan with _before_durable_append=recheck
    ...
```

Both validate the expected module-issued snapshot before any protected-store
access, then reread all three slots under the same lock order.  They use a
separate no-refresh locked bundle reader: it requires `READY`, a current
refresh-token window when one is finite, a finite unexpired bearer, and a
non-rolling-back trusted clock.  It never dispatches OAuth, changes a bundle
state, creates a missing key, or repairs local state.  The fresh protected HMAC
and all detached profile/configuration/account/manager facts must equal the
expected association.  A fresh `resolved_at` may advance, but cannot precede
the original snapshot; it is not part of the stable HMAC.  A second sample that
reaches bearer expiry is credential drift; only a second sample earlier than
the first is clock rollback.

The retention context holds the three credential locks only after its caller
has opened the configuration transaction.  Its owner-thread callback rereads
all three slots through the already-held backend and service handles, then
recomputes the protected HMAC and checks the clock.  It performs no new backend
acquisition, ledger I/O, provider network operation, OAuth refresh, protected
store write, or lock reentry.  At least one callback is required; escaped,
cross-thread, post-context, or zero-use callbacks fail closed.  The future
planner uses it immediately as the ledger's `before_durable_append` callback,
so the ledger stream lock never contains new credential coordination or network
work while still detecting local slot drift immediately before append.
The held context advances its expected timestamp after every successful reread;
a later callback cannot roll back to a time above the original snapshot but
below a previously accepted sample.

The module uses fixed diagnostics for malformed snapshots, unavailable binding
keys, credential drift/expiry, retention misuse, and clock rollback.  At the
frozen M54 fixed-reader callback, the planning facade translates only safe
PAUSE drift/expiry diagnostics into the existing EDIT reader vocabulary; the
PAUSE helper codes remain intact outside that callback.

This is a local association proof.  It does not prove provider permission,
manager access, live mutation behaviour, acknowledgement semantics,
idempotency, CAS, delivery, spend, or execution authority.
