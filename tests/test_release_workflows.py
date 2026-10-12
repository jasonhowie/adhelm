"""The release workflow publishes only for a release tag and passes event data via env only.

The workflows are checked as text because the project has no YAML parser dependency. `actionlint`
validates their syntax and expressions separately; these tests pin the release-safety structure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
_TAG_ONLY = (
    "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v') "
    "&& github.repository == 'jasonhowie/adhelm'"
)
_PRIVILEGED = (
    "id-token: write",
    "attestations: write",
    "contents: write",
    "gh-action-pypi-publish",
    "gh release create",
    "environment:",
)
_TAG_JOBS = {"attest", "publish", "github-release"}


def _text(name: str) -> str:
    return (_WORKFLOWS / name).read_text(encoding="utf-8")


def _jobs(text: str) -> dict[str, str]:
    """Split a workflow into job bodies keyed by the two-space-indented job name."""
    _, _, body = text.partition("\njobs:\n")
    names = list(re.finditer(r"^  ([a-z][a-z0-9-]*):\s*$", body, re.MULTILINE))
    return {
        match.group(1): body[
            match.end() : names[index + 1].start() if index + 1 < len(names) else None
        ]
        for index, match in enumerate(names)
    }


def test_every_action_is_pinned_to_a_full_commit_hash() -> None:
    for name in ("release.yml", "dco.yml"):
        references = re.findall(r"^\s*-?\s*uses:\s*(\S+)", _text(name), re.MULTILINE)
        assert references, name
        for reference in references:
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", reference), (name, reference)


def test_the_release_workflow_builds_on_every_push_and_pull_request_but_tags_only_v_versions() -> (
    None
):
    text = _text("release.yml")
    triggers = text.split("\npermissions:", 1)[0]

    assert 'branches: ["**"]' in triggers
    assert 'tags: ["v[0-9]*"]' in triggers
    assert re.search(r"^  pull_request:\s*$", triggers, re.MULTILINE)
    assert "workflow_dispatch" not in text and "schedule" not in text


def test_nothing_is_granted_by_default_and_the_build_and_smoke_jobs_only_read() -> None:
    text = _text("release.yml")
    jobs = _jobs(text)

    assert re.search(r"^permissions: \{\}$", text, re.MULTILINE)
    assert set(jobs) == {"build", "smoke", *_TAG_JOBS}
    for name in ("build", "smoke"):
        assert not re.search(r":\s*write\b", jobs[name]), name
        assert "contents: read" in jobs[name]


@pytest.mark.parametrize("job", sorted(_TAG_JOBS))
def test_attestation_publishing_and_the_github_release_run_only_for_a_tag_in_the_public_repository(
    job: str,
) -> None:
    assert _TAG_ONLY in _jobs(_text("release.yml"))[job]


def test_no_credentialed_step_exists_outside_the_tag_only_jobs() -> None:
    jobs = _jobs(_text("release.yml"))

    for name, body in jobs.items():
        for marker in _PRIVILEGED:
            if name in _TAG_JOBS:
                continue
            assert marker not in body, (name, marker)
    # The markers are real: each is used by the job that needs it, so this test is not vacuous.
    assert "id-token: write" in jobs["publish"] and "environment:" in jobs["publish"]
    assert "attestations: write" in jobs["attest"]
    assert "contents: write" in jobs["github-release"]
    assert "gh release create" in jobs["github-release"]
    assert "gh-action-pypi-publish" in jobs["publish"]


def test_publishing_uses_the_protected_pypi_environment_and_no_stored_token() -> None:
    publish = _jobs(_text("release.yml"))["publish"]

    assert re.search(r"environment:\n\s+name: pypi\n", publish)
    assert "password" not in publish.lower() and "PYPI_TOKEN" not in publish
    assert "secrets." not in _text("release.yml")


def test_publication_waits_for_the_smoke_on_every_platform_and_for_the_attestation() -> None:
    text = _text("release.yml")
    jobs = _jobs(text)

    assert "needs: [build, smoke]" in jobs["attest"]
    assert "needs: [build, attest]" in jobs["publish"]
    assert "needs: [build, publish]" in jobs["github-release"]
    assert "fail-fast: false" in jobs["smoke"]
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in jobs["smoke"]


def test_the_artifacts_are_built_once_and_every_later_job_checks_the_same_files() -> None:
    jobs = _jobs(_text("release.yml"))

    assert len(re.findall(r"^\s+uv build ", _text("release.yml"), re.MULTILINE)) == 2
    assert "uv build" in jobs["build"] and "$RUNNER_TEMP/rebuild" in jobs["build"]
    assert "cmp " in jobs["build"]
    for name in ("smoke", "attest", "publish", "github-release"):
        assert "uv build" not in jobs[name], name
        assert "download-artifact" in jobs[name], name
    assert "check-checksums" in jobs["smoke"]
    for name in ("attest", "publish", "github-release"):
        assert "sha256sum --check --strict SHA256SUMS" in jobs[name], name


def test_the_tag_is_checked_against_the_package_version_before_anything_is_uploaded() -> None:
    build = _jobs(_text("release.yml"))["build"]

    assert 'verify --dist dist --tag "$TAG"' in build
    assert build.index("--tag") < build.index("upload-artifact")
    assert "TAG: ${{ github.ref_name }}" in build


@pytest.mark.parametrize("name", ["release.yml", "dco.yml"])
def test_no_expression_is_expanded_inside_a_shell_script(name: str) -> None:
    """Event data such as a tag or branch name reaches shell only through env variables."""
    in_script = False
    script_indent = 0
    for line in _text(name).splitlines():
        indent = len(line) - len(line.lstrip())
        run = re.match(r"^(\s*)(?:-\s+)?run:\s*(.*)$", line)
        if run:
            in_script, script_indent = True, len(run.group(1))
            assert "${{" not in run.group(2), line
        elif in_script and line.strip() and indent <= script_indent:
            in_script = False
        elif in_script:
            assert "${{" not in line, line


def test_the_dco_workflow_runs_on_pull_requests_with_full_history_and_read_only_access() -> None:
    text = _text("dco.yml")

    assert re.search(r"^on: pull_request$", text, re.MULTILINE)
    assert "contents: read" in text and not re.search(r":\s*write\b", text)
    assert "fetch-depth: 0" in text
    assert "BASE_SHA: ${{ github.event.pull_request.base.sha }}" in text
    assert "HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in text
    assert 'scripts/check_dco.py --base "$BASE_SHA" --head "$HEAD_SHA"' in text


def test_the_locked_runtime_dependencies_are_audited_before_anything_is_uploaded() -> None:
    build = _jobs(_text("release.yml"))["build"]
    export = (
        "uv export --locked --no-dev --no-emit-project --format requirements-txt --quiet "
        '--output-file "$RUNNER_TEMP/runtime-requirements.txt"'
    )
    audit = (
        "uv run pip-audit --strict --require-hashes --disable-pip "
        '--requirement "$RUNNER_TEMP/runtime-requirements.txt"'
    )

    assert export in build and audit in build
    assert build.index(export) < build.index(audit) < build.index("upload-artifact")
    # The scan reads the lock's runtime graph and must precede the build it gates.
    assert build.index(audit) < build.index("uv build")
    # It is part of the job every later job depends on, so a finding stops attestation and publish.
    assert "|| true" not in build and "continue-on-error" not in build
    for name in _TAG_JOBS:
        assert "needs: [build" in _jobs(_text("release.yml"))[name]


def test_pip_audit_is_a_locked_development_dependency_the_job_installs_before_using() -> None:
    build = _jobs(_text("release.yml"))["build"]
    pyproject = (_WORKFLOWS.parents[1] / "pyproject.toml").read_text(encoding="utf-8")

    assert '"pip-audit>=' in pyproject
    assert build.index("uv sync --frozen") < build.index("uv run pip-audit")


def test_the_sbom_is_bound_to_the_source_and_exported_twice_with_identical_results() -> None:
    build = _jobs(_text("release.yml"))["build"]
    step = build[build.index("Write the SBOM and checksums") : build.index("upload-artifact")]

    assert 'SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"' in step
    assert step.count("release_artifacts.py normalise-sbom") == 2
    assert step.count("uv export --locked --no-dev --format cyclonedx1.5") == 2
    assert step.index("normalise-sbom") < step.index("verify-sbom") < step.index("cmp ")
    assert 'cmp "$sbom" "$RUNNER_TEMP/second.cdx.json"' in step
    assert step.index("verify-sbom") < step.index("write-checksums")


def test_the_release_build_cannot_override_the_build_constraints() -> None:
    build = _jobs(_text("release.yml"))["build"]

    # No flag or variable may swap the build constraints out for the release builds.
    assert "--build-constraint" not in build and "--no-build-isolation" not in build
    assert "UV_BUILD_CONSTRAINT" not in _text("release.yml")
