"""Verified local retention for sealed multi-provider strategy observations.

Only a fresh closed ``StrategyBrief`` may enter the preparation path.  The
result intentionally stores normalized child reviews rather than the strategy
source or native candidate specifications, so it remains historical evidence
instead of a plan-promotion input.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from adhelm.candidate_records import CandidateCompilationRecord, CompiledCandidate
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Money
from adhelm.saved_candidates import compile_candidate
from adhelm.strategy import (
    OpenAIStrategyCandidate,
    ProviderStrategyBrief,
    StrategyBrief,
    StrategyCandidate,
    compile_strategy,
)
from adhelm.strategy_records import StrategyCandidateCompilationRecord


def _candidate_maximum_spend(candidate: StrategyCandidate) -> Money:
    if isinstance(candidate, OpenAIStrategyCandidate):
        return candidate.spec.activation_boundary.maximum_spend
    return candidate.spec.maximum_spend


def _checked_utc_now(now: Callable[[], datetime]) -> datetime:
    current = now()
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise AdhelmError(
            "STRATEGY_CLOCK_INVALID",
            "Strategy retention requires a timezone-aware UTC clock.",
            exit_code=2,
        )
    return current.astimezone(UTC)


def _require_unexpired(expires_at: datetime, now: Callable[[], datetime]) -> None:
    if expires_at <= _checked_utc_now(now):
        raise AdhelmError(
            "STRATEGY_EXPIRED",
            "The strategy brief has expired.",
            exit_code=2,
            next_action="Prepare a new strategy brief with a future expiry and new digest.",
        )


def _normalized_child_record(
    source: ProviderStrategyBrief, candidate: StrategyCandidate
) -> CandidateCompilationRecord:
    """Recompile one closed source child into its non-replayable review record."""
    # Every source union variant has a reviewed native brief.  Passing through
    # the existing compiler record constructor avoids reconstructing a native
    # request or accepting an asserted candidate digest.
    normalized: CompiledCandidate = compile_candidate(source.brief)
    expected_spend = _candidate_maximum_spend(candidate)
    if (
        normalized.provider != candidate.provider
        or normalized.input_digest != candidate.spec.input_digest
        or normalized.spec_digest != candidate.spec.spec_digest
        or normalized.account_ref != candidate.spec.account_ref
        or normalized.maximum_spend != expected_spend
    ):
        raise AdhelmError(
            "STRATEGY_CHILD_BINDING_INVALID",
            "A normalized child review did not match the freshly compiled strategy.",
            exit_code=2,
        )
    return CandidateCompilationRecord(
        compilation=normalized,
        candidate_digest=normalized.candidate_digest,
    )


def compile_strategy_observation(
    brief: StrategyBrief, *, now: Callable[[], datetime]
) -> StrategyCandidateCompilationRecord:
    """Compile all children and create one closed, unpersisted observation."""
    compiled = compile_strategy(brief, now=now)
    child_records = tuple(
        _normalized_child_record(source, candidate)
        for source, candidate in zip(
            compiled.source_brief.provider_briefs, compiled.candidates, strict=True
        )
    )
    return StrategyCandidateCompilationRecord(
        strategy_brief_digest=compiled.strategy_brief_id.removeprefix("strategy:sha256:"),
        input_digest=compiled.input_digest,
        strategy_spec_digest=compiled.strategy_spec_digest,
        expires_at=compiled.expires_at,
        total_caps_by_currency=compiled.total_caps_by_currency,
        maximum_spend_by_currency=compiled.maximum_spend_by_currency,
        child_compilations=child_records,
        ordered_child_candidate_digests=tuple(child.candidate_digest for child in child_records),
        ordered_child_spec_digests=compiled.ordered_child_spec_digests,
    )


def retain_strategy(
    ledger: Ledger,
    brief: StrategyBrief,
    *,
    now: Callable[[], datetime],
    before_append: Callable[[StrategyCandidateCompilationRecord], None] | None = None,
) -> dict[str, Any]:
    """Append exactly one prepared strategy observation after a caller preflight.

    ``before_append`` lets the CLI size its key-free receipt before this module
    creates the canonical history directory or appends anything.  It receives a
    frozen closed record, never a source brief or native specification.
    """
    observation = compile_strategy_observation(brief, now=now)
    if before_append is not None:
        before_append(observation)
    # Compilation and a bounded output preflight can consume time.  A strategy
    # must still be current at the only durable side-effect boundary.
    _require_unexpired(observation.expires_at, now)
    try:
        return ledger.append(
            "changes",
            "STRATEGY_CANDIDATE_COMPILED",
            observation.model_dump(mode="json"),
            provider=None,
            account_ref=None,
            source="LOCAL_CLI",
            run_id=None,
            _before_durable_append=lambda: _require_unexpired(observation.expires_at, now),
        )
    except (AdhelmError, OSError) as exc:
        raise AdhelmError(
            "STRATEGY_RETENTION_UNVERIFIED",
            "Local strategy retention could not be verified; no remote write occurred.",
            next_action="Run history verify and strategy catalog before retaining again.",
        ) from exc


def _selected_strategy_record(
    ledger: Ledger, record_digest: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(record_digest, str) or re.fullmatch(r"[0-9a-f]{64}", record_digest) is None:
        raise AdhelmError(
            "INVALID_STRATEGY_SELECTION",
            "A retained strategy record digest is required.",
            exit_code=2,
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in checkpoint["records_by_stream"]["changes"]
        if record["record_kind"] == "STRATEGY_CANDIDATE_COMPILED"
        and record["record_digest"] == record_digest
    ]
    if len(records) != 1:
        raise AdhelmError(
            "STRATEGY_NOT_RETAINED", "Selected strategy is absent from verified local history."
        )
    return records[0], checkpoint


def load_strategy(
    ledger: Ledger, record_digest: str
) -> tuple[StrategyCandidateCompilationRecord, dict[str, Any]]:
    """Load one verified sealed observation without treating it as current authority."""
    record, checkpoint = _selected_strategy_record(ledger, record_digest)
    observation = StrategyCandidateCompilationRecord.model_validate_json(
        canonical_bytes(record["payload"])
    )
    return observation, {
        "record_digest": record_digest,
        "strategy_brief_digest": observation.strategy_brief_digest,
        "strategy_spec_digest": observation.strategy_spec_digest,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "recorded_at": record["recorded_at"],
    }


def strategy_catalog(ledger: Ledger, *, cursor: int = 0, limit: int = 50) -> dict[str, Any]:
    """Return a bounded, key-free catalog from one verified checkpoint."""
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise AdhelmError("INVALID_STRATEGY_SELECTION", "Invalid strategy pagination.", exit_code=2)
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in reversed(checkpoint["records_by_stream"]["changes"])
        if record["record_kind"] == "STRATEGY_CANDIDATE_COMPILED"
    ]
    if cursor > len(records):
        raise AdhelmError(
            "INVALID_STRATEGY_SELECTION", "Cursor exceeds retained strategy count.", exit_code=2
        )
    strategies: list[dict[str, Any]] = []
    for record in records[cursor : cursor + limit]:
        observation = StrategyCandidateCompilationRecord.model_validate_json(
            canonical_bytes(record["payload"])
        )
        strategies.append(
            {
                "record_digest": record["record_digest"],
                "strategy_brief_digest": observation.strategy_brief_digest,
                "strategy_spec_digest": observation.strategy_spec_digest,
                "selected_provider_count": len(observation.child_compilations),
                "total_caps_by_currency": [
                    cap.model_dump(mode="json") for cap in observation.total_caps_by_currency
                ],
                "maximum_spend_by_currency": [
                    spend.model_dump(mode="json") for spend in observation.maximum_spend_by_currency
                ],
                "expires_at": observation.expires_at.isoformat(),
                "execution_status": observation.execution_status,
                "recorded_at": record["recorded_at"],
            }
        )
    return {
        "strategies": strategies,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "total_count": len(records),
        "next_cursor": str(cursor + limit) if cursor + limit < len(records) else None,
    }
