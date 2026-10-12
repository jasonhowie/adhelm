# ruff: noqa: E501
"""The `audit --provider-rules` route, end to end, for Meta Ads.

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
from adhelm.audits.meta_ads import RULE_TABLE_VERSION, evaluate
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.ledger import Ledger
from adhelm.models import Money
from adhelm.providers.meta_ads import (
    META_ADS_BID_STRATEGY_RETURNED,
    META_ADS_BUDGET_MODE_DAILY,
    META_ADS_BUDGET_MODE_NOT_RETURNED,
    META_ADS_BUYING_TYPE_RETURNED,
    META_ADS_CREATIVE_ASSOCIATED,
    META_ADS_CREATIVE_IMAGE_HASH_PRESENT,
    META_ADS_CREATIVE_LINK_ABSENT,
    META_ADS_OBJECTIVE_RETURNED,
    META_ADS_SCHEDULE_END_ABSENT,
    META_ADS_SCHEDULE_END_PRESENT,
    META_ADS_TARGETING_AGE_PRESENT,
    META_ADS_TARGETING_DECLARED,
    META_ADS_TARGETING_GEO_ABSENT,
    META_ADS_TARGETING_INTEREST_PRESENT,
)
from adhelm.reporting import LedgerCheckpoint, NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.saved_audit_evidence import load_audit_evidence
from adhelm.workflows import persist_report

runner = CliRunner()

# Retained Meta ledger records are keyed by the *native* account identifier:
# `run_inventory` appends with `profile.account_ref`, which a
# `MetaAccountTokenProfile` declares as the bare numeric ad-account ID.  A
# `MetaAdsClient` report envelope instead carries the canonical
# `meta_ads.account.act_<id>` reference.  Evidence selection and the audited
# report therefore share the native key here, and the mismatch is pinned below.
NATIVE_ACCOUNT = "123"
ACCOUNT = "meta_ads.account.act_123"
CAMPAIGN = "meta_ads.campaign.11"
AD_SET = "meta_ads.adset.12"
AD = "meta_ads.ad.13"


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
        "provider_api_version": "v26.0",
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


def _inventory_read(ledger: Ledger, *, status: str = "ACTIVE") -> list[dict[str, Any]]:
    snapshots = [
        _snapshot("ACCOUNT", ACCOUNT, [], {"status": "1", "currency": "USD"}, []),
        _snapshot(
            "CAMPAIGN",
            CAMPAIGN,
            [ACCOUNT],
            {
                "status": status,
                "currency": "USD",
                "objective": "OUTCOME_TRAFFIC",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "budget_minor": 5_000,
                "budget_type": "DAILY",
            },
            [
                META_ADS_OBJECTIVE_RETURNED,
                META_ADS_BUYING_TYPE_RETURNED,
                META_ADS_BID_STRATEGY_RETURNED,
                META_ADS_BUDGET_MODE_DAILY,
                # No stop time: the campaign-scoped, serving-guarded rule that
                # the `PAUSE` row rests on.
                META_ADS_SCHEDULE_END_ABSENT,
            ],
        ),
        _snapshot(
            "AD_GROUP",
            AD_SET,
            [CAMPAIGN],
            {
                "status": status,
                "currency": "USD",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "schedule_end": "2026-09-30T00:00:00+00:00",
            },
            [
                META_ADS_TARGETING_DECLARED,
                META_ADS_TARGETING_GEO_ABSENT,
                META_ADS_TARGETING_AGE_PRESENT,
                META_ADS_TARGETING_INTEREST_PRESENT,
                META_ADS_BUDGET_MODE_NOT_RETURNED,
                META_ADS_SCHEDULE_END_PRESENT,
                META_ADS_BID_STRATEGY_RETURNED,
            ],
        ),
        _snapshot(
            "AD",
            AD,
            [CAMPAIGN, AD_SET],
            {"status": status, "currency": "USD"},
            [
                META_ADS_CREATIVE_ASSOCIATED,
                META_ADS_CREATIVE_IMAGE_HASH_PRESENT,
                META_ADS_CREATIVE_LINK_ABSENT,
                "META_AD_REVIEW_NOT_FLAGGED",
            ],
        ),
    ]
    appended = [
        ledger.append(
            "changes", "RESOURCE_SNAPSHOT", item, provider="meta_ads", account_ref=NATIVE_ACCOUNT
        )
        for item in snapshots
    ]
    ledger.append(
        "changes",
        "INVENTORY_READ_COMPLETED",
        {
            "schema_version": 1,
            "provider": "meta_ads",
            "account_ref": NATIVE_ACCOUNT,
            "snapshot_record_digests": [record["record_digest"] for record in appended],
            "resource_counts": {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1},
            "read_started_at": "2026-09-01T11:00:00Z",
            "read_finished_at": "2026-09-01T12:00:00Z",
        },
        provider="meta_ads",
        account_ref=NATIVE_ACCOUNT,
    )
    return appended


def _retain_report(ledger: Ledger, *, impressions: int = 100) -> str:
    checkpoint = ledger.checkpoint()
    report = NormalizedReport(
        envelope=ReportEnvelope(
            report_id="report:meta",
            provider="meta_ads",
            source_account_ref=NATIVE_ACCOUNT,
            query_contract_hash="c" * 64,
            window_start=date(2026, 8, 25),
            window_end=date(2026, 8, 31),
            source_timezone="UTC",
            report_timezone="UTC",
            currency="USD",
            attribution_model="META_ACTION_ATTRIBUTION_WINDOWS",
            attribution_window="7d_click,1d_view",
            observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            data_fresh_through=None,
            data_quality_statuses=["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"],
            provider_api_version="v26.0",
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
                report_id="report:meta",
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="META_ADS_CAMPAIGN",
                resource_ref=CAMPAIGN,
                campaign_ref=CAMPAIGN,
                dimension_type="DATE",
                dimension_value="2026-08-25",
                impressions=impressions,
                clicks=4,
                reach=80,
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


def test_the_loader_selects_the_retained_meta_read_for_the_reports_account(
    prepared: tuple[Path, str],
) -> None:
    directory, _digest = prepared
    evidence, provenance = load_audit_evidence(
        Ledger(directory), provider="meta_ads", account_ref=NATIVE_ACCOUNT
    )
    assert evidence.inventory is not None
    assert evidence.inventory.provider == "meta_ads"
    assert evidence.inventory.completeness == "COMPLETE_BOUNDED_READ"
    assert len(evidence.inventory.records) == 4
    assert provenance["selected_inventory_completeness"] == "COMPLETE_BOUNDED_READ"


def test_the_meta_rule_module_is_reachable_through_the_registry() -> None:
    assert provider_rule("meta_ads") is evaluate


def test_the_provider_rules_route_evaluates_the_meta_table_end_to_end(
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
    assert data["provider"] == "meta_ads"
    assert data["rules_status"] == "EVALUATED"
    sections = data["sections"]
    assert sections["rule_table_version"] == RULE_TABLE_VERSION
    assert sections["live_provider_reads"] == "NONE_RETAINED_RECORDS_ONLY"
    assert sections["analytics_completeness"] == "BOUNDED_NON_EXHAUSTIVE"
    assert len(sections["coverage"]) == 14
    coverage = {item["dimension"]: item["status"] for item in sections["coverage"]}
    assert coverage["STRUCTURE"] == "COVERED"
    assert coverage["PLACEMENT"] == "NOT_READ"
    assert coverage["BROWSER_MEASUREMENT"] == "NOT_APPLICABLE"


def test_the_route_reports_the_meta_findings_and_a_campaign_pause_recommendation(
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
    assert any("declared no geography" in item for item in observations)
    assert any("neither of the two documented link fields" in item for item in observations)
    assert any("carries no retained stop time" in item for item in observations)
    assert all(item["rule_id"].startswith("opaque:") for item in sections["findings"])
    pause = [item for item in sections["recommendations"] if item["operation_kind"] == "PAUSE"]
    assert len(pause) == 1
    assert pause[0]["execution_status"] == "NOT_EXECUTED"
    assert pause[0]["plan_status"] == "NOT_A_PLAN"
    assert pause[0]["authorization_status"] == "NO_AUTHORIZATION_DIGEST_PRESENT"
    assert pause[0]["recommendation"]["proposed_plan_ref"] is None
    assert pause[0]["recommendation"]["spend_impact_minor"] is None
    assert "plan meta-campaign-pause create" in pause[0]["recommendation"]["hypothesis"]
    # No retained targeting, creative, or destination value reaches the output.
    body = result.stdout
    for value in ("geo_locations", "image_hash", "object_url", "link_url"):
        assert value not in body


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


def test_drift_between_two_retained_meta_reads_is_reported_with_an_unknown_actor(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    _inventory_read(ledger, status="PAUSED")
    _inventory_read(ledger, status="ACTIVE")
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


def _report_for(account_ref: str) -> NormalizedReport:
    return NormalizedReport(
        envelope=ReportEnvelope(
            report_id="report:meta-canonical",
            provider="meta_ads",
            source_account_ref=account_ref,
            query_contract_hash="c" * 64,
            window_start=date(2026, 8, 25),
            window_end=date(2026, 8, 31),
            source_timezone="UTC",
            report_timezone="UTC",
            currency="USD",
            attribution_model="META_ACTION_ATTRIBUTION_WINDOWS",
            attribution_window="7d_click",
            observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            data_quality_statuses=["COMPLETE"],
            provider_api_version="v26.0",
            connector_version="test",
        ),
        performance_rows=[],
    )


def test_a_canonical_report_reference_reaches_its_own_natively_keyed_evidence(
    prepared: tuple[Path, str],
) -> None:
    """One Meta account is written two ways, and both name the same account.

    Retained Meta inventory records are keyed by the native ad-account
    identifier, while `MetaAdsClient` report envelopes carry the canonical
    `meta_ads.account.act_<id>` reference.  Both are reduced to the same
    retained key before they are compared, so a report this build produced
    reaches the inventory this build retained for it.
    """
    from adhelm.audit import AuditPolicy, audit_provider
    from adhelm.audits import ProviderAuditEvidence

    directory, _record_digest = prepared
    evidence, _provenance = load_audit_evidence(
        Ledger(directory), provider="meta_ads", account_ref=NATIVE_ACCOUNT
    )
    result = audit_provider(
        _report_for(ACCOUNT),
        source_record_digest="d" * 64,
        policy=AuditPolicy(
            maximum_observation_age_seconds=604_800, require_conversion_evidence=False
        ),
        as_of=datetime(2026, 9, 1, 18, tzinfo=UTC),
        evidence=ProviderAuditEvidence(inventory=evidence.inventory),
    )
    assert result.rules_status == "EVALUATED"
    assert result.sections is not None


def test_evidence_for_a_genuinely_different_account_is_refused_rather_than_mixed(
    prepared: tuple[Path, str],
) -> None:
    """Normalizing two spellings of one account never merges two accounts.

    The typed refusal still fires for evidence that belongs to a different ad
    account, in either spelling.
    """
    from adhelm.audit import AuditPolicy, audit_provider
    from adhelm.audits import ProviderAuditEvidence
    from adhelm.errors import AdhelmError

    directory, _record_digest = prepared
    evidence, _provenance = load_audit_evidence(
        Ledger(directory), provider="meta_ads", account_ref=NATIVE_ACCOUNT
    )
    for other in ("meta_ads.account.act_999", "999"):
        with pytest.raises(AdhelmError) as error:
            audit_provider(
                _report_for(other),
                source_record_digest="d" * 64,
                policy=AuditPolicy(
                    maximum_observation_age_seconds=604_800, require_conversion_evidence=False
                ),
                as_of=datetime(2026, 9, 1, 18, tzinfo=UTC),
                evidence=ProviderAuditEvidence(inventory=evidence.inventory),
            )
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


def _second_read(ledger: Ledger, account_ref: str, *, status: str) -> None:
    """One more completed read for the same account, keyed by `account_ref`."""
    snapshots = [
        _snapshot("ACCOUNT", ACCOUNT, [], {"status": "1", "currency": "USD"}, []),
        _snapshot("CAMPAIGN", CAMPAIGN, [ACCOUNT], {"status": status, "currency": "USD"}, []),
        _snapshot("AD_GROUP", AD_SET, [CAMPAIGN], {"status": status, "currency": "USD"}, []),
        _snapshot("AD", AD, [CAMPAIGN, AD_SET], {"status": status}, []),
    ]
    appended = [
        ledger.append(
            "changes", "RESOURCE_SNAPSHOT", item, provider="meta_ads", account_ref=account_ref
        )
        for item in snapshots
    ]
    ledger.append(
        "changes",
        "INVENTORY_READ_COMPLETED",
        {
            "schema_version": 1,
            "provider": "meta_ads",
            "account_ref": account_ref,
            "snapshot_record_digests": [record["record_digest"] for record in appended],
            "resource_counts": {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1},
            "read_started_at": "2026-09-02T11:00:00Z",
            "read_finished_at": "2026-09-02T12:00:00Z",
        },
        provider="meta_ads",
        account_ref=account_ref,
    )


def test_reads_retained_under_both_spellings_are_joined_and_named_in_provenance(
    prepared: tuple[Path, str],
) -> None:
    """Two spellings of one account are one account, and every read counts.

    `run_inventory` keys by `profile.account_ref`: a `MetaAccountTokenProfile`
    constrains that to the bare numeric identifier, while a generic `Profile`
    for `meta_ads` accepts the `act_<id>` form.  One account can therefore hold
    completed reads under both, and excluding one spelling's reads would audit
    a stale hierarchy as current while under-counting the reads.
    """
    directory, _record_digest = prepared
    ledger = Ledger(directory)
    _second_read(ledger, ACCOUNT, status="PAUSED")

    evidence, provenance = load_audit_evidence(Ledger(directory), provider="meta_ads")
    assert provenance["retained_inventory_reads"] == 2
    assert provenance["retained_account_spellings"] == sorted([ACCOUNT, NATIVE_ACCOUNT])
    assert evidence.inventory is not None
    # The newest completed read wins, whichever spelling wrote it.
    assert [
        record.snapshot.effective_status
        for record in evidence.inventory.records
        if record.snapshot.resource_type == "CAMPAIGN"
    ] == ["PAUSED"]
    # The earlier read is the baseline, so drift is comparable across spellings.
    assert evidence.earlier_inventory is not None
    assert provenance["earlier_inventory_status"] == "SELECTED"


def test_either_spelling_selects_the_same_joined_reads(prepared: tuple[Path, str]) -> None:
    directory, _record_digest = prepared
    _second_read(Ledger(directory), ACCOUNT, status="PAUSED")
    by_native, native_provenance = load_audit_evidence(
        Ledger(directory), provider="meta_ads", account_ref=NATIVE_ACCOUNT
    )
    by_canonical, canonical_provenance = load_audit_evidence(
        Ledger(directory), provider="meta_ads", account_ref=ACCOUNT
    )
    # The selection is deterministic: the same ledger yields the same evidence
    # and the same provenance whichever spelling the caller typed.
    assert by_native.inventory == by_canonical.inventory
    assert native_provenance == canonical_provenance
    for provenance in (native_provenance, canonical_provenance):
        assert provenance["retained_inventory_reads"] == 2
        assert provenance["retained_account_spellings"] == sorted([ACCOUNT, NATIVE_ACCOUNT])


def test_two_genuinely_different_meta_accounts_still_require_an_explicit_selection(
    prepared: tuple[Path, str],
) -> None:
    """Joining two spellings never joins two accounts."""
    from adhelm.errors import AdhelmError

    directory, _record_digest = prepared
    _second_read(Ledger(directory), "meta_ads.account.act_999", status="PAUSED")
    with pytest.raises(AdhelmError) as raised:
        load_audit_evidence(Ledger(directory), provider="meta_ads")
    assert raised.value.code == "AUDIT_ACCOUNT_SELECTION_REQUIRED"


@pytest.mark.parametrize("reference", ["meta_ads.account.act_", "act_", "meta_ads.account."])
def test_a_meta_account_reference_that_names_no_account_is_refused(reference: str) -> None:
    """An empty derived key would compare equal to every other empty key."""
    from adhelm.errors import AdhelmError
    from adhelm.saved_audit_evidence import retained_account_key

    with pytest.raises(AdhelmError) as raised:
        retained_account_key("meta_ads", reference)
    assert raised.value.code == "INVALID_AUDIT_EVIDENCE_SELECTION"
    assert raised.value.exit_code == 2
    # No other provider is normalized, so no other provider can produce one.
    assert retained_account_key("openai_ads", reference) == reference
