"""Retained provider-audit evidence selection and the CLI provider-rules route.

Every case is offline: no provider request, no credential, no network.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Money, OutputPreferences
from adhelm.reporting import LedgerCheckpoint, NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.saved_audit_evidence import load_audit_evidence
from adhelm.workflows import persist_report

runner = CliRunner()

ACCOUNT = "linkedin_ads.account.123"
GROUP = "linkedin_ads.campaign_group.900"
CAMPAIGN = "linkedin_ads.campaign.456"
CREATIVE = "linkedin_ads.creative.789"


def _snapshot(
    resource_type: str, resource_ref: str, parents: list[str], state: dict[str, Any]
) -> dict[str, Any]:
    state_digest = digest(state)
    return {
        "snapshot_id": "snapshot:" + digest({"resource": resource_ref, "state": state_digest}),
        "provider_api_version": "202608",
        "connector_version": "test",
        "resource_type": resource_type,
        "resource_ref": resource_ref,
        "parent_resource_refs": parents,
        "remote_fingerprint": state_digest,
        "normalized_state": state,
        "normalized_state_digest": state_digest,
        "effective_status": str(state["status"]),
        "source_updated_at": None,
        "data_quality_statuses": [],
    }


def _inventory_read(
    ledger: Ledger,
    *,
    campaign_status: str = "ACTIVE",
    account_ref: str = ACCOUNT,
    complete: bool = True,
) -> list[dict[str, Any]]:
    account = account_ref.rsplit(".", 1)[0]
    snapshots = [
        _snapshot("ACCOUNT", account_ref, [], {"status": "ACTIVE", "currency": "USD"}),
        _snapshot(
            "CAMPAIGN_GROUP",
            f"{account}.group.900" if account_ref != ACCOUNT else GROUP,
            [account_ref],
            {"status": "ACTIVE", "currency": "USD"},
        ),
        _snapshot(
            "CAMPAIGN",
            f"{account}.campaign.456" if account_ref != ACCOUNT else CAMPAIGN,
            [f"{account}.group.900" if account_ref != ACCOUNT else GROUP],
            {"status": campaign_status, "objective": "WEBSITE_VISIT", "currency": "USD"},
        ),
        _snapshot(
            "AD",
            f"{account}.creative.789" if account_ref != ACCOUNT else CREATIVE,
            [f"{account}.campaign.456" if account_ref != ACCOUNT else CAMPAIGN],
            {"status": "ACTIVE", "currency": "USD"},
        ),
    ]
    appended = [
        ledger.append(
            "changes",
            "RESOURCE_SNAPSHOT",
            item,
            provider="linkedin_ads",
            account_ref=account_ref,
        )
        for item in snapshots
    ]
    if complete:
        # A finished read records its own boundary, exactly as `run_inventory`
        # does. Without it the records are a prefix or legacy history.
        ledger.append(
            "changes",
            "INVENTORY_READ_COMPLETED",
            {
                "schema_version": 1,
                "provider": "linkedin_ads",
                "account_ref": account_ref,
                "snapshot_record_digests": [record["record_digest"] for record in appended],
                "resource_counts": {
                    "ACCOUNT": 1,
                    "CAMPAIGN_GROUP": 1,
                    "CAMPAIGN": 1,
                    "AD_GROUP": 0,
                    "AD": 1,
                },
                "read_started_at": "2026-09-01T11:00:00Z",
                "read_finished_at": "2026-09-01T12:00:00Z",
            },
            provider="linkedin_ads",
            account_ref=account_ref,
        )
    return appended


def _retain_report(ledger: Ledger, *, impressions: int = 100) -> str:
    checkpoint = ledger.checkpoint()
    report = NormalizedReport(
        envelope=ReportEnvelope(
            report_id="report:linkedin",
            provider="linkedin_ads",
            source_account_ref=ACCOUNT,
            query_contract_hash="c" * 64,
            window_start=date(2026, 8, 25),
            window_end=date(2026, 8, 31),
            source_timezone="UTC",
            report_timezone="UTC",
            currency="USD",
            attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
            observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            data_quality_statuses=["NOT_AVAILABLE_FROM_PROVIDER"],
            provider_api_version="202608",
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
                report_id="report:linkedin",
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="LINKEDIN_SPONSORED_CAMPAIGN",
                resource_ref=CAMPAIGN,
                impressions=impressions,
                clicks=4,
                spend=Money(minor_units=125, currency="USD"),
                join_status="EXACT_PROVIDER_ID",
                data_quality_statuses=["NOT_AVAILABLE_FROM_PROVIDER"],
                provider_extensions={
                    "linkedin_ads_cost_in_local_currency": "1.25",
                    "linkedin_ads_rounding": "HALF_EVEN_TO_MINOR",
                },
            )
        ],
    )
    return str(persist_report(ledger, report)["record_digest"])


def _policy_file(directory: Path) -> Path:
    policy = directory / "audit-policy.json"
    policy.write_text(
        json.dumps(
            {
                "maximum_observation_age_seconds": 604800,
                "require_conversion_evidence": False,
            }
        )
    )
    return policy


# --- loader -------------------------------------------------------------------


def test_loader_selects_the_latest_complete_read_and_its_predecessor(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    first = _inventory_read(ledger, campaign_status="PAUSED")
    second = _inventory_read(ledger)
    evidence, provenance = load_audit_evidence(ledger, provider="linkedin_ads")
    assert evidence.inventory is not None and evidence.earlier_inventory is not None
    assert [item.record_digest for item in evidence.inventory.records] == [
        record["record_digest"] for record in second
    ]
    assert [item.record_digest for item in evidence.earlier_inventory.records] == [
        record["record_digest"] for record in first
    ]
    assert evidence.inventory.completeness == "COMPLETE_BOUNDED_READ"
    assert provenance["retained_inventory_reads"] == 2
    assert provenance["selected_inventory_read_index"] == 1
    assert provenance["earlier_inventory_status"] == "SELECTED"
    assert provenance["live_provider_reads"] == "NONE_RETAINED_RECORDS_ONLY"
    assert provenance["checkpoint_digest"] == ledger.checkpoint()["checkpoint_digest"]


def test_loader_selects_an_explicit_read_by_record_digest(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    first = _inventory_read(ledger, campaign_status="PAUSED")
    _inventory_read(ledger)
    evidence, provenance = load_audit_evidence(
        ledger, provider="linkedin_ads", inventory_record_digest=first[2]["record_digest"]
    )
    assert provenance["selected_inventory_read_index"] == 0
    assert provenance["earlier_inventory_status"] == "NO_EARLIER_RETAINED_READ"
    assert evidence.earlier_inventory is None


def test_loader_can_omit_drift_and_the_creative_catalog(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    _inventory_read(ledger)
    _inventory_read(ledger)
    evidence, provenance = load_audit_evidence(
        ledger, provider="linkedin_ads", include_earlier=False, include_creative_catalog=False
    )
    assert evidence.earlier_inventory is None
    assert evidence.creative_catalog is None
    assert provenance["earlier_inventory_status"] == "NOT_REQUESTED"
    assert provenance["creative_catalog_status"] == "NOT_REQUESTED"


def test_loader_refuses_without_a_retained_inventory(tmp_path: Path) -> None:
    with pytest.raises(AdhelmError) as error:
        load_audit_evidence(Ledger(tmp_path), provider="linkedin_ads")
    assert error.value.code == "INVENTORY_NOT_RETAINED"
    assert error.value.exit_code == 3


def test_loader_refuses_an_unretained_record_digest(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    _inventory_read(ledger)
    with pytest.raises(AdhelmError) as error:
        load_audit_evidence(ledger, provider="linkedin_ads", inventory_record_digest="0" * 64)
    assert error.value.code == "INVENTORY_SNAPSHOT_NOT_RETAINED"
    with pytest.raises(AdhelmError) as invalid:
        load_audit_evidence(ledger, provider="linkedin_ads", inventory_record_digest="nope")
    assert invalid.value.code == "INVALID_AUDIT_EVIDENCE_SELECTION"
    assert invalid.value.exit_code == 2


def test_loader_refuses_a_read_that_is_not_a_complete_hierarchy(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.append(
        "changes",
        "RESOURCE_SNAPSHOT",
        _snapshot("CAMPAIGN", CAMPAIGN, [GROUP], {"status": "ACTIVE", "currency": "USD"}),
        provider="linkedin_ads",
        account_ref=ACCOUNT,
    )
    with pytest.raises(AdhelmError) as error:
        load_audit_evidence(ledger, provider="linkedin_ads")
    assert error.value.code == "INVENTORY_READ_INCOMPLETE"
    assert error.value.exit_code == 3


def test_loader_omits_an_incomplete_earlier_read_explicitly(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    # An earlier read whose creative names a campaign the same read never
    # returned is not a whole hierarchy and is never audited as one.
    for item in (
        _snapshot("ACCOUNT", ACCOUNT, [], {"status": "ACTIVE", "currency": "USD"}),
        _snapshot("CAMPAIGN_GROUP", GROUP, [ACCOUNT], {"status": "ACTIVE", "currency": "USD"}),
        _snapshot("CAMPAIGN", CAMPAIGN, [GROUP], {"status": "ACTIVE", "currency": "USD"}),
        _snapshot(
            "AD",
            CREATIVE,
            ["linkedin_ads.campaign.absent"],
            {"status": "ACTIVE", "currency": "USD"},
        ),
    ):
        ledger.append(
            "changes", "RESOURCE_SNAPSHOT", item, provider="linkedin_ads", account_ref=ACCOUNT
        )
    _inventory_read(ledger)
    evidence, provenance = load_audit_evidence(ledger, provider="linkedin_ads")
    assert provenance["earlier_inventory_status"] == "OMITTED_INCOMPLETE_EARLIER_READ"
    assert evidence.earlier_inventory is None


def test_loader_requires_an_account_when_history_holds_more_than_one(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    _inventory_read(ledger)
    _inventory_read(ledger, account_ref="linkedin_ads.account.999")
    with pytest.raises(AdhelmError) as error:
        load_audit_evidence(ledger, provider="linkedin_ads")
    assert error.value.code == "AUDIT_ACCOUNT_SELECTION_REQUIRED"
    assert error.value.exit_code == 2
    evidence, provenance = load_audit_evidence(ledger, provider="linkedin_ads", account_ref=ACCOUNT)
    assert provenance["account_ref"] == ACCOUNT
    assert evidence.inventory is not None
    assert len(evidence.inventory.records) == 4
    assert [
        item.snapshot.resource_ref
        for item in evidence.inventory.records
        if item.snapshot.resource_type == "ACCOUNT"
    ] == [ACCOUNT]


def test_loader_ignores_another_providers_snapshots(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.append(
        "changes",
        "RESOURCE_SNAPSHOT",
        _snapshot("ACCOUNT", "meta_ads.account.5", [], {"status": "ACTIVE", "currency": "USD"}),
        provider="meta_ads",
        account_ref="meta_ads.account.5",
    )
    _inventory_read(ledger)
    evidence, _ = load_audit_evidence(ledger, provider="linkedin_ads")
    assert evidence.inventory is not None
    assert len(evidence.inventory.records) == 4
    with pytest.raises(AdhelmError) as error:
        load_audit_evidence(ledger, provider="ga4")
    assert error.value.code == "INVALID_AUDIT_EVIDENCE_SELECTION"


# --- CLI ----------------------------------------------------------------------


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    import adhelm.credentials

    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"p" * 32)
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_rows=2)))
    ledger = Ledger(tmp_path)
    _inventory_read(ledger, campaign_status="PAUSED")
    _inventory_read(ledger)
    return _retain_report(ledger, impressions=0), _policy_file(tmp_path)


def test_provider_rules_route_emits_the_bounded_provider_audit_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_digest, policy = _prepare(tmp_path, monkeypatch)
    base = ["--data-dir", str(tmp_path)]
    command = [
        *base,
        "audit",
        record_digest,
        "--input",
        str(policy),
        "--at",
        "2026-09-01T18:00:00Z",
        "--provider-rules",
    ]
    result = runner.invoke(app, command)
    assert result.exit_code == 0, result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["command"] == "audit --provider-rules"
    assert envelope["outcome"] == "INCOMPLETE_COVERAGE"
    data = envelope["data"]
    assert data["provider"] == "linkedin_ads"
    assert data["rules_status"] == "EVALUATED"
    assert data["sections"]["live_provider_reads"] == "NONE_RETAINED_RECORDS_ONLY"
    assert data["sections"]["analytics_completeness"] == "BOUNDED_NON_EXHAUSTIVE"
    assert len(data["sections"]["coverage"]) == 14
    assert data["provenance"]["earlier_inventory_status"] == "SELECTED"
    assert data["provenance"]["record_digest"] == record_digest
    assert len(data["sections"]["findings"]) == 2
    assert data["row_counts"]["findings"] > 2
    assert envelope["next_cursor"] == "2"
    assert "provider_audit_digest" in data
    # The embedded provider-neutral audit result is still present and complete.
    assert len(data["audit"]["coverage"]) == 12


def test_provider_rules_route_paginates_without_changing_the_audit_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_digest, policy = _prepare(tmp_path, monkeypatch)
    base = [
        "--data-dir",
        str(tmp_path),
        "audit",
        record_digest,
        "--input",
        str(policy),
        "--at",
        "2026-09-01T18:00:00Z",
    ]
    first = json.loads(runner.invoke(app, [*base, "--provider-rules"]).stdout)
    second = json.loads(runner.invoke(app, [*base, "--provider-rules", "--cursor", "2"]).stdout)
    assert first["data"]["provider_audit_digest"] == second["data"]["provider_audit_digest"]
    assert first["data"]["sections"]["findings"] != second["data"]["sections"]["findings"]


def test_provider_rules_route_can_disable_drift_and_select_a_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_digest, policy = _prepare(tmp_path, monkeypatch)
    base = ["--data-dir", str(tmp_path), "audit", record_digest, "--input", str(policy)]
    result = json.loads(runner.invoke(app, [*base, "--provider-rules", "--no-drift"]).stdout)
    assert result["data"]["provenance"]["earlier_inventory_status"] == "NOT_REQUESTED"
    drift = [
        item for item in result["data"]["sections"]["coverage"] if item["dimension"] == "DRIFT"
    ]
    assert drift[0]["status"] == "NOT_READ"


def test_existing_audit_arguments_keep_their_exact_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_digest, policy = _prepare(tmp_path, monkeypatch)
    base = ["--data-dir", str(tmp_path)]
    command = [*base, "audit", record_digest, "--input", str(policy)]
    envelope = json.loads(runner.invoke(app, command).stdout)
    assert envelope["command"] == "audit"
    assert "audit_digest" in envelope["data"]
    assert "sections" not in envelope["data"]
    assert len(envelope["data"]["coverage"]) == 12


def test_inventory_selection_flags_require_the_provider_rules_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_digest, policy = _prepare(tmp_path, monkeypatch)
    command = [
        "--data-dir",
        str(tmp_path),
        "audit",
        record_digest,
        "--input",
        str(policy),
        "--account",
        ACCOUNT,
    ]
    result = runner.invoke(app, command)
    assert result.exit_code == 2
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "INVALID_AUDIT_SELECTION"


def test_provider_rules_route_refuses_without_retained_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.credentials

    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    record_digest = _retain_report(ledger)
    policy = _policy_file(tmp_path)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "audit",
            record_digest,
            "--input",
            str(policy),
            "--provider-rules",
        ],
    )
    assert result.exit_code == 3
    envelope = json.loads(result.stdout)
    assert envelope["command"] == "audit --provider-rules"
    assert envelope["diagnostics"][0]["code"] == "INVENTORY_NOT_RETAINED"


def test_provider_rules_route_appends_no_record_and_makes_no_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adhelm.transport import SafeHttpClient

    record_digest, policy = _prepare(tmp_path, monkeypatch)
    before = len(Ledger(tmp_path).records("changes")) + len(Ledger(tmp_path).records("performance"))

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("The audit route performs no request and appends no record")

    monkeypatch.setattr(SafeHttpClient, "request", _forbidden)
    monkeypatch.setattr(Ledger, "append", _forbidden)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "audit",
            record_digest,
            "--input",
            str(policy),
            "--provider-rules",
        ],
    )
    assert result.exit_code == 0, result.stdout
    monkeypatch.undo()
    after = len(Ledger(tmp_path).records("changes")) + len(Ledger(tmp_path).records("performance"))
    assert after == before


def test_discover_describes_the_provider_audit_route_truthfully() -> None:
    data = json.loads(runner.invoke(app, ["discover"]).stdout)["data"]
    assert "audit --provider-rules" in data["commands"]
    assert "provider-audit-result" in data["schemas"]
    assert "provider-audit-evidence" in data["schemas"]
    contract = data["provider_audit"]
    assert contract["live_provider_reads"] == "NONE"
    assert contract["ledger_appends"] == "NONE"
    assert contract["evidence_level"] == "FIXTURE_VERIFIED"
    assert contract["provider_ui_reconciled"] is False
    assert contract["live_verified"] is False
    assert contract["analytics_completeness"] == "BOUNDED_NON_EXHAUSTIVE"
    assert contract["read_boundary"] == "INVENTORY_READ_COMPLETED_MARKER_REQUIRED"
    # Derived from the seam's own registry, so a registered module cannot be
    # missing from the catalogue an agent discovers.
    assert contract["rule_modules"] == [
        "ga4",
        "google_ads",
        "linkedin_ads",
        "meta_ads",
        "openai_ads",
        "reddit_ads",
        "tiktok_ads",
    ]


def test_provider_rules_route_is_inert_for_a_provider_without_a_rule_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.credentials

    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    # Every advertising provider has a registered rule module now, so withdraw
    # Reddit Ads' registration (restored on teardown) to prove the route stays
    # inert for a provider without one.
    import adhelm.audits as audits

    monkeypatch.delitem(audits._REGISTERED, "reddit_ads", raising=False)
    monkeypatch.setattr(
        audits,
        "_RULE_MODULES",
        {key: value for key, value in audits._RULE_MODULES.items() if key != "reddit_ads"},
    )
    account = "reddit_ads.account.7"
    for item in (
        _snapshot("ACCOUNT", account, [], {"status": "ACTIVE", "currency": "USD"}),
        _snapshot(
            "CAMPAIGN", "reddit_ads.campaign.8", [account], {"status": "ACTIVE", "currency": "USD"}
        ),
    ):
        ledger.append(
            "changes", "RESOURCE_SNAPSHOT", item, provider="reddit_ads", account_ref=account
        )
    checkpoint = ledger.checkpoint()
    report = NormalizedReport(
        envelope=ReportEnvelope(
            report_id="report:reddit",
            provider="reddit_ads",
            source_account_ref=account,
            query_contract_hash="d" * 64,
            window_start=date(2026, 8, 25),
            window_end=date(2026, 8, 31),
            source_timezone="UTC",
            report_timezone="UTC",
            currency="USD",
            attribution_model="DECLARED_PROVIDER_MODEL",
            observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
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
        performance_rows=[],
    )
    record_digest = str(persist_report(ledger, report)["record_digest"])
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "audit",
            record_digest,
            "--input",
            str(_policy_file(tmp_path)),
            "--provider-rules",
        ],
    )
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["rules_status"] == "NO_PROVIDER_RULE_MODULE"
    assert data["sections"] is None
    assert data["row_counts"] == {"audit_findings": 13, "audit_recommendations": 1}
    # The embedded provider-neutral audit rows still page and still count.
    assert json.loads(result.stdout)["total_count"] == 13


def test_run_inventory_records_its_own_read_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished read appends the marker a later reader needs to trust it."""
    import test_inventory as harness

    import adhelm.inventory as inventory_module

    save_configuration(
        tmp_path,
        Configuration(profiles=[harness._profile("google_ads", "1234567890", google=True)]),
    )
    harness._patch_runtime(monkeypatch, harness._google_handler(), [])
    result = inventory_module.run_inventory(tmp_path, "google_ads", "google_ads-read")
    markers = [
        record
        for record in Ledger(tmp_path).records("changes")
        if record["record_kind"] == "INVENTORY_READ_COMPLETED"
    ]
    assert len(markers) == 1
    payload = markers[0]["payload"]
    assert payload["snapshot_record_digests"] == result["snapshot_record_digests"]
    assert payload["resource_counts"] == result["resource_counts"]
    assert payload["read_started_at"] <= payload["read_finished_at"]
    assert result["read_completion_record_digest"] == markers[0]["record_digest"]
    # The marker is appended after the last snapshot of the read it closes.
    sequences = {
        record["record_digest"]: record["stream_sequence"]
        for record in Ledger(tmp_path).records("changes")
    }
    assert markers[0]["stream_sequence"] > max(
        sequences[item] for item in payload["snapshot_record_digests"]
    )
    # The marker names records and counts only; the account reference it binds
    # is the same envelope field every retained record carries, and the export
    # boundary pseudonymizes it there.
    assert set(payload) == {
        "schema_version",
        "provider",
        "account_ref",
        "snapshot_record_digests",
        "resource_counts",
        "read_started_at",
        "read_finished_at",
    }


def test_an_interrupted_read_after_a_marker_is_never_selected(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    complete = _inventory_read(ledger)
    # The durable prefix an interrupted append loop leaves behind.
    ledger.append(
        "changes",
        "RESOURCE_SNAPSHOT",
        _snapshot("ACCOUNT", ACCOUNT, [], {"status": "ACTIVE", "currency": "USD"}),
        provider="linkedin_ads",
        account_ref=ACCOUNT,
    )
    evidence, provenance = load_audit_evidence(ledger, provider="linkedin_ads")
    assert evidence.inventory is not None
    assert [item.record_digest for item in evidence.inventory.records] == [
        record["record_digest"] for record in complete
    ]
    assert evidence.inventory.completeness == "COMPLETE_BOUNDED_READ"
    assert provenance["interrupted_snapshot_record_count"] == 1
    assert provenance["selected_inventory_read_boundary"] == "INVENTORY_READ_COMPLETED_MARKER"
    assert provenance["selected_inventory_completion_record_digest"] is not None


def test_history_without_a_completion_marker_is_reported_as_legacy(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    _inventory_read(ledger, complete=False)
    evidence, provenance = load_audit_evidence(ledger, provider="linkedin_ads")
    assert evidence.inventory is not None
    assert evidence.inventory.completeness == "READ_BOUNDARY_UNVERIFIED"
    assert provenance["selected_inventory_read_boundary"] == "LEGACY_READ_WITHOUT_COMPLETION_MARKER"
    assert provenance["legacy_snapshot_record_count"] == 4
    assert provenance["interrupted_snapshot_record_count"] == 0


def test_selecting_an_unfinished_read_by_digest_is_refused_precisely(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    _inventory_read(ledger)
    prefix = ledger.append(
        "changes",
        "RESOURCE_SNAPSHOT",
        _snapshot("ACCOUNT", ACCOUNT, [], {"status": "ACTIVE", "currency": "USD"}),
        provider="linkedin_ads",
        account_ref=ACCOUNT,
    )
    with pytest.raises(AdhelmError) as error:
        load_audit_evidence(
            ledger, provider="linkedin_ads", inventory_record_digest=prefix["record_digest"]
        )
    assert error.value.code == "INVENTORY_READ_INCOMPLETE"
    assert error.value.exit_code == 3


def test_the_route_pages_and_counts_every_section_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bound_exceeded` pages and counts like every other agent-visible list."""
    record_digest, policy = _prepare(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "audit",
            record_digest,
            "--input",
            str(policy),
            "--at",
            "2026-09-01T18:00:00Z",
            "--provider-rules",
        ],
    )
    assert result.exit_code == 0, result.stdout
    counts = json.loads(result.stdout)["data"]["row_counts"]
    assert set(counts) == {
        "findings",
        "recommendations",
        "no_recommendations",
        "drift_observations",
        "bound_exceeded",
        "audit_findings",
        "audit_recommendations",
    }


def test_provenance_names_both_read_boundaries(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    _inventory_read(ledger, campaign_status="PAUSED")
    _inventory_read(ledger)
    _, provenance = load_audit_evidence(ledger, provider="linkedin_ads")
    assert provenance["earlier_inventory_status"] == "SELECTED"
    assert provenance["earlier_inventory_completeness"] == "COMPLETE_BOUNDED_READ"
    assert provenance["earlier_inventory_read_boundary"] == "INVENTORY_READ_COMPLETED_MARKER"
    assert provenance["earlier_inventory_completion_record_digest"] is not None
    _, without = load_audit_evidence(ledger, provider="linkedin_ads", include_earlier=False)
    assert without["earlier_inventory_completeness"] is None
    assert without["earlier_inventory_read_boundary"] is None
