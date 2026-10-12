"""The package, command and error type carry the AdHelm identity and nothing of the old name."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

from typer.testing import CliRunner

import adhelm
import adhelm.cli as root_cli
import adhelm.errors as errors

_ROOT = Path(__file__).resolve().parents[1]
_RETIRED_PACKAGE = "ad" + "cp"
_PROJECT = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_distribution_command_and_wheel_layout_use_the_new_name() -> None:
    project = _PROJECT["project"]
    assert project["name"] == "adhelm"
    assert project["version"] == adhelm.__version__
    assert ".dev" not in adhelm.__version__  # the artifacts are named for the release version
    assert project["scripts"] == {"adhelm": "adhelm.cli:main"}
    wheel = _PROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/adhelm"]
    assert set(wheel["force-include"].values()) == {
        "adhelm/bundled_skills",
        "adhelm/pilot_record_files",
    }
    assert _PROJECT["tool"]["mypy"]["packages"] == ["adhelm"]


def _source_files() -> list[str]:
    """Tracked and unadded, non-ignored files under `src`; ignored build leftovers do not count."""
    listing = subprocess.run(
        [
            "git",
            "-C",
            str(_ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "src",
        ],
        capture_output=True,
        check=True,
    ).stdout.decode()
    return sorted(name for name in listing.split("\0") if name)


def test_the_source_tree_ships_no_package_alias_for_the_retired_name() -> None:
    # An unrelated distribution may legitimately own the retired import name in a developer's
    # environment, and an ignored `__pycache__` can outlive a checkout, so this checks the files
    # the project tracks rather than what can be imported or what happens to be on disk.
    files = _source_files()
    assert files, "no source files were listed"
    assert {name.split("/")[1] for name in files} == {"adhelm"}
    assert not [name for name in files if _RETIRED_PACKAGE in name.lower()]
    assert not [name for name in dir(errors) if _RETIRED_PACKAGE in name.lower()]
    assert not [name for name in dir(root_cli) if _RETIRED_PACKAGE in name.lower()]
    assert errors.AdhelmError.__name__ == "AdhelmError"


def test_the_command_reports_the_release_version_in_its_envelope(tmp_path: Path) -> None:
    result = CliRunner().invoke(root_cli.app, ["--data-dir", str(tmp_path), "version"])
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["tool_version"] == adhelm.__version__
    assert envelope["command"] == "version"


def test_the_module_entry_point_runs_as_the_new_package(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "adhelm", "--data-dir", str(tmp_path), "version"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["tool_version"] == adhelm.__version__
