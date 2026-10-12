"""Swift's checked-in protocol vectors must equal current Python safety contracts."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes
from adhelm.plan_records import revalidate_openai_pause_action
from adhelm.plan_views import preview_openai_pause
from adhelm.providers.openai_pause_contract import OpenAIPausePlannedAction
from adhelm.safety import DelegationPolicy

_FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures"
)


def test_native_vectors_regenerate_exactly_from_current_python_models(tmp_path: Path) -> None:
    path = _FIXTURES / "generate_safety_vectors.py"
    spec = importlib.util.spec_from_file_location("native_safety_vectors", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.FIXTURE_DIR = tmp_path
    module.main()
    generated = sorted(path.name for path in tmp_path.glob("*.json"))
    assert generated == sorted(path.name for path in _FIXTURES.glob("*.json"))
    assert len(generated) >= 8
    for name in generated:
        assert (tmp_path / name).read_bytes() == (_FIXTURES / name).read_bytes(), name
    for name in ("openai-pause-challenge-v1.json", "ga4-key-event-create-challenge-v1.json"):
        macos_fixture = _FIXTURES.parents[1] / "ApprovalHostMacOSTests/Fixtures" / name
        assert macos_fixture.read_bytes() == (_FIXTURES / name).read_bytes()


@pytest.mark.parametrize("prefix", ("openai-pause", "openai-pause-whole-seconds"))
def test_native_pause_vectors_bind_the_exact_python_action_and_digests(prefix: str) -> None:
    raw = (_FIXTURES / f"{prefix}-action-v1.json").read_bytes()
    action = revalidate_openai_pause_action(OpenAIPausePlannedAction.model_validate_json(raw))
    preview_openai_pause(action, opaque_key=b"\0" * 32, now=datetime(2030, 1, 1, tzinfo=UTC))
    assert canonical_bytes(action) == raw
    challenge = json.loads((_FIXTURES / f"{prefix}-challenge-v1.json").read_bytes())
    assert canonical_bytes(challenge["action"]) == raw
    metadata = json.loads((_FIXTURES / "safety-vector-metadata-v1.json").read_bytes())
    key = "whole_seconds_" if "whole-seconds" in prefix else ""
    assert metadata[f"{key}action_plan_digest"] == action.plan.plan_digest
    assert metadata[f"{key}action_subplan_digest"] == action.plan.subplan_digests[0]
    assert action.plan.profile_ref != action.plan.subplans[0].account.auth_profile_ref


def test_native_policy_fixture_uses_the_actual_money_limit_contract() -> None:
    raw = (_FIXTURES / "delegation-policy-v1.json").read_bytes()
    policy = DelegationPolicy.model_validate_json(raw)
    assert canonical_bytes(policy) == raw
    metadata = json.loads((_FIXTURES / "safety-vector-metadata-v1.json").read_bytes())
    assert metadata["policy_digest"] == policy.policy_digest
    assert policy.maximum_change_per_operation[0].maximum_minor_units == 0
    for kind in ("install", "revoke"):
        challenge = json.loads(
            (_FIXTURES / f"delegation-policy-{kind}-challenge-v1.json").read_bytes()
        )
        assert canonical_bytes(challenge["policy"]) == raw
        assert challenge["kind"] == f"DELEGATION_POLICY_{kind.upper()}"


def test_native_configuration_vectors_bind_only_the_closed_python_protocol_fields() -> None:
    raw = (_FIXTURES / "approval-host-configuration-vectors-v1.json").read_bytes()
    fixture = json.loads(raw)
    assert canonical_bytes(fixture) == raw
    assert fixture["scope"] == "PROTOCOL_CONFIGURATION_ONLY_NOT_INSTALLATION_ATTESTATION"
    assert {vector["case_id"] for vector in fixture["vectors"]} == {
        "defaults",
        "minimum_limits",
        "maximum_limits",
        "unicode_references",
    }
    for vector in fixture["vectors"]:
        configuration = ApprovalHostConfiguration(**vector["configuration"])
        assert len(configuration.to_data()) == 7
        assert configuration.canonical_bytes.decode("utf-8") == vector["canonical_json"]
        assert configuration.configuration_digest == vector["configuration_digest"]
