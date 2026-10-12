"""Installed offline preview boundaries for fixed Meta and Google candidates."""

import json
from pathlib import Path

import pytest
from test_google_ads_compiler import _brief as google_brief
from test_meta_compiler import _brief as meta_brief
from typer.testing import CliRunner

from adhelm import compile_cli
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.models import Configuration, OutputPreferences
from adhelm.providers.google_ads_compiler import compile_google_ads_search_campaign
from adhelm.providers.meta_compiler import compile_meta_ads_campaign


@pytest.mark.parametrize("provider", ["meta-ads", "google-ads"])
def test_installed_preview_preserves_proof_gaps_and_hides_customer_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setattr(compile_cli, "export_key", lambda: b"k" * 32)
    brief = meta_brief() if provider == "meta-ads" else google_brief()
    spec = (
        compile_meta_ads_campaign(brief)
        if provider == "meta-ads"
        else compile_google_ads_search_campaign(brief)
    )
    path = tmp_path / "brief.json"
    path.write_text(brief.model_dump_json())
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "compile", provider, "--input", str(path)],
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    data = envelope["data"]
    assert data["provider"] == spec.provider
    assert data["input_digest"] == spec.input_digest
    assert data["review"]["spec_digest"] == spec.spec_digest
    assert data["compiler_evidence"] == "FIXTURE_VERIFIED_LIVE_UNVERIFIED"
    assert data["persisted"] is False and data["remote_side_effects"] is False
    assert data["activation_requires_new_plan_and_authorization"] is True
    assert data["planning_boundary"] == spec.planning_boundary.model_dump(mode="json")
    expected = spec.redacted_review.model_dump(mode="json")
    for field, value in expected.items():
        if field.endswith("_handle") or field == "account_ref":
            output_field = (
                field.removesuffix("_handle") + "_ref" if field.endswith("_handle") else field
            )
            assert data["review"][output_field].startswith("opaque:")
            assert data["review"][output_field] != value
        elif field != "creative_version_id":
            assert data["review"][field] == value
    for private in (
        brief.account_ref,
        brief.campaign_name,
        brief.ad_name,
        brief.creative.destination_url,
    ):
        assert private not in result.output
    if provider == "meta-ads":
        for private in (
            brief.creative.page_id,
            brief.creative.image_reference.image_hash,
            brief.creative.primary_text,
            brief.creative.headline,
        ):
            assert private not in result.output
        assert data["review"]["maximum_spend"] == {"currency": "USD", "minor_units": 25_000}
        assert data["review"]["placement_eligibility_status"] == "REQUIRES_API_READBACK"
    else:
        for private in (
            *brief.creative.headlines,
            *brief.creative.descriptions,
            brief.keywords[0].text,
        ):
            assert private not in result.output
        assert data["review"]["campaign_total_budget"] == {"currency": "USD", "minor_units": 25_000}
        assert data["review"]["rsa_component_composition"] == "DYNAMIC_COMBINATION_UNKNOWN"
        assert data["review"]["component_level_attribution"] == "NOT_AVAILABLE_FROM_PROVIDER"
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("provider", ["meta-ads", "google-ads"])
def test_unknown_brief_fields_fail_before_key_access_or_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    def forbidden() -> bytes:
        pytest.fail("Invalid input must not access the private sharing key")

    monkeypatch.setattr(compile_cli, "export_key", forbidden)
    brief = (meta_brief() if provider == "meta-ads" else google_brief()).model_dump(mode="json")
    brief["customer_uploaded_audience"] = ["sensitive-fixture@example.test"]
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(brief))
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "compile", provider, "--input", str(path)],
    )
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert "sensitive-fixture" not in result.output
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("provider", ["meta-ads", "google-ads"])
def test_output_limit_fails_with_bounded_diagnostic_and_no_partial_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setattr(compile_cli, "export_key", lambda: b"k" * 32)
    save_configuration(
        tmp_path / "data", Configuration(output=OutputPreferences(max_output_bytes=1024))
    )
    path = tmp_path / "brief.json"
    path.write_text((meta_brief() if provider == "meta-ads" else google_brief()).model_dump_json())
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "compile", provider, "--input", str(path)],
    )
    assert result.exit_code == 3, result.output
    assert len(result.output.encode()) <= 1024
    assert json.loads(result.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert sorted(item.name for item in (tmp_path / "data").iterdir()) == ["config.json"]


def test_discovery_exposes_versioned_briefs_without_execution_commands() -> None:
    runner = CliRunner()
    discovery = json.loads(runner.invoke(app, ["discover"]).output)["data"]
    for command, schema in (
        ("compile meta-ads", "meta-campaign-brief"),
        ("compile google-ads", "google-ads-search-brief"),
    ):
        assert command in discovery["commands"] and schema in discovery["schemas"]
        output = runner.invoke(app, ["schema", schema])
        assert output.exit_code == 0, output.output
        properties = json.loads(output.output)["data"]["schema"]["properties"]
        assert properties["schema_version"]["const"] == 1
        assert "creative" in properties and "account_ref" in properties
    assert "apply" not in discovery["commands"]
