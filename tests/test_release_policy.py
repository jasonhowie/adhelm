"""The 0.1.0 release-policy gate refuses every remote mutation before anything else.

These tests never use the ``simulate_released_writes`` fixture: they run the
shipped gate. The fakes below fail the test when the credential store, the
ledger, the safety kernel or an egress path is touched.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import keyring
import pytest
import typer
from test_ga4_create_execution_workflow import _material as _ga4_material
from test_generic_execution_route import (
    _FakeAdapterFactory,
    _FakeExecutionAdapter,
    _retain,
)
from test_meta_pause_execution_workflow import _prepared_record as _meta_prepared_record
from test_meta_pause_planning_integration import runtime as runtime
from test_openai_pause_execution_workflow import _Harness as _OpenAIHarness
from typer.core import TyperGroup, TyperOption
from typer.testing import CliRunner

import adhelm.cli as root_cli
import adhelm.credentials as credentials_module
import adhelm.egress_permit as egress_permit
import adhelm.ga4_create_execution_workflow as ga4_workflow
import adhelm.openai_pause_execution_workflow as openai_workflow
import adhelm.pause_workflow as openai_planning
import adhelm.providers.ga4_admin_key_event_create_adapter as ga4_adapter
import adhelm.release_policy as release_policy
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_create_execution_workflow import (
    execute_retained_ga4_key_event_create,
    execute_retained_ga4_key_event_create_with_installed_approval,
)
from adhelm.generic_execution_workflow import (
    AttemptRecoveryRequest,
    ExecutionPlanExecutionRequest,
    executable_execution_operations,
    execute_retained_execution_plan_with_installed_approval,
    record_retained_attempt_recovery_with_installed_approval,
)
from adhelm.ledger import Ledger
from adhelm.meta_pause_execution_workflow import (
    execute_retained_meta_campaign_pause_with_installed_approval,
)
from adhelm.models import Configuration
from adhelm.openai_pause_execution_workflow import (
    execute_retained_openai_pause_with_installed_approval,
)
from adhelm.safety import CompositePlan, DirectGrant, OperationKind, SafetyKernel, new_direct_grant

_RUNNER = CliRunner()
_DIGEST = "a" * 64
_PRIVATE = "never-echoed-input-marker"


def _tree(command: Any, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, Any]]:
    """Yield every registered leaf command with its full path."""
    if isinstance(command, TyperGroup):
        for name, child in sorted(command.commands.items()):
            yield from _tree(child, (*prefix, name))
    else:
        yield " ".join(prefix), command


def _registered_leaves() -> dict[str, Any]:
    return dict(_tree(typer.main.get_command(root_cli.app)))


def _declared_leaves() -> dict[str, Any]:
    """Leaves whose registered callback carries the release-policy declaration."""
    return {
        path: leaf
        for path, leaf in _registered_leaves().items()
        if release_policy.declared_command(leaf.callback) is not None
    }


def _arguments(leaf: Any, tmp_path: Path) -> list[str]:
    """Build a syntactically valid invocation from the leaf's own parameters."""
    arguments: list[str] = []
    for parameter in leaf.params:
        if not parameter.required:
            continue
        if isinstance(parameter, TyperOption):
            arguments += [parameter.opts[0], str(tmp_path / "request.json")]
        else:
            arguments.append(_DIGEST)
    return arguments


def _invoke(directory: Path, path: str, *arguments: str) -> Any:
    return _RUNNER.invoke(root_cli.app, ["--data-dir", str(directory), *path.split(), *arguments])


def _diagnostic(result: Any) -> dict[str, Any]:
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    return dict(envelope["diagnostics"][0])


@pytest.fixture
def untouchable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if a write path reaches a credential store, ledger, kernel or egress."""

    def touched(surface: str) -> Any:
        def fail(*_args: object, **_kwargs: object) -> Any:
            pytest.fail(f"the release-policy gate let a command reach {surface}")

        return fail

    for name in ("get_keyring", "get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, touched(f"the OS credential store ({name})"))
    for name in ("_backend", "credential", "export_key", "openai_pause_binding_key"):
        monkeypatch.setattr(credentials_module, name, touched(f"adhelm.credentials.{name}"))
    monkeypatch.setattr(Ledger, "__init__", touched("the ledger"))
    monkeypatch.setattr(SafetyKernel, "execute", touched("the safety kernel"))
    monkeypatch.setattr(SafetyKernel, "record_attempt_recovery", touched("recovery"))
    monkeypatch.setattr(egress_permit, "_kernel_permit", touched("an egress permit"))


# --- every registered leaf --------------------------------------------------


def test_the_registry_finds_the_known_remote_mutation_leaves() -> None:
    """Guards the enumeration itself: it must not be an empty parametrization."""
    assert set(_declared_leaves()) >= {
        "execute openai-pause",
        "execute ga4-key-event-create",
        "execute meta-campaign-pause",
        "execute plan",
        "recover attempt",
    }


@pytest.mark.parametrize("path", sorted(_declared_leaves()))
def test_every_declared_leaf_is_refused_first_with_exit_three(
    path: str, tmp_path: Path, untouchable: None
) -> None:
    leaf = _declared_leaves()[path]
    data_directory = tmp_path / "never-created"

    result = _invoke(data_directory, path, *_arguments(leaf, tmp_path))

    assert result.exit_code == ExitCode.PRECONDITION == 3, result.output
    diagnostic = _diagnostic(result)
    assert diagnostic["code"] == "WRITES_NOT_RELEASED"
    assert "release policy" in diagnostic["next_action"]
    assert json.loads(result.output)["data"] == {}
    # Neither the data directory, its configuration nor its ledger was opened.
    assert not data_directory.exists()


@pytest.mark.parametrize("path", sorted(_declared_leaves()))
def test_the_refusal_precedes_reading_the_named_input(
    path: str, tmp_path: Path, untouchable: None
) -> None:
    """A request file is never opened, so nothing in it is parsed or echoed."""
    leaf = _declared_leaves()[path]
    (tmp_path / "request.json").write_text(_PRIVATE, encoding="utf-8")

    result = _invoke(tmp_path / "data", path, *_arguments(leaf, tmp_path))

    assert _diagnostic(result)["code"] == "WRITES_NOT_RELEASED"
    assert _PRIVATE not in result.output


def test_no_undeclared_leaf_reaches_an_execution_workflow() -> None:
    """A command that can run a retained execution must be declared a write leaf.

    The retained execution workflows are named by convention (`execute_retained_*`
    and `record_retained_*`). This follows each registered command's callback
    through the functions of its own module, so a new leaf that forgets the
    declaration, or hides the call behind a helper, fails here.
    """
    workflow = re.compile(r"^(execute_retained_|record_retained_)")

    def reaches_workflow(function: types.FunctionType, seen: set[object]) -> bool:
        pending = [function.__code__]
        while pending:
            code = pending.pop()
            for name in code.co_names:
                if workflow.match(name):
                    return True
                helper = function.__globals__.get(name)
                if isinstance(helper, types.FunctionType) and helper not in seen:
                    seen.add(helper)
                    if helper.__module__ == function.__module__ and reaches_workflow(helper, seen):
                        return True
            pending.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
        return False

    def reaches(callback: Any) -> bool:
        function = inspect.unwrap(callback)
        return reaches_workflow(function, {function})

    registered = _registered_leaves()
    declared = _declared_leaves()
    # Non-vacuous: the scan finds the workflow behind every declared leaf.
    assert all(reaches(declared[path].callback) for path in declared)
    undeclared = sorted(
        path for path in registered.keys() - declared.keys() if reaches(registered[path].callback)
    )
    assert undeclared == []


def test_the_declared_leaves_are_exactly_the_commands_discover_marks_unreleased() -> None:
    declared = set(_declared_leaves())
    discovery = json.loads(_RUNNER.invoke(root_cli.app, ["discover"]).output)["data"]
    policy = discovery["release_policy"]

    assert declared == set(release_policy.unreleased_commands())
    assert declared == {item["command"] for item in policy["unreleased_commands"]}
    assert {item["status"] for item in policy["unreleased_commands"]} == {"UNRELEASED"}
    assert declared <= set(discovery["commands"])
    # The declared path is the path the command is registered under.
    for path, leaf in _declared_leaves().items():
        assert release_policy.declared_command(leaf.callback) == path


# --- the merged execution paths ---------------------------------------------


def test_the_merged_google_and_meta_pairs_are_registered_behind_execute_plan() -> None:
    pairs = {(item["provider"], item["operation"]) for item in executable_execution_operations()}
    assert {
        ("google_ads", "ACTIVATE"),
        ("google_ads", "UPDATE"),
        ("meta_ads", "CREATE"),
    } <= pairs
    assert "execute plan" in _declared_leaves()


@pytest.mark.parametrize(
    ("provider", "operation"),
    sorted(
        (item["provider"], OperationKind(item["operation"]))
        for item in executable_execution_operations()
    ),
)
def test_execute_plan_refuses_a_current_retained_plan_for_every_registered_pair(
    provider: str,
    operation: OperationKind,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, _record, record_digest = _retain(tmp_path, provider=provider, operation=operation)
    before = ledger.records("changes")
    monkeypatch.setattr(credentials_module, "_backend", lambda: pytest.fail("credential store"))
    monkeypatch.setattr(SafetyKernel, "execute", lambda *_a, **_k: pytest.fail("kernel"))

    result = _invoke(tmp_path, "execute plan", record_digest)

    assert result.exit_code == ExitCode.PRECONDITION
    assert _diagnostic(result)["code"] == "WRITES_NOT_RELEASED"
    assert Ledger(tmp_path).records("changes") == before


# --- the gate below the CLI --------------------------------------------------
#
# The CLI decorator is the first refusal, not the only one. Every route that can
# reach an adapter passes `SafetyKernel.execute` or `record_attempt_recovery`, and
# every retained workflow entry refuses on its own. These tests call them directly
# with a real retained plan and no `simulate_released_writes` fixture.

_KERNEL_AND_WORKFLOW_MODULES = {
    "safety",
    "generic_execution_workflow",
    "openai_pause_execution_workflow",
    "ga4_create_execution_workflow",
    "meta_pause_execution_workflow",
}


@pytest.fixture
def no_provider_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if a direct call gets as far as a lock, credential or egress permit."""

    def touched(surface: str) -> Any:
        def fail(*_args: object, **_kwargs: object) -> Any:
            pytest.fail(f"a direct call let the release-policy gate be bypassed: {surface}")

        return fail

    for name in ("get_keyring", "get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, touched(f"the OS credential store ({name})"))
    for name in ("_backend", "credential", "export_key", "openai_pause_binding_key"):
        monkeypatch.setattr(credentials_module, name, touched(f"adhelm.credentials.{name}"))
    monkeypatch.setattr(SafetyKernel, "_coordinator_locks", touched("a coordinator lock"))
    monkeypatch.setattr(egress_permit, "_kernel_permit", touched("an egress permit"))


class _TouchedAdapter:
    """An adapter that fails the test on any use."""

    def __getattr__(self, name: str) -> Any:
        pytest.fail(f"the release-policy gate let a direct call reach the adapter ({name})")


class _TouchedApprovalHost:
    def __getattr__(self, name: str) -> Any:
        pytest.fail(f"the release-policy gate let a direct call reach the approval host ({name})")


def _refused(call: Any) -> None:
    with pytest.raises(AdhelmError) as refused:
        call()
    assert refused.value.code == release_policy.WRITES_NOT_RELEASED
    assert refused.value.exit_code == ExitCode.PRECONDITION


def _grant(plan: CompositePlan) -> DirectGrant:
    return new_direct_grant(
        authorization_id="direct-1",
        plan=plan,
        grantor_ref="marketer",
        host_ref="local-trusted-host",
        user_interaction_ref="approved-interaction",
        expires_at=plan.expires_at,
    )


@pytest.mark.parametrize("entry", ["execute", "record_attempt_recovery"])
@pytest.mark.parametrize(
    ("provider", "operation"),
    sorted(
        (item["provider"], OperationKind(item["operation"]))
        for item in executable_execution_operations()
    ),
)
def test_the_kernel_refuses_every_registered_pair_before_any_lock_or_journal_write(
    entry: str,
    provider: str,
    operation: OperationKind,
    tmp_path: Path,
    no_provider_effect: None,
) -> None:
    ledger, record, _digest = _retain(tmp_path, provider=provider, operation=operation)
    before = ledger.records("changes")
    kernel = SafetyKernel(tmp_path, ledger)
    grant = _grant(record.plan)
    adapters = {provider: _TouchedAdapter()}

    if entry == "execute":
        _refused(lambda: kernel.execute(record.plan, grant, adapters))
    else:
        _refused(
            lambda: kernel.record_attempt_recovery(
                record.plan, grant, grant, _TouchedApprovalHost(), adapters
            )
        )

    assert Ledger(tmp_path, profile_ref=ledger.profile_ref).records("changes") == before
    assert not any(
        item["payload"].get("event_type") == "APPLY_STARTED"
        for item in ledger.records("changes")
        if item["record_kind"] == "CHANGE_EVENT"
    )


@pytest.mark.parametrize(
    ("provider", "operation"),
    sorted(
        (item["provider"], OperationKind(item["operation"]))
        for item in executable_execution_operations()
    ),
)
def test_the_generic_workflow_entries_refuse_every_registered_pair_directly(
    provider: str, operation: OperationKind, tmp_path: Path, no_provider_effect: None
) -> None:
    ledger, record, record_digest = _retain(tmp_path, provider=provider, operation=operation)
    before = ledger.records("changes")
    factory = _FakeAdapterFactory(_FakeExecutionAdapter(record.plan_digest))
    registry = {(provider, operation): factory}

    _refused(
        lambda: execute_retained_execution_plan_with_installed_approval(
            tmp_path,
            ExecutionPlanExecutionRequest(plan_record_digest=record_digest),
            _adapter_registry=registry,
        )
    )
    _refused(
        lambda: record_retained_attempt_recovery_with_installed_approval(
            tmp_path,
            AttemptRecoveryRequest(started_record_digest=record_digest),
            _adapter_registry=registry,
        )
    )

    assert factory.opened == 0
    assert factory.adapter.apply_calls == 0
    assert Ledger(tmp_path, profile_ref=ledger.profile_ref).records("changes") == before


def test_the_openai_pause_workflow_refuses_a_retained_plan_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_provider_effect: None
) -> None:
    harness = _OpenAIHarness(tmp_path, monkeypatch)
    # The harness stubbed the credential reads its planning step needed; from here
    # on any credential read fails the test.
    for module in (openai_workflow, openai_planning):
        for name in ("credential", "openai_pause_binding_key"):
            monkeypatch.setattr(module, name, lambda *_a, **_k: pytest.fail("credential read"))
    before = Ledger(tmp_path).records("changes")

    _refused(
        lambda: execute_retained_openai_pause_with_installed_approval(tmp_path, harness.request)
    )

    assert harness.scenario.requests == []
    assert Ledger(tmp_path).records("changes") == before


def test_the_meta_pause_workflow_refuses_a_retained_plan_directly(
    runtime: Any, no_provider_effect: None
) -> None:
    ledger, _action, request = _meta_prepared_record(runtime)
    before = ledger.records("changes")
    paths = list(runtime.paths)

    _refused(
        lambda: execute_retained_meta_campaign_pause_with_installed_approval(runtime.root, request)
    )

    assert runtime.paths == paths
    assert Ledger(runtime.root).records("changes") == before


@pytest.mark.parametrize("route", ["legacy_pinned_receipt", "installed_approval"])
def test_the_ga4_create_workflow_refuses_a_retained_plan_directly(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, receipt, challenge, trust, ledger, *_rest, scenario = _ga4_material(
        tmp_path, monkeypatch
    )
    before = ledger.records("changes")
    calls = len(scenario.calls)
    for module in (ga4_workflow, ga4_adapter):
        monkeypatch.setattr(module, "export_key", lambda: pytest.fail("credential read"))

    if route == "legacy_pinned_receipt":
        _refused(
            lambda: execute_retained_ga4_key_event_create(
                tmp_path, request, receipt=receipt, expected_challenge=challenge, trust=trust
            )
        )
    else:
        _refused(
            lambda: execute_retained_ga4_key_event_create_with_installed_approval(tmp_path, request)
        )

    assert scenario.posts == 0
    assert len(scenario.calls) == calls
    assert Ledger(tmp_path, profile_ref=ledger.profile_ref).records("changes") == before


def test_every_kernel_and_workflow_entry_calls_the_gate_first() -> None:
    """A structural backstop: a new entry or a new kernel user cannot skip the gate.

    The direct tests above prove behaviour for the entries they name. This pins
    the shape the runtime tests cannot enumerate: the set of modules, anywhere
    under src/adhelm, that build a `SafetyKernel`, and that each retained workflow
    entry in them opens with the gate call as its first statement after the
    docstring.
    """
    source = Path(release_policy.__file__).parent

    def is_gate_call(node: ast.stmt) -> bool:
        return (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func) == "release_policy.require_writes_released"
        )

    def first_statement(function: ast.FunctionDef) -> ast.stmt:
        body = list(function.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        return body[0]

    building_a_kernel: set[str] = set()
    entries: dict[str, list[str]] = {}
    for path in sorted(source.rglob("*.py")):
        # Keyed by the path below `source`, so a `ga4.py` in providers/ and one in
        # audits/ cannot collide and a module in a subpackage is never missed.
        module = path.relative_to(source).with_suffix("").as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if module != "safety" and any(
            isinstance(node, ast.Call) and ast.unparse(node.func) == "SafetyKernel"
            for node in ast.walk(tree)
        ):
            building_a_kernel.add(module)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and re.match(
                r"^(execute_retained_|record_retained_)", node.name
            ):
                entries.setdefault(module, []).append(node.name)
                assert is_gate_call(first_statement(node)), f"{module}.{node.name}"

    assert building_a_kernel == _KERNEL_AND_WORKFLOW_MODULES - {"safety"}
    assert set(entries) == building_a_kernel

    tree = ast.parse((source / "safety.py").read_text(encoding="utf-8"))
    kernel = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SafetyKernel"
    )
    for name in ("execute", "record_attempt_recovery"):
        method = next(
            node for node in kernel.body if isinstance(node, ast.FunctionDef) and node.name == name
        )
        assert is_gate_call(first_statement(method)), name


# --- nothing enables writes -------------------------------------------------


@pytest.mark.parametrize("path", sorted(_declared_leaves()))
def test_no_configuration_environment_or_flag_lifts_the_refusal(
    path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, untouchable: None
) -> None:
    leaf = _declared_leaves()[path]
    for name in (
        "ADHELM_WRITES_RELEASED",
        "ADHELM_ENABLE_WRITES",
        "ADHELM_RELEASE_POLICY",
        "ADHELM_EDITION",
        "ADHELM_WRITES_RELEASED",
        "ADHELM_ENABLE_WRITES",
    ):
        monkeypatch.setenv(name, "1")
    data = tmp_path / "data"
    save_configuration(data, Configuration())
    config = data / "config.json"
    document = json.loads(config.read_text(encoding="utf-8"))
    document.update(
        {"edition": "operator", "writes_released": True, "release_policy": "WRITES_RELEASED"}
    )
    config.write_text(json.dumps(document), encoding="utf-8")

    result = _invoke(data, path, *_arguments(leaf, tmp_path))

    assert _diagnostic(result)["code"] == "WRITES_NOT_RELEASED"

    for flag in ("--enable-writes", "--released", "--edition=operator", "--force"):
        flagged = _invoke(data, path, *_arguments(leaf, tmp_path), flag)
        assert flagged.exit_code == ExitCode.INVALID_INPUT, flag
        assert "WRITES" not in flagged.output


def test_the_gate_takes_no_input_and_imports_no_configuration_or_environment() -> None:
    """The refusal is a function of nothing, so nothing can be set to change it."""
    assert not inspect.signature(release_policy.require_writes_released).parameters

    tree = ast.parse(Path(release_policy.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name if isinstance(node, ast.Import) else node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }
    # `adhelm.cli` is the deferred import of the CLI's own failure envelope.
    assert imported == {
        "__future__",
        "functools",
        "collections.abc",
        "typing",
        "adhelm.errors",
        "adhelm.cli",
    }


def test_the_configuration_schema_has_no_edition_or_write_switch() -> None:
    fields = set(Configuration.model_fields)
    assert not {name for name in fields if re.search(r"edition|write|release|operator", name)}
    with pytest.raises(ValueError):
        Configuration.model_validate({"edition": "operator"})


# --- what stays available ---------------------------------------------------


def test_read_only_and_planning_commands_stay_available(tmp_path: Path) -> None:
    catalog = _invoke(tmp_path, "recover catalog")
    assert catalog.exit_code == 0, catalog.output

    discovery = json.loads(_RUNNER.invoke(root_cli.app, ["discover"]).output)["data"]
    policy = discovery["release_policy"]
    assert policy["remote_writes"] == "UNRELEASED"
    assert policy["refusal_code"] == "WRITES_NOT_RELEASED"
    assert policy["exit_code"] == 3
    assert policy["enabled_by"] == "NOTHING"
    assert "offline_planning" not in policy
    planning = policy["planning_without_remote_effect"]
    assert planning["status"] == "AVAILABLE"
    assert planning["execution"] == "UNRELEASED"
    # Some planning commands read provider state, so none is labelled offline.
    assert planning["remote_reads"] == "SOME_COMMANDS_READ_PROVIDER_STATE"
    assert {"compile google-ads", "plan execution create", "plan show"} <= set(planning["commands"])
    assert not set(planning["commands"]) & set(release_policy.unreleased_commands())
    assert {"discover", "report ga4", "audit", "history verify"} <= set(discovery["commands"])
    assert "edition" not in discovery
