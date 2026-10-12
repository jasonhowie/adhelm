"""Independent adversarial review of retained GA4 CREATE candidate selection."""

from __future__ import annotations

import inspect
import json
import socket
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_saved_ga4_key_event_candidates import START, _definition, _request

import adhelm.ga4_create_selection as selection
from adhelm.canonical import canonical_bytes
from adhelm.conversion_contracts import ConversionContract
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_key_event_candidates import Ga4KeyEventCandidateInput
from adhelm.ledger import Ledger
from adhelm.saved_conversion_contracts import retain_conversion_contract
from adhelm.saved_ga4_key_event_candidates import retain_ga4_key_event_candidate


def _history(
    tmp_path: Path,
) -> tuple[
    Ledger,
    dict[str, object],
    ConversionContract,
    Ga4KeyEventCandidateInput,
    dict[str, object],
]:
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


def _assert_code(error: pytest.ExceptionInfo[AdhelmError], code: str) -> None:
    assert error.value.code == code
    assert error.value.exit_code in {ExitCode.INVALID_INPUT, ExitCode.PRECONDITION}
    assert "qualified_request" not in error.value.message
    assert "customer-defined qualified request" not in error.value.message


def test_selects_from_one_checkpoint_without_append_or_egress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, definition, contract, fresh, candidate = _history(tmp_path)
    checkpoint = ledger.checkpoint()
    calls = 0

    def one_checkpoint() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return checkpoint

    def prohibited(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("selection must not append or perform egress")

    monkeypatch.setattr(ledger, "checkpoint", one_checkpoint)
    monkeypatch.setattr(ledger, "append", prohibited)
    monkeypatch.setattr(socket, "socket", prohibited)
    result = selection.select_ga4_key_event_create_candidate(
        ledger,
        candidate["record_digest"],
        fresh=fresh,
        as_of=START,
    )

    assert calls == 1
    assert result.checkpoint_digest == checkpoint["checkpoint_digest"]
    assert result.contract_record_digest == definition["record_digest"]
    assert result.contract == contract
    assert result.candidate.model_dump(mode="json") == candidate["payload"]
    assert not hasattr(result, "fresh")
    assert "qualified_request" not in repr(result)
    assert set(inspect.signature(selection.select_ga4_key_event_create_candidate).parameters) == {
        "ledger",
        "candidate_record_digest",
        "fresh",
        "as_of",
    }


def test_selection_requires_fresh_source_but_does_not_retain_it_in_the_candidate(
    tmp_path: Path,
) -> None:
    ledger, _definition_envelope, _contract, fresh, candidate = _history(tmp_path)
    result = selection.select_ga4_key_event_create_candidate(
        ledger,
        candidate["record_digest"],
        fresh=fresh,
        as_of=START,
    )

    retained_candidate = json.dumps(result.candidate.model_dump(mode="json"), sort_keys=True)
    assert "A customer-defined qualified request event." not in retained_candidate
    assert "Count each declared occurrence." not in retained_candidate
    assert "qualified_request" in retained_candidate


def test_future_supersession_blocks_before_any_fresh_recompile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, _definition_envelope, contract, fresh, candidate = _history(tmp_path)
    retain_conversion_contract(
        ledger,
        _definition(
            version=2,
            supersedes=contract.content_digest,
            effective_from=START + timedelta(days=7),
        ),
    )

    def must_not_recompile(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("superseded candidates must fail before recompilation")

    monkeypatch.setattr(selection, "compile_ga4_key_event_candidate", must_not_recompile)
    with pytest.raises(AdhelmError) as raised:
        selection.select_ga4_key_event_create_candidate(
            ledger,
            candidate["record_digest"],
            fresh=fresh,
            as_of=START,
        )
    _assert_code(raised, "GA4_CREATE_CONTRACT_SUPERSEDED")


@pytest.mark.parametrize(
    "change",
    ["customer_definition", "field_required", "counting_rule", "profile_ref"],
)
def test_exact_fresh_source_recompile_rejects_structural_or_prose_drift(
    tmp_path: Path,
    change: str,
) -> None:
    ledger, _definition_envelope, _contract, fresh, candidate = _history(tmp_path)
    raw = fresh.model_dump(mode="json")
    semantics = raw["key_event"]["event_contract"]["semantics"]
    if change == "customer_definition":
        semantics["customer_definition"] = "A private changed declaration."
    elif change == "field_required":
        raw["key_event"]["event_contract"]["fields"][0]["required"] = False
    elif change == "counting_rule":
        semantics["counting_rule"] = "A private changed count rule."
    else:
        raw["declared_destination_profile_ref"] = "ga4.changed"
    changed = Ga4KeyEventCandidateInput.model_validate_json(canonical_bytes(raw))

    with pytest.raises(AdhelmError) as raised:
        selection.select_ga4_key_event_create_candidate(
            ledger,
            candidate["record_digest"],
            fresh=changed,
            as_of=START,
        )
    _assert_code(raised, "GA4_CREATE_FRESH_INPUT_MISMATCH")
    assert "private changed" not in str(raised.value)


@pytest.mark.parametrize(
    "candidate_digest, fresh, as_of",
    [
        ("F" * 64, None, START),
        ("f" * 64, None, START),
        ("f" * 64, None, datetime(2026, 9, 1)),
        ("f" * 64, None, True),
    ],
)
def test_invalid_public_inputs_fail_before_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_digest: object,
    fresh: object,
    as_of: object,
) -> None:
    ledger, _definition_envelope, _contract, valid_fresh, _candidate = _history(tmp_path)
    if fresh is None:
        fresh = valid_fresh
    if candidate_digest == "f" * 64 and as_of is START:
        fresh = valid_fresh.model_copy(
            update={"key_event": valid_fresh.key_event.model_copy(update={"counting_method": True})}
        )

    def unexpected_checkpoint() -> dict[str, object]:
        pytest.fail("invalid public input must fail before history access")

    monkeypatch.setattr(ledger, "checkpoint", unexpected_checkpoint)
    with pytest.raises(AdhelmError) as raised:
        selection.select_ga4_key_event_create_candidate(
            ledger,
            candidate_digest,  # type: ignore[arg-type]
            fresh=fresh,  # type: ignore[arg-type]
            as_of=as_of,  # type: ignore[arg-type]
        )
    _assert_code(raised, "INVALID_GA4_CREATE_SELECTION")


def test_selection_uses_the_single_historical_checkpoint_without_mixing_later_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, _definition_envelope, contract, fresh, candidate = _history(tmp_path)
    historic = ledger.checkpoint()
    appended = False

    def checkpoint_then_append() -> dict[str, object]:
        nonlocal appended
        if not appended:
            appended = True
            retain_conversion_contract(
                ledger,
                _definition(
                    version=2,
                    supersedes=contract.content_digest,
                    effective_from=START + timedelta(days=1),
                ),
            )
        return historic

    monkeypatch.setattr(ledger, "checkpoint", checkpoint_then_append)
    result = selection.select_ga4_key_event_create_candidate(
        ledger,
        candidate["record_digest"],
        fresh=fresh,
        as_of=START,
    )

    assert appended is True
    assert result.checkpoint_digest == historic["checkpoint_digest"]
    assert result.contract_record_digest != ""
    assert Ledger.checkpoint(ledger)["checkpoint_digest"] != historic["checkpoint_digest"]


def test_current_definition_must_be_effective_at_the_explicit_aware_instant(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path)
    future = START + timedelta(hours=1)
    definition = retain_conversion_contract(ledger, _definition(effective_from=future))
    contract = ConversionContract.model_validate_json(canonical_bytes(definition["payload"]))
    fresh = _request(definition["record_digest"])
    candidate = retain_ga4_key_event_candidate(
        ledger,
        fresh,
        retained_contract=contract,
        retained_contract_record_digest=definition["record_digest"],
    )

    with pytest.raises(AdhelmError) as raised:
        selection.select_ga4_key_event_create_candidate(
            ledger,
            candidate["record_digest"],
            fresh=fresh,
            as_of=future - timedelta(microseconds=1),
        )
    _assert_code(raised, "GA4_CREATE_CONTRACT_NOT_EFFECTIVE")
