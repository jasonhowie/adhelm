"""Independent fake-only boundaries for installed OpenAI pause execution.

These cases use the existing ephemeral signed bridge fixture and mock HTTP
transport. They never launch a helper or host, call an OS credential store,
perform native authentication, or contact OpenAI.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_openai_pause import NOW
from test_openai_pause_execution_workflow import _Harness
from test_pause_cli_workflow import profile

import adhelm.openai_pause_execution_workflow as workflow
from adhelm.approval_acceptance import AcceptedNativeApproval
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OpenAIAccountKeyProfile
from adhelm.openai_pause_execution_workflow import OpenAIPauseExecutionRequest
from adhelm.providers.openai_pause_contract import build_openai_pause_plan
from adhelm.safety import EffectStatus
from adhelm.saved_plans import retain_openai_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(tmp_path, monkeypatch)


def test_legacy_source_without_explicit_workspace_never_reaches_bridge_or_credentials(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = build_openai_pause_plan(
        harness.action.binding.model_copy(update={"profile_ref": "default"}),
        expires_at=harness.action.plan.expires_at,
    )
    record = retain_openai_pause_plan(Ledger(harness.directory), legacy)
    request = OpenAIPauseExecutionRequest(plan_record_digest=record["record_digest"])
    monkeypatch.setattr(workflow, "_configuration_digest", lambda *_a: pytest.fail("configuration"))
    monkeypatch.setattr(
        workflow,
        "_request_and_record_installed_native_approval",
        lambda *_a, **_k: pytest.fail("native approval"),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.execute_retained_openai_pause_with_installed_approval(harness.directory, request)

    assert raised.value.code == "WORKSPACE_BINDING_REQUIRED"
    assert harness.bridge_calls == 0
    assert harness.scenario.requests == []


@pytest.mark.parametrize(
    "updated_profile",
    [
        profile(credential_source="EXPLICIT_CI"),
        profile(profile_id="another-openai-edit-profile"),
    ],
)
def test_current_os_store_edit_profile_must_exactly_match_before_native_review(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    updated_profile: OpenAIAccountKeyProfile,
) -> None:
    save_configuration(
        harness.directory,
        Configuration(profiles=[updated_profile]),
        replace=True,
    )
    monkeypatch.setattr(workflow, "credential", lambda _: pytest.fail("credential"))
    monkeypatch.setattr(
        workflow,
        "_request_and_record_installed_native_approval",
        lambda *_a, **_k: pytest.fail("native approval"),
    )

    with pytest.raises(AdhelmError) as raised:
        harness.execute()

    assert raised.value.code == "OPENAI_ACCOUNT_KEY_EDIT_PROFILE_REQUIRED"
    assert harness.bridge_calls == 0
    assert harness.scenario.requests == []


def test_plan_expiry_after_native_claim_blocks_kernel_and_provider(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.install_bridge(monkeypatch)
    harness.after_approval = lambda: monkeypatch.setattr(
        workflow, "_now", lambda: NOW + timedelta(minutes=20)
    )

    with pytest.raises(AdhelmError) as raised:
        harness.execute()

    assert raised.value.code == "PLAN_EXPIRED"
    assert harness.bridge_calls == 1
    assert harness.scenario.posts == 0
    payloads = [record["payload"] for record in Ledger(harness.directory).records("changes")]
    assert any(payload.get("event_type") == "APPROVAL_GRANTED" for payload in payloads)
    assert not any(payload.get("event_type") == "APPLY_STARTED" for payload in payloads)


def test_configuration_lock_is_released_before_native_review_wait(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.install_bridge(monkeypatch)

    def rewrite_identical_configuration() -> None:
        save_configuration(
            harness.directory,
            Configuration(profiles=[profile()]),
            replace=True,
        )

    harness.after_approval = rewrite_identical_configuration
    result = harness.execute()

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert harness.bridge_calls == 1
    assert harness.scenario.posts == 1


def test_same_plan_grant_with_changed_durable_identity_cannot_reach_http(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.install_bridge(monkeypatch)

    def alter_grant(value: AcceptedNativeApproval) -> AcceptedNativeApproval:
        direct_grant = value.direct_grant
        assert direct_grant is not None
        return replace(
            value,
            direct_grant=direct_grant.model_copy(
                update={"grantor_ref": "logical:unrecorded-grantor"}
            ),
        )

    harness.accepted_transform = alter_grant
    with pytest.raises(AdhelmError) as raised:
        harness.execute()

    assert raised.value.code == "USER_APPROVAL_HOST_REQUIRED"
    assert harness.bridge_calls == 1
    assert harness.scenario.posts == 0
    assert not any(
        record["payload"].get("event_type") == "APPLY_STARTED"
        for record in Ledger(harness.directory).records("changes")
    )
