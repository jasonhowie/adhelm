"""Bounded GA4 key-event CREATE plan preparation and historical review.

The create command retains an exact local plan but cannot authorize or send a
provider mutation. It preflights a compact receipt before local-key/provider
work and fails closed if that receipt cannot fit the installed output bound;
the complete redacted planning review is available through ``show`` or an
explicit retained static artifact request.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import typer
from pydantic import Field, field_validator

from adhelm._installed_approval_execution_status import (
    INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
)
from adhelm.artifacts import ArtifactDecision
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_create_plan_records import ga4_key_event_create_plan_record
from adhelm.ga4_create_plan_views import preview_ga4_key_event_create_plan
from adhelm.ga4_create_workflow import (
    Ga4KeyEventCreatePlanRequest,
    RetainedGa4KeyEventCreatePlan,
    prepare_and_retain_ga4_key_event_create_plan,
)
from adhelm.input_files import validate_json_file
from adhelm.ledger import Ledger
from adhelm.measurement_cli import _fits
from adhelm.models import Money, OutputPreferences, StrictModel
from adhelm.saved_ga4_create_plans import (
    ga4_key_event_create_plan_catalog,
    load_ga4_key_event_create_plan,
)
from adhelm.strict_json import validate_json_value

app = typer.Typer(no_args_is_help=True)

_PREFIX = "plan ga4-key-event"
_CREATE_OUTCOME = "GA4_KEY_EVENT_CREATE_PLAN_RETAINED_NO_EXECUTION_AUTHORITY"
_HISTORY_OUTCOME = "GA4_KEY_EVENT_CREATE_PLAN_HISTORY_NO_EXECUTION_AUTHORITY"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_MAX_UTC_TIMESTAMP = "9999-12-31T23:59:59.999999+00:00"
_Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class _Ga4KeyEventCreateCatalogRow(StrictModel):
    """One allowlisted, non-authorizing historical plan catalog row."""

    record_digest: _Digest
    plan_digest: _Digest
    recorded_at: datetime
    expires_at: datetime
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
    maximum_incremental_spend: Money
    execution_authority: Literal[False] = False

    @field_validator("recorded_at", "expires_at")
    @classmethod
    def aware_times(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("catalog timestamp must be aware")
        return value.astimezone(UTC)

    @field_validator("execution_authority", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("catalog authority must be false")
        return value


class _Ga4KeyEventCreateCatalog(StrictModel):
    """Closed catalog response with fixed command-level non-authority facts."""

    plans: list[_Ga4KeyEventCreateCatalogRow] = Field(max_length=500)
    checkpoint_digest: _Digest
    total_count: int = Field(ge=0)
    next_cursor: str | None
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_command: Literal["UNRELEASED"] = INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS

    @field_validator("total_count", mode="before")
    @classmethod
    def exact_count(cls, value: object) -> int:
        if type(value) is not int:
            raise ValueError("catalog count must be an integer")
        return value

    @field_validator("next_cursor", mode="before")
    @classmethod
    def exact_cursor(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or not value.isascii() or not value.isdecimal():
            raise ValueError("catalog cursor is invalid")
        return value

    @field_validator("execution_authority", "remote_mutations", mode="before")
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("catalog controls must be false")
        return value


class _Ga4KeyEventCreatePlanProvenance(StrictModel):
    """Closed historical provenance that can appear beside a redacted review."""

    record_digest: _Digest
    plan_digest: _Digest
    action_digest: _Digest
    checkpoint_digest: _Digest
    recorded_at: datetime
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    execution_authority: Literal[False] = False
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("recorded_at")
    @classmethod
    def aware_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provenance timestamp must be aware")
        return value.astimezone(UTC)

    @field_validator("execution_authority", mode="before")
    @classmethod
    def exact_false_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("provenance authority must be false")
        return value

    @field_validator("requires_current_kernel_checks", mode="before")
    @classmethod
    def exact_true_kernel_check(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("provenance kernel check must be true")
        return value


def _require_digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise AdhelmError(
            "GA4_CREATE_PLAN_RESULT_INVALID", "Retained CREATE plan result is invalid."
        )
    return value


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _reviewed_at() -> datetime:
    try:
        value = _now_utc()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock")
        return value.astimezone(UTC)
    except Exception:
        raise AdhelmError("CLOCK_INVALID", "Plan review requires an aware local clock.") from None


def _review_command(record_digest: str) -> str:
    return f"plan ga4-key-event show {record_digest}"


def _receipt(
    *,
    record_digest: str,
    observation_record_digest: str,
    action_digest: str,
    plan_digest: str,
    expires_at: str,
    capability_status: str,
    maximum_incremental_spend: dict[str, object],
) -> dict[str, object]:
    """Return the fixed receipt surface reserved before local key or HTTP use."""
    return {
        "record_digest": record_digest,
        "observation_record_digest": observation_record_digest,
        "action_digest": action_digest,
        "plan_digest": plan_digest,
        "expires_at": expires_at,
        "capability_status": capability_status,
        "maximum_incremental_spend": maximum_incremental_spend,
        "review_required": True,
        "review_command": _review_command(record_digest),
        "execution_authority": False,
        "remote_mutations": False,
        "execution_command": INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
    }


def _receipt_probe() -> dict[str, object]:
    """Use longest closed values so a retained receipt cannot overflow later."""
    placeholder = "0" * 64
    return _receipt(
        record_digest=placeholder,
        observation_record_digest=placeholder,
        action_digest=placeholder,
        plan_digest=placeholder,
        expires_at=_MAX_UTC_TIMESTAMP,
        capability_status="DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED",
        maximum_incremental_spend={"minor_units": 0, "currency": "USD"},
    )


def _create_result_data(
    receipt: dict[str, object], *, artifact_result: dict[str, object] | None = None
) -> dict[str, object]:
    """Keep the durable plan receipt visible if optional files later fail."""
    if artifact_result is None:
        return receipt
    return {**receipt, "artifact_result": artifact_result}


def _require_receipt_fit(
    data: dict[str, object], maximum: int, *, artifact_refs: list[str] | None = None
) -> None:
    if not _fits(
        _PREFIX + " create",
        data,
        maximum,
        outcome=_CREATE_OUTCOME,
        artifact_refs=artifact_refs or [],
    ):
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            "The required GA4 CREATE plan receipt exceeds the saved output bound.",
            next_action=(
                "Increase saved max_output_bytes; no provider read, local key, or plan retention "
                "was performed."
            ),
        )


def _preflight_artifact_policy(
    *, preferences: OutputPreferences, profile_id: str
) -> ArtifactDecision:
    """Resolve the exact GA4 before-approval policy before workflow reads."""
    from adhelm.ga4_create_plan_artifacts import preflight_ga4_key_event_create_plan_artifacts

    return preflight_ga4_key_event_create_plan_artifacts(preferences, profile_id=profile_id)


def _read_request(input_file: Path) -> Ga4KeyEventCreatePlanRequest:
    return validate_json_file(Ga4KeyEventCreatePlanRequest, input_file)


def _receipt_retention_unverified() -> AdhelmError:
    """Report a post-retention receipt failure without inviting a blind repeat."""
    return AdhelmError(
        "GA4_CREATE_PLAN_RECEIPT_UNVERIFIED",
        "The GA4 CREATE plan may be retained, but its receipt could not be verified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Inspect history verify and plan ga4-key-event catalog before preparing another "
            "plan; do not repeat an ambiguous plan preparation."
        ),
    )


def _receipt_from_retained(ledger: Ledger, value: object) -> dict[str, object]:
    """Bind the fixed receipt to the exact verified plan record before emitting it."""
    if type(value) is not RetainedGa4KeyEventCreatePlan:
        raise AdhelmError(
            "GA4_CREATE_PLAN_RESULT_INVALID", "Retained CREATE plan result is invalid."
        )
    try:
        retained = value
        record_digest = _require_digest(getattr(retained, "plan_record_digest", None))
        observation_record_digest = _require_digest(
            getattr(retained, "observation_record_digest", None)
        )
        returned_record = ga4_key_event_create_plan_record(retained.action)
        stored_action, provenance = load_ga4_key_event_create_plan(ledger, record_digest)
        record = ga4_key_event_create_plan_record(stored_action)
        action = record.action
        if (
            type(provenance) is not dict
            or provenance.get("record_digest") != record_digest
            or provenance.get("action_digest") != record.action_digest
            or provenance.get("plan_digest") != record.plan_digest
            or canonical_bytes(returned_record.action) != canonical_bytes(action)
            or observation_record_digest != action.observation_binding.observation_record_digest
        ):
            raise ValueError("observation binding")
        money = record.maximum_incremental_spend.model_dump(mode="json", warnings="error")
        if type(money) is not dict:
            raise ValueError("money")
        return _receipt(
            record_digest=record_digest,
            observation_record_digest=observation_record_digest,
            action_digest=record.action_digest,
            plan_digest=record.plan_digest,
            expires_at=record.expires_at.isoformat(),
            capability_status=action.status,
            maximum_incremental_spend=money,
        )
    except OSError:
        raise _receipt_retention_unverified() from None
    except Exception:
        raise AdhelmError(
            "GA4_CREATE_PLAN_RESULT_INVALID", "Retained CREATE plan result is invalid."
        ) from None


def _review_data(review: object, provenance: object) -> dict[str, object]:
    """Close loader provenance before it reaches the agent-visible review result."""
    try:
        if type(review) is not dict:
            raise ValueError("review shape")
        # The preview function reconstructs this value from a closed retained
        # action.  Ensure it is at least canonical-JSON representable here,
        # while provenance uses a separate exact allowlist below.
        canonical_bytes(review)
        if type(provenance) is not dict:
            raise ValueError("provenance shape")
        closed_provenance = validate_json_value(
            _Ga4KeyEventCreatePlanProvenance, cast(dict[str, Any], provenance)
        ).model_dump(mode="json", warnings="error")
        return {
            "review": review,
            "provenance": closed_provenance,
            "historical_review": True,
            "execution_authority": False,
            "remote_mutations": False,
            "execution_command": INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
        }
    except Exception:
        raise AdhelmError(
            "INVALID_GA4_CREATE_PLAN_REVIEW", "GA4 CREATE plan review is invalid."
        ) from None


def _closed_catalog(value: object) -> dict[str, Any]:
    """Validate saved history before adding it to an agent-visible result."""
    try:
        if type(value) is not dict:
            raise ValueError("catalog shape")
        return validate_json_value(_Ga4KeyEventCreateCatalog, value).model_dump(mode="json")
    except Exception:
        raise AdhelmError(
            "INVALID_GA4_CREATE_PLAN_CATALOG", "GA4 CREATE plan catalog is invalid."
        ) from None


@app.command("create")
def create(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Option("--input")],
) -> None:
    """Read fixed EDIT inventory, retain a non-executable CREATE plan, and return its receipt."""
    from adhelm.cli import emit, guarded, root

    command = _PREFIX + " create"

    def work() -> None:
        from adhelm.ga4_create_plan_artifacts import (
            ga4_create_plan_artifact_failure_preview,
            ga4_create_plan_artifact_failure_result,
            preflight_ga4_key_event_create_plan_artifact_output,
            render_ga4_key_event_create_plan_artifacts,
            revalidate_ga4_key_event_create_plan_artifact_source,
        )

        request = _read_request(input_file)
        directory = root(ctx)
        configuration = load_configuration(directory)
        artifact_decision = _preflight_artifact_policy(
            preferences=configuration.output,
            profile_id=request.profile_id,
        )
        artifact_preview: dict[str, object] | None = None
        artifact_refs: list[str] = []
        if artifact_decision.outcome == "GENERATE":
            artifact_preview, artifact_refs = ga4_create_plan_artifact_failure_preview(
                artifact_decision,
                source_record_digest="0" * 64,
                checkpoint_digest="0" * 64,
            )
        _require_receipt_fit(
            _create_result_data(_receipt_probe(), artifact_result=artifact_preview),
            configuration.output.max_output_bytes,
            artifact_refs=artifact_refs,
        )
        retained = prepare_and_retain_ga4_key_event_create_plan(
            directory,
            request,
            expected_configuration_digest=digest(configuration),
        )
        try:
            receipt = _receipt_from_retained(Ledger(directory), retained)
        except OSError:
            # Ledger construction itself can fail after the workflow has made
            # a durable append; preserve that ambiguity rather than inviting
            # an automatic or blind second preparation.
            raise _receipt_retention_unverified() from None
        artifact_result: dict[str, object] | None = None
        artifact_failure = False
        artifact_refs = []
        if artifact_decision.outcome == "GENERATE":
            source_checkpoint_digest: str | None = None
            try:
                action, provenance = load_ga4_key_event_create_plan(
                    Ledger(directory), _require_digest(receipt["record_digest"])
                )
                action, source = revalidate_ga4_key_event_create_plan_artifact_source(
                    action, provenance
                )
                if (
                    source.record_digest != receipt["record_digest"]
                    or source.action_digest != receipt["action_digest"]
                    or source.plan_digest != receipt["plan_digest"]
                ):
                    raise AdhelmError(
                        "GA4_CREATE_PLAN_ARTIFACT_SOURCE_INVALID",
                        "Verified GA4 CREATE plan artifact source does not match the retained "
                        "receipt.",
                        exit_code=2,
                    )
                source_checkpoint_digest = source.checkpoint_digest
                source_data = source.model_dump(mode="json", warnings="error")
                # This full-content preflight occurs after durable source reload,
                # before a sharing key or artifact writer is reached.
                preflight_ga4_key_event_create_plan_artifact_output(
                    action, source_data, artifact_decision
                )
                rendered = render_ga4_key_event_create_plan_artifacts(
                    action,
                    source_data,
                    artifact_decision,
                    key=export_key(),
                )
            except AdhelmError as exc:
                rendered = ga4_create_plan_artifact_failure_result(
                    artifact_decision,
                    source_record_digest=_require_digest(receipt["record_digest"]),
                    checkpoint_digest=source_checkpoint_digest,
                    failure_code=exc.code,
                )
            except (OSError, TypeError, ValueError):
                rendered = ga4_create_plan_artifact_failure_result(
                    artifact_decision,
                    source_record_digest=_require_digest(receipt["record_digest"]),
                    checkpoint_digest=source_checkpoint_digest,
                    failure_code="GA4_CREATE_PLAN_ARTIFACT_SOURCE_UNVERIFIED",
                )
            artifact_result = rendered.to_data()
            artifact_refs = [
                "artifact:sha256:" + item.artifact_digest for item in rendered.artifacts
            ]
            artifact_failure = rendered.generation_failure_code is not None
        # ``_receipt_probe`` plus the maximum partial artifact preview reserve
        # every emitted reference before provider reads. A later optional file
        # failure therefore cannot erase the durable plan receipt.
        emit(
            command,
            _create_result_data(receipt, artifact_result=artifact_result),
            outcome="PARTIAL" if artifact_failure else _CREATE_OUTCOME,
            maximum_bytes=configuration.output.max_output_bytes,
            artifact_refs=artifact_refs,
        )
        if artifact_failure:
            raise typer.Exit(5)

    guarded(command, work)


@app.command("catalog")
def catalog(ctx: typer.Context, cursor: int = 0, limit: int = 50) -> None:
    """List bounded retained GA4 CREATE plan metadata without credentials or provider reads."""
    from adhelm.cli import emit, guarded, root

    command = _PREFIX + " catalog"

    def work() -> None:
        if cursor < 0 or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_GA4_CREATE_PLAN_SELECTION", "Plan pagination is invalid.", exit_code=2
            )
        preferences = load_configuration(root(ctx)).output
        data = _closed_catalog(
            ga4_key_event_create_plan_catalog(
                Ledger(root(ctx)), cursor=cursor, limit=min(limit, preferences.max_rows)
            )
        )
        while not _fits(
            command,
            data,
            preferences.max_output_bytes,
            outcome=_HISTORY_OUTCOME,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            plans = data["plans"]
            if type(plans) is not list or len(plans) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "GA4 CREATE plan catalog metadata exceeds the saved output bound.",
                    next_action="Increase saved max_output_bytes to review this catalog.",
                )
            plans.pop()
            data["next_cursor"] = str(cursor + len(plans))
        emit(
            command,
            data,
            outcome=_HISTORY_OUTCOME,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded(command, work)


@app.command("show")
def show(ctx: typer.Context, record_digest: str) -> None:
    """Show one complete redacted historical plan; it does not authorize a CREATE."""
    from adhelm.cli import emit, guarded, root

    command = _PREFIX + " show"

    def work() -> None:
        directory = root(ctx)
        preferences = load_configuration(directory).output
        action, provenance = load_ga4_key_event_create_plan(Ledger(directory), record_digest)
        reviewed_at = _reviewed_at()
        record = ga4_key_event_create_plan_record(action)
        probe = preview_ga4_key_event_create_plan(
            record,
            opaque_key=b"\0" * 32,
            reviewed_at=reviewed_at,
        )
        data = _review_data(probe, provenance)
        if not _fits(command, data, preferences.max_output_bytes, outcome=_HISTORY_OUTCOME):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Complete GA4 CREATE plan review exceeds the saved output bound.",
                next_action=(
                    "Increase saved max_output_bytes to review every planning and "
                    "evidence limitation together."
                ),
            )
        review = preview_ga4_key_event_create_plan(
            record,
            opaque_key=export_key(),
            reviewed_at=reviewed_at,
        )
        emit(
            command,
            _review_data(review, provenance),
            outcome=_HISTORY_OUTCOME,
            maximum_bytes=preferences.max_output_bytes,
        )

    guarded(command, work)
