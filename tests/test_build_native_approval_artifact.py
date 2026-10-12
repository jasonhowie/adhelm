"""Fake-only boundaries for the unsigned native approval-host builder."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import plistlib
import stat
import struct
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import replace
from functools import cache
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


@cache
def builder() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/build_native_approval_artifact.py"
    spec = importlib.util.spec_from_file_location("build_native_approval_artifact_test", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


_COMMIT = "a" * 40
_EPOCH = 1_725_000_000
_GENERATED_RELATIVE = (
    Path("native")
    / "macos-approval-host"
    / "Sources"
    / "ApprovalHostCore"
    / "GeneratedApprovalHostBuildMetadata.swift"
)
_GENERATED_META_PYTHON_RELATIVE = Path(
    "src/adhelm/_generated_meta_campaign_pause_approval_bridge_binding_v2.py"
)
_GENERATED_META_ROUTE_RELATIVE = (
    Path("native")
    / "macos-approval-host"
    / "Sources"
    / "ApprovalHostCore"
    / "GeneratedMetaCampaignPauseApprovalRouteBinding.swift"
)
_META_COMPILED_RELATIVE = Path("src/adhelm/_compiled_meta_campaign_pause_approval_bridge_v2.py")


def _contract_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": 1,
        "host_ref": "host:local-review",
        "signing_key_ref": "key:local-receipt",
        "mach_service_name": "com.aptsonic.adhelm.approvalhost",
        "client_bundle_identifier": "com.aptsonic.adhelm.client",
        "client_team_identifier": "ABCDE12345",
        "host_bundle_identifier": "com.aptsonic.adhelm.host",
        "host_team_identifier": "ABCDE12345",
        "signing_key_application_tag_hex": "00616263ff",
        "signing_key_access_group": None,
        "key_storage": "KEYCHAIN_P256",
        "provenance_service": "com.aptsonic.adhelm.provenance",
        "provenance_account": "approval-host-v1",
        "maximum_challenge_bytes": 65_536,
        "maximum_challenge_lifetime_seconds": 300,
        "maximum_issued_at_skew_seconds": 60,
        "maximum_subplans": 6,
        "maximum_policy_items": 64,
    }
    data.update(overrides)
    return data


def _write_spec(directory: Path, **overrides: object) -> Path:
    path = directory / "artifact-spec.json"
    path.write_text(json.dumps(_contract_data(**overrides)), encoding="utf-8")
    return path


def _tar_regular(bundle: tarfile.TarFile, name: str, data: bytes) -> None:
    entry = tarfile.TarInfo(name)
    entry.size = len(data)
    entry.mode = 0o644
    bundle.addfile(entry, io.BytesIO(data))


def _write_source_tree(repo: Path, *, version: str = "0.1.0") -> None:
    generated = repo / _GENERATED_RELATIVE
    generated.parent.mkdir(parents=True)
    generated.write_text("package enum GeneratedApprovalHostBuildMetadata {}\n", encoding="utf-8")
    (repo / "native/macos-approval-host").mkdir(parents=True, exist_ok=True)
    (repo / "native/macos-approval-host/Package.swift").write_text(
        "// swift-tools-version: 6.0\n", encoding="utf-8"
    )
    generated_python = repo / "src/adhelm/_generated_installed_approval_bridge_binding.py"
    generated_python.parent.mkdir(parents=True)
    generated_python.write_text("def _configured_binding(): return None\n")
    source_root = Path(__file__).resolve().parents[1]
    for relative in (_GENERATED_META_PYTHON_RELATIVE, _GENERATED_META_ROUTE_RELATIVE):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source_root / relative).read_bytes())
    (repo / _META_COMPILED_RELATIVE).write_bytes(b"synthetic-meta-compiled-binding\n")
    (generated_python.parent / "cli.py").write_bytes(b"synthetic-cli")
    (generated_python.parent / "py.typed").write_bytes(b"")
    (generated_python.parent / "_installed_native_approval_bridge.py").write_bytes(
        b"synthetic-coordinator"
    )
    (repo / "skills").mkdir()
    (repo / "skills/README.md").write_bytes(b"synthetic-skills")
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "adhelm"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    scripts = repo / "scripts"
    scripts.mkdir()
    repository_scripts = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("build_native_approval_artifact.py", "native_approval_artifact_contract.py"):
        (scripts / name).write_bytes((repository_scripts / name).read_bytes())


def _archive_tree(repo: Path, output: Path) -> None:
    names = (
        "pyproject.toml",
        "native/macos-approval-host/Package.swift",
        _GENERATED_RELATIVE.as_posix(),
        "src/adhelm/_generated_installed_approval_bridge_binding.py",
        _GENERATED_META_PYTHON_RELATIVE.as_posix(),
        _GENERATED_META_ROUTE_RELATIVE.as_posix(),
        _META_COMPILED_RELATIVE.as_posix(),
        "src/adhelm/cli.py",
        "src/adhelm/py.typed",
        "src/adhelm/_installed_native_approval_bridge.py",
        "skills/README.md",
        "scripts/build_native_approval_artifact.py",
        "scripts/native_approval_artifact_contract.py",
    )
    with tarfile.open(output, "w") as bundle:
        for name in names:
            _tar_regular(bundle, name, (repo / name).read_bytes())


def _descriptor_bytes(specification: Path) -> bytes:
    loaded = builder()
    _, descriptor, _ = loaded._load_contract(specification)
    return descriptor.canonical_bytes


def _macho_host_bytes(loaded: ModuleType, *, cpu_subtype: int = 0) -> bytes:
    build_version = struct.pack(
        "<IIIIII",
        loaded._MACHO_LC_BUILD_VERSION,
        24,
        loaded._MACHO_PLATFORM_MACOS,
        13 << 16,
        (26 << 16) | (2 << 8),
        0,
    )
    header = struct.pack(
        "<IiiIIIII",
        loaded._MACHO_MAGIC_64_LE,
        loaded._MACHO_CPU_TYPE_ARM64,
        cpu_subtype,
        2,
        1,
        len(build_version),
        0,
        0,
    )
    return header + build_version


def _write_synthetic_wheel(
    path: Path,
    *,
    payloads: dict[str, bytes],
    recorded_payloads: tuple[str, ...],
) -> None:
    record_rows = []
    for name in recorded_payloads:
        value = payloads[name]
        digest = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=")
        record_rows.append(f"{name},sha256={digest.decode()},{len(value)}\n")
    values = {
        **payloads,
        "adhelm-0.1.0.dist-info/entry_points.txt": (
            b"[console_scripts]\nadhelm = adhelm.cli:main\n"
        ),
        "adhelm-0.1.0.dist-info/METADATA": (
            b"Metadata-Version: 2.4\nName: adhelm\nVersion: 0.1.0\n"
        ),
        "adhelm-0.1.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        "adhelm-0.1.0.dist-info/RECORD": "".join(record_rows).encode("ascii"),
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in values.items():
            archive.writestr(name, value)


class _FakePythonToolchain:
    def __init__(self, *, version: tuple[int, int, int], implementation: str = "cpython") -> None:
        self.implementation = SimpleNamespace(name=implementation)
        self.version_info = (*version, "final", 0)
        self.executable = "/private/tmp/adhelm-fake-pinned-python"

    def __getattr__(self, name: str) -> Any:
        return getattr(sys, name)


def _fake_python_toolchain(
    *,
    version: tuple[int, int, int],
    implementation: str = "cpython",
) -> _FakePythonToolchain:
    return _FakePythonToolchain(version=version, implementation=implementation)


def _fake_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo: Path,
    descriptor: bytes,
    inspector_output: bytes | None = None,
) -> list[tuple[str, ...]]:
    loaded = builder()
    calls: list[tuple[str, ...]] = []

    # Fake-build tests model the reviewed SDK identity but must not read the
    # runner's Xcode installation. The production artifact builder retains
    # those deliberate filesystem checks.
    fake_sdk = loaded.PinnedSDKProfile(
        declared_path=loaded._PINNED_MACOS_SDK_DECLARED_PATH,
        canonical_path=loaded._PINNED_MACOS_SDK_CANONICAL_PATH,
        version=loaded._PINNED_MACOS_SDK_VERSION,
        build_version=loaded._PINNED_MACOS_SDK_BUILD_VERSION,
    )
    # Fake-build tests model the reviewed Python identity but must not depend on
    # the runner's interpreter patch release. The production builder retains
    # its exact CPython version gate.
    fake_python = _fake_python_toolchain(version=loaded._PINNED_PYTHON_VERSION)

    def fake_require_pinned_sdk(environment: object, workspace: object) -> object:
        assert isinstance(environment, dict)
        assert isinstance(workspace, Path)
        return fake_sdk

    def fake_run_tool(
        arguments: object,
        *,
        cwd: Path,
        environment: object,
        inspector: Path | None = None,
        swift_context: tuple[Path, Path] | None = None,
        archive_output: Path | None = None,
        python_build_context: tuple[Path, Path] | None = None,
        profile: object = None,
        timeout: int = 600,
    ) -> subprocess.CompletedProcess[bytes]:
        assert isinstance(arguments, list)
        assert isinstance(environment, dict)
        assert loaded._allowed_tool(
            arguments,
            inspector=inspector,
            swift_context=swift_context,
            archive_output=archive_output,
            python_build_context=python_build_context,
            profile=(loaded._ArtifactBuildProfile.V1 if profile is None else profile),
        )
        assert timeout == 600
        calls.append(tuple(arguments))
        if arguments[0] == loaded._UV:
            if arguments == [loaded._UV, "--version"]:
                return subprocess.CompletedProcess(
                    arguments, 0, stdout=(loaded._PINNED_UV_VERSION + "\n").encode(), stderr=b""
                )
            assert python_build_context is not None
            snapshot, workspace = python_build_context
            wheel = workspace / "python-wheel" / loaded._WHEEL_NAME
            payloads: dict[str, bytes] = {}
            for source, prefix in (
                (snapshot / "src/adhelm", "adhelm/"),
                (snapshot / "skills", "adhelm/bundled_skills/"),
            ):
                for path in sorted(source.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        payloads[prefix + path.relative_to(source).as_posix()] = path.read_bytes()
            record_rows = []
            for name, value in sorted(payloads.items()):
                digest = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=")
                record_rows.append(f"{name},sha256={digest.decode()},{len(value)}\n")
            with zipfile.ZipFile(wheel, "w") as archive:
                values = {
                    **payloads,
                    "adhelm-0.1.0.dist-info/entry_points.txt": (
                        b"[console_scripts]\nadhelm = adhelm.cli:main\n"
                    ),
                    "adhelm-0.1.0.dist-info/METADATA": (
                        b"Metadata-Version: 2.4\nName: adhelm\nVersion: 0.1.0\n"
                    ),
                    "adhelm-0.1.0.dist-info/WHEEL": (
                        b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
                    ),
                    "adhelm-0.1.0.dist-info/RECORD": ("".join(record_rows)).encode("ascii"),
                }
                for name, value in values.items():
                    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    archive.writestr(info, value)
            return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")
        if arguments[0] == loaded._GIT:
            if arguments[1] == "rev-parse":
                return subprocess.CompletedProcess(
                    arguments, 0, stdout=f"{_COMMIT}\n".encode(), stderr=b""
                )
            if arguments[1] == "show":
                return subprocess.CompletedProcess(
                    arguments, 0, stdout=f"{_EPOCH}\n".encode(), stderr=b""
                )
            assert arguments[1] == "archive" and archive_output is not None
            _archive_tree(repo, archive_output)
            return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")
        if arguments == [loaded._SWIFT, "--version"]:
            version = (
                f"{loaded._PINNED_SWIFT_DRIVER} {loaded._PINNED_SWIFT_LANGUAGE}\n"
                f"{loaded._PINNED_SWIFT_TARGET}\n"
            ).encode()
            return subprocess.CompletedProcess(arguments, 0, stdout=version, stderr=b"")
        if arguments[0] == loaded._XCRUN:
            outputs = {
                "--show-sdk-version": loaded._PINNED_MACOS_SDK_VERSION,
                "--show-sdk-build-version": loaded._PINNED_MACOS_SDK_BUILD_VERSION,
                "--show-sdk-path": str(loaded._PINNED_MACOS_SDK_DECLARED_PATH),
            }
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=f"{outputs[arguments[-1]]}\n".encode(),
                stderr=b"",
            )
        if arguments[0] == loaded._SWIFT:
            assert swift_context is not None
            _, workspace = swift_context
            binaries = workspace / "swift-build/release"
            binaries.mkdir(parents=True, exist_ok=True)
            products = (
                loaded._HOST_PRODUCT,
                loaded._CLIENT_PRODUCT,
                loaded._INSPECTOR_PRODUCT,
            )
            if profile is loaded._ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2:
                products += (loaded._META_CAMPAIGN_PAUSE_CLIENT_PRODUCT,)
            for product in products:
                binary = binaries / product
                binary.write_bytes(
                    _macho_host_bytes(loaded)
                    if product != loaded._INSPECTOR_PRODUCT
                    else product.encode()
                )
                binary.chmod(0o755)
            if arguments[-1] == "--show-bin-path":
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout=f"{binaries}\n".encode(),
                    stderr=b"",
                )
            return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")
        assert inspector is not None
        assert arguments == [str(inspector)]
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=descriptor if inspector_output is None else inspector_output,
            stderr=b"",
        )

    monkeypatch.setattr(loaded, "_require_pinned_sdk", fake_require_pinned_sdk)
    monkeypatch.setattr(loaded, "sys", fake_python)
    monkeypatch.setattr(loaded, "_run_tool", fake_run_tool)
    return calls


def test_python_toolchain_gate_rejects_wrong_patch_before_tool_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = builder()
    calls: list[tuple[str, ...]] = []

    def unexpected_run_tool(
        arguments: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        assert isinstance(arguments, list)
        calls.append(tuple(arguments))
        pytest.fail("Python toolchain refusal must precede tool execution")

    monkeypatch.setattr(
        loaded,
        "sys",
        _fake_python_toolchain(version=(3, 12, 10)),
    )
    monkeypatch.setattr(loaded, "_run_tool", unexpected_run_tool)

    with pytest.raises(
        loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_PYTHON_TOOLCHAIN_MISMATCH"
    ):
        loaded._build_python_wheel(
            tmp_path / "source",
            tmp_path / "workspace",
            environment={},
            profile=loaded._ArtifactBuildProfile.V1,
            generated_python_sha256="a" * 64,
            generated_meta_python_sha256=None,
            generated_source_hashes=(),
        )

    assert calls == []


def test_python_toolchain_gate_accepts_exact_version_before_uv_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = builder()
    calls: list[tuple[str, ...]] = []

    def fake_run_tool(arguments: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert isinstance(arguments, list)
        calls.append(tuple(arguments))
        return subprocess.CompletedProcess(arguments, 0, stdout=b"unexpected\n", stderr=b"")

    monkeypatch.setattr(
        loaded,
        "sys",
        _fake_python_toolchain(version=loaded._PINNED_PYTHON_VERSION),
    )
    monkeypatch.setattr(loaded, "_run_tool", fake_run_tool)
    monkeypatch.setattr(loaded, "_require_unchanged_generated_sources", lambda _hashes: None)

    with pytest.raises(
        loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_PYTHON_TOOLCHAIN_MISMATCH"
    ):
        loaded._build_python_wheel(
            tmp_path / "source",
            tmp_path / "workspace",
            environment={},
            profile=loaded._ArtifactBuildProfile.V1,
            generated_python_sha256="a" * 64,
            generated_meta_python_sha256=None,
            generated_source_hashes=(),
        )

    assert calls == [(loaded._UV, "--version")]


def _synthetic_pinned_sdk_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_declared_sdk: bool = True,
) -> tuple[ModuleType, Path, Path]:
    loaded = builder()
    sdk_directory = (
        tmp_path / "Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs"
    )
    canonical = sdk_directory / "MacOSX.sdk"
    canonical.mkdir(parents=True)
    declared = sdk_directory / "MacOSX26.2.sdk"
    if include_declared_sdk:
        declared.symlink_to(canonical.name)
    monkeypatch.setattr(loaded, "_PINNED_MACOS_SDK_DECLARED_PATH", declared)
    monkeypatch.setattr(loaded, "_PINNED_MACOS_SDK_CANONICAL_PATH", canonical)
    return loaded, declared, canonical


def _fake_pinned_sdk_probes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    loaded: ModuleType,
    workspace: Path,
) -> list[tuple[str, ...]]:
    outputs = {
        "--show-sdk-version": loaded._PINNED_MACOS_SDK_VERSION,
        "--show-sdk-build-version": loaded._PINNED_MACOS_SDK_BUILD_VERSION,
        "--show-sdk-path": str(loaded._PINNED_MACOS_SDK_DECLARED_PATH),
    }
    calls: list[tuple[str, ...]] = []

    def fake_run_tool(
        arguments: object, *, cwd: Path, environment: object
    ) -> subprocess.CompletedProcess[bytes]:
        assert isinstance(arguments, list)
        assert cwd == workspace
        assert environment == {}
        assert arguments[:3] == [loaded._XCRUN, "--sdk", "macosx"]
        calls.append(tuple(arguments))
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=f"{outputs[arguments[-1]]}\n".encode(),
            stderr=b"",
        )

    monkeypatch.setattr(loaded, "_run_tool", fake_run_tool)
    return calls


def test_pinned_sdk_verifier_accepts_exact_root_owned_synthetic_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, declared, canonical = _synthetic_pinned_sdk_paths(tmp_path, monkeypatch)
    calls = _fake_pinned_sdk_probes(monkeypatch, loaded=loaded, workspace=tmp_path)
    original_lstat = Path.lstat

    def root_owned_lstat(path: Path) -> object:
        value = original_lstat(path)
        if path not in (declared, canonical):
            return value
        return loaded.os.stat_result(
            (
                value.st_mode,
                value.st_ino,
                value.st_dev,
                value.st_nlink,
                0,
                value.st_gid,
                value.st_size,
                value.st_atime,
                value.st_mtime,
                value.st_ctime,
            )
        )

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)
    sdk = loaded._require_pinned_sdk({}, tmp_path)

    assert sdk.to_data() == {
        "declared_path": str(declared),
        "canonical_path": str(canonical),
        "version": "26.2",
        "build_version": "25C57",
    }
    assert calls == [
        (loaded._XCRUN, "--sdk", "macosx", "--show-sdk-version"),
        (loaded._XCRUN, "--sdk", "macosx", "--show-sdk-build-version"),
        (loaded._XCRUN, "--sdk", "macosx", "--show-sdk-path"),
    ]


def test_pinned_sdk_verifier_rejects_missing_declared_sdk_after_matching_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded, _declared, _canonical = _synthetic_pinned_sdk_paths(
        tmp_path,
        monkeypatch,
        include_declared_sdk=False,
    )
    calls = _fake_pinned_sdk_probes(monkeypatch, loaded=loaded, workspace=tmp_path)

    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_TOOLCHAIN_MISMATCH"):
        loaded._require_pinned_sdk({}, tmp_path)

    assert calls == [
        (loaded._XCRUN, "--sdk", "macosx", "--show-sdk-version"),
        (loaded._XCRUN, "--sdk", "macosx", "--show-sdk-build-version"),
        (loaded._XCRUN, "--sdk", "macosx", "--show-sdk-path"),
    ]


def _build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    inspector_output: bytes | None = None,
    access_group: str | None = None,
    profile: str = "V1",
) -> tuple[Any, list[tuple[str, ...]], Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repository"
    repo.mkdir()
    _write_source_tree(repo)
    specification = _write_spec(tmp_path, signing_key_access_group=access_group)
    expected = _descriptor_bytes(specification)
    calls = _fake_runner(
        monkeypatch,
        repo=repo,
        descriptor=expected,
        inspector_output=inspector_output,
    )
    output = tmp_path / "artifact-output"
    result = builder().build_native_approval_artifact(
        repo=repo,
        revision="HEAD",
        specification=specification,
        output=output,
        source_date_epoch=_EPOCH,
        profile=profile,
    )
    return result, calls, repo, specification, output


def test_fake_build_stages_closed_app_manifest_and_launch_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, calls, repo, specification, output = _build(tmp_path, monkeypatch)
    loaded = builder()
    app = output / loaded._APP_NAME
    executable = app / "Contents/MacOS" / loaded._HOST_PRODUCT
    launch_agent = app / "Contents/Library/LaunchAgents/com.aptsonic.adhelm.approvalhost.plist"

    assert result.output_directory == output
    assert result.archive == output / loaded._ARCHIVE_NAME
    assert result.to_data() == {
        "schema_version": 1,
        "artifact_status": "UNSIGNED_UNVERIFIED",
        "archive_name": loaded._ARCHIVE_NAME,
        "source_commit": _COMMIT,
        "descriptor_digest": result.descriptor_digest,
        "metadata_digest": result.metadata_digest,
        "protocol_configuration_digest": result.protocol_configuration_digest,
        "archive_sha256": result.archive_sha256,
        "developer_identity_signed": False,
        "developer_identity_signing_status": "NOT_VERIFIED",
        "compiler_signature_status": "NOT_INSPECTED",
        "notarized": False,
        "registered": False,
        "registration_status": "NOT_VERIFIED",
        "identity_authorized": False,
        "identity_authorization_status": "NOT_VERIFIED",
        "host_started": False,
        "client_started": False,
        "scope": "UNSIGNED_DEVELOPMENT_ARTIFACT_NOT_INSTALLED_OR_APPROVAL_AUTHORITY",
    }
    assert executable.read_bytes() == _macho_host_bytes(loaded)
    assert stat.S_IMODE(executable.stat().st_mode) == 0o755
    client = app / "Contents/MacOS" / loaded._CLIENT_PRODUCT
    assert client.read_bytes() == _macho_host_bytes(loaded)
    assert stat.S_IMODE(client.stat().st_mode) == 0o755
    assert not (app / "Contents/MacOS" / loaded._INSPECTOR_PRODUCT).exists()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == loaded._HOST_PRODUCT
    assert info["CFBundleIdentifier"] == "com.aptsonic.adhelm.host"
    assert info["CFBundleShortVersionString"] == "0.1.0"
    assert info["CFBundleVersion"] == "1"
    launch = plistlib.loads(launch_agent.read_bytes())
    assert launch == {
        "BundleProgram": f"Contents/MacOS/{loaded._HOST_PRODUCT}",
        "Label": "com.aptsonic.adhelm.approvalhost",
        "MachServices": {"com.aptsonic.adhelm.approvalhost": True},
    }
    assert "RunAtLoad" not in launch and "KeepAlive" not in launch
    assert not (output / "AdHelmApprovalHost.entitlements").exists()
    manifest = json.loads((output / loaded._MANIFEST_NAME).read_text(encoding="utf-8"))
    assert "meta_campaign_pause_approval_bridge_v2" not in manifest
    assert manifest["source"]["commit"] == _COMMIT
    assert manifest["source"]["source_date_epoch"] == _EPOCH
    assert manifest["source"]["development_version"] == "0.1.0"
    assert (
        manifest["metadata"]["native_descriptor_sha256"]
        == hashlib.sha256(_descriptor_bytes(specification)).hexdigest()
    )
    assert manifest["developer_identity_signed"] is False
    assert manifest["developer_identity_signing_status"] == "NOT_VERIFIED"
    assert manifest["compiler_signature_status"] == "NOT_INSPECTED"
    assert manifest["toolchain"]["sdk"] == {
        "declared_path": str(loaded._PINNED_MACOS_SDK_DECLARED_PATH),
        "canonical_path": str(loaded._PINNED_MACOS_SDK_CANONICAL_PATH),
        "version": "26.2",
        "build_version": "25C57",
    }
    assert manifest["toolchain"]["compiler_default_target"] == "arm64-apple-macosx26.0"
    assert manifest["toolchain"]["package_deployment_target"] == "macos-13.0"
    assert manifest["toolchain"]["effective_binary_target"] == {
        "architecture": "arm64",
        "platform": "macos",
        "minimum_os_version": "13.0.0",
        "sdk_version": "26.2.0",
    }
    assert manifest["registered"] is False and manifest["registration_status"] == "NOT_VERIFIED"
    assert manifest["identity_authorized"] is False
    assert manifest["identity_authorization_status"] == "NOT_VERIFIED"
    assert manifest["host_started"] is False
    assert manifest["client_started"] is False
    assert manifest["python_build"]["build_network"] == "OFFLINE"
    assert (
        manifest["python_build"]["inspection"]
        == "BOUNDED_ZIP_READ_ONLY_NO_CONFIGURED_MODULE_IMPORT"
    )
    assert loaded._WHEEL_NAME in manifest["components"]
    with zipfile.ZipFile(output / loaded._WHEEL_NAME) as wheel:
        assert (
            hashlib.sha256(wheel.read(loaded._GENERATED_PYTHON_WHEEL_MEMBER)).hexdigest()
            == manifest["metadata"]["generated_python_source_sha256"]
        )
    assert (
        manifest["toolchain"]["effective_client_binary_target"]
        == manifest["toolchain"]["effective_binary_target"]
    )
    assert (
        manifest["components"][str(client.relative_to(output))]
        == hashlib.sha256(client.read_bytes()).hexdigest()
    )
    assert not any(command[0].endswith("/" + loaded._CLIENT_PRODUCT) for command in calls)
    with tarfile.open(result.archive, "r:") as bundle:
        assert bundle.extractfile(str(client.relative_to(output))).read() == client.read_bytes()
    assert not (output / ".private-build").exists()
    assert (repo / _GENERATED_RELATIVE).read_text(encoding="utf-8") == (
        "package enum GeneratedApprovalHostBuildMetadata {}\n"
    )
    assert all(
        command[0] in {loaded._GIT, loaded._SWIFT, loaded._XCRUN, loaded._UV} or len(command) == 1
        for command in calls
    )
    assert not any(command == (str(executable),) for command in calls)
    assert not any(
        command[-2:] == ("--product", loaded._META_CAMPAIGN_PAUSE_CLIENT_PRODUCT)
        for command in calls
    )


def test_explicit_meta_profile_stages_only_the_fixed_helper_and_binding_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = builder()
    result, calls, _repo, _specification, output = _build(
        tmp_path,
        monkeypatch,
        profile=loaded._ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2.value,
    )
    helper = (
        output
        / loaded._APP_NAME
        / "Contents"
        / "MacOS"
        / loaded._META_CAMPAIGN_PAUSE_CLIENT_PRODUCT
    )
    assert helper.read_bytes() == _macho_host_bytes(loaded)
    assert stat.S_IMODE(helper.stat().st_mode) == 0o755
    manifest = json.loads((output / loaded._MANIFEST_NAME).read_text(encoding="utf-8"))
    evidence = manifest["meta_campaign_pause_approval_bridge_v2"]
    assert evidence == {
        "profile": loaded._ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2.value,
        "scope": "EXPLICIT_BUILD_PROFILE_STATIC_BINDING_NOT_INSTALLED_OR_APPROVAL_AUTHORITY",
        "generated_python_binding_sha256": evidence["generated_python_binding_sha256"],
        "generated_native_route_binding_sha256": evidence["generated_native_route_binding_sha256"],
        "metadata_digest": result.metadata_digest,
        "protocol_configuration_digest": result.protocol_configuration_digest,
        "helper_relative_path": str(helper.relative_to(output)),
        "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "helper_binary_target": {
            "architecture": "arm64",
            "platform": "macos",
            "minimum_os_version": "13.0.0",
            "sdk_version": "26.2.0",
        },
    }
    assert set(evidence) == {
        "profile",
        "scope",
        "generated_python_binding_sha256",
        "generated_native_route_binding_sha256",
        "metadata_digest",
        "protocol_configuration_digest",
        "helper_relative_path",
        "helper_sha256",
        "helper_binary_target",
    }
    assert manifest["components"][str(helper.relative_to(output))] == evidence["helper_sha256"]
    with zipfile.ZipFile(output / loaded._WHEEL_NAME) as wheel:
        generated = wheel.read(loaded._GENERATED_META_PYTHON_WHEEL_MEMBER)
        assert hashlib.sha256(generated).hexdigest() == evidence["generated_python_binding_sha256"]
        assert loaded._META_COMPILED_PYTHON_WHEEL_MEMBER in wheel.namelist()
        records = wheel.read("adhelm-0.1.0.dist-info/RECORD").decode()
        assert f"{loaded._GENERATED_META_PYTHON_WHEEL_MEMBER},sha256=" in records
    with tarfile.open(result.archive, "r:") as archive:
        member = archive.getmember(str(helper.relative_to(output)))
    assert member.isfile() and member.mode == 0o755
    meta_builds = [
        command
        for command in calls
        if command[-2:] == ("--product", loaded._META_CAMPAIGN_PAUSE_CLIENT_PRODUCT)
    ]
    assert len(meta_builds) == 1
    assert not any(command == (str(helper),) for command in calls)
    assert (
        result.to_data()["scope"]
        == "UNSIGNED_DEVELOPMENT_ARTIFACT_NOT_INSTALLED_OR_APPROVAL_AUTHORITY"
    )


@pytest.mark.parametrize("case", ["missing-record", "changed-source", "missing-compiled"])
def test_meta_profile_wheel_requires_exact_generated_payload_record_and_compiled_module(
    tmp_path: Path, case: str
) -> None:
    loaded = builder()
    v1 = b"generated-v1\n"
    meta = b"generated-meta\n"
    payloads = {
        loaded._GENERATED_PYTHON_WHEEL_MEMBER: v1,
        loaded._GENERATED_META_PYTHON_WHEEL_MEMBER: meta,
        loaded._META_COMPILED_PYTHON_WHEEL_MEMBER: b"compiled-meta\n",
        "adhelm/_installed_native_approval_bridge.py": b"v1-private\n",
        "adhelm/cli.py": b"def main() -> int: return 0\n",
        "adhelm/py.typed": b"",
    }
    expected_payloads = {
        name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()
    }
    recorded = tuple(payloads)
    if case == "missing-record":
        recorded = tuple(
            name for name in recorded if name != loaded._GENERATED_META_PYTHON_WHEEL_MEMBER
        )
    elif case == "changed-source":
        payloads[loaded._GENERATED_META_PYTHON_WHEEL_MEMBER] = b"changed-meta\n"
    elif case == "missing-compiled":
        payloads.pop(loaded._META_COMPILED_PYTHON_WHEEL_MEMBER)
        expected_payloads.pop(loaded._META_COMPILED_PYTHON_WHEEL_MEMBER)
        recorded = tuple(
            name for name in recorded if name != loaded._META_COMPILED_PYTHON_WHEEL_MEMBER
        )
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(case)
    wheel = tmp_path / loaded._WHEEL_NAME
    _write_synthetic_wheel(wheel, payloads=payloads, recorded_payloads=recorded)
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_PYTHON_WHEEL_INVALID"):
        loaded._inspect_python_wheel(
            wheel,
            profile=loaded._ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2,
            generated_python_sha256=hashlib.sha256(v1).hexdigest(),
            generated_meta_python_sha256=hashlib.sha256(meta).hexdigest(),
            expected_payloads=expected_payloads,
        )


@pytest.mark.parametrize("invalid", ["meta", "META_CAMPAIGN_PAUSE_V3", True, None])
def test_invalid_profile_fails_before_contract_source_output_or_tool_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: object
) -> None:
    loaded = builder()
    calls: list[tuple[str, ...]] = []

    def no_tool(*arguments: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(tuple(str(argument) for argument in arguments))
        raise AssertionError("profile validation must happen before tools")

    monkeypatch.setattr(loaded, "_run_tool", no_tool)
    output = tmp_path / "artifact-output"
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_PROFILE_INVALID"):
        loaded.build_native_approval_artifact(
            repo=tmp_path / "not-read",
            revision="HEAD",
            specification=tmp_path / "not-read.json",
            output=output,
            profile=invalid,
        )
    assert calls == []
    assert not output.exists()


@pytest.mark.parametrize(
    ("profile_name", "relative"),
    (
        ("V1", _GENERATED_META_PYTHON_RELATIVE),
        ("META_CAMPAIGN_PAUSE_V2", _GENERATED_META_ROUTE_RELATIVE),
    ),
)
def test_profile_generated_source_mutation_after_swift_build_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_name: str,
    relative: Path,
) -> None:
    loaded = builder()
    repo = tmp_path / "repository"
    repo.mkdir()
    _write_source_tree(repo)
    specification = _write_spec(tmp_path)
    _fake_runner(monkeypatch, repo=repo, descriptor=_descriptor_bytes(specification))
    fake_run_tool = loaded._run_tool

    def changing_run_tool(
        arguments: object,
        *,
        cwd: Path,
        environment: object,
        inspector: Path | None = None,
        swift_context: tuple[Path, Path] | None = None,
        archive_output: Path | None = None,
        python_build_context: tuple[Path, Path] | None = None,
        profile: object = None,
        timeout: int = 600,
    ) -> subprocess.CompletedProcess[bytes]:
        result = fake_run_tool(
            arguments,
            cwd=cwd,
            environment=environment,
            inspector=inspector,
            swift_context=swift_context,
            archive_output=archive_output,
            python_build_context=python_build_context,
            profile=profile,
            timeout=timeout,
        )
        if (
            isinstance(arguments, list)
            and arguments[0] == loaded._SWIFT
            and arguments[-2:] == ["--product", loaded._HOST_PRODUCT]
            and swift_context is not None
        ):
            package, _ = swift_context
            (package.parents[1] / relative).write_text("tampered\n", encoding="utf-8")
        return result

    monkeypatch.setattr(loaded, "_run_tool", changing_run_tool)
    output = tmp_path / "artifact-output"
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_METADATA_INVALID"):
        loaded.build_native_approval_artifact(
            repo=repo,
            revision="HEAD",
            specification=specification,
            output=output,
            source_date_epoch=_EPOCH,
            profile=profile_name,
        )
    assert not output.exists()


def test_v1_rejects_archived_nondefault_meta_source_before_swift_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = builder()
    repo = tmp_path / "repository"
    repo.mkdir()
    _write_source_tree(repo)
    (repo / _GENERATED_META_ROUTE_RELATIVE).write_text("configured route\n", encoding="utf-8")
    specification = _write_spec(tmp_path)
    calls = _fake_runner(monkeypatch, repo=repo, descriptor=_descriptor_bytes(specification))
    output = tmp_path / "artifact-output"
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_METADATA_INVALID"):
        loaded.build_native_approval_artifact(
            repo=repo,
            revision="HEAD",
            specification=specification,
            output=output,
            source_date_epoch=_EPOCH,
        )
    assert not output.exists()
    assert not any(command[0] == loaded._SWIFT for command in calls)


def test_build_adds_template_only_entitlement_when_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, _, output = _build(
        tmp_path,
        monkeypatch,
        access_group="ABCDE12345.com.aptsonic.adhelm.host",
    )
    entitlement = plistlib.loads((output / "AdHelmApprovalHost.entitlements").read_bytes())
    assert entitlement == {"keychain-access-groups": ["ABCDE12345.com.aptsonic.adhelm.host"]}
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["keychain_access_group_template_only"] is True
    assert "AdHelmApprovalHost.entitlements" in manifest["components"]


def test_build_requires_exact_inspector_descriptor_before_output_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(builder().ArtifactBuildError, match="NATIVE_ARTIFACT_INSPECTOR_MISMATCH"):
        _build(tmp_path, monkeypatch, inspector_output=b"{}")
    assert not (tmp_path / "artifact-output").exists()


def test_builder_refuses_an_archived_generator_that_differs_from_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repository"
    repo.mkdir()
    _write_source_tree(repo)
    (repo / "scripts/native_approval_artifact_contract.py").write_text(
        "# different archived generator\n", encoding="utf-8"
    )
    specification = _write_spec(tmp_path)
    _fake_runner(monkeypatch, repo=repo, descriptor=_descriptor_bytes(specification))
    with pytest.raises(
        builder().ArtifactBuildError, match="NATIVE_ARTIFACT_SOURCE_PROVENANCE_MISMATCH"
    ):
        builder().build_native_approval_artifact(
            repo=repo,
            revision="HEAD",
            specification=specification,
            output=tmp_path / "artifact-output",
            source_date_epoch=_EPOCH,
        )
    assert not (tmp_path / "artifact-output").exists()


def test_builder_refuses_generated_metadata_changed_during_a_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = builder()
    repo = tmp_path / "repository"
    repo.mkdir()
    _write_source_tree(repo)
    specification = _write_spec(tmp_path)
    original_run_tool = loaded._run_tool
    _fake_runner(monkeypatch, repo=repo, descriptor=_descriptor_bytes(specification))
    fake_run_tool = loaded._run_tool

    def changing_run_tool(
        arguments: object,
        *,
        cwd: Path,
        environment: object,
        inspector: Path | None = None,
        swift_context: tuple[Path, Path] | None = None,
        archive_output: Path | None = None,
        python_build_context: tuple[Path, Path] | None = None,
        profile: object = None,
        timeout: int = 600,
    ) -> subprocess.CompletedProcess[bytes]:
        result = fake_run_tool(
            arguments,
            cwd=cwd,
            environment=environment,
            inspector=inspector,
            swift_context=swift_context,
            archive_output=archive_output,
            python_build_context=python_build_context,
            profile=profile,
            timeout=timeout,
        )
        if (
            isinstance(arguments, list)
            and arguments[0] == loaded._SWIFT
            and arguments[-1] == loaded._HOST_PRODUCT
            and swift_context is not None
        ):
            package, _ = swift_context
            generated = package.parents[1] / _GENERATED_RELATIVE
            generated.write_text("package enum Tampered {}\n", encoding="utf-8")
        return result

    monkeypatch.setattr(loaded, "_run_tool", changing_run_tool)
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_METADATA_INVALID"):
        loaded.build_native_approval_artifact(
            repo=repo,
            revision="HEAD",
            specification=specification,
            output=tmp_path / "artifact-output",
            source_date_epoch=_EPOCH,
        )
    assert not (tmp_path / "artifact-output").exists()
    monkeypatch.setattr(loaded, "_run_tool", original_run_tool)


def test_builder_produces_byte_identical_tar_for_same_fake_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _, _, _, output_one = _build(tmp_path / "one", monkeypatch)
    second, _, _, _, output_two = _build(tmp_path / "two", monkeypatch)
    assert first.archive_sha256 == second.archive_sha256
    assert first.archive.read_bytes() == second.archive.read_bytes()
    with tarfile.open(first.archive, "r:") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == sorted(member.name for member in members)
    assert all(member.mtime == _EPOCH for member in members)
    assert all(member.uid == 0 and member.gid == 0 for member in members)
    assert output_one != output_two


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "traversal", "noncanonical"])
def test_source_extraction_rejects_links_devices_and_traversal(tmp_path: Path, kind: str) -> None:
    archive = tmp_path / f"{kind}.tar"
    with tarfile.open(archive, "w") as bundle:
        entry = tarfile.TarInfo("source")
        if kind == "symlink":
            entry.type = tarfile.SYMTYPE
            entry.linkname = "elsewhere"
        elif kind == "hardlink":
            entry.type = tarfile.LNKTYPE
            entry.linkname = "elsewhere"
        elif kind == "fifo":
            entry.type = tarfile.FIFOTYPE
        elif kind == "traversal":
            entry.name = "../outside"
            entry.size = 1
            bundle.addfile(entry, io.BytesIO(b"x"))
            entry = None
        else:
            entry.name = "a//b"
            entry.size = 1
            bundle.addfile(entry, io.BytesIO(b"x"))
            entry = None
        if entry is not None:
            bundle.addfile(entry)
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(
        builder().ArtifactBuildError, match="NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID"
    ):
        builder().extract_exact_source(archive, destination, epoch=_EPOCH)


def test_tool_allowlist_is_context_bound_and_source_epoch_is_exact(tmp_path: Path) -> None:
    loaded = builder()
    package = tmp_path / "package"
    workspace = tmp_path / "workspace"
    package.mkdir()
    workspace.mkdir()
    allowed = [
        loaded._SWIFT,
        "build",
        *loaded._swift_options(package, workspace),
        "--product",
        loaded._HOST_PRODUCT,
    ]
    assert loaded._allowed_tool(allowed, swift_context=(package, workspace))
    options = loaded._swift_options(package, workspace)
    assert ["-Xswiftc", "-file-prefix-map", "-Xswiftc"] == options[
        options.index("-file-prefix-map") - 1 : options.index("-file-prefix-map") + 2
    ]
    assert f"{workspace}={loaded._REPRODUCIBLE_BUILD_PREFIX}" in options
    assert "-no_uuid" not in options
    assert options[-2:] == ["-Xlinker", "-S"]
    assert not loaded._allowed_tool(
        [loaded._SWIFT, "build", "--package-path", "/private/tmp/untrusted"]
    )
    assert not loaded._allowed_tool(
        [loaded._GIT, "archive", "--format=tar", "--output=/tmp/out.tar", _COMMIT]
    )
    assert loaded._allowed_tool(
        [loaded._GIT, "archive", "--format=tar", "--output=/tmp/out.tar", _COMMIT],
        archive_output=Path("/tmp/out.tar"),
    )
    assert loaded._allowed_tool([loaded._XCRUN, "--sdk", "macosx", "--show-sdk-version"])
    assert not loaded._allowed_tool(
        [loaded._XCRUN, "--sdk", "iphonesimulator", "--show-sdk-version"]
    )
    assert not loaded._allowed_tool([loaded._XCRUN, "--find", "swift"])
    source = loaded.ResolvedSource(commit=_COMMIT, epoch=_EPOCH)
    assert loaded._checked_epoch(None, source) == _EPOCH
    for invalid in (True, 1.0, _EPOCH + 1):
        with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_EPOCH_INVALID"):
            loaded._checked_epoch(invalid, source)


def test_real_path_input_and_version_mapping_are_accepted(tmp_path: Path) -> None:
    loaded = builder()
    value = tmp_path / "regular.json"
    value.write_bytes(b"{}")
    assert loaded._read_regular_file(value, maximum_bytes=2) == b"{}"
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    assert loaded._development_bundle_versions(snapshot) == ("0.1.0", "1")


def test_archive_publish_refuses_existing_final_symlink(tmp_path: Path) -> None:
    loaded = builder()
    output = tmp_path / "output"
    output.mkdir()
    app = output / loaded._APP_NAME / "Contents"
    (app / "MacOS").mkdir(parents=True)
    (app / "Library/LaunchAgents").mkdir(parents=True)
    (app / "Info.plist").write_bytes(b"info")
    executable = app / "MacOS" / loaded._HOST_PRODUCT
    executable.write_bytes(b"host")
    executable.chmod(0o755)
    (app / "Library/LaunchAgents/service.plist").write_bytes(b"plist")
    (output / "manifest.json").write_bytes(b"{}")
    archive = output / loaded._ARCHIVE_NAME
    target = tmp_path / "target"
    target.write_bytes(b"unchanged")
    archive.symlink_to(target)
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_OUTPUT_INVALID"):
        loaded.create_deterministic_archive(output, archive=archive, epoch=_EPOCH)
    assert target.read_bytes() == b"unchanged"


def test_result_serializer_rejects_direct_or_changed_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _, _, _ = _build(tmp_path, monkeypatch)
    with pytest.raises(builder().ArtifactBuildError, match="NATIVE_ARTIFACT_RESULT_INVALID"):
        replace(result, source_commit="private-sentinel")
    forged = object.__new__(builder().ArtifactBuildResult)
    with pytest.raises(builder().ArtifactBuildError, match="NATIVE_ARTIFACT_RESULT_INVALID"):
        forged.to_data()


def test_result_serializer_refuses_a_final_path_replaced_with_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _, _, _ = _build(tmp_path, monkeypatch)
    loaded = builder()
    target = tmp_path / "external-archive"
    target.write_bytes(result.archive.read_bytes())
    original_open = loaded.os.open
    swapped = False

    def swap_final_path(path: object, flags: int, *args: object) -> int:
        nonlocal swapped
        if (
            not swapped
            and Path(path) == result.archive
            and flags & (loaded.os.O_WRONLY | loaded.os.O_RDWR) == 0
        ):
            swapped = True
            result.archive.unlink()
            result.archive.symlink_to(target)
        return original_open(path, flags, *args)

    monkeypatch.setattr(loaded.os, "open", swap_final_path)
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_RESULT_INVALID"):
        result.to_data()
    assert result.archive.is_symlink()
    assert target.read_bytes()


def test_macho_reader_rejects_arm64e_even_with_a_valid_build_version(tmp_path: Path) -> None:
    loaded = builder()
    host = tmp_path / loaded._HOST_PRODUCT
    host.write_bytes(_macho_host_bytes(loaded, cpu_subtype=2))
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_HOST_BINARY_INVALID"):
        loaded._read_macho_build_target(host)
