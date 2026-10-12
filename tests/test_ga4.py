import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.providers.ga4 import Ga4DataClient, Ga4RealtimeQuery, Ga4ReportQuery
from adhelm.transport import SafeHttpClient


def _response(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=body)


def _client(handler: httpx.MockTransport) -> Ga4DataClient:
    http = SafeHttpClient(
        httpx.Client(transport=handler),
        {"analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"},
        read_attempts=1,
    )
    return Ga4DataClient(
        http,
        lambda: "test-token-not-to-be-persisted",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="test",
    )


def _property() -> dict[str, Any]:
    return {
        "name": "properties/123",
        "timeZone": "America/Chicago",
        "currencyCode": "USD",
    }


def _streams() -> dict[str, Any]:
    return {
        "dataStreams": [
            {
                "name": "properties/123/dataStreams/456",
                "type": "WEB_DATA_STREAM",
            }
        ]
    }


def _report(rows: list[dict[str, Any]], row_count: int = 2) -> dict[str, Any]:
    return {
        "dimensionHeaders": [
            {"name": "sessionCampaignId"},
            {"name": "sessionSource"},
        ],
        "metricHeaders": [{"name": "sessions"}, {"name": "keyEvents"}],
        "rows": rows,
        "rowCount": row_count,
        "metadata": {"timeZone": "America/Chicago", "currencyCode": "USD"},
    }


def test_identity_and_complete_bounded_run_report_pagination() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer test-token-not-to-be-persisted"
        if request.url.host == "analyticsadmin.googleapis.com" and request.url.path.endswith(
            "/properties/123"
        ):
            return _response(_property())
        if request.url.host == "analyticsadmin.googleapis.com":
            return _response(_streams())
        offset = json.loads(request.content).get("offset")
        if offset == "0":
            return _response(
                _report(
                    [
                        {
                            "dimensionValues": [{"value": "77"}, {"value": "google"}],
                            "metricValues": [{"value": "2"}, {"value": "1"}],
                        }
                    ]
                )
            )
        return _response(
            _report(
                [
                    {
                        "dimensionValues": [{"value": "78"}, {"value": "linkedin"}],
                        "metricValues": [{"value": "3"}, {"value": "0"}],
                    }
                ]
            )
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123", expected_web_stream_id="456")
    result = client.run_report(
        Ga4ReportQuery(
            property_id="123",
            account_ref=identity.property_ref,
            report_id="report.1",
            query_contract_hash="a" * 64,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 2),
            dimensions=["sessionCampaignId", "sessionSource"],
            metrics=["sessions", "keyEvents"],
            source_timezone="America/Chicago",
        ),
        identity=identity,
    )
    assert identity.property_client_ref == "123"
    assert len(result.performance_rows) == 2
    assert result.performance_rows[0].sessions == 2
    assert result.performance_rows[0].provider_extensions == {"ga4_sessionSource": "google"}
    assert len(calls) == 4


def test_unsafe_report_dimension_fails_before_transport() -> None:
    with pytest.raises(ValidationError):
        Ga4ReportQuery(
            property_id="123",
            account_ref="account.1",
            report_id="report.1",
            query_contract_hash="a" * 64,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 2),
            dimensions=["userPseudoId"],
            metrics=["sessions"],
            source_timezone="UTC",
        )


@pytest.mark.parametrize("token", ["token space", "token\x7f", "token\u0100"])
def test_malformed_bearer_token_fails_before_ga4_identity_egress(token: str) -> None:
    calls: list[str] = []

    def never_request(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("malformed token reached HTTP")

    client = Ga4DataClient(
        SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(never_request), trust_env=False),
            {"analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"},
            read_attempts=1,
        ),
        lambda: token,
    )

    with pytest.raises(AdhelmError) as raised:
        client.discover_identity("123", expected_web_stream_id="456")

    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert token not in str(raised.value)
    assert calls == []


def test_unknown_provider_field_is_rejected_without_exposing_transport_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "analyticsadmin.googleapis.com" and request.url.path.endswith(
            "/properties/123"
        ):
            return _response(_property())
        if request.url.host == "analyticsadmin.googleapis.com":
            return _response(_streams())
        return _response({"unexpected": "Bearer test-token-not-to-be-persisted"})

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123", expected_web_stream_id="456")
    with pytest.raises(AdhelmError) as raised:
        client.run_report(
            Ga4ReportQuery(
                property_id="123",
                account_ref=identity.property_ref,
                report_id="report.1",
                query_contract_hash="a" * 64,
                window_start=date(2026, 9, 1),
                window_end=date(2026, 9, 2),
                metrics=["sessions"],
                source_timezone="America/Chicago",
            ),
            identity=identity,
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "test-token" not in raised.value.message


def test_future_standard_window_and_currency_mismatch_stop_before_report_egress() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/properties/123"):
            return _response(_property())
        if request.url.host == "analyticsadmin.googleapis.com":
            return _response(_streams())
        pytest.fail("unsafe GA4 report request escaped")

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123", expected_web_stream_id="456")
    with pytest.raises(AdhelmError) as future:
        client.run_report(
            Ga4ReportQuery(
                property_id="123",
                account_ref=identity.property_ref,
                report_id="report.future",
                query_contract_hash="a" * 64,
                window_start=date(2026, 9, 5),
                window_end=date(2026, 9, 5),
                metrics=["sessions"],
                source_timezone="America/Chicago",
            ),
            identity=identity,
        )
    assert future.value.code == "INVALID_REPORT_WINDOW"

    with pytest.raises(AdhelmError) as currency:
        client.run_realtime_report(
            Ga4RealtimeQuery(
                property_id="123",
                account_ref=identity.property_ref,
                report_id="report.currency",
                query_contract_hash="a" * 64,
                allowed_event_names=["approved_event"],
                source_timezone="America/Chicago",
                currency="CAD",
            ),
            identity=identity,
        )
    assert currency.value.code == "CURRENCY_MISMATCH"
    assert len(calls) == 2


def test_realtime_event_is_a_partition_without_claiming_freshness() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/properties/123"):
            return _response(_property())
        if request.url.host == "analyticsadmin.googleapis.com":
            return _response(_streams())
        body = json.loads(request.content)
        assert body["dimensionFilter"]["filter"]["fieldName"] == "eventName"
        return _response(
            {
                "dimensionHeaders": [{"name": "eventName"}],
                "metricHeaders": [{"name": "eventCount"}, {"name": "keyEvents"}],
                "rows": [
                    {
                        "dimensionValues": [{"value": "approved_event"}],
                        "metricValues": [{"value": "5"}, {"value": "2"}],
                    }
                ],
                "rowCount": 1,
                "metadata": {"timeZone": "America/Chicago", "currencyCode": "USD"},
            }
        )

    client = _client(httpx.MockTransport(handler))
    identity = client.discover_identity("123", expected_web_stream_id="456")
    report = client.run_realtime_report(
        Ga4RealtimeQuery(
            property_id="123",
            account_ref=identity.property_ref,
            report_id="report.realtime",
            query_contract_hash="a" * 64,
            allowed_event_names=["approved_event"],
            metrics=["eventCount", "keyEvents"],
            source_timezone="America/Chicago",
        ),
        identity=identity,
    )
    row = report.performance_rows[0]
    assert row.dimension_type == "GA4_EVENT"
    assert row.dimension_value == "ga4.event.approved_event"
    assert row.provider_extensions == {}
    assert report.envelope.data_fresh_through is None
    assert report.envelope.data_quality_statuses == ["PROCESSING_PENDING"]


def test_malformed_nested_admin_and_metadata_responses_fail_closed() -> None:
    def bad_stream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/properties/123"):
            return _response(_property())
        return _response(
            {
                "dataStreams": [
                    {
                        "name": "properties/123/dataStreams/456",
                        "type": "WEB_DATA_STREAM",
                        "webStreamData": "not-an-object",
                    }
                ]
            }
        )

    with pytest.raises(AdhelmError) as malformed_stream:
        _client(httpx.MockTransport(bad_stream)).discover_identity("123")
    assert malformed_stream.value.code == "PROVIDER_RESPONSE_INVALID"

    def bad_metadata(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/properties/123"):
            return _response(_property())
        if request.url.host == "analyticsadmin.googleapis.com":
            return _response(_streams())
        report = _report(
            [
                {
                    "dimensionValues": [{"value": "77"}, {"value": "google"}],
                    "metricValues": [{"value": "2"}, {"value": "1"}],
                }
            ],
            row_count=1,
        )
        report["metadata"] = {
            "timeZone": "America/Chicago",
            "currencyCode": "USD",
            "samplingMetadatas": [{"samplesReadCounts": ["1"], "unexpected": []}],
        }
        return _response(report)

    client = _client(httpx.MockTransport(bad_metadata))
    identity = client.discover_identity("123", expected_web_stream_id="456")
    with pytest.raises(AdhelmError) as malformed_metadata:
        client.run_report(
            Ga4ReportQuery(
                property_id="123",
                account_ref=identity.property_ref,
                report_id="report.nested",
                query_contract_hash="a" * 64,
                window_start=date(2026, 9, 1),
                window_end=date(2026, 9, 1),
                dimensions=["sessionCampaignId", "sessionSource"],
                metrics=["sessions", "keyEvents"],
                source_timezone="America/Chicago",
            ),
            identity=identity,
        )
    assert malformed_metadata.value.code == "PROVIDER_RESPONSE_INVALID"
