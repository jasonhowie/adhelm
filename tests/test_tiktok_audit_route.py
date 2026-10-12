"""The `audit --provider-rules` route, end to end, for TikTok Ads.

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
from adhelm.audits.tiktok_ads import RULE_TABLE_VERSION, evaluate
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Money
from adhelm.providers.tiktok_ads import (
    TIKTOK_ADS_PAGINATION_TOTAL_DECLARED,
    TIKTOK_ADS_STATE_SHAPE_STATUS,
)
from adhelm.reporting import LedgerCheckpoint, NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.saved_audit_evidence import load_audit_evidence, retained_account_key
from adhelm.workflows import persist_report

runner = CliRunner()

# Retained TikTok ledger records are keyed by the *native* advertiser
# identifier: `run_inventory` appends with `profile.account_ref`, which a TikTok
# profile declares as the bare decimal advertiser ID.  A `TikTokAdsClient` report
# envelope instead carries the canonical `tiktok_ads.advertiser.<id>` reference.
# Both spellings name one advertiser, and the join is pinned below.
NATIVE_ACCOUNT = "123"
ACCOUNT = "tiktok_ads.advertiser.123"
CAMPAIGN = "tiktok_ads.campaign.11"
AD_GROUP = "tiktok_ads.adgroup.12"
AD = "tiktok_ads.ad.13"


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
        "provider_api_version": "v1.3",
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


def _inventory_read(ledger: Ledger, *, status: str = "ENABLE") -> list[dict[str, Any]]:
    snapshots = [
        _snapshot(
            "ACCOUNT",
            ACCOUNT,
            [],
            {"status": "STATUS_ENABLE", "currency": "USD"},
            [TIKTOK_ADS_STATE_SHAPE_STATUS, TIKTOK_ADS_PAGINATION_TOTAL_DECLARED],
        ),
        _snapshot(
            "CAMPAIGN",
            CAMPAIGN,
            [ACCOUNT],
            {
                "status": status,
                "currency": "USD",
                "objective": "TRAFFIC",
                "tiktok_ads_budget_mode": "BUDGET_MODE_INFINITE",
            },
            [
                TIKTOK_ADS_STATE_SHAPE_STATUS,
                "TIKTOK_OBJECTIVE_RETURNED",
                # The documented unlimited mode: the rule the OBSERVE row rests on.
                "TIKTOK_BUDGET_MODE_INFINITE",
                "TIKTOK_BUDGET_AMOUNT_ABSENT",
                "TIKTOK_OPERATION_STATUS_REVIEWED",
            ],
        ),
        _snapshot(
            "AD_GROUP",
            AD_GROUP,
            [CAMPAIGN],
            {
                "status": status,
                "currency": "USD",
                "bid_strategy": "BID_TYPE_NO_BID",
                "tiktok_ads_budget_mode": "BUDGET_MODE_DAY",
                "budget_minor": 5_000,
                "budget_type": "DAILY",
            },
            [
                TIKTOK_ADS_STATE_SHAPE_STATUS,
                "TIKTOK_BUDGET_MODE_DAY",
                "TIKTOK_BUDGET_AMOUNT_PRESENT",
                "TIKTOK_BID_TYPE_RETURNED",
                "TIKTOK_BID_PRICE_ABSENT",
                "TIKTOK_OPERATION_STATUS_REVIEWED",
            ],
        ),
        _snapshot(
            "AD",
            AD,
            [CAMPAIGN, AD_GROUP],
            {"status": status, "currency": "USD"},
            [
                "CREATIVE_BINDING_NOT_AVAILABLE_FROM_PROVIDER",
                TIKTOK_ADS_STATE_SHAPE_STATUS,
                "TIKTOK_OPERATION_STATUS_REVIEWED",
            ],
        ),
    ]
    appended = [
        ledger.append(
            "changes", "RESOURCE_SNAPSHOT", item, provider="tiktok_ads", account_ref=NATIVE_ACCOUNT
        )
        for item in snapshots
    ]
    ledger.append(
        "changes",
        "INVENTORY_READ_COMPLETED",
        {
            "schema_version": 1,
            "provider": "tiktok_ads",
            "account_ref": NATIVE_ACCOUNT,
            "snapshot_record_digests": [record["record_digest"] for record in appended],
            "resource_counts": {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1},
            "read_started_at": "2026-09-01T11:00:00Z",
            "read_finished_at": "2026-09-01T12:00:00Z",
        },
        provider="tiktok_ads",
        account_ref=NATIVE_ACCOUNT,
    )
    return appended


def _retain_report(ledger: Ledger, *, impressions: int = 100) -> str:
    checkpoint = ledger.checkpoint()
    report = NormalizedReport(
        envelope=ReportEnvelope(
            report_id="report:tiktok",
            provider="tiktok_ads",
            # The canonical connector spelling, exactly as `TikTokAdsClient`
            # produces it.  The retained records above use the native one.
            source_account_ref=ACCOUNT,
            query_contract_hash="c" * 64,
            window_start=date(2026, 8, 25),
            window_end=date(2026, 8, 31),
            source_timezone="UTC",
            report_timezone="UTC",
            currency="USD",
            attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
            attribution_window=None,
            observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            data_fresh_through=None,
            data_quality_statuses=["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"],
            provider_api_version="v1.3",
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
                report_id="report:tiktok",
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="TIKTOK_ADS_CAMPAIGN",
                resource_ref=CAMPAIGN,
                campaign_ref=CAMPAIGN,
                dimension_type="DATE",
                dimension_value="2026-08-25",
                impressions=impressions,
                clicks=4,
                spend=Money(minor_units=125, currency="USD"),
                data_quality_statuses=["COMPLETE"],
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


def test_the_tiktok_rule_module_is_reachable_through_the_registry() -> None:
    assert provider_rule("tiktok_ads") is evaluate


def test_two_spellings_of_one_advertiser_reduce_to_the_same_retained_key() -> None:
    assert retained_account_key("tiktok_ads", ACCOUNT) == NATIVE_ACCOUNT
    assert retained_account_key("tiktok_ads", NATIVE_ACCOUNT) == NATIVE_ACCOUNT
    # A genuinely different advertiser still differs and is still refused.
    assert retained_account_key("tiktok_ads", "tiktok_ads.advertiser.456") != NATIVE_ACCOUNT
    # A reference that reduces to nothing is refused rather than joining
    # everything.
    with pytest.raises(AdhelmError) as error:
        retained_account_key("tiktok_ads", "tiktok_ads.advertiser.")
    assert error.value.code == "INVALID_AUDIT_EVIDENCE_SELECTION"


def test_the_loader_selects_the_retained_tiktok_read_for_the_reports_account(
    prepared: tuple[Path, str],
) -> None:
    directory, _digest = prepared

    evidence, provenance = load_audit_evidence(
        Ledger(directory), provider="tiktok_ads", account_ref=ACCOUNT
    )

    assert evidence.inventory is not None
    assert evidence.inventory.provider == "tiktok_ads"
    assert evidence.inventory.completeness == "COMPLETE_BOUNDED_READ"
    assert len(evidence.inventory.records) == 4
    assert provenance["selected_inventory_completeness"] == "COMPLETE_BOUNDED_READ"


def test_the_provider_rules_route_evaluates_the_tiktok_table_end_to_end(
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
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["provider"] == "tiktok_ads"
    assert payload["rules_status"] == "EVALUATED"
    sections = payload["sections"]
    assert sections["rule_table_version"] == RULE_TABLE_VERSION
    assert sections["live_provider_reads"] == "NONE_RETAINED_RECORDS_ONLY"
    assert sections["analytics_completeness"] == "BOUNDED_NON_EXHAUSTIVE"
    assert len(sections["coverage"]) == 14
    assert payload["row_counts"]["findings"] >= 1
    # Every recommendation the TikTok table can emit is a re-read.
    assert {item["operation_kind"] for item in sections["recommendations"]} <= {"OBSERVE"}
    assert sections["no_recommendations"]


def test_the_route_refuses_evidence_bound_to_another_advertiser(
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
            "--account",
            "456",
        ],
    )

    # There is no retained read for advertiser 456, so the route fails closed
    # rather than auditing this report against another advertiser's records.
    assert result.exit_code != 0
    assert '"outcome":"FAILED_CLOSED"' in result.output
    assert '"data":{}' in result.output


def test_the_seam_refuses_retained_evidence_bound_to_another_advertiser(
    prepared: tuple[Path, str],
) -> None:
    from adhelm.audit import AuditPolicy, audit_provider
    from adhelm.audits import ProviderAuditEvidence

    directory, record_digest = prepared
    evidence, _provenance = load_audit_evidence(
        Ledger(directory), provider="tiktok_ads", account_ref=ACCOUNT
    )
    assert evidence.inventory is not None
    other = evidence.inventory.model_copy(update={"account_ref": "tiktok_ads.advertiser.456"})
    from adhelm.saved_reports import load_report

    report, _provenance_two = load_report(Ledger(directory), record_digest)

    with pytest.raises(AdhelmError) as error:
        audit_provider(
            report,
            source_record_digest=record_digest,
            policy=AuditPolicy(
                maximum_observation_age_seconds=604800, require_conversion_evidence=False
            ),
            as_of=datetime(2026, 9, 2, 12, tzinfo=UTC),
            evidence=ProviderAuditEvidence(inventory=other),
        )

    assert error.value.code == "INVALID_AUDIT_INPUT"
    assert error.value.exit_code == 2


def test_the_route_appends_no_ledger_record_and_makes_no_request(
    prepared: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    directory, record_digest = prepared
    before = len(list(Ledger(directory).records("changes")))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the provider-rules route must make no request")

    monkeypatch.setattr(httpx, "Client", forbidden)
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

    assert result.exit_code == 0, result.output
    assert len(list(Ledger(directory).records("changes"))) == before


def test_the_route_result_is_deterministic(prepared: tuple[Path, str]) -> None:
    directory, record_digest = prepared
    arguments = [
        "--data-dir",
        str(directory),
        "audit",
        record_digest,
        "--input",
        str(_policy_file(directory)),
        "--provider-rules",
        "--at",
        "2026-09-02T12:00:00+00:00",
    ]

    first = runner.invoke(app, arguments)
    second = runner.invoke(app, arguments)

    assert first.exit_code == 0, first.output
    assert first.output == second.output
    assert json.loads(first.output)["data"]["provider_audit_digest"]


def test_drift_between_two_retained_reads_reaches_the_route(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    _inventory_read(ledger, status="DISABLE")
    _inventory_read(ledger, status="ENABLE")
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
        ],
    )

    assert result.exit_code == 0, result.output
    sections = json.loads(result.output)["data"]["sections"]
    assert sections["drift_observations"]
    for observation in sections["drift_observations"]:
        assert observation["observed_actor"] == "UNKNOWN"


def test_disabling_drift_selection_drops_only_the_earlier_read(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    _inventory_read(ledger, status="DISABLE")
    _inventory_read(ledger, status="ENABLE")
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
            "--no-drift",
        ],
    )

    assert result.exit_code == 0, result.output
    sections = json.loads(result.output)["data"]["sections"]
    assert sections["drift_observations"] == []
    drift = next(item for item in sections["coverage"] if item["dimension"] == "DRIFT")
    assert drift["status"] == "NOT_READ"
