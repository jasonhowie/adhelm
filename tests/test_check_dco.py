"""The DCO check requires a well-formed sign-off by the author or committer on each commit."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_AUTHOR = ("Ada Author", "ada@example.test")
_COMMITTER = ("Cy Committer", "cy@example.test")


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_dco_under_test", _ROOT / "scripts/check_dco.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dco = _script()


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=True,
        env={
            **{name: value for name, value in os.environ.items() if not name.startswith("GIT_")},
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": _AUTHOR[0],
            "GIT_AUTHOR_EMAIL": _AUTHOR[1],
            "GIT_COMMITTER_NAME": _COMMITTER[0],
            "GIT_COMMITTER_EMAIL": _COMMITTER[1],
        },
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "base.txt")
    _git(root, "commit", "--quiet", "--no-gpg-sign", "-m", "base commit")
    return root


def _commit(repository: Path, message: str, name: str = "file") -> str:
    path = repository / f"{name}.txt"
    path.write_text(message, encoding="utf-8")
    _git(repository, "add", path.name)
    _git(repository, "commit", "--quiet", "--no-gpg-sign", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _findings(repository: Path, base: str) -> list[str]:
    result: list[str] = dco.unsigned_commits(
        repository, base, _git(repository, "rev-parse", "HEAD")
    )
    return result


def test_a_trailer_key_in_another_case_is_accepted_like_the_dco_app_accepts_it(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _commit(repository, "feat: lower\n\nsigned-off-by: Ada Author <ada@example.test>", "lower")
    _commit(repository, "feat: upper\n\nSIGNED-OFF-BY: Ada Author <ada@example.test>", "upper")

    assert _findings(repository, base) == []


def test_a_lower_case_key_still_needs_the_authors_or_committers_email(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _commit(repository, "feat: other\n\nsigned-off-by: Eve Other <eve@example.test>")

    assert _findings(repository, base) == [
        f"{_git(repository, 'rev-parse', 'HEAD')[:12]} has a Signed-off-by email that is "
        "neither the author's nor the committer's"
    ]


def test_a_commit_signed_off_by_its_author_passes(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _commit(repository, "feat: add thing\n\nSigned-off-by: Ada Author <ada@example.test>")

    assert _findings(repository, base) == []


def test_git_commit_dash_s_produces_an_accepted_sign_off(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "signed.txt").write_text("x", encoding="utf-8")
    _git(repository, "add", "signed.txt")
    _git(repository, "commit", "--quiet", "--no-gpg-sign", "-s", "-m", "feat: signed with -s")

    assert _findings(repository, base) == []


def test_a_sign_off_by_the_committer_alone_passes_and_case_in_the_email_is_ignored(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _commit(repository, "fix: x\n\nSigned-off-by: Cy Committer <CY@Example.Test>")

    assert _findings(repository, base) == []


def test_a_commit_without_a_sign_off_is_reported_with_a_short_hash(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    unsigned = _commit(repository, "feat: no sign-off")

    assert _findings(repository, base) == [f"{unsigned[:12]} has no Signed-off-by trailer"]


def test_only_the_unsigned_commits_in_the_range_are_reported_oldest_first(
    repository: Path,
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    first = _commit(repository, "one", "one")
    _commit(repository, "two\n\nSigned-off-by: Ada Author <ada@example.test>", "two")
    third = _commit(repository, "three", "three")

    assert _findings(repository, base) == [
        f"{first[:12]} has no Signed-off-by trailer",
        f"{third[:12]} has no Signed-off-by trailer",
    ]


def test_a_commit_before_the_base_is_not_checked(repository: Path) -> None:
    unsigned_history = _commit(repository, "old unsigned work")

    assert _findings(repository, unsigned_history) == []


def test_a_sign_off_for_someone_else_is_refused(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    commit = _commit(repository, "feat: x\n\nSigned-off-by: Eve Other <eve@example.test>")

    assert _findings(repository, base) == [
        f"{commit[:12]} has a Signed-off-by email that is neither the author's nor the committer's"
    ]


@pytest.mark.parametrize(
    "trailer",
    [
        "Signed-off-by: Ada Author",
        "Signed-off-by: <ada@example.test>",
        "Signed-off-by: Ada Author <not-an-email>",
        "Signed-off-by: Ada Author ada@example.test",
        "Signed-off-by:",
    ],
)
def test_a_sign_off_that_is_not_name_and_email_is_refused(repository: Path, trailer: str) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _commit(repository, f"feat: x\n\n{trailer}")

    (finding,) = _findings(repository, base)

    assert finding.endswith("has a Signed-off-by trailer that is not 'Name <email>'")


def test_a_sign_off_line_in_the_message_body_is_not_a_trailer(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    commit = _commit(
        repository,
        "feat: x\n\nSigned-off-by: Ada Author <ada@example.test>\n\nThe change follows.",
    )

    assert _findings(repository, base) == [f"{commit[:12]} has no Signed-off-by trailer"]


def test_a_co_authored_by_trailer_is_not_a_sign_off(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    commit = _commit(repository, "feat: x\n\nCo-Authored-By: Ada Author <ada@example.test>")

    assert _findings(repository, base) == [f"{commit[:12]} has no Signed-off-by trailer"]


def test_one_valid_sign_off_among_several_trailers_is_enough(repository: Path) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _commit(
        repository,
        "feat: x\n\nSigned-off-by: Eve Other <eve@example.test>\n"
        "Signed-off-by: Ada Author <ada@example.test>",
    )

    assert _findings(repository, base) == []


def test_merge_commits_are_not_required_to_carry_a_sign_off(repository: Path) -> None:
    _git(repository, "checkout", "--quiet", "-b", "topic")
    signed = "feat: topic\n\nSigned-off-by: Ada Author <ada@example.test>"
    _commit(repository, signed, "topic")
    _git(repository, "checkout", "--quiet", "main")
    _commit(repository, signed, "main-side")
    base = _git(repository, "rev-parse", "HEAD~1")
    _git(repository, "merge", "--quiet", "--no-gpg-sign", "--no-ff", "-m", "Merge topic", "topic")

    assert _findings(repository, base) == []


@pytest.mark.parametrize(
    "revision", ["HEAD", "main", "--all", "origin/main..HEAD", "a" * 39, "A" * 40, ""]
)
def test_a_base_or_head_that_is_not_a_full_lowercase_hash_is_refused_before_running_git(
    repository: Path, revision: str
) -> None:
    head = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(dco.DcoError, match="full lowercase commit hashes"):
        dco.unsigned_commits(repository, revision, head)
    with pytest.raises(dco.DcoError, match="full lowercase commit hashes"):
        dco.unsigned_commits(repository, head, revision)


def test_an_unknown_commit_is_a_could_not_run_error_not_a_pass(repository: Path) -> None:
    head = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(dco.DcoError, match="git rev-list failed"):
        dco.unsigned_commits(repository, "0" * 40, head)


def test_the_command_exits_one_with_instructions_for_an_unsigned_commit(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    unsigned = _commit(repository, "feat: no sign-off")
    head = _git(repository, "rev-parse", "HEAD")

    status = dco.main(["--base", base, "--head", head, "--repository", str(repository)])

    output = capsys.readouterr().out
    assert status == 1
    assert unsigned[:12] in output
    assert "git commit -s --amend" in output


def test_the_command_exits_zero_when_every_commit_is_signed(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _commit(repository, "feat: x\n\nSigned-off-by: Ada Author <ada@example.test>")
    head = _git(repository, "rev-parse", "HEAD")

    status = dco.main(["--base", base, "--head", head, "--repository", str(repository)])

    assert status == 0
    assert "All commits carry a DCO sign-off." in capsys.readouterr().out


def test_the_command_exits_two_when_it_cannot_read_the_range(
    repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = dco.main(["--base", "HEAD", "--head", "HEAD", "--repository", str(repository)])

    assert status == 2
    assert "DCO check could not run" in capsys.readouterr().err
