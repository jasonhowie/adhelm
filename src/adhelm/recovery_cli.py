"""Bounded command surface for the one authorized exit from the write barrier.

`docs/implementation/kernel-generalization.md` added
``SafetyKernel.record_attempt_recovery`` and deliberately left it without a
command: an attempt this kernel durably started and never finished journaling
refuses every later guarded write on every provider and account, and until now
the only way out was a private library integration.  These two leaves are that
surface.

Neither leaf settles, attributes, retries, or compensates anything.  ``recover
catalog`` reads local history only.  ``recover attempt`` obtains one
``ATTEMPT_RECOVERY`` approval for exactly one durable ``APPLY_STARTED``, takes a
fresh identity check and read through the configured adapter, and records what
it saw with an ``UNKNOWN`` actor.  Neither accepts a receipt, challenge, trust
pin, key, grant, adapter, transport, credential, provider request, or artifact
option: the record digest is the entire input.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from pydantic import Field, field_validator

from adhelm._installed_native_approval_bridge import (
    _require_compiled_installed_approval_bridge_ready,
)
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.execution_cli import _checked_record_digest
from adhelm.generic_execution_workflow import (
    AttemptRecoveryOutcome,
    AttemptRecoveryRequest,
    record_retained_attempt_recovery_with_installed_approval,
    unresolved_attempt_catalog,
)
from adhelm.ledger import Ledger
from adhelm.measurement_cli import _fits
from adhelm.models import Provider, StrictModel
from adhelm.release_policy import remote_mutation_leaf
from adhelm.safety import OperationKind
from adhelm.strict_json import validate_json_value

app = typer.Typer(no_args_is_help=True)

_ATTEMPT_COMMAND = "recover attempt"
_CATALOG_COMMAND = "recover catalog"
#: Recording an observation is this command's whole purpose, so its result is
#: not an execution result and does not carry the execute leaves'
#: status-to-exit mapping.  The outcome string and the closed fields below --
#: not the exit code -- are where the envelope states that the attempt itself
#: stays unattributed, unsettled, and unsafe to retry.
_ATTEMPT_OUTCOME = "ATTEMPT_RECOVERY_OBSERVED_NO_ATTRIBUTION"
_CATALOG_OUTCOME = "UNRESOLVED_ATTEMPT_HISTORY_NO_EXECUTION_AUTHORITY"
_MAXIMUM_CATALOG_ROWS = 500

_Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
#: The closed installed sets, so the reservation below is the real widest
#: envelope rather than an invented one that would demand a larger saved output
#: bound than any observation can actually need.
#: Every ``OperationKind`` value is at most sixteen characters, so the widest
#: reservation below is the widest envelope an observation can produce.
_OperationName = Annotated[str, Field(min_length=1, max_length=16)]
_LONGEST_PROVIDER: Provider = "linkedin_ads"
_LONGEST_OPERATION = max(sorted(item.value for item in OperationKind), key=len)
_ObservedRelation = Literal["AT_BASELINE", "AT_TARGET", "OTHER"]
_Disposition = Literal["RECOVERED_AT_BASELINE", "RECOVERED_AT_TARGET", "RECOVERED_DIVERGED"]
_AttemptNextAction = Literal[
    "Compile a new plan from a fresh read before any further mutation; inspect history status."
]


class _AttemptRecoveryResult(StrictModel):
    """The exact facts the durable observation record itself contains.

    Every field is copied from the ``ATTEMPT_RECOVERY_OBSERVED`` record the
    kernel appended, or is a fixed statement about what that record does not
    say.  It carries no authorization identifier, receipt or key material,
    grant, approval-bridge detail, credential fact, provider payload, observed
    state fingerprint, or adapter diagnostic.
    """

    schema_version: Literal[1] = 1
    record_digest: _Digest
    started_record_digest: _Digest
    plan_digest: _Digest
    subplan_digest: _Digest
    provider: Provider
    operation: _OperationName
    observed_relation: _ObservedRelation
    disposition: _Disposition
    actor_ref: Literal["UNKNOWN"]
    attempt_effect_status: Literal["AMBIGUOUS_REMOTE_STATE"]
    attribution: Literal["NO_ACTOR_ATTRIBUTED"] = "NO_ACTOR_ATTRIBUTED"
    provider_change_applied: Literal[False] = False
    attempt_settled: Literal[False] = False
    retry_authorized: Literal[False] = False
    history_command: Literal["history status"] = "history status"
    next_action: _AttemptNextAction = (
        "Compile a new plan from a fresh read before any further mutation; inspect history status."
    )

    @field_validator(
        "provider_change_applied", "attempt_settled", "retry_authorized", mode="before"
    )
    @classmethod
    def false_only(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("a recorded observation changes and settles nothing")
        return value


class _UnresolvedAttemptRow(StrictModel):
    """One unresolved durable start, at subplan or journaled-child level."""

    started_record_digest: _Digest
    level: Literal["SUBPLAN", "CHILD"]
    child_index: int | None = Field(default=None, ge=1)
    provider: Provider
    operation: _OperationName
    plan_digest: _Digest
    subplan_digest: _Digest
    recorded_at: datetime
    refusal_code: Literal["UNRESOLVED_APPLY", "UNRESOLVED_CHILD_APPLY"]
    #: Whether ``recover attempt`` can act on this exact row today.  It needs
    #: the provider-neutral retained plan, so an attempt started from one of
    #: the three provider-specific retained families reports ``false``.  It
    #: never implies that recovering the row makes a retry safe.
    recoverable: bool

    @field_validator("recorded_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("catalog timestamp must be aware")
        return value.astimezone(UTC)


class _UnresolvedAttemptCatalog(StrictModel):
    attempts: list[_UnresolvedAttemptRow] = Field(max_length=_MAXIMUM_CATALOG_ROWS)
    total_count: int = Field(ge=0)
    next_cursor: str | None
    history_verification: Literal["HASH_CHAIN_VERIFIED_LOCAL_SCAN"]
    blocking_effect: Literal["BLOCKS_EVERY_LATER_GUARDED_WRITE"]
    attribution: Literal["NO_ACTOR_ATTRIBUTED"]
    recovery_command: Literal["recover attempt"]
    recovery_authorizes_retry: Literal[False]

    @field_validator("next_cursor", mode="before")
    @classmethod
    def cursor(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or not value.isascii() or not value.isdecimal():
            raise ValueError("invalid cursor")
        return value

    @field_validator("recovery_authorizes_retry", mode="before")
    @classmethod
    def false_only(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("a recovery never authorizes a retry")
        return value


def _invalid_selection(detail: str) -> AdhelmError:
    return AdhelmError(
        "INVALID_ATTEMPT_RECOVERY_SELECTION", detail, exit_code=ExitCode.INVALID_INPUT
    )


def _attempt_result_probe() -> dict[str, object]:
    """Reserve the widest observation envelope before any approval work.

    The longest permitted provider name, operation name, and disposition are
    reserved together, so a recovery whose record would not fit the saved
    output bound is refused *before* the approval bridge is asked for anything.
    A recovery grant is one-use: burning one on an unpresentable result would
    leave the barrier in place with no second approval available.
    """

    surface = _AttemptRecoveryResult(
        record_digest="0" * 64,
        started_record_digest="0" * 64,
        plan_digest="0" * 64,
        subplan_digest="0" * 64,
        provider=_LONGEST_PROVIDER,
        operation=_LONGEST_OPERATION,
        observed_relation="AT_BASELINE",
        disposition="RECOVERED_AT_BASELINE",
        actor_ref="UNKNOWN",
        attempt_effect_status="AMBIGUOUS_REMOTE_STATE",
    )
    return surface.model_dump(mode="json", warnings="error")


def _require_attempt_result_fit(maximum_bytes: int) -> None:
    if not _fits(
        _ATTEMPT_COMMAND, _attempt_result_probe(), maximum_bytes, outcome=_ATTEMPT_OUTCOME
    ):
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            "The complete recovery observation exceeds the saved output bound.",
            next_action=(
                "Increase saved max_output_bytes; no approval bridge, credential, native review, "
                "or provider read was started."
            ),
        )


def _unpresentable_observation() -> AdhelmError:
    """Report a possibly recorded recovery honestly rather than repeating it."""

    return AdhelmError(
        "ATTEMPT_RECOVERY_RESULT_UNAVAILABLE",
        "The recovery may be recorded, but its observation is not safely presentable.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect history verify and history status; do not repeat the recovery.",
    )


def _attempt_result_data(value: object) -> dict[str, object]:
    """Detach the workflow's outcome into the fixed redacted command output."""

    try:
        if type(value) is not AttemptRecoveryOutcome:
            raise ValueError("outcome")
        return _AttemptRecoveryResult(
            record_digest=value.record_digest,
            started_record_digest=value.started_record_digest,
            plan_digest=value.plan_digest,
            subplan_digest=value.subplan_digest,
            provider=value.provider,  # type: ignore[arg-type]
            operation=value.operation,
            observed_relation=value.observed_relation,  # type: ignore[arg-type]
            disposition=value.disposition,  # type: ignore[arg-type]
            actor_ref=value.actor_ref,  # type: ignore[arg-type]
            attempt_effect_status=value.attempt_effect_status,  # type: ignore[arg-type]
        ).model_dump(mode="json", warnings="error")
    except Exception:
        raise _unpresentable_observation() from None


def _completed_recovery(directory: Path, selected: str) -> AttemptRecoveryOutcome:
    try:
        return record_retained_attempt_recovery_with_installed_approval(
            directory, AttemptRecoveryRequest(started_record_digest=selected)
        )
    except AdhelmError:
        raise
    except Exception:
        raise _unpresentable_observation() from None


def _closed_catalog(value: object) -> dict[str, Any]:
    try:
        if type(value) is not dict:
            raise ValueError("catalog")
        return validate_json_value(_UnresolvedAttemptCatalog, value).model_dump(
            mode="json", warnings="error"
        )
    except Exception:
        raise _invalid_selection("The unresolved attempt catalog is invalid.") from None


@app.command("attempt")
@remote_mutation_leaf("recover attempt")
def recover_attempt(ctx: typer.Context, started_record_digest: str) -> None:
    """Record the current state of one unresolved attempt under its own approval.

    The argument is the durable ``APPLY_STARTED`` record digest that ``recover
    catalog`` lists.  This sends no provider mutation, attributes nothing to
    anyone, settles nothing, and never makes a retry safe: it records what the
    configured adapter reads now and releases the global write barrier.
    """

    from adhelm.cli import emit, guarded, root

    def work() -> None:
        selected = _checked_record_digest(started_record_digest)
        directory = root(ctx)
        configuration = load_configuration(directory)
        _require_attempt_result_fit(configuration.output.max_output_bytes)
        _require_compiled_installed_approval_bridge_ready()
        outcome = _completed_recovery(directory, selected)
        emit(
            _ATTEMPT_COMMAND,
            _attempt_result_data(outcome),
            outcome=_ATTEMPT_OUTCOME,
            maximum_bytes=configuration.output.max_output_bytes,
        )

    guarded(_ATTEMPT_COMMAND, work)


@app.command("catalog")
def recover_catalog(ctx: typer.Context, cursor: int = 0, limit: int = 50) -> None:
    """List the unresolved durable attempts that block every later guarded write."""

    from adhelm.cli import emit, guarded, root

    def work() -> None:
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise _invalid_selection("Unresolved attempt pagination is invalid.")
        directory = root(ctx)
        preferences = load_configuration(directory).output
        data = _closed_catalog(
            unresolved_attempt_catalog(
                Ledger(directory), cursor=cursor, limit=min(limit, preferences.max_rows)
            )
        )
        while not _fits(
            _CATALOG_COMMAND,
            data,
            preferences.max_output_bytes,
            outcome=_CATALOG_OUTCOME,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            attempts = data["attempts"]
            if type(attempts) is not list or len(attempts) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "The unresolved attempt catalog exceeds the saved output bound.",
                    exit_code=ExitCode.PRECONDITION,
                    next_action="Increase saved max_output_bytes to review these attempts.",
                )
            attempts.pop()
            data["next_cursor"] = str(cursor + len(attempts))
        emit(
            _CATALOG_COMMAND,
            data,
            outcome=_CATALOG_OUTCOME,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded(_CATALOG_COMMAND, work)


__all__ = ["app"]
