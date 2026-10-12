"""Fake-only build boundary tests for paired native/Python approval artifacts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_build_native_approval_artifact import (
    _EPOCH,
    _descriptor_bytes,
    _fake_runner,
    _write_source_tree,
    _write_spec,
    builder,
)


def test_paired_build_environment_has_no_inherited_configuration_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "UV_INDEX",
        "UV_CONFIG_FILE",
        "UV_PYTHON",
        "UV_OFFLINE",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PIP_INDEX_URL",
        "VIRTUAL_ENV",
        "OPENAI_API_KEY",
        "UNRELATED_CUSTOM_SECRET",
        "DYLD_INSERT_LIBRARIES",
        "DEVELOPER_DIR",
    ):
        monkeypatch.setenv(name, "synthetic-private-sentinel")
    environment = builder()._sanitized_environment(tmp_path, epoch=_EPOCH)
    assert set(environment) == {
        "PATH",
        "LC_ALL",
        "LANG",
        "TZ",
        "HOME",
        "TMPDIR",
        "SOURCE_DATE_EPOCH",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_ATTR_NOSYSTEM",
    }
    assert "synthetic-private-sentinel" not in environment.values()
    assert environment["SOURCE_DATE_EPOCH"] == str(_EPOCH)
    assert environment["TZ"] == "UTC"


def test_uv_allowlist_admits_only_fixed_offline_build_and_version(tmp_path: Path) -> None:
    loaded = builder()
    source, workspace = tmp_path / "source", tmp_path / "workspace"
    context = (source, workspace)
    command = loaded._python_wheel_command(source, workspace)
    assert loaded._allowed_tool(command, python_build_context=context)
    assert loaded._allowed_tool([loaded._UV, "--version"])
    assert not loaded._allowed_tool(command)
    assert not loaded._allowed_tool(command, python_build_context=(source, tmp_path / "other"))
    for option in ("--offline", "--no-config", "--no-sources", "--no-python-downloads"):
        altered = list(command)
        altered.remove(option)
        assert not loaded._allowed_tool(altered, python_build_context=context)
    for forbidden in (
        [loaded._UV, "run", "adhelm"],
        [loaded._UV, "pip", "install", "arbitrary"],
        [loaded._UV, "build", "--refresh"],
        ["/Applications/AdHelmApprovalHost.app/Contents/MacOS/adhelm-macos-approval-client"],
    ):
        assert not loaded._allowed_tool(forbidden, python_build_context=context)


@pytest.mark.parametrize("tamper", ["generated-source", "bridge-source", "uv-version"])
def test_changed_build_facts_fail_before_artifact_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    loaded = builder()
    repo = tmp_path / "repository"
    repo.mkdir()
    _write_source_tree(repo)
    specification = _write_spec(tmp_path)
    _fake_runner(monkeypatch, repo=repo, descriptor=_descriptor_bytes(specification))
    fake = loaded._run_tool

    def altered(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        result = fake(arguments, **kwargs)
        if arguments == [loaded._UV, "--version"] and tamper == "uv-version":
            return subprocess.CompletedProcess(arguments, 0, b"uv unexpected\n", b"")
        if arguments[0] == loaded._UV and "build" in arguments:
            source, _ = kwargs["python_build_context"]
            if tamper == "generated-source":
                selected = source / loaded._GENERATED_PYTHON_RELATIVE_PATH
                selected.write_bytes(selected.read_bytes() + b"# changed\n")
            if tamper == "bridge-source":
                selected = source / "src/adhelm/_installed_native_approval_bridge.py"
                selected.write_bytes(b"altered-coordinator")
        return result

    monkeypatch.setattr(loaded, "_run_tool", altered)
    expected = {
        "generated-source": "NATIVE_ARTIFACT_METADATA_INVALID",
        "bridge-source": "NATIVE_ARTIFACT_PYTHON_SOURCE_CHANGED",
        "uv-version": "NATIVE_ARTIFACT_PYTHON_TOOLCHAIN_MISMATCH",
    }[tamper]
    output = tmp_path / "output"
    with pytest.raises(loaded.ArtifactBuildError, match=expected):
        loaded.build_native_approval_artifact(
            repo=repo,
            revision="HEAD",
            specification=specification,
            output=output,
            source_date_epoch=_EPOCH,
        )
    assert not output.exists()
