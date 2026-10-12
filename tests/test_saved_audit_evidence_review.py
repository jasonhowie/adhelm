# ruff: noqa: E501
"""Independent review of the P01-LI.2 audit evidence loader and CLI route.

Every case is offline: no provider request, no credential, no network.  Each
test states the behaviour `AGENTS.md` (rules 20 and 24) and
`docs/implementation/audit-contract.md` require of retained-record evidence
selection.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from adhelm.audit import AuditPolicy, audit_provider
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Money, OutputPreferences
from adhelm.reporting import LedgerCheckpoint, NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.saved_audit_evidence import _creative_catalog, load_audit_evidence
from adhelm.workflows import persist_report

runner = CliRunner()

ACCOUNT = "linkedin_ads.account.123"
OTHER_ACCOUNT = "linkedin_ads.account.999"
GROUP = "linkedin_ads.campaign_group.900"
CAMPAIGN = "linkedin_ads.campaign.456"
CREATIVE = "linkedin_ads.creative.789"
ACTIVE = {"status": "ACTIVE", "currency": "USD"}


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


def _append(ledger: Ledger, items: list[dict[str, Any]], account_ref: str = ACCOUNT) -> None:
    for item in items:
        ledger.append(
            "changes", "RESOURCE_SNAPSHOT", item, provider="linkedin_ads", account_ref=account_ref
        )


def _whole_read(
    account_ref: str = ACCOUNT, *, campaign_status: str = "ACTIVE"
) -> list[dict[str, Any]]:
    prefix = account_ref.rsplit(".", 1)[0]
    group = GROUP if account_ref == ACCOUNT else f"{prefix}.group.900"
    campaign = CAMPAIGN if account_ref == ACCOUNT else f"{prefix}.campaign.456"
    creative = CREATIVE if account_ref == ACCOUNT else f"{prefix}.creative.789"
    return [
        _snapshot("ACCOUNT", account_ref, [], ACTIVE),
        _snapshot("CAMPAIGN_GROUP", group, [account_ref], ACTIVE),
        _snapshot(
            "CAMPAIGN",
            campaign,
            [group],
            {"status": campaign_status, "objective": "WEBSITE_VISIT", "currency": "USD"},
        ),
        _snapshot("AD", creative, [campaign], ACTIVE),
    ]


def _interrupted_read(account_ref: str = ACCOUNT) -> list[dict[str, Any]]:
    """The prefix an interrupted `_complete_inventory` append loop leaves behind.

    `run_inventory` normalizes the whole hierarchy before appending, but
    `_complete_inventory` then appends one `RESOURCE_SNAPSHOT` at a time.  A
    process that stops between two appends leaves this durable prefix.
    """
    return _whole_read(account_ref)[:2]


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
    policy = directory / "review-audit-policy.json"
    policy.write_text(
        json.dumps(
            {"maximum_observation_age_seconds": 604800, "require_conversion_evidence": False}
        )
    )
    return policy


def _policy() -> AuditPolicy:
    return AuditPolicy(
        maximum_observation_age_seconds=604_800,
        maximum_cpc=None,
        minimum_impressions=None,
        require_conversion_evidence=False,
    )


def _report_model() -> NormalizedReport:
    return NormalizedReport(
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
        ),
        performance_rows=[],
    )


# --- Finding: an interrupted append loop becomes a "complete bounded read" ----


def test_a_truncated_tail_read_is_not_declared_a_complete_bounded_read(tmp_path: Path) -> None:
    """The last durable prefix of an interrupted read is not a complete read.

    `_split_reads` reconstructs reads by splitting on a repeated resource
    reference and `_complete_hierarchy` only checks internal consistency, so a
    read interrupted after the account and campaign-group appends passes both.
    `_inventory` then hardcodes `completeness="COMPLETE_BOUNDED_READ"`, which
    turns a truncated prefix into evidence that absence is meaningful.
    Refusing the read or declaring it `BOUNDED_NON_EXHAUSTIVE` are both honest;
    declaring it complete is not.
    """
    ledger = Ledger(tmp_path)
    _append(ledger, _whole_read())
    _append(ledger, _interrupted_read())
    try:
        evidence, provenance = load_audit_evidence(ledger, provider="linkedin_ads")
    except AdhelmError:
        return
    assert evidence.inventory is not None
    assert provenance["selected_inventory_record_count"] == 2
    assert evidence.inventory.completeness != "COMPLETE_BOUNDED_READ"


def test_a_truncated_read_does_not_become_out_of_band_deletions(tmp_path: Path) -> None:
    """A snapshot this product never appended is not an external deletion.

    `AGENTS.md` rule 24 lets reconciliation record an out-of-band change with an
    observation interval and an `UNKNOWN` actor; it never lets it invent one.
    With the latest read truncated, every resource the interrupted run had not
    reached yet is reported as `RESOURCE_ABSENT_FROM_LATER_INVENTORY`.
    """
    ledger = Ledger(tmp_path)
    _append(ledger, _whole_read())
    _append(ledger, _interrupted_read())
    evidence, _ = load_audit_evidence(ledger, provider="linkedin_ads")
    result = audit_provider(
        _report_model(),
        source_record_digest="a" * 64,
        policy=_policy(),
        as_of=datetime(2026, 9, 2, tzinfo=UTC),
        evidence=evidence,
    )
    assert result.sections is not None
    invented = [
        item.resource_ref
        for item in result.sections.drift_observations
        if item.change_kind == "RESOURCE_ABSENT_FROM_LATER_INVENTORY"
    ]
    assert invented == [], f"an unfinished read was reported as external deletions: {invented}"


def test_a_truncated_earlier_read_does_not_become_out_of_band_creations(tmp_path: Path) -> None:
    """A resource an earlier interrupted run never reached was not created externally.

    The earlier read is only screened with the same `_complete_hierarchy`
    check, so an account-plus-group prefix is selected as the drift baseline and
    every later campaign and creative is reported as newly appearing with an
    `UNKNOWN` actor.
    """
    ledger = Ledger(tmp_path)
    _append(ledger, _interrupted_read())
    _append(ledger, _whole_read())
    evidence, provenance = load_audit_evidence(ledger, provider="linkedin_ads")
    assert provenance["earlier_inventory_status"] == "SELECTED"
    result = audit_provider(
        _report_model(),
        source_record_digest="a" * 64,
        policy=_policy(),
        as_of=datetime(2026, 9, 2, tzinfo=UTC),
        evidence=evidence,
    )
    assert result.sections is not None
    invented = [
        item.resource_ref
        for item in result.sections.drift_observations
        if item.change_kind == "RESOURCE_ABSENT_FROM_EARLIER_INVENTORY"
    ]
    assert invented == [], f"an unfinished read was reported as external creations: {invented}"


# --- Finding: the evidence is not bound to the audited report's account -------


def test_provider_rules_route_refuses_evidence_from_another_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retained evidence must belong to the account the audited report describes.

    `audit_provider` checks the evidence provider but never the account, and the
    CLI passes `--account` straight through, so a report for one LinkedIn
    account is audited against another account's retained hierarchy and every
    structure, objective, and delivery finding relates resources that never
    appeared in that report.
    """
    import adhelm.credentials

    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"p" * 32)
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_rows=200)))
    ledger = Ledger(tmp_path)
    _append(ledger, _whole_read())
    _append(ledger, _whole_read(OTHER_ACCOUNT), account_ref=OTHER_ACCOUNT)
    record_digest = _retain_report(ledger, impressions=0)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "audit",
            record_digest,
            "--input",
            str(_policy_file(tmp_path)),
            "--at",
            "2026-09-01T18:00:00Z",
            "--provider-rules",
            "--account",
            OTHER_ACCOUNT,
        ],
    )
    assert result.exit_code == 2, result.stdout
    assert json.loads(result.stdout)["outcome"] == "FAILED_CLOSED"


# --- Finding: the route fails on the size of the customer's account ----------


def test_provider_rules_route_survives_seventeen_changed_campaigns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seventeen out-of-band campaign changes must produce a result, not a refusal.

    `ProviderRecommendation.finding_ids` caps at sixteen entries while the rule
    table binds every matching finding, so the route raises a pydantic
    `ValidationError` that `guarded` reports as `SCHEMA_VALIDATION_FAILED`
    ("Input does not match the installed schema"), blaming the caller's policy
    file for an internal bound.
    """
    import adhelm.credentials

    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"p" * 32)
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_rows=200)))
    ledger = Ledger(tmp_path)
    for status in ("PAUSED", "ACTIVE"):
        items = [
            _snapshot("ACCOUNT", ACCOUNT, [], ACTIVE),
            _snapshot("CAMPAIGN_GROUP", GROUP, [ACCOUNT], ACTIVE),
        ]
        items.extend(
            _snapshot(
                "CAMPAIGN",
                f"linkedin_ads.campaign.{index}",
                [GROUP],
                {"status": status, "objective": "WEBSITE_VISIT", "currency": "USD"},
            )
            for index in range(17)
        )
        _append(ledger, items)
    record_digest = _retain_report(ledger, impressions=0)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "audit",
            record_digest,
            "--input",
            str(_policy_file(tmp_path)),
            "--at",
            "2026-09-01T18:00:00Z",
            "--provider-rules",
        ],
    )
    assert result.exit_code == 0, result.stdout


# --- Finding: a closed creative association is reported as a current binding --


def test_a_closed_creative_deployment_is_not_a_current_local_binding() -> None:
    """An ended, superseded association is not a current local creative binding.

    `_creative_catalog` keys retained deployments by their content-derived
    `deployment_id`, which includes the effective interval, and ignores
    `effective_to` and `association_status`.  A replaced creative version and
    its replacement therefore both become `LOCAL_DECLARED_UNVERIFIED` bindings
    for the same ad, so the audit reports the same ad twice and treats a closed
    association as an outstanding declaration.
    """

    def _deployment(version: str, effective_to: str, association_status: str) -> dict[str, Any]:
        payload = {
            "schema_version": 2,
            "deployment_id": "deployment:sha256:" + digest({"version": version}),
            "provider": "linkedin_ads",
            "account_ref": ACCOUNT,
            "ad_ref": CREATIVE,
            "creative_family_id": "family:one",
            "creative_version_id": version,
            "rendition_refs": ["rendition:sha256:" + "d" * 64],
            "effective_from": "2026-08-01T00:00:00Z",
            "effective_to": effective_to,
            "observed_from": "2026-08-01T00:00:00Z",
            "observed_to": effective_to,
            "association_status": association_status,
        }
        return {
            "record_kind": "CREATIVE_DEPLOYMENT",
            "record_digest": digest(payload),
            "payload": payload,
        }

    records = [
        _deployment("creative:sha256:" + "a" * 64, "2026-08-20T00:00:00Z", "ENDED"),
        _deployment("creative:sha256:" + "b" * 64, "2026-09-01T00:00:00Z", "ACTIVE"),
    ]
    catalog, count = _creative_catalog(records, "linkedin_ads", ACCOUNT)
    assert count == 1, "only the current association is an outstanding local binding"
    assert [item.creative_version_id for item in catalog.entries] == ["creative:sha256:" + "b" * 64]


# --- Re-review: the drift baseline's read boundary is not in the provenance --


def _marker(ledger: Ledger, records: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for record in records:
        kind = str(record["payload"]["resource_type"])
        counts[kind] = counts.get(kind, 0) + 1
    return ledger.append(
        "changes",
        "INVENTORY_READ_COMPLETED",
        {
            "schema_version": 1,
            "provider": "linkedin_ads",
            "account_ref": ACCOUNT,
            "snapshot_record_digests": [record["record_digest"] for record in records],
            "resource_counts": counts,
            "read_started_at": "2026-09-01T12:00:00Z",
            "read_finished_at": "2026-09-01T12:00:05Z",
        },
        provider="linkedin_ads",
        account_ref=ACCOUNT,
    )


def test_provenance_names_the_read_boundary_of_the_drift_baseline(tmp_path: Path) -> None:
    """The baseline's boundary decides whether an absence means anything.

    On the ordinary upgrade path -- history retained before this product
    recorded read completions, then one marked read -- the selected read is
    `COMPLETE_BOUNDED_READ` while the drift baseline is
    `READ_BOUNDARY_UNVERIFIED`, so absence-based drift is silently dropped.
    The provenance reports `selected_inventory_completeness` and
    `selected_inventory_read_boundary` for the selected read only and says just
    `SELECTED` for the earlier one, so nothing in the envelope records that the
    comparison was narrowed.
    """
    ledger = Ledger(tmp_path)
    _append(ledger, _whole_read(campaign_status="PAUSED"))
    marked = [
        ledger.append(
            "changes", "RESOURCE_SNAPSHOT", item, provider="linkedin_ads", account_ref=ACCOUNT
        )
        for item in _whole_read()
    ]
    _marker(ledger, marked)
    evidence, provenance = load_audit_evidence(ledger, provider="linkedin_ads")
    assert evidence.earlier_inventory is not None
    assert evidence.earlier_inventory.completeness == "READ_BOUNDARY_UNVERIFIED"
    assert provenance.get("earlier_inventory_completeness") == "READ_BOUNDARY_UNVERIFIED"
    assert provenance.get("earlier_inventory_read_boundary") == (
        "LEGACY_READ_WITHOUT_COMPLETION_MARKER"
    )
