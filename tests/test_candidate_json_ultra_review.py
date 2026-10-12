"""Independent candidate-history regressions for pre-canonical JSON type checks."""

import json
import warnings
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_google_ads_compiler import _brief as google_brief
from test_meta_compiler import _brief as meta_brief
from test_openai_compiler import _brief as openai_brief

from adhelm.candidate_records import CandidateCompilationRecord
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.saved_candidates import compile_candidate


def payload(provider: str) -> dict[str, Any]:
    compiled = compile_candidate(
        {"openai": openai_brief, "meta": meta_brief, "google": google_brief}[provider]()
    )
    return CandidateCompilationRecord(
        compilation=compiled, candidate_digest=compiled.candidate_digest
    ).model_dump(mode="json")


@pytest.mark.parametrize("provider", ["openai", "meta", "google"])
@pytest.mark.parametrize("field", ["budget", "schema_version", "authority_flag", "remote_flag"])
def test_candidate_json_rejects_numeric_type_laundering_before_history(
    tmp_path: Path,
    provider: str,
    field: str,
) -> None:
    raw = payload(provider)
    candidate = raw["compilation"]
    if field == "budget":
        candidate["maximum_spend"]["minor_units"] = float(candidate["maximum_spend"]["minor_units"])
    elif field == "schema_version":
        candidate[field] = 1.0
    elif field == "authority_flag":
        candidate["requires_new_compilation_for_planning"] = 1
    else:
        candidate["remote_side_effects"] = 0
    with pytest.raises(ValidationError):
        CandidateCompilationRecord.model_validate_json(json.dumps(raw))
    ledger = Ledger(tmp_path)
    with pytest.raises(AdhelmError):
        ledger.append(
            "changes",
            "CANDIDATE_COMPILED",
            raw,
            provider=candidate["provider"],
            account_ref=candidate["account_ref"],
        )
    assert not ledger.records("changes")


@pytest.mark.parametrize("value", [True, 1.0])
def test_record_version_cannot_be_boolean_or_float(value: Any) -> None:
    raw = payload("openai")
    raw["schema_version"] = value
    with pytest.raises(ValidationError):
        CandidateCompilationRecord.model_validate(raw)


def test_constructed_nested_money_does_not_emit_serializer_repr() -> None:
    class PrivateObject:
        def __repr__(self) -> str:
            return "private-candidate-serializer-marker"

    compiled = compile_candidate(openai_brief())
    bad_money = compiled.maximum_spend.model_copy(update={"minor_units": PrivateObject()})
    constructed = compiled.model_copy(update={"maximum_spend": bad_money})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(ValidationError):
            CandidateCompilationRecord(
                compilation=constructed, candidate_digest=compiled.candidate_digest
            )
    assert not caught


@pytest.mark.parametrize("provider", ["openai", "meta", "google"])
def test_valid_review_roundtrip_preserves_existing_canonical_digest(provider: str) -> None:
    raw = payload(provider)
    parsed = CandidateCompilationRecord.model_validate_json(json.dumps(raw))
    assert parsed.model_dump(mode="json") == raw
    assert parsed.compilation.candidate_digest == raw["candidate_digest"]
