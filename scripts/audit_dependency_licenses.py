#!/usr/bin/env python3
"""Create a bounded local inventory of license evidence for an exact uv lock.

The inventory is intentionally evidentiary.  It records exact lock versions and
artifact hashes, then records installed distribution metadata and hashes of
declared license-related files.  It never decides whether a dependency is
legally compatible, approved, or suitable for a release.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import sys
import tomllib
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT_NAME = "adhelm"
MAX_LOCK_BYTES = 4 * 1024 * 1024
MAX_LOCK_PACKAGES = 1_024
MAX_LOCK_ARTIFACTS_PER_PACKAGE = 256
MAX_DEPENDENCIES_PER_PACKAGE = 256
MAX_OPTIONAL_DEPENDENCY_GROUPS = 64
MAX_EXTRAS_PER_DEPENDENCY = 64
MAX_METADATA_VALUE_BYTES = 512
MAX_CLASSIFIERS = 128
MAX_DISTRIBUTION_FILE_RECORDS = 100_000
MAX_LICENSE_RECORD_PATH_BYTES = 1_024
MAX_LICENSE_FILES = 64
MAX_LICENSE_FILE_BYTES = 1 * 1024 * 1024
MAX_TOTAL_LICENSE_FILE_BYTES = 4 * 1024 * 1024
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EXTRA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_LICENSE_FILE_RE = re.compile(
    r"(?:^|[._-])(?:licen[cs]e|copying|notice|copyright|unlicense)(?:$|[._-])",
    re.IGNORECASE,
)

# These are deliberately a small, closed set of exact classifiers.  A broad
# classifier such as ``BSD License`` is not a unique SPDX expression and is
# left for review.  This map does not make a compatibility or approval decision.
EXACT_TROVE_CLASSIFIER_TO_SPDX: Mapping[str, str] = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "Python-2.0",
    "License :: OSI Approved :: Zope Public License": "ZPL-2.0",
}


class InventoryError(RuntimeError):
    """A closed-input or safe-output error from this local scanner."""


class LockFormatError(InventoryError):
    """The lock cannot be represented by this bounded scanner."""


class OutputPathError(InventoryError):
    """The selected output path is not a safe new regular file."""


@dataclass(frozen=True)
class LockedDependency:
    name: str
    marker: str | None
    extras: tuple[str, ...]


@dataclass(frozen=True)
class LockedArtifact:
    kind: str
    sha256: str
    size: int


@dataclass(frozen=True)
class LockedPackage:
    name: str
    version: str
    source_kind: str
    dependencies: tuple[LockedDependency, ...]
    optional_dependencies: Mapping[str, tuple[LockedDependency, ...]]
    artifacts: tuple[LockedArtifact, ...]


@dataclass(frozen=True)
class LockGraph:
    sha256: str
    format_version: int
    revision: int | None
    requires_python: str | None
    resolution_markers: tuple[str, ...]
    packages: Mapping[str, LockedPackage]
    runtime_roots: tuple[LockedDependency, ...]
    development_roots: tuple[LockedDependency, ...]


DistributionLookup = Callable[[str], metadata.Distribution]


def _is_exact_int(value: object) -> bool:
    return type(value) is int


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _lock_text(value: object, *, field: str, maximum: int = MAX_METADATA_VALUE_BYTES) -> str:
    if not isinstance(value, str):
        raise LockFormatError(f"{field} must be a string.")
    if not value or len(value.encode("utf-8")) > maximum or "\x00" in value:
        raise LockFormatError(f"{field} is empty, contains NUL, or exceeds its bound.")
    return value


def _lock_artifact(kind: str, raw: object, *, package_name: str) -> LockedArtifact:
    if not isinstance(raw, Mapping):
        raise LockFormatError(f"{package_name} {kind} artifact must be a mapping.")
    digest = raw.get("hash")
    size = raw.get("size")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise LockFormatError(f"{package_name} {kind} artifact must carry a lowercase sha256 hash.")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > 2**53 - 1:
        raise LockFormatError(f"{package_name} {kind} artifact has an unsafe size.")
    return LockedArtifact(kind=kind, sha256=digest, size=size)


def _locked_dependencies(raw: object, *, package_name: str) -> tuple[LockedDependency, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_DEPENDENCIES_PER_PACKAGE:
        raise LockFormatError(f"{package_name} has an invalid or oversized dependency list.")
    dependencies: list[LockedDependency] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise LockFormatError(f"{package_name} dependency must be a mapping.")
        if set(entry) - {"name", "marker", "extra"}:
            raise LockFormatError(f"{package_name} dependency has an unsupported field.")
        dependency_name = _normalized_name(_lock_text(entry.get("name"), field="dependency name"))
        if dependency_name in seen:
            raise LockFormatError(f"{package_name} repeats dependency {dependency_name}.")
        seen.add(dependency_name)
        marker = entry.get("marker")
        if marker is not None:
            marker = _lock_text(marker, field="dependency marker")
        extras = _locked_dependency_extras(entry.get("extra"), package_name=package_name)
        dependencies.append(LockedDependency(name=dependency_name, marker=marker, extras=extras))
    return tuple(
        sorted(
            dependencies,
            key=lambda dependency: (dependency.name, dependency.marker or "", dependency.extras),
        )
    )


def _locked_dependency_extras(raw: object, *, package_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_EXTRAS_PER_DEPENDENCY:
        raise LockFormatError(f"{package_name} dependency extra list is invalid or oversized.")
    extras: set[str] = set()
    for value in raw:
        extra = _lock_text(value, field="dependency extra", maximum=64)
        if _EXTRA_NAME_RE.fullmatch(extra) is None:
            raise LockFormatError(f"{package_name} dependency extra is invalid.")
        normalized = _normalized_name(extra)
        if normalized in extras:
            raise LockFormatError(f"{package_name} repeats dependency extra {normalized}.")
        extras.add(normalized)
    return tuple(sorted(extras))


def _optional_dependencies(
    raw: object, *, package_name: str
) -> Mapping[str, tuple[LockedDependency, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or len(raw) > MAX_OPTIONAL_DEPENDENCY_GROUPS:
        raise LockFormatError(f"{package_name} optional dependencies are invalid or oversized.")
    groups: dict[str, tuple[LockedDependency, ...]] = {}
    total_edges = 0
    for raw_extra, raw_dependencies in raw.items():
        extra = _lock_text(raw_extra, field="optional dependency extra", maximum=64)
        if _EXTRA_NAME_RE.fullmatch(extra) is None:
            raise LockFormatError(f"{package_name} optional dependency extra is invalid.")
        normalized = _normalized_name(extra)
        if normalized in groups:
            raise LockFormatError(f"{package_name} repeats optional dependency extra {normalized}.")
        dependencies = _locked_dependencies(raw_dependencies, package_name=package_name)
        total_edges += len(dependencies)
        if total_edges > MAX_DEPENDENCIES_PER_PACKAGE:
            raise LockFormatError(f"{package_name} optional dependency edges exceed the bound.")
        groups[normalized] = dependencies
    return dict(sorted(groups.items()))


def _source_kind(raw: object, *, package_name: str) -> str:
    if not isinstance(raw, Mapping):
        raise LockFormatError(f"{package_name} source must be a mapping.")
    keys = {key for key in raw if isinstance(key, str)}
    if keys == {"registry"} and isinstance(raw.get("registry"), str):
        return "REGISTRY"
    if keys == {"editable"} and raw.get("editable") == ".":
        return "EDITABLE_PROJECT"
    return "OTHER_SOURCE_REVIEW_REQUIRED"


def _root_dev_dependencies(raw: object) -> tuple[LockedDependency, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise LockFormatError("Project dev-dependencies must be a mapping.")
    dev = raw.get("dev", [])
    if not isinstance(dev, list) or len(dev) > MAX_DEPENDENCIES_PER_PACKAGE:
        raise LockFormatError("Project dev dependency group is invalid or oversized.")
    return _locked_dependencies(dev, package_name="Project dev group")


def _no_follow_flag() -> int:
    flag = getattr(os, "O_NOFOLLOW", None)
    if type(flag) is not int:
        raise OSError("safe no-follow descriptor mode is unavailable")
    return flag


def _read_open_flags() -> int:
    return os.O_RDONLY | os.O_NONBLOCK | _no_follow_flag()


def _directory_open_flags() -> int:
    return _read_open_flags() | getattr(os, "O_DIRECTORY", 0)


def _stable_file_state(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    """Return the identity and mutable state that must not change during a read."""

    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _same_stable_file_state(first: os.stat_result, second: os.stat_result) -> bool:
    return _stable_file_state(first) == _stable_file_state(second)


def _same_directory_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(first.st_mode)
        and stat.S_ISDIR(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
    )


def _absolute_lexical_path(path: Path) -> Path:
    """Make a path absolute without resolving symlinks or touching the filesystem."""

    return Path(os.path.abspath(os.fspath(path)))


def _open_absolute_directory(path: Path) -> int:
    """Hold a descriptor for every non-symlink component of an absolute directory."""

    if not path.is_absolute() or not path.anchor:
        raise OSError("directory path must be absolute")
    descriptor = os.open(path.anchor, _directory_open_flags())
    try:
        root_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise OSError("root is not a directory")
        for component in path.parts[1:]:
            if component in {"", ".", ".."}:
                raise OSError("unsafe directory component")
            child = os.open(component, _directory_open_flags(), dir_fd=descriptor)
            try:
                child_stat = os.fstat(child)
                if not stat.S_ISDIR(child_stat.st_mode):
                    raise OSError("path component is not a directory")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_bounded_stable_regular_descriptor(
    descriptor: int, *, maximum_bytes: int
) -> tuple[bytes, os.stat_result] | None:
    """Read a fixed-size regular descriptor and reject any observed mutation."""

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
            return None
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            return None
        after = os.fstat(descriptor)
    except OSError:
        return None
    if not _same_stable_file_state(before, after):
        return None
    return b"".join(chunks), after


def _read_bounded_stable_regular_at(
    directory_descriptor: int, name: str, *, maximum_bytes: int
) -> tuple[bytes, os.stat_result] | None:
    """Open one final component without following it, then read it stably."""

    descriptor = os.open(name, _read_open_flags(), dir_fd=directory_descriptor)
    try:
        return _read_bounded_stable_regular_descriptor(descriptor, maximum_bytes=maximum_bytes)
    finally:
        os.close(descriptor)


def _read_stable_lock_bytes(lock_path: Path) -> bytes:
    """Read one exact regular lock without path replacement or content races."""

    absolute = _absolute_lexical_path(lock_path)
    if not absolute.name or absolute.name in {".", ".."}:
        raise OSError("lock path must name a file")
    parent_descriptor = _open_absolute_directory(absolute.parent)
    try:
        parent_stat = os.fstat(parent_descriptor)
        result = _read_bounded_stable_regular_at(
            parent_descriptor, absolute.name, maximum_bytes=MAX_LOCK_BYTES
        )
        if result is None:
            raise OSError("lock is not a stable bounded regular file")
        raw_bytes, file_stat = result
        current_parent_descriptor = _open_absolute_directory(absolute.parent)
        try:
            current_parent_stat = os.fstat(current_parent_descriptor)
            current_file_stat = os.stat(
                absolute.name, dir_fd=current_parent_descriptor, follow_symlinks=False
            )
        finally:
            os.close(current_parent_descriptor)
        if not _same_directory_identity(
            parent_stat, current_parent_stat
        ) or not _same_stable_file_state(file_stat, current_file_stat):
            raise OSError("lock path changed during read")
        return raw_bytes
    finally:
        os.close(parent_descriptor)


def read_lock_graph(lock_path: Path) -> LockGraph:
    """Parse the supported bounded uv.lock subset without resolving anything."""
    try:
        raw_bytes = _read_stable_lock_bytes(lock_path)
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except FileNotFoundError:
        raise LockFormatError(f"Lock file is absent: {lock_path}.") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise LockFormatError("Lock file is not a stable bounded UTF-8 TOML file.") from None
    if not isinstance(raw, Mapping):
        raise LockFormatError("Lock root must be a mapping.")
    format_version = raw.get("version")
    if not _is_exact_int(format_version) or format_version != 1:
        raise LockFormatError("Only uv.lock format version 1 is supported.")
    revision = raw.get("revision")
    if revision is not None and not _is_exact_int(revision):
        raise LockFormatError("Lock revision must be an integer when present.")
    requires_python_raw = raw.get("requires-python")
    requires_python = (
        _lock_text(requires_python_raw, field="requires-python")
        if requires_python_raw is not None
        else None
    )
    resolution_markers_raw = raw.get("resolution-markers", [])
    if not isinstance(resolution_markers_raw, list) or len(resolution_markers_raw) > 128:
        raise LockFormatError("Lock resolution markers are invalid or oversized.")
    resolution_markers = tuple(
        sorted(_lock_text(marker, field="resolution marker") for marker in resolution_markers_raw)
    )
    package_records = raw.get("package")
    if (
        not isinstance(package_records, list)
        or not package_records
        or len(package_records) > MAX_LOCK_PACKAGES
    ):
        raise LockFormatError("Lock package list is absent, empty, or oversized.")
    packages: dict[str, LockedPackage] = {}
    root: Mapping[str, object] | None = None
    for package_raw in package_records:
        if not isinstance(package_raw, Mapping):
            raise LockFormatError("Every lock package must be a mapping.")
        name = _normalized_name(_lock_text(package_raw.get("name"), field="package name"))
        version = _lock_text(package_raw.get("version"), field=f"{name} version")
        if name in packages:
            raise LockFormatError(
                f"Lock repeats package {name}; multi-version graphs are unsupported."
            )
        source_kind = _source_kind(package_raw.get("source"), package_name=name)
        dependencies = _locked_dependencies(package_raw.get("dependencies"), package_name=name)
        optional_dependencies = _optional_dependencies(
            package_raw.get("optional-dependencies"), package_name=name
        )
        artifacts: list[LockedArtifact] = []
        sdist = package_raw.get("sdist")
        if sdist is not None:
            artifacts.append(_lock_artifact("SDIST", sdist, package_name=name))
        wheels = package_raw.get("wheels", [])
        if not isinstance(wheels, list) or len(wheels) > MAX_LOCK_ARTIFACTS_PER_PACKAGE:
            raise LockFormatError(f"{name} wheel list is invalid or oversized.")
        artifacts.extend(_lock_artifact("WHEEL", wheel, package_name=name) for wheel in wheels)
        if source_kind == "REGISTRY" and not artifacts:
            raise LockFormatError(f"Registry package {name} has no bounded artifact hashes.")
        packages[name] = LockedPackage(
            name=name,
            version=version,
            source_kind=source_kind,
            dependencies=dependencies,
            optional_dependencies=optional_dependencies,
            artifacts=tuple(
                sorted(artifacts, key=lambda artifact: (artifact.kind, artifact.sha256))
            ),
        )
        if name == PROJECT_NAME:
            root = package_raw
    if root is None:
        raise LockFormatError(f"Lock does not contain editable project {PROJECT_NAME}.")
    if packages[PROJECT_NAME].source_kind != "EDITABLE_PROJECT":
        raise LockFormatError(f"{PROJECT_NAME} must be the editable project record.")
    runtime_roots = packages[PROJECT_NAME].dependencies
    development_roots = _root_dev_dependencies(root.get("dev-dependencies"))
    missing_roots = {
        dependency.name for dependency in (*runtime_roots, *development_roots)
    }.difference(packages)
    if missing_roots:
        raise LockFormatError(f"Project roots are absent from the lock: {sorted(missing_roots)}.")
    _validate_requested_optional_extras(packages)
    return LockGraph(
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        format_version=format_version,
        revision=revision,
        requires_python=requires_python,
        resolution_markers=resolution_markers,
        packages=packages,
        runtime_roots=runtime_roots,
        development_roots=development_roots,
    )


def _validate_requested_optional_extras(packages: Mapping[str, LockedPackage]) -> None:
    """Reject a selected extra that the exact locked target does not define."""

    for package in packages.values():
        for dependency in _all_package_dependencies(package):
            if not dependency.extras:
                continue
            target = packages.get(dependency.name)
            if target is None:
                continue
            unknown = set(dependency.extras).difference(target.optional_dependencies)
            if unknown:
                raise LockFormatError(
                    f"{package.name} requests unknown extras from {dependency.name}: "
                    f"{sorted(unknown)}."
                )


def _all_package_dependencies(package: LockedPackage) -> Iterable[LockedDependency]:
    yield from package.dependencies
    for dependencies in package.optional_dependencies.values():
        yield from dependencies


def _reachable(graph: LockGraph, roots: Iterable[LockedDependency]) -> set[str]:
    """Traverse required edges and every explicitly selected optional edge.

    The lock resolves all marker branches, so this deliberately does not
    evaluate markers for the current host. A package may be revisited only
    when a new requested extra becomes enabled.
    """

    reachable: set[str] = set()
    enabled_extras: dict[str, set[str]] = {}
    pending: deque[LockedDependency] = deque(
        sorted(roots, key=lambda dependency: (dependency.name, dependency.extras))
    )
    while pending:
        requested = pending.popleft()
        name = requested.name
        package = graph.packages.get(name)
        if package is None:
            raise LockFormatError(f"Dependency {name} is absent from the exact lock graph.")
        unknown_extras = set(requested.extras).difference(package.optional_dependencies)
        if unknown_extras:
            raise LockFormatError(
                f"Dependency {name} requests unknown extras: {sorted(unknown_extras)}."
            )
        prior_extras = enabled_extras.get(name)
        first_visit = prior_extras is None
        if prior_extras is None:
            prior_extras = set()
            enabled_extras[name] = prior_extras
        new_extras = set(requested.extras).difference(prior_extras)
        if not first_visit and not new_extras:
            continue
        prior_extras.update(new_extras)
        reachable.add(name)
        if first_visit:
            pending.extend(package.dependencies)
        for extra in sorted(new_extras):
            pending.extend(package.optional_dependencies[extra])
    return reachable


def _scopes_for(graph: LockGraph) -> Mapping[str, tuple[str, ...]]:
    runtime = _reachable(graph, graph.runtime_roots)
    development = _reachable(graph, graph.development_roots)
    scopes: dict[str, tuple[str, ...]] = {}
    for name in graph.packages:
        if name == PROJECT_NAME:
            continue
        labels: list[str] = []
        if name in runtime:
            labels.append("RUNTIME")
        if name in development:
            labels.append("DEVELOPMENT")
        if not labels:
            labels.append("UNREACHABLE_LOCKED")
        scopes[name] = tuple(labels)
    return scopes


def _safe_metadata_text(value: object) -> tuple[str, str | None]:
    if value is None:
        return "MISSING", None
    if not isinstance(value, str):
        return "INVALID_TYPE", None
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return "OUT_OF_BOUNDS", None
    if not value or len(encoded) > MAX_METADATA_VALUE_BYTES or "\x00" in value:
        return "OUT_OF_BOUNDS", None
    return "PRESENT", value


def _validate_spdx_expression(expression: str) -> tuple[str, str | None]:
    """Return parser evidence, never a license compatibility conclusion."""
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in expression):
        return "INVALID_OR_UNSUPPORTED", None
    try:
        from license_expression import get_spdx_licensing  # type: ignore[import-untyped]
    except ImportError:
        return "PARSER_UNAVAILABLE", None
    try:
        parsed = get_spdx_licensing().parse(expression, validate=True, strict=True)
        if parsed is None:
            return "INVALID_OR_UNSUPPORTED", None
        rendered = parsed.render()
        if not isinstance(rendered, str):
            return "INVALID_OR_UNSUPPORTED", None
        return "VALID", rendered
    except Exception:  # The parser intentionally has a broad expression error family.
        return "INVALID_OR_UNSUPPORTED", None


def _license_classifier_values(distribution: metadata.Distribution) -> tuple[str, tuple[str, ...]]:
    values = distribution.metadata.get_all("Classifier") or []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        return "INVALID_OR_OUT_OF_BOUNDS", ()
    if len(values) > MAX_CLASSIFIERS:
        return "INVALID_OR_OUT_OF_BOUNDS", ()
    classifiers: list[str] = []
    for value in values:
        status, text = _safe_metadata_text(value)
        if status != "PRESENT" or text is None:
            return "INVALID_OR_OUT_OF_BOUNDS", ()
        if text.startswith("License ::"):
            classifiers.append(text)
    return "PRESENT", tuple(sorted(set(classifiers)))


class _UnsafeRelativePathError(RuntimeError):
    """A descriptor-relative license path contained an unsafe component."""


class _LicenseTotalBoundError(RuntimeError):
    """A candidate exceeds the remaining aggregate license-byte budget."""


def _open_relative_regular_file(directory_descriptor: int, parts: tuple[str, ...]) -> int:
    """Open a relative regular-file candidate through held non-symlink directories."""

    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise _UnsafeRelativePathError
    current_descriptor = os.dup(directory_descriptor)
    try:
        for component in parts[:-1]:
            try:
                child_descriptor = os.open(
                    component, _directory_open_flags(), dir_fd=current_descriptor
                )
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise _UnsafeRelativePathError from None
                raise
            try:
                if not stat.S_ISDIR(os.fstat(child_descriptor).st_mode):
                    raise _UnsafeRelativePathError
            except BaseException:
                os.close(child_descriptor)
                raise
            os.close(current_descriptor)
            current_descriptor = child_descriptor
        try:
            descriptor = os.open(parts[-1], _read_open_flags(), dir_fd=current_descriptor)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _UnsafeRelativePathError from None
            raise
        return descriptor
    finally:
        os.close(current_descriptor)


def _read_bounded_regular_file(
    directory_descriptor: int, parts: tuple[str, ...], *, maximum_bytes: int
) -> tuple[str, int, str] | None:
    """Hash one descriptor-relative stable regular file without returning bytes."""

    descriptor = _open_relative_regular_file(directory_descriptor, parts)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None
        if before.st_size > maximum_bytes:
            if maximum_bytes < MAX_LICENSE_FILE_BYTES:
                raise _LicenseTotalBoundError
            return None
        result = _read_bounded_stable_regular_descriptor(descriptor, maximum_bytes=maximum_bytes)
    finally:
        os.close(descriptor)
    if result is None:
        return None
    raw_bytes, file_stat = result
    return hashlib.sha256(raw_bytes).hexdigest(), file_stat.st_size, "HASHED"


def _distribution_site_root_descriptors(
    distribution: metadata.Distribution,
) -> tuple[int, int, str] | None:
    """Hold the reported site root and dist-info directory without path resolution."""

    try:
        dist_info_raw = getattr(distribution, "_path", None)
        if not isinstance(dist_info_raw, Path):
            return None
        dist_info = _absolute_lexical_path(dist_info_raw)
        if not dist_info.name or dist_info.name in {".", ".."}:
            return None
        site_root_descriptor = _open_absolute_directory(dist_info.parent)
        dist_info_descriptor: int | None = None
        try:
            dist_info_descriptor = os.open(
                dist_info.name, _directory_open_flags(), dir_fd=site_root_descriptor
            )
            if not stat.S_ISDIR(os.fstat(dist_info_descriptor).st_mode):
                os.close(dist_info_descriptor)
                os.close(site_root_descriptor)
                return None
        except BaseException:
            if dist_info_descriptor is not None:
                os.close(dist_info_descriptor)
            os.close(site_root_descriptor)
            raise
        if dist_info_descriptor is None:
            os.close(site_root_descriptor)
            return None
    except OSError:
        return None
    return site_root_descriptor, dist_info_descriptor, dist_info.name


def _license_file_evidence(distribution: metadata.Distribution) -> dict[str, Any]:
    records = distribution.files
    if records is None:
        return {
            "status": "FILES_METADATA_UNAVAILABLE_REVIEW_REQUIRED",
            "files": [],
            "issues": ["FILES_METADATA_UNAVAILABLE"],
        }
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        return {
            "status": "FILES_METADATA_INVALID_REVIEW_REQUIRED",
            "files": [],
            "issues": ["FILES_METADATA_INVALID"],
        }
    if len(records) > MAX_DISTRIBUTION_FILE_RECORDS:
        return {
            "status": "FILES_METADATA_RECORD_COUNT_EXCEEDS_BOUND_REVIEW_REQUIRED",
            "files": [],
            "issues": ["FILES_METADATA_RECORD_COUNT_EXCEEDS_BOUND"],
        }
    candidates: list[PurePosixPath] = []
    issues: list[str] = []
    for record in records:
        record_name = getattr(record, "name", None)
        if not isinstance(record_name, str):
            issues.append("LICENSE_RECORD_INVALID")
            continue
        if _LICENSE_FILE_RE.search(record_name) is None:
            continue
        record_text = str(record)
        if (
            not record_text
            or "\x00" in record_text
            or "\\" in record_text
            or len(record_text.encode("utf-8")) > MAX_LICENSE_RECORD_PATH_BYTES
        ):
            issues.append("UNSAFE_LICENSE_RECORD_PATH")
            continue
        candidates.append(PurePosixPath(record_text))
    candidates.sort(key=str)
    if len(candidates) > MAX_LICENSE_FILES:
        return {
            "status": "LICENSE_FILE_COUNT_EXCEEDS_BOUND_REVIEW_REQUIRED",
            "files": [],
            "issues": sorted(set([*issues, "LICENSE_FILE_COUNT_EXCEEDS_BOUND"])),
        }
    descriptors = _distribution_site_root_descriptors(distribution)
    if descriptors is None:
        return {
            "status": "DISTRIBUTION_LOCATION_UNAVAILABLE_REVIEW_REQUIRED",
            "files": [],
            "issues": ["DISTRIBUTION_LOCATION_UNAVAILABLE"],
        }
    site_root_descriptor, dist_info_descriptor, dist_info_name = descriptors
    try:
        files: list[dict[str, Any]] = []
        total_bytes = 0
        dist_info_stat = os.fstat(dist_info_descriptor)
        for candidate_record in candidates:
            if (
                candidate_record.is_absolute()
                or ".." in candidate_record.parts
                or not candidate_record.parts
                or candidate_record.parts[0] != dist_info_name
                or len(candidate_record.parts) < 2
            ):
                issues.append("UNSAFE_LICENSE_RECORD_PATH")
                continue
            remaining_total_bytes = MAX_TOTAL_LICENSE_FILE_BYTES - total_bytes
            if remaining_total_bytes <= 0:
                return {
                    "status": "LICENSE_FILE_TOTAL_EXCEEDS_BOUND_REVIEW_REQUIRED",
                    "files": files,
                    "issues": sorted(set([*issues, "LICENSE_FILE_TOTAL_EXCEEDS_BOUND"])),
                }
            try:
                current_dist_info_stat = os.stat(
                    dist_info_name, dir_fd=site_root_descriptor, follow_symlinks=False
                )
                if not _same_directory_identity(dist_info_stat, current_dist_info_stat):
                    issues.append("DISTRIBUTION_LOCATION_UNAVAILABLE")
                    continue
                evidence = _read_bounded_regular_file(
                    dist_info_descriptor,
                    candidate_record.parts[1:],
                    maximum_bytes=min(MAX_LICENSE_FILE_BYTES, remaining_total_bytes),
                )
            except _LicenseTotalBoundError:
                return {
                    "status": "LICENSE_FILE_TOTAL_EXCEEDS_BOUND_REVIEW_REQUIRED",
                    "files": files,
                    "issues": sorted(set([*issues, "LICENSE_FILE_TOTAL_EXCEEDS_BOUND"])),
                }
            except _UnsafeRelativePathError:
                issues.append("UNSAFE_LICENSE_FILE_PATH")
                continue
            except OSError:
                issues.append("LICENSE_FILE_UNREADABLE")
                continue
            if evidence is None:
                issues.append("LICENSE_FILE_UNSAFE_OR_OUT_OF_BOUNDS")
                continue
            digest, size, _ = evidence
            total_bytes += size
            try:
                current_dist_info_stat = os.stat(
                    dist_info_name, dir_fd=site_root_descriptor, follow_symlinks=False
                )
            except OSError:
                issues.append("DISTRIBUTION_LOCATION_UNAVAILABLE")
                continue
            if not _same_directory_identity(dist_info_stat, current_dist_info_stat):
                return {
                    "status": "PARTIAL_REVIEW_REQUIRED",
                    "files": files,
                    "issues": sorted(set([*issues, "DISTRIBUTION_LOCATION_UNAVAILABLE"])),
                }
            files.append({"path": str(candidate_record), "sha256": digest, "byte_count": size})
    finally:
        os.close(dist_info_descriptor)
        os.close(site_root_descriptor)
    if not candidates:
        issues.append("NO_DECLARED_LICENSE_FILES")
    status = "HASHED" if not issues else "PARTIAL_REVIEW_REQUIRED"
    return {"status": status, "files": files, "issues": sorted(set(issues))}


def _license_evidence(distribution: metadata.Distribution) -> dict[str, Any]:
    expression_status, expression = _safe_metadata_text(
        distribution.metadata.get("License-Expression")
    )
    parsed_expression_status = "NOT_PRESENT"
    determined_expression: str | None = None
    provenance = "NONE"
    if expression_status == "PRESENT" and expression is not None:
        parsed_expression_status, determined_expression = _validate_spdx_expression(expression)
        if parsed_expression_status == "VALID":
            provenance = "PEP_639_LICENSE_EXPRESSION"
    legacy_status, legacy = _safe_metadata_text(distribution.metadata.get("License"))
    legacy_evidence: dict[str, Any] = {"status": "MISSING"}
    if legacy_status == "PRESENT" and legacy is not None:
        legacy_evidence = {
            "status": "PRESENT_UNINTERPRETED_REVIEW_REQUIRED",
            "sha256": hashlib.sha256(legacy.encode("utf-8")).hexdigest(),
            "byte_count": len(legacy.encode("utf-8")),
        }
    elif legacy_status != "MISSING":
        legacy_evidence = {"status": f"{legacy_status}_REVIEW_REQUIRED"}
    classifier_values_status, classifiers = _license_classifier_values(distribution)
    exact_mappings = sorted(
        {
            EXACT_TROVE_CLASSIFIER_TO_SPDX[value]
            for value in classifiers
            if value in EXACT_TROVE_CLASSIFIER_TO_SPDX
        }
    )
    unmapped = sorted(value for value in classifiers if value not in EXACT_TROVE_CLASSIFIER_TO_SPDX)
    classifier_evidence: dict[str, Any] = {
        "status": "NO_LICENSE_CLASSIFIERS",
        "license_classifiers": list(classifiers),
        "mapped_expression": None,
        "unmapped_license_classifiers": unmapped,
    }
    if classifier_values_status != "PRESENT":
        classifier_evidence["status"] = (
            "INVALID_OR_OUT_OF_BOUNDS_CLASSIFIER_METADATA_REVIEW_REQUIRED"
        )
    elif classifiers:
        if unmapped:
            classifier_evidence["status"] = (
                "UNMAPPED_OR_AMBIGUOUS_LICENSE_CLASSIFIER_REVIEW_REQUIRED"
            )
        elif len(exact_mappings) == 1:
            classifier_evidence.update(
                status="EXACT_CLOSED_MAPPING",
                mapped_expression=exact_mappings[0],
            )
            if determined_expression is None and expression_status == "MISSING":
                determined_expression = exact_mappings[0]
                provenance = "EXACT_TROVE_CLASSIFIER_MAPPING"
        else:
            classifier_evidence["status"] = "MULTIPLE_LICENSE_CLASSIFIER_MAPPINGS_REVIEW_REQUIRED"
    if expression_status != "MISSING" and expression_status != "PRESENT":
        parsed_expression_status = f"{expression_status}_REVIEW_REQUIRED"
    return {
        "decision": "REVIEW_REQUIRED_NO_LEGAL_COMPATIBILITY_DECISION",
        "determined_expression": determined_expression,
        "expression_provenance": provenance,
        "metadata_expression": {
            "status": parsed_expression_status,
            "source_expression": expression if expression_status == "PRESENT" else None,
        },
        "legacy_license_metadata": legacy_evidence,
        "classifier_mapping": classifier_evidence,
        "license_files": _license_file_evidence(distribution),
    }


def _installed_package_evidence(
    package: LockedPackage,
    *,
    distribution_lookup: DistributionLookup,
) -> dict[str, Any]:
    try:
        distribution = distribution_lookup(package.name)
    except metadata.PackageNotFoundError:
        return {
            "status": "NOT_INSTALLED_REVIEW_REQUIRED",
            "installed_version": None,
            "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
        }
    except Exception:
        return {
            "status": "METADATA_READ_FAILED_REVIEW_REQUIRED",
            "installed_version": None,
            "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
        }
    try:
        installed_version_status, installed_version = _safe_metadata_text(distribution.version)
    except Exception:
        return {
            "status": "METADATA_READ_FAILED_REVIEW_REQUIRED",
            "installed_version": None,
            "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
        }
    if installed_version_status != "PRESENT" or installed_version is None:
        return {
            "status": "INSTALLED_VERSION_INVALID_OR_UNAVAILABLE_REVIEW_REQUIRED",
            "installed_version": None,
            "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
        }
    if installed_version != package.version:
        return {
            "status": "VERSION_MISMATCH_REVIEW_REQUIRED",
            "installed_version": installed_version,
            "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED_VERSION_MISMATCH"},
        }
    try:
        installed_name_status, installed_name = _safe_metadata_text(
            distribution.metadata.get("Name")
        )
    except Exception:
        return {
            "status": "METADATA_READ_FAILED_REVIEW_REQUIRED",
            "installed_version": installed_version,
            "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
        }
    if (
        installed_name_status != "PRESENT"
        or installed_name is None
        or _normalized_name(installed_name) != package.name
    ):
        return {
            "status": "INSTALLED_NAME_MISMATCH_OR_UNAVAILABLE_REVIEW_REQUIRED",
            "installed_version": installed_version,
            "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
        }
    try:
        license_evidence = _license_evidence(distribution)
    except Exception:
        return {
            "status": "LICENSE_METADATA_READ_FAILED_REVIEW_REQUIRED",
            "installed_version": installed_version,
            "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
        }
    return {
        "status": "VERSION_MATCHED",
        "installed_version": installed_version,
        "license_evidence": license_evidence,
    }


def _package_artifacts(package: LockedPackage) -> list[dict[str, Any]]:
    return [
        {"kind": artifact.kind, "sha256": artifact.sha256, "byte_count": artifact.size}
        for artifact in package.artifacts
    ]


def _serialized_dependency(dependency: LockedDependency) -> dict[str, Any]:
    return {
        "name": dependency.name,
        "marker": dependency.marker,
        "extras": list(dependency.extras),
    }


def build_inventory(
    lock_path: Path,
    *,
    scope: str = "all",
    distribution_lookup: DistributionLookup = metadata.distribution,
) -> dict[str, Any]:
    """Build a deterministic inventory from exact local inputs without egress."""
    if scope not in {"all", "runtime", "development"}:
        raise InventoryError("scope must be all, runtime, or development.")
    graph = read_lock_graph(lock_path)
    scopes = _scopes_for(graph)
    selected_scope = scope.upper()
    packages: list[dict[str, Any]] = []
    for name in sorted(scopes):
        package_scopes = scopes[name]
        if scope == "runtime" and "RUNTIME" not in package_scopes:
            continue
        if scope == "development" and "DEVELOPMENT" not in package_scopes:
            continue
        package = graph.packages[name]
        installed = _installed_package_evidence(package, distribution_lookup=distribution_lookup)
        packages.append(
            {
                "name": package.name,
                "locked_version": package.version,
                "source_kind": package.source_kind,
                "scopes": list(package_scopes),
                "dependencies": [
                    _serialized_dependency(dependency) for dependency in package.dependencies
                ],
                "optional_dependencies": {
                    extra: [_serialized_dependency(dependency) for dependency in dependencies]
                    for extra, dependencies in package.optional_dependencies.items()
                },
                "lock_artifacts": _package_artifacts(package),
                "installed": installed,
            }
        )
    status_counts: dict[str, int] = {}
    expression_provenance_counts: dict[str, int] = {}
    expression_status_counts: dict[str, int] = {}
    license_file_status_counts: dict[str, int] = {}
    review_required_count = 0
    for package_entry in packages:
        installed = package_entry["installed"]
        status = str(installed["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "VERSION_MATCHED":
            evidence = installed["license_evidence"]
            provenance = str(evidence["expression_provenance"])
            expression_status = str(evidence["metadata_expression"]["status"])
            file_status = str(evidence["license_files"]["status"])
            expression_provenance_counts[provenance] = (
                expression_provenance_counts.get(provenance, 0) + 1
            )
            expression_status_counts[expression_status] = (
                expression_status_counts.get(expression_status, 0) + 1
            )
            license_file_status_counts[file_status] = (
                license_file_status_counts.get(file_status, 0) + 1
            )
        # Every result is evidence for a human review, even when metadata has a
        # syntactically valid SPDX expression.  This scanner makes no legal decision.
        review_required_count += 1
    all_scope_counts = {
        "runtime_reachable": sum("RUNTIME" in labels for labels in scopes.values()),
        "development_reachable": sum("DEVELOPMENT" in labels for labels in scopes.values()),
        "unreachable_locked": sum(labels == ("UNREACHABLE_LOCKED",) for labels in scopes.values()),
    }
    return {
        "schema_version": 1,
        "inventory_kind": "DEPENDENCY_LICENSE_EVIDENCE_V1",
        "decision": "REVIEW_REQUIRED_NO_LEGAL_COMPATIBILITY_DECISION",
        "scope": selected_scope,
        "scope_basis": {
            "runtime": (
                "Reachable from the editable project's lock runtime dependencies across all "
                "lock resolution markers."
            ),
            "development": (
                "Reachable from the editable project's lock dev group across all lock "
                "resolution markers."
            ),
            "unreachable_locked": (
                "Present in uv.lock but unreachable from either editable-project root in this "
                "scanner."
            ),
        },
        "lock": {
            "filename": lock_path.name,
            "sha256": graph.sha256,
            "format_version": graph.format_version,
            "revision": graph.revision,
            "requires_python": graph.requires_python,
            "resolution_markers": list(graph.resolution_markers),
            "editable_project": {
                "name": PROJECT_NAME,
                "version": graph.packages[PROJECT_NAME].version,
                "runtime_dependencies": [
                    _serialized_dependency(dependency) for dependency in graph.runtime_roots
                ],
                "development_dependencies": [
                    _serialized_dependency(dependency) for dependency in graph.development_roots
                ],
            },
        },
        "packages": packages,
        "summary": {
            "selected_package_count": len(packages),
            "all_lock_scope_counts": all_scope_counts,
            "installed_status_counts": dict(sorted(status_counts.items())),
            "matched_license_evidence_counts": {
                "expression_provenance": dict(sorted(expression_provenance_counts.items())),
                "metadata_expression_status": dict(sorted(expression_status_counts.items())),
                "license_file_status": dict(sorted(license_file_status_counts.items())),
            },
            "review_required_package_count": review_required_count,
            "legal_compatibility_decision": "NOT_MADE",
        },
    }


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode only stable scanner output; no wall clock or environment paths enter it."""
    try:
        return (
            json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise InventoryError("Inventory contains unsupported JSON values.") from error


def _remove_created_output_if_unchanged(
    directory_descriptor: int, name: str, output_stat: os.stat_result | None
) -> None:
    if output_stat is None:
        return
    try:
        current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if current.st_dev == output_stat.st_dev and current.st_ino == output_stat.st_ino:
            os.unlink(name, dir_fd=directory_descriptor)
    except OSError:
        pass


def _verify_output_path_stability(
    output_path: Path,
    expected_parent_stat: os.stat_result,
    name: str,
    expected_output_stat: os.stat_result,
) -> None:
    """Require the requested directory entry to still name the created descriptor."""

    try:
        current_parent_descriptor = _open_absolute_directory(output_path.parent)
        try:
            current_parent_stat = os.fstat(current_parent_descriptor)
            current_output_stat = os.stat(
                name, dir_fd=current_parent_descriptor, follow_symlinks=False
            )
        finally:
            os.close(current_parent_descriptor)
    except OSError:
        raise OutputPathError("--output path changed during safe creation.") from None
    if (
        not _same_directory_identity(expected_parent_stat, current_parent_stat)
        or current_output_stat.st_dev != expected_output_stat.st_dev
        or current_output_stat.st_ino != expected_output_stat.st_ino
        or not stat.S_ISREG(current_output_stat.st_mode)
        or current_output_stat.st_nlink != 1
    ):
        raise OutputPathError("--output path changed during safe creation.")


def write_new_output(output_path: Path, content: bytes) -> None:
    """Write one fsynced new regular file through held non-symlink directories."""
    if not output_path.is_absolute():
        raise OutputPathError("--output must be an absolute local path.")
    if not output_path.name or output_path.name in {".", ".."} or ".." in output_path.parts:
        raise OutputPathError("--output must name a file.")
    absolute = _absolute_lexical_path(output_path)
    try:
        parent_descriptor = _open_absolute_directory(absolute.parent)
    except FileNotFoundError:
        raise OutputPathError("--output parent directory does not exist.") from None
    except OSError:
        raise OutputPathError("--output parent must be a real non-symlink directory.") from None
    output_stat: os.stat_result | None = None
    descriptor: int | None = None
    try:
        parent_stat = os.fstat(parent_descriptor)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise OutputPathError("--output parent must be a real non-symlink directory.")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag()
        try:
            descriptor = os.open(absolute.name, flags, 0o600, dir_fd=parent_descriptor)
        except FileExistsError:
            raise OutputPathError("--output must be a new path; refusing overwrite.") from None
        except OSError:
            raise OutputPathError("Unable to create a safe output file.") from None
        output_stat = os.fstat(descriptor)
        if not stat.S_ISREG(output_stat.st_mode) or output_stat.st_nlink != 1:
            raise OutputPathError("Refusing a non-regular or linked output file.")
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OutputPathError("Output write did not make progress.")
            offset += written
        os.fsync(descriptor)
        final_output_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(final_output_stat.st_mode)
            or final_output_stat.st_nlink != 1
            or final_output_stat.st_size != len(content)
        ):
            raise OutputPathError("Output file changed during safe creation.")
        _verify_output_path_stability(absolute, parent_stat, absolute.name, final_output_stat)
    except OSError:
        _remove_created_output_if_unchanged(parent_descriptor, absolute.name, output_stat)
        raise OutputPathError("Output I/O failed during safe creation.") from None
    except BaseException:
        _remove_created_output_if_unchanged(parent_descriptor, absolute.name, output_stat)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "uv.lock",
        help="exact regular uv.lock to inventory (default: repository uv.lock)",
    )
    parser.add_argument(
        "--scope",
        choices=("all", "runtime", "development"),
        default="all",
        help=(
            "select all locked dependencies, runtime-reachable packages, or "
            "development-reachable packages"
        ),
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="new absolute local JSON output path"
    )
    args = parser.parse_args(argv)
    try:
        inventory = build_inventory(args.lock, scope=args.scope)
        output = args.output
        write_new_output(output, canonical_json_bytes(inventory))
    except InventoryError as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "output": str(output),
                "selected_package_count": inventory["summary"]["selected_package_count"],
                "review_required_package_count": inventory["summary"][
                    "review_required_package_count"
                ],
                "legal_compatibility_decision": "NOT_MADE",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
