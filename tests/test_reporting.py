from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from adhelm.reporting import (
    NormalizedReport,
    PerformanceRow,
    ReportEnvelope,
    resolve_campaign_join,
)


def _envelope() -> ReportEnvelope:
    return ReportEnvelope(
        report_id="report.1",
        provider="ga4",
        source_account_ref="account.1",
        query_contract_hash="a" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 2),
        source_timezone="America/Chicago",
        report_timezone="America/Chicago",
        attribution_model="GA4_DATA_API",
        observed_at=datetime(2026, 9, 4, tzinfo=UTC),
        provider_api_version="v1beta",
        connector_version="test",
    )


def _account_row() -> PerformanceRow:
    return PerformanceRow(
        report_id="report.1",
        resource_level="ACCOUNT",
        metric_grain="ACCOUNT",
        resource_type="GA4_PROPERTY",
        resource_ref="account.1",
        sessions=4,
    )


def test_exact_join_never_uses_names_or_fuzzy_matching() -> None:
    exact = resolve_campaign_join(
        provider_campaign_ref="google.campaign.1",
        source_campaign_ref="ga4.campaign.1",
        provider_exact_id="123",
        source_exact_id="123",
        provider_controlled_key=None,
        source_controlled_key=None,
    )
    assert exact.status == "EXACT_PROVIDER_ID"

    unmapped = resolve_campaign_join(
        provider_campaign_ref="google.campaign.1",
        source_campaign_ref="ga4.campaign.1",
        provider_exact_id="123",
        source_exact_id="456",
        provider_controlled_key=None,
        source_controlled_key=None,
    )
    assert unmapped.status == "UNMAPPED"


def test_dynamic_ad_metrics_need_an_exposure_caveat() -> None:
    with pytest.raises(ValidationError):
        PerformanceRow(
            report_id="report.1",
            resource_level="AD",
            metric_grain="AD",
            resource_type="AD",
            resource_ref="ad.1",
            creative_version_id="creative.1",
            impressions=10,
        )


def test_ad_grain_cannot_be_fanned_out_to_renditions() -> None:
    row = PerformanceRow(
        report_id="report.1",
        resource_level="AD",
        metric_grain="AD",
        resource_type="AD",
        resource_ref="ad.1",
        creative_version_id="creative.1",
        impressions=10,
        data_quality_statuses=["COMBINATION_UNKNOWN"],
    )
    with pytest.raises(ValidationError):
        NormalizedReport(envelope=_envelope(), performance_rows=[row, row])


def test_ledger_payload_is_normalized_and_contains_no_raw_transport_shape() -> None:
    report = NormalizedReport(envelope=_envelope(), performance_rows=[_account_row()])
    payload = report.to_ledger_payload(
        observation_key="observation.1",
        observation_revision=1,
        source_report_digest="b" * 64,
        query_as_of=datetime(2026, 9, 4, tzinfo=UTC),
    )
    assert set(payload) == {
        "observation_key",
        "observation_revision",
        "supersedes_record_id",
        "query_as_of",
        "source_report_digest",
        "report_envelope",
        "performance_rows",
        "conversion_rows",
    }
    assert "raw_response" not in str(payload)


def test_reddit_native_micros_are_provider_bound_and_exact() -> None:
    from pydantic import ValidationError

    from adhelm.reporting import PerformanceRow

    row = PerformanceRow(
        report_id="report:reddit-micros",
        resource_level="CAMPAIGN",
        metric_grain="CAMPAIGN",
        resource_type="REDDIT_CAMPAIGN",
        resource_ref="reddit_ads.campaign.c1",
        spend={"minor_units": 2, "currency": "USD"},
        provider_extensions={
            "reddit_ads_spend_micros": "20001",
            "reddit_ads_rounding": "HALF_EVEN_TO_MINOR",
        },
    )
    assert row.spend and row.spend.minor_units == 2
    with pytest.raises(ValidationError):
        PerformanceRow(
            report_id="report:bad-micros",
            resource_level="CAMPAIGN",
            metric_grain="CAMPAIGN",
            resource_type="REDDIT_CAMPAIGN",
            resource_ref="reddit_ads.campaign.c1",
            spend={"minor_units": 2, "currency": "USD"},
            provider_extensions={
                "reddit_ads_spend_micros": "9223372036854775808",
                "reddit_ads_rounding": "HALF_EVEN_TO_MINOR",
            },
        )


def test_reddit_report_rejects_google_native_spend_extension() -> None:
    from pydantic import ValidationError

    from adhelm.reporting import NormalizedReport, ReportEnvelope

    row = PerformanceRow(
        report_id="report:cross-provider",
        resource_level="CAMPAIGN",
        metric_grain="CAMPAIGN",
        resource_type="REDDIT_CAMPAIGN",
        resource_ref="reddit_ads.campaign.c1",
        spend={"minor_units": 2, "currency": "USD"},
        provider_extensions={
            "google_ads_cost_micros": "20001",
            "google_ads_rounding": "HALF_EVEN_TO_MINOR",
        },
    )
    envelope = ReportEnvelope(
        report_id="report:cross-provider",
        provider="reddit_ads",
        source_account_ref="reddit_ads.account.one",
        query_contract_hash="a" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 1),
        source_timezone="UTC",
        report_timezone="UTC",
        currency="USD",
        attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
        provider_api_version="v3",
        connector_version="test",
    )
    with pytest.raises(ValidationError, match="declared provider"):
        NormalizedReport(envelope=envelope, performance_rows=[row])


def test_linkedin_native_decimal_is_retained_and_bound_to_rounded_cents() -> None:
    row = PerformanceRow(
        report_id="report:linkedin-decimal",
        resource_level="CAMPAIGN",
        metric_grain="CAMPAIGN",
        resource_type="LINKEDIN_SPONSORED_CAMPAIGN",
        resource_ref="linkedin_ads.campaign.c1",
        spend={"minor_units": 1, "currency": "USD"},
        provider_extensions={
            "linkedin_ads_cost_in_local_currency": "0.010001",
            "linkedin_ads_rounding": "HALF_EVEN_TO_MINOR",
        },
    )
    envelope = _envelope().model_copy(
        update={
            "report_id": "report:linkedin-decimal",
            "provider": "linkedin_ads",
            "source_account_ref": "linkedin_ads.account.one",
            "currency": "USD",
        }
    )
    assert NormalizedReport(envelope=envelope, performance_rows=[row]).performance_rows == [row]
    with pytest.raises(ValidationError, match="retained LinkedIn decimal"):
        PerformanceRow(
            report_id="report:linkedin-decimal",
            resource_level="CAMPAIGN",
            metric_grain="CAMPAIGN",
            resource_type="LINKEDIN_SPONSORED_CAMPAIGN",
            resource_ref="linkedin_ads.campaign.c1",
            spend={"minor_units": 0, "currency": "USD"},
            provider_extensions={
                "linkedin_ads_cost_in_local_currency": "0.010001",
                "linkedin_ads_rounding": "HALF_EVEN_TO_MINOR",
            },
        )
    with pytest.raises(ValidationError, match="exact nonnegative decimal string"):
        PerformanceRow(
            report_id="report:linkedin-decimal",
            resource_level="CAMPAIGN",
            metric_grain="CAMPAIGN",
            resource_type="LINKEDIN_SPONSORED_CAMPAIGN",
            resource_ref="linkedin_ads.campaign.c1",
            spend={"minor_units": 1, "currency": "USD"},
            provider_extensions={
                "linkedin_ads_cost_in_local_currency": "1e-2",
                "linkedin_ads_rounding": "HALF_EVEN_TO_MINOR",
            },
        )
