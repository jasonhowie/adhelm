"""Configured Google Ads planning with fake observations and a real local ledger."""

from __future__ import annotations

import pickle
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_google_ads_pause_contract import (
    _CAMPAIGN,
    _CLIENT,
    _CUSTOMER,
    _PRINCIPAL,
    _T0,
    _account,
    _execution,
    _observation,
)

from adhelm import google_ads_pause_workflow as workflow
from adhelm.canonical import digest
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile
from adhelm.providers.google_ads_pause_contract import GoogleAdsCampaignPauseExecutionBinding
from adhelm.saved_google_ads_pause_plans import (
    load_google_ads_campaign_pause_plan,
    retain_google_ads_campaign_pause_plan,
)


def _profile() -> Profile:
    return Profile(
        profile_id="google-ads-edit",
        provider="google_ads",
        account_ref=_CUSTOMER,
        principal_ref=_PRINCIPAL,
        client_ref=_CLIENT,
        scopes=["https://www.googleapis.com/auth/adwords"],
        purpose="EDIT",
        currency="USD",
        timezone="UTC",
        credential_source="OS_OAUTH_GOOGLE",
        credential_ref="test-primary",
        login_customer_ref="0987654321",
    )


def _request() -> workflow.GoogleAdsCampaignPausePlanRequest:
    return workflow.GoogleAdsCampaignPausePlanRequest(
        profile_id="google-ads-edit",
        campaign_id=_CAMPAIGN,
    )


@dataclass
class _FakeSession:
    configuration_digest: str
    binding_updates: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    before_append_error: AdhelmError | None = None

    @property
    def execution_binding(self) -> GoogleAdsCampaignPauseExecutionBinding:
        self.calls.append("binding")
        data = {
            "configuration_digest": self.configuration_digest,
            "profile_configuration_digest": digest(_profile()),
            **self.binding_updates,
        }
        return _execution(**data)

    def observe_campaign(self, campaign_id: str) -> SimpleNamespace:
        assert campaign_id == _CAMPAIGN
        self.calls.append("fresh-pair")
        return SimpleNamespace(account_binding=_account(), observation_binding=_observation())

    @contextmanager
    def retention_recheck(self) -> Iterator[Callable[[], None]]:
        self.calls.append("retention-open")

        def recheck() -> None:
            self.calls.append("recheck")
            if self.before_append_error is not None:
                raise self.before_append_error

        try:
            yield recheck
        finally:
            self.calls.append("retention-close")


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    monkeypatch.delenv("CI", raising=False)
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    session = _FakeSession(digest(load_configuration(tmp_path)))

    @contextmanager
    def factory(
        directory: Path,
        profile_id: str,
        *,
        expected_configuration_digest: str,
    ) -> Iterator[_FakeSession]:
        assert directory == tmp_path
        assert profile_id == _profile().profile_id
        assert expected_configuration_digest == session.configuration_digest
        session.calls.append("open")
        yield session
        session.calls.append("close")

    monkeypatch.setattr(workflow, "configured_google_ads_pause_planning_session", factory)
    monkeypatch.setattr(workflow, "_now", lambda: _T0 + timedelta(seconds=3))
    return session


def _plans(root: Path) -> list[dict[str, Any]]:
    return [
        record
        for record in Ledger(root).records("changes")
        if record["record_kind"] == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
    ]


def test_fresh_pair_retains_one_exact_non_authorizing_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install(tmp_path, monkeypatch)
    retained = workflow.plan_configured_google_ads_campaign_pause(tmp_path, _request())
    assert session.calls == [
        "open",
        "binding",
        "fresh-pair",
        "retention-open",
        "recheck",
        "retention-close",
        "close",
    ]
    action, facts = load_google_ads_campaign_pause_plan(
        Ledger(tmp_path), retained.plan_record_digest
    )
    assert action == retained.action
    assert action.observation_binding == _observation()
    assert action.plan.expires_at == _T0 + timedelta(seconds=302)
    assert action.account_binding.account_status == "CLOSED"
    assert action.plan.subplans[0].max_spend.minor_units == 0
    assert action.execution_authority is facts["execution_authority"] is False
    assert len(_plans(tmp_path)) == 1
    assert _CUSTOMER not in repr(retained)
    with pytest.raises(TypeError):
        pickle.dumps(retained)


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": True},
        {"campaign_id": "0987654321"},
        {"campaign_id": "9223372036854775808"},
        {"campaign_id": 123},
        {"expires_in_seconds": True},
        {"expires_in_seconds": 900.0},
        {"expires_in_seconds": 59},
        {"expires_in_seconds": 901},
    ],
)
def test_constructed_request_refuses_before_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, Any],
) -> None:
    monkeypatch.setattr(workflow, "load_configuration", lambda _: pytest.fail("configuration"))
    request = _request().model_copy(update=updates)
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_google_ads_campaign_pause(tmp_path, request)
    assert raised.value.code == "INVALID_GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_INPUT"


@pytest.mark.parametrize("gate", ["ci", "stale-config", "read-profile", "recovery"])
def test_preflight_gates_refuse_before_protected_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
) -> None:
    _install(tmp_path, monkeypatch)
    monkeypatch.setattr(
        workflow,
        "configured_google_ads_pause_planning_session",
        lambda *_args, **_kwargs: pytest.fail("protected session"),
    )
    expected: str | None = None
    if gate == "ci":
        monkeypatch.setenv("CI", "TRUE")
        monkeypatch.setattr(workflow, "load_configuration", lambda _: pytest.fail("configuration"))
    elif gate == "stale-config":
        expected = "f" * 64
    elif gate == "read-profile":
        save_configuration(
            tmp_path,
            Configuration(profiles=[_profile().model_copy(update={"purpose": "READ"})]),
            replace=True,
        )
    else:
        monkeypatch.setattr(Ledger, "unresolved_starts", lambda _: ["unresolved"])
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_google_ads_campaign_pause(
            tmp_path,
            _request(),
            expected_configuration_digest=expected,
        )
    assert (
        raised.value.code
        == {
            "ci": "GOOGLE_ADS_CAMPAIGN_PAUSE_CI_UNSUPPORTED",
            "stale-config": "CONFIGURATION_CHANGED",
            "read-profile": "GOOGLE_ADS_EDIT_PROFILE_REQUIRED",
            "recovery": "RECOVERY_REQUIRED",
        }[gate]
    )
    assert _plans(tmp_path) == []


@pytest.mark.parametrize(
    "updates",
    [
        {"profile_ref": "another-profile"},
        {"configuration_digest": "f" * 64},
        {"login_customer_client_ref": None},
        {"profile_configuration_digest": "f" * 64},
    ],
)
def test_other_local_binding_cannot_be_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, Any],
) -> None:
    session = _install(tmp_path, monkeypatch)
    session.binding_updates = updates
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_google_ads_campaign_pause(tmp_path, _request())
    assert raised.value.code == "GOOGLE_ADS_CAMPAIGN_PAUSE_FACTS_INVALID"
    assert "retention-open" not in session.calls
    assert _plans(tmp_path) == []


def test_requested_expiry_is_rejected_instead_of_clamped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install(tmp_path, monkeypatch)
    session.binding_updates["expires_at"] = _T0 + timedelta(seconds=301)
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_google_ads_campaign_pause(tmp_path, _request())
    assert raised.value.code == "PLAN_EXPIRED"
    assert _plans(tmp_path) == []


@pytest.mark.parametrize(
    "current,code",
    [
        (_T0, "CLOCK_INVALID"),
        (_T0 + timedelta(seconds=302), "PLAN_EXPIRED"),
    ],
)
def test_final_callback_rejects_clock_or_expiry_without_plan_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current: datetime,
    code: str,
) -> None:
    session = _install(tmp_path, monkeypatch)
    times = iter([_T0 + timedelta(seconds=3), current])
    monkeypatch.setattr(workflow, "_now", lambda: next(times))
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_google_ads_campaign_pause(tmp_path, _request())
    assert raised.value.code == code
    assert "recheck" in session.calls
    assert _plans(tmp_path) == []


def test_protected_recheck_failure_does_not_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install(tmp_path, monkeypatch)
    session.before_append_error = AdhelmError("CREDENTIAL_CHANGED", "The held association changed.")
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_google_ads_campaign_pause(tmp_path, _request())
    assert raised.value.code == "CREDENTIAL_CHANGED"
    assert _plans(tmp_path) == []


def test_lost_retention_result_preserves_plan_and_reports_uncertainty_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(tmp_path, monkeypatch)
    original = retain_google_ads_campaign_pause_plan
    calls = 0

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        raise OSError("private filesystem details")

    monkeypatch.setattr(workflow, "retain_google_ads_campaign_pause_plan", interrupted)
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_google_ads_campaign_pause(tmp_path, _request())
    assert raised.value.code == "GOOGLE_ADS_PAUSE_PLAN_RETENTION_UNVERIFIED"
    assert raised.value.exit_code == ExitCode.AMBIGUOUS
    assert "private filesystem details" not in str(raised.value)
    assert calls == len(_plans(tmp_path)) == 1


def test_final_clock_cannot_roll_back_after_successful_plan_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    samples = iter([_T0 + timedelta(seconds=120), _T0 + timedelta(seconds=60)])
    monkeypatch.setattr(workflow, "_now", lambda: next(samples))
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_google_ads_campaign_pause(tmp_path, _request())
    assert raised.value.code == "CLOCK_INVALID"
    assert _plans(tmp_path) == []
