"""The `audit --provider-rules` route and evidence selection, end to end, for GA4.

Every case is offline: retained ledger records only, no provider request, no
credential, no network.  Reports are produced by the real Data API adapter over
an in-memory ``httpx.MockTransport`` and retained through the ordinary report
persistence, and Admin observations through the ordinary measurement retention.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from adhelm.cli import app
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_records import Ga4MeasurementSnapshot
from adhelm.providers.ga4 import Ga4AggregateQuery, Ga4DataClient, Ga4Identity
from adhelm.providers.ga4_admin import (
    Ga4AdminEventRule,
    Ga4AdminIdentity,
    Ga4AdminInventory,
    Ga4AdminKeyEvent,
)
from adhelm.reporting import LedgerCheckpoint
from adhelm.saved_audit_evidence import load_audit_evidence
from adhelm.saved_ga4_audit_evidence import load_ga4_audit_evidence
from adhelm.saved_measurements import retain_measurement
from adhelm.transport import SafeHttpClient
from adhelm.workflows import persist_report

runner = CliRunner()

ZONE = "America/Chicago"
PROPERTY = "ga4.property.123"
OBSERVED = datetime(2026, 9, 4, 12, tzinfo=UTC)
FIRST_READ = datetime(2026, 9, 1, 12, tzinfo=UTC)
SECOND_READ = datetime(2026, 9, 3, 12, tzinfo=UTC)
AUDIT_AT = "2026-09-05T12:00:00+00:00"


def _key_event(resource_id: int, counting_method: str = "ONCE_PER_EVENT") -> Ga4AdminKeyEvent:
    return Ga4AdminKeyEvent(
        resource_ref=f"ga4_admin.key_event.{resource_id}",
        resource_client_ref=str(resource_id),
        property_ref="ga4_admin.property.123",
        configuration_fingerprint=format(resource_id, "064x"),
        event_name_fingerprint=format(resource_id + 1_000, "064x"),
        counting_method=counting_method,  # type: ignore[arg-type]
        custom=True,
        deletable=True,
    )


def _retain_admin(
    ledger: Ledger,
    *,
    property_id: str = "123",
    key_events: int = 2,
    counting_method: str = "ONCE_PER_EVENT",
    finished: datetime = SECOND_READ,
    with_edit_rule: bool = False,
) -> str:
    identity = Ga4AdminIdentity(
        property_ref=f"ga4_admin.property.{property_id}",
        property_client_ref=property_id,
        web_stream_ref="ga4_admin.web_stream.456",
        web_stream_client_ref="456",
    )
    rules = (
        (
            Ga4AdminEventRule(
                resource_ref="ga4_admin.event_edit_rule.8",
                resource_client_ref="8",
                property_ref=identity.property_ref,
                web_stream_ref=identity.web_stream_ref,
                rule_kind="EVENT_EDIT_RULE",
                configuration_fingerprint=format(2_008, "064x"),
                condition_count=1,
                parameter_mutation_count=1,
                processing_order="8",
            ),
        )
        if with_edit_rule
        else ()
    )
    snapshot = Ga4MeasurementSnapshot(
        inventory=Ga4AdminInventory(
            identity=identity,
            key_events=tuple(
                _key_event(index, counting_method).model_copy(
                    update={"property_ref": identity.property_ref}
                )
                for index in range(1, key_events + 1)
            ),
            event_edit_rules=rules,
        )
    )
    record = retain_measurement(
        ledger,
        snapshot,
        profile_ref="profile.ga4",
        read_started_at=finished - timedelta(seconds=5),
        read_finished_at=finished,
    )
    return str(record["record_digest"])


def _retain_report(ledger: Ledger, *, sampled: bool = False) -> str:
    def handler(_request: httpx.Request) -> httpx.Response:
        metadata: dict[str, Any] = {"timeZone": ZONE, "currencyCode": "USD"}
        if sampled:
            metadata["samplingMetadatas"] = [{"samplesReadCount": "10", "samplingSpaceSize": "100"}]
        return httpx.Response(
            200,
            json={
                "dimensionHeaders": [{"name": "eventName"}],
                "metricHeaders": [{"name": "keyEvents"}],
                "rows": [
                    {"dimensionValues": [{"value": "purchase"}], "metricValues": [{"value": "5"}]}
                ],
                "rowCount": 1,
                "metadata": metadata,
            },
        )

    client = Ga4DataClient(
        SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
            {"analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"},
            read_attempts=1,
        ),
        lambda: "test-token-not-to-be-persisted",
        now=lambda: OBSERVED,
        connector_version="test",
    )
    report = client.run_aggregate_report(
        Ga4AggregateQuery(
            aggregate_shape="KEY_EVENT_TOTALS",
            property_id="123",
            account_ref=PROPERTY,
            report_id="report.ga4",
            query_contract_hash="c" * 64,
            window_start=date(2026, 8, 28),
            window_end=date(2026, 9, 3),
            key_event_names=("generate_lead", "purchase"),
            source_timezone=ZONE,
            currency="USD",
        ),
        identity=Ga4Identity(
            property_ref=PROPERTY,
            property_client_ref="123",
            web_stream_ref="ga4.stream.456",
            web_stream_client_ref="456",
            timezone=ZONE,
            currency="USD",
        ),
    )
    normalized = report.to_normalized_report()
    # The ledger binds every retained report to the complete checkpoint vector it was read at.
    checkpoint = ledger.checkpoint()
    envelope = normalized.envelope.model_copy(
        update={
            "source_ledger_checkpoints": [
                LedgerCheckpoint(
                    stream_id=value["stream_id"],
                    high_watermark_sequence=value["high_watermark"],
                    head_digest=value["head_digest"],
                )
                for value in checkpoint["streams"].values()
            ]
        }
    )
    stored = normalized.model_copy(update={"envelope": envelope})
    return str(persist_report(ledger, stored)["record_digest"])


def _policy_file(directory: Path) -> Path:
    policy = directory / "audit-policy.json"
    policy.write_text(
        json.dumps(
            {"maximum_observation_age_seconds": 604800, "require_conversion_evidence": False}
        )
    )
    return policy


def _invoke(directory: Path, record_digest: str, *extra: str) -> Any:
    return runner.invoke(
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
            AUDIT_AT,
            "--limit",
            "200",
            *extra,
        ],
    )


@pytest.fixture(autouse=True)
def _local_export_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redaction uses the private OS export key; supply a fixed local one."""
    import adhelm.credentials

    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"p" * 32)


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Path, str, str]:
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    admin = _retain_admin(ledger)
    return directory, _retain_report(ledger), admin


# --- evidence selection ------------------------------------------------------------------------


def test_the_loader_selects_the_retained_admin_observation_for_the_reports_property(
    prepared: tuple[Path, str, str],
) -> None:
    directory, _report, admin_digest = prepared

    evidence, provenance = load_audit_evidence(
        Ledger(directory), provider="ga4", account_ref=PROPERTY
    )

    assert evidence.ga4_admin is not None
    assert evidence.ga4_admin.record_digest == admin_digest
    assert evidence.ga4_admin.observed_at == SECOND_READ
    assert evidence.ga4_admin.inventory.identity.property_client_ref == "123"
    assert len(evidence.ga4_admin.inventory.key_events) == 2
    assert evidence.inventory is None
    assert provenance["admin_inventory_status"] == "SELECTED"
    assert provenance["selected_admin_observation_record_digest"] == admin_digest
    assert provenance["retained_admin_observation_count"] == 1
    # The property identifier itself is not echoed into agent-visible provenance.
    # Exact matches only, so a hex digest that happens to contain "123" cannot collide.
    serialized = json.dumps(provenance)
    assert "ga4.property.123" not in serialized
    assert "ga4_admin.property.123" not in serialized
    assert "123" not in provenance.values()
    assert evidence.ga4_requested_property_ref == PROPERTY


def test_the_latest_observation_wins_and_an_explicit_digest_selects_an_older_one(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path / "data")
    older = _retain_admin(ledger, key_events=1, finished=FIRST_READ)
    newer = _retain_admin(ledger, key_events=3, finished=SECOND_READ)

    latest, latest_provenance = load_ga4_audit_evidence(ledger, account_ref=PROPERTY)
    chosen, chosen_provenance = load_ga4_audit_evidence(
        ledger, account_ref=PROPERTY, inventory_record_digest=older
    )

    assert latest.ga4_admin is not None and chosen.ga4_admin is not None
    assert latest.ga4_admin.record_digest == newer
    assert len(latest.ga4_admin.inventory.key_events) == 3
    assert chosen.ga4_admin.record_digest == older
    assert len(chosen.ga4_admin.inventory.key_events) == 1
    assert latest_provenance["retained_admin_observation_count"] == 2
    assert chosen_provenance["retained_admin_observation_count"] == 2


def test_no_retained_observation_is_named_in_provenance_and_yields_empty_evidence(
    tmp_path: Path,
) -> None:
    evidence, provenance = load_ga4_audit_evidence(Ledger(tmp_path / "data"), account_ref=PROPERTY)

    assert evidence.ga4_admin is None
    assert provenance["admin_inventory_status"] == "NOT_RETAINED"
    assert provenance["retained_admin_observation_count"] == 0
    # The requested property survives an empty selection, so the audit can still refuse it.
    assert evidence.ga4_requested_property_ref == PROPERTY


def test_another_propertys_observation_is_never_selected_for_this_report(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "data")
    other = _retain_admin(ledger, property_id="999")

    evidence, provenance = load_ga4_audit_evidence(ledger, account_ref=PROPERTY)
    with pytest.raises(AdhelmError) as error:
        load_ga4_audit_evidence(ledger, account_ref=PROPERTY, inventory_record_digest=other)

    assert evidence.ga4_admin is None
    assert provenance["retained_admin_observation_count"] == 0
    assert error.value.code == "INVENTORY_SNAPSHOT_NOT_RETAINED"


def test_a_digest_that_names_no_observation_is_refused_rather_than_replaced(
    prepared: tuple[Path, str, str],
) -> None:
    directory, report_digest, _admin = prepared

    with pytest.raises(AdhelmError) as unknown:
        load_ga4_audit_evidence(
            Ledger(directory), account_ref=PROPERTY, inventory_record_digest="d" * 64
        )
    # A retained record of the wrong kind is not a measurement observation.
    with pytest.raises(AdhelmError) as wrong_kind:
        load_ga4_audit_evidence(
            Ledger(directory), account_ref=PROPERTY, inventory_record_digest=report_digest
        )

    assert unknown.value.code == "INVENTORY_SNAPSHOT_NOT_RETAINED"
    assert wrong_kind.value.code == "INVENTORY_SNAPSHOT_NOT_RETAINED"


@pytest.mark.parametrize("digest_value", ["", "A" * 64, "a" * 63, "not-a-digest"])
def test_a_malformed_digest_is_refused_before_history_is_read(
    tmp_path: Path, digest_value: str
) -> None:
    with pytest.raises(AdhelmError) as error:
        load_ga4_audit_evidence(
            Ledger(tmp_path / "data"), account_ref=PROPERTY, inventory_record_digest=digest_value
        )
    assert error.value.code == "INVALID_AUDIT_EVIDENCE_SELECTION"
    assert error.value.exit_code == 2


@pytest.mark.parametrize(
    "account_ref",
    [None, "", "ga4.property.", "ga4.property.abc", "ga4.stream.456", "openai_ads.account.123"],
)
def test_evidence_is_selected_only_by_a_ga4_property_reference(
    tmp_path: Path, account_ref: str | None
) -> None:
    with pytest.raises(AdhelmError) as error:
        load_ga4_audit_evidence(Ledger(tmp_path / "data"), account_ref=account_ref)
    assert error.value.code == "INVALID_AUDIT_EVIDENCE_SELECTION"


def test_the_retained_admin_property_reference_is_accepted_as_a_selector(
    prepared: tuple[Path, str, str],
) -> None:
    directory, _report, admin_digest = prepared
    evidence, _provenance = load_ga4_audit_evidence(
        Ledger(directory), account_ref="ga4_admin.property.123"
    )
    assert evidence.ga4_admin is not None
    assert evidence.ga4_admin.record_digest == admin_digest


@pytest.mark.parametrize("provider", ["ga4_admin", "gtm", "hubspot"])
def test_other_non_advertising_providers_still_have_no_audit_evidence(
    tmp_path: Path, provider: str
) -> None:
    with pytest.raises(AdhelmError) as error:
        load_audit_evidence(Ledger(tmp_path / "data"), provider=provider, account_ref=PROPERTY)
    assert error.value.code == "INVALID_AUDIT_EVIDENCE_SELECTION"


# --- the route -----------------------------------------------------------------------------------


def test_the_route_evaluates_the_ga4_table_with_its_admin_evidence(
    prepared: tuple[Path, str, str],
) -> None:
    directory, report_digest, admin_digest = prepared

    result = _invoke(directory, report_digest)

    assert result.exit_code == 0, result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["command"] == "audit --provider-rules"
    assert envelope["outcome"] == "INCOMPLETE_COVERAGE"
    data = envelope["data"]
    assert data["provider"] == "ga4"
    assert data["rules_status"] == "EVALUATED"
    sections = data["sections"]
    assert sections["live_provider_reads"] == "NONE_RETAINED_RECORDS_ONLY"
    assert sections["analytics_completeness"] == "BOUNDED_NON_EXHAUSTIVE"
    coverage = {item["dimension"]: item["status"] for item in sections["coverage"]}
    assert len(coverage) == 14
    assert coverage["STRUCTURE"] == "PARTIAL"
    assert coverage["TRACKING"] == "PARTIAL"
    assert coverage["NATIVE_CONVERSION"] == "NOT_APPLICABLE"
    assert coverage["DRIFT"] == "NOT_READ"
    assert data["provenance"]["admin_inventory_status"] == "SELECTED"
    assert data["provenance"]["selected_admin_observation_record_digest"] == admin_digest
    # The provider-neutral audit no longer counts a GA4 key event as native conversion evidence.
    neutral = {item["dimension"]: item["status"] for item in data["audit"]["coverage"]}
    assert neutral["NATIVE_CONVERSION"] == "EVIDENCE_MISSING"


def test_the_route_audits_the_report_alone_when_no_admin_observation_is_retained(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "data"
    report_digest = _retain_report(Ledger(directory))

    result = _invoke(directory, report_digest)

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    coverage = {item["dimension"]: item["status"] for item in data["sections"]["coverage"]}
    assert coverage["STRUCTURE"] == "NOT_READ"
    assert data["provenance"]["admin_inventory_status"] == "NOT_RETAINED"
    observations = {item["finding"]["observation"] for item in data["sections"]["findings"]}
    assert any("No GA4 Admin inventory is retained" in item for item in observations)


def test_the_route_reports_admin_findings_through_the_redacted_boundary(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    _retain_admin(ledger, counting_method="COUNTING_METHOD_UNSPECIFIED", with_edit_rule=True)
    report_digest = _retain_report(ledger, sampled=True)

    result = _invoke(directory, report_digest)

    assert result.exit_code == 0, result.stdout
    sections = json.loads(result.stdout)["data"]["sections"]
    # Rule identifiers and resource references are pseudonymized on this route, so the
    # rules are recognized by the exact observation prose they emit.
    observations = [item["finding"]["observation"] for item in sections["findings"]]
    assert sum("counting method is unspecified" in item for item in observations) == 2
    assert any("event create or edit rules" in item for item in observations)
    assert any("reported sampling" in item for item in observations)
    assert all(item["rule_id"].startswith("opaque:") for item in sections["findings"])
    serialized = result.stdout
    # `generate_lead` was declared but not returned, so a finding names its event reference;
    # the redacted route must not show that reference in the clear.
    assert any("no row in the window" in item for item in observations)
    assert "ga4.event.generate_lead" not in serialized
    assert format(1_001, "064x") not in serialized
    for item in sections["recommendations"]:
        assert item["operation_kind"] == "OBSERVE"
        assert item["execution_status"] == "NOT_EXECUTED"
        assert item["recommendation"]["proposed_plan_ref"] is None


def test_the_route_appends_no_record_and_makes_no_provider_request(
    prepared: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.transport as transport_module

    directory, report_digest, _admin = prepared
    before = Ledger(directory).checkpoint()
    monkeypatch.setattr(
        transport_module.SafeHttpClient,
        "request",
        lambda *_a, **_k: pytest.fail("the audit route must make no request"),
    )

    result = _invoke(directory, report_digest)

    assert result.exit_code == 0, result.stdout
    assert Ledger(directory).checkpoint()["streams"] == before["streams"]


def test_the_route_pages_without_changing_the_provider_audit_digest(
    prepared: tuple[Path, str, str],
) -> None:
    directory, report_digest, _admin = prepared

    first = json.loads(_invoke(directory, report_digest, "--limit", "2").stdout)
    second = json.loads(_invoke(directory, report_digest, "--limit", "2", "--cursor", "2").stdout)

    assert first["data"]["provider_audit_digest"] == second["data"]["provider_audit_digest"]
    assert first["data"]["row_counts"] == second["data"]["row_counts"]
    assert first["data"]["sections"]["findings"] != second["data"]["sections"]["findings"]


def test_the_route_selects_an_older_observation_by_digest(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    older = _retain_admin(ledger, key_events=1, finished=FIRST_READ)
    _retain_admin(ledger, key_events=2, finished=SECOND_READ)
    report_digest = _retain_report(ledger)

    result = _invoke(directory, report_digest, "--inventory-digest", older)

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["provenance"]["selected_admin_observation_record_digest"] == older
    # One key event in the older inventory against two declared by the report.
    observations = [item["finding"]["observation"] for item in data["sections"]["findings"]]
    assert any(
        "differs from the number in the retained Admin inventory" in item for item in observations
    )


def test_the_route_refuses_an_unretained_inventory_digest_with_a_precondition_exit(
    prepared: tuple[Path, str, str],
) -> None:
    directory, report_digest, _admin = prepared

    result = _invoke(directory, report_digest, "--inventory-digest", "d" * 64)

    assert result.exit_code == 3
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "INVENTORY_SNAPSHOT_NOT_RETAINED"


def test_the_route_audits_the_report_without_another_propertys_observation(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    _retain_admin(ledger, property_id="999")
    report_digest = _retain_report(ledger)

    result = _invoke(directory, report_digest)

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["provenance"]["admin_inventory_status"] == "NOT_RETAINED"
    coverage = {item["dimension"]: item["status"] for item in data["sections"]["coverage"]}
    assert coverage["STRUCTURE"] == "NOT_READ"


# --- an explicit --account that is not the report's property -----------------------------------


_FOREIGN_PROPERTY = "The requested GA4 property is not the property the report describes."
_ADMIN_IN_THE_FUTURE = "observed after the audit evaluation time"


def _assert_refused_without_findings(result: Any, message: str) -> None:
    """Assert the refusal is the expected one, not merely the shared code.

    ``INVALID_AUDIT_INPUT`` is also raised for Admin evidence of another
    property and for an ``--at`` earlier than the report, so the code alone
    would let a fixture change pass on the wrong refusal.
    """
    assert result.exit_code == 2, result.stdout
    envelope = json.loads(result.stdout)
    diagnostic = envelope["diagnostics"][0]
    assert diagnostic["code"] == "INVALID_AUDIT_INPUT"
    assert message in diagnostic["message"], diagnostic["message"]
    assert envelope.get("data") in (None, {})
    assert "findings" not in result.stdout


def test_an_account_that_is_not_the_reports_property_is_refused_when_nothing_is_retained_for_it(
    prepared: tuple[Path, str, str],
) -> None:
    directory, report_digest, _admin = prepared

    result = _invoke(directory, report_digest, "--account", "ga4.property.999")

    _assert_refused_without_findings(result, _FOREIGN_PROPERTY)


def test_an_account_that_is_not_the_reports_property_is_refused_when_it_has_an_observation(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    _retain_admin(ledger)
    _retain_admin(ledger, property_id="999")
    report_digest = _retain_report(ledger)

    result = _invoke(directory, report_digest, "--account", "ga4.property.999")

    _assert_refused_without_findings(result, _FOREIGN_PROPERTY)


def test_an_account_that_names_the_reports_own_property_is_audited(
    prepared: tuple[Path, str, str],
) -> None:
    directory, report_digest, admin_digest = prepared

    result = _invoke(directory, report_digest, "--account", PROPERTY)

    assert result.exit_code == 0, result.stdout
    provenance = json.loads(result.stdout)["data"]["provenance"]
    assert provenance["selected_admin_observation_record_digest"] == admin_digest


def test_the_route_refuses_admin_evidence_that_finished_after_the_evaluation_time(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    _retain_admin(ledger, finished=datetime(2026, 9, 6, 12, tzinfo=UTC))
    report_digest = _retain_report(ledger)

    result = _invoke(directory, report_digest)

    _assert_refused_without_findings(result, _ADMIN_IN_THE_FUTURE)
