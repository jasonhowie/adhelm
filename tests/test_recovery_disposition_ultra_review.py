"""Independent boundary regressions for GA4 CREATE's no-compensation disposition."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adhelm.approval_protocol import ApprovalHostConfiguration, build_openai_pause_challenge
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.plan_records import revalidate_openai_pause_action
from adhelm.providers.openai_pause_contract import OpenAIPausePlannedAction
from adhelm.safety import CompositePlan, OperationKind, SafetyKernel, SubPlan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures"
)
_DISPOSITION = "RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION"
_NOW = datetime(2030, 1, 1, tzinfo=UTC)


class _ExplodingLedger:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"invalid plan must not access ledger.{name}")


def _fixture_data(name: str) -> dict[str, Any]:
    loaded = json.loads((_FIXTURES / name).read_bytes())
    assert type(loaded) is dict
    return loaded


def _legacy_plan_data() -> dict[str, Any]:
    plan = _fixture_data("openai-pause-action-v1.json")["plan"]
    assert type(plan) is dict
    copied = json.loads(json.dumps(plan))
    assert type(copied) is dict
    return copied


def _ga4_subplan_data() -> dict[str, Any]:
    plan = _legacy_plan_data()
    subplans = plan["subplans"]
    assert type(subplans) is list and len(subplans) == 1 and type(subplans[0]) is dict
    subplan = dict(subplans[0])
    subplan.update(
        provider="ga4_admin",
        operation="CREATE",
        recovery_actions=[],
        recovery_disposition=_DISPOSITION,
        account={
            "account_ref": "ga4_admin.property.123",
            "stable_account_id": "123",
            "auth_profile_ref": "ga4-edit",
            "principal_ref": "synthetic.principal",
            "client_application_ref": "synthetic.client",
            "scopes": ["https://www.googleapis.com/auth/analytics.edit"],
        },
    )
    return subplan


def _ga4_plan() -> CompositePlan:
    plan = _legacy_plan_data()
    plan["subplans"] = [_ga4_subplan_data()]
    return CompositePlan.model_validate_json(json.dumps(plan))


def _rejects_at_kernel(tmp_path: Path, plan: CompositePlan) -> None:
    kernel = SafetyKernel(tmp_path, _ExplodingLedger(), clock=lambda: _NOW)
    with pytest.raises(AdhelmError) as raised:
        # Plan revalidation must occur before authorization, ledger, or adapter use.
        kernel.execute(plan, object(), {})  # type: ignore[arg-type]
    assert raised.value.code == "INVALID_SAFETY_INPUT"


def _nonce(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def test_legacy_openai_action_and_challenge_vectors_remain_exact() -> None:
    action_bytes = (_FIXTURES / "openai-pause-action-v1.json").read_bytes()
    action = revalidate_openai_pause_action(
        OpenAIPausePlannedAction.model_validate_json(action_bytes)
    )
    assert action.plan.subplans[0].recovery_disposition is None
    assert action.plan.subplans[0].recovery_actions == (OperationKind.PAUSE,)
    assert canonical_bytes(action) == action_bytes

    challenge_data = _fixture_data("openai-pause-challenge-v1.json")
    challenge = build_openai_pause_challenge(
        action,
        grantor_ref="customer-grantor-1",
        expires_at=_NOW + timedelta(minutes=4),
        configuration=ApprovalHostConfiguration(
            host_ref="host:adhelm-macos-approval-v1",
            signing_key_ref="key:approval-test-v1",
        ),
        nonce=_nonce(str(challenge_data["nonce"])),
        issued_at=_NOW,
        now=_NOW,
    )
    assert challenge.canonical_bytes == (_FIXTURES / "openai-pause-challenge-v1.json").read_bytes()


def test_disposition_is_only_the_explicit_empty_ga4_create_case_in_model_and_schema() -> None:
    valid = SubPlan.model_validate_json(json.dumps(_ga4_subplan_data()))
    assert valid.recovery_actions == ()
    assert valid.recovery_disposition == _DISPOSITION

    for changes in (
        {"recovery_disposition": None},
        {"recovery_disposition": "DELETE"},
        {"provider": "google_ads"},
        {"operation": "DELETE"},
        {"recovery_actions": ["DELETE"]},
    ):
        with pytest.raises(ValidationError):
            SubPlan.model_validate_json(json.dumps({**_ga4_subplan_data(), **changes}))

    schema = SubPlan.model_json_schema()
    assert schema["properties"]["recovery_disposition"] == {
        "type": "string",
        "const": _DISPOSITION,
    }
    condition = schema["allOf"][0]
    assert condition["if"] == {"required": ["recovery_disposition"]}
    assert condition["then"]["properties"]["provider"] == {"const": "ga4_admin"}
    assert condition["then"]["properties"]["operation"] == {"const": "CREATE"}
    assert condition["then"]["properties"]["recovery_actions"] == {"maxItems": 0}
    assert condition["else"]["properties"]["recovery_actions"] == {"minItems": 1}


def test_kernel_revalidates_constructed_and_copied_recovery_bypasses_before_side_effects(
    tmp_path: Path,
) -> None:
    valid = _ga4_plan()
    subplan = valid.subplans[0]
    forged_subplans = (
        subplan.model_copy(update={"provider": "google_ads"}),
        subplan.model_copy(update={"recovery_actions": (OperationKind.DELETE,)}),
        SubPlan.model_construct(**{**subplan.__dict__, "operation": OperationKind.DELETE}),
    )

    for forged_subplan in forged_subplans:
        forged_plan = CompositePlan.model_construct(
            **{**valid.__dict__, "subplans": (forged_subplan,)}
        )
        _rejects_at_kernel(tmp_path, forged_plan)

    legacy = CompositePlan.model_validate_json(json.dumps(_legacy_plan_data()))
    forged_legacy = legacy.model_copy(
        update={"subplans": (legacy.subplans[0].model_copy(update={"recovery_actions": ()}),)}
    )
    _rejects_at_kernel(tmp_path, forged_legacy)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", True),
        ("schema_version", 1.0),
        ("stop_on_non_verified", 1),
        ("stop_on_non_verified", 1.0),
    ),
)
def test_plan_control_scalars_are_exact_in_json_and_at_kernel_boundary(
    tmp_path: Path, field: str, value: bool | float
) -> None:
    raw = _legacy_plan_data()
    raw[field] = value
    with pytest.raises(ValidationError):
        CompositePlan.model_validate_json(json.dumps(raw))

    valid = CompositePlan.model_validate_json(json.dumps(_legacy_plan_data()))
    forged = valid.model_copy(update={field: value})
    _rejects_at_kernel(tmp_path, forged)
