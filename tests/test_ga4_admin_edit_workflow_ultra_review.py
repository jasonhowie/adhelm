"""Independent fixture-only boundaries for configured GA4 Admin EDIT inventory history."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import adhelm.ga4_admin_edit_cli as edit_cli
import adhelm.ga4_admin_edit_workflow as workflow
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences, Profile
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditInventoryTarget,
    Ga4AdminEditKeyEventInventory,
    Ga4AdminEditKeyEventInventoryClient,
)
from adhelm.saved_ga4_admin_edit_inventories import (
    ga4_admin_edit_inventory_catalog,
    retain_ga4_admin_edit_inventory,
    validate_selection,
)
from adhelm.strict_json import validate_json_value
from adhelm.transport import SafeHttpClient

_KEY = b"k" * 32
_SCOPE = "https://www.googleapis.com/auth/analytics.edit"
_FIELDS = (
    "keyEvents(name,eventName,createTime,deletable,custom,countingMethod,defaultValue),"
    "nextPageToken"
)


def _profile(**changes: object) -> Profile:
    values: dict[str, object] = {
        "profile_id": "edit-inventory",
        "provider": "ga4_admin",
        "account_ref": "123",
        "principal_ref": "fixture.principal",
        "client_ref": "fixture.client",
        "scopes": [_SCOPE],
        "purpose": "EDIT",
        "currency": "USD",
        "timezone": "UTC",
        "credential_source": "EXPLICIT_CI",
        "credential_ref": "EDIT_FIXTURE_TOKEN",
    }
    values.update(changes)
    return Profile.model_validate(values)


def _configure(
    directory: Path, profile: Profile | None = None, *, maximum: int = 16_384
) -> Configuration:
    configuration = Configuration(
        profiles=[profile or _profile()], output=OutputPreferences(max_output_bytes=maximum)
    )
    save_configuration(directory, configuration)
    return configuration


def _replace_account(
    directory: Path, configuration: Configuration, account_ref: str = "124"
) -> None:
    changed = configuration.profiles[0].model_copy(update={"account_ref": account_ref})
    save_configuration(
        directory,
        Configuration(profiles=[changed], output=configuration.output),
        replace=True,
    )


def _property(identifier: str = "123") -> dict[str, object]:
    return {
        "name": f"properties/{identifier}",
        "timeZone": "UTC",
        "currencyCode": "USD",
        "serviceLevel": "GOOGLE_ANALYTICS_STANDARD",
    }


def _stream(property_id: str = "123", stream_id: str = "456") -> dict[str, object]:
    return {"name": f"properties/{property_id}/dataStreams/{stream_id}", "type": "WEB_DATA_STREAM"}


def _key_event(property_id: str = "123", identifier: str = "701") -> dict[str, object]:
    return {
        "name": f"properties/{property_id}/keyEvents/{identifier}",
        "eventName": "private_fixture_event",
        "custom": True,
        "deletable": True,
        "countingMethod": "ONCE_PER_EVENT",
    }


def _inventory_response(
    request: httpx.Request, *, property_id: str = "123", stream_id: str = "456"
) -> httpx.Response:
    assert request.method == "GET"
    assert request.url.host == "analyticsadmin.googleapis.com"
    assert request.content == b""
    if request.url.path == f"/v1beta/properties/{property_id}":
        assert dict(request.url.params) == {
            "fields": "name,timeZone,currencyCode,serviceLevel,deleteTime"
        }
        return httpx.Response(200, json=_property(property_id))
    if request.url.path == f"/v1beta/properties/{property_id}/dataStreams/{stream_id}":
        assert dict(request.url.params) == {"fields": "name,type"}
        return httpx.Response(200, json=_stream(property_id, stream_id))
    assert request.url.path == f"/v1alpha/properties/{property_id}/keyEvents"
    assert dict(request.url.params) == {"pageSize": "200", "fields": _FIELDS}
    return httpx.Response(200, json={"keyEvents": [_key_event(property_id)]})


def _fresh_inventory(
    *, property_id: str = "123", stream_id: str = "456"
) -> Ga4AdminEditKeyEventInventory:
    target = Ga4AdminEditInventoryTarget(
        property_ref=f"ga4_admin.property.{property_id}",
        property_client_ref=property_id,
        web_stream_ref=f"ga4_admin.web_stream.{stream_id}",
        web_stream_client_ref=stream_id,
        expected_timezone="UTC",
        expected_currency="USD",
    )
    raw_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: _inventory_response(
                request, property_id=property_id, stream_id=stream_id
            )
        ),
        trust_env=False,
    )
    try:
        return Ga4AdminEditKeyEventInventoryClient(
            SafeHttpClient(raw_client, {"analyticsadmin.googleapis.com"}),
            lambda: "fixture-edit-token",
            fingerprint_key=_KEY,
        ).read_inventory(target)
    finally:
        raw_client.close()


def _observation(inventory: Ga4AdminEditKeyEventInventory) -> Ga4AdminEditInventoryObservation:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    return Ga4AdminEditInventoryObservation(
        profile_ref="edit-inventory",
        configuration_digest="a" * 64,
        profile_configuration_digest="b" * 64,
        read_started_at=now,
        read_finished_at=now,
        inventory=inventory,
        inventory_digest=digest(inventory),
    )


def _install_workflow_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[str]:
    client_type = httpx.Client
    token_calls: list[str] = []

    def client_factory(*, trust_env: bool, follow_redirects: bool) -> httpx.Client:
        assert trust_env is False and follow_redirects is False
        return client_type(
            transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
        )

    def credential(profile: Profile, *, ci_enabled: bool) -> str:
        assert ci_enabled is True
        token_calls.append(profile.profile_id)
        return "fixture-edit-token"

    monkeypatch.setattr(workflow.httpx, "Client", client_factory)
    monkeypatch.setattr(workflow, "credential", credential)
    return token_calls


@pytest.mark.parametrize(
    ("profile_changes", "stream_id", "ci_enabled"),
    [
        ({"purpose": "READ"}, "456", True),
        ({"scopes": ["https://www.googleapis.com/auth/analytics.readonly"]}, "456", True),
        ({"account_ref": "not-a-native-property"}, "456", True),
        ({}, "not-a-native-stream", True),
        ({}, "456", False),
    ],
)
def test_invalid_edit_profile_or_public_input_stops_before_token_and_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_changes: dict[str, object],
    stream_id: str,
    ci_enabled: bool,
) -> None:
    _configure(tmp_path, _profile(**profile_changes))
    calls = _install_workflow_transport(
        monkeypatch, lambda _request: pytest.fail("invalid input must not reach HTTP")
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.read_ga4_admin_edit_inventory(
            tmp_path,
            "edit-inventory",
            stream_id=stream_id,
            fingerprint_key=_KEY,
            ci_enabled=ci_enabled,
        )

    assert raised.value.code in {
        "GA4_EDIT_PROFILE_REQUIRED",
        "INVALID_WORKFLOW_INPUT",
        "CI_AUTH_NOT_ENABLED",
    }
    assert calls == []


def test_configured_workflow_captures_one_token_for_all_three_fixed_gets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request.headers["authorization"]))
        return _inventory_response(request)

    token_calls = _install_workflow_transport(monkeypatch, handler)
    inventory = workflow.read_ga4_admin_edit_inventory(
        tmp_path, "edit-inventory", stream_id="456", fingerprint_key=_KEY, ci_enabled=True
    )

    assert token_calls == ["edit-inventory"]
    assert requests == [
        ("/v1beta/properties/123", "Bearer fixture-edit-token"),
        ("/v1beta/properties/123/dataStreams/456", "Bearer fixture-edit-token"),
        ("/v1alpha/properties/123/keyEvents", "Bearer fixture-edit-token"),
    ]
    assert inventory.capacity.complete_observed_key_event_total == 1


def test_configuration_drift_after_token_or_read_never_returns_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _configure(tmp_path)
    token_calls: list[str] = []
    original_client = httpx.Client

    def token(_profile: Profile, *, ci_enabled: bool) -> str:
        assert ci_enabled is True
        token_calls.append("token")
        _replace_account(tmp_path, configuration)
        return "fixture-edit-token"

    monkeypatch.setattr(workflow, "credential", token)
    monkeypatch.setattr(
        workflow.httpx,
        "Client",
        lambda **_kwargs: pytest.fail("drift after token must stop before HTTP"),
    )
    with pytest.raises(AdhelmError) as after_token:
        workflow.read_ga4_admin_edit_inventory(
            tmp_path, "edit-inventory", stream_id="456", fingerprint_key=_KEY, ci_enabled=True
        )
    assert after_token.value.code == "CONFIGURATION_DRIFT"
    assert token_calls == ["token"]

    after_read_root = tmp_path / "after-read"
    configuration = _configure(after_read_root)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        response = _inventory_response(request)
        if request.url.path.endswith("/keyEvents"):
            _replace_account(after_read_root, configuration)
        return response

    monkeypatch.setattr(workflow.httpx, "Client", original_client)
    token_calls = _install_workflow_transport(monkeypatch, handler)
    with pytest.raises(AdhelmError) as after_read:
        workflow.read_ga4_admin_edit_inventory(
            after_read_root,
            "edit-inventory",
            stream_id="456",
            fingerprint_key=_KEY,
            ci_enabled=True,
        )
    assert after_read.value.code == "CONFIGURATION_DRIFT"
    assert token_calls == ["edit-inventory"]
    assert requests[-1] == "/v1alpha/properties/123/keyEvents"


@pytest.mark.parametrize(
    ("inventory", "expected_code"),
    [
        (
            lambda: _fresh_inventory().model_copy(update={"inventory_content_marker": "0" * 64}),
            "GA4_FINGERPRINT_EDIT_INVENTORY_CONTENT_MISMATCH",
        ),
        (lambda: _fresh_inventory(property_id="124"), "IDENTITY_MISMATCH"),
    ],
)
def test_returned_detached_inventory_is_revalidated_and_bound_to_configured_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inventory: Callable[[], Ga4AdminEditKeyEventInventory],
    expected_code: str,
) -> None:
    _configure(tmp_path)
    returned = inventory()

    class ReturnedInventoryClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def read_inventory(
            self, _target: Ga4AdminEditInventoryTarget
        ) -> Ga4AdminEditKeyEventInventory:
            return returned

    monkeypatch.setattr(workflow, "Ga4AdminEditKeyEventInventoryClient", ReturnedInventoryClient)
    monkeypatch.setattr(workflow, "credential", lambda *_args, **_kwargs: "fixture-edit-token")
    with pytest.raises(AdhelmError) as raised:
        workflow.read_ga4_admin_edit_inventory(
            tmp_path, "edit-inventory", stream_id="456", fingerprint_key=_KEY, ci_enabled=True
        )
    assert raised.value.code == expected_code


def test_retention_rejects_raw_control_aliases_marker_tamper_and_rehashed_provenance(
    tmp_path: Path,
) -> None:
    inventory = _fresh_inventory()
    observation = _observation(inventory)
    raw = json.loads(observation.model_dump_json())

    for path, value in (
        (("schema_version",), 1.0),
        (("execution_authority",), 0),
        (("inventory", "pagination_verified"), 1),
        (("inventory", "capacity", "complete_observed_key_event_total"), 1.0),
    ):
        candidate = json.loads(json.dumps(raw))
        cursor: dict[str, object] = candidate
        for member in path[:-1]:
            child = cursor[member]
            assert isinstance(child, dict)
            cursor = child
        cursor[path[-1]] = value
        with pytest.raises(ValidationError):
            validate_json_value(Ga4AdminEditInventoryObservation, candidate)

    forged = inventory.model_copy(update={"inventory_content_marker": "0" * 64})
    tampered = observation.model_copy(update={"inventory": forged})
    with pytest.raises(AdhelmError) as marker_error:
        retain_ga4_admin_edit_inventory(Ledger(tmp_path), tampered, fingerprint_key=_KEY)
    assert marker_error.value.code == "GA4_FINGERPRINT_EDIT_INVENTORY_CONTENT_MISMATCH"
    assert not (tmp_path / "ledger").exists()

    retained = retain_ga4_admin_edit_inventory(Ledger(tmp_path), observation, fingerprint_key=_KEY)
    path = next((tmp_path / "ledger" / "changes").glob("*.jsonl"))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[-1]["account_ref"] = "ga4_admin.property.999"
    records[-1]["record_digest"] = digest(
        {name: value for name, value in records[-1].items() if name != "record_digest"}
    )
    path.write_bytes(b"".join(canonical_bytes(record) + b"\n" for record in records))

    with pytest.raises(AdhelmError) as rehashed:
        ga4_admin_edit_inventory_catalog(Ledger(tmp_path))
    assert rehashed.value.code == "INVALID_MEASUREMENT_PROVENANCE"
    assert retained["record_digest"] not in str(rehashed.value)


def test_cli_rechecks_config_before_export_key_after_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _configure(tmp_path)
    real_checkpoint = Ledger.checkpoint
    key_calls: list[None] = []

    def checkpoint_then_drift(ledger: Ledger) -> dict[str, object]:
        result = real_checkpoint(ledger)
        _replace_account(tmp_path, configuration)
        return result

    monkeypatch.setattr(Ledger, "checkpoint", checkpoint_then_drift)
    monkeypatch.setattr(edit_cli, "export_key", lambda: key_calls.append(None) or _KEY)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: pytest.fail("drift must stop before credential"),
    )
    result = CliRunner().invoke(
        __import__("adhelm.cli", fromlist=["app"]).app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "ga4-admin-edit",
            "read",
            "--profile",
            "edit-inventory",
            "--stream-id",
            "456",
            "--ci-enabled",
        ],
    )
    assert result.exit_code != 0
    assert json.loads(result.output)["diagnostics"][0]["code"] == "CONFIGURATION_CHANGED"
    assert key_calls == []


@pytest.mark.parametrize(
    ("profile_changes", "stream_id", "ci_enabled"),
    [
        ({"purpose": "READ"}, "456", True),
        ({"scopes": ["https://www.googleapis.com/auth/analytics.readonly"]}, "456", True),
        ({"account_ref": "not-a-native-property"}, "456", True),
        ({}, "not-a-native-stream", True),
        ({}, "456", False),
    ],
)
def test_cli_invalid_configuration_or_input_never_acquires_export_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_changes: dict[str, object],
    stream_id: str,
    ci_enabled: bool,
) -> None:
    _configure(tmp_path, _profile(**profile_changes))
    key_calls: list[None] = []
    monkeypatch.setattr(edit_cli, "export_key", lambda: key_calls.append(None) or _KEY)
    monkeypatch.setattr(
        workflow,
        "credential",
        lambda *_args, **_kwargs: pytest.fail("invalid CLI input reached credentials"),
    )
    result = CliRunner().invoke(
        __import__("adhelm.cli", fromlist=["app"]).app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "ga4-admin-edit",
            "read",
            "--profile",
            "edit-inventory",
            "--stream-id",
            stream_id,
            *(["--ci-enabled"] if ci_enabled else []),
        ],
    )
    assert result.exit_code != 0
    assert json.loads(result.output)["diagnostics"][0]["code"] in {
        "GA4_EDIT_PROFILE_REQUIRED",
        "INVALID_WORKFLOW_INPUT",
        "CI_AUTH_NOT_ENABLED",
    }
    assert key_calls == []


def test_cli_retention_uncertainty_is_redacted_and_does_not_claim_a_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    inventory = _fresh_inventory()
    monkeypatch.setattr(edit_cli, "export_key", lambda: _KEY)
    monkeypatch.setattr(
        edit_cli, "read_ga4_admin_edit_inventory", lambda *_args, **_kwargs: inventory
    )
    monkeypatch.setattr(
        edit_cli,
        "retain_ga4_admin_edit_inventory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic local failure")),
    )

    result = CliRunner().invoke(
        __import__("adhelm.cli", fromlist=["app"]).app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "ga4-admin-edit",
            "read",
            "--profile",
            "edit-inventory",
            "--stream-id",
            "456",
            "--ci-enabled",
        ],
    )
    assert result.exit_code != 0
    envelope = json.loads(result.output)
    assert envelope["diagnostics"][0]["code"] == "MEASUREMENT_RETENTION_UNVERIFIED"
    assert "private_fixture_event" not in result.output
    assert not ga4_admin_edit_inventory_catalog(Ledger(tmp_path))["inventories"]


def test_post_read_configuration_drift_prevents_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _configure(tmp_path)
    inventory = _fresh_inventory()

    def read_then_drift(*_args: object, **_kwargs: object) -> Ga4AdminEditKeyEventInventory:
        _replace_account(tmp_path, configuration)
        return inventory

    monkeypatch.setattr(edit_cli, "export_key", lambda: _KEY)
    monkeypatch.setattr(edit_cli, "read_ga4_admin_edit_inventory", read_then_drift)
    result = CliRunner().invoke(
        __import__("adhelm.cli", fromlist=["app"]).app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "ga4-admin-edit",
            "read",
            "--profile",
            "edit-inventory",
            "--stream-id",
            "456",
            "--ci-enabled",
        ],
    )
    assert result.exit_code != 0
    assert json.loads(result.output)["diagnostics"][0]["code"] == "CONFIGURATION_CHANGED"
    assert not ga4_admin_edit_inventory_catalog(Ledger(tmp_path))["inventories"]


def test_history_pagination_controls_are_exact_and_minimum_review_fails_before_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, maximum=1024)
    observation = _observation(_fresh_inventory())
    retained = retain_ga4_admin_edit_inventory(Ledger(tmp_path), observation, fingerprint_key=_KEY)
    for cursor, limit in ((True, 1), (0, True), (0.0, 1), (0, 1.0)):
        with pytest.raises(AdhelmError) as invalid:
            validate_selection(cursor, limit)  # type: ignore[arg-type]
        assert invalid.value.code == "INVALID_MEASUREMENT_SELECTION"

    key_calls: list[None] = []
    monkeypatch.setattr(edit_cli, "export_key", lambda: key_calls.append(None) or _KEY)
    app = __import__("adhelm.cli", fromlist=["app"]).app
    catalog = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "measurement", "ga4-admin-edit", "catalog"]
    )
    assert catalog.exit_code == 0 and len(catalog.output.encode()) <= 1024
    review = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "ga4-admin-edit",
            "show",
            retained["record_digest"],
        ],
    )
    assert review.exit_code != 0
    assert json.loads(review.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert key_calls == []
