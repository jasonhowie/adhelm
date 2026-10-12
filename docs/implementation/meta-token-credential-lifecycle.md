# Meta token credential lifecycle

This lifecycle library connects the reviewed fixed Meta `debug_token`
inspection boundary to the existing local configuration and approved
operating-system credential-store boundary. Its later
[CLI integration](meta-token-cli.md) provides explicit hidden-token handoff,
local status and fixed inspection. Dedicated
[read workflows](meta-token-read-workflows.md) compose it with account identity,
inventory and aggregate reporting. The lifecycle itself makes no token exchange,
refresh, account-access or mutation-authority claim.

## Closed customer declaration

`Configuration.profiles` accepts a third closed shape,
`MetaAccountTokenProfile`, alongside the existing scoped `Profile` and
OpenAI account-key profile. Its exact integer `schema_version` is `1` and its
fixed fields are:

| Field | Meaning |
| --- | --- |
| `profile_kind` | `META_ADS_ACCOUNT_TOKEN` |
| `provider` | `meta_ads` |
| `profile_id` | local nonsecret profile selector |
| `account_ref` | customer-declared native numeric ad-account identifier |
| `client_application_ref` | customer-declared native numeric app identifier |
| `currency` | customer-declared three-letter account-currency expectation |
| `timezone` | customer-declared IANA account-timezone expectation |
| `purpose` | `READ` or `EDIT` local intent |
| `inspector_credential_kind` | customer app-access token or customer app-developer user-token declaration |
| `credential_source` | fixed `OS_META_TOKEN` |
| `credential_ref` | fixed domain-separated derivation from `profile_id` |

The profile contains no subject, principal, granted-scope observation, expiry,
raw token, app secret, or proof of account access. Currency and timezone are
required customer declarations included in the profile digest and therefore in
the private bundle binding; they are not current-account facts. The dedicated
read session compares them with its fixed account identity read before using
inventory or report results.
`READ` derives only `ads_read`; `EDIT` derives only `ads_management`. The
customer declaration of either field does not prove Meta granted that
permission. The fixed inspector later requires a current expiring `USER`
subject with the derived scope and the declared app before it returns its
separate redacted observation.

The derived reference is reserved across generic `Profile`, OpenAI key, and
Google OAuth setup validators. Generic `credential()` and generic auth-slot
setup reject `OS_META_TOKEN` before opening a credential backend. Inventory and
reporting now use the dedicated Meta read session for READ profiles, including
configuration and bundle continuity through retention. OpenAI measurement
continues to reject this unrelated profile. EDIT workflows and CI environment
credentials are not enabled by this integration.

## One private bundle

The only stored secret value is one canonical JSON bundle in the fixed service
`adhelm.meta-token-v1`. It contains the subject token, the
customer app/developer inspector token, a fresh local binding key, the exact
profile ID, and the profile digest. It is private implementation state: it is
not configuration, a credential result, a status result, a ledger record,
artifact, exception detail, or generic bearer value.

Enrollment and explicit rotation first create an opaque process-local install
slot. A domain-separated HMAC binds the selected derived store slot and the
raw value seen before hidden token collection. Configuration is locked before
the bundle slot. Before writing, the lifecycle re-reads the expected
configuration and bundle; after one complete-bundle `set_password`, it reads
the exact bytes back. Both tokens and the local binding key are therefore
replaced together. It never writes one token independently of the other.

New enrollment requires an absent profile and absent bundle. Rotation requires
an existing, canonical bundle bound to the exact current profile. Missing,
malformed, profile-mismatched, changed, failed-write, or failed-readback state
does not trigger deletion, rollback, automatic retry, token refresh, or a
partial repair. Once a configuration or bundle write has begun and cannot be
proved by readback, the result is the fixed
`META_TOKEN_CREDENTIAL_TRANSACTION_UNVERIFIED` ambiguity outcome; local status
can later report only `READY`, `MISSING`, or `UNVERIFIED`.

Local removal is a separate, explicit command, `auth meta-token remove-local`
(see [Meta CLI](meta-token-cli.md)). It never decodes the bundle, deletes only
the selected profile's derived slot under the slot's credential lock, and proves
the deletion by readback; a delete the store reports but the readback
contradicts is the same `META_TOKEN_CREDENTIAL_TRANSACTION_UNVERIFIED`
ambiguity. It is not revocation and sends no request to Meta.

The process-local HMAC snapshot coordinates this library handoff and detects
cooperating concurrent writes. It is not a claim of cross-process transaction
atomicity or provider credential validity.

## Inspection handoff

`inspect_configured_meta_token(directory, profile_id)` captures the complete
configuration digest and exact Meta profile facts, then gives the reviewed
inspector a private supplier. Each supplier call rechecks the full
configuration, profile digest, derived app ID, and derived scope before it
reads and canonical-decodes the bundle. The inspector takes its two snapshots
through that same resolver, so local profile/bundle changes cannot bridge one
observation.

The resulting observation can establish only the fixed inspector's app,
pseudonymous user identity, observed scope, and expiring-USER-token facts. It
does not establish ad-account access, app review, remote mutation authority,
or an authorization grant. No test opens an actual OS credential store or
contacts Meta; tests use fake stores, fake suppliers, and a fake inspector
handoff.
