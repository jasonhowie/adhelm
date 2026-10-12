#!/usr/bin/env python3
"""Check the locked runtime dependencies against the reviewed Apache-2.0 license record.

`audit_dependency_licenses.py` inventories license evidence and, by design, decides
nothing. This script holds the decision the 0.1.0 release needs: every package reachable
from the project's runtime dependencies must appear in the reviewed record at its locked
version, with a license expression that can be redistributed beside an Apache-2.0 work, and
the record may not disagree with the license metadata the installed package declares. A new
or upgraded dependency fails until a person reviews it and updates the record. An installed
package whose metadata could not be matched to the lock is a finding, and so is a run that
compared no installed package at all, so an environment that drifted from the lock or was never
synced cannot report a clearance. This is an
engineering check of declared metadata and license files, not legal advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_LICENSE = "Apache-2.0"
# Permissive licenses, plus MPL-2.0, whose copyleft is file level and applies only to changes
# made to the MPL-covered files. AdHelm installs such a package unmodified as a dependency
# and never bundles or patches it.
COMPATIBLE_SPDX_IDS = frozenset(
    {
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "MIT",
        "MIT-0",
        "MIT-CMU",
        "MPL-2.0",
        "PSF-2.0",
        "Python-2.0",
    }
)
REVIEW_BASES = frozenset({"PEP_639_METADATA", "TROVE_CLASSIFIER", "LICENSE_FILE"})
# The scanner could not look at the package, or found a different one: a finding, not a pass.
# NOT_INSTALLED is the one tolerated status, because the lock covers packages that only apply on
# other platforms; `compared_count` makes sure the tolerance cannot swallow the whole graph.
INSTALLED_MATCHED = "VERSION_MATCHED"
INSTALLED_ABSENT = "NOT_INSTALLED_REVIEW_REQUIRED"
DEFAULT_REVIEW = (
    Path(__file__).resolve().parents[1] / "docs/implementation/dependency-license-review.json"
)


def is_compatible(expression: str) -> bool:
    """Whether the SPDX expression can be satisfied using only compatible identifiers.

    Only `OR` and `AND` are understood. Parentheses, `WITH` exceptions and unknown
    identifiers are not compatible, so an unfamiliar expression fails closed.
    """
    return any(
        all(term.strip() in COMPATIBLE_SPDX_IDS for term in alternative.split(" AND "))
        for alternative in expression.split(" OR ")
    )


def findings(inventory: Mapping[str, Any], review: Mapping[str, Any]) -> list[str]:
    """Return one line per way the runtime lock graph departs from the reviewed record."""
    if inventory.get("inventory_kind") != "DEPENDENCY_LICENSE_EVIDENCE_V1":
        return ["The inventory is not a DEPENDENCY_LICENSE_EVIDENCE_V1 document."]
    if inventory.get("scope") != "RUNTIME":
        return ["The inventory must be taken with --scope runtime."]
    if review.get("schema_version") != 1 or review.get("project_license") != PROJECT_LICENSE:
        return [f"The review record must be schema 1 for {PROJECT_LICENSE}."]
    entries = review.get("packages")
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in entries
    ):
        return ["The review record must list its packages as objects with a name."]
    reviewed = {entry["name"]: entry for entry in entries}
    if len(reviewed) != len(entries):
        return ["The review record lists a package more than once."]
    problems: list[str] = []
    locked = set()
    for package in inventory["packages"]:
        name, version = package["name"], package["locked_version"]
        locked.add(name)
        entry = reviewed.get(name)
        if entry is None:
            problems.append(f"{name} {version}: not reviewed")
            continue
        if entry.get("version") != version:
            problems.append(f"{name}: locked at {version} but reviewed at {entry.get('version')}")
        if entry.get("basis") not in REVIEW_BASES:
            problems.append(f"{name}: review basis {entry.get('basis')!r} is not recognised")
        expression = entry.get("license")
        if not isinstance(expression, str) or not is_compatible(expression):
            problems.append(f"{name}: license {expression!r} is not Apache-2.0 compatible")
        installed = package["installed"]
        status = installed["status"]
        declared = None
        if status == INSTALLED_MATCHED:
            declared = installed["license_evidence"].get("determined_expression")
        elif status != INSTALLED_ABSENT:
            problems.append(
                f"{name}: the installed package could not be compared with the lock ({status})"
            )
        if declared is not None and declared != expression:
            problems.append(
                f"{name}: reviewed as {expression!r} but installed metadata says {declared!r}"
            )
    problems.extend(
        f"{name}: reviewed but no longer a runtime dependency"
        for name in sorted(set(reviewed) - locked)
    )
    return problems


def compared_count(inventory: Mapping[str, Any]) -> int:
    """How many locked packages had installed metadata that matched the lock and was read."""
    return sum(
        package["installed"]["status"] == INSTALLED_MATCHED for package in inventory["packages"]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        type=Path,
        required=True,
        help="JSON from audit_dependency_licenses.py --scope runtime",
    )
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    args = parser.parse_args(argv)
    try:
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
        review = json.loads(args.review.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"Could not read the inventory or review record: {error}", file=sys.stderr)
        return 2
    problems = findings(inventory, review)
    if not problems and compared_count(inventory) == 0:
        problems = [
            "No installed package was compared with the lock: run `uv sync --frozen` first."
        ]
    if problems:
        print(f"Runtime dependencies that are not cleared for {PROJECT_LICENSE} distribution:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(
        json.dumps(
            {
                "status": "CLEARED",
                "project_license": PROJECT_LICENSE,
                "runtime_packages": len(inventory["packages"]),
                "installed_packages_compared": compared_count(inventory),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
