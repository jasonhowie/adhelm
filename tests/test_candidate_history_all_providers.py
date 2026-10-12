"""All six candidate variants keep review evidence without manufacturing authority."""

import json
from collections.abc import Callable
from decimal import localcontext
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_google_ads_compiler import _brief as google_brief
from test_linkedin_compiler import _brief as linkedin_brief
from test_meta_compiler import _brief as meta_brief
from test_openai_compiler import _brief as openai_brief
from test_reddit_compiler import _brief as reddit_brief
from test_tiktok_compiler import _brief as tiktok_brief
from typer.testing import CliRunner

from adhelm import additional_compile_cli, candidate_cli, compile_cli
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

_NEW = (linkedin_brief, reddit_brief, tiktok_brief)
_ALL = (openai_brief, meta_brief, google_brief, *_NEW)


@pytest.mark.parametrize("factory", _NEW)
def test_retention_roundtrip_preserves_closed_provider_review_and_budget(
    tmp_path: Path, factory: Callable[[], Any]
) -> None:
    brief = factory()
    compiled = compile_candidate(brief)
    record = retain_candidate(Ledger(tmp_path), brief)
    loaded, provenance = load_candidate(Ledger(tmp_path), record["record_digest"])
    assert canonical_bytes(loaded) == canonical_bytes(compiled)
    assert provenance["candidate_digest"] == compiled.candidate_digest
    assert loaded.review.maximum_spend == loaded.maximum_spend
    assert loaded.execution_status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert loaded.compiler_evidence == "FIXTURE_VERIFIED_LIVE_UNVERIFIED"
    checks = loaded.planning_boundary.model_dump(mode="json")
    assert all(value is True for key, value in checks.items() if key.startswith("requires_"))
    assert loaded.creative_references[0].reference_status == "DECLARED_NOT_VERIFIED_IN_LEDGER"
    if loaded.provider == "linkedin_ads":
        assert loaded.native_group_total_budget_amount == "250.00"
        assert loaded.review.initial_state == "DRAFT"
    elif loaded.provider == "reddit_ads":
        assert loaded.native_lifetime_spend_micros == 25_000_000
        assert loaded.review.initial_state == "PAUSED"
    else:
        assert loaded.native_adgroup_total_budget_amount == "250.00"
        assert loaded.review.initial_state == "DISABLE"
        assert loaded.review.video_asset_status == "DECLARED_UNVERIFIED_NO_LOCAL_DECODE"
        assert (
            loaded.review.video_execution_eligibility == "REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET"
        )
    checkpoint = Ledger(tmp_path).checkpoint()
    assert {
        row["record_kind"] for rows in checkpoint["records_by_stream"].values() for row in rows
    } == {"SEGMENT_STARTED", "CANDIDATE_COMPILED"}
    assert not Ledger(tmp_path).unresolved_starts()


@pytest.mark.parametrize("factory", _NEW)
def test_retained_history_and_exports_exclude_native_source_material(
    tmp_path: Path, factory: Callable[[], Any]
) -> None:
    brief = factory()
    record = retain_candidate(Ledger(tmp_path / "data"), brief)
    retained = canonical_bytes(record["payload"]).decode()
    private = (
        "urn:li:",
        "Customer supplied",
        "Fixture engagement group",
        "Fixture dark-post campaign",
        "Customer Reddit",
        "Customer US feed",
        "Customer existing post",
        "p2_fixture_pixel",
        "fi_fixture_funding",
        "t3_fixturepost",
        "https://example.test",
        "Fixture TikTok",
        "Fixture video caption",
        "opaque-tiktok-video",
        "opaque-tiktok-identity",
    )
    for text in private:
        assert text not in retained
    assert "source_brief" not in retained and "campaign_request" not in retained
    export_verified(Ledger(tmp_path / "data"), tmp_path / "export", opaque_key=b"k" * 32)
    exported = (tmp_path / "export" / "records.jsonl").read_text()
    for name, value in brief.model_dump(mode="json").items():
        if name.endswith(("_ref", "_handle")):
            assert json.dumps(value) not in exported
    for text in private:
        assert text not in exported
    assert record["payload"]["candidate_digest"] in exported


@pytest.mark.parametrize("factory", _NEW)
@pytest.mark.parametrize(
    "mutation", ("identity", "handle", "native_budget", "float", "boolean", "video")
)
def test_rehashed_forged_review_fails_before_history(
    tmp_path: Path, factory: Callable[[], Any], mutation: str
) -> None:
    compiled = compile_candidate(factory()).model_dump(mode="json")
    if mutation == "identity":
        compiled["review"]["account_ref"] = "account:wrong"
    elif mutation == "handle":
        role = next(
            key
            for key in compiled["review"]
            if key.endswith("_handle") and key != "campaign_handle"
        )
        compiled["review"][role] = compiled["review"]["campaign_handle"]
    elif mutation == "native_budget":
        field = next(key for key in compiled if key.startswith("native_"))
        compiled[field] = "250.01" if isinstance(compiled[field], str) else compiled[field] + 1
    elif mutation == "float":
        compiled["maximum_spend"]["minor_units"] = float(compiled["maximum_spend"]["minor_units"])
    elif mutation == "boolean":
        compiled["planning_boundary"]["requires_creative_ledger_check"] = 1
    else:
        compiled["review"]["video_asset_status"] = "VERIFIED"
    payload = {"compilation": compiled, "candidate_digest": digest(compiled)}
    with pytest.raises(AdhelmError, match="schema"):
        Ledger(tmp_path).append(
            "changes",
            "CANDIDATE_COMPILED",
            payload,
            provider=compiled["provider"],
            account_ref=compiled["account_ref"],
        )
    assert not (tmp_path / "ledger").exists()


@pytest.mark.parametrize("factory", _NEW)
def test_cli_retention_needs_no_sharing_key_and_respects_minimum_receipt_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any]
) -> None:
    def forbidden() -> bytes:
        pytest.fail("Retention must not access sharing keys")

    monkeypatch.setattr(compile_cli, "export_key", forbidden)
    monkeypatch.setattr(additional_compile_cli, "export_key", forbidden)
    save_configuration(
        tmp_path / "data", Configuration(output=OutputPreferences(max_output_bytes=1024))
    )
    brief = factory()
    path = tmp_path / "brief.json"
    path.write_text(brief.model_dump_json())
    provider = compile_candidate(brief).provider.replace("_", "-")
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            provider,
            "--input",
            str(path),
            "--retain",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(result.output.encode()) <= 1024
    body = json.loads(result.output)
    assert body["outcome"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert body["data"]["persisted"] is True and body["data"]["remote_side_effects"] is False
    assert brief.account_ref not in result.output
    loaded, _ = load_candidate(Ledger(tmp_path / "data"), body["data"]["record_digest"])
    assert loaded.provider.replace("_", "-") == provider


def test_mixed_provider_catalog_and_shared_review_are_bounded_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(candidate_cli, "export_key", lambda: b"k" * 32)
    records = [retain_candidate(Ledger(tmp_path), factory()) for factory in _ALL]
    providers = []
    cursor = 0
    while True:
        page = candidate_catalog(Ledger(tmp_path), cursor=cursor, limit=2)
        providers.extend(row["provider"] for row in page["candidates"])
        if page["next_cursor"] is None:
            break
        cursor = int(page["next_cursor"])
    assert providers == [record["provider"] for record in reversed(records)]
    for record in records:
        result = CliRunner().invoke(
            app, ["--data-dir", str(tmp_path), "candidate", "show", record["record_digest"]]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["account_ref"].startswith("opaque:")
        assert data["campaign_ref"].startswith("opaque:")
        for name, value in data.get("review", {}).items():
            if name.endswith(("_handle", "_ref")):
                assert value.startswith("opaque:")


@pytest.mark.parametrize("factory", _NEW)
def test_show_keeps_mandatory_checks_when_saved_limit_is_too_small(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any]
) -> None:
    def forbidden() -> bytes:
        pytest.fail("Oversized review must fail before real sharing-key access")

    monkeypatch.setattr(candidate_cli, "export_key", forbidden)
    record = retain_candidate(Ledger(tmp_path), factory())
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    result = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "candidate", "show", record["record_digest"]]
    )
    assert result.exit_code != 0
    assert json.loads(result.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert candidate_catalog(Ledger(tmp_path))["total_count"] == 1


def test_reviewed_m13_candidates_remain_readable_without_legacy_recompilation() -> None:
    # The fixture is a synthetic M13 compilation captured from this exact
    # archive. M28 deliberately derives a stricter current rendition identity,
    # so compiling the current Meta test brief again must not be presented as
    # the older candidate's digest.
    expected_openai = "d78d10a55bcf1e2bc6e67e7fa578d7b0bccc6db01a2ae4b13728b1f901af83f8"
    expected_meta = "87d2379b21f314f882898e994be3b8ac97ea90219a8d32aa1b12089d8cc42b43"
    expected_google = "53a237dfee255440ba945a8aeb1e7ce6b7485d14ea030a55e7b62d028a4548e2"
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "m13-meta-candidate-compiled-v1.json").read_text()
    )
    assert fixture["fixture_schema_version"] == 1
    assert fixture["source_commit"] == "dccc7af5fd88198a177c443f96ed6c2c7a6fc5ca"
    assert fixture["source_candidate_digest"] == expected_meta
    raw_record = canonical_bytes(fixture["candidate_record"])
    record = CandidateCompilationRecord.model_validate_json(raw_record)

    assert record.candidate_digest == expected_meta
    assert canonical_bytes(record.model_dump(mode="json", warnings="error")) == raw_record
    assert compile_candidate(openai_brief()).candidate_digest == expected_openai
    assert compile_candidate(google_brief()).candidate_digest == expected_google
    assert compile_candidate(meta_brief()).candidate_digest != expected_meta


def test_native_budget_validation_does_not_depend_on_decimal_context() -> None:
    records = []
    for factory in (linkedin_brief, tiktok_brief):
        compiled = compile_candidate(factory())
        records.append(
            CandidateCompilationRecord(
                compilation=compiled, candidate_digest=compiled.candidate_digest
            ).model_dump_json()
        )
    with localcontext() as context:
        context.prec = 1
        for record in records:
            CandidateCompilationRecord.model_validate_json(record)


@pytest.mark.parametrize("factory", _NEW)
def test_bypassed_invalid_source_declaration_is_recompiled_before_retention(
    tmp_path: Path, factory: Callable[[], Any]
) -> None:
    brief = factory().model_copy(update={"account_currency": "EUR"})
    with pytest.raises((ValidationError, AdhelmError)):
        retain_candidate(Ledger(tmp_path), brief)
    assert not (tmp_path / "ledger").exists()
