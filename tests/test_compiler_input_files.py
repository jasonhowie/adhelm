"""Compiler CLIs consume only bounded regular JSON files before local effects."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from test_strategy import _six_strategy
from typer.testing import CliRunner, Result

from adhelm import additional_compile_cli, compile_cli, strategy_cli
from adhelm.cli import app

_CASES = (
    ("openai", "openai-ads", compile_cli),
    ("meta", "meta-ads", compile_cli),
    ("google_ads", "google-ads", compile_cli),
    ("linkedin", "linkedin-ads", additional_compile_cli),
    ("reddit", "reddit-ads", additional_compile_cli),
    ("tiktok", "tiktok-ads", additional_compile_cli),
    ("strategy", "strategy", strategy_cli),
)


def _brief(provider: str) -> Any:
    if provider == "strategy":
        return _six_strategy()
    return import_module(f"test_{provider}_compiler")._brief()


def _replace_money_scalar(data: dict[str, Any], provider: str, value: object) -> None:
    if provider == "strategy":
        data["total_caps_by_currency"][0]["minor_units"] = value
        return
    field = {
        "openai": "lifetime_budget",
        "meta": "lifetime_budget",
        "google_ads": "total_budget",
        "linkedin": "group_total_budget",
        "reddit": "lifetime_budget",
        "tiktok": "total_budget",
    }[provider]
    data[field]["minor_units"] = value


def _reject_key() -> bytes:
    pytest.fail("Input validation must finish before sharing-key access")


def _unexpected_retention(*_args: object, **_kwargs: object) -> None:
    pytest.fail("Input validation must finish before ledger retention")


def _run(tmp_path: Path, command: str, input_file: Path, *, retain: bool) -> Result:
    arguments = [
        "--data-dir",
        str(tmp_path / "data"),
        "compile",
        command,
        "--input",
        str(input_file),
    ]
    if retain:
        arguments.append("--retain")
    return CliRunner().invoke(app, arguments)


@pytest.fixture(autouse=True)
def fixed_strategy_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_cli, "_utc_now", lambda: datetime(2026, 9, 5, tzinfo=UTC))


@pytest.mark.parametrize(("provider", "command", "module"), _CASES)
def test_every_compiler_rejects_a_fifo_before_key_or_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    command: str,
    module: object,
    make_pipe,
) -> None:
    input_file = make_pipe(f"{provider}-private-input")
    monkeypatch.setattr(module, "export_key", _reject_key)
    monkeypatch.setattr(module, "_retain_compilation", _unexpected_retention, raising=False)
    monkeypatch.setattr(module, "_retain_strategy", _unexpected_retention, raising=False)

    result = _run(tmp_path, command, input_file, retain=True)

    assert result.exit_code == 2, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["data"] == {}
    assert envelope["diagnostics"][0]["code"] == "INPUT_FILE_INVALID"
    assert input_file.name not in result.output
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(("provider", "command", "module"), _CASES)
@pytest.mark.parametrize("invalid_kind", ("float", "boolean"))
def test_every_compiler_preserves_raw_money_types_before_key_or_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    command: str,
    module: object,
    invalid_kind: str,
) -> None:
    data = _brief(provider).model_dump(mode="json")
    current = (
        data["total_caps_by_currency"][0]["minor_units"]
        if provider == "strategy"
        else data[
            {
                "openai": "lifetime_budget",
                "meta": "lifetime_budget",
                "google_ads": "total_budget",
                "linkedin": "group_total_budget",
                "reddit": "lifetime_budget",
                "tiktok": "total_budget",
            }[provider]
        ]["minor_units"]
    )
    _replace_money_scalar(data, provider, float(current) if invalid_kind == "float" else True)
    input_file = tmp_path / f"{provider}-{invalid_kind}.json"
    input_file.write_text(json.dumps(data))
    monkeypatch.setattr(module, "export_key", _reject_key)
    monkeypatch.setattr(module, "_retain_compilation", _unexpected_retention, raising=False)
    monkeypatch.setattr(module, "_retain_strategy", _unexpected_retention, raising=False)

    result = _run(tmp_path, command, input_file, retain=True)

    assert result.exit_code == 2, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["data"] == {}
    assert envelope["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(("provider", "command", "module"), _CASES)
def test_every_compiler_accepts_a_regular_valid_input_after_checked_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    command: str,
    module: object,
) -> None:
    input_file = tmp_path / f"{provider}-brief.json"
    input_file.write_text(_brief(provider).model_dump_json())
    monkeypatch.setattr(module, "export_key", lambda: b"k" * 32)

    result = _run(tmp_path, command, input_file, retain=False)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["outcome"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert not (tmp_path / "data").exists()
