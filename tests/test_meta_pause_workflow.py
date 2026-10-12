"""Offline coverage for the configured Meta campaign PAUSE planning workflow."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from adhelm import meta_ads_token_edit_session as edit_session
from adhelm import meta_pause_workflow as workflow
from adhelm.canonical import digest
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.models import Configuration, MetaAccountTokenProfile, meta_token_credential_reference
from adhelm.providers.meta_ads import MetaAdsIdentity
from adhelm.providers.meta_campaign_state import MetaCampaignState

_START = datetime(2030, 1, 1, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def local_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model a local operator; explicit CI-refusal cases set their own CI value."""
    monkeypatch.delenv("CI", raising=False)


def _profile(*, purpose: Literal["READ", "EDIT"] = "EDIT") -> MetaAccountTokenProfile:
    return MetaAccountTokenProfile(
        profile_kind="META_ADS_ACCOUNT_TOKEN",
        profile_id="meta-edit",
        account_ref="123456",
        client_application_ref="987654",
        currency="USD",
        timezone="UTC",
        purpose=purpose,
        inspector_credential_kind="CUSTOMER_APP_ACCESS_TOKEN",
        credential_ref=meta_token_credential_reference("meta-edit"),
    )


def _configure(
    root: Path, *, profile: MetaAccountTokenProfile | None = None
) -> tuple[Configuration, str]:
    configuration = Configuration(profiles=[_profile() if profile is None else profile])
    save_configuration(root, configuration)
    current = load_configuration(root)
    return current, digest(current)


def _request(**updates: object) -> workflow.MetaCampaignPausePlanRequest:
    values: dict[str, object] = {
        "profile_id": "meta-edit",
        "campaign_id": "654321",
        "expires_in_seconds": 300,
    }
    values.update(updates)
    return workflow.MetaCampaignPausePlanRequest.model_validate(values)


@dataclass
class _FakeSession:
    configuration_digest: str
    profile: MetaAccountTokenProfile
    observed_at: datetime = _START
    expires_at: datetime = _START + timedelta(hours=1)
    data_access_expires_at: datetime = _START + timedelta(hours=2)
    configured_status: str = "ACTIVE"
    effective_status: str = "WITH_ISSUES"
    campaign_account_ref: str = "123456"
    recheck_error: AdhelmError | None = None
    calls: list[str] = field(default_factory=list)

    @property
    def binding(self) -> edit_session.MetaAdsTokenEditBinding:
        self.calls.append("binding")
        return edit_session._new_binding(
            self.profile.profile_id,
            self.profile.client_application_ref,
            "meta_token_principal_v1." + "a" * 64,
            "ads_management",
            "credential-binding:sha256:" + "b" * 64,
            self.configuration_digest,
            digest(self.profile),
            self.observed_at,
            self.expires_at,
            self.data_access_expires_at,
        )

    @property
    def identity(self) -> MetaAdsIdentity:
        self.calls.append("identity")
        return MetaAdsIdentity(
            account_ref="meta_ads.account.act_123456",
            account_client_ref="act_123456",
            timezone="UTC",
            currency="USD",
            status="1",
        )

    def read_campaign_state(self, campaign_id: str) -> MetaCampaignState:
        self.calls.append("campaign")
        return MetaCampaignState(
            campaign_client_ref=campaign_id,
            campaign_ref=f"meta_ads.campaign.{campaign_id}",
            account_client_ref=self.campaign_account_ref,
            configured_status=self.configured_status,  # type: ignore[arg-type]
            effective_status=self.effective_status,  # type: ignore[arg-type]
        )

    @contextmanager
    def retention_recheck(self) -> Iterator[Callable[[], None]]:
        self.calls.append("retention-open")

        def recheck() -> None:
            self.calls.append("recheck")
            if self.recheck_error is not None:
                raise self.recheck_error

        yield recheck
        self.calls.append("retention-close")


def _install_session(
    monkeypatch: pytest.MonkeyPatch,
    session: _FakeSession,
    expected_calls: list[str],
) -> None:
    @contextmanager
    def factory(
        directory: Path,
        profile_id: str,
        *,
        expected_configuration_digest: str,
    ) -> Iterator[_FakeSession]:
        expected_calls.append(f"open:{profile_id}")
        assert isinstance(directory, Path)
        assert expected_configuration_digest == session.configuration_digest
        yield session
        expected_calls.append("close")

    monkeypatch.setattr(workflow, "configured_meta_ads_token_edit_session", factory)


def _install_clock(monkeypatch: pytest.MonkeyPatch, *values: datetime) -> None:
    values_list = list(values)
    monkeypatch.setattr(workflow, "_now", lambda: values_list.pop(0))


def _retained_pause_records(root: Path) -> list[dict[str, Any]]:
    return [
        record
        for record in Ledger(root).records("changes")
        if record["record_kind"] == "META_CAMPAIGN_PAUSE_PLAN"
    ]


def test_plans_fixed_active_campaign_and_retains_one_closed_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration, expected = _configure(tmp_path)
    session = _FakeSession(configuration_digest=expected, profile=configuration.profiles[0])  # type: ignore[arg-type]
    opens: list[str] = []
    _install_session(monkeypatch, session, opens)
    _install_clock(monkeypatch, _START + timedelta(seconds=4), _START + timedelta(seconds=5))

    retained = workflow.plan_configured_meta_campaign_pause(
        tmp_path,
        _request(),
        expected_configuration_digest=expected,
    )

    assert retained.action.observation_binding.read_started_at == _START
    assert retained.action.observation_binding.read_finished_at == _START + timedelta(seconds=4)
    assert retained.action.plan.expires_at == _START + timedelta(seconds=304)
    assert retained.action.plan.subplans[0].operation.value == "PAUSE"
    assert retained.action.execution_authority is False
    assert retained.action.plan.subplans[0].max_spend.minor_units == 0
    assert opens == ["open:meta-edit", "close"]
    assert session.calls == [
        "binding",
        "identity",
        "campaign",
        "retention-open",
        "recheck",
        "retention-close",
    ]
    records = _retained_pause_records(tmp_path)
    assert len(records) == 1
    assert records[0]["record_digest"] == retained.plan_record_digest
    assert records[0]["payload"]["action"]["observation_binding"]["read_started_at"] == (
        "2030-01-01T12:00:00Z"
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {"schema_version": 1.0, "profile_id": "meta-edit", "campaign_id": "654321"},
        {"schema_version": 1, "profile_id": "meta-edit", "campaign_id": "0654321"},
        {
            "schema_version": 1,
            "profile_id": "meta-edit",
            "campaign_id": "654321",
            "expires_in_seconds": True,
        },
    ],
)
def test_invalid_request_fails_before_configuration_or_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: dict[str, object]
) -> None:
    monkeypatch.setattr(
        workflow,
        "load_configuration",
        lambda _directory: pytest.fail("invalid request must precede configuration"),
    )
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(
            tmp_path,
            workflow.MetaCampaignPausePlanRequest.model_construct(
                schema_version=invalid.get("schema_version"),
                profile_id=invalid.get("profile_id"),
                campaign_id=invalid.get("campaign_id"),
                expires_in_seconds=invalid.get("expires_in_seconds", 300),
            ),
        )
    assert raised.value.code == "INVALID_META_CAMPAIGN_PAUSE_PLAN_INPUT"


def test_ci_refusal_precedes_configuration_ledger_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(
        workflow,
        "load_configuration",
        lambda _directory: pytest.fail("CI refusal must precede configuration"),
    )
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(tmp_path, _request())
    assert raised.value.code == "META_CAMPAIGN_PAUSE_CI_UNSUPPORTED"
    assert raised.value.exit_code == ExitCode.AUTHORIZATION
    assert not (tmp_path / "ledger").exists()


def test_missing_or_read_profile_fails_before_ledger_or_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, profile=_profile(purpose="READ"))
    monkeypatch.setattr(
        workflow,
        "configured_meta_ads_token_edit_session",
        lambda *args, **kwargs: pytest.fail("profile refusal must precede session"),
    )
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(tmp_path, _request())
    assert raised.value.code == "META_TOKEN_EDIT_PROFILE_REQUIRED"
    assert raised.value.exit_code == ExitCode.AUTHORIZATION
    assert not (tmp_path / "ledger").exists()


def test_unresolved_history_blocks_before_session_or_campaign_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration, expected = _configure(tmp_path)
    session = _FakeSession(configuration_digest=expected, profile=configuration.profiles[0])  # type: ignore[arg-type]
    opens: list[str] = []
    _install_session(monkeypatch, session, opens)
    monkeypatch.setattr(
        workflow,
        "_require_no_recovery",
        lambda _ledger: (_ for _ in ()).throw(
            AdhelmError("RECOVERY_REQUIRED", "recovery", exit_code=ExitCode.PRECONDITION)
        ),
    )
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(tmp_path, _request())
    assert raised.value.code == "RECOVERY_REQUIRED"
    assert opens == []
    assert session.calls == []


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("configured_status", "PAUSED", "META_CAMPAIGN_PAUSE_FACTS_INVALID"),
        ("campaign_account_ref", "999999", "META_CAMPAIGN_PAUSE_FACTS_INVALID"),
    ],
)
def test_state_outside_fixed_active_account_scope_never_reaches_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    code: str,
) -> None:
    configuration, expected = _configure(tmp_path)
    session = _FakeSession(configuration_digest=expected, profile=configuration.profiles[0])  # type: ignore[arg-type]
    setattr(session, field, value)
    _install_session(monkeypatch, session, [])
    _install_clock(monkeypatch, _START + timedelta(seconds=4))
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(tmp_path, _request())
    assert raised.value.code == code
    assert "retention-open" not in session.calls
    assert not (tmp_path / "ledger").exists()


def test_user_ttl_is_not_silently_shortened_to_token_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration, expected = _configure(tmp_path)
    session = _FakeSession(
        configuration_digest=expected,
        profile=configuration.profiles[0],  # type: ignore[arg-type]
        expires_at=_START + timedelta(seconds=120),
        data_access_expires_at=_START + timedelta(hours=1),
    )
    _install_session(monkeypatch, session, [])
    _install_clock(monkeypatch, _START + timedelta(seconds=4))
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(
            tmp_path,
            _request(expires_in_seconds=300),
        )
    assert raised.value.code == "PLAN_EXPIRED"
    assert "retention-open" not in session.calls
    assert not (tmp_path / "ledger").exists()


def test_final_recheck_refusal_prevents_append_without_ledger_io_in_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration, expected = _configure(tmp_path)
    session = _FakeSession(
        configuration_digest=expected,
        profile=configuration.profiles[0],  # type: ignore[arg-type]
        recheck_error=AdhelmError("CONFIGURATION_CHANGED", "changed"),
    )
    _install_session(monkeypatch, session, [])
    _install_clock(monkeypatch, _START + timedelta(seconds=4))
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(tmp_path, _request())
    assert raised.value.code == "CONFIGURATION_CHANGED"
    assert session.calls == [
        "binding",
        "identity",
        "campaign",
        "retention-open",
        "recheck",
    ]
    assert _retained_pause_records(tmp_path) == []


def test_local_retention_io_error_is_ambiguous_after_fixed_campaign_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration, expected = _configure(tmp_path)
    session = _FakeSession(configuration_digest=expected, profile=configuration.profiles[0])  # type: ignore[arg-type]
    _install_session(monkeypatch, session, [])
    _install_clock(monkeypatch, _START + timedelta(seconds=4))
    monkeypatch.setattr(
        workflow,
        "retain_meta_campaign_pause_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic")),
    )
    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(tmp_path, _request())
    assert raised.value.code == "META_PAUSE_PLAN_RETENTION_UNVERIFIED"
    assert raised.value.exit_code == ExitCode.AMBIGUOUS
