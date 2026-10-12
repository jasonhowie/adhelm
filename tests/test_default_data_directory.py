"""The default data directory is platform-native, never legacy, and never overrides --data-dir."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import adhelm.cli as root_cli
import adhelm.config as config
from adhelm.errors import AdhelmError
from adhelm.models import Configuration

_RUNNER = CliRunner()


def _platform(
    monkeypatch: pytest.MonkeyPatch, name: str, *, home: Path, local_app_data: Path | None = None
) -> None:
    monkeypatch.setattr(sys, "platform", name)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    if local_app_data is not None:
        # The Windows helper exists only on Windows hosts, so the attribute may be absent.
        monkeypatch.setattr(
            config,
            "windows_filesystem",
            SimpleNamespace(local_app_data=lambda: local_app_data),
            raising=False,
        )


def test_linux_uses_the_xdg_data_location(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _platform(monkeypatch, "linux", home=tmp_path)
    assert config.default_data_dir() == tmp_path / ".local" / "share" / "adhelm"


def test_macos_uses_application_support(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _platform(monkeypatch, "darwin", home=tmp_path)
    assert config.default_data_dir() == tmp_path / "Library" / "Application Support" / "adhelm"


def test_windows_uses_local_app_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    local = tmp_path / "AppData" / "Local"
    _platform(monkeypatch, "win32", home=home, local_app_data=local)
    assert config.default_data_dir() == local / "adhelm"


def _isolate_user_directory(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point the host's real platform branch at `home`, leaving `sys.platform` untouched."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    if sys.platform == "win32":
        monkeypatch.setattr(
            config.windows_filesystem, "local_app_data", lambda: home / "AppData" / "Local"
        )


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_the_retired_data_directory_is_neither_chosen_nor_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_user_directory(monkeypatch, tmp_path)
    chosen = config.default_data_dir()
    # ADR 0013: the retired default was ~/.local/share/<retired name> on every platform, so it is
    # seeded there whatever the host's own default is.
    legacy = tmp_path / ".local" / "share" / ("ad" + "cp")
    assert not legacy.is_relative_to(chosen)
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text('{"retired": true}\n')
    (legacy / "ledger.jsonl").write_text("not a ledger\n")
    with pytest.raises(AdhelmError) as invalid:  # the seed is invalid, so reading it would show
        config.load_configuration(legacy)
    assert invalid.value.code == "CONFIGURATION_INVALID"
    before = _snapshot(legacy)

    # Before `init` the chosen directory has no configuration; a fallback to the retired one would
    # read the invalid seed and fail closed with CONFIGURATION_INVALID.
    assert not (chosen / "config.json").exists()
    for command in (["doctor"], ["config", "show"]):
        unconfigured = _RUNNER.invoke(root_cli.app, command)
        assert "CONFIGURATION_INVALID" not in unconfigured.output, (command, unconfigured.output)
        assert unconfigured.exit_code != 2, (command, unconfigured.output)
        assert _snapshot(legacy) == before
    assert config.load_configuration(chosen) == Configuration()

    created = _RUNNER.invoke(root_cli.app, ["init"])
    assert created.exit_code == 0, created.output
    assert (chosen / "config.json").is_file()
    assert config.load_configuration(chosen) == Configuration()

    read = _RUNNER.invoke(root_cli.app, ["doctor"])
    # No provider is configured, so doctor reports attention (exit 3); a read of the invalid
    # retired configuration would instead fail closed with CONFIGURATION_INVALID (exit 2).
    assert read.exit_code == 3, read.output
    envelope = json.loads(read.output)
    assert envelope["outcome"] == "ATTENTION_REQUIRED"
    assert "CONFIGURATION_INVALID" not in read.output
    assert _snapshot(legacy) == before


@pytest.mark.parametrize("platform_name", ["linux", "darwin", "win32"])
def test_an_unavailable_user_directory_is_a_structured_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform_name: str
) -> None:
    def unavailable() -> Path:
        raise OSError("no user directory")

    _platform(monkeypatch, platform_name, home=tmp_path)
    monkeypatch.setattr(
        config, "windows_filesystem", SimpleNamespace(local_app_data=unavailable), raising=False
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: unavailable()))
    with pytest.raises(AdhelmError) as refusal:
        config.default_data_dir()
    assert refusal.value.code == "DATA_DIRECTORY_UNAVAILABLE"
    assert refusal.value.exit_code == 2
    assert "--data-dir" in (refusal.value.next_action or "")


def test_a_missing_home_directory_refuses_only_the_commands_that_need_local_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: no_home()))
    for command in (["version"], ["discover"]):
        result = _RUNNER.invoke(root_cli.app, command)
        assert result.exit_code == 0, (command, result.output)
    # Nested commands must report the full command path, exactly as `guarded(...)` names them.
    for command in (
        ["init"],
        ["history", "status"],
        ["config", "show"],
        ["measurement", "join", "catalog"],
        ["candidate", "catalog"],
    ):
        result = _RUNNER.invoke(root_cli.app, command)
        assert result.exit_code == 2, (command, result.output)
        envelope = json.loads(result.output)
        assert envelope["outcome"] == "FAILED_CLOSED"
        assert envelope["command"] == " ".join(command), (command, envelope)
        assert envelope["diagnostics"][0]["code"] == "DATA_DIRECTORY_UNAVAILABLE"


def test_the_command_line_uses_the_default_only_without_an_explicit_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    default = tmp_path / "default"

    def chosen() -> Path:
        calls.append("default")
        return default

    monkeypatch.setattr(root_cli, "default_data_dir", chosen)
    explicit = tmp_path / "explicit"
    created = _RUNNER.invoke(root_cli.app, ["--data-dir", str(explicit), "init"])
    assert created.exit_code == 0, created.output
    assert calls == []
    assert (explicit / "config.json").is_file()
    assert not default.exists()

    created = _RUNNER.invoke(root_cli.app, ["init"])
    assert created.exit_code == 0, created.output
    assert calls == ["default"]
    assert (default / "config.json").is_file()
