# ruff: noqa: E501
from datetime import UTC, date, datetime

import pytest

from adhelm.audit import AuditPolicy, audit_from_json, audit_report
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.models import Money
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope

SOURCE_DIGEST = "a" * 64
OBSERVED = datetime(2026, 9, 1, 12, tzinfo=UTC)
AS_OF = datetime(2026, 9, 2, 12, tzinfo=UTC)


def _envelope(*, quality: list[str] | None = None) -> ReportEnvelope:
    return ReportEnvelope(
        report_id="report:fixture",
        provider="google_ads",
        source_account_ref="account:fixture",
        query_contract_hash="b" * 64,
        window_start=date(2026, 8, 30),
        window_end=date(2026, 8, 31),
        source_timezone="UTC",
        report_timezone="UTC",
        currency="USD",
        attribution_model="DECLARED_PROVIDER_MODEL",
        attribution_window="7d_click",
        observed_at=OBSERVED,
        data_quality_statuses=quality or ["COMPLETE"],
        provider_api_version="v1",
        connector_version="test",
    )


def _row(
    *,
    resource_ref: str = "campaign:one",
    level: str = "CAMPAIGN",
    grain: str = "CAMPAIGN",
    impressions: int | None = 100,
    clicks: int | None = 2,
    spend: Money | None = None,
    default_spend: bool = True,
) -> PerformanceRow:
    if spend is None and default_spend:
        spend = Money(minor_units=300, currency="USD")
    return PerformanceRow(
        report_id="report:fixture",
        resource_level=level,  # type: ignore[arg-type]
        metric_grain=grain,  # type: ignore[arg-type]
        resource_type="FIXTURE_RESOURCE",
        resource_ref=resource_ref,
        campaign_ref="campaign:one",
        impressions=impressions,
        clicks=clicks,
        spend=spend,
        data_quality_statuses=["COMPLETE"],
    )


def _policy(**changes: object) -> AuditPolicy:
    values: dict[str, object] = {
        "maximum_observation_age_seconds": 172_800,
        "maximum_cpc": Money(minor_units=100, currency="USD"),
        "minimum_impressions": 200,
        "require_conversion_evidence": True,
    }
    values.update(changes)
    return AuditPolicy(**values)


def test_audit_is_deterministic_and_keeps_each_declared_metric_grain_separate() -> None:
    ad_row = _row(resource_ref="ad:one", level="AD", grain="AD", impressions=3, clicks=1)
    report = NormalizedReport(envelope=_envelope(), performance_rows=[_row(), ad_row])
    first = audit_report(report, source_record_digest=SOURCE_DIGEST, policy=_policy(), as_of=AS_OF)
    second = audit_report(report, source_record_digest=SOURCE_DIGEST, policy=_policy(), as_of=AS_OF)
    assert first == second
    high_cpc = [
        item for item in first.findings if item.finding_type == "CPC_ABOVE_DECLARED_MAXIMUM"
    ]
    assert {item.resource_refs[0] for item in high_cpc} == {"campaign:one", "ad:one"}
    assert all(item.automatic_write_allowed is False for item in first.findings)
    assert all(item.proposed_plan_ref is None for item in first.recommendations)
    assert len(first.recommendations) == len(high_cpc) + 1
    coverage = {item.dimension: item.status for item in first.coverage}
    assert coverage["STRUCTURE"] == "EVIDENCE_MISSING"
    assert coverage["DELIVERY"] == "CHECKED"
    assert coverage["POLICY"] == "EVIDENCE_MISSING"


def test_partial_quality_and_missing_conversion_evidence_are_explicit() -> None:
    report = NormalizedReport(
        envelope=_envelope(quality=["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"]),
        performance_rows=[_row()],
    )
    result = audit_report(report, source_record_digest=SOURCE_DIGEST, policy=_policy(), as_of=AS_OF)
    types = {item.finding_type for item in result.findings}
    assert "REPORT_QUALITY_PARTIAL" in types
    assert "CONVERSION_EVIDENCE_MISSING" in types
    assert any(
        "No conversion count is inferred" in value
        for item in result.recommendations
        for value in item.assumptions
    )
    assert all(item.evidence_refs == (SOURCE_DIGEST,) for item in result.findings)


def test_cpc_currency_mismatch_and_missing_metrics_never_create_a_threshold_claim() -> None:
    policy = _policy(maximum_cpc=Money(minor_units=100, currency="EUR"))
    report = NormalizedReport(
        envelope=_envelope(),
        performance_rows=[
            _row(),
            _row(
                resource_ref="campaign:two",
                impressions=None,
                clicks=None,
                spend=None,
                default_spend=False,
            ),
        ],
    )
    result = audit_report(report, source_record_digest=SOURCE_DIGEST, policy=policy, as_of=AS_OF)
    types = {item.finding_type for item in result.findings}
    assert "CPC_CURRENCY_MISMATCH" in types
    assert "CPC_EVIDENCE_MISSING" in types
    assert "CPC_ABOVE_DECLARED_MAXIMUM" not in types


def test_stale_observation_and_strict_json_input() -> None:
    report = NormalizedReport(envelope=_envelope(), performance_rows=[])
    result = audit_report(
        report,
        source_record_digest=SOURCE_DIGEST,
        policy=_policy(maximum_observation_age_seconds=60),
        as_of=AS_OF,
    )
    assert "OBSERVATION_STALE" in {item.finding_type for item in result.findings}
    raw = canonical_bytes(
        {
            "report": report.model_dump(mode="json"),
            "source_record_digest": SOURCE_DIGEST,
            "policy": _policy().model_dump(mode="json"),
            "as_of": AS_OF.isoformat().replace("+00:00", "Z"),
            "unexpected_query_url": "https://example.invalid",
        }
    )
    with pytest.raises(AdhelmError, match="strict schema"):
        audit_from_json(raw)


def test_reddit_cpc_uses_retained_micros_not_rounded_cents() -> None:
    envelope = _envelope().model_copy(update={"provider": "reddit_ads"})
    row = _row(spend=Money(minor_units=2, currency="USD"), clicks=2).model_copy(
        update={
            "provider_extensions": {
                "reddit_ads_spend_micros": "20001",
                "reddit_ads_rounding": "HALF_EVEN_TO_MINOR",
            }
        }
    )
    report = NormalizedReport(envelope=envelope, performance_rows=[row])
    result = audit_report(
        report,
        source_record_digest=SOURCE_DIGEST,
        policy=_policy(maximum_cpc=Money(minor_units=1, currency="USD")),
        as_of=AS_OF,
    )
    assert "CPC_ABOVE_DECLARED_MAXIMUM" in {item.finding_type for item in result.findings}


def test_linkedin_cpc_uses_retained_decimal_not_rounded_zero() -> None:
    envelope = _envelope().model_copy(update={"provider": "linkedin_ads"})
    row = PerformanceRow(
        report_id="report:fixture",
        resource_level="CAMPAIGN",
        metric_grain="CAMPAIGN",
        resource_type="LINKEDIN_SPONSORED_CAMPAIGN",
        resource_ref="campaign:one",
        campaign_ref="campaign:one",
        impressions=100,
        clicks=1,
        spend=Money(minor_units=0, currency="USD"),
        data_quality_statuses=["COMPLETE"],
        provider_extensions={
            "linkedin_ads_cost_in_local_currency": "0.005",
            "linkedin_ads_rounding": "HALF_EVEN_TO_MINOR",
        },
    )
    report = NormalizedReport(envelope=envelope, performance_rows=[row])
    result = audit_report(
        report,
        source_record_digest=SOURCE_DIGEST,
        policy=_policy(maximum_cpc=Money(minor_units=0, currency="USD")),
        as_of=AS_OF,
    )
    assert "CPC_ABOVE_DECLARED_MAXIMUM" in {item.finding_type for item in result.findings}
