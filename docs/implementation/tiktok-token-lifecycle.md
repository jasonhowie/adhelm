# TikTok Ads app auth-code token lifecycle

Status: `FIXTURE_VERIFIED`. No live TikTok request was made for this work.

This mirrors [`linkedin-token-lifecycle.md`](linkedin-token-lifecycle.md) in
shape and differs from it in every place TikTok's documented contract differs.
The wire contract, and what could and could not be verified, is recorded in
[`../research/tiktok-oauth-contract.md`](../research/tiktok-oauth-contract.md).

## Why this is not the reusable OAuth exchange

`adhelm.oauth_enrollment` is the reusable confidential-client module. Its
non-wire seams are reused unchanged:

* `prompt_hidden_secret` for all three hidden prompts;
* `OAuthTransportError` and `OAuthTokenDispatchState` so a refusal carries only
  its dispatch classification and never a response body.

Its **wire** functions are not reused, and cannot be. `exchange_authorization_code`
builds an RFC 6749 `application/x-www-form-urlencoded` grant with `grant_type`,
`redirect_uri` and optional PKCE, and `_parse_token_response` parses an RFC 6749
token object. TikTok's exchange is:

```
POST https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/
Content-Type: application/json
{"app_id": "...", "secret": "...", "auth_code": "..."}
```

returning `{code, message, request_id, data}`. There is no `grant_type`, no
`redirect_uri`, no PKCE parameter, and no refresh grant. `OAuthProviderContract`
additionally requires a non-empty `supported_scopes` frozenset, and TikTok
publishes no scope vocabulary this build could fetch, so constructing one would
mean inventing a permission string.

`adhelm.tiktok_token_workflow.FixedTikTokTokenTransport` is therefore the TikTok
analogue of `FixedOAuthTokenTransport`: one fixed endpoint, one POST, no
redirects, no ambient environment, a bounded response, and a whole-response
elapsed budget. It is deliberately separate from `adhelm.transport.SafeHttpClient`,
which correctly rejects a body carrying `secret` and `auth_code` field names at
its generic privacy boundary.

## No browser handoff

TikTok shows each application its own authorization URL on the app detail page
in the My Apps console, and publishes no constructable template on any page this
build could fetch. This product therefore:

* never constructs, opens, or emits an authorization URL;
* runs no loopback listener and needs no registered redirect port;
* asks the operator to paste the **one-hour, single-use** authorization code
  into a hidden prompt.

`tests/test_tiktok_token_workflow.py` pins that the workflow module contains no
authorization-URL constructor, browser launcher, or loopback receiver at all.

## The four slots

| Slot | Service | Contents |
| --- | --- | --- |
| `ACCESS` | `adhelm` | the advertiser access token, in the profile's ordinary OS-store slot, so the shared `auth status` / `auth remove-local` contract keeps working |
| `APP_SECRET` | `adhelm.tiktok-token-v1` | the customer application's secret |
| `ADVERTISER_IDS` | `adhelm.tiktok-token-v1` | the granted advertiser ID list, written only when the exchange returned one |
| `STATE` | `adhelm.tiktok-token-v1` | the canonical non-secret lifecycle record |

The three protected selectors are derived by domain-separated SHA-256 over the
profile ID under the reserved `tiktok-token-v1#` prefix. The `#` separator
cannot occur in a configured `SafeRef`, so a derived selector can never collide
with a customer-chosen credential reference.

`install_tiktok_token_grant` writes a durable `ENROLL_STARTED` into the state
slot before any secret, so an interrupted install is reported as `UNVERIFIED`
rather than as a usable profile. No credential mutation lock is ever held across
the one HTTP call: the credential module opens and closes each locked section
and the network step happens between them. `tests/test_tiktok_token_workflow.py`
asserts that lock depth is zero at every recorded HTTP event.

## Commands

| Command | Behaviour |
| --- | --- |
| `auth tiktok-token enroll --profile <id> --environment PRODUCTION --interactive` | hidden prompts for app ID, app secret and the one-hour auth code; one fixed pinned POST; installs the four slots |
| `auth tiktok-token status --profile <id>` | bounded local state; contacts no provider; no secret, no raw advertiser ID |
| `auth tiktok-token refresh --profile <id>` | typed `TIKTOK_TOKEN_REFRESH_NOT_DOCUMENTED` refusal, exit 4, pointing at re-enrollment; performs no egress |
| `auth tiktok-token remove-local --profile <id>` | deletes only the three protected slots; never described as revocation |

`refresh` is still profile-scoped: an unconfigured profile is
`TIKTOK_TOKEN_PROFILE_REQUIRED` (exit 2) and a profile with no enrollment is
`TIKTOK_TOKEN_ENROLLMENT_REQUIRED` (exit 4), so the generic "no refresh" message
never hides a different problem.

## What is recorded, and what is refused

* **Permissions.** Recorded as the fixed literal `NOT_DOCUMENTED_LOCALLY`. TikTok
  selects endpoint permissions per app in My Apps and publishes no scope
  vocabulary. The state slot records only `scope_field_returned` — whether a
  permission field was present — and never its value. A TikTok profile's declared
  `scopes` are a local label and are never compared with or recorded as a
  provider permission.
* **App approval.** `app_approval_verified` is always `False` and app approval is
  reported as `NOT_DOCUMENTED_LOCALLY`.
* **Expiry.** Tracked only when the response declares a bounded whole-second
  lifetime, with `expiry_source="PROVIDER_DECLARED"`. Otherwise
  `expiry_source="NOT_DOCUMENTED_LOCALLY"` and `access_token_expired` is `None`,
  never `False`: a boolean there would report an undocumented lifetime as
  healthy. The SDK's "creator access token is valid for 24 hours" note is about
  the `/tt_user/...` creator token, not the advertiser token. Both lifetime field
  names (`expires_in`, `token_expires_in`) are `UNVERIFIED`, so neither
  arbitrates the other: a response carrying both with **different** values is
  refused as `TIKTOK_TOKEN_RESPONSE_UNSUPPORTED` rather than reduced to whichever
  key the parser reads first. Two equal declarations agree and are accepted.
* **Advertiser binding.** When the exchange returns `advertiser_ids`, the
  configured profile advertiser must be in that list or enrollment fails with
  `TIKTOK_TOKEN_ADVERTISER_NOT_GRANTED` (exit 4). When it does not, the binding
  is recorded as `NOT_PROVEN_NO_ADVERTISER_LIST_RETURNED` and the advertiser slot
  is not written. An absent list is never treated as an empty grant.
* **An empty advertiser list.** A returned `[]` is not a missing fact; it is a
  definite provider answer that this grant reaches no advertiser account.
  Enrollment therefore fails with a definite
  `TIKTOK_TOKEN_NO_ADVERTISERS_GRANTED` (exit 4, `DEFINITE_RESPONSE`) rather than
  the uncertain `TIKTOK_TOKEN_RESPONSE_UNSUPPORTED`. Nothing is installed, and
  the single-use code was consumed by the request that returned it, so
  re-enrollment needs a new code.
* **Advertiser identity in output.** Only salted per-profile pseudonyms
  (`tiktok_ads_advertiser_v1.<sha256>`) are reported. The complete count is
  always given; the inline list is capped at 16 with an explicit omitted count.
* **Environment.** A declared fact passed as `--environment`, validated against a
  closed host map before any prompt or request. `PRODUCTION` resolves to the one
  proven host `business-api.tiktok.com`. `SANDBOX` is a real TikTok environment
  whose base URL no first-party page this build could fetch publishes, so it is a
  typed `TIKTOK_TOKEN_ENVIRONMENT_NOT_DOCUMENTED` refusal rather than a guessed
  host. The environment is recorded in the state slot and reported in status.
* **Endpoint pinning.** The closed host map is enforced inside the transport as
  well as at the call site. Every `FixedTikTokTokenTransport` constructor,
  including `open()`, refuses a host outside
  `TIKTOK_TOKEN_ENVIRONMENT_HOSTS.values()` with a `NOT_DISPATCHED`
  `TIKTOK_TOKEN_TRANSPORT_HOST_NOT_PINNED` (exit 2) before any client exists, so
  neither the app secret nor the authorization code can be addressed anywhere
  else — including at a host carrying userinfo, where `https://{host}` would
  otherwise resolve to whatever follows the `@`.

## Revocation

Local removal is never revocation and is never evidence of it. The status and
remove-local results both carry the separated guidance. Its provider-side
route follows TikTok's Authorization FAQs (fetched 2026-10-11): the advertiser
removes the developer's authorization at the TikTok For Business authorization
settings page, `ads.tiktok.com/ac/page/authorizations`. TikTok also documents a
revoke endpoint, `/oauth2/revoke_token/`, which takes the app ID, app secret and
the token; AdHelm does not send it, and `remote_revocation_verified` is always
`false`.

## Integration

Both integration points are applied, and the commands are wired.

* `src/adhelm/cli.py` imports `register_commands` from `adhelm.tiktok_token_cli`,
  calls it on the `auth` sub-application beside the other credential families,
  and lists all four leaves in `_COMMANDS`, so `adhelm discover` returns
  `auth tiktok-token enroll|refresh|status|remove-local`. The command-parity test
  and `tiktok_token_status_disclosure_smoke` in `scripts/validate_development.py`
  both exercise the real `adhelm.cli.app`.
* `src/adhelm/models.py` declares `TIKTOK_TOKEN_CREDENTIAL_REFERENCE_PREFIX` and
  reserves it in `is_reserved_credential_reference` beside the Meta and LinkedIn
  prefixes. `adhelm.tiktok_token_profiles` imports that one constant instead of
  repeating the literal, exactly as `adhelm.linkedin_token_profiles` does, so the
  reservation and the derived selectors cannot drift apart. The reservation is
  defence in depth: `select_tiktok_ads_profile` already refuses a profile whose
  `credential_ref` selects a derived TikTok slot.

The CLI cases in `tests/test_tiktok_token_workflow.py` still drive a private
Typer root. That is a test-isolation choice, not a wiring gap; the parity of the
real root application is covered by the command-parity test and by `discover`.

## Evidence limits

Fixture-only. No live TikTok request, no provider-UI reconciliation, no sandbox
proven. Permission scopes, app approval, access-token expiry, any refresh
capability, the sandbox base URL, the `data` payload field names
(`advertiser_ids`, `scope`), and the provider-side revocation path are all
`UNVERIFIED`. This promotes no TikTok Ads operation group.
