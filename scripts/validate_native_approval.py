"""Build and test an exact committed native approval-host foundation on macOS.

This records local development evidence only. It neither enrolls keys nor
installs, release-signs, authenticates, or starts an approval service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

_UNCONFIGURED_METADATA_SHA256 = "3278b078c8bf70d8a006ac4b75a1e147b1a7cbbd3f1461eee8d515a9299f4180"
_UNCONFIGURED_META_ROUTE_SHA256 = "eb068767a51e8b9dc3970eb43017d4273b0fa45e13d6525b12a3dfd08d06d625"


def validation_environment(output: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith(("SWIFT", "DYLD_", "LD_", "GIT_", "SDKROOT", "TOOLCHAINS")) or name in {
            "DEVELOPER_DIR",
            "CC",
            "CXX",
            "CFLAGS",
            "CXXFLAGS",
            "LDFLAGS",
            "TSAN_OPTIONS",
            "ASAN_OPTIONS",
            "UBSAN_OPTIONS",
        }:
            environment.pop(name)
    environment.update(
        PATH="/usr/bin:/bin:/usr/sbin:/sbin",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        LC_ALL="C",
        CLANG_MODULE_CACHE_PATH=str(output / "clang-modules"),
    )
    return environment


def run(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    log_path: Path | None = None,
    stdout_only: bool = False,
) -> str:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=600,
    )
    output = result.stdout + result.stderr
    if log_path is not None:
        # Failed builds and runner-startup failures are evidence too. Persist
        # the actual transcript before raising; never emit a passing report
        # merely because a test bundle compiled successfully.
        log_path.write_text(output)
    if result.returncode:
        raise RuntimeError(
            f"Native validation failed ({result.returncode}): {arguments[0]}\n"
            f"{result.stdout[-8192:]}\n{result.stderr[-8192:]}"
        )
    return result.stdout if stdout_only else output


def swift_options(package: Path, output: Path) -> list[str]:
    return [
        "--package-path",
        str(package),
        "--scratch-path",
        str(output / "build"),
        "--cache-path",
        str(output / "cache"),
        "--config-path",
        str(output / "config"),
        "--security-path",
        str(output / "security"),
        "--manifest-cache",
        "local",
        "--disable-keychain",
        "--disable-netrc",
        "--skip-update",
        "--disable-automatic-resolution",
        "-Xswiftc",
        "-module-cache-path",
        "-Xswiftc",
        str(output / "swift-modules"),
    ]


def test_counts(output: str) -> dict[str, int]:
    """Cross-check XCTest case results with its one completed top-level suite.

    Swift 6's --xunit-output produces Swift Testing XML, not XCTest XML. A
    zero-case sibling report is therefore not evidence for this package. Run
    XCTest explicitly and retain/hash its full console transcript instead.
    """
    if "ThreadSanitizer:" in output or "Sanitizer:DEADLYSIGNAL" in output:
        raise RuntimeError("Native runtime instrumentation reported a failure.")
    starts = re.findall(r"^Test Case '([^'\r\n]+)' started\.$", output, flags=re.MULTILINE)
    results = re.findall(
        r"^Test Case '([^'\r\n]+)' (passed|failed|skipped)(?: \([0-9.]+ seconds\))?\.$",
        output,
        flags=re.MULTILINE,
    )
    summaries = re.findall(
        r"^Test Suite 'All tests' passed at [^\r\n]+\r?\n"
        r"[ \t]+Executed ([1-9][0-9]*) tests?, with 0 failures \(0 unexpected\) "
        r"in [0-9.]+ \([0-9.]+\) seconds[ \t]*$",
        output,
        flags=re.MULTILINE,
    )
    names = [name for name, _ in results]
    if (
        len(summaries) != 1
        or not results
        or any(status != "passed" for _, status in results)
        or len(names) != len(set(names))
        or len(starts) != len(set(starts))
        or set(starts) != set(names)
        or len(results) != int(summaries[0])
    ):
        raise RuntimeError("Native XCTest transcript is incomplete, inconsistent or failing.")
    return {"passed": len(results), "failed": 0, "skipped": 0}


def unconfigured_launch_smoke(executable: Path, output: Path) -> dict[str, object]:
    """Execute the foundation and require its exact refusal in two launch modes.

    All overrides are synthetic. This executable must not install or activate
    anything, including when arguments or environment suggest configuration.
    """
    expected = (
        "APPROVAL_HOST_NOT_INSTALLED: "
        "This compiled foundation has no installed trusted-host configuration.\n"
    )
    results: list[dict[str, object]] = []
    log = output / "unconfigured-launch.json"
    cases: tuple[tuple[str, list[str]], ...] = (
        ("DEFAULT", []),
        (
            "SYNTHETIC_RUNTIME_OVERRIDES",
            [
                "--host-ref",
                "host:untrusted",
                "--mach-service",
                "invalid.adhelm.fixture",
                "--activate",
            ],
        ),
    )
    for name, arguments in cases:
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}
        if name == "SYNTHETIC_RUNTIME_OVERRIDES":
            environment.update(
                ADHELM_APPROVAL_HOST_REF="host:untrusted",
                ADHELM_APPROVAL_MACH_SERVICE="invalid.adhelm.fixture",
                ADHELM_APPROVAL_SIGNING_KEY_REF="key:untrusted",
                ADHELM_APPROVAL_CODE_REQUIREMENT="true",
            )
        try:
            result = subprocess.run(
                [str(executable), *arguments],
                cwd=output,
                env=environment,
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            results.append({"case": name, "status": "FAILED_TO_COMPLETE"})
            log.write_text(json.dumps(results, indent=2) + "\n")
            raise RuntimeError("Unconfigured native executable did not refuse promptly.") from error
        passed = result.returncode == 78 and result.stdout == "" and result.stderr == expected
        results.append(
            {
                "case": name,
                "status": "PASSED" if passed else "FAILED",
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        log.write_text(json.dumps(results, indent=2) + "\n")
        if not passed:
            raise RuntimeError("Unconfigured native executable did not emit its exact refusal.")
    return {
        "status": "PASSED",
        "cases": len(results),
        "transcript_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "scope": "UNCONFIGURED_EXECUTABLE_REFUSAL_NOT_INSTALLED_HOST_STARTUP",
    }


def unconfigured_artifact_targets_smoke(
    binary_directory: Path, package: Path, output: Path
) -> dict[str, object]:
    """Run only checked-in unconfigured targets, never a generated host app.

    The exact default source hash is an intentional guard against accidentally
    validating a configured product by executing its application entry point.
    Configured artifacts are inspected only with the separate Core-only tool.
    """
    metadata = package / "Sources/ApprovalHostCore/GeneratedApprovalHostBuildMetadata.swift"
    metadata_sha256 = hashlib.sha256(metadata.read_bytes()).hexdigest()
    if metadata_sha256 != _UNCONFIGURED_METADATA_SHA256:
        raise RuntimeError("Native launch smoke requires the exact unconfigured metadata source.")
    meta_route = (
        package / "Sources/ApprovalHostCore/GeneratedMetaCampaignPauseApprovalRouteBinding.swift"
    )
    if (
        not meta_route.is_file()
        or hashlib.sha256(meta_route.read_bytes()).hexdigest() != _UNCONFIGURED_META_ROUTE_SHA256
    ):
        raise RuntimeError("Native launch smoke requires the exact absent Meta route source.")
    log = output / "unconfigured-artifact-targets.json"
    results: list[dict[str, object]] = []
    targets = (
        (
            "adhelm-macos-installed-approval-host",
            "APPROVAL_HOST_ARGUMENTS_UNSUPPORTED: This host accepts no launch arguments.\n",
        ),
        (
            "adhelm-macos-approval-build-inspector",
            "APPROVAL_BUILD_ARGUMENTS_UNSUPPORTED: Build inspection accepts no arguments.\n",
        ),
        (
            "adhelm-macos-approval-client",
            "APPROVAL_CLIENT_ARGUMENTS_UNSUPPORTED: This client accepts no launch arguments.\n",
        ),
        (
            "adhelm-macos-meta-campaign-pause-approval-client",
            "APPROVAL_META_CAMPAIGN_PAUSE_CLIENT_ARGUMENTS_UNSUPPORTED: "
            "This client accepts no launch arguments.\n",
        ),
    )
    for target, argument_diagnostic in targets:
        for overrides in (False, True):
            for arguments in ([], ["--host-ref", "host:synthetic", "--activate"]):
                case = {
                    "target": target,
                    "arguments_supplied": bool(arguments),
                    "synthetic_environment_supplied": overrides,
                }
                environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}
                if overrides:
                    environment.update(
                        ADHELM_APPROVAL_HOST_REF="host:synthetic",
                        ADHELM_APPROVAL_CODE_REQUIREMENT="true",
                        ADHELM_APPROVAL_MACH_SERVICE="invalid.adhelm.synthetic",
                    )
                expected_code = 64 if arguments else 78
                expected_stderr = (
                    argument_diagnostic
                    if arguments
                    else "APPROVAL_HOST_NOT_INSTALLED: "
                    "This compiled foundation has no installed trusted-host configuration.\n"
                )
                try:
                    result = subprocess.run(
                        [str(binary_directory / target), *arguments],
                        cwd=output,
                        env=environment,
                        capture_output=True,
                        text=True,
                        errors="replace",
                        check=False,
                        timeout=5,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    results.append({**case, "status": "FAILED_TO_COMPLETE"})
                    log.write_text(json.dumps(results, indent=2) + "\n")
                    raise RuntimeError(
                        "Unconfigured artifact target did not refuse promptly."
                    ) from error
                passed = (
                    result.returncode == expected_code
                    and result.stdout == ""
                    and result.stderr == expected_stderr
                )
                results.append(
                    {
                        **case,
                        "status": "PASSED" if passed else "FAILED",
                        "exit_code": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                )
                log.write_text(json.dumps(results, indent=2) + "\n")
                if not passed:
                    raise RuntimeError(
                        "Unconfigured artifact target did not emit its exact refusal."
                    )
    return {
        "status": "PASSED",
        "cases": len(results),
        "transcript_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "default_metadata_source_sha256": metadata_sha256,
        "default_meta_route_source_sha256": _UNCONFIGURED_META_ROUTE_SHA256,
        "scope": "UNCONFIGURED_TARGETS_ONLY_NO_CONFIGURED_HOST_EXECUTION",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--swift", default="/usr/bin/swift")
    parser.add_argument("--thread-sanitizer", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("Native macOS validation requires macOS; no platform result was fabricated.")
    repo, output = args.repo.resolve(), args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    environment = validation_environment(output)
    commit = run(
        [
            "/usr/bin/git",
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{args.revision}^{{commit}}",
        ],
        cwd=repo,
        environment=environment,
    ).strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise RuntimeError("Native validation requires one exact source commit.")
    archive = output / "source.tar"
    run(
        ["/usr/bin/git", "archive", "--format=tar", "--output", str(archive), commit],
        cwd=repo,
        environment=environment,
    )
    snapshot = output / "source"
    snapshot.mkdir(mode=0o700)
    with tarfile.open(archive) as bundle:
        bundle.extractall(snapshot, filter="data")
    package = snapshot / "native/macos-approval-host"
    if not (package / "Package.swift").is_file():
        raise RuntimeError("Exact source has no native approval-host package.")
    options = swift_options(package, output)
    if args.thread_sanitizer:
        options.extend(["--sanitize", "thread"])
    toolchain = run([args.swift, "--version"], cwd=output, environment=environment).strip()
    run(
        [args.swift, "build", *options, "--product", "adhelm-macos-approval-host"],
        cwd=output,
        environment=environment,
        log_path=output / "build.txt",
    )
    print("native_product_build: PASSED", flush=True)
    binary_directory = run(
        [args.swift, "build", *options, "--show-bin-path"],
        cwd=output,
        environment=environment,
        log_path=output / "binary-path.txt",
        stdout_only=True,
    ).strip()
    launch_result = unconfigured_launch_smoke(
        Path(binary_directory) / "adhelm-macos-approval-host", output
    )
    print("native_unconfigured_launch_smoke: PASSED", flush=True)
    for product in (
        "adhelm-macos-installed-approval-host",
        "adhelm-macos-approval-build-inspector",
        "adhelm-macos-approval-client",
        "adhelm-macos-meta-campaign-pause-approval-client",
    ):
        run(
            [args.swift, "build", *options, "--product", product],
            cwd=output,
            environment=environment,
            log_path=output / f"build-{product}.txt",
        )
    artifact_launch_result = unconfigured_artifact_targets_smoke(
        Path(binary_directory), package, output
    )
    print("native_unconfigured_artifact_targets_smoke: PASSED", flush=True)
    tests_log = run(
        [args.swift, "test", *options, "--enable-xctest", "--disable-swift-testing"],
        cwd=output,
        environment=environment,
        log_path=output / "tests.txt",
    )
    counts = test_counts(tests_log)
    print("native_offline_tests: PASSED", flush=True)
    result = {
        "schema_version": 1,
        "source_commit": commit,
        "validated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "release_state": "IMPLEMENTING",
        "scope": "MACOS_NATIVE_FOUNDATION_OFFLINE_NOT_INSTALLED_APPROVAL_AUTHORITY",
        "toolchain": toolchain,
        "runtime_instrumentation": "THREAD_SANITIZER" if args.thread_sanitizer else "NONE",
        "checks": {
            "native_product_build": "PASSED",
            "native_offline_tests": counts,
            "native_unconfigured_launch_smoke": launch_result,
            "native_unconfigured_artifact_targets_smoke": artifact_launch_result,
        },
        "test_report_format": "XCTEST_CONSOLE_CASES_AND_ALL_TESTS_SUMMARY",
        "test_report_sha256": hashlib.sha256((output / "tests.txt").read_bytes()).hexdigest(),
        "real_os_authentication": False,
        "real_keychain_enrollment": False,
        "real_installed_host_preflight": False,
        "release_signed_or_installed": False,
        "production_receipt_verification": False,
    }
    (output / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
