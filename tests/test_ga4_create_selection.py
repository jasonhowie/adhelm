from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_saved_ga4_key_event_candidates import START, _definition, _request

from adhelm.canonical import canonical_bytes
from adhelm.conversion_contracts import ConversionContract
from adhelm.errors import AdhelmError
from adhelm.ga4_create_selection import select_ga4_key_event_create_candidate
from adhelm.ga4_key_event_candidates import Ga4KeyEventCandidateInput
from adhelm.ledger import Ledger
from adhelm.saved_conversion_contracts import retain_conversion_contract
from adhelm.saved_ga4_key_event_candidates import retain_ga4_key_event_candidate


def _history(tmp_path: Path):
    ledger = Ledger(tmp_path)
    definition = retain_conversion_contract(ledger, _definition())
    contract = ConversionContract.model_validate_json(canonical_bytes(definition["payload"]))
    fresh = _request(definition["record_digest"])
    candidate = retain_ga4_key_event_candidate(
        ledger,
        fresh,
        retained_contract=contract,
        retained_contract_record_digest=definition["record_digest"],
    )
    return ledger, definition, contract, fresh, candidate


def test_selection_recompiles_from_one_checkpoint_without_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, definition, contract, fresh, candidate = _history(tmp_path)
    checkpoint = ledger.checkpoint()
    calls = 0

    def checked_once():
        nonlocal calls
        calls += 1
        return checkpoint

    monkeypatch.setattr(ledger, "checkpoint", checked_once)
    result = select_ga4_key_event_create_candidate(
        ledger, candidate["record_digest"], fresh=fresh, as_of=START
    )
    assert calls == 1
    assert result.contract == contract
    assert result.contract_record_digest == definition["record_digest"]
    assert result.candidate.model_dump(mode="json") == candidate["payload"]
    assert result.checkpoint_digest == checkpoint["checkpoint_digest"]
    assert Ledger.checkpoint(ledger)["checkpoint_digest"] == checkpoint["checkpoint_digest"]
    assert not hasattr(result, "fresh")
    assert "qualified_request" not in repr(result)


@pytest.mark.parametrize("future", [False, True])
def test_newer_definition_blocks_old_candidate_even_when_future_effective(
    tmp_path: Path, future: bool
) -> None:
    ledger, _, contract, fresh, candidate = _history(tmp_path)
    retain_conversion_contract(
        ledger,
        _definition(
            version=2,
            supersedes=contract.content_digest,
            effective_from=START + timedelta(days=2 if future else 1),
        ),
    )
    with pytest.raises(AdhelmError) as error:
        select_ga4_key_event_create_candidate(
            ledger,
            candidate["record_digest"],
            fresh=fresh,
            as_of=START + timedelta(days=1),
        )
    assert error.value.code == "GA4_CREATE_CONTRACT_SUPERSEDED"


def test_effective_boundary_is_inclusive_and_normalizes_timezone(tmp_path: Path) -> None:
    ledger, _, _, fresh, candidate = _history(tmp_path)
    with pytest.raises(AdhelmError) as error:
        select_ga4_key_event_create_candidate(
            ledger,
            candidate["record_digest"],
            fresh=fresh,
            as_of=START - timedelta(microseconds=1),
        )
    assert error.value.code == "GA4_CREATE_CONTRACT_NOT_EFFECTIVE"
    result = select_ga4_key_event_create_candidate(
        ledger,
        candidate["record_digest"],
        fresh=fresh,
        as_of=START.astimezone(timezone(timedelta(hours=-5))),
    )
    assert result.evaluated_at == START
    assert result.evaluated_at.tzinfo is UTC


@pytest.mark.parametrize("change", ["event_name", "prose", "stream", "contract", "destination"])
def test_changed_fresh_input_cannot_reuse_retained_candidate(tmp_path: Path, change: str) -> None:
    ledger, _, _, fresh, candidate = _history(tmp_path)
    data = fresh.model_dump(mode="json")
    if change == "event_name":
        data["key_event"]["event_contract"]["event_name"] = "qualified_action"
    elif change == "prose":
        data["key_event"]["event_contract"]["semantics"]["customer_definition"] = (
            "A changed customer declaration."
        )
    elif change == "stream":
        data["key_event"]["identity"].update(
            web_stream_ref="ga4_admin.web_stream.789", web_stream_client_ref="789"
        )
    elif change == "contract":
        data["conversion_contract_record_digest"] = "f" * 64
    else:
        data["declared_destination_profile_ref"] = "ga4.other"
    changed = Ga4KeyEventCandidateInput.model_validate_json(canonical_bytes(data))
    with pytest.raises(AdhelmError) as error:
        select_ga4_key_event_create_candidate(
            ledger, candidate["record_digest"], fresh=changed, as_of=START
        )
    assert error.value.code == "GA4_CREATE_FRESH_INPUT_MISMATCH"
    assert "qualified_action" not in str(error.value)


@pytest.mark.parametrize("change", ["digest", "clock", "raw_input", "constructed_version"])
def test_invalid_input_rejects_before_history_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    ledger, _, _, fresh, candidate = _history(tmp_path)
    selected = candidate["record_digest"]
    at = START
    if change == "digest":
        selected = True
    elif change == "clock":
        at = datetime(2026, 9, 1)
    elif change == "raw_input":
        fresh = fresh.model_dump(mode="json")
    else:
        fresh = fresh.model_copy(update={"schema_version": True})

    def unexpected_read():
        pytest.fail("invalid input must fail before reading local history")

    monkeypatch.setattr(ledger, "checkpoint", unexpected_read)
    with pytest.raises(AdhelmError) as error:
        select_ga4_key_event_create_candidate(ledger, selected, fresh=fresh, as_of=at)
    assert error.value.code == "INVALID_GA4_CREATE_SELECTION"


def test_non_candidate_digest_does_not_select_definition(tmp_path: Path) -> None:
    ledger, definition, _, fresh, _ = _history(tmp_path)
    with pytest.raises(AdhelmError) as error:
        select_ga4_key_event_create_candidate(
            ledger, definition["record_digest"], fresh=fresh, as_of=START
        )
    assert error.value.code == "GA4_KEY_EVENT_CANDIDATE_NOT_RETAINED"


def test_unrelated_contract_does_not_supersede_selected_definition(tmp_path: Path) -> None:
    ledger, _, _, fresh, candidate = _history(tmp_path)
    retain_conversion_contract(
        ledger, _definition().model_copy(update={"contract_id": "conversion.unrelated"})
    )
    result = select_ga4_key_event_create_candidate(
        ledger, candidate["record_digest"], fresh=fresh, as_of=START
    )
    assert result.candidate_record_digest == candidate["record_digest"]
