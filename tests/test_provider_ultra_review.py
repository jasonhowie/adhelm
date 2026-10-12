"""Adversarial regressions for fixed provider reads and inventory retention."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

import adhelm.inventory as inventory
from adhelm.canonical import digest
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Money, Profile
from adhelm.providers.google_ads import _reported_micros
from adhelm.providers.meta_ads import MetaAdsAdSet, MetaAdsCampaign, MetaAdsIdentity
from adhelm.reporting import (
    ConversionBreakdownRow,
    NormalizedReport,
    PerformanceRow,
    ReportEnvelope,
)


def _profile() -> Profile:
    return Profile(
        profile_id="meta-read",
        provider="meta_ads",
        account_ref="123",
        principal_ref="principal.review",
        client_ref="client.review",
        scopes=["ads_read"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="fixture-credential",
    )


def _snapshot(state: dict[str, Any]) -> dict[str, Any]:
    state_digest = digest(state)
    return {
        "snapshot_id": "snapshot.review",
        "provider_api_version": "v26.0",
        "connector_version": "review",
        "resource_type": "CAMPAIGN",
        "resource_ref": "meta_ads.campaign.1",
        "parent_resource_refs": ["meta_ads.account.act_123"],
        "remote_fingerprint": state_digest,
        "normalized_state": state,
        "normalized_state_digest": state_digest,
        "effective_status": "PAUSED",
        "source_updated_at": None,
        "data_quality_statuses": [],
    }


def _envelope(provider: str) -> ReportEnvelope:
    return ReportEnvelope(
        report_id="report.review",
        provider=provider,  # type: ignore[arg-type]
        source_account_ref="account.review",
        query_contract_hash="a" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 1),
        source_timezone="UTC",
        report_timezone="UTC",
        attribution_model="REVIEW",
        observed_at=datetime(2026, 9, 4, tzinfo=UTC),
        provider_api_version="review",
        connector_version="review",
    )


def test_currencyless_minor_snapshot_cannot_enter_canonical_history(tmp_path: Path) -> None:
    with pytest.raises(AdhelmError) as raised:
        Ledger(tmp_path).append(
            "changes",
            "RESOURCE_SNAPSHOT",
            _snapshot({"budget_minor": 125}),
            provider="meta_ads",
            account_ref="123",
        )

    assert raised.value.code == "INVALID_LEDGER_PAYLOAD"
    assert not (tmp_path / "ledger").exists()


def test_meta_lifetime_budget_is_retained_as_currency_bound_state() -> None:
    identity = MetaAdsIdentity(
        account_ref="meta_ads.account.act_123",
        account_client_ref="act_123",
        timezone="UTC",
        currency="USD",
        status="1",
    )
    campaign = MetaAdsCampaign(
        campaign_ref="meta_ads.campaign.11",
        campaign_client_ref="11",
        status="PAUSED",
        objective="OUTCOME_TRAFFIC",
        lifetime_budget=Money(minor_units=900, currency="USD"),
    )
    adset = MetaAdsAdSet(
        adset_ref="meta_ads.adset.12",
        adset_client_ref="12",
        campaign_ref=campaign.campaign_ref,
        status="PAUSED",
        lifetime_budget=Money(minor_units=700, currency="USD"),
    )

    snapshots = inventory._meta_snapshots(identity, [campaign], [adset], [])
    states = {snapshot["resource_ref"]: snapshot["normalized_state"] for snapshot in snapshots}

    assert states[campaign.campaign_ref] == {
        "status": "PAUSED",
        "objective": "OUTCOME_TRAFFIC",
        "currency": "USD",
        "budget_minor": 900,
        "budget_type": "LIFETIME",
    }
    assert states[adset.adset_ref] == {
        "status": "PAUSED",
        "currency": "USD",
        "budget_minor": 700,
        "budget_type": "LIFETIME",
    }

    simultaneous = campaign.model_copy(
        update={"daily_budget": Money(minor_units=500, currency="USD")}
    )
    simultaneous_state = inventory._meta_snapshots(identity, [simultaneous], [], [])[1][
        "normalized_state"
    ]
    assert simultaneous_state["currency"] == "USD"
    assert simultaneous_state["daily_budget_minor"] == 500
    assert simultaneous_state["lifetime_budget_minor"] == 900
    assert "budget_minor" not in simultaneous_state
    assert "budget_type" not in simultaneous_state


def test_orphan_meta_hierarchy_fails_before_any_snapshot_is_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v26.0/act_123":
            return httpx.Response(
                200,
                json={"id": "123", "account_status": 1, "currency": "USD", "timezone_name": "UTC"},
            )
        if request.url.path.endswith("/campaigns"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "11",
                            "effective_status": "PAUSED",
                            "objective": "OUTCOME_TRAFFIC",
                            "daily_budget": "500",
                            "lifetime_budget": None,
                            "spend_cap": "0",
                            "start_time": None,
                            "stop_time": None,
                        }
                    ]
                },
            )
        if request.url.path.endswith("/adsets"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "12",
                            "campaign_id": "999",
                            "effective_status": "PAUSED",
                            "billing_event": None,
                            "bid_amount": None,
                            "bid_strategy": None,
                            "daily_budget": None,
                            "lifetime_budget": None,
                            "start_time": None,
                            "end_time": None,
                        }
                    ]
                },
            )
        assert request.url.path.endswith("/ads")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "13",
                        "campaign_id": "999",
                        "adset_id": "12",
                        "effective_status": "PAUSED",
                    }
                ]
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        inventory.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(inventory, "credential", lambda *_args, **_kwargs: "fixture-token")
    monkeypatch.setattr(inventory, "export_key", lambda: b"r" * 32)

    with pytest.raises(AdhelmError) as raised:
        inventory.run_inventory(tmp_path, "meta_ads", "meta-read")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert not list((tmp_path / "ledger" / "changes").glob("*.jsonl"))


def test_cross_provider_extensions_and_unreviewed_conversion_extensions_are_rejected() -> None:
    row = PerformanceRow(
        report_id="report.review",
        resource_level="CAMPAIGN",
        metric_grain="CAMPAIGN",
        resource_type="META_ADS_CAMPAIGN",
        resource_ref="meta_ads.campaign.1",
        provider_extensions={"ga4_event_ref": "ga4.event.review"},
    )
    with pytest.raises(ValidationError, match="declared provider"):
        NormalizedReport(envelope=_envelope("meta_ads"), performance_rows=[row])

    # The GA4 aggregate cell/report provenance keys are the only reviewed
    # conversion extensions; every other key is still rejected.
    with pytest.raises(ValidationError, match="Unsupported conversion reporting extension"):
        ConversionBreakdownRow(
            report_id="report.review",
            conversion_source="PROVIDER",
            conversion_kind="PURCHASE",
            attribution_model="REVIEW",
            provider_extensions={"ga4_event_ref": "ga4.event.review"},
        )


def test_google_native_cost_retains_signed_int64_precision_and_rejects_overflow() -> None:
    maximum = "9223372036854775807"
    spend, source = _reported_micros(maximum, "USD", "review cost")
    assert source == maximum
    assert spend.minor_units == 922_337_203_685_478

    report = NormalizedReport(
        envelope=_envelope("google_ads").model_copy(update={"currency": "USD"}),
        performance_rows=[
            PerformanceRow(
                report_id="report.review",
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="GOOGLE_ADS_CAMPAIGN",
                resource_ref="google_ads.campaign.1",
                spend=spend,
                provider_extensions={
                    "google_ads_cost_micros": source,
                    "google_ads_rounding": "HALF_EVEN_TO_MINOR",
                },
            )
        ],
    )
    assert report.performance_rows[0].provider_extensions["google_ads_cost_micros"] == maximum

    with pytest.raises(AdhelmError) as adapter_overflow:
        _reported_micros("9223372036854775808", "USD", "review cost")
    assert adapter_overflow.value.code == "PROVIDER_RESPONSE_INVALID"

    with pytest.raises(ValidationError, match="exact nonnegative decimal integer"):
        PerformanceRow(
            report_id="report.review",
            resource_level="CAMPAIGN",
            metric_grain="CAMPAIGN",
            resource_type="GOOGLE_ADS_CAMPAIGN",
            resource_ref="google_ads.campaign.1",
            spend=spend,
            provider_extensions={
                "google_ads_cost_micros": "9223372036854775808",
                "google_ads_rounding": "HALF_EVEN_TO_MINOR",
            },
        )
