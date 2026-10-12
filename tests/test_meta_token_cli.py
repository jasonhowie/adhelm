"""Fake-only command coverage for the closed Meta token lifecycle surface."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import cast

import httpx
import pytest
from typer.testing import CliRunner

from adhelm import credential_setup, meta_token_cli, meta_token_workflow
from adhelm import meta_token_credentials as token_credentials
from adhelm import meta_token_inspection as inspection
from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.meta_token_credentials import enroll_meta_token_profile
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.models import Configuration, OutputPreferences

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_SUBJECT = "synthetic-meta-subject-token"
_INSPECTOR = "synthetic-meta-inspector-token"


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    writes: list[tuple[str, str, str]] = field(default_factory=list)
    drop_writes: bool = False

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.writes.append((service_name, username, password))
        if not self.drop_writes:
            self.values[(service_name, username)] = password


def _draft_data(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 1,
        "profile_id": "meta-cli-read",
        "account_ref": "12345",
        "client_application_ref": "67890",
        "currency": "USD",
        "timezone": "UTC",
        "purpose": "READ",
        "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
    }
    result.update(updates)
    return result


def _write_draft(directory: Path, **updates: object) -> Path:
    path = directory / "meta-draft.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_draft_data(**updates)), encoding="utf-8")
    return path


def _install_store(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore) -> None:
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)


def _allow_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta_token_cli, "_require_interactive_terminal", lambda interactive: None)


def _prompt_values(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    prompted: list[str] = []

    def prompt(*, prompt: str = "Credential secret: ") -> str:
        prompted.append(prompt)
        return _SUBJECT if len(prompted) == 1 else _INSPECTOR

    monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", prompt)
    return prompted


def test_meta_hidden_prompt_labels_are_closed_and_use_the_existing_no_echo_boundary() -> None:
    class _Tty(StringIO):
        def isatty(self) -> bool:
            return True

    labels: list[str] = []

    def hidden_prompt(prompt: str, stream: StringIO) -> str:
        labels.append(prompt)
        return "synthetic-hidden-value"

    terminal = _Tty()
    assert (
        credential_setup.prompt_secret_from_terminal(
            stdin=terminal,
            stderr=terminal,
            getpass_function=hidden_prompt,
            prompt=meta_token_cli._SUBJECT_PROMPT_LABEL,
        )
        == "synthetic-hidden-value"
    )
    with pytest.raises(AdhelmError) as failure:
        credential_setup.prompt_secret_from_terminal(
            stdin=terminal,
            stderr=terminal,
            getpass_function=hidden_prompt,
            prompt=cast(credential_setup.CredentialPrompt, "Injected terminal prompt: "),
        )
    assert failure.value.code == "SECRET_PROMPT_UNAVAILABLE"
    assert labels == [meta_token_cli._SUBJECT_PROMPT_LABEL]


def _configure(directory: Path, *, maximum: int = 1024, replace: bool = False) -> None:
    save_configuration(
        directory,
        Configuration(output=OutputPreferences(max_output_bytes=maximum)),
        replace=replace,
    )


def _enroll_direct(directory: Path, monkeypatch: pytest.MonkeyPatch, store: _MemoryStore) -> None:
    _install_store(monkeypatch, store)
    enroll_meta_token_profile(
        directory,
        MetaAccountTokenProfileDraft.model_validate(_draft_data()),
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )


def _inspection_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expires_at: datetime = _NOW + timedelta(minutes=5),
    data_access_expires_at: datetime = _NOW + timedelta(days=1),
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "graph.facebook.com"
        assert request.url.path == "/v26.0/debug_token"
        assert request.headers["authorization"] == "Bearer " + _INSPECTOR
        assert dict(request.url.params) == {"input_token": _SUBJECT}
        return httpx.Response(
            200,
            json={
                "data": {
                    "app_id": "67890",
                    "user_id": "54321",
                    "is_valid": True,
                    "type": "USER",
                    "scopes": ["ads_read", "public_profile"],
                    "issued_at": int((_NOW - timedelta(minutes=1)).timestamp()),
                    "expires_at": int(expires_at.timestamp()),
                    "data_access_expires_at": int(data_access_expires_at.timestamp()),
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
    return requests


def _result_data(result) -> dict[str, object]:
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def test_discovery_and_draft_schema_are_read_only_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("discovery/schema must not open a store, prompt, or inspect Meta")

    monkeypatch.setattr(token_credentials, "_credential_backend", forbidden)
    monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", forbidden)
    runner = CliRunner()
    discovery = _result_data(runner.invoke(app, ["discover"]))
    assert {
        "auth meta-token enroll",
        "auth meta-token rotate",
        "auth meta-token status",
        "auth meta-token inspect",
        "auth meta-token remove-local",
    } <= set(discovery["commands"])
    assert discovery["credential_handoff"]["meta_token"].endswith("DEVELOPMENT_TESTED")
    schema = _result_data(runner.invoke(app, ["schema", "meta-token-profile-draft"]))
    properties = schema["schema"]["properties"]
    assert set(properties) == {
        "account_ref",
        "client_application_ref",
        "currency",
        "inspector_credential_kind",
        "profile_id",
        "purpose",
        "schema_version",
        "timezone",
    }
    assert not {"subject_token", "inspector_token", "credential_ref"} & set(properties)


def test_enroll_validates_nonsecret_input_before_handoff_or_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _write_draft(tmp_path, subject_token=_SUBJECT)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("invalid draft must stop before a prompt or store access")

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
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"


def test_default_enroll_requires_explicit_handoff_before_store_or_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _write_draft(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("default machine path must stop before a prompt or store access")

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
    )
    assert result.exit_code == 4
    assert (
        json.loads(result.output)["diagnostics"][0]["code"] == "EXPLICIT_TERMINAL_HANDOFF_REQUIRED"
    )


def test_interactive_enroll_requires_a_real_tty_before_preparing_a_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = _write_draft(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("a non-TTY must not prepare a slot or access the store")

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
    assert result.exit_code == 4
    assert json.loads(result.output)["diagnostics"][0]["code"] == "INTERACTIVE_TERMINAL_REQUIRED"


def test_enroll_preflights_complete_output_before_slot_or_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    draft = _write_draft(tmp_path)
    _allow_interactive(monkeypatch)

    def limit(*_args: object, **_kwargs: object) -> None:
        raise AdhelmError("OUTPUT_LIMIT_EXCEEDED", "fixed output failure", exit_code=3)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("output preflight must precede slot/store/prompt work")

    monkeypatch.setattr(meta_token_cli, "_require_result_fit", limit)
    monkeypatch.setattr(meta_token_cli, "prepare_meta_token_enrollment", forbidden)
    monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", forbidden)
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "meta-token",
            "enroll",
            "--input",
            str(draft),
            "--interactive",
        ],
    )
    assert result.exit_code == 3
    assert json.loads(result.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"


def test_enroll_rotate_and_status_emit_only_redacted_bounded_local_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    # `auth meta-token status` now carries the operator-facing revocation
    # guidance, the retained-inspection state, and the purpose scope clamp, so
    # its worst-case result no longer fits a 1 KiB bound. The enrolment and
    # rotation results are unchanged and are still pinned at 1 KiB below.
    _configure(directory, maximum=2048)
    store = _MemoryStore()
    _install_store(monkeypatch, store)
    _allow_interactive(monkeypatch)
    prompted = _prompt_values(monkeypatch)
    draft = _write_draft(tmp_path)
    runner = CliRunner()

    enrolled = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "meta-token",
            "enroll",
            "--input",
            str(draft),
            "--interactive",
        ],
    )
    enrollment = _result_data(enrolled)
    assert enrollment["operation"] == "ENROLL"
    assert enrollment["local_state"] == "READY"
    assert enrollment["account_access_verified"] is False
    assert enrollment["execution_authority"] is False
    assert prompted == [
        meta_token_cli._SUBJECT_PROMPT_LABEL,
        meta_token_cli._INSPECTOR_PROMPT_LABEL,
    ]

    prompted.clear()
    rotated = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "meta-token",
            "rotate",
            "--profile",
            "meta-cli-read",
            "--interactive",
        ],
    )
    rotation = _result_data(rotated)
    assert rotation["operation"] == "ROTATE"
    assert prompted == [
        meta_token_cli._SUBJECT_PROMPT_LABEL,
        meta_token_cli._INSPECTOR_PROMPT_LABEL,
    ]

    status = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "meta-token",
            "status",
            "--profile",
            "meta-cli-read",
        ],
    )
    state = _result_data(status)
    assert state["local_state"] == "READY"
    assert state["provider_authentication_verified"] is False
    assert len(status.output.encode()) <= 2048
    for output in (enrolled.output, rotated.output):
        assert len(output.encode()) <= 1024
    for output in (enrolled.output, rotated.output, status.output):
        for private in (_SUBJECT, _INSPECTOR, "meta-cli-read", "12345", "67890"):
            assert private not in output


def test_unverified_bundle_write_reports_existing_ambiguity_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    store = _MemoryStore(drop_writes=True)
    _install_store(monkeypatch, store)
    _allow_interactive(monkeypatch)
    _prompt_values(monkeypatch)
    draft = _write_draft(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "meta-token",
            "enroll",
            "--input",
            str(draft),
            "--interactive",
        ],
    )
    assert result.exit_code == 6
    assert json.loads(result.output)["diagnostics"][0]["code"] == (
        "META_TOKEN_CREDENTIAL_TRANSACTION_UNVERIFIED"
    )
    assert len(store.writes) == 1
    assert _SUBJECT not in result.output
    assert _INSPECTOR not in result.output


def test_inspect_returns_only_declared_and_observed_privilege_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    store = _MemoryStore()
    _enroll_direct(directory, monkeypatch, store)
    requests = _inspection_transport(monkeypatch)
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "meta-token",
            "inspect",
            "--profile",
            "meta-cli-read",
        ],
    )
    data = _result_data(result)
    assert len(requests) == 1
    assert data["declared_purpose"] == "READ"
    assert data["expected_application"] == "VERIFIED"
    assert data["subject_type"] == "USER"
    assert data["required_scope"] == "OBSERVED"
    assert data["additional_granted_scope_count"] == 1
    assert data["account_access_verified"] is False
    assert data["app_review_verified"] is False
    assert data["remote_mutations"] is False
    assert data["execution_authority"] is False
    assert len(result.output.encode()) <= 1024
    for private in (
        _SUBJECT,
        _INSPECTOR,
        "meta-cli-read",
        "12345",
        "67890",
        "54321",
        "ads_read",
        "public_profile",
        "credential-binding",
        "meta_token_principal",
    ):
        assert private not in result.output


def test_inspect_accepts_independent_data_access_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    store = _MemoryStore()
    _enroll_direct(directory, monkeypatch, store)
    _inspection_transport(
        monkeypatch,
        expires_at=_NOW + timedelta(minutes=5),
        data_access_expires_at=_NOW + timedelta(minutes=1),
    )
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "meta-token",
            "inspect",
            "--profile",
            "meta-cli-read",
        ],
    )
    data = _result_data(result)
    assert data["expires_at"] > data["data_access_expires_at"]


def test_inspection_configuration_change_after_provider_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    store = _MemoryStore()
    _enroll_direct(directory, monkeypatch, store)
    _inspection_transport(monkeypatch)
    original = meta_token_cli.inspect_configured_meta_token

    def changed(
        directory_arg: Path,
        profile_id: str,
        *,
        expected_configuration_digest: str | None = None,
    ):
        value = original(
            directory_arg,
            profile_id,
            expected_configuration_digest=expected_configuration_digest,
        )
        _configure(directory_arg, maximum=2048, replace=True)
        return value

    monkeypatch.setattr(meta_token_cli, "inspect_configured_meta_token", changed)
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "meta-token",
            "inspect",
            "--profile",
            "meta-cli-read",
        ],
    )
    assert result.exit_code == 6
    assert json.loads(result.output)["diagnostics"][0]["code"] == "CONFIGURATION_CHANGED"


def test_inspection_configuration_retarget_at_workflow_handoff_stops_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "data"
    _configure(directory)
    store = _MemoryStore()
    _enroll_direct(directory, monkeypatch, store)
    original_prepare = meta_token_workflow._prepare_meta_token_inspection
    calls = 0

    def retargeted_prepare(directory_arg: Path, profile_id: str):
        current = load_configuration(directory_arg)
        save_configuration(
            directory_arg,
            current.model_copy(
                update={"output": current.output.model_copy(update={"max_output_bytes": 2048})}
            ),
            replace=True,
        )
        return original_prepare(directory_arg, profile_id)

    def forbidden_http(**_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        pytest.fail("configuration retarget must stop before the fixed inspector GET")

    monkeypatch.setattr(meta_token_workflow, "_prepare_meta_token_inspection", retargeted_prepare)
    monkeypatch.setattr(meta_token_workflow, "inspect_meta_token", forbidden_http)
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "auth",
            "meta-token",
            "inspect",
            "--profile",
            "meta-cli-read",
        ],
    )
    assert result.exit_code == 6
    assert calls == 0
    assert json.loads(result.output)["diagnostics"][0]["code"] == "CONFIGURATION_CHANGED"
