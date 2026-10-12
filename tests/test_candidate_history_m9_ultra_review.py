"""Independent adversarial checks for M9 Meta/Google candidate history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer
from pydantic import ValidationError
from test_google_ads_compiler import _brief as google_brief
from test_meta_compiler import _brief as meta_brief
from typer.main import get_command
from typer.testing import CliRunner

from adhelm import __version__, candidate_cli, compile_cli
from adhelm.candidate_records import CandidateCompilationRecord
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences, ResultEnvelope
from adhelm.privacy import opaque_ref
from adhelm.projections import export_verified
from adhelm.saved_candidates import compile_candidate, retain_candidate


def _brief(provider: str) -> Any:
    if provider == "meta_ads":
        return meta_brief()
    if provider == "google_ads":
        return google_brief()
    raise AssertionError(f"unexpected provider: {provider}")


def _private_handle_brief(provider: str) -> Any:
    """Use labels that cannot be confused with field names in leakage assertions."""
    brief = _brief(provider)
    if provider == "meta_ads":
        return brief.model_copy(
            update={
                "campaign_handle": "private-campaign-label",
                "adset_handle": "private-adset-label",
                "ad_handle": "private-ad-label",
            }
        )
    return brief.model_copy(
        update={
            "budget_handle": "private-budget-label",
            "campaign_handle": "private-campaign-label",
            "ad_group_handle": "private-adgroup-label",
            "ad_handle": "private-ad-label",
        }
    )


@pytest.mark.parametrize("provider", ("meta_ads", "google_ads"))
@pytest.mark.parametrize("construction", ("model_copy", "model_construct"))
def test_record_revalidates_forged_nested_provider_models(provider: str, construction: str) -> None:
    """A forged in-memory nested model cannot evade the retained-record gates."""
    compiled = compile_candidate(_brief(provider))
    boundary = compiled.planning_boundary.model_copy(
        update={"requires_drift_check_and_authorization": False}
    )
    if construction == "model_copy":
        forged = compiled.model_copy(update={"planning_boundary": boundary})
    else:
        forged = type(compiled).model_construct(
            **{**compiled.__dict__, "planning_boundary": boundary}
        )

    assert forged.planning_boundary.requires_drift_check_and_authorization is False
    with pytest.raises(ValidationError) as raised:
        CandidateCompilationRecord(compilation=forged, candidate_digest="0" * 64)

    locations = {tuple(error["loc"]) for error in raised.value.errors()}
    assert (
        "compilation",
        provider,
        "planning_boundary",
        "requires_drift_check_and_authorization",
    ) in locations

    retained = CandidateCompilationRecord(
        compilation=compiled, candidate_digest=compiled.candidate_digest
    )
    assert retained.compilation == compiled


@pytest.mark.parametrize("provider", ("meta_ads", "google_ads"))
@pytest.mark.parametrize("tamper", ("planner_gate", "native_budget", "parent_handle"))
def test_rehashed_serialized_candidate_tampering_fails_before_append(
    tmp_path: Path, provider: str, tamper: str
) -> None:
    """Changing retained gates, exact native money, or hierarchy bindings is invalid."""
    compiled = compile_candidate(_brief(provider))
    value = compiled.model_dump(mode="json")
    review = value["review"]
    assert isinstance(review, dict)

    if tamper == "planner_gate":
        boundary = value["planning_boundary"]
        assert isinstance(boundary, dict)
        boundary["requires_drift_check_and_authorization"] = False
    elif tamper == "native_budget":
        field = (
            "native_lifetime_budget_minor_units"
            if provider == "meta_ads"
            else "native_total_budget_micros"
        )
        assert isinstance(value[field], int)
        value[field] += 1
    elif provider == "meta_ads":
        review["adset_handle"] = review["campaign_handle"]
    else:
        review["ad_group_handle"] = review["campaign_handle"]

    payload = {
        "schema_version": 1,
        "event_type": "CANDIDATE_COMPILED",
        "compilation": value,
        # This models an attacker who recomputes the review hash after editing
        # the serialized record rather than relying on a digest mismatch.
        "candidate_digest": digest(value),
    }
    with pytest.raises(AdhelmError, match="declared schema"):
        Ledger(tmp_path).append(
            "changes",
            "CANDIDATE_COMPILED",
            payload,
            provider=compiled.provider,
            account_ref=compiled.account_ref,
        )
    assert not (tmp_path / "ledger").exists()


@pytest.mark.parametrize("provider", ("meta_ads", "google_ads"))
def test_show_and_export_hmac_every_repeated_account_and_role_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Nested handles use the same opaque reference as their top-level facts."""
    key = b"m" * 32
    brief = _private_handle_brief(provider)
    compiled = compile_candidate(brief)
    ledger = Ledger(tmp_path / "data")
    record = retain_candidate(ledger, brief)
    monkeypatch.setattr(candidate_cli, "export_key", lambda: key)

    shown = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "candidate", "show", record["record_digest"]],
    )
    assert shown.exit_code == 0, shown.output
    review = json.loads(shown.output)["data"]
    assert (
        review["account_ref"]
        == review["review"]["account_ref"]
        == opaque_ref(key, brief.account_ref)
    )
    assert (
        review["campaign_ref"]
        == review["review"]["campaign_handle"]
        == opaque_ref(key, brief.campaign_handle)
    )
    creative_version_id = compiled.creative_references[0].creative_version_id
    assert (
        review["creative_references"][0]["creative_version_id"]
        == review["review"]["creative_version_id"]
        == opaque_ref(key, creative_version_id)
    )
    assert all(
        value.startswith("opaque:")
        for name, value in review["review"].items()
        if name.endswith("_handle")
    )
    for raw in (brief.account_ref, brief.campaign_handle, creative_version_id):
        assert raw not in shown.output

    export_verified(ledger, tmp_path / "export", opaque_key=key)
    exported = next(
        json.loads(line)
        for line in (tmp_path / "export" / "records.jsonl").read_text().splitlines()
        if json.loads(line)["record_kind"] == "CANDIDATE_COMPILED"
    )
    compilation = exported["payload"]["compilation"]
    assert compilation["account_ref"] == compilation["review"]["account_ref"]
    assert compilation["campaign_ref"] == compilation["review"]["campaign_handle"]
    assert (
        compilation["creative_references"][0]["creative_version_id"]
        == compilation["review"]["creative_version_id"]
    )
    exported_text = canonical_bytes(exported).decode()
    for raw in (brief.account_ref, brief.campaign_handle, creative_version_id):
        assert raw not in exported_text


def test_retention_receipt_reserves_the_newline_before_append_and_key_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The receipt uses the final newline-inclusive size before local persistence."""
    brief = meta_brief()
    compiled = compile_candidate(brief)
    command = "compile " + "x" * 1_000
    data = {
        "candidate_digest": compiled.candidate_digest,
        "spec_digest": compiled.spec_digest,
        "record_digest": "0" * 64,
        "maximum_spend": compiled.maximum_spend.model_dump(mode="json"),
        "persisted": True,
        "remote_side_effects": False,
        "requires_new_compilation_for_planning": True,
    }
    exact_limit = (
        len(
            canonical_bytes(
                ResultEnvelope(
                    tool_version=__version__,
                    command=command,
                    outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
                    data=data,
                )
            )
        )
        + 1
    )
    assert exact_limit >= 1_024
    limit = {"value": exact_limit}

    def configuration(_: Path) -> Configuration:
        return Configuration(output=OutputPreferences(max_output_bytes=limit["value"]))

    def forbidden_key() -> bytes:
        pytest.fail("candidate retention must not access the sharing key")

    monkeypatch.setattr(compile_cli, "load_configuration", configuration)
    monkeypatch.setattr(compile_cli, "export_key", forbidden_key)
    context = typer.Context(get_command(app), obj={"data_dir": tmp_path})
    compile_cli._retain_compilation(context, brief, command)
    assert len(capsys.readouterr().out.encode()) == exact_limit

    def candidate_records() -> list[dict[str, Any]]:
        return [
            record
            for record in Ledger(tmp_path).records("changes")
            if record["record_kind"] == "CANDIDATE_COMPILED"
        ]

    assert len(candidate_records()) == 1

    limit["value"] = exact_limit - 1

    def unexpected_retain(*_: object, **__: object) -> dict[str, object]:
        pytest.fail("an over-limit receipt must fail before append")

    monkeypatch.setattr(compile_cli, "retain_candidate", unexpected_retain)
    with pytest.raises(AdhelmError) as raised:
        compile_cli._retain_compilation(context, brief, command)
    assert raised.value.code == "OUTPUT_LIMIT_EXCEEDED"
    assert "no candidate was appended" in raised.value.next_action
    assert len(candidate_records()) == 1


def test_catalog_pages_twelve_mixed_candidates_with_no_omission_under_one_kib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adaptive page shrinking advances from emitted rows, not the requested limit."""
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1_024)))

    def forbidden_key() -> bytes:
        pytest.fail("candidate catalog must not access the sharing key")

    monkeypatch.setattr(candidate_cli, "export_key", forbidden_key)
    records = [
        retain_candidate(Ledger(tmp_path), _brief("meta_ads" if index % 2 else "google_ads"))
        for index in range(12)
    ]
    expected = [record["record_digest"] for record in reversed(records)]
    seen: list[str] = []
    cursor: str | None = "0"
    pages = 0
    while cursor is not None:
        result = CliRunner().invoke(
            app,
            ["--data-dir", str(tmp_path), "candidate", "catalog", "--cursor", cursor],
        )
        assert result.exit_code == 0, result.output
        assert len(result.output.encode()) <= 1_024
        envelope = json.loads(result.output)
        assert envelope["total_count"] == len(expected)
        seen.extend(item["record_digest"] for item in envelope["data"]["candidates"])
        cursor = envelope["next_cursor"]
        pages += 1
        assert pages <= len(expected)

    assert seen == expected
    assert len(set(seen)) == len(expected)


def test_invalid_retain_input_and_candidate_selection_do_not_read_a_sharing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public validation errors happen before a key or local candidate append."""
    raw = json.loads(meta_brief().model_dump_json())
    raw["unexpected_field"] = True
    input_file = tmp_path / "brief.json"
    input_file.write_bytes(canonical_bytes(raw))

    def forbidden(*_: object, **__: object) -> bytes:
        pytest.fail("invalid public input must not reach a private key or append")

    monkeypatch.setattr(compile_cli, "export_key", forbidden)
    monkeypatch.setattr(compile_cli, "retain_candidate", forbidden)
    monkeypatch.setattr(candidate_cli, "export_key", forbidden)
    runner = CliRunner()
    retained = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            "meta-ads",
            "--input",
            str(input_file),
            "--retain",
        ],
    )
    assert retained.exit_code == 2, retained.output
    assert json.loads(retained.output)["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert not (tmp_path / "data" / "ledger").exists()

    selected = runner.invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "candidate", "show", "not-a-record-digest"],
    )
    assert selected.exit_code == 2, selected.output
    assert json.loads(selected.output)["diagnostics"][0]["code"] == "INVALID_CANDIDATE_SELECTION"
