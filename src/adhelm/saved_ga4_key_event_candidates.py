"""Verified local retention for non-executable GA4 key-event candidates.

This module has no provider, credential, or execution surface.  It appends a
closed candidate through ``Ledger`` and selects historical records only from a
verified checkpoint.  The catalog deliberately exposes counts and fixed review
states, never event names, parameter names, native IDs, or a native request.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from adhelm.conversion_contracts import ConversionContract
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_key_event_candidates import (
    Ga4KeyEventCandidateInput,
    Ga4KeyEventCandidateRecord,
    compile_ga4_key_event_candidate,
    revalidate_ga4_key_event_candidate_record,
)
from adhelm.ledger import Ledger

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def retain_ga4_key_event_candidate(
    ledger: Ledger,
    request: Ga4KeyEventCandidateInput,
    *,
    retained_contract: ConversionContract,
    retained_contract_record_digest: str,
    before_append: Callable[[Ga4KeyEventCandidateRecord], None] | None = None,
) -> dict[str, Any]:
    """Compile and append one closed local candidate after an optional preflight.

    The ledger validates the exact envelope plus the latest preceding
    conversion-definition record under its changes-stream lock.  A caller must
    not treat successful retention as current provider identity, duplicate
    inventory, source-event mapping, consent enforcement, or authorization.
    """
    record = compile_ga4_key_event_candidate(
        request,
        retained_contract=retained_contract,
        retained_contract_record_digest=retained_contract_record_digest,
    )
    if before_append is not None:
        before_append(record)
    return ledger.append(
        "changes",
        "GA4_KEY_EVENT_CANDIDATE_COMPILED",
        record.model_dump(mode="json"),
        provider="ga4_admin",
        account_ref=record.compilation.compiler_candidate.identity.property_ref,
        source="LOCAL_CLI",
        run_id=None,
        occurred_at=None,
        observed_at=None,
    )


def _selected_record(ledger: Ledger, record_digest: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if type(record_digest) is not str or _DIGEST.fullmatch(record_digest) is None:
        raise AdhelmError(
            "INVALID_GA4_KEY_EVENT_CANDIDATE_SELECTION",
            "A retained GA4 key-event candidate record digest is required.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in checkpoint["records_by_stream"]["changes"]
        if record["record_kind"] == "GA4_KEY_EVENT_CANDIDATE_COMPILED"
        and record["record_digest"] == record_digest
    ]
    if len(records) != 1:
        raise AdhelmError(
            "GA4_KEY_EVENT_CANDIDATE_NOT_RETAINED",
            "Selected GA4 key-event candidate is absent from verified local history.",
            exit_code=ExitCode.PRECONDITION,
        )
    return records[0], checkpoint


def load_ga4_key_event_candidate(
    ledger: Ledger, record_digest: str
) -> tuple[Ga4KeyEventCandidateRecord, dict[str, Any]]:
    """Load one closed historical record without promoting it to a fresh plan."""
    stored, checkpoint = _selected_record(ledger, record_digest)
    record = revalidate_ga4_key_event_candidate_record(stored["payload"])
    compilation = record.compilation
    binding = compilation.conversion_binding
    return record, {
        "record_digest": record_digest,
        "candidate_digest": record.candidate_digest,
        "compiler_candidate_digest": compilation.compiler_candidate_digest,
        "conversion_contract_record_digest": binding.conversion_contract_record_digest,
        "conversion_contract_content_digest": binding.content_digest,
        "contract_version": binding.contract_version,
        "contract_effective_from": binding.contract_effective_from.isoformat(),
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "recorded_at": stored["recorded_at"],
        "execution_status": compilation.execution_status,
        "semantic_accuracy": compilation.semantic_accuracy,
        "source_event_mapping": compilation.source_event_mapping,
        "effective_time_status": compilation.effective_time_status,
    }


def ga4_key_event_candidate_catalog(
    ledger: Ledger, *, cursor: int = 0, limit: int = 50
) -> dict[str, Any]:
    """Return bounded redaction-safe candidate facts from one verified checkpoint."""
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise AdhelmError(
            "INVALID_GA4_KEY_EVENT_CANDIDATE_SELECTION",
            "Invalid GA4 key-event candidate pagination.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in reversed(checkpoint["records_by_stream"]["changes"])
        if record["record_kind"] == "GA4_KEY_EVENT_CANDIDATE_COMPILED"
    ]
    if cursor > len(records):
        raise AdhelmError(
            "INVALID_GA4_KEY_EVENT_CANDIDATE_SELECTION",
            "Cursor exceeds retained GA4 key-event candidate count.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    candidates: list[dict[str, Any]] = []
    for stored in records[cursor : cursor + limit]:
        record = revalidate_ga4_key_event_candidate_record(stored["payload"])
        compilation = record.compilation
        binding = compilation.conversion_binding
        candidates.append(
            {
                "record_digest": stored["record_digest"],
                "candidate_digest": record.candidate_digest,
                "conversion_contract_record_digest": binding.conversion_contract_record_digest,
                "contract_version": binding.contract_version,
                "contract_effective_from": binding.contract_effective_from.isoformat(),
                "bound_boolean_field_count": len(binding.bound_boolean_fields),
                "compiler_evidence": compilation.compiler_evidence,
                "execution_status": compilation.execution_status,
                "semantic_accuracy": compilation.semantic_accuracy,
                "source_event_mapping": compilation.source_event_mapping,
                "effective_time_status": compilation.effective_time_status,
                "unresolved_planning_gate_count": len(
                    compilation.compiler_candidate.planning_boundary.model_dump(mode="json")
                )
                - 1,
                "recorded_at": stored["recorded_at"],
            }
        )
    return {
        "candidates": candidates,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "total_count": len(records),
        "next_cursor": str(cursor + limit) if cursor + limit < len(records) else None,
    }


__all__ = [
    "ga4_key_event_candidate_catalog",
    "load_ga4_key_event_candidate",
    "retain_ga4_key_event_candidate",
]
