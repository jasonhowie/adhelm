"""Hermetic CLI coverage for the fixed TikTok and LinkedIn report adapters."""

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


def _profile(provider: str, account_ref: str, *, timezone: str = "UTC") -> Profile:
    return Profile(
        profile_id=f"{provider}.read",
        provider=provider,  # type: ignore[arg-type]
        account_ref=account_ref,
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["read"],
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


def test_tiktok_ads_cli_ad_report_preserves_exact_minor_spend_and_redacts_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile("tiktok_ads", "123")]))
    query_file = tmp_path / "tiktok-report.json"
    query_file.write_text(
        json.dumps(
            {
                "account_ref": "123",
                "report_id": "report.tiktok.ad",
                "query_contract_hash": "a" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
                "source_timezone": "UTC",
                "currency": "USD",
                "level": "AD",
                "maximum_rows": 2,
                "page_size": 2,
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["access-token"] == "oauth-fixture"
        if request.url.path.endswith("/advertiser/info/"):
            assert request.url.params["advertiser_ids"] == '["123"]'
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "OK",
                    "request_id": "fixture-request",
                    "data": {
                        "list": [
                            {
                                "advertiser_id": "123",
                                "currency": "USD",
                                "timezone": "UTC",
                                "status": "STATUS_ENABLE",
                            }
                        ]
                    },
                },
            )
        assert request.url.path.endswith("/report/integrated/get/")
        assert request.url.params["data_level"] == "AUCTION_AD"
        assert request.url.params["dimensions"] == '["stat_time_day","ad_id"]'
        assert json.loads(request.url.params["filtering"]) == [
            {
                "field_name": "ad_status",
                "filter_type": "IN",
                "filter_value": '["STATUS_ALL"]',
            }
        ]
        return httpx.Response(
            200,
            # Keep the provider's numeric decimal lexeme intact through JSON parsing.
            content=(
                b'{"code":0,"message":"OK","request_id":"fixture-request","data":'
                b'{"list":[{"dimensions":{"stat_time_day":"2026-09-01","ad_id":"13"},'
                b'"metrics":{"impressions":"4","clicks":"1","spend":0.75}}],'
                b'"page_info":{"page":1,"page_size":2,"total_number":1,"total_page":1}}}'
            ),
        )

    credentials: list[bool] = []
    _wire_http(monkeypatch, handler, credentials)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "report",
            "tiktok-ads",
            "tiktok_ads.read",
            "--input",
            str(query_file),
        ],
    )

    assert result.exit_code == 0, result.stdout
    output = json.loads(result.stdout)["data"]
    row = output["performance_rows"][0]
    assert row["metric_grain"] == "AD"
    assert row["resource_level"] == "AD"
    assert row["spend"] == {"currency": "USD", "minor_units": 75}
    assert row["data_quality_statuses"] == [
        "COMPLETE",
        "CREATIVE_NOT_EXPOSED",
        "COMBINATION_UNKNOWN",
    ]
    assert output["envelope"]["data_quality_statuses"] == [
        "COMPLETE",
        "NOT_AVAILABLE_FROM_PROVIDER",
    ]
    assert row["resource_ref"].startswith("opaque:")
    assert len(output["envelope"]["source_ledger_checkpoints"]) == 3
    assert output["retained_record_digest"]
    assert "tiktok_ads.ad.13" not in result.stdout
    assert "tiktok_ads.advertiser.123" not in result.stdout
    assert credentials == [False, False]

    records = _performance_records(tmp_path)
    assert len(records) == 1
    retained = records[0]["payload"]
    assert retained["performance_rows"][0]["resource_ref"] == "tiktok_ads.ad.13"  # type: ignore[index]
    assert retained["performance_rows"][0]["spend"] == {"minor_units": 75, "currency": "USD"}  # type: ignore[index]
    assert Ledger(tmp_path).verify()["status"] == "VERIFIED"


def test_linkedin_ads_cli_campaign_report_keeps_unknown_completeness_and_redacts_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_profile("linkedin_ads", "123")]))
    query_file = tmp_path / "linkedin-report.json"
    query_file.write_text(
        json.dumps(
            {
                "account_ref": "123",
                "report_id": "report.linkedin.campaign",
                "query_contract_hash": "b" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
                "source_timezone": "UTC",
                "currency": "USD",
                "level": "CAMPAIGN",
                "maximum_rows": 2,
                "page_size": 2,
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer oauth-fixture"
        assert request.headers["linkedin-version"] == "202608"
        assert request.headers["x-restli-protocol-version"] == "2.0.0"
        if request.url.path == "/rest/adAccounts/123":
            return httpx.Response(200, json={"id": 123, "currency": "USD", "status": "ACTIVE"})
        assert request.url.path == "/rest/adAnalytics"
        assert request.url.params["pivot"] == "CAMPAIGN"
        assert request.url.params["timeGranularity"] == "DAILY"
        assert request.url.params["dateRange"] == (
            "(start:(year:2026,month:9,day:1),end:(year:2026,month:9,day:1))"
        )
        assert "start" not in request.url.params and "count" not in request.url.params
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "clicks": 2,
                        "impressions": 10,
                        "costInLocalCurrency": "1.25",
                        "pivotValues": ["urn:li:sponsoredCampaign:456"],
                        "dateRange": {
                            "start": {"year": 2026, "month": 9, "day": 1},
                            "end": {"year": 2026, "month": 9, "day": 1},
                        },
                    }
                ],
                "paging": {"start": 0, "count": 1, "links": []},
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
            "linkedin-ads",
            "linkedin_ads.read",
            "--input",
            str(query_file),
        ],
    )

    assert result.exit_code == 0, result.stdout
    output = json.loads(result.stdout)["data"]
    row = output["performance_rows"][0]
    assert row["metric_grain"] == "CAMPAIGN"
    assert row["resource_level"] == "CAMPAIGN"
    assert row["spend"] == {"currency": "USD", "minor_units": 125}
    assert row["provider_extensions"] == {
        "linkedin_ads_cost_in_local_currency": "1.25",
        "linkedin_ads_rounding": "HALF_EVEN_TO_MINOR",
    }
    assert row["resource_ref"].startswith("opaque:")
    assert output["envelope"]["source_timezone"] == "UTC"
    assert output["envelope"]["report_timezone"] == "UTC"
    assert output["envelope"]["data_quality_statuses"] == ["NOT_AVAILABLE_FROM_PROVIDER"]
    assert "COMPLETE" not in output["envelope"]["data_quality_statuses"]
    assert len(output["envelope"]["source_ledger_checkpoints"]) == 3
    assert output["retained_record_digest"]
    assert "linkedin_ads.campaign.456" not in result.stdout
    assert "linkedin_ads.account.123" not in result.stdout
    assert credentials == [False, False]

    records = _performance_records(tmp_path)
    assert len(records) == 1
    retained = records[0]["payload"]
    assert retained["performance_rows"][0]["resource_ref"] == "linkedin_ads.campaign.456"  # type: ignore[index]
    assert retained["performance_rows"][0]["provider_extensions"] == row["provider_extensions"]  # type: ignore[index]
    assert Ledger(tmp_path).verify()["status"] == "VERIFIED"


def test_linkedin_ads_cli_requires_a_utc_read_profile_before_credentials_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(
        tmp_path,
        Configuration(profiles=[_profile("linkedin_ads", "123", timezone="America/Chicago")]),
    )
    query_file = tmp_path / "linkedin-report.json"
    query_file.write_text(
        json.dumps(
            {
                "account_ref": "123",
                "report_id": "report.linkedin.campaign",
                "query_contract_hash": "c" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
                "source_timezone": "UTC",
            }
        )
    )
    credential_calls: list[bool] = []
    monkeypatch.setattr(
        workflows,
        "credential",
        lambda *_args, secondary=False, **_kwargs: credential_calls.append(secondary),
    )

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "report",
            "linkedin-ads",
            "linkedin_ads.read",
            "--input",
            str(query_file),
        ],
    )

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["diagnostics"][0]["code"] == "TIMEZONE_MISMATCH"
    assert credential_calls == []
    assert _performance_records(tmp_path) == []
