#!/usr/bin/env python3
"""Check, checksum and describe the release artifacts the release workflow builds once.

The workflow builds the sdist and wheel a single time. Every later stage (the install
smoke on three platforms, the checksums, the SBOM, the attestation and the PyPI upload)
handles those exact files, and each stage re-checks them with this script instead of
trusting the stage before it. Nothing here signs, uploads or contacts a network.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tomllib
import uuid
import zipfile
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT_NAME = "adhelm"
LICENSE_EXPRESSION = "Apache-2.0"
LICENSE_FILES = ("LICENSE", "NOTICE")
CHECKSUM_FILE = "SHA256SUMS"
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)")
_BUNDLED_SKILL_MINIMUM = 2
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_GIT_TIMEOUT_SECONDS = 60
SBOM_SCOPE = "RUNTIME_LOCK_GRAPH"
# A relative markdown link in the long description resolves against pypi.org and 404s there.
_CODE = re.compile(r"^(```|~~~).*?^\1[^\n]*$|`[^`\n]*`", re.DOTALL | re.MULTILINE)
_TAGGED_LINK = re.compile(r"https://github\.com/jasonhowie/adhelm/blob/(v[^/\s)]+)/")
_RELATIVE_LINK = re.compile(
    r"\]\((?!https?://|#|mailto:)[^)\s][^)]*\)"  # [text](docs/guide.md)
    r"|^\s*\[[^\]]+\]:\s*(?!https?://|#|mailto:)\S"  # [text]: docs/guide.md
    r"|\b(?:href|src)=[\"'](?!https?://|#|mailto:)",  # <a href="docs/guide.md">
    re.MULTILINE,
)


class ReleaseError(RuntimeError):
    """An artifact, tag or checksum does not satisfy the release definition."""


def project_version(pyproject: Path) -> str:
    """Return the version from pyproject.toml after checking the distribution name."""
    project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    if project.get("name") != PROJECT_NAME:
        raise ReleaseError(f"pyproject.toml must name the project {PROJECT_NAME}.")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ReleaseError("pyproject.toml must carry a static project version.")
    return version


def require_tag(tag: str, version: str) -> None:
    """A release tag is `v` plus the exact package version; nothing else may publish."""
    if tag != f"v{version}":
        raise ReleaseError(f"Tag {tag!r} does not match package version {version} (v{version}).")


def _require_regular(path: Path) -> Path:
    """Reject a symlink, device or oversized file before anything reads it."""
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_ARTIFACT_BYTES:
        raise ReleaseError(f"{path.name} must be a regular file within the size bound.")
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(_require_regular(path).read_bytes()).hexdigest()


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or not name:
        raise ReleaseError(f"Archive member {name!r} escapes the archive root.")
    return path


def _wheel_members(wheel: Path) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(wheel) as archive:
            members: dict[str, bytes] = {}
            for info in archive.infolist():
                _safe_member(info.filename)
                if not info.is_dir():
                    members[info.filename] = archive.read(info)
            return members
    except zipfile.BadZipFile as error:
        raise ReleaseError(f"{wheel.name} is not a valid wheel archive.") from error


def _sdist_members(sdist: Path, root: str) -> dict[str, bytes]:
    try:
        with tarfile.open(sdist, "r:gz") as archive:
            members: dict[str, bytes] = {}
            for info in archive.getmembers():
                path = _safe_member(info.name)
                if path.parts[0] != root:
                    raise ReleaseError(f"{sdist.name} member {info.name!r} is outside {root}/.")
                if info.isdir():
                    continue
                if not info.isfile():
                    raise ReleaseError(f"{sdist.name} member {info.name!r} is not a regular file.")
                extracted = archive.extractfile(info)
                if extracted is None:
                    raise ReleaseError(f"{sdist.name} member {info.name!r} is unreadable.")
                members["/".join(path.parts[1:])] = extracted.read()
            return members
    except (tarfile.TarError, EOFError) as error:
        raise ReleaseError(f"{sdist.name} is not a valid gzip sdist.") from error


def _require_license_files(members: dict[str, bytes], prefix: str, root: Path, label: str) -> None:
    for name in LICENSE_FILES:
        packaged = members.get(f"{prefix}{name}")
        if packaged is None:
            raise ReleaseError(f"{label} does not carry {name}.")
        if packaged != (root / name).read_bytes():
            raise ReleaseError(f"{label} {name} differs from the repository {name}.")


def _long_description(metadata_bytes: bytes) -> str:
    """The METADATA body, which is the project's README as PyPI renders it."""
    _, _, body = metadata_bytes.partition(b"\n\n")
    text = body.decode("utf-8", errors="replace")
    # Code is shown literally, never rendered as a link.
    return _CODE.sub("", text)


def _require_portable_description(label: str, metadata_bytes: bytes, version: str) -> None:
    """The long description must read correctly on pypi.org, away from the repository.

    A relative link resolves against the PyPI page and 404s, and a link pinned to another
    release's tag means the substitution in pyproject.toml was not updated with the version.
    """
    description = _long_description(metadata_bytes)
    relative = [match.group(0).strip() for match in _RELATIVE_LINK.finditer(description)]
    if relative:
        raise ReleaseError(
            f"{label} long description has relative links that break on PyPI: "
            f"{sorted(set(relative))[:5]}."
        )
    stale = sorted({tag for tag in _TAGGED_LINK.findall(description) if tag != f"v{version}"})
    if stale:
        raise ReleaseError(f"{label} long description links to {stale}, not v{version}.")


def verify_wheel(wheel: Path, version: str, root: Path) -> None:
    members = _wheel_members(_require_regular(wheel))
    require_release_constants(members.get("adhelm/__init__.py", b""), version)
    dist_info = f"{PROJECT_NAME}-{version}.dist-info"
    stray = {
        PurePosixPath(name).parts[0]
        for name in members
        if PurePosixPath(name).parts[0] not in {PROJECT_NAME, dist_info}
    }
    if stray:
        raise ReleaseError(
            f"{wheel.name} carries top-level entries outside the package: {sorted(stray)}."
        )
    metadata_bytes = members.get(f"{dist_info}/METADATA")
    if metadata_bytes is None:
        raise ReleaseError(f"{wheel.name} has no METADATA.")
    metadata = BytesParser(policy=compat32).parsebytes(metadata_bytes)
    if metadata.get("Name") != PROJECT_NAME or metadata.get("Version") != version:
        raise ReleaseError(f"{wheel.name} metadata does not name {PROJECT_NAME} {version}.")
    if metadata.get("License-Expression") != LICENSE_EXPRESSION:
        raise ReleaseError(f"{wheel.name} must declare License-Expression: {LICENSE_EXPRESSION}.")
    if sorted(metadata.get_all("License-File") or []) != sorted(LICENSE_FILES):
        raise ReleaseError(f"{wheel.name} must declare exactly {', '.join(LICENSE_FILES)}.")
    _require_license_files(members, f"{dist_info}/licenses/", root, wheel.name)
    if not metadata.get("Requires-Python"):
        raise ReleaseError(f"{wheel.name} metadata has no Requires-Python.")
    _require_portable_description(wheel.name, metadata_bytes, version)
    if f"{PROJECT_NAME}/py.typed" not in members:
        raise ReleaseError(f"{wheel.name} is missing py.typed.")
    skills = [
        name
        for name in members
        if re.fullmatch(rf"{PROJECT_NAME}/bundled_skills/[^/]+/SKILL\.md", name)
    ]
    if len(skills) < _BUNDLED_SKILL_MINIMUM:
        raise ReleaseError(f"{wheel.name} is missing the bundled skills.")
    if f"{PROJECT_NAME}/pilot_record_files/README.md" not in members:
        raise ReleaseError(f"{wheel.name} is missing the committed pilot records directory.")


def verify_sdist(sdist: Path, version: str, root: Path) -> None:
    members = _sdist_members(_require_regular(sdist), f"{PROJECT_NAME}-{version}")
    require_release_constants(members.get("src/adhelm/__init__.py", b""), version)
    for required in ("pyproject.toml", "PKG-INFO", "README.md"):
        if required not in members:
            raise ReleaseError(f"{sdist.name} is missing {required}.")
    _require_license_files(members, "", root, sdist.name)
    _require_portable_description(sdist.name, members["PKG-INFO"], version)


def require_release_constants(source: bytes, version: str) -> None:
    """Inspect the packaged source, without executing code from an artifact."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        raise ReleaseError("The artifact has invalid package release constants.") from None
    values: dict[str, list[object]] = {"__version__": [], "RELEASE_STATE": []}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in values:
                values[target.id].append(value.value if isinstance(value, ast.Constant) else None)
    if values != {"__version__": [version], "RELEASE_STATE": ["RELEASED_0_1_0"]}:
        raise ReleaseError(
            "The artifact version or release state does not match the read-only release."
        )


def verify_artifacts(dist: Path, pyproject: Path, tag: str | None = None) -> dict[str, str]:
    """Require exactly the named wheel and sdist, each carrying the license files."""
    version = project_version(pyproject)
    if tag is not None:
        require_tag(tag, version)
    wheel_name = f"{PROJECT_NAME}-{version}-py3-none-any.whl"
    sdist_name = f"{PROJECT_NAME}-{version}.tar.gz"
    # uv writes a `.gitignore` into the directory it builds into; it is not an artifact.
    present = sorted(entry.name for entry in dist.iterdir() if entry.name != ".gitignore")
    if present != sorted([wheel_name, sdist_name]):
        raise ReleaseError(
            f"{dist} must hold exactly {wheel_name} and {sdist_name}; found {present}."
        )
    root = pyproject.parent
    verify_wheel(dist / wheel_name, version, root)
    verify_sdist(dist / sdist_name, version, root)
    return {name: _digest(dist / name) for name in (sdist_name, wheel_name)}


def _release_files(directory: Path) -> list[Path]:
    files = sorted(entry for entry in directory.iterdir() if entry.name != CHECKSUM_FILE)
    if not files:
        raise ReleaseError(f"{directory} holds no files to checksum.")
    return files


def write_checksums(directory: Path) -> str:
    """Write a `sha256sum -c` compatible SHA256SUMS covering every file in the directory."""
    lines = [f"{_digest(path)}  {path.name}\n" for path in _release_files(directory)]
    for line in lines:
        if _CHECKSUM_LINE.fullmatch(line.rstrip("\n")) is None:
            raise ReleaseError(
                f"Unsupported file name for a checksum line: {line.split('  ')[1]!r}."
            )
    text = "".join(lines)
    (directory / CHECKSUM_FILE).write_text(text, encoding="utf-8", newline="\n")
    return text


def check_checksums(directory: Path) -> None:
    """Every listed file matches and no unlisted file sits beside them."""
    try:
        listing = (directory / CHECKSUM_FILE).read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseError(f"{directory} has no readable {CHECKSUM_FILE}.") from error
    expected: dict[str, str] = {}
    for line in listing.splitlines():
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None or match.group(2) in expected:
            raise ReleaseError(f"{CHECKSUM_FILE} has a malformed or duplicate line.")
        expected[match.group(2)] = match.group(1)
    actual = {path.name for path in _release_files(directory)}
    if actual != set(expected):
        raise ReleaseError(f"{CHECKSUM_FILE} does not list exactly the files present.")
    for name, digest in expected.items():
        if _digest(directory / name) != digest:
            raise ReleaseError(f"{name} does not match its recorded SHA-256.")


def lock_digest(pyproject: Path) -> str:
    """SHA-256 of the `uv.lock` that sits beside the project file."""
    return _digest(pyproject.parent / "uv.lock")


def _sbom_serial(commit: str, digest: str, epoch: int) -> str:
    """The serial number derived from the source, so the same source yields the same SBOM.

    The scope is part of the derivation so the release SBOM never shares a serial with the
    development runtime-lock graph that `validate_development.py` builds from the same inputs.
    """
    name = f"adhelm:{SBOM_SCOPE}:{commit}:{digest}:{epoch}"
    return "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _sbom_timestamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _sbom_properties(commit: str, digest: str, epoch: int) -> list[dict[str, str]]:
    return [
        {"name": "adhelm:source_commit", "value": commit},
        {"name": "adhelm:uv_lock_sha256", "value": digest},
        {"name": "adhelm:source_date_epoch", "value": str(epoch)},
        {"name": "adhelm:scope", "value": SBOM_SCOPE},
    ]


def normalise_sbom(
    document: dict[str, Any], *, commit: str, digest: str, epoch: int
) -> dict[str, Any]:
    """Bind a `uv export` CycloneDX document to its source commit and lock digest.

    `uv export` stamps a random serial number and the wall-clock time. The release SBOM is
    shipped in `SHA256SUMS`, so those two are replaced with values derived from the commit,
    the `uv.lock` digest and `SOURCE_DATE_EPOCH` (the same derivation
    `scripts/validate_development.py` uses), and the commit and digest are recorded as
    properties. Running this twice on one commit gives identical bytes.
    """
    if _COMMIT.fullmatch(commit) is None:
        raise ReleaseError("The source commit must be a full lowercase commit hash.")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(document.get("components"), list):
        raise ReleaseError("uv did not produce the expected CycloneDX runtime-lock graph.")
    existing = metadata.get("properties", [])
    if not isinstance(existing, list):
        raise ReleaseError("CycloneDX metadata properties are invalid.")
    ours = {entry["name"] for entry in _sbom_properties(commit, digest, epoch)}
    kept = [
        entry for entry in existing if not (isinstance(entry, dict) and entry.get("name") in ours)
    ]
    document["serialNumber"] = _sbom_serial(commit, digest, epoch)
    metadata["timestamp"] = _sbom_timestamp(epoch)
    metadata["properties"] = [*kept, *_sbom_properties(commit, digest, epoch)]
    return document


def serialise_sbom(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"


def verify_sbom(sbom: Path, pyproject: Path, *, commit: str, epoch: int) -> None:
    """The SBOM is CycloneDX 1.5, describes this project, lists its locked dependencies and is
    bound to this checkout: the commit, the `uv.lock` digest and the derived serial and time."""
    try:
        document = json.loads(_require_regular(sbom).read_bytes())
    except ValueError as error:
        raise ReleaseError(f"{sbom.name} is not valid JSON.") from error
    if not isinstance(document, dict):
        raise ReleaseError(f"{sbom.name} must be a JSON object.")
    if document.get("bomFormat") != "CycloneDX" or document.get("specVersion") != "1.5":
        raise ReleaseError(f"{sbom.name} must be a CycloneDX 1.5 document.")
    metadata = document.get("metadata")
    root = metadata.get("component") if isinstance(metadata, dict) else None
    version = project_version(pyproject)
    if not isinstance(root, dict) or (root.get("name"), root.get("version")) != (
        PROJECT_NAME,
        version,
    ):
        raise ReleaseError(f"{sbom.name} does not describe {PROJECT_NAME} {version}.")
    components = document.get("components")
    if not isinstance(components, list) or not components:
        raise ReleaseError(f"{sbom.name} lists no components.")
    for component in components:
        purl = component.get("purl") if isinstance(component, dict) else None
        if not isinstance(purl, str) or not purl.startswith("pkg:pypi/"):
            raise ReleaseError(f"{sbom.name} has a component without a PyPI package URL.")
    _require_sbom_binding(document, sbom.name, pyproject, commit, epoch)


def _require_sbom_binding(
    document: dict[str, Any],
    name: str,
    pyproject: Path,
    commit: str,
    epoch: int,
) -> None:
    if _COMMIT.fullmatch(commit) is None:
        raise ReleaseError("The source commit must be a full lowercase commit hash.")
    digest = lock_digest(pyproject)
    properties = document["metadata"].get("properties")
    recorded = (
        {entry.get("name"): entry.get("value") for entry in properties if isinstance(entry, dict)}
        if isinstance(properties, list)
        else {}
    )
    for entry in _sbom_properties(commit, digest, epoch):
        if recorded.get(entry["name"]) != entry["value"]:
            raise ReleaseError(
                f"{name} property {entry['name']} is {recorded.get(entry['name'])!r}, "
                f"expected {entry['value']!r} for this checkout."
            )
    if document.get("serialNumber") != _sbom_serial(commit, digest, epoch):
        raise ReleaseError(f"{name} serial number is not derived from the commit and lock file.")
    if document["metadata"].get("timestamp") != _sbom_timestamp(epoch):
        raise ReleaseError(f"{name} timestamp is not SOURCE_DATE_EPOCH.")


def _git_output(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseError(f"git could not be run in {root}: {error}") from error
    if result.returncode != 0:
        raise ReleaseError(f"git {arguments[0]} failed in {root}: {result.stderr.strip()}")
    return result.stdout.strip()


def checkout_commit(root: Path) -> str:
    return _git_output(root, "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}")


def checkout_epoch(root: Path) -> int:
    """SOURCE_DATE_EPOCH when set (as the build step sets it), else the commit time."""
    configured = os.environ.get("SOURCE_DATE_EPOCH")
    if configured is not None:
        if not configured.isdecimal():
            raise ReleaseError("SOURCE_DATE_EPOCH must be a nonnegative integer.")
        return int(configured)
    return int(_git_output(root, "log", "-1", "--format=%ct"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    version = commands.add_parser("version", help="print the package version")
    version.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    verify = commands.add_parser("verify", help="check the built wheel and sdist")
    verify.add_argument("--dist", type=Path, required=True)
    verify.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    verify.add_argument("--tag", help="the release tag, which must be v plus the package version")
    for name, help_text in (
        ("write-checksums", f"write {CHECKSUM_FILE} for every file in a directory"),
        ("check-checksums", f"check a directory against its {CHECKSUM_FILE}"),
    ):
        commands.add_parser(name, help=help_text).add_argument("--dir", type=Path, required=True)
    for name, help_text in (
        (
            "normalise-sbom",
            "bind a `uv export` CycloneDX file to this commit and uv.lock, in place",
        ),
        ("verify-sbom", "check the CycloneDX SBOM and its binding to this checkout"),
    ):
        sbom = commands.add_parser(name, help=help_text)
        sbom.add_argument("--sbom", type=Path, required=True)
        sbom.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "version":
            print(project_version(args.pyproject))
        elif args.command == "verify":
            digests = verify_artifacts(args.dist, args.pyproject, args.tag)
            print(json.dumps({"status": "VERIFIED", "sha256": digests}, sort_keys=True))
        elif args.command == "write-checksums":
            sys.stdout.write(write_checksums(args.dir))
        elif args.command == "check-checksums":
            check_checksums(args.dir)
            print(json.dumps({"status": "VERIFIED"}))
        else:
            root = args.pyproject.resolve().parent
            commit, epoch = checkout_commit(root), checkout_epoch(root)
            if args.command == "normalise-sbom":
                document = json.loads(_require_regular(args.sbom).read_bytes())
                normalised = normalise_sbom(
                    document, commit=commit, digest=lock_digest(args.pyproject), epoch=epoch
                )
                args.sbom.write_text(serialise_sbom(normalised), encoding="utf-8", newline="\n")
                print(json.dumps({"status": "NORMALISED", "source_commit": commit}))
            else:
                verify_sbom(args.sbom, args.pyproject, commit=commit, epoch=epoch)
                print(json.dumps({"status": "VERIFIED"}))
    except (ReleaseError, OSError, ValueError) as error:
        print(f"release check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
