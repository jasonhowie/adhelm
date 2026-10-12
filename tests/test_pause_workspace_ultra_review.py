"""Independent adversarial checks for workspace routing and legacy plan compatibility."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_execution_plan_ultra_review import _action as legacy_fixture
from test_openai_pause import NOW, _action, _Host, _Scenario

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.providers.openai_pause import OpenAIPauseAdapter
from adhelm.providers.openai_pause_contract import (
    OpenAIPausePlannedAction,
    build_openai_pause_plan,
)
from adhelm.safety import EffectStatus, SafetyKernel, new_direct_grant
from adhelm.saved_plans import load_openai_pause_plan, retain_openai_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


def test_legacy_action_wire_digest_stays_identical_across_new_workspace_feature() -> None:
    legacy = legacy_fixture()
    assert "workspace_binding" not in legacy.model_dump(mode="json")
    assert digest(legacy) == "23ffca944521091fd8367b4f13af454755ad7dd326cb7521aa4139ee63d4fa3e"
    rebuilt = build_openai_pause_plan(legacy.binding, expires_at=legacy.plan.expires_at)
    assert rebuilt.model_dump_json() == legacy.model_dump_json()
    assert digest(rebuilt) == digest(legacy)


@pytest.mark.parametrize("value", [None, True, 1, 1.0, [], {}, ""])
def test_explicit_invalid_workspace_cannot_turn_into_legacy_routing(value: Any) -> None:
    legacy = legacy_fixture()
    with pytest.raises(AdhelmError):
        build_openai_pause_plan(
            legacy.binding, expires_at=legacy.plan.expires_at, workspace_profile_ref=value
        )


@pytest.mark.parametrize("value", [None, True, [], "default"])
def test_invalid_workspace_wire_value_is_rejected(value: Any) -> None:
    raw = legacy_fixture().model_dump(mode="json")
    raw["workspace_binding"] = value
    with pytest.raises(ValidationError):
        OpenAIPausePlannedAction.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("value", [True, 1.0])
def test_rehashed_workspace_literal_coercion_cannot_survive_retention(value: Any) -> None:
    legacy = legacy_fixture()
    new = build_openai_pause_plan(
        legacy.binding, expires_at=legacy.plan.expires_at, workspace_profile_ref="default"
    )
    raw = new.model_dump(mode="json")
    raw["workspace_binding"]["schema_version"] = value
    with pytest.raises(ValidationError):
        OpenAIPausePlannedAction.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("value", ["PRIVATE-UNTRUSTED-EXPIRY", None, True, datetime(2030, 1, 1)])
def test_pure_plan_builder_sanitizes_bad_expiry_before_reconstruction(value: Any) -> None:
    with pytest.raises(AdhelmError) as error:
        build_openai_pause_plan(
            legacy_fixture().binding, expires_at=value, workspace_profile_ref="default"
        )
    assert "PRIVATE-UNTRUSTED-EXPIRY" not in str(error.value)


def test_workspace_plan_retains_in_shared_ledger_and_runs_existing_guarded_fixture(
    tmp_path: Path,
) -> None:
    scenario = _Scenario()
    legacy, credentials = _action(scenario)
    action = build_openai_pause_plan(
        legacy.binding, expires_at=legacy.plan.expires_at, workspace_profile_ref="default"
    )
    assert action.plan.plan_digest != legacy.plan.plan_digest
    assert action.plan.subplan_digests == legacy.plan.subplan_digests
    assert action.binding.profile_ref == "profile"
    assert action.plan.subplans[0].account.auth_profile_ref == "profile"
    ledger = Ledger(tmp_path)
    retained = retain_openai_pause_plan(ledger, action)
    loaded, _ = load_openai_pause_plan(ledger, retained["record_digest"])
    assert loaded == action
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: NOW)
    grant = new_direct_grant(
        authorization_id="workspace-review-grant",
        plan=loaded.plan,
        grantor_ref="owner",
        host_ref="fixture-host",
        user_interaction_ref="fixture-interaction",
        expires_at=NOW + timedelta(minutes=5),
    )
    kernel.record_direct_grant(loaded.plan, grant, _Host())
    result = kernel.execute(
        loaded.plan,
        grant,
        {
            "openai_ads": OpenAIPauseAdapter(
                loaded, scenario.transport(), lambda: credentials, ledger, clock=lambda: NOW
            )
        },
    )
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert scenario.posts == 1
    assert not ledger.unresolved_starts()


def test_workspace_bound_plan_cannot_be_retained_in_credential_role_ledger(tmp_path: Path) -> None:
    legacy = legacy_fixture()
    action = build_openai_pause_plan(
        legacy.binding, expires_at=legacy.plan.expires_at, workspace_profile_ref="default"
    )
    ledger = Ledger(tmp_path, profile_ref=legacy.binding.profile_ref)
    with pytest.raises(AdhelmError):
        retain_openai_pause_plan(ledger, action)
    assert ledger.records("changes") == []
