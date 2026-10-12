"""Independent hostile-input checks for all-provider candidate retention."""

import json
from collections.abc import Callable
from decimal import Inexact, Rounded, localcontext
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_linkedin_compiler import _brief as linkedin_brief
from test_reddit_compiler import _brief as reddit_brief
from test_tiktok_compiler import _brief as tiktok_brief
from typer.testing import CliRunner

from adhelm import additional_compile_cli, compile_cli
from adhelm.candidate_records import CandidateCompilationRecord
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Money
from adhelm.saved_candidates import (
    candidate_catalog,
    compile_candidate,
    load_candidate,
    retain_candidate,
)

_SAFE_INTEGER = 9_007_199_254_740_991
_REDDIT_MAX_MINOR_UNITS = _SAFE_INTEGER // 10_000

_NEW_PROVIDER_CASES: tuple[tuple[str, Callable[[], Any], str], ...] = (
    ("linkedin-ads", linkedin_brief, "group_total_budget"),
    ("reddit-ads", reddit_brief, "lifetime_budget"),
    ("tiktok-ads", tiktok_brief, "total_budget"),
)


@pytest.mark.parametrize(
    ("factory", "native_field", "expected_amount"),
    (
        (linkedin_brief, "native_group_total_budget_amount", "250.00"),
        (tiktok_brief, "native_adgroup_total_budget_amount", "250.00"),
    ),
)
def test_usd_native_amounts_are_independent_of_decimal_context(
    tmp_path: Path,
    factory: Callable[[], Any],
    native_field: str,
    expected_amount: str,
) -> None:
    """The compiler and durable recompile must not inherit caller Decimal settings."""
    ledger = Ledger(tmp_path)
    with localcontext() as context:
        context.prec = 1
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        compiled = compile_candidate(factory())
        retained = retain_candidate(ledger, factory())

    assert getattr(compiled, native_field) == expected_amount
    loaded, _ = load_candidate(ledger, retained["record_digest"])
    assert getattr(loaded, native_field) == expected_amount


@pytest.mark.parametrize(
    ("factory", "budget_field", "maximum_minor_units", "native_field", "expected"),
    (
        (
            linkedin_brief,
            "group_total_budget",
            _SAFE_INTEGER,
            "native_group_total_budget_amount",
            "90071992547409.91",
        ),
        (
            tiktok_brief,
            "total_budget",
            _SAFE_INTEGER,
            "native_adgroup_total_budget_amount",
            "90071992547409.91",
        ),
        (
            reddit_brief,
            "lifetime_budget",
            _REDDIT_MAX_MINOR_UNITS,
            "native_lifetime_spend_micros",
            _REDDIT_MAX_MINOR_UNITS * 10_000,
        ),
    ),
)
def test_native_budget_boundaries_preserve_exact_typed_money(
    factory: Callable[[], Any],
    budget_field: str,
    maximum_minor_units: int,
    native_field: str,
    expected: str | int,
) -> None:
    brief = factory().model_copy(
        update={budget_field: Money(minor_units=maximum_minor_units, currency="USD")}
    )
    compiled = compile_candidate(brief)
    assert compiled.maximum_spend == Money(minor_units=maximum_minor_units, currency="USD")
    assert getattr(compiled, native_field) == expected

    bypassed_overflow = Money.model_construct(minor_units=maximum_minor_units + 1, currency="USD")
    forged = factory().model_copy(update={budget_field: bypassed_overflow})
    with pytest.raises((ValidationError, AdhelmError)):
        compile_candidate(forged)


@pytest.mark.parametrize("provider,factory,budget_field", _NEW_PROVIDER_CASES)
@pytest.mark.parametrize("invalid_minor_units", (2500.0, True))
def test_retain_cli_rejects_float_or_bool_money_before_key_or_ledger_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    factory: Callable[[], Any],
    budget_field: str,
    invalid_minor_units: float | bool,
) -> None:
    def forbidden_key() -> bytes:
        pytest.fail("invalid retention input must not access a sharing key")

    monkeypatch.setattr(additional_compile_cli, "export_key", forbidden_key)
    monkeypatch.setattr(compile_cli, "export_key", forbidden_key)
    raw = factory().model_dump(mode="json")
    raw[budget_field]["minor_units"] = invalid_minor_units
    input_file = tmp_path / "brief.json"
    input_file.write_text(json.dumps(raw))

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "compile",
            provider,
            "--input",
            str(input_file),
            "--retain",
        ],
    )

    assert result.exit_code != 0
    assert json.loads(result.output)["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert not (tmp_path / "ledger").exists()


@pytest.mark.parametrize("_provider,factory,_budget_field", _NEW_PROVIDER_CASES)
def test_rehashed_constructed_nested_candidate_cannot_reach_history(
    tmp_path: Path,
    _provider: str,
    factory: Callable[[], Any],
    _budget_field: str,
) -> None:
    compiled = compile_candidate(factory())
    malformed_boundary = compiled.planning_boundary.model_copy(
        update={"requires_creative_ledger_check": False}
    )
    constructed = type(compiled).model_construct(
        **{
            **compiled.__dict__,
            "planning_boundary": malformed_boundary,
        }
    )
    payload = {
        "compilation": constructed,
        "candidate_digest": digest(constructed),
    }

    with pytest.raises(AdhelmError):
        Ledger(tmp_path).append(
            "changes",
            "CANDIDATE_COMPILED",
            payload,
            provider=compiled.provider,
            account_ref=compiled.account_ref,
        )
    assert not (tmp_path / "ledger").exists()


def test_verified_checkpoint_rotation_preserves_new_candidate_variants(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, segment_bytes=1024)
    retained = [retain_candidate(ledger, factory()) for _, factory, _ in _NEW_PROVIDER_CASES]

    checkpoint = ledger.checkpoint()
    assert checkpoint["streams"]["changes"]["segments"] >= 2
    assert [row["provider"] for row in candidate_catalog(ledger)["candidates"]] == [
        "tiktok_ads",
        "reddit_ads",
        "linkedin_ads",
    ]
    for record in retained:
        loaded, provenance = load_candidate(ledger, record["record_digest"])
        assert provenance["candidate_digest"] == loaded.candidate_digest
        CandidateCompilationRecord.model_validate(record["payload"])
