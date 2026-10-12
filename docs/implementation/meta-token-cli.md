# Meta token CLI

`auth meta-token` exposes the reviewed local Meta token lifecycle and fixed
token-inspection boundary through five typed commands:

| Command | Input and effect |
| --- | --- |
| `auth meta-token enroll --input DRAFT --interactive` | Validates the complete nonsecret `meta-token-profile-draft`, then collects two tokens through hidden local-terminal prompts and attempts to add the profile and one complete protected bundle with exact readback. |
| `auth meta-token rotate --profile PROFILE --interactive` | Replaces the existing exact protected bundle through two hidden local-terminal prompts. It never refreshes or exchanges a token. |
| `auth meta-token status --profile PROFILE` | Returns the bounded local bundle state, the retained inspection facts for the bundle installed **now**, the purpose-clamped scope grant, the revocation boundary, and the app-review/Business-Verification disclosure. It does not prompt, construct an HTTP client, or contact Meta. |
| `auth meta-token inspect --profile PROFILE` | Performs the one fixed `debug_token` metadata read and returns only a redacted expected-app, `USER`, required-scope, additional-scope-count, and expiry summary. |
| `auth meta-token remove-local --profile PROFILE` | Deletes that profile's protected bundle and its retained inspection record, proves the bundle is gone by readback, and reports what it found. It does not prompt, construct an HTTP client, contact Meta, or revoke anything. |

Enrollment parses its bounded regular JSON input before the interaction gate.
Both secret-taking commands require exact `--interactive` and an actual stdin
and stderr TTY before they prepare a credential slot or access the credential
store. The first hidden prompt is labelled `Meta USER token:` and the second
`Meta inspector token:` through the existing no-echo `getpass` boundary; no
token is accepted through a command flag, JSON field, local configuration,
environment variable, CI setting, browser handoff, helper, or generic
credential command.

The lifecycle first prepares an opaque configuration/store snapshot. It then
collects both tokens without holding a configuration or credential lock and
installs the complete private bundle with the existing exact readback checks.
Changed, missing, malformed, and uncertain local state keep the lifecycle's
fixed diagnostics, including `META_TOKEN_CREDENTIAL_TRANSACTION_UNVERIFIED`;
the CLI does not delete, roll back, refresh, or retry a bundle.

Every command proves its largest complete result envelope fits the configured
output bound before any prompt, protected-store access, fixed inspection HTTP
request, or local configuration write. Enrollment and rotation project only
local state, provider/purpose, a configuration digest, and zero authority
facts. Inspection intentionally omits profile identifiers, account and app
identifiers, user/principal references, token values, credential bindings, raw
scope strings, and provider response text. It distinguishes the declared local
purpose from the current fixed required-scope observation through a bounded
count of additional granted scopes. Inspection itself does not prove account
access, Meta app review, a mutation permission, or execution authority.

## What `status` retains and reports

`inspect` performs the one reviewed `debug_token` read and then retains only
nonsecret facts from it: the two documented expiry timestamps, the observation
time, how many scopes the token carried, and the granted scopes **clamped to
the profile purpose's closed allowlist**. A `READ` profile's record therefore
never contains `ads_management` even when the customer's token carries it; the
extra scopes are counted and never named. No token, inspector token, binding
key, principal, or application identifier is written.

`status` makes no provider request at all, so everything it reports about the
token is a retained local fact:

- `token_validity` is `VALID_AT_LAST_INSPECTION` or `NOT_INSPECTED_LOCALLY`,
  never a current provider claim. `expires_at`, `data_access_expires_at`,
  `purpose_clamped_scopes`, and `granted_scope_count` are `null` or empty
  whenever the installed token has not been inspected here.
- A retained record is answered **only** when it was produced by inspecting the
  bundle that is installed now. It carries the inspection's own
  `credential_binding_ref` — an HMAC over that bundle's private key and its
  exact subject token — and `auth meta-token rotate` replaces both, so a
  rotated bundle reads as never inspected rather than inheriting the replaced
  token's expiry and grant. That reference is an internal comparison value and
  is never part of a status result.
- `retained_inspection` says which of those applies:
  `MATCHES_INSTALLED_BUNDLE`, `NOT_RETAINED`, or `UNREADABLE`. An unreadable
  retained file leaves a declared fact unread, so the envelope outcome is
  `INCOMPLETE_COVERAGE` rather than a clean `OK`.
- A retained expiry that has already passed makes `status` fail with
  `META_TOKEN_EXPIRED` at exit code 4 rather than report a token that might
  still work.
- `revocation_guidance` carries the guidance itself, not a code for it:
  removing the local bundle or the retained record stops this product from
  using the token and is **not** revocation. Only Meta can revoke one, and this
  product sends no revocation request.
- `app_review_status` and `business_verification_status` are both
  `NOT_DOCUMENTED_LOCALLY`. No command in this build reads Meta's app-review
  outcome, Standard-versus-Advanced access tier, or Business Verification
  state, so no local status asserts any of them.

The fixed inspection supports expiring user tokens only. A zero or unbounded
expiry — as a long-lived system-user token reports — is refused as
`META_TOKEN_EXPIRY_UNSUPPORTED`, and that refusal says the expiry semantics are
undocumented for this protocol rather than that identity could not be verified.

A separately invoked configured `READ` profile can support the fixed
`inventory meta_ads PROFILE` and `report meta-ads PROFILE --input QUERY` read
workflows. Those workflows are development/offline validated and make their
own inspector, exact account-identity, and credential-continuity checks. The
auth commands do not stand in for those checks, and the read workflows do not
prove Meta app review, write or activation permission, or execution authority.

The CLI binds inspection to its preflight configuration digest before the
workflow can obtain credentials or issue the fixed metadata GET. A retargeted
configuration fails with `CONFIGURATION_CHANGED` before that provider request.

## What `remove-local` deletes

`remove-local` is the Meta profile's local-removal command; the generic
`auth remove-local` still refuses a Meta profile with
`META_TOKEN_CREDENTIAL_LIFECYCLE_REQUIRED` and now names this command. It
deletes exactly two things, both for the selected profile only:

- the `adhelm.meta-token-v1` credential-store entry whose account name is the
  profile's derived `meta-token-bundle-v1.` selector, and
- that profile's record in `meta-token-observations.json`. Every other
  profile's record is written back unchanged, under the same observations-file
  lock that `inspect` takes when it retains a record, so the guarantee holds
  against every AdHelm writer of that file, not against a manual edit.

The bundle is never decoded. It is checked for presence, deleted under the
slot's credential lock, and read back; an entry that is still there is the
fixed `META_TOKEN_CREDENTIAL_TRANSACTION_UNVERIFIED` ambiguity outcome
(exit code 6), whatever the store's `delete` reported, and the retained
inspection is left alone. Because nothing is decoded, a malformed bundle or one
bound to a changed profile is removed like any other.

The result states `operation: LOCAL_REMOVE_ONLY`, `local_state: MISSING`,
`remote_revocation_verified: false`, and the same revocation guidance `status`
carries. `bundle_removed` and `retained_inspection_removed` are `false` when
there was nothing of that kind to delete, so a repeated removal, or a profile
that was never inspected, is not reported as a deletion. A missing bundle with
a retained inspection still clears the inspection.

If the bundle is removed but the shared inspection file cannot be read or
rewritten, the command fails with `META_TOKEN_INSPECTION_REMOVAL_UNVERIFIED`
(exit code 6). The credential is gone; the file is not rewritten, because
rewriting an unreadable file would drop the other profiles' records, and the
stale record cannot match any bundle.

If another AdHelm command holds the observations-file lock, the bundle is still
removed and the record is not, and the command fails with
`META_TOKEN_INSPECTION_REMOVAL_BUSY` (exit code 6). Its `next_action` names
`adhelm auth meta-token remove-local --profile <id>` as the recovery: run it
again once the other command has finished. The rerun reports
`bundle_removed: false` and `retained_inspection_removed: true`. Deleting
`meta-token-observations.json` is the advice for the unreadable case only.

`inspect` takes the same lock when it retains its result. If the lock is held,
the `debug_token` call has already completed, nothing is retained, and the
command fails with `META_TOKEN_OBSERVATIONS_BUSY` (exit code 3). The
`next_action` says to rerun `inspect`; `status` does not show that inspection
until a rerun retains it. The lock is nonblocking and is held only for the
file's read and rewrite, so the refused command fails at once and a rerun
shortly afterwards succeeds. A busy lock is never reported as a credential
setup (`CREDENTIAL_SETUP_BUSY`), because none is running.

The profile stays in the configuration. Because `rotate` requires an existing
bundle and `enroll` requires an absent profile, the removed profile ID cannot be
installed again; a replacement enrolls under a new `profile_id`.

`discover` lists all five commands and the `meta-token-profile-draft` schema.
Discovery and schema inspection do not read a profile, prompt, access a
credential store, or contact Meta.
