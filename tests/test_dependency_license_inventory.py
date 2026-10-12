"""Independent regression coverage for the offline dependency license inventory."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

import pytest


def _module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "audit_dependency_licenses.py"
    spec = importlib.util.spec_from_file_location("dependency_license_inventory_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _module()


class _Metadata:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str, default: object | None = None) -> object | None:
        return self._values.get(key, default)

    def get_all(self, key: str) -> list[object] | None:
        value = self._values.get(key)
        return value if isinstance(value, list) else None


class _Distribution:
    def __init__(
        self,
        site_root: Path,
        *,
        name: str,
        version: str,
        metadata_values: dict[str, object],
        files: list[PurePosixPath] | None = None,
    ) -> None:
        self.version = version
        self.metadata = _Metadata({"Name": name, **metadata_values})
        self.files = files
        self._path = site_root / f"{name}-{version}.dist-info"
        self._path.mkdir(parents=True, exist_ok=True)
        self._site_root = site_root

    def locate_file(self, path: PurePosixPath) -> Path:
        return self._site_root / path


def _sha(character: str) -> str:
    return f"sha256:{character * 64}"


def _registry_package(
    name: str,
    version: str,
    *,
    dependencies: tuple[str, ...] = (),
    hash_character: str = "a",
) -> str:
    dependency_value = ""
    if dependencies:
        dependency_value = (
            "\ndependencies = ["
            + ", ".join(f'{{ name = "{dependency}" }}' for dependency in dependencies)
            + "]"
        )
    return (
        "\n[[package]]\n"
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        f'sdist = {{ hash = "{_sha(hash_character)}", size = 42 }}\n'
        f'wheels = [{{ hash = "{_sha(chr(ord(hash_character) + 1))}", size = 84 }}]\n'
        f"{dependency_value}\n"
    )


def _lock(
    *,
    runtime: tuple[str, ...] = ("runtime-lib",),
    development: tuple[str, ...] = ("dev-tool",),
    packages: str | None = None,
) -> str:
    runtime_entries = ", ".join(f'{{ name = "{name}" }}' for name in runtime)
    development_entries = ", ".join(f'{{ name = "{name}" }}' for name in development)
    root = (
        "version = 1\n"
        "revision = 1\n"
        'requires-python = ">=3.12"\n'
        "resolution-markers = [\"python_full_version < '3.14'\"]\n\n"
        "[[package]]\n"
        'name = "adhelm"\n'
        'version = "0.1.0"\n'
        'source = { editable = "." }\n'
        f"dependencies = [{runtime_entries}]\n\n"
        "[package.dev-dependencies]\n"
        f"dev = [{development_entries}]\n"
    )
    if packages is None:
        packages = (
            _registry_package(
                "runtime-lib", "1.0", dependencies=("shared-lib",), hash_character="a"
            )
            + _registry_package("shared-lib", "2.0", hash_character="c")
            + _registry_package("dev-tool", "3.0", dependencies=("shared-lib",), hash_character="e")
            + _registry_package("unreachable", "4.0", hash_character="1")
        )
    return root + packages


def _write_lock(tmp_path: Path, contents: str) -> Path:
    lock = tmp_path / "uv.lock"
    lock.write_text(contents)
    return lock


def _lookup(distributions: dict[str, _Distribution]):
    def lookup(name: str) -> _Distribution:
        if name not in distributions:
            raise MODULE.metadata.PackageNotFoundError(name)
        return distributions[name]

    return lookup


def _by_name(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {package["name"]: package for package in inventory["packages"]}


def test_inventory_is_stable_and_keeps_runtime_development_and_lock_bindings_distinct(
    tmp_path: Path,
) -> None:
    lock = _write_lock(tmp_path, _lock())
    site = tmp_path / "site"
    runtime_license = site / "runtime-lib-1.0.dist-info/licenses/LICENSE"
    runtime_license.parent.mkdir(parents=True)
    runtime_license.write_text("runtime license\n")
    shared_notice = site / "shared-lib-2.0.dist-info/licenses/NOTICE"
    shared_notice.parent.mkdir(parents=True)
    shared_notice.write_text("shared notice\n")
    distributions = {
        "runtime-lib": _Distribution(
            site,
            name="runtime-lib",
            version="1.0",
            metadata_values={
                "License-Expression": "MIT OR Apache-2.0",
                "License": "legacy text is not an expression",
                "Classifier": ["License :: OSI Approved :: BSD License"],
            },
            files=[PurePosixPath("runtime-lib-1.0.dist-info/licenses/LICENSE")],
        ),
        "shared-lib": _Distribution(
            site,
            name="shared-lib",
            version="2.0",
            metadata_values={
                "Classifier": ["License :: OSI Approved :: Apache Software License"],
            },
            files=[PurePosixPath("shared-lib-2.0.dist-info/licenses/NOTICE")],
        ),
        "dev-tool": _Distribution(
            site,
            name="dev-tool",
            version="3.0",
            metadata_values={"License": "BSD-style but not a normalized expression"},
            files=[],
        ),
        "unreachable": _Distribution(
            site,
            name="unreachable",
            version="4.0",
            metadata_values={},
            files=[],
        ),
    }

    first = MODULE.build_inventory(lock, distribution_lookup=_lookup(distributions))
    second = MODULE.build_inventory(lock, distribution_lookup=_lookup(distributions))
    packages = _by_name(first)

    assert MODULE.canonical_json_bytes(first) == MODULE.canonical_json_bytes(second)
    assert first["lock"]["sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()
    assert list(packages) == ["dev-tool", "runtime-lib", "shared-lib", "unreachable"]
    assert packages["runtime-lib"]["scopes"] == ["RUNTIME"]
    assert packages["dev-tool"]["scopes"] == ["DEVELOPMENT"]
    assert packages["shared-lib"]["scopes"] == ["RUNTIME", "DEVELOPMENT"]
    assert packages["unreachable"]["scopes"] == ["UNREACHABLE_LOCKED"]
    assert packages["runtime-lib"]["lock_artifacts"][0]["sha256"] == _sha("a")
    assert packages["runtime-lib"]["installed"]["license_evidence"]["expression_provenance"] == (
        "PEP_639_LICENSE_EXPRESSION"
    )
    assert packages["runtime-lib"]["installed"]["license_evidence"]["determined_expression"] == (
        "MIT OR Apache-2.0"
    )
    assert (
        packages["runtime-lib"]["installed"]["license_evidence"]["legacy_license_metadata"][
            "status"
        ]
        == "PRESENT_UNINTERPRETED_REVIEW_REQUIRED"
    )
    assert packages["shared-lib"]["installed"]["license_evidence"]["expression_provenance"] == (
        "EXACT_TROVE_CLASSIFIER_MAPPING"
    )
    assert packages["shared-lib"]["installed"]["license_evidence"]["determined_expression"] == (
        "Apache-2.0"
    )
    assert packages["dev-tool"]["installed"]["license_evidence"]["determined_expression"] is None
    assert first["decision"] == "REVIEW_REQUIRED_NO_LEGAL_COMPATIBILITY_DECISION"
    assert first["summary"]["legal_compatibility_decision"] == "NOT_MADE"
    assert first["summary"]["matched_license_evidence_counts"]["expression_provenance"] == {
        "EXACT_TROVE_CLASSIFIER_MAPPING": 1,
        "NONE": 2,
        "PEP_639_LICENSE_EXPRESSION": 1,
    }


def test_scope_filter_retains_shared_dependencies_without_laundering_dev_only_packages(
    tmp_path: Path,
) -> None:
    lock = _write_lock(tmp_path, _lock())

    runtime = MODULE.build_inventory(lock, scope="runtime", distribution_lookup=_lookup({}))
    development = MODULE.build_inventory(lock, scope="development", distribution_lookup=_lookup({}))

    assert [package["name"] for package in runtime["packages"]] == ["runtime-lib", "shared-lib"]
    assert [package["name"] for package in development["packages"]] == ["dev-tool", "shared-lib"]
    assert all(
        package["installed"]["status"] == "NOT_INSTALLED_REVIEW_REQUIRED"
        for package in runtime["packages"]
    )


def test_missing_or_version_mismatched_metadata_cannot_be_used_as_license_evidence(
    tmp_path: Path,
) -> None:
    packages = (
        _registry_package("runtime-lib", "1.0", dependencies=("shared-lib",), hash_character="a")
        + _registry_package("shared-lib", "2.0", hash_character="c")
        + _registry_package("wrong-version", "1.0", hash_character="e")
    )
    lock = _write_lock(
        tmp_path,
        _lock(
            runtime=("runtime-lib", "wrong-version"),
            development=(),
            packages=packages,
        ),
    )
    site = tmp_path / "site"
    distributions = {
        "runtime-lib": _Distribution(
            site,
            name="runtime-lib",
            version="1.0",
            metadata_values={"License-Expression": "MIT"},
            files=[],
        ),
        "wrong-version": _Distribution(
            site,
            name="wrong-version",
            version="999.0",
            metadata_values={"License-Expression": "Apache-2.0"},
            files=[],
        ),
    }

    inventory = MODULE.build_inventory(lock, distribution_lookup=_lookup(distributions))
    packages = _by_name(inventory)

    assert packages["wrong-version"]["installed"] == {
        "status": "VERSION_MISMATCH_REVIEW_REQUIRED",
        "installed_version": "999.0",
        "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED_VERSION_MISMATCH"},
    }
    assert packages["shared-lib"]["installed"]["status"] == "NOT_INSTALLED_REVIEW_REQUIRED"
    assert packages["shared-lib"]["installed"]["license_evidence"] == {
        "decision": "REVIEW_REQUIRED_NOT_SCANNED"
    }


def test_ambiguous_classifier_and_legacy_freetext_are_never_inferred_as_spdx(
    tmp_path: Path,
) -> None:
    lock = _write_lock(tmp_path, _lock(runtime=("runtime-lib",), development=()))
    site = tmp_path / "site"
    distributions = {
        "runtime-lib": _Distribution(
            site,
            name="runtime-lib",
            version="1.0",
            metadata_values={
                "License": "BSD-like, see project website",
                "Classifier": ["License :: OSI Approved :: BSD License"],
            },
            files=[],
        )
    }

    package = _by_name(MODULE.build_inventory(lock, distribution_lookup=_lookup(distributions)))[
        "runtime-lib"
    ]
    evidence = package["installed"]["license_evidence"]

    assert evidence["determined_expression"] is None
    assert evidence["expression_provenance"] == "NONE"
    assert evidence["legacy_license_metadata"]["status"] == "PRESENT_UNINTERPRETED_REVIEW_REQUIRED"
    assert evidence["classifier_mapping"]["status"] == (
        "UNMAPPED_OR_AMBIGUOUS_LICENSE_CLASSIFIER_REVIEW_REQUIRED"
    )


def test_invalid_pep639_expression_cannot_fall_back_to_a_classifier_mapping(tmp_path: Path) -> None:
    lock = _write_lock(tmp_path, _lock(runtime=("runtime-lib",), development=()))
    site = tmp_path / "site"
    distribution = _Distribution(
        site,
        name="runtime-lib",
        version="1.0",
        metadata_values={
            "License-Expression": "Unknown-License-Text",
            "Classifier": ["License :: OSI Approved :: Apache Software License"],
        },
        files=[],
    )

    evidence = _by_name(
        MODULE.build_inventory(lock, distribution_lookup=_lookup({"runtime-lib": distribution}))
    )["runtime-lib"]["installed"]["license_evidence"]

    assert evidence["metadata_expression"]["status"] == "INVALID_OR_UNSUPPORTED"
    assert evidence["classifier_mapping"]["status"] == "EXACT_CLOSED_MAPPING"
    assert evidence["determined_expression"] is None
    assert evidence["expression_provenance"] == "NONE"


def test_license_file_hashing_rejects_symlink_records_and_preserves_regular_file_hashes(
    tmp_path: Path,
) -> None:
    lock = _write_lock(tmp_path, _lock(runtime=("runtime-lib",), development=()))
    site = tmp_path / "site"
    outside = tmp_path / "outside-license"
    outside.write_text("outside\n")
    regular = site / "runtime-lib-1.0.dist-info/licenses/NOTICE"
    regular.parent.mkdir(parents=True)
    regular.write_text("notice\n")
    linked = site / "runtime-lib-1.0.dist-info/licenses/LICENSE"
    linked.symlink_to(outside)
    distribution = _Distribution(
        site,
        name="runtime-lib",
        version="1.0",
        metadata_values={"License-Expression": "MIT"},
        files=[
            PurePosixPath("runtime-lib-1.0.dist-info/licenses/LICENSE"),
            PurePosixPath("runtime-lib-1.0.dist-info/licenses/NOTICE"),
        ],
    )

    evidence = _by_name(
        MODULE.build_inventory(lock, distribution_lookup=_lookup({"runtime-lib": distribution}))
    )["runtime-lib"]["installed"]["license_evidence"]["license_files"]

    assert evidence["status"] == "PARTIAL_REVIEW_REQUIRED"
    assert evidence["issues"] == ["UNSAFE_LICENSE_FILE_PATH"]
    assert evidence["files"] == [
        {
            "path": "runtime-lib-1.0.dist-info/licenses/NOTICE",
            "sha256": hashlib.sha256(b"notice\n").hexdigest(),
            "byte_count": len(b"notice\n"),
        }
    ]


def test_lock_rejects_boolean_artifact_size_instead_of_normalizing_it(tmp_path: Path) -> None:
    contents = _lock(runtime=("runtime-lib",), development=()).replace("size = 42", "size = true")
    lock = _write_lock(tmp_path, contents)

    with pytest.raises(MODULE.LockFormatError, match="unsafe size"):
        MODULE.read_lock_graph(lock)


def test_lock_descriptor_read_rejects_path_replacement_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _write_lock(tmp_path, _lock(runtime=(), development=(), packages=""))
    original_read = MODULE.os.read
    replaced = False

    def race_read(descriptor: int, amount: int) -> bytes:
        nonlocal replaced
        result = original_read(descriptor, amount)
        if not replaced:
            replaced = True
            replacement = tmp_path / "replacement.lock"
            replacement.write_text(_lock(runtime=(), development=(), packages="") + "\n# changed\n")
            os.replace(replacement, lock)
        return result

    monkeypatch.setattr(MODULE.os, "read", race_read)
    with pytest.raises(MODULE.LockFormatError, match="stable bounded") as error:
        MODULE.read_lock_graph(lock)
    assert "changed" not in str(error.value)


def test_lock_descriptor_read_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("platform lacks FIFO fixtures")
    lock = tmp_path / "uv.lock"
    os.mkfifo(lock)

    with pytest.raises(MODULE.LockFormatError, match="stable bounded"):
        MODULE.read_lock_graph(lock)


def test_optional_extra_edges_are_serialized_and_make_transitive_package_reachable(
    tmp_path: Path,
) -> None:
    pip_audit = _registry_package("pip-audit", "1.0", dependencies=("cachecontrol",)).replace(
        'dependencies = [{ name = "cachecontrol" }]',
        'dependencies = [{ name = "cachecontrol", extra = ["filecache"] }]',
    )
    cachecontrol = _registry_package("cachecontrol", "1.0", hash_character="c") + (
        '\n[package.optional-dependencies]\nfilecache = [{ name = "filelock" }]\n'
    )
    packages = pip_audit + cachecontrol + _registry_package("filelock", "1.0", hash_character="e")
    lock = _write_lock(
        tmp_path,
        _lock(runtime=(), development=("pip-audit",), packages=packages),
    )

    inventory = MODULE.build_inventory(lock, scope="development", distribution_lookup=_lookup({}))
    by_name = _by_name(inventory)

    assert set(by_name) == {"cachecontrol", "filelock", "pip-audit"}
    assert by_name["filelock"]["scopes"] == ["DEVELOPMENT"]
    assert by_name["pip-audit"]["dependencies"] == [
        {"name": "cachecontrol", "marker": None, "extras": ["filecache"]}
    ]
    assert by_name["cachecontrol"]["optional_dependencies"] == {
        "filecache": [{"name": "filelock", "marker": None, "extras": []}]
    }
    assert inventory["lock"]["editable_project"]["development_dependencies"] == [
        {"name": "pip-audit", "marker": None, "extras": []}
    ]


def test_root_selected_extra_is_preserved_in_its_serialized_lock_edge(tmp_path: Path) -> None:
    cachecontrol = _registry_package("cachecontrol", "1.0", hash_character="c") + (
        '\n[package.optional-dependencies]\nfilecache = [{ name = "filelock" }]\n'
    )
    packages = cachecontrol + _registry_package("filelock", "1.0", hash_character="e")
    contents = _lock(runtime=("cachecontrol",), development=(), packages=packages).replace(
        'dependencies = [{ name = "cachecontrol" }]',
        'dependencies = [{ name = "cachecontrol", extra = ["filecache"] }]',
    )
    lock = _write_lock(tmp_path, contents)

    inventory = MODULE.build_inventory(lock, scope="runtime", distribution_lookup=_lookup({}))

    assert _by_name(inventory)["filelock"]["scopes"] == ["RUNTIME"]
    assert inventory["lock"]["editable_project"]["runtime_dependencies"] == [
        {"name": "cachecontrol", "marker": None, "extras": ["filecache"]}
    ]


@pytest.mark.parametrize(
    ("extras", "match"),
    [
        ('["unknown"]', "unknown extras"),
        ('["filecache", "filecache"]', "repeats dependency extra"),
    ],
)
def test_optional_extra_edges_reject_unknown_or_duplicate_requested_extras(
    tmp_path: Path, extras: str, match: str
) -> None:
    pip_audit = _registry_package("pip-audit", "1.0", dependencies=("cachecontrol",)).replace(
        'dependencies = [{ name = "cachecontrol" }]',
        f'dependencies = [{{ name = "cachecontrol", extra = {extras} }}]',
    )
    cachecontrol = _registry_package("cachecontrol", "1.0", hash_character="c") + (
        '\n[package.optional-dependencies]\nfilecache = [{ name = "filelock" }]\n'
    )
    packages = pip_audit + cachecontrol + _registry_package("filelock", "1.0", hash_character="e")
    lock = _write_lock(
        tmp_path,
        _lock(runtime=(), development=("pip-audit",), packages=packages),
    )

    with pytest.raises(MODULE.LockFormatError, match=match):
        MODULE.read_lock_graph(lock)


def test_same_version_distribution_with_wrong_metadata_name_is_not_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _write_lock(tmp_path, _lock(runtime=("runtime-lib",), development=()))
    distribution = _Distribution(
        tmp_path / "site",
        name="runtime-lib",
        version="1.0",
        metadata_values={"Name": "unrelated-lib", "License-Expression": "MIT"},
        files=[],
    )

    def unexpected_license_scan(_: object) -> dict[str, object]:
        raise AssertionError("wrong-name distribution must not reach license scanning")

    monkeypatch.setattr(MODULE, "_license_evidence", unexpected_license_scan)
    installed = _by_name(
        MODULE.build_inventory(lock, distribution_lookup=_lookup({"runtime-lib": distribution}))
    )["runtime-lib"]["installed"]

    assert installed == {
        "status": "INSTALLED_NAME_MISMATCH_OR_UNAVAILABLE_REVIEW_REQUIRED",
        "installed_version": "1.0",
        "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
    }


def test_license_directory_swap_cannot_hash_outside_declared_site_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _write_lock(tmp_path, _lock(runtime=("runtime-lib",), development=()))
    site = tmp_path / "site"
    licenses = site / "runtime-lib-1.0.dist-info/licenses"
    licenses.mkdir(parents=True)
    (licenses / "NOTICE").write_text("declared notice\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "NOTICE").write_text("outside private notice\n")
    distribution = _Distribution(
        site,
        name="runtime-lib",
        version="1.0",
        metadata_values={"License-Expression": "MIT"},
        files=[PurePosixPath("runtime-lib-1.0.dist-info/licenses/NOTICE")],
    )
    original_open = MODULE._open_relative_regular_file
    swapped = False

    def swap_then_open(directory_descriptor: int, parts: tuple[str, ...]) -> int:
        nonlocal swapped
        if not swapped:
            swapped = True
            moved = site / "moved-licenses"
            os.rename(licenses, moved)
            licenses.symlink_to(outside, target_is_directory=True)
        return original_open(directory_descriptor, parts)

    monkeypatch.setattr(MODULE, "_open_relative_regular_file", swap_then_open)
    evidence = _by_name(
        MODULE.build_inventory(lock, distribution_lookup=_lookup({"runtime-lib": distribution}))
    )["runtime-lib"]["installed"]["license_evidence"]["license_files"]

    assert evidence["files"] == []
    assert "UNSAFE_LICENSE_FILE_PATH" in evidence["issues"]
    assert hashlib.sha256(b"outside private notice\n").hexdigest() not in json.dumps(evidence)


def test_cross_distribution_license_record_is_never_opened_from_the_site_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _write_lock(tmp_path, _lock(runtime=("runtime-lib",), development=()))
    site = tmp_path / "site"
    other_license = site / "other-lib-1.0.dist-info/licenses/LICENSE"
    other_license.parent.mkdir(parents=True)
    other_license.write_text("other distribution license\n")
    distribution = _Distribution(
        site,
        name="runtime-lib",
        version="1.0",
        metadata_values={"License-Expression": "MIT"},
        files=[PurePosixPath("other-lib-1.0.dist-info/licenses/LICENSE")],
    )

    def unexpected_open(*_: object, **__: object) -> tuple[str, int, str]:
        raise AssertionError("a cross-distribution RECORD path must not be opened")

    monkeypatch.setattr(MODULE, "_read_bounded_regular_file", unexpected_open)
    evidence = _by_name(
        MODULE.build_inventory(lock, distribution_lookup=_lookup({"runtime-lib": distribution}))
    )["runtime-lib"]["installed"]["license_evidence"]["license_files"]

    assert evidence == {
        "status": "PARTIAL_REVIEW_REQUIRED",
        "files": [],
        "issues": ["UNSAFE_LICENSE_RECORD_PATH"],
    }


def test_license_total_bound_is_reserved_before_a_later_file_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    first = site / "runtime-lib-1.0.dist-info/licenses/LICENSE-A"
    second = site / "runtime-lib-1.0.dist-info/licenses/LICENSE-B"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"a" * 8)
    second.write_bytes(b"b" * 8)
    distribution = _Distribution(
        site,
        name="runtime-lib",
        version="1.0",
        metadata_values={},
        files=[
            PurePosixPath("runtime-lib-1.0.dist-info/licenses/LICENSE-A"),
            PurePosixPath("runtime-lib-1.0.dist-info/licenses/LICENSE-B"),
        ],
    )
    monkeypatch.setattr(MODULE, "MAX_LICENSE_FILE_BYTES", 8)
    monkeypatch.setattr(MODULE, "MAX_TOTAL_LICENSE_FILE_BYTES", 12)
    original_read = MODULE._read_bounded_stable_regular_descriptor
    bounded_reads: list[int] = []

    def record_bounded_read(descriptor: int, *, maximum_bytes: int):  # type: ignore[no-untyped-def]
        bounded_reads.append(maximum_bytes)
        return original_read(descriptor, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(MODULE, "_read_bounded_stable_regular_descriptor", record_bounded_read)
    evidence = MODULE._license_file_evidence(distribution)

    assert bounded_reads == [8]
    assert evidence["status"] == "LICENSE_FILE_TOTAL_EXCEEDS_BOUND_REVIEW_REQUIRED"
    assert evidence["files"] == [
        {
            "path": "runtime-lib-1.0.dist-info/licenses/LICENSE-A",
            "sha256": hashlib.sha256(b"a" * 8).hexdigest(),
            "byte_count": 8,
        }
    ]
    assert evidence["issues"] == ["LICENSE_FILE_TOTAL_EXCEEDS_BOUND"]


def test_installed_metadata_access_and_non_utf8_name_fail_closed_without_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _write_lock(tmp_path, _lock(runtime=("runtime-lib",), development=()))
    site = tmp_path / "site"
    distribution = _Distribution(
        site,
        name="runtime-lib",
        version="1.0",
        metadata_values={"Name": "\ud800", "License-Expression": "MIT"},
        files=[],
    )

    def unexpected_license_scan(_: object) -> dict[str, object]:
        raise AssertionError("malformed installed name must not reach license scanning")

    monkeypatch.setattr(MODULE, "_license_evidence", unexpected_license_scan)
    installed = _by_name(
        MODULE.build_inventory(lock, distribution_lookup=_lookup({"runtime-lib": distribution}))
    )["runtime-lib"]["installed"]

    assert installed == {
        "status": "INSTALLED_NAME_MISMATCH_OR_UNAVAILABLE_REVIEW_REQUIRED",
        "installed_version": "1.0",
        "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
    }

    class _ExplodingVersionDistribution:
        @property
        def version(self) -> str:
            raise RuntimeError("private metadata backend detail")

    failed = _by_name(
        MODULE.build_inventory(
            lock,
            distribution_lookup=_lookup({"runtime-lib": _ExplodingVersionDistribution()}),
        )
    )["runtime-lib"]["installed"]
    assert failed == {
        "status": "METADATA_READ_FAILED_REVIEW_REQUIRED",
        "installed_version": None,
        "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
    }

    class _ExplodingMetadata:
        def get(self, _: str) -> object:
            raise RuntimeError("private metadata backend detail")

    class _ExplodingNameDistribution:
        version = "1.0"
        metadata = _ExplodingMetadata()

    failed_name = _by_name(
        MODULE.build_inventory(
            lock,
            distribution_lookup=_lookup({"runtime-lib": _ExplodingNameDistribution()}),
        )
    )["runtime-lib"]["installed"]
    assert failed_name == {
        "status": "METADATA_READ_FAILED_REVIEW_REQUIRED",
        "installed_version": "1.0",
        "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
    }


def test_output_parent_swap_cannot_create_outside_held_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    output = parent / "inventory.json"
    original_open = MODULE._open_absolute_directory
    swapped = False

    def swap_after_open(path: Path) -> int:
        nonlocal swapped
        descriptor = original_open(path)
        if not swapped and path == parent:
            swapped = True
            moved = tmp_path / "moved-parent"
            os.rename(parent, moved)
            parent.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(MODULE, "_open_absolute_directory", swap_after_open)
    with pytest.raises(MODULE.OutputPathError, match="changed|non-symlink"):
        MODULE.write_new_output(output, b'{"stable":true}\n')

    assert not (outside / "inventory.json").exists()
    assert not (tmp_path / "moved-parent" / "inventory.json").exists()


def test_output_verifier_rejects_a_new_hardlink_after_creation(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    output = parent / "inventory.json"
    output.write_bytes(b"stable\n")
    parent_stat = os.stat(parent)
    output_stat = os.stat(output)
    os.link(output, parent / "second-name")

    with pytest.raises(MODULE.OutputPathError, match="changed"):
        MODULE._verify_output_path_stability(output, parent_stat, output.name, output_stat)


def test_new_output_is_fsynced_without_overwrite_or_symlink_following(tmp_path: Path) -> None:
    content = b'{"stable":true}\n'
    output = tmp_path / "inventory.json"

    MODULE.write_new_output(output, content)

    assert output.read_bytes() == content
    assert os.stat(output).st_mode & 0o777 == 0o600
    with pytest.raises(MODULE.OutputPathError, match="new path"):
        MODULE.write_new_output(output, content)
    with pytest.raises(MODULE.OutputPathError, match="absolute"):
        MODULE.write_new_output(Path("relative.json"), content)
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(output)
    with pytest.raises(MODULE.OutputPathError, match="new path"):
        MODULE.write_new_output(symlink, content)
    unsafe_parent_target = tmp_path / "real-parent"
    unsafe_parent_target.mkdir()
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.symlink_to(unsafe_parent_target, target_is_directory=True)
    with pytest.raises(MODULE.OutputPathError, match="non-symlink directory"):
        MODULE.write_new_output(unsafe_parent / "inventory.json", content)


def test_cli_requires_a_new_absolute_local_output_and_produces_parseable_json(
    tmp_path: Path,
) -> None:
    lock = _write_lock(tmp_path, _lock(runtime=(), development=(), packages=""))
    output = tmp_path / "inventory.json"
    script = Path(__file__).resolve().parents[1] / "scripts" / "audit_dependency_licenses.py"
    command = [
        sys.executable,
        str(script),
        "--lock",
        str(lock),
        "--output",
        str(output),
    ]

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    repeated = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(output.read_text())["packages"] == []
    assert json.loads(result.stdout)["legal_compatibility_decision"] == "NOT_MADE"
    assert repeated.returncode == 2
    assert "refusing overwrite" in repeated.stderr
    relative = subprocess.run(
        [sys.executable, str(script), "--lock", str(lock), "--output", "relative-inventory.json"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert relative.returncode == 2
    assert "absolute local path" in relative.stderr
