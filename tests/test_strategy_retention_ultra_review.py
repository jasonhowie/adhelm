"""Independent regressions for the new atomic strategy-history boundary."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_strategy import _six_strategy

from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.saved_strategies import compile_strategy_observation, retain_strategy
from adhelm.strategy_records import validate_strategy_candidate_record

NOW = datetime(2026, 9, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requires_new_compilation_for_planning", 1),
        ("requires_new_compilation_for_planning", 1.0),
        ("activation_requires_new_digest_and_authorization", 1),
        ("activation_requires_new_digest_and_authorization", 1.0),
        ("remote_side_effects", 0),
        ("remote_side_effects", 0.0),
    ],
)
def test_numeric_control_flags_cannot_enter_strategy_history(
    tmp_path: Path, field: str, value: object
) -> None:
    original = compile_strategy_observation(_six_strategy(), now=lambda: NOW)
    payload = original.model_dump(mode="json")
    payload[field] = value
    with pytest.raises((ValueError, ValidationError)):
        validate_strategy_candidate_record(payload)
    with pytest.raises(AdhelmError):
        Ledger(tmp_path).append("changes", "STRATEGY_CANDIDATE_COMPILED", payload)
    assert not (tmp_path / "ledger").exists()


def test_rehashed_strategy_history_does_not_normalize_a_numeric_control_flag(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path)
    retained = retain_strategy(ledger, _six_strategy(), now=lambda: NOW)
    segment = next((tmp_path / "ledger/changes").glob("*.jsonl"))
    records = [json.loads(line) for line in segment.read_text().splitlines()]
    assert records[-1]["record_digest"] == retained["record_digest"]
    records[-1]["payload"]["remote_side_effects"] = 0
    records[-1].pop("record_digest")
    records[-1]["record_digest"] = digest(records[-1])
    segment.write_bytes(b"".join(canonical_bytes(record) + b"\n" for record in records))
    with pytest.raises(AdhelmError):
        ledger.checkpoint()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("provider", "google_ads"),
        ("account_ref", "unrelated-account"),
        ("run_id", "unrelated-run"),
        ("source", "UNKNOWN"),
        ("observed_at", NOW.isoformat()),
        ("occurred_at", NOW.isoformat()),
    ],
)
def test_each_false_aggregate_provenance_claim_fails_before_append(
    tmp_path: Path, name: str, value: str
) -> None:
    record = compile_strategy_observation(_six_strategy(), now=lambda: NOW)
    with pytest.raises(AdhelmError) as error:
        Ledger(tmp_path).append(
            "changes",
            "STRATEGY_CANDIDATE_COMPILED",
            record.model_dump(mode="json"),
            **{name: value},
        )
    assert error.value.code == "INVALID_STRATEGY_CANDIDATE_PROVENANCE"
    assert not (tmp_path / "ledger").exists()


def test_strategy_expiry_during_append_lock_wait_cannot_retain_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(tmp_path)
    brief = _six_strategy()
    current = [NOW]
    original = ledger._make_record

    def expire(*args: object, **kwargs: object) -> dict:
        value = original(*args, **kwargs)  # type: ignore[arg-type]
        if args[1] == "STRATEGY_CANDIDATE_COMPILED":
            current[0] = brief.expires_at
        return value

    monkeypatch.setattr(ledger, "_make_record", expire)
    with pytest.raises(AdhelmError) as failed:
        retain_strategy(ledger, brief, now=lambda: current[0])
    assert failed.value.code == "STRATEGY_RETENTION_UNVERIFIED"
    assert not any(
        item["record_kind"] == "STRATEGY_CANDIDATE_COMPILED" for item in ledger.records("changes")
    )
