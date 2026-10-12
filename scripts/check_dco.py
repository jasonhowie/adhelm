#!/usr/bin/env python3
"""Require a Developer Certificate of Origin sign-off on every commit in a pull request.

Each non-merge commit in `base..head` must carry a `Signed-off-by: Name <email>` trailer
whose email is the commit's author or committer email. The key is matched without regard to
case, so `signed-off-by:` counts exactly as `Signed-off-by:` does, which is how the DCO app
reads it. Trailers are read with `git interpret-trailers`, so a sign-off buried in the message
body does not count.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SIGN_OFF = re.compile(
    r"(?i:signed-off-by): (?P<name>[^<>\n]*[^<>\s]) <(?P<email>[^<>@\s]+@[^<>\s]+)>"
)
_GIT_TIMEOUT_SECONDS = 60


class DcoError(RuntimeError):
    """The range cannot be read, so no sign-off conclusion is possible."""


def _git(repository: Path, *arguments: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise DcoError(f"git {arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout


def commit_problem(repository: Path, commit: str) -> str | None:
    """Return why `commit` lacks a valid sign-off, or None when it has one."""
    author, committer, message = _git(
        repository, "show", "--no-patch", "--format=%ae%x00%ce%x00%B", commit
    ).split("\0", 2)
    trailers = _git(repository, "interpret-trailers", "--parse", stdin=message).splitlines()
    sign_offs = [line for line in trailers if line.lower().startswith("signed-off-by:")]
    if not sign_offs:
        return "has no Signed-off-by trailer"
    allowed = {author.lower(), committer.lower()}
    for line in sign_offs:
        match = _SIGN_OFF.fullmatch(line)
        if match is not None and match.group("email").lower() in allowed:
            return None
    if all(_SIGN_OFF.fullmatch(line) is None for line in sign_offs):
        return "has a Signed-off-by trailer that is not 'Name <email>'"
    return "has a Signed-off-by email that is neither the author's nor the committer's"


def unsigned_commits(repository: Path, base: str, head: str) -> list[str]:
    """Describe every non-merge commit in base..head that lacks a valid sign-off."""
    for revision in (base, head):
        if _SHA.fullmatch(revision) is None:
            raise DcoError("base and head must be full lowercase commit hashes.")
    commits = _git(repository, "rev-list", "--no-merges", "--reverse", f"{base}..{head}").split()
    findings = []
    for commit in commits:
        problem = commit_problem(repository, commit)
        if problem is not None:
            findings.append(f"{commit[:12]} {problem}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="full hash of the merge base or base branch")
    parser.add_argument("--head", required=True, help="full hash of the pull request head")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        findings = unsigned_commits(args.repository, args.base, args.head)
    except (DcoError, OSError, subprocess.SubprocessError) as error:
        print(f"DCO check could not run: {error}", file=sys.stderr)
        return 2
    if findings:
        print("These commits are not signed off under the Developer Certificate of Origin:")
        for finding in findings:
            print(f"  {finding}")
        print("Add the trailer with `git commit -s --amend` or `git rebase --signoff`, then push.")
        return 1
    print("All commits carry a DCO sign-off.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
