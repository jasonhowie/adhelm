"""Exact, bounded installed-approval execution command surfaces.

These commands do not accept a receipt, pin, key, grant, candidate, adapter,
transport, credential, provider request, or execution selector. Each is a
remote-mutation leaf refused by the release-policy gate (``adhelm.release_policy``)
before it reads input, configuration, credentials, native review, or remote
egress. The ordinary checked-in binding is also deliberately unconfigured. The
commands do not create artifacts.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal, cast

import typer
from pydantic import Field, field_validator

from adhelm._compiled_meta_campaign_pause_approval_bridge_v2 import (
    _require_compiled_meta_campaign_pause_approval_bridge_v2_ready,
)
from adhelm._installed_native_approval_bridge import (
    _require_compiled_installed_approval_bridge_ready,
)
from adhelm.canonical import canonical_bytes
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_create_execution_workflow import (
    Ga4KeyEventCreateExecutionRequest,
    execute_retained_ga4_key_event_create_with_installed_approval,
)
from adhelm.generic_execution_workflow import (
    ExecutionPlanExecutionRequest,
    execute_retained_execution_plan_with_installed_approval,
)
from adhelm.input_files import validate_json_file
from adhelm.measurement_cli import _fits
from adhelm.meta_pause_execution_workflow import (
    MetaCampaignPauseExecutionRequest,
    execute_retained_meta_campaign_pause_with_installed_approval,
)
from adhelm.models import MAX_CHILD_EGRESS_COUNT, PROVIDERS, Provider, StrictModel
from adhelm.openai_pause_execution_workflow import (
    OpenAIPauseExecutionRequest,
    execute_retained_openai_pause_with_installed_approval,
)
from adhelm.release_policy import remote_mutation_leaf
from adhelm.safety import EffectStatus, ExecutionResult
from adhelm.strict_json import validate_json_value

app = typer.Typer(no_args_is_help=True)

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_OPAQUE_REF = re.compile(r"^opaque:sha256:[a-f0-9]{64}$")
_EVIDENCE_REF = re.compile(r"^evidence:sha256:[a-f0-9]{64}$")
_OUTCOME = "EXECUTION_RESULT"
#: The three fixed provider leaves keep their original single-resource bound.
_MAX_OPAQUE_REFS = 1
#: The generic leaf executes any registered operation, including a CREATE whose
#: one approved subplan issues several journaled child requests.  A subplan may
#: issue at most ``MAX_CHILD_EGRESS_COUNT`` requests, so it can name at most
#: that many created resources; the bound is the kernel's own rather than a new
#: number.  Reserving it makes ``execute plan`` require a larger saved output
#: bound than the fixed leaves, which it refuses to exceed silently.
_MAX_GENERIC_OPAQUE_REFS = MAX_CHILD_EGRESS_COUNT
_MAX_EVIDENCE_REFS = 3
_Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
_OpaqueRef = Annotated[str, Field(pattern=r"^opaque:sha256:[a-f0-9]{64}$")]
_EvidenceRef = Annotated[str, Field(pattern=r"^evidence:sha256:[a-f0-9]{64}$")]
#: Widened from the three fixed leaves to every installed provider, because the
#: generic route executes whichever pair a provider ticket registers.  Each
#: fixed leaf still requires its own exact provider in the returned result:
#: :func:`_execution_result_data` takes that exact name as ``expected_provider``.
#: The two fixed leaves whose operation creates nothing.  The generic route
#: applies the same rule to the operation instead of the provider, in
#: ``generic_execution_workflow``, where the retained plan is in scope.
_RESOURCE_FREE_PROVIDERS = frozenset({"openai_ads", "meta_ads"})
#: The longest installed provider name, so the generic reservation is truthful.
_LONGEST_PROVIDER: Provider = "linkedin_ads"
_EffectStatusText = Literal[
    "NOT_STARTED",
    "APPLIED_VERIFIED",
    "FAILED_NO_EFFECT",
    "PARTIAL_EFFECT",
    "AMBIGUOUS_REMOTE_STATE",
]
_FixedNextAction = Literal[
    "Inspect history status.",
    "Prepare a fresh plan before another execution.",
    "Reconcile provider state before another execution.",
    "Resolve prior provider state before another execution.",
]

_NEXT_ACTION_BY_STATUS: dict[EffectStatus, _FixedNextAction] = {
    EffectStatus.APPLIED_VERIFIED: "Inspect history status.",
    EffectStatus.FAILED_NO_EFFECT: "Prepare a fresh plan before another execution.",
    EffectStatus.PARTIAL_EFFECT: "Reconcile provider state before another execution.",
    EffectStatus.AMBIGUOUS_REMOTE_STATE: "Reconcile provider state before another execution.",
    EffectStatus.NOT_STARTED: "Resolve prior provider state before another execution.",
}

_EXIT_CODE_BY_STATUS: dict[_EffectStatusText, ExitCode] = {
    "APPLIED_VERIFIED": ExitCode.OK,
    "FAILED_NO_EFFECT": ExitCode.PRECONDITION,
    "PARTIAL_EFFECT": ExitCode.PARTIAL,
    "AMBIGUOUS_REMOTE_STATE": ExitCode.AMBIGUOUS,
    "NOT_STARTED": ExitCode.PRECONDITION,
}


class _ExecutionProviderResult(StrictModel):
    """Closed agent-visible result for one direct-provider execution."""

    provider: Provider
    subplan_digest: _Digest
    status: _EffectStatusText
    #: The model carries the widest installed bound; each command still applies
    #: its own narrower maximum in :func:`_checked_ref_list` before this is
    #: built, so a fixed leaf cannot present a second resource reference.
    opaque_resource_refs: list[_OpaqueRef] = Field(max_length=_MAX_GENERIC_OPAQUE_REFS)
    evidence_refs: list[_EvidenceRef] = Field(max_length=_MAX_EVIDENCE_REFS)
    next_action: _FixedNextAction


class _ExecutionResultSurface(StrictModel):
    """Bounded result emitted after a SafetyKernel-owned execution returns."""

    plan_digest: _Digest
    authorization_consumed: bool
    recovery_required: bool
    provider_results: list[_ExecutionProviderResult] = Field(min_length=1, max_length=1)
    history_command: Literal["history status"] = "history status"

    @field_validator("authorization_consumed", "recovery_required", mode="before")
    @classmethod
    def exact_booleans(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("execution controls must be JSON booleans")
        return value


def _execution_result_probe() -> dict[str, object]:
    """Reserve the full fixed direct-execution envelope before side effects."""
    longest_action = max(_NEXT_ACTION_BY_STATUS.values(), key=len)
    surface = _ExecutionResultSurface(
        plan_digest="0" * 64,
        authorization_consumed=True,
        recovery_required=True,
        provider_results=[
            _ExecutionProviderResult(
                provider="openai_ads",
                subplan_digest="0" * 64,
                status="AMBIGUOUS_REMOTE_STATE",
                opaque_resource_refs=["opaque:sha256:" + "0" * 64],
                evidence_refs=[
                    "evidence:sha256:" + f"{index:064x}" for index in range(_MAX_EVIDENCE_REFS)
                ],
                next_action=longest_action,
            )
        ],
    )
    return surface.model_dump(mode="json", warnings="error")


def _generic_execution_result_probe() -> dict[str, object]:
    """Reserve the widest generic-execution envelope before side effects.

    The generic leaf reserves the longest installed provider name and the full
    per-child resource bound, so a plan whose adapter names one resource for
    every approved child request still emits a complete result rather than an
    unpresentable one.
    """
    longest_action = max(_NEXT_ACTION_BY_STATUS.values(), key=len)
    surface = _ExecutionResultSurface(
        plan_digest="0" * 64,
        authorization_consumed=True,
        recovery_required=True,
        provider_results=[
            _ExecutionProviderResult(
                provider=_LONGEST_PROVIDER,
                subplan_digest="0" * 64,
                status="AMBIGUOUS_REMOTE_STATE",
                opaque_resource_refs=[
                    "opaque:sha256:" + f"{index:064x}" for index in range(_MAX_GENERIC_OPAQUE_REFS)
                ],
                evidence_refs=[
                    "evidence:sha256:" + f"{index:064x}" for index in range(_MAX_EVIDENCE_REFS)
                ],
                next_action=longest_action,
            )
        ],
    )
    return surface.model_dump(mode="json", warnings="error")


def _output_limit_exceeded() -> AdhelmError:
    return AdhelmError(
        "OUTPUT_LIMIT_EXCEEDED",
        "The complete execution result exceeds the saved output bound.",
        next_action=(
            "Increase saved max_output_bytes; no approval bridge, credential, native review, "
            "or provider request was started."
        ),
    )


def _require_result_fit(command: str, maximum_bytes: int) -> None:
    if not _fits(command, _execution_result_probe(), maximum_bytes, outcome=_OUTCOME):
        raise _output_limit_exceeded()


def _require_generic_result_fit(command: str, maximum_bytes: int) -> None:
    if not _fits(command, _generic_execution_result_probe(), maximum_bytes, outcome=_OUTCOME):
        raise _output_limit_exceeded()


def _checked_record_digest(value: object) -> str:
    """Refuse a malformed generic selector before any other work.

    The generic leaves take one positional record digest rather than an input
    file, so this is where their whole input is validated: before the data
    directory is resolved, before configuration is read, before the output
    bound is reserved, and before native readiness is asked for anything.
    """

    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise AdhelmError(
            "INVALID_EXECUTION_SELECTION",
            "An exact 64 character lowercase hexadecimal record digest is required.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return value


def _checked_ref_list(value: object, *, pattern: re.Pattern[str], maximum: int) -> list[str]:
    if type(value) is not tuple or len(value) > maximum:
        raise ValueError("reference list is invalid")
    if any(type(item) is not str or pattern.fullmatch(item) is None for item in value):
        raise ValueError("reference is invalid")
    if len(set(value)) != len(value):
        raise ValueError("references must be unique")
    return list(value)


def _execution_result_data(
    value: object,
    *,
    expected_provider: Provider | None,
    maximum_opaque_refs: int = _MAX_OPAQUE_REFS,
) -> dict[str, object]:
    """Detach a kernel result into the fixed redacted command output shape.

    ``expected_provider`` is the fixed provider of one of the three provider
    leaves.  The generic leaf passes ``None``, because which installed provider
    executed is decided by the retained plan rather than by the command; the
    workflow binds the result to that plan's own subplan before it returns, so
    this boundary only requires an installed provider name.
    """
    try:
        if type(value) is not ExecutionResult:
            raise ValueError("result type")
        checked = ExecutionResult.model_validate_json(
            canonical_bytes(value.model_dump(mode="json", warnings="error"))
        )
        if (
            type(checked.plan_digest) is not str
            or _DIGEST.fullmatch(checked.plan_digest) is None
            or type(checked.consumed) is not bool
            or checked.consumed is not True
            or type(checked.recovery_required) is not bool
            or type(checked.results) is not tuple
            or len(checked.results) != 1
        ):
            raise ValueError("result facts")
        provider_result = checked.results[0]
        if type(provider_result.provider) is not str or (
            provider_result.provider != expected_provider
            if expected_provider is not None
            else provider_result.provider not in PROVIDERS
        ):
            raise ValueError("provider")
        provider = cast(Provider, provider_result.provider)
        if (
            type(provider_result.subplan_digest) is not str
            or _DIGEST.fullmatch(provider_result.subplan_digest) is None
        ):
            raise ValueError("subplan")
        if type(provider_result.status) is not EffectStatus:
            raise ValueError("effect status")
        expected_recovery = provider_result.status is not EffectStatus.APPLIED_VERIFIED
        if checked.recovery_required is not expected_recovery:
            raise ValueError("recovery result")
        resources = _checked_ref_list(
            provider_result.resource_refs, pattern=_OPAQUE_REF, maximum=maximum_opaque_refs
        )
        evidence = _checked_ref_list(
            provider_result.evidence_refs, pattern=_EVIDENCE_REF, maximum=_MAX_EVIDENCE_REFS
        )
        if expected_provider in _RESOURCE_FREE_PROVIDERS and resources:
            raise ValueError("pause resource references are invalid")
        surface = _ExecutionResultSurface(
            plan_digest=checked.plan_digest,
            authorization_consumed=checked.consumed,
            recovery_required=checked.recovery_required,
            provider_results=[
                _ExecutionProviderResult(
                    provider=provider,
                    subplan_digest=provider_result.subplan_digest,
                    status=provider_result.status.value,
                    opaque_resource_refs=resources,
                    evidence_refs=evidence,
                    next_action=_NEXT_ACTION_BY_STATUS[provider_result.status],
                )
            ],
        )
        return validate_json_value(
            _ExecutionResultSurface, surface.model_dump(mode="json", warnings="error")
        ).model_dump(mode="json", warnings="error")
    except Exception:
        raise AdhelmError(
            "EXECUTION_RESULT_INVALID",
            "The local execution result could not be safely presented.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history status before requesting another execution.",
        ) from None


def _execution_exit_code(data: object) -> ExitCode:
    """Map the closed result status to its fixed non-authorizing process exit."""
    try:
        if type(data) is not dict or type(data.get("recovery_required")) is not bool:
            raise ValueError("result data")
        provider_results = data.get("provider_results")
        if type(provider_results) is not list or len(provider_results) != 1:
            raise ValueError("provider results")
        provider = provider_results[0]
        if type(provider) is not dict:
            raise ValueError("provider result")
        status = provider.get("status")
        if type(status) is not str or status not in _EXIT_CODE_BY_STATUS:
            raise ValueError("status")
        exit_code = _EXIT_CODE_BY_STATUS[status]
        if (exit_code is ExitCode.OK) != (data["recovery_required"] is False):
            raise ValueError("recovery status")
        return exit_code
    except Exception:
        raise AdhelmError(
            "EXECUTION_RESULT_INVALID",
            "The local execution result could not be safely presented.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history status before requesting another execution.",
        ) from None


def _completed_workflow_result(
    workflow: Callable[..., ExecutionResult], directory: Path, request: object
) -> ExecutionResult:
    try:
        return workflow(directory, request)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "EXECUTION_RESULT_UNAVAILABLE",
            "The execution workflow did not return a safely presentable result.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history status before requesting another execution.",
        ) from None


def _emit_execution_result(
    command: str,
    result: ExecutionResult,
    *,
    maximum_bytes: int,
    expected_provider: Provider | None,
    maximum_opaque_refs: int = _MAX_OPAQUE_REFS,
) -> None:
    """Emit the bounded result, then map its status to a fixed process exit."""
    from adhelm.cli import emit

    data = _execution_result_data(
        result, expected_provider=expected_provider, maximum_opaque_refs=maximum_opaque_refs
    )
    emit(command, data, outcome=_OUTCOME, maximum_bytes=maximum_bytes)
    exit_code = _execution_exit_code(data)
    if exit_code is not ExitCode.OK:
        raise typer.Exit(exit_code)


def _execute(
    ctx: typer.Context,
    *,
    command: str,
    input_file: Path,
    request_model: (
        type[OpenAIPauseExecutionRequest]
        | type[Ga4KeyEventCreateExecutionRequest]
        | type[MetaCampaignPauseExecutionRequest]
    ),
    expected_provider: Provider,
    workflow: Callable[..., ExecutionResult],
    readiness: Callable[[], None] | None = None,
) -> None:
    from adhelm.cli import guarded, root

    def work() -> None:
        request = validate_json_file(request_model, input_file)
        directory = root(ctx)
        configuration = load_configuration(directory)
        _require_result_fit(command, configuration.output.max_output_bytes)
        (readiness or _require_compiled_installed_approval_bridge_ready)()
        result = _completed_workflow_result(workflow, directory, request)
        _emit_execution_result(
            command,
            result,
            maximum_bytes=configuration.output.max_output_bytes,
            expected_provider=expected_provider,
        )

    guarded(command, work)


@app.command("openai-pause")
@remote_mutation_leaf("execute openai-pause")
def openai_pause(ctx: typer.Context, input_file: Annotated[Path, typer.Option("--input")]) -> None:
    """Execute one retained OpenAI pause through the private installed bridge."""
    _execute(
        ctx,
        command="execute openai-pause",
        input_file=input_file,
        request_model=OpenAIPauseExecutionRequest,
        expected_provider="openai_ads",
        workflow=execute_retained_openai_pause_with_installed_approval,
    )


@app.command("ga4-key-event-create")
@remote_mutation_leaf("execute ga4-key-event-create")
def ga4_key_event_create(
    ctx: typer.Context, input_file: Annotated[Path, typer.Option("--input")]
) -> None:
    """Execute one retained GA4 key-event CREATE through the private bridge."""
    _execute(
        ctx,
        command="execute ga4-key-event-create",
        input_file=input_file,
        request_model=Ga4KeyEventCreateExecutionRequest,
        expected_provider="ga4_admin",
        workflow=execute_retained_ga4_key_event_create_with_installed_approval,
    )


@app.command("meta-campaign-pause")
@remote_mutation_leaf("execute meta-campaign-pause")
def meta_campaign_pause(
    ctx: typer.Context, input_file: Annotated[Path, typer.Option("--input")]
) -> None:
    """Execute one retained Meta pause through its separate native V2 bridge."""
    _execute(
        ctx,
        command="execute meta-campaign-pause",
        input_file=input_file,
        request_model=MetaCampaignPauseExecutionRequest,
        expected_provider="meta_ads",
        workflow=execute_retained_meta_campaign_pause_with_installed_approval,
        readiness=_require_compiled_meta_campaign_pause_approval_bridge_v2_ready,
    )


@app.command("plan")
@remote_mutation_leaf("execute plan")
def execute_plan(ctx: typer.Context, record_digest: str) -> None:
    """Execute one retained provider-neutral execution plan through the bridge.

    This is the generic leaf.  It takes one retained
    ``EXECUTION_PLAN_RETAINED`` record digest and nothing else: no receipt,
    challenge, trust pin, key, grant, adapter, transport, credential, provider
    request, or artifact option.  Which provider and operation run is decided
    by the retained plan, and only by an adapter a provider ticket registered.
    """
    from adhelm.cli import guarded, root

    command = "execute plan"

    def work() -> None:
        selected = _checked_record_digest(record_digest)
        directory = root(ctx)
        configuration = load_configuration(directory)
        _require_generic_result_fit(command, configuration.output.max_output_bytes)
        _require_compiled_installed_approval_bridge_ready()
        result = _completed_workflow_result(
            execute_retained_execution_plan_with_installed_approval,
            directory,
            ExecutionPlanExecutionRequest(plan_record_digest=selected),
        )
        _emit_execution_result(
            command,
            result,
            maximum_bytes=configuration.output.max_output_bytes,
            expected_provider=None,
            maximum_opaque_refs=_MAX_GENERIC_OPAQUE_REFS,
        )

    guarded(command, work)
