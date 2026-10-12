"""Independent F02 review findings for Google Ads PAUSE retained-plan artifacts.

These regressions were written by the independent reviewer of
``claude/w1-f02-google-artifacts`` at ``03dc994``. They fail against that commit
and describe the behavior the repair must produce. They contact no provider,
credential store, native host, or execution path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from test_google_ads_pause_plan_artifact_cli import _data, _invoke, _retain
from typer.testing import CliRunner

import adhelm.cli as root_cli
from adhelm import credentials, google_ads_pause_cli
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import ArtifactRule, OutputPreferences

_PLANNING_PROFILE = "google-edit"
_ARTIFACT_COMMAND = "artifacts google-ads-campaign-pause"


def _matching_before_approval_rule(output: Path | None = None) -> OutputPreferences:
    """Return the saved policy a user configures to get plan-review artifacts."""
    return OutputPreferences(
        artifact_mode="RULES",
        purposes=["plan_review"],
        output_directory=str(output) if output is not None else None,
        workflow_rules=[
            ArtifactRule(
                workflow="plan",
                stage="before_approval",
                providers=["google_ads"],
                profiles=[_PLANNING_PROFILE],
                formats=["json"],
            )
        ],
    )


def test_create_artifact_refusal_no_longer_claims_google_artifacts_are_unavailable(
    tmp_path: Path,
) -> None:
    """Finding 1: the planner still tells the agent the capability does not exist.

    ``src/adhelm/cli.py:499`` now advertises
    ``artifact_generation: STATIC_REDACTED_LOCAL_HISTORY_ONLY`` and
    ``docs/implementation/google-ads-pause-cli.md:36`` now says requested output
    is produced by ``artifacts google-ads-campaign-pause``. The refusal raised by
    ``src/adhelm/google_ads_pause_cli.py:226`` still says the artifacts "are not
    available yet" and its ``next_action`` only offers ``--no-artifacts`` or
    deleting the rule. That diagnostic is untrue and does not route the agent to
    the installed command (AGENTS.md rules 20 and 27).

    Repair note (F02 implementer): as committed, this test passed the same
    complete rule as
    ``test_saved_matching_before_approval_rule_does_not_break_google_planning``
    and required it to raise, while that test required the identical call not to
    raise. Only the fixture argument is changed here, to the incomplete rule
    that is still legitimately refused; every assertion is the reviewer's.
    """
    output = tmp_path / "views"
    output.mkdir(mode=0o700)

    with pytest.raises(AdhelmError) as raised:
        google_ads_pause_cli._artifact_notice(
            _matching_before_approval_rule(), _PLANNING_PROFILE, None
        )

    error = raised.value
    assert "not available yet" not in error.message, error.message
    assert _ARTIFACT_COMMAND in error.next_action, error.next_action


def test_saved_matching_before_approval_rule_does_not_break_google_planning(
    tmp_path: Path,
) -> None:
    """Finding 2: the saved-rule leg of F02 has no installed route.

    ``preflight_google_ads_campaign_pause_plan_artifacts`` resolves a matching
    ``RULES`` policy to ``GENERATE``
    (``tests/test_google_ads_pause_plan_artifact_policy.py:98``), but no shipped
    command consumes that outcome: ``artifacts google-ads-campaign-pause``
    always builds an explicit ``ArtifactTaskOverride`` from a request whose
    ``formats`` field is required, and ``plan google-ads-campaign-pause create``
    refuses outright. A user who saved the rule therefore cannot plan at all and
    never receives the files the rule configured (AGENTS.md rule 28).
    """
    output = tmp_path / "views"
    output.mkdir(mode=0o700)

    try:
        notice = google_ads_pause_cli._artifact_notice(
            _matching_before_approval_rule(output), _PLANNING_PROFILE, None
        )
    except AdhelmError as exc:  # pragma: no cover - the finding under review
        pytest.fail(
            "a saved before-approval Google Ads plan-review rule still fails planning "
            f"with {exc.code}; the retained-plan artifact route must satisfy it instead"
        )
    assert notice.reason is not None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "shared artifact-policy boundary: disabled purpose not enforced on explicit "
        "requests; owned by U01 artifact-policy ticket"
    ),
)
def test_explicit_artifact_request_respects_a_disabled_plan_review_purpose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3: a disabled artifact purpose does not survive an explicit request.

    ``docs/implementation/google-ads-pause-plan-artifacts.md:64`` states that
    ``OFF``, ``ON_REQUEST`` and "a disabled purpose all remain file-free".
    ``resolve_artifact_policy`` (``src/adhelm/artifacts.py:275``) never compares
    ``context.purpose`` against ``saved.purposes`` on the task-override branch,
    so ``artifacts google-ads-campaign-pause`` publishes a plan-review file even
    when the user removed ``plan_review`` from the enabled purposes. AGENTS.md
    rule 28 makes the purpose list a user configuration, not a default.

    This is a shared artifact-policy boundary; Meta, GA4 and OpenAI inherit it.
    """
    record_digest = _retain(tmp_path)
    destination = tmp_path / "views"
    destination.mkdir(mode=0o700)
    configuration = load_configuration(tmp_path)
    save_configuration(
        tmp_path,
        configuration.model_copy(
            update={
                "output": OutputPreferences(
                    artifact_mode="OFF", purposes=[], max_output_bytes=131_072
                )
            }
        ),
        replace=True,
    )
    request = tmp_path / "disabled-purpose-request.json"
    request.write_text(
        json.dumps(
            {"schema_version": 1, "formats": ["json"], "output_directory": str(destination)}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(credentials, "export_key", lambda: b"r" * 32)

    result = _invoke(tmp_path, record_digest, "--input", str(request))

    assert list(destination.iterdir()) == [], sorted(p.name for p in destination.iterdir())
    assert result.exit_code != 0, result.output


def test_artifact_execution_command_stays_bound_to_the_installed_discovery_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 4 guard: two independent copies of the Google execution status.

    Both the Google renderer
    (``src/adhelm/google_ads_pause_plan_artifacts.py``) and ``discover``
    (``src/adhelm/cli.py``) now read
    ``adhelm._installed_approval_execution_status.GOOGLE_ADS_PAUSE_EXECUTION_COMMAND_STATUS``,
    so P03's change of that one constant moved both together. This guard keeps
    them in step; it must not be relaxed to a literal in either place.
    """
    record_digest = _retain(tmp_path)
    destination = tmp_path / "views"
    destination.mkdir(mode=0o700)
    configuration = load_configuration(tmp_path)
    save_configuration(
        tmp_path,
        configuration.model_copy(update={"output": OutputPreferences(max_output_bytes=131_072)}),
        replace=True,
    )
    request = tmp_path / "execution-status-request.json"
    request.write_text(
        json.dumps(
            {"schema_version": 1, "formats": ["json"], "output_directory": str(destination)}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(credentials, "export_key", lambda: b"r" * 32)

    result = _invoke(tmp_path, record_digest, "--input", str(request))
    assert result.exit_code == 0, result.output
    path = Path(_data(result)["data"]["artifact_result"]["artifacts"][0]["path"])
    document = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))

    discovery = cast(
        dict[str, Any], json.loads(CliRunner().invoke(root_cli.app, ["discover"]).output)
    )["data"]["google_ads_campaign_pause"]
    assert document["content"]["authority"]["execution_command"] == discovery["execution"], (
        discovery
    )
