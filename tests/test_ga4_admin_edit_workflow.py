"""Hermetic end-to-end EDIT observations, persistence, and bounded CLI review."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import adhelm.ga4_admin_edit_cli as edit_cli
import adhelm.ga4_admin_edit_workflow as workflow
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences, Profile
from adhelm.providers.ga4_admin import Ga4AdminDefaultValue
from adhelm.saved_ga4_admin_edit_inventories import (
    ga4_admin_edit_inventory_catalog,
    load_ga4_admin_edit_inventory,
    retain_ga4_admin_edit_inventory,
)
from adhelm.saved_measurements import measurement_catalog
from adhelm.strict_json import validate_json_value

KEY = b"synthetic-test-fingerprint-key-32!"


def configure(path: Path, *, maximum: int = 16_384) -> Configuration:
    config = Configuration(
        profiles=[
            Profile(
                provider="ga4_admin",
                profile_id="admin-edit",
                purpose="EDIT",
                account_ref="123",
                principal_ref="synthetic.principal",
                client_ref="synthetic.client",
                credential_source="EXPLICIT_CI",
                credential_ref="TEST_EDIT_TOKEN",
                scopes=["https://www.googleapis.com/auth/analytics.edit"],
                timezone="UTC",
                currency="USD",
            )
        ],
        output=OutputPreferences(max_output_bytes=maximum),
    )
    save_configuration(path, config)
    return config


def install(monkeypatch: pytest.MonkeyPatch, count: int = 3) -> list[httpx.Request]:
    client_type = httpx.Client
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET" and not request.content
        assert request.url.host == "analyticsadmin.googleapis.com"
        assert request.headers["authorization"] == "Bearer fixture-edit"
        if request.url.path == "/v1beta/properties/123":
            return httpx.Response(
                200,
                json={
                    "name": "properties/123",
                    "timeZone": "UTC",
                    "currencyCode": "USD",
                    "serviceLevel": "GOOGLE_ANALYTICS_STANDARD",
                },
            )
        if request.url.path == "/v1beta/properties/123/dataStreams/456":
            return httpx.Response(
                200,
                json={
                    "name": "properties/123/dataStreams/456",
                    "type": "WEB_DATA_STREAM",
                },
            )
        assert request.url.path == "/v1alpha/properties/123/keyEvents"
        return httpx.Response(
            200,
            json={
                "keyEvents": [
                    {
                        "name": f"properties/123/keyEvents/{700 + i}",
                        "eventName": f"private_event_{i}",
                        "custom": True,
                        "deletable": True,
                        "countingMethod": "ONCE_PER_EVENT",
                    }
                    for i in reversed(range(count))
                ]
            },
        )

    def factory(*, trust_env: bool, follow_redirects: bool) -> httpx.Client:
        assert trust_env is False and follow_redirects is False
        return client_type(
            transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
        )

    monkeypatch.setattr(workflow.httpx, "Client", factory)
    monkeypatch.setattr(workflow, "credential", lambda *args, **kwargs: "fixture-edit")
    monkeypatch.setattr(edit_cli, "export_key", lambda: KEY)
    return calls


def invoke(path: Path, *args: str) -> tuple[int, dict]:
    result = CliRunner().invoke(
        app, ["--data-dir", str(path), "measurement", "ga4-admin-edit", *args]
    )
    assert result.stdout, repr(result.exception)
    return result.exit_code, json.loads(result.stdout)


def observe(path: Path, config: Configuration) -> Ga4AdminEditInventoryObservation:
    inventory = workflow.read_ga4_admin_edit_inventory(
        path, "admin-edit", stream_id="456", fingerprint_key=KEY, ci_enabled=True
    )
    when = datetime.now(UTC)
    return Ga4AdminEditInventoryObservation(
        profile_ref="admin-edit",
        configuration_digest=digest(config),
        profile_configuration_digest=digest(config.profiles[0]),
        read_started_at=when,
        read_finished_at=when,
        inventory=inventory,
        inventory_digest=digest(inventory),
    )


def test_cli_reads_retains_then_reviews_without_provider_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    calls = install(monkeypatch)
    code, result = invoke(
        tmp_path, "read", "--profile", "admin-edit", "--stream-id", "456", "--ci-enabled"
    )
    assert code == 0, result
    receipt = result["data"]
    assert receipt["persisted"] is True and receipt["row_count"] == 3
    assert receipt["execution_authority"] is False
    assert len(calls) == 3
    selected = receipt["record_digest"]
    records = Ledger(tmp_path).checkpoint()["records_by_stream"]["changes"]
    assert records[-1]["record_kind"] == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
    assert records[-1]["source"] == "UNKNOWN"
    assert measurement_catalog(Ledger(tmp_path))["total_count"] == 0
    code, catalog = invoke(tmp_path, "catalog")
    assert code == 0 and catalog["data"]["inventories"][0]["record_digest"] == selected
    code, review = invoke(tmp_path, "show", selected, "--limit", "2")
    assert code == 0, review
    assert review["next_cursor"] == "2" and review["total_count"] == 3
    data = review["data"]
    assert data["capacity"]["remaining_documented_slots"] == 27
    assert data["create_permission"] == "NOT_PREPROVABLE"
    assert data["content_authentication"] == "NOT_CHECKED"
    public = json.dumps(review)
    for private in (
        "private_event",
        "fingerprint",
        "ga4_admin.property.123",
        'admin-edit"',
        "fixture-edit",
        "key_event.700",
    ):
        assert private not in public
    assert len(calls) == 3


def test_empty_inventory_remains_retained_and_reviewable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configure(tmp_path)
    install(monkeypatch, 0)
    observation = observe(tmp_path, config)
    record = retain_ga4_admin_edit_inventory(Ledger(tmp_path), observation, fingerprint_key=KEY)
    loaded, _ = load_ga4_admin_edit_inventory(Ledger(tmp_path), record["record_digest"])
    assert loaded == observation
    code, review = invoke(tmp_path, "show", record["record_digest"])
    assert code == 0 and review["total_count"] == 0
    assert review["data"]["rows"] == []


def test_minimum_output_retains_complete_inventory_with_small_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path, maximum=1024)
    install(monkeypatch)
    code, result = invoke(
        tmp_path, "read", "--profile", "admin-edit", "--stream-id", "456", "--ci-enabled"
    )
    assert code == 0, result
    code, catalog = invoke(tmp_path, "catalog")
    assert code == 0, catalog
    monkeypatch.setattr(
        edit_cli, "export_key", lambda: pytest.fail("no key before output preflight")
    )
    code, result = invoke(tmp_path, "show", result["data"]["record_digest"])
    assert code != 0 and result["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"


def test_retention_rejects_wrong_key_without_appending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configure(tmp_path)
    install(monkeypatch)
    observation = observe(tmp_path, config)
    with pytest.raises(AdhelmError, match="current local key"):
        retain_ga4_admin_edit_inventory(Ledger(tmp_path), observation, fingerprint_key=b"z" * 32)
    assert ga4_admin_edit_inventory_catalog(Ledger(tmp_path))["total_count"] == 0


def test_prior_configuration_digest_mismatch_stops_before_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path)
    monkeypatch.setattr(workflow, "credential", lambda *args, **kwargs: pytest.fail("credential"))
    with pytest.raises(AdhelmError, match="preflight"):
        workflow.read_ga4_admin_edit_inventory(
            tmp_path,
            "admin-edit",
            stream_id="456",
            ci_enabled=True,
            fingerprint_key=KEY,
            expected_configuration_digest="0" * 64,
        )


@pytest.mark.parametrize("alias", [1, 1.0, True])
def test_observation_rejects_decimal_numeric_alias_before_model_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: object
) -> None:
    config = configure(tmp_path)
    install(monkeypatch, 1)
    original = observe(tmp_path, config)
    member = original.inventory.key_events[0].model_copy(
        update={"default_value": Ga4AdminDefaultValue(numeric_value=Decimal("1"), currency="USD")}
    )
    inventory = original.inventory.model_copy(update={"key_events": (member,)})
    # This is a shape-only observation fixture, never a retained authenticated
    # inventory. Leave its syntactic marker unchanged: the test targets raw
    # scalar rejection even when the ordinary normalized digest would agree.
    observation = original.model_copy(
        update={"inventory": inventory, "inventory_digest": digest(inventory)}
    )
    raw = observation.model_dump(mode="json")
    assert validate_json_value(Ga4AdminEditInventoryObservation, raw)
    raw["inventory"]["key_events"][0]["default_value"]["numeric_value"] = alias
    with pytest.raises((AdhelmError, ValueError, ValidationError)):
        validate_json_value(Ga4AdminEditInventoryObservation, raw)
