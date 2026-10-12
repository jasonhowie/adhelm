"""Every locked runtime dependency is reviewed for Apache-2.0 distribution at its version."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_REVIEW = _ROOT / "docs/implementation/dependency-license-review.json"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", _ROOT / f"scripts/{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load("check_dependency_licenses")
inventory_module = _load("audit_dependency_licenses")


def _package(name: str, version: str = "1.0", *, declared: str | None = None) -> dict[str, Any]:
    installed: dict[str, Any] = {"status": "NOT_INSTALLED_REVIEW_REQUIRED"}
    if declared is not None:
        installed = {
            "status": "VERSION_MATCHED",
            "license_evidence": {"determined_expression": declared},
        }
    return {"name": name, "locked_version": version, "installed": installed}


def _unmatched(name: str, version: str, status: str) -> dict[str, Any]:
    """A package the scanner looked for but could not match to the lock."""
    return {"name": name, "locked_version": version, "installed": {"status": status}}


def _inventory(*packages: dict[str, Any]) -> dict[str, Any]:
    return {
        "inventory_kind": "DEPENDENCY_LICENSE_EVIDENCE_V1",
        "scope": "RUNTIME",
        "packages": list(packages),
    }


def _review(*entries: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_license": "Apache-2.0",
        "packages": [
            {"name": name, "version": version, "license": license_, "basis": "LICENSE_FILE"}
            for name, version, license_ in entries
        ],
    }


@pytest.mark.parametrize(
    "expression",
    [
        "Apache-2.0",
        "MIT",
        "BSD-3-Clause",
        "MPL-2.0",
        "PSF-2.0",
        "Apache-2.0 OR BSD-3-Clause",
        "MIT OR GPL-3.0-only",
        "Apache-2.0 AND MIT",
    ],
)
def test_expressions_made_only_of_compatible_licenses_are_accepted(expression: str) -> None:
    assert check.is_compatible(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "GPL-3.0-only",
        "GPL-2.0-or-later",
        "LGPL-3.0-only",
        "AGPL-3.0-only",
        "SSPL-1.0",
        "CC-BY-NC-4.0",
        "LicenseRef-Proprietary",
        "MIT AND GPL-3.0-only",
        "GPL-3.0-only OR AGPL-3.0-only",
        "MIT WITH Classpath-exception-2.0",
        "(MIT OR Apache-2.0)",
        "mit",
        "",
    ],
)
def test_copyleft_unknown_and_unparseable_expressions_are_refused(expression: str) -> None:
    assert not check.is_compatible(expression)


def test_a_reviewed_lock_graph_has_no_findings() -> None:
    inventory = _inventory(_package("httpx", "0.28.1"), _package("idna", "3.19", declared="MIT"))
    review = _review(("httpx", "0.28.1", "BSD-3-Clause"), ("idna", "3.19", "MIT"))

    assert check.findings(inventory, review) == []


def test_a_new_runtime_dependency_fails_until_it_is_reviewed() -> None:
    inventory = _inventory(_package("httpx", "0.28.1"), _package("newcomer", "2.0"))

    assert check.findings(inventory, _review(("httpx", "0.28.1", "BSD-3-Clause"))) == [
        "newcomer 2.0: not reviewed"
    ]


def test_an_upgraded_dependency_fails_until_its_review_is_renewed() -> None:
    inventory = _inventory(_package("httpx", "0.29.0"))

    assert check.findings(inventory, _review(("httpx", "0.28.1", "BSD-3-Clause"))) == [
        "httpx: locked at 0.29.0 but reviewed at 0.28.1"
    ]


def test_a_reviewed_package_that_left_the_lock_is_reported_stale() -> None:
    inventory = _inventory(_package("httpx", "0.28.1"))
    review = _review(("httpx", "0.28.1", "BSD-3-Clause"), ("gone", "1.0", "MIT"))

    assert check.findings(inventory, review) == [
        "gone: reviewed but no longer a runtime dependency"
    ]


def test_a_copyleft_review_is_refused_even_when_the_metadata_agrees() -> None:
    inventory = _inventory(_package("viral", "1.0", declared="GPL-3.0-only"))

    assert check.findings(inventory, _review(("viral", "1.0", "GPL-3.0-only"))) == [
        "viral: license 'GPL-3.0-only' is not Apache-2.0 compatible"
    ]


def test_a_review_that_contradicts_the_installed_metadata_is_refused() -> None:
    """A dependency whose metadata now says GPL cannot hide behind an old MIT review."""
    inventory = _inventory(_package("relicensed", "1.0", declared="GPL-3.0-only"))

    assert check.findings(inventory, _review(("relicensed", "1.0", "MIT"))) == [
        "relicensed: reviewed as 'MIT' but installed metadata says 'GPL-3.0-only'"
    ]


@pytest.mark.parametrize(
    "status",
    [
        "VERSION_MISMATCH_REVIEW_REQUIRED",
        "INSTALLED_VERSION_INVALID_OR_UNAVAILABLE_REVIEW_REQUIRED",
        "INSTALLED_NAME_MISMATCH_OR_UNAVAILABLE_REVIEW_REQUIRED",
        "METADATA_READ_FAILED_REVIEW_REQUIRED",
        "LICENSE_METADATA_READ_FAILED_REVIEW_REQUIRED",
        "SOMETHING_THE_SCANNER_ADDS_LATER",
    ],
)
def test_an_installed_package_that_could_not_be_compared_with_the_lock_is_a_finding(
    status: str,
) -> None:
    inventory = _inventory(_unmatched("httpx", "0.28.1", status))

    assert check.findings(inventory, _review(("httpx", "0.28.1", "BSD-3-Clause"))) == [
        f"httpx: the installed package could not be compared with the lock ({status})"
    ]


def test_a_package_that_is_not_installed_is_tolerated_but_not_counted_as_compared() -> None:
    inventory = _inventory(_package("httpx", "0.28.1"), _package("idna", "3.19", declared="MIT"))
    review = _review(("httpx", "0.28.1", "BSD-3-Clause"), ("idna", "3.19", "MIT"))

    assert check.findings(inventory, review) == []
    assert check.compared_count(inventory) == 1


def test_a_run_that_compared_no_installed_package_does_not_report_cleared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory = tmp_path / "inventory.json"
    review = tmp_path / "review.json"
    inventory.write_text(json.dumps(_inventory(_package("httpx", "0.28.1"))), encoding="utf-8")
    review.write_text(json.dumps(_review(("httpx", "0.28.1", "BSD-3-Clause"))), encoding="utf-8")

    assert check.main(["--inventory", str(inventory), "--review", str(review)]) == 1
    output = capsys.readouterr().out
    assert "No installed package was compared" in output and "CLEARED" not in output


def test_an_unrecognised_review_basis_is_refused() -> None:
    review = _review(("httpx", "0.28.1", "BSD-3-Clause"))
    review["packages"][0]["basis"] = "TRUST_ME"

    assert check.findings(_inventory(_package("httpx", "0.28.1")), review) == [
        "httpx: review basis 'TRUST_ME' is not recognised"
    ]


@pytest.mark.parametrize(
    "broken",
    [
        {"inventory_kind": "OTHER"},
        {"scope": "ALL"},
        {"scope": "DEVELOPMENT"},
    ],
)
def test_an_inventory_that_is_not_the_runtime_scan_is_refused(broken: dict[str, str]) -> None:
    inventory = {**_inventory(_package("httpx", "0.28.1")), **broken}

    assert len(check.findings(inventory, _review(("httpx", "0.28.1", "MIT")))) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda review: review.update(schema_version=2),
        lambda review: review.update(project_license="MIT"),
        lambda review: review.update(packages="none"),
        lambda review: review.update(packages=["httpx"]),
        lambda review: review.update(packages=[{"version": "1"}]),
        lambda review: review["packages"].append(copy.deepcopy(review["packages"][0])),
    ],
)
def test_a_malformed_review_record_is_refused(mutate: Any) -> None:
    review = _review(("httpx", "0.28.1", "BSD-3-Clause"))
    mutate(review)

    assert len(check.findings(_inventory(_package("httpx", "0.28.1")), review)) == 1


def test_the_command_reports_cleared_and_not_cleared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory = tmp_path / "inventory.json"
    review = tmp_path / "review.json"
    installed = _package("httpx", "0.28.1", declared="BSD-3-Clause")
    inventory.write_text(json.dumps(_inventory(installed)), encoding="utf-8")
    review.write_text(json.dumps(_review(("httpx", "0.28.1", "BSD-3-Clause"))), encoding="utf-8")
    arguments = ["--inventory", str(inventory), "--review", str(review)]

    assert check.main(arguments) == 0
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["status"] == "CLEARED" and cleared["installed_packages_compared"] == 1

    review.write_text(json.dumps(_review()), encoding="utf-8")
    assert check.main(arguments) == 1
    assert "httpx 0.28.1: not reviewed" in capsys.readouterr().out

    review.write_text("{", encoding="utf-8")
    assert check.main(arguments) == 2
    assert "Could not read" in capsys.readouterr().err


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="O4.3: scripts/audit_dependency_licenses.py reads the lock file and license records "
    "through POSIX descriptors (O_NONBLOCK, dir_fd); the dependency inventory is produced on the "
    "Linux build host, like the release one",
)
def test_the_locked_runtime_dependencies_are_cleared_for_apache_2_0() -> None:
    """The committed record covers the committed lock, and this environment's metadata agrees."""
    inventory = inventory_module.build_inventory(_ROOT / "uv.lock", scope="runtime")

    assert check.findings(inventory, json.loads(_REVIEW.read_text(encoding="utf-8"))) == []
    # The scan must have read real installed metadata, not skipped every package unseen.
    matched = [
        package
        for package in inventory["packages"]
        if package["installed"]["status"] == "VERSION_MATCHED"
    ]
    assert len(matched) >= 30
