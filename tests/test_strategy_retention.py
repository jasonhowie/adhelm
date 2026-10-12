"""Closed-history coverage for one atomic six-provider strategy observation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_strategy import _six_strategy, _strategy
from typer.testing import CliRunner

from adhelm import saved_strategies, strategy_cli, strategy_history_cli
from adhelm.candidate_records import GoogleCandidateCompilation
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Money, OutputPreferences
from adhelm.saved_strategies import (
    compile_strategy_observation,
    load_strategy,
    retain_strategy,
    strategy_catalog,
)
from adhelm.strategy_records import validate_strategy_candidate_record

_NOW = datetime(2026, 9, 5, tzinfo=UTC)


def _no_key() -> bytes:
    pytest.fail("Strategy retention must not access the export key")


def _retain(root: Path) -> dict[str, Any]:
    return retain_strategy(Ledger(root), _six_strategy(), now=lambda: _NOW)


def test_retention_appends_one_atomic_closed_six_provider_observation(tmp_path: Path) -> None:
    record = _retain(tmp_path)
    ledger = Ledger(tmp_path)
    rows = ledger.records("changes")
    strategy_rows = [row for row in rows if row["record_kind"] == "STRATEGY_CANDIDATE_COMPILED"]
    assert len(strategy_rows) == 1
    assert not [row for row in rows if row["record_kind"] == "CANDIDATE_COMPILED"]
    assert record["record_digest"] == strategy_rows[0]["record_digest"]
    assert record["provider"] is None and record["account_ref"] is None and record["run_id"] is None
    assert record["occurred_at"] is None and record["observed_at"] is None

    observation, provenance = load_strategy(ledger, record["record_digest"])
    assert provenance["record_digest"] == record["record_digest"]
    assert tuple(child.compilation.provider for child in observation.child_compilations) == (
        "google_ads",
        "meta_ads",
        "linkedin_ads",
        "tiktok_ads",
        "reddit_ads",
        "openai_ads",
    )
    assert observation.ordered_child_candidate_digests == tuple(
        child.candidate_digest for child in observation.child_compilations
    )
    assert observation.ordered_child_spec_digests == tuple(
        child.compilation.spec_digest for child in observation.child_compilations
    )
    assert observation.total_caps_by_currency == observation.maximum_spend_by_currency
    child_data = {
        child.compilation.provider: child.compilation.model_dump(mode="json")
        for child in observation.child_compilations
    }
    assert child_data["google_ads"]["native_total_budget_micros"] == 250_000_000
    assert child_data["meta_ads"]["native_lifetime_budget_minor_units"] == 25_000
    assert child_data["linkedin_ads"]["native_group_total_budget_amount"] == "250.00"
    assert child_data["tiktok_ads"]["native_adgroup_total_budget_amount"] == "250.00"
    assert child_data["reddit_ads"]["native_lifetime_spend_micros"] == 25_000_000
    assert child_data["openai_ads"]["maximum_spend_micros"] == 25_000_000
    assert observation.unmet_live_proof == "ALL_SELECTED_PROVIDER_BOUNDARIES_UNMET"
    assert observation.authorization_status == "NO_AUTHORITY_GRANTED"
    assert all(
        child.compilation.requires_new_compilation_for_planning
        and child.compilation.activation_requires_new_digest_and_authorization
        and not child.compilation.remote_side_effects
        for child in observation.child_compilations
    )
    assert not ledger.unresolved_runs()


def test_retained_payload_omits_source_text_native_specs_and_replayable_payloads(
    tmp_path: Path,
) -> None:
    brief = _six_strategy()
    record = retain_strategy(Ledger(tmp_path), brief, now=lambda: _NOW)
    payload = canonical_bytes(record["payload"]).decode()
    for value in (
        brief.business_intent.statement,
        brief.approval_owner,
        *brief.assumptions,
    ):
        assert value not in payload
    for field in (
        '"source_brief"',
        '"provider_briefs"',
        '"campaign_request"',
        '"destination_url"',
        '"request_body"',
        '"statement"',
        '"spec"',
    ):
        assert field not in payload


def test_record_rejects_rehashed_nested_gate_and_constructed_money(tmp_path: Path) -> None:
    observation = compile_strategy_observation(_six_strategy(), now=lambda: _NOW)
    payload = observation.model_dump(mode="json")
    first = payload["child_compilations"][0]
    compilation = first["compilation"]
    compilation["planning_boundary"]["requires_current_account_identity"] = False
    first["candidate_digest"] = digest(compilation)
    payload["ordered_child_candidate_digests"][0] = first["candidate_digest"]
    with pytest.raises(ValidationError):
        validate_strategy_candidate_record(payload)
    with pytest.raises(AdhelmError):
        Ledger(tmp_path).append(
            "changes",
            "STRATEGY_CANDIDATE_COMPILED",
            payload,
            provider=None,
            account_ref=None,
        )
    assert not (tmp_path / "ledger").exists()

    raw_money = observation.model_dump(mode="json")
    raw_money["total_caps_by_currency"][0]["minor_units"] = 105_000.0
    raw_money["maximum_spend_by_currency"][0]["minor_units"] = 105_000.0
    with pytest.raises(ValidationError):
        validate_strategy_candidate_record(raw_money)

    first_child = observation.child_compilations[0]
    assert isinstance(first_child.compilation, GoogleCandidateCompilation)
    malformed_boundary = first_child.compilation.planning_boundary.model_copy(
        update={"requires_current_account_identity": False}
    )
    malformed_compilation = type(first_child.compilation).model_construct(
        **{**first_child.compilation.__dict__, "planning_boundary": malformed_boundary}
    )
    malformed_child = type(first_child).model_construct(
        **{**first_child.__dict__, "compilation": malformed_compilation}
    )
    constructed = observation.model_construct(
        **{
            **observation.__dict__,
            "child_compilations": (malformed_child, *observation.child_compilations[1:]),
        }
    )
    with pytest.raises((ValidationError, ValueError)):
        validate_strategy_candidate_record(constructed)

    constructed_money = observation.model_construct(
        **{
            **observation.__dict__,
            "maximum_spend_by_currency": (
                Money.model_construct(minor_units=105_000.0, currency="USD"),
            ),
        }
    )
    with pytest.raises((ValidationError, ValueError)):
        validate_strategy_candidate_record(constructed_money)


def test_late_child_normalization_and_expiry_recheck_leave_no_partial_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = saved_strategies._normalized_child_record

    def fail_last(source: object, candidate: object) -> object:
        if getattr(candidate, "provider", None) == "openai_ads":
            raise AdhelmError(
                "STRATEGY_CHILD_BINDING_INVALID", "forced late child failure", exit_code=2
            )
        return real(source, candidate)  # type: ignore[arg-type]

    monkeypatch.setattr(saved_strategies, "_normalized_child_record", fail_last)
    with pytest.raises(AdhelmError, match="forced late child failure"):
        retain_strategy(Ledger(tmp_path), _six_strategy(), now=lambda: _NOW)
    assert not (tmp_path / "ledger").exists()

    brief = _strategy(expires_at=_NOW + timedelta(seconds=1))
    ticks = iter((_NOW, _NOW + timedelta(seconds=2)))
    with pytest.raises(AdhelmError) as error:
        retain_strategy(Ledger(tmp_path / "expired"), brief, now=lambda: next(ticks))
    assert error.value.code == "STRATEGY_EXPIRED"
    assert not (tmp_path / "expired" / "ledger").exists()


def test_cli_retain_is_key_free_preflighted_and_returns_only_compact_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief = _six_strategy()
    input_file = tmp_path / "strategy.json"
    input_file.write_text(brief.model_dump_json())
    monkeypatch.setattr(strategy_cli, "_utc_now", lambda: _NOW)
    monkeypatch.setattr(strategy_cli, "export_key", _no_key)
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            "strategy",
            "--input",
            str(input_file),
            "--retain",
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["outcome"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert body["data"]["persisted"] is True
    assert body["data"]["remote_side_effects"] is False
    assert body["data"]["authorization_status"] == "NO_AUTHORITY_GRANTED"
    assert body["data"]["selected_provider_count"] == 6
    assert brief.business_intent.statement not in result.output
    assert brief.approval_owner not in result.output
    loaded, _ = load_strategy(Ledger(tmp_path / "data"), body["data"]["record_digest"])
    assert len(loaded.child_compilations) == 6


def test_cli_receipt_over_limit_rejects_before_ledger_or_export_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_file = tmp_path / "strategy.json"
    input_file.write_text(_six_strategy().model_dump_json())
    monkeypatch.setattr(strategy_cli, "_utc_now", lambda: _NOW)
    monkeypatch.setattr(strategy_cli, "export_key", _no_key)
    monkeypatch.setattr(strategy_cli, "canonical_bytes", lambda _value: b"x" * 65_536)
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            "strategy",
            "--input",
            str(input_file),
            "--retain",
        ],
    )
    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert not (tmp_path / "data" / "ledger").exists()


def test_catalog_and_complete_show_use_verified_history_and_redact_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _retain(tmp_path / "data")
    second = _retain(tmp_path / "data")
    ledger = Ledger(tmp_path / "data")
    catalog = strategy_catalog(ledger, cursor=0, limit=1)
    assert catalog["total_count"] == 2
    assert catalog["strategies"][0]["record_digest"] == second["record_digest"]
    assert catalog["next_cursor"] == "1"

    monkeypatch.setattr(strategy_history_cli, "_utc_now", lambda: _NOW)
    monkeypatch.setattr(strategy_history_cli, "export_key", lambda: b"k" * 32)
    shown = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "strategy", "show", first["record_digest"]],
    )
    assert shown.exit_code == 0, shown.output
    data = json.loads(shown.output)["data"]
    assert data["historical_status"] == "RETAINED_OFFLINE_OBSERVATION"
    assert data["expiry_status_at_as_of"] == "NOT_EXPIRED_AT_AS_OF_REMAINS_NON_EXECUTABLE"
    assert len(data["child_compilations"]) == 6
    assert all(
        child["compilation"]["account_ref"].startswith("opaque:")
        for child in data["child_compilations"]
    )
    assert _six_strategy().business_intent.statement not in shown.output


def test_show_over_limit_fails_before_export_key_without_trimming_child_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _retain(tmp_path / "data")
    save_configuration(
        tmp_path / "data", Configuration(output=OutputPreferences(max_output_bytes=1024))
    )
    monkeypatch.setattr(strategy_history_cli, "export_key", _no_key)
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "strategy", "show", record["record_digest"]],
    )
    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert strategy_catalog(Ledger(tmp_path / "data"))["total_count"] == 1


def test_show_marks_expired_history_without_calling_it_currently_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _retain(tmp_path / "data")
    monkeypatch.setattr(strategy_history_cli, "_utc_now", lambda: datetime(2026, 11, 1, tzinfo=UTC))
    monkeypatch.setattr(strategy_history_cli, "export_key", lambda: b"k" * 32)
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "strategy", "show", record["record_digest"]],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["expiry_status_at_as_of"] == "EXPIRED_AT_AS_OF"
    assert data["execution_status"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert data["authorization_status"] == "NO_AUTHORITY_GRANTED"


def test_aggregate_envelope_rejects_provider_account_run_and_remote_times(tmp_path: Path) -> None:
    observation = compile_strategy_observation(_six_strategy(), now=lambda: _NOW)
    with pytest.raises(AdhelmError) as error:
        Ledger(tmp_path).append(
            "changes",
            "STRATEGY_CANDIDATE_COMPILED",
            observation.model_dump(mode="json"),
            provider="google_ads",
            account_ref="account.declared",
            run_id="run.declared",
            occurred_at=_NOW.isoformat(),
            observed_at=_NOW.isoformat(),
        )
    assert error.value.code == "INVALID_STRATEGY_CANDIDATE_PROVENANCE"
    assert not (tmp_path / "ledger").exists()


def test_append_uncertainty_never_claims_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(tmp_path)

    def fail_append(*args: object, **kwargs: object) -> dict[str, Any]:
        raise OSError("forced durable append failure")

    monkeypatch.setattr(ledger, "append", fail_append)
    with pytest.raises(AdhelmError) as error:
        retain_strategy(ledger, _six_strategy(), now=lambda: _NOW)
    assert error.value.code == "STRATEGY_RETENTION_UNVERIFIED"
    assert not (tmp_path / "ledger").exists()
