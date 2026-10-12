"""Adversarial checks for non-executable local candidate retention."""

import json
from pathlib import Path

import pytest
from test_openai_compiler import _brief
from typer.testing import CliRunner

from adhelm.candidate_records import CandidateCompilation, CandidateCompilationRecord
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.projections import export_verified
from adhelm.providers.openai_compiler import OpenAIAdsCampaignBrief
from adhelm.saved_candidates import candidate_catalog, compile_candidate, retain_candidate


def _payload() -> tuple[dict[str, object], CandidateCompilation]:
    compilation = compile_candidate(_brief())
    return (
        CandidateCompilationRecord(
            compilation=compilation, candidate_digest=compilation.candidate_digest
        ).model_dump(mode="json"),
        compilation,
    )


def test_candidate_cannot_be_attached_to_an_execution_run_before_append(tmp_path: Path) -> None:
    payload, compilation = _payload()

    with pytest.raises(AdhelmError, match="cannot join an execution run"):
        Ledger(tmp_path).append(
            "changes",
            "CANDIDATE_COMPILED",
            payload,
            provider=compilation.provider,
            account_ref=compilation.account_ref,
            run_id="run:attempted-authority-escalation",
        )

    assert not (tmp_path / "ledger").exists()


def test_rehashed_candidate_with_execution_run_is_rejected_on_verified_read(tmp_path: Path) -> None:
    record = retain_candidate(Ledger(tmp_path), _brief())
    path = next((tmp_path / "ledger" / "changes").glob("*.jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    candidate = next(row for row in rows if row["record_kind"] == "CANDIDATE_COMPILED")
    candidate["run_id"] = "run:attempted-authority-escalation"
    candidate["record_digest"] = digest(
        {key: value for key, value in candidate.items() if key != "record_digest"}
    )
    path.write_bytes(b"".join(canonical_bytes(row) + b"\n" for row in rows))

    with pytest.raises(AdhelmError, match="cannot join an execution run"):
        candidate_catalog(Ledger(tmp_path))
    assert record["record_digest"]


def test_candidate_show_and_export_redact_every_retained_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("adhelm.candidate_cli.export_key", lambda: b"k" * 32)
    brief = _brief()
    ledger = Ledger(tmp_path / "data")
    record = retain_candidate(ledger, brief)
    creative_id = record["payload"]["compilation"]["creative_references"][0]["creative_version_id"]

    shown = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "candidate", "show", record["record_digest"]],
    )
    assert shown.exit_code == 0, shown.output
    assert creative_id not in shown.output
    assert brief.account_ref not in shown.output
    assert brief.campaign_handle not in shown.output

    export_verified(ledger, tmp_path / "export", opaque_key=b"k" * 32)
    exported = (tmp_path / "export" / "records.jsonl").read_text()
    assert creative_id not in exported
    assert brief.account_ref not in exported
    assert brief.campaign_handle not in exported


def test_post_append_retention_uncertainty_never_claims_a_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_retain = retain_candidate

    def append_then_fail(ledger: Ledger, brief: OpenAIAdsCampaignBrief) -> dict[str, object]:
        actual_retain(ledger, brief)
        raise OSError("durability state unknown")

    monkeypatch.setattr("adhelm.compile_cli.retain_candidate", append_then_fail)
    input_file = tmp_path / "brief.json"
    input_file.write_text(_brief().model_dump_json())

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            "openai-ads",
            "--input",
            str(input_file),
            "--retain",
        ],
    )

    assert result.exit_code != 0
    output = json.loads(result.output)
    assert output["diagnostics"][0]["code"] == "CANDIDATE_RETENTION_UNVERIFIED"
    assert "persisted" not in output["data"]
    # A caller must inspect verified local history, rather than retrying based
    # on this uncertain outcome; the record may already be durable.
    assert candidate_catalog(Ledger(tmp_path / "data"))["total_count"] == 1
