"""Synthetic private native bridge, OS-store and fixed HTTP execution checks."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_installed_native_approval_bridge import (
    _candidate_for_pending,
    _install_binding,
    _timestamp,
)
from test_openai_pause import NOW, _Scenario
from test_pause_cli_workflow import profile, request

import adhelm._installed_native_approval_bridge as bridge
import adhelm.approval_acceptance as acceptance
import adhelm.ledger as ledger_module
import adhelm.openai_pause_execution_workflow as workflow
import adhelm.pause_workflow as planning
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration
from adhelm.openai_pause_execution_workflow import (
    OpenAIPauseExecutionRequest,
    execute_retained_openai_pause_with_installed_approval,
)
from adhelm.safety import EffectStatus
from adhelm.saved_plans import retain_openai_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


class _Harness:
    def __init__(self, directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.directory = directory
        self.scenario = _Scenario()
        self.secret = "synthetic-pause-secret"
        self.key = b"k" * 32
        self.bridge_calls = 0
        self.after_approval: Any = lambda: None
        self.accepted_transform: Any = lambda value: value
        original_client = httpx.Client

        def factory(*, trust_env: bool) -> httpx.Client:
            assert trust_env is False
            return original_client(transport=httpx.MockTransport(self.handler), trust_env=False)

        monkeypatch.setattr(workflow.httpx, "Client", factory)
        for module in (workflow, planning):
            monkeypatch.setattr(module, "credential", lambda _: self.secret)
            monkeypatch.setattr(module, "openai_pause_binding_key", lambda _: self.key)
        monkeypatch.setattr(workflow, "_now", lambda: NOW)
        monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(NOW))
        save_configuration(directory, Configuration(profiles=[profile()]))
        self.action, _ = planning.prepare_openai_pause(directory, request(), clock=lambda: NOW)
        record = retain_openai_pause_plan(Ledger(directory), self.action)
        self.request = OpenAIPauseExecutionRequest(plan_record_digest=record["record_digest"])
        self.scenario.requests.clear()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            rows = Ledger(self.directory).records("changes")
            assert any(row["payload"].get("event_type") == "APPLY_STARTED" for row in rows)
            assert any(row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED" for row in rows)
        return self.scenario.handler(request)

    def install_bridge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        configuration = _install_binding(monkeypatch)
        original_prepare = bridge._prepare_installed_native_approval_request
        original_exchange = workflow._request_and_record_installed_native_approval
        pending: list[object] = []
        harness = self

        def prepare(*args: Any, **kwargs: Any) -> object:
            value = original_prepare(*args, **kwargs)
            pending.append(value)
            return value

        class Pipe:
            def exchange(self, frame: bytes, *, cancellation: object) -> bytes:
                assert len(frame) > 4
                harness.bridge_calls += 1
                return _candidate_for_pending(
                    pending[-1],
                    configuration=configuration,
                    receipt_issued_at=NOW,
                    native_observed_at=NOW,
                )

        def approve(*args: Any, **kwargs: Any) -> object:
            value = original_exchange(*args, **kwargs)
            self.after_approval()
            return self.accepted_transform(value)

        monkeypatch.setattr(bridge, "_prepare_installed_native_approval_request", prepare)
        monkeypatch.setattr(bridge, "_resolve_pipe_adapter", Pipe)
        monkeypatch.setattr(workflow, "_request_and_record_installed_native_approval", approve)
        monkeypatch.setattr(
            acceptance, "accept_approval_receipt", lambda *_a, **_k: pytest.fail("pinned path")
        )

    def execute(self) -> Any:
        return execute_retained_openai_pause_with_installed_approval(self.directory, self.request)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(tmp_path, monkeypatch)


def test_default_binding_refuses_before_helper_or_provider(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_resolve_pipe_adapter", lambda: pytest.fail("helper launched"))
    with pytest.raises(AdhelmError) as error:
        harness.execute()
    assert error.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"
    assert harness.scenario.requests == []
    assert len(Ledger(harness.directory).records("changes")) == 2


def test_real_private_acceptance_to_kernel_has_durable_claim_before_one_post(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.install_bridge(monkeypatch)
    result = harness.execute()
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert harness.bridge_calls == 1
    assert harness.scenario.posts == 1
    assert harness.secret not in repr(result)
    assert harness.secret not in repr(Ledger(harness.directory).records("changes"))


@pytest.mark.parametrize("changed", ["secret", "key", "configuration"])
def test_post_approval_local_drift_stops_before_provider(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    harness.install_bridge(monkeypatch)

    def change() -> None:
        if changed == "secret":
            harness.secret = "rotated-synthetic-secret"
        elif changed == "key":
            harness.key = b"r" * 32
        else:
            save_configuration(
                harness.directory, Configuration(profiles=[profile(currency="EUR")]), replace=True
            )

    harness.after_approval = change
    with pytest.raises(AdhelmError) as error:
        harness.execute()
    assert error.value.code in {"CREDENTIAL_BINDING_MISMATCH", "CONFIGURATION_CHANGED"}
    assert harness.bridge_calls == 1
    assert harness.scenario.requests == []
    rows = Ledger(harness.directory).records("changes")
    assert not any(row["payload"].get("event_type") == "APPLY_STARTED" for row in rows)


@pytest.mark.parametrize(
    "field,value",
    [("currency", "EUR"), ("account_ref", "other"), ("timezone", "UTC"), ("purpose", "READ")],
)
def test_ineligible_current_configuration_stops_before_approval_or_credentials(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    save_configuration(
        harness.directory, Configuration(profiles=[profile(**{field: value})]), replace=True
    )
    monkeypatch.setattr(workflow, "credential", lambda _: pytest.fail("credential read"))
    monkeypatch.setattr(
        workflow,
        "_request_and_record_installed_native_approval",
        lambda *_a, **_k: pytest.fail("approval"),
    )
    with pytest.raises(AdhelmError):
        harness.execute()
    assert harness.scenario.requests == []


def test_expired_plan_stops_before_configuration_or_approval(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow, "_now", lambda: NOW + timedelta(hours=1))
    monkeypatch.setattr(workflow, "_configuration_digest", lambda *_a: pytest.fail("configuration"))
    with pytest.raises(AdhelmError) as error:
        harness.execute()
    assert error.value.code == "PLAN_EXPIRED"


def test_wrong_grant_cannot_reach_provider(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.install_bridge(monkeypatch)
    harness.accepted_transform = lambda value: replace(
        value, direct_grant=value.direct_grant.model_copy(update={"plan_digest": "a" * 64})
    )
    with pytest.raises(AdhelmError) as error:
        harness.execute()
    assert error.value.code == "OPENAI_PAUSE_APPROVAL_REQUIRED"
    assert harness.scenario.requests == []


@pytest.mark.parametrize("post", ["lost", "malformed", "failure"])
def test_ambiguous_write_is_not_retried_and_next_attempt_requires_recovery(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, post: Any
) -> None:
    harness.install_bridge(monkeypatch)
    harness.scenario.post = post
    result = harness.execute()
    assert result.recovery_required is True
    assert harness.scenario.posts == 1
    assert harness.bridge_calls == 1
    with pytest.raises(AdhelmError) as error:
        harness.execute()
    assert error.value.code == "RECOVERY_REQUIRED"
    assert harness.scenario.posts == 1
    assert harness.bridge_calls == 1


def test_constructed_request_is_revalidated_before_ledger(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.request = harness.request.model_copy(update={"schema_version": True})
    monkeypatch.setattr(workflow, "Ledger", lambda _: pytest.fail("ledger opened"))
    with pytest.raises(AdhelmError) as error:
        harness.execute()
    assert error.value.code == "INVALID_OPENAI_PAUSE_EXECUTION_INPUT"


def test_remote_change_after_native_approval_never_posts(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.install_bridge(monkeypatch)
    harness.after_approval = lambda: setattr(harness.scenario, "status", "paused")
    with pytest.raises(AdhelmError):
        harness.execute()
    assert harness.bridge_calls == 1
    assert harness.scenario.posts == 0


def test_configuration_change_during_last_provider_read_is_caught_before_post(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.install_bridge(monkeypatch)
    original_handler = harness.handler

    def changing_handler(request: httpx.Request) -> httpx.Response:
        response = original_handler(request)
        rows = Ledger(harness.directory).records("changes")
        if request.method == "GET" and any(
            row["payload"].get("event_type") == "APPLY_STARTED" for row in rows
        ):
            save_configuration(
                harness.directory, Configuration(profiles=[profile(currency="EUR")]), replace=True
            )
        return response

    monkeypatch.setattr(harness, "handler", changing_handler)
    result = harness.execute()
    assert result.results[0].status is not EffectStatus.APPLIED_VERIFIED
    assert harness.scenario.posts == 0


@pytest.mark.parametrize("field", ["secret", "key"])
def test_changed_credential_before_approval_never_launches_helper(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    setattr(harness, field, "rotated-synthetic-secret" if field == "secret" else b"n" * 32)
    monkeypatch.setattr(
        workflow,
        "_request_and_record_installed_native_approval",
        lambda *_a, **_k: pytest.fail("approval requested"),
    )
    with pytest.raises(AdhelmError) as error:
        harness.execute()
    assert error.value.code == "CREDENTIAL_BINDING_MISMATCH"
    assert harness.scenario.requests == []
