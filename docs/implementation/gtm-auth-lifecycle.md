# GTM auth lifecycle on the shared Google OAuth path

Implemented: 2026-10-10 (ticket O2.5). GTM has no lifecycle code of its own. A
`gtm` READ profile uses the shared Google OAuth enrollment, reconnect, status,
refresh and local-removal code in
[`google-oauth-onboarding.md`](google-oauth-onboarding.md),
[`google-oauth-credential-lifecycle.md`](google-oauth-credential-lifecycle.md)
and [`google-oauth-status.md`](google-oauth-status.md). This record lists what
was missing for GTM, what changed, and what stays open.

## Lifecycle, by release-definition item

| Item | GTM behaviour | Evidence |
| --- | --- | --- |
| Enrollment with the user's own app | `auth google-oauth enroll` with a `provider: "gtm"` draft: numeric account, exactly `tagmanager.readonly`, no currency, developer token or manager route | `tests/test_gtm_auth_lifecycle.py` |
| Status | `auth google-oauth status`: local state, scopes, expiry, masked identity; `developer_token_slot_state` is `NOT_REQUIRED`; contacts no Google endpoint | same |
| Refresh | Automatic and shared. A near-expiry bearer is refreshed once, before the first GTM request, and the same bearer serves every request in the read | same |
| Reconnect | `auth google-oauth reconnect`, same Google subject, client and scope only | same |
| Local removal | `auth remove-local`; afterwards status is `MISSING` and a read fails with `AUTHENTICATION_REQUIRED` before any request | same |
| Revocation guidance | Provider-side grant removal, plus a GTM-only line: container access is granted by Tag Manager user permissions, not by this grant | same |

There is no `auth google-oauth refresh` leaf: refresh happens inside a read and
nowhere else, exactly as for Google Ads and GA4.

## Gap list (integration tip `2edfa0c`)

| # | Gap | State |
| --- | --- | --- |
| G1 | No GTM profile had been read through the real `OS_OAUTH_GOOGLE` credential path. `tests/test_gtm_measurement_workflow.py` and `tests/test_gtm_workflow_ultra_review.py` use `EXPLICIT_CI` and replace `credential`; the OAuth suites mention `gtm` only in scope and edit-refusal parametrizations | Closed: bearer, refresh, rejected refresh, expired refresh token, missing grant, cross-provider bundle and removal cases |
| G2 | GTM draft, enrollment and reconnect were never exercised | Closed: wrong scopes (including GTM edit and publish scopes), developer-token and manager fields, non-numeric account and `PUBLISH` purpose are refused; enrollment stores one bound bundle and no secondary slot; reconnect refuses another Google subject |
| G3 | `status` gave a GTM profile the Google Ads developer-token revocation line and nothing about Tag Manager permissions | Closed: `google_oauth_revocation_guidance(provider)`. GA4 profiles also stop receiving the developer-token line |
| G4 | `401`/`403` on the default GTM read surfaced as the transport's generic `PROVIDER_ACCESS_REQUIRED` at exit 3 with a scope-only next action, while the full inventory retyped the same statuses at exit 4 and dropped the observed status. Neither pointed at `auth google-oauth status` or reconnect | Closed: one classifier, applied at all four GTM request sites |
| G5 | Google documents the Tag Manager quota refusal as a `403` ([limits and quotas](https://developers.google.com/tag-platform/tag-manager/api/v2/limits-quotas), fetched 2026-10-10), so a `403` cannot be attributed to permission, a disabled API or quota from the status alone, and the transport does not read the body | Closed as a disclosure: the `403` diagnostic names all three causes, says it cannot choose, and tells the agent to wait before its single repeat |
| G6 | The cached bearer is reused until 60 seconds before expiry and is captured once per read, so a long read can outlive it | Open, documented below |
| G7 | The project quota of 10,000 requests per day and 0.25 requests per second is not paced by the full-inventory read | Open: not an auth change, see "Known limitation: the full-inventory read is not paced" below |
| G8 | `docs/setup/gtm.md`, `doctor`/`discover` per-provider auth labels | `docs/setup/gtm.md` closed by O4.2a (`tests/test_setup_guides.py` runs its commands against a fake Tag Manager API); the per-provider auth labels stay open: O3.3 |

## Authorization refusals

`classify_gtm_transport_error` reads only the HTTP status and bounded
`Retry-After` value the shared transport records. `request_gtm_read` applies it
at the account and container reads, the version reads, the workspace list and
the complete-inventory requests.

| Status | Code | Exit | Meaning |
| --- | --- | --- | --- |
| 401 | `GTM_AUTHORIZATION_REJECTED` | 4 | Google did not accept the bearer. Next action: read `auth google-oauth status`; an expired access token is refreshed by repeating the read once; `RECONNECT_REQUIRED`, or a `READY` grant Google keeps rejecting, needs `reconnect` as the original Google user |
| 403 | `GTM_ACCESS_NOT_PERMITTED` | 4 | At least Read container access, the Tag Manager API enabled in the OAuth client's Cloud project, or the project quota. Not narrowed to one cause. Next action: check all three, confirm the local grant, then wait before repeating the bounded read once, never in a loop. The wait is the disclosed `Retry-After` when Google sent one (as the Google Ads `429` classifier does); otherwise at least 100 seconds, because the 0.25 requests per second limit is enforced over a 100-second sliding window, and an exhausted daily quota clears only at midnight Pacific time |
| anything else | unchanged | unchanged | `429`, `5xx` and other statuses are returned as the transport raised them. Google's quota page documents no `429` for Tag Manager, so none is relabelled |

No response body, header text, token or client secret reaches a diagnostic; the
tests send a body containing a sentinel and prove it is absent.

## Known limitation: the full-inventory read is not paced

The complete-inventory read is bounded at 1,000 requests (`_MAX_REQUESTS` in
`src/adhelm/gtm_inventory.py`) but sends them as fast as the responses arrive.
Google documents a Tag Manager project limit of 0.25 requests per second,
enforced over a 100-second sliding window (25 requests per window), and 10,000
requests per day, and returns a quota refusal as a `403`
(<https://developers.google.com/tag-platform/tag-manager/api/v2/limits-quotas>,
fetched 2026-10-10, page last updated 2026-07-16). The expected symptom on a
large container is `GTM_ACCESS_NOT_PERMITTED` part-way through the read, after
identity has already succeeded. The read then fails closed and retains
nothing; wait as the next action says and repeat once. Pacing the read is not
part of this ticket. A paced read of that size could also outlive its bearer
(next section).

## Known limitation: a read can outlive its bearer

A GTM read takes one bearer snapshot before the first request (so identity and
inventory bind to one principal). A grant with just over 60 seconds left is not
refreshed, and a long full-inventory read can run past its expiry. The read then
fails closed with `GTM_AUTHORIZATION_REJECTED`, retains nothing, and the repeated
read refreshes first. The snapshot rule and the 60 second skew are shared with
Google Ads and GA4 and are not changed here.

## Evidence limits

- Fixture-only: an in-memory credential store, `httpx.MockTransport`, and a
  seam for the browser grant and the refresh. No browser, OS credential store,
  OAuth endpoint or Tag Manager request is made.
- Revocation at Google, the Tag Manager API enablement requirement and the
  permission model are read from published help pages (checked 2026-10-10 and
  2026-09-09): <https://support.google.com/accounts/answer/13533235> (Google
  Account), <https://support.google.com/tagmanager/answer/6107011> (Tag Manager
  user permissions) and
  <https://developers.google.com/tag-platform/tag-manager/api/v2/limits-quotas>
  (quota numbers, the quota `403`, the 100-second window and the midnight
  Pacific reset; fetched 2026-10-10, page last updated 2026-07-16). This
  product has never observed a revoked grant or a live `401`/`403` from Tag
  Manager.
- A GTM user's permission on a container is never read, so `status` reports
  `resource_access_verified: false` and a `READY` grant proves nothing about
  access to any container.
- The Tag Manager `v2` response vocabulary for `403` reasons is not used, so
  none is claimed.
