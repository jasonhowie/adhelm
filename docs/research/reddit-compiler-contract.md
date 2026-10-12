# Reddit Ads offline compiler contract

Reviewed 2026-09-04. This is a narrow, fixture-only Reddit Ads API v3
compiler for one paused, manual standard traffic hierarchy: one CBO campaign,
one ad group, and one ad that references a customer-supplied existing `t3_`
post. It is an offline candidate only: it sends no request, creates no
resource, uploads no media, and cannot activate or spend.

## Reviewed native route

The compiler models the documented `POST /ad_accounts/{ad_account_id}/campaigns`,
`/ad_groups`, and `/ads` operations as a local dependency graph. Its handles
are lower-case local logical names, never native IDs. A later guarded executor
must resolve each parent handle from the preceding successful, account-bound
readback before it can construct any request.

The fixed supported campaign is `CLICKS` with campaign budget optimization,
`BIDLESS`, `CPC`, `goal_type=LIFETIME_SPEND`, an exact positive integer native
microcurrency `goal_value`, and UTC start/end times. That native lifetime field is the
whole exposure limit. The compiler does not calculate a total from a daily
limit or claim that a multiplied daily budget bounds spend.

The brief's `lifetime_budget` is a typed `{minor_units, currency}` object with
positive USD matching the account. Native `goal_value` is exactly minor units
multiplied by 10,000. The review preserves both the typed maximum spend and native
micros. The local cap is 900,719,925,474 minor units to keep micros canonically
representable; it is not proof of provider eligibility or permitted allocation.

Campaign, ad-group, and ad names are nonblank and capped at the current
documented 500-character maximum. This is a local shape check only; no account
or provider policy approval is implied.

The current v3 API history says `funding_instrument_id` is required for every
campaign. The compiler therefore retains an account-bound, customer-local,
opaque funding reference and emits it in the native campaign candidate. The
current Standard CBO guide example does not show that field, so this is not
live-wire proof: a future guarded executor must read back the exact issued
funding instrument and its available allocation before planning a request.

The CBO ad group inherits its bid strategy/value as documented `null`, retains
the same UTC schedule, binds the same pixel reference, and has the complete
closed targeting surface `geolocations=[US]`, `locations=[FEED]`,
`platforms=[ALL]`, and `view_modes=[ALL]`. It cannot represent community,
keyword, interest, audience, demographic, device, carrier, or arbitrary
placement data. The ad is `PAUSED` and binds its existing `post_id` and
`click_url` directly to the canonical creative destination.

The official schema requires a conversion pixel for all CBO campaigns and all
ad groups as of 2026-07-13. The candidate uses a bounded printable opaque
provider reference instead of inventing a provider-specific pixel-ID grammar.
It is local declaration only. Its ownership, readiness, measurement role, and
native eligibility are unproven until readback.

Every candidate includes a content-addressed local creative version, source
asset and rendition, plus evidence binding an existing native post to the
exact canonical headline, CTA, destination, and serving manifest. The review
projection intentionally omits copy, destination, pixel ID, post ID, asset
digest, and evidence reference.

## Promotion boundary and exact remaining limits

Every result is `OFFLINE_CANDIDATE_NOT_EXECUTABLE` and
`FIXTURE_ONLY_NOT_LIVE_VALIDATED`. Before any write it still requires:

- current account identity and USD/currency-exponent readback;
- account-bound funding-instrument and allocation readback;
- account-bound pixel/native conversion readback;
- account-bound post/media ownership, readiness, policy, and format eligibility;
- objective, US geography, FEED placement, platform/view-mode eligibility;
- provider creative transformation and preview/readback, and local creative-ledger verification;
- destination and tracking validation; remote drift rejection; exact
  digest-bound authorization; and the full `APPLY_STARTED`/apply/re-read/
  verification/recovery/evidence workflow;
- a separately authorized activation plan and bounded live test.

The compiler does not establish provider minimum budget, funding-instrument
availability, ads review outcome, account/API access, OAuth lifecycle,
idempotency/ambiguous-write behavior, runtime delivery, spend enforcement
behavior, pixel receipt, report continuation, or live readback schema. The
Reddit route is documented, but those facts remain release-gating feasibility
work; fixtures do not substitute for them.

## Primary sources

- [Reddit Ads API v3 OpenAPI specification](https://ads-api.reddit.com/api/v3/openapi.json): linked for future raw-schema validation. It was not independently parsed for this fixture-only contract, so this document does not claim wire-schema proof from it.
- [Set Up a Standard Campaign](https://ads-api.reddit.com/docs/v3/guides/programs/campaign/campaign-setup), accessed 2026-09-04: traffic CBO `CLICKS`/`BIDLESS`/`CPC`/`LIFETIME_SPEND`, CBO ad-group inheritance, post-bound standard ad, and `click_url` requirements.
- [Create Campaign](https://ads-api.reddit.com/docs/v3/api/create-campaign), accessed 2026-09-04: v3 route and 2026-07-13 CBO pixel requirement.
- [Create Ad Group](https://ads-api.reddit.com/docs/v3/api/create-ad-group), accessed 2026-09-04: v3 route and all-ad-group pixel requirement.
- [What's New](https://ads-api.reddit.com/docs/v3/history), accessed 2026-09-04: `funding_instrument_id` requirement for all campaigns and the 2026 CBO/ad-group pixel requirement.
- [Reddit fixed read contract](reddit-ads-read-contract.md): existing fixture-only v3 identity and inventory boundary.
