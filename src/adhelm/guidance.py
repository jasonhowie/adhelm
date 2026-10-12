"""Progressive read access to the authored guidance shipped in this installation."""

import hashlib
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

from adhelm.errors import AdhelmError

if sys.platform == "win32":
    from adhelm import windows_filesystem

_BUNDLES = {
    "ad-program-optimization": {
        "main": "SKILL.md",
        "program-analysis": "references/program-analysis.md",
    },
    "conversion-optimization": {
        "main": "SKILL.md",
        "funnel-experiments": "references/funnel-experiments.md",
        "measurement-workflow": "references/measurement-workflow.md",
    },
}
_MAX_DOCUMENT_BYTES = 65_536
_NAME = re.compile(r"name: ([a-z-]+)")
_DESCRIPTION = re.compile(r"description: ([^\r\n]+)")
_VERSION = re.compile(r'  version: "([0-9]+\.[0-9]+\.[0-9]+)"')


def _os_flag(name: str) -> int:
    value = getattr(os, name, 0)
    return value if type(value) is int else 0


def _is_directory(path: Path) -> bool:
    """Return whether a path is a real directory without following a link."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdhelmError("GUIDANCE_INVALID", "Installed guidance cannot be read safely.") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AdhelmError("GUIDANCE_INVALID", "Installed guidance must use real directories.")
    return True


def _bundle_root() -> Path:
    package = Path(__file__).resolve().parent
    installed = package / "bundled_skills"
    if _is_directory(installed):
        return installed
    # Hatch force-includes this directory in wheels. Source checkouts deliberately
    # keep the same authored files outside src; installed packages have no fallback.
    source_root = package.parent.parent
    source_skills = source_root / "skills"
    if (
        package.parent.name == "src"
        and (source_root / "pyproject.toml").is_file()
        and _is_directory(source_skills)
    ):
        return source_skills
    raise AdhelmError("GUIDANCE_NOT_INSTALLED", "The installed guidance bundle is unavailable.")


def _open_document(root: Path, skill: str, section: str) -> int:
    """Open an allowlisted file beneath a checked directory without path traversal."""
    parts = (skill, *_BUNDLES[skill][section].split("/"))
    if sys.platform == "win32":
        return _open_document_windows(root, parts)
    if os.open not in os.supports_dir_fd or not getattr(os, "O_NOFOLLOW", 0):
        raise AdhelmError(
            "GUIDANCE_INVALID", "This platform cannot safely read installed guidance files."
        )
    if any(part in {"", ".", ".."} for part in parts):
        raise AdhelmError("GUIDANCE_INVALID", "Installed guidance path is invalid.")

    descriptor = -1
    common_flags = os.O_RDONLY | _os_flag("O_NOFOLLOW") | _os_flag("O_CLOEXEC")
    try:
        descriptor = os.open(root, common_flags | _os_flag("O_DIRECTORY"))
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("Guidance root is not a directory")
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            flags = common_flags | (_os_flag("O_NONBLOCK") if final else _os_flag("O_DIRECTORY"))
            child = os.open(part, flags, dir_fd=descriptor)
            try:
                if not final and not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise OSError("Guidance path component is not a directory")
            except OSError:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise AdhelmError(
            "GUIDANCE_INVALID", "Installed guidance cannot traverse symbolic links or unsafe paths."
        ) from exc


if sys.platform == "win32":

    def _open_document_windows(root: Path, parts: tuple[str, ...]) -> int:
        """Open the document through directory handles held open, rejecting reparse points.

        The guidance root and every directory below it are opened as reparse
        points and stay pinned until the file handle exists, so no component can
        be swapped for a junction or link in between. Aliases above the root remain
        usable, as they are on POSIX.
        """
        try:
            directory = Path(root, *parts[:-1])
            pinned = windows_filesystem.open_directory(directory, depth=len(parts))
            try:
                descriptor = pinned.open_file(parts[-1])
            except BaseException:
                pinned.close()
                raise
            try:
                pinned.close()
            except OSError:
                os.close(descriptor)
                raise
            return descriptor
        except OSError as exc:
            raise AdhelmError(
                "GUIDANCE_INVALID",
                "Installed guidance cannot traverse symbolic links or unsafe paths.",
            ) from exc


def _read_document(skill: str, section: str) -> str:
    if skill not in _BUNDLES or section not in _BUNDLES[skill]:
        raise AdhelmError(
            "UNKNOWN_GUIDANCE",
            "Select an installed skill and section from guidance catalog.",
            exit_code=2,
        )
    root = _bundle_root()
    descriptor = _open_document(root, skill, section)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_DOCUMENT_BYTES:
            raise AdhelmError(
                "GUIDANCE_INVALID", "Installed guidance must be a bounded regular file."
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_DOCUMENT_BYTES + 1)
        if len(raw) > _MAX_DOCUMENT_BYTES:
            raise AdhelmError("GUIDANCE_INVALID", "Installed guidance exceeds its byte limit.")
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise AdhelmError("GUIDANCE_INVALID", "Installed guidance must be UTF-8 text.") from exc
    finally:
        os.close(descriptor)


def _metadata_from_main(skill: str, main: str) -> dict[str, str]:
    lines = main.splitlines()
    if not lines or lines[0] != "---":
        raise AdhelmError("GUIDANCE_INVALID", "Installed guidance metadata is invalid.")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise AdhelmError("GUIDANCE_INVALID", "Installed guidance metadata is invalid.") from exc
    header = lines[1:closing]
    names = [_NAME.fullmatch(line) for line in header]
    descriptions = [_DESCRIPTION.fullmatch(line) for line in header]
    metadata_indexes = [index for index, line in enumerate(header) if line == "metadata:"]
    versions = [_VERSION.fullmatch(line) for line in header]
    if (
        len([match for match in names if match is not None]) != 1
        or len([match for match in descriptions if match is not None]) != 1
        or len(metadata_indexes) != 1
        or len([match for match in versions if match is not None]) != 1
    ):
        raise AdhelmError("GUIDANCE_INVALID", "Installed guidance metadata is invalid.")
    name = next(match for match in names if match is not None)
    description = next(match for match in descriptions if match is not None)
    version = next(match for match in versions if match is not None)
    version_index = next(index for index, match in enumerate(versions) if match is not None)
    metadata_index = metadata_indexes[0]
    if name[1] != skill or version_index <= metadata_index:
        raise AdhelmError("GUIDANCE_INVALID", "Installed guidance metadata is invalid.")
    for line in header[metadata_index + 1 : version_index]:
        if line and not line.startswith((" ", "\t")):
            raise AdhelmError("GUIDANCE_INVALID", "Installed guidance metadata is invalid.")
    return {"name": skill, "guidance_version": version[1], "description": description[1]}


def _metadata(skill: str) -> dict[str, str]:
    return _metadata_from_main(skill, _read_document(skill, "main"))


def guidance_catalog() -> dict[str, Any]:
    return {
        "guidance": [
            {**_metadata(skill), "sections": list(sections)} for skill, sections in _BUNDLES.items()
        ],
        "provenance": "AUTHORED_INSTALLED_GUIDANCE",
        "capability_evidence": False,
        "execution_authority": False,
        "remote_side_effects": False,
    }


def guidance_document(skill: str, section: str = "main") -> dict[str, Any]:
    text = _read_document(skill, section)
    return {
        **_metadata_from_main(skill, text if section == "main" else _read_document(skill, "main")),
        "section": section,
        "document_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
        "total_characters": len(text),
        "provenance": "AUTHORED_INSTALLED_GUIDANCE",
        "capability_evidence": False,
        "execution_authority": False,
    }
