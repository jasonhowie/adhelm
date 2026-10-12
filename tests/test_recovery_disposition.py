"""Explicit no-compensation recovery without changing existing plan bytes."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from adhelm.canonical import canonical_bytes, digest
from adhelm.safety import CompositePlan, SubPlan

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/openai-pause-action-v1.json"
)
_DISPOSITION = "RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION"


def _raw() -> dict:
    return json.loads(_FIXTURE.read_bytes())["plan"]["subplans"][0]


def _ga4() -> dict:
    raw = _raw()
    raw.update(
        provider="ga4_admin",
        operation="CREATE",
        recovery_actions=[],
        recovery_disposition=_DISPOSITION,
    )
    raw["account"] = {
        "account_ref": "ga4_admin.property.123",
        "stable_account_id": "123",
        "auth_profile_ref": "ga4-edit",
        "principal_ref": "synthetic.principal",
        "client_application_ref": "synthetic.client",
        "scopes": ["https://www.googleapis.com/auth/analytics.edit"],
    }
    return raw


def test_existing_subplan_and_composite_keep_exact_canonical_bytes() -> None:
    raw = json.loads(_FIXTURE.read_bytes())["plan"]
    checked = CompositePlan.model_validate_json(json.dumps(raw))
    assert canonical_bytes(checked) == canonical_bytes(raw)
    assert checked.plan_digest == digest(raw)
    assert "recovery_disposition" not in checked.model_dump(mode="json")["subplans"][0]


def test_explicit_ga4_disposition_is_digest_bound_without_mutation_compensation() -> None:
    raw = _ga4()
    checked = SubPlan.model_validate_json(json.dumps(raw))
    assert checked.recovery_actions == ()
    assert checked.recovery_disposition == _DISPOSITION
    assert canonical_bytes(checked) == canonical_bytes(raw)
    assert checked.subplan_digest != digest(
        {k: v for k, v in raw.items() if k != "recovery_disposition"}
    )
    schema = SubPlan.model_json_schema()
    assert schema["properties"]["recovery_disposition"] == {
        "type": "string",
        "const": _DISPOSITION,
    }
    condition = schema["allOf"][0]
    assert condition["if"] == {"required": ["recovery_disposition"]}
    assert condition["then"]["properties"] == {
        "provider": {"const": "ga4_admin"},
        "operation": {"const": "CREATE"},
        "recovery_actions": {"maxItems": 0},
    }
    assert condition["else"]["properties"]["recovery_actions"]["minItems"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"recovery_disposition": None},
        {"recovery_disposition": "DELETE"},
        {"recovery_actions": ["DELETE"]},
        {"recovery_actions": ["PAUSE"]},
        {"provider": "google_ads"},
        {"operation": "DELETE"},
    ],
)
def test_disposition_cannot_hide_remote_recovery_or_broaden_supported_operation(
    changes: dict,
) -> None:
    raw = {**_ga4(), **changes}
    with pytest.raises(ValidationError):
        SubPlan.model_validate_json(json.dumps(raw))


def test_empty_legacy_recovery_is_still_rejected_by_model_and_schema() -> None:
    raw = _raw()
    raw["recovery_actions"] = []
    with pytest.raises(ValidationError):
        SubPlan.model_validate_json(json.dumps(raw))
