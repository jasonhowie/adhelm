# `auth google-oauth status`: bounded local Google credential state

Implemented: 2026-09-09. `src/adhelm/google_oauth_cli.py`,
`src/adhelm/google_oauth_workflow.py` and
`src/adhelm/google_oauth_credentials.py`, beside the existing
[`google-oauth-onboarding.md`](google-oauth-onboarding.md) enrollment and
reconnect handoff and the
[credential lifecycle](google-oauth-credential-lifecycle.md).

```
adhelm auth google-oauth status --profile <profile-id>
```

The command **contacts no Google endpoint**, reads no advertising resource,
appends no ledger record and writes nothing. It reads one protected local
bundle under its own credential reference lock and releases that lock before
returning, so no lock is ever held across a network call — this path makes
none. It mirrors the LinkedIn lifecycle shape in
[`linkedin-audit-recommendations.md`](linkedin-audit-recommendations.md)'s
sibling `auth linkedin-token status`.

## What it reports

| Field | Meaning |
| --- | --- |
| `profile_configuration_digest` | The exact configured profile this status describes |
| `provider`, `purpose` | The configured Google surface and read/edit purpose |
| `local_state` | `READY`, `REFRESH_STARTED`, `RECONNECT_REQUIRED`, `MISSING`, or `UNVERIFIED` |
| `identity_pseudonym` | The derived `google_oidc_principal_v1.<digest>` reference; the raw Google subject never leaves the sealed bundle |
| `developer_token_slot_state` | `PRESENT`, `ABSENT`, `UNKNOWN_STORE_FAILURE`, or `NOT_REQUIRED` |
| `developer_token_slot_next_action` | The actionable step for `ABSENT` and `UNKNOWN_STORE_FAILURE`; null for a settled state that needs none |
| `requested_scopes`, `granted_scopes` | What enrollment asked for and what Google returned |
| `unrequested_granted_scopes` | Permissions Google echoed that this enrollment never requested — reported, never recorded as local authority |
| `access_token_expires_at`, `access_token_expired` | Local expiry tracking for the stored bearer |
| `refresh_token_expires_at`, `refresh_token_expired` | Local expiry tracking for the stored refresh token, or null when Google declared none |
| `identity_last_verified_at` | Always `null` — see below |
| `identity_last_verified_not_after` | The proven upper bound on the last verification |
| `identity_last_verified_status` | `NOT_RETAINED_BOUNDED_BY_ACCESS_TOKEN_EXPIRY` |
| `identity_reverified_on_refresh` | `true`: the refresh path re-verifies the same subject before any token is returned |
| `primary_slot_present` | Whether the protected OAuth bundle slot holds a value |
| `developer_token_slot_required`, `developer_token_slot_present` | Whether this profile needs a separate Google Ads developer token, and whether that slot holds a value. The value itself is never read out. `developer_token_slot_present` is `null` whenever the question was not settled — see below |
| `developer_token_access_level` | Always `NOT_DOCUMENTED_LOCALLY` |
| `revocation_guidance` | Provider-side revocation and local-only removal, described separately |
| `provider_authentication_verified`, `remote_revocation_verified`, `resource_access_verified`, `execution_authority` | Always `false` |

## A credential store that cannot answer is not an absent token

An OS credential backend can fail for one entry and answer for another: a
locked collection, a dropped D-Bus session, a Keychain prompt the user
dismissed. None of that means the developer token is missing, and reporting it
as absent would be indistinguishable from an enrolment that never stored one —
two different facts with two different next actions.

`developer_token_slot_state` is therefore a four-way typed value:

| State | Meaning | `developer_token_slot_present` |
| --- | --- | --- |
| `PRESENT` | The slot holds a value. The value itself is never read out | `true` |
| `ABSENT` | The profile declares a secondary reference and the store answered that nothing is stored, or the profile declares no reference at all | `false` |
| `UNKNOWN_STORE_FAILURE` | The credential store did not answer | `null` |
| `NOT_REQUIRED` | This is not a Google Ads profile | `null` |

`UNKNOWN_STORE_FAILURE` additionally sets the envelope outcome to
`INCOMPLETE_COVERAGE` rather than `OK`, and
`developer_token_slot_next_action` names the step that would settle it. A
status read never repairs, retries, or removes the slot.

## The identity pseudonym is deterministic, not a random alias

`identity_pseudonym` is `google_oidc_principal_v1.<sha256>` derived from the
canonical Google OIDC issuer and the raw subject by
`derive_google_oidc_principal_ref`. It is **unsalted and deterministic**: the
same Google account always produces the same reference on every machine, which
is exactly what makes it usable to bind a grant to a configured profile and to
detect a subject change on refresh.

The consequence is that it is *confirmable*, not anonymous. Anyone who already
holds a candidate Google subject identifier can hash it and check whether it
matches. It hides the raw subject from an agent reading this output; it does
not defend against an adversary testing a guess. Treat it as a stable
account-scoped reference, not as a privacy control.

## Why there is no "last verified" timestamp

The protected bundle schema retains no successful-verification instant, and
inventing one would be a claim rather than a reading. What *is* proven is the
bound: the currently stored access token was issued by an authorization or a
refresh that re-verified this exact Google subject, and that token expires at
the recorded instant, so the verification happened at some point before it.
`identity_last_verified_at` is therefore always `null`,
`identity_last_verified_not_after` carries the access-token expiry, and
`identity_last_verified_status` names the substitution explicitly.

Recording a real verification instant would require a bundle schema change and
its own review. That is deliberately **not** done here: the bundle encoding is
also read by the guarded Google Ads pause credential binding, and a silent
schema bump would change what that binding hashes.

## Refresh re-verifies identity

`resolve_google_oauth_access_token` refreshes only inside the near-expiry
window, marks the bundle `REFRESH_STARTED` and proves that marker through
readback before any token egress, and then calls
`refresh_google_oauth_grant` with the identity taken from the sealed bundle.
That protocol function verifies the returned identity token against that exact
expected identity before a refreshed bearer exists. A refresh that returns a
different subject, or that fails ambiguously, leaves the bundle
`RECONNECT_REQUIRED` and blocks automatic retry.

An **expired or revoked refresh token** therefore fails closed with
`GOOGLE_OAUTH_RECONNECT_REQUIRED` and **exit code 4**, before any token
endpoint is contacted, and no access token, refresh token, client secret,
developer token, or raw Google subject appears in the diagnostic.
`tests/test_google_oauth_status.py` pins both the exit code and the absence of
every one of those values.

## Revocation guidance

Local removal is never described as revocation. The guidance is chosen from the
profile's provider by `google_oauth_revocation_guidance`.

- **Provider-side.** Google Account → Security → *Your connections to
  third-party apps & services*, open the enrolled app, then remove its access.
  Reference: <https://support.google.com/accounts/answer/13533235> (checked
  2026-09-09).
- **Provider-side, separate credential (Google Ads only).** A Google Ads
  developer token is not part of this OAuth grant. It is managed in the API
  Center of the manager account that owns it, and removing the OAuth grant does
  not affect it. GA4 and GTM profiles have no developer token and are not told
  about one.
- **Provider-side, separate permission (GTM only).** Tag Manager account and
  container access is granted to the Google user in Admin → User management,
  not by this OAuth grant. Revoking the grant leaves those permissions in
  place, and removing them does not revoke the grant. Reference:
  <https://support.google.com/tagmanager/answer/6107011> ("Managing users and
  permissions", checked 2026-10-10). Tag Manager's project quota, whose refusal is
  also a `403`, is documented at
  <https://developers.google.com/tag-platform/tag-manager/api/v2/limits-quotas>
  (fetched 2026-10-10, page last updated 2026-07-16).
- **Local-only.** `adhelm auth remove-local --profile <id>` deletes the stored
  local credential entry for this profile.
- Local removal **never revokes the grant at Google** and is never evidence that
  it was revoked; removing a shared application's access can also interrupt
  other uses of it.

## Developer-token access level

Google publishes Test, Basic and Standard developer-token access levels, and
the level constrains both the environments a token may reach and its daily
operation allowance. **No reviewed Google Ads v25 read returns the level in
use.** The status result therefore always reports
`NOT_DOCUMENTED_LOCALLY`, and `GoogleAdsIdentity.developer_token_access_level`
carries the same value so that a future reviewed disclosure has exactly one
place to land. Nothing in this build infers a tier, a quota, or an environment
from it.

The adjacent fact that *is* documented is `customer.test_account`, which the
fixed identity read retains. A customer that returns no flag is neither test
nor proven production.

## Evidence limits

- **Fixture-only.** `tests/test_google_oauth_status.py` is hermetic: the
  credential store is an in-memory fake, the token endpoint is never reached,
  and one case pins that the status route constructs no HTTP client at all. No live Google authorization, refresh, or revocation has been
  performed by this product.
- **Revocation is unverified.** The provider-side guidance is read from
  Google's published help page. This product has never observed a revoked
  grant's behaviour, so `remote_revocation_verified` is permanently `false`.
- **Access-level disclosure is unproven.** That no reviewed read returns the
  developer-token tier is a documentation reading, not a live observation.
