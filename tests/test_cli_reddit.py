"""Hermetic CLI coverage for Reddit Ads fixed report reads."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from adhelm import workflows
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile

runner = CliRunner()
ResponseHandler = Callable[[httpx.Request], httpx.Response]


def _profile(*, timezone: str = "America/Chicago") -> Profile:
    return Profile(
        profile_id="reddit_ads.read",
        provider="reddit_ads",
        account_ref="a2_fixture",
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["adsread"],
        purpose="READ",
        currency="USD",
        timezone=timezone,
        credential_ref="oauth.fixture",
    )


def _wire_http(
    monkeypatch: pytest.MonkeyPatch, handler: ResponseHandler, credential_calls: list[bool]
) -> None:
    actual_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: actual_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    def credentials(*_: object, secondary: bool = False, **__: object) -> str:
        credential_calls.append(secondary)
        return "oauth-fixture"

    monkeypatch.setattr(workflows, "credential", credentials)
    monkeypatch.setattr(workflows, "export_key", lambda: b"r" * 32)


def _performance_records(root: Path) -> list[dict[str, object]]:
    return [
        record
        for record in Ledger(root).records("performance")
        if record["record_kind"] == "PERFORMANCE_OBSERVATION"
    ]


def test_reddit_ads_cli_ad_report_retains_normalized_ledger_and_hmac_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    query_file = tmp_path / "reddit-report.json"
    query_file.write_text(
        json.dumps(
            {
                "account_ref": "a2_fixture",
                "report_id": "report.reddit.ad",
                "query_contract_hash": "a" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
                "currency": "USD",
                "level": "AD",
                "maximum_rows": 2,
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer oauth-fixture"
        if request.url.path == "/api/v3/ad_accounts/a2_fixture":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "a2_fixture",
                        "currency": "USD",
                        "time_zone_id": "America/Chicago",
                    }
                },
            )
        assert request.method == "POST"
        assert request.url.path == "/api/v3/ad_accounts/a2_fixture/reports"
        body = json.loads(request.content)
        assert body["data"]["breakdowns"] == ["ad_id", "date"]
        assert body["data"]["fields"] == ["impressions", "clicks", "spend"]
        assert body["data"]["time_zone_id"] == "UTC"
        return httpx.Response(
            200,
            json={
                "data": {
                    "metrics": [
                        {
                            "ad_id": "ad1",
                            "date": "2026-09-01",
                            "impressions": 4,
                            "clicks": 2,
                            "spend": 20_001,
                        }
                    ],
                    "metrics_updated_at": "2026-09-02T00:00:00+00:00",
                }
            },
        )

    credentials: list[bool] = []
    _wire_http(monkeypatch, handler, credentials)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "report",
            "reddit-ads",
            "reddit_ads.read",
            "--input",
            str(query_file),
        ],
    )

    assert result.exit_code == 0, result.stdout
    output = json.loads(result.stdout)["data"]
    row = output["performance_rows"][0]
    assert row["metric_grain"] == "AD"
    assert row["resource_level"] == "AD"
    assert row["campaign_ref"] is None
    assert row["spend"] == {"currency": "USD", "minor_units": 2}
    assert row["provider_extensions"] == {
        "reddit_ads_spend_micros": "20001",
        "reddit_ads_rounding": "HALF_EVEN_TO_MINOR",
    }
    assert row["resource_ref"].startswith("opaque:")
    assert output["envelope"]["source_timezone"] == "UTC"
    assert output["envelope"]["report_timezone"] == "UTC"
    assert output["envelope"]["data_quality_statuses"] == [
        "PROCESSING_PENDING",
        "NOT_AVAILABLE_FROM_PROVIDER",
    ]
    assert len(output["envelope"]["source_ledger_checkpoints"]) == 3
    assert output["retained_record_digest"]
    assert "reddit_ads.ad.ad1" not in result.stdout
    assert "reddit_ads.account.a2_fixture" not in result.stdout
    assert credentials == [False, False]

    records = _performance_records(tmp_path)
    assert len(records) == 1
    retained = records[0]["payload"]
    assert retained["performance_rows"][0]["resource_ref"] == "reddit_ads.ad.ad1"  # type: ignore[index]
    assert retained["performance_rows"][0]["spend"] == {"minor_units": 2, "currency": "USD"}  # type: ignore[index]
    assert Ledger(tmp_path).verify()["status"] == "VERIFIED"


def test_reddit_ads_cli_rejects_account_timezone_drift_before_report_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile(timezone="UTC")]))
    query_file = tmp_path / "reddit-report.json"
    query_file.write_text(
        json.dumps(
            {
                "account_ref": "a2_fixture",
                "report_id": "report.reddit.campaign",
                "query_contract_hash": "b" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
            }
        )
    )
    report_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal report_requests
        if request.url.path == "/api/v3/ad_accounts/a2_fixture":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "a2_fixture",
                        "currency": "USD",
                        "time_zone_id": "America/Chicago",
                    }
                },
            )
        report_requests += 1
        return httpx.Response(500)

    credentials: list[bool] = []
    _wire_http(monkeypatch, handler, credentials)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "report",
            "reddit-ads",
            "reddit_ads.read",
            "--input",
            str(query_file),
        ],
    )

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["diagnostics"][0]["code"] == "IDENTITY_MISMATCH"
    assert report_requests == 0
    assert credentials == [False]
    assert _performance_records(tmp_path) == []
