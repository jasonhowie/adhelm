# ruff: noqa: E501
"""Deterministic Reddit Ads audit rules over retained local records.

This module makes **no provider request**.  It evaluates the retained
`PERFORMANCE_OBSERVATION` report the caller selected, the retained
`RESOURCE_SNAPSHOT` set a completed inventory read appended, an optional
earlier snapshot set, and an optional local creative catalog.  Audit-time
quota, 401/403, and 429 handling therefore stays entirely with the read
adapters that produced those records; nothing here can leak a response body
because no response is available here.

Two honesty bounds shape every rule:

* A Reddit report is read as **one provider response**.  The reviewed material
  establishes that reports paginate but not how a continuation is fetched, so
  the adapter fails closed when the provider signals one and never follows a
  guessed request.  A retained report is therefore never exhaustive: every
  finding derived from it is `BOUNDED_NON_EXHAUSTIVE`, states that absence is not
  zero, and never claims a total.
* The reviewed fixed read returns identity, hierarchy, declared status, and the
  campaign objective only.  Targeting criteria, bids, budgets, run schedules,
  placements, creative content, ad review state, landing pages, UTM values, and
  conversion tracking are not retained by this build, so their dimensions are
  `NOT_READ` rather than healthy.

`docs/implementation/reddit-audit-recommendations.md` carries the rule table,
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
from adhelm.providers.reddit_ads import (
    REDDIT_ADS_READ_SURFACES,
    REDDIT_ADS_REVIEWED_STATUSES,
    REDDIT_ADS_SERVING_INTENT_STATUS,
)
from adhelm.reporting import NormalizedReport, PerformanceRow

PROVIDER: Final[Provider] = "reddit_ads"
# Bump this when a rule's semantics change.  It is part of every finding's
# content identity, so an audit produced by a different rule table can never be
# mistaken for one produced by this table.
RULE_TABLE_VERSION: Final = "reddit_ads.rules.2026-10-10"
_BOUNDED: Final[AnalyticsCompleteness] = "BOUNDED_NON_EXHAUSTIVE"
_NOT_ANALYTICS: Final[AnalyticsCompleteness] = "NOT_APPLICABLE"
# The inventory records the status "NOT_AVAILABLE_FROM_PROVIDER" when the
# provider returned neither status field.  It is a statement about the read, not
# a Reddit status value, so no status rule treats it as one.
_STATUS_NOT_RETURNED: Final = "NOT_AVAILABLE_FROM_PROVIDER"
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


def _info(
    rule_id: str,
    finding_type: str,
    dimension: AuditDimension,
    observation: str,
    suggested_action: str,
) -> _Rule:
    """An always-on, high-confidence, low-risk statement about an unread surface."""
    return _Rule(
        rule_id=rule_id,
        finding_type=finding_type,
        dimension=dimension,
        severity="INFO",
        confidence="HIGH",
        risk="LOW",
        analytics_completeness=_NOT_ANALYTICS,
        observation=observation,
        suggested_action=suggested_action,
    )


_RULE_LIST: Final[tuple[_Rule, ...]] = (
    _Rule(
        rule_id="RD_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP",
        finding_type="REDDIT_CAMPAIGN_WITHOUT_AD_GROUP",
        dimension="STRUCTURE",
        severity="MEDIUM",
        confidence="HIGH",
        risk="MEDIUM",
        analytics_completeness=_NOT_ANALYTICS,
        observation="This retained Reddit campaign has no retained child ad group in the same inventory read.",
        suggested_action="Confirm the campaign's ad groups in the Reddit Ads interface; a campaign with no ad group cannot serve.",
    ),
    _Rule(
        rule_id="RD_STRUCTURE_AD_GROUP_WITHOUT_AD",
        finding_type="REDDIT_AD_GROUP_WITHOUT_AD",
        dimension="STRUCTURE",
        severity="MEDIUM",
        confidence="HIGH",
        risk="MEDIUM",
        analytics_completeness=_NOT_ANALYTICS,
        observation="This retained Reddit ad group has no retained child ad in the same inventory read.",
        suggested_action="Confirm the ad group's ads in the Reddit Ads interface; an ad group with no ad cannot serve.",
    ),
    _Rule(
        rule_id="RD_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED",
        finding_type="REDDIT_AD_GROUP_PARENT_CAMPAIGN_NOT_RETAINED",
        dimension="STRUCTURE",
        severity="MEDIUM",
        confidence="HIGH",
        risk="MEDIUM",
        analytics_completeness=_NOT_ANALYTICS,
        observation="This retained Reddit ad group names a parent campaign that is absent from the same retained inventory.",
        suggested_action="Read the bounded inventory again; an ad group whose parent campaign is missing cannot be evaluated in context.",
    ),
    _Rule(
        rule_id="RD_STRUCTURE_AD_PARENT_NOT_RETAINED",
        finding_type="REDDIT_AD_PARENT_AD_GROUP_NOT_RETAINED",
        dimension="STRUCTURE",
        severity="MEDIUM",
        confidence="HIGH",
        risk="MEDIUM",
        analytics_completeness=_NOT_ANALYTICS,
        observation="This retained Reddit ad names a parent ad group that is absent from the same retained inventory.",
        suggested_action="Read the bounded inventory again; an ad whose parent ad group is missing cannot be evaluated in context.",
    ),
    _Rule(
        rule_id="RD_STRUCTURE_AD_CAMPAIGN_MISMATCH",
        finding_type="REDDIT_AD_CAMPAIGN_DIFFERS_FROM_AD_GROUP_CAMPAIGN",
        dimension="STRUCTURE",
        severity="MEDIUM",
        confidence="HIGH",
        risk="MEDIUM",
        analytics_completeness=_NOT_ANALYTICS,
        observation="This retained Reddit ad names a campaign that is not the campaign of its retained ad group, so the two reads describe an inconsistent hierarchy.",
        suggested_action="Read the bounded inventory again before relying on this ad's position in the hierarchy; the audit does not decide which parent is right.",
    ),
    _Rule(
        rule_id="RD_STRUCTURE_SERVING_INTENT_MISMATCH",
        finding_type="REDDIT_SERVING_INTENT_MISMATCH_WITH_PARENT",
        dimension="STRUCTURE",
        severity="MEDIUM",
        confidence="HIGH",
        risk="MEDIUM",
        analytics_completeness=_NOT_ANALYTICS,
        observation="This retained Reddit resource declares the ACTIVE status while its retained parent does not, so its declared intent cannot be met.",
        suggested_action="Reconcile the parent and child statuses in the Reddit Ads interface; this audit reads declared status only, not serving state.",
    ),
    _Rule(
        rule_id="RD_STRUCTURE_STATUS_NOT_RETURNED",
        finding_type="REDDIT_STATUS_NOT_RETURNED",
        dimension="STRUCTURE",
        severity="LOW",
        confidence="HIGH",
        risk="LOW",
        analytics_completeness=_NOT_ANALYTICS,
        observation="Reddit returned neither a configured nor an effective status for this retained resource, so neither a serving nor a paused state can be attributed to it.",
        suggested_action="Read the bounded inventory again; an absent status is not an active resource.",
    ),
    _Rule(
        rule_id="RD_STRUCTURE_STATUS_NOT_REVIEWED",
        finding_type="REDDIT_STATUS_VALUE_NOT_IN_REVIEWED_ENUM",
        dimension="STRUCTURE",
        severity="INFO",
        confidence="HIGH",
        risk="LOW",
        analytics_completeness=_NOT_ANALYTICS,
        observation="This retained Reddit resource carries a status outside the two values this build reviewed, ACTIVE and PAUSED.",
        suggested_action="Recheck the Reddit status documentation for this resource type before interpreting the value; the audit does not guess its meaning.",
    ),
    _Rule(
        rule_id="RD_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS",
        finding_type="REDDIT_ACTIVE_CAMPAIGN_WITHOUT_RETAINED_IMPRESSIONS",
        dimension="DELIVERY",
        severity="MEDIUM",
        confidence="LOW",
        risk="MEDIUM",
        analytics_completeness=_BOUNDED,
        observation="This retained Reddit campaign declares the ACTIVE status and the retained bounded report contains no impressions for it. The report is not exhaustive, so this is not proof of zero delivery.",
        suggested_action="Read the same bounded campaign report again over a compatible window before treating the absence of rows as zero delivery.",
    ),
    _Rule(
        rule_id="RD_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
        finding_type="REDDIT_CLICKS_EXCEED_IMPRESSIONS",
        dimension="DELIVERY",
        severity="MEDIUM",
        confidence="HIGH",
        risk="MEDIUM",
        analytics_completeness=_BOUNDED,
        observation="This retained Reddit report row declares more clicks than impressions at its declared grain, which no consistent delivery aggregate produces.",
        suggested_action="Read the same bounded report again and compare the declared grain and window before using this row for any decision.",
    ),
    _Rule(
        rule_id="RD_SPEND_WITHOUT_IMPRESSIONS",
        finding_type="REDDIT_SPEND_WITHOUT_IMPRESSIONS",
        dimension="SPEND",
        severity="HIGH",
        confidence="HIGH",
        risk="HIGH",
        analytics_completeness=_BOUNDED,
        observation="This retained Reddit report row declares spend with zero impressions at its declared grain. The retained native micros amount, not a rounded cent projection, was compared.",
        suggested_action="Read the same bounded report again and reconcile the row in the Reddit Ads interface before drawing a spend conclusion.",
    ),
    _info(
        "RD_SPEND_PACING_INPUTS_NOT_READ",
        "REDDIT_BUDGET_AND_SCHEDULE_NOT_READ",
        "SPEND",
        "Budget, spend-cap and run-schedule fields are not retained by this build's fixed Reddit read, so budget pacing against a schedule is not evaluated for this account.",
        "Treat pacing as unevaluated; do not infer a pacing state from retained delivery aggregates alone.",
    ),
    _info(
        "RD_BIDS_NOT_READ",
        "REDDIT_BID_CONFIGURATION_NOT_READ",
        "BIDS",
        "Bid strategy and bid value are not retained by this build's fixed Reddit read, so no bid configuration is evaluated for this account.",
        "Review bid configuration in the Reddit Ads interface; this audit makes no bid claim.",
    ),
    _Rule(
        rule_id="RD_OBJECTIVE_NOT_RETURNED",
        finding_type="REDDIT_CAMPAIGN_OBJECTIVE_NOT_RETURNED",
        dimension="OBJECTIVE",
        severity="INFO",
        confidence="HIGH",
        risk="LOW",
        analytics_completeness=_NOT_ANALYTICS,
        observation="This retained Reddit campaign snapshot carries no objective value, so its optimization intent is unknown to this audit.",
        suggested_action="Read the bounded campaign inventory again; an absent objective is not a default objective.",
    ),
    _Rule(
        rule_id="RD_OBJECTIVE_ENUM_NOT_REVIEWED",
        finding_type="REDDIT_CAMPAIGN_OBJECTIVE_ENUM_NOT_REVIEWED",
        dimension="OBJECTIVE",
        severity="INFO",
        confidence="HIGH",
        risk="LOW",
        analytics_completeness=_NOT_ANALYTICS,
        observation="Reddit has announced changes to its campaign objective enumeration and this build pins no reviewed list, so the retained objective value is surfaced without being classified as supported or unsupported.",
        suggested_action="Review the retained objective value against current Reddit documentation before relying on it in an optimization decision.",
    ),
    _info(
        "RD_TARGETING_NOT_READ",
        "REDDIT_TARGETING_CRITERIA_NOT_READ",
        "TARGETING",
        "Ad group targeting criteria are not retained by this build's fixed Reddit read, so no targeting claim is available for this account.",
        "Review targeting in the Reddit Ads interface; this audit neither reads nor infers audience criteria.",
    ),
    _info(
        "RD_PLACEMENT_NOT_READ",
        "REDDIT_PLACEMENT_NOT_READ",
        "PLACEMENT",
        "Placement configuration and placement report facets are not retained by this build's fixed Reddit read, so no placement claim is available.",
        "Review placement settings in the Reddit Ads interface; this audit makes no placement claim.",
    ),
    _info(
        "RD_CREATIVE_CONTENT_NOT_READ",
        "REDDIT_CREATIVE_CONTENT_NOT_READ",
        "CREATIVE",
        "Ad content fields, including the referenced post, are not retained by this build's fixed Reddit read. Only ad identity, declared status, and hierarchy association are retained.",
        "Review ad content and calls to action in the Reddit Ads interface; this audit makes no creative-content claim.",
    ),
    _Rule(
        rule_id="RD_CREATIVE_LOCAL_BINDING_UNVERIFIED",
        finding_type="REDDIT_LOCAL_CREATIVE_BINDING_UNVERIFIED",
        dimension="CREATIVE",
        severity="LOW",
        confidence="HIGH",
        risk="MEDIUM",
        analytics_completeness=_NOT_ANALYTICS,
        observation="A local creative version declares a Reddit ad reference, but the retained inventory carries no provider-verified creative association for it.",
        suggested_action="Treat the local binding as a declaration only; an independently reviewed provider association is still required.",
    ),
    _Rule(
        rule_id="RD_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
        finding_type="REDDIT_LOCAL_CREATIVE_BINDING_WITHOUT_RETAINED_RESOURCE",
        dimension="CREATIVE",
        severity="MEDIUM",
        confidence="HIGH",
        risk="MEDIUM",
        analytics_completeness=_NOT_ANALYTICS,
        observation="A local creative version declares a Reddit ad reference that is absent from the retained inventory for this account.",
        suggested_action="Read the bounded ad inventory again and reconcile the local declaration; do not assume the provider resource exists.",
    ),
    _info(
        "RD_DESTINATION_NOT_READ",
        "REDDIT_DESTINATION_AND_UTM_NOT_READ",
        "DESTINATION_CONFIGURATION",
        "Click URLs and campaign-tagging fields are not retained by this build's fixed Reddit read, so no destination or UTM claim is available.",
        "Review destination URLs and tagging in the Reddit Ads interface; this audit reads no URL or query value.",
    ),
    _info(
        "RD_TRACKING_NOT_READ",
        "REDDIT_TRACKING_CONFIGURATION_NOT_READ",
        "TRACKING",
        "Conversion pixel and tracking configuration are not retained by this build's fixed Reddit read, so no tracking health claim is available for this account.",
        "Verify tracking configuration through its own reviewed surface; retained delivery aggregates cannot establish it.",
    ),
    _info(
        "RD_NATIVE_CONVERSION_NOT_READ",
        "REDDIT_NATIVE_CONVERSION_NOT_READ",
        "NATIVE_CONVERSION",
        "The fixed Reddit report requests impressions, clicks, and spend only, so no native conversion count, action, or attribution context is available for this account.",
        "Do not infer a conversion count from clicks or sessions; a native conversion read is required and is not implemented here.",
    ),
    _info(
        "RD_BROWSER_NOT_APPLICABLE",
        "REDDIT_BROWSER_MEASUREMENT_NOT_APPLICABLE_LOCAL_AUDIT",
        "BROWSER_MEASUREMENT",
        "This local audit executes no browser and loads no page, so browser measurement is outside what it can evaluate for this account.",
        "Use the separate reviewed browser verification path; no local audit result is evidence of real-browser firing.",
    ),
    _info(
        "RD_POLICY_REVIEW_STATE_NOT_READ",
        "REDDIT_POLICY_REVIEW_STATE_NOT_READ",
        "POLICY",
        "Ad review and rejection states, and the account status, are not retained by this build's fixed Reddit read, so no policy health claim is available.",
        "Review policy and ad review state in the Reddit Ads interface; an absent rejection is not an approval.",
    ),
    _Rule(
        rule_id="RD_DRIFT_STATE_CHANGED",
        finding_type="REDDIT_OUT_OF_BAND_STATE_CHANGE_OBSERVED",
        dimension="DRIFT",
        severity="MEDIUM",
        confidence="HIGH",
        risk="MEDIUM",
        analytics_completeness=_NOT_ANALYTICS,
        observation="Two retained snapshots of this Reddit resource differ, and this product applied no change between them. The actor is UNKNOWN and the change occurred somewhere inside the recorded observation interval.",
        suggested_action="Reconcile the change against the Reddit Ads interface before planning anything; no exact external change time or actor is available.",
    ),
    _Rule(
        rule_id="RD_DRIFT_ABSENT_FROM_LATER_INVENTORY",
        finding_type="REDDIT_RESOURCE_ABSENT_FROM_LATER_INVENTORY",
        dimension="DRIFT",
        severity="MEDIUM",
        confidence="MEDIUM",
        risk="MEDIUM",
        analytics_completeness=_NOT_ANALYTICS,
        observation="This Reddit resource is present in the earlier retained inventory and absent from the later one. Absence is not proof of deletion; the actor is UNKNOWN.",
        suggested_action="Read the bounded inventory again and confirm the resource in the Reddit Ads interface before treating it as removed.",
    ),
    _Rule(
        rule_id="RD_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
        finding_type="REDDIT_RESOURCE_ABSENT_FROM_EARLIER_INVENTORY",
        dimension="DRIFT",
        severity="LOW",
        confidence="MEDIUM",
        risk="LOW",
        analytics_completeness=_NOT_ANALYTICS,
        observation="This Reddit resource is present in the later retained inventory and absent from the earlier one. This product created nothing; the actor is UNKNOWN.",
        suggested_action="Confirm who created the resource in the Reddit Ads interface; no exact external creation time or actor is available.",
    ),
)
RULES: Final[dict[str, _Rule]] = {rule.rule_id: rule for rule in _RULE_LIST}


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


def _is_serving_intent(status: str) -> bool:
    return status == REDDIT_ADS_SERVING_INTENT_STATUS


def _spend_is_positive(row: PerformanceRow) -> bool:
    """Compare the retained native micros, never a rounded cent projection."""
    native = row.provider_extensions.get("reddit_ads_spend_micros")
    if isinstance(native, str):
        return Decimal(native) > 0
    return row.spend is not None and row.spend.minor_units > 0


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
    children: dict[str, list[str]] = {}
    by_ref = {record.snapshot.resource_ref: record for record in inventory.records}
    for record in inventory.records:
        for parent in record.snapshot.parent_resource_refs:
            children.setdefault(parent, []).append(record.snapshot.resource_ref)
    campaign_refs = {item.snapshot.resource_ref for item in grouped["CAMPAIGN"]}
    group_refs = {item.snapshot.resource_ref for item in grouped["AD_GROUP"]}
    ad_refs = {item.snapshot.resource_ref for item in grouped["AD"]}
    for record in grouped["CAMPAIGN"]:
        if not group_refs & set(children.get(record.snapshot.resource_ref, ())):
            findings.append(
                _emit(
                    RULES["RD_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP"],
                    resource_refs=(record.snapshot.resource_ref,),
                    evidence_refs=(record.record_digest,),
                    window=window,
                    coverage_status=status,
                    confidence=absence_confidence,
                )
            )
    for record in grouped["AD_GROUP"]:
        if not ad_refs & set(children.get(record.snapshot.resource_ref, ())):
            findings.append(
                _emit(
                    RULES["RD_STRUCTURE_AD_GROUP_WITHOUT_AD"],
                    resource_refs=(record.snapshot.resource_ref,),
                    evidence_refs=(record.record_digest,),
                    window=window,
                    coverage_status=status,
                    confidence=absence_confidence,
                )
            )
    orphan_rules = {
        "AD_GROUP": ("RD_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED", campaign_refs),
        "AD": ("RD_STRUCTURE_AD_PARENT_NOT_RETAINED", group_refs),
    }
    for resource_type, (rule_id, known) in orphan_rules.items():
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
    for record in grouped["AD"]:
        # An ad's retained campaign must be the campaign of its retained ad
        # group. Both are declared parents of the ad, so a disagreement between
        # them is an inconsistent read, not a hierarchy this audit can repair.
        ad_campaigns = set(record.snapshot.parent_resource_refs) & campaign_refs
        for parent in sorted(set(record.snapshot.parent_resource_refs) & group_refs):
            group_campaigns = set(by_ref[parent].snapshot.parent_resource_refs) & campaign_refs
            if ad_campaigns and group_campaigns and not ad_campaigns <= group_campaigns:
                findings.append(
                    _emit(
                        RULES["RD_STRUCTURE_AD_CAMPAIGN_MISMATCH"],
                        resource_refs=(record.snapshot.resource_ref, parent),
                        evidence_refs=(record.record_digest, by_ref[parent].record_digest),
                        window=window,
                        coverage_status=status,
                    )
                )
    for resource_type in ("CAMPAIGN", "AD_GROUP", "AD"):
        for record in grouped[resource_type]:
            if not _is_serving_intent(record.snapshot.effective_status):
                continue
            for parent in record.snapshot.parent_resource_refs:
                parent_record = by_ref.get(parent)
                if (
                    parent_record is None
                    or parent_record.snapshot.effective_status == _STATUS_NOT_RETURNED
                    or _is_serving_intent(parent_record.snapshot.effective_status)
                ):
                    continue
                findings.append(
                    _emit(
                        RULES["RD_STRUCTURE_SERVING_INTENT_MISMATCH"],
                        resource_refs=(record.snapshot.resource_ref, parent),
                        evidence_refs=(record.record_digest, parent_record.record_digest),
                        window=window,
                        coverage_status=status,
                    )
                )
    for resource_type in ("CAMPAIGN", "AD_GROUP", "AD"):
        for record in grouped[resource_type]:
            value = record.snapshot.effective_status
            if value == _STATUS_NOT_RETURNED:
                rule_id = "RD_STRUCTURE_STATUS_NOT_RETURNED"
            elif value not in REDDIT_ADS_REVIEWED_STATUSES:
                rule_id = "RD_STRUCTURE_STATUS_NOT_REVIEWED"
            else:
                continue
            findings.append(
                _emit(
                    RULES[rule_id],
                    resource_refs=(record.snapshot.resource_ref,),
                    evidence_refs=(record.record_digest,),
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
    for row in report.performance_rows:
        if row.impressions is None or row.clicks is None:
            continue
        if row.clicks > row.impressions:
            findings.append(
                _emit(
                    RULES["RD_DELIVERY_CLICKS_EXCEED_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
        if row.impressions == 0 and _spend_is_positive(row):
            findings.append(
                _emit(
                    RULES["RD_SPEND_WITHOUT_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
    # An AD-grain report carries native ad rows that this build cannot join to a
    # campaign, so campaign delivery is only evaluated from campaign rows.
    campaign_impressions: dict[str, int] = {}
    for row in report.performance_rows:
        if row.metric_grain != "CAMPAIGN":
            continue
        campaign_impressions[row.resource_ref] = campaign_impressions.get(row.resource_ref, 0) + (
            row.impressions or 0
        )
    if campaign_impressions:
        for record in grouped["CAMPAIGN"]:
            if not _is_serving_intent(record.snapshot.effective_status):
                continue
            if campaign_impressions.get(record.snapshot.resource_ref, 0) > 0:
                continue
            findings.append(
                _emit(
                    RULES["RD_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"],
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
    """One finding per unread or inapplicable dimension, anchored to the account.

    These fire only when an account snapshot is retained, so an audit with no
    retained inventory reports the same coverage without attaching an
    account-level finding to a resource it never observed.
    """
    accounts = grouped["ACCOUNT"]
    if not accounts:
        return []
    account = accounts[0]
    anchored = [
        ("RD_SPEND_PACING_INPUTS_NOT_READ", True),
        ("RD_BIDS_NOT_READ", True),
        ("RD_TARGETING_NOT_READ", bool(grouped["AD_GROUP"])),
        ("RD_PLACEMENT_NOT_READ", True),
        ("RD_CREATIVE_CONTENT_NOT_READ", bool(grouped["AD"])),
        ("RD_DESTINATION_NOT_READ", True),
        ("RD_TRACKING_NOT_READ", True),
        ("RD_NATIVE_CONVERSION_NOT_READ", True),
        ("RD_BROWSER_NOT_APPLICABLE", True),
        ("RD_POLICY_REVIEW_STATE_NOT_READ", True),
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


def _objective_findings(
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
    status: ProviderCoverageStatus,
) -> list[ProviderAuditFinding]:
    findings: list[ProviderAuditFinding] = []
    for record in grouped["CAMPAIGN"]:
        rule_id = (
            "RD_OBJECTIVE_ENUM_NOT_REVIEWED"
            if record.snapshot.normalized_state.get("objective") is not None
            else "RD_OBJECTIVE_NOT_RETURNED"
        )
        findings.append(
            _emit(
                RULES[rule_id],
                resource_refs=(record.snapshot.resource_ref,),
                evidence_refs=(record.record_digest,),
                window=window,
                coverage_status=status,
            )
        )
    return findings


def _creative_findings(
    context: ProviderAuditContext,
    grouped: dict[str, list[RetainedSnapshotRecord]],
    window: AuditDataWindow,
    status: ProviderCoverageStatus,
) -> list[ProviderAuditFinding]:
    catalog = context.evidence.creative_catalog
    if catalog is None:
        return []
    findings: list[ProviderAuditFinding] = []
    retained = {record.snapshot.resource_ref: record for record in grouped["AD"]}
    for entry in sorted(catalog.entries, key=lambda item: item.creative_version_id):
        if entry.provider != PROVIDER or entry.provider_resource_ref is None:
            continue
        bound = retained.get(entry.provider_resource_ref)
        if bound is None:
            findings.append(
                _emit(
                    RULES["RD_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE"],
                    resource_refs=(entry.creative_version_id, entry.provider_resource_ref),
                    evidence_refs=(entry.record_digest,),
                    window=window,
                    coverage_status=status,
                )
            )
            continue
        findings.append(
            _emit(
                RULES["RD_CREATIVE_LOCAL_BINDING_UNVERIFIED"],
                resource_refs=(entry.creative_version_id, entry.provider_resource_ref),
                evidence_refs=(entry.record_digest, bound.record_digest),
                window=window,
                coverage_status=status,
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
                RULES["RD_DRIFT_STATE_CHANGED"],
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
        elif first is not None and absence_is_evidence:
            finding = _emit(
                RULES["RD_DRIFT_ABSENT_FROM_LATER_INVENTORY"],
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
        elif second is not None and first is None and absence_is_evidence:
            finding = _emit(
                RULES["RD_DRIFT_ABSENT_FROM_EARLIER_INVENTORY"],
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
    """How many local creative bindings declare a Reddit resource."""
    catalog = context.evidence.creative_catalog
    if catalog is None:
        return 0
    return sum(
        1
        for entry in catalog.entries
        if entry.provider == PROVIDER and entry.provider_resource_ref is not None
    )


# The read surfaces that back each audit dimension.  `REDDIT_ADS_READ_SURFACES`
# is the one declaration of what this build's fixed Reddit reads return, so the
# ceiling for a dimension is derived from it here rather than restated as a
# literal that could drift away from the adapter.  `BROWSER_MEASUREMENT` has no
# entry: no provider read could back it, and it is always `NOT_APPLICABLE`.
_DIMENSION_SURFACES: Final[dict[AuditDimension, tuple[str, ...]]] = {
    "STRUCTURE": ("CAMPAIGN_INVENTORY", "AD_GROUP_INVENTORY", "AD_INVENTORY"),
    "DELIVERY": ("CAMPAIGN_DELIVERY_REPORT", "AD_DELIVERY_REPORT"),
    "SPEND": ("CAMPAIGN_DELIVERY_REPORT", "AD_DELIVERY_REPORT"),
    "BIDS": ("CAMPAIGN_BID_AND_BUDGET",),
    "OBJECTIVE": ("CAMPAIGN_OBJECTIVE",),
    "TARGETING": ("CAMPAIGN_TARGETING",),
    "PLACEMENT": ("PLACEMENT",),
    "CREATIVE": ("AD_INVENTORY",),
    "DESTINATION_CONFIGURATION": ("LANDING_PAGE_AND_UTM",),
    "TRACKING": ("CONVERSION_TRACKING",),
    "NATIVE_CONVERSION": ("NATIVE_CONVERSION_METRICS",),
    "POLICY": ("AD_REVIEW_STATE", "ACCOUNT_SERVING_STATUS"),
    "DRIFT": ("CAMPAIGN_INVENTORY", "AD_GROUP_INVENTORY", "AD_INVENTORY"),
}
_IMPLEMENTED_SURFACES: Final[frozenset[str]] = frozenset(
    {"IMPLEMENTED_FIXED_READ", "IMPLEMENTED_FIXED_READ_SINGLE_RESPONSE"}
)


def _surface_backed(dimension: AuditDimension) -> bool:
    """Whether any fixed read this build implements backs one dimension."""
    return any(
        REDDIT_ADS_READ_SURFACES[surface] in _IMPLEMENTED_SURFACES
        for surface in _DIMENSION_SURFACES.get(dimension, ())
    )


def _coverage_map(
    context: ProviderAuditContext, grouped: dict[str, list[RetainedSnapshotRecord]]
) -> dict[AuditDimension, ProviderCoverageStatus]:
    """Give every dimension one status: the evidence supplied, capped by the read.

    The evidence expressions below say what the retained records could support.
    `REDDIT_ADS_READ_SURFACES` then caps each one: a dimension no implemented
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
        # Never COVERED: the report is one provider response, and the reviewed
        # material documents no continuation, so no retained report proves it
        # returned every row.
        "DELIVERY": "PARTIAL" if has_report_rows else "NOT_READ",
        "SPEND": "PARTIAL" if has_report_rows else "NOT_READ",
        "BIDS": "NOT_READ",
        "OBJECTIVE": "PARTIAL" if grouped["CAMPAIGN"] else "NOT_READ",
        "TARGETING": "NOT_READ",
        "PLACEMENT": "NOT_READ",
        # Creative evidence is a retained ad snapshot or a local binding checked
        # against a retained inventory; a binding with no inventory to check it
        # against yields no finding, so it is not coverage.
        "CREATIVE": (
            "PARTIAL"
            if has_inventory and (grouped["AD"] or _local_bindings(context))
            else "NOT_READ"
        ),
        "DESTINATION_CONFIGURATION": "NOT_READ",
        "TRACKING": "NOT_READ",
        "NATIVE_CONVERSION": "NOT_READ",
        "BROWSER_MEASUREMENT": "NOT_APPLICABLE",
        "POLICY": "NOT_READ",
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
    "STRUCTURE": "Account, campaign, ad group, and ad identity, parents, and declared status are read by the fixed v3 inventory.",
    "DELIVERY": "Impressions and clicks come from one fixed report response; the reviewed material documents no report continuation, so it is never labeled complete.",
    "SPEND": "The retained native spend micros are evaluated; budget, spend-cap and run-schedule fields are not retained, so pacing is not evaluated.",
    "BIDS": "Bid strategy and bid value are not retained by the fixed v3 read.",
    "OBJECTIVE": "The campaign objective is retained, but this build pins no reviewed objective enumeration to classify it against.",
    "TARGETING": "Ad group targeting criteria are not retained by the fixed v3 read.",
    "PLACEMENT": "Placement configuration and placement report facets are not retained by the fixed v3 read.",
    "CREATIVE": "Ad identity, declared status, and hierarchy association are read; ad content and review state are not.",
    "DESTINATION_CONFIGURATION": "Click URLs and campaign-tagging fields are not retained by the fixed v3 read.",
    "TRACKING": "Conversion pixel and tracking configuration are not retained by the fixed v3 read.",
    "NATIVE_CONVERSION": "The fixed report requests impressions, clicks, and spend only; no conversion field is requested.",
    "BROWSER_MEASUREMENT": "A local audit executes no browser and loads no page.",
    "POLICY": "Ad review and rejection states and the account status are not retained by the fixed v3 read.",
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
            if dimension in {"DELIVERY", "SPEND"}:
                sources.append("PERFORMANCE_OBSERVATION")
                refs = (context.source_record_digest,)
                count += 1
            if dimension in {"STRUCTURE", "OBJECTIVE", "DRIFT"} and inventory is not None:
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

    def _observe(
        rule_ids: set[str],
        *,
        hypothesis: str,
        expected_impact: str,
        assumptions: tuple[str, ...],
        confidence: Confidence,
        completeness: AnalyticsCompleteness,
    ) -> None:
        matched = _ids(findings, rule_ids)
        if not matched:
            return
        bound = _bound(
            matched,
            rule_ids,
            scope="RECOMMENDATION_FINDING_IDS",
            # The first rule identifier in content order names the exact row
            # inside a fixed-size subject; the row's own `rule_ids` carries the
            # complete membership.
            subject=f"OBSERVE:{min(rule_ids)}",
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
                    risk="LOW",
                    expires_at=expires,
                    identity={
                        "rule_table_version": RULE_TABLE_VERSION,
                        "finding_ids": bound,
                        "operation_kind": "OBSERVE",
                    },
                ),
                finding_ids=bound,
                bound_finding_count=len(matched),
                bound_rule_ids=tuple(sorted(rule_ids)),
                operation_kind="OBSERVE",
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
                    # The reason alone repeats across declined groups, and the
                    # considered operation alone can repeat as well. The first
                    # rule of the row's membership identifies the exact row.
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

    _observe(
        {"RD_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"},
        hypothesis="A repeated bounded campaign read may show delivery this single-response report did not return.",
        expected_impact="A second compatible read establishes whether the absence of rows reflects real non-delivery.",
        assumptions=(
            "The absence of a row in a report with no documented continuation is not evidence of zero delivery.",
        ),
        confidence="LOW",
        completeness=_BOUNDED,
    )
    _observe(
        {"RD_SPEND_WITHOUT_IMPRESSIONS", "RD_DELIVERY_CLICKS_EXCEED_IMPRESSIONS"},
        hypothesis="A repeated bounded read over the same declared window may resolve this inconsistent retained row.",
        expected_impact="A second compatible read establishes whether the inconsistency persists before any decision uses the row.",
        assumptions=(
            "A single inconsistent aggregate row is not evidence of a delivery or billing defect.",
        ),
        confidence="MEDIUM",
        completeness=_BOUNDED,
    )
    _observe(
        {
            "RD_DRIFT_STATE_CHANGED",
            "RD_DRIFT_ABSENT_FROM_LATER_INVENTORY",
            "RD_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
        },
        hypothesis="A fresh bounded inventory read establishes the current state of the resources that changed out of band.",
        expected_impact="A current snapshot bounds the observation interval before any later work relies on the old state.",
        assumptions=(
            "The actor and the exact time of an out-of-band change are unknown and are not inferred.",
        ),
        confidence="HIGH",
        completeness=_NOT_ANALYTICS,
    )
    _no_recommendation(
        {"RD_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS", "RD_SPEND_WITHOUT_IMPRESSIONS"},
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="Pausing a Reddit campaign would be the natural follow-up, but this build compiles no Reddit pause or update operation, so no such recommendation is made.",
        considered="PAUSE",
    )
    _no_recommendation(
        {"RD_STRUCTURE_SERVING_INTENT_MISMATCH"},
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="Reconciling a parent and child status would require a Reddit status update, which this build cannot compile.",
        considered="UPDATE",
    )
    _no_recommendation(
        {"RD_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP", "RD_STRUCTURE_AD_GROUP_WITHOUT_AD"},
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="The offline Reddit compiler produces only a complete new paused campaign, ad group, and ad candidate; it cannot compile a child into an existing retained parent.",
        considered="CREATE",
    )
    _no_recommendation(
        {
            "RD_STRUCTURE_STATUS_NOT_REVIEWED",
            "RD_STRUCTURE_STATUS_NOT_RETURNED",
            "RD_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED",
            "RD_STRUCTURE_AD_PARENT_NOT_RETAINED",
            "RD_STRUCTURE_AD_CAMPAIGN_MISMATCH",
            "RD_OBJECTIVE_ENUM_NOT_REVIEWED",
            "RD_OBJECTIVE_NOT_RETURNED",
            "RD_CREATIVE_LOCAL_BINDING_UNVERIFIED",
            "RD_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
        },
        reason="EVIDENCE_INSUFFICIENT",
        detail="These findings record missing, unreviewed, or unverified evidence. Resolving them needs a documentation review or a further read, not an advertising operation.",
    )
    _no_recommendation(
        {
            "RD_SPEND_PACING_INPUTS_NOT_READ",
            "RD_BIDS_NOT_READ",
            "RD_TARGETING_NOT_READ",
            "RD_PLACEMENT_NOT_READ",
            "RD_CREATIVE_CONTENT_NOT_READ",
            "RD_DESTINATION_NOT_READ",
            "RD_TRACKING_NOT_READ",
            "RD_NATIVE_CONVERSION_NOT_READ",
            "RD_BROWSER_NOT_APPLICABLE",
            "RD_POLICY_REVIEW_STATE_NOT_READ",
        },
        reason="COVERAGE_NOT_READ",
        detail="These dimensions are not read, or not applicable, in this build's fixed Reddit read. An unread dimension yields no recommendation and is never reported as healthy.",
    )
    return recommendations, absences, exceeded


def evaluate(context: ProviderAuditContext) -> ProviderAuditSections:
    """Evaluate the Reddit rule table over retained records only."""
    if context.provider != PROVIDER:
        raise ValueError("The Reddit rule table evaluates Reddit Ads evidence only")
    window = _window(context.report)
    inventory = context.evidence.inventory
    grouped = _by_type(inventory)
    statuses = _coverage_map(context, grouped)
    findings: list[ProviderAuditFinding] = []
    if inventory is not None and inventory.records:
        findings.extend(_structure_findings(grouped, inventory, window, statuses["STRUCTURE"]))
        findings.extend(_objective_findings(grouped, window, statuses["OBJECTIVE"]))
        findings.extend(_creative_findings(context, grouped, window, statuses["CREATIVE"]))
        findings.extend(_account_scoped_findings(grouped, window, statuses))
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
        # The retained report is one provider response with no documented
        # continuation, so it is always bounded here, exactly as the per-rule
        # labels are. The section and its findings must never disagree.
        analytics_completeness=_BOUNDED,
        coverage=_coverage(context, statuses, grouped),
        findings=tuple(findings),
        recommendations=tuple(recommendations),
        no_recommendations=tuple(absences),
        drift_observations=tuple(observations),
        bound_exceeded=tuple(exceeded),
    )


register_provider_rule(PROVIDER, evaluate)
