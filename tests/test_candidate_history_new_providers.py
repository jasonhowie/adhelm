"""Retained Meta/Google compilation reviews preserve limits without native payloads."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_google_ads_compiler import _brief as google_brief
from test_meta_compiler import _brief as meta_brief
from typer.testing import CliRunner

from adhelm import candidate_cli, compile_cli
from adhelm.candidate_records import CandidateCompilationRecord
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences
from adhelm.projections import export_verified
from adhelm.saved_candidates import (
    candidate_catalog,
    compile_candidate,
    load_candidate,
    retain_candidate,
)


def test_google_candidate_rejects_matching_money_outside_canonical_native_range() -> None:
    compiled = compile_candidate(google_brief())
    payload = compiled.model_dump(mode="json")
    money = {"minor_units": 900_719_925_475, "currency": "USD"}
    payload["maximum_spend"] = money
    payload["review"]["campaign_total_budget"] = money
    payload["native_total_budget_micros"] = money["minor_units"] * 10_000
    with pytest.raises(ValidationError, match="native_total_budget_micros"):
        type(compiled).model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("provider", ["meta-ads", "google-ads"])
def test_retained_review_preserves_provider_gates_and_never_native_serving_payloads(
    tmp_path: Path, provider: str
) -> None:
    brief = meta_brief() if provider == "meta-ads" else google_brief()
    compiled = compile_candidate(brief)
    record = retain_candidate(Ledger(tmp_path / "data"), brief)
    loaded, provenance = load_candidate(Ledger(tmp_path / "data"), record["record_digest"])
    assert loaded == compiled
    assert provenance["candidate_digest"] == compiled.candidate_digest
    assert loaded.planning_boundary == compiled.planning_boundary
    assert loaded.maximum_spend.minor_units == 25_000
    assert loaded.requires_new_compilation_for_planning
    assert loaded.activation_requires_new_digest_and_authorization
    assert not loaded.remote_side_effects
    assert loaded.compiler_evidence == "FIXTURE_VERIFIED_LIVE_UNVERIFIED"
    assert not Ledger(tmp_path / "data").unresolved_runs()
    body = canonical_bytes(record["payload"]).decode()
    for private in (brief.campaign_name, brief.ad_name, brief.creative.destination_url):
        assert private not in body
    if provider == "meta-ads":
        assert loaded.native_lifetime_budget_minor_units == 25_000
        for private in (
            brief.creative.page_id,
            brief.creative.image_reference.image_hash,
            brief.creative.primary_text,
        ):
            assert private not in body
    else:
        assert loaded.native_total_budget_micros == 250_000_000
        assert loaded.review.rsa_component_composition == "DYNAMIC_COMBINATION_UNKNOWN"
        for private in (
            *brief.creative.headlines,
            *brief.creative.descriptions,
            brief.keywords[0].text,
        ):
            assert private not in body
    assert '"request":' not in body
    export_verified(Ledger(tmp_path / "data"), tmp_path / "export", opaque_key=b"k" * 32)
    exported = (tmp_path / "export/records.jsonl").read_text()
    assert brief.account_ref not in exported
    exported_record = next(
        json.loads(line)
        for line in exported.splitlines()
        if json.loads(line)["record_kind"] == "CANDIDATE_COMPILED"
    )
    review = exported_record["payload"]["compilation"]["review"]
    assert all(
        value.startswith("opaque:") for name, value in review.items() if name.endswith("_handle")
    )
    assert compiled.candidate_digest in exported


@pytest.mark.parametrize("provider", ["meta-ads", "google-ads"])
def test_minimum_receipt_is_key_free_and_full_review_is_bounded_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    def forbidden() -> bytes:
        pytest.fail("Receipt and over-limit review must not access the sharing key")

    monkeypatch.setattr(compile_cli, "export_key", forbidden)
    monkeypatch.setattr(candidate_cli, "export_key", forbidden)
    data_dir = tmp_path / "data"
    save_configuration(data_dir, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    brief = meta_brief() if provider == "meta-ads" else google_brief()
    path = tmp_path / "brief.json"
    path.write_text(brief.model_dump_json())
    base = ["--data-dir", str(data_dir)]
    runner = CliRunner()
    receipt = runner.invoke(app, [*base, "compile", provider, "--input", str(path), "--retain"])
    assert receipt.exit_code == 0, receipt.output
    assert len(receipt.output.encode()) <= 1024
    data = json.loads(receipt.output)["data"]
    assert data["persisted"] is True and data["remote_side_effects"] is False
    record_digest = data["record_digest"]
    too_small = runner.invoke(app, [*base, "candidate", "show", record_digest])
    assert too_small.exit_code == 3, too_small.output
    assert "OUTPUT_LIMIT_EXCEEDED" in too_small.output
    save_configuration(
        data_dir, Configuration(output=OutputPreferences(max_output_bytes=8192)), replace=True
    )
    monkeypatch.setattr(candidate_cli, "export_key", lambda: b"k" * 32)
    shown = runner.invoke(app, [*base, "candidate", "show", record_digest])
    assert shown.exit_code == 0, shown.output
    view = json.loads(shown.output)["data"]
    assert view["account_ref"].startswith("opaque:")
    assert view["review"]["campaign_handle"].startswith("opaque:")
    assert view["planning_boundary"]["requires_drift_check_and_authorization"] is True
    assert view["creative_references"][0]["reference_status"] == "DECLARED_NOT_VERIFIED_IN_LEDGER"


@pytest.mark.parametrize("provider", ["meta-ads", "google-ads"])
@pytest.mark.parametrize("tamper", ["budget", "account", "creative", "gate", "timezone", "handles"])
def test_rehashed_invalid_review_fails_before_history(
    tmp_path: Path, provider: str, tamper: str
) -> None:
    compiled = compile_candidate(meta_brief() if provider == "meta-ads" else google_brief())
    value = compiled.model_dump(mode="json")
    if tamper == "budget":
        value["maximum_spend"]["minor_units"] += 1
    elif tamper == "account":
        value["review"]["account_ref"] = "account.substituted"
    elif tamper == "creative":
        value["creative_references"][0]["creative_content_digest"] = "0" * 64
    elif tamper == "gate":
        value["planning_boundary"]["requires_drift_check_and_authorization"] = False
    elif tamper == "timezone":
        value["review"]["account_timezone"] = "not-an-iana-zone"
    else:
        value["review"]["ad_handle"] = value["review"]["campaign_handle"]
    payload = {
        "schema_version": 1,
        "event_type": "CANDIDATE_COMPILED",
        "compilation": value,
        "candidate_digest": digest(value),
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


def test_mixed_catalog_adapts_to_byte_bound_without_skipping_records(tmp_path: Path) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    records = [
        retain_candidate(Ledger(tmp_path), brief)
        for brief in (meta_brief(), google_brief(), meta_brief())
    ]
    expected = [record["record_digest"] for record in reversed(records)]
    seen = []
    cursor = "0"
    while cursor is not None:
        result = CliRunner().invoke(
            app, ["--data-dir", str(tmp_path), "candidate", "catalog", "--cursor", cursor]
        )
        assert result.exit_code == 0, result.output
        assert len(result.output.encode()) <= 1024
        envelope = json.loads(result.output)
        assert envelope["total_count"] == 3
        seen.extend(item["record_digest"] for item in envelope["data"]["candidates"])
        cursor = envelope["next_cursor"]
    assert seen == expected
    with pytest.raises(AdhelmError, match="Cursor exceeds"):
        candidate_catalog(Ledger(tmp_path), cursor=4)


def test_schema_discriminator_includes_only_installed_retained_provider_variants() -> None:
    properties = CandidateCompilationRecord.model_json_schema()["properties"]
    assert set(properties["compilation"]["discriminator"]["mapping"]) == {
        "openai_ads",
        "meta_ads",
        "google_ads",
        "linkedin_ads",
        "reddit_ads",
        "tiktok_ads",
    }
