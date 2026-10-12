# ruff: noqa: E501
"""Deterministic GA4 audit rules over retained local records.

This module makes **no provider request**.  It evaluates the retained GA4 Data
API `PERFORMANCE_OBSERVATION` report the caller selected and, when one is
retained, the GA4 Admin `MEASUREMENT_OBSERVATION` for the same property.  Audit-
time quota, 401/403, and 429 handling therefore stays with the read adapters
that produced those records; nothing here can leak a response body because no
response is available here.

Three honesty bounds shape every rule:

* A retained GA4 report is **never labelled exhaustive**.  Reconciled
  pagination is not proof that the returned rows are every matching row, and
  the Data API documents no snapshot isolation between pages.  Every finding
  derived from a report is `BOUNDED_NON_EXHAUSTIVE`, and a missing, withheld,
  restricted or never-returned value is reported as unmeasured, never as zero.
* The Data API returns **no freshness field and no attribution model**, and the
  Admin attribution-settings read is not performed.  Neither is guessed.
* A GA4 key event is **not a provider-native conversion** (AGENTS rule 16), so
  `NATIVE_CONVERSION` is `NOT_APPLICABLE` and no key-event count satisfies a
  provider conversion requirement.

The Admin inventory holds event names and rule content only as keyed
fingerprints, so a cross-read comparison with a report compares counts, never
names.  Drift between two retained Admin observations is compared by
`measurement compare`; the audit leaves `DRIFT` explicitly `NOT_READ`.

`docs/implementation/ga4-audit-recommendations.md` carries the gap list, rule
table, coverage map, and evidence limits in prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    register_provider_rule,
)
from adhelm.errors import AdhelmError
from adhelm.models import Provider
from adhelm.reporting import NormalizedReport, PerformanceRow, QualityStatus

PROVIDER: Final[Provider] = "ga4"
# Bump this when a rule's semantics change.  It is part of every finding's
# content identity, so an audit produced by a different rule table can never be
# mistaken for one produced by this table.
RULE_TABLE_VERSION: Final = "ga4.rules.2026-10-10.4"
_BOUNDED: Final[AnalyticsCompleteness] = "BOUNDED_NON_EXHAUSTIVE"
_NOT_ANALYTICS: Final[AnalyticsCompleteness] = "NOT_APPLICABLE"
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
# The report-level facts the fixed aggregate read persists as row extensions.
# Their presence is what marks a report as read through the aggregate contract.
_COMPLETENESS_EXTENSION: Final = "ga4_completeness"
_INCOMPLETE_COMPLETENESS: Final = frozenset(
    {"INCOMPLETE_PAGE_BOUND_EXCEEDED", "INCOMPLETE_ROW_COUNT_MISMATCH"}
)
_TRAFFIC_SHAPES: Final = frozenset(
    {"GA4_CAMPAIGN_SOURCE_MEDIUM", "GA4_CAMPAIGN_SOURCE_MEDIUM_KEY_EVENT"}
)
_INCONSISTENT_SAMPLING: Final = "SAMPLING_METADATA_INCONSISTENT"
_NOT_SET: Final = "(not set)"
# Values GA4 reports when a session has no campaign-style attribution at all.
_NO_ATTRIBUTION_VALUES: Final = frozenset({"(not set)", "(other)", "(direct)", "(none)"})
_REALTIME_ATTRIBUTION: Final = "GA4_REALTIME"


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
                rule_id="GA4_REPORT_THRESHOLDED",
                finding_type="GA4_REPORT_SUBJECT_TO_THRESHOLDING",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="GA4 declared this report subject to thresholding, so rows below its minimum aggregation threshold may be suppressed. A missing row is withheld or absent data, not a measured zero.",
                suggested_action="Treat every absent row as unmeasured, and do not sum, difference or rank this report as if it held every row.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_REPORT_SAMPLED",
                finding_type="GA4_REPORT_SAMPLED_ESTIMATES",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="GA4 reported sampling for this report, so its session, user and key-event counts are estimates drawn from a sample of the available data.",
                suggested_action="Treat these counts as estimates, and do not expect them to match the GA4 interface exactly.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_REPORT_OTHER_ROW_ROLLUP",
                finding_type="GA4_VALUES_ROLLED_INTO_OTHER_ROW",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="GA4 rolled less-common dimension values into its '(other)' row, so some campaigns, sources, mediums or events are not individually visible. Filters do not look inside that row.",
                suggested_action="Do not read the '(other)' row as one campaign or event, and do not treat the named rows as every value.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_REPORT_METRICS_RESTRICTED",
                finding_type="GA4_METRICS_RESTRICTED",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="GA4 reported active metric restrictions for this report, so a restricted metric carries no value. A restricted metric is unavailable, not zero.",
                suggested_action="Use only the metrics GA4 returned. Review the property's data restrictions in the GA4 interface if a restricted metric is needed.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_REPORT_SAMPLING_METADATA_INCONSISTENT",
                finding_type="GA4_SAMPLING_METADATA_DIFFERS_BETWEEN_PAGES",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="The pages of this report carried different sampling metadata, so the sample ratio is itself unreliable and the counts are not one consistent estimate. The Data API documents no snapshot isolation between pages.",
                suggested_action="Do not quote a sample ratio for this report, and read the same window again to see whether GA4 reports consistent sampling.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_REPORT_PROVIDER_FLAGGED_UNKNOWN",
                finding_type="GA4_PROVIDER_FLAGGED_UNRETAINED_ROLLUP_OR_RESTRICTION",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="MEDIUM",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="GA4 flagged a roll-up into its '(other)' row or an active metric restriction on this report, and this report does not retain which of them it was or which values it touched, because a standard or Realtime read keeps no roll-up or restriction fact. Some values may be rolled up or unavailable. A restricted or rolled-up value is not zero.",
                suggested_action="Treat this report's values as possibly incomplete, and read a fixed aggregate shape to learn whether a roll-up or a metric restriction applied.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_REPORT_PROVIDER_FLAGGED_UNATTRIBUTABLE",
                finding_type="GA4_PROVIDER_FLAGGED_UNATTRIBUTABLE_ROLLUP_OR_RESTRICTION",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="MEDIUM",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="GA4 flagged a roll-up into its '(other)' row or an active metric restriction on this report, and this read cannot say which of them it was: a report with no rows has no row to carry that fact, and an aggregate can carry the flag without naming a restricted metric. Some values may be rolled up or unavailable. A restricted or rolled-up value is not zero.",
                suggested_action="Treat this report's values as possibly incomplete. This read cannot attribute the flag, so do not infer that no roll-up or restriction applied from the absence of a row or a restriction fact.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_REPORT_INCOMPLETE",
                finding_type="GA4_REPORT_PAGE_WALK_INCOMPLETE",
                dimension="TRACKING",
                severity="HIGH",
                confidence="HIGH",
                risk="HIGH",
                analytics_completeness=_BOUNDED,
                observation="The page walk ended before every row GA4 declared was consumed, or the declared row count changed between pages. This report is not the full table.",
                suggested_action="Do not use this report for totals. Read it again with a larger page bound or a narrower window.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_REPORT_NOT_AVAILABLE_FROM_PROVIDER",
                finding_type="GA4_PROVIDER_DECLINED_TO_REPORT",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="GA4 declined to report for a reason it does not enumerate. An empty result of this kind is not evidence that no activity occurred.",
                suggested_action="Read the same window again later, and treat this window as unmeasured until a read returns data.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_REPORT_NO_ROWS",
                finding_type="GA4_REPORT_HOLDS_NO_ROWS",
                dimension="TRACKING",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="This report holds no rows. GA4 returned no data for the window, which is not a measured zero for any campaign or key event and may reflect thresholding, a processing delay or the requested filter.",
                suggested_action="Read the window again after the normal processing interval before concluding that nothing happened.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_KEY_EVENT_THRESHOLD_WITHHELD",
                finding_type="GA4_KEY_EVENT_COUNT_WITHHELD_BY_THRESHOLDING",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This declared key event has no row because GA4 declared the report subject to thresholding. Its count is withheld, not zero.",
                suggested_action="Treat this key event's count as unmeasured for the window.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_KEY_EVENT_NOT_RETURNED",
                finding_type="GA4_KEY_EVENT_HAS_NO_RETURNED_ROW",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="MEDIUM",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This declared key event has no row in the window and the report was not thresholded. No count was returned: the event may not have occurred, may still be processing, or may have been renamed. It is not a measured zero.",
                suggested_action="Read the window again after the normal processing interval, and confirm the event name against the property's key-event configuration.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_ROWS_JOIN_UNRESOLVED",
                finding_type="GA4_ROWS_NOT_JOINED_TO_PROVIDER_CAMPAIGNS",
                dimension="TRACKING",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="Campaign-level GA4 rows are not resolved to provider campaigns by a customer-declared join. They stay visible and are excluded from authoritative joined totals; no campaign identity is inferred from a name.",
                suggested_action="Declare a campaign join contract for these rows before relating GA4 traffic to a provider campaign.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_ROWS_JOIN_CONFLICT",
                finding_type="GA4_ROW_JOIN_CONFLICT",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="At least one GA4 row conflicts with the customer's declared campaign join, so two declarations claim it or one provider campaign is claimed by two GA4 rows. The conflicting rows are excluded from authoritative totals.",
                suggested_action="Resolve the conflict in the declared join contract, then read the report again.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_SESSIONS_WITHOUT_CAMPAIGN_NAME",
                finding_type="GA4_SOURCE_AND_MEDIUM_WITHOUT_CAMPAIGN_NAME",
                dimension="TRACKING",
                severity="LOW",
                confidence="MEDIUM",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="Some sessions carry a source and medium but no campaign name, so GA4 cannot attribute them to a named campaign. They may be untagged campaign traffic or traffic that needs no campaign name; this read cannot tell which.",
                suggested_action="Check the campaign-tagging parameters on the destination URLs for these source and medium pairs in the advertising provider. This build does not read destination URLs.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_WINDOW_INCLUDES_CURRENT_PROPERTY_DATE",
                finding_type="GA4_WINDOW_ENDS_ON_THE_OBSERVATION_DATE",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="The report window ends on the property-local date on which it was observed. GA4 documents no freshness guarantee and returns no freshness field, so the counts for that last day are provisional.",
                suggested_action="Read the window again after its last day has closed and GA4's normal processing interval has passed.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_REPORT_REALTIME_NOT_PROCESSED",
                finding_type="GA4_REALTIME_REPORT_IS_NOT_PROCESSED_DATA",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="This is a GA4 Realtime read. A Realtime observation is not processed standard-report data and is not evidence that an event is counted in processed reports.",
                suggested_action="Verify the event in a processed aggregate report after the normal processing interval.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_REPORT_COMPLETENESS_FACTS_NOT_RETAINED",
                finding_type="GA4_REPORT_COMPLETENESS_FACTS_NOT_RETAINED",
                dimension="TRACKING",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_BOUNDED,
                observation="This report was not read through the fixed aggregate contract, so its completeness, roll-up, restriction, sampling-consistency and per-key-event cell facts are not retained with it. Only its envelope quality statuses are evaluated, and a roll-up or restriction GA4 flagged is reported as one finding that cannot say which it was.",
                suggested_action="Read a fixed aggregate shape when audit-grade completeness facts are needed.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_NO_KEY_EVENTS_CONFIGURED",
                finding_type="GA4_PROPERTY_HAS_NO_KEY_EVENTS",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The retained Admin inventory lists no key events for this property, so GA4 counts none and key-event reporting has nothing to read.",
                suggested_action="Decide in the GA4 interface which events the business counts as key events. A GA4 key event is not an advertising-provider conversion.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_KEY_EVENT_COUNTING_METHOD_UNSPECIFIED",
                finding_type="GA4_KEY_EVENT_COUNTING_METHOD_UNSPECIFIED",
                dimension="TRACKING",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This key event's counting method is unspecified in the Admin read, so whether it counts once per event or once per session is not established by this evidence.",
                suggested_action="Confirm the counting method for this key event in the GA4 interface before comparing its counts.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_EVENT_RULES_CONTENT_NOT_INSPECTED",
                finding_type="GA4_EVENT_RULE_CONTENT_OPAQUE",
                dimension="TRACKING",
                severity="LOW",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The property has event create or edit rules. Their conditions and parameter mutations are held only as local fingerprints, so this audit cannot establish whether a rule creates, renames or alters an event that a key event depends on.",
                suggested_action="Review the property's event create and edit rules in the GA4 interface when a key event's counts look wrong.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_ADMIN_INVENTORY_STALE",
                finding_type="GA4_ADMIN_OBSERVATION_OLDER_THAN_DECLARED_MAXIMUM",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="HIGH",
                risk="MEDIUM",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The retained Admin inventory is older than the maximum observation age the customer declared, so the key-event configuration it describes may have changed.",
                suggested_action="Read the property's Admin inventory again before relying on its key-event configuration.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_KEY_EVENT_COUNT_DIFFERS_FROM_ADMIN",
                finding_type="GA4_DECLARED_KEY_EVENT_COUNT_DIFFERS_FROM_ADMIN_INVENTORY",
                dimension="TRACKING",
                severity="MEDIUM",
                confidence="LOW",
                risk="MEDIUM",
                analytics_completeness=_BOUNDED,
                observation="The number of key events the report declared differs from the number in the retained Admin inventory. The report may cover a subset or predate a configuration change. Only counts are compared, because the inventory holds event names as fingerprints.",
                suggested_action="Read the Admin inventory again, then read the key-event aggregate with the key events that inventory lists.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_ADMIN_INVENTORY_NOT_RETAINED",
                finding_type="GA4_ADMIN_INVENTORY_NOT_RETAINED",
                dimension="STRUCTURE",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="No GA4 Admin inventory is retained for this property, so key-event and event-rule configuration was not evaluated.",
                suggested_action="Read the property's Admin inventory with the measurement command, then audit again.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_DESTINATION_AND_LINKS_NOT_READ",
                finding_type="GA4_DESTINATION_AND_PRODUCT_LINKS_NOT_READ",
                dimension="DESTINATION_CONFIGURATION",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Data stream URLs, cross-domain settings, referral exclusions and product links such as Google Ads links are not read by the fixed GA4 reads.",
                suggested_action="Review these settings in the GA4 interface. They are not evaluated here and are not reported healthy.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_POLICY_SETTINGS_NOT_READ",
                finding_type="GA4_CONSENT_AND_RETENTION_SETTINGS_NOT_READ",
                dimension="POLICY",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="Consent, data collection, data retention and Google signals settings are not read by the fixed GA4 reads.",
                suggested_action="Review these settings in the GA4 interface. They are not evaluated here and are not reported healthy.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_ATTRIBUTION_SETTINGS_NOT_READ",
                finding_type="GA4_ATTRIBUTION_MODEL_NOT_READ",
                dimension="TRACKING",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="The Data API exposes no attribution model and the Admin attribution-settings read is not performed, so the property's attribution model and lookback window are unknown.",
                suggested_action="Do not relate GA4 key-event counts to a provider's conversions as if they shared an attribution model.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_DRIFT_NOT_EVALUATED_BY_AUDIT",
                finding_type="GA4_ADMIN_DRIFT_NOT_EVALUATED",
                dimension="DRIFT",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="This audit evaluates one retained Admin observation. Changes between two retained GA4 Admin observations are compared by the measurement compare command, not here.",
                suggested_action="Compare two retained Admin observations with the measurement compare command to see out-of-band configuration changes.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_KEY_EVENT_NOT_A_PROVIDER_CONVERSION",
                finding_type="GA4_KEY_EVENT_IS_NOT_A_PROVIDER_CONVERSION",
                dimension="NATIVE_CONVERSION",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="A GA4 key event is not an advertising provider's native conversion and is not automatically imported into one. No GA4 key-event count satisfies a provider conversion requirement.",
                suggested_action="Read the advertising provider's own conversion reporting for native conversion evidence.",
            )
        ),
        _rule(
            _Rule(
                rule_id="GA4_BROWSER_NOT_APPLICABLE",
                finding_type="GA4_BROWSER_MEASUREMENT_NOT_APPLICABLE_LOCAL_AUDIT",
                dimension="BROWSER_MEASUREMENT",
                severity="INFO",
                confidence="HIGH",
                risk="LOW",
                analytics_completeness=_NOT_ANALYTICS,
                observation="A local audit executes no browser and loads no page, so it neither proves that an event fires in a browser nor that GA4 received it.",
                suggested_action="Verify firing in a real browser, then in GA4 Realtime, then in a processed report. This audit does not replace those steps.",
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
) -> ProviderAuditFinding:
    finding = build_finding(
        finding_type=rule.finding_type,
        severity=rule.severity,
        provider=PROVIDER,
        resource_refs=resource_refs,
        observation=rule.observation,
        evidence_refs=evidence_refs,
        data_window=window,
        confidence=rule.confidence,
        risk=rule.risk,
        suggested_action=rule.suggested_action,
        identity={
            "rule_table_version": RULE_TABLE_VERSION,
            "rule_id": rule.rule_id,
            "resource_refs": resource_refs,
            "evidence_refs": evidence_refs,
            "coverage_status": coverage_status,
            "confidence": rule.confidence,
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


def _extension_true(report: NormalizedReport, key: str) -> bool:
    return any(row.provider_extensions.get(key) == "true" for row in report.performance_rows)


def _extension_values(report: NormalizedReport, key: str) -> set[str]:
    return {
        str(row.provider_extensions[key])
        for row in report.performance_rows
        if row.provider_extensions.get(key) is not None
    }


def _statuses(report: NormalizedReport) -> set[QualityStatus]:
    statuses = set(report.envelope.data_quality_statuses)
    for row in report.performance_rows:
        statuses.update(row.data_quality_statuses)
    return statuses


def _is_realtime(report: NormalizedReport) -> bool:
    return report.envelope.attribution_model == _REALTIME_ATTRIBUTION


def _has_completeness_facts(report: NormalizedReport) -> bool:
    return bool(_extension_values(report, _COMPLETENESS_EXTENSION))


def _traffic_rows(report: NormalizedReport, shape: str | None = None) -> list[PerformanceRow]:
    return [
        row
        for row in report.performance_rows
        if row.dimension_type in ({shape} if shape is not None else _TRAFFIC_SHAPES)
    ]


def _has_session_without_campaign(report: NormalizedReport) -> bool:
    """Whether a measured campaign-shape row has a source and medium but no campaign name."""
    for row in _traffic_rows(report, "GA4_CAMPAIGN_SOURCE_MEDIUM"):
        extensions = row.provider_extensions
        if (
            extensions.get("ga4_sessionCampaignName") == _NOT_SET
            and extensions.get("ga4_sessionSource") not in _NO_ATTRIBUTION_VALUES
            and extensions.get("ga4_sessionMedium") not in _NO_ATTRIBUTION_VALUES
            and row.sessions is not None
            and row.sessions > 0
        ):
            return True
    return False


def _window_includes_observation_date(report: NormalizedReport) -> bool:
    try:
        zone = ZoneInfo(report.envelope.source_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AdhelmError(
            "INVALID_AUDIT_INPUT",
            "The GA4 report timezone is not a valid IANA zone.",
            exit_code=2,
        ) from exc
    return report.envelope.window_end >= report.envelope.observed_at.astimezone(zone).date()


def _key_event_rows(report: NormalizedReport) -> list[tuple[str, str]]:
    """Declared key events with a persisted cell status, as (action ref, status)."""
    return sorted(
        (row.conversion_action_ref, str(row.provider_extensions["ga4_cell_status_keyEvents"]))
        for row in report.conversion_rows
        if row.conversion_source == "GA4"
        and row.conversion_kind == "KEY_EVENT"
        and row.conversion_action_ref is not None
        and "ga4_cell_status_keyEvents" in row.provider_extensions
    )


def _report_findings(
    context: ProviderAuditContext,
    window: AuditDataWindow,
    status: ProviderCoverageStatus,
) -> list[ProviderAuditFinding]:
    report = context.report
    evidence = (context.source_record_digest,)
    account = (report.envelope.source_account_ref,)
    findings: list[ProviderAuditFinding] = []

    def _fire(rule_id: str, refs: tuple[str, ...] = account) -> None:
        findings.append(
            _emit(
                RULES[rule_id],
                resource_refs=refs,
                evidence_refs=evidence,
                window=window,
                coverage_status=status,
            )
        )

    realtime = _is_realtime(report)
    aggregate = _has_completeness_facts(report)
    quality = _statuses(report)
    completeness = _extension_values(report, _COMPLETENESS_EXTENSION)
    if "THRESHOLDED" in quality or _extension_true(report, "ga4_subject_to_thresholding"):
        _fire("GA4_REPORT_THRESHOLDED")
    if "SAMPLED" in quality:
        _fire("GA4_REPORT_SAMPLED")
    sampling_inconsistent = _INCONSISTENT_SAMPLING in _extension_values(
        report, "ga4_sampling_consistency"
    )
    if sampling_inconsistent:
        _fire("GA4_REPORT_SAMPLING_METADATA_INCONSISTENT")
    rolled_up = _extension_true(report, "ga4_other_row_aggregation")
    if rolled_up:
        _fire("GA4_REPORT_OTHER_ROW_ROLLUP")
    restricted = bool(_extension_values(report, "ga4_restricted_metrics"))
    if restricted:
        _fire("GA4_REPORT_METRICS_RESTRICTED")
    # GA4 flagged something the persisted facts do not name.  The envelope status
    # is then the only trace.  A finer rule above that already explains the status
    # takes its place.
    if "PROVIDER_REPORTED_UNKNOWN" in report.envelope.data_quality_statuses and not (
        rolled_up or restricted or sampling_inconsistent
    ):
        # The variants are chosen by whether this report KIND keeps the facts, not
        # by whether it has rows.  A standard or Realtime read with rows keeps no
        # roll-up or restriction fact, and a fixed aggregate read would keep one,
        # so that advice is true only there.  Every other flagged report cannot be
        # attributed by repeating the read: a report with no rows has no row to
        # carry the fact, and an aggregate can carry the flag (an empty
        # `activeMetricRestrictions`, for one) without any fact naming the cause.
        _fire(
            "GA4_REPORT_PROVIDER_FLAGGED_UNKNOWN"
            if not aggregate and report.performance_rows
            else "GA4_REPORT_PROVIDER_FLAGGED_UNATTRIBUTABLE"
        )
    if completeness & _INCOMPLETE_COMPLETENESS or "PARTIAL_PAGE_FAILURE" in quality:
        _fire("GA4_REPORT_INCOMPLETE")
    if "NOT_AVAILABLE_FROM_PROVIDER" in quality:
        _fire("GA4_REPORT_NOT_AVAILABLE_FROM_PROVIDER")
    if not report.performance_rows:
        _fire("GA4_REPORT_NO_ROWS")
    for action_ref, cell_status in _key_event_rows(report):
        if cell_status == "THRESHOLD_WITHHELD":
            _fire("GA4_KEY_EVENT_THRESHOLD_WITHHELD", (action_ref,))
        elif cell_status == "NOT_RETURNED":
            _fire("GA4_KEY_EVENT_NOT_RETURNED", (action_ref,))
    traffic = _traffic_rows(report)
    if any(row.join_status in {"UNMAPPED", "HEURISTIC"} for row in traffic):
        _fire("GA4_ROWS_JOIN_UNRESOLVED")
    if any(row.join_status == "CONFLICT" for row in traffic):
        _fire("GA4_ROWS_JOIN_CONFLICT")
    if _has_session_without_campaign(report):
        _fire("GA4_SESSIONS_WITHOUT_CAMPAIGN_NAME")
    if realtime:
        _fire("GA4_REPORT_REALTIME_NOT_PROCESSED")
    elif _window_includes_observation_date(report):
        _fire("GA4_WINDOW_INCLUDES_CURRENT_PROPERTY_DATE")
    if not aggregate and report.performance_rows:
        _fire("GA4_REPORT_COMPLETENESS_FACTS_NOT_RETAINED")
    _fire("GA4_ATTRIBUTION_SETTINGS_NOT_READ")
    return findings


def _declared_key_event_count(report: NormalizedReport) -> int | None:
    """The key events a key-event-totals report declared, or None for other shapes.

    Only the property-wide totals shape emits a row for every declared key event,
    including one GA4 never returned, so only that shape fixes the declared set.
    """
    if not any(row.dimension_type == "GA4_KEY_EVENT_TOTALS" for row in report.performance_rows):
        return None
    return len(_key_event_rows(report))


def _admin_findings(
    context: ProviderAuditContext,
    window: AuditDataWindow,
    status: ProviderCoverageStatus,
) -> list[ProviderAuditFinding]:
    observation = context.evidence.ga4_admin
    if observation is None:
        return []
    inventory = observation.inventory
    evidence = (observation.record_digest,)
    property_ref = (inventory.identity.property_ref,)
    findings: list[ProviderAuditFinding] = []

    def _fire(
        rule_id: str,
        refs: tuple[str, ...],
        evidence_refs: tuple[str, ...] = evidence,
    ) -> None:
        findings.append(
            _emit(
                RULES[rule_id],
                resource_refs=refs,
                evidence_refs=evidence_refs,
                window=window,
                coverage_status=status,
            )
        )

    if not inventory.key_events:
        _fire("GA4_NO_KEY_EVENTS_CONFIGURED", property_ref)
    for key_event in inventory.key_events:
        if key_event.counting_method == "COUNTING_METHOD_UNSPECIFIED":
            _fire("GA4_KEY_EVENT_COUNTING_METHOD_UNSPECIFIED", (key_event.resource_ref,))
    if inventory.event_create_rules or inventory.event_edit_rules:
        _fire("GA4_EVENT_RULES_CONTENT_NOT_INSPECTED", property_ref)
    maximum_age = timedelta(seconds=context.policy.maximum_observation_age_seconds)
    if context.as_of - observation.observed_at > maximum_age:
        _fire("GA4_ADMIN_INVENTORY_STALE", property_ref)
    declared = _declared_key_event_count(context.report)
    if declared is not None and declared != len(inventory.key_events):
        _fire(
            "GA4_KEY_EVENT_COUNT_DIFFERS_FROM_ADMIN",
            (context.report.envelope.source_account_ref,),
            (context.source_record_digest, observation.record_digest),
        )
    return findings


def _static_findings(
    context: ProviderAuditContext,
    window: AuditDataWindow,
    statuses: dict[AuditDimension, ProviderCoverageStatus],
) -> list[ProviderAuditFinding]:
    """Findings that state a dimension's coverage limit rather than a measured defect."""
    evidence = (context.source_record_digest,)
    account = (context.report.envelope.source_account_ref,)
    rule_ids = [
        "GA4_DESTINATION_AND_LINKS_NOT_READ",
        "GA4_POLICY_SETTINGS_NOT_READ",
        "GA4_DRIFT_NOT_EVALUATED_BY_AUDIT",
        "GA4_KEY_EVENT_NOT_A_PROVIDER_CONVERSION",
        "GA4_BROWSER_NOT_APPLICABLE",
    ]
    if context.evidence.ga4_admin is None:
        rule_ids.append("GA4_ADMIN_INVENTORY_NOT_RETAINED")
    return [
        _emit(
            RULES[rule_id],
            resource_refs=account,
            evidence_refs=evidence,
            window=window,
            coverage_status=statuses[RULES[rule_id].dimension],
        )
        for rule_id in rule_ids
    ]


def _coverage_map(context: ProviderAuditContext) -> dict[AuditDimension, ProviderCoverageStatus]:
    return {
        "STRUCTURE": "PARTIAL" if context.evidence.ga4_admin is not None else "NOT_READ",
        "DELIVERY": "NOT_APPLICABLE",
        "SPEND": "NOT_APPLICABLE",
        "BIDS": "NOT_APPLICABLE",
        "OBJECTIVE": "NOT_APPLICABLE",
        "TARGETING": "NOT_APPLICABLE",
        "PLACEMENT": "NOT_APPLICABLE",
        "CREATIVE": "NOT_APPLICABLE",
        "DESTINATION_CONFIGURATION": "NOT_READ",
        # Never COVERED: browser firing, processed receipt and a GA4-interface
        # comparison are not evidenced, and the Data API returns no freshness.
        "TRACKING": "PARTIAL",
        "NATIVE_CONVERSION": "NOT_APPLICABLE",
        "BROWSER_MEASUREMENT": "NOT_APPLICABLE",
        "POLICY": "NOT_READ",
        "DRIFT": "NOT_READ",
    }


_RATIONALE: Final[dict[AuditDimension, str]] = {
    "STRUCTURE": "The Admin read lists key events and the event create and edit rules of one configured web stream. Other streams, custom dimensions, audiences and product links are not read.",
    "DELIVERY": "GA4 delivers no advertising; its sessions and key events are measurement, not delivery.",
    "SPEND": "GA4 holds no advertising spend in the fixed reads.",
    "BIDS": "GA4 has no bids.",
    "OBJECTIVE": "GA4 has no campaign objective.",
    "TARGETING": "GA4 has no targeting configuration.",
    "PLACEMENT": "GA4 has no ad placement.",
    "CREATIVE": "GA4 holds no ad creative.",
    "DESTINATION_CONFIGURATION": "Data stream URLs, cross-domain settings, referral exclusions and product links are not requested by the fixed GA4 reads.",
    "TRACKING": "Report quality, key-event cells and key-event configuration are evaluated. Attribution settings, browser firing, processed receipt and a GA4-interface comparison are not evidenced, and the Data API returns no freshness field.",
    "NATIVE_CONVERSION": "A GA4 key event is not a provider-native conversion, so no GA4 read can establish native conversion configuration.",
    "BROWSER_MEASUREMENT": "A local audit executes no browser and loads no page.",
    "POLICY": "Consent, data collection, data retention and Google signals settings are not requested by the fixed GA4 reads.",
    "DRIFT": "Drift between two retained Admin observations is compared by the measurement compare command; this audit reads one observation.",
}


def _coverage(
    context: ProviderAuditContext,
    statuses: dict[AuditDimension, ProviderCoverageStatus],
) -> tuple[ProviderAuditCoverage, ...]:
    """Name, for every dimension, exactly the evidence it was evaluated from."""
    observation = context.evidence.ga4_admin
    result: list[ProviderAuditCoverage] = []
    for dimension in _DIMENSION_ORDER:
        status = statuses[dimension]
        sources: list[EvidenceSource] = []
        refs: tuple[str, ...] = ()
        if status in {"COVERED", "PARTIAL"}:
            if dimension == "TRACKING":
                sources.append("PERFORMANCE_OBSERVATION")
                refs = (context.source_record_digest,)
            if observation is not None:
                sources.append("RETAINED_MEASUREMENT_OBSERVATION")
                refs = (*refs, observation.record_digest)
        result.append(
            ProviderAuditCoverage(
                dimension=dimension,
                status=status,
                evidence_sources=tuple(sources),
                evidence_record_count=len(refs),
                evidence_refs=refs,
                rationale=_RATIONALE[dimension],
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

    A report can hold up to fifty declared key events, each its own finding, so
    one recommendation can rest on more findings than one output row may name.
    Nothing is dropped: every finding stays an individual row in `findings`, the
    row states its complete `bound_finding_count` and the `bound_rule_ids` that
    define its membership, and an explicit `BoundExceeded` entry records the cap.
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
        subject: str,
        hypothesis: str,
        expected_impact: str,
        assumptions: tuple[str, ...],
        confidence: Confidence,
    ) -> None:
        matched = _ids(findings, rule_ids)
        if not matched:
            return
        selected = [item for item in findings if item.rule_id in rule_ids]
        evidence_refs = tuple(
            sorted({digest for item in selected for digest in item.finding.evidence_refs})
        )[:4]
        recommendations.append(
            ProviderRecommendation(
                recommendation=build_recommendation(
                    hypothesis=hypothesis,
                    evidence_refs=evidence_refs,
                    observations=tuple(sorted({item.finding.observation for item in selected}))[:8],
                    assumptions=assumptions,
                    confidence=confidence,
                    expected_impact=expected_impact,
                    risk="LOW",
                    expires_at=expires,
                    identity={
                        "rule_table_version": RULE_TABLE_VERSION,
                        "finding_ids": matched,
                        "operation_kind": "OBSERVE",
                    },
                ),
                finding_ids=_bound(
                    matched,
                    rule_ids,
                    scope="RECOMMENDATION_FINDING_IDS",
                    subject="OBSERVE:" + subject,
                    exceeded=exceeded,
                ),
                bound_finding_count=len(matched),
                bound_rule_ids=tuple(sorted(rule_ids)),
                operation_kind="OBSERVE",
                provider=PROVIDER,
                analytics_completeness=_BOUNDED,
            )
        )

    def _decline(
        rule_ids: set[str],
        *,
        subject: str,
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
                    subject=f"{reason}:{considered or 'NONE'}:{subject}",
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
        {"GA4_REPORT_INCOMPLETE", "GA4_REPORT_NOT_AVAILABLE_FROM_PROVIDER", "GA4_REPORT_NO_ROWS"},
        subject="MISSING_ROWS",
        hypothesis="A repeated bounded read over the same declared window may return the rows this read did not, or show whether GA4 still declines to report.",
        expected_impact="A second compatible read establishes whether the missing rows reflect real absence or a read that stopped early.",
        assumptions=(
            "A report with no rows, an unreconciled page walk, or a provider refusal is not evidence of zero activity.",
        ),
        confidence="MEDIUM",
    )
    _observe(
        {"GA4_REPORT_SAMPLING_METADATA_INCONSISTENT"},
        subject="SAMPLING_REREAD",
        hypothesis="A repeated bounded read over the same declared window may carry consistent sampling metadata on every page, or show that GA4 keeps changing it between pages.",
        expected_impact="A second compatible read separates a transient difference between pages from sampling GA4 reports inconsistently, before any sample ratio is quoted.",
        assumptions=(
            "The Data API documents no snapshot isolation between pages.",
            "A sampled count is an estimate, not an exact value.",
        ),
        confidence="MEDIUM",
    )
    _observe(
        {
            "GA4_KEY_EVENT_NOT_RETURNED",
            "GA4_WINDOW_INCLUDES_CURRENT_PROPERTY_DATE",
            "GA4_REPORT_REALTIME_NOT_PROCESSED",
        },
        subject="PROCESSED_READ",
        hypothesis="A processed aggregate read taken after GA4's normal processing interval may show counts this read could not.",
        expected_impact="A later processed read separates a processing delay from an event that does not occur, before any decision relies on the missing value.",
        assumptions=(
            "GA4 documents no processing guarantee, so the interval needed is not known.",
            "A key event without a returned row is not a measured zero.",
        ),
        confidence="MEDIUM",
    )
    _observe(
        {"GA4_ADMIN_INVENTORY_STALE", "GA4_KEY_EVENT_COUNT_DIFFERS_FROM_ADMIN"},
        subject="ADMIN_REREAD",
        hypothesis="A fresh Admin inventory read, followed by a key-event aggregate built from it, establishes whether the report and the configuration agree.",
        expected_impact="Two reads taken close together bound the observation interval for any difference between the report and the configuration.",
        assumptions=(
            "The inventory holds event names as fingerprints, so only counts are compared.",
            "The actor and exact time of any configuration change are unknown and are not inferred.",
        ),
        confidence="MEDIUM",
    )
    _observe(
        {"GA4_REPORT_COMPLETENESS_FACTS_NOT_RETAINED"},
        subject="AGGREGATE_REREAD",
        hypothesis="Reading a fixed aggregate shape retains the completeness, roll-up, restriction and per-key-event cell facts this report lacks.",
        expected_impact="An aggregate read lets every later audit tell a withheld or never-returned value from a measured zero.",
        assumptions=(
            "The quality statuses on this report's envelope are evaluated as retained.",
            "Where GA4 flagged a roll-up or restriction, this report kind cannot say which it was.",
        ),
        confidence="HIGH",
    )
    _decline(
        {"GA4_NO_KEY_EVENTS_CONFIGURED"},
        subject="NO_KEY_EVENTS",
        reason="EVIDENCE_INSUFFICIENT",
        detail="Creating a key event needs an event contract the customer chooses. Nothing in this evidence identifies which event the business counts, and a GA4 key event is not a provider conversion.",
        considered="CREATE",
    )
    _decline(
        {
            "GA4_REPORT_THRESHOLDED",
            "GA4_REPORT_SAMPLED",
            "GA4_REPORT_OTHER_ROW_ROLLUP",
            "GA4_REPORT_METRICS_RESTRICTED",
            "GA4_REPORT_PROVIDER_FLAGGED_UNKNOWN",
            "GA4_REPORT_PROVIDER_FLAGGED_UNATTRIBUTABLE",
            "GA4_KEY_EVENT_THRESHOLD_WITHHELD",
        },
        subject="GA4_ANSWER_PROPERTIES",
        reason="EVIDENCE_INSUFFICIENT",
        detail="These findings describe how GA4 answered, and a repeated identical read does not change them. The right response is to treat the affected values as unmeasured or estimated, not to run an operation.",
    )
    _decline(
        {
            "GA4_ROWS_JOIN_UNRESOLVED",
            "GA4_ROWS_JOIN_CONFLICT",
            "GA4_SESSIONS_WITHOUT_CAMPAIGN_NAME",
            "GA4_KEY_EVENT_COUNTING_METHOD_UNSPECIFIED",
            "GA4_EVENT_RULES_CONTENT_NOT_INSPECTED",
            "GA4_ADMIN_INVENTORY_NOT_RETAINED",
        },
        subject="NEEDS_CUSTOMER_ACTION",
        reason="EVIDENCE_INSUFFICIENT",
        detail="Resolving these needs a customer declaration, a review in the GA4 interface or the advertising provider, or an Admin read that has not been retained. No GA4 operation can be derived from this evidence.",
    )
    _decline(
        {
            "GA4_ATTRIBUTION_SETTINGS_NOT_READ",
            "GA4_DESTINATION_AND_LINKS_NOT_READ",
            "GA4_POLICY_SETTINGS_NOT_READ",
            "GA4_DRIFT_NOT_EVALUATED_BY_AUDIT",
            "GA4_KEY_EVENT_NOT_A_PROVIDER_CONVERSION",
            "GA4_BROWSER_NOT_APPLICABLE",
        },
        subject="UNREAD_DIMENSIONS",
        reason="COVERAGE_NOT_READ",
        detail="These dimensions are not read, or not applicable, in this build's fixed GA4 reads. An unread dimension yields no recommendation and is never reported as healthy.",
    )
    return recommendations, absences, exceeded


def evaluate(context: ProviderAuditContext) -> ProviderAuditSections:
    """Evaluate the GA4 rule table over retained records only."""
    if context.provider != PROVIDER:
        raise ValueError("The GA4 rule table evaluates GA4 evidence only")
    window = _window(context.report)
    statuses = _coverage_map(context)
    findings = [
        *_report_findings(context, window, statuses["TRACKING"]),
        *_admin_findings(context, window, statuses["TRACKING"]),
        *_static_findings(context, window, statuses),
    ]
    recommendations, absences, exceeded = _recommend(findings, context)
    findings.sort(key=lambda item: item.finding.finding_id)
    recommendations.sort(key=lambda item: item.recommendation.recommendation_id)
    absences.sort(key=lambda item: (item.reason, item.finding_ids))
    return ProviderAuditSections(
        provider=PROVIDER,
        rule_table_version=RULE_TABLE_VERSION,
        analytics_completeness=_BOUNDED,
        coverage=_coverage(context, statuses),
        findings=tuple(findings),
        recommendations=tuple(recommendations),
        no_recommendations=tuple(absences),
        bound_exceeded=tuple(exceeded),
    )


register_provider_rule(PROVIDER, evaluate)
