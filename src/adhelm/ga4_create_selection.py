"""Select and freshly recompile a GA4 CREATE candidate from one verified checkpoint.

This is a local planning prerequisite. It reads no configuration or credential,
performs no provider I/O, and grants no execution authority. Source event prose
must be supplied again; the retained structural candidate cannot replace it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from adhelm.canonical import canonical_bytes
from adhelm.conversion_contracts import ConversionContract
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_key_event_candidates import (
    Ga4KeyEventCandidateInput,
    Ga4KeyEventCandidateRecord,
    _revalidate_candidate_input,
    _revalidate_contract,
    compile_ga4_key_event_candidate,
    revalidate_ga4_key_event_candidate_record,
)
from adhelm.ledger import Ledger

_DIGEST = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True, repr=False)
class Ga4KeyEventCreateSelection:
    """Detached local selection; never a current provider observation or grant.

    These normalized inputs contain the local native request and must go
    through the normal redacted sharing boundary before an agent sees them.
    The full fresh source EventContract is intentionally not retained here.
    """

    candidate: Ga4KeyEventCandidateRecord
    contract: ConversionContract
    candidate_record_digest: str
    contract_record_digest: str
    checkpoint_digest: str
    evaluated_at: datetime


def select_ga4_key_event_create_candidate(
    ledger: Ledger,
    candidate_record_digest: str,
    *,
    fresh: Ga4KeyEventCandidateInput,
    as_of: datetime,
) -> Ga4KeyEventCreateSelection:
    """Require one retained candidate, the latest contract, and exact recompilation.

    ``as_of`` is an explicit evaluation instant. The eventual configured plan
    workflow must obtain it from its own clock, and must recheck history before
    retention and execution. A checkpoint is only one historical high-watermark;
    this helper does not hold its locks after selection or reserve the contract.
    A newer future-effective declaration also blocks an older candidate: this
    helper cannot infer scheduling or silently choose an earlier definition.
    """

    if (
        type(candidate_record_digest) is not str
        or _DIGEST.fullmatch(candidate_record_digest) is None
        or type(fresh) is not Ga4KeyEventCandidateInput
    ):
        raise _invalid_selection()
    try:
        if type(as_of) is not datetime or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("evaluation instant")
        evaluated_at = as_of.astimezone(UTC)
        checked_fresh = _revalidate_candidate_input(fresh)
    except Exception:
        raise _invalid_selection() from None

    # One checkpoint supplies both references. Calling separate historical
    # loaders could select the candidate and its contract at different tips.
    checkpoint = ledger.checkpoint()
    records = checkpoint["records_by_stream"]["changes"]
    candidates = [
        item
        for item in records
        if item["record_kind"] == "GA4_KEY_EVENT_CANDIDATE_COMPILED"
        and item["record_digest"] == candidate_record_digest
    ]
    if len(candidates) != 1:
        raise AdhelmError(
            "GA4_KEY_EVENT_CANDIDATE_NOT_RETAINED",
            "Select a GA4 key-event candidate from verified local history.",
            exit_code=ExitCode.PRECONDITION,
        )
    candidate = revalidate_ga4_key_event_candidate_record(candidates[0]["payload"])
    binding = candidate.compilation.conversion_binding
    latest = next(
        (
            item
            for item in reversed(records)
            if item["record_kind"] == "CONVERSION_CONTRACT"
            and item["payload"]["definition"]["contract_id"] == binding.contract_id
        ),
        None,
    )
    if latest is None or latest["record_digest"] != binding.conversion_contract_record_digest:
        raise AdhelmError(
            "GA4_CREATE_CONTRACT_SUPERSEDED",
            "The selected GA4 candidate does not use the latest retained conversion definition.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Compile and retain a new candidate against the latest definition.",
        )
    contract = _revalidate_contract(latest["payload"])
    if contract.definition.effective_from > evaluated_at:
        raise AdhelmError(
            "GA4_CREATE_CONTRACT_NOT_EFFECTIVE",
            "The selected conversion definition is not yet effective at the evaluation instant.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Evaluate a new plan after the declared effective time.",
        )
    try:
        rebuilt = compile_ga4_key_event_candidate(
            checked_fresh,
            retained_contract=contract,
            retained_contract_record_digest=latest["record_digest"],
        )
        if canonical_bytes(rebuilt) != canonical_bytes(candidate):
            raise ValueError("fresh input differs")
    except Exception:
        raise AdhelmError(
            "GA4_CREATE_FRESH_INPUT_MISMATCH",
            "Fresh GA4 input does not reproduce the selected retained candidate and definition.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Supply the full source event contract or retain its new compilation.",
        ) from None
    return Ga4KeyEventCreateSelection(
        candidate=rebuilt,
        contract=contract,
        candidate_record_digest=candidate_record_digest,
        contract_record_digest=latest["record_digest"],
        checkpoint_digest=checkpoint["checkpoint_digest"],
        evaluated_at=evaluated_at,
    )


def _invalid_selection() -> AdhelmError:
    return AdhelmError(
        "INVALID_GA4_CREATE_SELECTION",
        "A retained candidate digest, complete fresh input and aware evaluation instant "
        "are required.",
        exit_code=ExitCode.INVALID_INPUT,
    )
