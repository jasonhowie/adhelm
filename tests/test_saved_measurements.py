"""Canonical measurement reads preserve uncertainty and require verified selection."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_ga4_measurement_workflow import _profile
from typer.testing import CliRunner

from adhelm import ga4_measurement_workflow, measurement_cli
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_records import (
    Ga4MeasurementSnapshot,
    MeasurementObservation,
    OpenAIMeasurementSnapshot,
)
from adhelm.models import Configuration, OutputPreferences
from adhelm.projections import export_verified
from adhelm.providers.ga4_admin import Ga4AdminIdentity, Ga4AdminInventory, Ga4AdminKeyEvent
from adhelm.providers.openai_ads import OpenAIAdsIdentity
from adhelm.providers.openai_conversions import OpenAIAdsConversionSetting
from adhelm.saved_measurements import load_measurement, measurement_catalog, retain_measurement

START = datetime(2026, 9, 4, tzinfo=UTC)
END = START + timedelta(seconds=2)


def _ga4_snapshot(count: int = 1) -> Ga4MeasurementSnapshot:
    identity = Ga4AdminIdentity(
        property_ref="ga4_admin.property.123",
        property_client_ref="123",
        web_stream_ref="ga4_admin.web_stream.456",
        web_stream_client_ref="456",
    )
    return Ga4MeasurementSnapshot(
        inventory=Ga4AdminInventory(
            identity=identity,
            key_events=tuple(
                Ga4AdminKeyEvent(
                    resource_ref=f"ga4_admin.key_event.{700 + i}",
                    resource_client_ref=str(700 + i),
                    property_ref=identity.property_ref,
                    configuration_fingerprint="a" * 64,
                    event_name_fingerprint="b" * 64,
                    counting_method="ONCE_PER_EVENT",
                    custom=True,
                    deletable=True,
                )
                for i in range(count)
            ),
        )
    )


def _openai_snapshot() -> OpenAIMeasurementSnapshot:
    identity = OpenAIAdsIdentity(
        account_ref="openai_ads.account.acct_fixture",
        account_client_ref="acct_fixture",
        timezone="UTC",
        currency="USD",
        status="active",
    )
    return OpenAIMeasurementSnapshot(
        identity=identity,
        settings=(
            OpenAIAdsConversionSetting(
                account_ref=identity.account_ref,
                conversion_setting_ref="openai_ads.conversion_setting.ces_fixture",
                conversion_setting_client_ref="ces_fixture",
                event_type="order_created",
                attribution_window_days=30,
                status="NOT_ARCHIVED",
                campaign_count=0,
                version=1,
            ),
        ),
    )


def _retain(
    root: Path, snapshot: Ga4MeasurementSnapshot | OpenAIMeasurementSnapshot | None = None
) -> dict:
    return retain_measurement(
        Ledger(root),
        snapshot or _ga4_snapshot(),
        profile_ref="measurement.fixture",
        read_started_at=START,
        read_finished_at=END,
    )


def test_snapshot_observations_survive_restart_without_receipt_or_change_claim(
    tmp_path: Path,
) -> None:
    first = _retain(tmp_path)
    second = _retain(tmp_path)
    third = _retain(tmp_path, _openai_snapshot())
    assert first["record_digest"] != second["record_digest"]
    assert first["payload"]["snapshot_digest"] == second["payload"]["snapshot_digest"]
    assert first["occurred_at"] is None and first["source"] == "UNKNOWN" and first["run_id"] is None
    restored, provenance = load_measurement(Ledger(tmp_path), first["record_digest"])
    assert restored.read_finished_at == END and restored.snapshot.row_count == 1
    assert (
        restored.runtime_firing
        == restored.processed_receipt
        == restored.change_attribution
        == "NOT_EVALUATED"
    )
    assert restored.execution_authority is False
    assert provenance["checkpoint_digest"] == Ledger(tmp_path).checkpoint()["checkpoint_digest"]
    catalog = measurement_catalog(Ledger(tmp_path), limit=1)
    assert catalog["measurements"][0]["record_digest"] == third["record_digest"]
    assert catalog["total_count"] == 3 and catalog["next_cursor"] == "1"
    assert not Ledger(tmp_path).unresolved_runs()


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "meta_ads"),
        ("account_ref", "wrong"),
        ("source", "LOCAL_CLI"),
        ("run_id", "run.fixture"),
        ("occurred_at", START.isoformat()),
        ("observed_at", START.isoformat()),
    ],
)
def test_measurement_envelope_binding_before_append(tmp_path: Path, field: str, value: str) -> None:
    snapshot = _ga4_snapshot()
    payload = MeasurementObservation(
        profile_ref="fixture",
        read_started_at=START,
        read_finished_at=END,
        snapshot=snapshot,
        snapshot_digest=digest(snapshot),
    ).model_dump(mode="json")
    kwargs = dict(
        provider="ga4_admin",
        account_ref=snapshot.account_ref,
        source="UNKNOWN",
        observed_at=END.isoformat(),
    )
    kwargs[field] = value
    with pytest.raises(AdhelmError, match="Measurement history"):
        Ledger(tmp_path).append("changes", "MEASUREMENT_OBSERVATION", payload, **kwargs)
    assert not (tmp_path / "ledger").exists()


def test_rehashed_measurement_provenance_is_revalidated_on_reads(tmp_path: Path) -> None:
    _retain(tmp_path)
    path = next((tmp_path / "ledger/changes").glob("*.jsonl"))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[-1]["account_ref"] = "other"
    records[-1]["record_digest"] = digest(
        {k: v for k, v in records[-1].items() if k != "record_digest"}
    )
    path.write_bytes(b"".join(canonical_bytes(item) + b"\n" for item in records))
    with pytest.raises(AdhelmError, match="Measurement history"):
        measurement_catalog(Ledger(tmp_path))


def test_rejects_bypassed_nested_models_and_false_digest_or_time(tmp_path: Path) -> None:
    snapshot = _ga4_snapshot()
    bad_item = snapshot.inventory.key_events[0].model_copy(update={"property_ref": "other"})
    bad = snapshot.model_copy(
        update={"inventory": snapshot.inventory.model_copy(update={"key_events": (bad_item,)})}
    )
    with pytest.raises(ValidationError):
        _retain(tmp_path, bad)
    assert not (tmp_path / "ledger").exists()
    for changes in (
        {"snapshot_digest": "0" * 64},
        {"read_started_at": END, "read_finished_at": START},
        {"read_started_at": START.replace(tzinfo=None)},
    ):
        with pytest.raises(ValidationError):
            MeasurementObservation.model_validate(
                {
                    "profile_ref": "fixture",
                    "read_started_at": START,
                    "read_finished_at": END,
                    "snapshot": snapshot,
                    "snapshot_digest": digest(snapshot),
                    **changes,
                }
            )


def test_export_hides_all_native_measurement_ids_and_preserves_exact_digests(
    tmp_path: Path,
) -> None:
    first = _retain(tmp_path / "data")
    second = _retain(tmp_path / "data", _openai_snapshot())
    export_verified(Ledger(tmp_path / "data"), tmp_path / "export", opaque_key=b"k" * 32)
    exported = (tmp_path / "export/records.jsonl").read_text()
    assert (
        "ga4_admin.property.123" not in exported
        and "acct_fixture" not in exported
        and "ces_fixture" not in exported
    )
    assert (
        first["payload"]["snapshot_digest"] in exported
        and second["payload"]["snapshot_digest"] in exported
    )
    assert '"property_client_ref":"123"' not in exported


def test_cli_retention_receipt_and_paged_local_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(
        tmp_path,
        Configuration(profiles=[_profile()], output=OutputPreferences(max_output_bytes=1024)),
    )
    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"k" * 32)
    monkeypatch.setattr(
        ga4_measurement_workflow,
        "read_ga4_admin",
        lambda *args, **kwargs: _ga4_snapshot(3).inventory,
    )
    base = ["--data-dir", str(tmp_path), "measurement"]
    runner = CliRunner()
    read = runner.invoke(app, [*base, "ga4-admin", "ga4-admin-read", "456"])
    assert read.exit_code == 0, read.output
    assert len(read.output.encode()) <= 1024
    receipt = json.loads(read.output)["data"]
    assert receipt["persisted"] is True and receipt["row_count"] == 3
    assert "ga4_admin.property.123" not in read.output
    catalog = runner.invoke(app, [*base, "catalog"])
    assert catalog.exit_code == 0 and len(catalog.output.encode()) <= 1024
    save_configuration(
        tmp_path,
        Configuration(profiles=[_profile()], output=OutputPreferences(max_rows=1)),
        replace=True,
    )
    shown = runner.invoke(app, [*base, "show", receipt["record_digest"]])
    assert shown.exit_code == 0, shown.output
    result = json.loads(shown.output)
    assert result["next_cursor"] == "1" and result["total_count"] == 3
    assert len(result["data"]["rows"]) == 1
    assert result["data"]["identity"]["property_client_ref"].startswith("opaque:")
    assert result["data"]["processed_receipt"] == "NOT_EVALUATED"
    assert result["data"]["implementation_evidence"] == "FIXTURE_VERIFIED_LIVE_UNVERIFIED"


def test_cli_failed_read_or_durability_never_claims_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"k" * 32)

    def failure(*args, **kwargs):
        raise AdhelmError("PAGE_INCOMPLETE", "Read failed closed.")

    monkeypatch.setattr(ga4_measurement_workflow, "read_ga4_admin", failure)
    args = ["--data-dir", str(tmp_path), "measurement", "ga4-admin", "ga4-admin-read", "456"]
    failed = CliRunner().invoke(app, args)
    assert failed.exit_code != 0 and not measurement_catalog(Ledger(tmp_path))["measurements"]
    monkeypatch.setattr(
        ga4_measurement_workflow, "read_ga4_admin", lambda *a, **kw: _ga4_snapshot().inventory
    )

    def uncertain(*args, **kwargs):
        raise OSError("private filesystem path")

    monkeypatch.setattr(measurement_cli, "retain_measurement", uncertain)
    failed = CliRunner().invoke(app, args)
    assert failed.exit_code != 0
    assert json.loads(failed.output)["diagnostics"][0]["code"] == "MEASUREMENT_RETENTION_UNVERIFIED"
    assert (
        "private filesystem" not in failed.output
        and "persisted" not in json.loads(failed.output)["data"]
    )


def test_measurement_discovery_and_schema_are_installed() -> None:
    discovery = CliRunner().invoke(app, ["discover"])
    data = json.loads(discovery.output)["data"]
    assert {
        "measurement ga4-admin",
        "measurement openai-conversions",
        "measurement catalog",
        "measurement show",
    } <= set(data["commands"])
    schema = CliRunner().invoke(app, ["schema", "measurement-observation"])
    assert schema.exit_code == 0, schema.output
    for name in ("measurement-ga4-read", "measurement-openai-read"):
        request = CliRunner().invoke(app, ["schema", name])
        assert request.exit_code == 0, request.output
        properties = json.loads(request.output)["data"]["schema"]["properties"]
        assert properties["schema_version"]["const"] == 1 and "profile_id" in properties


def test_adaptive_review_pages_preserve_every_row_with_exact_cursors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _retain(tmp_path, _ga4_snapshot(12))
    save_configuration(
        tmp_path, Configuration(output=OutputPreferences(max_rows=50, max_output_bytes=4096))
    )
    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"k" * 32)
    observed = []
    cursor = "0"
    pages = 0
    while True:
        result = CliRunner().invoke(
            app,
            [
                "--data-dir",
                str(tmp_path),
                "measurement",
                "show",
                record["record_digest"],
                "--cursor",
                cursor,
                "--limit",
                "50",
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(result.output.encode()) <= 4096
        envelope = json.loads(result.output)
        assert envelope["total_count"] == 12
        rows = envelope["data"]["rows"]
        assert rows
        observed.extend(row["resource_ref"] for row in rows)
        pages += 1
        if envelope["next_cursor"] is None:
            break
        assert int(envelope["next_cursor"]) == len(observed)
        cursor = envelope["next_cursor"]
    assert pages > 1 and len(observed) == len(set(observed)) == 12
