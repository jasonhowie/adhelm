"""Retain and select exact execution plans from verified local history.

Selection reads only a :meth:`adhelm.ledger.Ledger.checkpoint` vector.  The
checkpoint scans and revalidates every record under ordered stream locks, so an
unverified or truncated tail raises before any plan is returned; there is no
path here that reads the rebuildable index or an unverified record.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from adhelm.errors import AdhelmError, ExitCode
from adhelm.execution_plan_records import (
    EXECUTION_PLAN_RECORD_KIND,
    ExecutionPlanRecord,
    revalidate_execution_plan_record,
)
from adhelm.ledger import Ledger
from adhelm.models import PROVIDERS
from adhelm.safety import CompositePlan, OperationKind

_KIND = EXECUTION_PLAN_RECORD_KIND
_DIGEST = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_ACCOUNT_REF = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$", re.ASCII)
_MAXIMUM_LIMIT = 500


def _invalid_selection(detail: str) -> AdhelmError:
    return AdhelmError(
        "INVALID_EXECUTION_PLAN_SELECTION",
        detail,
        exit_code=ExitCode.INVALID_INPUT,
    )


def retain_execution_plan_record(
    ledger: Ledger,
    record: ExecutionPlanRecord,
    *,
    before_durable_append: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Retain one local plan without asserting a current remote condition.

    Configured callers acquire configuration then credential locks before this
    call.  Their final callback uses only already-held state, performs no
    ledger I/O, and runs immediately before the durable payload append under
    the ledger stream lock.  Historical retention grants no execution
    authority, and this helper never retries a partial local write.
    """

    checked = revalidate_execution_plan_record(record)
    if ledger.profile_ref != checked.provenance.workspace_profile_ref:
        raise AdhelmError(
            "INVALID_EXECUTION_PLAN_PROVENANCE",
            "The execution plan belongs to a different local workspace.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return ledger.append(
        "changes",
        _KIND,
        checked.model_dump(mode="json", warnings="error"),
        provider=checked.provider,
        account_ref=checked.provenance.account_ref,
        source="LOCAL_CLI",
        _before_durable_append=before_durable_append,
    )


def load_execution_plan_record(
    ledger: Ledger, record_digest: str
) -> tuple[ExecutionPlanRecord, dict[str, Any]]:
    """Load one exact retained record with its verified checkpoint facts."""

    if type(record_digest) is not str or _DIGEST.fullmatch(record_digest) is None:
        raise _invalid_selection("An exact retained execution plan record digest is required.")
    checkpoint = ledger.checkpoint()
    selected = [
        item
        for item in checkpoint["records_by_stream"]["changes"]
        if item["record_kind"] == _KIND and item["record_digest"] == record_digest
    ]
    if len(selected) != 1:
        raise AdhelmError(
            "EXECUTION_PLAN_NOT_RETAINED",
            "The execution plan is not retained in verified local history.",
            next_action="List retained plans with plan execution catalog.",
        )
    stored = selected[0]
    record = revalidate_execution_plan_record(stored["payload"])
    return record, {
        "record_digest": stored["record_digest"],
        "plan_digest": record.plan_digest,
        "subplan_digests": list(record.subplan_digests),
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "recorded_at": stored["recorded_at"],
        "provider": record.provider,
        "operation": record.operation.value,
        "supersedes_record_digest": record.supersedes_record_digest,
        "historical_evidence_status": record.historical_evidence_status,
        "execution_authority": False,
        "requires_current_kernel_checks": True,
    }


def load_execution_plan(ledger: Ledger, record_digest: str) -> tuple[CompositePlan, dict[str, Any]]:
    """Load one exact retained plan with its verified checkpoint facts."""

    record, provenance = load_execution_plan_record(ledger, record_digest)
    return record.plan, provenance


def execution_plan_catalog(
    ledger: Ledger,
    *,
    cursor: int = 0,
    limit: int = 50,
    provider: str | None = None,
    operation: str | None = None,
    account_ref: str | None = None,
) -> dict[str, Any]:
    """Return newest-first bounded historical plan summaries only.

    Filters narrow the retained set before pagination, so ``total_count`` and
    ``next_cursor`` always describe the filtered selection rather than the
    whole stream.
    """

    if (
        type(cursor) is not int
        or type(cursor) is bool
        or cursor < 0
        or type(limit) is not int
        or type(limit) is bool
        or not 1 <= limit <= _MAXIMUM_LIMIT
    ):
        raise _invalid_selection("Execution plan pagination is invalid.")
    if provider is not None and (type(provider) is not str or provider not in PROVIDERS):
        raise _invalid_selection("Execution plan provider filter is invalid.")
    if operation is not None and (
        type(operation) is not str or operation not in {item.value for item in OperationKind}
    ):
        raise _invalid_selection("Execution plan operation filter is invalid.")
    if account_ref is not None and (
        type(account_ref) is not str or _ACCOUNT_REF.fullmatch(account_ref) is None
    ):
        raise _invalid_selection("Execution plan account filter is invalid.")
    checkpoint = ledger.checkpoint()
    rows: list[dict[str, Any]] = []
    matched: list[tuple[dict[str, Any], ExecutionPlanRecord]] = []
    # The reverse of `supersedes_record_digest`, built once from the same
    # verified checkpoint.  A row that only names its predecessor cannot tell a
    # reader whether it is still the current plan; the supersession chain is
    # linear, so each predecessor has at most one successor.
    successors: dict[str, str] = {}
    for stored in checkpoint["records_by_stream"]["changes"]:
        if stored["record_kind"] != _KIND:
            continue
        predecessor = stored["payload"].get("supersedes_record_digest")
        if type(predecessor) is str:
            successors[predecessor] = str(stored["record_digest"])
    for stored in reversed(checkpoint["records_by_stream"]["changes"]):
        if stored["record_kind"] != _KIND:
            continue
        record = revalidate_execution_plan_record(stored["payload"])
        if (
            (provider is not None and record.provider != provider)
            or (operation is not None and record.operation.value != operation)
            or (account_ref is not None and record.provenance.account_ref != account_ref)
        ):
            continue
        matched.append((stored, record))
    if cursor > len(matched):
        raise _invalid_selection("Cursor exceeds the retained execution plan count.")
    for stored, record in matched[cursor : cursor + limit]:
        rows.append(
            {
                "record_digest": stored["record_digest"],
                "plan_digest": record.plan_digest,
                "provider": record.provider,
                "operation": record.operation.value,
                "recorded_at": stored["recorded_at"],
                "expires_at": record.expires_at.isoformat().replace("+00:00", "Z"),
                "expected_child_egress_count": record.expected_child_egress_count,
                "capability_status": record.provenance.capability_status,
                "spend_bounds_by_currency": [
                    bound.model_dump(mode="json") for bound in record.spend_bounds_by_currency
                ],
                "supersedes_record_digest": record.supersedes_record_digest,
                "superseded_by": successors.get(stored["record_digest"]),
                "execution_authority": False,
            }
        )
    return {
        "plans": rows,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "total_count": len(matched),
        "next_cursor": str(cursor + limit) if cursor + limit < len(matched) else None,
        "historical_evidence_status": "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
    }


__all__ = [
    "execution_plan_catalog",
    "load_execution_plan",
    "load_execution_plan_record",
    "retain_execution_plan_record",
]
