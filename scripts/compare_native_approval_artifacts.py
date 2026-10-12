"""Compare two local unsigned native-approval artifact directories without executing them.

This is a bounded structural and byte comparison for staged development
artifacts.  It reads only the fixed artifact tree, manifest, and deterministic
tar archive.  Manifest provenance and runtime-status fields remain declarations
made by the artifact; this tool does not run Git, a wheel, a host, a helper, or
any approval flow.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final, Never, cast

_SCHEMA_VERSION: Final = 1
_SCOPE: Final = (
    "LOCAL_UNSIGNED_ARTIFACT_PAIR_COMPARISON_MANIFEST_DECLARATIONS_NOT_"
    "INDEPENDENT_PROVENANCE_OR_RUNTIME_EVIDENCE"
)
_BUILD_SCOPE: Final = "UNSIGNED_DEVELOPMENT_ARTIFACT_NOT_INSTALLED_OR_APPROVAL_AUTHORITY"
_META_SCOPE: Final = "EXPLICIT_BUILD_PROFILE_STATIC_BINDING_NOT_INSTALLED_OR_APPROVAL_AUTHORITY"
_PROFILE_V1: Final = "V1"
_PROFILE_META: Final = "META_CAMPAIGN_PAUSE_V2"
_APP_NAME: Final = "AdHelmApprovalHost.app"
_ARCHIVE_NAME: Final = "adhelm-macos-installed-approval-host-unsigned.tar"
_MANIFEST_NAME: Final = "manifest.json"
_WHEEL_NAME: Final = "adhelm-0.1.0-py3-none-any.whl"
_HOST_PRODUCT: Final = "adhelm-macos-installed-approval-host"
_CLIENT_PRODUCT: Final = "adhelm-macos-approval-client"
_META_CLIENT_PRODUCT: Final = "adhelm-macos-meta-campaign-pause-approval-client"
_MAXIMUM_MANIFEST_BYTES: Final = 1_048_576
_MAXIMUM_COMPONENT_BYTES: Final = 268_435_456
_MAXIMUM_ARCHIVE_MEMBERS: Final = 16
_MAXIMUM_DIRECTORY_ENTRIES: Final = 16
_CHUNK_BYTES: Final = 65_536
_HEX: Final = frozenset("0123456789abcdef")

_MANIFEST_KEYS: Final = frozenset(
    {
        "schema_version",
        "artifact_status",
        "scope",
        "source",
        "toolchain",
        "python_build",
        "metadata",
        "components",
        "developer_identity_signed",
        "developer_identity_signing_status",
        "compiler_signature_status",
        "notarized",
        "registered",
        "registration_status",
        "identity_authorized",
        "identity_authorization_status",
        "host_started",
        "client_started",
        "keychain_access_group_template_only",
    }
)
_META_MANIFEST_KEY: Final = "meta_campaign_pause_approval_bridge_v2"
_SOURCE_KEYS: Final = frozenset(
    {
        "commit",
        "git_archive_sha256",
        "source_date_epoch",
        "development_version",
        "bundle_short_version",
        "bundle_version",
    }
)
_METADATA_KEYS: Final = frozenset(
    {
        "contract",
        "canonical_contract_sha256",
        "generated_source_sha256",
        "generated_python_source_sha256",
        "native_descriptor",
        "native_descriptor_sha256",
        "native_descriptor_digest",
        "metadata_digest",
        "protocol_configuration_digest",
    }
)
_META_KEYS: Final = frozenset(
    {
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
)
_TARGET_KEYS: Final = frozenset({"architecture", "platform", "minimum_os_version", "sdk_version"})


def _load_contract_module() -> ModuleType:
    """Load the adjacent closed contract parser, never artifact-supplied code."""
    path = Path(__file__).with_name("native_approval_artifact_contract.py")
    spec = importlib.util.spec_from_file_location("_adhelm_artifact_pair_contract", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("The adjacent native artifact contract parser is unavailable.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CONTRACT = _load_contract_module()


class NativeApprovalArtifactComparisonError(ValueError):
    """One fixed local comparison failure without an artifact-path echo."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _FileFacts:
    sha256: str
    bytes: int
    mode: int
    identity: tuple[int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _ArtifactLayout:
    profile: str
    directories: tuple[tuple[str, ...], ...]
    files: tuple[tuple[str, ...], ...]
    components: tuple[tuple[str, ...], ...]
    archive_members: tuple[tuple[str, ...], ...]
    executable_files: frozenset[tuple[str, ...]]
    source: dict[str, object]
    configuration: dict[str, object]
    manifest_assertions: dict[str, object]


@dataclass(slots=True)
class _OpenedArtifact:
    path: Path
    root_descriptor: int
    root_identity: tuple[int, int, int, int, int]
    manifest_bytes: bytes
    manifest: dict[str, object]
    layout: _ArtifactLayout
    files: dict[tuple[str, ...], _FileFacts]
    archive_headers: tuple[dict[str, object], ...]

    def close(self) -> None:
        os.close(self.root_descriptor)


def _fail(code: str) -> Never:
    raise NativeApprovalArtifactComparisonError(code)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        _fail("NATIVE_ARTIFACT_PAIR_PLATFORM_UNSUPPORTED")
    return os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)


def _read_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not nonblock:
        _fail("NATIVE_ARTIFACT_PAIR_PLATFORM_UNSUPPORTED")
    return os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0)


def _require_absolute_directory_path(path: object) -> Path:
    if not isinstance(path, Path):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    checked = path
    if not checked.is_absolute() or not checked.name or ".." in checked.parts:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    return checked


def _open_absolute_directory(path: Path) -> tuple[int, tuple[int, int, int, int, int]]:
    checked = _require_absolute_directory_path(path)
    descriptor: int | None = None
    try:
        descriptor = os.open("/", _directory_flags())
        for part in checked.parts[1:]:
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        value = os.fstat(descriptor)
        if not stat.S_ISDIR(value.st_mode):
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
        return descriptor, _identity(value)
    except NativeApprovalArtifactComparisonError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")


def _verify_root_identity(
    path: Path, descriptor: int, expected: tuple[int, int, int, int, int]
) -> None:
    try:
        selected = path.lstat()
        opened = os.fstat(descriptor)
    except OSError:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
    if (
        stat.S_ISLNK(selected.st_mode)
        or not stat.S_ISDIR(selected.st_mode)
        or _identity(selected) != expected
        or _identity(opened) != expected
    ):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")


def _verify_directory_location(
    path: Path, descriptor: int, expected: tuple[int, int, int, int, int]
) -> None:
    """Check a held directory was not replaced while allowing its own writes."""
    try:
        selected = path.lstat()
        opened = os.fstat(descriptor)
    except OSError:
        _fail("NATIVE_ARTIFACT_PAIR_OUTPUT_INVALID")
    if (
        stat.S_ISLNK(selected.st_mode)
        or not stat.S_ISDIR(selected.st_mode)
        or (selected.st_dev, selected.st_ino) != expected[:2]
        or (opened.st_dev, opened.st_ino) != expected[:2]
    ):
        _fail("NATIVE_ARTIFACT_PAIR_OUTPUT_INVALID")


def _check_parts(parts: tuple[str, ...]) -> None:
    if not parts or any(not item or item in {".", ".."} or "/" in item for item in parts):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")


def _open_relative_directory(root_descriptor: int, parts: tuple[str, ...]) -> int:
    _check_parts(parts)
    try:
        current = os.dup(root_descriptor)
    except OSError:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    try:
        for part in parts:
            child = os.open(part, _directory_flags(), dir_fd=current)
            os.close(current)
            current = child
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
        return current
    except NativeApprovalArtifactComparisonError:
        os.close(current)
        raise
    except OSError:
        os.close(current)
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")


def _open_relative_regular(
    root_descriptor: int, parts: tuple[str, ...], *, maximum_bytes: int
) -> tuple[int, os.stat_result]:
    _check_parts(parts)
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    try:
        parent = os.dup(root_descriptor)
    except OSError:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    descriptor: int | None = None
    try:
        for part in parts[:-1]:
            child = os.open(part, _directory_flags(), dir_fd=parent)
            os.close(parent)
            parent = child
        selected = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(selected.st_mode)
            or stat.S_ISLNK(selected.st_mode)
            or selected.st_nlink != 1
            or selected.st_size < 0
            or selected.st_size > maximum_bytes
        ):
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
        descriptor = os.open(parts[-1], _read_flags(), dir_fd=parent)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(selected)
        ):
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
        return descriptor, opened
    except NativeApprovalArtifactComparisonError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    finally:
        os.close(parent)


def _read_relative_bytes(
    root_descriptor: int, parts: tuple[str, ...], *, maximum_bytes: int
) -> tuple[bytes, _FileFacts]:
    descriptor, before = _open_relative_regular(root_descriptor, parts, maximum_bytes=maximum_bytes)
    try:
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(_CHUNK_BYTES, remaining))
            if not chunk:
                _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
        after = os.fstat(descriptor)
    except NativeApprovalArtifactComparisonError:
        raise
    except OSError:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
    finally:
        os.close(descriptor)
    if _identity(after) != _identity(before):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
    data = b"".join(chunks)
    return data, _FileFacts(
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        mode=stat.S_IMODE(before.st_mode),
        identity=_identity(before),
    )


def _hash_relative_file(
    root_descriptor: int, parts: tuple[str, ...], *, maximum_bytes: int
) -> _FileFacts:
    descriptor, before = _open_relative_regular(root_descriptor, parts, maximum_bytes=maximum_bytes)
    try:
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(_CHUNK_BYTES, remaining))
            if not chunk:
                _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
        after = os.fstat(descriptor)
    except NativeApprovalArtifactComparisonError:
        raise
    except OSError:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
    finally:
        os.close(descriptor)
    if _identity(after) != _identity(before):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
    return _FileFacts(
        sha256=digest.hexdigest(),
        bytes=before.st_size,
        mode=stat.S_IMODE(before.st_mode),
        identity=_identity(before),
    )


def _json_pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> object:
    raise ValueError("non-finite JSON constant")


def _decode_manifest(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, TypeError, ValueError, RecursionError):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    return cast(dict[str, object], value)


def _mapping(value: object, *, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    mapped = cast(dict[str, object], value)
    if set(mapped) != keys:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    return mapped


def _string(value: object, *, maximum_bytes: int = 1_024) -> str:
    if type(value) is not str:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    text = value
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    if (
        not text
        or len(encoded) > maximum_bytes
        or any(ord(item) < 32 or ord(item) == 127 for item in text)
    ):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    return text


def _lower_hex(value: object, *, length: int) -> str:
    text = _string(value, maximum_bytes=length)
    if len(text) != length or any(character not in _HEX for character in text):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    return text


def _exact_int(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    result = value
    if result < minimum or result > maximum:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    return result


def _checked_source(value: object) -> dict[str, object]:
    source = _mapping(value, keys=_SOURCE_KEYS)
    _lower_hex(source["commit"], length=40)
    _lower_hex(source["git_archive_sha256"], length=64)
    _exact_int(source["source_date_epoch"], minimum=0, maximum=9_223_372_036_854_775_807)
    if _string(source["development_version"], maximum_bytes=64) != "0.1.0":
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    _string(source["bundle_short_version"], maximum_bytes=64)
    _string(source["bundle_version"], maximum_bytes=64)
    return source


def _checked_target(value: object) -> dict[str, object]:
    target = _mapping(value, keys=_TARGET_KEYS)
    for item in target.values():
        _string(item, maximum_bytes=64)
    return target


def _strictly_equal(left: object, right: object) -> bool:
    """Compare JSON-shaped values without accepting numeric aliases."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        assert isinstance(left, dict) and isinstance(right, dict)
        return set(left) == set(right) and all(
            _strictly_equal(left[key], right[key]) for key in left
        )
    if type(left) is list:
        assert isinstance(left, list) and isinstance(right, list)
        return len(left) == len(right) and all(
            _strictly_equal(item, expected) for item, expected in zip(left, right, strict=True)
        )
    return left == right


def _checked_metadata(value: object) -> tuple[dict[str, object], dict[str, object]]:
    """Rebuild full18 metadata and descriptor digests through the source parser."""
    metadata = _mapping(value, keys=_METADATA_KEYS)
    try:
        contract = _CONTRACT.parse_native_approval_artifact_contract(metadata["contract"])
        descriptor = _CONTRACT.native_approval_artifact_descriptor(contract)
        canonical_contract = cast(
            bytes, _CONTRACT.canonical_native_approval_artifact_contract(contract)
        )
        checked_contract = cast(dict[str, object], contract.to_data())
        checked_descriptor = cast(dict[str, object], descriptor.to_data())
        metadata_digest = cast(str, descriptor.metadata_digest)
        protocol_digest = cast(str, descriptor.protocol_configuration_digest)
        descriptor_digest = cast(str, descriptor.digest)
    except (AttributeError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    if (
        not _strictly_equal(metadata["contract"], checked_contract)
        or not _strictly_equal(metadata["native_descriptor"], checked_descriptor)
        or _lower_hex(metadata["canonical_contract_sha256"], length=64)
        != hashlib.sha256(canonical_contract).hexdigest()
        or _lower_hex(metadata["metadata_digest"], length=64) != metadata_digest
        or _lower_hex(metadata["protocol_configuration_digest"], length=64) != protocol_digest
        or _lower_hex(metadata["native_descriptor_sha256"], length=64) != descriptor_digest
        or _lower_hex(metadata["native_descriptor_digest"], length=64) != descriptor_digest
    ):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    _lower_hex(metadata["generated_source_sha256"], length=64)
    _lower_hex(metadata["generated_python_source_sha256"], length=64)
    return metadata, checked_contract


def _expected_mode(parts: tuple[str, ...]) -> int:
    if parts == (_ARCHIVE_NAME,):
        return 0o600
    if parts[-1] in {_HOST_PRODUCT, _CLIENT_PRODUCT, _META_CLIENT_PRODUCT}:
        return 0o755
    return 0o644


def _checked_manifest(manifest: dict[str, object]) -> _ArtifactLayout:
    keys = set(manifest)
    meta_present = _META_MANIFEST_KEY in keys
    expected_keys = _MANIFEST_KEYS | ({_META_MANIFEST_KEY} if meta_present else set())
    if keys != expected_keys:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    if (
        _exact_int(manifest["schema_version"], minimum=1, maximum=1) != 1
        or manifest["artifact_status"] != "UNSIGNED_UNVERIFIED"
        or manifest["scope"] != _BUILD_SCOPE
    ):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    source = _checked_source(manifest["source"])
    metadata, contract = _checked_metadata(manifest["metadata"])
    if not isinstance(manifest["toolchain"], dict) or not isinstance(
        manifest["python_build"], dict
    ):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    assertions: dict[str, object] = {
        "artifact_status": "UNSIGNED_UNVERIFIED",
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
    }
    for key, expected in assertions.items():
        if manifest[key] != expected or type(manifest[key]) is not type(expected):
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    access_group = contract["signing_key_access_group"]
    if type(manifest["keychain_access_group_template_only"]) is not bool or manifest[
        "keychain_access_group_template_only"
    ] is not (access_group is not None):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    assertions["artifact_manifest_scope"] = manifest["scope"]
    assertions["keychain_access_group_template_only"] = access_group is not None
    mach_service = contract["mach_service_name"]
    if type(mach_service) is not str:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    launch_agent = (
        _APP_NAME,
        "Contents",
        "Library",
        "LaunchAgents",
        f"{mach_service}.plist",
    )
    components = {
        (_APP_NAME, "Contents", "Info.plist"),
        launch_agent,
        (_APP_NAME, "Contents", "MacOS", _HOST_PRODUCT),
        (_APP_NAME, "Contents", "MacOS", _CLIENT_PRODUCT),
        (_WHEEL_NAME,),
    }
    profile = _PROFILE_V1
    if access_group is not None:
        components.add(("AdHelmApprovalHost.entitlements",))
    if meta_present:
        profile = _PROFILE_META
        meta = _mapping(manifest[_META_MANIFEST_KEY], keys=_META_KEYS)
        helper = (_APP_NAME, "Contents", "MacOS", _META_CLIENT_PRODUCT)
        if (
            meta["profile"] != _PROFILE_META
            or meta["scope"] != _META_SCOPE
            or meta["helper_relative_path"] != "/".join(helper)
            or _lower_hex(meta["generated_python_binding_sha256"], length=64) == ""
            or _lower_hex(meta["generated_native_route_binding_sha256"], length=64) == ""
            or _lower_hex(meta["metadata_digest"], length=64) != metadata["metadata_digest"]
            or _lower_hex(meta["protocol_configuration_digest"], length=64)
            != metadata["protocol_configuration_digest"]
        ):
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
        helper_target = _checked_target(meta["helper_binary_target"])
        toolchain = cast(dict[str, object], manifest["toolchain"])
        if _checked_target(toolchain.get("effective_client_binary_target")) != helper_target:
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
        components.add(helper)
    raw_components_value = manifest["components"]
    if not isinstance(raw_components_value, dict) or any(
        type(key) is not str for key in raw_components_value
    ):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    raw_components = cast(dict[str, object], raw_components_value)
    expected_component_names = {"/".join(item) for item in components}
    if set(raw_components) != expected_component_names:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    for relative in components:
        digest = _lower_hex(raw_components["/".join(relative)], length=64)
        if profile == _PROFILE_META and relative[-1] == _META_CLIENT_PRODUCT:
            meta = cast(dict[str, object], manifest[_META_MANIFEST_KEY])
            if _lower_hex(meta["helper_sha256"], length=64) != digest:
                _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    directories = (
        (_APP_NAME,),
        (_APP_NAME, "Contents"),
        (_APP_NAME, "Contents", "Library"),
        (_APP_NAME, "Contents", "Library", "LaunchAgents"),
        (_APP_NAME, "Contents", "MacOS"),
    )
    files = tuple(sorted((*components, (_ARCHIVE_NAME,), (_MANIFEST_NAME,))))
    archive_members = tuple(sorted((*directories, *components, (_MANIFEST_NAME,))))
    return _ArtifactLayout(
        profile=profile,
        directories=directories,
        files=files,
        components=tuple(sorted(components)),
        archive_members=archive_members,
        executable_files=frozenset(
            item
            for item in components
            if item[-1] in {_HOST_PRODUCT, _CLIENT_PRODUCT, _META_CLIENT_PRODUCT}
        ),
        source=source,
        configuration={
            "metadata_digest": metadata["metadata_digest"],
            "protocol_configuration_digest": metadata["protocol_configuration_digest"],
            "native_descriptor_digest": metadata["native_descriptor_digest"],
            "signing_key_access_group_declared": access_group is not None,
        },
        manifest_assertions=assertions,
    )


def _expected_children(layout: _ArtifactLayout, directory: tuple[str, ...]) -> set[str]:
    children: set[str] = set()
    for child in layout.directories:
        if child[:-1] == directory:
            children.add(child[-1])
    for child in layout.files:
        if child[:-1] == directory:
            children.add(child[-1])
    return children


def _check_directory_entries(root_descriptor: int, layout: _ArtifactLayout) -> None:
    for directory in ((), *layout.directories):
        descriptor: int | None = None
        try:
            descriptor = (
                os.dup(root_descriptor)
                if not directory
                else _open_relative_directory(root_descriptor, directory)
            )
            expected = _expected_children(layout, directory)
            names: set[str] = set()
            with os.scandir(descriptor) as iterator:
                for item in iterator:
                    if len(names) >= _MAXIMUM_DIRECTORY_ENTRIES:
                        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
                    names.add(item.name)
            if names != expected:
                _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
        except NativeApprovalArtifactComparisonError:
            raise
        except OSError:
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _compare_tar_member_to_staged(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    root_descriptor: int,
    parts: tuple[str, ...],
    expected: _FileFacts,
) -> str:
    selected = archive.extractfile(member)
    if selected is None:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    descriptor: int | None = None
    before: os.stat_result | None = None
    after: os.stat_result | None = None
    try:
        descriptor, before = _open_relative_regular(
            root_descriptor, parts, maximum_bytes=_MAXIMUM_COMPONENT_BYTES
        )
        if _identity(before) != expected.identity:
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
        staged_digest = hashlib.sha256()
        tar_digest = hashlib.sha256()
        remaining = member.size
        while remaining:
            wanted = min(_CHUNK_BYTES, remaining)
            archive_chunk = selected.read(wanted)
            staged_chunk = os.read(descriptor, wanted)
            if (
                len(archive_chunk) != wanted
                or len(staged_chunk) != wanted
                or archive_chunk != staged_chunk
            ):
                _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
            tar_digest.update(archive_chunk)
            staged_digest.update(staged_chunk)
            remaining -= wanted
        if selected.read(1) or os.read(descriptor, 1):
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
        after = os.fstat(descriptor)
    except NativeApprovalArtifactComparisonError:
        raise
    except (OSError, tarfile.TarError):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    finally:
        selected.close()
        if descriptor is not None:
            os.close(descriptor)
    if before is None or after is None:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
    if _identity(after) != _identity(before):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
    if staged_digest.hexdigest() != expected.sha256:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
    return tar_digest.hexdigest()


def _inspect_archive(
    root_descriptor: int, layout: _ArtifactLayout, files: Mapping[tuple[str, ...], _FileFacts]
) -> tuple[dict[str, object], ...]:
    archive_path = (_ARCHIVE_NAME,)
    archive_facts = files[archive_path]
    descriptor, before = _open_relative_regular(
        root_descriptor, archive_path, maximum_bytes=_MAXIMUM_COMPONENT_BYTES
    )
    if _identity(before) != archive_facts.identity:
        os.close(descriptor)
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
    if archive_facts.bytes % tarfile.RECORDSIZE:
        os.close(descriptor)
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    headers: list[dict[str, object]] = []
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            with tarfile.open(fileobj=handle, mode="r:") as archive:
                if len(layout.archive_members) > _MAXIMUM_ARCHIVE_MEMBERS:
                    _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
                for expected_parts in layout.archive_members:
                    member = archive.next()
                    if member is None:
                        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
                    expected_name = "/".join(expected_parts)
                    expected_directory = expected_parts in layout.directories
                    expected_mode = (
                        0o755
                        if expected_directory or expected_parts in layout.executable_files
                        else 0o644
                    )
                    if (
                        member.name != expected_name
                        or member.uid != 0
                        or member.gid != 0
                        or member.mtime != layout.source["source_date_epoch"]
                        or member.mode != expected_mode
                        or member.uname != ""
                        or member.gname != ""
                        or member.linkname != ""
                        or member.pax_headers
                        or member.devmajor != 0
                        or member.devminor != 0
                    ):
                        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
                    if expected_directory:
                        if not member.isdir() or member.size != 0:
                            _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
                        headers.append(
                            {
                                "name": expected_name,
                                "kind": "directory",
                                "mode": expected_mode,
                                "uid": 0,
                                "gid": 0,
                                "mtime": member.mtime,
                                "bytes": 0,
                            }
                        )
                    else:
                        facts = files.get(expected_parts)
                        if facts is None or not member.isreg() or member.size != facts.bytes:
                            _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
                        digest = _compare_tar_member_to_staged(
                            archive, member, root_descriptor, expected_parts, facts
                        )
                        if digest != facts.sha256:
                            _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
                        headers.append(
                            {
                                "name": expected_name,
                                "kind": "regular",
                                "mode": expected_mode,
                                "uid": 0,
                                "gid": 0,
                                "mtime": member.mtime,
                                "bytes": facts.bytes,
                                "sha256": digest,
                            }
                        )
                if archive.next() is not None:
                    _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
                while tail := handle.read(_CHUNK_BYTES):
                    if any(tail):
                        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
        after = os.fstat(descriptor)
    except NativeApprovalArtifactComparisonError:
        raise
    except (OSError, tarfile.TarError):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    finally:
        os.close(descriptor)
    if _identity(after) != _identity(before):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
    return tuple(headers)


def _inspect_artifact(path: Path) -> _OpenedArtifact:
    root_descriptor, root_identity = _open_absolute_directory(path)
    try:
        manifest_bytes, manifest_facts = _read_relative_bytes(
            root_descriptor, (_MANIFEST_NAME,), maximum_bytes=_MAXIMUM_MANIFEST_BYTES
        )
        manifest = _decode_manifest(manifest_bytes)
        layout = _checked_manifest(manifest)
        _check_directory_entries(root_descriptor, layout)
        files: dict[tuple[str, ...], _FileFacts] = {}
        for parts in layout.files:
            facts = (
                manifest_facts
                if parts == (_MANIFEST_NAME,)
                else _hash_relative_file(
                    root_descriptor, parts, maximum_bytes=_MAXIMUM_COMPONENT_BYTES
                )
            )
            if facts.mode != _expected_mode(parts):
                _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
            files[parts] = facts
        components = manifest["components"]
        assert type(components) is dict
        for parts in layout.components:
            if files[parts].sha256 != components["/".join(parts)]:
                _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
        headers = _inspect_archive(root_descriptor, layout, files)
        _verify_root_identity(path, root_descriptor, root_identity)
        return _OpenedArtifact(
            path=path,
            root_descriptor=root_descriptor,
            root_identity=root_identity,
            manifest_bytes=manifest_bytes,
            manifest=manifest,
            layout=layout,
            files=files,
            archive_headers=headers,
        )
    except BaseException:
        os.close(root_descriptor)
        raise


def _compare_regular_files(
    first: _OpenedArtifact, second: _OpenedArtifact, parts: tuple[str, ...]
) -> None:
    first_facts = first.files[parts]
    second_facts = second.files[parts]
    if (
        first_facts.sha256 != second_facts.sha256
        or first_facts.bytes != second_facts.bytes
        or first_facts.mode != second_facts.mode
    ):
        _fail("NATIVE_ARTIFACT_PAIR_MISMATCH")
    first_descriptor: int | None = None
    second_descriptor: int | None = None
    first_before: os.stat_result | None = None
    second_before: os.stat_result | None = None
    first_after: os.stat_result | None = None
    second_after: os.stat_result | None = None
    try:
        first_descriptor, first_before = _open_relative_regular(
            first.root_descriptor, parts, maximum_bytes=_MAXIMUM_COMPONENT_BYTES
        )
        second_descriptor, second_before = _open_relative_regular(
            second.root_descriptor, parts, maximum_bytes=_MAXIMUM_COMPONENT_BYTES
        )
        if (
            _identity(first_before) != first_facts.identity
            or _identity(second_before) != second_facts.identity
        ):
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
        remaining = first_facts.bytes
        while remaining:
            wanted = min(_CHUNK_BYTES, remaining)
            first_chunk = os.read(first_descriptor, wanted)
            second_chunk = os.read(second_descriptor, wanted)
            if (
                len(first_chunk) != wanted
                or len(second_chunk) != wanted
                or first_chunk != second_chunk
            ):
                _fail("NATIVE_ARTIFACT_PAIR_MISMATCH")
            remaining -= wanted
        if os.read(first_descriptor, 1) or os.read(second_descriptor, 1):
            _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
        first_after = os.fstat(first_descriptor)
        second_after = os.fstat(second_descriptor)
    except NativeApprovalArtifactComparisonError:
        raise
    except OSError:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
    finally:
        if first_descriptor is not None:
            os.close(first_descriptor)
        if second_descriptor is not None:
            os.close(second_descriptor)
    if first_before is None or second_before is None or first_after is None or second_after is None:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
    if _identity(first_after) != _identity(first_before) or _identity(second_after) != _identity(
        second_before
    ):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")


def _checked_pair(first: _OpenedArtifact, second: _OpenedArtifact) -> None:
    _verify_root_identity(first.path, first.root_descriptor, first.root_identity)
    _verify_root_identity(second.path, second.root_descriptor, second.root_identity)
    if first.root_identity[:2] == second.root_identity[:2]:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    if (
        first.layout.profile != second.layout.profile
        or first.manifest_bytes != second.manifest_bytes
        or first.layout.files != second.layout.files
        or first.layout.archive_members != second.layout.archive_members
        or first.archive_headers != second.archive_headers
    ):
        _fail("NATIVE_ARTIFACT_PAIR_MISMATCH")
    for parts in first.layout.files:
        _compare_regular_files(first, second, parts)
    _verify_root_identity(first.path, first.root_descriptor, first.root_identity)
    _verify_root_identity(second.path, second.root_descriptor, second.root_identity)


def _report(first: _OpenedArtifact) -> dict[str, object]:
    archive_parts = (_ARCHIVE_NAME,)
    archive = first.files[archive_parts]
    manifest = first.files[(_MANIFEST_NAME,)]
    files = {
        "/".join(parts): {
            "sha256": facts.sha256,
            "bytes": facts.bytes,
            "mode": facts.mode,
            "identical": True,
        }
        for parts, facts in sorted(first.files.items())
    }
    return {
        "schema_version": _SCHEMA_VERSION,
        "scope": _SCOPE,
        "profile": first.layout.profile,
        "source": first.layout.source,
        "configuration": first.layout.configuration,
        "manifest": {
            "sha256": manifest.sha256,
            "bytes": manifest.bytes,
            "identical": True,
        },
        "files": files,
        "archive": {
            "path": _ARCHIVE_NAME,
            "sha256": archive.sha256,
            "bytes": archive.bytes,
            "identical": True,
            "member_count": len(first.archive_headers),
            "headers": list(first.archive_headers),
        },
        "manifest_assertions": first.layout.manifest_assertions,
    }


def _canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                value, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")


def _write_new_report(path: Path, data: bytes) -> None:
    if not isinstance(path, Path) or not path.is_absolute() or not path.name or ".." in path.parts:
        _fail("NATIVE_ARTIFACT_PAIR_OUTPUT_INVALID")
    if not path.anchor:
        _fail("NATIVE_ARTIFACT_PAIR_OUTPUT_INVALID")
    parent = path.parent
    descriptor: int | None = None
    output: int | None = None
    try:
        descriptor, parent_identity = _open_absolute_directory(parent)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        output = os.open(path.name, flags, 0o600, dir_fd=descriptor)
        before = os.fstat(output)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _fail("NATIVE_ARTIFACT_PAIR_OUTPUT_INVALID")
        offset = 0
        while offset < len(data):
            written = os.write(output, data[offset:])
            if written <= 0:
                _fail("NATIVE_ARTIFACT_PAIR_OUTPUT_INVALID")
            offset += written
        os.fsync(output)
        after = os.fstat(output)
        selected = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or after.st_size != len(data)
            or _identity(after) != _identity(selected)
        ):
            _fail("NATIVE_ARTIFACT_PAIR_OUTPUT_INVALID")
        _verify_directory_location(parent, descriptor, parent_identity)
    except NativeApprovalArtifactComparisonError:
        raise
    except FileExistsError:
        _fail("NATIVE_ARTIFACT_PAIR_OUTPUT_EXISTS")
    except OSError:
        _fail("NATIVE_ARTIFACT_PAIR_OUTPUT_INVALID")
    finally:
        if output is not None:
            os.close(output)
        if descriptor is not None:
            os.close(descriptor)


def compare_native_approval_artifacts(
    first: Path, second: Path, *, report_path: Path | None = None
) -> dict[str, object]:
    """Return deterministic evidence that two bounded staged artifacts match.

    `first` and `second` must be separate, absolute, non-symlink artifact
    directories.  When supplied, `report_path` must be a new absolute file
    outside both trees; the function never overwrites it.
    """
    first_path = _require_absolute_directory_path(first)
    second_path = _require_absolute_directory_path(second)
    if first_path == second_path:
        _fail("NATIVE_ARTIFACT_PAIR_INPUT_INVALID")
    if report_path is not None:
        if not isinstance(report_path, Path) or not report_path.is_absolute():
            _fail("NATIVE_ARTIFACT_PAIR_OUTPUT_INVALID")
        try:
            report_path.relative_to(first_path)
        except ValueError:
            pass
        else:
            _fail("NATIVE_ARTIFACT_PAIR_OUTPUT_INVALID")
        try:
            report_path.relative_to(second_path)
        except ValueError:
            pass
        else:
            _fail("NATIVE_ARTIFACT_PAIR_OUTPUT_INVALID")
    opened_first = _inspect_artifact(first_path)
    try:
        opened_second = _inspect_artifact(second_path)
        try:
            _checked_pair(opened_first, opened_second)
            result = _report(opened_first)
            if report_path is not None:
                _write_new_report(report_path, _canonical_json(result))
            return result
        finally:
            opened_second.close()
    finally:
        opened_first.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local comparator without importing or executing an artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--second", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = compare_native_approval_artifacts(
            arguments.first, arguments.second, report_path=arguments.report
        )
    except NativeApprovalArtifactComparisonError as error:
        print(error.code, file=sys.stderr)
        return 2
    sys.stdout.buffer.write(_canonical_json(result))
    return 0


if __name__ == "__main__":  # pragma: no cover - direct script entry point
    raise SystemExit(main())
