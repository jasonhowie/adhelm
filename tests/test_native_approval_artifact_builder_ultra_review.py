"""Independent fake-only adversarial review of the M30 artifact builder."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import shutil
import struct
import sys
import tarfile
import zipfile
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest


@cache
def _builder() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/build_native_approval_artifact.py"
    spec = importlib.util.spec_from_file_location("native_artifact_builder_ultra_target", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def _macho(*, cpu_subtype: int, minimum: int = 13 << 16, sdk: int = (26 << 16) | (2 << 8)) -> bytes:
    loaded = _builder()
    command = struct.pack(
        "<IIIIII",
        loaded._MACHO_LC_BUILD_VERSION,
        24,
        loaded._MACHO_PLATFORM_MACOS,
        minimum,
        sdk,
        0,
    )
    header = struct.pack(
        "<IiiIIIII",
        loaded._MACHO_MAGIC_64_LE,
        loaded._MACHO_CPU_TYPE_ARM64,
        cpu_subtype,
        2,
        1,
        len(command),
        0,
        0,
    )
    return header + command


def _staged_tree(root: Path) -> tuple[Path, Path]:
    loaded = _builder()
    contents = root / loaded._APP_NAME / "Contents"
    macos = contents / "MacOS"
    launch_agents = contents / "Library" / "LaunchAgents"
    macos.mkdir(parents=True)
    launch_agents.mkdir(parents=True)
    executable = macos / loaded._HOST_PRODUCT
    executable.write_bytes(b"synthetic-host")
    executable.chmod(0o755)
    client = macos / loaded._CLIENT_PRODUCT
    client.write_bytes(b"synthetic-client")
    client.chmod(0o755)
    (contents / "Info.plist").write_bytes(b"synthetic-info")
    (launch_agents / "service.plist").write_bytes(b"synthetic-plist")
    (root / loaded._MANIFEST_NAME).write_bytes(b"{}")
    # M35 archives the configured wheel as a required, ordinary file.  Keep
    # this fixture benign so its hard-link/FIFO assertions still exercise the
    # component they name rather than failing on an unrelated missing wheel.
    (root / loaded._WHEEL_NAME).write_bytes(b"synthetic-wheel")
    return root, executable


def _wheel_payloads() -> dict[str, bytes]:
    """Small closed package surface accepted by the builder's ZIP inspector."""
    default_meta_binding = (
        Path(__file__).resolve().parents[1]
        / "src/adhelm/_generated_meta_campaign_pause_approval_bridge_binding_v2.py"
    ).read_bytes()
    return {
        "adhelm/__init__.py": b"",
        "adhelm/_generated_installed_approval_bridge_binding.py": b"GENERATED = True\n",
        "adhelm/_generated_meta_campaign_pause_approval_bridge_binding_v2.py": (
            default_meta_binding
        ),
        "adhelm/_installed_native_approval_bridge.py": b"TRUSTED_BRIDGE = True\n",
        "adhelm/cli.py": b"def main() -> int:\n    return 0\n",
        "adhelm/py.typed": b"",
    }


def _write_closed_wheel(
    path: Path,
    *,
    payloads: dict[str, bytes],
    generated_sha256: str,
) -> None:
    """Write a fake wheel without importing any configured package module."""
    dist_info = "adhelm-0.1.0.dist-info"
    generated_size = len(payloads["adhelm/_generated_installed_approval_bridge_binding.py"])
    record_digest = base64.urlsafe_b64encode(bytes.fromhex(generated_sha256)).rstrip(b"=")
    record = (
        "adhelm/_generated_installed_approval_bridge_binding.py,sha256="
        + record_digest.decode("ascii")
        + f",{generated_size}\n"
    ).encode("ascii")
    members = {
        **payloads,
        f"{dist_info}/METADATA": b"Name: adhelm\nVersion: 0.1.0\n",
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist_info}/RECORD": record,
        f"{dist_info}/entry_points.txt": b"[console_scripts]\nadhelm = adhelm.cli:main\n",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in members.items():
            archive.writestr(name, value)


def _payload_hashes(payloads: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()}


def test_v1_wheel_fixture_admits_the_reviewed_absent_meta_binding(tmp_path: Path) -> None:
    """Keep later payload negatives from passing because their base wheel is stale."""
    loaded = _builder()
    payloads = _wheel_payloads()
    generated = payloads["adhelm/_generated_installed_approval_bridge_binding.py"]
    meta_binding = payloads["adhelm/_generated_meta_campaign_pause_approval_bridge_binding_v2.py"]
    assert hashlib.sha256(meta_binding).hexdigest() == loaded._DEFAULT_META_PYTHON_SOURCE_SHA256

    wheel = tmp_path / loaded._WHEEL_NAME
    _write_closed_wheel(
        wheel,
        payloads=payloads,
        generated_sha256=hashlib.sha256(generated).hexdigest(),
    )

    loaded._inspect_python_wheel(
        wheel,
        generated_python_sha256=hashlib.sha256(generated).hexdigest(),
        expected_payloads=_payload_hashes(payloads),
    )


def test_macho_reader_requires_exact_arm64_all_subtype_and_closed_build_values(
    tmp_path: Path,
) -> None:
    loaded = _builder()
    valid = tmp_path / "arm64"
    valid.write_bytes(_macho(cpu_subtype=0))
    parsed = loaded._read_macho_build_target(valid)
    assert parsed.to_data() == {
        "architecture": "arm64",
        "platform": "macos",
        "minimum_os_version": "13.0.0",
        "sdk_version": "26.2.0",
    }

    arm64e = tmp_path / "arm64e"
    arm64e.write_bytes(_macho(cpu_subtype=2))
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_HOST_BINARY_INVALID"):
        loaded._read_macho_build_target(arm64e)

    wrong_sdk = tmp_path / "wrong-sdk"
    wrong_sdk.write_bytes(_macho(cpu_subtype=0, sdk=26 << 16))
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_HOST_BINARY_INVALID"):
        loaded._read_macho_build_target(wrong_sdk)


def test_hash_rejects_a_final_path_replacement_before_following_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = _builder()
    selected = tmp_path / "selected"
    replacement = tmp_path / "replacement"
    selected.write_bytes(b"same-content")
    replacement.write_bytes(b"same-content")
    original_open = loaded.os.open

    def swap_then_open(path: object, flags: int, *args: object) -> int:
        if Path(path) == selected:
            selected.unlink()
            selected.symlink_to(replacement)
        return original_open(path, flags, *args)

    monkeypatch.setattr(loaded.os, "open", swap_then_open)
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_RESULT_INVALID"):
        loaded._sha256_file(
            selected,
            maximum_bytes=1_024,
            code="NATIVE_ARTIFACT_RESULT_INVALID",
        )


def test_actual_fifo_and_hardlinked_staged_component_are_rejected_without_following_or_hanging(
    tmp_path: Path,
) -> None:
    loaded = _builder()
    fifo = tmp_path / "source.fifo"
    os.mkfifo(fifo)
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID"):
        loaded.extract_exact_source(fifo, destination, epoch=1)

    output, executable = _staged_tree(tmp_path / "output")
    external_link = tmp_path / "linked-host"
    os.link(executable, external_link)
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_OUTPUT_INVALID"):
        loaded.create_deterministic_archive(
            output,
            archive=output / loaded._ARCHIVE_NAME,
            epoch=1,
        )


def test_archive_retains_the_fixed_client_as_an_executable_component(tmp_path: Path) -> None:
    """The staged one-shot client must not lose its executable bit in the tarball."""
    loaded = _builder()
    output, _host = _staged_tree(tmp_path / "output")
    archive = output / loaded._ARCHIVE_NAME

    loaded.create_deterministic_archive(output, archive=archive, epoch=1)

    client_name = (
        Path(loaded._APP_NAME) / "Contents" / "MacOS" / loaded._CLIENT_PRODUCT
    ).as_posix()
    with tarfile.open(archive, "r:") as bundle:
        member = bundle.getmember(client_name)
    assert member.isfile()
    assert member.mode == 0o755


def test_missing_fixed_client_fails_closed_before_creating_an_archive(tmp_path: Path) -> None:
    """A partial staged tree cannot turn a missing helper into a raw traceback."""
    loaded = _builder()
    output, _host = _staged_tree(tmp_path / "output")
    client = output / loaded._APP_NAME / "Contents" / "MacOS" / loaded._CLIENT_PRODUCT
    client.unlink()
    archive = output / loaded._ARCHIVE_NAME

    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_OUTPUT_INVALID"):
        loaded.create_deterministic_archive(output, archive=archive, epoch=1)

    assert not archive.exists()


def test_tool_allowlist_admits_only_fixed_sdk_probes_and_context_bound_products(
    tmp_path: Path,
) -> None:
    loaded = _builder()
    package = tmp_path / "package"
    workspace = tmp_path / "workspace"
    package.mkdir()
    workspace.mkdir()
    for command in (
        [loaded._XCRUN, "--sdk", "macosx", "--show-sdk-version"],
        [loaded._XCRUN, "--sdk", "macosx", "--show-sdk-build-version"],
        [loaded._XCRUN, "--sdk", "macosx", "--show-sdk-path"],
    ):
        assert loaded._allowed_tool(command)
    assert not loaded._allowed_tool([loaded._XCRUN, "--find", "codesign"])
    assert not loaded._allowed_tool([loaded._XCRUN, "--sdk", "iphoneos", "--show-sdk-path"])

    inspector = workspace / "synthetic-inspector"
    assert not loaded._allowed_tool([str(inspector), "--metadata"])
    assert not loaded._allowed_tool([loaded._SWIFT, "build", "--product", "arbitrary"])
    allowed = [
        loaded._SWIFT,
        "build",
        *loaded._swift_options(package, workspace),
        "--product",
        loaded._INSPECTOR_PRODUCT,
    ]
    assert loaded._allowed_tool(allowed, swift_context=(package, workspace))


def test_reproducible_swift_options_have_fixed_map_and_debug_strip_but_retain_uuid(
    tmp_path: Path,
) -> None:
    loaded = _builder()
    package = tmp_path / "source" / "native" / "macos-approval-host"
    package.mkdir(parents=True)
    first_workspace = tmp_path / "first-build"
    second_workspace = tmp_path / "second-build"
    first_workspace.mkdir()
    second_workspace.mkdir()

    first = loaded._swift_options(package, first_workspace)
    second = loaded._swift_options(package, second_workspace)
    for options, workspace in ((first, first_workspace), (second, second_workspace)):
        map_index = options.index("-file-prefix-map")
        assert options[map_index - 1 : map_index + 3] == [
            "-Xswiftc",
            "-file-prefix-map",
            "-Xswiftc",
            f"{workspace}={loaded._REPRODUCIBLE_BUILD_PREFIX}",
        ]
        assert options.count("-file-prefix-map") == 1
        assert "-no_uuid" not in options
        assert options.count("-Xlinker") == 1
        assert options[-2:] == ["-Xlinker", "-S"]
        allowed = [
            loaded._SWIFT,
            "build",
            *options,
            "--product",
            loaded._HOST_PRODUCT,
        ]
        assert loaded._allowed_tool(allowed, swift_context=(package, workspace))
        altered = list(allowed)
        altered[altered.index(f"{workspace}={loaded._REPRODUCIBLE_BUILD_PREFIX}")] = (
            f"{workspace}=/caller-controlled"
        )
        assert not loaded._allowed_tool(altered, swift_context=(package, workspace))
        altered_strip = list(allowed)
        altered_strip[-3] = "-dead_strip"
        assert not loaded._allowed_tool(altered_strip, swift_context=(package, workspace))

    assert f"{first_workspace}={loaded._REPRODUCIBLE_BUILD_PREFIX}" in first
    assert f"{second_workspace}={loaded._REPRODUCIBLE_BUILD_PREFIX}" in second
    assert f"{first_workspace}={loaded._REPRODUCIBLE_BUILD_PREFIX}" not in second


def test_archived_builder_and_contract_must_match_the_running_builder_before_staging(
    tmp_path: Path,
) -> None:
    loaded = _builder()
    snapshot_scripts = tmp_path / "snapshot" / "scripts"
    snapshot_scripts.mkdir(parents=True)
    source_scripts = Path(loaded.__file__).resolve().parent
    for name in ("build_native_approval_artifact.py", "native_approval_artifact_contract.py"):
        shutil.copyfile(source_scripts / name, snapshot_scripts / name)

    loaded._require_archived_builder_provenance(tmp_path / "snapshot")
    (snapshot_scripts / "native_approval_artifact_contract.py").write_text(
        "altered archived generator", encoding="utf-8"
    )
    with pytest.raises(
        loaded.ArtifactBuildError,
        match="NATIVE_ARTIFACT_SOURCE_PROVENANCE_MISMATCH",
    ):
        loaded._require_archived_builder_provenance(tmp_path / "snapshot")


@pytest.mark.parametrize(
    "tamper",
    (
        pytest.param(
            lambda payloads: payloads.__setitem__(
                "adhelm/_installed_native_approval_bridge.py", b"ALTERED_BRIDGE = True\n"
            ),
            id="altered-bridge-module",
        ),
        pytest.param(
            lambda payloads: payloads.__setitem__("adhelm/sitecustomize.pth", b"adhelm\n"),
            id="foreign-pth-member",
        ),
        pytest.param(
            lambda payloads: payloads.__setitem__(
                "adhelm-0.1.0.data/purelib/foreign.py", b"foreign\n"
            ),
            id="foreign-data-member",
        ),
    ),
)
def test_wheel_inspection_rejects_changed_or_foreign_payloads(
    tmp_path: Path,
    tamper: object,
) -> None:
    """A matching generated binding cannot bless another wheel member."""
    loaded = _builder()
    expected = _wheel_payloads()
    generated = expected["adhelm/_generated_installed_approval_bridge_binding.py"]
    actual = dict(expected)
    assert callable(tamper)
    tamper(actual)
    wheel = tmp_path / loaded._WHEEL_NAME
    _write_closed_wheel(
        wheel,
        payloads=actual,
        generated_sha256=hashlib.sha256(generated).hexdigest(),
    )

    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_PYTHON_WHEEL_INVALID"):
        loaded._inspect_python_wheel(
            wheel,
            generated_python_sha256=hashlib.sha256(generated).hexdigest(),
            expected_payloads=_payload_hashes(expected),
        )
