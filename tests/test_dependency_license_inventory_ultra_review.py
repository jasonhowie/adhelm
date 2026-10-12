"""Adversarial review coverage for the offline dependency-license inventory."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

import pytest


def _module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "audit_dependency_licenses.py"
    spec = importlib.util.spec_from_file_location("dependency_license_inventory_ultra_review", path)
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
        metadata_values: dict[str, object] | None = None,
        files: list[PurePosixPath] | None = None,
        dist_info_name: str | None = None,
    ) -> None:
        self.version = version
        self._metadata = _Metadata({"Name": name, **(metadata_values or {})})
        self._files = files
        self._path = site_root / (dist_info_name or f"{name}-{version}.dist-info")
        self._path.mkdir(parents=True, exist_ok=True)

    @property
    def metadata(self) -> _Metadata:
        return self._metadata

    @property
    def files(self) -> list[PurePosixPath] | None:
        return self._files


def _sha(character: str) -> str:
    return f"sha256:{character * 64}"


def _registry_package(
    name: str,
    version: str,
    *,
    dependencies: str = "[]",
    optional_dependencies: str = "",
    hash_character: str = "a",
) -> str:
    return (
        "\n[[package]]\n"
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        f"dependencies = {dependencies}\n"
        f'sdist = {{ hash = "{_sha(hash_character)}", size = 1 }}\n'
        f"{optional_dependencies}"
    )


def _lock(*, development: str = "[]", packages: str) -> str:
    return (
        "version = 1\n"
        "revision = 1\n"
        'requires-python = ">=3.12"\n\n'
        "[[package]]\n"
        'name = "adhelm"\n'
        'version = "0.1.0"\n'
        'source = { editable = "." }\n'
        "dependencies = []\n\n"
        "[package.dev-dependencies]\n"
        f"dev = {development}\n"
        f"{packages}"
    )


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


def _installed(inventory: dict[str, Any], name: str) -> dict[str, Any]:
    package = next(value for value in inventory["packages"] if value["name"] == name)
    return package["installed"]


def test_nested_selected_extras_are_part_of_the_declared_development_graph(
    tmp_path: Path,
) -> None:
    packages = (
        _registry_package(
            "top",
            "1",
            optional_dependencies=(
                "[package.optional-dependencies]\n"
                'root-extra = [{ name = "base", extra = ["feature"] }]\n'
            ),
            hash_character="a",
        )
        + _registry_package(
            "base",
            "1",
            optional_dependencies=(
                "[package.optional-dependencies]\n"
                'feature = [{ name = "middle", extra = ["nested"] }]\n'
            ),
            hash_character="b",
        )
        + _registry_package(
            "middle",
            "1",
            optional_dependencies=(
                '[package.optional-dependencies]\nnested = [{ name = "leaf" }]\n'
            ),
            hash_character="c",
        )
        + _registry_package("leaf", "1", hash_character="d")
    )
    lock = _write_lock(
        tmp_path,
        _lock(development='[{ name = "top", extra = ["root-extra"] }]', packages=packages),
    )

    inventory = MODULE.build_inventory(lock, scope="development", distribution_lookup=_lookup({}))
    by_name = {package["name"]: package for package in inventory["packages"]}

    assert set(by_name) == {"base", "leaf", "middle", "top"}
    assert by_name["leaf"]["scopes"] == ["DEVELOPMENT"]
    assert by_name["top"]["dependencies"] == []
    assert by_name["top"]["optional_dependencies"] == {
        "root-extra": [{"name": "base", "marker": None, "extras": ["feature"]}]
    }
    assert inventory["lock"]["editable_project"]["development_dependencies"] == [
        {"name": "top", "marker": None, "extras": ["root-extra"]}
    ]
    assert by_name["base"]["optional_dependencies"] == {
        "feature": [{"name": "middle", "marker": None, "extras": ["nested"]}]
    }


def test_lock_snapshot_rejects_a_swap_after_bounded_descriptor_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = _lock(packages="")
    lock = _write_lock(tmp_path, contents)
    original = MODULE._read_bounded_stable_regular_at

    def read_then_replace(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)
        replacement = tmp_path / "replacement.lock"
        replacement.write_text(contents + "# private-replacement-sentinel\n")
        os.replace(replacement, lock)
        return result

    monkeypatch.setattr(MODULE, "_read_bounded_stable_regular_at", read_then_replace)

    with pytest.raises(MODULE.LockFormatError, match="stable bounded") as error:
        MODULE.read_lock_graph(lock)

    assert "private-replacement-sentinel" not in str(error.value)


def test_declared_license_record_cannot_cross_into_a_different_dist_info_directory(
    tmp_path: Path,
) -> None:
    lock = _write_lock(
        tmp_path,
        _lock(
            development="[]",
            packages=_registry_package("runtime-lib", "1", hash_character="a"),
        ),
    )
    site = tmp_path / "site"
    foreign = site / "other-lib-1.dist-info/licenses/LICENSE"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("foreign-license-sentinel\n")
    distribution = _Distribution(
        site,
        name="runtime-lib",
        version="1",
        metadata_values={"License-Expression": "MIT"},
        files=[PurePosixPath("other-lib-1.dist-info/licenses/LICENSE")],
    )

    evidence = _installed(
        MODULE.build_inventory(lock, distribution_lookup=_lookup({"runtime-lib": distribution})),
        "runtime-lib",
    )["license_evidence"]["license_files"]

    assert evidence == {
        "status": "PARTIAL_REVIEW_REQUIRED",
        "files": [],
        "issues": ["UNSAFE_LICENSE_RECORD_PATH"],
    }
    assert "foreign-license-sentinel" not in json.dumps(evidence)
    assert hashlib.sha256(b"foreign-license-sentinel\n").hexdigest() not in json.dumps(evidence)


def test_total_license_bound_stops_before_opening_a_fifth_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _write_lock(
        tmp_path,
        _lock(
            development="[]",
            packages=_registry_package("runtime-lib", "1", hash_character="a"),
        ),
    )
    site = tmp_path / "site"
    distribution = _Distribution(site, name="runtime-lib", version="1")
    records: list[PurePosixPath] = []
    for index in range(4):
        name = f"LICENSE-{index}"
        path = distribution._path / name
        path.write_bytes(b"x" * MODULE.MAX_LICENSE_FILE_BYTES)
        records.append(PurePosixPath(f"{distribution._path.name}/{name}"))
    final_name = "NOTICE-final"
    (distribution._path / final_name).write_text("must-not-be-opened\n")
    records.append(PurePosixPath(f"{distribution._path.name}/{final_name}"))
    distribution._files = records
    original = MODULE._open_relative_regular_file

    def reject_final_candidate(directory_descriptor: int, parts: tuple[str, ...]) -> int:
        if parts == (final_name,):
            raise AssertionError("an already-exhausted aggregate cap must prevent another open")
        return original(directory_descriptor, parts)

    monkeypatch.setattr(MODULE, "_open_relative_regular_file", reject_final_candidate)

    evidence = _installed(
        MODULE.build_inventory(lock, distribution_lookup=_lookup({"runtime-lib": distribution})),
        "runtime-lib",
    )["license_evidence"]["license_files"]

    assert evidence["status"] == "LICENSE_FILE_TOTAL_EXCEEDS_BOUND_REVIEW_REQUIRED"
    assert len(evidence["files"]) == 4
    assert evidence["issues"] == ["LICENSE_FILE_TOTAL_EXCEEDS_BOUND"]


def test_declared_license_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("platform lacks FIFO fixtures")
    lock = _write_lock(
        tmp_path,
        _lock(
            development="[]",
            packages=_registry_package("runtime-lib", "1", hash_character="a"),
        ),
    )
    site = tmp_path / "site"
    distribution = _Distribution(site, name="runtime-lib", version="1")
    fifo = distribution._path / "LICENSE"
    os.mkfifo(fifo)
    distribution._files = [PurePosixPath(f"{distribution._path.name}/LICENSE")]

    evidence = _installed(
        MODULE.build_inventory(lock, distribution_lookup=_lookup({"runtime-lib": distribution})),
        "runtime-lib",
    )["license_evidence"]["license_files"]

    assert evidence == {
        "status": "PARTIAL_REVIEW_REQUIRED",
        "files": [],
        "issues": ["LICENSE_FILE_UNSAFE_OR_OUT_OF_BOUNDS"],
    }


@pytest.mark.parametrize(
    ("field", "expected_status"),
    [
        ("version", "INSTALLED_VERSION_INVALID_OR_UNAVAILABLE_REVIEW_REQUIRED"),
        ("name", "INSTALLED_NAME_MISMATCH_OR_UNAVAILABLE_REVIEW_REQUIRED"),
    ],
)
def test_malformed_utf8_metadata_never_escapes_or_reaches_license_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, expected_status: str
) -> None:
    lock = _write_lock(
        tmp_path,
        _lock(
            development="[]",
            packages=_registry_package("runtime-lib", "1", hash_character="a"),
        ),
    )
    site = tmp_path / "site"
    distribution = _Distribution(site, name="runtime-lib", version="1")
    if field == "version":
        distribution.version = "\ud800"
    else:
        distribution._metadata = _Metadata({"Name": "\ud800"})

    def unexpected_license_scan(_: object) -> dict[str, object]:
        raise AssertionError("invalid installed metadata must stop before license scanning")

    monkeypatch.setattr(MODULE, "_license_evidence", unexpected_license_scan)
    inventory = MODULE.build_inventory(
        lock, distribution_lookup=_lookup({"runtime-lib": distribution})
    )
    installed = _installed(inventory, "runtime-lib")

    assert installed["status"] == expected_status
    assert "\ud800" not in MODULE.canonical_json_bytes(inventory).decode("utf-8")


def test_metadata_accessor_failure_is_sanitized_before_license_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _write_lock(
        tmp_path,
        _lock(
            development="[]",
            packages=_registry_package("runtime-lib", "1", hash_character="a"),
        ),
    )

    class ExplodingMetadata:
        def get(self, _: str, default: object | None = None) -> object | None:
            del default
            raise RuntimeError("private-metadata-backend-sentinel")

    class ExplodingDistribution:
        version = "1"
        metadata = ExplodingMetadata()

    def unexpected_license_scan(_: object) -> dict[str, object]:
        raise AssertionError("metadata failure must stop before license scanning")

    monkeypatch.setattr(MODULE, "_license_evidence", unexpected_license_scan)
    inventory = MODULE.build_inventory(
        lock, distribution_lookup=_lookup({"runtime-lib": ExplodingDistribution()})
    )
    installed = _installed(inventory, "runtime-lib")

    assert installed == {
        "status": "METADATA_READ_FAILED_REVIEW_REQUIRED",
        "installed_version": "1",
        "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
    }
    assert "private-metadata-backend-sentinel" not in MODULE.canonical_json_bytes(inventory).decode(
        "utf-8"
    )


def test_same_version_wrong_name_does_not_scan_or_attribute_license_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _write_lock(
        tmp_path,
        _lock(
            development="[]",
            packages=_registry_package("runtime-lib", "1", hash_character="a"),
        ),
    )
    distribution = _Distribution(
        tmp_path / "site",
        name="runtime-lib",
        version="1",
        metadata_values={"Name": "imposter-lib", "License-Expression": "MIT"},
    )

    def unexpected_license_scan(_: object) -> dict[str, object]:
        raise AssertionError("wrong-name metadata must not reach license scanning")

    monkeypatch.setattr(MODULE, "_license_evidence", unexpected_license_scan)
    installed = _installed(
        MODULE.build_inventory(lock, distribution_lookup=_lookup({"runtime-lib": distribution})),
        "runtime-lib",
    )

    assert installed == {
        "status": "INSTALLED_NAME_MISMATCH_OR_UNAVAILABLE_REVIEW_REQUIRED",
        "installed_version": "1",
        "license_evidence": {"decision": "REVIEW_REQUIRED_NOT_SCANNED"},
    }


def test_hardlinked_output_is_rejected_and_requested_path_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    output = parent / "inventory.json"
    alias = parent / "attacker-link.json"
    original = MODULE._verify_output_path_stability

    def link_then_verify(*args: object, **kwargs: object) -> None:
        os.link(output, alias)
        original(*args, **kwargs)

    monkeypatch.setattr(MODULE, "_verify_output_path_stability", link_then_verify)

    with pytest.raises(MODULE.OutputPathError, match="changed"):
        MODULE.write_new_output(output, b'{"stable":true}\n')

    assert not output.exists()
    assert alias.read_bytes() == b'{"stable":true}\n'


def test_output_parent_swap_returns_a_fixed_error_without_writing_through_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    output = parent / "inventory.json"
    original = MODULE._verify_output_path_stability

    def swap_then_verify(*args: object, **kwargs: object) -> None:
        moved = tmp_path / "moved-parent"
        os.rename(parent, moved)
        parent.symlink_to(outside, target_is_directory=True)
        original(*args, **kwargs)

    monkeypatch.setattr(MODULE, "_verify_output_path_stability", swap_then_verify)

    with pytest.raises(MODULE.OutputPathError, match="changed") as error:
        MODULE.write_new_output(output, b'{"stable":true}\n')

    assert "NotADirectoryError" not in str(error.value)
    assert not (outside / "inventory.json").exists()
    assert not (tmp_path / "moved-parent" / "inventory.json").exists()


def test_legacy_license_text_is_hashed_not_interpreted_or_emitted(tmp_path: Path) -> None:
    lock = _write_lock(
        tmp_path,
        _lock(
            development="[]",
            packages=_registry_package("runtime-lib", "1", hash_character="a"),
        ),
    )
    legacy = "private legacy license prose sentinel"
    distribution = _Distribution(
        tmp_path / "site",
        name="runtime-lib",
        version="1",
        metadata_values={"License": legacy},
        files=[],
    )

    first = MODULE.build_inventory(lock, distribution_lookup=_lookup({"runtime-lib": distribution}))
    second = MODULE.build_inventory(
        lock, distribution_lookup=_lookup({"runtime-lib": distribution})
    )
    evidence = _installed(first, "runtime-lib")["license_evidence"]
    serialized = MODULE.canonical_json_bytes(first)

    assert MODULE.canonical_json_bytes(first) == MODULE.canonical_json_bytes(second)
    assert evidence["determined_expression"] is None
    assert evidence["legacy_license_metadata"] == {
        "status": "PRESENT_UNINTERPRETED_REVIEW_REQUIRED",
        "sha256": hashlib.sha256(legacy.encode("utf-8")).hexdigest(),
        "byte_count": len(legacy.encode("utf-8")),
    }
    assert legacy.encode("utf-8") not in serialized
    assert first["decision"] == "REVIEW_REQUIRED_NO_LEGAL_COMPATIBILITY_DECISION"
    assert first["summary"]["legal_compatibility_decision"] == "NOT_MADE"
