# ruff: noqa: E501
"""Deterministic LinkedIn Ads audit rules over retained local records.

This module makes **no provider request**.  It evaluates the retained
`PERFORMANCE_OBSERVATION` report the caller selected, the retained
`RESOURCE_SNAPSHOT` set a completed inventory read appended, an optional
earlier snapshot set, and an optional local creative catalog.  Audit-time
quota, 401/403, and 429 handling therefore stays entirely with the read
adapters that produced those records; nothing here can leak a response body
because no response is available here.

Two honesty bounds shape every rule:

* The reviewed LinkedIn Ad Analytics finder has **no documented pagination**,
  so a retained report is never exhaustive.  Every finding derived from it is
  `BOUNDED_NON_EXHAUSTIVE`, states that absence is not zero, and never claims a
  total.
* The reviewed fixed read returns identity, hierarchy, status, and the campaign
  objective only.  Targeting criteria, bids, budgets, run schedules,
  placements, creative content, creative review state, landing pages, UTM
  values, and conversion tracking are not requested by this build, so their
  dimensions are `NOT_READ` rather than healthy.

`docs/implementation/linkedin-audit-recommendations.md` carries the rule table,
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
from adhelm.providers.linkedin_ads import (
    LINKEDIN_ADS_READ_SURFACES,
    LINKEDIN_ADS_REVIEWED_CAMPAIGN_GROUP_STATUSES,
    LINKEDIN_ADS_REVIEWED_CAMPAIGN_STATUSES,
    LINKEDIN_ADS_REVIEWED_CREATIVE_STATUSES,
    LINKEDIN_ADS_SERVING_INTENT_STATUS,
)
from adhelm.reporting import NormalizedReport, PerformanceRow

PROVIDER: Final[Provider] = "linkedin_ads"
# Bump this when a rule's semantics change.  It is part of every finding's
# content identity, so an audit produced by a different rule table can never be
# mistaken for one produced by this table.
RULE_TABLE_VERSION: Final = "linkedin_ads.rules.2026-09-08"
_BOUNDED: Final[AnalyticsCompleteness] = "BOUNDED_NON_EXHAUSTIVE"
_NOT_ANALYTICS: Final[AnalyticsCompleteness] = "NOT_APPLICABLE"
_REVIEWED_STATUSES: Final[dict[str, tuple[str, ...]]] = {
    "CAMPAIGN_GROUP": LINKEDIN_ADS_REVIEWED_CAMPAIGN_GROUP_STATUSES,
    "CAMPAIGN": LINKEDIN_ADS_REVIEWED_CAMPAIGN_STATUSES,
    "AD": LINKEDIN_ADS_REVIEWED_CREATIVE_STATUSES,
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
                rule_id="LI_STRUCTURE_GROUP_WITHOUT_CAMPAIGN",
                finding_type="LINKEDIN_CAMPAIGN_GROUP_WITHOUT_CAMPAIGN",
                dimension="STRUCTURE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained LinkedIn campaign group has no retained child campaign in the same inventory read.",
                suggested_action="Review the group in the LinkedIn interface, or read the bounded inventory again, before treating it as unused.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_STRUCTURE_CAMPAIGN_WITHOUT_CREATIVE",
                finding_type="LINKEDIN_CAMPAIGN_WITHOUT_CREATIVE",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained LinkedIn campaign has no retained creative in the same inventory read.",
                suggested_action="Confirm the campaign's creative association in the LinkedIn interface; a campaign with no creative cannot serve.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_STRUCTURE_CREATIVE_PARENT_NOT_RETAINED",
                finding_type="LINKEDIN_CREATIVE_PARENT_CAMPAIGN_NOT_RETAINED",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained LinkedIn creative names a parent campaign that is absent from the same retained inventory.",
                suggested_action="Read the bounded inventory again; a creative whose parent campaign is missing cannot be evaluated in context.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_STRUCTURE_CAMPAIGN_PARENT_NOT_RETAINED",
                finding_type="LINKEDIN_CAMPAIGN_PARENT_GROUP_NOT_RETAINED",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained LinkedIn campaign names a parent campaign group that is absent from the same retained inventory.",
                suggested_action="Read the bounded inventory again; a campaign whose parent group is missing cannot be evaluated in context.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_STRUCTURE_SERVING_INTENT_MISMATCH",
                finding_type="LINKEDIN_SERVING_INTENT_MISMATCH_WITH_PARENT",
                dimension="STRUCTURE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained LinkedIn resource declares the ACTIVE status while its retained parent does not, so its declared intent cannot be met.",
                suggested_action="Reconcile the parent and child statuses in the LinkedIn interface; this audit reads declared status only, not serving state.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_STRUCTURE_STATUS_NOT_REVIEWED",
                finding_type="LINKEDIN_STATUS_VALUE_NOT_IN_REVIEWED_ENUM",
                dimension="STRUCTURE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained LinkedIn resource carries a status outside the reviewed status enumeration this connector compiles into its search criteria.",
                suggested_action="Recheck the LinkedIn status documentation for this resource type before interpreting the value; the audit does not guess its meaning.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS",
                finding_type="LINKEDIN_ACTIVE_CAMPAIGN_WITHOUT_RETAINED_IMPRESSIONS",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="LOW",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained LinkedIn campaign declares the ACTIVE status and the retained bounded report contains no impressions for it. The report is not exhaustive, so this is not proof of zero delivery.",
                suggested_action="Read the same bounded campaign report again over a compatible window before treating the absence of rows as zero delivery.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
                finding_type="LINKEDIN_CLICKS_EXCEED_IMPRESSIONS",
                dimension="DELIVERY",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This retained LinkedIn report row declares more clicks than impressions at its declared grain, which no consistent delivery aggregate produces.",
                suggested_action="Read the same bounded report again and compare the declared grain and window before using this row for any decision.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_DELIVERY_METRICS_MISSING",
                finding_type="LINKEDIN_DELIVERY_METRICS_MISSING_ON_ROW",
                dimension="DELIVERY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="This retained LinkedIn report row declares no impression or click count, so no delivery rate can be derived from it.",
                suggested_action="Read the same declared grain again with the fixed impression and click fields before interpreting delivery.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_SPEND_WITHOUT_IMPRESSIONS",
                finding_type="LINKEDIN_SPEND_WITHOUT_IMPRESSIONS",
                dimension="SPEND",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_BOUNDED,
                observation="This retained LinkedIn report row declares spend with zero impressions at its declared grain. The retained native decimal amount, not a rounded cent projection, was compared.",
                suggested_action="Read the same bounded report again and reconcile the row in the LinkedIn interface before drawing a spend conclusion.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_SPEND_PACING_INPUTS_NOT_READ",
                finding_type="LINKEDIN_BUDGET_AND_SCHEDULE_NOT_READ",
                dimension="SPEND",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Budget and run-schedule fields are not requested by this build's fixed LinkedIn read, so budget pacing against a schedule is not evaluated for this account.",
                suggested_action="Treat pacing as unevaluated; do not infer a pacing state from retained delivery aggregates alone.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_BIDS_NOT_READ",
                finding_type="LINKEDIN_BID_CONFIGURATION_NOT_READ",
                dimension="BIDS",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Bid amount and bid strategy are not requested by this build's fixed LinkedIn read, so no bid configuration is evaluated for this account.",
                suggested_action="Review bid configuration in the LinkedIn interface; this audit makes no bid claim.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_OBJECTIVE_NOT_RETURNED",
                finding_type="LINKEDIN_CAMPAIGN_OBJECTIVE_NOT_RETURNED",
                dimension="OBJECTIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained LinkedIn campaign snapshot carries no objective value, so its optimization intent is unknown to this audit.",
                suggested_action="Read the bounded campaign inventory again; an absent objective is not a default objective.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_OBJECTIVE_ENUM_NOT_REVIEWED",
                finding_type="LINKEDIN_CAMPAIGN_OBJECTIVE_ENUM_NOT_REVIEWED",
                dimension="OBJECTIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This build pins no reviewed LinkedIn campaign objective enumeration, so the retained objective value is surfaced without being classified as supported or unsupported.",
                suggested_action="Review the retained objective value against current LinkedIn documentation before relying on it in an optimization decision.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_TARGETING_NOT_READ",
                finding_type="LINKEDIN_TARGETING_CRITERIA_NOT_READ",
                dimension="TARGETING",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Campaign targeting criteria are not requested by this build's fixed LinkedIn read, so no targeting claim is available for this account.",
                suggested_action="Review targeting in the LinkedIn interface; this audit neither reads nor infers audience criteria.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_PLACEMENT_NOT_READ",
                finding_type="LINKEDIN_PLACEMENT_NOT_READ",
                dimension="PLACEMENT",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Placement configuration and placement report facets are not requested by this build's fixed LinkedIn read, so no placement claim is available.",
                suggested_action="Review placement settings in the LinkedIn interface; this audit makes no placement claim.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_CREATIVE_CONTENT_NOT_READ",
                finding_type="LINKEDIN_CREATIVE_CONTENT_NOT_READ",
                dimension="CREATIVE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Creative content fields are not requested by this build's fixed LinkedIn read. Only creative identity, declared intended status, and campaign association are retained.",
                suggested_action="Review creative copy, assets, and calls to action in the LinkedIn interface; this audit makes no creative-content claim.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_CREATIVE_STATUS_NOT_RETURNED",
                finding_type="LINKEDIN_CREATIVE_STATUS_NOT_RETURNED",
                dimension="CREATIVE",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This retained LinkedIn creative snapshot carries no declared intended status, so neither a serving nor a paused state can be attributed to it.",
                suggested_action="Read the bounded creative inventory again; an absent status is not an active creative.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_CREATIVE_LOCAL_BINDING_UNVERIFIED",
                finding_type="LINKEDIN_LOCAL_CREATIVE_BINDING_UNVERIFIED",
                dimension="CREATIVE",
                severity="LOW",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="A local creative version declares a LinkedIn creative reference, but the retained inventory carries no provider-verified creative association for it.",
                suggested_action="Treat the local binding as a declaration only; an independently reviewed provider association is still required.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
                finding_type="LINKEDIN_LOCAL_CREATIVE_BINDING_WITHOUT_RETAINED_RESOURCE",
                dimension="CREATIVE",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="A local creative version declares a LinkedIn creative reference that is absent from the retained inventory for this account.",
                suggested_action="Read the bounded creative inventory again and reconcile the local declaration; do not assume the provider resource exists.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_DESTINATION_NOT_READ",
                finding_type="LINKEDIN_DESTINATION_AND_UTM_NOT_READ",
                dimension="DESTINATION_CONFIGURATION",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Landing page and campaign-tagging fields are not requested by this build's fixed LinkedIn read, so no destination or UTM claim is available.",
                suggested_action="Review destination URLs and tagging in the LinkedIn interface; this audit reads no URL or query value.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_TRACKING_NOT_READ",
                finding_type="LINKEDIN_TRACKING_CONFIGURATION_NOT_READ",
                dimension="TRACKING",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Conversion tracking configuration is not requested by this build's fixed LinkedIn read, so no tracking health claim is available for this account.",
                suggested_action="Verify tracking configuration through its own reviewed surface; retained delivery aggregates cannot establish it.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_NATIVE_CONVERSION_NOT_READ",
                finding_type="LINKEDIN_NATIVE_CONVERSION_NOT_READ",
                dimension="NATIVE_CONVERSION",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The fixed LinkedIn report requests impressions, clicks, and cost only, so no native conversion count, action, or attribution context is available for this account.",
                suggested_action="Do not infer a conversion count from clicks or sessions; a native conversion read is required and is not implemented here.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_BROWSER_NOT_APPLICABLE",
                finding_type="LINKEDIN_BROWSER_MEASUREMENT_NOT_APPLICABLE_LOCAL_AUDIT",
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
                rule_id="LI_POLICY_REVIEW_STATE_NOT_READ",
                finding_type="LINKEDIN_POLICY_REVIEW_STATE_NOT_READ",
                dimension="POLICY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Creative review and rejection states, and account serving statuses, are not requested by this build's fixed LinkedIn read, so no policy health claim is available.",
                suggested_action="Review policy and creative review state in the LinkedIn interface; an absent rejection is not an approval.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_DRIFT_STATE_CHANGED",
                finding_type="LINKEDIN_OUT_OF_BAND_STATE_CHANGE_OBSERVED",
                dimension="DRIFT",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Two retained snapshots of this LinkedIn resource differ, and this product applied no change between them. The actor is UNKNOWN and the change occurred somewhere inside the recorded observation interval.",
                suggested_action="Reconcile the change against the LinkedIn interface before planning anything; no exact external change time or actor is available.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_DRIFT_ABSENT_FROM_LATER_INVENTORY",
                finding_type="LINKEDIN_RESOURCE_ABSENT_FROM_LATER_INVENTORY",
                dimension="DRIFT",
                severity="MEDIUM",
                confidence="MEDIUM",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This LinkedIn resource is present in the earlier retained inventory and absent from the later one. Absence is not proof of deletion; the actor is UNKNOWN.",
                suggested_action="Read the bounded inventory again and confirm the resource in the LinkedIn interface before treating it as removed.",
            )
        ),
        _rule(
            _Rule(
                rule_id="LI_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
                finding_type="LINKEDIN_RESOURCE_ABSENT_FROM_EARLIER_INVENTORY",
                dimension="DRIFT",
                severity="LOW",
                confidence="MEDIUM",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This LinkedIn resource is present in the later retained inventory and absent from the earlier one. This product created nothing; the actor is UNKNOWN.",
                suggested_action="Confirm who created the resource in the LinkedIn interface; no exact external creation time or actor is available.",
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


def _is_serving_intent(status: str) -> bool:
    return status == LINKEDIN_ADS_SERVING_INTENT_STATUS


def _native_spend(row: PerformanceRow) -> Decimal | None:
    value = row.provider_extensions.get("linkedin_ads_cost_in_local_currency")
    return Decimal(value) if isinstance(value, str) else None


def _spend_is_positive(row: PerformanceRow) -> bool:
    """Compare the retained native decimal, never a rounded cent projection."""
    native = _native_spend(row)
    if native is not None:
        return native > 0
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
    parents: dict[str, list[str]] = {}
    digests = {record.snapshot.resource_ref: record.record_digest for record in inventory.records}
    statuses = {
        record.snapshot.resource_ref: record.snapshot.effective_status
        for record in inventory.records
    }
    for record in inventory.records:
        for parent in record.snapshot.parent_resource_refs:
            parents.setdefault(parent, []).append(record.snapshot.resource_ref)
    for record in grouped["CAMPAIGN_GROUP"]:
        if not any(
            child in {item.snapshot.resource_ref for item in grouped["CAMPAIGN"]}
            for child in parents.get(record.snapshot.resource_ref, ())
        ):
            findings.append(
                _emit(
                    RULES["LI_STRUCTURE_GROUP_WITHOUT_CAMPAIGN"],
                    resource_refs=(record.snapshot.resource_ref,),
                    evidence_refs=(record.record_digest,),
                    window=window,
                    coverage_status=status,
                    confidence=absence_confidence,
                )
            )
    for record in grouped["CAMPAIGN"]:
        children = set(parents.get(record.snapshot.resource_ref, ()))
        if not any(item.snapshot.resource_ref in children for item in grouped["AD"]):
            findings.append(
                _emit(
                    RULES["LI_STRUCTURE_CAMPAIGN_WITHOUT_CREATIVE"],
                    resource_refs=(record.snapshot.resource_ref,),
                    evidence_refs=(record.record_digest,),
                    window=window,
                    coverage_status=status,
                    confidence=absence_confidence,
                )
            )
    orphan_rules = {
        "CAMPAIGN": ("LI_STRUCTURE_CAMPAIGN_PARENT_NOT_RETAINED", "CAMPAIGN_GROUP"),
        "AD": ("LI_STRUCTURE_CREATIVE_PARENT_NOT_RETAINED", "CAMPAIGN"),
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
    for resource_type in ("CAMPAIGN_GROUP", "CAMPAIGN", "AD"):
        for record in grouped[resource_type]:
            if not _is_serving_intent(record.snapshot.effective_status):
                continue
            for parent in record.snapshot.parent_resource_refs:
                parent_status = statuses.get(parent)
                if parent_status is None or _is_serving_intent(parent_status):
                    continue
                findings.append(
                    _emit(
                        RULES["LI_STRUCTURE_SERVING_INTENT_MISMATCH"],
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
            if record.snapshot.effective_status not in reviewed:
                findings.append(
                    _emit(
                        RULES["LI_STRUCTURE_STATUS_NOT_REVIEWED"],
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
            findings.append(
                _emit(
                    RULES["LI_DELIVERY_METRICS_MISSING"],
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
                    RULES["LI_DELIVERY_CLICKS_EXCEED_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
        if row.impressions == 0 and _spend_is_positive(row):
            findings.append(
                _emit(
                    RULES["LI_SPEND_WITHOUT_IMPRESSIONS"],
                    resource_refs=(row.resource_ref,),
                    evidence_refs=(report_digest,),
                    window=window,
                    coverage_status="PARTIAL",
                )
            )
    # An AD-grain report carries creative-pivot rows that this build cannot join
    # to a campaign, so campaign delivery is only evaluated from campaign rows.
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
                    RULES["LI_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"],
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
        ("LI_SPEND_PACING_INPUTS_NOT_READ", True),
        ("LI_BIDS_NOT_READ", True),
        ("LI_TARGETING_NOT_READ", bool(grouped["CAMPAIGN"])),
        ("LI_PLACEMENT_NOT_READ", True),
        ("LI_CREATIVE_CONTENT_NOT_READ", bool(grouped["AD"])),
        ("LI_DESTINATION_NOT_READ", True),
        ("LI_TRACKING_NOT_READ", True),
        ("LI_NATIVE_CONVERSION_NOT_READ", True),
        ("LI_BROWSER_NOT_APPLICABLE", True),
        ("LI_POLICY_REVIEW_STATE_NOT_READ", True),
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
            "LI_OBJECTIVE_ENUM_NOT_REVIEWED"
            if record.snapshot.normalized_state.get("objective") is not None
            else "LI_OBJECTIVE_NOT_RETURNED"
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
    findings: list[ProviderAuditFinding] = []
    for record in grouped["AD"]:
        if record.snapshot.normalized_state.get("status") is None:
            findings.append(
                _emit(
                    RULES["LI_CREATIVE_STATUS_NOT_RETURNED"],
                    resource_refs=(record.snapshot.resource_ref,),
                    evidence_refs=(record.record_digest,),
                    window=window,
                    coverage_status=status,
                )
            )
    catalog = context.evidence.creative_catalog
    if catalog is None:
        return findings
    retained = {record.snapshot.resource_ref: record for record in grouped["AD"]}
    for entry in sorted(catalog.entries, key=lambda item: item.creative_version_id):
        if entry.provider != PROVIDER or entry.provider_resource_ref is None:
            continue
        bound = retained.get(entry.provider_resource_ref)
        if bound is None:
            findings.append(
                _emit(
                    RULES["LI_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE"],
                    resource_refs=(entry.creative_version_id, entry.provider_resource_ref),
                    evidence_refs=(entry.record_digest,),
                    window=window,
                    coverage_status=status,
                )
            )
            continue
        findings.append(
            _emit(
                RULES["LI_CREATIVE_LOCAL_BINDING_UNVERIFIED"],
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
            rule = RULES["LI_DRIFT_STATE_CHANGED"]
            finding = _emit(
                rule,
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
        if first is not None:
            if not absence_is_evidence:
                continue
            rule = RULES["LI_DRIFT_ABSENT_FROM_LATER_INVENTORY"]
            finding = _emit(
                rule,
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
        if not absence_is_evidence:
            continue
        rule = RULES["LI_DRIFT_ABSENT_FROM_EARLIER_INVENTORY"]
        finding = _emit(
            rule,
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
    """How many local creative bindings declare a LinkedIn resource."""
    catalog = context.evidence.creative_catalog
    if catalog is None:
        return 0
    return sum(
        1
        for entry in catalog.entries
        if entry.provider == PROVIDER and entry.provider_resource_ref is not None
    )


def _coverage_map(
    context: ProviderAuditContext, grouped: dict[str, list[RetainedSnapshotRecord]]
) -> dict[AuditDimension, ProviderCoverageStatus]:
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
    return {
        "STRUCTURE": "COVERED" if complete else "PARTIAL" if has_inventory else "NOT_READ",
        # Never COVERED: the reviewed Ad Analytics finder documents no
        # pagination, so no retained report proves it returned every row.
        "DELIVERY": "PARTIAL" if has_report_rows else "NOT_READ",
        "SPEND": "PARTIAL" if has_report_rows else "NOT_READ",
        "BIDS": "NOT_READ",
        "OBJECTIVE": "PARTIAL" if grouped["CAMPAIGN"] else "NOT_READ",
        "TARGETING": "NOT_READ",
        "PLACEMENT": "NOT_READ",
        # Creative evidence is either a retained creative snapshot or a local
        # binding that names one; both produce findings, so both are coverage.
        "CREATIVE": ("PARTIAL" if grouped["AD"] or _local_bindings(context) else "NOT_READ"),
        "DESTINATION_CONFIGURATION": "NOT_READ",
        "TRACKING": "NOT_READ",
        "NATIVE_CONVERSION": "NOT_READ",
        "BROWSER_MEASUREMENT": "NOT_APPLICABLE",
        "POLICY": "NOT_READ",
        "DRIFT": (
            "COVERED" if drift_is_covered else "PARTIAL" if earlier is not None else "NOT_READ"
        ),
    }


_RATIONALE: Final[dict[AuditDimension, str]] = {
    "STRUCTURE": "Account, campaign-group, campaign, and creative identity, parents, and declared status are read by the fixed 202608 inventory.",
    "DELIVERY": "Impressions and clicks come from the fixed Ad Analytics finder, which documents no pagination and is never labeled complete.",
    "SPEND": "The retained native cost decimal is evaluated; budget and run-schedule fields are not requested, so pacing is not evaluated.",
    "BIDS": "Bid amount and bid strategy are not requested by the fixed 202608 read.",
    "OBJECTIVE": "The campaign objective is retained, but this build pins no reviewed objective enumeration to classify it against.",
    "TARGETING": "Campaign targeting criteria are not requested by the fixed 202608 read.",
    "PLACEMENT": "Placement configuration and placement report facets are not requested by the fixed 202608 read.",
    "CREATIVE": "Creative identity, declared intended status, and campaign association are read; creative content and review state are not.",
    "DESTINATION_CONFIGURATION": "Landing page and campaign-tagging fields are not requested by the fixed 202608 read.",
    "TRACKING": "Conversion tracking configuration is not requested by the fixed 202608 read.",
    "NATIVE_CONVERSION": "The fixed report requests impressions, clicks, and cost only; no conversion field is requested.",
    "BROWSER_MEASUREMENT": "A local audit executes no browser and loads no page.",
    "POLICY": "Creative review and rejection states and account serving statuses are not requested by the fixed 202608 read.",
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
            subject="OBSERVE:" + ",".join(sorted(rule_ids)),
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
                    # The reason alone repeats across declined groups; naming the
                    # considered operation identifies the exact declined row.
                    subject=f"{reason}:{considered or 'NONE'}",
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
        {"LI_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"},
        hypothesis="A repeated bounded campaign read may show delivery this non-exhaustive report did not return.",
        expected_impact="A second compatible read establishes whether the absence of rows reflects real non-delivery.",
        assumptions=(
            "The absence of a row in a report with no documented pagination is not evidence of zero delivery.",
        ),
        confidence="LOW",
        completeness=_BOUNDED,
    )
    _observe(
        {"LI_SPEND_WITHOUT_IMPRESSIONS", "LI_DELIVERY_CLICKS_EXCEED_IMPRESSIONS"},
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
            "LI_DRIFT_STATE_CHANGED",
            "LI_DRIFT_ABSENT_FROM_LATER_INVENTORY",
            "LI_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
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
        {"LI_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS", "LI_SPEND_WITHOUT_IMPRESSIONS"},
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="Pausing a LinkedIn campaign would be the natural follow-up, but this build compiles no LinkedIn pause or update operation, so no such recommendation is made.",
        considered="PAUSE",
    )
    _no_recommendation(
        {"LI_STRUCTURE_SERVING_INTENT_MISMATCH"},
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="Reconciling a parent and child status would require a LinkedIn status update, which this build cannot compile.",
        considered="UPDATE",
    )
    _no_recommendation(
        {"LI_STRUCTURE_CAMPAIGN_WITHOUT_CREATIVE"},
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="Associating a creative with an existing LinkedIn campaign is not a compilable operation in this build; the offline compiler only produces a complete new candidate.",
        considered="ASSOCIATE",
    )
    _no_recommendation(
        {"LI_STRUCTURE_GROUP_WITHOUT_CAMPAIGN"},
        reason="OPERATION_NOT_COMPILABLE_FOR_PROVIDER",
        detail="The offline LinkedIn compiler produces only a complete new campaign-group, campaign, and creative candidate; it cannot compile a campaign into an existing retained group.",
        considered="CREATE",
    )
    _no_recommendation(
        {
            "LI_STRUCTURE_STATUS_NOT_REVIEWED",
            "LI_OBJECTIVE_ENUM_NOT_REVIEWED",
            "LI_OBJECTIVE_NOT_RETURNED",
            "LI_CREATIVE_STATUS_NOT_RETURNED",
            "LI_CREATIVE_LOCAL_BINDING_UNVERIFIED",
            "LI_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
            "LI_STRUCTURE_CREATIVE_PARENT_NOT_RETAINED",
            "LI_STRUCTURE_CAMPAIGN_PARENT_NOT_RETAINED",
            "LI_DELIVERY_METRICS_MISSING",
        },
        reason="EVIDENCE_INSUFFICIENT",
        detail="These findings record missing, unreviewed, or unverified evidence. Resolving them needs a documentation review or a further read, not an advertising operation.",
    )
    _no_recommendation(
        {
            "LI_SPEND_PACING_INPUTS_NOT_READ",
            "LI_BIDS_NOT_READ",
            "LI_TARGETING_NOT_READ",
            "LI_PLACEMENT_NOT_READ",
            "LI_CREATIVE_CONTENT_NOT_READ",
            "LI_DESTINATION_NOT_READ",
            "LI_TRACKING_NOT_READ",
            "LI_NATIVE_CONVERSION_NOT_READ",
            "LI_BROWSER_NOT_APPLICABLE",
            "LI_POLICY_REVIEW_STATE_NOT_READ",
        },
        reason="COVERAGE_NOT_READ",
        detail="These dimensions are not read, or not applicable, in this build's fixed LinkedIn read. An unread dimension yields no recommendation and is never reported as healthy.",
    )
    return recommendations, absences, exceeded


def evaluate(context: ProviderAuditContext) -> ProviderAuditSections:
    """Evaluate the LinkedIn rule table over retained records only."""
    if context.provider != PROVIDER:
        raise ValueError("The LinkedIn rule table evaluates LinkedIn Ads evidence only")
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
        analytics_completeness=_BOUNDED
        if LINKEDIN_ADS_READ_SURFACES["ANALYTICS_PAGINATION"] == "NOT_DOCUMENTED"
        else _NOT_ANALYTICS,
        coverage=_coverage(context, statuses, grouped),
        findings=tuple(findings),
        recommendations=tuple(recommendations),
        no_recommendations=tuple(absences),
        drift_observations=tuple(observations),
        bound_exceeded=tuple(exceeded),
    )


register_provider_rule(PROVIDER, evaluate)
