"""Workspace-routing regressions for the fixed OpenAI pause action."""

from __future__ import annotations

import traceback
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_openai_pause import NOW, _action, _credentials, _Scenario

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.plan_records import revalidate_openai_pause_action
from adhelm.providers.openai_ads import OpenAIAdsClient
from adhelm.providers.openai_pause import (
    OpenAIPauseCredentials,
    OpenAIPausePlannedAction,
    build_openai_pause_plan,
    plan_openai_campaign_pause,
)
from adhelm.saved_plans import load_openai_pause_plan, retain_openai_pause_plan


def _planner(scenario: _Scenario) -> tuple[OpenAIAdsClient, OpenAIPauseCredentials]:
    credentials = _credentials()
    return OpenAIAdsClient(scenario.transport(), lambda: credentials.secret), credentials


def test_legacy_action_omits_workspace_binding_and_keeps_its_exact_digest() -> None:
    action, _ = _action(_Scenario())

    assert action.workspace_binding is None
    assert "workspace_binding" not in action.model_dump(mode="json")
    assert action.plan.profile_ref == action.binding.profile_ref == "profile"
    assert digest(action) == "75ea6e147c869940bf966c5139b209bdc1f9e4eb7c278a56f2ad78cbd6792f4c"

    reparsed = OpenAIPausePlannedAction.model_validate_json(action.model_dump_json())
    rebuilt = build_openai_pause_plan(action.binding, expires_at=action.plan.expires_at)
    assert "workspace_binding" not in reparsed.model_fields_set
    assert reparsed.model_dump(mode="json") == action.model_dump(mode="json")
    assert digest(reparsed) == digest(rebuilt) == digest(action)
    assert "workspace_binding" not in rebuilt.model_dump(mode="json")

    copied = action.model_copy()
    constructed = OpenAIPausePlannedAction.model_construct(
        schema_version=1,
        status=action.status,
        binding=action.binding,
        plan=action.plan,
    )
    for compatible in (copied, constructed):
        assert "workspace_binding" not in compatible.model_dump(mode="json")
        assert digest(revalidate_openai_pause_action(compatible)) == digest(action)


def test_explicit_workspace_binding_routes_the_plan_without_replacing_auth_profile(
    tmp_path: Path,
) -> None:
    scenario = _Scenario()
    client, credentials = _planner(scenario)
    action = plan_openai_campaign_pause(
        client=client,
        credentials=credentials,
        expected_account_client_ref="account_fixture",
        campaign_client_ref="campaign_fixture",
        expires_at=NOW + timedelta(minutes=10),
        workspace_profile_ref="workspace-default",
        expected_account_currency="USD",
        expected_account_timezone="America/Chicago",
        clock=lambda: NOW,
    )

    assert action.workspace_binding is not None
    assert action.workspace_binding.model_dump(mode="json") == {
        "schema_version": 1,
        "binding_kind": "LOCAL_LEDGER_WORKSPACE",
        "workspace_profile_ref": "workspace-default",
    }
    assert action.plan.profile_ref == "workspace-default"
    assert action.binding.profile_ref == "profile"
    assert action.plan.subplans[0].account.auth_profile_ref == "profile"
    payload = action.model_dump(mode="json")
    assert payload["workspace_binding"] == action.workspace_binding.model_dump(mode="json")
    assert scenario.requests == [
        "GET /v1/ad_account",
        "GET /v1/campaigns",
        "GET /v1/campaigns/campaign_fixture",
    ]

    ledger = Ledger(tmp_path, profile_ref="workspace-default")
    receipt = retain_openai_pause_plan(ledger, action)
    loaded, provenance = load_openai_pause_plan(ledger, receipt["record_digest"])
    assert loaded == action
    assert provenance["plan_digest"] == action.plan.plan_digest


@pytest.mark.parametrize(
    "workspace",
    [None, True, False, 1, 1.0, "unsafe/path", "unsafe\\path", "unsafe\npath", "unsafe\x7fpath"],
)
def test_invalid_workspace_argument_fails_before_provider_discovery(workspace: object) -> None:
    scenario = _Scenario()
    client, credentials = _planner(scenario)

    with pytest.raises(AdhelmError) as error:
        plan_openai_campaign_pause(
            client=client,
            credentials=credentials,
            expected_account_client_ref="account_fixture",
            campaign_client_ref="campaign_fixture",
            expires_at=NOW + timedelta(minutes=10),
            workspace_profile_ref=workspace,
            clock=lambda: NOW,
        )

    assert error.value.code == "INVALID_INPUT"
    assert scenario.requests == []


@pytest.mark.parametrize("workspace", [None, True, False, [], "not-a-binding"])
def test_explicit_nonobject_workspace_field_cannot_become_a_legacy_action(
    workspace: object,
) -> None:
    legacy, _ = _action(_Scenario())
    raw = legacy.model_dump(mode="json")
    raw["workspace_binding"] = workspace

    with pytest.raises(ValidationError):
        OpenAIPausePlannedAction.model_validate(raw)
    with pytest.raises(AdhelmError) as error:
        revalidate_openai_pause_action(raw)
    assert error.value.code == "INVALID_EXECUTION_PLAN"


@pytest.mark.parametrize("workspace_ref", ["unsafe\npath", "unsafe\x00path", "unsafe\x7fpath"])
def test_explicit_workspace_control_characters_cannot_be_retained(workspace_ref: str) -> None:
    legacy, _ = _action(_Scenario())
    raw = legacy.model_dump(mode="json")
    raw["workspace_binding"] = {
        "schema_version": 1,
        "binding_kind": "LOCAL_LEDGER_WORKSPACE",
        "workspace_profile_ref": workspace_ref,
    }

    with pytest.raises(ValidationError):
        OpenAIPausePlannedAction.model_validate(raw)
    with pytest.raises(AdhelmError) as error:
        revalidate_openai_pause_action(raw)
    assert error.value.code == "INVALID_EXECUTION_PLAN"


@pytest.mark.parametrize("schema_version", [True, 1.0, "1"])
def test_workspace_binding_version_is_exact_and_model_copy_cannot_normalize_it(
    schema_version: object,
) -> None:
    legacy, _ = _action(_Scenario())
    workspace_action = build_openai_pause_plan(
        legacy.binding,
        expires_at=legacy.plan.expires_at,
        workspace_profile_ref="workspace-default",
    )
    raw = workspace_action.model_dump(mode="json")
    raw["workspace_binding"]["schema_version"] = schema_version

    with pytest.raises(ValidationError):
        OpenAIPausePlannedAction.model_validate(raw)

    for forged in (
        legacy.model_copy(update={"workspace_binding": None}),
        legacy.model_copy(update={"workspace_binding": True}),
        OpenAIPausePlannedAction.model_construct(
            schema_version=1,
            status=legacy.status,
            binding=legacy.binding,
            workspace_binding=None,
            plan=legacy.plan,
        ),
    ):
        with pytest.raises(AdhelmError) as error:
            revalidate_openai_pause_action(forged)
        assert error.value.code == "INVALID_EXECUTION_PLAN"


@pytest.mark.parametrize(
    ("currency", "timezone"),
    [
        ("USD", None),
        (None, "UTC"),
        ("usd", "UTC"),
        ("USD", "not/a/timezone"),
        (True, "UTC"),
        ("USD", True),
    ],
)
def test_invalid_or_partial_expected_metadata_fails_before_provider_discovery(
    currency: object,
    timezone: object,
) -> None:
    scenario = _Scenario()
    client, credentials = _planner(scenario)

    with pytest.raises(AdhelmError) as error:
        plan_openai_campaign_pause(
            client=client,
            credentials=credentials,
            expected_account_client_ref="account_fixture",
            campaign_client_ref="campaign_fixture",
            expires_at=NOW + timedelta(minutes=10),
            expected_account_currency=currency,  # type: ignore[arg-type]
            expected_account_timezone=timezone,  # type: ignore[arg-type]
            clock=lambda: NOW,
        )

    assert error.value.code == "INVALID_INPUT"
    assert scenario.requests == []


@pytest.mark.parametrize(
    ("field", "value", "currency", "timezone"),
    [
        ("currency_code", "EUR", "USD", "America/Chicago"),
        ("timezone", "UTC", "USD", "America/Chicago"),
    ],
)
def test_discovered_metadata_mismatch_stops_before_campaign_inventory(
    field: str, value: str, currency: str, timezone: str
) -> None:
    scenario = _Scenario()
    scenario.account[field] = value
    client, credentials = _planner(scenario)

    with pytest.raises(AdhelmError) as error:
        plan_openai_campaign_pause(
            client=client,
            credentials=credentials,
            expected_account_client_ref="account_fixture",
            campaign_client_ref="campaign_fixture",
            expires_at=NOW + timedelta(minutes=10),
            workspace_profile_ref="workspace-default",
            expected_account_currency=currency,
            expected_account_timezone=timezone,
            clock=lambda: NOW,
        )

    assert error.value.code == "IDENTITY_MISMATCH"
    assert scenario.requests == ["GET /v1/ad_account"]


@pytest.mark.parametrize(
    "expiry",
    ["PRIVATE-UNTRUSTED-EXPIRY", True, None, datetime(2030, 1, 1)],
)
def test_pure_builder_sanitizes_invalid_expiry_before_reconstruction(expiry: object) -> None:
    legacy, _ = _action(_Scenario())

    with pytest.raises(AdhelmError) as error:
        build_openai_pause_plan(
            legacy.binding,
            expires_at=expiry,  # type: ignore[arg-type]
            workspace_profile_ref="workspace-default",
        )

    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.code == "INVALID_INPUT"
    assert "PRIVATE-UNTRUSTED-EXPIRY" not in str(error.value)
    assert "PRIVATE-UNTRUSTED-EXPIRY" not in rendered


def test_pure_builder_sanitizes_malformed_constructed_binding() -> None:
    class UntrustedBindingValue:
        def __repr__(self) -> str:
            return "PRIVATE-UNTRUSTED-BINDING"

    legacy, _ = _action(_Scenario())
    malformed = legacy.binding.model_copy(update={"account_ref": UntrustedBindingValue()})

    with pytest.raises(AdhelmError) as error:
        build_openai_pause_plan(
            malformed,
            expires_at=legacy.plan.expires_at,
            workspace_profile_ref="workspace-default",
        )

    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.code == "INVALID_INPUT"
    assert "PRIVATE-UNTRUSTED-BINDING" not in str(error.value)
    assert "PRIVATE-UNTRUSTED-BINDING" not in rendered
