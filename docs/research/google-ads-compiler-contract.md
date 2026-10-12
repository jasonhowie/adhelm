# Google Ads v25 Search/RSA compiler contract

**Status: IMPLEMENTING / UNVERIFIED.** This is an offline, fixture-only
compiler. It has no transport, credentials, Google Ads SDK dependency, GAQL
input, protobuf passthrough, upload, write, approval, or activation path. Each
result remains `OFFLINE_CANDIDATE_NOT_EXECUTABLE`.

## Fixed local candidate

`compile_google_ads_search_campaign(GoogleAdsSearchCampaignBrief)` accepts one
customer-declared USD account and produces this closed local graph:

```text
non-shared STANDARD CUSTOM_PERIOD campaign-total budget
  -> paused SEARCH campaign, bound to the budget by a local handle
     -> paused positive location, language, and ad-schedule criteria
     -> paused SEARCH_STANDARD ad group
        -> paused positive exact/phrase keyword criteria
        -> paused responsive search ad
```

The graph's parent references are local role handles, never native resource
names or temporary negative IDs. `campaign_budget_handle` binds the campaign
to the budget node, each criterion/ad group names its campaign handle, and
each keyword/RSA names its ad-group handle. Revalidation checks all links and
globally unique handles. A future guarded executor must resolve those handles
to verified v25 create results before a mutate; this compiler does not claim
that a resource has an ID or that a local handle is wire-ready.

The v25 budget is `CUSTOM_PERIOD`, `explicitly_shared=false`,
`delivery_method=STANDARD`, and has only `total_amount_micros`. `amount_micros`
is not representable. The total is a campaign-lifetime cap, not an average
daily budget. USD minor units convert exactly to micros (`minor_units *
10,000`), and the compiled maximum spend is rechecked against the native
total. The local contract permits an inclusive duration of 1--366 calendar
days. A non-shared budget inherits its name/status from the campaign, so the
candidate does not invent a separate budget name.

The input `total_budget` is `{minor_units, currency}` with positive USD matching
the account. The local maximum of 900,719,925,474 minor units keeps derived
integer micros inside the exact RFC 8785 range before digest creation. This is
a compiler representation bound, not a provider or account budget entitlement.

The campaign is explicitly `SEARCH` and `PAUSED`. It emits v25
`start_date_time` at `00:00:00` and `end_date_time` at `23:59:59`, using the
declared account calendar and the formal v25 `yyyy-MM-dd HH:mm:ss` shape. Its
standard click strategy is an empty `target_spend`; it never sends deprecated
`target_spend_micros`, a bid ceiling, a conversion goal, or `maximize_clicks`
(which is not the v25 Campaign oneof field). Its network settings fix Google
Search true and Search Network, Search Partners, Display, YouTube, and Google
TV false.

Input schedules use quarter-hour integers solely as an input form. The native
candidate maps them to v25 `MinuteOfHour` enum symbols (`ZERO`, `FIFTEEN`,
`THIRTY`, or `FORTY_FIVE`), rejects invalid intervals, and limits each day to
six schedules. The local candidate has at most 20 locations, 20 languages,
21 schedules, and 50 keywords. Criteria and keywords are positive and paused;
the only keyword match types are `EXACT` and `PHRASE`.

Names are nonblank, at most 255 UTF-8 bytes, and reject NUL, line feed, and
carriage return. `ad_name` is retained solely as a bounded local review label:
the v25 `AdGroupAd` request does not have a native ad-name field. No resource
identifier, audience, demographic, custom parameter, tracking template, URL
template, native conversion reference, or arbitrary native field is
representable in this slice.

The sole creative is a customer-supplied text-only Responsive Search Ad with
3--15 unique headlines, 2--4 unique descriptions, one `LEARN_MORE` CTA
headline, and one controlled HTTPS final URL. The input manifest must contain
exactly the ordered headline/description/CTA surface. The compiled result
retains the destination policy, CTA, content digest, and creative-version ID,
then rechecks the native text, URL, CTA, and digest together. This prevents a
serialized candidate from being revalidated after its text or destination is
changed. The default redacted review excludes exact copy, destination,
keywords, allowed hosts/paths, and target-constant IDs.

An RSA's declared CTA is an eligible headline, not a native CTA button or a
served-component guarantee. Components remain unpinned and the review reports
`DYNAMIC_COMBINATION_UNKNOWN` and
`NOT_AVAILABLE_FROM_PROVIDER` for component-level attribution.

## Primary v25 sources

The v25 [Campaign reference](https://developers.google.com/google-ads/api/reference/rpc/v25/Campaign)
defines the `target_spend` standard strategy oneof and the
`start_date_time`/`end_date_time` fields in customer time. The corresponding
official [v25 Campaign proto](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign.proto)
is the field-name cross-check. The [TargetSpend reference](https://developers.google.com/google-ads/api/reference/rpc/v25/TargetSpend)
describes click optimization and marks `target_spend_micros` deprecated.

The v25 [CampaignBudget reference](https://developers.google.com/google-ads/api/reference/rpc/v25/CampaignBudget)
defines `total_amount_micros`, its mutual exclusion with `amount_micros`,
non-shared budget behavior, and `STANDARD` delivery. Google's
[campaign-budget guide](https://developers.google.com/google-ads/api/docs/campaigns/budgets/create-budgets)
states that `CUSTOM_PERIOD` plus `total_amount_micros` is a lifetime cap,
requires `explicitly_shared=false`, and requires set start/end dates for
Search.

The v25 [NetworkSettings reference](https://developers.google.com/google-ads/api/reference/rpc/v25/Campaign.NetworkSettings)
defines the fixed network flags. The v25 [AdScheduleInfo reference](https://developers.google.com/google-ads/api/reference/rpc/v25/AdScheduleInfo)
defines day/time schedules, `MinuteOfHour`, and the six-per-day constraint.
The v25 [CampaignCriterion reference](https://developers.google.com/google-ads/api/reference/rpc/v25/CampaignCriterion)
and [AdGroupCriterion reference](https://developers.google.com/google-ads/api/reference/rpc/v25/AdGroupCriterion)
define the location/language/schedule and keyword criterion unions. The v25
[AdGroup reference](https://developers.google.com/google-ads/api/reference/rpc/v25/AdGroup)
defines required ad-group naming and the campaign association.

The official [Responsive Search Ad creation guide](https://developers.google.com/google-ads/api/docs/responsive-search-ads/create-responsive-search-ads)
requires at least three headlines, two descriptions, and one final URL. The
[RSA overview](https://developers.google.com/google-ads/api/docs/responsive-search-ads/overview)
explains that Google rotates combinations and can pin them. The compiler does
not pin or claim a served combination.

The formal Campaign reference is the source for the hyphenated date-time
format. Its RSA creation sample currently shows compact date values in one
code path. That documentation inconsistency is unresolved by fixture work, so
this compiler follows the formal v25 field reference and makes no live-wire
claim.

**Resolved, 2026-09-09 (P04).** The official v25
[`campaign.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign.proto)
states the format verbatim for both fields: `start_date_time` is "in the
customer's time zone and in `yyyy-MM-dd HH:mm:ss` format. Set the time
component to 00:00:00 for daily granularity", and `end_date_time` is "in
serving customer's timezone in `yyyy-MM-dd HH:mm:ss` format. Set the time
component to 23:59:59 for daily granularity". This is **CITED** and matches the
compiled candidate exactly, so the documentation inconsistency above is
resolved in favour of the hyphenated form. It remains a *field-definition*
citation, not a live-wire acceptance proof: no request has been sent.

## Required promotion checks

The compiler does not emit `geo_target_type_setting`. The v25
[GeoTargetTypeSetting reference](https://developers.google.com/google-ads/api/reference/rpc/v25/Campaign.GeoTargetTypeSetting)
has distinct presence/interest choices, so a future plan must read, record,
and authorize the account/campaign setting. A location criterion alone is not
proof of exact geographic reach.

**How P04 discharges this, 2026-09-09.** `geo_target_type_setting` is a
*Campaign* field, confirmed **CITED** in the v25
[`campaign.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign.proto)
as `GeoTargetTypeSetting` with `positive_geo_target_type` and
`negative_geo_target_type`. There is no account-scoped surface to read before
the campaign exists, so
[`google-ads-create-execution.md`](../implementation/google-ads-create-execution.md)
records the plan-time precondition as the explicit status
`NOT_REQUESTED_PROVIDER_DEFAULT_READ_BACK_AFTER_CREATE` and records the
provider-chosen values in its post-create readback, marked
`OBSERVED_PROVIDER_DEFAULT_NOT_AUTHORIZED`. The plan authorizes no geographic
reach mode.

The compiler also does not prove that account or campaign asset automation,
text expansion, final-URL expansion, or AI Max settings preserve the candidate
text/destination. Google's [asset automation settings guide](https://developers.google.com/google-ads/api/docs/assets/asset-automation-settings)
and v25 [AI Max setting reference](https://developers.google.com/google-ads/api/reference/rpc/v25/Campaign.AiMaxSetting)
require a current readback before a future write plan can make any such claim.

**How P04 discharges this, 2026-09-09, per claim.**

* *Campaign asset automation is readable.* **CITED**: the v25
  [`campaign.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign.proto)
  declares `repeated AssetAutomationSetting asset_automation_settings = 88;`
  with `asset_automation_type` and `asset_automation_status`. P04 reads these
  back after the create and retains them verbatim.
* *Account asset automation is not readable through the API.* **CITED**: the
  [asset automation settings guide](https://developers.google.com/google-ads/api/docs/assets/asset-automation-settings),
  fetched 2026-09-09, states "This account-level setting can only be configured
  in the Google Ads web interface", and
  `google/ads/googleads/v25/resources/customer_asset_automation_setting.proto`
  returns HTTP 404. P04 therefore records
  `NOT_READABLE_WEB_INTERFACE_ONLY_IN_V25` rather than implying a check.
* *Whether automation preserves the compiled text.* `UNVERIFIED`, and not
  predicted. P04's readback binds the exact observed headline/description/final
  URL surface, so expanded text fails verification instead of being accepted.
* *AI Max.* `UNVERIFIED (reference page unreachable on 2026-09-09)`. P04 neither
  requests nor reads `Campaign.ai_max_setting`; no claim is made about it.

**Budget-shape facts confirmed for P04, 2026-09-09.** The v25
[`campaign_budget.proto`](https://raw.githubusercontent.com/googleapis/googleapis/master/google/ads/googleads/v25/resources/campaign_budget.proto)
states **CITED** that `total_amount_micros` "is used *only* when the
CampaignBudget `period` is set to `CUSTOM_PERIOD`", that `amount_micros` is the
`DAILY` field, that `delivery_method` "Defaults to STANDARD", that `period`
"Defaults to DAILY if not specified", and that `explicitly_shared` "Defaults to
true if unspecified in a create operation" -- so an executor must send
`explicitly_shared: false` explicitly. It also resolves the budget-name question **CITED**:
"When creating a campaign budget through CampaignBudgetService, every
explicitly shared campaign budget must have a non-null, non-empty name.
Campaign budgets that are not explicitly shared derive their name from the
attached campaign's name." A non-shared budget therefore needs no name, which
is exactly why this compiler does not invent one. The same file states that
with `explicitly_shared` false "The budget's name and status will stay in sync
with the campaign's name and status", and that `CampaignBudget.status` is
"Output only ... read-only" -- so a paused campaign implies a paused-tracking
budget and no executor may treat the budget status as an independent fact. The
one remaining caveat is scope: the *requirement* half of the name comment is
written for `CampaignBudgetService`, while a guarded executor creates through
`GoogleAdsService.Mutate`; the derivation sentence carries no such scope.

Current account identity, currency/timezone, total-budget eligibility, target
constant existence, geo mode, schedule interpretation, account-default
conversion goals, ad-group/RSA eligibility, keyword approval, policy review,
creative-ledger retention, destination/tracking behavior, remote drift, exact
direct/delegated authorization, mutation outcomes, and recovery all require
guarded account-bound reads. Fixture tests do not establish provider approval,
native-ID binding, resource creation order, idempotency, live serving,
conversion behavior, or release evidence.
