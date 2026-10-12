# ruff: noqa: E501
"""Deterministic, evidence-bounded audit of one normalized report.

This module evaluates only values present in the supplied report.  It neither
contacts a provider nor infers configuration, conversion, browser, or policy
health from performance aggregates.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Literal

from pydantic import Field, field_validator

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.models import Money, Provider, SafeRef, StrictModel
from adhelm.reporting import LedgerCheckpoint, NormalizedReport, PerformanceRow
from adhelm.strict_json import validate_json_input

if TYPE_CHECKING:  # `adhelm.audits` imports this module; the seam stays one-directional.
    from adhelm.audits import ProviderAuditEvidence, ProviderAuditResult

CoverageStatus = Literal["EVIDENCE_MISSING", "PARTIAL", "CHECKED"]
Severity = Literal["INFO", "LOW", "MEDIUM", "HIGH"]
Confidence = Literal["LOW", "MEDIUM", "HIGH"]
Risk = Literal["LOW", "MEDIUM", "HIGH"]
AuditDimension = Literal[
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
]

# The report-only audit below evaluates exactly these dimensions in this order.
# `OBJECTIVE` and `DRIFT` are readable only from retained configuration
# snapshots, never from one normalized report, so they belong to the provider
# rule modules in `adhelm.audits`.  This tuple is an output contract: extending it
# would change every existing audit result.
_DIMENSIONS: tuple[AuditDimension, ...] = (
    "STRUCTURE",
    "DELIVERY",
    "SPEND",
    "BIDS",
    "TARGETING",
    "PLACEMENT",
    "CREATIVE",
    "DESTINATION_CONFIGURATION",
    "TRACKING",
    "NATIVE_CONVERSION",
    "BROWSER_MEASUREMENT",
    "POLICY",
)
_INCOMPLETE_QUALITY = frozenset(
    {
        "PROCESSING_PENDING",
        "PARTIAL_PAGE_FAILURE",
        "STALE",
        "SAMPLED",
        "THRESHOLDED",
        "MISSING_EVENT",
        "DUPLICATE_FIRING",
        "TIMEZONE_MISMATCH",
        "CURRENCY_MISMATCH",
        "ATTRIBUTION_MISMATCH",
        "UNMAPPED",
        "AMBIGUOUS_REMOTE_STATE",
        "PROVIDER_REPORTED_UNKNOWN",
        "MIXED_CREATIVE_VERSION",
        "CREATIVE_NOT_EXPOSED",
        "COMBINATION_UNKNOWN",
        "CREATIVE_METADATA_MISSING",
        "EXTRACTION_FAILED",
        "MINIMUM_CELL_SUPPRESSED",
        "SOURCE_EDITION_UNAVAILABLE",
        "NOT_AVAILABLE_FROM_PROVIDER",
        "NOT_SUPPORTED_BY_ANALYZER",
    }
)


def _aware(value: datetime, context: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{context} must include a timezone")
    return value.astimezone(UTC)


class AuditPolicy(StrictModel):
    """Only customer-declared thresholds; no universal optimization defaults."""

    maximum_observation_age_seconds: int = Field(ge=1, le=31_536_000)
    maximum_cpc: Money | None = None
    minimum_impressions: int | None = Field(default=None, ge=0)
    require_conversion_evidence: bool


class AuditDataWindow(StrictModel):
    window_start: date
    window_end: date
    source_timezone: str = Field(min_length=1, max_length=80)
    attribution_model: str = Field(min_length=1, max_length=120)
    attribution_window: str | None = Field(default=None, max_length=120)


class AuditCoverage(StrictModel):
    dimension: AuditDimension
    status: CoverageStatus
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=1)

    @field_validator("evidence_refs")
    @classmethod
    def record_digests_only(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in values
        ):
            raise ValueError("Audit evidence references must be record digests")
        return values


class AuditFinding(StrictModel):
    finding_id: str = Field(pattern=r"^finding:sha256:[0-9a-f]{64}$")
    finding_type: str = Field(min_length=1, max_length=80)
    severity: Severity
    provider: Provider
    # A report-only finding cites one row resource and the one record digest
    # that supplied it.  A provider rule in `adhelm.audits` may compare a retained
    # snapshot pair or relate a parent and child resource, so both bounds allow
    # a small fixed number of references.  The report-only rules below still
    # emit exactly one of each, so existing audit output is unchanged.
    resource_refs: tuple[SafeRef, ...] = Field(default_factory=tuple, max_length=4)
    observation: str = Field(min_length=1, max_length=500)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=4)
    data_window: AuditDataWindow
    confidence: Confidence
    risk: Risk
    suggested_action: str = Field(min_length=1, max_length=500)
    automatic_write_allowed: Literal[False] = False

    @field_validator("evidence_refs")
    @classmethod
    def record_digests_only(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in values
        ):
            raise ValueError("Audit evidence references must be record digests")
        return values


class Recommendation(StrictModel):
    recommendation_id: str = Field(pattern=r"^recommendation:sha256:[0-9a-f]{64}$")
    hypothesis: str = Field(min_length=1, max_length=500)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=4)
    observations: tuple[str, ...] = Field(min_length=1, max_length=8)
    assumptions: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    confidence: Confidence
    expected_impact: str = Field(min_length=1, max_length=500)
    risk: Risk
    spend_impact_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    proposed_plan_ref: Literal[None] = None
    expires_at: datetime

    @field_validator("evidence_refs")
    @classmethod
    def record_digests_only(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in values
        ):
            raise ValueError("Recommendation evidence references must be record digests")
        return values

    @field_validator("expires_at")
    @classmethod
    def expires_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "Recommendation expiry")


class AuditResult(StrictModel):
    source_record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ledger_checkpoints: tuple[LedgerCheckpoint, ...] = Field(default_factory=tuple)
    audit_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: datetime
    coverage: tuple[AuditCoverage, ...]
    findings: tuple[AuditFinding, ...]
    recommendations: tuple[Recommendation, ...]

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "Audit as_of")


class AuditInput(StrictModel):
    report: NormalizedReport
    source_record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: AuditPolicy
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "Audit as_of")


def audit_report(
    report: NormalizedReport,
    *,
    source_record_digest: str,
    policy: AuditPolicy,
    as_of: datetime,
) -> AuditResult:
    """Audit exactly one report without merging rows or inferring omitted evidence."""
    if len(source_record_digest) != 64 or any(
        char not in "0123456789abcdef" for char in source_record_digest
    ):
        raise AdhelmError(
            "INVALID_AUDIT_INPUT", "Audit source record digest is invalid.", exit_code=2
        )
    try:
        evaluated_at = _aware(as_of, "Audit as_of")
    except ValueError as exc:
        raise AdhelmError(
            "INVALID_AUDIT_INPUT", "Audit as_of must include a timezone.", exit_code=2
        ) from exc
    if evaluated_at < report.envelope.observed_at:
        raise AdhelmError(
            "INVALID_AUDIT_INPUT", "Audit as_of cannot precede report observation.", exit_code=2
        )
    policy_digest = digest(policy)
    window = AuditDataWindow(
        window_start=report.envelope.window_start,
        window_end=report.envelope.window_end,
        source_timezone=report.envelope.source_timezone,
        attribution_model=report.envelope.attribution_model,
        attribution_window=report.envelope.attribution_window,
    )
    evidence = (source_record_digest,)
    coverage = _coverage(report, evidence)
    findings: list[AuditFinding] = []
    recommendations: list[Recommendation] = []

    age = evaluated_at - report.envelope.observed_at
    if age > timedelta(seconds=policy.maximum_observation_age_seconds):
        finding = _finding(
            finding_type="OBSERVATION_STALE",
            severity="MEDIUM",
            provider=report.envelope.provider,
            resource_refs=(),
            observation="The report observation is older than the user-declared maximum age.",
            evidence_refs=evidence,
            data_window=window,
            confidence="HIGH",
            risk="MEDIUM",
            suggested_action="Read the same bounded report again before making an operational decision.",
            identity={"source_record_digest": source_record_digest, "policy_digest": policy_digest},
        )
        findings.append(finding)
        recommendations.append(
            _recommendation(
                finding,
                hypothesis="A refreshed report may change the observed performance state.",
                observations=(finding.observation,),
                assumptions=(),
                confidence="HIGH",
                expected_impact="A refreshed read reduces the risk of acting on stale evidence.",
                risk="LOW",
                expires_at=evaluated_at + timedelta(seconds=policy.maximum_observation_age_seconds),
            )
        )

    if _has_incomplete_quality(report):
        findings.append(
            _finding(
                finding_type="REPORT_QUALITY_PARTIAL",
                severity="MEDIUM",
                provider=report.envelope.provider,
                resource_refs=(),
                observation="Declared report quality statuses prevent a complete report-derived evaluation.",
                evidence_refs=evidence,
                data_window=window,
                confidence="HIGH",
                risk="MEDIUM",
                suggested_action="Read a complete compatible report before relying on threshold findings.",
                identity={
                    "source_record_digest": source_record_digest,
                    "policy_digest": policy_digest,
                },
            )
        )

    for row in report.performance_rows:
        row_findings = _row_findings(
            row, report, policy, source_record_digest, policy_digest, window
        )
        findings.extend(row_findings)
        for finding in row_findings:
            if finding.finding_type != "CPC_ABOVE_DECLARED_MAXIMUM":
                continue
            recommendations.append(
                _recommendation(
                    finding,
                    hypothesis=(
                        "A compatible bid, budget, and attribution read is required before "
                        "evaluating any change."
                    ),
                    observations=(finding.observation,),
                    assumptions=(
                        "The threshold comparison is not evidence of a causal optimization effect.",
                    ),
                    confidence="HIGH",
                    expected_impact=(
                        "The next read can determine whether a bounded change proposal has "
                        "compatible evidence."
                    ),
                    risk="LOW",
                    expires_at=evaluated_at
                    + timedelta(seconds=policy.maximum_observation_age_seconds),
                )
            )
    if policy.require_conversion_evidence and not _has_conversion_evidence(report):
        finding = _finding(
            finding_type="CONVERSION_EVIDENCE_MISSING",
            severity="MEDIUM",
            provider=report.envelope.provider,
            resource_refs=(),
            observation="The report contains no declared conversion count evidence.",
            evidence_refs=evidence,
            data_window=window,
            confidence="HIGH",
            risk="MEDIUM",
            suggested_action="Read configured conversion aggregates and their declared attribution context.",
            identity={"source_record_digest": source_record_digest, "policy_digest": policy_digest},
        )
        findings.append(finding)
        recommendations.append(
            _recommendation(
                finding,
                hypothesis="A dedicated conversion read may provide the evidence required by policy.",
                observations=(finding.observation,),
                assumptions=(
                    "No conversion count is inferred from clicks, sessions, or key events.",
                ),
                confidence="HIGH",
                expected_impact="The next read can establish whether declared conversion evidence is available.",
                risk="LOW",
                expires_at=evaluated_at + timedelta(seconds=policy.maximum_observation_age_seconds),
            )
        )
    for item in coverage:
        if item.status == "EVIDENCE_MISSING":
            findings.append(
                _finding(
                    finding_type=f"{item.dimension}_EVIDENCE_MISSING",
                    severity="INFO",
                    provider=report.envelope.provider,
                    resource_refs=(),
                    observation="This audit dimension has no direct evidence in the supplied report.",
                    evidence_refs=evidence,
                    data_window=window,
                    confidence="HIGH",
                    risk="LOW",
                    suggested_action="Read the dedicated bounded evidence surface before making a health claim.",
                    identity={
                        "source_record_digest": source_record_digest,
                        "policy_digest": policy_digest,
                        "dimension": item.dimension,
                    },
                )
            )
    findings.sort(key=lambda item: item.finding_id)
    recommendations.sort(key=lambda item: item.recommendation_id)
    return AuditResult(
        source_record_digest=source_record_digest,
        source_ledger_checkpoints=tuple(report.envelope.source_ledger_checkpoints),
        audit_policy_digest=policy_digest,
        as_of=evaluated_at,
        coverage=coverage,
        findings=tuple(findings),
        recommendations=tuple(recommendations),
    )


def audit_from_json(raw: str | bytes) -> AuditResult:
    """Strict file-input boundary for a complete local audit request."""
    try:
        request = validate_json_input(AuditInput, raw)
    except Exception as exc:  # Pydantic's JSON error is intentionally normalized for callers.
        raise AdhelmError(
            "INVALID_AUDIT_INPUT", "Audit input does not match the strict schema.", exit_code=2
        ) from exc
    return audit_report(
        request.report,
        source_record_digest=request.source_record_digest,
        policy=request.policy,
        as_of=request.as_of,
    )


def _coverage(report: NormalizedReport, evidence: tuple[str, ...]) -> tuple[AuditCoverage, ...]:
    rows = report.performance_rows
    direct = {
        "DELIVERY": bool(rows)
        and all(row.impressions is not None and row.clicks is not None for row in rows),
        "SPEND": bool(rows) and all(row.spend is not None for row in rows),
        "BIDS": bool(rows) and all(row.bid_amount is not None for row in rows),
        "PLACEMENT": bool(rows)
        and all(
            row.dimension_type == "PLACEMENT" and row.dimension_value is not None for row in rows
        ),
    }
    partial = {
        "DELIVERY": bool(rows) and not direct["DELIVERY"],
        "SPEND": bool(rows) and not direct["SPEND"],
        "BIDS": bool(rows) and not direct["BIDS"],
        "PLACEMENT": bool(rows) and any(row.dimension_type == "PLACEMENT" for row in rows),
        # A GA4 key event, and a CRM outcome, is never a provider-native
        # conversion (AGENTS rule 16): only provider-sourced rows count.
        "NATIVE_CONVERSION": any(
            row.conversion_source == "PROVIDER" for row in report.conversion_rows
        ),
    }
    result: list[AuditCoverage] = []
    for dimension in _DIMENSIONS:
        if direct.get(dimension, False):
            status: CoverageStatus = "CHECKED"
        elif partial.get(dimension, False):
            status = "PARTIAL"
        else:
            status = "EVIDENCE_MISSING"
        result.append(
            AuditCoverage(
                dimension=dimension,
                status=status,
                evidence_refs=evidence if status != "EVIDENCE_MISSING" else (),
            )
        )
    return tuple(result)


def _row_findings(
    row: PerformanceRow,
    report: NormalizedReport,
    policy: AuditPolicy,
    source_record_digest: str,
    policy_digest: str,
    window: AuditDataWindow,
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    evidence = (source_record_digest,)
    identity = {
        "source_record_digest": source_record_digest,
        "policy_digest": policy_digest,
        "resource_ref": row.resource_ref,
        "metric_grain": row.metric_grain,
    }
    if policy.minimum_impressions is not None:
        if row.impressions is None:
            findings.append(
                _finding(
                    finding_type="IMPRESSION_EVIDENCE_MISSING",
                    severity="INFO",
                    provider=report.envelope.provider,
                    resource_refs=(row.resource_ref,),
                    observation="This declared aggregate row has no impression count.",
                    evidence_refs=evidence,
                    data_window=window,
                    confidence="HIGH",
                    risk="LOW",
                    suggested_action="Read the same declared metric grain with an impression count.",
                    identity=identity,
                )
            )
        elif row.impressions < policy.minimum_impressions:
            findings.append(
                _finding(
                    finding_type="LOW_IMPRESSION_VOLUME",
                    severity="INFO",
                    provider=report.envelope.provider,
                    resource_refs=(row.resource_ref,),
                    observation="This declared aggregate row is below the user-declared minimum impression volume.",
                    evidence_refs=evidence,
                    data_window=window,
                    confidence="HIGH",
                    risk="LOW",
                    suggested_action="Collect more evidence at this same declared grain before interpreting rates.",
                    identity=identity,
                )
            )
    if policy.maximum_cpc is not None:
        findings.extend(_cpc_findings(row, report, policy, evidence, window, identity))
    return findings


def _cpc_findings(
    row: PerformanceRow,
    report: NormalizedReport,
    policy: AuditPolicy,
    evidence: tuple[str, ...],
    window: AuditDataWindow,
    identity: dict[str, str],
) -> list[AuditFinding]:
    if policy.maximum_cpc is None:
        return []
    if row.spend is None or row.clicks is None or row.clicks == 0:
        return [
            _finding(
                finding_type="CPC_EVIDENCE_MISSING",
                severity="INFO",
                provider=report.envelope.provider,
                resource_refs=(row.resource_ref,),
                observation="This declared aggregate row lacks spend and a nonzero click count for a CPC check.",
                evidence_refs=evidence,
                data_window=window,
                confidence="HIGH",
                risk="LOW",
                suggested_action="Read compatible spend and click aggregates before applying the CPC threshold.",
                identity=identity,
            )
        ]
    if row.spend.currency != policy.maximum_cpc.currency:
        return [
            _finding(
                finding_type="CPC_CURRENCY_MISMATCH",
                severity="INFO",
                provider=report.envelope.provider,
                resource_refs=(row.resource_ref,),
                observation="The declared row spend currency differs from the user-declared CPC threshold currency.",
                evidence_refs=evidence,
                data_window=window,
                confidence="HIGH",
                risk="LOW",
                suggested_action="Use a threshold in the declared row currency; no currency conversion is performed.",
                identity=identity,
            )
        ]
    native_micros = row.provider_extensions.get("google_ads_cost_micros")
    native_micros = row.provider_extensions.get("reddit_ads_spend_micros", native_micros)
    linkedin_decimal = row.provider_extensions.get("linkedin_ads_cost_in_local_currency")
    if isinstance(linkedin_decimal, str):
        # The normalized cent projection may be zero or rounded down while the
        # retained provider decimal is nonzero. Compare that original amount.
        with localcontext() as context_precision:
            context_precision.prec = 100
            within_limit = Decimal(linkedin_decimal) * Decimal(100) <= Decimal(
                policy.maximum_cpc.minor_units * row.clicks
            )
    elif isinstance(native_micros, str):
        within_limit = int(native_micros) <= policy.maximum_cpc.minor_units * 10_000 * row.clicks
    else:
        within_limit = row.spend.minor_units <= policy.maximum_cpc.minor_units * row.clicks
    if within_limit:
        return []
    finding = _finding(
        finding_type="CPC_ABOVE_DECLARED_MAXIMUM",
        severity="MEDIUM",
        provider=report.envelope.provider,
        resource_refs=(row.resource_ref,),
        observation="This declared aggregate row exceeds the user-declared maximum CPC threshold.",
        evidence_refs=evidence,
        data_window=window,
        confidence="HIGH",
        risk="MEDIUM",
        suggested_action="Read declared bid, budget, and attribution evidence before proposing a change.",
        identity=identity,
    )
    return [finding]


def _has_conversion_evidence(report: NormalizedReport) -> bool:
    # A GA4 key event or an unmapped outcome cannot prove a provider-native signal.
    # This recognizes only mapped native reporting; browser/receipt coverage remains missing.
    return any(
        row.conversion_source == "PROVIDER"
        and row.conversion_count is not None
        and row.conversion_action_ref is not None
        and row.conversion_contract_version_ref is not None
        and row.conversion_mapping_version_ref is not None
        and row.counting_method is not None
        and row.join_status in {"EXACT_PROVIDER_ID", "EXACT_CONTROLLED_KEY", "EXPLICIT_MAPPING"}
        and row.attribution_model not in {"UNKNOWN", "NOT_AVAILABLE_FROM_PROVIDER"}
        and not set(row.data_quality_statuses).intersection(_INCOMPLETE_QUALITY)
        for row in report.conversion_rows
    )


def _has_incomplete_quality(report: NormalizedReport) -> bool:
    statuses = set(report.envelope.data_quality_statuses)
    for performance_row in report.performance_rows:
        statuses.update(performance_row.data_quality_statuses)
    for conversion_row in report.conversion_rows:
        statuses.update(conversion_row.data_quality_statuses)
    return bool(statuses & _INCOMPLETE_QUALITY)


def _finding(
    *,
    finding_type: str,
    severity: Severity,
    provider: Provider,
    resource_refs: tuple[str, ...],
    observation: str,
    evidence_refs: tuple[str, ...],
    data_window: AuditDataWindow,
    confidence: Confidence,
    risk: Risk,
    suggested_action: str,
    identity: Mapping[str, object],
) -> AuditFinding:
    finding_id = "finding:sha256:" + digest(
        {
            "finding_type": finding_type,
            "severity": severity,
            "provider": provider,
            "resource_refs": resource_refs,
            "observation": observation,
            "evidence_refs": evidence_refs,
            "data_window": data_window.model_dump(mode="json"),
            "confidence": confidence,
            "risk": risk,
            "suggested_action": suggested_action,
            "identity": identity,
        }
    )
    return AuditFinding(
        finding_id=finding_id,
        finding_type=finding_type,
        severity=severity,
        provider=provider,
        resource_refs=resource_refs,
        observation=observation,
        evidence_refs=evidence_refs,
        data_window=data_window,
        confidence=confidence,
        risk=risk,
        suggested_action=suggested_action,
    )


def _recommendation(
    finding: AuditFinding,
    *,
    hypothesis: str,
    observations: tuple[str, ...],
    assumptions: tuple[str, ...],
    confidence: Confidence,
    expected_impact: str,
    risk: Risk,
    expires_at: datetime,
) -> Recommendation:
    identity = {
        "finding_id": finding.finding_id,
        "hypothesis": hypothesis,
        "observations": observations,
        "assumptions": assumptions,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }
    return Recommendation(
        recommendation_id="recommendation:sha256:" + digest(identity),
        hypothesis=hypothesis,
        evidence_refs=finding.evidence_refs,
        observations=observations,
        assumptions=assumptions,
        confidence=confidence,
        expected_impact=expected_impact,
        risk=risk,
        spend_impact_minor=None,
        currency=None,
        proposed_plan_ref=None,
        expires_at=expires_at,
    )


def build_finding(
    *,
    finding_type: str,
    severity: Severity,
    provider: Provider,
    resource_refs: tuple[str, ...],
    observation: str,
    evidence_refs: tuple[str, ...],
    data_window: AuditDataWindow,
    confidence: Confidence,
    risk: Risk,
    suggested_action: str,
    identity: Mapping[str, object],
) -> AuditFinding:
    """Public finding constructor for the provider rule modules in `adhelm.audits`.

    Provider rules use exactly the same RFC 8785 content identity as the
    report-only rules above, so an identical rule over identical retained
    evidence always produces an identical finding identifier.
    """
    return _finding(
        finding_type=finding_type,
        severity=severity,
        provider=provider,
        resource_refs=resource_refs,
        observation=observation,
        evidence_refs=evidence_refs,
        data_window=data_window,
        confidence=confidence,
        risk=risk,
        suggested_action=suggested_action,
        identity=identity,
    )


def build_recommendation(
    *,
    hypothesis: str,
    evidence_refs: tuple[str, ...],
    observations: tuple[str, ...],
    assumptions: tuple[str, ...],
    confidence: Confidence,
    expected_impact: str,
    risk: Risk,
    expires_at: datetime,
    identity: Mapping[str, object],
) -> Recommendation:
    """Public recommendation constructor bound to one or more findings.

    The caller supplies the bound finding identifiers inside `identity`, so a
    recommendation derived from a different finding set gets a different
    identifier.  `proposed_plan_ref` stays `None`: a recommendation is never a
    plan and never authorizes a remote mutation.
    """
    expiry = _aware(expires_at, "Recommendation expiry")
    payload = {
        "hypothesis": hypothesis,
        "observations": observations,
        "assumptions": assumptions,
        "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        "identity": identity,
    }
    return Recommendation(
        recommendation_id="recommendation:sha256:" + digest(payload),
        hypothesis=hypothesis,
        evidence_refs=evidence_refs,
        observations=observations,
        assumptions=assumptions,
        confidence=confidence,
        expected_impact=expected_impact,
        risk=risk,
        spend_impact_minor=None,
        currency=None,
        proposed_plan_ref=None,
        expires_at=expiry,
    )


def audit_provider(
    report: NormalizedReport,
    *,
    source_record_digest: str,
    policy: AuditPolicy,
    as_of: datetime,
    evidence: ProviderAuditEvidence | None = None,
) -> ProviderAuditResult:
    """Audit one report and dispatch to the registered provider rule module.

    The provider-neutral `audit_report` result is embedded unchanged.  A
    provider with no registered rule module returns
    `rules_status="NO_PROVIDER_RULE_MODULE"` and no sections, so this entry
    point never alters an existing provider's audit output.  `evidence` carries
    retained local records only -- an inventory snapshot set, an optional
    earlier snapshot set for drift, an optional local creative catalog, and, for
    GA4, one retained Admin observation of the report's own property.
    Nothing here contacts a provider, reads a credential, or appends a record.
    """
    from adhelm.audits import ProviderAuditContext, provider_rule
    from adhelm.audits import ProviderAuditEvidence as _Evidence
    from adhelm.audits import ProviderAuditResult as _Result
    from adhelm.saved_audit_evidence import retained_account_key

    audit = audit_report(
        report, source_record_digest=source_record_digest, policy=policy, as_of=as_of
    )
    provider = report.envelope.provider
    rule = provider_rule(provider)
    if rule is None:
        return _Result(provider=provider, audit=audit, rules_status="NO_PROVIDER_RULE_MODULE")
    supplied = evidence if evidence is not None else _Evidence()
    account_ref = report.envelope.source_account_ref
    for retained in (supplied.inventory, supplied.earlier_inventory):
        if retained is None:
            continue
        if retained.provider != provider:
            raise AdhelmError(
                "INVALID_AUDIT_INPUT",
                "Retained inventory evidence belongs to a different provider.",
                exit_code=2,
            )
        # Evidence for another account describes resources this report never
        # measured. Auditing across the two would relate unrelated resources.
        #
        # One account can be written two ways: Meta keys its retained records
        # by the bare numeric ad-account identifier while its report envelope
        # carries the canonical `meta_ads.account.act_<id>` reference.  Both
        # sides are reduced to the same retained key before they are compared,
        # so a report this build produced reaches its own retained inventory
        # and a genuinely different account is still refused.
        if retained_account_key(provider, retained.account_ref) != retained_account_key(
            provider, account_ref
        ):
            raise AdhelmError(
                "INVALID_AUDIT_INPUT",
                "Retained inventory evidence belongs to a different account than the report.",
                exit_code=2,
                next_action="Audit the retained inventory for the report's own account.",
            )
    if supplied.ga4_requested_property_ref is not None and (
        provider != "ga4" or supplied.ga4_requested_property_ref != account_ref
    ):
        # The caller asked for GA4 evidence of a property the report does not
        # describe.  This is refused whether or not an observation is retained
        # for that property, so the outcome never depends on what happens to be
        # in the ledger and an absent observation cannot be reported as absent
        # evidence for the report's own property.
        raise AdhelmError(
            "INVALID_AUDIT_INPUT",
            "The requested GA4 property is not the property the report describes.",
            exit_code=2,
            next_action="Audit the report with its own property, or omit the account selector.",
        )
    if supplied.ga4_admin is not None:
        if provider != "ga4" or not supplied.ga4_admin.describes_report_account(account_ref):
            raise AdhelmError(
                "INVALID_AUDIT_INPUT",
                "Retained GA4 Admin evidence belongs to a different property than the report.",
                exit_code=2,
                next_action="Audit the retained Admin observation for the report's own property.",
            )
        # Configuration evidence that finished after the evaluation time did not
        # exist at that time.  Its age would be negative and so never stale, which
        # would let a past `--at` audit read a future observation without comment.
        if supplied.ga4_admin.observed_at > audit.as_of:
            raise AdhelmError(
                "INVALID_AUDIT_INPUT",
                "Retained GA4 Admin evidence was observed after the audit evaluation time.",
                exit_code=2,
                next_action="Evaluate at or after the Admin observation, or select an earlier one.",
            )
    sections = rule(
        ProviderAuditContext(
            provider=provider,
            report=report,
            source_record_digest=source_record_digest,
            policy=policy,
            as_of=audit.as_of,
            evidence=supplied,
        )
    )
    return _Result(provider=provider, audit=audit, rules_status="EVALUATED", sections=sections)
