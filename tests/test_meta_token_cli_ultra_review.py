"""Independent fake-only review of the closed Meta token CLI surface."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from adhelm import config as config_module
from adhelm import credential_setup, meta_token_cli
from adhelm import meta_token_credentials as token_credentials
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.meta_token_credentials import enroll_meta_token_profile
from adhelm.meta_token_inspection import MetaTokenObservation
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.models import Configuration, OutputPreferences

_NOW = datetime(2030, 1, 4, 12, 0, tzinfo=UTC)
_SUBJECT = "independent-cli-subject-token"
_INSPECTOR = "independent-cli-inspector-token"
_FILE_SECRET = "independent-cli-file-secret"
_FLAG_SECRET = "independent-cli-flag-secret"


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


def _draft_data(**updates: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": 1,
        "profile_id": "independent-cli-meta",
        "account_ref": "12345",
        "client_application_ref": "67890",
        "currency": "USD",
        "timezone": "UTC",
        "purpose": "READ",
        "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
    }
    data.update(updates)
    return data


def _write_draft(root: Path, **updates: object) -> Path:
    path = root / "independent-meta-profile.json"
    path.write_text(json.dumps(_draft_data(**updates)), encoding="utf-8")
    return path


def _wire_store(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore) -> None:
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        token_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(config_module, "credential_mutation_lock", lambda _reference: nullcontext())


def _configure(root: Path, *, maximum_bytes: int = 1024) -> None:
    save_configuration(
        root,
        Configuration(output=OutputPreferences(max_output_bytes=maximum_bytes)),
        replace=True,
    )


def _enroll_direct(root: Path, monkeypatch: pytest.MonkeyPatch) -> _MemoryStore:
    store = _MemoryStore()
    _wire_store(monkeypatch, store)
    enroll_meta_token_profile(
        root,
        MetaAccountTokenProfileDraft.model_validate(_draft_data()),
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    return store


def _diagnostic_code(result: object) -> str:
    assert hasattr(result, "output")
    payload = json.loads(result.output)
    return payload["diagnostics"][0]["code"]


@pytest.mark.parametrize("field", ["subject_token", "inspector_token"])
def test_token_fields_in_draft_are_rejected_and_redacted_before_prompt_or_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    draft = _write_draft(tmp_path, **{field: _FILE_SECRET})

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("a token-bearing draft must not reach prompt or protected store")

    monkeypatch.setattr(token_credentials, "_credential_backend", forbidden)
    monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", forbidden)

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "auth",
            "meta-token",
            "enroll",
            "--input",
            str(draft),
            "--interactive",
        ],
    )

    assert result.exit_code == 2
    assert _diagnostic_code(result) == "SCHEMA_VALIDATION_FAILED"
    assert _FILE_SECRET not in result.output


@pytest.mark.parametrize("flag", ["--subject-token", "--inspector-token", "--token"])
def test_token_like_flags_are_not_a_secret_entry_path_and_never_echo_their_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    draft = _write_draft(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("an unsupported token flag must not reach a prompt or protected store")

    monkeypatch.setattr(token_credentials, "_credential_backend", forbidden)
    monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", forbidden)

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "auth",
            "meta-token",
            "enroll",
            "--input",
            str(draft),
            flag,
            _FLAG_SECRET,
        ],
    )

    assert result.exit_code == 2
    assert _FLAG_SECRET not in result.output
    assert "No such option" in result.output


def test_token_named_environment_values_are_ignored_before_terminal_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _write_draft(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("environment values must not reach a prompt or protected store")

    monkeypatch.setattr(token_credentials, "_credential_backend", forbidden)
    monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", forbidden)

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "auth",
            "meta-token",
            "enroll",
            "--input",
            str(draft),
        ],
        env={"META_TOKEN_SUBJECT": _FLAG_SECRET, "META_TOKEN_INSPECTOR": _FILE_SECRET},
    )

    assert result.exit_code == 4
    assert _diagnostic_code(result) == "EXPLICIT_TERMINAL_HANDOFF_REQUIRED"
    assert _FLAG_SECRET not in result.output
    assert _FILE_SECRET not in result.output


@pytest.mark.parametrize("leaf", ["rotate", "status", "inspect"])
def test_each_non_enroll_leaf_preflights_output_before_its_side_effect_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leaf: str
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    if leaf == "inspect":
        _enroll_direct(directory, monkeypatch)

    def output_failure(*_args: object, **_kwargs: object) -> None:
        raise AdhelmError("OUTPUT_LIMIT_EXCEEDED", "fixed output failure", exit_code=3)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("output preflight must stop before the command side effect")

    monkeypatch.setattr(meta_token_cli, "_require_result_fit", output_failure)
    monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", forbidden)
    if leaf == "rotate":
        monkeypatch.setattr(meta_token_cli, "prepare_meta_token_rotation", forbidden)
        arguments = ["--data-dir", str(directory), "auth", "meta-token", leaf, "--profile", "x"]
    elif leaf == "status":
        monkeypatch.setattr(meta_token_cli, "meta_token_credential_status", forbidden)
        arguments = ["--data-dir", str(directory), "auth", "meta-token", leaf, "--profile", "x"]
    else:
        monkeypatch.setattr(meta_token_cli, "inspect_configured_meta_token", forbidden)
        arguments = [
            "--data-dir",
            str(directory),
            "auth",
            "meta-token",
            leaf,
            "--profile",
            "independent-cli-meta",
        ]

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 3
    assert _diagnostic_code(result) == "OUTPUT_LIMIT_EXCEEDED"


def test_inspect_passes_the_exact_preflight_configuration_digest_and_redacts_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    _enroll_direct(directory, monkeypatch)
    configuration = load_configuration(directory)
    profile = configuration.profiles[0]
    expected_configuration_digest = digest(configuration)
    calls: list[tuple[Path, str, str]] = []
    observation = MetaTokenObservation(
        client_application_ref="67890",
        principal_ref="private-observed-principal",
        profile_ref="independent-cli-meta",
        credential_binding_ref="credential-binding:sha256:" + "b" * 64,
        scopes=("ads_read", "private-extra-scope"),
        observed_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
        data_access_expires_at=_NOW + timedelta(days=1),
    )

    def fixed_inspection(
        directory_arg: Path,
        profile_id: str,
        *,
        expected_configuration_digest: str | None = None,
    ) -> MetaTokenObservation:
        assert type(expected_configuration_digest) is str
        calls.append((directory_arg, profile_id, expected_configuration_digest))
        return observation

    monkeypatch.setattr(meta_token_cli, "inspect_configured_meta_token", fixed_inspection)

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "meta-token",
            "inspect",
            "--profile",
            "independent-cli-meta",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(directory, "independent-cli-meta", expected_configuration_digest)]
    data: dict[str, Any] = json.loads(result.output)["data"]
    assert data["profile_configuration_digest"] == digest(profile)
    assert data["additional_granted_scope_count"] == 1
    assert data["account_access_verified"] is False
    assert data["execution_authority"] is False
    for private in (
        _SUBJECT,
        _INSPECTOR,
        "private-observed-principal",
        "credential-binding:sha256:" + "b" * 64,
        "private-extra-scope",
        "independent-cli-meta",
        "12345",
        "67890",
    ):
        assert private not in result.output
