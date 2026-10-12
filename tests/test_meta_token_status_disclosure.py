# ruff: noqa: E501
"""What `auth meta-token status` may and may not say about a configured token.

Every case is fake-only: an in-memory credential store and a `MockTransport`
inspection.  No live Meta request is made, and no secret is written anywhere a
test can observe it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from adhelm import credential_setup, meta_token_cli
from adhelm import meta_token_credentials as token_credentials
from adhelm import meta_token_inspection as inspection
from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.meta_token_credentials import enroll_meta_token_profile
from adhelm.meta_token_observation_records import (
    META_TOKEN_APP_REVIEW_DISCLOSURE,
    META_TOKEN_PURPOSE_SCOPE_ALLOWLIST,
    META_TOKEN_REVOCATION_GUIDANCE,
    clamp_meta_token_scopes,
    load_meta_token_observation,
    meta_token_purpose_allowed_scopes,
    retain_meta_token_observation,
)
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.models import Configuration, MetaAccountTokenProfile, OutputPreferences

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_SUBJECT = "synthetic-meta-subject-token"
_INSPECTOR = "synthetic-meta-inspector-token"
_PROFILE = "meta-cli-read"
# A wall-clock past instant: `status` compares retained expiries with the real
# clock rather than with the fixed inspection clock the transport fake pins.
_PAST = datetime(2020, 1, 1, tzinfo=UTC)


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


def _draft_data(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 1,
        "profile_id": _PROFILE,
        "account_ref": "12345",
        "client_application_ref": "67890",
        "currency": "USD",
        "timezone": "UTC",
        "purpose": "READ",
        "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
    }
    result.update(updates)
    return result


def _configure(directory: Path, *, maximum: int = 2048) -> None:
    save_configuration(directory, Configuration(output=OutputPreferences(max_output_bytes=maximum)))


def _enroll(directory: Path, monkeypatch: pytest.MonkeyPatch, **updates: object) -> _MemoryStore:
    store = _MemoryStore()
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    enroll_meta_token_profile(
        directory,
        MetaAccountTokenProfileDraft.model_validate(_draft_data(**updates)),
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    return store


def _inspection_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
    data_access_expires_at: datetime | None = None,
) -> None:
    real_client = httpx.Client
    granted = scopes if scopes is not None else ["ads_read", "ads_management", "public_profile"]
    expiry = expires_at if expires_at is not None else _NOW + timedelta(days=30)
    data_expiry = (
        data_access_expires_at if data_access_expires_at is not None else _NOW + timedelta(days=60)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "app_id": "67890",
                    "user_id": "54321",
                    "is_valid": True,
                    "type": "USER",
                    "scopes": granted,
                    "issued_at": int((_NOW - timedelta(minutes=1)).timestamp()),
                    "expires_at": int(expiry.timestamp()),
                    "data_access_expires_at": int(data_expiry.timestamp()),
                }
            },
        )

    monkeypatch.setattr(
        inspection.httpx,
        "Client",
        lambda *, trust_env: real_client(transport=httpx.MockTransport(handler), trust_env=False),
    )
    monkeypatch.setattr(inspection, "_now", lambda: _NOW)
    monkeypatch.setattr(inspection, "_monotonic", lambda: 100.0)


def _run(directory: Path, *arguments: str):
    return CliRunner().invoke(app, ["--data-dir", str(directory), "auth", "meta-token", *arguments])


def _data(result) -> dict[str, object]:
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _installed_binding(directory: Path) -> str:
    """The binding reference of the bundle installed for the test profile."""
    from adhelm.meta_token_credentials import meta_token_credential_status

    binding = meta_token_credential_status(directory, _PROFILE).credential_binding_ref
    assert binding is not None
    return binding


def _installed_key(directory: Path) -> str:
    """The local file key this profile's retained inspection record uses."""
    import adhelm.meta_token_observation_records as module

    return module._key(_profile(directory))


def _profile(directory: Path) -> MetaAccountTokenProfile:
    profile = next(
        item for item in load_configuration(directory).profiles if item.profile_id == _PROFILE
    )
    assert isinstance(profile, MetaAccountTokenProfile)
    return profile


# ------------------------------------------------------------- scope clamping


def test_a_read_profile_never_records_the_management_scope() -> None:
    assert meta_token_purpose_allowed_scopes("READ") == ("ads_read",)
    clamped = clamp_meta_token_scopes(
        "READ", ("ads_read", "ads_management", "business_management", "public_profile")
    )
    assert clamped == ("ads_read",)
    assert "ads_management" not in clamped


def test_an_edit_profile_records_both_documented_scopes_and_nothing_else() -> None:
    clamped = clamp_meta_token_scopes("EDIT", ("ads_read", "ads_management", "pages_show_list"))
    assert clamped == ("ads_management", "ads_read")
    assert set(clamped) == set(META_TOKEN_PURPOSE_SCOPE_ALLOWLIST["EDIT"])


def test_a_retained_record_that_names_an_out_of_purpose_scope_is_refused(tmp_path: Path) -> None:
    """The retained file is local state; a tampered one fails closed."""
    import adhelm.meta_token_observation_records as module

    directory = tmp_path / "data"
    directory.mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "purpose": "READ",
        "profile_configuration_digest": "a" * 64,
        "observed_at": "2030-01-01T00:00:00Z",
        "expires_at": "2030-02-01T00:00:00Z",
        "data_access_expires_at": "2030-03-01T00:00:00Z",
        "purpose_allowed_scopes": ["ads_read"],
        "purpose_clamped_scopes": ["ads_read", "ads_management"],
        "granted_scope_count": 2,
    }
    with pytest.raises(AdhelmError) as raised:
        module._decode(payload)
    assert raised.value.code == "META_TOKEN_OBSERVATION_RECORD_INVALID"


# ------------------------------------------------------- inspect then status


def test_status_without_an_inspection_reports_no_local_expiry_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    data = _data(_run(directory, "status", "--profile", _PROFILE))
    assert data["local_state"] == "READY"
    assert data["token_validity"] == "NOT_INSPECTED_LOCALLY"
    assert data["expires_at"] is None
    assert data["data_access_expires_at"] is None
    assert data["purpose_allowed_scopes"] == ["ads_read"]
    assert data["purpose_clamped_scopes"] == []
    # Null, not zero: a token nothing has inspected does not carry zero scopes.
    assert data["granted_scope_count"] is None
    assert data["retained_inspection"] == "NOT_RETAINED"


def test_status_reports_the_retained_expiry_and_clamped_scopes_after_an_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    _inspection_transport(monkeypatch)
    inspected = _data(_run(directory, "inspect", "--profile", _PROFILE))
    assert inspected["additional_granted_scope_count"] == 2

    data = _data(_run(directory, "status", "--profile", _PROFILE))
    assert data["token_validity"] == "VALID_AT_LAST_INSPECTION"
    assert data["expires_at"] == "2030-01-31T00:00:00Z"
    assert data["data_access_expires_at"] == "2030-03-02T00:00:00Z"
    # The token carried `ads_management`; a READ profile records only the
    # scope its purpose allows and counts the rest.
    assert data["purpose_allowed_scopes"] == ["ads_read"]
    assert data["purpose_clamped_scopes"] == ["ads_read"]
    assert data["granted_scope_count"] == 3
    assert data["provider_authentication_verified"] is False
    assert data["account_access_verified"] is False
    assert data["remote_mutations"] is False
    assert data["execution_authority"] is False


def test_status_discloses_revocation_and_review_state_without_asserting_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    data = _data(_run(directory, "status", "--profile", _PROFILE))
    # The guidance itself, not a code for it: an agent reading this route has to
    # be able to tell the marketer how revocation actually works.
    assert data["revocation_guidance"] == META_TOKEN_REVOCATION_GUIDANCE
    assert data["app_review_status"] == META_TOKEN_APP_REVIEW_DISCLOSURE == "NOT_DOCUMENTED_LOCALLY"
    assert data["business_verification_status"] == "NOT_DOCUMENTED_LOCALLY"
    # The one place the separation is written out states it in both halves.
    assert "does not revoke it" in META_TOKEN_REVOCATION_GUIDANCE
    assert "Only Meta can revoke a token" in META_TOKEN_REVOCATION_GUIDANCE
    assert "performs no revocation request" in META_TOKEN_REVOCATION_GUIDANCE


def test_status_output_echoes_no_secret_profile_or_application_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    _inspection_transport(monkeypatch)
    inspected = _run(directory, "inspect", "--profile", _PROFILE)
    status = _run(directory, "status", "--profile", _PROFILE)
    for output in (inspected.output, status.output):
        for private in (_SUBJECT, _INSPECTOR, _PROFILE, "12345", "67890", "54321"):
            assert private not in output
    assert len(status.output.encode()) <= 2048


def test_an_expired_retained_inspection_makes_status_an_authorization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    profile = _profile(directory)
    retain_meta_token_observation(
        directory,
        profile,
        granted_scopes=("ads_read",),
        credential_binding_ref=_installed_binding(directory),
        # Wall-clock past: `status` compares a retained expiry with the real
        # clock, so this record is expired whenever the suite runs.
        observed_at=_PAST - timedelta(days=2),
        expires_at=_PAST,
        data_access_expires_at=datetime(2999, 1, 1, tzinfo=UTC),
    )
    result = _run(directory, "status", "--profile", _PROFILE)
    assert result.exit_code == 4
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    diagnostic = envelope["diagnostics"][0]
    assert diagnostic["code"] == "META_TOKEN_EXPIRED"
    assert "does not revoke it" in diagnostic["next_action"]
    for private in (_SUBJECT, _INSPECTOR, _PROFILE):
        assert private not in result.output


def test_an_expired_data_access_window_is_also_an_authorization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    retain_meta_token_observation(
        directory,
        _profile(directory),
        granted_scopes=("ads_read",),
        credential_binding_ref=_installed_binding(directory),
        observed_at=_PAST - timedelta(days=2),
        expires_at=datetime(2999, 1, 1, tzinfo=UTC),
        data_access_expires_at=_PAST,
    )
    assert _run(directory, "status", "--profile", _PROFILE).exit_code == 4


def test_an_invalid_or_expired_token_fails_inspection_with_exit_code_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    _inspection_transport(monkeypatch, expires_at=_NOW - timedelta(minutes=1))
    result = _run(directory, "inspect", "--profile", _PROFILE)
    assert result.exit_code == 4
    assert json.loads(result.output)["diagnostics"][0]["code"] == "META_TOKEN_EXPIRED"
    for private in (_SUBJECT, _INSPECTOR):
        assert private not in result.output
    # A refused inspection retains nothing, so status keeps saying it has no
    # local claim rather than inheriting a failed observation.
    data = _data(_run(directory, "status", "--profile", _PROFILE))
    assert data["token_validity"] == "NOT_INSPECTED_LOCALLY"


def test_a_retained_record_is_ignored_after_the_profile_declaration_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    profile = _profile(directory)
    retain_meta_token_observation(
        directory,
        profile,
        granted_scopes=("ads_read",),
        credential_binding_ref=_installed_binding(directory),
        observed_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
        data_access_expires_at=_NOW + timedelta(days=60),
    )
    binding = _installed_binding(directory)
    assert (
        load_meta_token_observation(directory, profile, credential_binding_ref=binding).state
        == "RETAINED"
    )
    changed = profile.model_copy(update={"timezone": "Europe/London"})
    assert (
        load_meta_token_observation(directory, changed, credential_binding_ref=binding).state
        == "NOT_RETAINED"
    )
    # A bundle this record was not produced from is not answered either.
    assert (
        load_meta_token_observation(
            directory, profile, credential_binding_ref="credential-binding:sha256:" + "c" * 64
        ).state
        == "NOT_RETAINED"
    )


def test_the_retained_record_holds_no_token_principal_or_application_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    _inspection_transport(monkeypatch)
    _run(directory, "inspect", "--profile", _PROFILE)
    stored = (directory / "meta-token-observations.json").read_text(encoding="utf-8")
    for private in (_SUBJECT, _INSPECTOR, _PROFILE, "67890", "54321", "12345"):
        assert private not in stored
    assert "ads_management" not in stored
    assert json.loads(stored)


def test_the_status_output_bound_is_enforced_before_any_credential_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured bound that cannot hold the worst case refuses first."""

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("output preflight must precede credential-store work")

    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    monkeypatch.setattr(
        meta_token_cli,
        "_require_result_fit",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AdhelmError("OUTPUT_LIMIT_EXCEEDED", "fixed output failure", exit_code=3)
        ),
    )
    monkeypatch.setattr(meta_token_cli, "meta_token_credential_status", forbidden)
    monkeypatch.setattr(meta_token_cli, "load_meta_token_observation", forbidden)
    monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", forbidden)
    result = _run(directory, "status", "--profile", _PROFILE)
    assert result.exit_code == 3
    assert json.loads(result.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"


def test_an_unreadable_retained_inspection_file_is_reported_as_incomplete_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared fact this product cannot read is not a clean OK.

    The Google OAuth status route reports `INCOMPLETE_COVERAGE` when the
    credential store could not answer, rather than reporting the unanswered
    slot as settled.  A retained Meta inspection file this product cannot parse
    is the same class of fact: the status is answered, but one thing it
    declares was not read.
    """
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    (directory / "meta-token-observations.json").write_text(
        json.dumps({_installed_key(directory): {"schema_version": 99}}), encoding="utf-8"
    )
    result = _run(directory, "status", "--profile", _PROFILE)
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "INCOMPLETE_COVERAGE"
    data = envelope["data"]
    assert data["retained_inspection"] == "UNREADABLE"
    # An unreadable record answers nothing about the installed token.
    assert data["token_validity"] == "NOT_INSPECTED_LOCALLY"
    assert data["expires_at"] is None
    assert data["granted_scope_count"] is None
    # It still discloses the revocation boundary and the review limits.
    assert data["revocation_guidance"] == META_TOKEN_REVOCATION_GUIDANCE
    assert data["app_review_status"] == "NOT_DOCUMENTED_LOCALLY"


def test_an_inspection_of_one_bundle_is_never_reported_against_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is bound to the exact bundle the inspection looked at.

    `_installed_binding` changes whenever the bundle does, because a rotation
    replaces both tokens and the private binding key.
    """
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    _inspection_transport(monkeypatch)
    _run(directory, "inspect", "--profile", _PROFILE)
    before = _data(_run(directory, "status", "--profile", _PROFILE))
    assert before["token_validity"] == "VALID_AT_LAST_INSPECTION"
    assert before["retained_inspection"] == "MATCHES_INSTALLED_BUNDLE"

    from adhelm.meta_token_credentials import (
        install_meta_token_credentials,
        prepare_meta_token_rotation,
    )

    slot = prepare_meta_token_rotation(directory, _PROFILE)
    install_meta_token_credentials(
        directory,
        slot,
        subject_token_supplier=lambda: _SUBJECT + "-rotated",
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    after = _data(_run(directory, "status", "--profile", _PROFILE))
    assert after["local_state"] == "READY"
    assert after["token_validity"] == "NOT_INSPECTED_LOCALLY"
    assert after["retained_inspection"] == "NOT_RETAINED"
    assert after["expires_at"] is None
    assert after["data_access_expires_at"] is None
    assert after["granted_scope_count"] is None
    assert after["purpose_clamped_scopes"] == []


def test_no_status_output_carries_the_installed_bundle_binding_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding reference is an internal comparison value, never a result field."""
    directory = tmp_path / "data"
    _configure(directory)
    _enroll(directory, monkeypatch)
    _inspection_transport(monkeypatch)
    binding = _installed_binding(directory)
    inspected = _run(directory, "inspect", "--profile", _PROFILE)
    status = _run(directory, "status", "--profile", _PROFILE)
    assert binding.startswith("credential-binding:sha256:")
    for output in (inspected.output, status.output):
        assert binding not in output
        assert "credential-binding" not in output
