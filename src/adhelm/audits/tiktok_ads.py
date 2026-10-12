# ruff: noqa: E501
"""Deterministic TikTok Ads audit rules over retained local records.

This module makes **no provider request**.  It evaluates the retained
`PERFORMANCE_OBSERVATION` report the caller selected, the retained
`RESOURCE_SNAPSHOT` set a completed inventory read appended, an optional
earlier snapshot set, and an optional local creative catalog.  Authorization,
quota, 401/403 and rate-limit handling therefore stays entirely with the read
adapter that produced those records; nothing here can leak a response body
because no response is available here.

Five honesty bounds shape every rule:

* The reviewed TikTok inventory and report reads both walk numbered
  `page`/`page_size` pages and validate the declared `total_number` on every
  one, failing closed with `RESULT_LIMIT_REQUIRED`, `PAGINATION_LIMIT` or
  `PROVIDER_RESPONSE_INVALID` rather than returning a partial collection.  That
  is a property of the adapter, not of a record set handed to this module, and
  the shared audit vocabulary in :mod:`adhelm.audits` has no exhaustive
  `AnalyticsCompleteness` member at all.  Every analytics-derived finding
  therefore stays `BOUNDED_NON_EXHAUSTIVE` and never claims a total.  A
  retained account snapshot that does **not** declare
  `TIKTOK_PAGINATION_TOTAL_DECLARED` predates that proof and is reported.
* **Presence is not content, and unread is not healthy.**  TikTok's reference
  portal renders through JavaScript and the TikTok-maintained SDK documents the
  `fields` parameter of `/campaign/get/`, `/adgroup/get/` and `/ad/get/` as a
  bare `list[str]` with no enumeration of returnable field names.  This build
  therefore requests exactly the reviewed projection and nothing else: ad-group
  schedule and targeting, ad-group placement, ad creative association, video
  presence, ad copy, destination, pixel health and native conversions are all
  declared unread in `TIKTOK_ADS_READ_SURFACES` and reported as unread here.
  Adding an unproven field name would be a fabricated capability that a fixture
  would silently accept.
* **An enumeration this product cannot check is not an enumeration.**  No
  fetchable first-party source publishes TikTok's `objective_type`, `bid_type`
  or advertiser `status` vocabularies, so those facts are retained as presence
  only and reported with an explicit `NOT_DOCUMENTED_LOCALLY` disclosure.  Only
  `operation_status` has a reviewed pair (`ENABLE`, `DISABLE`) and only the
  three reviewed budget modes are accepted by the read at all.
* **`operation_status` does not fold a parent in.**  Unlike Meta's
  `effective_status`, TikTok's `operation_status` is the value set on that exact
  object; the value that would propagate a parent -- `secondary_status` -- is
  not in the reviewed read.  An `ENABLE` child under a `DISABLE` parent is
  therefore a genuine contradiction and gets its own rule.
* **No TikTok pause route exists in this build.**  `COMPILABLE_OPERATION_KINDS`
  names `CREATE` for TikTok, and that compiler produces one offline,
  non-executable paused candidate hierarchy -- not a change to an existing
  resource.  Every recommendation this table emits is therefore `OBSERVE`, and
  every mutation it considered, including the pause a serving contradiction
  might otherwise suggest, appears as an explicit declined row.

`docs/implementation/tiktok-ads-audit-recommendations.md` carries the rule
table, coverage map and evidence limits in prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal

from adhelm.audit import (
    AuditDataWindow,
    AuditDimension,
    Confidence,
    Risk,
    Severity,
    build_finding,
    build_recommendation,
)
from adhelm.audits import (
    MAXIMUM_INLINE_FINDING_IDS,
    AnalyticsCompleteness,
    BoundExceeded,
    DriftObservation,
    EvidenceSource,
    NoRecommendation,
    NoRecommendationReason,
    ProviderAuditContext,
    ProviderAuditCoverage,
    ProviderAuditFinding,
    ProviderAuditSections,
    ProviderCoverageStatus,
    ProviderRecommendation,
    RecommendedOperationKind,
    RetainedInventory,
    RetainedSnapshotRecord,
    register_provider_rule,
)
from adhelm.models import Provider
from adhelm.providers.tiktok_ads import (
    TIKTOK_ADS_BID_PRICE_ABSENT,
    TIKTOK_ADS_BID_TYPE_NOT_RETURNED,
    TIKTOK_ADS_BUDGET_AMOUNT_ABSENT,
    TIKTOK_ADS_BUDGET_MODE_INFINITE,
    TIKTOK_ADS_BUDGET_MODE_NOT_RETURNED,
    TIKTOK_ADS_OBJECTIVE_NOT_RETURNED,
    TIKTOK_ADS_OPERATION_STATUS_NOT_REVIEWED,
    TIKTOK_ADS_PAGINATION_TOTAL_DECLARED,
    TIKTOK_ADS_READ_SURFACES,
    TIKTOK_ADS_REPORT_COMPLETE_STATUS,
    TIKTOK_ADS_STATE_SHAPE_PREFIX,
    tiktok_ads_declares_serving_intent,
)
from adhelm.reporting import NormalizedReport, PerformanceRow

PROVIDER: Final[Provider] = "tiktok_ads"
# Bump this when a rule's semantics change.  It is part of every finding's
# content identity, so an audit produced by a different rule table can never be
# mistaken for one produced by this table.
RULE_TABLE_VERSION: Final = "tiktok_ads.rules.2026-09-09.1"
_BOUNDED: Final[AnalyticsCompleteness] = "BOUNDED_NON_EXHAUSTIVE"
_NOT_ANALYTICS: Final[AnalyticsCompleteness] = "NOT_APPLICABLE"
# The only TikTok bid type this repository's reviewed v1.3 compiler contract
# pins.  It is the documented "no bid" mode, so an absent bid price is expected
# there and is not reported.  No other value can be interpreted, because no
# fetchable first-party source enumerates the field.
_NO_BID_TYPE: Final = "BID_TYPE_NO_BID"
# The resource types whose retained `effective_status` is a TikTok
# `operation_status`.  The advertiser account's status is a different field with
# no reviewed vocabulary, so it is never compared with one of these.
_OPERATION_STATUS_TYPES: Final[frozenset[str]] = frozenset({"CAMPAIGN", "AD_GROUP", "AD"})
_DIMENSION_ORDER: Final[tuple[AuditDimension, ...]] = (
    "STRUCTURE",
    "DELIVERY",
    "SPEND",
    "BIDS",
    "OBJECTIVE",
    "TARGETING",
    "PLACEMENT",
    "CREATIVE",
    "DESTINATION_CONFIGURATION",
    "TRACKING",
    "NATIVE_CONVERSION",
    "BROWSER_MEASUREMENT",
    "POLICY",
    "DRIFT",
)


@dataclass(frozen=True)
class _Rule:
    """One deterministic rule: fixed prose, fixed severity, fixed dimension."""

    rule_id: str
    finding_type: str
    dimension: AuditDimension
    severity: Severity
    confidence: Confidence
    risk: Risk
    analytics_completeness: AnalyticsCompleteness
    observation: str
    suggested_action: str


def _rule(rule: _Rule) -> tuple[str, _Rule]:
    return rule.rule_id, rule


RULES: Final[dict[str, _Rule]] = dict(
    (
        # ----------------------------------------------------------- structure
        _rule(
            _Rule(
                rule_id="TT_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP",
                finding_type="TIKTOK_CAMPAIGN_WITHOUT_AD_GROUP",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok campaign has no retained ad group in the same inventory read.",
                suggested_action="Confirm the campaign in TikTok Ads Manager, or read the bounded hierarchy again; a campaign with no ad group cannot serve.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_STRUCTURE_AD_GROUP_WITHOUT_AD",
                finding_type="TIKTOK_AD_GROUP_WITHOUT_AD",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok ad group has no retained ad in the same inventory read.",
                suggested_action="Confirm the ad group in TikTok Ads Manager; an ad group with no ad cannot deliver an impression.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED",
                finding_type="TIKTOK_AD_GROUP_PARENT_NOT_RETAINED",
                dimension="STRUCTURE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok ad group names a campaign the same inventory read did not retain.",
                suggested_action="Read the bounded hierarchy again so the ad group's campaign is retained beside it; nothing here claims the campaign does not exist at TikTok.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_STRUCTURE_AD_PARENT_NOT_RETAINED",
                finding_type="TIKTOK_AD_PARENT_NOT_RETAINED",
                dimension="STRUCTURE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok ad names an ad group the same inventory read did not retain.",
                suggested_action="Read the bounded hierarchy again so the ad's ad group is retained beside it; nothing here claims the ad group does not exist at TikTok.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_STRUCTURE_STATUS_NOT_REVIEWED",
                finding_type="TIKTOK_OPERATION_STATUS_NOT_REVIEWED",
                dimension="STRUCTURE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok resource declares an operation status outside the pair this build reviewed.",
                suggested_action="Review the value against TikTok's current v1.3 reference before any decision uses it; this audit does not guess what an unreviewed status means.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_STRUCTURE_SERVING_INTENT_CONTRADICTS_PARENT",
                finding_type="TIKTOK_SERVING_INTENT_CONTRADICTS_PARENT",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok resource declares the serving-intent operation status while a retained parent does not. TikTok's operation status is set per object and does not fold a parent in.",
                suggested_action="Confirm the intended state in TikTok Ads Manager. The propagated value would be `secondary_status`, which the reviewed fixed read does not request, so this product cannot tell whether the child is actually serving.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_STRUCTURE_PAGINATION_TOTAL_NOT_DECLARED",
                finding_type="TIKTOK_PAGINATION_TOTAL_NOT_DECLARED",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok account snapshot does not record that its inventory read proved the provider's declared total result count.",
                suggested_action="Read the bounded inventory again with the current connector; what an older record set omits proves nothing about the account.",
            )
        ),
        # ------------------------------------------------------------ delivery
        _rule(
            _Rule(
                rule_id="TT_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
                finding_type="TIKTOK_REPORT_COMPLETENESS_NOT_DECLARED",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained TikTok report envelope does not declare the completeness status the reviewed read records for a whole page walk.",
                suggested_action="Run the same bounded report again before a decision uses it; a report that does not declare completeness is not evidence of a total.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_DELIVERY_REPORT_FRESHNESS_NOT_READ",
                finding_type="TIKTOK_REPORT_FRESHNESS_NOT_READ",
                dimension="DELIVERY",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="TikTok's reviewed synchronous report response proves no data-freshness time, so the retained envelope records none.",
                suggested_action="Treat the report as of its own observation time only; do not infer how current TikTok's processed data was.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_DELIVERY_REPORT_ATTRIBUTION_NOT_READ",
                finding_type="TIKTOK_REPORT_ATTRIBUTION_NOT_READ",
                dimension="DELIVERY",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="TikTok's reviewed synchronous report response declares no attribution model or window, so the retained envelope records the fact as unavailable.",
                suggested_action="Do not compare these rows with another provider's attributed conversions; no attribution setting is known for this read.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_DELIVERY_METRICS_MISSING",
                finding_type="TIKTOK_DELIVERY_METRICS_MISSING",
                dimension="DELIVERY",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="This retained TikTok report row is missing an impression or click count.",
                suggested_action="Run the same bounded report again; a row without both counts supports no delivery conclusion.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
                finding_type="TIKTOK_CLICKS_EXCEED_IMPRESSIONS",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="MEDIUM",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="This retained TikTok report row records more clicks than impressions.",
                suggested_action="Run the same bounded report over the same declared window again before the row is used; one inconsistent aggregate is not evidence of a delivery or billing defect.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS",
                finding_type="TIKTOK_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="LOW",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained TikTok campaign declares the serving-intent operation status and the retained campaign-grain report returned no impressions for it.",
                suggested_action="Read the bounded campaign report again; the absence of a row in a retained report is not evidence of zero delivery.",
            )
        ),
        # --------------------------------------------------------------- spend
        _rule(
            _Rule(
                rule_id="TT_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET",
                finding_type="TIKTOK_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET",
                dimension="SPEND",
                severity="HIGH",
                confidence="MEDIUM",
                risk="HIGH",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok campaign declares the serving-intent operation status and neither it nor any retained ad group under it retained an amount that bounds spend.",
                suggested_action="Read the bounded hierarchy again and confirm the budget in TikTok Ads Manager; a read that returned no budget is not proof that the campaign has none.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_SPEND_INFINITE_BUDGET_MODE",
                finding_type="TIKTOK_INFINITE_BUDGET_MODE_RETAINED",
                dimension="SPEND",
                severity="MEDIUM",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok resource declares the documented unlimited budget mode, so its own record bounds no spend.",
                suggested_action="Confirm where the spend bound sits for this hierarchy in TikTok Ads Manager; an unlimited mode at one level may be bounded at another this read did not retain.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_SPEND_BUDGET_AMOUNT_ABSENT",
                finding_type="TIKTOK_BUDGET_AMOUNT_ABSENT",
                dimension="SPEND",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok resource declares a budget mode that carries an amount, and the read retained no amount for it.",
                suggested_action="Read the bounded hierarchy again; a declared daily or lifetime mode without a retained amount bounds nothing locally.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_SPEND_BUDGET_MODE_NOT_RETURNED",
                finding_type="TIKTOK_BUDGET_MODE_NOT_RETURNED",
                dimension="SPEND",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok resource returned no budget mode at all, so the meaning of any retained amount is unknown.",
                suggested_action="Read the bounded hierarchy again; without a mode, an amount cannot be read as a daily or a lifetime bound.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_SPEND_WITHOUT_IMPRESSIONS",
                finding_type="TIKTOK_SPEND_WITHOUT_IMPRESSIONS",
                dimension="SPEND",
                severity="HIGH",
                confidence="MEDIUM",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained TikTok report row records spend against zero impressions.",
                suggested_action="Run the same bounded report over the same declared window again before the row is used; one inconsistent aggregate is not evidence of a billing defect.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_SPEND_SCHEDULE_NOT_READ",
                finding_type="TIKTOK_SCHEDULE_NOT_READ",
                dimension="SPEND",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The reviewed fixed TikTok read requests no schedule field, so no retained record proves when any resource starts or stops.",
                suggested_action="Confirm schedules in TikTok Ads Manager. No `fields` value for the ad-group schedule is published on any first-party page this build could fetch, so it is not requested rather than guessed.",
            )
        ),
        # ---------------------------------------------------------------- bids
        _rule(
            _Rule(
                rule_id="TT_BIDS_TYPE_NOT_RETURNED",
                finding_type="TIKTOK_BID_TYPE_NOT_RETURNED",
                dimension="BIDS",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok ad group returned no bid type.",
                suggested_action="Read the bounded hierarchy again; without a bid type, a retained bid price cannot be interpreted.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_BIDS_PRICE_ABSENT",
                finding_type="TIKTOK_BID_PRICE_ABSENT",
                dimension="BIDS",
                severity="LOW",
                confidence="MEDIUM",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok ad group returned a bid type other than the documented no-bid mode and no bid price beside it.",
                suggested_action="Confirm the bid in TikTok Ads Manager. TikTok publishes no bid-type enumeration this build could check, so which types require a price is not known locally.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_BIDS_TYPE_ENUM_NOT_DOCUMENTED_LOCALLY",
                finding_type="TIKTOK_BID_TYPE_ENUM_NOT_DOCUMENTED_LOCALLY",
                dimension="BIDS",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="No first-party TikTok source this build could fetch publishes the bid-type vocabulary, so a retained bid type is a presence fact and never a checked enumeration.",
                suggested_action="Treat a retained bid type as an opaque provider value. TikTok's reference portal renders through JavaScript and the TikTok-maintained SDK documents the field as a bare string.",
            )
        ),
        # ----------------------------------------------------------- objective
        _rule(
            _Rule(
                rule_id="TT_OBJECTIVE_NOT_RETURNED",
                finding_type="TIKTOK_OBJECTIVE_NOT_RETURNED",
                dimension="OBJECTIVE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained TikTok campaign returned no objective.",
                suggested_action="Read the bounded hierarchy again; a campaign whose objective is unknown supports no objective conclusion.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_OBJECTIVE_ENUM_NOT_DOCUMENTED_LOCALLY",
                finding_type="TIKTOK_OBJECTIVE_ENUM_NOT_DOCUMENTED_LOCALLY",
                dimension="OBJECTIVE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="No first-party TikTok source this build could fetch publishes the objective vocabulary, so a retained objective is a presence fact and never a checked enumeration.",
                suggested_action="Treat a retained objective as an opaque provider value; this audit never infers a delivery consequence from it.",
            )
        ),
        # ----------------------------------------------------------- targeting
        _rule(
            _Rule(
                rule_id="TT_TARGETING_NOT_READ",
                finding_type="TIKTOK_TARGETING_NOT_READ",
                dimension="TARGETING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The reviewed fixed TikTok ad-group read requests no targeting field, so no retained record carries even the presence of a geography, an age range or an audience.",
                suggested_action="Confirm targeting in TikTok Ads Manager. This dimension is unread, not healthy, and no retained record may be used to argue that an ad group is targeted at all.",
            )
        ),
        # ----------------------------------------------------------- placement
        _rule(
            _Rule(
                rule_id="TT_PLACEMENT_NOT_READ",
                finding_type="TIKTOK_PLACEMENT_NOT_READ",
                dimension="PLACEMENT",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The reviewed fixed TikTok ad-group read requests no placement field and the fixed report requests no placement facet.",
                suggested_action="Confirm placements in TikTok Ads Manager; this dimension is unread and is never reported as healthy.",
            )
        ),
        # ------------------------------------------------------------ creative
        _rule(
            _Rule(
                rule_id="TT_CREATIVE_ASSOCIATION_NOT_READ",
                finding_type="TIKTOK_CREATIVE_ASSOCIATION_NOT_READ",
                dimension="CREATIVE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The reviewed fixed TikTok ad read returns identity, parents and the operation status only, so no retained record proves which creative or video an ad serves.",
                suggested_action="Confirm creative associations in TikTok Ads Manager; a retained ad snapshot proves the ad exists in a bounded read and nothing about what it shows.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_CREATIVE_CONTENT_NOT_READ",
                finding_type="TIKTOK_CREATIVE_CONTENT_NOT_READ",
                dimension="CREATIVE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Ad copy, call to action, video identifiers and identity references are never requested by this build's fixed TikTok read, so no creative content exists locally to evaluate.",
                suggested_action="Register the creative locally if it must be reviewed; this product never retrieves TikTok creative text or media through this read.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_CREATIVE_LOCAL_BINDING_UNVERIFIED",
                finding_type="TIKTOK_LOCAL_CREATIVE_BINDING_UNVERIFIED",
                dimension="CREATIVE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This local creative version declares a TikTok ad that the retained inventory also holds, but the association itself was never verified at TikTok.",
                suggested_action="Treat the binding as a local declaration awaiting an independently reviewed association; local registration retains no provider currentness.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
                finding_type="TIKTOK_LOCAL_CREATIVE_BINDING_WITHOUT_RESOURCE",
                dimension="CREATIVE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This local creative version declares a TikTok ad that a whole bounded inventory read did not return.",
                suggested_action="Read the bounded inventory again before treating the local binding as current; this compares two retained record sets and claims nothing about TikTok.",
            )
        ),
        # --------------------------------------------------------- destination
        _rule(
            _Rule(
                rule_id="TT_DESTINATION_VALUES_NOT_READ",
                finding_type="TIKTOK_DESTINATION_NOT_READ",
                dimension="DESTINATION_CONFIGURATION",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The reviewed fixed TikTok read requests no landing-page or tracking-parameter field, so no destination, redirect or UTM value exists locally.",
                suggested_action="Validate destinations outside this audit; this product never retains a TikTok destination URL or query value.",
            )
        ),
        # ------------------------------------------------------------ tracking
        _rule(
            _Rule(
                rule_id="TT_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ",
                finding_type="TIKTOK_PIXEL_AND_EVENT_HEALTH_NOT_READ",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The reviewed fixed TikTok read requests no pixel, event or Events API field, so no retained record carries tracking health.",
                suggested_action="Review pixel and event configuration in TikTok Events Manager; this dimension is unread and is never reported as healthy.",
            )
        ),
        # --------------------------------------------------- native conversion
        _rule(
            _Rule(
                rule_id="TT_NATIVE_CONVERSION_NOT_READ",
                finding_type="TIKTOK_NATIVE_CONVERSION_NOT_READ",
                dimension="NATIVE_CONVERSION",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="The fixed TikTok report projects impressions, clicks and spend only; no conversion or action metric is requested.",
                suggested_action="Do not read a conversion outcome from this report; no native conversion metric was requested at all.",
            )
        ),
        # --------------------------------------------------------- browser
        _rule(
            _Rule(
                rule_id="TT_BROWSER_NOT_APPLICABLE",
                finding_type="TIKTOK_BROWSER_MEASUREMENT_NOT_APPLICABLE",
                dimension="BROWSER_MEASUREMENT",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="A local audit executes no browser and loads no page, so browser measurement cannot be evaluated from retained records.",
                suggested_action="Use the separate browser verification workflow; nothing here observes a page or a measurement request.",
            )
        ),
        # -------------------------------------------------------------- policy
        _rule(
            _Rule(
                rule_id="TT_POLICY_ACCOUNT_STATUS_NOT_REVIEWED",
                finding_type="TIKTOK_ACCOUNT_STATUS_NOT_REVIEWED",
                dimension="POLICY",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The retained TikTok advertiser status is read, but no first-party source this build could fetch publishes its vocabulary, so its meaning is not checked.",
                suggested_action="Confirm the advertiser's standing in TikTok Ads Manager; the retained value is an opaque provider status.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_POLICY_AD_REVIEW_STATE_NOT_READ",
                finding_type="TIKTOK_AD_REVIEW_STATE_NOT_READ",
                dimension="POLICY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The reviewed fixed TikTok read requests no `secondary_status` or review field, so no retained record says whether an ad passed review.",
                suggested_action="Check ad review state in TikTok Ads Manager; a retained `operation_status` is what the operator set, never a review outcome.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_POLICY_APP_APPROVAL_AND_PERMISSIONS_NOT_DOCUMENTED",
                finding_type="TIKTOK_APP_APPROVAL_AND_PERMISSIONS_NOT_DOCUMENTED_LOCALLY",
                dimension="POLICY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="TikTok selects an application's endpoint permissions in its My Apps console and publishes no permission vocabulary this build could fetch, so the app's approval state and granted permissions are not documented locally.",
                suggested_action="Confirm the application's approved endpoint permissions in TikTok's My Apps console; no local record proves what this credential may read.",
            )
        ),
        # --------------------------------------------------------------- drift
        _rule(
            _Rule(
                rule_id="TT_DRIFT_STATE_CHANGED",
                finding_type="TIKTOK_OUT_OF_BAND_STATE_CHANGE_OBSERVED",
                dimension="DRIFT",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Two retained TikTok snapshots of this resource differ in at least one field both reads returned.",
                suggested_action="Read the bounded hierarchy again to bound the observation interval; the actor and the exact change time are unknown and are not inferred.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_DRIFT_LOCAL_STATE_SHAPE_CHANGED",
                finding_type="TIKTOK_LOCAL_STATE_SHAPE_DIFFERENCE",
                dimension="DRIFT",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Two retained TikTok snapshots of this resource carry different state field sets, which this product cannot tell apart from a change to what it retains.",
                suggested_action="Compare the two retained connector state-shape versions; nothing here claims a change at TikTok.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_DRIFT_ABSENT_FROM_LATER_INVENTORY",
                finding_type="TIKTOK_RESOURCE_ABSENT_FROM_LATER_INVENTORY",
                dimension="DRIFT",
                severity="MEDIUM",
                confidence="MEDIUM",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This TikTok resource is in the earlier retained inventory and not in the later one.",
                suggested_action="Read the bounded hierarchy again; a disappearance between two retained reads is an observation, never a proven deletion time.",
            )
        ),
        _rule(
            _Rule(
                rule_id="TT_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
                finding_type="TIKTOK_RESOURCE_ABSENT_FROM_EARLIER_INVENTORY",
                dimension="DRIFT",
                severity="LOW",
                confidence="MEDIUM",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This TikTok resource is in the later retained inventory and not in the earlier one.",
                suggested_action="Read the bounded hierarchy again; an appearance between two retained reads is an observation, never a proven creation time.",
            )
        ),
    )
)


def _window(report: NormalizedReport) -> AuditDataWindow:
    return AuditDataWindow(
        window_start=report.envelope.window_start,
        window_end=report.envelope.window_end,
        source_timezone=report.envelope.source_timezone,
        attribution_model=report.envelope.attribution_model,
        attribution_window=report.envelope.attribution_window,
    )


def _emit(
    rule: _Rule,
    *,
    resource_refs: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    window: AuditDataWindow,
    coverage_status: ProviderCoverageStatus,
    confidence: Confidence | None = None,
    suggested_action: str | None = None,
) -> ProviderAuditFinding:
    selected: Confidence = rule.confidence if confidence is None else confidence
    # A rule's prose is fixed.  One rule -- the local state-shape difference --
    # can additionally name the two retained connector state-shape versions it
    # compared, which is the only thing that makes its suggested action
    # actionable.  The override is part of the finding's content identity, so a
    # finding that names different versions is a different finding.
    action = rule.suggested_action if suggested_action is None else suggested_action
    finding = build_finding(
        finding_type=rule.finding_type,
        severity=rule.severity,
        provider=PROVIDER,
        resource_refs=resource_refs,
        observation=rule.observation,
        evidence_refs=evidence_refs,
        data_window=window,
        confidence=selected,
        risk=rule.risk,
        suggested_action=action,
        identity={
            "rule_table_version": RULE_TABLE_VERSION,
            "rule_id": rule.rule_id,
            "resource_refs": resource_refs,
            "evidence_refs": evidence_refs,
            "coverage_status": coverage_status,
            "confidence": selected,
            "analytics_completeness": rule.analytics_completeness,
            "suggested_action": action,
        },
    )
    return ProviderAuditFinding(
        finding=finding,
        dimension=rule.dimension,
        coverage_status=coverage_status,
        analytics_completeness=rule.analytics_completeness,
        rule_id=rule.rule_id,
    )


def _by_type(inventory: RetainedInventory | None) -> dict[str, list[RetainedSnapshotRecord]]:
    grouped: dict[str, list[RetainedSnapshotRecord]] = {
        "ACCOUNT": [],
        "CAMPAIGN_GROUP": [],
        "CAMPAIGN": [],
        "AD_GROUP": [],
        "AD": [],
    }
    if inventory is not None:
        for record in inventory.records:
            grouped[record.snapshot.resource_type].append(record)
    for records in grouped.values():
        records.sort(key=lambda item: item.snapshot.resource_ref)
    return grouped


def _quality(record: RetainedSnapshotRecord) -> frozenset[str]:
    return frozenset(record.snapshot.data_quality_statuses)


def _spend_is_positive(row: PerformanceRow) -> bool:
    """Whether a retained TikTok row records positive spend.

    The TikTok read parses `spend` as an exact decimal against a reviewed
    per-currency exponent and refuses any value with unsupported precision
    before a row exists, so a retained minor-unit amount is the exact provider
    amount in the account's own currency rather than a rounded projection.
    """
    return row.spend is not None and row.spend.minor_units > 0


def _has_retained_spend_bound(record: RetainedSnapshotRecord) -> bool:
    """Whether one retained snapshot carries an amount that bounds spend.

    The documented unlimited mode is deliberately not a bound: TikTok returns no
    amount with it, and treating its presence as a budget would hide exactly the
    campaigns whose spend is uncapped.
    """
    state = record.snapshot.normalized_state
    return state.get("budget_minor") is not None


def _absence_is_evidence(inventory: RetainedInventory) -> bool:
    """Whether what a retained record set omits carries any information."""
    return inventory.completeness != "READ_BOUNDARY_UNVERIFIED"


def _structure_findings(
    grouped: dict[str, list[RetainedSnapshotRecord]],
    inventory: RetainedInventory,
    window: AuditDataWindow,
    status: ProviderCoverageStatus,
) -> list[ProviderAuditFinding]:
    # A complete bounded read makes an absence meaningful.  A caller-declared
    # subset does not, so absence rules drop to LOW confidence there.
    absence_confidence: Confidence = (
        "HIGH" if inventory.completeness == "COMPLETE_BOUNDED_READ" else "LOW"
    )
    findings: list[ProviderAuditFinding] = []
    parents: dict[str, list[str]] = {}
    for record in inventory.records:
        for parent in record.snapshot.parent_resource_refs:
            parents.setdefault(parent, []).append(record.snapshot.resource_ref)
    childless = {
        "CAMPAIGN": ("TT_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP", "AD_GROUP"),
        "AD_GROUP": ("TT_STRUCTURE_AD_GROUP_WITHOUT_AD", "AD"),
    }
    for resource_type, (rule_id, child_type) in childless.items():
        known = {item.snapshot.resource_ref for item in grouped[child_type]}
        for record in grouped[resource_type]:
            if any(child in known for child in parents.get(record.snapshot.resource_ref, ())):
                continue
            findings.append(
                _emit(
                    RULES[rule_id],
                    resource_refs=(record.snapshot.resource_ref,),
                    evidence_refs=(record.record_digest,),
                    window=window,
                    coverage_status=status,
                    confidence=absence_confidence,
                )
            )
    orphan_rules = {
        "AD_GROUP": ("TT_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED", "CAMPAIGN"),
        "AD": ("TT_STRUCTURE_AD_PARENT_NOT_RETAINED", "AD_GROUP"),
    }
    for resource_type, (rule_id, parent_type) in orphan_rules.items():
        known = {item.snapshot.resource_ref for item in grouped[parent_type]}
        for record in grouped[resource_type]:
            if not set(record.snapshot.parent_resource_refs) & known:
                findings.append(
                    _emit(
                        RULES[rule_id],
                        resource_refs=(record.snapshot.resource_ref,),
                        evidence_refs=(record.record_digest,),
                        window=window,
                        coverage_status=status,
                    )
                )
    # Only campaign, ad-group and ad statuses are `operation_status` values.
    # The advertiser account carries a different status whose vocabulary no
    # first-party source this build could fetch publishes, so it is excluded
    # from both sides: an account must never be read as contradicting its own
    # children on a value this product cannot interpret.
    #
    # A retained `operation_status` outside the reviewed pair is excluded for
    # exactly the same reason.  `TT_STRUCTURE_STATUS_NOT_REVIEWED` says out loud
    # that this audit does not guess what such a value means, so it may not then
    # be read as "not serving" on the parent side either -- that would build a
    # MEDIUM contradiction and a declined `PAUSE` row on a value the same run
    # reported as uninterpretable.
    serving: dict[str, bool] = {
        record.snapshot.resource_ref: tiktok_ads_declares_serving_intent(
            record.snapshot.effective_status
        )
        for record in inventory.records
        if record.snapshot.resource_type in _OPERATION_STATUS_TYPES
        and TIKTOK_ADS_OPERATION_STATUS_NOT_REVIEWED not in _quality(record)
    }
    for record in inventory.records:
        if TIKTOK_ADS_OPERATION_STATUS_NOT_REVIEWED in _quality(record):
            findings.append(
                _emit(
                    RULES["TT_STRUCTURE_STATUS_NOT_REVIEWED"],
                    resource_refs=(record.snapshot.resource_ref,),
                    evidence_refs=(record.record_digest,),
                    window=window,
                    coverage_status=status,
                )
            )
            continue
        # TikTok's `operation_status` is per object and does not fold a parent
        # in, so a serving-intent child under a retained non-serving parent is a
        # real contradiction rather than the documented propagation Meta has.
        if not serving.get(record.snapshot.resource_ref):
            continue
        contradicting = [
            parent
            for parent in record.snapshot.parent_resource_refs
            if parent in serving and not serving[parent]
        ]
        if contradicting:
            findings.append(
                _emit(
                    RULES["TT_STRUCTURE_SERVING_INTENT_CONTRADICTS_PARENT"],
                    resource_refs=(record.snapshot.resource_ref, *sorted(contradicting)),
                    evidence_refs=(record.record_digest,),
                    window=window,
                    coverage_status=status,
                )
            )
    for record in grouped["ACCOUNT"]:
        if TIKTOK_ADS_PAGINATION_TOTAL_DECLARED in _quality(record):
            continue
        findings.append(
            _emit(
                RULES["TT_STRUCTURE_PAGINATION_TOTAL_NOT_DECLARED"],
                resource_refs=(record.snapshot.resource_ref,),
                evidence_refs=(record.record_digest,),
                window=window,
                coverage_status=status,
            )
        )
    return findings


def _campaign_setting_findings(
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
    coverage: dict[AuditDimension, ProviderCoverageStatus],
) -> list[ProviderAuditFinding]:
    """Spend and objective rules over the retained campaign settings."""
    findings: list[ProviderAuditFinding] = []
    ad_groups_by_campaign: dict[str, list[RetainedSnapshotRecord]] = {}
    for record in grouped["AD_GROUP"]:
        for parent in record.snapshot.parent_resource_refs:
            ad_groups_by_campaign.setdefault(parent, []).append(record)
    for record in grouped["CAMPAIGN"]:
        quality = _quality(record)
        refs = (record.snapshot.resource_ref,)
        evidence = (record.record_digest,)
        if tiktok_ads_declares_serving_intent(
            record.snapshot.effective_status
        ) and not _has_retained_spend_bound(record):
            # TikTok allows the budget to sit on the campaign or on each ad
            # group, so a campaign bounds no spend only when neither level
            # retained an amount.
            children = ad_groups_by_campaign.get(record.snapshot.resource_ref, [])
            if not any(_has_retained_spend_bound(child) for child in children):
                findings.append(
                    _emit(
                        RULES["TT_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET"],
                        resource_refs=refs,
                        evidence_refs=evidence,
                        window=window,
                        coverage_status=coverage["SPEND"],
                    )
                )
        findings.extend(
            _budget_findings(record, quality, refs, evidence, window, coverage["SPEND"])
        )
        if TIKTOK_ADS_OBJECTIVE_NOT_RETURNED in quality:
            findings.append(
                _emit(
                    RULES["TT_OBJECTIVE_NOT_RETURNED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["OBJECTIVE"],
                )
            )
    return findings


def _ad_group_setting_findings(
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
    coverage: dict[AuditDimension, ProviderCoverageStatus],
) -> list[ProviderAuditFinding]:
    """Budget and bid rules over the retained ad groups."""
    findings: list[ProviderAuditFinding] = []
    for record in grouped["AD_GROUP"]:
        quality = _quality(record)
        refs = (record.snapshot.resource_ref,)
        evidence = (record.record_digest,)
        findings.extend(
            _budget_findings(record, quality, refs, evidence, window, coverage["SPEND"])
        )
        if TIKTOK_ADS_BID_TYPE_NOT_RETURNED in quality:
            findings.append(
                _emit(
                    RULES["TT_BIDS_TYPE_NOT_RETURNED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["BIDS"],
                )
            )
            continue
        bid_type = record.snapshot.normalized_state.get("bid_strategy")
        if (
            isinstance(bid_type, str)
            and bid_type.upper() != _NO_BID_TYPE
            and TIKTOK_ADS_BID_PRICE_ABSENT in quality
        ):
            findings.append(
                _emit(
                    RULES["TT_BIDS_PRICE_ABSENT"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["BIDS"],
                )
            )
    return findings


def _budget_findings(
    record: RetainedSnapshotRecord,
    quality: frozenset[str],
    refs: tuple[str, ...],
    evidence: tuple[str, ...],
    window: AuditDataWindow,
    status: ProviderCoverageStatus,
) -> list[ProviderAuditFinding]:
    """The three budget-mode rules that apply identically to both levels."""
    findings: list[ProviderAuditFinding] = []
    if TIKTOK_ADS_BUDGET_MODE_NOT_RETURNED in quality:
        findings.append(
            _emit(
                RULES["TT_SPEND_BUDGET_MODE_NOT_RETURNED"],
                resource_refs=refs,
                evidence_refs=evidence,
                window=window,
                coverage_status=status,
            )
        )
        return findings
    if TIKTOK_ADS_BUDGET_MODE_INFINITE in quality:
        findings.append(
            _emit(
                RULES["TT_SPEND_INFINITE_BUDGET_MODE"],
                resource_refs=refs,
                evidence_refs=evidence,
                window=window,
                coverage_status=status,
            )
        )
        return findings
    if TIKTOK_ADS_BUDGET_AMOUNT_ABSENT in quality:
        findings.append(
            _emit(
                RULES["TT_SPEND_BUDGET_AMOUNT_ABSENT"],
                resource_refs=refs,
                evidence_refs=evidence,
                window=window,
                coverage_status=status,
            )
        )
    return findings


def _delivery_and_spend_findings(
    report: NormalizedReport,
    grouped: dict[str, list[RetainedSnapshotRecord]],
    report_digest: str,
    window: AuditDataWindow,
) -> list[ProviderAuditFinding]:
    findings: list[ProviderAuditFinding] = []
    account = report.envelope.source_account_ref
    if TIKTOK_ADS_REPORT_COMPLETE_STATUS not in report.envelope.data_quality_statuses:
        findings.append(
            _emit(
                RULES["TT_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED"],
                resource_refs=(account,),
                evidence_refs=(report_digest,),
                window=window,
                coverage_status="PARTIAL",
            )
        )
    for rule_id in (
        "TT_DELIVERY_REPORT_FRESHNESS_NOT_READ",
        "TT_DELIVERY_REPORT_ATTRIBUTION_NOT_READ",
    ):
        findings.append(
            _emit(
                RULES[rule_id],
                resource_refs=(account,),
                evidence_refs=(report_digest,),
                window=window,
                coverage_status="PARTIAL",
            )
        )
    for row in report.performance_rows:
        if row.impressions is None or row.clicks is None:
            findings.append(
                _emit(
                    RULES["TT_DELIVERY_METRICS_MISSING"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
            continue
        if row.clicks > row.impressions:
            findings.append(
                _emit(
                    RULES["TT_DELIVERY_CLICKS_EXCEED_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
        if row.impressions == 0 and _spend_is_positive(row):
            findings.append(
                _emit(
                    RULES["TT_SPEND_WITHOUT_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
    # An AD-grain TikTok report proves ad identifiers only -- the reviewed ad
    # report uses one date dimension and one ad ID and infers no campaign join --
    # so campaign delivery is evaluated from campaign-grain rows alone.
    campaign_impressions: dict[str, int] = {}
    for row in report.performance_rows:
        if row.metric_grain != "CAMPAIGN":
            continue
        campaign_impressions[row.resource_ref] = campaign_impressions.get(row.resource_ref, 0) + (
            row.impressions or 0
        )
    if campaign_impressions:
        for record in grouped["CAMPAIGN"]:
            if not tiktok_ads_declares_serving_intent(record.snapshot.effective_status):
                continue
            if campaign_impressions.get(record.snapshot.resource_ref, 0) > 0:
                continue
            findings.append(
                _emit(
                    RULES["TT_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"],
                    resource_refs=(record.snapshot.resource_ref,),
                    evidence_refs=(record.record_digest, report_digest),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
    return findings


def _account_scoped_findings(
    context: ProviderAuditContext,
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
    coverage: dict[AuditDimension, ProviderCoverageStatus],
) -> list[ProviderAuditFinding]:
    """One finding per unread, undocumented, or retention-limited fact.

    These describe what this build's fixed reads never request and what no
    first-party TikTok source publishes.  That is a property of the read
    contract, not of any one retained record, so they are reported whatever
    evidence was supplied: an audit with no retained inventory at all still
    reports every one of them, anchored to the account the audited report names
    and cited to that report's own record digest.  Only the ones that describe a
    retained *resource* wait for such a resource to exist.
    """
    accounts = grouped["ACCOUNT"]
    resource_refs = (
        (accounts[0].snapshot.resource_ref,)
        if accounts
        else (context.report.envelope.source_account_ref,)
    )
    evidence_refs = (accounts[0].record_digest,) if accounts else (context.source_record_digest,)
    anchored = [
        ("TT_SPEND_SCHEDULE_NOT_READ", True),
        ("TT_BIDS_TYPE_ENUM_NOT_DOCUMENTED_LOCALLY", bool(grouped["AD_GROUP"])),
        ("TT_OBJECTIVE_ENUM_NOT_DOCUMENTED_LOCALLY", bool(grouped["CAMPAIGN"])),
        ("TT_TARGETING_NOT_READ", True),
        ("TT_PLACEMENT_NOT_READ", True),
        ("TT_CREATIVE_ASSOCIATION_NOT_READ", bool(grouped["AD"])),
        ("TT_CREATIVE_CONTENT_NOT_READ", bool(grouped["AD"])),
        ("TT_DESTINATION_VALUES_NOT_READ", bool(grouped["AD"])),
        ("TT_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ", True),
        ("TT_NATIVE_CONVERSION_NOT_READ", True),
        ("TT_BROWSER_NOT_APPLICABLE", True),
        ("TT_POLICY_ACCOUNT_STATUS_NOT_REVIEWED", bool(accounts)),
        ("TT_POLICY_AD_REVIEW_STATE_NOT_READ", True),
        ("TT_POLICY_APP_APPROVAL_AND_PERMISSIONS_NOT_DOCUMENTED", True),
    ]
    findings: list[ProviderAuditFinding] = []
    for rule_id, applies in anchored:
        if not applies:
            continue
        rule = RULES[rule_id]
        findings.append(
            _emit(
                rule,
                resource_refs=resource_refs,
                evidence_refs=evidence_refs,
                window=window,
                coverage_status=coverage[rule.dimension],
            )
        )
    return findings


def _creative_binding_findings(
    context: ProviderAuditContext,
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
    coverage: dict[AuditDimension, ProviderCoverageStatus],
) -> list[ProviderAuditFinding]:
    catalog = context.evidence.creative_catalog
    if catalog is None:
        return []
    findings: list[ProviderAuditFinding] = []
    # "Absent from the retained inventory" is a claim about a read boundary, so
    # it needs one.  With no retained inventory nothing was read and nothing is
    # absent; with an unproven boundary the omission carries no information at
    # all.  A caller-declared subset is weaker than a whole read, so the claim
    # survives only at `LOW` confidence there.
    inventory = context.evidence.inventory
    absence_is_evidence = inventory is not None and _absence_is_evidence(inventory)
    absence_confidence: Confidence = (
        "HIGH"
        if inventory is not None and inventory.completeness == "COMPLETE_BOUNDED_READ"
        else "LOW"
    )
    retained = {record.snapshot.resource_ref: record for record in grouped["AD"]}
    for entry in sorted(catalog.entries, key=lambda item: item.creative_version_id):
        if entry.provider != PROVIDER or entry.provider_resource_ref is None:
            continue
        bound = retained.get(entry.provider_resource_ref)
        if bound is None:
            if not absence_is_evidence:
                continue
            findings.append(
                _emit(
                    RULES["TT_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE"],
                    resource_refs=(entry.creative_version_id, entry.provider_resource_ref),
                    evidence_refs=(entry.record_digest,),
                    window=window,
                    coverage_status=coverage["CREATIVE"],
                    confidence=absence_confidence,
                )
            )
            continue
        findings.append(
            _emit(
                RULES["TT_CREATIVE_LOCAL_BINDING_UNVERIFIED"],
                resource_refs=(entry.creative_version_id, entry.provider_resource_ref),
                evidence_refs=(entry.record_digest, bound.record_digest),
                window=window,
                coverage_status=coverage["CREATIVE"],
            )
        )
    return findings


_NO_STATE_SHAPE_VERSION: Final = "NOT_RECORDED"


def _state_shape_action(before: RetainedSnapshotRecord, after: RetainedSnapshotRecord) -> str:
    """The suggested action for one local state-shape difference, with its evidence.

    The two retained connector state-shape versions are the whole reason this
    difference can be resolved at all, so the action names them instead of
    telling a reader to go and find them.  A snapshot written before this
    product recorded one reports `NOT_RECORDED`, which is itself the answer: the
    older read predates the version marker, so a retention change is the likelier
    explanation.
    """
    first = _state_shape_version(before) or _NO_STATE_SHAPE_VERSION
    second = _state_shape_version(after) or _NO_STATE_SHAPE_VERSION
    difference = (
        "The two reads declare different connector state-shape versions, so this product changed "
        "what it retains between them."
        if first != second
        else "Both reads declare the same connector state-shape version, so a value became absent "
        "rather than a field being added or dropped."
    )
    return (
        f"Retained connector state-shape versions: earlier {first}, later {second}. {difference} "
        "Only fields both reads returned are compared as values; nothing here claims a change at "
        "TikTok Ads."
    )


def _state_shape_version(record: RetainedSnapshotRecord) -> str | None:
    """The connector state-shape version a retained snapshot declares, if any.

    Snapshots written before this product recorded one carry none.  The value
    lives in `data_quality_statuses`, never in `normalized_state`, so recording
    it creates no drift of its own.
    """
    versions = sorted(
        status.removeprefix(TIKTOK_ADS_STATE_SHAPE_PREFIX)
        for status in record.snapshot.data_quality_statuses
        if status.startswith(TIKTOK_ADS_STATE_SHAPE_PREFIX)
    )
    return versions[0] if len(versions) == 1 else None


def _state_deltas(
    before: RetainedSnapshotRecord, after: RetainedSnapshotRecord
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a snapshot pair's differences into value changes and shape changes.

    The shared `snapshot_state_deltas` compares `normalized_state.get(field)`,
    which reads a field absent from one side as a differing value.  Two things
    produce that absence and this product cannot tell them apart from the
    records alone: a connector release that began or stopped retaining the
    field, and a value that became absent, because `_snapshot` drops a `None`.

    Publishing either as `TIKTOK_OUT_OF_BAND_STATE_CHANGE_OBSERVED` would assert
    a change at TikTok inside a recorded interval that this product never
    observed, which AGENTS.md rule 24 forbids.  The first returned tuple is
    therefore only the fields **both** reads returned whose values differ, plus
    `parent_resource_refs` when the parent chain differs; the second is the
    key-set difference, reported as a local state-shape difference instead.
    """
    first, second = before.snapshot.normalized_state, after.snapshot.normalized_state
    shared = set(first) & set(second)
    value_paths = [
        f"normalized_state.{field}" for field in sorted(shared) if first[field] != second[field]
    ]
    if before.snapshot.parent_resource_refs != after.snapshot.parent_resource_refs:
        value_paths.append("parent_resource_refs")
    shape_paths = tuple(f"normalized_state.{field}" for field in sorted(set(first) ^ set(second)))
    return tuple(value_paths), shape_paths


def _drift(
    context: ProviderAuditContext, window: AuditDataWindow, status: ProviderCoverageStatus
) -> tuple[list[ProviderAuditFinding], list[DriftObservation]]:
    later, earlier = context.evidence.inventory, context.evidence.earlier_inventory
    if later is None or earlier is None:
        return [], []
    # A set difference between two record sets is a real observation only when
    # both sets have a proven read boundary.  Without one, the same comparison
    # fabricates deletions for resources an interrupted run never reached and
    # creations for resources an interrupted baseline never held.  A
    # per-resource state comparison stays valid either way, so only the absence
    # rules stop.
    absence_is_evidence = _absence_is_evidence(later) and _absence_is_evidence(earlier)
    absence_confidence: Confidence = (
        "MEDIUM"
        if later.completeness == "COMPLETE_BOUNDED_READ"
        and earlier.completeness == "COMPLETE_BOUNDED_READ"
        else "LOW"
    )
    before = {record.snapshot.resource_ref: record for record in earlier.records}
    after = {record.snapshot.resource_ref: record for record in later.records}
    findings: list[ProviderAuditFinding] = []
    observations: list[DriftObservation] = []
    for resource_ref in sorted(set(before) | set(after)):
        first, second = before.get(resource_ref), after.get(resource_ref)
        if first is not None and second is not None:
            value_paths, shape_paths = _state_deltas(first, second)
            if shape_paths:
                findings.append(
                    _emit(
                        RULES["TT_DRIFT_LOCAL_STATE_SHAPE_CHANGED"],
                        resource_refs=(resource_ref,),
                        evidence_refs=(first.record_digest, second.record_digest),
                        window=window,
                        coverage_status=status,
                        suggested_action=_state_shape_action(first, second),
                    )
                )
            if not value_paths:
                continue
            finding = _emit(
                RULES["TT_DRIFT_STATE_CHANGED"],
                resource_refs=(resource_ref,),
                evidence_refs=(first.record_digest, second.record_digest),
                window=window,
                coverage_status=status,
            )
            findings.append(finding)
            observations.append(
                DriftObservation(
                    resource_type=second.snapshot.resource_type,
                    resource_ref=resource_ref,
                    change_kind="STATE_CHANGED",
                    changed_paths=value_paths,
                    before_record_digest=first.record_digest,
                    after_record_digest=second.record_digest,
                    observation_interval_start=first.observed_at,
                    observation_interval_end=second.observed_at,
                    finding_id=finding.finding.finding_id,
                )
            )
            continue
        if not absence_is_evidence:
            continue
        if first is not None:
            finding = _emit(
                RULES["TT_DRIFT_ABSENT_FROM_LATER_INVENTORY"],
                resource_refs=(resource_ref,),
                evidence_refs=(first.record_digest,),
                window=window,
                coverage_status=status,
                confidence=absence_confidence,
            )
            findings.append(finding)
            observations.append(
                DriftObservation(
                    resource_type=first.snapshot.resource_type,
                    resource_ref=resource_ref,
                    change_kind="RESOURCE_ABSENT_FROM_LATER_INVENTORY",
                    before_record_digest=first.record_digest,
                    observation_interval_start=first.observed_at,
                    observation_interval_end=later.observed_at,
                    finding_id=finding.finding.finding_id,
                )
            )
            continue
        assert second is not None
        finding = _emit(
            RULES["TT_DRIFT_ABSENT_FROM_EARLIER_INVENTORY"],
            resource_refs=(resource_ref,),
            evidence_refs=(second.record_digest,),
            window=window,
            coverage_status=status,
            confidence=absence_confidence,
        )
        findings.append(finding)
        observations.append(
            DriftObservation(
                resource_type=second.snapshot.resource_type,
                resource_ref=resource_ref,
                change_kind="RESOURCE_ABSENT_FROM_EARLIER_INVENTORY",
                after_record_digest=second.record_digest,
                observation_interval_start=earlier.observed_at,
                observation_interval_end=second.observed_at,
                finding_id=finding.finding.finding_id,
            )
        )
    return findings, observations


def _local_bindings(context: ProviderAuditContext) -> int:
    """How many local creative bindings declare a TikTok Ads resource."""
    catalog = context.evidence.creative_catalog
    if catalog is None:
        return 0
    return sum(
        1
        for entry in catalog.entries
        if entry.provider == PROVIDER and entry.provider_resource_ref is not None
    )


# The read surfaces that back each audit dimension.  `TIKTOK_ADS_READ_SURFACES`
# is the one declaration of what this build's fixed TikTok reads return, so the
# ceiling for a dimension is derived from it here rather than restated as a
# literal that could drift away from the adapter.  `BROWSER_MEASUREMENT` has no
# entry: no provider read could back it, and it is always `NOT_APPLICABLE`.
#
# `CREATIVE` is backed by `AD_INVENTORY` alone.  That is deliberate and narrow:
# a retained ad snapshot can prove only that the resource a local binding names
# was returned by a bounded read.  Creative association, video presence and ad
# copy are separately declared unread, and the rule table says so out loud.
_DIMENSION_SURFACES: Final[dict[AuditDimension, tuple[str, ...]]] = {
    "STRUCTURE": ("CAMPAIGN_INVENTORY", "AD_GROUP_INVENTORY", "AD_INVENTORY"),
    "DELIVERY": ("CAMPAIGN_DELIVERY_REPORT", "AD_DELIVERY_REPORT"),
    "SPEND": (
        "CAMPAIGN_BUDGET_MODE",
        "CAMPAIGN_BUDGET",
        "AD_GROUP_BUDGET_MODE",
        "AD_GROUP_BUDGET",
        "AD_GROUP_SCHEDULE",
    ),
    "BIDS": ("AD_GROUP_BID_TYPE", "AD_GROUP_BID_PRICE", "AD_GROUP_OPTIMIZATION_GOAL"),
    "OBJECTIVE": ("CAMPAIGN_OBJECTIVE",),
    "TARGETING": ("AD_GROUP_TARGETING_PRESENCE", "AD_GROUP_TARGETING_VALUES"),
    "PLACEMENT": ("AD_GROUP_PLACEMENT",),
    "CREATIVE": ("AD_INVENTORY",),
    "DESTINATION_CONFIGURATION": ("AD_DESTINATION_URL_VALUE",),
    "TRACKING": ("PIXEL_AND_EVENT_HEALTH",),
    "NATIVE_CONVERSION": ("NATIVE_CONVERSION_METRICS",),
    "POLICY": ("ACCOUNT_STATUS", "AD_REVIEW_STATE", "SECONDARY_STATUS"),
    "DRIFT": ("CAMPAIGN_INVENTORY", "AD_GROUP_INVENTORY", "AD_INVENTORY"),
}
_IMPLEMENTED_SURFACES: Final[frozenset[str]] = frozenset(
    {"IMPLEMENTED_FIXED_READ", "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY"}
)


def _surface_backed(dimension: AuditDimension) -> bool:
    """Whether any fixed read this build implements backs one dimension."""
    return any(
        TIKTOK_ADS_READ_SURFACES[surface] in _IMPLEMENTED_SURFACES
        for surface in _DIMENSION_SURFACES.get(dimension, ())
    )


def _coverage_map(
    context: ProviderAuditContext, grouped: dict[str, list[RetainedSnapshotRecord]]
) -> dict[AuditDimension, ProviderCoverageStatus]:
    """Give every dimension one status: the evidence supplied, capped by the read.

    The evidence expressions below say what the retained records could support.
    `TIKTOK_ADS_READ_SURFACES` then caps each one: a dimension no implemented
    fixed read backs is `NOT_READ` whatever a caller supplies, so `TARGETING`,
    `PLACEMENT`, `DESTINATION_CONFIGURATION`, `TRACKING` and `NATIVE_CONVERSION`
    cannot become readable by accident.
    """
    inventory = context.evidence.inventory
    earlier = context.evidence.earlier_inventory
    has_inventory = inventory is not None and bool(inventory.records)
    complete = (
        has_inventory
        and inventory is not None
        and (inventory.completeness == "COMPLETE_BOUNDED_READ")
    )
    has_report_rows = bool(context.report.performance_rows)
    has_accounts = bool(grouped["ACCOUNT"])
    has_campaigns = bool(grouped["CAMPAIGN"])
    has_ad_groups = bool(grouped["AD_GROUP"])
    has_ads = bool(grouped["AD"])
    drift_is_covered = (
        earlier is not None
        and inventory is not None
        and earlier.completeness == "COMPLETE_BOUNDED_READ"
        and inventory.completeness == "COMPLETE_BOUNDED_READ"
    )
    evaluated: dict[AuditDimension, ProviderCoverageStatus] = {
        "STRUCTURE": "COVERED" if complete else "PARTIAL" if has_inventory else "NOT_READ",
        # Never COVERED: the shared audit vocabulary has no exhaustive
        # `AnalyticsCompleteness` member, so no retained report may be reported
        # as the whole window even when the adapter walked every page.
        "DELIVERY": "PARTIAL" if has_report_rows else "NOT_READ",
        "SPEND": ("PARTIAL" if has_report_rows or has_campaigns or has_ad_groups else "NOT_READ"),
        "BIDS": "PARTIAL" if has_ad_groups else "NOT_READ",
        "OBJECTIVE": "PARTIAL" if has_campaigns else "NOT_READ",
        "TARGETING": "PARTIAL" if has_ad_groups else "NOT_READ",
        "PLACEMENT": "PARTIAL" if has_ad_groups else "NOT_READ",
        "CREATIVE": "PARTIAL" if has_ads or _local_bindings(context) else "NOT_READ",
        "DESTINATION_CONFIGURATION": "PARTIAL" if has_ads else "NOT_READ",
        "TRACKING": "PARTIAL" if has_campaigns else "NOT_READ",
        "NATIVE_CONVERSION": "PARTIAL" if has_report_rows else "NOT_READ",
        "BROWSER_MEASUREMENT": "NOT_APPLICABLE",
        "POLICY": "PARTIAL" if has_ads or has_accounts else "NOT_READ",
        "DRIFT": (
            "COVERED" if drift_is_covered else "PARTIAL" if earlier is not None else "NOT_READ"
        ),
    }
    return {
        dimension: (
            status if status == "NOT_APPLICABLE" or _surface_backed(dimension) else "NOT_READ"
        )
        for dimension, status in evaluated.items()
    }


_RATIONALE: Final[dict[AuditDimension, str]] = {
    "STRUCTURE": "Advertiser, campaign, ad-group and ad identity, parents and the operator-set operation status are read by the fixed v1.3 inventory endpoints, whose numbered page walk validates the provider's declared total on every page.",
    "DELIVERY": "Impressions and clicks come from the fixed synchronous daily report, whose retained result is never labelled exhaustive by this audit vocabulary.",
    "SPEND": "Retained spend rows, campaign and ad-group budget modes and exact decimal budgets are evaluated as retained native minor units; nothing is rounded. No schedule field is requested at all.",
    "BIDS": "The ad-group bid type and bid price are retained as presence facts; TikTok publishes no bid-type vocabulary this build could fetch, so neither is checked against an enumeration. No optimization goal is requested at all, so what an ad group optimizes towards is unread here and never inferred from a retained bid.",
    "OBJECTIVE": "The campaign objective is read as a presence fact only; TikTok publishes no objective vocabulary this build could fetch.",
    "TARGETING": "The reviewed fixed ad-group read requests no targeting field, and no first-party source enumerates a returnable one.",
    "PLACEMENT": "The reviewed fixed ad-group read requests no placement field, and the fixed report requests no placement facet.",
    "CREATIVE": "Ad identity and the operator-set operation status are read; the creative association, video reference, ad copy and call to action are not requested at all.",
    "DESTINATION_CONFIGURATION": "The reviewed fixed read requests no landing-page or tracking-parameter field, so no destination value is retained.",
    "TRACKING": "The reviewed fixed read requests no pixel, event or Events API field.",
    "NATIVE_CONVERSION": "The fixed report request projects impressions, clicks and spend only; no action or conversion metric is requested.",
    "BROWSER_MEASUREMENT": "A local audit executes no browser and loads no page.",
    "POLICY": "The advertiser status is read but has no reviewed enumeration; `secondary_status`, ad review state, the application's approval and its selected endpoint permissions are not documented locally at all.",
    "DRIFT": "Drift compares two retained snapshot sets for one advertiser; it needs both to be supplied.",
}
_UNPROVEN_BOUNDARY_RATIONALE: Final = (
    " The retained record set has no proven read boundary, so what it omits proves nothing."
)
_SUBSET_RATIONALE: Final = (
    " The caller declared this record set a subset, so an absence is weak evidence."
)
# Suppressing a rule is an omission, so the coverage entry says which rules did
# not run rather than leaving a quieter result to look like a cleaner one.
_DRIFT_ABSENCE_SUPPRESSED: Final = (
    " Resource appearance and disappearance were therefore not evaluated at all;"
    " only per-resource state changes were compared."
)
# Every dimension a retained inventory can back.  `TARGETING`, `PLACEMENT`,
# `DESTINATION_CONFIGURATION` and `TRACKING` are capped to `NOT_READ` by
# `TIKTOK_ADS_READ_SURFACES` today, so they cite nothing; they are still listed
# so that promoting one of those surfaces names its evidence rather than
# producing a covered dimension with no source.
_INVENTORY_DIMENSIONS: Final[frozenset[AuditDimension]] = frozenset(
    {
        "STRUCTURE",
        "SPEND",
        "BIDS",
        "OBJECTIVE",
        "TARGETING",
        "PLACEMENT",
        "DESTINATION_CONFIGURATION",
        "TRACKING",
        "POLICY",
        "DRIFT",
    }
)


def _coverage(
    context: ProviderAuditContext,
    statuses: dict[AuditDimension, ProviderCoverageStatus],
    grouped: dict[str, list[RetainedSnapshotRecord]],
) -> tuple[ProviderAuditCoverage, ...]:
    """Name, for every dimension, exactly the evidence it was evaluated from."""
    inventory = context.evidence.inventory
    earlier = context.evidence.earlier_inventory
    bindings = _local_bindings(context)
    result: list[ProviderAuditCoverage] = []
    for dimension in _DIMENSION_ORDER:
        status = statuses[dimension]
        sources: list[EvidenceSource] = []
        refs: tuple[str, ...] = ()
        count = 0
        rationale = _RATIONALE[dimension]
        if status in {"COVERED", "PARTIAL"}:
            if dimension in {"DELIVERY", "SPEND", "NATIVE_CONVERSION"} and (
                context.report.performance_rows
            ):
                sources.append("PERFORMANCE_OBSERVATION")
                refs = (context.source_record_digest,)
                count += 1
            if dimension in _INVENTORY_DIMENSIONS and inventory is not None:
                sources.append("RETAINED_INVENTORY")
                count += len(inventory.records)
            if dimension == "CREATIVE":
                if grouped["AD"] and inventory is not None:
                    sources.append("RETAINED_INVENTORY")
                    count += len(grouped["AD"])
                if bindings:
                    sources.append("LOCAL_CREATIVE_CATALOG")
                    count += bindings
            if dimension == "DRIFT" and earlier is not None:
                sources.append("EARLIER_RETAINED_INVENTORY")
                count += len(earlier.records)
            # Drift reasons over a pair, so either side's boundary can narrow it.
            considered = (
                ([inventory, earlier] if dimension == "DRIFT" else [inventory])
                if dimension in {"STRUCTURE", "DRIFT"}
                else []
            )
            declared = [item.completeness for item in considered if item is not None]
            if "READ_BOUNDARY_UNVERIFIED" in declared:
                rationale += _UNPROVEN_BOUNDARY_RATIONALE
                if dimension == "DRIFT":
                    rationale += _DRIFT_ABSENCE_SUPPRESSED
            elif "BOUNDED_NON_EXHAUSTIVE" in declared:
                rationale += _SUBSET_RATIONALE
        result.append(
            ProviderAuditCoverage(
                dimension=dimension,
                status=status,
                evidence_sources=tuple(sources),
                evidence_record_count=count,
                evidence_refs=refs,
                rationale=rationale,
            )
        )
    return tuple(result)


def _ids(findings: list[ProviderAuditFinding], rule_ids: set[str]) -> tuple[str, ...]:
    return tuple(sorted(item.finding.finding_id for item in findings if item.rule_id in rule_ids))


def _bound(
    identifiers: tuple[str, ...],
    rule_ids: set[str],
    *,
    scope: Literal["RECOMMENDATION_FINDING_IDS", "DECLINED_FINDING_IDS"],
    subject: str,
    exceeded: list[BoundExceeded],
) -> tuple[str, ...]:
    """Inline the first identifiers, and record it when there are more."""
    if len(identifiers) <= MAXIMUM_INLINE_FINDING_IDS:
        return identifiers
    exceeded.append(
        BoundExceeded(
            scope=scope,
            subject=subject,
            bound=MAXIMUM_INLINE_FINDING_IDS,
            total_finding_count=len(identifiers),
            inlined_finding_count=MAXIMUM_INLINE_FINDING_IDS,
            omitted_finding_count=len(identifiers) - MAXIMUM_INLINE_FINDING_IDS,
            rule_ids=tuple(sorted(rule_ids)),
        )
    )
    return identifiers[:MAXIMUM_INLINE_FINDING_IDS]


# Every rule identifier in `RULES` belongs to exactly one group below, so no
# finding can be produced that neither a recommendation nor an explicit declined
# outcome accounts for.  `tests/test_tiktok_audit.py` pins the partition.
_OBSERVE_DELIVERY_ABSENCE: Final[set[str]] = {"TT_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"}
_OBSERVE_INCONSISTENT_ROWS: Final[set[str]] = {
    "TT_SPEND_WITHOUT_IMPRESSIONS",
    "TT_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
    "TT_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
}
_OBSERVE_DRIFT: Final[set[str]] = {
    "TT_DRIFT_STATE_CHANGED",
    "TT_DRIFT_ABSENT_FROM_LATER_INVENTORY",
    "TT_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
}
_OBSERVE_UNBOUNDED_SPEND: Final[set[str]] = {
    "TT_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET",
    "TT_SPEND_INFINITE_BUDGET_MODE",
    "TT_SPEND_BUDGET_AMOUNT_ABSENT",
}
_OBSERVE_UNPROVEN_READ_BOUNDARY: Final[set[str]] = {"TT_STRUCTURE_PAGINATION_TOTAL_NOT_DECLARED"}
_DECLINED_PAUSE: Final[set[str]] = {"TT_STRUCTURE_SERVING_INTENT_CONTRADICTS_PARENT"}
_DECLINED_CREATE_CHILD: Final[set[str]] = {
    "TT_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP",
    "TT_STRUCTURE_AD_GROUP_WITHOUT_AD",
}
_DECLINED_UPDATE: Final[set[str]] = {
    "TT_SPEND_BUDGET_MODE_NOT_RETURNED",
    "TT_BIDS_PRICE_ABSENT",
}
_DECLINED_EVIDENCE: Final[set[str]] = {
    "TT_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED",
    "TT_STRUCTURE_AD_PARENT_NOT_RETAINED",
    "TT_STRUCTURE_STATUS_NOT_REVIEWED",
    "TT_DELIVERY_METRICS_MISSING",
    "TT_OBJECTIVE_NOT_RETURNED",
    "TT_BIDS_TYPE_NOT_RETURNED",
    "TT_CREATIVE_LOCAL_BINDING_UNVERIFIED",
    "TT_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
    "TT_POLICY_ACCOUNT_STATUS_NOT_REVIEWED",
    "TT_DRIFT_LOCAL_STATE_SHAPE_CHANGED",
}
_DECLINED_NOT_READ: Final[set[str]] = {
    "TT_DELIVERY_REPORT_FRESHNESS_NOT_READ",
    "TT_DELIVERY_REPORT_ATTRIBUTION_NOT_READ",
    "TT_SPEND_SCHEDULE_NOT_READ",
    "TT_BIDS_TYPE_ENUM_NOT_DOCUMENTED_LOCALLY",
    "TT_OBJECTIVE_ENUM_NOT_DOCUMENTED_LOCALLY",
    "TT_TARGETING_NOT_READ",
    "TT_PLACEMENT_NOT_READ",
    "TT_CREATIVE_ASSOCIATION_NOT_READ",
    "TT_CREATIVE_CONTENT_NOT_READ",
    "TT_DESTINATION_VALUES_NOT_READ",
    "TT_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ",
    "TT_NATIVE_CONVERSION_NOT_READ",
    "TT_BROWSER_NOT_APPLICABLE",
    "TT_POLICY_AD_REVIEW_STATE_NOT_READ",
    "TT_POLICY_APP_APPROVAL_AND_PERMISSIONS_NOT_DOCUMENTED",
}
RULE_OUTCOME_GROUPS: Final[tuple[frozenset[str], ...]] = tuple(
    frozenset(group)
    for group in (
        _OBSERVE_DELIVERY_ABSENCE,
        _OBSERVE_INCONSISTENT_ROWS,
        _OBSERVE_DRIFT,
        _OBSERVE_UNBOUNDED_SPEND,
        _OBSERVE_UNPROVEN_READ_BOUNDARY,
        _DECLINED_PAUSE,
        _DECLINED_CREATE_CHILD,
        _DECLINED_UPDATE,
        _DECLINED_EVIDENCE,
        _DECLINED_NOT_READ,
    )
)


def _recommend(
    findings: list[ProviderAuditFinding], context: ProviderAuditContext
) -> tuple[list[ProviderRecommendation], list[NoRecommendation], list[BoundExceeded]]:
    expires = context.as_of + timedelta(seconds=context.policy.maximum_observation_age_seconds)
    recommendations: list[ProviderRecommendation] = []
    absences: list[NoRecommendation] = []
    exceeded: list[BoundExceeded] = []

    def _propose(
        rule_ids: set[str],
        *,
        operation_kind: RecommendedOperationKind,
        hypothesis: str,
        expected_impact: str,
        assumptions: tuple[str, ...],
        confidence: Confidence,
        risk: Risk,
        completeness: AnalyticsCompleteness,
    ) -> None:
        matched = _ids(findings, rule_ids)
        if not matched:
            return
        bound = _bound(
            matched,
            rule_ids,
            scope="RECOMMENDATION_FINDING_IDS",
            # The proposed groups are disjoint, so the first rule identifier in
            # content order names the exact row inside the subject's fixed size
            # bound, and the row's own `rule_ids` carries the complete
            # membership.  Every proposed row here is `OBSERVE`, so the operation
            # kind alone would not distinguish two of them.
            subject=f"{operation_kind}:{min(rule_ids)}",
            exceeded=exceeded,
        )
        selected = [item for item in findings if item.rule_id in rule_ids]
        observations = tuple(sorted({item.finding.observation for item in selected}))[:8]
        recommendations.append(
            ProviderRecommendation(
                recommendation=build_recommendation(
                    hypothesis=hypothesis,
                    evidence_refs=(context.source_record_digest,),
                    observations=observations,
                    assumptions=assumptions,
                    confidence=confidence,
                    expected_impact=expected_impact,
                    risk=risk,
                    expires_at=expires,
                    identity={
                        "rule_table_version": RULE_TABLE_VERSION,
                        "finding_ids": bound,
                        "operation_kind": operation_kind,
                    },
                ),
                finding_ids=bound,
                bound_finding_count=len(matched),
                bound_rule_ids=tuple(sorted(rule_ids)),
                operation_kind=operation_kind,
                provider=PROVIDER,
                analytics_completeness=completeness,
            )
        )

    def _no_recommendation(
        rule_ids: set[str],
        *,
        reason: NoRecommendationReason,
        detail: str,
        considered: RecommendedOperationKind | None = None,
    ) -> None:
        matched = _ids(findings, rule_ids)
        if not matched:
            return
        absences.append(
            NoRecommendation(
                finding_ids=_bound(
                    matched,
                    rule_ids,
                    scope="DECLINED_FINDING_IDS",
                    # Neither the reason nor the considered operation kind is
                    # unique on its own.  A declined group's rule identifiers are
                    # disjoint from every other group's, so the first one in
                    # content order identifies the exact declined row within the
                    # subject's fixed size bound.
                    subject=f"{reason}:{considered or 'NONE'}:{min(rule_ids)}",
                    exceeded=exceeded,
                ),
                bound_finding_count=len(matched),
                bound_rule_ids=tuple(sorted(rule_ids)),
                reason=reason,
                detail=detail,
                considered_operation_kind=considered,
            )
        )

    _propose(
        _OBSERVE_DELIVERY_ABSENCE,
        operation_kind="OBSERVE",
        hypothesis="A repeated bounded campaign report may show delivery this retained report did not return.",
        expected_impact="A second compatible read establishes whether the absence of rows reflects real non-delivery.",
        assumptions=(
            "The absence of a row in a retained report is not evidence of zero delivery.",
        ),
        confidence="LOW",
        risk="LOW",
        completeness=_BOUNDED,
    )
    _propose(
        _OBSERVE_INCONSISTENT_ROWS,
        operation_kind="OBSERVE",
        hypothesis="A repeated bounded read over the same declared window may resolve this inconsistent or unbounded retained result.",
        expected_impact="A second compatible read establishes whether the inconsistency persists before any decision uses the row.",
        assumptions=(
            "A single inconsistent aggregate row is not evidence of a delivery or billing defect.",
        ),
        confidence="MEDIUM",
        risk="LOW",
        completeness=_BOUNDED,
    )
    _propose(
        _OBSERVE_DRIFT,
        operation_kind="OBSERVE",
        hypothesis="A fresh bounded inventory read establishes the current state of the resources that changed out of band.",
        expected_impact="A current snapshot bounds the observation interval before any later work relies on the old state.",
        assumptions=(
            "The actor and the exact time of an out-of-band change are unknown and are not inferred.",
        ),
        confidence="HIGH",
        risk="LOW",
        completeness=_NOT_ANALYTICS,
    )
    # This is where the Meta and Google tables propose a compiled `PAUSE`. TikTok
    # has no pause route in this build at all, so an unbounded-spend finding
    # yields a re-read and nothing else. The declined `PAUSE` row below says so
    # explicitly rather than leaving the omission to be inferred.
    _propose(
        _OBSERVE_UNBOUNDED_SPEND,
        operation_kind="OBSERVE",
        hypothesis="A fresh bounded hierarchy read establishes where, if anywhere, the spend of these TikTok resources is bounded.",
        expected_impact="A current snapshot shows whether a budget exists at the campaign or the ad-group level before any spend decision is taken. Nothing here changes a budget or pauses anything.",
        assumptions=(
            "A retained read that returned no budget amount is not proof that the resource has none.",
            "A declared serving-intent operation status is an intent to serve, not an observation of delivery.",
            "The documented unlimited budget mode bounds no spend in the record that declares it; a bound may still exist at another level.",
        ),
        confidence="MEDIUM",
        risk="LOW",
        completeness=_NOT_ANALYTICS,
    )
    _propose(
        _OBSERVE_UNPROVEN_READ_BOUNDARY,
        operation_kind="OBSERVE",
        hypothesis="A bounded inventory read with the current connector records that the provider's declared total was proved for every page.",
        expected_impact="A re-read replaces a record set whose completeness is unknown with one whose read boundary is recorded.",
        assumptions=(
            "What a record set without a proven read boundary omits carries no information at all.",
        ),
        confidence="HIGH",
        risk="LOW",
        completeness=_NOT_ANALYTICS,
    )
    _no_recommendation(
        _DECLINED_PAUSE,
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail=(
            "Reconciling a serving-intent contradiction would require pausing a TikTok resource. "
            "This build compiles no TikTok pause, status update, or any other change to an "
            "existing TikTok resource, so no pause is proposed. The contradiction is reported "
            "and left to the operator."
        ),
        considered="PAUSE",
    )
    _no_recommendation(
        _DECLINED_CREATE_CHILD,
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail=(
            "The offline TikTok compiler produces only a complete new non-serving campaign, "
            "ad-group and single-video ad candidate; it cannot compile a child into an existing "
            "retained parent, and that candidate is explicitly not executable."
        ),
        considered="CREATE",
    )
    _no_recommendation(
        _DECLINED_UPDATE,
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail=(
            "Reconciling a budget mode or a bid would require a TikTok update operation, which "
            "this build cannot compile."
        ),
        considered="UPDATE",
    )
    _no_recommendation(
        _DECLINED_EVIDENCE,
        reason="EVIDENCE_INSUFFICIENT",
        detail=(
            "These findings record missing, unreviewed, or unverifiable evidence. Resolving them "
            "needs a documentation review or a further read, not an advertising operation."
        ),
    )
    _no_recommendation(
        _DECLINED_NOT_READ,
        reason="COVERAGE_NOT_READ",
        detail=(
            "These dimensions are unread, presence-only, or not documented locally at all in this "
            "build's fixed TikTok read. An unread dimension yields no recommendation and is never "
            "reported as healthy."
        ),
    )
    return recommendations, absences, exceeded


def evaluate(context: ProviderAuditContext) -> ProviderAuditSections:
    """Evaluate the TikTok Ads rule table over retained records only."""
    if context.provider != PROVIDER:
        raise ValueError("The TikTok rule table evaluates TikTok Ads evidence only")
    window = _window(context.report)
    inventory = context.evidence.inventory
    grouped = _by_type(inventory)
    statuses = _coverage_map(context, grouped)
    findings: list[ProviderAuditFinding] = []
    # The read-contract disclosures describe the fixed read, not one retained
    # record, so they are reported whatever evidence was supplied.
    findings.extend(_account_scoped_findings(context, grouped, window, statuses))
    if inventory is not None and inventory.records:
        findings.extend(_structure_findings(grouped, inventory, window, statuses["STRUCTURE"]))
        findings.extend(_campaign_setting_findings(grouped, window, statuses))
        findings.extend(_ad_group_setting_findings(grouped, window, statuses))
    findings.extend(_creative_binding_findings(context, grouped, window, statuses))
    if context.report.performance_rows:
        findings.extend(
            _delivery_and_spend_findings(
                context.report, grouped, context.source_record_digest, window
            )
        )
    drift_findings, observations = _drift(context, window, statuses["DRIFT"])
    findings.extend(drift_findings)
    recommendations, absences, exceeded = _recommend(findings, context)
    findings.sort(key=lambda item: item.finding.finding_id)
    recommendations.sort(key=lambda item: item.recommendation.recommendation_id)
    absences.sort(key=lambda item: (item.reason, item.finding_ids))
    observations.sort(key=lambda item: (item.resource_ref, item.change_kind))
    return ProviderAuditSections(
        provider=PROVIDER,
        rule_table_version=RULE_TABLE_VERSION,
        # No reviewed TikTok analytics read can be published as exhaustive by
        # this vocabulary, so a retained report is always bounded here.
        analytics_completeness=_BOUNDED,
        coverage=_coverage(context, statuses, grouped),
        findings=tuple(findings),
        recommendations=tuple(recommendations),
        no_recommendations=tuple(absences),
        drift_observations=tuple(observations),
        bound_exceeded=tuple(exceeded),
    )


register_provider_rule(PROVIDER, evaluate)
