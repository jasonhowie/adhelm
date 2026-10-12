# ruff: noqa: E501
"""The `audit --provider-rules` route, end to end, for Google Ads.

Every case is offline: retained ledger records only, no provider request, no
credential, no network.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from adhelm.audits import provider_rule
from adhelm.audits.google_ads import RULE_TABLE_VERSION, evaluate
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.ledger import Ledger
from adhelm.models import Money
from adhelm.providers.google_ads import (
    GOOGLE_ADS_AD_SCHEDULE_ABSENT,
    GOOGLE_ADS_BUDGET_NOT_RETURNED,
    GOOGLE_ADS_BUDGET_TYPE_NOT_RETURNED,
    GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED,
    GOOGLE_ADS_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED,
    GOOGLE_ADS_FINAL_URL_PRESENT,
    GOOGLE_ADS_GEO_TARGET_TYPE_RETURNED,
    GOOGLE_ADS_NETWORK_CONTENT_ENABLED,
    GOOGLE_ADS_NETWORK_SETTINGS_RETURNED,
    GOOGLE_ADS_PAGINATION_TOTAL_DECLARED,
    GOOGLE_ADS_POLICY_APPROVAL_PREFIX,
    GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED,
    GOOGLE_ADS_PRODUCTION_ACCOUNT_DECLARED,
    GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED,
)
from adhelm.reporting import LedgerCheckpoint, NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.saved_audit_evidence import load_audit_evidence
from adhelm.workflows import persist_report

runner = CliRunner()

# `run_inventory` appends Google Ads records with `profile.account_ref`, which
# the read adapter also uses as the account resource reference: the ten-digit
# customer identifier without formatting hyphens.  Evidence selection and the
# audited report therefore share that key.
ACCOUNT = "1234567890"
CAMPAIGN = "google_ads.campaign.11"
AD_GROUP = "google_ads.ad_group.21"
AD = "google_ads.ad.31"


def _snapshot(
    resource_type: str,
    resource_ref: str,
    parents: list[str],
    state: dict[str, Any],
    quality: list[str],
) -> dict[str, Any]:
    state_digest = digest(state)
    return {
        "snapshot_id": "snapshot:" + digest({"resource": resource_ref, "state": state_digest}),
        "provider_api_version": "v25",
        "connector_version": "test",
        "resource_type": resource_type,
        "resource_ref": resource_ref,
        "parent_resource_refs": parents,
        "remote_fingerprint": state_digest,
        "normalized_state": state,
        "normalized_state_digest": state_digest,
        "effective_status": str(state["status"]),
        "source_updated_at": None,
        "data_quality_statuses": quality,
    }


def _inventory_read(ledger: Ledger, *, campaign_status: str = "ENABLED") -> list[dict[str, Any]]:
    snapshots = [
        _snapshot(
            "ACCOUNT",
            ACCOUNT,
            [],
            {"status": "NOT_AVAILABLE_FROM_PROVIDER", "currency": "USD", "timezone": "UTC"},
            [
                "NOT_AVAILABLE_FROM_PROVIDER",
                GOOGLE_ADS_PAGINATION_TOTAL_DECLARED,
                GOOGLE_ADS_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED,
                GOOGLE_ADS_PRODUCTION_ACCOUNT_DECLARED,
            ],
        ),
        _snapshot(
            "CAMPAIGN",
            CAMPAIGN,
            [ACCOUNT],
            {
                "status": campaign_status,
                "objective": "SEARCH",
                "currency": "USD",
                "bid_strategy": "MANUAL_CPC",
                "schedule_start": "2026-01-01",
            },
            [
                GOOGLE_ADS_AD_SCHEDULE_ABSENT,
                GOOGLE_ADS_BUDGET_NOT_RETURNED,
                GOOGLE_ADS_BUDGET_TYPE_NOT_RETURNED,
                GOOGLE_ADS_NETWORK_SETTINGS_RETURNED,
                GOOGLE_ADS_GEO_TARGET_TYPE_RETURNED,
                GOOGLE_ADS_NETWORK_CONTENT_ENABLED,
            ],
        ),
        _snapshot(
            "AD_GROUP",
            AD_GROUP,
            [CAMPAIGN],
            {
                "status": campaign_status,
                "objective": "SEARCH_STANDARD",
                "currency": "USD",
                "bid_amount_minor": 150,
            },
            [],
        ),
        _snapshot(
            "AD",
            AD,
            [CAMPAIGN, AD_GROUP],
            {"status": campaign_status, "objective": "RESPONSIVE_SEARCH_AD"},
            [
                "CREATIVE_NOT_EXPOSED",
                GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED,
                GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED,
                GOOGLE_ADS_FINAL_URL_PRESENT,
                GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED,
                GOOGLE_ADS_POLICY_APPROVAL_PREFIX + "APPROVED",
            ],
        ),
    ]
    appended = [
        ledger.append(
            "changes", "RESOURCE_SNAPSHOT", item, provider="google_ads", account_ref=ACCOUNT
        )
        for item in snapshots
    ]
    ledger.append(
        "changes",
        "INVENTORY_READ_COMPLETED",
        {
            "schema_version": 1,
            "provider": "google_ads",
            "account_ref": ACCOUNT,
            "snapshot_record_digests": [record["record_digest"] for record in appended],
            "resource_counts": {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1},
            "read_started_at": "2026-09-01T11:00:00Z",
            "read_finished_at": "2026-09-01T12:00:00Z",
        },
        provider="google_ads",
        account_ref=ACCOUNT,
    )
    return appended


def _retain_report(ledger: Ledger, *, impressions: int = 100) -> str:
    checkpoint = ledger.checkpoint()
    report = NormalizedReport(
        envelope=ReportEnvelope(
            report_id="report:google",
            provider="google_ads",
            source_account_ref=ACCOUNT,
            query_contract_hash="c" * 64,
            window_start=date(2026, 8, 25),
            window_end=date(2026, 8, 31),
            source_timezone="UTC",
            report_timezone="UTC",
            currency="USD",
            attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
            observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            data_quality_statuses=["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"],
            provider_api_version="v25",
            connector_version="test",
            source_ledger_checkpoints=[
                LedgerCheckpoint(
                    stream_id=value["stream_id"],
                    high_watermark_sequence=value["high_watermark"],
                    head_digest=value["head_digest"],
                )
                for value in checkpoint["streams"].values()
            ],
        ),
        performance_rows=[
            PerformanceRow(
                report_id="report:google",
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="GOOGLE_ADS_CAMPAIGN",
                resource_ref=CAMPAIGN,
                campaign_ref=CAMPAIGN,
                dimension_type="DATE",
                dimension_value="2026-08-25",
                impressions=impressions,
                clicks=4,
                spend=Money(minor_units=125, currency="USD"),
                data_quality_statuses=["COMPLETE"],
                provider_extensions={
                    "google_ads_cost_micros": "1250000",
                    "google_ads_rounding": "HALF_EVEN_TO_MINOR",
                },
            )
        ],
    )
    return str(persist_report(ledger, report)["record_digest"])


def _policy_file(directory: Path) -> Path:
    policy = directory / "audit-policy.json"
    policy.write_text(
        json.dumps(
            {"maximum_observation_age_seconds": 604800, "require_conversion_evidence": False}
        )
    )
    return policy


@pytest.fixture(autouse=True)
def _local_export_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redaction uses the private OS export key; supply a fixed local one."""
    import adhelm.credentials

    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"p" * 32)


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Path, str]:
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    _inventory_read(ledger)
    return directory, _retain_report(ledger)


def test_the_loader_selects_the_retained_google_read_for_the_reports_account(
    prepared: tuple[Path, str],
) -> None:
    directory, _digest = prepared
    evidence, provenance = load_audit_evidence(
        Ledger(directory), provider="google_ads", account_ref=ACCOUNT
    )
    assert evidence.inventory is not None
    assert evidence.inventory.provider == "google_ads"
    assert evidence.inventory.completeness == "COMPLETE_BOUNDED_READ"
    assert len(evidence.inventory.records) == 4
    assert provenance["selected_inventory_read_boundary"] == "INVENTORY_READ_COMPLETED_MARKER"


def test_the_google_rule_module_is_reachable_through_the_registry() -> None:
    assert provider_rule("google_ads") is evaluate


def test_the_provider_rules_route_evaluates_the_google_table_end_to_end(
    prepared: tuple[Path, str],
) -> None:
    directory, record_digest = prepared
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "audit",
            record_digest,
            "--input",
            str(_policy_file(directory)),
            "--provider-rules",
            "--at",
            "2026-09-01T18:00:00+00:00",
        ],
    )
    assert result.exit_code == 0, result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["command"] == "audit --provider-rules"
    assert envelope["outcome"] == "INCOMPLETE_COVERAGE"
    data = envelope["data"]
    assert data["provider"] == "google_ads"
    assert data["rules_status"] == "EVALUATED"
    sections = data["sections"]
    assert sections["rule_table_version"] == RULE_TABLE_VERSION
    assert sections["live_provider_reads"] == "NONE_RETAINED_RECORDS_ONLY"
    assert sections["analytics_completeness"] == "BOUNDED_NON_EXHAUSTIVE"
    assert len(sections["coverage"]) == 14
    assert data["provenance"]["live_provider_reads"] == "NONE_RETAINED_RECORDS_ONLY"


def test_the_route_reports_the_unbounded_budget_finding_and_a_pause_recommendation(
    prepared: tuple[Path, str],
) -> None:
    directory, record_digest = prepared
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "audit",
            record_digest,
            "--input",
            str(_policy_file(directory)),
            "--provider-rules",
            "--limit",
            "200",
        ],
    )
    assert result.exit_code == 0, result.stdout
    sections = json.loads(result.stdout)["data"]["sections"]
    # Rule and finding identifiers are pseudonymized on the redacted route, so
    # the rules are recognized by the exact observation prose they emit.
    observations = {item["finding"]["observation"] for item in sections["findings"]}
    assert any("carries no campaign budget amount" in item for item in observations)
    assert any("opt it into the Display content network" in item for item in observations)
    assert all(item["rule_id"].startswith("opaque:") for item in sections["findings"])
    pause = [item for item in sections["recommendations"] if item["operation_kind"] == "PAUSE"]
    assert len(pause) == 1
    assert pause[0]["execution_status"] == "NOT_EXECUTED"
    assert pause[0]["plan_status"] == "NOT_A_PLAN"
    assert pause[0]["authorization_status"] == "NO_AUTHORIZATION_DIGEST_PRESENT"
    assert "plan google-ads-campaign-pause create" in pause[0]["recommendation"]["hypothesis"]
    assert pause[0]["recommendation"]["proposed_plan_ref"] is None
    # In 0.1.0 no authorization can release a write, so the prose may not promise
    # an authorization path, and the manual step must be stated plainly.
    recommendation = pause[0]["recommendation"]
    prose = (
        recommendation["hypothesis"],
        recommendation["expected_impact"],
        *recommendation["observations"],
        *recommendation["assumptions"],
    )
    assert not any("authoriz" in item.lower() for item in prose)
    assert "unreleased in 0.1.0" in recommendation["expected_impact"]
    assert "manual step in the Google Ads interface" in recommendation["expected_impact"]


def test_the_route_appends_no_record_and_makes_no_provider_request(
    prepared: tuple[Path, str],
) -> None:
    directory, record_digest = prepared
    before = len(Ledger(directory).records("changes"))
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "audit",
            record_digest,
            "--input",
            str(_policy_file(directory)),
            "--provider-rules",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert len(Ledger(directory).records("changes")) == before


def test_a_paused_account_reaches_the_same_route_without_a_pause_recommendation(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    _inventory_read(ledger, campaign_status="PAUSED")
    record_digest = _retain_report(ledger)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "audit",
            record_digest,
            "--input",
            str(_policy_file(directory)),
            "--provider-rules",
            "--limit",
            "200",
        ],
    )
    assert result.exit_code == 0, result.stdout
    sections = json.loads(result.stdout)["data"]["sections"]
    assert [item for item in sections["recommendations"] if item["operation_kind"] == "PAUSE"] == []


def test_discover_lists_the_google_rule_module_for_an_agent() -> None:
    data = json.loads(runner.invoke(app, ["discover"]).stdout)["data"]
    assert "google_ads" in data["provider_audit"]["rule_modules"]
