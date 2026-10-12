"""Independent adversarial CLI checks for static Google Ads PAUSE plan artifacts."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from test_google_ads_pause_cli import _action
from test_google_ads_pause_plan_artifact_cli import _retain, _widen_output
from typer.testing import CliRunner

import adhelm.cli as root_cli
from adhelm import credentials, google_ads_pause_artifact_cli
from adhelm.ledger import Ledger
from adhelm.providers.google_ads_pause_contract import build_google_ads_campaign_pause_plan
from adhelm.saved_google_ads_pause_plans import retain_google_ads_campaign_pause_plan


def _invoke(directory: Path, *arguments: str) -> Any:
    return CliRunner().invoke(
        root_cli.app,
        ["--data-dir", str(directory), "artifacts", "google-ads-campaign-pause", *arguments],
    )


def _envelope(result: Any) -> dict[str, Any]:
    value = json.loads(result.output)
    assert type(value) is dict
    return cast(dict[str, Any], value)


def _append_distinct_local_plan(directory: Path) -> None:
    """Advance only the ledger checkpoint through another closed local plan."""
    base = _action()
    action = build_google_ads_campaign_pause_plan(
        workspace_profile_ref="default",
        account_binding=base.account_binding,
        execution_binding=base.execution_binding,
        observation_binding=base.observation_binding,
        expires_at=base.plan.expires_at + timedelta(seconds=1),
    )
    retain_google_ads_campaign_pause_plan(Ledger(directory), action)


@pytest.mark.parametrize(
    "contents",
    (
        b'{"schema_version":1}',
        b'{"schema_version":1,"formats":[]}',
        b'{"schema_version":1,"formats":["json","json"]}',
        b'{"schema_version":1,"formats":["json"],"replacement_authorized":1}',
        b'{"schema_version":1,"formats":["json"],"action":{}}',
        b'{"schema_version":1,"formats":["pdf"]}',
        b'{"schema_version":2,"formats":["json"]}',
    ),
)
def test_malformed_historical_request_never_reaches_runtime_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: bytes,
) -> None:
    """A missing/aliased format cannot trigger config, history, or key access."""
    request = tmp_path / "private-request.json"
    request.write_bytes(contents)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("invalid artifact request reached a runtime boundary")

    monkeypatch.setattr(google_ads_pause_artifact_cli, "load_configuration", forbidden)
    monkeypatch.setattr(google_ads_pause_artifact_cli, "_load_source", forbidden)
    monkeypatch.setattr(credentials, "export_key", forbidden)

    result = _invoke(tmp_path, "0" * 64, "--input", str(request))

    assert result.exit_code == 2, result.output
    diagnostic = _envelope(result)["diagnostics"][0]
    assert diagnostic["code"] == "SCHEMA_VALIDATION_FAILED"
    assert request.name not in result.output


@pytest.mark.parametrize("destination_kind", ("symlink", "fifo"))
def test_historical_cli_rejects_unsafe_destination_before_sharing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination_kind: str,
    make_link,
    make_pipe,
) -> None:
    """An explicit history request cannot make a special destination reachable."""
    record_digest = _retain(tmp_path)
    destination = tmp_path / destination_kind
    outside = tmp_path / "outside"
    if destination_kind == "symlink":
        outside.mkdir(mode=0o700)
        make_link(destination, outside)
    else:
        destination = make_pipe(destination_kind)
    request = tmp_path / "unsafe-artifact-request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "formats": ["json"],
                "output_directory": str(destination),
            }
        ),
        encoding="utf-8",
    )

    def forbidden() -> bytes:
        pytest.fail("unsafe destination reached sharing-key access")

    monkeypatch.setattr(credentials, "export_key", forbidden)
    result = _invoke(tmp_path, record_digest, "--input", str(request))

    assert result.exit_code == 3, result.output
    assert _envelope(result)["diagnostics"][0]["code"] == "UNSAFE_ARTIFACT_DESTINATION"
    if destination_kind == "symlink":
        assert list(outside.iterdir()) == []


def test_real_checkpoint_drift_between_reloads_blocks_key_and_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later retained plan changes the held checkpoint before final handoff."""
    record_digest = _retain(tmp_path)
    _widen_output(tmp_path)
    destination = tmp_path / "artifacts"
    destination.mkdir(mode=0o700)
    request = tmp_path / "historical-artifact-request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "formats": ["json"],
                "output_directory": str(destination),
            }
        ),
        encoding="utf-8",
    )
    original_load = google_ads_pause_artifact_cli._load_source
    calls = 0

    def reload_after_checkpoint_change(
        directory: Path, selected: str
    ) -> tuple[object, dict[str, Any], object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            _append_distinct_local_plan(directory)
        return original_load(directory, selected)

    def forbidden() -> bytes:
        pytest.fail("changed checkpoint reached sharing-key access")

    monkeypatch.setattr(
        google_ads_pause_artifact_cli, "_load_source", reload_after_checkpoint_change
    )
    monkeypatch.setattr(credentials, "export_key", forbidden)
    result = _invoke(tmp_path, record_digest, "--input", str(request))

    assert result.exit_code == 2, result.output
    assert (
        _envelope(result)["diagnostics"][0]["code"]
        == "GOOGLE_ADS_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID"
    )
    assert calls == 2
    assert list(destination.iterdir()) == []


def test_discovery_and_closed_schemas_are_runtime_free_and_noninjectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only command discovery cannot load retained sources or local keys."""

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("static CLI discovery reached a runtime boundary")

    monkeypatch.setattr(google_ads_pause_artifact_cli, "load_configuration", forbidden)
    monkeypatch.setattr(google_ads_pause_artifact_cli, "_load_source", forbidden)
    monkeypatch.setattr(credentials, "export_key", forbidden)
    runner = CliRunner()

    discovery = runner.invoke(root_cli.app, ["discover"])
    assert discovery.exit_code == 0, discovery.output
    data = _envelope(discovery)["data"]
    assert "artifacts google-ads-campaign-pause" in data["commands"]
    assert (
        data["google_ads_campaign_pause"]["artifact_generation"]
        == "STATIC_REDACTED_LOCAL_HISTORY_ONLY"
    )
    assert data["google_ads_campaign_pause"]["execution"] == "UNRELEASED"

    request_schema = runner.invoke(
        root_cli.app,
        ["schema", "google-ads-campaign-pause-plan-artifact-render", "--root-only"],
    )
    assert request_schema.exit_code == 0, request_schema.output
    schema = _envelope(request_schema)["data"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "schema_version",
        "formats",
        "output_directory",
        "replacement_authorized",
    }
    serialized = json.dumps(schema, sort_keys=True)
    for forbidden_name in (
        "action",
        "provenance",
        "credential",
        "challenge",
        "receipt",
        "native",
        "customer",
    ):
        assert forbidden_name not in serialized
