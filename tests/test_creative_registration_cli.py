"""Installed CLI coverage for closed local creative registration."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from typer.testing import CliRunner

from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.models import Configuration, OutputPreferences

_RUNNER = CliRunner()


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 1), "#123456").save(output, format="PNG")
    return output.getvalue()


def _invoke(directory: Path, *arguments: str) -> tuple[object, dict[str, Any]]:
    result = _RUNNER.invoke(app, ["--data-dir", str(directory), *arguments])
    assert result.exit_code == 0, result.output
    return result, json.loads(result.output)["data"]


def _request(source: dict[str, object], *, kind: str = "LOCAL_IMAGE") -> dict[str, object]:
    return {
        "schema_version": 1,
        "creative_family_id": "family:private-id",
        "family_label": "Private campaign family",
        "provider_format": kind,
        "locale": "en-US",
        "destination_ref": "destination:private",
        "components": [
            {
                "component_id": "component:headline",
                "component_role": "HEADLINE",
                "ordinal": 0,
                "exact_text": "Private marketing copy",
                "normalized_text": "Private marketing copy",
                "language": "en-US",
                "provider_slot": "headline",
                "provenance": "DECLARED",
                "confidence_basis_points": 10_000,
                "extraction_status": "DECLARED",
            }
        ],
        "created_at": "2026-01-01T00:00:00.123456Z",
        "source": source,
    }


def test_cli_image_registration_is_retained_idempotent_redacted_and_minimum_bound(
    tmp_path: Path,
) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    image_path = tmp_path / "identity.png"
    image_path.write_bytes(_png())

    analyze_result, analysis = _invoke(tmp_path, "creative", "analyze", "--input", str(image_path))
    assert analysis["observation_persisted"] is True
    request_path = tmp_path / "registration.json"
    request_path.write_text(
        json.dumps(
            _request(
                {
                    "registration_kind": "IDENTITY_IMAGE",
                    "asset_record_digest": analysis["asset_record_digest"],
                    "asset_analysis_record_digest": analysis["asset_analysis_record_digest"],
                }
            )
        ),
        encoding="utf-8",
    )

    register_result, first = _invoke(tmp_path, "creative", "register", "--input", str(request_path))
    retry_result, second = _invoke(tmp_path, "creative", "register", "--input", str(request_path))
    assert first["local_registration_status"] == "RETAINED_VERIFIED_LOCAL_ONLY"
    assert first["registration_recovery_status"] == "REGISTERED"
    assert first["version_created_at"] == "2026-01-01T00:00:00.123456Z"
    assert first["version_time_basis"] == "DECLARED_LOCAL_VERSION_TIME"
    assert first["rendition_id"].startswith("rendition:sha256:")
    assert first["fingerprint_id"].startswith("fingerprint:sha256:")
    assert first["remote_mutations"] is False
    assert first["execution_authority"] is False
    assert first["deployment_status"] == "NOT_CREATED"
    assert first["provider_currentness"] == "NOT_EVALUATED"
    assert second["registration_recovery_status"] == "ALREADY_REGISTERED"
    assert second["record_digest"] == first["record_digest"]
    for output in (analyze_result.output, register_result.output, retry_result.output):
        for secret_or_free_text in (
            "family:private-id",
            "Private campaign family",
            "Private marketing copy",
            "destination:private",
        ):
            assert secret_or_free_text not in output

    save_configuration(
        tmp_path,
        Configuration(output=OutputPreferences(max_output_bytes=4096)),
        replace=True,
    )
    _, catalog = _invoke(tmp_path, "creative", "catalog")
    _, shown = _invoke(tmp_path, "creative", "show", first["record_digest"])
    assert catalog["versions"][0]["record_digest"] == first["record_digest"]
    assert shown["creative_version_id"] == first["creative_version_id"]
    assert shown["final_rendition_evidence"] == "FINAL_RENDITION_EVIDENCE_RETAINED"
    for value in (catalog, shown):
        rendered = json.dumps(value, sort_keys=True)
        assert "Private marketing copy" not in rendered
        assert "Private campaign family" not in rendered
        assert "destination:private" not in rendered


def test_cli_text_registration_and_schema_are_closed(tmp_path: Path) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    request_path = tmp_path / "text.json"
    request_path.write_text(
        json.dumps(_request({"registration_kind": "TEXT_ONLY"}, kind="LOCAL_TEXT")),
        encoding="utf-8",
    )

    _, receipt = _invoke(tmp_path, "creative", "register", "--input", str(request_path))
    assert receipt["registration_kind"] == "TEXT_ONLY"
    assert receipt["rendition_id"] is None
    assert receipt["fingerprint_id"] is None
    schema_result = _RUNNER.invoke(app, ["schema", "creative-registration-request", "--root-only"])
    assert schema_result.exit_code == 0, schema_result.output
    schema = json.loads(schema_result.output)["data"]
    assert "IDENTITY_IMAGE" in json.dumps(schema)
    assert "TEXT_ONLY" in json.dumps(schema)


def test_cli_register_rejects_raw_schema_alias_before_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(
        json.dumps({**_request({"registration_kind": "TEXT_ONLY"}), "schema_version": 1.0}),
        encoding="utf-8",
    )
    called = {"value": False}

    def unexpected_registration(*_args: object, **_kwargs: object) -> object:
        called["value"] = True
        raise AssertionError("registration must not run for invalid input")

    monkeypatch.setattr("adhelm.cli.register_creative_request", unexpected_registration)
    result = _RUNNER.invoke(
        app, ["--data-dir", str(tmp_path), "creative", "register", "--input", str(invalid_path)]
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert called["value"] is False


def test_cli_analysis_output_preflight_precedes_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    image_path = tmp_path / "identity.png"
    image_path.write_bytes(_png())
    called = {"value": False}

    def unexpected_retention(*_args: object, **_kwargs: object) -> object:
        called["value"] = True
        raise AssertionError("retention must follow the output preflight")

    monkeypatch.setattr("adhelm.cli._fits", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("adhelm.cli.retain_local_image_analysis", unexpected_retention)
    result = _RUNNER.invoke(
        app, ["--data-dir", str(tmp_path), "creative", "analyze", "--input", str(image_path)]
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert called["value"] is False
