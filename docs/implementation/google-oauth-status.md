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
| `requested_scopes`, `granted_scopes` | What enrollment asked for and what Google returned |
| `unrequested_granted_scopes` | Permissions Google echoed that this enrollment never requested — reported, never recorded as local authority |
| `access_token_expires_at`, `access_token_expired` | Local expiry tracking for the stored bearer |
| `refresh_token_expires_at`, `refresh_token_expired` | Local expiry tracking for the stored refresh token, or null when Google declared none |
| `identity_last_verified_at` | Always `null` — see below |
| `identity_last_verified_not_after` | The proven upper bound on the last verification |
| `identity_last_verified_status` | `NOT_RETAINED_BOUNDED_BY_ACCESS_TOKEN_EXPIRY` |
| `identity_reverified_on_refresh` | `true`: the refresh path re-verifies the same subject before any token is returned |
| `primary_slot_present` | Whether the protected OAuth bundle slot holds a value |
| `cloud_project_access_level` | Always `NOT_OBSERVABLE` |
| `revocation_guidance` | Provider-side revocation and local-only removal, described separately |
| `provider_authentication_verified`, `remote_revocation_verified`, `resource_access_verified`, `execution_authority` | Always `false` |

## No developer-token slot

The published 0.1.0 also reported `developer_token_slot_required`,
`developer_token_slot_state`, `developer_token_slot_present` and
`developer_token_slot_next_action` for Google Ads profiles, and a store failure
for that slot set the envelope outcome to `INCOMPLETE_COVERAGE`. Google retired
developer tokens on 2026-09-09 (<https://developers.google.com/google-ads/api/docs/api-policy/developer-token>,
fetched 2026-10-12, page last updated 2026-10-07), so AdHelm holds no such slot,
and the fields, the state and the outcome no longer exist. A profile
written by 0.1.0 carries `secondary_credential_ref` (null, or the name of a
developer-token entry); it is dropped on load and never read or written back. Because
the profile digest changed, a Google grant sealed by 0.1.0 fails its binding with
`GOOGLE_OAUTH_PROFILE_BINDING_STALE` (exit 4), whose next action is `auth google-oauth
reconnect` for that profile; `status` shows such a grant as `UNVERIFIED`.

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
or raw Google subject appears in the diagnostic.
`tests/test_google_oauth_status.py` pins both the exit code and the absence of
every one of those values.

## Revocation guidance

Local removal is never described as revocation. The guidance is chosen from the
profile's provider by `google_oauth_revocation_guidance`.

- **Provider-side.** Google Account → Security → *Your connections to
  third-party apps & services*, open the enrolled app, then remove its access.
  Reference: <https://support.google.com/accounts/answer/13533235> (checked
  2026-09-09).
- **Provider-side, project access (Google Ads only).** Google Ads API access
  belongs to the Google Cloud project that owns the OAuth client, not to this
  grant. Removing the OAuth grant leaves the project's access level in place,
  and the level is managed on the project's Google Ads API Overview page. GA4 and
  GTM profiles are not told about it.
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

## Cloud project access level

Google publishes Test, Explorer, Basic and Standard access levels for the Cloud
project that owns the OAuth client, and the level constrains both the accounts
the project may reach and its daily operation allowance. **No reviewed Google
Ads v25 read returns the level in use.** The status result therefore always
reports `NOT_OBSERVABLE`, and `GoogleAdsIdentity.cloud_project_access_level`
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
  Cloud project access level is a documentation reading, not a live observation.
