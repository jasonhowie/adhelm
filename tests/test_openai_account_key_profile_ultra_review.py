"""Independent adversarial coverage for OpenAI Ads account-key configuration."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import adhelm.credential_setup as credential_setup
import adhelm.credentials as credentials
import adhelm.inventory as inventory
import adhelm.openai_measurement_workflow as openai_measurement_workflow
import adhelm.workflows as workflows
from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import (
    Configuration,
    OpenAIAccountKeyProfile,
    Profile,
    validate_configured_profile_json,
)
from adhelm.providers.openai_ads import OpenAIAdsIdentity


def _account_key_profile(**changes: object) -> OpenAIAccountKeyProfile:
    values: dict[str, object] = {
        "profile_kind": "OPENAI_ADS_ACCOUNT_KEY",
        "profile_id": "openai-key-read",
        "account_ref": "account_fixture",
        "purpose": "READ",
        "currency": "USD",
        "timezone": "UTC",
        "credential_source": "OS_STORE",
        "credential_ref": "openai-key-slot",
    }
    values.update(changes)
    return OpenAIAccountKeyProfile.model_validate(values)


def _legacy_profile(*, profile_id: str, credential_ref: str) -> Profile:
    return Profile(
        profile_id=profile_id,
        provider="meta_ads",
        account_ref="account",
        principal_ref="principal",
        client_ref="client",
        scopes=["read"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref=credential_ref,
    )


def _report_input(root: Path, account_ref: str = "account_fixture") -> Path:
    path = root / "openai-report.json"
    path.write_text(
        json.dumps(
            {
                "account_ref": account_ref,
                "report_id": "report.openai.key",
                "query_contract_hash": "a" * 64,
                "window_start": "2026-01-01",
                "window_end": "2026-01-01",
                "source_timezone": "UTC",
                "currency": "USD",
            }
        )
    )
    return path


def _meta_report_input(root: Path) -> Path:
    path = root / "meta-report.json"
    path.write_text(
        json.dumps(
            {
                "account_ref": "account_fixture",
                "report_id": "report.meta.forged",
                "query_contract_hash": "b" * 64,
                "window_start": "2026-01-01",
                "window_end": "2026-01-01",
                "source_timezone": "UTC",
                "currency": "USD",
                "action_attribution_windows": ["7d_click"],
            }
        )
    )
    return path


def _account_response() -> dict[str, object]:
    return {
        "id": "account_fixture",
        "name": "Private account name must not leave the adapter",
        "url": "https://example.invalid/account",
        "preview_url": None,
        "status": "active",
        "timezone": "UTC",
        "currency_code": "USD",
        "review": {"status": "approved"},
    }


def _conversion_page() -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {
                "id": "setting_fixture",
                "name": "Private conversion name discarded",
                "event_type": "order_created",
                "custom_event_name": None,
                "attribution_window_days": 30,
                "ad_account_id": "account_fixture",
                "source_ids": ["source_fixture"],
                "sources": [{"id": "source_fixture", "name": "Private source name"}],
                "campaigns": [],
                "archived": False,
                "version": 1,
            }
        ],
        "first_id": "setting_fixture",
        "last_id": "setting_fixture",
        "has_more": False,
    }


def _install_openai_transport(
    monkeypatch: pytest.MonkeyPatch, module: object, *, conversion: bool
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/ad_account":
            return httpx.Response(200, json=_account_response())
        if conversion:
            assert request.url.path == "/v1/conversions/event_settings"
            return httpx.Response(200, json=_conversion_page())
        assert request.url.path == "/v1/ad_account/insights"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "count": 1,
                "data": [
                    {
                        "id": "row_fixture",
                        "readable_time": "2026-01-01",
                        "start_time": 1767225600,
                        "end_time": 1767312000,
                        "campaign_id": "campaign_fixture",
                        "impressions": 2,
                        "clicks": 1,
                        "spend": 1.25,
                    }
                ],
                "has_more": False,
                "first_id": "row_fixture",
                "last_id": "row_fixture",
            },
        )

    actual_client = httpx.Client
    monkeypatch.setattr(
        module.httpx,  # type: ignore[attr-defined]
        "Client",
        lambda **kwargs: actual_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return requests


def test_installed_schema_and_config_add_support_only_the_key_profile_shape(tmp_path: Path) -> None:
    runner = CliRunner()
    schema = runner.invoke(app, ["schema", "openai-account-key-profile"])
    assert schema.exit_code == 0, schema.output
    properties = json.loads(schema.output)["data"]["schema"]["properties"]
    assert properties["schema_version"]["const"] == 1
    assert properties["profile_kind"]["const"] == "OPENAI_ADS_ACCOUNT_KEY"
    assert {"principal_ref", "client_ref", "scopes", "secondary_credential_ref"}.isdisjoint(
        properties
    )
    config_fragment = runner.invoke(
        app, ["schema", "config", "--definition", "OpenAIAccountKeyProfile"]
    )
    assert config_fragment.exit_code == 0, config_fragment.output
    assert (
        json.loads(config_fragment.output)["data"]["schema"]["properties"]["profile_kind"]["const"]
        == "OPENAI_ADS_ACCOUNT_KEY"
    )

    source = tmp_path / "account-key-profile.json"
    source.write_text(_account_key_profile().model_dump_json())
    data_root = tmp_path / "data"
    added = runner.invoke(
        app,
        ["--data-dir", str(data_root), "config", "add-profile", "--input", str(source)],
    )
    assert added.exit_code == 0, added.output
    assert json.loads(added.output)["data"] == {
        "provider": "openai_ads",
        "purpose": "READ",
        "credential_stored": False,
    }
    assert "account_fixture" not in added.output and "openai-key-slot" not in added.output
    configured = load_configuration(data_root)
    assert isinstance(configured.profiles[0], OpenAIAccountKeyProfile)


@pytest.mark.parametrize("invalid_version", [1.0, True, "1"])
def test_account_key_schema_version_rejects_float_bool_and_string(
    invalid_version: object, tmp_path: Path
) -> None:
    raw = _account_key_profile().model_dump(mode="json")
    raw["schema_version"] = invalid_version
    with pytest.raises(ValidationError):
        validate_configured_profile_json(json.dumps(raw))

    source = tmp_path / "invalid-profile.json"
    source.write_text(json.dumps(raw))
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "config", "add-profile", "--input", str(source)],
    )
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert not (tmp_path / "data" / "config.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_ref", "bad.account"),
        ("timezone", "Not/AZone"),
        ("provider", "meta_ads"),
        ("purpose", "UNSUPPORTED"),
    ],
)
def test_account_key_native_account_and_timezone_contract_are_closed(
    field: str, value: str
) -> None:
    raw = _account_key_profile().model_dump(mode="json")
    raw[field] = value
    with pytest.raises(ValidationError):
        validate_configured_profile_json(json.dumps(raw))


def test_forged_configuration_cannot_reach_inventory_key_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forged = OpenAIAccountKeyProfile.model_construct(
        **{**_account_key_profile().model_dump(mode="python"), "account_ref": "bad.account"}
    )
    configuration = Configuration.model_construct(profiles=[forged])
    monkeypatch.setattr(inventory, "load_configuration", lambda _root: configuration)
    monkeypatch.setattr(
        inventory,
        "export_key",
        lambda: pytest.fail("forged profile reached private sharing-key access"),
    )
    monkeypatch.setattr(
        inventory.httpx,
        "Client",
        lambda **_kwargs: pytest.fail("forged profile reached HTTP client construction"),
    )

    with pytest.raises(AdhelmError) as raised:
        inventory.run_inventory(tmp_path, "openai_ads", "openai-key-read")
    assert raised.value.code == "CONFIGURATION_INVALID"


def test_forged_configuration_cannot_reach_report_key_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forged = OpenAIAccountKeyProfile.model_construct(
        **{**_account_key_profile().model_dump(mode="python"), "account_ref": "bad.account"}
    )
    configuration = Configuration.model_construct(profiles=[forged])
    monkeypatch.setattr(workflows, "load_configuration", lambda _root: configuration)
    monkeypatch.setattr(
        workflows,
        "export_key",
        lambda: pytest.fail("forged profile reached private sharing-key access"),
    )
    monkeypatch.setattr(
        workflows.httpx,
        "Client",
        lambda **_kwargs: pytest.fail("forged profile reached HTTP client construction"),
    )

    with pytest.raises(AdhelmError) as raised:
        workflows.run_report(
            tmp_path,
            "openai_ads",
            "openai-key-read",
            _report_input(tmp_path, "bad.account"),
            ephemeral=True,
        )
    assert raised.value.code == "CONFIGURATION_INVALID"


def test_constructed_wrong_provider_cannot_reach_report_key_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forged = OpenAIAccountKeyProfile.model_construct(
        **{**_account_key_profile().model_dump(mode="python"), "provider": "meta_ads"}
    )
    monkeypatch.setattr(
        workflows,
        "load_configuration",
        lambda _root: Configuration.model_construct(profiles=[forged]),
    )
    monkeypatch.setattr(
        workflows,
        "export_key",
        lambda: pytest.fail("wrong-provider profile reached private sharing-key access"),
    )
    monkeypatch.setattr(
        workflows.httpx,
        "Client",
        lambda **_kwargs: pytest.fail("wrong-provider profile reached HTTP client construction"),
    )

    with pytest.raises(AdhelmError) as raised:
        workflows.run_report(
            tmp_path,
            "meta_ads",
            "openai-key-read",
            _meta_report_input(tmp_path),
            ephemeral=True,
        )
    assert raised.value.code == "CONFIGURATION_INVALID"


def test_wrong_purpose_and_constructed_kind_fail_before_conversion_credential_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong_purpose = _account_key_profile(purpose="EDIT")
    save_configuration(tmp_path, Configuration(profiles=[wrong_purpose]))
    monkeypatch.setattr(
        openai_measurement_workflow,
        "credential",
        lambda *_args, **_kwargs: pytest.fail("wrong purpose reached credential access"),
    )
    monkeypatch.setattr(
        openai_measurement_workflow.httpx,
        "Client",
        lambda **_kwargs: pytest.fail("wrong purpose reached HTTP client construction"),
    )
    with pytest.raises(AdhelmError) as raised:
        openai_measurement_workflow.read_openai_conversions(tmp_path, "openai-key-read")
    assert raised.value.code == "READ_PROFILE_REQUIRED"

    forged = OpenAIAccountKeyProfile.model_construct(
        **{**_account_key_profile().model_dump(mode="python"), "profile_kind": "FORGED"}
    )
    monkeypatch.setattr(
        openai_measurement_workflow,
        "load_configuration",
        lambda _root: Configuration.model_construct(profiles=[forged]),
    )
    with pytest.raises(AdhelmError) as raised:
        openai_measurement_workflow.read_openai_conversions(tmp_path, "openai-key-read")
    assert raised.value.code == "CONFIGURATION_INVALID"


def test_account_key_credential_ci_alias_reserved_and_secondary_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_profile = _account_key_profile()
    monkeypatch.setattr(
        credentials,
        "_backend",
        lambda: pytest.fail("account-key secondary lookup opened the credential store"),
    )
    with pytest.raises(TypeError):
        credentials.credential(key_profile, secondary=True)  # type: ignore[call-arg]

    ci_profile = _account_key_profile(
        credential_source="EXPLICIT_CI", credential_ref="OPENAI_ACCOUNT_KEY_CI"
    )
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("OPENAI_ACCOUNT_KEY_CI", "fixture-value-not-for-output")
    assert credentials.credential(ci_profile, ci_enabled=True) == "fixture-value-not-for-output"
    with pytest.raises(AdhelmError) as ci_disabled:
        credentials.credential(ci_profile, ci_enabled=False)
    assert ci_disabled.value.code == "CI_OPT_IN_REQUIRED"

    aliased = Configuration(
        profiles=[
            key_profile.model_copy(update={"credential_ref": "Shared-Slot"}),
            _legacy_profile(profile_id="legacy-read", credential_ref="shared-slot"),
        ]
    )
    monkeypatch.setattr(
        credential_setup,
        "os_store_backend",
        lambda: pytest.fail("alias check opened the credential store"),
    )
    with pytest.raises(AdhelmError) as alias:
        credential_setup.credential_status(aliased, key_profile.profile_id)
    assert alias.value.code == "CREDENTIAL_REFERENCE_ALIAS"

    monkeypatch.setattr(
        credential_setup,
        "prompt_secret_from_terminal",
        lambda: pytest.fail("account-key secondary setup prompted for a secret"),
    )
    with pytest.raises(TypeError):
        credential_setup.store_interactive(  # type: ignore[call-arg]
            Configuration(profiles=[key_profile]),
            key_profile.profile_id,
            "SECONDARY",
        )

    reserved = OpenAIAccountKeyProfile.model_construct(
        **{
            **key_profile.model_dump(mode="python"),
            "credential_ref": "EXPORT-PSEUDONYMIZATION-V1",
        }
    )
    with pytest.raises(AdhelmError) as reserved_failure:
        credentials.credential(reserved)
    assert reserved_failure.value.code == "PROFILE_INVALID"


def test_account_key_auth_status_is_installed_and_does_not_expose_local_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _account_key_profile(
        profile_id="private-profile-alias", credential_ref="private-key-alias"
    )
    save_configuration(tmp_path, Configuration(profiles=[profile]))

    class EmptyStore:
        def get_password(self, _service: str, _reference: str) -> None:
            return None

    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: EmptyStore())
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path), "auth", "status", "--profile", profile.profile_id],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["slots"] == [
        {
            "provider": "openai_ads",
            "slot": "PRIMARY",
            "state": "MISSING",
            "operation": "STATUS",
            "remote_revocation_verified": False,
        }
    ]
    assert profile.profile_id not in result.output and profile.credential_ref not in result.output


def test_account_key_profile_composes_with_conversion_read_without_raw_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _account_key_profile(credential_source="EXPLICIT_CI")
    save_configuration(tmp_path, Configuration(profiles=[profile]))
    requests = _install_openai_transport(monkeypatch, openai_measurement_workflow, conversion=True)
    credential_calls: list[OpenAIAccountKeyProfile] = []

    def fixture_credential(selected: OpenAIAccountKeyProfile, *, ci_enabled: bool) -> str:
        credential_calls.append(selected)
        assert ci_enabled is True
        return "fixture-value-not-for-output"

    monkeypatch.setattr(openai_measurement_workflow, "credential", fixture_credential)
    identity, settings = openai_measurement_workflow.read_openai_conversions(
        tmp_path, profile.profile_id, maximum_records=1, page_size=1, ci_enabled=True
    )
    assert identity.account_client_ref == profile.account_ref
    assert [setting.conversion_setting_client_ref for setting in settings] == ["setting_fixture"]
    assert credential_calls == [profile]
    assert [request.url.path for request in requests] == [
        "/v1/ad_account",
        "/v1/conversions/event_settings",
    ]


def test_account_key_profile_composes_with_inventory_and_report_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _account_key_profile()
    save_configuration(tmp_path, Configuration(profiles=[profile]))
    identity = OpenAIAdsIdentity(
        account_ref="openai_ads.account.account_fixture",
        account_client_ref="account_fixture",
        timezone="UTC",
        currency="USD",
        status="active",
    )

    def fake_inventory(
        _client: httpx.Client,
        selected: OpenAIAccountKeyProfile,
        _getter: object,
        _maximum_records: int,
    ) -> tuple[list[dict[str, object]], dict[str, str | None]]:
        assert selected == profile
        return [inventory._account_snapshot(identity)], {
            "timezone": "UTC",
            "currency": "USD",
            "status": "active",
        }

    monkeypatch.setattr(inventory, "_openai_inventory", fake_inventory)
    monkeypatch.setattr(inventory, "export_key", lambda: b"k" * 32)
    inventory_result = inventory.run_inventory(
        tmp_path, "openai_ads", profile.profile_id, ephemeral=True
    )
    assert inventory_result["account_ref"].startswith("opaque:")
    assert profile.account_ref not in json.dumps(inventory_result)

    _install_openai_transport(monkeypatch, workflows, conversion=False)
    monkeypatch.setattr(
        workflows, "credential", lambda *_args, **_kwargs: "fixture-value-not-for-output"
    )
    monkeypatch.setattr(workflows, "export_key", lambda: b"p" * 32)
    report = workflows.run_report(
        tmp_path,
        "openai_ads",
        profile.profile_id,
        _report_input(tmp_path),
        ephemeral=True,
    )
    rendered = json.dumps(report)
    assert report["envelope"]["source_account_ref"].startswith("opaque:")
    assert profile.account_ref not in rendered
    assert "fixture-value-not-for-output" not in rendered
