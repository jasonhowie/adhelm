# ruff: noqa: E501
"""The `audit --provider-rules` route, end to end, for OpenAI Ads.

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

# The `_RULE_MODULES` entry that would import the OpenAI rule module on demand
# is a pending dependency request against the seam; importing the module here
# performs exactly the registration that entry triggers.
from adhelm.audits.openai_ads import RULE_TABLE_VERSION, evaluate  # noqa: F401
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.ledger import Ledger
from adhelm.models import Money
from adhelm.providers.openai_ads import (
    OPENAI_ADS_AD_REVIEW_STATUS_RETURNED,
    OPENAI_ADS_CONTEXT_HINTS_DECLARED,
    OPENAI_ADS_CONVERSION_SETTINGS_NOT_ASSOCIATED,
    OPENAI_ADS_CREATIVE_BODY_PRESENT,
    OPENAI_ADS_CREATIVE_FILE_ASSOCIATED,
    OPENAI_ADS_CREATIVE_TARGET_URL_PRESENT,
    OPENAI_ADS_CREATIVE_TITLE_PRESENT,
    OPENAI_ADS_TARGETING_NOT_DECLARED,
)
from adhelm.reporting import LedgerCheckpoint, NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.saved_audit_evidence import load_audit_evidence
from adhelm.workflows import persist_report

runner = CliRunner()

# The retained OpenAI ledger records are keyed by the *native* account
# identifier: `run_inventory` appends with `profile.account_ref`, and the
# ledger's OpenAI snapshot validation requires that value to equal the account
# resource reference with its `openai_ads.account.` prefix removed.  Evidence
# selection and the audited report therefore share that native key.
NATIVE_ACCOUNT = "account_fixture"
ACCOUNT = f"openai_ads.account.{NATIVE_ACCOUNT}"
CAMPAIGN = "openai_ads.campaign.campaign_fixture"
AD_GROUP = "openai_ads.ad_group.group_fixture"
AD = "openai_ads.ad.ad_fixture"


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
        "provider_api_version": "v1",
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


def _inventory_read(ledger: Ledger, *, campaign_status: str = "active") -> list[dict[str, Any]]:
    snapshots = [
        _snapshot("ACCOUNT", ACCOUNT, [], {"status": "active", "currency": "USD"}, []),
        _snapshot(
            "CAMPAIGN",
            CAMPAIGN,
            [ACCOUNT],
            {
                "status": campaign_status,
                "currency": "USD",
                "bid_strategy": "clicks",
                "openai_ads_lifetime_spend_limit_micros": "10000000",
                "budget_type": "LIFETIME",
                "budget_minor": 1000,
                "lifetime_budget_minor": 1000,
                "schedule_end": "2026-09-30T00:00:00+00:00",
            },
            [OPENAI_ADS_TARGETING_NOT_DECLARED, OPENAI_ADS_CONVERSION_SETTINGS_NOT_ASSOCIATED],
        ),
        _snapshot(
            "AD_GROUP",
            AD_GROUP,
            [CAMPAIGN],
            {
                "status": campaign_status,
                "currency": "USD",
                "openai_ads_max_bid_micros": "250000",
                "bid_amount_minor": 25,
            },
            [OPENAI_ADS_CONTEXT_HINTS_DECLARED],
        ),
        _snapshot(
            "AD",
            AD,
            [CAMPAIGN, AD_GROUP],
            {"status": campaign_status, "currency": "USD"},
            [
                "CREATIVE_BINDING_UNVERIFIED",
                OPENAI_ADS_CREATIVE_FILE_ASSOCIATED,
                OPENAI_ADS_CREATIVE_TITLE_PRESENT,
                OPENAI_ADS_CREATIVE_BODY_PRESENT,
                OPENAI_ADS_CREATIVE_TARGET_URL_PRESENT,
                OPENAI_ADS_AD_REVIEW_STATUS_RETURNED,
            ],
        ),
    ]
    appended = [
        ledger.append(
            "changes", "RESOURCE_SNAPSHOT", item, provider="openai_ads", account_ref=NATIVE_ACCOUNT
        )
        for item in snapshots
    ]
    ledger.append(
        "changes",
        "INVENTORY_READ_COMPLETED",
        {
            "schema_version": 1,
            "provider": "openai_ads",
            "account_ref": NATIVE_ACCOUNT,
            "snapshot_record_digests": [record["record_digest"] for record in appended],
            "resource_counts": {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1},
            "read_started_at": "2026-09-01T11:00:00Z",
            "read_finished_at": "2026-09-01T12:00:00Z",
        },
        provider="openai_ads",
        account_ref=NATIVE_ACCOUNT,
    )
    return appended


def _retain_report(ledger: Ledger, *, impressions: int = 100) -> str:
    checkpoint = ledger.checkpoint()
    report = NormalizedReport(
        envelope=ReportEnvelope(
            report_id="report:openai",
            provider="openai_ads",
            source_account_ref=NATIVE_ACCOUNT,
            query_contract_hash="c" * 64,
            window_start=date(2026, 8, 25),
            window_end=date(2026, 8, 31),
            source_timezone="UTC",
            report_timezone="UTC",
            currency="USD",
            attribution_model="OPENAI_ADS_PROVIDER_REPORTED",
            observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            data_quality_statuses=["COMPLETE"],
            provider_api_version="v1",
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
                report_id="report:openai",
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="OPENAI_ADS_CAMPAIGN",
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


def test_the_loader_selects_the_retained_openai_read_for_the_reports_account(
    prepared: tuple[Path, str],
) -> None:
    directory, _digest = prepared
    evidence, provenance = load_audit_evidence(
        Ledger(directory), provider="openai_ads", account_ref=NATIVE_ACCOUNT
    )
    assert evidence.inventory is not None
    assert evidence.inventory.provider == "openai_ads"
    assert evidence.inventory.completeness == "COMPLETE_BOUNDED_READ"
    assert len(evidence.inventory.records) == 4
    assert provenance["selected_inventory_completeness"] == "COMPLETE_BOUNDED_READ"


def test_the_openai_rule_module_is_reachable_through_the_registry() -> None:
    assert provider_rule("openai_ads") is evaluate


def test_the_provider_rules_route_evaluates_the_openai_table_end_to_end(
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
    assert data["provider"] == "openai_ads"
    assert data["rules_status"] == "EVALUATED"
    sections = data["sections"]
    assert sections["rule_table_version"] == RULE_TABLE_VERSION
    assert sections["live_provider_reads"] == "NONE_RETAINED_RECORDS_ONLY"
    assert sections["analytics_completeness"] == "BOUNDED_NON_EXHAUSTIVE"
    assert len(sections["coverage"]) == 14
    assert {item["dimension"] for item in sections["coverage"]} >= {"STRUCTURE", "DRIFT"}


def test_the_route_reports_the_undeclared_targeting_finding_and_a_pause_recommendation(
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
    assert any("no non-empty targeting object" in item for item in observations)
    assert any("no associated conversion event setting" in item for item in observations)
    assert all(item["rule_id"].startswith("opaque:") for item in sections["findings"])
    pause = [item for item in sections["recommendations"] if item["operation_kind"] == "PAUSE"]
    assert len(pause) == 1
    assert pause[0]["execution_status"] == "NOT_EXECUTED"
    assert pause[0]["plan_status"] == "NOT_A_PLAN"
    assert pause[0]["authorization_status"] == "NO_AUTHORIZATION_DIGEST_PRESENT"
    assert pause[0]["recommendation"]["proposed_plan_ref"] is None
    assert pause[0]["recommendation"]["spend_impact_minor"] is None
    assert "plan openai-pause" in pause[0]["recommendation"]["hypothesis"]


def test_the_route_appends_no_record_and_makes_no_provider_request(
    prepared: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.transport as transport_module

    directory, record_digest = prepared
    before = Ledger(directory).checkpoint()
    monkeypatch.setattr(
        transport_module.SafeHttpClient,
        "request",
        lambda *_args, **_kwargs: pytest.fail("the audit route must make no request"),
    )
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
    assert Ledger(directory).checkpoint()["streams"] == before["streams"]


def test_the_route_pages_without_changing_the_provider_audit_digest(
    prepared: tuple[Path, str],
) -> None:
    directory, record_digest = prepared
    base = [
        "--data-dir",
        str(directory),
        "audit",
        record_digest,
        "--input",
        str(_policy_file(directory)),
        "--provider-rules",
        "--at",
        "2026-09-01T18:00:00+00:00",
    ]
    first = json.loads(runner.invoke(app, base).stdout)
    second = json.loads(runner.invoke(app, [*base, "--cursor", "2"]).stdout)
    assert first["data"]["provider_audit_digest"] == second["data"]["provider_audit_digest"]
    assert first["data"]["row_counts"] == second["data"]["row_counts"]


def test_drift_between_two_retained_openai_reads_is_reported_with_an_unknown_actor(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    _inventory_read(ledger, campaign_status="paused")
    _inventory_read(ledger, campaign_status="active")
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
    observations = sections["drift_observations"]
    assert observations
    for item in observations:
        assert item["observed_actor"] == "UNKNOWN"
        assert item["change_kind"] == "STATE_CHANGED"
        assert item["changed_paths"]
    coverage = {item["dimension"]: item["status"] for item in sections["coverage"]}
    assert coverage["DRIFT"] == "COVERED"


def test_a_canonical_report_reference_joins_natively_keyed_evidence_but_another_account_is_refused(
    prepared: tuple[Path, str],
) -> None:
    """The two spellings of one account join; a different account is still refused.

    Retained OpenAI inventory records are keyed by the native account identifier,
    because `run_inventory` appends with `profile.account_ref`, while
    `OpenAIAdsClient` report envelopes carry the canonical
    `openai_ads.account.<id>` reference.  The mapping is one fixed prefix, so the
    audit of a report this product retained finds the inventory it retained (a live
    read pilot depends on that), and evidence for another account is never mixed in.
    """
    from adhelm.audit import AuditPolicy, audit_provider
    from adhelm.audits import ProviderAuditEvidence
    from adhelm.errors import AdhelmError

    directory, _record_digest = prepared
    evidence, _provenance = load_audit_evidence(
        Ledger(directory), provider="openai_ads", account_ref=NATIVE_ACCOUNT
    )

    def report_for(account: str) -> NormalizedReport:
        return NormalizedReport(
            envelope=ReportEnvelope(
                report_id="report:openai-canonical",
                provider="openai_ads",
                source_account_ref=account,
                query_contract_hash="c" * 64,
                window_start=date(2026, 8, 25),
                window_end=date(2026, 8, 31),
                source_timezone="UTC",
                report_timezone="UTC",
                currency="USD",
                attribution_model="OPENAI_ADS_PROVIDER_REPORTED",
                observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
                data_quality_statuses=["COMPLETE"],
                provider_api_version="v1",
                connector_version="test",
            ),
            performance_rows=[],
        )

    def audit(account: str) -> object:
        return audit_provider(
            report_for(account),
            source_record_digest="d" * 64,
            policy=AuditPolicy(
                maximum_observation_age_seconds=604_800, require_conversion_evidence=False
            ),
            as_of=datetime(2026, 9, 1, 18, tzinfo=UTC),
            evidence=ProviderAuditEvidence(inventory=evidence.inventory),
        )

    joined = audit(ACCOUNT)
    assert joined.sections is not None
    structure = next(row for row in joined.sections.coverage if row.dimension == "STRUCTURE")
    # The natively keyed inventory was joined to the canonically named report.
    assert structure.status == "COVERED"
    assert structure.evidence_sources == ("RETAINED_INVENTORY",)
    assert structure.evidence_record_count > 0
    with pytest.raises(AdhelmError) as error:
        audit("openai_ads.account.another_account")
    assert error.value.code == "INVALID_AUDIT_INPUT"


def test_an_explicit_account_selection_reaches_the_same_retained_read(
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
            NATIVE_ACCOUNT,
            "--no-drift",
        ],
    )
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["rules_status"] == "EVALUATED"
    coverage = {item["dimension"]: item["status"] for item in data["sections"]["coverage"]}
    assert coverage["DRIFT"] == "NOT_READ"
