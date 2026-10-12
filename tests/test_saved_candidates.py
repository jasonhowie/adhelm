"""Durable candidate evidence cannot masquerade as executable or approved plans."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_openai_compiler import _brief
from typer.testing import CliRunner

from adhelm import candidate_cli, compile_cli
from adhelm.candidate_records import CandidateCompilationRecord
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Money, OutputPreferences
from adhelm.projections import export_verified
from adhelm.saved_candidates import (
    candidate_catalog,
    compile_candidate,
    load_candidate,
    retain_candidate,
)


def test_retained_compilation_survives_restart_and_preserves_each_observation(
    tmp_path: Path,
) -> None:
    first = retain_candidate(Ledger(tmp_path), _brief())
    second = retain_candidate(Ledger(tmp_path), _brief())
    changed = _brief().model_copy(
        update={"lifetime_budget": Money(minor_units=3_000, currency="USD")}
    )
    third = retain_candidate(Ledger(tmp_path), changed)
    assert first["record_digest"] != second["record_digest"]
    assert first["payload"]["candidate_digest"] == second["payload"]["candidate_digest"]
    assert third["payload"]["candidate_digest"] != first["payload"]["candidate_digest"]
    catalog = candidate_catalog(Ledger(tmp_path), limit=1)
    assert catalog["total_count"] == 3 and catalog["next_cursor"] == "1"
    assert catalog["candidates"][0]["record_digest"] == third["record_digest"]
    assert (
        candidate_catalog(Ledger(tmp_path), cursor=1, limit=1)["candidates"][0]["record_digest"]
        == second["record_digest"]
    )
    compilation, provenance = load_candidate(Ledger(tmp_path), first["record_digest"])
    assert provenance["candidate_digest"] == compilation.candidate_digest
    assert compilation.maximum_spend.minor_units == 2500
    assert compilation.maximum_spend_micros == 25_000_000
    assert compilation.execution_status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert not Ledger(tmp_path).unresolved_starts()
    assert not Ledger(tmp_path).unresolved_runs()
    assert {r["record_kind"] for r in Ledger(tmp_path).records("changes")} == {
        "CANDIDATE_COMPILED",
        "SEGMENT_STARTED",
    }


def test_candidate_retains_only_closed_review_and_redacted_exports(tmp_path: Path) -> None:
    brief = _brief()
    ledger = Ledger(tmp_path / "data")
    record = retain_candidate(ledger, brief)
    payload = canonical_bytes(record["payload"])
    for private in (
        brief.name,
        brief.ad_groups[0].name,
        brief.ad_groups[0].ads[0].name,
        brief.ad_groups[0].ads[0].creative.title,
        brief.ad_groups[0].ads[0].creative.body,
        brief.ad_groups[0].ads[0].creative.destination_url,
        brief.ad_groups[0].ads[0].creative.file_reference.file_id,
    ):
        assert private.encode() not in payload
    assert not {"plan_digest", "approval_ref", "subplan_digest", "request", "file_id"} & set(
        record["payload"]
    )
    assert (
        record["payload"]["compilation"]["creative_references"][0]["reference_status"]
        == "DECLARED_NOT_VERIFIED_IN_LEDGER"
    )
    export_verified(ledger, tmp_path / "export", opaque_key=b"k" * 32)
    exported = (tmp_path / "export" / "records.jsonl").read_text()
    assert brief.account_ref not in exported
    assert brief.campaign_handle not in exported
    assert record["payload"]["candidate_digest"] in exported


@pytest.mark.parametrize(
    "field,value",
    [("provider", "meta_ads"), ("account_ref", "account:wrong"), ("source", "IMPORT")],
)
def test_candidate_rejects_false_envelope_before_creating_history(
    tmp_path: Path, field: str, value: str
) -> None:
    compilation = compile_candidate(_brief())
    payload = CandidateCompilationRecord(
        compilation=compilation, candidate_digest=compilation.candidate_digest
    ).model_dump(mode="json")
    kwargs = {
        "provider": compilation.provider,
        "account_ref": compilation.account_ref,
        "source": "LOCAL_CLI",
    }
    kwargs[field] = value
    with pytest.raises(AdhelmError, match="provider and account"):
        Ledger(tmp_path).append("changes", "CANDIDATE_COMPILED", payload, **kwargs)
    assert not (tmp_path / "ledger").exists()


def test_rehashed_candidate_with_wrong_account_still_fails_verified_reads(tmp_path: Path) -> None:
    record = retain_candidate(Ledger(tmp_path), _brief())
    path = next((tmp_path / "ledger" / "changes").glob("*.jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[-1]["account_ref"] = "account:substituted"
    rows[-1]["record_digest"] = digest({k: v for k, v in rows[-1].items() if k != "record_digest"})
    path.write_bytes(b"".join(canonical_bytes(row) + b"\n" for row in rows))
    for operation in (
        lambda: candidate_catalog(Ledger(tmp_path)),
        lambda: load_candidate(Ledger(tmp_path), record["record_digest"]),
    ):
        with pytest.raises(AdhelmError, match="provider and account"):
            operation()


def test_candidate_validation_rejects_bypassed_brief_and_forged_review(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        retain_candidate(Ledger(tmp_path), _brief().model_copy(update={"account_currency": "EUR"}))
    compilation = compile_candidate(_brief())
    payload = {"compilation": compilation.model_dump(mode="json"), "candidate_digest": "0" * 64}
    with pytest.raises(AdhelmError, match="schema"):
        Ledger(tmp_path).append("changes", "CANDIDATE_COMPILED", payload)
    assert not (tmp_path / "ledger").exists()
    for key, value in (
        ("execution_status", "APPROVED"),
        ("maximum_spend_micros", 1_000_001),
        ("ad_count", 9),
    ):
        bad = compilation.model_dump(mode="json")
        bad[key] = value
        payload = {"compilation": bad, "candidate_digest": digest(bad)}
        with pytest.raises(AdhelmError, match="schema"):
            Ledger(tmp_path).append("changes", "CANDIDATE_COMPILED", payload)


def test_retain_cli_receipt_fits_minimum_limit_without_private_key_or_provider_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden() -> bytes:
        pytest.fail("A hash-only receipt needs no private key or provider credential")

    monkeypatch.setattr(compile_cli, "export_key", forbidden)
    save_configuration(
        tmp_path / "data", Configuration(output=OutputPreferences(max_output_bytes=1024))
    )
    path = tmp_path / "brief.json"
    path.write_text(_brief().model_dump_json())
    base = ["--data-dir", str(tmp_path / "data")]
    retained = CliRunner().invoke(
        app, [*base, "compile", "openai-ads", "--input", str(path), "--retain"]
    )
    assert retained.exit_code == 0, retained.output
    assert len(retained.output.encode()) <= 1024
    data = json.loads(retained.output)["data"]
    assert data["persisted"] is True and data["remote_side_effects"] is False
    assert (
        data["record_digest"]
        == candidate_catalog(Ledger(tmp_path / "data"))["candidates"][0]["record_digest"]
    )
    assert _brief().account_ref not in retained.output


def test_candidate_cli_shares_verified_review_and_reports_unknown_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(candidate_cli, "export_key", lambda: b"k" * 32)
    record = retain_candidate(Ledger(tmp_path), _brief())
    runner = CliRunner()
    base = ["--data-dir", str(tmp_path), "candidate"]
    result = runner.invoke(app, [*base, "show", record["record_digest"]])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["account_ref"].startswith("opaque:")
    assert data["campaign_ref"].startswith("opaque:")
    assert data["requires_account_bound_file_readback"] is True
    assert data["requires_new_compilation_for_planning"] is True
    assert data["row_counts"] == {"creative_references": 1}
    missing = runner.invoke(app, [*base, "show", "0" * 64])
    assert missing.exit_code != 0
    assert json.loads(missing.output)["diagnostics"][0]["code"] == "CANDIDATE_NOT_RETAINED"
    discovery = json.loads(runner.invoke(app, ["discover"]).output)["data"]
    assert {"candidate catalog", "candidate show"} <= set(discovery["commands"])


def test_failed_durability_does_not_claim_retention_or_blindly_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def uncertain(*args: object, **kwargs: object) -> None:
        raise OSError("private path details")

    monkeypatch.setattr(compile_cli, "retain_candidate", uncertain)
    path = tmp_path / "brief.json"
    path.write_text(_brief().model_dump_json())
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            "openai-ads",
            "--input",
            str(path),
            "--retain",
        ],
    )
    assert result.exit_code != 0
    output = json.loads(result.output)
    assert output["outcome"] == "FAILED_CLOSED"
    assert output["diagnostics"][0]["code"] == "CANDIDATE_RETENTION_UNVERIFIED"
    assert "private path" not in result.output
    assert "persisted" not in output["data"]
