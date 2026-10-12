"""Actual CLI-to-ledger workflows; provider and OS-store boundaries are mocked."""

import json
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image
from typer.testing import CliRunner

from adhelm import workflows
from adhelm.assets import store_image
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile

runner = CliRunner()


def png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 4), "#124578").save(buffer, format="PNG")
    return buffer.getvalue()


def test_image_persistence_and_ephemeral_mode(tmp_path: Path) -> None:
    media = tmp_path / "input.wrong-extension"
    media.write_bytes(png())
    root = tmp_path / "private"
    command = ["--data-dir", str(root), "creative", "analyze", "--input", str(media)]
    ephemeral = runner.invoke(app, [*command, "--ephemeral"])
    assert ephemeral.exit_code == 0, ephemeral.stdout
    assert not root.exists()
    persisted = runner.invoke(app, command)
    assert persisted.exit_code == 0, persisted.stdout
    result = json.loads(persisted.stdout)["data"]
    assert result["mime_type"] == "image/png"
    assert result["observation_persisted"] is True
    assert (root / "assets" / "sha256" / result["asset_content_digest"]).read_bytes() == png()
    records = Ledger(root).records("creative")
    assert [r["record_kind"] for r in records[1:]] == [
        "CREATIVE_ASSET_VERSION",
        "CREATIVE_ASSET_ANALYSIS",
    ]
    assert Ledger(root).checkpoint()["status"] == "VERIFIED_CHECKPOINT"
    assert "input.wrong-extension" not in persisted.stdout


def test_corrupt_asset_is_not_replaced(tmp_path: Path) -> None:
    import hashlib

    content = png()
    digest = hashlib.sha256(content).hexdigest()
    stored = store_image(tmp_path, content, digest)
    stored.write_bytes(b"damaged")
    with pytest.raises(AdhelmError, match="digest verification"):
        store_image(tmp_path, content, digest)
    assert stored.read_bytes() == b"damaged"


def test_doctor_reports_recovery_without_exposing_resource(tmp_path: Path) -> None:
    Ledger(tmp_path).append(
        "changes",
        "CHANGE_EVENT",
        {
            "event_type": "APPLY_STARTED",
            "operation": "CREATE",
            "origin": "LOCAL_CLI",
            "resource_type": "CAMPAIGN",
            "resource_ref": "private-resource",
            "effect_status": "NO_REMOTE_WRITE",
            "idempotency_ref": "attempt:one",
        },
    )
    for command in (["doctor"], ["history", "verify"]):
        result = runner.invoke(app, ["--data-dir", str(tmp_path), *command])
        assert result.exit_code == 3
        assert json.loads(result.stdout)["data"]["mutation_readiness"] == "RECOVERY_REQUIRED"
        assert "private-resource" not in result.stdout


@pytest.mark.parametrize("level", ["CAMPAIGN", "AD"])
def test_report_cli_persists_revisions_and_shares_only_opaque_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    profile = Profile(
        profile_id="openai-reader",
        provider="openai_ads",
        account_ref="account_fixture",
        principal_ref="principal",
        client_ref="client",
        scopes=["ads:read"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="credential-reference",
    )
    save_configuration(tmp_path, Configuration(profiles=[profile]))
    query_path = tmp_path / "report-input.json"
    query_path.write_text(
        json.dumps(
            {
                "account_ref": "account_fixture",
                "level": level,
                "report_id": "report:one",
                "query_contract_hash": "a" * 64,
                "window_start": "2026-01-01",
                "window_end": "2026-01-01",
                "source_timezone": "UTC",
                "currency": "USD",
            }
        )
    )
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/ad_account":
            return httpx.Response(
                200,
                json={
                    "id": "account_fixture",
                    "name": "Fixture",
                    "status": "active",
                    "timezone": "UTC",
                    "currency_code": "USD",
                },
            )
        assert request.url.path == "/v1/ad_account/insights"
        assert request.url.params["aggregation_level"] == level.lower()
        return httpx.Response(
            200,
            json={
                "object": "list",
                "count": 1,
                "data": [
                    {
                        "id": "row_fixture",
                        "readable_time": "2026-01-01",
                        "start_time": 1767225600,
                        "end_time": 1767312000,
                        "campaign_id": "campaign_fixture",
                        **({"ad_id": "ad_fixture"} if level == "AD" else {}),
                        "impressions": 2,
                        "clicks": 1,
                        "spend": 1.25,
                    }
                ],
                "has_more": False,
                "first_id": "row_fixture",
                "last_id": "row_fixture",
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        workflows.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr(workflows, "credential", lambda *args, **kwargs: "fixture-key")
    monkeypatch.setattr(workflows, "export_key", lambda: b"p" * 32)
    command = [
        "--data-dir",
        str(tmp_path),
        "report",
        "openai-ads",
        "openai-reader",
        "--input",
        str(query_path),
    ]
    for _ in range(2):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.stdout
        output = json.loads(result.stdout)["data"]
        assert output["performance_rows"][0]["spend"]["minor_units"] == 125
        assert output["performance_rows"][0]["metric_grain"] == level
        assert "ad_fixture" not in result.stdout
        assert len(output["envelope"]["source_ledger_checkpoints"]) == 3
        assert "account_fixture" not in result.stdout
        assert "campaign_fixture" not in result.stdout
        assert output["envelope"]["query_contract_hash"] != "a" * 64
    records = [
        r
        for r in Ledger(tmp_path).records("performance")
        if r["record_kind"] == "PERFORMANCE_OBSERVATION"
    ]
    assert [r["payload"]["observation_revision"] for r in records] == [1, 2]
    assert records[1]["payload"]["supersedes_record_id"] == records[0]["record_id"]
    ephemeral = runner.invoke(app, [*command, "--ephemeral"])
    assert ephemeral.exit_code == 0, ephemeral.stdout
    assert len(Ledger(tmp_path).records("performance")) == 3
    assert all(r.method == "GET" for r in calls)
