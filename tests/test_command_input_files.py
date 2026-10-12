"""Every migrated JSON CLI boundary rejects unsafe files before downstream work."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
from typer.testing import CliRunner

from adhelm import (
    artifact_cli,
    conversion_cli,
    ga4_create_cli,
    ga4_key_event_candidate_cli,
    join_cli,
    plan_cli,
)


@dataclass(frozen=True)
class _CommandInput:
    name: str
    module: ModuleType
    app: object
    arguments: tuple[str, ...]
    downstream_name: str


_INPUT = "{input}"
_DIGEST = "0" * 64
_COMMANDS = (
    _CommandInput(
        "artifacts configure",
        artifact_cli,
        artifact_cli.app,
        ("configure", "--input", _INPUT),
        "update_configuration",
    ),
    _CommandInput(
        "artifacts policy",
        artifact_cli,
        artifact_cli.app,
        ("policy", "--input", _INPUT),
        "load_configuration",
    ),
    _CommandInput(
        "artifacts report",
        artifact_cli,
        artifact_cli.app,
        ("report", _DIGEST, "--input", _INPUT),
        "load_configuration",
    ),
    _CommandInput(
        "artifacts plan",
        artifact_cli,
        artifact_cli.app,
        ("plan", _DIGEST, "--input", _INPUT),
        "load_configuration",
    ),
    _CommandInput(
        "artifacts measurement-comparison",
        artifact_cli,
        artifact_cli.app,
        ("measurement-comparison", _DIGEST, "f" * 64, "--input", _INPUT),
        "load_configuration",
    ),
    _CommandInput(
        "artifacts ga4-key-event-plan",
        artifact_cli,
        artifact_cli.app,
        ("ga4-key-event-plan", _DIGEST, "--input", _INPUT),
        "load_configuration",
    ),
    _CommandInput(
        "plan openai-pause",
        plan_cli,
        plan_cli.app,
        ("openai-pause", "--input", _INPUT),
        "pause_configuration",
    ),
    _CommandInput(
        "conversion register",
        conversion_cli,
        conversion_cli.app,
        ("register", "--input", _INPUT),
        "compile_conversion_contract",
    ),
    _CommandInput(
        "measurement join create",
        join_cli,
        join_cli.app,
        ("create", "--input", _INPUT, "--ga4-report", _DIGEST),
        "_resolved_map",
    ),
    _CommandInput(
        "measurement key-event-candidate compile",
        ga4_key_event_candidate_cli,
        ga4_key_event_candidate_cli.app,
        ("compile", "--input", _INPUT),
        "load_configuration",
    ),
    _CommandInput(
        "plan ga4-key-event create",
        ga4_create_cli,
        ga4_create_cli.app,
        ("create", "--input", _INPUT),
        "load_configuration",
    ),
    _CommandInput(
        "plan execution create",
        plan_cli,
        plan_cli.execution_plan_app,
        (
            "create",
            "--provider",
            "google_ads",
            "--operation",
            "PAUSE",
            "--input",
            _INPUT,
        ),
        "load_configuration",
    ),
)


def _arguments(command: _CommandInput, input_file: Path) -> list[str]:
    return [str(input_file) if value == _INPUT else value for value in command.arguments]


def _code(result: object) -> str:
    return json.loads(result.output)["diagnostics"][0]["code"]  # type: ignore[union-attr]


def _unsafe_input(directory: Path, kind: str, make_link, make_pipe) -> Path:
    selected = directory / "private-input.json"
    if kind == "symlink":
        target = directory / "private-target.json"
        target.write_text("{}")
        make_link(selected, target)
    elif kind == "fifo":
        selected = make_pipe("private-input.json")
    elif kind == "oversized":
        selected.write_bytes(b"x" * 1_048_577)
    else:
        raise AssertionError(f"unknown fixture kind: {kind}")
    return selected


@pytest.mark.parametrize("command", _COMMANDS, ids=lambda item: item.name)
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("symlink", "INPUT_FILE_INVALID"),
        ("fifo", "INPUT_FILE_INVALID"),
        ("oversized", "INPUT_TOO_LARGE"),
    ],
)
def test_json_commands_reject_unsafe_input_before_configuration_key_or_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: _CommandInput,
    kind: str,
    expected: str,
    make_link,
    make_pipe,
) -> None:
    selected = _unsafe_input(tmp_path, kind, make_link, make_pipe)
    sentinel = "downstream-private-sentinel"

    def prohibited(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(sentinel)

    monkeypatch.setattr(command.module, command.downstream_name, prohibited)
    result = CliRunner().invoke(command.app, _arguments(command, selected))

    assert result.exit_code != 0, result.output
    assert _code(result) == expected
    assert sentinel not in result.output
    assert str(selected) not in result.output


def test_artifact_configure_keeps_raw_schema_version_type_before_configuration_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "private-output-preferences.json"
    selected.write_bytes(b'{"schema_version":1.0}')

    def prohibited(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("raw float schema version reached configuration write")

    monkeypatch.setattr(artifact_cli, "update_configuration", prohibited)
    result = CliRunner().invoke(artifact_cli.app, ["configure", "--input", str(selected)])

    assert result.exit_code != 0, result.output
    assert _code(result) == "SCHEMA_VALIDATION_FAILED"
    assert str(selected) not in result.output


def test_duplicate_json_is_rejected_before_every_migrated_command_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "private-duplicate.json"
    selected.write_bytes(b'{"field":1,"field":2}')

    for command in _COMMANDS:

        def prohibited(
            *_args: object, command_name: str = command.name, **_kwargs: object
        ) -> object:
            raise AssertionError(f"duplicate JSON reached {command_name}")

        monkeypatch.setattr(command.module, command.downstream_name, prohibited)
        result = CliRunner().invoke(command.app, _arguments(command, selected))
        assert result.exit_code != 0, result.output
        assert _code(result) == "INVALID_JSON"
        assert str(selected) not in result.output
