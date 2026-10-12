"""Content integrity survives real normalization and retained-history boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from test_ga4_admin_preconditions import _create_input, _inventory_identity
from test_ga4_continuity_integration_ultra_review import _rewrite_last_measurement
from test_ga4_measurement_workflow import _profile
from test_saved_measurements import END, START
from typer.testing import CliRunner

from adhelm import ga4_measurement_workflow, measurement_cli
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_records import Ga4MeasurementSnapshot
from adhelm.models import Configuration
from adhelm.providers.ga4_admin import Ga4AdminInventory, Ga4AdminReadClient
from adhelm.providers.ga4_admin_preconditions import evaluate_ga4_key_event_create_preconditions
from adhelm.saved_measurements import load_measurement, retain_measurement
from adhelm.transport import SafeHttpClient

_KEY = b"i" * 32


def _fresh_inventory() -> Ga4AdminInventory:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(
                200,
                json={
                    "keyEvents": [
                        {
                            "name": f"properties/123/keyEvents/{native}",
                            "eventName": name,
                            "countingMethod": "ONCE_PER_EVENT",
                            "custom": True,
                            "deletable": True,
                        }
                        for native, name in ((702, "download_complete"), (700, "purchase"))
                    ]
                },
            )
        if request.url.path.endswith("/eventCreateRules"):
            return httpx.Response(200, json={"eventCreateRules": []})
        assert request.url.path.endswith("/eventEditRules")
        return httpx.Response(200, json={"eventEditRules": []})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        identity = _inventory_identity()
        return Ga4AdminReadClient(
            SafeHttpClient(client, {"analyticsadmin.googleapis.com"}),
            lambda: "synthetic-inventory-token",
            lambda candidate: candidate == identity,
            fingerprint_key=_KEY,
        ).inventory(identity=identity)


def test_cli_normalization_retention_load_preserve_content_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = _fresh_inventory()
    assert [row.resource_client_ref for row in inventory.key_events] == ["700", "702"]
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    monkeypatch.setattr(
        ga4_measurement_workflow, "read_ga4_admin", lambda *_args, **_kwargs: inventory
    )
    monkeypatch.setattr(measurement_cli, "export_key", lambda: _KEY)
    base = ["--data-dir", str(tmp_path), "measurement"]
    receipt = CliRunner().invoke(app, [*base, "ga4-admin", "ga4-admin-read", "456"])
    assert receipt.exit_code == 0, receipt.output
    reference = json.loads(receipt.output)["data"]["record_digest"]
    loaded, _ = load_measurement(Ledger(tmp_path), reference)
    assert isinstance(loaded.snapshot, Ga4MeasurementSnapshot)
    assert canonical_bytes(loaded.snapshot.inventory) == canonical_bytes(inventory)
    result = evaluate_ga4_key_event_create_preconditions(
        _create_input(), loaded.snapshot.inventory, fingerprint_key=_KEY
    )
    assert result.duplicate_status == "NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY"
    assert result.current_provider_state == "NOT_ESTABLISHED"
    shown = CliRunner().invoke(app, [*base, "show", reference])
    assert shown.exit_code == 0, shown.output
    for hidden in (
        "inventory_content_marker",
        "fingerprint_key_marker",
        inventory.inventory_content_marker,
    ):
        assert hidden and hidden not in shown.output


def test_rehashed_history_member_removal_cannot_satisfy_content_precondition(
    tmp_path: Path,
) -> None:
    inventory = _fresh_inventory()
    retain_measurement(
        Ledger(tmp_path),
        Ga4MeasurementSnapshot(inventory=inventory),
        profile_ref="ga4.read",
        read_started_at=START,
        read_finished_at=END,
    )

    def remove_members(payload: dict) -> None:
        payload["snapshot"]["inventory"]["key_events"] = []

    rewritten = _rewrite_last_measurement(tmp_path, remove_members)
    ledger = Ledger(tmp_path)
    assert ledger.checkpoint()["status"] == "VERIFIED_CHECKPOINT"
    loaded, _ = load_measurement(ledger, rewritten)
    assert isinstance(loaded.snapshot, Ga4MeasurementSnapshot)
    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(), loaded.snapshot.inventory, fingerprint_key=_KEY
        )
    assert error.value.code == "GA4_FINGERPRINT_INVENTORY_CONTENT_MISMATCH"


def test_m18_observation_bytes_stay_readable_without_new_integrity_claim(tmp_path: Path) -> None:
    wire = _fresh_inventory().model_dump(mode="json")
    wire.pop("inventory_content_marker")
    old_bytes = canonical_bytes(wire)
    old_digest = digest(wire)
    legacy = Ga4AdminInventory.model_validate_json(old_bytes)
    record = retain_measurement(
        Ledger(tmp_path),
        Ga4MeasurementSnapshot(inventory=legacy),
        profile_ref="ga4.read",
        read_started_at=START,
        read_finished_at=END,
    )
    loaded, _ = load_measurement(Ledger(tmp_path), record["record_digest"])
    assert isinstance(loaded.snapshot, Ga4MeasurementSnapshot)
    assert canonical_bytes(loaded.snapshot.inventory) == old_bytes
    assert digest(loaded.snapshot.inventory) == old_digest
    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(), loaded.snapshot.inventory, fingerprint_key=_KEY
        )
    assert error.value.code == "GA4_FINGERPRINT_INVENTORY_CONTENT_NOT_EVIDENCED"
