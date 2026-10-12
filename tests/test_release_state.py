"""One constant names the release state; every release surface that reports it follows it.

The release surfaces are `adhelm version`, `discover` and `doctor` and
`scripts/check_repository.py`. The development-evidence scripts (`validate_development`,
`validate_native_approval`, `traceability`) report the v1.0.0 definition-of-done state and do not
follow the constant; this module does not cover them.
"""

import ast
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest
from typer.testing import CliRunner

import adhelm
from adhelm.cli import app

_ROOT = Path(__file__).resolve().parents[1]
_runner = CliRunner()


def _defined_gate_states() -> list[str]:
    """The states in the "Gate states" table of `docs/release-0.1.0.md`, in table order."""
    document = (_ROOT / "docs/release-0.1.0.md").read_text(encoding="utf-8")
    section = document.partition("\n## Gate states\n")[2].partition("\n## ")[0]
    return re.findall(r"(?m)^\| `([A-Z0-9_]+)` \|", section)


def _spelled_states(source: str) -> list[str]:
    """The gate states that `source` spells as a whole string literal."""
    states = set(_defined_gate_states())
    return [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in states
    ]


def _repository_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_repository_under_test", _ROOT / "scripts/check_repository.py"
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_the_release_document_defines_the_three_gate_states_in_order() -> None:
    assert _defined_gate_states() == [
        "IMPLEMENTING",
        "READY_FOR_USER_AUTHORIZATION",
        "RELEASED_0_1_0",
    ]


def test_the_tree_reports_the_released_state() -> None:
    # 0.1.0 was published to PyPI on 2026-10-12, so the tree reports the final gate state.
    assert adhelm.RELEASE_STATE == "RELEASED_0_1_0"
    assert adhelm.RELEASE_STATE in _defined_gate_states()


@pytest.mark.parametrize("command", ["version", "discover", "doctor"])
def test_the_command_reports_the_release_state(tmp_path: Path, command: str) -> None:
    result = _runner.invoke(app, ["--data-dir", str(tmp_path / "data"), command])
    assert json.loads(result.stdout)["data"]["release_state"] == adhelm.RELEASE_STATE


@pytest.mark.parametrize("command", ["version", "discover", "doctor"])
def test_the_command_follows_the_constant_and_keeps_no_copy_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setattr("adhelm.cli.RELEASE_STATE", "A_STATE_THE_CONSTANT_WAS_CHANGED_TO")
    result = _runner.invoke(app, ["--data-dir", str(tmp_path / "data"), command])
    assert json.loads(result.stdout)["data"]["release_state"] == (
        "A_STATE_THE_CONSTANT_WAS_CHANGED_TO"
    )


def test_the_repository_check_reports_and_follows_the_constant(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _repository_checker()
    checker.main()
    assert json.loads(capsys.readouterr().out)["release_state"] == adhelm.RELEASE_STATE
    monkeypatch.setattr(checker, "RELEASE_STATE", "A_STATE_THE_CONSTANT_WAS_CHANGED_TO")
    checker.main()
    assert json.loads(capsys.readouterr().out)["release_state"] == (
        "A_STATE_THE_CONSTANT_WAS_CHANGED_TO"
    )


def test_only_the_constant_spells_a_gate_state() -> None:
    sources = [*sorted((_ROOT / "src/adhelm").rglob("*.py")), _ROOT / "scripts/check_repository.py"]
    spelled = {
        path.relative_to(_ROOT).as_posix(): states
        for path in sources
        if (states := _spelled_states(path.read_text(encoding="utf-8")))
    }
    # This test checks where the state is spelled, not its value; the value is pinned by
    # test_the_build_reports_the_state_it_stops_at and tests/test_core.py.
    assert spelled == {"src/adhelm/__init__.py": [adhelm.RELEASE_STATE]}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('data = {"release_state": "IMPLEMENTING"}', ["IMPLEMENTING"]),
        ('state = "READY_FOR_USER_AUTHORIZATION"', ["READY_FOR_USER_AUTHORIZATION"]),
        ('states = ("IMPLEMENTING", "RELEASED_0_1_0")', ["IMPLEMENTING", "RELEASED_0_1_0"]),
        ("from adhelm import RELEASE_STATE\nstate = RELEASE_STATE", []),
        ('text = "release remains IMPLEMENTING"', []),
    ],
)
def test_the_scan_finds_a_gate_state_spelled_as_a_whole_string(
    source: str, expected: list[str]
) -> None:
    assert _spelled_states(source) == expected
