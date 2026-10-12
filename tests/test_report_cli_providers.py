"""Hermetic provider report CLI integration tests with fixed adapter transports."""

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


def _profile(provider: str, account_ref: str, *, google: bool = False) -> Profile:
    return Profile(
        profile_id=f"{provider}.read",
        provider=provider,  # type: ignore[arg-type]
        account_ref=account_ref,
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["read"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="oauth.fixture",
        secondary_credential_ref="developer.fixture" if google else None,
        login_customer_ref="0987654321" if google else None,
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
        return "developer-fixture" if secondary else "oauth-fixture"

    monkeypatch.setattr(workflows, "credential", credentials)
    monkeypatch.setattr(workflows, "export_key", lambda: b"r" * 32)


def _report_records(root: Path) -> list[dict[str, object]]:
    return [
        record
        for record in Ledger(root).records("performance")
        if record["record_kind"] == "PERFORMANCE_OBSERVATION"
    ]


def test_google_ads_cli_report_preserves_exact_micros_and_hides_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(
        tmp_path, Configuration(profiles=[_profile("google_ads", "1234567890", google=True)])
    )
    query_file = tmp_path / "google-report.json"
    query_file.write_text(
        json.dumps(
            {
                "account_ref": "1234567890",
                "report_id": "report.google.ad",
                "query_contract_hash": "a" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
                "source_timezone": "UTC",
                "currency": "USD",
                "level": "AD",
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v25/customers/1234567890/googleAds:search"
        assert request.headers["authorization"] == "Bearer oauth-fixture"
        assert request.headers["developer-token"] == "developer-fixture"
        assert request.headers["login-customer-id"] == "0987654321"
        gaql = json.loads(request.content)["query"]
        if "customer.currency_code" in gaql:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customer": {
                                "id": "1234567890",
                                "currencyCode": "USD",
                                "timeZone": "UTC",
                            }
                        }
                    ]
                },
            )
        assert "FROM ad_group_ad" in gaql
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "campaign": {"id": "11"},
                        "adGroup": {"id": "21"},
                        "adGroupAd": {"ad": {"id": "31"}},
                        "segments": {"date": "2026-09-01"},
                        "metrics": {"impressions": "3", "clicks": "1", "costMicros": "5001"},
                    }
                ]
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
            "google-ads",
            "google_ads.read",
            "--input",
            str(query_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    output = json.loads(result.stdout)["data"]
    row = output["performance_rows"][0]
    assert row["metric_grain"] == "AD"
    assert row["resource_level"] == "AD"
    assert row["resource_ref"].startswith("opaque:")
    assert row["parent_resource_ref"].startswith("opaque:")
    assert row["data_quality_statuses"] == ["COMPLETE", "CREATIVE_NOT_EXPOSED"]
    assert row["spend"] == {"currency": "USD", "minor_units": 1}
    assert row["provider_extensions"] == {
        "google_ads_cost_micros": "5001",
        "google_ads_rounding": "HALF_EVEN_TO_MINOR",
    }
    assert len(output["envelope"]["source_ledger_checkpoints"]) == 3
    assert output["retained_record_digest"]
    assert "1234567890" not in result.stdout
    assert "google_ads.ad.31" not in result.stdout
    # One fixed Google read snapshots both credentials before discovery so a
    # protected OAuth refresh cannot change headers between identity/report calls.
    assert credentials == [False, True]

    records = _report_records(tmp_path)
    assert len(records) == 1
    retained = records[0]["payload"]
    assert retained["performance_rows"][0]["resource_ref"] == "google_ads.ad.31"  # type: ignore[index]
    assert retained["performance_rows"][0]["provider_extensions"] == row["provider_extensions"]  # type: ignore[index]
    assert Ledger(tmp_path).verify()["status"] == "VERIFIED"


def test_ga4_report_snapshots_the_primary_credential_across_identity_and_report_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile("ga4", "123")]))
    query_file = tmp_path / "ga4-report.json"
    query_file.write_text(
        json.dumps(
            {
                "property_id": "123",
                "account_ref": "123",
                "report_id": "report.ga4.snapshot",
                "query_contract_hash": "c" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
                "metrics": ["sessions"],
                "source_timezone": "UTC",
                "currency": "USD",
            }
        )
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer oauth-fixture"
        if request.url.host == "analyticsadmin.googleapis.com" and request.url.path.endswith(
            "/properties/123"
        ):
            return httpx.Response(
                200,
                json={"name": "properties/123", "timeZone": "UTC", "currencyCode": "USD"},
            )
        if request.url.host == "analyticsadmin.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "dataStreams": [
                        {
                            "name": "properties/123/dataStreams/456",
                            "type": "WEB_DATA_STREAM",
                        }
                    ]
                },
            )
        assert request.url.host == "analyticsdata.googleapis.com"
        return httpx.Response(
            200,
            json={
                "dimensionHeaders": [],
                "metricHeaders": [{"name": "sessions"}],
                "rows": [{"dimensionValues": [], "metricValues": [{"value": "2"}]}],
                "rowCount": 1,
                "metadata": {"timeZone": "UTC", "currencyCode": "USD"},
            },
        )

    credential_calls: list[bool] = []
    _wire_http(monkeypatch, handler, credential_calls)
    result = workflows.run_report(
        tmp_path,
        "ga4",
        "ga4.read",
        query_file,
        stream_id="456",
    )
    assert len(result["performance_rows"]) == 1
    assert len(requests) == 3
    # Discovery and the Data API report use one bearer snapshot, so a protected
    # refresh cannot change the identity/report binding within this observation.
    assert credential_calls == [False]


def test_meta_ads_cli_report_retains_ad_grain_and_shares_opaque_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile("meta_ads", "123")]))
    query_file = tmp_path / "meta-report.json"
    query_file.write_text(
        json.dumps(
            {
                "account_ref": "123",
                "report_id": "report.meta.ad",
                "query_contract_hash": "b" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
                "source_timezone": "UTC",
                "currency": "USD",
                "level": "AD",
                "action_attribution_windows": ["7d_click", "1d_view"],
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer oauth-fixture"
        if request.url.path == "/v26.0/act_123":
            return httpx.Response(
                200,
                json={"id": "123", "account_status": 1, "currency": "USD", "timezone_name": "UTC"},
            )
        assert request.url.path == "/v26.0/act_123/insights"
        assert request.url.params["level"] == "ad"
        assert request.url.params["action_attribution_windows"] == '["7d_click","1d_view"]'
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "campaign_id": "11",
                        "adset_id": "21",
                        "ad_id": "31",
                        "date_start": "2026-09-01",
                        "date_stop": "2026-09-01",
                        "impressions": "4",
                        "clicks": "2",
                        "spend": "1.25",
                        "reach": "3",
                    }
                ]
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
            "meta-ads",
            "meta_ads.read",
            "--input",
            str(query_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    output = json.loads(result.stdout)["data"]
    row = output["performance_rows"][0]
    assert row["metric_grain"] == "AD"
    assert row["resource_level"] == "AD"
    assert row["resource_ref"].startswith("opaque:")
    assert row["campaign_ref"].startswith("opaque:")
    assert row["spend"] == {"currency": "USD", "minor_units": 125}
    assert row["data_quality_statuses"] == [
        "COMPLETE",
        "CREATIVE_NOT_EXPOSED",
        "COMBINATION_UNKNOWN",
    ]
    assert "meta_ads.ad.31" not in result.stdout
    assert output["envelope"]["source_account_ref"].startswith("opaque:")
    # A short account number can occur by chance inside a valid SHA-256 digest.
    assert '"123"' not in result.stdout
    assert credentials == [False, False]

    records = _report_records(tmp_path)
    assert len(records) == 1
    retained = records[0]["payload"]
    assert retained["performance_rows"][0]["metric_grain"] == "AD"  # type: ignore[index]
    assert Ledger(tmp_path).verify()["status"] == "VERIFIED"
