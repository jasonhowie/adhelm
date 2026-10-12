import json
from pathlib import Path

import pytest
import typer
from test_credential_setup import FakeStore, _profile
from typer.testing import CliRunner

from adhelm import credential_cli, credential_setup
from adhelm.cli import app as installed_app
from adhelm.config import save_configuration
from adhelm.credential_lock import credential_mutation_lock
from adhelm.models import Configuration, OutputPreferences


def _app(tmp_path: Path) -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def context(ctx: typer.Context) -> None:
        ctx.obj = {"data_dir": tmp_path / "data"}

    app.add_typer(credential_cli.app, name="auth")
    return app


def test_installed_auth_discovery_and_default_handoff_are_noninteractive(tmp_path: Path) -> None:
    runner = CliRunner()
    discovery = runner.invoke(installed_app, ["discover"])
    data = json.loads(discovery.output)["data"]
    assert {"auth status", "auth store", "auth rotate", "auth remove-local"} <= set(
        data["commands"]
    )
    assert data["credential_handoff"]["default"] == "NONINTERACTIVE"
    result = runner.invoke(
        installed_app,
        ["--data-dir", str(tmp_path / "data"), "auth", "store", "--profile", "missing"],
    )
    assert result.exit_code == 4
    assert not (tmp_path / "data").exists()


def test_default_machine_path_never_prompts_or_touches_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden():
        pytest.fail("Machine default must request a terminal handoff before secret access")

    monkeypatch.setattr(credential_setup, "os_store_backend", forbidden)
    monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", forbidden)
    result = CliRunner().invoke(
        _app(tmp_path), ["auth", "store", "--profile", "configured-profile"]
    )
    assert result.exit_code == 4
    assert (
        json.loads(result.output)["diagnostics"][0]["code"] == "EXPLICIT_TERMINAL_HANDOFF_REQUIRED"
    )
    assert not (tmp_path / "data").exists()


def test_local_handoff_round_trip_has_no_secret_alias_or_remote_revocation_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    save_configuration(
        tmp_path / "data",
        Configuration(profiles=[profile], output=OutputPreferences(max_output_bytes=1024)),
    )
    store = FakeStore()
    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: store)
    monkeypatch.setattr(
        credential_setup, "prompt_secret_from_terminal", lambda: "private-test-credential"
    )
    runner = CliRunner()
    app = _app(tmp_path)
    for command, state in (
        (["store", "--interactive"], "PRESENT"),
        (["status"], "PRESENT"),
        (["remove-local"], "LOCAL_REMOVED"),
    ):
        result = runner.invoke(app, ["auth", *command, "--profile", profile.profile_id])
        assert result.exit_code == 0, result.output
        assert len(result.output.encode()) <= 1024
        data = json.loads(result.output)["data"]
        assert data["slots"][0]["state"] == state
        assert data["slots"][0]["remote_revocation_verified"] is False
        assert data["provider_authentication_verified"] is False
        for private in ("private-test-credential", profile.profile_id, profile.credential_ref):
            assert private not in result.output
    assert store.values == {}


def test_explicit_interactive_flag_still_requires_a_real_hidden_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    save_configuration(tmp_path / "data", Configuration(profiles=[profile]))
    store = FakeStore()
    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: store)
    # The terminal check is exercised directly: a noninteractive caller must
    # never get a getpass stdin fallback, regardless of its command flag.
    result = CliRunner().invoke(
        _app(tmp_path),
        ["auth", "store", "--profile", profile.profile_id, "--interactive"],
    )
    assert result.exit_code != 0
    assert json.loads(result.output)["diagnostics"][0]["code"] == "INTERACTIVE_TERMINAL_REQUIRED"
    assert store.values == {}


def test_concurrent_setup_is_busy_before_prompt_or_keychain_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    save_configuration(tmp_path / "data", Configuration(profiles=[profile]))

    def forbidden():
        pytest.fail("An occupied coordination lock must stop before the store or hidden prompt")

    monkeypatch.setattr(credential_setup, "os_store_backend", forbidden)
    monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", forbidden)
    with credential_mutation_lock(profile.credential_ref.upper()):
        result = CliRunner().invoke(
            _app(tmp_path),
            ["auth", "store", "--profile", profile.profile_id, "--interactive"],
        )
    assert result.exit_code == 3
    assert json.loads(result.output)["diagnostics"][0]["code"] == "CREDENTIAL_SETUP_BUSY"


def test_unverified_local_change_cannot_be_reported_as_failed_before_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    save_configuration(
        tmp_path / "data",
        Configuration(profiles=[profile], output=OutputPreferences(max_output_bytes=1024)),
    )
    store = FakeStore(drop_writes=True)
    monkeypatch.setattr(credential_setup, "os_store_backend", lambda: store)
    monkeypatch.setattr(
        credential_setup, "prompt_secret_from_terminal", lambda: "private-test-value"
    )
    result = CliRunner().invoke(
        _app(tmp_path),
        ["auth", "store", "--profile", profile.profile_id, "--interactive"],
    )
    assert result.exit_code == 6
    assert len(result.output.encode()) <= 1024
    output = json.loads(result.output)
    assert output["outcome"] == "LOCAL_CHANGE_UNVERIFIED"
    assert output["data"]["local_state"] == "UNVERIFIED"
    assert output["data"]["remote_revocation_verified"] is False
    assert "private-test-value" not in result.output
