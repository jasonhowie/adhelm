"""Native evidence requires completed XCTest cases and matching aggregate counts."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/validate_native_approval.py"
    spec = importlib.util.spec_from_file_location("native_validation", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


_CASES = (
    "Test Case '-[CoreTests testFirst]' started.\n"
    "Test Case '-[CoreTests testFirst]' passed (0.001 seconds).\n"
    "Test Case '-[CoreTests testSecond]' started.\n"
    "Test Case '-[CoreTests testSecond]' passed (0.002 seconds).\n"
)
_SUMMARY = (
    "Test Suite 'All tests' passed at 2026-09-05 03:00:00.000.\n"
    "\t Executed 2 tests, with 0 failures (0 unexpected) in 0.003 (0.004) seconds\n"
)
_TARGET_DIAGNOSTICS = {
    "adhelm-macos-installed-approval-host": (
        "APPROVAL_HOST_ARGUMENTS_UNSUPPORTED: This host accepts no launch arguments.\n"
    ),
    "adhelm-macos-approval-build-inspector": (
        "APPROVAL_BUILD_ARGUMENTS_UNSUPPORTED: Build inspection accepts no arguments.\n"
    ),
    "adhelm-macos-approval-client": (
        "APPROVAL_CLIENT_ARGUMENTS_UNSUPPORTED: This client accepts no launch arguments.\n"
    ),
    "adhelm-macos-meta-campaign-pause-approval-client": (
        "APPROVAL_META_CAMPAIGN_PAUSE_CLIENT_ARGUMENTS_UNSUPPORTED: "
        "This client accepts no launch arguments.\n"
    ),
}


def _exact_target_refusal(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    diagnostic = (
        _TARGET_DIAGNOSTICS[Path(arguments[0]).name]
        if len(arguments) > 1
        else (
            "APPROVAL_HOST_NOT_INSTALLED: "
            "This compiled foundation has no installed trusted-host configuration.\n"
        )
    )
    return subprocess.CompletedProcess(arguments, 64 if len(arguments) > 1 else 78, "", diagnostic)


@pytest.mark.parametrize(
    "content",
    [
        "",
        '<testsuites tests="0"><testsuite/></testsuites>',
        _SUMMARY,
        _CASES,
        _CASES + _SUMMARY.replace("Executed 2", "Executed 99"),
        _CASES.replace("passed", "failed", 1) + _SUMMARY,
        _CASES.replace("passed", "skipped", 1) + _SUMMARY,
        _CASES.replace("testSecond", "testFirst") + _SUMMARY,
        _CASES.replace("Test Case '-[CoreTests testFirst]' started.\n", "") + _SUMMARY,
        _CASES + _SUMMARY + _SUMMARY,
        _CASES + _SUMMARY + "WARNING: ThreadSanitizer: data race\n",
        _CASES + _SUMMARY.replace("All tests", "Selected tests"),
    ],
)
def test_incomplete_native_report_cannot_be_evidence(content: str) -> None:
    with pytest.raises(RuntimeError):
        module().test_counts(content)


def test_counts_require_actual_case_results_matching_the_completed_suite() -> None:
    transcript = "Build complete! (9.23s)\n" + _CASES + _SUMMARY
    assert module().test_counts(transcript) == {"passed": 2, "failed": 0, "skipped": 0}


def test_native_tools_do_not_inherit_compiler_or_credential_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overrides = [
        "SWIFT_EXEC",
        "DYLD_INSERT_LIBRARIES",
        "LD_PRELOAD",
        "SDKROOT",
        "TOOLCHAINS",
        "CC",
        "DEVELOPER_DIR",
        "TSAN_OPTIONS",
        "ASAN_OPTIONS",
        "UBSAN_OPTIONS",
    ]
    for name in overrides:
        monkeypatch.setenv(name, "untrusted-override")
    loaded = module()
    environment = loaded.validation_environment(tmp_path)
    assert not set(overrides) & environment.keys()
    assert environment["LC_ALL"] == "C"
    options = loaded.swift_options(tmp_path / "package", tmp_path)
    assert "--disable-keychain" in options and "--disable-netrc" in options
    assert "--disable-automatic-resolution" in options
    assert "--disable-sandbox" not in options
    assert options[options.index("--config-path") + 1] == str(tmp_path / "config")


def test_failed_runner_preserves_actual_output_before_raising(tmp_path: Path) -> None:
    loaded = module()
    log = tmp_path / "failed-tests.txt"
    with pytest.raises(RuntimeError, match="Native validation failed \\(7\\)"):
        loaded.run(
            [sys.executable, "-c", "import sys; print('NO_CASES_RAN'); sys.exit(7)"],
            cwd=tmp_path,
            environment=dict(os.environ),
            log_path=log,
        )
    assert log.read_text() == "NO_CASES_RAN\n"
    with pytest.raises(RuntimeError, match="incomplete"):
        loaded.test_counts(log.read_text())


@pytest.mark.parametrize(
    ("exit_code", "stdout", "stderr"),
    [(0, "", ""), (78, "APPROVAL_HOST_NOT_INSTALLED\n", ""), (78, "", "wrong\n")],
)
def test_unconfigured_smoke_rejects_success_and_inexact_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int, stdout: str, stderr: str
) -> None:
    loaded = module()
    monkeypatch.setattr(
        loaded.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], exit_code, stdout, stderr),
    )
    with pytest.raises(RuntimeError, match="exact refusal"):
        loaded.unconfigured_launch_smoke(tmp_path / "synthetic-host", tmp_path)
    assert '"status": "FAILED"' in (tmp_path / "unconfigured-launch.json").read_text()


def test_unconfigured_smoke_timeout_cannot_be_reported_as_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = module()

    def timeout(*args: object, **kwargs: object) -> None:
        assert kwargs["timeout"] == 5
        raise subprocess.TimeoutExpired("synthetic-host", 5)

    monkeypatch.setattr(loaded.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="promptly"):
        loaded.unconfigured_launch_smoke(tmp_path / "synthetic-host", tmp_path)
    assert "FAILED_TO_COMPLETE" in (tmp_path / "unconfigured-launch.json").read_text()


def test_configured_metadata_is_rejected_before_any_executable_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = module()
    metadata = tmp_path / "Sources/ApprovalHostCore/GeneratedApprovalHostBuildMetadata.swift"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("synthetic configured build source")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("A configured target must never be launched by offline smoke validation.")

    monkeypatch.setattr(loaded.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="exact unconfigured metadata"):
        loaded.unconfigured_artifact_targets_smoke(tmp_path, tmp_path, tmp_path)


@pytest.mark.parametrize("selection", ["configured", "missing"])
def test_nondefault_meta_route_is_rejected_before_any_executable_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    loaded = module()
    relative = Path("Sources/ApprovalHostCore/GeneratedApprovalHostBuildMetadata.swift")
    source_package = Path(__file__).resolve().parents[1] / "native/macos-approval-host"
    metadata = tmp_path / relative
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes((source_package / relative).read_bytes())
    if selection == "configured":
        (metadata.parent / "GeneratedMetaCampaignPauseApprovalRouteBinding.swift").write_text(
            "synthetic configured Meta route source"
        )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("A configured Meta route must never be launched by offline validation.")

    monkeypatch.setattr(loaded.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="exact absent Meta route"):
        loaded.unconfigured_artifact_targets_smoke(tmp_path, tmp_path, tmp_path)
    assert not (tmp_path / "unconfigured-artifact-targets.json").exists()


@pytest.mark.parametrize("outcome", ["success", "wrong-refusal", "timeout"])
@pytest.mark.parametrize(
    "failed_target",
    ["adhelm-macos-installed-approval-host", "adhelm-macos-meta-campaign-pause-approval-client"],
)
def test_artifact_target_failure_never_becomes_passing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, failed_target: str
) -> None:
    loaded = module()
    package = Path(__file__).resolve().parents[1] / "native/macos-approval-host"

    def fail(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 5
        if Path(arguments[0]).name != failed_target:
            return _exact_target_refusal(arguments)
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("synthetic-target", 5)
        return subprocess.CompletedProcess([], 0 if outcome == "success" else 78, "", "wrong")

    monkeypatch.setattr(loaded.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="Unconfigured artifact target"):
        loaded.unconfigured_artifact_targets_smoke(tmp_path, package, tmp_path)
    assert '"status": "FAILED' in (tmp_path / "unconfigured-artifact-targets.json").read_text()


def test_default_targets_include_helper_and_require_each_exact_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = module()
    package = Path(__file__).resolve().parents[1] / "native/macos-approval-host"
    seen: list[tuple[str, bool, bool]] = []

    def refuse(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        target = Path(arguments[0]).name
        has_arguments = len(arguments) > 1
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        seen.append((target, has_arguments, "ADHELM_APPROVAL_HOST_REF" in environment))
        assert kwargs["timeout"] == 5
        return _exact_target_refusal(arguments)

    monkeypatch.setattr(loaded.subprocess, "run", refuse)
    result = loaded.unconfigured_artifact_targets_smoke(tmp_path, package, tmp_path)
    assert result["cases"] == 16 and result["status"] == "PASSED"
    assert set(seen) == {
        (target, arguments, overrides)
        for target in _TARGET_DIAGNOSTICS
        for arguments in (False, True)
        for overrides in (False, True)
    }
