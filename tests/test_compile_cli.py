import json
from pathlib import Path

import pytest
import typer
from test_openai_compiler import _brief
from typer.testing import CliRunner

from adhelm import compile_cli
from adhelm.cli import app as installed_app
from adhelm.config import save_configuration
from adhelm.models import Configuration, OutputPreferences
from adhelm.providers.openai_compiler import compile_openai_ads_campaign


def _app(tmp_path: Path) -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def context(ctx: typer.Context) -> None:
        ctx.obj = {"data_dir": tmp_path / "data"}

    app.add_typer(compile_cli.app, name="compile")
    return app


def test_installed_cli_discovers_and_invokes_the_offline_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    discovery = runner.invoke(installed_app, ["discover"])
    data = json.loads(discovery.output)["data"]
    assert "compile openai-ads" in data["commands"]
    assert "openai-campaign-brief" in data["schemas"]
    schema = runner.invoke(installed_app, ["schema", "openai-campaign-brief"])
    assert schema.exit_code == 0, schema.output
    assert "file_reference" in schema.output
    monkeypatch.setattr(compile_cli, "export_key", lambda: b"k" * 32)
    path = tmp_path / "brief.json"
    path.write_text(_brief().model_dump_json())
    result = runner.invoke(
        installed_app,
        ["--data-dir", str(tmp_path / "data"), "compile", "openai-ads", "--input", str(path)],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["outcome"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert not (tmp_path / "data").exists()


def test_candidate_cli_preserves_spend_and_digest_without_exposing_serving_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compile_cli, "export_key", lambda: b"k" * 32)
    brief = _brief()
    path = tmp_path / "brief.json"
    path.write_text(brief.model_dump_json())
    result = CliRunner().invoke(_app(tmp_path), ["compile", "openai-ads", "--input", str(path)])
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["outcome"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    data = output["data"]
    assert data["persisted"] is False
    assert data["remote_side_effects"] is False
    assert data["planning_boundary"]["requires_verified_account_identity"] is True
    assert data["planning_boundary"]["requires_creative_ledger_check"] is True
    assert data["planning_boundary"]["requires_account_bound_file_readback"] is True
    assert data["review"]["spec_digest"] == compile_openai_ads_campaign(brief).spec_digest
    assert data["review"]["maximum_spend"] == {"currency": "USD", "minor_units": 2500}
    assert data["review"]["account_ref"].startswith("opaque:")
    assert data["review"]["campaign_ref"].startswith("opaque:")
    for private in (
        brief.account_ref,
        brief.campaign_handle,
        brief.ad_groups[0].ads[0].creative.title,
        brief.ad_groups[0].ads[0].creative.body,
        brief.ad_groups[0].ads[0].creative.destination_url,
        brief.ad_groups[0].ads[0].creative.file_reference.file_id,
    ):
        assert private not in result.output
    assert not (tmp_path / "data").exists()


def test_unsupported_candidate_fails_before_keychain_or_any_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden() -> bytes:
        pytest.fail("Invalid candidate must fail before private sharing-key access")

    monkeypatch.setattr(compile_cli, "export_key", forbidden)
    brief = _brief().model_dump(mode="json")
    brief["bidding_type"] = "conversions"
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(brief))
    result = CliRunner().invoke(_app(tmp_path), ["compile", "openai-ads", "--input", str(path)])
    assert result.exit_code == 2
    output = json.loads(result.output)
    assert output["diagnostics"][0]["code"] == "UNSUPPORTED_CONVERSION_OPTIMIZATION"
    assert not (tmp_path / "data").exists()


def test_candidate_cli_enforces_output_size_without_retaining_a_partial_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compile_cli, "export_key", lambda: b"k" * 32)
    save_configuration(
        tmp_path / "data", Configuration(output=OutputPreferences(max_output_bytes=1024))
    )
    path = tmp_path / "brief.json"
    path.write_text(_brief().model_dump_json())
    result = CliRunner().invoke(_app(tmp_path), ["compile", "openai-ads", "--input", str(path)])
    assert result.exit_code == 3
    assert json.loads(result.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert sorted(item.name for item in (tmp_path / "data").iterdir()) == ["config.json"]
