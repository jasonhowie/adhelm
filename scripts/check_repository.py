"""Offline portability and source/evidence checks; not a full credential/history scanner."""

import ast
import fnmatch
import hashlib
import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from adhelm import RELEASE_STATE

ROOT = Path(__file__).resolve().parents[1]

# The product was renamed to AdHelm (ADR 0013). The retired names are matched in file contents
# and in path names, case-insensitively, so `ADCP_*`, `AdcpError`, `com.example.adcp` and the
# old distribution name are all covered.
RETIRED_NAMES = re.compile(r"adcp|local[-_]ad[-_]control[-_]plane", re.IGNORECASE)

# The product's home is https://aptsonic.com/adhelm and its reverse-DNS root is
# `com.aptsonic.adhelm` (ADR 0013, owner decision 2026-10-12). The `adhelm.dev` domain and the
# `dev.adhelm` identifier root were never used publicly and must not reappear. Only the dated
# note in ADR 0013 records them (exempt by section, not by file), and this check and its tests
# spell them (exempt by file).
RETIRED_IDENTIFIERS = re.compile(r"\badhelm\.dev\b|(?<![A-Za-z0-9-])dev\.adhelm\b", re.IGNORECASE)
RETIRED_IDENTIFIER_ALLOWLIST = (
    "scripts/check_repository.py",
    "tests/test_check_repository.py",
)
RETIRED_IDENTIFIER_NOTE = (
    "docs/adr/0013-open-source-release.md",
    "### Domain and identifier root (2026-10-12)",
)

# Files that keep the retired names because they record what was true before the rename or
# define this check. Everything else, including every other ADR, must use the current names.
RETIRED_NAME_ALLOWLIST = (
    # Historical decision records that predate the rename; ADR 0013 states the rename itself.
    "docs/adr/0001-local-runtime.md",
    "docs/adr/0001-stack-and-package-layout.md",
    "docs/adr/0002-cli-result-and-exit-codes.md",
    "docs/adr/0002-machine-interface-and-editions.md",
    "docs/adr/0013-open-source-release.md",
    # Private build records that the public export omits (docs/implementation/oss-0.1.0-plan.md).
    "BUILD_PROMPT.md",
    "docs/implementation/oss-0.1.0-plan.md",
    "docs/implementation/terra-finishing-plan.md",
    "docs/implementation/status.md",
    "docs/implementation/finishing-*",
    "docs/implementation/*-validation*.json",
    # This check and its tests spell the names they search for.
    "scripts/check_repository.py",
    "tests/test_check_repository.py",
)


def repository_files(root: Path) -> list[str]:
    """Return tracked and not-yet-added, non-ignored files as POSIX paths relative to `root`."""
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
    ).stdout.decode()
    return sorted({name for name in listing.split("\0") if name})


def find_retired_names(
    root: Path,
    paths: Iterable[str],
    allowlist: Iterable[str] = RETIRED_NAME_ALLOWLIST,
    names: re.Pattern[str] = RETIRED_NAMES,
) -> list[str]:
    """Locate retired names outside the allowlist; the findings carry no file content."""
    patterns = tuple(allowlist)
    name_bytes = re.compile(names.pattern.encode("ascii"), names.flags & re.IGNORECASE)
    findings: list[str] = []
    for relative in paths:
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
            continue
        if names.search(relative):
            findings.append(f"{relative}: path")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            continue
        content = path.read_bytes()
        try:
            lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            # Images, archives and executables can carry an ASCII product name (PNG text
            # chunks, wheel or zip member names, embedded strings), so search the raw bytes.
            # There are no line numbers to report and no content is echoed.
            if name_bytes.search(content):
                findings.append(f"{relative}: binary")
            continue
        findings.extend(
            f"{relative}:{number}"
            for number, line in enumerate(lines, start=1)
            if names.search(line)
        )
    return findings


def _section_line_numbers(root: Path, relative: str, heading: str) -> set[int]:
    """Return the 1-based lines from `heading` up to, not including, the next heading."""
    path = root / relative
    if path.is_symlink() or not path.is_file():
        return set()
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == heading]
    if not starts:
        return set()
    start = starts[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("#")),
        len(lines),
    )
    return set(range(start + 1, end + 1))


def find_retired_identifiers(
    root: Path,
    paths: Iterable[str],
    allowlist: Iterable[str] = RETIRED_IDENTIFIER_ALLOWLIST,
    note: tuple[str, str] = RETIRED_IDENTIFIER_NOTE,
) -> list[str]:
    """Locate the retired `adhelm.dev` domain and `dev.adhelm` identifier root.

    Whole files in `allowlist` are exempt. In the `note` file only the lines of the named
    section are exempt, so the same text anywhere else in that file is still reported.
    """
    findings = find_retired_names(root, paths, allowlist, RETIRED_IDENTIFIERS)
    note_path, heading = note
    exempt = {f"{note_path}:{number}" for number in _section_line_numbers(root, note_path, heading)}
    return [finding for finding in findings if finding not in exempt]


def check_evidence_references(trace: dict[str, object]) -> None:
    """Check indexed source/test links without treating their existence as a pass."""
    requirements = trace["requirements"]
    assert isinstance(requirements, list)
    seen: set[str] = set()
    source_lines = (ROOT / "docs/definition-of-done.md").read_text().splitlines()
    test_nodes: dict[Path, set[str]] = {}
    for item in requirements:
        content_digest = hashlib.sha256(item["requirement"].encode()).hexdigest()
        assert item["requirement_digest"] == content_digest, item["requirement_id"]
        assert item["requirement_id"] not in seen, item["requirement_id"]
        seen.add(item["requirement_id"])
        index = item["source_line"] - 1
        assert item["source"] == "docs/definition-of-done.md"
        assert 0 <= index < len(source_lines) and source_lines[index].startswith("- ")
        paragraph = [source_lines[index][2:]]
        index += 1
        while index < len(source_lines) and source_lines[index].startswith("  "):
            paragraph.append(source_lines[index].strip())
            index += 1
        assert item["requirement"] == " ".join(paragraph), item["requirement_id"]
        for reference in item["evidence"] + item["tests"]:
            relative, *nodes = reference.split("::")
            path = (ROOT / relative).resolve()
            assert path.is_relative_to(ROOT) and path.is_file(), reference
            if not nodes:
                continue
            assert relative.startswith("tests/") and path.suffix == ".py", reference
            if path not in test_nodes:
                tree = ast.parse(path.read_text(), filename=relative)
                names = set()
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        names.add(node.name)
                    elif isinstance(node, ast.ClassDef):
                        names.update(
                            node.name + "::" + member.name
                            for member in node.body
                            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                        )
                test_nodes[path] = names
            assert "::".join(nodes) in test_nodes[path], reference


def main() -> None:
    trace = json.loads((ROOT / "docs/implementation/traceability.json").read_text())
    assert (
        trace["source_digest"]
        == hashlib.sha256((ROOT / "docs/definition-of-done.md").read_bytes()).hexdigest()
    )
    assert len(trace["requirements"]) >= 100
    assert trace["release_state"] != "COMPLETE", "Release proof is not yet present"
    check_evidence_references(trace)
    retired = find_retired_names(ROOT, repository_files(ROOT))
    assert not retired, f"Retired product name outside the allowlist: {retired[:20]}"
    identifiers = find_retired_identifiers(ROOT, repository_files(ROOT))
    assert not identifiers, f"Retired domain or identifier root: {identifiers[:20]}"
    for folder in (ROOT / "skills").iterdir():
        skill = (folder / "SKILL.md").read_text()
        assert skill.startswith("---\n")
        assert re.search(r"^name: .+", skill, re.M)
        assert re.search(r"^description: .+", skill, re.M)
        for path in re.findall(r"\]\((references/[^)#]+)(?:#[^)]*)?\)", skill):
            assert (folder / path).is_file(), path
        assert "/Users/" not in skill
    print(
        json.dumps(
            {
                "status": "PASSED",
                "kind": "OFFLINE_REPOSITORY_CHECK",
                "release_state": RELEASE_STATE,
            }
        )
    )


if __name__ == "__main__":
    main()
