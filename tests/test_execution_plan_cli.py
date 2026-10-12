"""Public-boundary coverage for the generic `plan execution` family.

No test opens a credential store for a provider, sends a provider request, or
installs an approval host.  The one success path uses an in-memory fake
planner that calls the real session with an injected fixture read, which is the
only way a plan can be retained until a provider ticket registers a real one.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from test_execution_plan_records import _EXPIRES, _T0, candidate, read
from typer.testing import CliRunner

import adhelm.cli as root_cli
from adhelm import execution_planning_session as session
from adhelm import plan_cli
from adhelm.config import save_configuration
from adhelm.errors import ExitCode
from adhelm.execution_planning_session import RetainedExecutionPlan, plan_execution
from adhelm.ledger import Ledger
from adhelm.models import ArtifactRule, Configuration, OutputPreferences


def _invoke(directory: Path, *arguments: str) -> Any:
    return CliRunner().invoke(
        root_cli.app, ["--data-dir", str(directory), "plan", "execution", *arguments]
    )


def _payload(result: Any) -> dict[str, Any]:
    value = json.loads(result.output)
    assert type(value) is dict
    return cast(dict[str, Any], value)


def _code(result: Any) -> str:
    diagnostics = _payload(result)["diagnostics"]
    assert type(diagnostics) is list and len(diagnostics) == 1
    return cast(str, diagnostics[0]["code"])


def _request(directory: Path, **overrides: object) -> Path:
    body: dict[str, object] = {
        "schema_version": 1,
        "profile_id": "google-edit",
        "provider": "google_ads",
        "operation": "PAUSE",
        "account_ref": "google_ads.alias",
    }
    body.update(overrides)
    path = directory / "request.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


class _FakePlanner:
    """An in-memory planner that injects a fixture read, and never egresses."""

    def __init__(self) -> None:
        self.calls = 0

    def plan_configured_execution(
        self, directory: Path, request: object, *, expected_configuration_digest: str
    ) -> RetainedExecutionPlan:
        self.calls += 1
        return plan_execution(
            Ledger(directory),
            candidate(),
            read=read(),
            plan_id="plan-1",
            profile_ref="default",
            expires_at=_EXPIRES,
            now=_T0 + timedelta(seconds=3),
        )


class _ProhibitedPlanner:
    def plan_configured_execution(
        self, directory: Path, request: object, *, expected_configuration_digest: str
    ) -> RetainedExecutionPlan:
        raise AssertionError("no planner may run after a failed preflight")


def _register(monkeypatch: pytest.MonkeyPatch, planner: object) -> None:
    from adhelm.safety import OperationKind

    monkeypatch.setattr(
        session,
        "CONFIGURED_EXECUTION_PLANNERS",
        {("google_ads", OperationKind.PAUSE): planner},
    )


def test_no_installed_planner_fails_closed_and_names_the_pair(tmp_path: Path) -> None:
    """``tiktok_ads CREATE`` has no planner.

    P03/P04 registered Google Ads, P04-OA OpenAI Ads and P04-MA Meta Ads; every
    other pair still refuses here before any local or provider work.
    """

    result = _invoke(
        tmp_path,
        "create",
        "--provider",
        "tiktok_ads",
        "--operation",
        "CREATE",
        "--input",
        str(_request(tmp_path, provider="tiktok_ads", operation="CREATE")),
    )

    assert result.exit_code == ExitCode.PRECONDITION
    diagnostic = _payload(result)["diagnostics"][0]
    assert diagnostic["code"] == "EXECUTION_PLAN_NOT_COMPILABLE"
    assert "NOT_COMPILABLE" in diagnostic["message"]
    assert "tiktok_ads CREATE" in diagnostic["message"]
    assert _payload(result)["outcome"] == "FAILED_CLOSED"
    assert _payload(result)["data"] == {}
    # Nothing was retained by a refused create.
    assert Ledger(tmp_path).verify()["status"] == "VERIFIED"


@pytest.mark.parametrize(
    ("provider", "operation"),
    [("not_a_provider", "PAUSE"), ("google_ads", "RECOVER"), ("google_ads", "pause")],
)
def test_an_unsupported_provider_or_operation_is_refused(
    tmp_path: Path, provider: str, operation: str
) -> None:
    result = _invoke(
        tmp_path,
        "create",
        "--provider",
        provider,
        "--operation",
        operation,
        "--input",
        str(_request(tmp_path)),
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert _code(result) == "INVALID_EXECUTION_PLAN_SELECTION"


def test_the_request_file_must_name_the_same_provider_and_operation(tmp_path: Path) -> None:
    result = _invoke(
        tmp_path,
        "create",
        "--provider",
        "meta_ads",
        "--operation",
        "PAUSE",
        "--input",
        str(_request(tmp_path)),
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert _code(result) == "INVALID_EXECUTION_PLAN_SELECTION"


def test_a_recover_request_file_is_refused_by_the_schema(tmp_path: Path) -> None:
    result = _invoke(
        tmp_path,
        "create",
        "--provider",
        "google_ads",
        "--operation",
        "PAUSE",
        "--input",
        str(_request(tmp_path, operation="RECOVER")),
    )

    assert result.exit_code != 0
    assert _code(result) in {"SCHEMA_VALIDATION_FAILED", "INVALID_EXECUTION_PLAN_SELECTION"}


def test_the_output_preflight_precedes_any_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1_024)))
    _register(monkeypatch, _ProhibitedPlanner())

    result = _invoke(
        tmp_path,
        "create",
        "--provider",
        "google_ads",
        "--operation",
        "PAUSE",
        "--input",
        str(_request(tmp_path)),
    )

    assert result.exit_code == ExitCode.PRECONDITION
    assert _code(result) == "OUTPUT_LIMIT_EXCEEDED"


def test_an_incomplete_saved_artifact_rule_names_the_retained_plan_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(
        tmp_path,
        Configuration(
            output=OutputPreferences(
                artifact_mode="RULES",
                purposes=["plan_review"],
                workflow_rules=[
                    ArtifactRule(
                        workflow="plan",
                        stage="before_approval",
                        providers=["google_ads"],
                        profiles=["google-edit"],
                        formats=["json"],
                    )
                ],
            )
        ),
    )
    _register(monkeypatch, _ProhibitedPlanner())

    result = _invoke(
        tmp_path,
        "create",
        "--provider",
        "google_ads",
        "--operation",
        "PAUSE",
        "--input",
        str(_request(tmp_path)),
    )

    assert result.exit_code == ExitCode.PRECONDITION
    diagnostic = _payload(result)["diagnostics"][0]
    assert diagnostic["code"] == "ARTIFACT_CONFIGURATION_REQUIRED"
    assert "artifacts plan" in diagnostic["next_action"]


def test_a_registered_planner_produces_a_bounded_durable_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner = _FakePlanner()
    _register(monkeypatch, planner)
    monkeypatch.setattr(plan_cli, "export_key", lambda: b"\x22" * 32)

    result = _invoke(
        tmp_path,
        "create",
        "--provider",
        "google_ads",
        "--operation",
        "PAUSE",
        "--input",
        str(_request(tmp_path)),
    )

    assert result.exit_code == 0, result.output
    envelope = _payload(result)
    assert envelope["outcome"] == "EXECUTION_PLAN_RETAINED_NO_EXECUTION_AUTHORITY"
    data = envelope["data"]
    assert planner.calls == 1
    assert data["provider"] == "google_ads" and data["operation"] == "PAUSE"
    assert data["execution_authority"] is False
    assert data["remote_mutations"] is False
    assert data["review_authorizes_execution"] is False
    assert data["requires_current_kernel_checks"] is True
    assert data["spend_bounds_by_currency"] == []
    assert data["review_command"] == "plan execution show " + data["record_digest"]
    assert data["artifact_policy"] == {
        "outcome": "SUPPRESSED",
        "reason": "ON_REQUEST_REQUIRES_EXPLICIT_USER_REQUEST",
        "generation_supported": False,
        "artifact_command": "artifacts plan",
    }
    assert "google_ads.principal" not in result.output

    catalog = _invoke(tmp_path, "catalog")
    assert catalog.exit_code == 0, catalog.output
    rows = _payload(catalog)["data"]["plans"]
    assert [row["record_digest"] for row in rows] == [data["record_digest"]]
    assert _payload(catalog)["outcome"] == "EXECUTION_PLAN_HISTORY_NO_EXECUTION_AUTHORITY"

    filtered = _invoke(tmp_path, "catalog", "--provider", "meta_ads")
    assert _payload(filtered)["data"]["total_count"] == 0

    shown = _invoke(tmp_path, "show", str(data["record_digest"]))
    assert shown.exit_code == 0, shown.output
    review = _payload(shown)["data"]
    assert review["review"]["provider"] == "google_ads"
    assert review["review"]["review_lines"][0] == ("Maximum incremental spend NOT_SPEND_CAPABLE")
    assert review["provenance"]["execution_authority"] is False
    assert review["review_authorizes_execution"] is False
    assert review["review"]["account_ref"].startswith("opaque:")
    assert "google_ads.principal" not in shown.output


def test_show_bounds_the_complete_review_before_the_sharing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner = _FakePlanner()
    _register(monkeypatch, planner)
    created = _invoke(
        tmp_path,
        "create",
        "--provider",
        "google_ads",
        "--operation",
        "PAUSE",
        "--input",
        str(_request(tmp_path)),
    )
    selected = str(_payload(created)["data"]["record_digest"])
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1_024)))
    monkeypatch.setattr(
        plan_cli,
        "export_key",
        lambda: pytest.fail("complete bound must precede the sharing key"),
    )

    result = _invoke(tmp_path, "show", selected)

    assert result.exit_code == ExitCode.PRECONDITION
    assert _code(result) == "OUTPUT_LIMIT_EXCEEDED"


def test_catalog_and_show_refuse_invalid_selections(tmp_path: Path) -> None:
    assert _code(_invoke(tmp_path, "show", "not-a-digest")) == "INVALID_EXECUTION_PLAN_SELECTION"
    assert _code(_invoke(tmp_path, "catalog", "--limit", "0")) == (
        "INVALID_EXECUTION_PLAN_SELECTION"
    )
    assert _code(_invoke(tmp_path, "catalog", "--provider", "nope")) == (
        "INVALID_EXECUTION_PLAN_SELECTION"
    )
    assert _code(_invoke(tmp_path, "show", "a" * 64)) == "EXECUTION_PLAN_NOT_RETAINED"


def test_the_existing_pause_plan_commands_are_unchanged() -> None:
    runner = CliRunner()
    for arguments in (
        ["plan", "openai-pause", "--help"],
        ["plan", "catalog", "--help"],
        ["plan", "show", "--help"],
        ["plan", "ga4-key-event", "--help"],
        ["plan", "meta-campaign-pause", "--help"],
        ["plan", "google-ads-campaign-pause", "--help"],
    ):
        assert runner.invoke(root_cli.app, arguments).exit_code == 0


def test_the_generic_family_is_mounted_once_under_plan() -> None:
    result = CliRunner().invoke(root_cli.app, ["plan", "--help"])

    assert result.exit_code == 0
    assert result.output.count("execution") == 1


def test_discover_advertises_the_generic_plan_family_truthfully() -> None:
    """AGENTS.md rule 27: the agent finds the typed CLI through discover."""
    discovery = _payload(CliRunner().invoke(root_cli.app, ["discover"]))["data"]

    assert {
        "plan execution create",
        "plan execution catalog",
        "plan execution show",
    } <= set(discovery["commands"])
    assert "execution-plan-request" in discovery["schemas"]
    # S4 registered `execute plan`, so the block also states which pairs an
    # installed adapter can execute, and how it refuses.  P03, P04, P05, P04-OA
    # and P04-MA registered six pairs between them -- Google PAUSE, CREATE,
    # ACTIVATE, and the maintenance kind UPDATE, plus Meta CREATE and OpenAI
    # CREATE -- so both lists name all six, in the registries' own sort by
    # (provider, operation).  Google ARCHIVE is delete-class and deliberately
    # absent: no planner compiles it, so discover must not list it.
    assert discovery["execution_plan"] == {
        "planning": "GENERIC_RECORD_INSTALLED_NO_PROVIDER_COMPILER",
        "compilable_operations": [
            {"provider": "google_ads", "operation": "ACTIVATE"},
            {"provider": "google_ads", "operation": "CREATE"},
            {"provider": "google_ads", "operation": "PAUSE"},
            {"provider": "google_ads", "operation": "UPDATE"},
            {"provider": "meta_ads", "operation": "CREATE"},
            {"provider": "openai_ads", "operation": "CREATE"},
        ],
        "refusal_code": "EXECUTION_PLAN_NOT_COMPILABLE",
        "historical_review": "REDACTED_LOCAL_HISTORY_ONLY",
        "artifact_generation": "NOT_IMPLEMENTED_USE_PER_PROVIDER_ARTIFACT_COMMANDS",
        "execution": "UNRELEASED",
        "execution_command": "execute plan",
        "execution_selection": "POSITIONAL_RECORD_DIGEST_ONLY_NO_INPUT_FILE",
        "executable_operations": [
            {"provider": "google_ads", "operation": "ACTIVATE"},
            {"provider": "google_ads", "operation": "CREATE"},
            {"provider": "google_ads", "operation": "PAUSE"},
            {"provider": "google_ads", "operation": "UPDATE"},
            {"provider": "meta_ads", "operation": "CREATE"},
            {"provider": "openai_ads", "operation": "CREATE"},
        ],
        # Derived from the gate order, so the named code is the one an
        # explicit request actually produces in this build.
        "execution_refusal_gates": [
            {
                "gate": "RELEASE_POLICY_WRITES_RELEASED",
                "satisfied": False,
                "refusal_code": "WRITES_NOT_RELEASED",
            },
            {
                "gate": "COMPILED_APPROVAL_BRIDGE_INSTALLED",
                "satisfied": None,
                "refusal_code": "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED",
            },
            {
                "gate": "APPROVAL_KIND_RENDERED_BY_INSTALLED_HOST",
                "satisfied": False,
                "refusal_code": "NATIVE_APPROVAL_BRIDGE_KIND_NOT_INSTALLED",
            },
            {
                "gate": "CONFIGURED_EXECUTION_ADAPTER",
                "satisfied": True,
                "refusal_code": "EXECUTION_ADAPTER_NOT_CONFIGURED",
            },
        ],
        "execution_first_refusal_code": "WRITES_NOT_RELEASED",
        "native_setup_prerequisite": "REQUIRED",
        "automatic_execution_from_plan_or_artifact": False,
        "default_runtime_provider_mutations": False,
        "live_provider_execution_verified": False,
    }


def test_discover_derives_compilable_operations_from_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalogue is rendered at call time, so it can never go stale."""
    from types import MappingProxyType

    from adhelm.safety import OperationKind

    private = {("google_ads", OperationKind.PAUSE): _FakePlanner()}
    monkeypatch.setattr(session, "CONFIGURED_EXECUTION_PLANNERS", MappingProxyType(private))

    discovery = _payload(CliRunner().invoke(root_cli.app, ["discover"]))["data"]

    assert discovery["execution_plan"]["compilable_operations"] == [
        {"provider": "google_ads", "operation": "PAUSE"}
    ]
