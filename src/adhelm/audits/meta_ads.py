# ruff: noqa: E501
"""Deterministic Meta Ads audit rules over retained local records.

This module makes **no provider request**.  It evaluates the retained
`PERFORMANCE_OBSERVATION` report the caller selected, the retained
`RESOURCE_SNAPSHOT` set a completed inventory read appended, an optional
earlier snapshot set, and an optional local creative catalog.  Authorization,
quota, 401/403, and Graph rate-limit handling therefore stays entirely with the
read adapters that produced those records; nothing here can leak a response
body because no response is available here.

Four honesty bounds shape every rule:

* The reviewed Meta inventory and insights reads both consume documented
  `paging.cursors.after` pagination and fail closed with an explicit
  `RESULT_LIMIT_REQUIRED` or `PAGINATION_LIMIT` error rather than returning a
  partial collection.  That is a property of the adapter, not of a record set
  handed to this module, and the shared audit vocabulary in :mod:`adhelm.audits`
  has no exhaustive `AnalyticsCompleteness` member at all.  Every
  analytics-derived finding therefore stays `BOUNDED_NON_EXHAUSTIVE` and never
  claims a total.
* **Presence is not content.**  Ad-set targeting and ad creative are read as
  presence facts only -- whether a targeting object, a geography, an age bound,
  or an interest was returned, and whether a creative, an image hash, or a link
  was returned.  No targeting value, image hash, destination URL, headline, or
  body copy enters a local record, so those dimensions are `PARTIAL` at best
  and are never reported as healthy.
* Placement, pixel/event health, native conversion metrics, and report
  freshness are not in the reviewed fixed read, so their dimensions are
  `NOT_READ`.  That ceiling is read from `META_ADS_READ_SURFACES` rather than
  restated here, so a dimension can never be reported as read because a caller
  supplied records for it.
* Meta documents `effective_status` as already folding a parent's status into
  a child's, so this table has no parent/child serving-intent contradiction
  rule: the contradiction OpenAI's table reports cannot arise in a Meta read.
  It reports the documented propagation values (`CAMPAIGN_PAUSED`,
  `ADSET_PAUSED`) as an explicit observation instead.

`docs/implementation/meta-ads-audit-recommendations.md` carries the rule table,
coverage map, and evidence limits in prose.
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
from adhelm.providers.meta_ads import (
    META_ADS_AD_REVIEW_ATTENTION,
    META_ADS_AD_REVIEW_BLOCKED,
    META_ADS_AD_REVIEW_PENDING,
    META_ADS_BID_STRATEGY_NOT_RETURNED,
    META_ADS_BID_STRATEGY_NOT_REVIEWED,
    META_ADS_BUDGET_MODE_DAILY_AND_LIFETIME,
    META_ADS_BUDGET_MODE_LIFETIME,
    META_ADS_BUYING_TYPE_NOT_RETURNED,
    META_ADS_BUYING_TYPE_RESERVED,
    META_ADS_CREATIVE_IMAGE_HASH_ABSENT,
    META_ADS_CREATIVE_LINK_ABSENT,
    META_ADS_CREATIVE_NOT_ASSOCIATED,
    META_ADS_CREATIVE_VALUE_WITHHELD,
    META_ADS_OBJECTIVE_NOT_RETURNED,
    META_ADS_OBJECTIVE_NOT_REVIEWED,
    META_ADS_READ_SURFACES,
    META_ADS_REPORT_COMPLETE_STATUS,
    META_ADS_REVIEWED_AD_SET_STATUSES,
    META_ADS_REVIEWED_AD_STATUSES,
    META_ADS_REVIEWED_CAMPAIGN_STATUSES,
    META_ADS_SCHEDULE_END_ABSENT,
    META_ADS_STATE_SHAPE_PREFIX,
    META_ADS_TARGETING_AGE_ABSENT,
    META_ADS_TARGETING_GEO_ABSENT,
    META_ADS_TARGETING_INTEREST_ABSENT,
    META_ADS_TARGETING_NOT_DECLARED,
    meta_ads_declares_serving_intent,
)
from adhelm.reporting import NormalizedReport, PerformanceRow

PROVIDER: Final[Provider] = "meta_ads"
# Bump this when a rule's semantics change.  It is part of every finding's
# content identity, so an audit produced by a different rule table can never be
# mistaken for one produced by this table.
RULE_TABLE_VERSION: Final = "meta_ads.rules.2026-09-09.1"
_BOUNDED: Final[AnalyticsCompleteness] = "BOUNDED_NON_EXHAUSTIVE"
_NOT_ANALYTICS: Final[AnalyticsCompleteness] = "NOT_APPLICABLE"
# The one route this build can compile for an existing Meta campaign.  Naming
# it in a recommendation is a vocabulary statement about what the product could
# be asked to compile next; it is never a plan and never an authorization.
PAUSE_PLAN_ROUTE: Final = "plan meta-campaign-pause create"
# The documented bid strategies that require an explicit bid or cost cap.
_CAPPED_BID_STRATEGIES: Final[frozenset[str]] = frozenset(
    {"LOWEST_COST_WITH_BID_CAP", "COST_CAP", "LOWEST_COST_WITH_MIN_ROAS"}
)
# The documented statuses that say a child is not serving because a parent is
# not.  `effective_status` already folds the parent in, so these are an
# observation about the hierarchy, never a contradiction.
_PARENT_PAUSE_STATUSES: Final[frozenset[str]] = frozenset({"CAMPAIGN_PAUSED", "ADSET_PAUSED"})
_REVIEWED_STATUSES: Final[dict[str, tuple[str, ...]]] = {
    "CAMPAIGN": META_ADS_REVIEWED_CAMPAIGN_STATUSES,
    "AD_GROUP": META_ADS_REVIEWED_AD_SET_STATUSES,
    "AD": META_ADS_REVIEWED_AD_STATUSES,
}
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
        _rule(
            _Rule(
                rule_id="MA_STRUCTURE_CAMPAIGN_WITHOUT_AD_SET",
                finding_type="META_CAMPAIGN_WITHOUT_AD_SET",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta campaign has no retained ad set in the same inventory read.",
                suggested_action="Confirm the campaign in Meta Ads Manager, or read the bounded hierarchy again; a campaign with no ad set cannot serve.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_STRUCTURE_AD_SET_WITHOUT_AD",
                finding_type="META_AD_SET_WITHOUT_AD",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad set has no retained ad in the same inventory read.",
                suggested_action="Confirm the ad set in Meta Ads Manager; an ad set with no ad cannot deliver an impression.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_STRUCTURE_AD_SET_PARENT_NOT_RETAINED",
                finding_type="META_AD_SET_PARENT_CAMPAIGN_NOT_RETAINED",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad set names a parent campaign that is absent from the same retained inventory.",
                suggested_action="Read the bounded hierarchy again; an ad set whose parent campaign is missing cannot be evaluated in context.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_STRUCTURE_AD_PARENT_NOT_RETAINED",
                finding_type="META_AD_PARENT_AD_SET_NOT_RETAINED",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad names a parent that is absent from the same retained inventory.",
                suggested_action="Read the bounded hierarchy again; an ad whose parent is missing cannot be evaluated in context.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_STRUCTURE_STATUS_NOT_REVIEWED",
                finding_type="META_STATUS_VALUE_NOT_IN_REVIEWED_ENUM",
                dimension="STRUCTURE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta resource carries an effective status outside the documented enumeration this connector pins for its node.",
                suggested_action="Recheck the Meta effective-status documentation for this node before interpreting the value; the audit does not guess its meaning.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_STRUCTURE_PARENT_PAUSE_PROPAGATED",
                finding_type="META_RESOURCE_NOT_SERVING_BECAUSE_A_PARENT_IS_PAUSED",
                dimension="STRUCTURE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta resource declares a documented status that says it is not serving because one of its parents is paused.",
                suggested_action="Reconcile the parent status in Meta Ads Manager if the child was expected to serve; this audit reads declared status only, not delivery.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS",
                finding_type="META_ACTIVE_CAMPAIGN_WITHOUT_RETAINED_IMPRESSIONS",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="LOW",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained Meta campaign declares the documented ACTIVE effective status and the retained bounded report contains no impressions for it. The retained report is not an exhaustive provider claim, so this is not proof of zero delivery.",
                suggested_action="Read the same bounded campaign insights report again over a compatible window before treating the absence of rows as zero delivery.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
                finding_type="META_CLICKS_EXCEED_IMPRESSIONS",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained Meta report row declares more clicks than impressions at its declared grain, which no consistent delivery aggregate produces.",
                suggested_action="Read the same bounded report again and compare the declared grain and window before using this row for any decision.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_DELIVERY_REACH_EXCEEDS_IMPRESSIONS",
                finding_type="META_REACH_EXCEEDS_IMPRESSIONS",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained Meta report row declares more reached people than impressions at its declared grain, which no consistent delivery aggregate produces.",
                suggested_action="Read the same bounded report again and compare the declared grain and window; reach can never exceed impressions for one row.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_DELIVERY_METRICS_MISSING",
                finding_type="META_DELIVERY_METRICS_MISSING_ON_ROW",
                dimension="DELIVERY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="This retained Meta report row declares no impression or click count, so no delivery rate can be derived from it.",
                suggested_action="Read the same declared grain again with the fixed impression and click fields before interpreting delivery.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
                finding_type="META_RETAINED_REPORT_DOES_NOT_DECLARE_COMPLETE_PAGINATION",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained Meta report does not declare the COMPLETE quality status that the fixed insights read records when it consumed every documented cursor page, so its row set has no proven read boundary.",
                suggested_action="Run the fixed Meta insights report again and audit the result it retains; do not treat this row set as the whole window.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_DELIVERY_REPORT_FRESHNESS_NOT_READ",
                finding_type="META_REPORT_FRESHNESS_NOT_RETURNED_BY_PROVIDER",
                dimension="DELIVERY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="The fixed Meta insights read returns no verified freshness timestamp, so the retained report declares NOT_AVAILABLE_FROM_PROVIDER and this audit can bound no processing lag.",
                suggested_action="Treat a recent Meta window as provisional; the provider exposes no freshness field to this reviewed read.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_SPEND_WITHOUT_IMPRESSIONS",
                finding_type="META_SPEND_WITHOUT_IMPRESSIONS",
                dimension="SPEND",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_BOUNDED,
                observation="This retained Meta report row declares spend with zero impressions at its declared grain.",
                suggested_action="Read the same bounded report again and reconcile the row in Meta Ads Manager before drawing a spend conclusion.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET",
                finding_type="META_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_SPEND_BOUND",
                dimension="SPEND",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta campaign declares the documented ACTIVE effective status and neither it nor any of its retained ad sets carries a daily budget, a lifetime budget, or a spend cap, so this audit can bound no maximum spend for it.",
                suggested_action="Confirm the campaign and its ad-set budgets in Meta Ads Manager; an absent budget in a retained read is not a proven unlimited spend, and it is not a proven bound either.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_SCHEDULE_END",
                finding_type="META_ACTIVE_CAMPAIGN_WITHOUT_RETAINED_STOP_TIME",
                dimension="SPEND",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta campaign declares the documented ACTIVE effective status and carries no retained stop time, so its run has no retained end.",
                suggested_action="Confirm the campaign schedule in Meta Ads Manager; an absent stop time in a retained read is not a proven open-ended run.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_SPEND_LIFETIME_BUDGET_WITHOUT_END_TIME",
                finding_type="META_LIFETIME_BUDGET_WITHOUT_RETAINED_END_TIME",
                dimension="SPEND",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad set declares a lifetime budget and no end time, which Meta documents as a required pairing for a lifetime budget.",
                suggested_action="Confirm the ad-set end time in Meta Ads Manager; a lifetime budget without one cannot be paced against a documented window.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_SPEND_BOTH_BUDGET_FIELDS_RETAINED",
                finding_type="META_DAILY_AND_LIFETIME_BUDGET_BOTH_RETAINED",
                dimension="SPEND",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta resource returned both a daily and a lifetime budget in the same read, and this build has reviewed no documented rule for which one governs delivery.",
                suggested_action="Confirm which budget Meta applies for this resource before using either retained amount; the audit retains both and prefers neither.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_BIDS_STRATEGY_NOT_RETURNED",
                finding_type="META_BID_STRATEGY_NOT_RETURNED",
                dimension="BIDS",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta resource returned no bid strategy, so this audit can state nothing about how it bids.",
                suggested_action="Confirm the bid strategy in Meta Ads Manager; an absent value in a retained read is not a proven default.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_BIDS_STRATEGY_NOT_REVIEWED",
                finding_type="META_BID_STRATEGY_NOT_IN_REVIEWED_ENUM",
                dimension="BIDS",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta resource returned a bid strategy outside the documented enumeration this connector pins.",
                suggested_action="Recheck the Meta bid-strategy documentation before interpreting the value; the audit does not guess its meaning.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_BIDS_CAPPED_STRATEGY_WITHOUT_BID_AMOUNT",
                finding_type="META_CAPPED_BID_STRATEGY_WITHOUT_RETAINED_BID_AMOUNT",
                dimension="BIDS",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad set declares a documented bid strategy that takes an explicit cap and carries no retained bid amount.",
                suggested_action="Confirm the ad-set bid amount in Meta Ads Manager; a capped strategy with no retained cap cannot be evaluated against a target.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_BIDS_BILLING_EVENT_NOT_RETAINED",
                finding_type="META_BILLING_EVENT_READ_BUT_NOT_RETAINED_IN_HISTORY",
                dimension="BIDS",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The fixed Meta ad-set read requests the documented billing event, but the shared closed snapshot-state model retains no field for it, so this audit never sees it.",
                suggested_action="Read the billing event in Meta Ads Manager; retaining it locally requires a shared snapshot-state change outside this rule table.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_OBJECTIVE_NOT_RETURNED",
                finding_type="META_CAMPAIGN_OBJECTIVE_NOT_RETURNED",
                dimension="OBJECTIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta campaign returned no objective, so this audit can state nothing about what it optimizes for.",
                suggested_action="Confirm the campaign objective in Meta Ads Manager before interpreting its delivery.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_OBJECTIVE_NOT_REVIEWED",
                finding_type="META_CAMPAIGN_OBJECTIVE_NOT_IN_REVIEWED_ENUM",
                dimension="OBJECTIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta campaign returned an objective outside the documented enumeration this connector pins.",
                suggested_action="Recheck the Meta campaign objective documentation before interpreting the value; the audit does not guess its meaning.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_OBJECTIVE_BUYING_TYPE_NOT_RETURNED",
                finding_type="META_CAMPAIGN_BUYING_TYPE_NOT_RETURNED",
                dimension="OBJECTIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta campaign returned no buying type, so this audit cannot tell an auction campaign from a reserved one.",
                suggested_action="Confirm the campaign buying type in Meta Ads Manager before comparing it with an auction campaign.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_OBJECTIVE_RESERVED_BUYING_TYPE_UNSUPPORTED",
                finding_type="META_RESERVED_BUYING_TYPE_NOT_SUPPORTED_BY_THIS_BUILD",
                dimension="OBJECTIVE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta campaign declares the documented RESERVED buying type, which this build's offline compiler and pause route have never been reviewed against.",
                suggested_action="Treat reach-and-frequency reservations as outside this build's reviewed subset; no local operation is proposed for one.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_TARGETING_NOT_DECLARED",
                finding_type="META_AD_SET_WITHOUT_DECLARED_TARGETING",
                dimension="TARGETING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad set declares the documented ACTIVE effective status and returned no targeting object at all.",
                suggested_action="Confirm the ad-set targeting in Meta Ads Manager; this audit reads whether a targeting object was returned, never what it contains.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_TARGETING_GEO_ABSENT",
                finding_type="META_AD_SET_TARGETING_WITHOUT_GEOGRAPHY",
                dimension="TARGETING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad set declares the documented ACTIVE effective status and its returned targeting object declared no geography, which Meta documents as required.",
                suggested_action="Confirm the ad-set geographic targeting in Meta Ads Manager; no country, region, or radius value is read or retained here.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_TARGETING_AGE_ABSENT",
                finding_type="META_AD_SET_TARGETING_WITHOUT_AGE_BOUND",
                dimension="TARGETING",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad set returned a targeting object that declared neither a minimum nor a maximum age.",
                suggested_action="Confirm the ad-set age targeting in Meta Ads Manager; no age value is read or retained here.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_TARGETING_INTEREST_ABSENT",
                finding_type="META_AD_SET_TARGETING_WITHOUT_INTEREST_OR_FLEXIBLE_SPEC",
                dimension="TARGETING",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad set returned a targeting object that declared no interests and no flexible specification.",
                suggested_action="Confirm the ad-set detailed targeting in Meta Ads Manager; no interest name or audience identifier is read or retained here.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_TARGETING_VALUES_NOT_READ",
                finding_type="META_TARGETING_CRITERION_VALUES_NOT_READ",
                dimension="TARGETING",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The fixed Meta read records targeting presence only; no criterion, audience, geography, age bound, or interest value is retained, so no targeting exposure can be evaluated.",
                suggested_action="Review targeting values in Meta Ads Manager; this product deliberately never retains them.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_PLACEMENT_NOT_READ",
                finding_type="META_PLACEMENT_NOT_READ",
                dimension="PLACEMENT",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The reviewed Meta fixed read requests no placement configuration field and no placement report facet, so placement is unread rather than healthy.",
                suggested_action="Review placements in Meta Ads Manager; this build's reviewed read subset returns none.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_CREATIVE_NOT_ASSOCIATED",
                finding_type="META_AD_WITHOUT_ASSOCIATED_CREATIVE",
                dimension="CREATIVE",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad returned no associated creative identifier, so it names nothing that could render.",
                suggested_action="Confirm the ad's creative in Meta Ads Manager; an ad with no retained creative association cannot be evaluated for content at all.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_CREATIVE_IMAGE_HASH_ABSENT",
                finding_type="META_AD_CREATIVE_WITHOUT_IMAGE_HASH",
                dimension="CREATIVE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad names a creative that returned no image hash, so this audit cannot tell that a single-image creative has an image.",
                suggested_action="Confirm the creative in Meta Ads Manager; a video, carousel, or dynamic creative legitimately returns no image hash, and the audit does not guess which.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_CREATIVE_CONTENT_TEXT_NOT_READ",
                finding_type="META_CREATIVE_COPY_TEXT_NOT_READ",
                dimension="CREATIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The fixed Meta read never requests creative headline, body, or story specification fields, so no ad copy is read or retained and no copy problem can be found here.",
                suggested_action="Review ad copy in Meta Ads Manager; this product deliberately never retains it.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_CREATIVE_IMAGE_BYTES_NOT_PROVABLE",
                finding_type="META_CREATIVE_IMAGE_BYTES_NOT_PROVABLE",
                dimension="CREATIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="A retained Meta image-hash presence fact proves that a creative names an image, never that Meta holds a particular local asset's bytes.",
                suggested_action="Compare a local creative version with the provider asset in Meta Ads Manager; no local read can prove byte equality.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_CREATIVE_LOCAL_BINDING_UNVERIFIED",
                finding_type="META_LOCAL_CREATIVE_BINDING_UNVERIFIED",
                dimension="CREATIVE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This local creative version declares a Meta ad that the retained inventory also holds, but the declaration is local and no reviewed association read has verified it.",
                suggested_action="Treat the binding as a local claim; verifying it needs an independently reviewed association read this build does not have.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
                finding_type="META_LOCAL_CREATIVE_BINDING_WITHOUT_RETAINED_RESOURCE",
                dimension="CREATIVE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This local creative version declares a Meta ad that is absent from the retained inventory read.",
                suggested_action="Read the bounded hierarchy again and reconcile the local declaration; the ad may never have been created.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_DESTINATION_LINK_ABSENT",
                finding_type="META_AD_CREATIVE_WITHOUT_DESTINATION_LINK",
                dimension="DESTINATION_CONFIGURATION",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad names a creative that returned neither of the two documented link fields this read requests, so no destination is retained for it.",
                suggested_action="Confirm the ad's destination in Meta Ads Manager; a creative whose link lives only in an unread story specification returns nothing here.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_DESTINATION_VALUES_NOT_READ",
                finding_type="META_DESTINATION_URL_VALUES_NOT_READ",
                dimension="DESTINATION_CONFIGURATION",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The fixed Meta read records destination presence only; no URL, query string, or campaign tagging value is retained, so no destination or UTM problem can be found here.",
                suggested_action="Review destinations and tagging in Meta Ads Manager; this product deliberately never retains them.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ",
                finding_type="META_PIXEL_AND_EVENT_HEALTH_NOT_READ",
                dimension="TRACKING",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The reviewed Meta fixed read requests no pixel, dataset, custom conversion, or event-health field, so tracking health is unread rather than healthy.",
                suggested_action="Review pixel and event health in Meta Events Manager; this build's reviewed read subset returns none.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_NATIVE_CONVERSION_NOT_READ",
                finding_type="META_NATIVE_CONVERSION_METRICS_NOT_READ",
                dimension="NATIVE_CONVERSION",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The fixed Meta insights request projects impressions, clicks, spend, and reach only; no actions or conversion field is requested, so no native conversion is read.",
                suggested_action="Review conversions in Meta Ads Manager; this build's reviewed insights subset requests none.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_BROWSER_NOT_APPLICABLE",
                finding_type="META_BROWSER_MEASUREMENT_NOT_APPLICABLE",
                dimension="BROWSER_MEASUREMENT",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="A local Meta audit executes no browser and loads no page, so browser measurement is not applicable to it rather than unhealthy.",
                suggested_action="Use the separate browser-measurement workflow; a retained-record audit cannot observe a page.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_POLICY_AD_REVIEW_BLOCKED",
                finding_type="META_AD_DISAPPROVED_IN_REVIEW",
                dimension="POLICY",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad declares the documented DISAPPROVED effective status, so Meta's review is blocking it.",
                suggested_action="Open the ad in Meta Ads Manager for the review feedback; this build never requests review feedback text.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_POLICY_AD_REVIEW_PENDING",
                finding_type="META_AD_PENDING_REVIEW",
                dimension="POLICY",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad declares a documented status that places it in review rather than in delivery.",
                suggested_action="Wait for Meta's review to complete and read the bounded hierarchy again; no local action changes a review outcome.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_POLICY_AD_NEEDS_ATTENTION",
                finding_type="META_AD_STATUS_NEEDS_ATTENTION",
                dimension="POLICY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad declares a documented status that names an issue or a missing billing precondition rather than delivery.",
                suggested_action="Open the ad and the ad account in Meta Ads Manager; this build reads no issue detail and no billing state.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_POLICY_ACCOUNT_STATUS_NOT_REVIEWED",
                finding_type="META_ACCOUNT_STATUS_HAS_NO_REVIEWED_ENUMERATION",
                dimension="POLICY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The retained Meta account snapshot carries the provider's numeric account status, for which this build pins no reviewed enumeration, so the audit makes no claim about what the value means.",
                suggested_action="Confirm the ad account's standing in Meta Ads Manager; the audit reports the retained value without interpreting it.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_POLICY_APP_REVIEW_AND_VERIFICATION_NOT_READ",
                finding_type="META_APP_REVIEW_AND_BUSINESS_VERIFICATION_NOT_DOCUMENTED_LOCALLY",
                dimension="POLICY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Meta's app review outcome, access tier, and Business Verification state are not documented locally and are not read by any command in this build, so no local record can show whether this app may read another party's ad account.",
                suggested_action="Confirm app review, access tier, and Business Verification in the Meta App Dashboard; no local status output asserts any of them.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_CREATIVE_VALUE_WITHHELD_BY_LOCAL_SCREEN",
                finding_type="META_CREATIVE_PRESENCE_VALUE_WITHHELD_BY_LOCAL_PRIVACY_SCREEN",
                dimension="CREATIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Meta ad returned a creative image hash or destination link whose value matched this product's local privacy screen. The value was never retained either way; the presence fact stands and the withheld value is recorded as withheld rather than absent.",
                suggested_action="Review the creative in Meta Ads Manager if the match is unexpected; the matched value is never named, quoted, or retained locally, and the read was not stopped.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_DRIFT_LOCAL_STATE_SHAPE_CHANGED",
                finding_type="META_LOCAL_RETAINED_STATE_SHAPE_DIFFERS",
                dimension="DRIFT",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Two retained snapshots of this Meta resource hold different normalized state fields. A field on only one side is either a field this connector began or stopped retaining, or a value that became absent; the two are indistinguishable locally, so no change at Meta is claimed from it.",
                suggested_action="Compare the two snapshots' retained connector state-shape versions before reading anything into the difference; only fields both reads returned are compared as values.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_DRIFT_STATE_CHANGED",
                finding_type="META_RESOURCE_STATE_CHANGED_OUT_OF_BAND",
                dimension="DRIFT",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Two retained snapshots of this Meta resource differ and this product applied no change between them.",
                suggested_action="Read the bounded hierarchy again to bound the observation interval; the actor and the exact change time are unknown and are not inferred.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_DRIFT_ABSENT_FROM_LATER_INVENTORY",
                finding_type="META_RESOURCE_ABSENT_FROM_LATER_INVENTORY",
                dimension="DRIFT",
                severity="MEDIUM",
                confidence="MEDIUM",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This Meta resource is in the earlier retained read and not in the later one.",
                suggested_action="Read the bounded hierarchy again; an absence between two retained reads is an observation, never a proven deletion.",
            )
        ),
        _rule(
            _Rule(
                rule_id="MA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
                finding_type="META_RESOURCE_ABSENT_FROM_EARLIER_INVENTORY",
                dimension="DRIFT",
                severity="LOW",
                confidence="MEDIUM",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This Meta resource is in the later retained read and not in the earlier one.",
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
    """Whether a retained Meta row records positive spend.

    The Meta read parses `spend` as an exact decimal against a reviewed
    per-currency exponent and refuses any value with unsupported precision
    before a row exists, so a retained minor-unit amount is the exact provider
    amount in the account's own currency rather than a rounded projection.
    """
    return row.spend is not None and row.spend.minor_units > 0


def _has_retained_spend_bound(record: RetainedSnapshotRecord) -> bool:
    """Whether one retained snapshot carries any amount that bounds spend."""
    state = record.snapshot.normalized_state
    return any(
        state.get(field) is not None
        for field in (
            "budget_minor",
            "daily_budget_minor",
            "lifetime_budget_minor",
            "spend_cap_minor",
        )
    )


def _structure_findings(
    grouped: dict[str, list[RetainedSnapshotRecord]],
    inventory: RetainedInventory,
    window: AuditDataWindow,
    status: ProviderCoverageStatus,
) -> list[ProviderAuditFinding]:
    # A complete bounded read makes an absence meaningful. A caller-declared
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
        "CAMPAIGN": ("MA_STRUCTURE_CAMPAIGN_WITHOUT_AD_SET", "AD_GROUP"),
        "AD_GROUP": ("MA_STRUCTURE_AD_SET_WITHOUT_AD", "AD"),
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
        "AD_GROUP": ("MA_STRUCTURE_AD_SET_PARENT_NOT_RETAINED", "CAMPAIGN"),
        "AD": ("MA_STRUCTURE_AD_PARENT_NOT_RETAINED", "AD_GROUP"),
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
    for resource_type, reviewed in _REVIEWED_STATUSES.items():
        for record in grouped[resource_type]:
            effective = record.snapshot.effective_status.upper()
            if effective not in reviewed:
                findings.append(
                    _emit(
                        RULES["MA_STRUCTURE_STATUS_NOT_REVIEWED"],
                        resource_refs=(record.snapshot.resource_ref,),
                        evidence_refs=(record.record_digest,),
                        window=window,
                        coverage_status=status,
                    )
                )
                continue
            # Meta's `effective_status` already folds a parent's status into a
            # child's, so these two documented values are the propagation
            # itself, never a contradiction between two retained records.
            if effective in _PARENT_PAUSE_STATUSES:
                findings.append(
                    _emit(
                        RULES["MA_STRUCTURE_PARENT_PAUSE_PROPAGATED"],
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
    """Spend, objective, and bid rules over the retained campaign settings."""
    findings: list[ProviderAuditFinding] = []
    ad_sets_by_campaign: dict[str, list[RetainedSnapshotRecord]] = {}
    for record in grouped["AD_GROUP"]:
        for parent in record.snapshot.parent_resource_refs:
            ad_sets_by_campaign.setdefault(parent, []).append(record)
    for record in grouped["CAMPAIGN"]:
        state = record.snapshot.normalized_state
        quality = _quality(record)
        refs = (record.snapshot.resource_ref,)
        evidence = (record.record_digest,)
        serving = meta_ads_declares_serving_intent(record.snapshot.effective_status)
        if serving and not _has_retained_spend_bound(record):
            # Meta allows the budget to sit on the campaign (campaign budget
            # optimization) or on each ad set, so a campaign bounds no spend
            # only when neither level retained one.
            children = ad_sets_by_campaign.get(record.snapshot.resource_ref, [])
            if not any(_has_retained_spend_bound(child) for child in children):
                findings.append(
                    _emit(
                        RULES["MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET"],
                        resource_refs=refs,
                        evidence_refs=evidence,
                        window=window,
                        coverage_status=coverage["SPEND"],
                    )
                )
        if serving and state.get("schedule_end") is None:
            findings.append(
                _emit(
                    RULES["MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_SCHEDULE_END"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["SPEND"],
                )
            )
        if META_ADS_BUDGET_MODE_DAILY_AND_LIFETIME in quality:
            findings.append(
                _emit(
                    RULES["MA_SPEND_BOTH_BUDGET_FIELDS_RETAINED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["SPEND"],
                )
            )
        for status_value, rule_id in (
            (META_ADS_OBJECTIVE_NOT_RETURNED, "MA_OBJECTIVE_NOT_RETURNED"),
            (META_ADS_OBJECTIVE_NOT_REVIEWED, "MA_OBJECTIVE_NOT_REVIEWED"),
            (META_ADS_BUYING_TYPE_NOT_RETURNED, "MA_OBJECTIVE_BUYING_TYPE_NOT_RETURNED"),
            (META_ADS_BUYING_TYPE_RESERVED, "MA_OBJECTIVE_RESERVED_BUYING_TYPE_UNSUPPORTED"),
            (META_ADS_BID_STRATEGY_NOT_REVIEWED, "MA_BIDS_STRATEGY_NOT_REVIEWED"),
        ):
            if status_value in quality:
                rule = RULES[rule_id]
                findings.append(
                    _emit(
                        rule,
                        resource_refs=refs,
                        evidence_refs=evidence,
                        window=window,
                        coverage_status=coverage[rule.dimension],
                    )
                )
    return findings


def _ad_set_setting_findings(
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
    coverage: dict[AuditDimension, ProviderCoverageStatus],
) -> list[ProviderAuditFinding]:
    """Budget, schedule, bid, and targeting-presence rules over retained ad sets."""
    findings: list[ProviderAuditFinding] = []
    for record in grouped["AD_GROUP"]:
        state = record.snapshot.normalized_state
        quality = _quality(record)
        refs = (record.snapshot.resource_ref,)
        evidence = (record.record_digest,)
        serving = meta_ads_declares_serving_intent(record.snapshot.effective_status)
        lifetime = quality & {
            META_ADS_BUDGET_MODE_LIFETIME,
            META_ADS_BUDGET_MODE_DAILY_AND_LIFETIME,
        }
        if lifetime and META_ADS_SCHEDULE_END_ABSENT in quality:
            findings.append(
                _emit(
                    RULES["MA_SPEND_LIFETIME_BUDGET_WITHOUT_END_TIME"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["SPEND"],
                )
            )
        if META_ADS_BUDGET_MODE_DAILY_AND_LIFETIME in quality:
            findings.append(
                _emit(
                    RULES["MA_SPEND_BOTH_BUDGET_FIELDS_RETAINED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["SPEND"],
                )
            )
        if META_ADS_BID_STRATEGY_NOT_RETURNED in quality:
            findings.append(
                _emit(
                    RULES["MA_BIDS_STRATEGY_NOT_RETURNED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["BIDS"],
                )
            )
        if META_ADS_BID_STRATEGY_NOT_REVIEWED in quality:
            findings.append(
                _emit(
                    RULES["MA_BIDS_STRATEGY_NOT_REVIEWED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["BIDS"],
                )
            )
        strategy = state.get("bid_strategy")
        if (
            isinstance(strategy, str)
            and strategy.upper() in _CAPPED_BID_STRATEGIES
            and state.get("bid_amount_minor") is None
        ):
            findings.append(
                _emit(
                    RULES["MA_BIDS_CAPPED_STRATEGY_WITHOUT_BID_AMOUNT"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["BIDS"],
                )
            )
        # Targeting rules are guarded by the documented serving intent, like the
        # campaign spend rules above.  Targeting decides only *where* an ad set
        # delivers, and an ad set whose retained effective status says it is not
        # serving is not delivering anywhere; reporting it as an exposure would
        # state a delivery consequence the retained status contradicts.
        if serving and META_ADS_TARGETING_NOT_DECLARED in quality:
            findings.append(
                _emit(
                    RULES["MA_TARGETING_NOT_DECLARED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["TARGETING"],
                )
            )
        if META_ADS_TARGETING_NOT_DECLARED in quality:
            continue
        if serving and META_ADS_TARGETING_GEO_ABSENT in quality:
            findings.append(
                _emit(
                    RULES["MA_TARGETING_GEO_ABSENT"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["TARGETING"],
                )
            )
        for status_value, rule_id in (
            (META_ADS_TARGETING_AGE_ABSENT, "MA_TARGETING_AGE_ABSENT"),
            (META_ADS_TARGETING_INTEREST_ABSENT, "MA_TARGETING_INTEREST_ABSENT"),
        ):
            if status_value in quality:
                findings.append(
                    _emit(
                        RULES[rule_id],
                        resource_refs=refs,
                        evidence_refs=evidence,
                        window=window,
                        coverage_status=coverage["TARGETING"],
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
    if META_ADS_REPORT_COMPLETE_STATUS not in report.envelope.data_quality_statuses:
        findings.append(
            _emit(
                RULES["MA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED"],
                resource_refs=(report.envelope.source_account_ref,),
                evidence_refs=(report_digest,),
                window=window,
                coverage_status="PARTIAL",
            )
        )
    findings.append(
        _emit(
            RULES["MA_DELIVERY_REPORT_FRESHNESS_NOT_READ"],
            resource_refs=(report.envelope.source_account_ref,),
            evidence_refs=(report_digest,),
            window=window,
            coverage_status="PARTIAL",
        )
    )
    for row in report.performance_rows:
        if row.impressions is None or row.clicks is None:
            findings.append(
                _emit(
                    RULES["MA_DELIVERY_METRICS_MISSING"],
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
                    RULES["MA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
        if row.reach is not None and row.reach > row.impressions:
            findings.append(
                _emit(
                    RULES["MA_DELIVERY_REACH_EXCEEDS_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
        if row.impressions == 0 and _spend_is_positive(row):
            findings.append(
                _emit(
                    RULES["MA_SPEND_WITHOUT_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
    # An AD-grain Meta report proves campaign, ad-set, and ad identifiers, but
    # the retained row's own resource is the ad, so campaign delivery is
    # evaluated from campaign-grain rows only.
    campaign_impressions: dict[str, int] = {}
    for row in report.performance_rows:
        if row.metric_grain != "CAMPAIGN":
            continue
        campaign_impressions[row.resource_ref] = campaign_impressions.get(row.resource_ref, 0) + (
            row.impressions or 0
        )
    if campaign_impressions:
        for record in grouped["CAMPAIGN"]:
            if not meta_ads_declares_serving_intent(record.snapshot.effective_status):
                continue
            if campaign_impressions.get(record.snapshot.resource_ref, 0) > 0:
                continue
            findings.append(
                _emit(
                    RULES["MA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"],
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
    """One finding per unread, presence-only, or retention-limited fact.

    Most of these describe what this build's fixed reads never request, or what
    the shared snapshot state cannot hold.  That is a property of the read
    contract, not of any one retained record, so they do not depend on an
    account snapshot being present: an audit with no retained inventory at all,
    or a retained read that omitted the account, still reports every one of
    them, anchored instead to the account the audited report names and cited to
    that report's own record digest.  Only the rules that describe a retained
    *resource* -- the ad-set and ad ones -- wait for such a resource to exist.
    """
    accounts = grouped["ACCOUNT"]
    resource_refs = (
        (accounts[0].snapshot.resource_ref,)
        if accounts
        else (context.report.envelope.source_account_ref,)
    )
    evidence_refs = (accounts[0].record_digest,) if accounts else (context.source_record_digest,)
    anchored = [
        ("MA_BIDS_BILLING_EVENT_NOT_RETAINED", bool(grouped["AD_GROUP"])),
        ("MA_TARGETING_VALUES_NOT_READ", bool(grouped["AD_GROUP"])),
        ("MA_PLACEMENT_NOT_READ", True),
        ("MA_CREATIVE_CONTENT_TEXT_NOT_READ", bool(grouped["AD"])),
        ("MA_CREATIVE_IMAGE_BYTES_NOT_PROVABLE", bool(grouped["AD"])),
        ("MA_DESTINATION_VALUES_NOT_READ", bool(grouped["AD"])),
        ("MA_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ", True),
        ("MA_NATIVE_CONVERSION_NOT_READ", True),
        ("MA_BROWSER_NOT_APPLICABLE", True),
        ("MA_POLICY_ACCOUNT_STATUS_NOT_REVIEWED", bool(accounts)),
        ("MA_POLICY_APP_REVIEW_AND_VERIFICATION_NOT_READ", True),
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


def _ad_findings(
    context: ProviderAuditContext,
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
    coverage: dict[AuditDimension, ProviderCoverageStatus],
) -> list[ProviderAuditFinding]:
    findings: list[ProviderAuditFinding] = []
    for record in grouped["AD"]:
        quality = _quality(record)
        refs = (record.snapshot.resource_ref,)
        evidence = (record.record_digest,)
        if META_ADS_CREATIVE_NOT_ASSOCIATED in quality:
            findings.append(
                _emit(
                    RULES["MA_CREATIVE_NOT_ASSOCIATED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["CREATIVE"],
                )
            )
        if META_ADS_CREATIVE_IMAGE_HASH_ABSENT in quality:
            findings.append(
                _emit(
                    RULES["MA_CREATIVE_IMAGE_HASH_ABSENT"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["CREATIVE"],
                )
            )
        if META_ADS_CREATIVE_VALUE_WITHHELD in quality:
            findings.append(
                _emit(
                    RULES["MA_CREATIVE_VALUE_WITHHELD_BY_LOCAL_SCREEN"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["CREATIVE"],
                )
            )
        if META_ADS_CREATIVE_LINK_ABSENT in quality:
            findings.append(
                _emit(
                    RULES["MA_DESTINATION_LINK_ABSENT"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["DESTINATION_CONFIGURATION"],
                )
            )
        for status_value, rule_id in (
            (META_ADS_AD_REVIEW_BLOCKED, "MA_POLICY_AD_REVIEW_BLOCKED"),
            (META_ADS_AD_REVIEW_PENDING, "MA_POLICY_AD_REVIEW_PENDING"),
            (META_ADS_AD_REVIEW_ATTENTION, "MA_POLICY_AD_NEEDS_ATTENTION"),
        ):
            if status_value in quality:
                findings.append(
                    _emit(
                        RULES[rule_id],
                        resource_refs=refs,
                        evidence_refs=evidence,
                        window=window,
                        coverage_status=coverage["POLICY"],
                    )
                )
    catalog = context.evidence.creative_catalog
    if catalog is None:
        return findings
    # "Absent from the retained inventory" is a claim about a read boundary, so
    # it needs one.  With no retained inventory nothing was read and nothing is
    # absent; with an unproven boundary the omission carries no information at
    # all, exactly as `_drift` treats its own absence rules.  A caller-declared
    # subset is weaker than a whole read, so the claim survives only at `LOW`
    # confidence there.
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
                    RULES["MA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE"],
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
                RULES["MA_CREATIVE_LOCAL_BINDING_UNVERIFIED"],
                resource_refs=(entry.creative_version_id, entry.provider_resource_ref),
                evidence_refs=(entry.record_digest, bound.record_digest),
                window=window,
                coverage_status=coverage["CREATIVE"],
            )
        )
    return findings


def _absence_is_evidence(inventory: RetainedInventory) -> bool:
    """Whether what a retained record set omits carries any information."""
    return inventory.completeness != "READ_BOUNDARY_UNVERIFIED"


_NO_STATE_SHAPE_VERSION: Final = "NOT_RECORDED"


def _state_shape_action(before: RetainedSnapshotRecord, after: RetainedSnapshotRecord) -> str:
    """The suggested action for one local state-shape difference, with its evidence.

    The two retained connector state-shape versions are the whole reason this
    difference can be resolved at all, so the action names them instead of
    telling a reader to go and find them.  A snapshot written before this
    product recorded one reports `NOT_RECORDED`, which is itself the answer:
    the older read predates the version marker, so a retention change is the
    likelier explanation.
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
        "Meta Ads."
    )


def _state_shape_version(record: RetainedSnapshotRecord) -> str | None:
    """The connector state-shape version a retained snapshot declares, if any.

    Snapshots written before this product recorded one carry none.  The value
    lives in `data_quality_statuses`, never in `normalized_state`, so recording
    it creates no drift of its own.
    """
    versions = sorted(
        status.removeprefix(META_ADS_STATE_SHAPE_PREFIX)
        for status in record.snapshot.data_quality_statuses
        if status.startswith(META_ADS_STATE_SHAPE_PREFIX)
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
    field -- this change added `normalized_state.bid_strategy` to every
    retained Meta campaign -- and a value that became absent, because
    `_snapshot` drops a `None`, so a campaign losing its stop time removes the
    key entirely.

    Publishing either as `META_ADS_OUT_OF_BAND_STATE_CHANGE_OBSERVED` would
    assert a change at Meta inside a recorded interval that this product never
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
    # creations for resources an interrupted baseline never held.  A per-resource
    # state comparison stays valid either way, so only the absence rules stop.
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
                # Not an out-of-band change and not a drift observation: a
                # field on only one side is either one this connector began or
                # stopped retaining, or a value that became absent, and the
                # retained records cannot tell those apart.  Reporting it as a
                # change at Meta would be a fabricated remote observation.
                findings.append(
                    _emit(
                        RULES["MA_DRIFT_LOCAL_STATE_SHAPE_CHANGED"],
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
                RULES["MA_DRIFT_STATE_CHANGED"],
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
                RULES["MA_DRIFT_ABSENT_FROM_LATER_INVENTORY"],
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
            RULES["MA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY"],
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
    """How many local creative bindings declare a Meta Ads resource."""
    catalog = context.evidence.creative_catalog
    if catalog is None:
        return 0
    return sum(
        1
        for entry in catalog.entries
        if entry.provider == PROVIDER and entry.provider_resource_ref is not None
    )


# The read surfaces that back each audit dimension.  `META_ADS_READ_SURFACES`
# is the one declaration of what this build's fixed Meta reads return, so the
# ceiling for a dimension is derived from it here rather than restated as a
# literal that could drift away from the adapter.  `BROWSER_MEASUREMENT` has no
# entry: no provider read could back it, and it is always `NOT_APPLICABLE`.
_DIMENSION_SURFACES: Final[dict[AuditDimension, tuple[str, ...]]] = {
    "STRUCTURE": ("CAMPAIGN_INVENTORY", "AD_SET_INVENTORY", "AD_INVENTORY"),
    "DELIVERY": ("CAMPAIGN_DELIVERY_REPORT", "AD_DELIVERY_REPORT"),
    "SPEND": (
        "CAMPAIGN_BUDGET",
        "CAMPAIGN_SPEND_CAP",
        "CAMPAIGN_SCHEDULE",
        "AD_SET_BUDGET_MODE",
        "AD_SET_SCHEDULE",
    ),
    "BIDS": ("CAMPAIGN_BID_STRATEGY", "AD_SET_BID_STRATEGY", "AD_SET_BID_AMOUNT"),
    "OBJECTIVE": ("CAMPAIGN_OBJECTIVE", "CAMPAIGN_BUYING_TYPE"),
    "TARGETING": ("AD_SET_TARGETING_PRESENCE",),
    "PLACEMENT": ("AD_SET_PLACEMENT",),
    "CREATIVE": ("AD_CREATIVE_ASSOCIATION", "AD_CREATIVE_IMAGE_HASH_PRESENCE"),
    "DESTINATION_CONFIGURATION": ("AD_CREATIVE_LINK_PRESENCE",),
    "TRACKING": ("PIXEL_AND_EVENT_HEALTH",),
    "NATIVE_CONVERSION": ("NATIVE_CONVERSION_METRICS",),
    "POLICY": ("AD_REVIEW_STATE", "ACCOUNT_STATUS"),
    "DRIFT": ("CAMPAIGN_INVENTORY", "AD_SET_INVENTORY", "AD_INVENTORY"),
}
_IMPLEMENTED_SURFACES: Final[frozenset[str]] = frozenset(
    {"IMPLEMENTED_FIXED_READ", "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY"}
)


def _surface_backed(dimension: AuditDimension) -> bool:
    """Whether any fixed read this build implements backs one dimension."""
    return any(
        META_ADS_READ_SURFACES[surface] in _IMPLEMENTED_SURFACES
        for surface in _DIMENSION_SURFACES.get(dimension, ())
    )


def _coverage_map(
    context: ProviderAuditContext, grouped: dict[str, list[RetainedSnapshotRecord]]
) -> dict[AuditDimension, ProviderCoverageStatus]:
    """Give every dimension one status: the evidence supplied, capped by the read.

    The evidence expressions below say what the retained records could support.
    `META_ADS_READ_SURFACES` then caps each one: a dimension no implemented
    fixed read backs is `NOT_READ` whatever a caller supplies, so `PLACEMENT`,
    `TRACKING`, and `NATIVE_CONVERSION` cannot become readable by accident.
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
    has_ad_sets = bool(grouped["AD_GROUP"])
    has_ads = bool(grouped["AD"])
    # A snapshot pair covers drift only when both sides are whole reads.  A
    # declared subset or an unproven read boundary can differ for reasons that
    # have nothing to do with the account changing.
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
        # as the whole window even when the adapter consumed every cursor page.
        "DELIVERY": "PARTIAL" if has_report_rows else "NOT_READ",
        "SPEND": ("PARTIAL" if has_report_rows or has_campaigns or has_ad_sets else "NOT_READ"),
        "BIDS": "PARTIAL" if has_campaigns or has_ad_sets else "NOT_READ",
        "OBJECTIVE": "PARTIAL" if has_campaigns else "NOT_READ",
        "TARGETING": "PARTIAL" if has_ad_sets else "NOT_READ",
        "PLACEMENT": "PARTIAL" if has_ad_sets else "NOT_READ",
        # Creative evidence is a retained ad snapshot or a local binding that
        # names one; both produce findings, so both are coverage.
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
    "STRUCTURE": "Account, campaign, ad-set, and ad identity, parents, and the documented effective status are read by the fixed v26.0 inventory edges.",
    "DELIVERY": "Impressions, clicks, and reach come from the fixed daily insights read, whose retained result is never labelled exhaustive by this audit vocabulary.",
    "SPEND": "Retained spend rows, campaign and ad-set daily/lifetime budgets, the campaign spend cap, and both schedules are evaluated as retained native minor units; nothing is rounded.",
    "BIDS": "The campaign and ad-set bid strategy and the ad-set bid amount are retained; the billing event is read but the shared snapshot state retains no field for it.",
    "OBJECTIVE": "The campaign objective and buying type are read against their documented enumerations; optimization goal and attribution setting are not.",
    "TARGETING": "Only whether an ad set declared a targeting object, and whether that object returned a geography, an age bound, or an interest/flexible spec; no value is retained.",
    "PLACEMENT": "The reviewed fixed ad-set read requests no placement field, and the fixed insights read requests no placement facet.",
    "CREATIVE": "Ad identity, the documented effective status, the creative association, and image-hash presence are read; no image hash, headline, or body copy is.",
    "DESTINATION_CONFIGURATION": "Only whether the ad's creative returned one of the two documented link fields this read requests; no URL, query, or tagging value is retained.",
    "TRACKING": "The reviewed fixed read requests no pixel, dataset, custom conversion, or event-health field.",
    "NATIVE_CONVERSION": "The fixed insights request projects impressions, clicks, spend, and reach only; no action or conversion field is requested.",
    "BROWSER_MEASUREMENT": "A local audit executes no browser and loads no page.",
    "POLICY": "Ad review state is read through the documented effective-status enumeration; the account's numeric status has no reviewed enumeration, and app review and Business Verification are not documented locally at all.",
    "DRIFT": "Drift compares two retained snapshot sets for one account; it needs both to be supplied.",
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
_INVENTORY_DIMENSIONS: Final[frozenset[AuditDimension]] = frozenset(
    {
        "STRUCTURE",
        "SPEND",
        "BIDS",
        "OBJECTIVE",
        "TARGETING",
        "DESTINATION_CONFIGURATION",
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
            if dimension in {"DELIVERY", "SPEND"} and context.report.performance_rows:
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
# finding can be produced that neither a recommendation nor an explicit
# declined outcome accounts for.  `tests/test_meta_audit.py` pins the partition.
_OBSERVE_DELIVERY_ABSENCE: Final[set[str]] = {"MA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"}
_OBSERVE_INCONSISTENT_ROWS: Final[set[str]] = {
    "MA_SPEND_WITHOUT_IMPRESSIONS",
    "MA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
    "MA_DELIVERY_REACH_EXCEEDS_IMPRESSIONS",
    "MA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
}
_OBSERVE_DRIFT: Final[set[str]] = {
    "MA_DRIFT_STATE_CHANGED",
    "MA_DRIFT_ABSENT_FROM_LATER_INVENTORY",
    "MA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
}
_PAUSE_RULES: Final[set[str]] = {
    "MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET",
    "MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_SCHEDULE_END",
}
_DECLINED_UPDATE: Final[set[str]] = {
    "MA_STRUCTURE_PARENT_PAUSE_PROPAGATED",
    "MA_SPEND_LIFETIME_BUDGET_WITHOUT_END_TIME",
    "MA_BIDS_STRATEGY_NOT_RETURNED",
    "MA_BIDS_CAPPED_STRATEGY_WITHOUT_BID_AMOUNT",
    "MA_TARGETING_NOT_DECLARED",
    "MA_TARGETING_GEO_ABSENT",
    "MA_TARGETING_AGE_ABSENT",
    "MA_TARGETING_INTEREST_ABSENT",
}
_DECLINED_ASSOCIATE: Final[set[str]] = {
    "MA_CREATIVE_NOT_ASSOCIATED",
    "MA_CREATIVE_IMAGE_HASH_ABSENT",
    "MA_DESTINATION_LINK_ABSENT",
}
_DECLINED_CREATE_CHILD: Final[set[str]] = {
    "MA_STRUCTURE_CAMPAIGN_WITHOUT_AD_SET",
    "MA_STRUCTURE_AD_SET_WITHOUT_AD",
}
_DECLINED_CREATE_RESERVED: Final[set[str]] = {"MA_OBJECTIVE_RESERVED_BUYING_TYPE_UNSUPPORTED"}
_DECLINED_PAUSE_AD_SCOPED: Final[set[str]] = {
    "MA_POLICY_AD_REVIEW_BLOCKED",
    "MA_POLICY_AD_NEEDS_ATTENTION",
}
_DECLINED_EVIDENCE: Final[set[str]] = {
    "MA_CREATIVE_VALUE_WITHHELD_BY_LOCAL_SCREEN",
    "MA_DRIFT_LOCAL_STATE_SHAPE_CHANGED",
    "MA_STRUCTURE_AD_SET_PARENT_NOT_RETAINED",
    "MA_STRUCTURE_AD_PARENT_NOT_RETAINED",
    "MA_STRUCTURE_STATUS_NOT_REVIEWED",
    "MA_DELIVERY_METRICS_MISSING",
    "MA_SPEND_BOTH_BUDGET_FIELDS_RETAINED",
    "MA_BIDS_STRATEGY_NOT_REVIEWED",
    "MA_OBJECTIVE_NOT_RETURNED",
    "MA_OBJECTIVE_NOT_REVIEWED",
    "MA_OBJECTIVE_BUYING_TYPE_NOT_RETURNED",
    "MA_CREATIVE_IMAGE_BYTES_NOT_PROVABLE",
    "MA_CREATIVE_LOCAL_BINDING_UNVERIFIED",
    "MA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
    "MA_POLICY_AD_REVIEW_PENDING",
    "MA_POLICY_ACCOUNT_STATUS_NOT_REVIEWED",
}
_DECLINED_NOT_READ: Final[set[str]] = {
    "MA_DELIVERY_REPORT_FRESHNESS_NOT_READ",
    "MA_BIDS_BILLING_EVENT_NOT_RETAINED",
    "MA_TARGETING_VALUES_NOT_READ",
    "MA_PLACEMENT_NOT_READ",
    "MA_CREATIVE_CONTENT_TEXT_NOT_READ",
    "MA_DESTINATION_VALUES_NOT_READ",
    "MA_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ",
    "MA_NATIVE_CONVERSION_NOT_READ",
    "MA_BROWSER_NOT_APPLICABLE",
    "MA_POLICY_APP_REVIEW_AND_VERIFICATION_NOT_READ",
}
RULE_OUTCOME_GROUPS: Final[tuple[frozenset[str], ...]] = tuple(
    frozenset(group)
    for group in (
        _OBSERVE_DELIVERY_ABSENCE,
        _OBSERVE_INCONSISTENT_ROWS,
        _OBSERVE_DRIFT,
        _PAUSE_RULES,
        _DECLINED_UPDATE,
        _DECLINED_ASSOCIATE,
        _DECLINED_CREATE_CHILD,
        _DECLINED_CREATE_RESERVED,
        _DECLINED_PAUSE_AD_SCOPED,
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
            # bound, and the row's own `rule_ids` carries the complete membership.
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
                    # unique on its own: two declined groups here share
                    # `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` with `CREATE`.
                    # A declined group's rule identifiers are disjoint from
                    # every other group's, so the first one in content order
                    # identifies the exact declined row within the subject's
                    # fixed size bound.
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
        hypothesis="A repeated bounded campaign insights read may show delivery this retained report did not return.",
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
    # The one mutation this build can compile for an existing Meta campaign.
    # It is a suggestion to compile a plan, never a plan: it carries no digest,
    # no authorization, and nothing here is executed.
    #
    # Membership is exactly the rules the named route can act on, and about
    # which this hypothesis is true.  `plan meta-campaign-pause create` acts on
    # one campaign, and the hypothesis asserts that the named campaign declares
    # the documented `ACTIVE` effective status, so every bound rule has to be
    # campaign-scoped *and* guarded by `meta_ads_declares_serving_intent`.
    # Both rules below are.  `MA_SPEND_WITHOUT_IMPRESSIONS` is deliberately not:
    # it is a report-row rule whose resource is whatever grain the retained row
    # named -- a `meta_ads.ad.*` reference for the AD-grain insights read --
    # which no route in this build can pause.  It keeps its `OBSERVE` row.
    _propose(
        _PAUSE_RULES,
        operation_kind="PAUSE",
        hypothesis=(
            "A Meta campaign that declares the documented `ACTIVE` effective status, and whose "
            "retained read leaves its maximum spend or its end time unbounded, may be worth "
            f"pausing while it is investigated; the `{PAUSE_PLAN_ROUTE}` route can describe that pause as an offline plan."
        ),
        expected_impact=(
            "A pause would stop further spend on the named campaigns. Execution is unreleased in "
            "0.1.0, so nothing here pauses anything and this is not a compiled plan. The pause is "
            "the marketer's manual step in Meta Ads Manager; planning stays available offline."
        ),
        assumptions=(
            "A retained read that returned no budget, spend cap, or stop time is not proof that the campaign has none.",
            "A declared `ACTIVE` effective status is an intent to serve, not an observation of delivery.",
            "Pausing is reversible in Meta Ads Manager, but the resulting delivery gap is not.",
            f"This names the `{PAUSE_PLAN_ROUTE}` route only, which describes the pause as an offline plan. Nothing executes a plan in 0.1.0, so the marketer makes the pause by hand.",
        ),
        confidence="MEDIUM",
        risk="MEDIUM",
        completeness=_NOT_ANALYTICS,
    )
    _no_recommendation(
        _DECLINED_UPDATE,
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="Reconciling a status, an end time, a bid, or a targeting object would require a Meta update operation, which this build cannot compile.",
        considered="UPDATE",
    )
    _no_recommendation(
        _DECLINED_ASSOCIATE,
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="Associating a creative, an image, or a destination link with an existing Meta ad is not a compilable operation in this build; the offline compiler only produces a complete new paused candidate.",
        considered="ASSOCIATE",
    )
    _no_recommendation(
        _DECLINED_CREATE_CHILD,
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="The offline Meta compiler produces only a complete new paused campaign, ad-set, and single-image ad candidate; it cannot compile a child into an existing retained parent.",
        considered="CREATE",
    )
    _no_recommendation(
        _DECLINED_CREATE_RESERVED,
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="This build's offline Meta compiler produces auction campaigns only, so it proposes no change to a reach-and-frequency reservation that already exists.",
        considered="CREATE",
    )
    _no_recommendation(
        _DECLINED_PAUSE_AD_SCOPED,
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="These findings name one ad. The only Meta pause route in this build acts on a campaign, so pausing the named ad is not a compilable operation and pausing its whole campaign is not what the finding supports.",
        considered="PAUSE",
    )
    _no_recommendation(
        _DECLINED_EVIDENCE,
        reason="EVIDENCE_INSUFFICIENT",
        detail="These findings record missing, unreviewed, or unverifiable evidence. Resolving them needs a documentation review or a further read, not an advertising operation.",
    )
    _no_recommendation(
        _DECLINED_NOT_READ,
        reason="COVERAGE_NOT_READ",
        detail="These dimensions are unread, presence-only, retained-state-limited, or not applicable in this build's fixed Meta read. An unread dimension yields no recommendation and is never reported as healthy.",
    )
    return recommendations, absences, exceeded


def evaluate(context: ProviderAuditContext) -> ProviderAuditSections:
    """Evaluate the Meta Ads rule table over retained records only."""
    if context.provider != PROVIDER:
        raise ValueError("The Meta rule table evaluates Meta Ads evidence only")
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
        findings.extend(_ad_set_setting_findings(grouped, window, statuses))
        findings.extend(_ad_findings(context, grouped, window, statuses))
    elif context.evidence.creative_catalog is not None:
        # A catalog with no retained ads produces no absence claim -- see
        # `_ad_findings` -- but a whole bounded read that returned no ad at all
        # is still a read boundary, and the local bindings it cannot match are
        # still evaluated here rather than silently skipped.
        findings.extend(_ad_findings(context, grouped, window, statuses))
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
        # No reviewed Meta analytics read can be published as exhaustive by
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
