"""Build one unsigned macOS approval-host development artifact from exact source.

This source-only builder does not sign, notarize, install, register, launch,
authenticate, enroll, or contact a provider.  It accepts one closed build
metadata document, archives one exact local Git commit into a private tree,
and runs only the fixed Swift toolchain plus the Core-only metadata inspector
compiled from that tree.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.parser import BytesParser
from enum import Enum
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Never, Protocol, cast

_BUILD_SCHEMA_VERSION = 1
_MAXIMUM_SPEC_BYTES = 16_384
_MAXIMUM_SOURCE_ARCHIVE_BYTES = 268_435_456
_MAXIMUM_SOURCE_EXTRACTED_BYTES = 268_435_456
_MAXIMUM_ARTIFACT_FILE_BYTES = 268_435_456
_MAXIMUM_BUILDER_SOURCE_BYTES = 1_048_576
_CHUNK_BYTES = 65_536
_GIT = "/usr/bin/git"
_SWIFT = "/usr/bin/swift"
_XCRUN = "/usr/bin/xcrun"
_HOST_PRODUCT = "adhelm-macos-installed-approval-host"
_INSPECTOR_PRODUCT = "adhelm-macos-approval-build-inspector"
_CLIENT_PRODUCT = "adhelm-macos-approval-client"
_META_CAMPAIGN_PAUSE_CLIENT_PRODUCT = "adhelm-macos-meta-campaign-pause-approval-client"
_UV = "/opt/homebrew/bin/uv"
_UV_CACHE = "/private/tmp/adhelm-uv-cache"
_PINNED_UV_VERSION = "uv 0.11.32 (Homebrew 2026-07-23 aarch64-apple-darwin)"
_PINNED_PYTHON_VERSION = (3, 12, 13)
_WHEEL_NAME = "adhelm-0.1.0-py3-none-any.whl"
_GENERATED_PYTHON_RELATIVE_PATH = Path("src/adhelm/_generated_installed_approval_bridge_binding.py")
_GENERATED_PYTHON_WHEEL_MEMBER = "adhelm/_generated_installed_approval_bridge_binding.py"
_GENERATED_META_PYTHON_RELATIVE_PATH = Path(
    "src/adhelm/_generated_meta_campaign_pause_approval_bridge_binding_v2.py"
)
_GENERATED_META_PYTHON_WHEEL_MEMBER = (
    "adhelm/_generated_meta_campaign_pause_approval_bridge_binding_v2.py"
)
_META_COMPILED_PYTHON_WHEEL_MEMBER = "adhelm/_compiled_meta_campaign_pause_approval_bridge_v2.py"
_MAXIMUM_WHEEL_BYTES = 67_108_864
_APP_NAME = "AdHelmApprovalHost.app"
_EXECUTABLE_NAME = _HOST_PRODUCT
_ARCHIVE_NAME = "adhelm-macos-installed-approval-host-unsigned.tar"
_MANIFEST_NAME = "manifest.json"
_GENERATED_METADATA_RELATIVE_PATH = Path(
    "native/macos-approval-host/Sources/ApprovalHostCore/GeneratedApprovalHostBuildMetadata.swift"
)
_GENERATED_META_ROUTE_RELATIVE_PATH = Path(
    "native/macos-approval-host/Sources/ApprovalHostCore/"
    "GeneratedMetaCampaignPauseApprovalRouteBinding.swift"
)
_DEFAULT_META_PYTHON_SOURCE_SHA256 = (
    "00b530fa0bc985a132695b75fa55b5d31e18f58fc86d7a2d1c129fa8383c2f2c"
)
_DEFAULT_META_ROUTE_SOURCE_SHA256 = (
    "eb068767a51e8b9dc3970eb43017d4273b0fa45e13d6525b12a3dfd08d06d625"
)

# This is deliberately a development-only ARM64 profile pin.  It is not a
# portable-release assertion and a different installed Swift toolchain fails
# rather than silently producing a differently compiled artifact.
_PINNED_SWIFT_DRIVER = "swift-driver version: 1.127.14.1"
_PINNED_SWIFT_LANGUAGE = "Apple Swift version 6.2.3 (swiftlang-6.2.3.3.21 clang-1700.6.3.2)"
_PINNED_SWIFT_TARGET = "Target: arm64-apple-macosx26.0"
_PINNED_SWIFT_VERSION = "\n".join(
    (_PINNED_SWIFT_LANGUAGE, _PINNED_SWIFT_TARGET, _PINNED_SWIFT_DRIVER)
)
_PINNED_MACOS_SDK_VERSION = "26.2"
_PINNED_MACOS_SDK_BUILD_VERSION = "25C57"
_PINNED_MACOS_SDK_DECLARED_PATH = Path(
    "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/"
    "MacOSX26.2.sdk"
)
_PINNED_MACOS_SDK_CANONICAL_PATH = Path(
    "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
)
_PACKAGE_DEPLOYMENT_TARGET = "macos-13.0"
_REPRODUCIBLE_BUILD_PREFIX = "/adhelm-native-build"
_MACHO_MAGIC_64_LE = 0xFEEDFACF
_MACHO_CPU_TYPE_ARM64 = 0x0100000C
_MACHO_CPU_SUBTYPE_BASE_MASK = 0x00FFFFFF
_MACHO_LC_BUILD_VERSION = 0x32
_MACHO_PLATFORM_MACOS = 1


class ArtifactBuildError(RuntimeError):
    """A bounded local build failure with no source/configuration disclosure."""


class _ArtifactBuildProfile(Enum):
    """The only source-isolated artifact layouts this builder can stage."""

    V1 = "V1"
    META_CAMPAIGN_PAUSE_V2 = "META_CAMPAIGN_PAUSE_V2"


class _ArtifactContract(Protocol):
    def to_data(self) -> dict[str, object]: ...


class _ArtifactDescriptor(Protocol):
    canonical_bytes: bytes
    digest: str
    metadata_canonical_bytes: bytes
    metadata_digest: str
    protocol_configuration_digest: str

    def to_data(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """One exact immutable source snapshot and its deterministic epoch."""

    commit: str
    epoch: int


@dataclass(frozen=True, slots=True)
class PinnedSDKProfile:
    """The single Xcode SDK admitted by the development build profile."""

    declared_path: Path
    canonical_path: Path
    version: str
    build_version: str

    def to_data(self) -> dict[str, str]:
        return {
            "declared_path": str(self.declared_path),
            "canonical_path": str(self.canonical_path),
            "version": self.version,
            "build_version": self.build_version,
        }


@dataclass(frozen=True, slots=True)
class MachOBuildTarget:
    """Minimal LC_BUILD_VERSION evidence from the staged host binary."""

    architecture: str
    platform: str
    minimum_os_version: str
    sdk_version: str

    def to_data(self) -> dict[str, str]:
        return {
            "architecture": self.architecture,
            "platform": self.platform,
            "minimum_os_version": self.minimum_os_version,
            "sdk_version": self.sdk_version,
        }


@dataclass(frozen=True, slots=True)
class _MetaCampaignPauseArtifactFacts:
    """Fixed facts added only by the explicit Meta artifact profile."""

    helper: Path
    helper_binary_target: MachOBuildTarget
    generated_python_sha256: str
    generated_route_sha256: str


@dataclass(frozen=True, slots=True)
class ArtifactBuildResult:
    """Safe local references for one staged unsigned artifact."""

    output_directory: Path
    archive: Path
    source_commit: str
    descriptor_digest: str
    metadata_digest: str
    protocol_configuration_digest: str
    archive_sha256: str

    def __post_init__(self) -> None:
        _validate_artifact_build_result(self)

    def to_data(self) -> dict[str, object]:
        _validate_artifact_build_result(self)
        return {
            "schema_version": _BUILD_SCHEMA_VERSION,
            "artifact_status": "UNSIGNED_UNVERIFIED",
            "archive_name": _ARCHIVE_NAME,
            "source_commit": self.source_commit,
            "descriptor_digest": self.descriptor_digest,
            "metadata_digest": self.metadata_digest,
            "protocol_configuration_digest": self.protocol_configuration_digest,
            "archive_sha256": self.archive_sha256,
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


def _fail(code: str) -> Never:
    raise ArtifactBuildError(code)


def _parse_artifact_build_profile(value: object) -> _ArtifactBuildProfile:
    """Accept one exact build-only profile before reading any caller path."""

    if type(value) is not str:
        _fail("NATIVE_ARTIFACT_PROFILE_INVALID")
    for profile in _ArtifactBuildProfile:
        if value == profile.value:
            return profile
    _fail("NATIVE_ARTIFACT_PROFILE_INVALID")


def _require_artifact_build_profile(value: object) -> _ArtifactBuildProfile:
    if type(value) is not _ArtifactBuildProfile:
        _fail("NATIVE_ARTIFACT_PROFILE_INVALID")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path, *, maximum_bytes: int, code: str) -> str:
    """Hash one selected regular file without reopening its final path unsafely."""

    try:
        return _sha256_bytes(_read_regular_file(path, maximum_bytes=maximum_bytes))
    except ArtifactBuildError:
        _fail(code)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _read_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    """Read one final-component regular file without following a symlink."""

    if not isinstance(path, Path) or type(maximum_bytes) is not int or maximum_bytes < 1:
        _fail("NATIVE_ARTIFACT_INPUT_INVALID")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not nonblock:
        _fail("NATIVE_ARTIFACT_PLATFORM_UNSUPPORTED")
    try:
        selected = path.lstat()
        if (
            not stat.S_ISREG(selected.st_mode)
            or selected.st_nlink != 1
            or selected.st_size > maximum_bytes
        ):
            _fail("NATIVE_ARTIFACT_INPUT_INVALID")
        descriptor = os.open(
            path,
            os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _identity(opened) != _identity(selected)
            ):
                _fail("NATIVE_ARTIFACT_INPUT_CHANGED")
            parts: list[bytes] = []
            size = 0
            while size <= maximum_bytes:
                chunk = os.read(descriptor, min(_CHUNK_BYTES, maximum_bytes + 1 - size))
                if not chunk:
                    break
                parts.append(chunk)
                size += len(chunk)
            if size > maximum_bytes:
                _fail("NATIVE_ARTIFACT_INPUT_INVALID")
            finished = os.fstat(descriptor)
            current = path.lstat()
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or _identity(opened) != _identity(finished)
                or _identity(opened) != _identity(current)
                or size != opened.st_size
            ):
                _fail("NATIVE_ARTIFACT_INPUT_CHANGED")
            return b"".join(parts)
        finally:
            os.close(descriptor)
    except ArtifactBuildError:
        raise
    except (OSError, ValueError):
        _fail("NATIVE_ARTIFACT_INPUT_INVALID")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("NATIVE_ARTIFACT_INPUT_INVALID")
        result[key] = value
    return result


def _contract_api() -> ModuleType:
    """Load the sibling closed contract without importing project runtime code."""

    contract_path = Path(__file__).with_name("native_approval_artifact_contract.py")
    if not contract_path.is_file() or contract_path.is_symlink():
        _fail("NATIVE_ARTIFACT_CONTRACT_UNAVAILABLE")
    module_name = "_adhelm_native_approval_artifact_contract_builder"
    spec = importlib.util.spec_from_file_location(module_name, contract_path)
    if spec is None or spec.loader is None:
        _fail("NATIVE_ARTIFACT_CONTRACT_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    try:
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        _fail("NATIVE_ARTIFACT_CONTRACT_UNAVAILABLE")
    return module


def _input_path(path: Path) -> Path:
    if not isinstance(path, Path):
        _fail("NATIVE_ARTIFACT_INPUT_INVALID")
    absolute = path if path.is_absolute() else Path.cwd() / path
    if ".." in absolute.parts:
        _fail("NATIVE_ARTIFACT_INPUT_INVALID")
    _require_real_directory(absolute.parent, code="NATIVE_ARTIFACT_INPUT_INVALID")
    return absolute


def _load_contract(spec_path: Path) -> tuple[_ArtifactContract, _ArtifactDescriptor, ModuleType]:
    raw = _read_regular_file(_input_path(spec_path), maximum_bytes=_MAXIMUM_SPEC_BYTES)
    api = _contract_api()
    try:
        parse = cast(
            Callable[[bytes], _ArtifactContract], api.parse_native_approval_artifact_contract_json
        )
        describe = cast(
            Callable[[_ArtifactContract], _ArtifactDescriptor],
            api.native_approval_artifact_descriptor,
        )
        contract = parse(raw)
        descriptor = describe(contract)
    except Exception:
        _fail("NATIVE_ARTIFACT_INPUT_INVALID")
    return contract, descriptor, api


def _safe_absolute(path: Path) -> Path:
    if not isinstance(path, Path):
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
    absolute = path if path.is_absolute() else Path.cwd() / path
    if ".." in absolute.parts:
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
    return absolute


def _is_lower_hex(value: object, *, length: int) -> bool:
    return type(value) is str and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def _validate_artifact_build_result(value: ArtifactBuildResult) -> None:
    """Keep agent-visible result data closed even after direct construction."""

    try:
        output_directory = value.output_directory
        archive_path = value.archive
        source_commit = value.source_commit
        descriptor_digest = value.descriptor_digest
        metadata_digest = value.metadata_digest
        protocol_digest = value.protocol_configuration_digest
        archive_sha256 = value.archive_sha256
    except AttributeError:
        _fail("NATIVE_ARTIFACT_RESULT_INVALID")
    if (
        type(value) is not ArtifactBuildResult
        or not isinstance(output_directory, Path)
        or not isinstance(archive_path, Path)
    ):
        _fail("NATIVE_ARTIFACT_RESULT_INVALID")
    if not output_directory.is_absolute() or not archive_path.is_absolute():
        _fail("NATIVE_ARTIFACT_RESULT_INVALID")
    if archive_path != output_directory / _ARCHIVE_NAME:
        _fail("NATIVE_ARTIFACT_RESULT_INVALID")
    try:
        _require_real_directory(output_directory, code="NATIVE_ARTIFACT_RESULT_INVALID")
        directory = output_directory.lstat()
        archive = archive_path.lstat()
    except ArtifactBuildError:
        raise
    except OSError:
        _fail("NATIVE_ARTIFACT_RESULT_INVALID")
    if (
        not stat.S_ISDIR(directory.st_mode)
        or stat.S_ISLNK(directory.st_mode)
        or not stat.S_ISREG(archive.st_mode)
        or stat.S_ISLNK(archive.st_mode)
        or archive.st_nlink != 1
        or not _is_lower_hex(source_commit, length=40)
        or not _is_lower_hex(descriptor_digest, length=64)
        or not _is_lower_hex(metadata_digest, length=64)
        or not _is_lower_hex(protocol_digest, length=64)
        or not _is_lower_hex(archive_sha256, length=64)
        or _sha256_file(
            archive_path,
            maximum_bytes=_MAXIMUM_ARTIFACT_FILE_BYTES,
            code="NATIVE_ARTIFACT_RESULT_INVALID",
        )
        != archive_sha256
    ):
        _fail("NATIVE_ARTIFACT_RESULT_INVALID")


def _require_real_directory(path: Path, *, code: str) -> None:
    """Reject a selected output parent that contains a symlink or non-directory."""

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            value = current.lstat()
        except OSError:
            _fail(code)
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            _fail(code)


def _create_new_private_directory(path: Path) -> Path:
    output = _safe_absolute(path)
    _require_real_directory(output.parent, code="NATIVE_ARTIFACT_OUTPUT_INVALID")
    try:
        # O_EXCL-style directory creation prevents an existing artifact path,
        # symlink, or hard-link alias from becoming an output target.
        os.mkdir(output, mode=0o700)
        opened = output.lstat()
        if not stat.S_ISDIR(opened.st_mode) or stat.S_ISLNK(opened.st_mode):
            _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
        os.chmod(output, 0o700)
        return output
    except ArtifactBuildError:
        raise
    except (FileExistsError, OSError):
        _fail("NATIVE_ARTIFACT_OUTPUT_EXISTS")


def _preflight_new_output_directory(path: Path) -> Path:
    """Check a user-selected output without creating it before the build passes."""

    output = _safe_absolute(path)
    _require_real_directory(output.parent, code="NATIVE_ARTIFACT_OUTPUT_INVALID")
    try:
        output.lstat()
    except FileNotFoundError:
        return output
    except OSError:
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
    _fail("NATIVE_ARTIFACT_OUTPUT_EXISTS")


def _write_private_file(path: Path, data: bytes, *, mode: int) -> None:
    """Write a generated file only once inside a builder-created directory."""

    if type(data) is not bytes or mode not in {0o600, 0o644, 0o755}:
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        _fail("NATIVE_ARTIFACT_PLATFORM_UNSUPPORTED")
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_CLOEXEC", 0),
            mode,
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
                view = view[written:]
            os.fsync(descriptor)
            written_status = os.fstat(descriptor)
            if not stat.S_ISREG(written_status.st_mode) or written_status.st_nlink != 1:
                _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
        finally:
            os.close(descriptor)
        os.chmod(path, mode)
    except ArtifactBuildError:
        raise
    except OSError:
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")


def _open_new_private_file(path: Path, *, mode: int) -> int:
    """Create exactly one regular, single-link output file without following it."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        _fail("NATIVE_ARTIFACT_PLATFORM_UNSUPPORTED")
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_CLOEXEC", 0),
            mode,
        )
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            os.close(descriptor)
            _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
        return descriptor
    except ArtifactBuildError:
        raise
    except OSError:
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")


def _make_private_directory(path: Path, *, mode: int = 0o700) -> None:
    try:
        os.mkdir(path, mode=mode)
        value = path.lstat()
        if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
            _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
        os.chmod(path, mode)
    except ArtifactBuildError:
        raise
    except OSError:
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")


def _validate_archive_name(name: str) -> PurePosixPath:
    value = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or "//" in name
        or value.is_absolute()
        or any(component in {"", ".", ".."} for component in value.parts)
        or value.as_posix() != name
    ):
        _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
    return value


def extract_exact_source(archive: Path, destination: Path, *, epoch: int) -> None:
    """Extract only regular files/directories from one Git archive safely."""

    if type(epoch) is not int or isinstance(epoch, bool) or epoch < 0:
        _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
    try:
        checked_archive = _input_path(archive)
    except ArtifactBuildError:
        _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
    if not isinstance(destination, Path):
        _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
    _require_real_directory(destination, code="NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not nonblock:
        _fail("NATIVE_ARTIFACT_PLATFORM_UNSUPPORTED")
    seen: set[PurePosixPath] = set()
    try:
        selected = checked_archive.lstat()
        if (
            not stat.S_ISREG(selected.st_mode)
            or stat.S_ISLNK(selected.st_mode)
            or selected.st_nlink != 1
            or selected.st_size > _MAXIMUM_SOURCE_ARCHIVE_BYTES
        ):
            _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
        descriptor = os.open(
            checked_archive,
            os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _identity(opened) != _identity(selected)
            ):
                _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
            with os.fdopen(descriptor, "rb", closefd=False) as archive_file:
                with tarfile.open(fileobj=archive_file, mode="r:") as bundle:
                    members = bundle.getmembers()
                    validated: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
                    extracted_size = 0
                    for member in members:
                        relative = _validate_archive_name(member.name.rstrip("/"))
                        if (
                            relative in seen
                            or not (member.isdir() or member.isreg())
                            or member.size < 0
                        ):
                            _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
                        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                            _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
                        extracted_size += member.size
                        if extracted_size > _MAXIMUM_SOURCE_EXTRACTED_BYTES:
                            _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
                        seen.add(relative)
                        validated.append((member, relative))
                    for member, relative in validated:
                        target = destination.joinpath(*relative.parts)
                        parent = target.parent
                        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        parent_status = parent.lstat()
                        if not stat.S_ISDIR(parent_status.st_mode) or stat.S_ISLNK(
                            parent_status.st_mode
                        ):
                            _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
                        if member.isdir():
                            target.mkdir(mode=0o700, exist_ok=True)
                            target_status = target.lstat()
                            if not stat.S_ISDIR(target_status.st_mode) or stat.S_ISLNK(
                                target_status.st_mode
                            ):
                                _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
                            os.chmod(target, 0o700)
                        else:
                            source = bundle.extractfile(member)
                            if source is None:
                                _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
                            try:
                                data = source.read()
                            finally:
                                source.close()
                            if len(data) != member.size:
                                _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
                            _write_private_file(target, data, mode=0o600)
                            os.utime(target, ns=(epoch * 1_000_000_000, epoch * 1_000_000_000))
                    for member, relative in reversed(validated):
                        if member.isdir():
                            target = destination.joinpath(*relative.parts)
                            os.utime(target, ns=(epoch * 1_000_000_000, epoch * 1_000_000_000))
            finished = os.fstat(descriptor)
            current = checked_archive.lstat()
            if _identity(opened) != _identity(finished) or _identity(opened) != _identity(current):
                _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
        finally:
            os.close(descriptor)
    except ArtifactBuildError:
        raise
    except (OSError, tarfile.TarError):
        _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")


def _sanitized_environment(workspace: Path, *, epoch: int) -> dict[str, str]:
    if type(epoch) is not int or isinstance(epoch, bool) or epoch < 0:
        _fail("NATIVE_ARTIFACT_EPOCH_INVALID")
    environment: dict[str, str] = {}
    home = workspace / "home"
    temporary = workspace / "tmp"
    _make_private_directory(home)
    _make_private_directory(temporary)
    environment.update(
        {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LC_ALL": "C",
            "LANG": "C",
            "TZ": "UTC",
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "SOURCE_DATE_EPOCH": str(epoch),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
        }
    )
    return environment


def _allowed_swift_build(
    arguments: Sequence[str],
    *,
    package: Path,
    workspace: Path,
    profile: _ArtifactBuildProfile = _ArtifactBuildProfile.V1,
) -> bool:
    selected_profile = _require_artifact_build_profile(profile)
    prefix = [_SWIFT, "build", *_swift_options(package, workspace)]
    if list(arguments[: len(prefix)]) != prefix:
        return False
    suffix = list(arguments[len(prefix) :])
    allowed: tuple[list[str], ...] = (
        ["--product", _HOST_PRODUCT],
        ["--product", _INSPECTOR_PRODUCT],
        ["--product", _CLIENT_PRODUCT],
        ["--show-bin-path"],
    )
    if selected_profile is _ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2:
        allowed += (["--product", _META_CAMPAIGN_PAUSE_CLIENT_PRODUCT],)
    return suffix in allowed


def _allowed_tool(
    arguments: Sequence[str],
    *,
    inspector: Path | None = None,
    swift_context: tuple[Path, Path] | None = None,
    archive_output: Path | None = None,
    python_build_context: tuple[Path, Path] | None = None,
    profile: _ArtifactBuildProfile = _ArtifactBuildProfile.V1,
) -> bool:
    selected_profile = _require_artifact_build_profile(profile)
    if not arguments:
        return False
    executable = arguments[0]
    if executable == _GIT:
        if len(arguments) == 5 and arguments[1:4] == ["rev-parse", "--verify", "--end-of-options"]:
            return re.fullmatch(r"[^\x00\r\n]{1,128}\^\{commit\}", arguments[4]) is not None
        if len(arguments) == 5 and arguments[1:4] == ["show", "-s", "--format=%ct"]:
            return re.fullmatch(r"[0-9a-f]{40}", arguments[4]) is not None
        if len(arguments) == 5 and arguments[1:3] == ["archive", "--format=tar"]:
            return (
                archive_output is not None
                and arguments[3] == f"--output={archive_output}"
                and re.fullmatch(r"[0-9a-f]{40}", arguments[4]) is not None
            )
        return False
    if executable == _UV:
        if list(arguments) == [_UV, "--version"]:
            return True
        return python_build_context is not None and list(arguments) == _python_wheel_command(
            *python_build_context
        )
    if executable == _SWIFT:
        if arguments == [_SWIFT, "--version"]:
            return True
        if swift_context is None:
            return False
        return _allowed_swift_build(
            arguments,
            package=swift_context[0],
            workspace=swift_context[1],
            profile=selected_profile,
        )
    if executable == _XCRUN:
        return list(arguments) in (
            [_XCRUN, "--sdk", "macosx", "--show-sdk-version"],
            [_XCRUN, "--sdk", "macosx", "--show-sdk-build-version"],
            [_XCRUN, "--sdk", "macosx", "--show-sdk-path"],
        )
    return inspector is not None and executable == str(inspector) and len(arguments) == 1


def _run_tool(
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    inspector: Path | None = None,
    swift_context: tuple[Path, Path] | None = None,
    archive_output: Path | None = None,
    python_build_context: tuple[Path, Path] | None = None,
    profile: _ArtifactBuildProfile = _ArtifactBuildProfile.V1,
    timeout: int = 600,
) -> subprocess.CompletedProcess[bytes]:
    """Run only a fixed Git/Swift command or the exact generated inspector."""

    command = list(arguments)
    if (
        type(timeout) is not int
        or isinstance(timeout, bool)
        or timeout < 1
        or not _allowed_tool(
            command,
            inspector=inspector,
            swift_context=swift_context,
            archive_output=archive_output,
            python_build_context=python_build_context,
            profile=profile,
        )
    ):
        _fail("NATIVE_ARTIFACT_TOOL_INVALID")
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        _fail("NATIVE_ARTIFACT_TOOL_FAILED")
    if result.returncode != 0:
        _fail("NATIVE_ARTIFACT_TOOL_FAILED")
    return result


def resolve_commit(repo: Path, revision: str, *, environment: Mapping[str, str]) -> ResolvedSource:
    if type(revision) is not str or not revision or len(revision) > 128:
        _fail("NATIVE_ARTIFACT_REVISION_INVALID")
    checked_repo = _safe_absolute(repo)
    _require_real_directory(checked_repo, code="NATIVE_ARTIFACT_REVISION_INVALID")
    try:
        commit = (
            _run_tool(
                [_GIT, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
                cwd=checked_repo,
                environment=environment,
            )
            .stdout.decode("ascii")
            .strip()
        )
        epoch_text = (
            _run_tool(
                [_GIT, "show", "-s", "--format=%ct", commit],
                cwd=checked_repo,
                environment=environment,
            )
            .stdout.decode("ascii")
            .strip()
        )
    except (ArtifactBuildError, UnicodeDecodeError):
        _fail("NATIVE_ARTIFACT_REVISION_INVALID")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None or re.fullmatch(r"[0-9]+", epoch_text) is None:
        _fail("NATIVE_ARTIFACT_REVISION_INVALID")
    epoch = int(epoch_text)
    if epoch < 0:
        _fail("NATIVE_ARTIFACT_REVISION_INVALID")
    return ResolvedSource(commit=commit, epoch=epoch)


def _checked_epoch(value: object, source: ResolvedSource) -> int:
    if value is None:
        return source.epoch
    if type(value) is not int or isinstance(value, bool) or value != source.epoch:
        _fail("NATIVE_ARTIFACT_EPOCH_INVALID")
    return value


def _swift_options(package: Path, workspace: Path) -> list[str]:
    return [
        "--package-path",
        str(package),
        "--configuration",
        "release",
        "--scratch-path",
        str(workspace / "swift-build"),
        "--cache-path",
        str(workspace / "swift-cache"),
        "--config-path",
        str(workspace / "swift-config"),
        "--security-path",
        str(workspace / "swift-security"),
        "--manifest-cache",
        "local",
        "--disable-keychain",
        "--disable-netrc",
        "--skip-update",
        "--disable-automatic-resolution",
        "-Xswiftc",
        "-sdk",
        "-Xswiftc",
        str(_PINNED_MACOS_SDK_CANONICAL_PATH),
        "-Xswiftc",
        "-file-prefix-map",
        "-Xswiftc",
        f"{workspace}={_REPRODUCIBLE_BUILD_PREFIX}",
        "-Xswiftc",
        "-module-cache-path",
        "-Xswiftc",
        str(workspace / "swift-modules"),
        "-Xlinker",
        "-S",
    ]


def _require_pinned_swift(environment: Mapping[str, str], workspace: Path) -> str:
    result = _run_tool([_SWIFT, "--version"], cwd=workspace, environment=environment)
    try:
        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8")
    except UnicodeDecodeError:
        _fail("NATIVE_ARTIFACT_TOOLCHAIN_MISMATCH")
    version = _normalized_swift_version(stdout, stderr)
    if version != _PINNED_SWIFT_VERSION:
        _fail("NATIVE_ARTIFACT_TOOLCHAIN_MISMATCH")
    return version


def _require_pinned_sdk(environment: Mapping[str, str], workspace: Path) -> PinnedSDKProfile:
    """Admit only the one reviewed SDK that the fixed Swift options name."""

    probes = (
        ([_XCRUN, "--sdk", "macosx", "--show-sdk-version"], _PINNED_MACOS_SDK_VERSION),
        (
            [_XCRUN, "--sdk", "macosx", "--show-sdk-build-version"],
            _PINNED_MACOS_SDK_BUILD_VERSION,
        ),
        (
            [_XCRUN, "--sdk", "macosx", "--show-sdk-path"],
            str(_PINNED_MACOS_SDK_DECLARED_PATH),
        ),
    )
    for arguments, expected in probes:
        result = _run_tool(arguments, cwd=workspace, environment=environment)
        try:
            output = result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            _fail("NATIVE_ARTIFACT_TOOLCHAIN_MISMATCH")
        if result.stderr or output != f"{expected}\n":
            _fail("NATIVE_ARTIFACT_TOOLCHAIN_MISMATCH")
    try:
        declared_status = _PINNED_MACOS_SDK_DECLARED_PATH.lstat()
        if (
            not stat.S_ISLNK(declared_status.st_mode)
            or declared_status.st_uid != 0
            or os.readlink(_PINNED_MACOS_SDK_DECLARED_PATH) != "MacOSX.sdk"
            or _PINNED_MACOS_SDK_DECLARED_PATH.resolve(strict=True)
            != _PINNED_MACOS_SDK_CANONICAL_PATH
        ):
            _fail("NATIVE_ARTIFACT_TOOLCHAIN_MISMATCH")
        canonical_status = _PINNED_MACOS_SDK_CANONICAL_PATH.lstat()
        if canonical_status.st_uid != 0:
            _fail("NATIVE_ARTIFACT_TOOLCHAIN_MISMATCH")
    except ArtifactBuildError:
        raise
    except OSError:
        _fail("NATIVE_ARTIFACT_TOOLCHAIN_MISMATCH")
    _require_real_directory(
        _PINNED_MACOS_SDK_CANONICAL_PATH,
        code="NATIVE_ARTIFACT_TOOLCHAIN_MISMATCH",
    )
    return PinnedSDKProfile(
        declared_path=_PINNED_MACOS_SDK_DECLARED_PATH,
        canonical_path=_PINNED_MACOS_SDK_CANONICAL_PATH,
        version=_PINNED_MACOS_SDK_VERSION,
        build_version=_PINNED_MACOS_SDK_BUILD_VERSION,
    )


def _normalized_swift_version(stdout: str, stderr: str) -> str:
    """Normalize the two known stdout/stderr orderings without weakening the pin."""

    candidates = {
        f"{_PINNED_SWIFT_DRIVER} {_PINNED_SWIFT_LANGUAGE}\n{_PINNED_SWIFT_TARGET}",
        _PINNED_SWIFT_VERSION,
    }
    combined = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    if combined not in candidates:
        _fail("NATIVE_ARTIFACT_TOOLCHAIN_MISMATCH")
    return _PINNED_SWIFT_VERSION


def _archive_source(
    repo: Path,
    source: ResolvedSource,
    destination: Path,
    *,
    environment: Mapping[str, str],
) -> Path:
    archive = destination / "source.tar"
    checked_repo = _safe_absolute(repo)
    _require_real_directory(checked_repo, code="NATIVE_ARTIFACT_REVISION_INVALID")
    _run_tool(
        [_GIT, "archive", "--format=tar", f"--output={archive}", source.commit],
        cwd=checked_repo,
        environment=environment,
        archive_output=archive,
    )
    try:
        value = archive.lstat()
    except OSError:
        _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
    if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode) or value.st_nlink != 1:
        _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
    return archive


def _write_generated_metadata(snapshot: Path, generated: str) -> Path:
    return _write_generated_source(snapshot, _GENERATED_METADATA_RELATIVE_PATH, generated)


def _write_generated_python_binding(snapshot: Path, generated: str) -> Path:
    return _write_generated_source(snapshot, _GENERATED_PYTHON_RELATIVE_PATH, generated)


def _write_generated_meta_python_binding(snapshot: Path, generated: str) -> Path:
    return _write_generated_source(snapshot, _GENERATED_META_PYTHON_RELATIVE_PATH, generated)


def _write_generated_meta_route_binding(snapshot: Path, generated: str) -> Path:
    return _write_generated_source(snapshot, _GENERATED_META_ROUTE_RELATIVE_PATH, generated)


def _write_generated_source(snapshot: Path, relative: Path, generated: str) -> Path:
    if relative not in (
        _GENERATED_METADATA_RELATIVE_PATH,
        _GENERATED_PYTHON_RELATIVE_PATH,
        _GENERATED_META_PYTHON_RELATIVE_PATH,
        _GENERATED_META_ROUTE_RELATIVE_PATH,
    ):
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    if type(generated) is not str or not generated or "\x00" in generated:
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    target = snapshot / relative
    try:
        current = target.lstat()
    except OSError:
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    if not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode) or current.st_nlink != 1:
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    try:
        target.unlink()
    except OSError:
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    _write_private_file(target, generated.encode("utf-8"), mode=0o600)
    return target


def _require_unchanged_generated_sources(sources: tuple[tuple[Path, str], ...]) -> None:
    """Hash only the closed source set selected by the private build profile."""

    if not 1 <= len(sources) <= 4:
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    seen: set[Path] = set()
    for path, expected_sha256 in sources:
        if (
            not isinstance(path, Path)
            or path in seen
            or not _is_lower_hex(expected_sha256, length=64)
        ):
            _fail("NATIVE_ARTIFACT_METADATA_INVALID")
        seen.add(path)
        _require_unchanged_generated_metadata(path, expected_sha256)


def _v1_meta_default_source_hashes(snapshot: Path) -> tuple[tuple[Path, str], ...]:
    return (
        (snapshot / _GENERATED_META_PYTHON_RELATIVE_PATH, _DEFAULT_META_PYTHON_SOURCE_SHA256),
        (snapshot / _GENERATED_META_ROUTE_RELATIVE_PATH, _DEFAULT_META_ROUTE_SOURCE_SHA256),
    )


def _profile_generated_source_hashes(
    snapshot: Path,
    *,
    profile: _ArtifactBuildProfile,
    generated_metadata_sha256: str,
    generated_python_sha256: str,
    generated_meta_python_sha256: str | None,
    generated_meta_route_sha256: str | None,
) -> tuple[tuple[Path, str], ...]:
    selected_profile = _require_artifact_build_profile(profile)
    sources: tuple[tuple[Path, str], ...] = (
        (snapshot / _GENERATED_METADATA_RELATIVE_PATH, generated_metadata_sha256),
        (snapshot / _GENERATED_PYTHON_RELATIVE_PATH, generated_python_sha256),
    )
    if selected_profile is _ArtifactBuildProfile.V1:
        if generated_meta_python_sha256 is not None or generated_meta_route_sha256 is not None:
            _fail("NATIVE_ARTIFACT_METADATA_INVALID")
        return sources + _v1_meta_default_source_hashes(snapshot)
    if not _is_lower_hex(generated_meta_python_sha256, length=64) or not _is_lower_hex(
        generated_meta_route_sha256, length=64
    ):
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    assert generated_meta_python_sha256 is not None
    assert generated_meta_route_sha256 is not None
    return sources + (
        (snapshot / _GENERATED_META_PYTHON_RELATIVE_PATH, generated_meta_python_sha256),
        (snapshot / _GENERATED_META_ROUTE_RELATIVE_PATH, generated_meta_route_sha256),
    )


def _require_archived_builder_provenance(snapshot: Path) -> None:
    """Require the archived generator and builder to equal the running sources."""

    running_directory = Path(__file__).resolve().parent
    for name in ("build_native_approval_artifact.py", "native_approval_artifact_contract.py"):
        try:
            running = _read_regular_file(
                running_directory / name,
                maximum_bytes=_MAXIMUM_BUILDER_SOURCE_BYTES,
            )
            archived = _read_regular_file(
                snapshot / "scripts" / name,
                maximum_bytes=_MAXIMUM_BUILDER_SOURCE_BYTES,
            )
        except ArtifactBuildError:
            _fail("NATIVE_ARTIFACT_SOURCE_PROVENANCE_MISMATCH")
        if running != archived:
            _fail("NATIVE_ARTIFACT_SOURCE_PROVENANCE_MISMATCH")


def _require_unchanged_generated_metadata(path: Path, expected_sha256: str) -> None:
    if not _is_lower_hex(expected_sha256, length=64):
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    if (
        _sha256_file(
            path,
            maximum_bytes=_MAXIMUM_SPEC_BYTES,
            code="NATIVE_ARTIFACT_METADATA_INVALID",
        )
        != expected_sha256
    ):
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")


def _development_bundle_versions(snapshot: Path) -> tuple[str, str]:
    """Verify the only development version mapping supported by this builder."""

    source = _read_regular_file(snapshot / "pyproject.toml", maximum_bytes=131_072)
    try:
        document = tomllib.loads(source.decode("utf-8"))
        project = document["project"]
        version = project["version"] if type(project) is dict else None
    except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        _fail("NATIVE_ARTIFACT_VERSION_INVALID")
    if type(version) is not str or version != "0.1.0":
        _fail("NATIVE_ARTIFACT_VERSION_INVALID")
    return "0.1.0", "1"


def _safe_regular_executable(path: Path, *, build_root: Path, expected_name: str) -> Path:
    try:
        resolved_root = build_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        value = path.lstat()
    except OSError:
        _fail("NATIVE_ARTIFACT_INSPECTOR_INVALID")
    if (
        not resolved.is_relative_to(resolved_root)
        or not stat.S_ISREG(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or value.st_mode & 0o111 == 0
        or path.name != expected_name
    ):
        _fail("NATIVE_ARTIFACT_INSPECTOR_INVALID")
    return path


def _macho_version(value: int) -> str:
    if type(value) is not int or value < 0:
        _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
    major = (value >> 16) & 0xFFFF
    minor = (value >> 8) & 0xFF
    patch = value & 0xFF
    if major < 1:
        _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
    return f"{major}.{minor}.{patch}"


def _read_macho_build_target(host: Path) -> MachOBuildTarget:
    """Read the pinned ARM64 host's LC_BUILD_VERSION without invoking a tool."""

    try:
        data = _read_regular_file(host, maximum_bytes=_MAXIMUM_ARTIFACT_FILE_BYTES)
    except ArtifactBuildError:
        _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
    if len(data) < 32:
        _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
    try:
        magic, cputype, cpusubtype, _, command_count, commands_size, _, _ = struct.unpack_from(
            "<IiiIIIII", data, 0
        )
    except struct.error:
        _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
    if (
        magic != _MACHO_MAGIC_64_LE
        or cputype != _MACHO_CPU_TYPE_ARM64
        or cpusubtype & _MACHO_CPU_SUBTYPE_BASE_MASK != 0
        or command_count > 4_096
        or commands_size > len(data) - 32
    ):
        _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
    command_end = 32 + commands_size
    offset = 32
    observed: MachOBuildTarget | None = None
    for _ in range(command_count):
        if offset + 8 > command_end:
            _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
        try:
            command, command_size = struct.unpack_from("<II", data, offset)
        except struct.error:
            _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
        if command_size < 8 or offset + command_size > command_end:
            _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
        if command == _MACHO_LC_BUILD_VERSION:
            if observed is not None or command_size < 24:
                _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
            try:
                _, _, platform, minimum_os, sdk, tools = struct.unpack_from("<IIIIII", data, offset)
            except struct.error:
                _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
            if platform != _MACHO_PLATFORM_MACOS or tools > 256 or 24 + tools * 8 > command_size:
                _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
            observed = MachOBuildTarget(
                architecture="arm64",
                platform="macos",
                minimum_os_version=_macho_version(minimum_os),
                sdk_version=_macho_version(sdk),
            )
        offset += command_size
    if offset != command_end or observed is None:
        _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
    if observed.minimum_os_version != "13.0.0" or observed.sdk_version != "26.2.0":
        _fail("NATIVE_ARTIFACT_HOST_BINARY_INVALID")
    return observed


def _build_and_inspect(
    snapshot: Path,
    workspace: Path,
    *,
    environment: Mapping[str, str],
    expected_descriptor: _ArtifactDescriptor,
    profile: _ArtifactBuildProfile,
    generated_source_hashes: tuple[tuple[Path, str], ...],
) -> tuple[
    Path,
    Path,
    Path | None,
    bytes,
    str,
    PinnedSDKProfile,
    MachOBuildTarget,
    MachOBuildTarget,
    MachOBuildTarget | None,
]:
    selected_profile = _require_artifact_build_profile(profile)
    package = snapshot / "native/macos-approval-host"
    if not package.is_dir() or package.is_symlink() or not (package / "Package.swift").is_file():
        _fail("NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
    _require_unchanged_generated_sources(generated_source_hashes)
    toolchain = _require_pinned_swift(environment, workspace)
    sdk = _require_pinned_sdk(environment, workspace)
    _require_unchanged_generated_sources(generated_source_hashes)
    options = _swift_options(package, workspace)
    _run_tool(
        [_SWIFT, "build", *options, "--product", _HOST_PRODUCT],
        cwd=workspace,
        environment=environment,
        swift_context=(package, workspace),
        profile=selected_profile,
    )
    _require_unchanged_generated_sources(generated_source_hashes)
    _run_tool(
        [_SWIFT, "build", *options, "--product", _INSPECTOR_PRODUCT],
        cwd=workspace,
        environment=environment,
        swift_context=(package, workspace),
        profile=selected_profile,
    )
    _require_unchanged_generated_sources(generated_source_hashes)
    _run_tool(
        [_SWIFT, "build", *options, "--product", _CLIENT_PRODUCT],
        cwd=workspace,
        environment=environment,
        swift_context=(package, workspace),
        profile=selected_profile,
    )
    _require_unchanged_generated_sources(generated_source_hashes)
    if selected_profile is _ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2:
        _run_tool(
            [_SWIFT, "build", *options, "--product", _META_CAMPAIGN_PAUSE_CLIENT_PRODUCT],
            cwd=workspace,
            environment=environment,
            swift_context=(package, workspace),
            profile=selected_profile,
        )
        _require_unchanged_generated_sources(generated_source_hashes)
    bin_path = _run_tool(
        [_SWIFT, "build", *options, "--show-bin-path"],
        cwd=workspace,
        environment=environment,
        swift_context=(package, workspace),
        profile=selected_profile,
    )
    _require_unchanged_generated_sources(generated_source_hashes)
    try:
        binary_directory = Path(bin_path.stdout.decode("utf-8").strip())
    except UnicodeDecodeError:
        _fail("NATIVE_ARTIFACT_INSPECTOR_INVALID")
    build_root = workspace / "swift-build"
    inspector = _safe_regular_executable(
        binary_directory / _INSPECTOR_PRODUCT,
        build_root=build_root,
        expected_name=_INSPECTOR_PRODUCT,
    )
    host = _safe_regular_executable(
        binary_directory / _HOST_PRODUCT,
        build_root=build_root,
        expected_name=_HOST_PRODUCT,
    )
    client = _safe_regular_executable(
        binary_directory / _CLIENT_PRODUCT,
        build_root=build_root,
        expected_name=_CLIENT_PRODUCT,
    )
    meta_client: Path | None = None
    if selected_profile is _ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2:
        meta_client = _safe_regular_executable(
            binary_directory / _META_CAMPAIGN_PAUSE_CLIENT_PRODUCT,
            build_root=build_root,
            expected_name=_META_CAMPAIGN_PAUSE_CLIENT_PRODUCT,
        )
    _require_unchanged_generated_sources(generated_source_hashes)
    result = _run_tool(
        [str(inspector)],
        cwd=workspace,
        environment=environment,
        inspector=inspector,
        profile=selected_profile,
    )
    _require_unchanged_generated_sources(generated_source_hashes)
    if result.stderr:
        _fail("NATIVE_ARTIFACT_INSPECTOR_INVALID")
    try:
        canonical = expected_descriptor.canonical_bytes
        actual_data = result.stdout
        expected_data = expected_descriptor.to_data()
        actual = json.loads(actual_data.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
        if actual_data != canonical or actual != expected_data:
            raise ValueError("descriptor mismatch")
    except Exception:
        _fail("NATIVE_ARTIFACT_INSPECTOR_MISMATCH")
    return (
        host,
        client,
        meta_client,
        actual_data,
        toolchain,
        sdk,
        _read_macho_build_target(host),
        _read_macho_build_target(client),
        _read_macho_build_target(meta_client) if meta_client is not None else None,
    )


def _python_wheel_command(snapshot: Path, workspace: Path) -> list[str]:
    return [
        _UV,
        "--no-config",
        "build",
        "--offline",
        "--no-sources",
        "--no-managed-python",
        "--no-python-downloads",
        "--keyring-provider",
        "disabled",
        "--cache-dir",
        _UV_CACHE,
        "--python",
        sys.executable,
        "--wheel",
        "--no-create-gitignore",
        "--out-dir",
        str(workspace / "python-wheel"),
        str(snapshot),
    ]


def _generated_python_wheel_members(
    *,
    profile: _ArtifactBuildProfile,
    generated_python_sha256: str,
    generated_meta_python_sha256: str | None,
) -> tuple[tuple[str, str], ...]:
    selected_profile = _require_artifact_build_profile(profile)
    if not _is_lower_hex(generated_python_sha256, length=64):
        _fail("NATIVE_ARTIFACT_PYTHON_WHEEL_INVALID")
    members: tuple[tuple[str, str], ...] = (
        (_GENERATED_PYTHON_WHEEL_MEMBER, generated_python_sha256),
    )
    if selected_profile is _ArtifactBuildProfile.V1:
        if generated_meta_python_sha256 is not None:
            _fail("NATIVE_ARTIFACT_PYTHON_WHEEL_INVALID")
        return members
    if not _is_lower_hex(generated_meta_python_sha256, length=64):
        _fail("NATIVE_ARTIFACT_PYTHON_WHEEL_INVALID")
    assert generated_meta_python_sha256 is not None
    return members + ((_GENERATED_META_PYTHON_WHEEL_MEMBER, generated_meta_python_sha256),)


def _inspect_python_wheel(
    wheel: Path,
    *,
    generated_python_sha256: str,
    expected_payloads: Mapping[str, str],
    profile: _ArtifactBuildProfile = _ArtifactBuildProfile.V1,
    generated_meta_python_sha256: str | None = None,
) -> None:
    """Read bounded ZIP metadata/module bytes; never import configured code."""
    selected_profile = _require_artifact_build_profile(profile)
    generated_members = _generated_python_wheel_members(
        profile=selected_profile,
        generated_python_sha256=generated_python_sha256,
        generated_meta_python_sha256=generated_meta_python_sha256,
    )
    if wheel.name != _WHEEL_NAME:
        _fail("NATIVE_ARTIFACT_PYTHON_WHEEL_INVALID")
    if (
        not expected_payloads
        or len(expected_payloads) > 10_000
        or any(
            not key.startswith("adhelm/") or not _is_lower_hex(value, length=64)
            for key, value in expected_payloads.items()
        )
        or any(expected_payloads.get(name) != digest for name, digest in generated_members)
        or (
            selected_profile is _ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2
            and _META_COMPILED_PYTHON_WHEEL_MEMBER not in expected_payloads
        )
    ):
        _fail("NATIVE_ARTIFACT_PYTHON_WHEEL_INVALID")
    data = _read_regular_file(wheel, maximum_bytes=_MAXIMUM_WHEEL_BYTES)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if not 1 <= len(members) <= 10_000 or len(set(names)) != len(names):
                raise ValueError("member set")
            total = 0
            for member in members:
                name = member.filename
                path = PurePosixPath(name.rstrip("/"))
                kind = stat.S_IFMT(member.external_attr >> 16)
                if (
                    not name
                    or len(name.encode("utf-8")) > 1024
                    or "\\" in name
                    or any(ord(character) < 32 for character in name)
                    or not path.parts
                    or path.is_absolute()
                    or any(part in (".", "..") for part in path.parts)
                    or path.as_posix() != name.rstrip("/")
                    or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or member.flag_bits & 1
                    or member.file_size < 0
                ):
                    raise ValueError("member shape")
                total += member.file_size
                if total > _MAXIMUM_WHEEL_BYTES:
                    raise ValueError("expanded wheel bound")
            distribution_members = {
                "adhelm-0.1.0.dist-info/" + name
                for name in ("METADATA", "WHEEL", "RECORD", "entry_points.txt")
            }
            actual_payloads = {member.filename for member in members if not member.is_dir()}
            if actual_payloads != set(expected_payloads) | distribution_members:
                raise ValueError("wheel source payload set")
            for name, digest in expected_payloads.items():
                value = _bounded_wheel_member(archive, name, _MAXIMUM_WHEEL_BYTES, allow_empty=True)
                if _sha256_bytes(value) != digest:
                    raise ValueError("wheel source payload mismatch")
            if (
                _bounded_wheel_member(archive, "adhelm-0.1.0.dist-info/entry_points.txt", 16_384)
                != b"[console_scripts]\nadhelm = adhelm.cli:main\n"
            ):
                raise ValueError("wheel entrypoint mismatch")
            for generated_name, generated_sha256 in generated_members:
                module = archive.getinfo(generated_name)
                if module.is_dir() or not 1 <= module.file_size <= _MAXIMUM_SPEC_BYTES:
                    raise ValueError("generated module bound")
                with archive.open(module) as stream:
                    source = stream.read(_MAXIMUM_SPEC_BYTES + 1)
                if len(source) != module.file_size or _sha256_bytes(source) != generated_sha256:
                    raise ValueError("generated module mismatch")
            required_members: tuple[str, ...] = (
                "adhelm/cli.py",
                "adhelm/py.typed",
                "adhelm/_installed_native_approval_bridge.py",
                "adhelm-0.1.0.dist-info/METADATA",
                "adhelm-0.1.0.dist-info/WHEEL",
                "adhelm-0.1.0.dist-info/RECORD",
            )
            if selected_profile is _ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2:
                required_members += (_META_COMPILED_PYTHON_WHEEL_MEMBER,)
            for name in required_members:
                if archive.getinfo(name).is_dir():
                    raise ValueError("required wheel member")
            metadata = BytesParser().parsebytes(
                _bounded_wheel_member(archive, "adhelm-0.1.0.dist-info/METADATA", 131_072)
            )
            wheel_metadata = BytesParser().parsebytes(
                _bounded_wheel_member(archive, "adhelm-0.1.0.dist-info/WHEEL", 16_384)
            )
            if (
                metadata.get_all("Name") != ["adhelm"]
                or metadata.get_all("Version") != ["0.1.0"]
                or wheel_metadata.get_all("Wheel-Version") != ["1.0"]
                or wheel_metadata.get_all("Root-Is-Purelib") != ["true"]
                or wheel_metadata.get_all("Tag") != ["py3-none-any"]
            ):
                raise ValueError("wheel identity")
            record = _bounded_wheel_member(
                archive, "adhelm-0.1.0.dist-info/RECORD", 2_097_152
            ).decode("utf-8")
            records = list(csv.reader(io.StringIO(record), strict=True))
            if any(len(row) != 3 for row in records):
                raise ValueError("wheel record shape")
            for generated_name, generated_sha256 in generated_members:
                source = _bounded_wheel_member(archive, generated_name, _MAXIMUM_SPEC_BYTES)
                matching = [row for row in records if row[0] == generated_name]
                digest = (
                    base64.urlsafe_b64encode(bytes.fromhex(generated_sha256)).rstrip(b"=").decode()
                )
                if matching != [[generated_name, "sha256=" + digest, str(len(source))]]:
                    raise ValueError("generated module record mismatch")
    except (OSError, ValueError, KeyError, RuntimeError, csv.Error, zipfile.BadZipFile):
        _fail("NATIVE_ARTIFACT_PYTHON_WHEEL_INVALID")


def _bounded_wheel_member(
    archive: zipfile.ZipFile, name: str, maximum: int, *, allow_empty: bool = False
) -> bytes:
    member = archive.getinfo(name)
    if member.is_dir() or not (0 if allow_empty else 1) <= member.file_size <= maximum:
        raise ValueError("wheel member bound")
    with archive.open(member) as stream:
        value = stream.read(maximum + 1)
    if len(value) != member.file_size:
        raise ValueError("wheel member length")
    return value


def _snapshot_python_payloads(snapshot: Path) -> dict[str, str]:
    payloads: dict[str, str] = {}
    total = 0
    for source, prefix in (
        (snapshot / "src/adhelm", "adhelm/"),
        (snapshot / "skills", "adhelm/bundled_skills/"),
    ):
        _require_real_directory(source, code="NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID")
        for path in sorted(source.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            name = prefix + path.relative_to(source).as_posix()
            if name in payloads or len(payloads) >= 10_000:
                _fail("NATIVE_ARTIFACT_PYTHON_WHEEL_INVALID")
            data = _read_regular_file(path, maximum_bytes=_MAXIMUM_WHEEL_BYTES)
            total += len(data)
            if total > _MAXIMUM_WHEEL_BYTES:
                _fail("NATIVE_ARTIFACT_PYTHON_WHEEL_INVALID")
            payloads[name] = _sha256_bytes(data)
    return dict(sorted(payloads.items()))


def _build_python_wheel(
    snapshot: Path,
    workspace: Path,
    *,
    environment: Mapping[str, str],
    profile: _ArtifactBuildProfile,
    generated_python_sha256: str,
    generated_meta_python_sha256: str | None,
    generated_source_hashes: tuple[tuple[Path, str], ...],
) -> tuple[Path, dict[str, object]]:
    selected_profile = _require_artifact_build_profile(profile)
    if (
        sys.implementation.name != "cpython"
        or tuple(sys.version_info[:3]) != _PINNED_PYTHON_VERSION
    ):
        _fail("NATIVE_ARTIFACT_PYTHON_TOOLCHAIN_MISMATCH")
    _require_unchanged_generated_sources(generated_source_hashes)
    version = _run_tool([_UV, "--version"], cwd=workspace, environment=environment)
    if version.stdout.strip() != _PINNED_UV_VERSION.encode() or version.stderr:
        _fail("NATIVE_ARTIFACT_PYTHON_TOOLCHAIN_MISMATCH")
    _require_unchanged_generated_sources(generated_source_hashes)
    payloads = _snapshot_python_payloads(snapshot)
    destination = workspace / "python-wheel"
    _make_private_directory(destination)
    _run_tool(
        _python_wheel_command(snapshot, workspace),
        cwd=workspace,
        environment=environment,
        python_build_context=(snapshot, workspace),
    )
    _require_unchanged_generated_sources(generated_source_hashes)
    if {path.name for path in destination.iterdir()} != {_WHEEL_NAME}:
        _fail("NATIVE_ARTIFACT_PYTHON_WHEEL_INVALID")
    wheel = destination / _WHEEL_NAME
    if _snapshot_python_payloads(snapshot) != payloads:
        _fail("NATIVE_ARTIFACT_PYTHON_SOURCE_CHANGED")
    _inspect_python_wheel(
        wheel,
        generated_python_sha256=generated_python_sha256,
        generated_meta_python_sha256=generated_meta_python_sha256,
        expected_payloads=payloads,
        profile=selected_profile,
    )
    python_build: dict[str, object] = {
        "scope": "CONFIGURED_DEVELOPMENT_WHEEL_NOT_INSTALLED_OR_EXECUTED",
        "uv_version": _PINNED_UV_VERSION,
        "python_version": ".".join(map(str, _PINNED_PYTHON_VERSION)),
        "wheel_name": _WHEEL_NAME,
        "generated_member": _GENERATED_PYTHON_WHEEL_MEMBER,
        "build_network": "OFFLINE",
        "build_dependencies": "EXISTING_LOCAL_UV_CACHE_NOT_RELEASE_DEPENDENCY_ATTESTATION",
        "inspection": "BOUNDED_ZIP_READ_ONLY_NO_CONFIGURED_MODULE_IMPORT",
        "source_payloads_sha256": _sha256_bytes(_canonical_json(payloads)),
        "source_payload_count": len(payloads),
        "bridge_source_sha256": payloads["adhelm/_installed_native_approval_bridge.py"],
    }
    if selected_profile is _ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2:
        python_build["meta_campaign_pause_generated_member"] = _GENERATED_META_PYTHON_WHEEL_MEMBER
    return wheel, python_build


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _plist_document(entries: Sequence[tuple[str, str]]) -> bytes:
    body = "".join(f"  <key>{_xml_escape(key)}</key>\n  {value}\n" for key, value in entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"{body}"
        "</dict>\n</plist>\n"
    ).encode()


def _contract_data(contract: _ArtifactContract) -> dict[str, object]:
    try:
        data = contract.to_data()
    except Exception:
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    if type(data) is not dict:
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    return data


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")


def _checked_meta_campaign_pause_artifact_facts(
    profile: _ArtifactBuildProfile,
    facts: _MetaCampaignPauseArtifactFacts | None,
) -> _MetaCampaignPauseArtifactFacts | None:
    selected_profile = _require_artifact_build_profile(profile)
    if selected_profile is _ArtifactBuildProfile.V1:
        if facts is not None:
            _fail("NATIVE_ARTIFACT_METADATA_INVALID")
        return None
    if type(facts) is not _MetaCampaignPauseArtifactFacts:
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    try:
        helper = facts.helper
        target = facts.helper_binary_target
        generated_python_sha256 = facts.generated_python_sha256
        generated_route_sha256 = facts.generated_route_sha256
    except AttributeError:
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    if (
        not isinstance(helper, Path)
        or helper.name != _META_CAMPAIGN_PAUSE_CLIENT_PRODUCT
        or type(target) is not MachOBuildTarget
        or not _is_lower_hex(generated_python_sha256, length=64)
        or not _is_lower_hex(generated_route_sha256, length=64)
        or _read_macho_build_target(helper) != target
    ):
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    return facts


def _stage_unsigned_artifact(
    output: Path,
    *,
    host: Path,
    client: Path,
    profile: _ArtifactBuildProfile,
    meta_campaign_pause_facts: _MetaCampaignPauseArtifactFacts | None,
    python_wheel: Path,
    python_build: dict[str, object],
    contract: _ArtifactContract,
    descriptor: _ArtifactDescriptor,
    descriptor_bytes: bytes,
    source: ResolvedSource,
    source_archive_sha256: str,
    generated_metadata_sha256: str,
    generated_python_sha256: str,
    toolchain: str,
    sdk: PinnedSDKProfile,
    binary_target: MachOBuildTarget,
    client_binary_target: MachOBuildTarget,
    bundle_short_version: str,
    bundle_version: str,
) -> tuple[Path, Path]:
    selected_profile = _require_artifact_build_profile(profile)
    checked_meta_facts = _checked_meta_campaign_pause_artifact_facts(
        selected_profile, meta_campaign_pause_facts
    )
    data = _contract_data(contract)
    try:
        host_bundle_identifier = data["host_bundle_identifier"]
        mach_service_name = data["mach_service_name"]
        access_group = data["signing_key_access_group"]
        descriptor_digest = descriptor.digest
        metadata_digest = descriptor.metadata_digest
        protocol_digest = descriptor.protocol_configuration_digest
        descriptor_data = descriptor.to_data()
    except Exception:
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    if (
        type(host_bundle_identifier) is not str
        or type(mach_service_name) is not str
        or (access_group is not None and type(access_group) is not str)
        or type(descriptor_digest) is not str
        or type(metadata_digest) is not str
        or type(protocol_digest) is not str
        or type(descriptor_data) is not dict
    ):
        _fail("NATIVE_ARTIFACT_METADATA_INVALID")
    app = output / _APP_NAME
    contents = app / "Contents"
    macos = contents / "MacOS"
    launch_agents = contents / "Library" / "LaunchAgents"
    _make_private_directory(app, mode=0o700)
    _make_private_directory(contents, mode=0o700)
    _make_private_directory(macos, mode=0o700)
    _make_private_directory(contents / "Library", mode=0o700)
    _make_private_directory(launch_agents, mode=0o700)
    executable = macos / _EXECUTABLE_NAME
    _write_private_file(
        executable,
        _read_regular_file(host, maximum_bytes=268_435_456),
        mode=0o755,
    )
    client_executable = macos / _CLIENT_PRODUCT
    _write_private_file(
        client_executable,
        _read_regular_file(client, maximum_bytes=_MAXIMUM_ARTIFACT_FILE_BYTES),
        mode=0o755,
    )
    meta_client_executable: Path | None = None
    if checked_meta_facts is not None:
        meta_client_executable = macos / _META_CAMPAIGN_PAUSE_CLIENT_PRODUCT
        _write_private_file(
            meta_client_executable,
            _read_regular_file(
                checked_meta_facts.helper,
                maximum_bytes=_MAXIMUM_ARTIFACT_FILE_BYTES,
            ),
            mode=0o755,
        )
    staged_wheel = output / _WHEEL_NAME
    _write_private_file(
        staged_wheel,
        _read_regular_file(python_wheel, maximum_bytes=_MAXIMUM_WHEEL_BYTES),
        mode=0o644,
    )
    info = _plist_document(
        (
            ("CFBundleExecutable", f"<string>{_xml_escape(_EXECUTABLE_NAME)}</string>"),
            ("CFBundleIdentifier", f"<string>{_xml_escape(host_bundle_identifier)}</string>"),
            ("CFBundleName", "<string>AdHelm Approval Host</string>"),
            ("CFBundlePackageType", "<string>APPL</string>"),
            ("CFBundleShortVersionString", f"<string>{bundle_short_version}</string>"),
            ("CFBundleVersion", f"<string>{bundle_version}</string>"),
        )
    )
    _write_private_file(contents / "Info.plist", info, mode=0o644)
    launch_agent = _plist_document(
        (
            ("Label", f"<string>{_xml_escape(mach_service_name)}</string>"),
            (
                "BundleProgram",
                f"<string>{_xml_escape('Contents/MacOS/' + _EXECUTABLE_NAME)}</string>",
            ),
            (
                "MachServices",
                f"<dict>\n    <key>{_xml_escape(mach_service_name)}</key>\n    <true/>\n  </dict>",
            ),
        )
    )
    _write_private_file(launch_agents / f"{mach_service_name}.plist", launch_agent, mode=0o644)
    components: dict[str, str] = {
        _WHEEL_NAME: _sha256_file(
            staged_wheel,
            maximum_bytes=_MAXIMUM_WHEEL_BYTES,
            code="NATIVE_ARTIFACT_OUTPUT_INVALID",
        ),
        str(client_executable.relative_to(output)): _sha256_file(
            client_executable,
            maximum_bytes=_MAXIMUM_ARTIFACT_FILE_BYTES,
            code="NATIVE_ARTIFACT_OUTPUT_INVALID",
        ),
        str(executable.relative_to(output)): _sha256_file(
            executable,
            maximum_bytes=_MAXIMUM_ARTIFACT_FILE_BYTES,
            code="NATIVE_ARTIFACT_OUTPUT_INVALID",
        ),
        str((contents / "Info.plist").relative_to(output)): _sha256_file(
            contents / "Info.plist",
            maximum_bytes=_MAXIMUM_ARTIFACT_FILE_BYTES,
            code="NATIVE_ARTIFACT_OUTPUT_INVALID",
        ),
        str((launch_agents / f"{mach_service_name}.plist").relative_to(output)): _sha256_file(
            launch_agents / f"{mach_service_name}.plist",
            maximum_bytes=_MAXIMUM_ARTIFACT_FILE_BYTES,
            code="NATIVE_ARTIFACT_OUTPUT_INVALID",
        ),
    }
    if meta_client_executable is not None:
        components[str(meta_client_executable.relative_to(output))] = _sha256_file(
            meta_client_executable,
            maximum_bytes=_MAXIMUM_ARTIFACT_FILE_BYTES,
            code="NATIVE_ARTIFACT_OUTPUT_INVALID",
        )
    if access_group is not None:
        entitlement = _plist_document(
            (
                (
                    "keychain-access-groups",
                    f"<array>\n    <string>{_xml_escape(access_group)}</string>\n  </array>",
                ),
            )
        )
        entitlement_path = output / "AdHelmApprovalHost.entitlements"
        _write_private_file(entitlement_path, entitlement, mode=0o644)
        components[str(entitlement_path.relative_to(output))] = _sha256_file(
            entitlement_path,
            maximum_bytes=_MAXIMUM_ARTIFACT_FILE_BYTES,
            code="NATIVE_ARTIFACT_OUTPUT_INVALID",
        )
    manifest = {
        "schema_version": _BUILD_SCHEMA_VERSION,
        "artifact_status": "UNSIGNED_UNVERIFIED",
        "scope": "UNSIGNED_DEVELOPMENT_ARTIFACT_NOT_INSTALLED_OR_APPROVAL_AUTHORITY",
        "source": {
            "commit": source.commit,
            "git_archive_sha256": source_archive_sha256,
            "source_date_epoch": source.epoch,
            "development_version": "0.1.0",
            "bundle_short_version": bundle_short_version,
            "bundle_version": bundle_version,
        },
        "toolchain": {
            "path": _SWIFT,
            "version": toolchain,
            "version_sha256": _sha256_bytes(toolchain.encode("utf-8")),
            "profile": "DEVELOPMENT_ARM64_FIXED_SWIFT_6_2_3",
            "compiler_default_target": "arm64-apple-macosx26.0",
            "package_deployment_target": _PACKAGE_DEPLOYMENT_TARGET,
            "sdk": sdk.to_data(),
            "effective_binary_target": binary_target.to_data(),
            "effective_client_binary_target": client_binary_target.to_data(),
        },
        "python_build": python_build,
        "metadata": {
            "contract": data,
            "canonical_contract_sha256": _sha256_bytes(descriptor.metadata_canonical_bytes),
            "generated_source_sha256": generated_metadata_sha256,
            "generated_python_source_sha256": generated_python_sha256,
            "native_descriptor": descriptor_data,
            "native_descriptor_sha256": _sha256_bytes(descriptor_bytes),
            "native_descriptor_digest": descriptor_digest,
            "metadata_digest": metadata_digest,
            "protocol_configuration_digest": protocol_digest,
        },
        "components": dict(sorted(components.items())),
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
        "keychain_access_group_template_only": access_group is not None,
    }
    if checked_meta_facts is not None and meta_client_executable is not None:
        manifest["meta_campaign_pause_approval_bridge_v2"] = {
            "profile": selected_profile.value,
            "scope": "EXPLICIT_BUILD_PROFILE_STATIC_BINDING_NOT_INSTALLED_OR_APPROVAL_AUTHORITY",
            "generated_python_binding_sha256": checked_meta_facts.generated_python_sha256,
            "generated_native_route_binding_sha256": checked_meta_facts.generated_route_sha256,
            "metadata_digest": metadata_digest,
            "protocol_configuration_digest": protocol_digest,
            "helper_relative_path": str(meta_client_executable.relative_to(output)),
            "helper_sha256": components[str(meta_client_executable.relative_to(output))],
            "helper_binary_target": checked_meta_facts.helper_binary_target.to_data(),
        }
    _write_private_file(output / _MANIFEST_NAME, _canonical_json(manifest), mode=0o644)
    archive = output / _ARCHIVE_NAME
    create_deterministic_archive(
        output,
        archive=archive,
        epoch=source.epoch,
        profile=selected_profile,
    )
    return archive, output / _MANIFEST_NAME


def create_deterministic_archive(
    output: Path,
    *,
    archive: Path,
    epoch: int,
    profile: _ArtifactBuildProfile = _ArtifactBuildProfile.V1,
) -> None:
    """Archive only controlled staged artifact components with fixed tar metadata."""

    if type(epoch) is not int or isinstance(epoch, bool) or epoch < 0:
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
    selected_profile = _require_artifact_build_profile(profile)
    names = [
        Path(_APP_NAME),
        Path(_APP_NAME) / "Contents",
        Path(_APP_NAME) / "Contents" / "Info.plist",
        Path(_APP_NAME) / "Contents" / "Library",
        Path(_APP_NAME) / "Contents" / "Library" / "LaunchAgents",
        Path(_APP_NAME) / "Contents" / "MacOS",
        Path(_APP_NAME) / "Contents" / "MacOS" / _EXECUTABLE_NAME,
        Path(_APP_NAME) / "Contents" / "MacOS" / _CLIENT_PRODUCT,
        *_optional_entitlement(output),
        Path(_MANIFEST_NAME),
        Path(_WHEEL_NAME),
    ]
    if selected_profile is _ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2:
        names.append(Path(_APP_NAME) / "Contents" / "MacOS" / _META_CAMPAIGN_PAUSE_CLIENT_PRODUCT)
    mach_plists = sorted(
        (output / _APP_NAME / "Contents" / "Library" / "LaunchAgents").glob("*.plist")
    )
    if len(mach_plists) != 1 or mach_plists[0].is_symlink():
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
    names.append(mach_plists[0].relative_to(output))
    names.sort(key=lambda item: item.as_posix())
    descriptor = _open_new_private_file(archive, mode=0o600)
    finished = False
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as archive_file:
            with tarfile.open(fileobj=archive_file, mode="w", format=tarfile.GNU_FORMAT) as bundle:
                for relative in names:
                    path = output / relative
                    value = path.lstat()
                    if stat.S_ISLNK(value.st_mode) or not (
                        stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)
                    ):
                        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
                    if stat.S_ISREG(value.st_mode) and value.st_nlink != 1:
                        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
                    info = tarfile.TarInfo(
                        relative.as_posix() + ("/" if stat.S_ISDIR(value.st_mode) else "")
                    )
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = epoch
                    if stat.S_ISDIR(value.st_mode):
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        bundle.addfile(info)
                    else:
                        info.mode = (
                            0o755
                            if relative.name
                            in (
                                _EXECUTABLE_NAME,
                                _CLIENT_PRODUCT,
                                _META_CAMPAIGN_PAUSE_CLIENT_PRODUCT,
                            )
                            else 0o644
                        )
                        data = _read_regular_file(path, maximum_bytes=268_435_456)
                        if len(data) != value.st_size:
                            _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
                        info.size = len(data)
                        bundle.addfile(info, io.BytesIO(data))
            archive_file.flush()
            os.fsync(descriptor)
        completed = os.fstat(descriptor)
        if not stat.S_ISREG(completed.st_mode) or completed.st_nlink != 1:
            _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
        os.fchmod(descriptor, 0o600)
        selected = archive.lstat()
        if (
            not stat.S_ISREG(selected.st_mode)
            or selected.st_nlink != 1
            or (selected.st_dev, selected.st_ino) != (completed.st_dev, completed.st_ino)
        ):
            _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
        finished = True
    except ArtifactBuildError:
        raise
    except (OSError, tarfile.TarError):
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
    finally:
        if not finished:
            # Remove only the partial regular file created by this invocation.
            # A replacement path or a caller-owned archive is never unlinked.
            try:
                owned = os.fstat(descriptor)
                selected = archive.lstat()
                if (
                    stat.S_ISREG(selected.st_mode)
                    and selected.st_nlink == 1
                    and (selected.st_dev, selected.st_ino) == (owned.st_dev, owned.st_ino)
                ):
                    archive.unlink()
            except OSError:
                pass
        try:
            os.close(descriptor)
        except OSError:
            pass


def _optional_entitlement(output: Path) -> tuple[Path, ...]:
    entitlement = output / "AdHelmApprovalHost.entitlements"
    try:
        value = entitlement.lstat()
    except FileNotFoundError:
        return ()
    except OSError:
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
    if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode) or value.st_nlink != 1:
        _fail("NATIVE_ARTIFACT_OUTPUT_INVALID")
    return (entitlement.relative_to(output),)


def build_native_approval_artifact(
    *,
    repo: Path,
    revision: str,
    specification: Path,
    output: Path,
    source_date_epoch: object = None,
    profile: str = _ArtifactBuildProfile.V1.value,
) -> ArtifactBuildResult:
    """Build and stage one source-bound unsigned development artifact.

    This function never invokes the host executable.  It invokes only Git,
    the fixed Swift executable, and the separately compiled no-argument Core
    inspector; all metadata is validated against the same closed contract
    before final staging.
    """

    selected_profile = _parse_artifact_build_profile(profile)
    contract, descriptor, api = _load_contract(specification)
    staged_output = _preflight_new_output_directory(output)
    # Resolve source before making any output directory so a bad revision does
    # not leave a path that looks like a staged artifact.
    with tempfile.TemporaryDirectory(prefix="adhelm-native-preflight-") as raw_preflight_workspace:
        preflight_workspace = Path(raw_preflight_workspace)
        preflight_environment = _sanitized_environment(preflight_workspace, epoch=0)
        source = resolve_commit(repo, revision, environment=preflight_environment)
    epoch = _checked_epoch(source_date_epoch, source)
    try:
        with tempfile.TemporaryDirectory(prefix="adhelm-native-build-") as raw_workspace:
            workspace = Path(raw_workspace).resolve(strict=True)
            os.chmod(workspace, 0o700)
            environment = _sanitized_environment(workspace, epoch=epoch)
            source_archive = _archive_source(repo, source, workspace, environment=environment)
            snapshot = workspace / "source"
            _make_private_directory(snapshot)
            extract_exact_source(source_archive, snapshot, epoch=epoch)
            _require_archived_builder_provenance(snapshot)
            if selected_profile is _ArtifactBuildProfile.V1:
                _require_unchanged_generated_sources(_v1_meta_default_source_hashes(snapshot))
            bundle_short_version, bundle_version = _development_bundle_versions(snapshot)
            generated_meta_python: str | None = None
            generated_meta_route: str | None = None
            try:
                render = cast(
                    Callable[[_ArtifactContract], str],
                    api.render_generated_approval_host_build_metadata,
                )
                generated = render(contract)
                render_python = cast(
                    Callable[[_ArtifactContract], str],
                    api.render_generated_installed_approval_bridge_binding,
                )
                generated_python = render_python(contract)
                if selected_profile is _ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2:
                    render_meta_python = cast(
                        Callable[[_ArtifactContract], str],
                        api.render_generated_meta_campaign_pause_approval_bridge_binding_v2,
                    )
                    render_meta_route = cast(
                        Callable[[_ArtifactContract], str],
                        api.render_generated_meta_campaign_pause_approval_route_binding_v2,
                    )
                    generated_meta_python = render_meta_python(contract)
                    generated_meta_route = render_meta_route(contract)
            except Exception:
                _fail("NATIVE_ARTIFACT_METADATA_INVALID")
            generated_path = _write_generated_metadata(snapshot, generated)
            generated_python_path = _write_generated_python_binding(snapshot, generated_python)
            generated_meta_python_path: Path | None = None
            generated_meta_route_path: Path | None = None
            if selected_profile is _ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2:
                if generated_meta_python is None or generated_meta_route is None:
                    _fail("NATIVE_ARTIFACT_METADATA_INVALID")
                generated_meta_python_path = _write_generated_meta_python_binding(
                    snapshot, generated_meta_python
                )
                generated_meta_route_path = _write_generated_meta_route_binding(
                    snapshot, generated_meta_route
                )
            generated_python_sha256 = _sha256_file(
                generated_python_path,
                maximum_bytes=_MAXIMUM_SPEC_BYTES,
                code="NATIVE_ARTIFACT_METADATA_INVALID",
            )
            generated_metadata_sha256 = _sha256_file(
                generated_path,
                maximum_bytes=_MAXIMUM_SPEC_BYTES,
                code="NATIVE_ARTIFACT_METADATA_INVALID",
            )
            generated_meta_python_sha256: str | None = None
            generated_meta_route_sha256: str | None = None
            if generated_meta_python_path is not None and generated_meta_route_path is not None:
                generated_meta_python_sha256 = _sha256_file(
                    generated_meta_python_path,
                    maximum_bytes=_MAXIMUM_SPEC_BYTES,
                    code="NATIVE_ARTIFACT_METADATA_INVALID",
                )
                generated_meta_route_sha256 = _sha256_file(
                    generated_meta_route_path,
                    maximum_bytes=_MAXIMUM_SPEC_BYTES,
                    code="NATIVE_ARTIFACT_METADATA_INVALID",
                )
            generated_source_hashes = _profile_generated_source_hashes(
                snapshot,
                profile=selected_profile,
                generated_metadata_sha256=generated_metadata_sha256,
                generated_python_sha256=generated_python_sha256,
                generated_meta_python_sha256=generated_meta_python_sha256,
                generated_meta_route_sha256=generated_meta_route_sha256,
            )
            _require_unchanged_generated_sources(generated_source_hashes)
            (
                host,
                client,
                meta_client,
                descriptor_bytes,
                toolchain,
                sdk,
                binary_target,
                client_binary_target,
                meta_client_binary_target,
            ) = _build_and_inspect(
                snapshot,
                workspace,
                environment=environment,
                expected_descriptor=descriptor,
                profile=selected_profile,
                generated_source_hashes=generated_source_hashes,
            )
            _require_archived_builder_provenance(snapshot)
            _require_unchanged_generated_sources(generated_source_hashes)
            python_wheel, python_build = _build_python_wheel(
                snapshot,
                workspace,
                environment=environment,
                profile=selected_profile,
                generated_python_sha256=generated_python_sha256,
                generated_meta_python_sha256=generated_meta_python_sha256,
                generated_source_hashes=generated_source_hashes,
            )
            _require_unchanged_generated_sources(generated_source_hashes)
            _require_archived_builder_provenance(snapshot)
            meta_campaign_pause_facts: _MetaCampaignPauseArtifactFacts | None = None
            if selected_profile is _ArtifactBuildProfile.META_CAMPAIGN_PAUSE_V2:
                if (
                    meta_client is None
                    or meta_client_binary_target is None
                    or generated_meta_python_sha256 is None
                    or generated_meta_route_sha256 is None
                ):
                    _fail("NATIVE_ARTIFACT_METADATA_INVALID")
                meta_campaign_pause_facts = _MetaCampaignPauseArtifactFacts(
                    helper=meta_client,
                    helper_binary_target=meta_client_binary_target,
                    generated_python_sha256=generated_meta_python_sha256,
                    generated_route_sha256=generated_meta_route_sha256,
                )
            elif meta_client is not None or meta_client_binary_target is not None:
                _fail("NATIVE_ARTIFACT_METADATA_INVALID")
            _require_unchanged_generated_sources(generated_source_hashes)
            target = _create_new_private_directory(staged_output)
            archive, _ = _stage_unsigned_artifact(
                target,
                host=host,
                client=client,
                profile=selected_profile,
                meta_campaign_pause_facts=meta_campaign_pause_facts,
                python_wheel=python_wheel,
                python_build=python_build,
                contract=contract,
                descriptor=descriptor,
                descriptor_bytes=descriptor_bytes,
                source=source,
                source_archive_sha256=_sha256_file(
                    source_archive,
                    maximum_bytes=_MAXIMUM_SOURCE_ARCHIVE_BYTES,
                    code="NATIVE_ARTIFACT_SOURCE_ARCHIVE_INVALID",
                ),
                generated_metadata_sha256=generated_metadata_sha256,
                generated_python_sha256=generated_python_sha256,
                toolchain=toolchain,
                sdk=sdk,
                binary_target=binary_target,
                client_binary_target=client_binary_target,
                bundle_short_version=bundle_short_version,
                bundle_version=bundle_version,
            )
            _require_unchanged_generated_sources(generated_source_hashes)
            try:
                descriptor_digest = descriptor.digest
                metadata_digest = descriptor.metadata_digest
                protocol_digest = descriptor.protocol_configuration_digest
            except Exception:
                _fail("NATIVE_ARTIFACT_METADATA_INVALID")
            return ArtifactBuildResult(
                output_directory=target,
                archive=archive,
                source_commit=source.commit,
                descriptor_digest=descriptor_digest,
                metadata_digest=metadata_digest,
                protocol_configuration_digest=protocol_digest,
                archive_sha256=_sha256_file(
                    archive,
                    maximum_bytes=_MAXIMUM_ARTIFACT_FILE_BYTES,
                    code="NATIVE_ARTIFACT_OUTPUT_INVALID",
                ),
            )
    except ArtifactBuildError:
        raise
    except Exception:
        _fail("NATIVE_ARTIFACT_BUILD_FAILED")


def _argument_epoch(raw: str) -> int:
    try:
        value = int(raw, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("source date epoch must be an integer") from None
    if value < 0:
        raise argparse.ArgumentTypeError("source date epoch must be nonnegative")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-date-epoch", type=_argument_epoch)
    parser.add_argument(
        "--profile",
        choices=tuple(profile.value for profile in _ArtifactBuildProfile),
        default=_ArtifactBuildProfile.V1.value,
    )
    arguments = parser.parse_args()
    try:
        result = build_native_approval_artifact(
            repo=arguments.repo,
            revision=arguments.revision,
            specification=arguments.input,
            output=arguments.output,
            source_date_epoch=arguments.source_date_epoch,
            profile=arguments.profile,
        )
    except ArtifactBuildError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result.to_data(), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
