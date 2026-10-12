"""Installed offline previews preserve each provider's unresolved evidence gates."""

import json
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text
from typer.testing import CliRunner

from adhelm import additional_compile_cli
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.models import Configuration, OutputPreferences

_PROVIDERS = ("linkedin", "reddit", "tiktok")


def _brief(provider: str) -> Any:
    return import_module(f"test_{provider}_compiler")._brief()


def _run(tmp_path: Path, provider: str, path: Path, *extra: str) -> Any:
    return CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            provider + "-ads",
            "--input",
            str(path),
            *extra,
        ],
    )


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_preview_keeps_gates_money_and_status_without_private_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    brief = _brief(provider)
    spec = getattr(
        import_module(f"adhelm.providers.{provider}_compiler"), f"compile_{provider}_ads_campaign"
    )(brief)
    path = tmp_path / "brief.json"
    path.write_text(brief.model_dump_json())
    monkeypatch.setattr(additional_compile_cli, "export_key", lambda: b"k" * 32)
    result = _run(tmp_path, provider, path)
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    data = envelope["data"]
    assert envelope["outcome"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert data["input_digest"] == spec.input_digest
    assert data["review"]["spec_digest"] == spec.spec_digest
    assert data["review"]["maximum_spend"] == spec.maximum_spend.model_dump(mode="json")
    assert data["planning_boundary"] == spec.planning_boundary.model_dump(mode="json")
    assert data["persisted"] is False and data["remote_side_effects"] is False
    assert data["activation_requires_new_plan_and_authorization"] is True
    for field, value in spec.redacted_review.model_dump(mode="json").items():
        if field.endswith("_handle") or field in {"account_ref", "creative_version_id"}:
            output_field = (
                field.removesuffix("_handle") + "_ref" if field.endswith("_handle") else field
            )
            assert data["review"][output_field].startswith("opaque:")
            assert json.dumps(value) not in result.output
        else:
            assert data["review"][field] == value
    assert brief.campaign_name not in result.output
    assert '"campaign_request"' not in result.output
    assert '"creative"' not in result.output
    if provider == "linkedin":
        assert brief.account_urn not in result.output
        assert brief.creative.existing_post.commentary not in result.output
        assert data["review"]["initial_state"] == "DRAFT"
        assert data["review"]["existing_post_status"] == "CUSTOMER_DECLARED_REQUIRES_READBACK"
    else:
        assert brief.creative.destination_url not in result.output
    if provider == "tiktok":
        assert data["review"]["video_asset_status"] == "DECLARED_UNVERIFIED_NO_LOCAL_DECODE"
        assert (
            data["review"]["video_execution_eligibility"]
            == "REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET"
        )
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_preview_rejects_invalid_input_before_key_access_or_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    def forbidden() -> bytes:
        pytest.fail("Invalid input must not access the sharing key")

    monkeypatch.setattr(additional_compile_cli, "export_key", forbidden)
    data = _brief(provider).model_dump(mode="json")
    data["audience_upload"] = ["private-fixture@example.test"]
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data))
    result = _run(tmp_path, provider, path)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert "private-fixture" not in result.output
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_exact_output_bound_includes_newline_and_precedes_key_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    path = tmp_path / "brief.json"
    path.write_text(_brief(provider).model_dump_json())
    monkeypatch.setattr(additional_compile_cli, "export_key", lambda: b"k" * 32)
    first = _run(tmp_path, provider, path)
    assert first.exit_code == 0, first.output
    exact = len(first.output.encode())
    save_configuration(
        tmp_path / "data", Configuration(output=OutputPreferences(max_output_bytes=exact))
    )
    accepted = _run(tmp_path, provider, path)
    assert accepted.exit_code == 0 and len(accepted.output.encode()) == exact
    save_configuration(
        tmp_path / "data",
        Configuration(output=OutputPreferences(max_output_bytes=exact - 1)),
        replace=True,
    )

    def forbidden() -> bytes:
        pytest.fail("An over-limit preview must not access the sharing key")

    monkeypatch.setattr(additional_compile_cli, "export_key", forbidden)
    rejected = _run(tmp_path, provider, path)
    assert rejected.exit_code == 3, rejected.output
    assert json.loads(rejected.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert sorted(p.name for p in (tmp_path / "data").iterdir()) == ["config.json"]


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_discovery_exposes_versioned_schema_and_optional_retention(provider: str) -> None:
    runner = CliRunner()
    discovery = json.loads(runner.invoke(app, ["discover"]).output)["data"]
    assert f"compile {provider}-ads" in discovery["commands"]
    name = f"{provider}-campaign-brief"
    assert name in discovery["schemas"]
    result = runner.invoke(app, ["schema", name])
    assert result.exit_code == 0, result.output
    schema = json.loads(result.output)["data"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == 1
    assert "account_ref" in schema["properties"] and "creative" in schema["properties"]
    assert (
        "--retain"
        in Text.from_ansi(runner.invoke(app, ["compile", f"{provider}-ads", "--help"]).output).plain
    )
