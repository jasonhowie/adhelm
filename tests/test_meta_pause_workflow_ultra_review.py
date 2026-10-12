"""Independent adversarial coverage for local Meta PAUSE plan preparation.

Every boundary below uses a fake EDIT session or local JSONL ledger.  The file
does not open a credential store, call Meta, launch a native helper, or prepare
an execution grant.
"""

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
from adhelm.providers.meta_pause_contract import MetaCampaignPausePlannedAction
from adhelm.saved_meta_pause_plans import retain_meta_campaign_pause_plan

_START = datetime(2030, 1, 1, 12, tzinfo=UTC)
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


@pytest.fixture(autouse=True)
def local_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model a local operator; explicit CI-refusal cases set their own CI value."""
    monkeypatch.delenv("CI", raising=False)


def _profile() -> MetaAccountTokenProfile:
    return MetaAccountTokenProfile(
        profile_kind="META_ADS_ACCOUNT_TOKEN",
        profile_id="meta-edit",
        account_ref="123456",
        client_application_ref="987654",
        currency="USD",
        timezone="UTC",
        purpose="EDIT",
        inspector_credential_kind="CUSTOMER_APP_ACCESS_TOKEN",
        credential_ref=meta_token_credential_reference("meta-edit"),
    )


def _configure(root: Path) -> tuple[Configuration, MetaAccountTokenProfile, str]:
    configuration = Configuration(profiles=[_profile()])
    save_configuration(root, configuration)
    current = load_configuration(root)
    profile = current.profiles[0]
    assert type(profile) is MetaAccountTokenProfile
    return current, profile, digest(current)


def _request() -> workflow.MetaCampaignPausePlanRequest:
    return workflow.MetaCampaignPausePlanRequest(
        profile_id="meta-edit",
        campaign_id="654321",
        expires_in_seconds=300,
    )


@dataclass
class _FakeEditSession:
    configuration_digest: str
    profile: MetaAccountTokenProfile
    binding_configuration_digest: str | None = None
    binding_profile_digest: str | None = None
    observed_at: datetime = _START
    expires_at: datetime = _START + timedelta(hours=1)
    data_access_expires_at: datetime = _START + timedelta(hours=2)
    identity_currency: str = "USD"
    identity_timezone: str = "UTC"
    events: list[str] = field(default_factory=list)

    @property
    def binding(self) -> edit_session.MetaAdsTokenEditBinding:
        self.events.append("binding")
        return edit_session._new_binding(
            self.profile.profile_id,
            self.profile.client_application_ref,
            "meta_token_principal_v1." + _DIGEST_A,
            "ads_management",
            "credential-binding:sha256:" + _DIGEST_B,
            self.binding_configuration_digest or self.configuration_digest,
            self.binding_profile_digest or digest(self.profile),
            self.observed_at,
            self.expires_at,
            self.data_access_expires_at,
        )

    @property
    def identity(self) -> MetaAdsIdentity:
        self.events.append("identity")
        return MetaAdsIdentity(
            account_ref="meta_ads.account.act_123456",
            account_client_ref="act_123456",
            timezone=self.identity_timezone,
            currency=self.identity_currency,
            status="1",
        )

    def read_campaign_state(self, campaign_id: str) -> MetaCampaignState:
        self.events.append("campaign")
        return MetaCampaignState(
            campaign_client_ref=campaign_id,
            campaign_ref=f"meta_ads.campaign.{campaign_id}",
            account_client_ref="123456",
            configured_status="ACTIVE",
            effective_status="WITH_ISSUES",
        )

    @contextmanager
    def retention_recheck(self) -> Iterator[Callable[[], None]]:
        self.events.append("retention-open")

        def recheck() -> None:
            self.events.append("recheck")

        try:
            yield recheck
        finally:
            self.events.append("retention-close")


def _install_session(
    monkeypatch: pytest.MonkeyPatch,
    session: _FakeEditSession,
) -> None:
    @contextmanager
    def factory(
        directory: Path,
        profile_id: str,
        *,
        expected_configuration_digest: str,
    ) -> Iterator[_FakeEditSession]:
        assert isinstance(directory, Path)
        assert profile_id == session.profile.profile_id
        assert expected_configuration_digest == session.configuration_digest
        session.events.append("open")
        try:
            yield session
        finally:
            session.events.append("close")

    monkeypatch.setattr(workflow, "configured_meta_ads_token_edit_session", factory)


def _install_clock(monkeypatch: pytest.MonkeyPatch, *values: datetime) -> None:
    remaining = list(values)
    monkeypatch.setattr(workflow, "_now", lambda: remaining.pop(0))


def _retained_records(root: Path) -> list[dict[str, object]]:
    return [
        record
        for record in Ledger(root).records("changes")
        if record["record_kind"] == "META_CAMPAIGN_PAUSE_PLAN"
    ]


def test_constructed_request_missing_slots_fails_before_configuration_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    constructed = object.__new__(workflow.MetaCampaignPausePlanRequest)
    monkeypatch.setattr(
        workflow,
        "load_configuration",
        lambda _directory: pytest.fail("malformed request must precede configuration access"),
    )

    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(tmp_path, constructed)

    assert raised.value.code == "INVALID_META_CAMPAIGN_PAUSE_PLAN_INPUT"
    assert not (tmp_path / "ledger").exists()


@pytest.mark.parametrize(
    "attribute",
    ["binding_configuration_digest", "binding_profile_digest"],
)
def test_post_read_profile_or_configuration_binding_mismatch_never_retains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attribute: Literal["binding_configuration_digest", "binding_profile_digest"],
) -> None:
    _configuration, profile, configuration_digest = _configure(tmp_path)
    session = _FakeEditSession(configuration_digest=configuration_digest, profile=profile)
    mismatch = _DIGEST_B if attribute == "binding_configuration_digest" else _DIGEST_A
    setattr(session, attribute, mismatch)
    _install_session(monkeypatch, session)
    _install_clock(monkeypatch, _START + timedelta(seconds=3))

    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(tmp_path, _request())

    assert raised.value.code == "META_CAMPAIGN_PAUSE_FACTS_INVALID"
    assert session.events == ["open", "binding", "identity", "campaign", "close"]
    assert _retained_records(tmp_path) == []


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("identity_currency", "CAD"),
        ("identity_timezone", "America/Chicago"),
    ],
)
def test_account_identity_expectations_are_bound_before_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attribute: Literal["identity_currency", "identity_timezone"],
    value: str,
) -> None:
    _configuration, profile, configuration_digest = _configure(tmp_path)
    session = _FakeEditSession(configuration_digest=configuration_digest, profile=profile)
    setattr(session, attribute, value)
    _install_session(monkeypatch, session)
    _install_clock(monkeypatch, _START + timedelta(seconds=3))

    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(tmp_path, _request())

    assert raised.value.code == "META_CAMPAIGN_PAUSE_FACTS_INVALID"
    assert "retention-open" not in session.events
    assert _retained_records(tmp_path) == []


def test_campaign_read_clock_regression_blocks_plan_before_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configuration, profile, configuration_digest = _configure(tmp_path)
    session = _FakeEditSession(
        configuration_digest=configuration_digest,
        profile=profile,
        observed_at=_START + timedelta(seconds=5),
    )
    _install_session(monkeypatch, session)
    _install_clock(monkeypatch, _START + timedelta(seconds=4))

    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(tmp_path, _request())

    assert raised.value.code == "CLOCK_INVALID"
    assert "retention-open" not in session.events
    assert _retained_records(tmp_path) == []


def test_final_callback_uses_no_ledger_read_while_append_lock_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configuration, profile, configuration_digest = _configure(tmp_path)
    session = _FakeEditSession(configuration_digest=configuration_digest, profile=profile)
    _install_session(monkeypatch, session)
    _install_clock(monkeypatch, _START + timedelta(seconds=3), _START + timedelta(seconds=4))

    original_checkpoint = Ledger.checkpoint
    callback_active = False

    def guarded_checkpoint(ledger: Ledger) -> dict[str, object]:
        if callback_active:
            pytest.fail("the final append callback must not query the ledger")
        return original_checkpoint(ledger)

    def retain_probe(
        ledger: Ledger,
        action: MetaCampaignPausePlannedAction,
        *,
        before_durable_append: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        del ledger, action
        assert before_durable_append is not None
        nonlocal callback_active
        callback_active = True
        try:
            before_durable_append()
        finally:
            callback_active = False
        return {"record_digest": _DIGEST_A}

    monkeypatch.setattr(Ledger, "checkpoint", guarded_checkpoint)
    monkeypatch.setattr(workflow, "retain_meta_campaign_pause_plan", retain_probe)

    retained = workflow.plan_configured_meta_campaign_pause(tmp_path, _request())

    assert retained.plan_record_digest == _DIGEST_A
    assert session.events == [
        "open",
        "binding",
        "identity",
        "campaign",
        "retention-open",
        "recheck",
        "retention-close",
        "close",
    ]


def test_append_then_local_failure_is_ambiguous_without_raw_error_or_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configuration, profile, configuration_digest = _configure(tmp_path)
    session = _FakeEditSession(configuration_digest=configuration_digest, profile=profile)
    _install_session(monkeypatch, session)
    _install_clock(monkeypatch, _START + timedelta(seconds=3), _START + timedelta(seconds=4))
    original_retain = retain_meta_campaign_pause_plan

    def append_then_fail(
        ledger: Ledger,
        action: MetaCampaignPausePlannedAction,
        *,
        before_durable_append: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        original_retain(ledger, action, before_durable_append=before_durable_append)
        raise OSError("private local storage implementation detail")

    monkeypatch.setattr(workflow, "retain_meta_campaign_pause_plan", append_then_fail)

    with pytest.raises(AdhelmError) as raised:
        workflow.plan_configured_meta_campaign_pause(tmp_path, _request())

    assert raised.value.code == "META_PAUSE_PLAN_RETENTION_UNVERIFIED"
    assert raised.value.exit_code == ExitCode.AMBIGUOUS
    assert "private local storage implementation detail" not in str(raised.value)
    assert len(_retained_records(tmp_path)) == 1
