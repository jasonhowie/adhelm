"""GTM command-to-HTTP-to-ledger behavior using synthetic provider data only."""

import json
from pathlib import Path

import httpx
import pytest
from test_gtm_measurement_workflow import (
    _account,
    _configure,
    _container,
    _header,
    _install_credential,
    _install_transport,
    _profile,
    _version,
)
from typer.testing import CliRunner

from adhelm import measurement_cli
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OutputPreferences
from adhelm.projections import export_verified
from adhelm.saved_measurements import load_measurement, measurement_catalog, measurement_rows


def _http_fixture(monkeypatch: pytest.MonkeyPatch, *, failure: str | None = None) -> list[str]:
    paths: list[str] = []
    header_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal header_reads
        paths.append(request.url.path)
        assert request.method == "GET" and request.url.host == "tagmanager.googleapis.com"
        if request.url.path == "/tagmanager/v2/accounts/1":
            return httpx.Response(200, json=_account())
        if request.url.path == "/tagmanager/v2/accounts/1/containers/2":
            return httpx.Response(
                200, json=_container(account_id="9" if failure == "identity" else "1")
            )
        if request.url.path.endswith("/version_headers:latest"):
            header_reads += 1
            return httpx.Response(
                200, json=_header("9" if failure == "drift" and header_reads == 2 else "8")
            )
        if request.url.path.endswith("/versions:live") or request.url.path.endswith("/versions/8"):
            value = _version("7" if request.url.path.endswith("/versions:live") else "8")
            value["tag"] = [
                {"tagId": str(i), "type": "html", "parameter": [{"value": "private-html-source"}]}
                for i in range(12)
            ]
            value["customTemplate"] = [
                {"templateId": "1", "templateData": "private-template-source"}
            ]
            return httpx.Response(200, json=value)
        if request.url.path.endswith("/workspaces"):
            if request.url.params.get("pageToken") is not None:
                assert failure == "late-page"
                return httpx.Response(200, json={"unsupported": "private-late-payload"})
            value = {
                "workspace": [
                    {
                        "path": f"accounts/1/containers/2/workspaces/{i}",
                        "accountId": "1",
                        "containerId": "2",
                        "workspaceId": str(i),
                        "name": "private-workspace-name",
                    }
                    for i in (9, 3)
                ]
            }
            if failure == "late-page":
                value["nextPageToken"] = "next"
            return httpx.Response(200, json=value)
        raise AssertionError(request.url.path)

    _install_transport(monkeypatch, handler)
    _install_credential(monkeypatch, [])
    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"k" * 32)
    return paths


def _args(root: Path) -> list[str]:
    return ["--data-dir", str(root), "measurement"]


def test_full_read_retains_only_normalized_metadata_then_pages_all_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(
        tmp_path,
        Configuration(profiles=[_profile()], output=OutputPreferences(max_output_bytes=1024)),
    )
    paths = _http_fixture(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(app, [*_args(tmp_path), "gtm", "gtm-read", "2", "--ci-enabled"])
    assert result.exit_code == 0, result.output
    assert len(result.output.encode()) <= 1024
    receipt = json.loads(result.output)["data"]
    assert receipt["row_count"] == 30 and receipt["remote_mutations"] is False
    assert len(paths) == 8
    observation, provenance = load_measurement(Ledger(tmp_path), receipt["record_digest"])
    assert observation.snapshot.provider == "gtm"
    assert observation.snapshot.identity.container_ref == "gtm.container.1.2"
    assert observation.snapshot.inventory.workspaces[0].workspace_ref == "gtm.workspace.1.2.3"
    assert observation.snapshot.row_count == len(measurement_rows(observation))
    assert provenance["record_digest"] == receipt["record_digest"]
    ledger_text = "".join(p.read_text() for p in (tmp_path / "ledger").rglob("*.jsonl"))
    for private in ("private-html", "private-template", "private-workspace", "fixture-token"):
        assert private not in ledger_text and private not in result.output
    save_configuration(
        tmp_path,
        Configuration(profiles=[_profile()], output=OutputPreferences(max_output_bytes=6144)),
        replace=True,
    )
    cursor, rows, pages = "0", [], 0
    while True:
        shown = runner.invoke(
            app, [*_args(tmp_path), "show", receipt["record_digest"], "--cursor", cursor]
        )
        assert shown.exit_code == 0, shown.output
        assert len(shown.output.encode()) <= 6144
        envelope = json.loads(shown.output)
        data = envelope["data"]
        assert data["dependency_completeness"] == "NOT_PROVEN"
        assert data["staging_eligibility"] == data["processed_receipt"] == "NOT_EVALUATED"
        assert data["account_features"]["support_multiple_containers"] == "UNKNOWN"
        assert data["latest_header_count_status"]["status"] == "HEADER_COUNTS_UNAVAILABLE"
        assert data["latest_header_count_status"]["missing_counter_fields"]
        assert data["identity"]["container_ref"].startswith("opaque:")
        assert data["observation_status"] == "SEQUENTIALLY_REREAD_NONATOMIC"
        assert "gtm.container.1.2" not in shown.output
        rows.extend(data["rows"])
        pages += 1
        if envelope["next_cursor"] is None:
            break
        assert int(envelope["next_cursor"]) == len(rows)
        cursor = envelope["next_cursor"]
    assert pages > 1 and len(rows) == 30 and len(paths) == 8
    assert [r["resource_type"] for r in rows].count("GTM_OPAQUE_CODE") == 26
    assert all("opaque_code" not in row for row in rows)
    export_verified(Ledger(tmp_path), tmp_path / "export", opaque_key=b"k" * 32)
    exported = (tmp_path / "export/records.jsonl").read_text()
    assert "gtm.container.1.2" not in exported and "private-html" not in exported
    assert receipt["snapshot_digest"] in exported


@pytest.mark.parametrize("failure", ["identity", "late-page", "drift"])
def test_failed_remote_read_never_retains_partial_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _configure(tmp_path, _profile())
    _http_fixture(monkeypatch, failure=failure)
    result = CliRunner().invoke(app, [*_args(tmp_path), "gtm", "gtm-read", "2", "--ci-enabled"])
    assert result.exit_code != 0, result.output
    assert json.loads(result.output)["outcome"] == "FAILED_CLOSED"
    assert measurement_catalog(Ledger(tmp_path))["total_count"] == 0
    assert "private-" not in result.output


def test_unconfigured_optional_gtm_fails_before_key_or_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden():
        raise AssertionError("optional missing configuration must not access the sharing key")

    monkeypatch.setattr(measurement_cli, "export_key", forbidden)
    result = CliRunner().invoke(app, [*_args(tmp_path), "gtm", "missing", "2"])
    assert result.exit_code != 0
    assert json.loads(result.output)["diagnostics"][0]["code"] == "NOT_CONFIGURED_OPTIONAL"
    assert not (tmp_path / "ledger").exists()
    assert CliRunner().invoke(app, ["--data-dir", str(tmp_path), "discover"]).exit_code == 0


def test_gtm_command_and_versioned_input_schema_are_discoverable() -> None:
    runner = CliRunner()
    discovery = json.loads(runner.invoke(app, ["discover"]).output)["data"]
    assert "measurement gtm" in discovery["commands"]
    capability = next(p for p in discovery["providers"] if p["provider"] == "gtm")
    assert capability["implementation"] == "FIXTURE_VERIFIED_READ_SUBSET"
    assert capability["live_verified"] is False
    schema = runner.invoke(app, ["schema", "measurement-gtm-read"])
    assert schema.exit_code == 0, schema.output
    properties = json.loads(schema.output)["data"]["schema"]["properties"]
    assert properties["schema_version"]["const"] == 1
    assert "profile_id" in properties and "container_id" in properties
