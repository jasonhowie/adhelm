"""Check the relative links and heading anchors of Markdown documents.

A finding names the document, the link target and the reason; it never echoes content. Links
to other sites, code spans and fenced examples are not checked. Anchors follow GitHub's
heading slugs, including the numbered suffix a repeated heading gets.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from fnmatch import fnmatchcase
from pathlib import Path

EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
FENCE = re.compile(r"^\s*(```|~~~)")


def outside_fences(text: str) -> list[str]:
    """Return the lines of a Markdown document that are not inside a fenced block."""
    kept: list[str] = []
    fenced = False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            kept.append(line)
    return kept


def link_targets(text: str) -> list[str]:
    prose = "\n".join(re.sub(r"`[^`]*`", "", line) for line in outside_fences(text))
    inline = re.findall(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", prose)
    reference = re.findall(r"(?m)^ {0,3}\[[^\]]+\]:\s*(\S+)", prose)
    return [*inline, *reference]


def _github_slug(heading: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for line in outside_fences(path.read_text(encoding="utf-8")):
        match = re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if match is None:
            continue
        slug = _github_slug(match[1])
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def broken_links(
    root: Path,
    documents: Iterable[str],
    omitted: Sequence[str] = (),
    pending: Collection[str] = (),
) -> list[str]:
    """Locate relative links that do not resolve, leave the tree, or reach omitted files.

    `omitted` holds glob patterns for files a link may not reach even when they exist, and
    `pending` names documents that are allowed to be missing for now.
    """
    base = root.resolve()
    findings: list[str] = []
    for relative in documents:
        source = base / relative
        for target in link_targets(source.read_text(encoding="utf-8")):
            if EXTERNAL.match(target):
                continue
            path_part, _, anchor = target.partition("#")
            resolved = (source.parent / path_part).resolve() if path_part else source
            try:
                destination = resolved.relative_to(base).as_posix()
            except ValueError:
                findings.append(f"{relative}: {target}: leaves the repository")
                continue
            if any(fnmatchcase(destination, pattern) for pattern in omitted):
                findings.append(f"{relative}: {target}: omitted from the public tree")
            elif not resolved.exists():
                # A document named in `pending` has not landed yet; any other missing file,
                # including a mistyped name, is a finding.
                if destination not in pending:
                    findings.append(f"{relative}: {target}: missing")
            elif anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
                findings.append(f"{relative}: {target}: no such heading")
    return findings
