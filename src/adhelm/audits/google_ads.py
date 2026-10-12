# ruff: noqa: E501
"""Deterministic Google Ads audit rules over retained local records.

This module makes **no provider request**.  It evaluates the retained
`PERFORMANCE_OBSERVATION` report the caller selected, the retained
`RESOURCE_SNAPSHOT` set one completed inventory read appended, an optional
earlier snapshot set, and an optional local creative catalog.  Authorization,
developer-token, quota, `RESOURCE_EXHAUSTED`, and pagination handling therefore
stays entirely with the read adapter that produced those records; nothing here
can leak a response body because no response is available here.

Four honesty bounds shape every rule:

* The reviewed `googleAds:search` reads walk documented page tokens to
  exhaustion and additionally request `searchSettings.returnTotalResultsCount`.
  A read whose provider declared that total, and whose retained rows equal it,
  is the strongest completeness this build can show -- and the shared audit
  vocabulary in :mod:`adhelm.audits` still has no exhaustive
  `AnalyticsCompleteness` member at all.  Every analytics-derived finding
  therefore stays `BOUNDED_NON_EXHAUSTIVE` and never claims a total.  A
  retained report or inventory that did *not* prove its boundary produces its
  own explicit finding rather than being quietly treated as whole.
* The fixed reads return identity, the customer's test-account flag, hierarchy,
  declared status, advertising channel type, bidding strategy type, exact-cent
  budget and CPC bid, budget period and sharing, network settings, geo target
  type setting, and **presence facts** for ad scheduling, responsive-search
  components, the final URL, and the ad policy summary.  Presence is not
  content: no keyword, location, headline, description, or landing page is
  read, so those dimensions are `PARTIAL` at best and are never reported as
  healthy.
* The developer token's Test, Basic or Standard access level is not returned by
  any reviewed read.  It is disclosed as `NOT_DOCUMENTED_LOCALLY` and never
  assumed, so no finding here rests on an assumed quota or environment.
* Placement, conversion actions, campaign conversion goals, native conversion
  metrics, tracking templates, report freshness, and the attribution window are
  not in the reviewed fixed reads, so their dimensions are `NOT_READ`.  That
  ceiling is read from `GOOGLE_ADS_READ_SURFACES` rather than restated here, so
  a dimension can never be reported as read because a caller supplied records
  for it.

`docs/implementation/google-ads-audit-recommendations.md` carries the rule
table, coverage map, and evidence limits in prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
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
from adhelm.providers.google_ads import (
    GOOGLE_ADS_ACCELERATED_BUDGET_DELIVERY,
    GOOGLE_ADS_AD_SCHEDULE_ABSENT,
    GOOGLE_ADS_BUDGET_DELIVERY_METHOD_PREFIX,
    GOOGLE_ADS_FINAL_URL_ABSENT,
    GOOGLE_ADS_GEO_TARGET_TYPE_NOT_RETURNED,
    GOOGLE_ADS_NETWORK_CONTENT_ENABLED,
    GOOGLE_ADS_NETWORK_PARTNER_SEARCH_ENABLED,
    GOOGLE_ADS_NETWORK_SETTINGS_NOT_RETURNED,
    GOOGLE_ADS_PAGINATION_TOTAL_NOT_DECLARED,
    GOOGLE_ADS_POLICY_APPROVAL_PREFIX,
    GOOGLE_ADS_POLICY_APPROVAL_STATUS_NOT_RETURNED,
    GOOGLE_ADS_POLICY_REVIEW_PREFIX,
    GOOGLE_ADS_POLICY_REVIEW_STATUS_NOT_RETURNED,
    GOOGLE_ADS_READ_SURFACES,
    GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED,
    GOOGLE_ADS_RESTRICTED_POLICY_APPROVAL_STATUSES,
    GOOGLE_ADS_REVIEWED_AD_GROUP_STATUSES,
    GOOGLE_ADS_REVIEWED_AD_STATUSES,
    GOOGLE_ADS_REVIEWED_CAMPAIGN_STATUSES,
    GOOGLE_ADS_REVIEWED_POLICY_APPROVAL_STATUSES,
    GOOGLE_ADS_REVIEWED_POLICY_REVIEW_STATUSES,
    GOOGLE_ADS_RSA_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM,
    GOOGLE_ADS_RSA_HEADLINES_BELOW_DOCUMENTED_MINIMUM,
    GOOGLE_ADS_STATE_SHAPE_PREFIX,
    GOOGLE_ADS_TEST_ACCOUNT_DECLARED,
    google_ads_declares_serving_intent,
)
from adhelm.reporting import NormalizedReport, PerformanceRow

PROVIDER: Final[Provider] = "google_ads"
# Bump this when a rule's semantics change.  It is part of every finding's
# content identity, so an audit produced by a different rule table can never be
# mistaken for one produced by this table.
RULE_TABLE_VERSION: Final = "google_ads.rules.2026-09-09.1"
_BOUNDED: Final[AnalyticsCompleteness] = "BOUNDED_NON_EXHAUSTIVE"
_NOT_ANALYTICS: Final[AnalyticsCompleteness] = "NOT_APPLICABLE"
# The one route this build can compile for an existing Google Ads campaign.
# Naming it in a recommendation is a vocabulary statement about what the
# product could be asked to compile next; it is never a plan and never an
# authorization.
PAUSE_PLAN_ROUTE: Final = "plan google-ads-campaign-pause create"
# The one advertising channel this build's offline compiler produces.
COMPILABLE_CHANNEL: Final = "SEARCH"
# The retained report envelope declares this when the adapter walked every page
# token *and* the provider declared a total result count equal to the rows kept.
_REPORT_COMPLETE: Final = "COMPLETE"
_REVIEWED_STATUSES: Final[dict[str, tuple[str, ...]]] = {
    "CAMPAIGN": GOOGLE_ADS_REVIEWED_CAMPAIGN_STATUSES,
    "AD_GROUP": GOOGLE_ADS_REVIEWED_AD_GROUP_STATUSES,
    "AD": GOOGLE_ADS_REVIEWED_AD_STATUSES,
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
                rule_id="GA_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP",
                finding_type="GOOGLE_ADS_CAMPAIGN_WITHOUT_AD_GROUP",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign has no retained ad group in the same inventory read.",
                suggested_action="Confirm the campaign in the Google Ads interface, or read the bounded hierarchy again; a campaign with no ad group cannot serve.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_STRUCTURE_AD_GROUP_WITHOUT_AD",
                finding_type="GOOGLE_ADS_AD_GROUP_WITHOUT_AD",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads ad group has no retained ad in the same inventory read.",
                suggested_action="Confirm the ad group in the Google Ads interface; an ad group with no ad cannot serve.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED",
                finding_type="GOOGLE_ADS_AD_GROUP_PARENT_CAMPAIGN_NOT_RETAINED",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads ad group names a parent campaign that is absent from the same retained inventory.",
                suggested_action="Read the bounded hierarchy again; an ad group whose parent campaign is missing cannot be evaluated in context.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_STRUCTURE_AD_PARENT_NOT_RETAINED",
                finding_type="GOOGLE_ADS_AD_PARENT_AD_GROUP_NOT_RETAINED",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads ad names a parent that is absent from the same retained inventory.",
                suggested_action="Read the bounded hierarchy again; an ad whose parent is missing cannot be evaluated in context.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_STRUCTURE_SERVING_INTENT_MISMATCH",
                finding_type="GOOGLE_ADS_SERVING_INTENT_MISMATCH_WITH_PARENT",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads resource declares ENABLED while its retained parent does not, so its declared intent cannot be met.",
                suggested_action="Reconcile the parent and child statuses in the Google Ads interface; this audit reads declared status only, never serving state.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_STRUCTURE_STATUS_NOT_REVIEWED",
                finding_type="GOOGLE_ADS_STATUS_VALUE_NOT_IN_REVIEWED_ENUM",
                dimension="STRUCTURE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads resource carries a status outside the reviewed v25 enumeration this connector pins.",
                suggested_action="Recheck the Google Ads v25 status documentation before interpreting the value; the audit does not guess its meaning.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_STRUCTURE_INVENTORY_PAGINATION_NOT_PROVEN",
                finding_type="GOOGLE_ADS_INVENTORY_TOTAL_RESULT_COUNT_NOT_DECLARED",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads inventory read does not record a declared total result count for every search the fixed request asked one for -- either a search returned none, or the read retained no account snapshot to record it -- so page-token exhaustion is all this read can show.",
                suggested_action="Run `adhelm inventory` again for this profile; treat the retained hierarchy as possibly partial until a whole read declares its total.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS",
                finding_type="GOOGLE_ADS_ENABLED_CAMPAIGN_WITHOUT_RETAINED_IMPRESSIONS",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="LOW",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained Google Ads campaign declares ENABLED and the retained bounded report contains no impressions for it. The retained report is not an exhaustive provider claim, so this is not proof of zero delivery.",
                suggested_action="Read the same bounded campaign report again over a compatible window before treating the absence of rows as zero delivery.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
                finding_type="GOOGLE_ADS_CLICKS_EXCEED_IMPRESSIONS",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained Google Ads report row declares more clicks than impressions at its declared grain, which no consistent delivery aggregate produces.",
                suggested_action="Read the same bounded report again and compare the declared grain and window before using this row for any decision.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_DELIVERY_METRICS_MISSING",
                finding_type="GOOGLE_ADS_DELIVERY_METRICS_MISSING_ON_ROW",
                dimension="DELIVERY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="This retained Google Ads report row declares no impression or click count, so no delivery rate can be derived from it.",
                suggested_action="Read the same declared grain again with the fixed impression and click fields before interpreting delivery.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
                finding_type="GOOGLE_ADS_RETAINED_REPORT_DOES_NOT_DECLARE_COMPLETE_PAGINATION",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained Google Ads report does not declare the COMPLETE quality status the fixed read records when the provider declared a total result count matching every retained row, so its row set has no proven read boundary.",
                suggested_action="Run the fixed Google Ads report again and audit the result it retains; do not treat this row set as the whole window.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_DELIVERY_IMPRESSIONS_BELOW_DECLARED_MINIMUM",
                finding_type="GOOGLE_ADS_CAMPAIGN_IMPRESSIONS_BELOW_DECLARED_MINIMUM",
                dimension="DELIVERY",
                severity="LOW",
                confidence="MEDIUM",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="This retained Google Ads campaign's impressions in the selected bounded report are below the minimum the customer declared in their audit policy. This audit applies no threshold the customer did not declare.",
                suggested_action="Compare the declared minimum against the campaign in the Google Ads interface; a bounded retained report is not the whole window.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_SPEND_WITHOUT_IMPRESSIONS",
                finding_type="GOOGLE_ADS_SPEND_WITHOUT_IMPRESSIONS",
                dimension="SPEND",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_BOUNDED,
                observation="This retained Google Ads report row declares spend with zero impressions at its declared grain.",
                suggested_action="Read the same bounded report again and reconcile the row in the Google Ads interface before drawing a spend conclusion.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_SPEND_BUDGET_NOT_RETURNED",
                finding_type="GOOGLE_ADS_ENABLED_CAMPAIGN_WITHOUT_RETAINED_BUDGET",
                dimension="SPEND",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign declares ENABLED and carries no campaign budget amount in the same read, so this audit can bound no daily or period spend for it.",
                suggested_action="Confirm the campaign's budget in the Google Ads interface; an absent budget in a retained read is not a proven unlimited budget, and it is not a proven limit either.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_SPEND_BUDGET_PERIOD_NOT_RETURNED",
                finding_type="GOOGLE_ADS_CAMPAIGN_BUDGET_PERIOD_NOT_RETURNED",
                dimension="SPEND",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign returned no budget period, so a retained budget amount cannot be read as daily or as a custom-period total.",
                suggested_action="Confirm the budget period in the Google Ads interface; the audit never assumes a retained amount is a daily budget.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_SPEND_BUDGET_EXPLICITLY_SHARED",
                finding_type="GOOGLE_ADS_CAMPAIGN_USES_AN_EXPLICITLY_SHARED_BUDGET",
                dimension="SPEND",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign draws on an explicitly shared budget, so the retained amount bounds a pool rather than this campaign, and other campaigns can consume it.",
                suggested_action="Review the shared budget and every campaign attached to it in the Google Ads interface; this audit does not read the other campaigns on that budget.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_SPEND_SCHEDULE_END_NOT_RETURNED",
                finding_type="GOOGLE_ADS_ENABLED_CAMPAIGN_WITHOUT_RETAINED_END_DATE",
                dimension="SPEND",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign declares ENABLED and carries no end date, so no scheduled stop bounds its exposure in this read.",
                suggested_action="Confirm the campaign schedule in the Google Ads interface; an absent end date in a retained read is not proof that the campaign runs forever.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_SPEND_ROW_COST_ROUNDED_TO_MINOR_UNITS",
                finding_type="GOOGLE_ADS_ROW_COST_ROUNDED_FROM_NATIVE_MICROS",
                dimension="SPEND",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="This retained Google Ads report row carries a cent amount rounded HALF_EVEN from the provider's native cost micros, which the row also retains exactly.",
                suggested_action="Sum the retained native micros before applying any rounding policy; do not recompute a total from the rounded row cents.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_BIDS_AD_GROUP_WITHOUT_CPC_BID",
                finding_type="GOOGLE_ADS_AD_GROUP_WITHOUT_RETAINED_CPC_BID",
                dimension="BIDS",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads ad group carries no CPC bid amount, so this audit can bound no per-click cost for it.",
                suggested_action="Confirm the ad group's bid in the Google Ads interface before relying on any cost expectation; an automated campaign strategy may set it outside the ad group.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_BIDS_CPC_BID_ABOVE_DECLARED_MAXIMUM",
                finding_type="GOOGLE_ADS_AD_GROUP_CPC_BID_ABOVE_DECLARED_MAXIMUM",
                dimension="BIDS",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads ad group's CPC bid exceeds the maximum the customer declared in their audit policy. This audit applies no threshold the customer did not declare.",
                suggested_action="Reconcile the ad-group bid against the declared maximum in the Google Ads interface; the audit changes nothing.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_BIDS_AUTOMATED_STRATEGY_NOT_LOCALLY_BOUNDED",
                finding_type="GOOGLE_ADS_CAMPAIGN_AUTOMATED_BID_STRATEGY_NOT_LOCALLY_BOUNDED",
                dimension="BIDS",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign uses a bidding strategy other than MANUAL_CPC. The fixed read returns the strategy type only, not its target CPA, target ROAS, or bid ceiling, so no per-click cost is bounded locally.",
                suggested_action="Review the strategy's targets and any bid limits in the Google Ads interface; this audit makes no pacing or cost claim for an automated strategy.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_BIDS_STRATEGY_DETAIL_NOT_READ",
                finding_type="GOOGLE_ADS_BID_STRATEGY_DETAIL_AND_PACING_NOT_READ",
                dimension="BIDS",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Beyond the campaign bidding strategy type and the ad-group CPC bid, no portfolio strategy, target CPA or ROAS, bid ceiling, pacing, or bid-modifier setting is requested by this build's fixed Google Ads read.",
                suggested_action="Review bid strategy detail and pacing in the Google Ads interface; this audit makes no pacing claim.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_OBJECTIVE_CHANNEL_OUTSIDE_COMPILABLE_SUBSET",
                finding_type="GOOGLE_ADS_CAMPAIGN_CHANNEL_OUTSIDE_COMPILABLE_SUBSET",
                dimension="OBJECTIVE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign declares an advertising channel type this build's offline compiler does not produce; only a Search campaign is compilable here.",
                suggested_action="Manage this campaign in the Google Ads interface; this product compiles no Display, Performance Max, Shopping, or Video candidate and proposes no change to one.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_OBJECTIVE_CONVERSION_GOAL_NOT_READ",
                finding_type="GOOGLE_ADS_CAMPAIGN_CONVERSION_GOAL_NOT_READ",
                dimension="OBJECTIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Campaign conversion goals and customer-level goal settings are not requested by this build's fixed Google Ads read, so the optimization target behind a bidding strategy is unknown to this audit.",
                suggested_action="Review conversion goals in the Google Ads interface; an unread goal is never reported as configured.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_TARGETING_AD_SCHEDULE_ABSENT",
                finding_type="GOOGLE_ADS_ENABLED_CAMPAIGN_WITHOUT_AD_SCHEDULE_CRITERION",
                dimension="TARGETING",
                severity="LOW",
                confidence="MEDIUM",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign declares ENABLED and the bounded criterion read returned no AD_SCHEDULE criterion for it. Google documents that a campaign with no ad schedule is eligible to run at any hour of any day.",
                suggested_action="Confirm the campaign's ad schedule in the Google Ads interface; this audit reads only whether a schedule criterion exists, never its days, hours, or bid modifiers.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_TARGETING_CONTENT_NETWORK_ENABLED",
                finding_type="GOOGLE_ADS_ENABLED_CAMPAIGN_TARGETS_THE_CONTENT_NETWORK",
                dimension="TARGETING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign declares ENABLED and its network settings opt it into the Display content network, so it can deliver and spend outside Search.",
                suggested_action="Confirm in the Google Ads interface whether Display delivery is intended for this campaign; the fixed read returns the network flags only, never where delivery actually occurred.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_TARGETING_PARTNER_SEARCH_ENABLED",
                finding_type="GOOGLE_ADS_ENABLED_CAMPAIGN_TARGETS_SEARCH_PARTNERS",
                dimension="TARGETING",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign declares ENABLED and its network settings opt it into Google search partner sites.",
                suggested_action="Confirm in the Google Ads interface whether search-partner delivery is intended; this audit reads the flag only and makes no performance claim about partners.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_TARGETING_NETWORK_SETTINGS_NOT_RETURNED",
                finding_type="GOOGLE_ADS_CAMPAIGN_NETWORK_SETTINGS_NOT_RETURNED",
                dimension="TARGETING",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign returned no network settings, so this audit cannot say which Google networks it is eligible to deliver on.",
                suggested_action="Read the bounded campaign inventory again; absent network flags are not proof that a network is switched off.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_TARGETING_GEO_TARGET_TYPE_NOT_RETURNED",
                finding_type="GOOGLE_ADS_CAMPAIGN_GEO_TARGET_TYPE_SETTING_NOT_RETURNED",
                dimension="TARGETING",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign returned no geo target type setting, so whether it targets presence or interest in a location is unknown to this audit.",
                suggested_action="Confirm the location options in the Google Ads interface; this audit reads the setting only, never the targeted locations.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_TARGETING_CRITERION_VALUES_NOT_READ",
                finding_type="GOOGLE_ADS_TARGETING_CRITERION_VALUES_NOT_READ",
                dimension="TARGETING",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Keyword, negative-keyword, location, language, audience, and device criterion values are deliberately not retained by this build's fixed Google Ads read; only whether an AD_SCHEDULE criterion exists is known.",
                suggested_action="Review the exact criteria in the Google Ads interface; this audit neither reads nor infers a keyword, location, or audience value.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_PLACEMENT_NOT_READ",
                finding_type="GOOGLE_ADS_PLACEMENT_NOT_READ",
                dimension="PLACEMENT",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="No placement view, placement criterion, or placement report facet is requested by this build's fixed Google Ads reads, so no placement claim is available for this account.",
                suggested_action="Review placements in the Google Ads interface; this audit makes no placement claim and does not treat an unread field as an absence of placements.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_CREATIVE_RESPONSIVE_SEARCH_AD_NOT_RETURNED",
                finding_type="GOOGLE_ADS_AD_WITHOUT_RETAINED_RESPONSIVE_SEARCH_CREATIVE",
                dimension="CREATIVE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads ad returned no responsive-search-ad component set, so no component count is available for it. Only responsive search ads are projected by this build's fixed read.",
                suggested_action="Review the ad in the Google Ads interface; an ad of another type is not evaluated for responsive-search components here.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_CREATIVE_HEADLINES_BELOW_DOCUMENTED_MINIMUM",
                finding_type="GOOGLE_ADS_RESPONSIVE_SEARCH_AD_HEADLINES_BELOW_DOCUMENTED_MINIMUM",
                dimension="CREATIVE",
                severity="HIGH",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads responsive search ad declared fewer headlines than the documented minimum of three. Only the number of components was read; no headline text was retained.",
                suggested_action="Review the ad's headlines in the Google Ads interface; this audit reports a component count against a documented minimum and reads no copy.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_CREATIVE_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM",
                finding_type="GOOGLE_ADS_RESPONSIVE_SEARCH_AD_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM",
                dimension="CREATIVE",
                severity="HIGH",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads responsive search ad declared fewer descriptions than the documented minimum of two. Only the number of components was read; no description text was retained.",
                suggested_action="Review the ad's descriptions in the Google Ads interface; this audit reports a component count against a documented minimum and reads no copy.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_CREATIVE_CONTENT_TEXT_NOT_READ",
                finding_type="GOOGLE_ADS_CREATIVE_CONTENT_TEXT_NOT_READ",
                dimension="CREATIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Headline and description text, component pinning, asset performance labels, and image assets are deliberately not retained by this build's fixed Google Ads read; only identity, type, and component counts are.",
                suggested_action="Review creative copy, pinning, and assets in the Google Ads interface; this audit makes no creative-content claim.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_CREATIVE_LOCAL_BINDING_UNVERIFIED",
                finding_type="GOOGLE_ADS_LOCAL_CREATIVE_BINDING_UNVERIFIED",
                dimension="CREATIVE",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="A local creative version declares a Google Ads ad reference, but the retained inventory carries no provider-verified creative association for it.",
                suggested_action="Treat the local binding as a declaration only; an independently reviewed provider association is still required.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
                finding_type="GOOGLE_ADS_LOCAL_CREATIVE_BINDING_WITHOUT_RETAINED_RESOURCE",
                dimension="CREATIVE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="A local creative version declares a Google Ads ad reference that is absent from the retained inventory for this account.",
                suggested_action="Read the bounded hierarchy again and reconcile the local declaration; do not assume the provider resource exists.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_DESTINATION_FINAL_URL_ABSENT",
                finding_type="GOOGLE_ADS_AD_WITHOUT_FINAL_URL",
                dimension="DESTINATION_CONFIGURATION",
                severity="HIGH",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads ad returned no final URL, which the documented ad contract requires for a serving ad.",
                suggested_action="Confirm the ad's final URL in the Google Ads interface; this audit reads only whether a destination was present, never its value.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_DESTINATION_VALUES_NOT_READ",
                finding_type="GOOGLE_ADS_DESTINATION_AND_TRACKING_VALUES_NOT_READ",
                dimension="DESTINATION_CONFIGURATION",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Final URL values, display paths, tracking templates, final URL suffixes, and custom URL parameters are deliberately not retained by this build's fixed Google Ads read; only whether an ad declared a final URL is known.",
                suggested_action="Review destination URLs and tracking templates in the Google Ads interface; this audit reads no URL or query value.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_TRACKING_CONVERSION_ACTION_NOT_READ",
                finding_type="GOOGLE_ADS_CONVERSION_ACTION_INVENTORY_NOT_READ",
                dimension="TRACKING",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Conversion actions, their tag status, and their campaign associations are not requested by this build's fixed Google Ads read, so no native conversion definition is bound to any retained campaign here.",
                suggested_action="Verify conversion actions and Google tag status through their own reviewed surface; a delivery aggregate cannot establish them.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_TRACKING_TEMPLATE_AND_PARAMETERS_NOT_READ",
                finding_type="GOOGLE_ADS_TRACKING_TEMPLATE_AND_URL_PARAMETERS_NOT_READ",
                dimension="TRACKING",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Account, campaign, ad-group, and ad tracking templates, final URL suffixes, and custom parameters are not requested by this build's fixed Google Ads read, so no campaign tagging claim is available.",
                suggested_action="Review tracking templates and URL options in the Google Ads interface; this audit reads no tagging value.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_NATIVE_CONVERSION_NOT_READ",
                finding_type="GOOGLE_ADS_NATIVE_CONVERSION_METRICS_NOT_READ",
                dimension="NATIVE_CONVERSION",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The fixed Google Ads delivery request projects date, identifiers, impressions, clicks, and cost micros only, so no native conversion count, conversion action, or attribution context is available for this account.",
                suggested_action="Do not infer a conversion count from clicks; a native conversion report read is required and is not implemented here.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_BROWSER_NOT_APPLICABLE",
                finding_type="GOOGLE_ADS_BROWSER_MEASUREMENT_NOT_APPLICABLE_LOCAL_AUDIT",
                dimension="BROWSER_MEASUREMENT",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This local audit executes no browser and loads no page, so browser measurement is outside what it can evaluate for this account.",
                suggested_action="Use the separate reviewed browser verification path; no local audit result is evidence of real-browser firing.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_POLICY_APPROVAL_RESTRICTED",
                finding_type="GOOGLE_ADS_AD_POLICY_APPROVAL_RESTRICTS_SERVING",
                dimension="POLICY",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads ad carries a policy approval status Google documents as disapproved or limited, so it does not serve as written everywhere its campaign targets.",
                suggested_action="Open the ad's policy details in the Google Ads interface; the fixed read returns the summary status only, never the policy topics behind it.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_POLICY_APPROVAL_STATUS_NOT_RETURNED",
                finding_type="GOOGLE_ADS_AD_POLICY_APPROVAL_STATUS_NOT_RETURNED",
                dimension="POLICY",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads ad returned no policy approval status, so neither an approved nor a restricted policy state can be attributed to it.",
                suggested_action="Read the bounded ad inventory again; an absent approval status is not an approval.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_POLICY_APPROVAL_STATUS_NOT_REVIEWED",
                finding_type="GOOGLE_ADS_AD_POLICY_APPROVAL_STATUS_NOT_IN_REVIEWED_ENUM",
                dimension="POLICY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads ad carries a policy approval status outside the reviewed v25 enumeration this connector pins, so the audit does not classify it as approved or restricted.",
                suggested_action="Recheck the Google Ads v25 policy approval status documentation before relying on the value.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_POLICY_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED",
                finding_type="GOOGLE_ADS_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED",
                dimension="POLICY",
                severity="INFO",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Google documents Test, Basic and Standard developer-token access levels, and no reviewed v25 read returns the level this account was reached with. The retained read therefore discloses it as not documented locally rather than assuming a tier.",
                suggested_action="Check the access level in the API Center of the manager account that owns the developer token; a Test token reaches test accounts only, and quotas differ by level.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_POLICY_TEST_ACCOUNT_DECLARED",
                finding_type="GOOGLE_ADS_CUSTOMER_DECLARES_A_TEST_ACCOUNT",
                dimension="POLICY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads customer declares itself a test account. Google documents that test accounts do not serve real ads and do not bill, so retained delivery and spend from this account describe no real campaign.",
                suggested_action="Audit a production customer before drawing any delivery or spend conclusion; nothing retained from a test account is evidence of live performance.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_POLICY_ACCOUNT_STATUS_NOT_READ",
                finding_type="GOOGLE_ADS_CUSTOMER_STATUS_NOT_RETAINED",
                dimension="POLICY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The customer status and any account-level policy or billing state are not requested by this build's fixed Google Ads read, so no account standing claim is available here.",
                suggested_action="Review account standing and billing in the Google Ads interface; an unread account status is not an account in good standing.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_POLICY_REVIEW_STATUS_NOT_RETURNED",
                finding_type="GOOGLE_ADS_AD_POLICY_REVIEW_STATUS_NOT_RETURNED",
                dimension="POLICY",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads ad returned no policy review status, so whether Google has finished reviewing it, or is holding it under appeal, cannot be attributed to it.",
                suggested_action="Read the bounded ad inventory again; an absent review status is not a completed review.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_POLICY_REVIEW_STATUS_NOT_REVIEWED",
                finding_type="GOOGLE_ADS_AD_POLICY_REVIEW_STATUS_NOT_IN_REVIEWED_ENUM",
                dimension="POLICY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads ad carries a policy review status outside the reviewed v25 enumeration this connector pins, so the audit does not decide what the value means for serving.",
                suggested_action="Recheck the Google Ads v25 policy review status documentation before relying on the value.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_SPEND_BUDGET_ACCELERATED_DELIVERY",
                finding_type="GOOGLE_ADS_CAMPAIGN_BUDGET_USES_ACCELERATED_DELIVERY",
                dimension="SPEND",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained Google Ads campaign declares ENABLED and its budget uses accelerated delivery, which Google documents as spending the budget as quickly as possible rather than evenly across the period.",
                suggested_action="Confirm the budget delivery method in the Google Ads interface; this audit reads the enum only and makes no claim about how the budget was actually spent.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_DRIFT_LOCAL_STATE_SHAPE_CHANGED",
                finding_type="GOOGLE_ADS_LOCAL_RETAINED_STATE_SHAPE_DIFFERS",
                dimension="DRIFT",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Two retained snapshots of this Google Ads resource hold different normalized state fields. A field on only one side is either a field this connector began or stopped retaining, or a value that became absent; the two are indistinguishable locally, so no change at Google is claimed from it.",
                suggested_action="Compare the two snapshots' retained connector state-shape versions before reading anything into the difference; only fields both reads returned are compared as values.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_DRIFT_STATE_CHANGED",
                finding_type="GOOGLE_ADS_OUT_OF_BAND_STATE_CHANGE_OBSERVED",
                dimension="DRIFT",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Two retained snapshots of this Google Ads resource differ, and this product applied no change between them. The actor is UNKNOWN and the change occurred somewhere inside the recorded observation interval.",
                suggested_action="Reconcile the change against the Google Ads interface before planning anything; no exact external change time or actor is available.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_DRIFT_ABSENT_FROM_LATER_INVENTORY",
                finding_type="GOOGLE_ADS_RESOURCE_ABSENT_FROM_LATER_INVENTORY",
                dimension="DRIFT",
                severity="MEDIUM",
                confidence="MEDIUM",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This Google Ads resource is present in the earlier retained inventory and absent from the later one. Absence is not proof of removal; the actor is UNKNOWN.",
                suggested_action="Read the bounded hierarchy again and confirm the resource in the Google Ads interface before treating it as removed.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
                finding_type="GOOGLE_ADS_RESOURCE_ABSENT_FROM_EARLIER_INVENTORY",
                dimension="DRIFT",
                severity="LOW",
                confidence="MEDIUM",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This Google Ads resource is present in the later retained inventory and absent from the earlier one. This product created nothing; the actor is UNKNOWN.",
                suggested_action="Confirm who created the resource in the Google Ads interface; no exact external creation time or actor is available.",
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
    """Whether a retained Google Ads row records positive spend.

    The Google read retains the provider's exact `cost_micros` decimal string
    beside the rounded cent amount, so the native value is compared first and a
    subminor cost is never rounded away into an apparent zero.
    """
    native = row.provider_extensions.get("google_ads_cost_micros")
    if isinstance(native, str):
        return Decimal(native) > 0
    return row.spend is not None and row.spend.minor_units > 0


def _absence_is_evidence(inventory: RetainedInventory) -> bool:
    """Whether what a retained record set omits carries any information."""
    return inventory.completeness != "READ_BOUNDARY_UNVERIFIED"


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
    digests = {record.snapshot.resource_ref: record.record_digest for record in inventory.records}
    # Only a campaign or an ad group declares a serving status.  The account
    # snapshot's effective status is the customer status this build does not
    # read, so comparing a child against it would manufacture a mismatch from
    # a value that means nothing about serving.
    statuses = {
        record.snapshot.resource_ref: record.snapshot.effective_status
        for record in inventory.records
        if record.snapshot.resource_type in {"CAMPAIGN", "AD_GROUP"}
    }
    for record in inventory.records:
        for parent in record.snapshot.parent_resource_refs:
            parents.setdefault(parent, []).append(record.snapshot.resource_ref)
    childless = {
        "CAMPAIGN": ("GA_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP", "AD_GROUP"),
        "AD_GROUP": ("GA_STRUCTURE_AD_GROUP_WITHOUT_AD", "AD"),
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
        "AD_GROUP": ("GA_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED", "CAMPAIGN"),
        "AD": ("GA_STRUCTURE_AD_PARENT_NOT_RETAINED", "AD_GROUP"),
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
    for resource_type in ("CAMPAIGN", "AD_GROUP", "AD"):
        for record in grouped[resource_type]:
            if not google_ads_declares_serving_intent(record.snapshot.effective_status):
                continue
            for parent in record.snapshot.parent_resource_refs:
                parent_status = statuses.get(parent)
                if parent_status is None or google_ads_declares_serving_intent(parent_status):
                    continue
                findings.append(
                    _emit(
                        RULES["GA_STRUCTURE_SERVING_INTENT_MISMATCH"],
                        resource_refs=(record.snapshot.resource_ref, parent),
                        evidence_refs=(record.record_digest, digests[parent]),
                        window=window,
                        coverage_status=status,
                    )
                )
    for resource_type, reviewed in _REVIEWED_STATUSES.items():
        for record in grouped[resource_type]:
            if record.snapshot.effective_status not in reviewed:
                findings.append(
                    _emit(
                        RULES["GA_STRUCTURE_STATUS_NOT_REVIEWED"],
                        resource_refs=(record.snapshot.resource_ref,),
                        evidence_refs=(record.record_digest,),
                        window=window,
                        coverage_status=status,
                    )
                )
    return findings


def _append(
    findings: list[ProviderAuditFinding],
    rule_id: str,
    *,
    refs: tuple[str, ...],
    evidence: tuple[str, ...],
    window: AuditDataWindow,
    coverage: dict[AuditDimension, ProviderCoverageStatus],
) -> None:
    """Emit one rule's finding at its dimension's coverage status."""
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


def _campaign_setting_findings(
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
    coverage: dict[AuditDimension, ProviderCoverageStatus],
) -> list[ProviderAuditFinding]:
    """Spend, bid, objective, and targeting rules over retained campaign settings."""
    findings: list[ProviderAuditFinding] = []
    for record in grouped["CAMPAIGN"]:
        state = record.snapshot.normalized_state
        quality = _quality(record)
        refs = (record.snapshot.resource_ref,)
        evidence = (record.record_digest,)
        serving = google_ads_declares_serving_intent(record.snapshot.effective_status)

        def emit(
            rule_id: str, refs: tuple[str, ...] = refs, evidence: tuple[str, ...] = evidence
        ) -> None:
            _append(
                findings, rule_id, refs=refs, evidence=evidence, window=window, coverage=coverage
            )

        # Spend exposure rules are guarded by the documented serving intent: a
        # campaign that does not declare an intent to serve is not spending,
        # and reporting an unbounded budget for it would state a consequence
        # the retained status contradicts.
        if serving and state.get("budget_minor") is None:
            emit("GA_SPEND_BUDGET_NOT_RETURNED")
        if serving and state.get("schedule_end") is None:
            emit("GA_SPEND_SCHEDULE_END_NOT_RETURNED")
        if "GOOGLE_ADS_BUDGET_TYPE_NOT_RETURNED" in quality:
            emit("GA_SPEND_BUDGET_PERIOD_NOT_RETURNED")
        if "GOOGLE_ADS_BUDGET_EXPLICITLY_SHARED" in quality:
            emit("GA_SPEND_BUDGET_EXPLICITLY_SHARED")
        if serving and (
            GOOGLE_ADS_BUDGET_DELIVERY_METHOD_PREFIX + GOOGLE_ADS_ACCELERATED_BUDGET_DELIVERY
            in quality
        ):
            emit("GA_SPEND_BUDGET_ACCELERATED_DELIVERY")
        bid_strategy = state.get("bid_strategy")
        if isinstance(bid_strategy, str) and bid_strategy != "MANUAL_CPC":
            emit("GA_BIDS_AUTOMATED_STRATEGY_NOT_LOCALLY_BOUNDED")
        objective = state.get("objective")
        if isinstance(objective, str) and objective != COMPILABLE_CHANNEL:
            emit("GA_OBJECTIVE_CHANNEL_OUTSIDE_COMPILABLE_SUBSET")
        if serving and GOOGLE_ADS_AD_SCHEDULE_ABSENT in quality:
            emit("GA_TARGETING_AD_SCHEDULE_ABSENT")
        if serving and GOOGLE_ADS_NETWORK_CONTENT_ENABLED in quality:
            emit("GA_TARGETING_CONTENT_NETWORK_ENABLED")
        if serving and GOOGLE_ADS_NETWORK_PARTNER_SEARCH_ENABLED in quality:
            emit("GA_TARGETING_PARTNER_SEARCH_ENABLED")
        if GOOGLE_ADS_NETWORK_SETTINGS_NOT_RETURNED in quality:
            emit("GA_TARGETING_NETWORK_SETTINGS_NOT_RETURNED")
        if GOOGLE_ADS_GEO_TARGET_TYPE_NOT_RETURNED in quality:
            emit("GA_TARGETING_GEO_TARGET_TYPE_NOT_RETURNED")
    return findings


def _ad_group_findings(
    grouped: dict[str, list[RetainedSnapshotRecord]],
    context: ProviderAuditContext,
    window: AuditDataWindow,
    coverage: dict[AuditDimension, ProviderCoverageStatus],
) -> list[ProviderAuditFinding]:
    findings: list[ProviderAuditFinding] = []
    maximum = context.policy.maximum_cpc
    for record in grouped["AD_GROUP"]:
        state = record.snapshot.normalized_state
        refs = (record.snapshot.resource_ref,)
        evidence = (record.record_digest,)
        bid = state.get("bid_amount_minor")
        if bid is None:
            findings.append(
                _emit(
                    RULES["GA_BIDS_AD_GROUP_WITHOUT_CPC_BID"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["BIDS"],
                )
            )
            continue
        # A customer-declared maximum only.  Nothing here supplies a default,
        # and a bid in another currency is not compared at all.
        if (
            maximum is not None
            and isinstance(bid, int)
            and not isinstance(bid, bool)
            and state.get("currency") == maximum.currency
            and bid > maximum.minor_units
        ):
            findings.append(
                _emit(
                    RULES["GA_BIDS_CPC_BID_ABOVE_DECLARED_MAXIMUM"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["BIDS"],
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

        def emit(
            rule_id: str, refs: tuple[str, ...] = refs, evidence: tuple[str, ...] = evidence
        ) -> None:
            _append(
                findings, rule_id, refs=refs, evidence=evidence, window=window, coverage=coverage
            )

        if GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED in quality:
            emit("GA_CREATIVE_RESPONSIVE_SEARCH_AD_NOT_RETURNED")
        if GOOGLE_ADS_RSA_HEADLINES_BELOW_DOCUMENTED_MINIMUM in quality:
            emit("GA_CREATIVE_HEADLINES_BELOW_DOCUMENTED_MINIMUM")
        if GOOGLE_ADS_RSA_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM in quality:
            emit("GA_CREATIVE_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM")
        if GOOGLE_ADS_FINAL_URL_ABSENT in quality:
            emit("GA_DESTINATION_FINAL_URL_ABSENT")
        approval = _approval_status(quality)
        if GOOGLE_ADS_POLICY_APPROVAL_STATUS_NOT_RETURNED in quality or approval is None:
            emit("GA_POLICY_APPROVAL_STATUS_NOT_RETURNED")
        elif approval not in GOOGLE_ADS_REVIEWED_POLICY_APPROVAL_STATUSES:
            emit("GA_POLICY_APPROVAL_STATUS_NOT_REVIEWED")
        elif approval in GOOGLE_ADS_RESTRICTED_POLICY_APPROVAL_STATUSES:
            emit("GA_POLICY_APPROVAL_RESTRICTED")
        # The review status is read from the same policy summary, so it is
        # evaluated symmetrically: an absent value is not a completed review,
        # and a value outside the reviewed enumeration is surfaced rather than
        # silently accepted.
        review = _review_status(quality)
        if GOOGLE_ADS_POLICY_REVIEW_STATUS_NOT_RETURNED in quality or review is None:
            emit("GA_POLICY_REVIEW_STATUS_NOT_RETURNED")
        elif review not in GOOGLE_ADS_REVIEWED_POLICY_REVIEW_STATUSES:
            emit("GA_POLICY_REVIEW_STATUS_NOT_REVIEWED")
    findings.extend(_creative_binding_findings(context, grouped, window, coverage))
    return findings


def _prefixed_status(quality: frozenset[str], prefix: str, sentinels: frozenset[str]) -> str | None:
    """Read the one prefixed enum value a retained snapshot carries, if exactly one.

    The `RETURNED` / `NOT_RETURNED` markers share the prefix, so they are named
    here and excluded rather than parsed as values.
    """
    values = sorted(
        status.removeprefix(prefix)
        for status in quality
        if status.startswith(prefix) and status not in sentinels
    )
    return values[0] if len(values) == 1 else None


_APPROVAL_SENTINELS: Final[frozenset[str]] = frozenset(
    {"GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED", GOOGLE_ADS_POLICY_APPROVAL_STATUS_NOT_RETURNED}
)
_REVIEW_SENTINELS: Final[frozenset[str]] = frozenset(
    {"GOOGLE_ADS_POLICY_REVIEW_STATUS_RETURNED", GOOGLE_ADS_POLICY_REVIEW_STATUS_NOT_RETURNED}
)


def _approval_status(quality: frozenset[str]) -> str | None:
    """Read the one prefixed approval status a retained ad snapshot carries."""
    return _prefixed_status(quality, GOOGLE_ADS_POLICY_APPROVAL_PREFIX, _APPROVAL_SENTINELS)


def _review_status(quality: frozenset[str]) -> str | None:
    """Read the one prefixed review status a retained ad snapshot carries."""
    return _prefixed_status(quality, GOOGLE_ADS_POLICY_REVIEW_PREFIX, _REVIEW_SENTINELS)


def _creative_binding_findings(
    context: ProviderAuditContext,
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
    coverage: dict[AuditDimension, ProviderCoverageStatus],
) -> list[ProviderAuditFinding]:
    catalog = context.evidence.creative_catalog
    if catalog is None:
        return []
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
    findings: list[ProviderAuditFinding] = []
    for entry in sorted(catalog.entries, key=lambda item: item.creative_version_id):
        if entry.provider != PROVIDER or entry.provider_resource_ref is None:
            continue
        bound = retained.get(entry.provider_resource_ref)
        if bound is None:
            if not absence_is_evidence:
                continue
            findings.append(
                _emit(
                    RULES["GA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE"],
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
                RULES["GA_CREATIVE_LOCAL_BINDING_UNVERIFIED"],
                resource_refs=(entry.creative_version_id, entry.provider_resource_ref),
                evidence_refs=(entry.record_digest, bound.record_digest),
                window=window,
                coverage_status=coverage["CREATIVE"],
            )
        )
    return findings


def _delivery_and_spend_findings(
    context: ProviderAuditContext,
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
) -> list[ProviderAuditFinding]:
    report = context.report
    report_digest = context.source_record_digest
    findings: list[ProviderAuditFinding] = []
    if _REPORT_COMPLETE not in report.envelope.data_quality_statuses:
        findings.append(
            _emit(
                RULES["GA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED"],
                resource_refs=(report.envelope.source_account_ref,),
                evidence_refs=(report_digest,),
                window=window,
                coverage_status="PARTIAL",
            )
        )
    rounded_reported = False
    for row in report.performance_rows:
        if row.provider_extensions.get("google_ads_rounding") == "HALF_EVEN_TO_MINOR" and (
            not rounded_reported
        ):
            # One row-level rounding disclosure per retained report: the marker
            # is a property of the read, not of an individual campaign, and one
            # finding per row would bury every other result.
            rounded_reported = True
            findings.append(
                _emit(
                    RULES["GA_SPEND_ROW_COST_ROUNDED_TO_MINOR_UNITS"],
                    resource_refs=(report.envelope.source_account_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
        if row.impressions is None or row.clicks is None:
            findings.append(
                _emit(
                    RULES["GA_DELIVERY_METRICS_MISSING"],
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
                    RULES["GA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
        if row.impressions == 0 and _spend_is_positive(row):
            findings.append(
                _emit(
                    RULES["GA_SPEND_WITHOUT_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
    campaign_impressions: dict[str, int] = {}
    for row in report.performance_rows:
        if row.metric_grain != "CAMPAIGN":
            continue
        campaign_impressions[row.resource_ref] = campaign_impressions.get(row.resource_ref, 0) + (
            row.impressions or 0
        )
    if not campaign_impressions:
        return findings
    minimum = context.policy.minimum_impressions
    for record in grouped["CAMPAIGN"]:
        if not google_ads_declares_serving_intent(record.snapshot.effective_status):
            continue
        observed = campaign_impressions.get(record.snapshot.resource_ref, 0)
        if observed == 0:
            findings.append(
                _emit(
                    RULES["GA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"],
                    resource_refs=(record.snapshot.resource_ref,),
                    evidence_refs=(record.record_digest, report_digest),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
            continue
        # Only a customer-declared minimum is applied, and only to a campaign
        # the retained report actually returned rows for.
        if minimum is not None and observed < minimum:
            findings.append(
                _emit(
                    RULES["GA_DELIVERY_IMPRESSIONS_BELOW_DECLARED_MINIMUM"],
                    resource_refs=(record.snapshot.resource_ref,),
                    evidence_refs=(record.record_digest, report_digest),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
    return findings


def _pagination_is_unproven(grouped: dict[str, list[RetainedSnapshotRecord]]) -> bool:
    """Whether the retained read failed to record a declared total for every search.

    The pagination fact is recorded on the account snapshot, so a read that
    retained no account snapshot has not proven its pagination either.  Both
    cases are unproven, and both are reported rather than left to look like a
    proven read.
    """
    accounts = grouped["ACCOUNT"]
    if not accounts:
        return True
    return GOOGLE_ADS_PAGINATION_TOTAL_NOT_DECLARED in _quality(accounts[0])


def _account_scoped_findings(
    context: ProviderAuditContext,
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
    coverage: dict[AuditDimension, ProviderCoverageStatus],
) -> list[ProviderAuditFinding]:
    """One finding per unread, presence-only, or read-boundary fact.

    Most of these describe what this build's fixed reads never request. That is
    a property of the read contract, not of any one retained record, so they do
    not depend on an account snapshot being present: a retained read that omits
    it still reports every one of them, anchored instead to the account the
    audited report names and cited to that report's own record digest.  Only
    the two rules that read an account-level *value* -- the pagination total and
    the test-account flag -- depend on the snapshot that carries it, and the
    pagination rule fires when that snapshot is missing as well, because an
    unrecorded total is not a proven one.
    """
    accounts = grouped["ACCOUNT"]
    quality = _quality(accounts[0]) if accounts else frozenset()
    resource_refs = (
        (accounts[0].snapshot.resource_ref,)
        if accounts
        else (context.report.envelope.source_account_ref,)
    )
    evidence_refs = (accounts[0].record_digest,) if accounts else (context.source_record_digest,)
    anchored: list[tuple[str, bool]] = [
        ("GA_STRUCTURE_INVENTORY_PAGINATION_NOT_PROVEN", _pagination_is_unproven(grouped)),
        ("GA_BIDS_STRATEGY_DETAIL_NOT_READ", True),
        ("GA_OBJECTIVE_CONVERSION_GOAL_NOT_READ", True),
        ("GA_TARGETING_CRITERION_VALUES_NOT_READ", True),
        ("GA_PLACEMENT_NOT_READ", True),
        ("GA_CREATIVE_CONTENT_TEXT_NOT_READ", True),
        ("GA_DESTINATION_VALUES_NOT_READ", True),
        ("GA_TRACKING_CONVERSION_ACTION_NOT_READ", True),
        ("GA_TRACKING_TEMPLATE_AND_PARAMETERS_NOT_READ", True),
        ("GA_NATIVE_CONVERSION_NOT_READ", True),
        ("GA_BROWSER_NOT_APPLICABLE", True),
        ("GA_POLICY_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED", True),
        ("GA_POLICY_TEST_ACCOUNT_DECLARED", GOOGLE_ADS_TEST_ACCOUNT_DECLARED in quality),
        ("GA_POLICY_ACCOUNT_STATUS_NOT_READ", True),
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
        "Google Ads."
    )


def _state_shape_version(record: RetainedSnapshotRecord) -> str | None:
    """The connector state-shape version a retained snapshot declares, if any.

    Snapshots written before this product recorded one carry none.  The value
    lives in `data_quality_statuses`, never in `normalized_state`, so recording
    it creates no drift of its own.
    """
    versions = sorted(
        status.removeprefix(GOOGLE_ADS_STATE_SHAPE_PREFIX)
        for status in record.snapshot.data_quality_statuses
        if status.startswith(GOOGLE_ADS_STATE_SHAPE_PREFIX)
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
    field, and a value that became absent — `_snapshot` drops a `None`, so a
    campaign losing its end date removes the key entirely.

    Publishing either as `GOOGLE_ADS_OUT_OF_BAND_STATE_CHANGE_OBSERVED` would
    assert a change at Google inside a recorded interval that this product
    never observed.  The first returned tuple is therefore only the fields
    **both** reads returned whose values differ, plus `parent_resource_refs`
    when the parent chain differs; the second is the key-set difference, which
    is reported as a local state-shape difference instead.
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
    # both sets have a proven read boundary.  Without one -- history retained
    # before this product recorded `INVENTORY_READ_COMPLETED` markers, for
    # example -- the same comparison fabricates removals for resources an
    # interrupted run never reached and creations for resources an interrupted
    # baseline never held.  A per-resource state comparison stays valid either
    # way, so only the absence rules stop.
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
                # change at Google would be a fabricated remote observation.
                findings.append(
                    _emit(
                        RULES["GA_DRIFT_LOCAL_STATE_SHAPE_CHANGED"],
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
                RULES["GA_DRIFT_STATE_CHANGED"],
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
                RULES["GA_DRIFT_ABSENT_FROM_LATER_INVENTORY"],
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
            RULES["GA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY"],
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
    """How many local creative bindings declare a Google Ads resource."""
    catalog = context.evidence.creative_catalog
    if catalog is None:
        return 0
    return sum(
        1
        for entry in catalog.entries
        if entry.provider == PROVIDER and entry.provider_resource_ref is not None
    )


# The read surfaces that back each audit dimension.  `GOOGLE_ADS_READ_SURFACES`
# is the one declaration of what this build's fixed Google Ads reads return, so
# the ceiling for a dimension is derived from it here rather than restated as a
# literal that could drift away from the adapter.  `BROWSER_MEASUREMENT` has no
# entry: no provider read could back it, and it is always `NOT_APPLICABLE`.
_DIMENSION_SURFACES: Final[dict[AuditDimension, tuple[str, ...]]] = {
    "STRUCTURE": ("CAMPAIGN_INVENTORY", "AD_GROUP_INVENTORY", "AD_INVENTORY"),
    "DELIVERY": ("CAMPAIGN_DELIVERY_REPORT", "AD_DELIVERY_REPORT"),
    "SPEND": ("CAMPAIGN_BUDGET_AMOUNT", "CAMPAIGN_BUDGET_TYPE", "CAMPAIGN_SCHEDULE_DATES"),
    "BIDS": ("CAMPAIGN_BIDDING_STRATEGY_TYPE", "AD_GROUP_CPC_BID"),
    "OBJECTIVE": ("CAMPAIGN_ADVERTISING_CHANNEL_TYPE",),
    "TARGETING": (
        "CAMPAIGN_NETWORK_SETTINGS",
        "CAMPAIGN_GEO_TARGET_TYPE_SETTING",
        "CAMPAIGN_AD_SCHEDULE_PRESENCE",
    ),
    "PLACEMENT": ("PLACEMENT",),
    "CREATIVE": ("AD_RSA_HEADLINE_PRESENCE", "AD_RSA_DESCRIPTION_PRESENCE", "AD_TYPE"),
    "DESTINATION_CONFIGURATION": ("AD_FINAL_URL_PRESENCE",),
    "TRACKING": ("CONVERSION_ACTION_INVENTORY", "AD_TRACKING_TEMPLATE_AND_URL_PARAMETERS"),
    "NATIVE_CONVERSION": ("NATIVE_CONVERSION_METRICS",),
    "POLICY": (
        "AD_POLICY_APPROVAL_STATUS",
        "AD_POLICY_REVIEW_STATUS",
        "CUSTOMER_TEST_ACCOUNT_FLAG",
    ),
    "DRIFT": ("CAMPAIGN_INVENTORY", "AD_GROUP_INVENTORY", "AD_INVENTORY"),
}
_IMPLEMENTED_SURFACES: Final[frozenset[str]] = frozenset(
    {"IMPLEMENTED_FIXED_READ", "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY"}
)


def _surface_backed(dimension: AuditDimension) -> bool:
    """Whether any fixed read this build implements backs one dimension."""
    return any(
        GOOGLE_ADS_READ_SURFACES[surface] in _IMPLEMENTED_SURFACES
        for surface in _DIMENSION_SURFACES.get(dimension, ())
    )


def _coverage_map(
    context: ProviderAuditContext, grouped: dict[str, list[RetainedSnapshotRecord]]
) -> dict[AuditDimension, ProviderCoverageStatus]:
    """Give every dimension one status: the evidence supplied, capped by the read.

    The evidence expressions below say what the retained records could support.
    `GOOGLE_ADS_READ_SURFACES` then caps each one: a dimension no implemented
    fixed read backs is `NOT_READ` whatever a caller supplies, so a dimension
    can never be reported as read because a record set happened to be present.
    """
    inventory = context.evidence.inventory
    earlier = context.evidence.earlier_inventory
    has_inventory = inventory is not None and bool(inventory.records)
    # `COVERED` needs both a proven local read boundary and the provider's own
    # declared total for that read.  Page-token exhaustion alone is `PARTIAL`.
    pagination_proven = not _pagination_is_unproven(grouped)
    complete = (
        has_inventory
        and inventory is not None
        and inventory.completeness == "COMPLETE_BOUNDED_READ"
        and pagination_proven
    )
    has_report_rows = bool(context.report.performance_rows)
    has_campaigns = bool(grouped["CAMPAIGN"])
    has_groups = bool(grouped["AD_GROUP"])
    has_ads = bool(grouped["AD"])
    drift_is_covered = (
        earlier is not None
        and inventory is not None
        and earlier.completeness == "COMPLETE_BOUNDED_READ"
        and inventory.completeness == "COMPLETE_BOUNDED_READ"
        and pagination_proven
    )
    evaluated: dict[AuditDimension, ProviderCoverageStatus] = {
        "STRUCTURE": "COVERED" if complete else "PARTIAL" if has_inventory else "NOT_READ",
        # Never COVERED: the shared audit vocabulary has no exhaustive
        # `AnalyticsCompleteness` member, so no retained report may be reported
        # as the whole window even when the provider declared its total.
        "DELIVERY": "PARTIAL" if has_report_rows else "NOT_READ",
        "SPEND": "PARTIAL" if has_report_rows or has_campaigns else "NOT_READ",
        "BIDS": "PARTIAL" if has_campaigns or has_groups else "NOT_READ",
        "OBJECTIVE": "PARTIAL" if has_campaigns else "NOT_READ",
        "TARGETING": "PARTIAL" if has_campaigns else "NOT_READ",
        "PLACEMENT": "PARTIAL" if has_campaigns or has_groups else "NOT_READ",
        # Creative evidence is a retained ad snapshot or a local binding that
        # names one; both produce findings, so both are coverage.
        "CREATIVE": "PARTIAL" if has_ads or _local_bindings(context) else "NOT_READ",
        "DESTINATION_CONFIGURATION": "PARTIAL" if has_ads else "NOT_READ",
        "TRACKING": "PARTIAL" if has_campaigns else "NOT_READ",
        "NATIVE_CONVERSION": "PARTIAL" if has_report_rows else "NOT_READ",
        "BROWSER_MEASUREMENT": "NOT_APPLICABLE",
        "POLICY": "PARTIAL" if has_ads or bool(grouped["ACCOUNT"]) else "NOT_READ",
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
    "STRUCTURE": "Customer, campaign, ad-group, and ad identity, parents, and declared status are read by the fixed v25 inventory searches.",
    "DELIVERY": "Impressions and clicks come from the fixed daily search report, whose retained result is never labelled exhaustive by this audit vocabulary.",
    "SPEND": "Retained cost rows, the exact-cent campaign budget, its period and sharing, and the campaign start and end dates are evaluated as retained; native cost micros are never re-rounded.",
    "BIDS": "The campaign bidding strategy type and the exact-cent ad-group CPC bid are read; portfolio strategies, targets, ceilings, pacing, and bid modifiers are not.",
    "OBJECTIVE": "The campaign advertising channel type is the documented intent this read returns; conversion goals are not read.",
    "TARGETING": "Only network settings, the geo target type setting, and whether an AD_SCHEDULE criterion exists are read; no keyword, location, audience, or schedule value is retained.",
    "PLACEMENT": "No placement view, placement criterion, or placement report facet is requested by this build's fixed Google Ads reads.",
    "CREATIVE": "Ad identity, type, declared status, and responsive-search component counts are read; headline and description text, pinning, and assets are not.",
    "DESTINATION_CONFIGURATION": "Only whether an ad declared a final URL is read; no URL, display path, suffix, or parameter value is retained.",
    "TRACKING": "Conversion actions, Google tag status, tracking templates, and URL parameters are not requested by this build's fixed read.",
    "NATIVE_CONVERSION": "The fixed report request projects impressions, clicks, and cost micros only; no conversion field is requested.",
    "BROWSER_MEASUREMENT": "A local audit executes no browser and loads no page.",
    "POLICY": "The ad policy summary approval and review statuses and the customer test-account flag are read; policy topics, customer status, and the developer token's access level are not.",
    "DRIFT": "Drift compares two retained snapshot sets for one account; it needs both to be supplied.",
}
_UNPROVEN_BOUNDARY_RATIONALE: Final = (
    " The retained record set has no proven read boundary, so what it omits proves nothing."
)
_SUBSET_RATIONALE: Final = (
    " The caller declared this record set a subset, so an absence is weak evidence."
)
_UNPROVEN_PAGINATION_RATIONALE: Final = (
    " The retained read does not record a declared total result count for every search,"
    " so the hierarchy is not proven whole."
)
_NO_ACCOUNT_SNAPSHOT_RATIONALE: Final = (
    " It retained no account snapshot, which is where that total is recorded."
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
    pagination_unproven = inventory is not None and _pagination_is_unproven(grouped)
    pagination_unrecorded = inventory is not None and not grouped["ACCOUNT"]
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
            if dimension in {"STRUCTURE", "DRIFT"} and pagination_unproven:
                rationale += _UNPROVEN_PAGINATION_RATIONALE
                if pagination_unrecorded:
                    rationale += _NO_ACCOUNT_SNAPSHOT_RATIONALE
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
    """Inline the first identifiers, and record it when there are more.

    The inline cap keeps one output row a constant size so the smallest page an
    agent can request always fits the output bound, whatever the size of the
    customer's account.  Reaching it is not a caller error and never fails the
    audit.  Nothing is dropped either: every finding remains its own row in
    `findings`, the row states its complete `bound_finding_count` and the
    `bound_rule_ids` that define its membership, and an explicit `BoundExceeded`
    entry records that the inline list was capped.
    """
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
            # membership.  Two rows can therefore never share a subject.
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
                    # unique on its own: two declined groups below share
                    # `OPERATION_NOT_COMPILABLE_FOR_PROVIDER` with `CREATE`.
                    # A declined group's rule identifiers are disjoint from
                    # every other group's, so the first one in content order
                    # identifies the exact declined row within the subject's
                    # fixed size bound.  The complete membership is the row's
                    # own `rule_ids`.
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
        {"GA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"},
        operation_kind="OBSERVE",
        hypothesis="A repeated bounded campaign read may show delivery this retained report did not return.",
        expected_impact="A second compatible read establishes whether the absence of rows reflects real non-delivery.",
        assumptions=(
            "The absence of a row in a retained report is not evidence of zero delivery.",
        ),
        confidence="LOW",
        risk="LOW",
        completeness=_BOUNDED,
    )
    _propose(
        {
            "GA_SPEND_WITHOUT_IMPRESSIONS",
            "GA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
            "GA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
            "GA_STRUCTURE_INVENTORY_PAGINATION_NOT_PROVEN",
        },
        operation_kind="OBSERVE",
        hypothesis="A repeated bounded read over the same declared window may resolve this inconsistent or unproven retained result.",
        expected_impact="A second compatible read establishes whether the inconsistency or the missing total result count persists before any decision uses the result.",
        assumptions=(
            "A single inconsistent aggregate row is not evidence of a delivery or billing defect.",
            "Page-token exhaustion without the provider's declared total is not proof that a read returned every row.",
        ),
        confidence="MEDIUM",
        risk="LOW",
        completeness=_BOUNDED,
    )
    _propose(
        {
            "GA_DRIFT_STATE_CHANGED",
            "GA_DRIFT_ABSENT_FROM_LATER_INVENTORY",
            "GA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
        },
        operation_kind="OBSERVE",
        hypothesis="A fresh bounded hierarchy read establishes the current state of the resources that changed out of band.",
        expected_impact="A current snapshot bounds the observation interval before any later work relies on the old state.",
        assumptions=(
            "The actor and the exact time of an out-of-band change are unknown and are not inferred.",
        ),
        confidence="HIGH",
        risk="LOW",
        completeness=_NOT_ANALYTICS,
    )
    # The one mutation this build can compile for an existing Google Ads
    # campaign.  It is a suggestion to compile a plan, never a plan: it carries
    # no digest, no authorization, and nothing here is executed.
    #
    # Membership is exactly the rules whose findings the named route can act
    # on, and about which this hypothesis is true.  The route pauses one named
    # campaign, and the hypothesis asserts that the campaign declares ENABLED,
    # so every bound rule has to be campaign-scoped *and* guarded by
    # `google_ads_declares_serving_intent`.  Both rules below are.
    # `GA_SPEND_WITHOUT_IMPRESSIONS` is deliberately not: it is a report-row
    # rule whose resource is whatever grain the retained row named -- a
    # `google_ads.ad.*` reference for the AD-grain report -- which no route in
    # this build can pause.  It keeps its `OBSERVE` row above.
    _propose(
        {"GA_SPEND_BUDGET_NOT_RETURNED", "GA_TARGETING_CONTENT_NETWORK_ENABLED"},
        operation_kind="PAUSE",
        hypothesis=(
            "A Google Ads campaign that declares ENABLED, and whose retained read either bounds "
            "no budget or opts it into the Display content network, may be worth pausing while "
            f"it is investigated; the `{PAUSE_PLAN_ROUTE}` route can describe that pause as an offline plan."
        ),
        expected_impact=(
            "A pause would stop further spend on the named campaigns. Execution is unreleased in "
            "0.1.0, so nothing here pauses anything and this is not a compiled plan. The pause is "
            "the marketer's manual step in the Google Ads interface; planning stays available offline."
        ),
        assumptions=(
            "A retained read that returned no budget is not proof that the campaign has none.",
            "A declared ENABLED status is an intent to serve, not an observation of delivery.",
            "Network flags say where a campaign is eligible to deliver, never where it delivered.",
            "Pausing is reversible in Google Ads, but the resulting delivery gap is not.",
            f"This names the `{PAUSE_PLAN_ROUTE}` route only; that route resolves its own campaign identifier and describes the pause as an offline plan. Nothing executes a plan in 0.1.0, so the marketer makes the pause by hand.",
        ),
        confidence="MEDIUM",
        risk="MEDIUM",
        completeness=_NOT_ANALYTICS,
    )
    _no_recommendation(
        {
            "GA_STRUCTURE_SERVING_INTENT_MISMATCH",
            "GA_BIDS_CPC_BID_ABOVE_DECLARED_MAXIMUM",
            "GA_SPEND_BUDGET_EXPLICITLY_SHARED",
            "GA_TARGETING_PARTNER_SEARCH_ENABLED",
            "GA_TARGETING_AD_SCHEDULE_ABSENT",
        },
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="Reconciling a status, lowering a bid, splitting a shared budget, changing a network flag, or adding an ad schedule would require a Google Ads update operation, which this build cannot compile.",
        considered="UPDATE",
    )
    _no_recommendation(
        {
            "GA_CREATIVE_HEADLINES_BELOW_DOCUMENTED_MINIMUM",
            "GA_CREATIVE_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM",
            "GA_DESTINATION_FINAL_URL_ABSENT",
            "GA_POLICY_APPROVAL_RESTRICTED",
        },
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="Adding a component, setting a destination, or appealing a policy decision on an existing Google Ads ad is not a compilable operation in this build; the offline compiler only produces a complete new paused Search candidate.",
        considered="REPLACE",
    )
    _no_recommendation(
        {"GA_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP", "GA_STRUCTURE_AD_GROUP_WITHOUT_AD"},
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="The offline Google Ads compiler produces only a complete new paused Search campaign, ad-group, keyword, and responsive-search-ad candidate; it cannot compile a child into an existing retained parent.",
        considered="CREATE",
    )
    _no_recommendation(
        {"GA_OBJECTIVE_CHANNEL_OUTSIDE_COMPILABLE_SUBSET"},
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="This build compiles a Search campaign only, so it proposes no change to a campaign on another advertising channel and would not recreate one.",
        considered="CREATE",
    )
    _no_recommendation(
        {
            "GA_STRUCTURE_STATUS_NOT_REVIEWED",
            "GA_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED",
            "GA_STRUCTURE_AD_PARENT_NOT_RETAINED",
            "GA_SPEND_BUDGET_PERIOD_NOT_RETURNED",
            "GA_SPEND_SCHEDULE_END_NOT_RETURNED",
            "GA_SPEND_ROW_COST_ROUNDED_TO_MINOR_UNITS",
            "GA_BIDS_AD_GROUP_WITHOUT_CPC_BID",
            "GA_BIDS_AUTOMATED_STRATEGY_NOT_LOCALLY_BOUNDED",
            "GA_TARGETING_NETWORK_SETTINGS_NOT_RETURNED",
            "GA_TARGETING_GEO_TARGET_TYPE_NOT_RETURNED",
            "GA_DELIVERY_METRICS_MISSING",
            "GA_DELIVERY_IMPRESSIONS_BELOW_DECLARED_MINIMUM",
            "GA_CREATIVE_RESPONSIVE_SEARCH_AD_NOT_RETURNED",
            "GA_CREATIVE_LOCAL_BINDING_UNVERIFIED",
            "GA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
            "GA_POLICY_APPROVAL_STATUS_NOT_RETURNED",
            "GA_POLICY_APPROVAL_STATUS_NOT_REVIEWED",
            "GA_POLICY_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED",
            "GA_POLICY_TEST_ACCOUNT_DECLARED",
        },
        reason="EVIDENCE_INSUFFICIENT",
        detail="These findings record missing, unreviewed, threshold-relative, or environment facts. Resolving them needs a documentation review, a policy change, or a further read, not an advertising operation.",
    )
    _no_recommendation(
        {
            "GA_BIDS_STRATEGY_DETAIL_NOT_READ",
            "GA_OBJECTIVE_CONVERSION_GOAL_NOT_READ",
            "GA_TARGETING_CRITERION_VALUES_NOT_READ",
            "GA_PLACEMENT_NOT_READ",
            "GA_CREATIVE_CONTENT_TEXT_NOT_READ",
            "GA_DESTINATION_VALUES_NOT_READ",
            "GA_TRACKING_CONVERSION_ACTION_NOT_READ",
            "GA_TRACKING_TEMPLATE_AND_PARAMETERS_NOT_READ",
            "GA_NATIVE_CONVERSION_NOT_READ",
            "GA_BROWSER_NOT_APPLICABLE",
            "GA_POLICY_ACCOUNT_STATUS_NOT_READ",
        },
        reason="COVERAGE_NOT_READ",
        detail="These dimensions are unread, presence-only, or not applicable in this build's fixed Google Ads read. An unread dimension yields no recommendation and is never reported as healthy.",
    )
    return recommendations, absences, exceeded


def evaluate(context: ProviderAuditContext) -> ProviderAuditSections:
    """Evaluate the Google Ads rule table over retained records only."""
    if context.provider != PROVIDER:
        raise ValueError("The Google Ads rule table evaluates Google Ads evidence only")
    inventory = context.evidence.inventory
    account_ref = context.report.envelope.source_account_ref
    # Account binding.  Retained snapshots and the audited report must describe
    # the same provider account, or a finding would cite one account's evidence
    # against another's resources.
    for candidate in (inventory, context.evidence.earlier_inventory):
        if candidate is None:
            continue
        if candidate.provider != PROVIDER or candidate.account_ref != account_ref:
            raise ValueError("Retained Google Ads evidence must belong to the audited account")
    window = _window(context.report)
    grouped = _by_type(inventory)
    statuses = _coverage_map(context, grouped)
    findings: list[ProviderAuditFinding] = []
    if inventory is not None and inventory.records:
        findings.extend(_structure_findings(grouped, inventory, window, statuses["STRUCTURE"]))
        findings.extend(_campaign_setting_findings(grouped, window, statuses))
        findings.extend(_ad_group_findings(grouped, context, window, statuses))
        findings.extend(_ad_findings(context, grouped, window, statuses))
        findings.extend(_account_scoped_findings(context, grouped, window, statuses))
    elif context.evidence.creative_catalog is not None:
        # A catalog with no retained ads produces no absence claim -- see
        # `_creative_binding_findings` -- but a whole bounded read that returned
        # no ad at all is still a read boundary, and the local bindings it
        # cannot match are still evaluated here rather than silently skipped.
        findings.extend(_creative_binding_findings(context, grouped, window, statuses))
    if context.report.performance_rows:
        findings.extend(_delivery_and_spend_findings(context, grouped, window))
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
        # No reviewed Google Ads analytics read can be published as exhaustive
        # by this vocabulary, so a retained report is always bounded here.
        analytics_completeness=_BOUNDED,
        coverage=_coverage(context, statuses, grouped),
        findings=tuple(findings),
        recommendations=tuple(recommendations),
        no_recommendations=tuple(absences),
        drift_observations=tuple(observations),
        bound_exceeded=tuple(exceeded),
    )


register_provider_rule(PROVIDER, evaluate)
