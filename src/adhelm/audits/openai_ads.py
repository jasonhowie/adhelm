# ruff: noqa: E501
"""Deterministic OpenAI Ads audit rules over retained local records.

This module makes **no provider request**.  It evaluates the retained
`PERFORMANCE_OBSERVATION` report the caller selected, the retained
`RESOURCE_SNAPSHOT` set a completed inventory read appended, an optional
earlier snapshot set, and an optional local creative catalog.  Authorization,
quota, 401/403/404, and 429 handling therefore stays entirely with the read
adapters that produced those records; nothing here can leak a response body
because no response is available here.

Three honesty bounds shape every rule:

* The reviewed OpenAI hierarchy and insights reads both use documented cursor
  pagination and fail closed with an explicit result-limit or pagination error
  rather than returning a partial collection.  That is a property of the
  adapter, not of a record set handed to this module, and the shared audit
  vocabulary in :mod:`adhelm.audits` has no exhaustive `AnalyticsCompleteness`
  member at all.  Every analytics-derived finding therefore stays
  `BOUNDED_NON_EXHAUSTIVE` and never claims a total.  A retained report that
  does not itself declare `COMPLETE` produces its own explicit finding.
* The fixed reads return identity, hierarchy, declared status, bidding type,
  native lifetime spend limit, run schedule, billing event, maximum bid, ad
  review status, and **presence facts** for targeting, conversion-setting
  association, context hints, the creative file association, creative copy, and
  the creative destination.  Presence is not content: no targeting criterion,
  ad copy, image URL, or landing page is read, so those dimensions are
  `PARTIAL` at best and are never reported as healthy.
* Placement, native conversion metrics, report freshness, and attribution
  window are not in the reviewed fixed read, so their dimensions are `NOT_READ`.
  That ceiling is read from `OPENAI_ADS_READ_SURFACES` rather than restated
  here, so a dimension can never be reported as read because a caller supplied
  records for it.

`docs/implementation/openai-audit-recommendations.md` carries the rule table,
coverage map, and evidence limits in prose.
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
    snapshot_state_deltas,
)
from adhelm.models import Provider
from adhelm.providers.openai_ads import (
    OPENAI_ADS_AD_REVIEW_STATUS_NOT_RETURNED,
    OPENAI_ADS_CONTEXT_HINTS_NOT_DECLARED,
    OPENAI_ADS_CONVERSION_SETTINGS_NOT_ASSOCIATED,
    OPENAI_ADS_CREATIVE_BODY_ABSENT,
    OPENAI_ADS_CREATIVE_FILE_NOT_ASSOCIATED,
    OPENAI_ADS_CREATIVE_TARGET_URL_ABSENT,
    OPENAI_ADS_CREATIVE_TITLE_ABSENT,
    OPENAI_ADS_READ_SURFACES,
    OPENAI_ADS_REVIEWED_AD_GROUP_STATUSES,
    OPENAI_ADS_REVIEWED_AD_STATUSES,
    OPENAI_ADS_REVIEWED_CAMPAIGN_STATUSES,
    OPENAI_ADS_TARGETING_NOT_DECLARED,
    openai_ads_declares_serving_intent,
)
from adhelm.reporting import NormalizedReport, PerformanceRow

PROVIDER: Final[Provider] = "openai_ads"
# Bump this when a rule's semantics change.  It is part of every finding's
# content identity, so an audit produced by a different rule table can never be
# mistaken for one produced by this table.
RULE_TABLE_VERSION: Final = "openai_ads.rules.2026-09-09.2"
_BOUNDED: Final[AnalyticsCompleteness] = "BOUNDED_NON_EXHAUSTIVE"
_NOT_ANALYTICS: Final[AnalyticsCompleteness] = "NOT_APPLICABLE"
# The one route this build can compile for an existing OpenAI campaign.  Naming
# it in a recommendation is a vocabulary statement about what the product could
# be asked to compile next; it is never a plan and never an authorization.
PAUSE_PLAN_ROUTE: Final = "plan openai-pause"
# The retained report envelope declares this when the adapter consumed every
# documented insights page for its bounded window.
_REPORT_COMPLETE: Final = "COMPLETE"
# The retained money statuses the OpenAI inventory branch appends when a native
# micros amount could not be projected exactly into USD minor units.
_UNPROJECTED_MONEY: Final[frozenset[str]] = frozenset(
    {
        "SUBMINOR_AMOUNT_RETAINED_AS_NATIVE_MICROS",
        "CURRENCY_EXPONENT_UNVERIFIED_NATIVE_MICROS_RETAINED",
    }
)
_REVIEWED_STATUSES: Final[dict[str, tuple[str, ...]]] = {
    "CAMPAIGN": OPENAI_ADS_REVIEWED_CAMPAIGN_STATUSES,
    "AD_GROUP": OPENAI_ADS_REVIEWED_AD_GROUP_STATUSES,
    "AD": OPENAI_ADS_REVIEWED_AD_STATUSES,
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
                rule_id="OA_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP",
                finding_type="OPENAI_CAMPAIGN_WITHOUT_AD_GROUP",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI campaign has no retained ad group in the same inventory read.",
                suggested_action="Confirm the campaign in OpenAI Ads Manager, or read the bounded hierarchy again; a campaign with no ad group cannot serve.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_STRUCTURE_AD_GROUP_WITHOUT_AD",
                finding_type="OPENAI_AD_GROUP_WITHOUT_AD",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI ad group has no retained ad in the same inventory read.",
                suggested_action="Confirm the ad group in OpenAI Ads Manager; an ad group with no ad cannot serve a chat card.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED",
                finding_type="OPENAI_AD_GROUP_PARENT_CAMPAIGN_NOT_RETAINED",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI ad group names a parent campaign that is absent from the same retained inventory.",
                suggested_action="Read the bounded hierarchy again; an ad group whose parent campaign is missing cannot be evaluated in context.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_STRUCTURE_AD_PARENT_NOT_RETAINED",
                finding_type="OPENAI_AD_PARENT_AD_GROUP_NOT_RETAINED",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI ad names a parent that is absent from the same retained inventory.",
                suggested_action="Read the bounded hierarchy again; an ad whose parent is missing cannot be evaluated in context.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_STRUCTURE_SERVING_INTENT_MISMATCH",
                finding_type="OPENAI_SERVING_INTENT_MISMATCH_WITH_PARENT",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI resource declares the documented active status while its retained parent does not, so its declared intent cannot be met.",
                suggested_action="Reconcile the parent and child statuses in OpenAI Ads Manager; this audit reads declared status only, not serving state.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_STRUCTURE_STATUS_NOT_REVIEWED",
                finding_type="OPENAI_STATUS_VALUE_NOT_IN_REVIEWED_ENUM",
                dimension="STRUCTURE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI resource carries a status outside the reviewed campaign-state enumeration this connector pins.",
                suggested_action="Recheck the OpenAI campaign status documentation before interpreting the value; the audit does not guess its meaning.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS",
                finding_type="OPENAI_ACTIVE_CAMPAIGN_WITHOUT_RETAINED_IMPRESSIONS",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="LOW",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained OpenAI campaign declares the documented active status and the retained bounded report contains no impressions for it. The retained report is not an exhaustive provider claim, so this is not proof of zero delivery.",
                suggested_action="Read the same bounded campaign report again over a compatible window before treating the absence of rows as zero delivery.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
                finding_type="OPENAI_CLICKS_EXCEED_IMPRESSIONS",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained OpenAI report row declares more clicks than impressions at its declared grain, which no consistent delivery aggregate produces.",
                suggested_action="Read the same bounded report again and compare the declared grain and window before using this row for any decision.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_DELIVERY_METRICS_MISSING",
                finding_type="OPENAI_DELIVERY_METRICS_MISSING_ON_ROW",
                dimension="DELIVERY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="This retained OpenAI report row declares no impression or click count, so no delivery rate can be derived from it.",
                suggested_action="Read the same declared grain again with the fixed impression and click fields before interpreting delivery.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
                finding_type="OPENAI_RETAINED_REPORT_DOES_NOT_DECLARE_COMPLETE_PAGINATION",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained OpenAI report does not declare the COMPLETE quality status that the fixed insights read records when it consumed every documented page, so its row set has no proven read boundary.",
                suggested_action="Run the fixed OpenAI report again and audit the result it retains; do not treat this row set as the whole window.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_SPEND_WITHOUT_IMPRESSIONS",
                finding_type="OPENAI_SPEND_WITHOUT_IMPRESSIONS",
                dimension="SPEND",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_BOUNDED,
                observation="This retained OpenAI report row declares spend with zero impressions at its declared grain.",
                suggested_action="Read the same bounded report again and reconcile the row in OpenAI Ads Manager before drawing a spend conclusion.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_SPEND_LIMIT_NOT_RETURNED",
                finding_type="OPENAI_ACTIVE_CAMPAIGN_WITHOUT_LIFETIME_SPEND_LIMIT",
                dimension="SPEND",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI campaign declares the documented active status and carries no native lifetime spend limit in the same read, so this audit can bound no maximum spend for it.",
                suggested_action="Confirm the campaign's lifetime spend limit in OpenAI Ads Manager; an absent limit in a retained read is not a proven unlimited budget, and it is not a proven limit either.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_SPEND_NATIVE_AMOUNT_NOT_PROJECTED",
                finding_type="OPENAI_NATIVE_AMOUNT_RETAINED_WITHOUT_EXACT_MINOR_PROJECTION",
                dimension="SPEND",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI resource holds a native micros amount that could not be projected exactly into USD minor units, so only the native amount is comparable.",
                suggested_action="Compare the retained native micros directly; the audit never rounds a subminor or unverified-currency amount into a minor-unit total.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_SPEND_SCHEDULE_NOT_RETURNED",
                finding_type="OPENAI_ACTIVE_CAMPAIGN_WITHOUT_RETAINED_END_SCHEDULE",
                dimension="SPEND",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI campaign declares the documented active status and carries no retained end time, so no scheduled stop bounds its exposure in this read.",
                suggested_action="Confirm the campaign schedule in OpenAI Ads Manager; an absent end time in a retained read is not proof that the campaign runs forever.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_BIDS_MAX_BID_NOT_RETURNED",
                finding_type="OPENAI_AD_GROUP_WITHOUT_RETAINED_MAXIMUM_BID",
                dimension="BIDS",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI ad group carries no native maximum bid micros, so this audit can bound no per-event cost for it.",
                suggested_action="Confirm the ad group's maximum bid in OpenAI Ads Manager before relying on any cost expectation.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_BIDS_STRATEGY_NOT_READ",
                finding_type="OPENAI_BID_STRATEGY_AND_PACING_NOT_READ",
                dimension="BIDS",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Beyond the campaign bidding type, the billing event, and the ad-group maximum bid, no bid strategy, pacing, or delivery-optimization setting is requested by this build's fixed OpenAI read.",
                suggested_action="Review bid strategy and pacing in OpenAI Ads Manager; this audit makes no pacing claim.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_OBJECTIVE_BIDDING_TYPE_NOT_RETURNED",
                finding_type="OPENAI_CAMPAIGN_BIDDING_TYPE_NOT_RETURNED",
                dimension="OBJECTIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI campaign snapshot carries no bidding type, so its optimization intent is unknown to this audit.",
                suggested_action="Read the bounded campaign inventory again; an absent bidding type is not a default optimization.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_OBJECTIVE_CONVERSION_BIDDING_UNSUPPORTED",
                finding_type="OPENAI_CAMPAIGN_USES_CONVERSION_BIDDING_THIS_BUILD_CANNOT_COMPILE",
                dimension="OBJECTIVE",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI campaign optimizes for conversions. OpenAI documents conversion bidding as requiring an enabled account and one active standard conversion event, and this build's offline compiler refuses that mode.",
                suggested_action="Verify the account's conversion enablement and its active standard event through OpenAI Ads Manager; this product compiles no conversion-optimized campaign.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_TARGETING_NOT_DECLARED",
                finding_type="OPENAI_CAMPAIGN_DECLARES_NO_TARGETING_OBJECT",
                dimension="TARGETING",
                severity="MEDIUM",
                confidence="MEDIUM",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI campaign declares the documented `active` serving intent and returned no non-empty targeting object. OpenAI documents that omitting a location selection can make a campaign eligible for every available location.",
                suggested_action="Confirm the campaign's location targeting in OpenAI Ads Manager; this audit reads only whether a targeting object was declared, never its criteria.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_TARGETING_AD_GROUP_WITHOUT_CONTEXT_HINTS",
                finding_type="OPENAI_AD_GROUP_DECLARES_NO_CONTEXT_HINTS",
                dimension="TARGETING",
                severity="LOW",
                confidence="MEDIUM",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI ad group returned no context hints, so it declares no contextual input this read can see.",
                suggested_action="Confirm the ad group's context hints in OpenAI Ads Manager; this audit reads only how many hints were returned, never their text.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_TARGETING_CRITERIA_VALUES_NOT_READ",
                finding_type="OPENAI_TARGETING_CRITERIA_VALUES_NOT_READ",
                dimension="TARGETING",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Targeting criteria values are deliberately not retained by this build's fixed OpenAI read; only whether a campaign declared a targeting object is known.",
                suggested_action="Review the exact targeting criteria in OpenAI Ads Manager; this audit neither reads nor infers audience or location values.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_PLACEMENT_NOT_READ",
                finding_type="OPENAI_PLACEMENT_NOT_READ",
                dimension="PLACEMENT",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="No placement configuration field or placement report facet is documented for the reviewed OpenAI Ads surfaces, so no placement claim is available for this account.",
                suggested_action="Review placement behaviour in OpenAI Ads Manager; this audit makes no placement claim and does not treat the absence of a documented field as an absence of placements.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_CREATIVE_FILE_NOT_ASSOCIATED",
                finding_type="OPENAI_AD_CREATIVE_WITHOUT_FILE_ASSOCIATION",
                dimension="CREATIVE",
                severity="HIGH",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI ad's chat-card creative names no provider file. The documented chat-card contract requires a file identifier returned by upload.",
                suggested_action="Confirm the ad's creative image in OpenAI Ads Manager; a chat card without its file cannot render as documented.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_CREATIVE_COPY_INCOMPLETE",
                finding_type="OPENAI_AD_CREATIVE_COPY_FIELD_ABSENT",
                dimension="CREATIVE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI ad's chat-card creative did not return a title or a body. Only the presence of each field was read; no copy text was retained.",
                suggested_action="Review the ad copy in OpenAI Ads Manager; this audit reports only that a documented required field was absent from the response.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_CREATIVE_CONTENT_TEXT_NOT_READ",
                finding_type="OPENAI_CREATIVE_CONTENT_TEXT_NOT_READ",
                dimension="CREATIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Creative copy text, image URLs, and rendered content are deliberately not retained by this build's fixed OpenAI read; only identity, file association, and field presence are.",
                suggested_action="Review creative copy, assets, and calls to action in OpenAI Ads Manager; this audit makes no creative-content claim.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_CREATIVE_FILE_BYTES_NOT_PROVABLE",
                finding_type="OPENAI_CREATIVE_FILE_BYTES_NOT_PROVABLE_FROM_DOCUMENTED_READBACK",
                dimension="CREATIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="OpenAI Ads documents no file metadata, listing, content, or hash read, so a retained file association proves that an ad names a file, never that the provider holds a particular local asset's bytes.",
                suggested_action="Treat the file association as an association only; a provider content digest or bounded content read would be required to compare bytes.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_CREATIVE_LOCAL_BINDING_UNVERIFIED",
                finding_type="OPENAI_LOCAL_CREATIVE_BINDING_UNVERIFIED",
                dimension="CREATIVE",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="A local creative version declares an OpenAI ad reference, but the retained inventory carries no provider-verified creative association for it.",
                suggested_action="Treat the local binding as a declaration only; an independently reviewed provider association is still required.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
                finding_type="OPENAI_LOCAL_CREATIVE_BINDING_WITHOUT_RETAINED_RESOURCE",
                dimension="CREATIVE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="A local creative version declares an OpenAI ad reference that is absent from the retained inventory for this account.",
                suggested_action="Read the bounded hierarchy again and reconcile the local declaration; do not assume the provider resource exists.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_DESTINATION_TARGET_URL_ABSENT",
                finding_type="OPENAI_AD_CREATIVE_WITHOUT_TARGET_URL",
                dimension="DESTINATION_CONFIGURATION",
                severity="HIGH",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI ad's chat-card creative returned no target URL, which the documented chat-card contract requires.",
                suggested_action="Confirm the ad's destination in OpenAI Ads Manager; this audit reads only whether a destination field was present, never its value.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_DESTINATION_VALUES_NOT_READ",
                finding_type="OPENAI_DESTINATION_AND_UTM_VALUES_NOT_READ",
                dimension="DESTINATION_CONFIGURATION",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Landing page values and campaign tagging are deliberately not retained by this build's fixed OpenAI read; only whether an ad declared a target URL is known.",
                suggested_action="Review destination URLs and tagging in OpenAI Ads Manager; this audit reads no URL or query value.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_TRACKING_NO_CONVERSION_SETTING_ASSOCIATED",
                finding_type="OPENAI_CAMPAIGN_WITHOUT_ASSOCIATED_CONVERSION_EVENT_SETTING",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="MEDIUM",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI campaign returned no associated conversion event setting identifier, so no native conversion definition is bound to it in this read.",
                suggested_action="Confirm the campaign's conversion setup in OpenAI Ads Manager; an absent association here is not proof that no conversion setting exists on the account.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ",
                finding_type="OPENAI_PIXEL_AND_EVENT_HEALTH_NOT_READ",
                dimension="TRACKING",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Pixel installation, event delivery, and conversion-setting health are not requested by this build's fixed OpenAI inventory read; the separate conversion-setting reader retrieves definitions only and can report the whole surface as not enabled for the account.",
                suggested_action="Verify pixel and event health through its own reviewed surface; retained delivery aggregates and a setting definition cannot establish it.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_NATIVE_CONVERSION_NOT_READ",
                finding_type="OPENAI_NATIVE_CONVERSION_METRICS_NOT_READ",
                dimension="NATIVE_CONVERSION",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The fixed OpenAI insights request projects readable date, identifiers, impressions, clicks, and spend only, so no native conversion count, action, or attribution context is available for this account.",
                suggested_action="Do not infer a conversion count from clicks or sessions; a native conversion report read is required and is not implemented here.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_BROWSER_NOT_APPLICABLE",
                finding_type="OPENAI_BROWSER_MEASUREMENT_NOT_APPLICABLE_LOCAL_AUDIT",
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
                rule_id="OA_POLICY_AD_REVIEW_STATUS_NOT_RETURNED",
                finding_type="OPENAI_AD_REVIEW_STATUS_NOT_RETURNED",
                dimension="POLICY",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained OpenAI ad returned no review status, so neither an approved nor a rejected policy state can be attributed to it.",
                suggested_action="Read the bounded ad inventory again; an absent review status is not an approval.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_POLICY_AD_REVIEW_ENUM_NOT_REVIEWED",
                finding_type="OPENAI_AD_REVIEW_STATUS_ENUM_NOT_REVIEWED",
                dimension="POLICY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This build pins no reviewed OpenAI ad review-status enumeration, so a returned review status is surfaced without being classified as approved, pending, or rejected.",
                suggested_action="Review the retained review status against current OpenAI documentation before relying on it; the audit does not decide what the value means.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_POLICY_ACCOUNT_SERVING_STATE_NOT_READ",
                finding_type="OPENAI_ACCOUNT_REVIEW_AND_SERVING_STATE_NOT_RETAINED",
                dimension="POLICY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The account review status the identity read returns is not retained in the account snapshot this audit reads, so no account-level policy or serving claim is available here.",
                suggested_action="Review account standing in OpenAI Ads Manager; an absent account review state is not an approved account.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_DRIFT_STATE_CHANGED",
                finding_type="OPENAI_OUT_OF_BAND_STATE_CHANGE_OBSERVED",
                dimension="DRIFT",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Two retained snapshots of this OpenAI resource differ, and this product applied no change between them. The actor is UNKNOWN and the change occurred somewhere inside the recorded observation interval.",
                suggested_action="Reconcile the change against OpenAI Ads Manager before planning anything; no exact external change time or actor is available.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_DRIFT_ABSENT_FROM_LATER_INVENTORY",
                finding_type="OPENAI_RESOURCE_ABSENT_FROM_LATER_INVENTORY",
                dimension="DRIFT",
                severity="MEDIUM",
                confidence="MEDIUM",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This OpenAI resource is present in the earlier retained inventory and absent from the later one. Absence is not proof of deletion or archival; the actor is UNKNOWN.",
                suggested_action="Read the bounded hierarchy again and confirm the resource in OpenAI Ads Manager before treating it as removed.",
            )
        ),
        _rule(
            _Rule(
                rule_id="OA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
                finding_type="OPENAI_RESOURCE_ABSENT_FROM_EARLIER_INVENTORY",
                dimension="DRIFT",
                severity="LOW",
                confidence="MEDIUM",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This OpenAI resource is present in the later retained inventory and absent from the earlier one. This product created nothing; the actor is UNKNOWN.",
                suggested_action="Confirm who created the resource in OpenAI Ads Manager; no exact external creation time or actor is available.",
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
) -> ProviderAuditFinding:
    selected: Confidence = rule.confidence if confidence is None else confidence
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
        suggested_action=rule.suggested_action,
        identity={
            "rule_table_version": RULE_TABLE_VERSION,
            "rule_id": rule.rule_id,
            "resource_refs": resource_refs,
            "evidence_refs": evidence_refs,
            "coverage_status": coverage_status,
            "confidence": selected,
            "analytics_completeness": rule.analytics_completeness,
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


def _spend_is_positive(row: PerformanceRow) -> bool:
    """Whether a retained OpenAI row records positive spend.

    The OpenAI read converts a documented USD major-unit amount into minor
    units exactly and refuses any value with unsupported precision before a row
    exists, so a retained minor-unit amount is the exact provider amount rather
    than a rounded projection.  A retained native decimal, where a future
    contract supplies one, is compared ahead of it.
    """
    native = row.provider_extensions.get("openai_ads_spend")
    if isinstance(native, str):
        return Decimal(native) > 0
    return row.spend is not None and row.spend.minor_units > 0


def _quality(record: RetainedSnapshotRecord) -> frozenset[str]:
    return frozenset(record.snapshot.data_quality_statuses)


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
    statuses = {
        record.snapshot.resource_ref: record.snapshot.effective_status
        for record in inventory.records
    }
    for record in inventory.records:
        for parent in record.snapshot.parent_resource_refs:
            parents.setdefault(parent, []).append(record.snapshot.resource_ref)
    childless = {
        "CAMPAIGN": ("OA_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP", "AD_GROUP"),
        "AD_GROUP": ("OA_STRUCTURE_AD_GROUP_WITHOUT_AD", "AD"),
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
        "AD_GROUP": ("OA_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED", "CAMPAIGN"),
        "AD": ("OA_STRUCTURE_AD_PARENT_NOT_RETAINED", "AD_GROUP"),
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
            if not openai_ads_declares_serving_intent(record.snapshot.effective_status):
                continue
            for parent in record.snapshot.parent_resource_refs:
                parent_status = statuses.get(parent)
                if parent_status is None or openai_ads_declares_serving_intent(parent_status):
                    continue
                findings.append(
                    _emit(
                        RULES["OA_STRUCTURE_SERVING_INTENT_MISMATCH"],
                        resource_refs=(record.snapshot.resource_ref, parent),
                        evidence_refs=(record.record_digest, digests[parent]),
                        window=window,
                        coverage_status=status,
                    )
                )
    for resource_type, reviewed in _REVIEWED_STATUSES.items():
        if not reviewed:
            # No reviewed enumeration exists for this resource type, so the
            # audit makes no supported/unsupported claim about its values.
            continue
        for record in grouped[resource_type]:
            if record.snapshot.effective_status.casefold() not in reviewed:
                findings.append(
                    _emit(
                        RULES["OA_STRUCTURE_STATUS_NOT_REVIEWED"],
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
    """Spend, bid, objective, targeting, and tracking rules over retained settings."""
    findings: list[ProviderAuditFinding] = []
    for record in grouped["CAMPAIGN"]:
        state = record.snapshot.normalized_state
        quality = _quality(record)
        refs = (record.snapshot.resource_ref,)
        evidence = (record.record_digest,)
        serving = openai_ads_declares_serving_intent(record.snapshot.effective_status)
        if serving and state.get("openai_ads_lifetime_spend_limit_micros") is None:
            findings.append(
                _emit(
                    RULES["OA_SPEND_LIMIT_NOT_RETURNED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["SPEND"],
                )
            )
        if serving and state.get("schedule_end") is None:
            findings.append(
                _emit(
                    RULES["OA_SPEND_SCHEDULE_NOT_RETURNED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["SPEND"],
                )
            )
        if quality & _UNPROJECTED_MONEY:
            findings.append(
                _emit(
                    RULES["OA_SPEND_NATIVE_AMOUNT_NOT_PROJECTED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["SPEND"],
                )
            )
        bidding_type = state.get("bid_strategy")
        if bidding_type is None:
            findings.append(
                _emit(
                    RULES["OA_OBJECTIVE_BIDDING_TYPE_NOT_RETURNED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["OBJECTIVE"],
                )
            )
        elif bidding_type == "conversions":
            findings.append(
                _emit(
                    RULES["OA_OBJECTIVE_CONVERSION_BIDDING_UNSUPPORTED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["OBJECTIVE"],
                )
            )
        # Guarded by the documented serving intent, like the spend-limit and
        # schedule rules above.  An undeclared targeting object only decides
        # where a campaign delivers, and a campaign that does not declare an
        # intent to serve is not delivering anywhere; reporting it as a
        # targeting exposure would state a delivery consequence the retained
        # status contradicts.
        if serving and OPENAI_ADS_TARGETING_NOT_DECLARED in quality:
            findings.append(
                _emit(
                    RULES["OA_TARGETING_NOT_DECLARED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["TARGETING"],
                )
            )
        if OPENAI_ADS_CONVERSION_SETTINGS_NOT_ASSOCIATED in quality:
            findings.append(
                _emit(
                    RULES["OA_TRACKING_NO_CONVERSION_SETTING_ASSOCIATED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["TRACKING"],
                )
            )
    for record in grouped["AD_GROUP"]:
        refs = (record.snapshot.resource_ref,)
        evidence = (record.record_digest,)
        if OPENAI_ADS_CONTEXT_HINTS_NOT_DECLARED in _quality(record):
            findings.append(
                _emit(
                    RULES["OA_TARGETING_AD_GROUP_WITHOUT_CONTEXT_HINTS"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["TARGETING"],
                )
            )
        if record.snapshot.normalized_state.get("openai_ads_max_bid_micros") is None:
            findings.append(
                _emit(
                    RULES["OA_BIDS_MAX_BID_NOT_RETURNED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["BIDS"],
                )
            )
        if _quality(record) & _UNPROJECTED_MONEY:
            findings.append(
                _emit(
                    RULES["OA_SPEND_NATIVE_AMOUNT_NOT_PROJECTED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["SPEND"],
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
    if _REPORT_COMPLETE not in report.envelope.data_quality_statuses:
        findings.append(
            _emit(
                RULES["OA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED"],
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
                    RULES["OA_DELIVERY_METRICS_MISSING"],
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
                    RULES["OA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
        if row.impressions == 0 and _spend_is_positive(row):
            findings.append(
                _emit(
                    RULES["OA_SPEND_WITHOUT_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
    # An AD-grain OpenAI report proves campaign and ad identifiers but no
    # ad-group parent, so campaign delivery is evaluated from campaign rows only.
    campaign_impressions: dict[str, int] = {}
    for row in report.performance_rows:
        if row.metric_grain != "CAMPAIGN":
            continue
        campaign_impressions[row.resource_ref] = campaign_impressions.get(row.resource_ref, 0) + (
            row.impressions or 0
        )
    if campaign_impressions:
        for record in grouped["CAMPAIGN"]:
            if not openai_ads_declares_serving_intent(record.snapshot.effective_status):
                continue
            if campaign_impressions.get(record.snapshot.resource_ref, 0) > 0:
                continue
            findings.append(
                _emit(
                    RULES["OA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"],
                    resource_refs=(record.snapshot.resource_ref,),
                    evidence_refs=(record.record_digest, report_digest),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
    return findings


def _account_scoped_findings(
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
    coverage: dict[AuditDimension, ProviderCoverageStatus],
) -> list[ProviderAuditFinding]:
    """One finding per unread or presence-only dimension, anchored to the account.

    These fire only when an account snapshot is retained, so an audit with no
    retained inventory reports the same coverage without attaching an
    account-level finding to a resource it never observed.
    """
    accounts = grouped["ACCOUNT"]
    if not accounts:
        return []
    account = accounts[0]
    anchored = [
        ("OA_BIDS_STRATEGY_NOT_READ", True),
        (
            "OA_TARGETING_CRITERIA_VALUES_NOT_READ",
            bool(grouped["CAMPAIGN"]) or bool(grouped["AD_GROUP"]),
        ),
        ("OA_PLACEMENT_NOT_READ", True),
        ("OA_CREATIVE_CONTENT_TEXT_NOT_READ", bool(grouped["AD"])),
        ("OA_CREATIVE_FILE_BYTES_NOT_PROVABLE", bool(grouped["AD"])),
        ("OA_DESTINATION_VALUES_NOT_READ", bool(grouped["AD"])),
        ("OA_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ", True),
        ("OA_NATIVE_CONVERSION_NOT_READ", True),
        ("OA_BROWSER_NOT_APPLICABLE", True),
        ("OA_POLICY_ACCOUNT_SERVING_STATE_NOT_READ", True),
    ]
    findings: list[ProviderAuditFinding] = []
    for rule_id, applies in anchored:
        if not applies:
            continue
        rule = RULES[rule_id]
        findings.append(
            _emit(
                rule,
                resource_refs=(account.snapshot.resource_ref,),
                evidence_refs=(account.record_digest,),
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
        if OPENAI_ADS_CREATIVE_FILE_NOT_ASSOCIATED in quality:
            findings.append(
                _emit(
                    RULES["OA_CREATIVE_FILE_NOT_ASSOCIATED"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["CREATIVE"],
                )
            )
        if quality & {OPENAI_ADS_CREATIVE_TITLE_ABSENT, OPENAI_ADS_CREATIVE_BODY_ABSENT}:
            findings.append(
                _emit(
                    RULES["OA_CREATIVE_COPY_INCOMPLETE"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["CREATIVE"],
                )
            )
        if OPENAI_ADS_CREATIVE_TARGET_URL_ABSENT in quality:
            findings.append(
                _emit(
                    RULES["OA_DESTINATION_TARGET_URL_ABSENT"],
                    resource_refs=refs,
                    evidence_refs=evidence,
                    window=window,
                    coverage_status=coverage["DESTINATION_CONFIGURATION"],
                )
            )
        rule_id = (
            "OA_POLICY_AD_REVIEW_STATUS_NOT_RETURNED"
            if OPENAI_ADS_AD_REVIEW_STATUS_NOT_RETURNED in quality
            else "OA_POLICY_AD_REVIEW_ENUM_NOT_REVIEWED"
        )
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
                    RULES["OA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE"],
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
                RULES["OA_CREATIVE_LOCAL_BINDING_UNVERIFIED"],
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
            paths = snapshot_state_deltas(first.snapshot, second.snapshot)
            if not paths:
                continue
            finding = _emit(
                RULES["OA_DRIFT_STATE_CHANGED"],
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
                    changed_paths=paths,
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
                RULES["OA_DRIFT_ABSENT_FROM_LATER_INVENTORY"],
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
            RULES["OA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY"],
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
    """How many local creative bindings declare an OpenAI Ads resource."""
    catalog = context.evidence.creative_catalog
    if catalog is None:
        return 0
    return sum(
        1
        for entry in catalog.entries
        if entry.provider == PROVIDER and entry.provider_resource_ref is not None
    )


# The read surfaces that back each audit dimension.  `OPENAI_ADS_READ_SURFACES`
# is the one declaration of what this build's fixed OpenAI reads return, so the
# ceiling for a dimension is derived from it here rather than restated as a
# literal that could drift away from the adapter.  `BROWSER_MEASUREMENT` has no
# entry: no provider read could back it, and it is always `NOT_APPLICABLE`.
_DIMENSION_SURFACES: Final[dict[AuditDimension, tuple[str, ...]]] = {
    "STRUCTURE": ("CAMPAIGN_INVENTORY", "AD_GROUP_INVENTORY", "AD_INVENTORY"),
    "DELIVERY": ("CAMPAIGN_DELIVERY_REPORT", "AD_DELIVERY_REPORT"),
    "SPEND": ("CAMPAIGN_LIFETIME_SPEND_LIMIT", "CAMPAIGN_RUN_SCHEDULE"),
    "BIDS": ("CAMPAIGN_BIDDING_TYPE", "AD_GROUP_BILLING_EVENT", "AD_GROUP_MAXIMUM_BID"),
    "OBJECTIVE": ("CAMPAIGN_BIDDING_TYPE",),
    "TARGETING": ("CAMPAIGN_TARGETING_CRITERIA", "AD_GROUP_CONTEXT_HINTS"),
    "PLACEMENT": ("PLACEMENT",),
    "CREATIVE": ("AD_CREATIVE_FILE_ASSOCIATION", "AD_CREATIVE_COPY_PRESENCE"),
    "DESTINATION_CONFIGURATION": ("AD_DESTINATION_URL_PRESENCE",),
    "TRACKING": ("CAMPAIGN_CONVERSION_EVENT_SETTINGS",),
    "NATIVE_CONVERSION": ("NATIVE_CONVERSION_METRICS",),
    "POLICY": ("AD_REVIEW_STATUS",),
    "DRIFT": ("CAMPAIGN_INVENTORY", "AD_GROUP_INVENTORY", "AD_INVENTORY"),
}
_IMPLEMENTED_SURFACES: Final[frozenset[str]] = frozenset(
    {"IMPLEMENTED_FIXED_READ", "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY"}
)


def _surface_backed(dimension: AuditDimension) -> bool:
    """Whether any fixed read this build implements backs one dimension."""
    return any(
        OPENAI_ADS_READ_SURFACES[surface] in _IMPLEMENTED_SURFACES
        for surface in _DIMENSION_SURFACES.get(dimension, ())
    )


def _coverage_map(
    context: ProviderAuditContext, grouped: dict[str, list[RetainedSnapshotRecord]]
) -> dict[AuditDimension, ProviderCoverageStatus]:
    """Give every dimension one status: the evidence supplied, capped by the read.

    The evidence expressions below say what the retained records could support.
    `OPENAI_ADS_READ_SURFACES` then caps each one: a dimension no implemented
    fixed read backs is `NOT_READ` whatever a caller supplies, so a dimension
    can never be reported as read because a record set happened to be present.
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
    has_campaigns = bool(grouped["CAMPAIGN"])
    has_groups = bool(grouped["AD_GROUP"])
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
        # as the whole window even when the adapter consumed every page.
        "DELIVERY": "PARTIAL" if has_report_rows else "NOT_READ",
        "SPEND": ("PARTIAL" if has_report_rows or has_campaigns or has_groups else "NOT_READ"),
        "BIDS": "PARTIAL" if has_campaigns or has_groups else "NOT_READ",
        "OBJECTIVE": "PARTIAL" if has_campaigns else "NOT_READ",
        "TARGETING": "PARTIAL" if has_campaigns or has_groups else "NOT_READ",
        "PLACEMENT": "PARTIAL" if has_campaigns or has_groups else "NOT_READ",
        # Creative evidence is a retained ad snapshot or a local binding that
        # names one; both produce findings, so both are coverage.
        "CREATIVE": "PARTIAL" if has_ads or _local_bindings(context) else "NOT_READ",
        "DESTINATION_CONFIGURATION": "PARTIAL" if has_ads else "NOT_READ",
        "TRACKING": "PARTIAL" if has_campaigns else "NOT_READ",
        "NATIVE_CONVERSION": "PARTIAL" if has_report_rows else "NOT_READ",
        "BROWSER_MEASUREMENT": "NOT_APPLICABLE",
        "POLICY": "PARTIAL" if has_ads else "NOT_READ",
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
    "STRUCTURE": "Account, campaign, ad-group, and ad identity, parents, and declared status are read by the fixed v1 hierarchy inventory.",
    "DELIVERY": "Impressions and clicks come from the fixed daily insights read, whose retained result is never labelled exhaustive by this audit vocabulary.",
    "SPEND": "Retained spend rows, the native campaign lifetime spend limit, the run schedule, and the native ad-group maximum bid are evaluated as retained; nothing is rounded.",
    "BIDS": "The campaign bidding type, the ad-group billing event, and the native maximum bid are read; bid strategy, pacing, and delivery optimization are not.",
    "OBJECTIVE": "The campaign bidding type is the documented optimization intent this read returns; OpenAI documents no separate campaign objective field.",
    "TARGETING": "Only whether a campaign declared a non-empty targeting object, and how many context hints an ad group returned, are read; no criterion or hint value is retained.",
    "PLACEMENT": "No placement configuration field or placement report facet is documented for the reviewed OpenAI Ads surfaces.",
    "CREATIVE": "Ad identity, declared status, chat-card type, the creative file association, and copy field presence are read; copy text and image URLs are not.",
    "DESTINATION_CONFIGURATION": "Only whether an ad's creative declared a target URL is read; no URL, query, or tagging value is retained.",
    "TRACKING": "Only the count of conversion event settings a campaign associates is read; pixel installation and event delivery health are not.",
    "NATIVE_CONVERSION": "The fixed insights request projects impressions, clicks, and spend only; no conversion field is requested.",
    "BROWSER_MEASUREMENT": "A local audit executes no browser and loads no page.",
    "POLICY": "The ad review status is read but has no reviewed enumeration; account review and serving state are not retained in the account snapshot.",
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
    """Name, for every dimension, exactly the evidence it was evaluated from.

    A dimension evaluated over a whole retained inventory names its sources and
    its record count rather than truncating a digest list; each finding still
    cites the exact record digests it used.  A dimension evaluated from the one
    selected report also cites that report's record digest.
    """
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
            # As for a declined row below: the proposed groups are disjoint, so
            # the first rule identifier in content order names the exact row
            # inside the subject's fixed size bound, and the row's own
            # `rule_ids` carries the complete membership.
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
        {"OA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"},
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
            "OA_SPEND_WITHOUT_IMPRESSIONS",
            "OA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
            "OA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
        },
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
        {
            "OA_DRIFT_STATE_CHANGED",
            "OA_DRIFT_ABSENT_FROM_LATER_INVENTORY",
            "OA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
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
    # The one mutation this build can compile for an existing OpenAI campaign.
    # It is a suggestion to compile a plan, never a plan: it carries no digest,
    # no authorization, and nothing here is executed.
    #
    # Membership is exactly the rules whose findings the named route can act
    # on, and about which this hypothesis is true.  `plan openai-pause` accepts
    # a campaign identifier only, and the hypothesis asserts that the named
    # campaign declares the documented serving intent, so every bound rule has
    # to be campaign-scoped *and* guarded by
    # `openai_ads_declares_serving_intent`.  Both rules below are.
    # `OA_SPEND_WITHOUT_IMPRESSIONS` is deliberately not: it is a report-row
    # rule whose resource is whatever grain the retained row named -- an
    # `openai_ads.ad.*` reference for the AD-grain insights read -- which no
    # route in this build can pause.  It keeps its `OBSERVE` row above.
    _propose(
        {"OA_SPEND_LIMIT_NOT_RETURNED", "OA_TARGETING_NOT_DECLARED"},
        operation_kind="PAUSE",
        hypothesis=(
            "An OpenAI campaign that declares the documented `active` serving intent, and whose "
            "retained read bounds neither its maximum spend nor its targeting, may be worth "
            f"pausing while it is investigated; the `{PAUSE_PLAN_ROUTE}` route can describe that pause as an offline plan."
        ),
        expected_impact=(
            "A pause would stop further spend on the named campaigns. Execution is unreleased in "
            "0.1.0, so nothing here pauses anything and this is not a compiled plan. The pause is "
            "the marketer's manual step in OpenAI Ads Manager; planning stays available offline."
        ),
        assumptions=(
            "A retained read that returned no spend limit or no targeting object is not proof that the campaign has neither.",
            "A declared `active` status is an intent to serve, not an observation of delivery.",
            "Pausing is reversible in OpenAI Ads, but the resulting delivery gap is not.",
            f"This names the `{PAUSE_PLAN_ROUTE}` route only, which describes the pause as an offline plan. Nothing executes a plan in 0.1.0, so the marketer makes the pause by hand.",
        ),
        confidence="MEDIUM",
        risk="MEDIUM",
        completeness=_NOT_ANALYTICS,
    )
    _no_recommendation(
        {"OA_STRUCTURE_SERVING_INTENT_MISMATCH", "OA_BIDS_MAX_BID_NOT_RETURNED"},
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="Reconciling a status or setting a maximum bid would require an OpenAI update operation, which this build cannot compile.",
        considered="UPDATE",
    )
    _no_recommendation(
        {
            "OA_CREATIVE_FILE_NOT_ASSOCIATED",
            "OA_CREATIVE_COPY_INCOMPLETE",
            "OA_DESTINATION_TARGET_URL_ABSENT",
            "OA_TRACKING_NO_CONVERSION_SETTING_ASSOCIATED",
        },
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="Associating a creative file, copy, destination, or conversion setting with an existing OpenAI ad or campaign is not a compilable operation in this build; the offline compiler only produces a complete new paused candidate.",
        considered="ASSOCIATE",
    )
    _no_recommendation(
        {"OA_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP", "OA_STRUCTURE_AD_GROUP_WITHOUT_AD"},
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="The offline OpenAI compiler produces only a complete new paused campaign, ad-group, and chat-card candidate; it cannot compile a child into an existing retained parent.",
        considered="CREATE",
    )
    _no_recommendation(
        {"OA_OBJECTIVE_CONVERSION_BIDDING_UNSUPPORTED"},
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="This build refuses to compile a conversion-optimized OpenAI campaign at all, so it proposes no change to one that already exists.",
        considered="CREATE",
    )
    _no_recommendation(
        {
            "OA_STRUCTURE_STATUS_NOT_REVIEWED",
            "OA_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED",
            "OA_STRUCTURE_AD_PARENT_NOT_RETAINED",
            "OA_SPEND_NATIVE_AMOUNT_NOT_PROJECTED",
            "OA_SPEND_SCHEDULE_NOT_RETURNED",
            "OA_OBJECTIVE_BIDDING_TYPE_NOT_RETURNED",
            "OA_TARGETING_AD_GROUP_WITHOUT_CONTEXT_HINTS",
            "OA_DELIVERY_METRICS_MISSING",
            "OA_POLICY_AD_REVIEW_STATUS_NOT_RETURNED",
            "OA_POLICY_AD_REVIEW_ENUM_NOT_REVIEWED",
            "OA_CREATIVE_LOCAL_BINDING_UNVERIFIED",
            "OA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
            "OA_CREATIVE_FILE_BYTES_NOT_PROVABLE",
        },
        reason="EVIDENCE_INSUFFICIENT",
        detail="These findings record missing, unreviewed, or unverifiable evidence. Resolving them needs a documentation review or a further read, not an advertising operation.",
    )
    _no_recommendation(
        {
            "OA_BIDS_STRATEGY_NOT_READ",
            "OA_TARGETING_CRITERIA_VALUES_NOT_READ",
            "OA_PLACEMENT_NOT_READ",
            "OA_CREATIVE_CONTENT_TEXT_NOT_READ",
            "OA_DESTINATION_VALUES_NOT_READ",
            "OA_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ",
            "OA_NATIVE_CONVERSION_NOT_READ",
            "OA_BROWSER_NOT_APPLICABLE",
            "OA_POLICY_ACCOUNT_SERVING_STATE_NOT_READ",
        },
        reason="COVERAGE_NOT_READ",
        detail="These dimensions are unread, presence-only, or not applicable in this build's fixed OpenAI read. An unread dimension yields no recommendation and is never reported as healthy.",
    )
    return recommendations, absences, exceeded


def evaluate(context: ProviderAuditContext) -> ProviderAuditSections:
    """Evaluate the OpenAI Ads rule table over retained records only."""
    if context.provider != PROVIDER:
        raise ValueError("The OpenAI rule table evaluates OpenAI Ads evidence only")
    window = _window(context.report)
    inventory = context.evidence.inventory
    grouped = _by_type(inventory)
    statuses = _coverage_map(context, grouped)
    findings: list[ProviderAuditFinding] = []
    if inventory is not None and inventory.records:
        findings.extend(_structure_findings(grouped, inventory, window, statuses["STRUCTURE"]))
        findings.extend(_campaign_setting_findings(grouped, window, statuses))
        findings.extend(_ad_findings(context, grouped, window, statuses))
        findings.extend(_account_scoped_findings(grouped, window, statuses))
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
        # No reviewed OpenAI analytics read can be published as exhaustive by
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
