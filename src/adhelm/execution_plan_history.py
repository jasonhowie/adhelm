"""Pure envelope and predecessor checks for retained provider-neutral plans.

Both functions are called by :mod:`adhelm.ledger` under its ``changes`` stream
lock before an append and again on every physical scan.  Neither performs
nested ledger I/O, provider access, credential access, or clock reads.
"""

from __future__ import annotations

from typing import Any

from adhelm.errors import AdhelmError, ExitCode
from adhelm.execution_plan_records import (
    EXECUTION_PLAN_RECORD_KIND,
    revalidate_execution_plan_record,
)

_KIND = EXECUTION_PLAN_RECORD_KIND


def validate_execution_plan_envelope(
    kind: str,
    payload: dict[str, Any],
    workspace_profile_ref: str,
    provider: str | None,
    account_ref: str | None,
    source: str,
    run_id: str | None,
    observed_at: str | None,
    occurred_at: str | None,
) -> None:
    """Bind exactly one closed plan to non-occurrence local history."""

    if kind != _KIND:
        return
    record = revalidate_execution_plan_record(payload)
    if (
        provider != record.provider
        or account_ref != record.provenance.account_ref
        or workspace_profile_ref != record.provenance.workspace_profile_ref
        or source != "LOCAL_CLI"
        or run_id is not None
        or observed_at is not None
        or occurred_at is not None
    ):
        raise AdhelmError(
            "INVALID_EXECUTION_PLAN_PROVENANCE",
            "A retained execution plan must bind its local workspace, provider and "
            "account without claiming a provider occurrence, observation or execution run.",
            exit_code=ExitCode.INVALID_INPUT,
        )


def validate_execution_plan_history(records: list[dict[str, Any]]) -> None:
    """Verify each retained plan's predecessor at the prefix it was compiled at.

    A plan that supersedes another names its exact record digest.  The named
    predecessor must already be retained in this workspace, describe the same
    provider, operation and account, and not already have been superseded, so
    a supersession chain is linear and cannot be forked or claimed backwards.
    """

    retained: dict[str, dict[str, Any]] = {}
    superseded: set[str] = set()
    for envelope in records:
        if envelope["record_kind"] != _KIND:
            if "record_digest" in envelope:
                retained.setdefault(envelope["record_digest"], envelope)
            continue
        record = revalidate_execution_plan_record(envelope["payload"])
        predecessor_digest = record.supersedes_record_digest
        if predecessor_digest is not None:
            predecessor = retained.get(predecessor_digest)
            if (
                predecessor is None
                or predecessor["record_kind"] != _KIND
                or predecessor_digest in superseded
            ):
                raise _predecessor_conflict()
            earlier = revalidate_execution_plan_record(predecessor["payload"])
            if (
                earlier.provider != record.provider
                or earlier.operation is not record.operation
                or earlier.provenance.account_ref != record.provenance.account_ref
                or earlier.provenance.workspace_profile_ref
                != record.provenance.workspace_profile_ref
                or earlier.plan_digest == record.plan_digest
            ):
                raise _predecessor_conflict()
            superseded.add(predecessor_digest)
        if "record_digest" in envelope:
            retained[envelope["record_digest"]] = envelope


def _predecessor_conflict() -> AdhelmError:
    return AdhelmError(
        "EXECUTION_PLAN_PREDECESSOR_CONFLICT",
        "The retained execution plan does not supersede one earlier distinct plan for "
        "the same workspace, provider, operation and account.",
        exit_code=ExitCode.PRECONDITION,
        next_action=(
            "Select the current retained plan with plan execution catalog and compile a "
            "new plan from a fresh bounded read."
        ),
    )


__all__ = ["validate_execution_plan_envelope", "validate_execution_plan_history"]
