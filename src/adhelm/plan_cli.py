"""Bounded plan preparation and historical review without plan-side execution.

Two families live here.  The original OpenAI Ads pause commands are unchanged.
The generic `plan execution` family below is the provider-neutral shape every
later CREATE, ACTIVATE, and MAINTAIN ticket reuses; it retains
:class:`adhelm.execution_plan_records.ExecutionPlanRecord` and issues no
authority of its own.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import typer
from pydantic import Field, field_validator, model_validator

from adhelm._installed_approval_execution_status import (
    INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
)
from adhelm.artifacts import (
    ArtifactContext,
    ArtifactDecision,
    ArtifactTaskOverride,
    resolve_artifact_policy,
)
from adhelm.canonical import digest
from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError, ExitCode
from adhelm.execution_plan_views import ExecutionPlanReview, review_execution_plan
from adhelm.execution_planning_session import (
    RetainedExecutionPlan,
    configured_execution_planner,
)
from adhelm.input_files import validate_json_file
from adhelm.ledger import Ledger
from adhelm.measurement_cli import _fits
from adhelm.models import (
    MAX_CHILD_EGRESS_COUNT,
    PROVIDERS,
    OutputPreferences,
    Provider,
    SafeRef,
    StrictModel,
)
from adhelm.pause_plan_artifacts import (
    PausePlanArtifactResult,
    pause_plan_artifact_failure_preview,
    preflight_openai_pause_plan_artifacts,
    render_openai_pause_plan_artifacts,
)
from adhelm.pause_workflow import (
    OpenAIPauseRequest,
    now_utc,
    pause_configuration,
    prepare_openai_pause,
)
from adhelm.plan_views import OpenAIPauseReview, preview_openai_pause
from adhelm.providers.google_ads_activate_contract import (
    GoogleAdsSearchCampaignActivateSelection,
)
from adhelm.providers.google_ads_create_contract import GoogleAdsSearchCampaignCreateSelection
from adhelm.providers.google_ads_maintain_contract import (
    GoogleAdsSearchCampaignMaintainSelection,
)
from adhelm.providers.meta_create_contract import MetaAdsSingleImageHierarchyCreateSelection
from adhelm.providers.openai_create_contract import (
    OpenAIAdsChatCardHierarchyCreateSelection,
)
from adhelm.safety import OperationKind
from adhelm.saved_execution_plans import execution_plan_catalog, load_execution_plan_record
from adhelm.saved_plans import (
    load_openai_pause_plan,
    openai_pause_plan_catalog,
    retain_openai_pause_plan,
)
from adhelm.strict_json import validate_json_value

app = typer.Typer(no_args_is_help=True)
_OUTCOME = "PLAN_REVIEW_NO_EXECUTION_AUTHORITY"


def _review_data(
    review: OpenAIPauseReview,
    provenance: dict[str, Any],
    *,
    artifact_result: dict[str, object] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "review": review.model_dump(mode="json"),
        "review_digest": review.review_digest,
        "provenance": provenance,
        "execution_authority": False,
        "remote_mutations": False,
        "execution_command": INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
    }
    if artifact_result is not None:
        data["artifact_result"] = artifact_result
    return data


def _require_fit(
    command: str,
    data: dict[str, Any],
    maximum: int,
    *,
    artifact_refs: list[str] | None = None,
) -> None:
    if not _fits(
        command,
        data,
        maximum,
        outcome=_OUTCOME,
        artifact_refs=artifact_refs or [],
    ):
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            "The complete pause review exceeds the saved output bound.",
            next_action="Increase max_output_bytes; critical review facts were preserved.",
        )


@app.command("openai-pause")
def openai_pause(ctx: typer.Context, input_file: Annotated[Path, typer.Option("--input")]) -> None:
    """Read a campaign baseline, retain its exact pause plan, and return a redacted review."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        request = validate_json_file(OpenAIPauseRequest, input_file)
        directory = root(ctx)
        preflight_configuration = pause_configuration(directory)
        artifact_decision = preflight_openai_pause_plan_artifacts(
            preflight_configuration.output, profile_id=request.profile_id
        )
        action, configuration = prepare_openai_pause(directory, request)
        if configuration.output != preflight_configuration.output:
            # The provider workflow revalidates configuration before touching
            # credentials. Re-resolve the optional local policy from that same
            # verified snapshot if it changed concurrently.
            artifact_decision = preflight_openai_pause_plan_artifacts(
                configuration.output, profile_id=request.profile_id
            )
        reviewed_at = now_utc()
        if action.plan.expires_at <= reviewed_at:
            raise AdhelmError("PLAN_EXPIRED", "Pause plan expired before retention.", exit_code=4)
        provenance: dict[str, Any] = {
            "record_digest": "0" * 64,
            "persisted": True,
            "historical_evidence_status": "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
            "requires_current_kernel_checks": True,
        }
        # Fixed-length pseudonyms let the complete output be sized before a
        # sharing key is touched or the canonical plan is appended.
        probe = preview_openai_pause(action, opaque_key=b"\0" * 32, now=reviewed_at)
        artifact_preview: dict[str, object] | None = None
        artifact_refs: list[str] = []
        if artifact_decision.outcome == "GENERATE":
            artifact_preview, artifact_refs = pause_plan_artifact_failure_preview(
                artifact_decision,
                source_record_digest="0" * 64,
                checkpoint_digest="0" * 64,
            )
        _require_fit(
            "plan openai-pause",
            _review_data(probe, provenance, artifact_result=artifact_preview),
            configuration.output.max_output_bytes,
            artifact_refs=artifact_refs,
        )
        sharing_key = export_key()
        review = preview_openai_pause(action, opaque_key=sharing_key, now=reviewed_at)
        if action.plan.expires_at <= now_utc():
            raise AdhelmError("PLAN_EXPIRED", "Pause plan expired before retention.", exit_code=4)
        try:
            record = retain_openai_pause_plan(Ledger(directory), action)
        except OSError:
            raise AdhelmError(
                "PLAN_RETENTION_UNVERIFIED",
                "Durable local plan retention could not be verified.",
                next_action="Inspect history verify and plan catalog before planning again.",
            ) from None
        provenance["record_digest"] = record["record_digest"]
        artifact_result: PausePlanArtifactResult | None = None
        if artifact_decision.outcome == "GENERATE":
            try:
                retained_action, retained_provenance = load_openai_pause_plan(
                    Ledger(directory), record["record_digest"]
                )
                artifact_result = render_openai_pause_plan_artifacts(
                    retained_action,
                    retained_provenance,
                    artifact_decision,
                    key=sharing_key,
                )
            except (AdhelmError, OSError):
                # The plan is already durably retained. Do not erase its review
                # or claim a static file when the optional verified-source path
                # cannot be established.
                artifact_result = PausePlanArtifactResult(
                    artifact_decision,
                    record["record_digest"],
                    None,
                    (),
                    generation_failure_code="PLAN_ARTIFACT_SOURCE_UNVERIFIED",
                )
        result_data = _review_data(
            review,
            provenance,
            artifact_result=artifact_result.to_data() if artifact_result else None,
        )
        result_refs = (
            ["artifact:sha256:" + item.artifact_digest for item in artifact_result.artifacts]
            if artifact_result
            else []
        )
        result_outcome = (
            "PARTIAL"
            if artifact_result is not None and artifact_result.generation_failure_code is not None
            else _OUTCOME
        )
        emit(
            "plan openai-pause",
            result_data,
            outcome=result_outcome,
            maximum_bytes=configuration.output.max_output_bytes,
            artifact_refs=result_refs,
        )
        if artifact_result is not None and artifact_result.generation_failure_code is not None:
            raise typer.Exit(5)

    guarded("plan openai-pause", work)


@app.command("catalog")
def catalog(ctx: typer.Context, cursor: int = 0, limit: int = 50) -> None:
    """List bounded historical plan metadata without credentials or provider reads."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        preferences = pause_configuration(root(ctx)).output
        if cursor < 0 or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_EXECUTION_PLAN_SELECTION", "Invalid plan pagination.", exit_code=2
            )
        data = openai_pause_plan_catalog(
            Ledger(root(ctx)), cursor=cursor, limit=min(limit, preferences.max_rows)
        )
        while not _fits(
            "plan catalog",
            data,
            preferences.max_output_bytes,
            outcome=_OUTCOME,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            if len(data["plans"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED", "Plan catalog exceeds the saved output bound."
                )
            data["plans"].pop()
            data["next_cursor"] = str(cursor + len(data["plans"]))
        emit(
            "plan catalog",
            data,
            outcome=_OUTCOME,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded("plan catalog", work)


@app.command("show")
def show(ctx: typer.Context, record_digest: str) -> None:
    """Review one verified retained plan; expiry does not hide historical facts."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        preferences = pause_configuration(root(ctx)).output
        action, provenance = load_openai_pause_plan(Ledger(root(ctx)), record_digest)
        reviewed_at = now_utc()
        probe = preview_openai_pause(action, opaque_key=b"\0" * 32, now=reviewed_at)
        _require_fit("plan show", _review_data(probe, provenance), preferences.max_output_bytes)
        review = preview_openai_pause(action, opaque_key=export_key(), now=reviewed_at)
        emit(
            "plan show",
            _review_data(review, provenance),
            outcome=_OUTCOME,
            maximum_bytes=preferences.max_output_bytes,
        )

    guarded("plan show", work)


# --------------------------------------------------------------------------
# Generic provider-neutral `plan execution` family (S3).
#
# The four provider-specific commands above and in the sibling modules are
# unchanged.  This family is the one command shape every provider CREATE,
# ACTIVATE, and MAINTAIN ticket reuses, so none of them ships a fifth
# retained-plan command, catalog, or review.
# --------------------------------------------------------------------------

execution_plan_app = typer.Typer(no_args_is_help=True)

_EXECUTION_PREFIX = "plan execution"
_EXECUTION_CREATE_OUTCOME = "EXECUTION_PLAN_RETAINED_NO_EXECUTION_AUTHORITY"
_EXECUTION_HISTORY_OUTCOME = "EXECUTION_PLAN_HISTORY_NO_EXECUTION_AUTHORITY"
_EXECUTION_DIGEST = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_EXECUTION_VIEW_MAXIMUM_BYTES = 65_536
_MAX_UTC_TIMESTAMP = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
_ExecutionDigest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
#: ``RECOVER`` authorizes recording an observation and sends no provider
#: request, so it is not a plan operation.  ``SubPlan`` already refuses it; the
#: command refuses it before any file, configuration, or policy work.
PLAN_OPERATIONS: tuple[str, ...] = tuple(
    sorted(item.value for item in OperationKind if item is not OperationKind.RECOVER)
)
_EXECUTION_ARTIFACT_COMMAND = "artifacts plan"
_EXECUTION_SUPPRESSION_REASONS = frozenset(
    {
        "ARTIFACT_MODE_OFF",
        "NO_MATCHING_WORKFLOW_RULE",
        "ON_REQUEST_REQUIRES_EXPLICIT_USER_REQUEST",
        "PURPOSE_NOT_ENABLED",
        "TASK_OVERRIDE_DISABLED",
    }
)
_EXECUTION_GENERATION_REASONS = frozenset({"EXPLICIT_USER_REQUEST", "MATCHING_SAVED_RULE"})


class ExecutionPlanRequest(StrictModel):
    """The closed generic selection a `plan execution create` file carries.

    A provider whose planner needs more than this extends the envelope as a
    discriminated union member in its own ticket, with its own schema and
    evidence.  Today the command refuses an unknown field rather than passing
    an uninterpreted body to a planner that does not exist.
    """

    schema_version: Literal[1] = 1
    profile_id: SafeRef
    provider: Provider
    operation: str = Field(min_length=1, max_length=32)
    account_ref: SafeRef
    resource_ref: SafeRef | None = None
    #: The provider-discriminated envelope extensions installed today.  Each is
    #: optional and separately named, so a request carries at most the one its
    #: own planner reads, and the command still refuses an unknown field: an
    #: envelope naming another provider's member is refused rather than passed
    #: on.  A provider whose planner needs more adds its own member here, with
    #: its own schema and evidence.
    selection: GoogleAdsSearchCampaignCreateSelection | None = None
    openai_selection: OpenAIAdsChatCardHierarchyCreateSelection | None = None
    meta_selection: MetaAdsSingleImageHierarchyCreateSelection | None = None
    #: P05, the spend-explicit activation of one retained CREATE plan.
    activate_selection: GoogleAdsSearchCampaignActivateSelection | None = None
    #: P05, one of the four guarded Search maintenance operations.
    maintain_selection: GoogleAdsSearchCampaignMaintainSelection | None = None
    supersedes_record_digest: _ExecutionDigest | None = None

    @field_validator("operation")
    @classmethod
    def planned_operation(cls, value: str) -> str:
        if value not in PLAN_OPERATIONS:
            raise ValueError("operation is not a compilable plan operation")
        return value

    @model_validator(mode="after")
    def one_provider_selection(self) -> "ExecutionPlanRequest":
        """Refuse a request that names more than one provider selection.

        Each member is read by exactly one registered planner, so an envelope
        carrying two of them describes two plans.  One plan is one operation.
        """

        supplied = sum(
            item is not None
            for item in (
                self.selection,
                self.openai_selection,
                self.activate_selection,
                self.maintain_selection,
            )
        )
        if supplied > 1:
            raise ValueError("one execution plan request carries at most one provider selection")
        return self


class _ExecutionCatalogRow(StrictModel):
    record_digest: _ExecutionDigest
    plan_digest: _ExecutionDigest
    provider: Provider
    operation: str = Field(min_length=1, max_length=32)
    recorded_at: datetime
    expires_at: datetime
    expected_child_egress_count: int = Field(ge=1, le=MAX_CHILD_EGRESS_COUNT)
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
    spend_bounds_by_currency: list[dict[str, Any]] = Field(max_length=1)
    supersedes_record_digest: _ExecutionDigest | None
    #: The later plan that replaced this one, when local history holds one.  A
    #: row with a value here is not the current plan and `execute plan` refuses
    #: it with EXECUTION_PLAN_SUPERSEDED.
    superseded_by: _ExecutionDigest | None
    execution_authority: Literal[False] = False

    @field_validator("recorded_at", "expires_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("catalog timestamp must be aware")
        return value.astimezone(UTC)

    @field_validator("execution_authority", mode="before")
    @classmethod
    def false_only(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("execution authority must be false")
        return value


class _ExecutionCatalog(StrictModel):
    plans: list[_ExecutionCatalogRow] = Field(max_length=500)
    checkpoint_digest: _ExecutionDigest
    total_count: int = Field(ge=0)
    next_cursor: str | None
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]

    @field_validator("next_cursor", mode="before")
    @classmethod
    def cursor(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or not value.isascii() or not value.isdecimal():
            raise ValueError("invalid cursor")
        return value


class _ExecutionProvenance(StrictModel):
    record_digest: _ExecutionDigest
    plan_digest: _ExecutionDigest
    subplan_digests: list[_ExecutionDigest] = Field(min_length=1, max_length=1)
    checkpoint_digest: _ExecutionDigest
    recorded_at: datetime
    provider: Provider
    operation: str = Field(min_length=1, max_length=32)
    supersedes_record_digest: _ExecutionDigest | None
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    execution_authority: Literal[False] = False
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("recorded_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provenance timestamp must be aware")
        return value.astimezone(UTC)


class _ExecutionArtifactNotice(StrictModel):
    """Closed saved/task output-policy fact beside the retained plan receipt."""

    outcome: Literal["GENERATE", "SUPPRESSED"]
    reason: str
    #: The generic family has no renderer of its own yet.  Saying so is the
    #: point: a GENERATE decision names the installed retained-plan artifact
    #: command instead of implying a file this command wrote.
    generation_supported: Literal[False] = False
    artifact_command: str = Field(min_length=1, max_length=180)

    @field_validator("generation_supported", mode="before")
    @classmethod
    def exact_generation_support(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("artifact support marker is invalid")
        return value

    @model_validator(mode="after")
    def exact_reason_for_outcome(self) -> "_ExecutionArtifactNotice":
        allowed = (
            _EXECUTION_GENERATION_REASONS
            if self.outcome == "GENERATE"
            else _EXECUTION_SUPPRESSION_REASONS
        )
        if self.reason not in allowed:
            raise ValueError("artifact policy reason is invalid")
        return self


class _ExecutionReceipt(StrictModel):
    record_digest: _ExecutionDigest
    plan_digest: _ExecutionDigest
    subplan_digests: tuple[_ExecutionDigest, ...] = Field(min_length=1, max_length=1)
    provider: Provider
    operation: str = Field(min_length=1, max_length=32)
    expected_child_egress_count: int = Field(ge=1, le=MAX_CHILD_EGRESS_COUNT)
    expires_at: datetime
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
    spend_bounds_by_currency: list[dict[str, Any]] = Field(max_length=1)
    supersedes_record_digest: _ExecutionDigest | None
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    review_required: Literal[True] = True
    review_command: str = Field(min_length=1, max_length=180)
    artifact_policy: _ExecutionArtifactNotice
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False
    review_authorizes_execution: Literal[False] = False
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("receipt expiry must be aware")
        return value.astimezone(UTC)


def _execution_invalid_selection(detail: str) -> AdhelmError:
    return AdhelmError("INVALID_EXECUTION_PLAN_SELECTION", detail, exit_code=ExitCode.INVALID_INPUT)


def _execution_require_digest(value: object) -> str:
    if type(value) is not str or _EXECUTION_DIGEST.fullmatch(value) is None:
        raise _execution_invalid_selection(
            "An exact retained execution plan record digest is required."
        )
    return value


def _execution_checked_provider(value: object) -> str:
    if type(value) is not str or value not in PROVIDERS:
        raise _execution_invalid_selection(
            "The selected provider is not one this installation implements."
        )
    return value


def _execution_checked_operation(value: object) -> OperationKind:
    if type(value) is not str or value not in PLAN_OPERATIONS:
        raise _execution_invalid_selection(
            "The selected operation is not a compilable plan operation."
        )
    return OperationKind(value)


def _execution_artifact_invalid() -> AdhelmError:
    return AdhelmError(
        "INVALID_EXECUTION_PLAN_ARTIFACT_REQUEST",
        "Execution plan artifact options are inconsistent.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action=(
            "Use --no-artifacts alone, or supply one or more formats with a local output directory."
        ),
    )


def _execution_override(
    no_artifacts: object, formats: object, output: object
) -> ArtifactTaskOverride | None:
    if type(no_artifacts) is not bool:
        raise _execution_artifact_invalid()
    if formats is None:
        values: tuple[Literal["json", "csv", "html"], ...] = ()
    elif (
        type(formats) is list
        and len(formats) <= 3
        and len(set(formats)) == len(formats)
        and all(type(item) is str and item in {"json", "csv", "html"} for item in formats)
    ):
        values = cast(tuple[Literal["json", "csv", "html"], ...], tuple(sorted(formats)))
    else:
        raise _execution_artifact_invalid()
    if output is not None and not isinstance(output, Path):
        raise _execution_artifact_invalid()
    if no_artifacts:
        if values or output is not None:
            raise _execution_artifact_invalid()
        return ArtifactTaskOverride(requested=False, purpose="plan_review")
    if values or output is not None:
        return ArtifactTaskOverride(
            requested=True,
            formats=values,
            output_directory=str(output) if output else None,
            purpose="plan_review",
        )
    return None


def _execution_artifact_notice(
    preferences: OutputPreferences,
    *,
    profile_id: str,
    provider: str,
    override: ArtifactTaskOverride | None,
) -> _ExecutionArtifactNotice:
    """Resolve the same closed F02 output policy the pause families resolve."""
    try:
        decision = resolve_artifact_policy(
            ArtifactContext(
                workflow="plan",
                stage="before_approval",
                purpose="plan_review",
                provider=cast(Provider, provider),
                profile_id=profile_id,
            ),
            preferences,
            task_override=override,
        )
        if type(decision) is not ArtifactDecision:
            raise ValueError("decision")
        if decision.outcome == "NEEDS_CONFIGURATION":
            raise AdhelmError(
                "ARTIFACT_CONFIGURATION_REQUIRED",
                "The selected execution plan artifact policy needs local configuration.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Configure supported formats and an existing local output directory, use "
                    f"--no-artifacts, or render a retained plan with "
                    f"{_EXECUTION_ARTIFACT_COMMAND}."
                ),
            )
        if decision.outcome == "GENERATE":
            outcome: Literal["GENERATE", "SUPPRESSED"] = "GENERATE"
        elif decision.outcome == "SUPPRESSED":
            outcome = "SUPPRESSED"
        else:
            raise ValueError("decision outcome")
        return _ExecutionArtifactNotice(
            outcome=outcome,
            reason=decision.reason,
            artifact_command=_EXECUTION_ARTIFACT_COMMAND,
        )
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "EXECUTION_PLAN_ARTIFACT_POLICY_INVALID",
            "Execution plan artifact policy is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _execution_receipt(
    *,
    record_digest: str,
    plan_digest: str,
    subplan_digests: tuple[str, ...],
    provider: str,
    operation: str,
    expected_child_egress_count: int,
    expires_at: datetime,
    capability_status: str,
    spend_bounds: list[dict[str, Any]],
    supersedes_record_digest: str | None,
    artifact: _ExecutionArtifactNotice,
) -> dict[str, Any]:
    return _ExecutionReceipt(
        record_digest=record_digest,
        plan_digest=plan_digest,
        subplan_digests=subplan_digests,
        provider=cast(Provider, provider),
        operation=operation,
        expected_child_egress_count=expected_child_egress_count,
        expires_at=expires_at,
        capability_status=cast(Any, capability_status),
        spend_bounds_by_currency=spend_bounds,
        supersedes_record_digest=supersedes_record_digest,
        historical_evidence_status="HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
        review_command=f"{_EXECUTION_PREFIX} show {record_digest}",
        artifact_policy=artifact,
    ).model_dump(mode="json", warnings="error")


def _execution_require_receipt_fit(data: dict[str, Any], maximum: int) -> None:
    if not _fits(
        _EXECUTION_PREFIX + " create",
        data,
        maximum,
        outcome=_EXECUTION_CREATE_OUTCOME,
    ):
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            "The required execution plan receipt exceeds the saved output bound.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Increase saved max_output_bytes before planning; no provider read or plan "
                "retention was performed."
            ),
        )


def _execution_not_compilable(provider: str, operation: OperationKind) -> AdhelmError:
    """Refuse an operation no installed planner compiles, and name the pair."""
    return AdhelmError(
        "EXECUTION_PLAN_NOT_COMPILABLE",
        "NOT_COMPILABLE: no installed planner compiles a "
        f"{provider} {operation.value} execution plan.",
        exit_code=ExitCode.PRECONDITION,
        next_action=(
            "Run discover for the installed plan operations; nothing was read, compiled, "
            "or retained."
        ),
    )


def _execution_receipt_from_retained(
    value: object, artifact: _ExecutionArtifactNotice
) -> dict[str, Any]:
    try:
        if type(value) is not RetainedExecutionPlan:
            raise ValueError("result")
        record = value.record
        return _execution_receipt(
            record_digest=_execution_require_digest(value.record_digest),
            plan_digest=record.plan_digest,
            subplan_digests=record.subplan_digests,
            provider=record.provider,
            operation=record.operation.value,
            expected_child_egress_count=record.expected_child_egress_count,
            expires_at=record.expires_at,
            capability_status=record.provenance.capability_status,
            spend_bounds=[
                bound.model_dump(mode="json") for bound in record.spend_bounds_by_currency
            ],
            supersedes_record_digest=record.supersedes_record_digest,
            artifact=artifact,
        )
    except AdhelmError:
        raise
    except Exception:
        # This boundary follows a possibly successful durable append.  A corrupt
        # returned object cannot establish that no plan was retained, so callers
        # must inspect local history instead of retrying.
        raise AdhelmError(
            "EXECUTION_PLAN_RECEIPT_UNVERIFIED",
            "The execution plan may be retained, but its receipt could not be verified.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action=(
                "Inspect history verify and plan execution catalog before planning again."
            ),
        ) from None


def _execution_closed_catalog(value: object) -> dict[str, Any]:
    try:
        return validate_json_value(_ExecutionCatalog, cast(dict[str, Any], value)).model_dump(
            mode="json", warnings="error"
        )
    except Exception:
        raise AdhelmError(
            "INVALID_EXECUTION_PLAN_CATALOG",
            "Execution plan catalog is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _execution_review_data(review: object, provenance: object) -> dict[str, Any]:
    try:
        if type(review) is not ExecutionPlanReview:
            raise ValueError("review")
        return {
            "review": review.model_dump(mode="json", warnings="error"),
            "review_digest": review.review_digest,
            "provenance": validate_json_value(
                _ExecutionProvenance, cast(dict[str, Any], provenance)
            ).model_dump(mode="json", warnings="error"),
            "historical_review": True,
            "execution_authority": False,
            "remote_mutations": False,
            "review_authorizes_execution": False,
        }
    except Exception:
        raise AdhelmError(
            "INVALID_EXECUTION_PLAN_REVIEW",
            "Execution plan review is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


@execution_plan_app.command("create")
def execution_create(
    ctx: typer.Context,
    provider: Annotated[str, typer.Option("--provider")],
    operation: Annotated[str, typer.Option("--operation")],
    input_file: Annotated[Path, typer.Option("--input")],
    no_artifacts: Annotated[bool, typer.Option("--no-artifacts")] = False,
    artifact_format: Annotated[list[str] | None, typer.Option("--artifact-format")] = None,
    artifact_output: Annotated[Path | None, typer.Option("--artifact-output")] = None,
) -> None:
    """Compile and retain one non-executable plan for one provider operation."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        selected_provider = _execution_checked_provider(provider)
        selected_operation = _execution_checked_operation(operation)
        # Strict regular-file parsing completes before configuration, output
        # policy, credentials, provider access, or durable plan retention.
        request = validate_json_file(ExecutionPlanRequest, input_file)
        if request.provider != selected_provider or request.operation != selected_operation.value:
            raise _execution_invalid_selection(
                "The request file names a different provider or operation than the command."
            )
        directory = root(ctx)
        configuration = load_configuration(directory)
        artifact = _execution_artifact_notice(
            configuration.output,
            profile_id=request.profile_id,
            provider=selected_provider,
            override=_execution_override(no_artifacts, artifact_format, artifact_output),
        )
        # Reserve a complete durable receipt before any planner, credential,
        # provider request, or ledger append.
        _execution_require_receipt_fit(
            _execution_receipt(
                record_digest="0" * 64,
                plan_digest="0" * 64,
                subplan_digests=("0" * 64,),
                provider=selected_provider,
                operation=selected_operation.value,
                expected_child_egress_count=MAX_CHILD_EGRESS_COUNT,
                expires_at=_MAX_UTC_TIMESTAMP,
                capability_status="DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED",
                spend_bounds=[{"currency": "USD", "maximum_minor_units": 9_007_199_254_740_991}],
                supersedes_record_digest="0" * 64,
                artifact=artifact,
            ),
            configuration.output.max_output_bytes,
        )
        planner = configured_execution_planner(selected_provider, selected_operation)
        if planner is None:
            raise _execution_not_compilable(selected_provider, selected_operation)
        retained = planner.plan_configured_execution(
            directory, request, expected_configuration_digest=digest(configuration)
        )
        emit(
            _EXECUTION_PREFIX + " create",
            _execution_receipt_from_retained(retained, artifact),
            outcome=_EXECUTION_CREATE_OUTCOME,
            maximum_bytes=configuration.output.max_output_bytes,
        )

    guarded(_EXECUTION_PREFIX + " create", work)


@execution_plan_app.command("catalog")
def execution_catalog(
    ctx: typer.Context,
    cursor: int = 0,
    limit: int = 50,
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    operation: Annotated[str | None, typer.Option("--operation")] = None,
    account_ref: Annotated[str | None, typer.Option("--account-ref")] = None,
) -> None:
    """List retained execution plan metadata without credential or provider access."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise _execution_invalid_selection("Execution plan pagination is invalid.")
        selected_provider = None if provider is None else _execution_checked_provider(provider)
        selected_operation = (
            None if operation is None else _execution_checked_operation(operation).value
        )
        preferences = load_configuration(root(ctx)).output
        data = _execution_closed_catalog(
            execution_plan_catalog(
                Ledger(root(ctx)),
                cursor=cursor,
                limit=min(limit, preferences.max_rows),
                provider=selected_provider,
                operation=selected_operation,
                account_ref=account_ref,
            )
        )
        while not _fits(
            _EXECUTION_PREFIX + " catalog",
            data,
            preferences.max_output_bytes,
            outcome=_EXECUTION_HISTORY_OUTCOME,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            plans = data["plans"]
            if type(plans) is not list or len(plans) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "Execution plan catalog exceeds the saved output bound.",
                    exit_code=ExitCode.PRECONDITION,
                    next_action="Increase saved max_output_bytes to review this catalog.",
                )
            plans.pop()
            data["next_cursor"] = str(cursor + len(plans))
        emit(
            _EXECUTION_PREFIX + " catalog",
            data,
            outcome=_EXECUTION_HISTORY_OUTCOME,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded(_EXECUTION_PREFIX + " catalog", work)


@execution_plan_app.command("show")
def execution_show(ctx: typer.Context, record_digest: str) -> None:
    """Show one redacted retained execution plan without refreshing it."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        selected = _execution_require_digest(record_digest)
        directory = root(ctx)
        preferences = load_configuration(directory).output
        record, provenance = load_execution_plan_record(Ledger(directory), selected)
        reviewed_at = datetime.now(UTC)
        probe = review_execution_plan(
            record,
            opaque_key=b"\0" * 32,
            reviewed_at=reviewed_at,
            max_output_bytes=_EXECUTION_VIEW_MAXIMUM_BYTES,
        )
        if not _fits(
            _EXECUTION_PREFIX + " show",
            _execution_review_data(probe, provenance),
            preferences.max_output_bytes,
            outcome=_EXECUTION_HISTORY_OUTCOME,
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "The complete execution plan review exceeds the saved output bound.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Increase saved max_output_bytes to review every historical limitation."
                ),
            )
        review = review_execution_plan(
            record,
            opaque_key=export_key(),
            reviewed_at=reviewed_at,
            max_output_bytes=_EXECUTION_VIEW_MAXIMUM_BYTES,
        )
        emit(
            _EXECUTION_PREFIX + " show",
            _execution_review_data(review, provenance),
            outcome=_EXECUTION_HISTORY_OUTCOME,
            maximum_bytes=preferences.max_output_bytes,
        )

    guarded(_EXECUTION_PREFIX + " show", work)


def register_commands(parent: typer.Typer) -> None:
    """Mount the generic `plan execution` family under the existing `plan` app."""

    parent.add_typer(execution_plan_app, name="execution")


__all__ = ["ExecutionPlanRequest", "app", "execution_plan_app", "register_commands"]


# P04, P04-OA and P04-MA register their execution planners and adapter factories here,
# at the end of the module `cli.py` already imports for the whole `plan` group,
# so `discover` renders every pair from the two registries themselves.  Nothing
# above changed: each registered planner recompiles the retained offline
# candidate its request names and retains the provider-neutral record beside its
# own provider-specific one.  The imports are deliberately last so a
# registration module can reach this module's request model lazily without an
# import cycle.
from adhelm.google_ads_activate_execution import (  # noqa: E402
    register_google_ads_activate_execution,
)
from adhelm.google_ads_create_execution import (  # noqa: E402
    register_google_ads_create_execution,
)
from adhelm.google_ads_maintain_execution import (  # noqa: E402
    register_google_ads_maintain_execution,
)
from adhelm.meta_create_execution import (  # noqa: E402
    register_meta_create_execution,
)
from adhelm.openai_create_execution import (  # noqa: E402
    register_openai_create_execution,
)

register_google_ads_create_execution()
register_meta_create_execution()
register_openai_create_execution()
# P05 registers ("google_ads", ACTIVATE) and the maintenance pair
# ("google_ads", UPDATE) here, beside the CREATE pairs, so `discover` renders
# every one of them from the two registries.  ("google_ads", ARCHIVE) is not
# registered: it is delete-class and no cited read can supply AGENTS rule 11's
# dependency proof, so every archive request would be refused at planning.
register_google_ads_activate_execution()
register_google_ads_maintain_execution()
