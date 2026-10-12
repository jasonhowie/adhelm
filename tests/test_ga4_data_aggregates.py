"""Fixed GA4 Data API aggregate reporting (ticket M00).

Every test drives the adapter through an in-memory ``httpx.MockTransport``.
No socket, credential, or live GA4 property is used, so these results are
fixture evidence for the request/response contract only.  They are not GA4 UI
reconciliation and not proof of live behaviour.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from adhelm import workflows
from adhelm.canonical import canonical_bytes
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile
from adhelm.privacy import validate_data
from adhelm.providers.ga4 import Ga4AggregateQuery, Ga4DataClient, Ga4Identity
from adhelm.transport import SafeHttpClient

_TZ = "America/Chicago"


def _client(
    handler: httpx.MockTransport, *, maximum_response_bytes: int = 8_388_608
) -> Ga4DataClient:
    return Ga4DataClient(
        SafeHttpClient(
            httpx.Client(transport=handler, trust_env=False),
            {"analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"},
            read_attempts=1,
            maximum_response_bytes=maximum_response_bytes,
        ),
        lambda: "test-token-not-to-be-persisted",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="test",
    )


def _identity(*, currency: str | None = "USD", timezone: str = _TZ) -> Ga4Identity:
    return Ga4Identity(
        property_ref="ga4.property.123",
        property_client_ref="123",
        web_stream_ref="ga4.stream.456",
        web_stream_client_ref="456",
        timezone=timezone,
        currency=currency,
    )


def _traffic_query(**overrides: Any) -> Ga4AggregateQuery:
    values: dict[str, Any] = {
        "aggregate_shape": "CAMPAIGN_SOURCE_MEDIUM",
        "property_id": "123",
        "account_ref": "ga4.property.123",
        "report_id": "report.aggregate",
        "query_contract_hash": "a" * 64,
        "window_start": date(2026, 9, 1),
        "window_end": date(2026, 9, 2),
        "source_timezone": _TZ,
        "currency": "USD",
    }
    values.update(overrides)
    return Ga4AggregateQuery(**values)


def _key_event_query(**overrides: Any) -> Ga4AggregateQuery:
    return _traffic_query(
        aggregate_shape="KEY_EVENT_TOTALS",
        key_event_names=("generate_lead", "purchase"),
        **overrides,
    )


def _traffic_row(campaign: str, source: str, medium: str, values: tuple[str, str, str]) -> Any:
    return {
        "dimensionValues": [{"value": campaign}, {"value": source}, {"value": medium}],
        "metricValues": [{"value": item} for item in values],
    }


def _traffic_page(rows: list[Any], row_count: int, **metadata: Any) -> dict[str, Any]:
    return {
        "dimensionHeaders": [
            {"name": "sessionCampaignName"},
            {"name": "sessionSource"},
            {"name": "sessionMedium"},
        ],
        "metricHeaders": [{"name": "sessions"}, {"name": "totalUsers"}, {"name": "keyEvents"}],
        "rows": rows,
        "rowCount": row_count,
        "metadata": {"timeZone": _TZ, "currencyCode": "USD", **metadata},
    }


def _key_event_page(rows: list[Any], row_count: int, **metadata: Any) -> dict[str, Any]:
    return {
        "dimensionHeaders": [{"name": "eventName"}],
        "metricHeaders": [{"name": "keyEvents"}],
        "rows": rows,
        "rowCount": row_count,
        "metadata": {"timeZone": _TZ, "currencyCode": "USD", **metadata},
    }


def _serve(pages: list[dict[str, Any]], sink: list[dict[str, Any]]) -> httpx.MockTransport:
    """Return one page per POST, recording the exact request body each time."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "analyticsdata.googleapis.com"
        body = json.loads(request.content)
        sink.append(body)
        index = min(len(sink) - 1, len(pages) - 1)
        return httpx.Response(200, json=pages[index])

    return httpx.MockTransport(handler)


def test_fixed_request_shape_accepts_no_free_form_dimensions_or_metrics() -> None:
    bodies: list[dict[str, Any]] = []
    page = _traffic_page([_traffic_row("brand", "google", "cpc", ("4", "3", "1"))], 1)
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _traffic_query(), identity=_identity()
    )

    assert "dimensions" not in Ga4AggregateQuery.model_fields
    assert "metrics" not in Ga4AggregateQuery.model_fields
    assert [item["name"] for item in bodies[0]["dimensions"]] == [
        "sessionCampaignName",
        "sessionSource",
        "sessionMedium",
    ]
    assert [item["name"] for item in bodies[0]["metrics"]] == [
        "sessions",
        "totalUsers",
        "keyEvents",
    ]
    # GA4 date ranges are inclusive at both ends; the adapter records the exact
    # property-local instants that boundary denotes.
    assert bodies[0]["dateRanges"] == [{"startDate": "2026-09-01", "endDate": "2026-09-02"}]
    assert report.window_boundary == "INCLUSIVE_START_INCLUSIVE_END_PROPERTY_LOCAL_DATES"
    assert report.window_start_instant == datetime(2026, 9, 1, 5, tzinfo=UTC)
    assert report.window_end_exclusive_instant == datetime(2026, 9, 3, 5, tzinfo=UTC)
    assert report.property_timezone == _TZ
    assert report.property_currency == "USD"
    assert report.completeness == "COMPLETE"
    assert report.exhaustive() is True


def test_no_documented_key_event_metric_form_is_requested() -> None:
    """GA4 documents no ``keyEvents:<name>`` metric, only rate metrics.

    Per-key-event counts therefore come from the ``eventName`` dimension
    restricted to the property's configured key events.
    """
    bodies: list[dict[str, Any]] = []
    page = _key_event_page(
        [{"dimensionValues": [{"value": "purchase"}], "metricValues": [{"value": "7"}]}], 1
    )
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _key_event_query(), identity=_identity()
    )

    assert [item["name"] for item in bodies[0]["metrics"]] == ["keyEvents"]
    assert all(":" not in item["name"] for item in bodies[0]["metrics"])
    assert bodies[0]["dimensionFilter"] == {
        "filter": {
            "fieldName": "eventName",
            "inListFilter": {"values": ["generate_lead", "purchase"], "caseSensitive": True},
        }
    }
    purchase = next(row for row in report.rows if row.event_name == "purchase")
    assert [(cell.metric, cell.status, cell.value) for cell in purchase.cells] == [
        ("keyEvents", "OBSERVED", 7)
    ]


def test_multi_page_consumption_reconciles_the_declared_row_count() -> None:
    bodies: list[dict[str, Any]] = []
    pages = [
        _traffic_page([_traffic_row("brand", "google", "cpc", ("4", "3", "1"))], 3),
        _traffic_page([_traffic_row("demand", "linkedin", "paid", ("5", "4", "0"))], 3),
        _traffic_page([_traffic_row("news", "email", "newsletter", ("6", "6", "2"))], 3),
    ]
    report = _client(_serve(pages, bodies)).run_aggregate_report(
        _traffic_query(page_size=1, page_bound=5), identity=_identity()
    )

    assert [body["offset"] for body in bodies] == ["0", "1", "2"]
    assert [body["limit"] for body in bodies] == ["1", "1", "1"]
    assert report.pages_consumed == 3
    assert report.declared_row_count == 3
    assert report.consumed_row_count == 3
    assert report.completeness == "COMPLETE"
    assert [row.session_campaign_name for row in report.rows] == ["brand", "demand", "news"]
    assert report.envelope.data_quality_statuses == ["COMPLETE"]


def test_exceeding_the_page_bound_reports_incomplete_and_never_claims_exhaustive() -> None:
    bodies: list[dict[str, Any]] = []
    pages = [
        _traffic_page([_traffic_row("brand", "google", "cpc", ("4", "3", "1"))], 9),
        _traffic_page([_traffic_row("demand", "linkedin", "paid", ("5", "4", "0"))], 9),
    ]
    report = _client(_serve(pages, bodies)).run_aggregate_report(
        _traffic_query(page_size=1, page_bound=2), identity=_identity()
    )

    assert report.completeness == "INCOMPLETE_PAGE_BOUND_EXCEEDED"
    assert report.exhaustive() is False
    assert report.pages_consumed == 2
    assert report.page_bound == 2
    assert report.declared_row_count == 9
    assert report.consumed_row_count == 2
    assert "PARTIAL_PAGE_FAILURE" in report.envelope.data_quality_statuses
    assert "COMPLETE" not in report.envelope.data_quality_statuses


def test_row_count_disagreement_between_pages_reports_incomplete() -> None:
    bodies: list[dict[str, Any]] = []
    pages = [
        _traffic_page([_traffic_row("brand", "google", "cpc", ("4", "3", "1"))], 4),
        _traffic_page([_traffic_row("demand", "linkedin", "paid", ("5", "4", "0"))], 6),
    ]
    report = _client(_serve(pages, bodies)).run_aggregate_report(
        _traffic_query(page_size=1, page_bound=5), identity=_identity()
    )

    assert report.completeness == "INCOMPLETE_ROW_COUNT_MISMATCH"
    assert report.exhaustive() is False
    assert report.consumed_row_count == 1
    assert "PARTIAL_PAGE_FAILURE" in report.envelope.data_quality_statuses


def test_reported_rows_beyond_the_declared_total_fail_closed() -> None:
    bodies: list[dict[str, Any]] = []
    pages = [
        _traffic_page(
            [
                _traffic_row("brand", "google", "cpc", ("4", "3", "1")),
                _traffic_row("demand", "linkedin", "paid", ("5", "4", "0")),
            ],
            1,
        )
    ]
    with pytest.raises(AdhelmError) as raised:
        _client(_serve(pages, bodies)).run_aggregate_report(
            _traffic_query(page_size=2, page_bound=2), identity=_identity()
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_thresholded_key_events_are_withheld_rather_than_zero() -> None:
    bodies: list[dict[str, Any]] = []
    page = _key_event_page(
        [{"dimensionValues": [{"value": "purchase"}], "metricValues": [{"value": "12"}]}],
        1,
        subjectToThresholding=True,
    )
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _key_event_query(), identity=_identity()
    )

    assert report.subject_to_thresholding is True
    assert report.completeness == "WITHHELD"
    assert report.exhaustive() is False
    withheld = next(row for row in report.rows if row.event_name == "generate_lead")
    assert withheld.dimension_statuses == ("KEY_EVENT_NOT_RETURNED",)
    assert [(cell.status, cell.value) for cell in withheld.cells] == [("THRESHOLD_WITHHELD", None)]
    assert "THRESHOLDED" in withheld.data_quality_statuses
    # The projected report keeps the same distinction: no count, not a zero.
    normalized = report.to_normalized_report()
    conversion = next(
        row for row in normalized.conversion_rows if row.conversion_action_ref.endswith("lead")
    )
    assert conversion.conversion_count is None
    assert all(row.key_events != 0 for row in normalized.performance_rows)


def test_missing_configured_key_event_is_reported_missing_not_zero() -> None:
    bodies: list[dict[str, Any]] = []
    page = _key_event_page(
        [{"dimensionValues": [{"value": "purchase"}], "metricValues": [{"value": "0"}]}], 1
    )
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _key_event_query(), identity=_identity()
    )

    observed = next(row for row in report.rows if row.event_name == "purchase")
    missing = next(row for row in report.rows if row.event_name == "generate_lead")
    # A real reported zero stays a zero; an absent key event stays unknown.
    assert [(cell.status, cell.value) for cell in observed.cells] == [("OBSERVED", 0)]
    assert [(cell.status, cell.value) for cell in missing.cells] == [("NOT_RETURNED", None)]
    assert "MISSING_EVENT" in missing.data_quality_statuses
    assert "MISSING_EVENT" in report.envelope.data_quality_statuses


def test_sampled_rows_are_labelled_estimates_with_the_documented_sample_sizes() -> None:
    bodies: list[dict[str, Any]] = []
    page = _traffic_page(
        [_traffic_row("brand", "google", "cpc", ("4", "3", "1"))],
        1,
        samplingMetadatas=[{"samplesReadCount": "500", "samplingSpaceSize": "1000"}],
    )
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _traffic_query(), identity=_identity()
    )

    assert [
        (item.date_range_index, item.samples_read_count, item.sampling_space_size)
        for item in report.sampling
    ] == [(0, 500, 1000)]
    assert all(cell.status == "SAMPLED_ESTIMATE" for cell in report.rows[0].cells)
    assert "SAMPLED" in report.envelope.data_quality_statuses
    assert "SAMPLED" in report.rows[0].data_quality_statuses


def test_legacy_plural_sampling_field_names_are_rejected() -> None:
    """The documented ``SamplingMetadata`` fields are singular scalars."""
    bodies: list[dict[str, Any]] = []
    page = _traffic_page(
        [_traffic_row("brand", "google", "cpc", ("4", "3", "1"))],
        1,
        samplingMetadatas=[{"samplesReadCounts": ["500"], "samplingSpaceSizes": ["1000"]}],
    )
    with pytest.raises(AdhelmError) as raised:
        _client(_serve([page], bodies)).run_aggregate_report(_traffic_query(), identity=_identity())
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_restricted_metric_is_withheld_and_flagged_unknown() -> None:
    bodies: list[dict[str, Any]] = []
    page = _traffic_page(
        [_traffic_row("brand", "google", "cpc", ("4", "3", "1"))],
        1,
        schemaRestrictionResponse={
            "activeMetricRestrictions": [
                {"metricName": "totalUsers", "restrictedMetricTypes": ["REVENUE_DATA"]}
            ]
        },
    )
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _traffic_query(), identity=_identity()
    )

    cells = {cell.metric: cell for cell in report.rows[0].cells}
    assert (cells["totalUsers"].status, cells["totalUsers"].value) == ("RESTRICTED", None)
    assert cells["sessions"].value == 4
    assert report.restricted_metrics[0].metric_name == "totalUsers"
    assert report.restricted_metrics[0].restricted_metric_types == ("REVENUE_DATA",)
    assert "PROVIDER_REPORTED_UNKNOWN" in report.envelope.data_quality_statuses


def test_other_row_aggregation_is_preserved_without_altering_the_value() -> None:
    bodies: list[dict[str, Any]] = []
    page = _traffic_page(
        [_traffic_row("(other)", "google", "cpc", ("4", "3", "1"))],
        1,
        dataLossFromOtherRow=True,
    )
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _traffic_query(), identity=_identity()
    )

    assert report.other_row_aggregation is True
    # The dimension value is retained exactly as GA4 returned it so the
    # deterministic join in M00.2 sees the provider's own text.
    assert report.rows[0].session_campaign_name == "(other)"
    assert report.rows[0].dimension_statuses == ("CAMPAIGN_OTHER_ROW",)
    assert "PROVIDER_REPORTED_UNKNOWN" in report.rows[0].data_quality_statuses
    # A roll-up bucket hides its members, so the table is not exhaustive.
    assert report.exhaustive() is False


def test_empty_result_records_the_provider_empty_reason() -> None:
    bodies: list[dict[str, Any]] = []
    page = _key_event_page([], 0, emptyReason="NO_DATA_IN_DATE_RANGE")
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _key_event_query(), identity=_identity()
    )

    # GA4 declined to report for a reason it does not enumerate, so this is
    # never evidence that zero rows match the query.
    assert report.completeness == "NOT_AVAILABLE_FROM_PROVIDER"
    assert report.exhaustive() is False
    assert report.empty_reason == "NO_DATA_IN_DATE_RANGE"
    assert report.consumed_row_count == 0
    assert "NOT_AVAILABLE_FROM_PROVIDER" in report.envelope.data_quality_statuses
    # Every declared key event stays explicitly unknown rather than zero.
    assert len(report.rows) == 2
    assert all(cell.value is None for row in report.rows for cell in row.cells)


def test_missing_property_currency_refuses_before_any_report_request() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("unbound GA4 aggregate request escaped")

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handler)).run_aggregate_report(
            _traffic_query(), identity=_identity(currency=None)
        )
    assert raised.value.code == "EXPECTED_CURRENCY_REQUIRED"
    assert calls == []


def test_timezone_and_currency_disagreement_refuse_before_any_report_request() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("unbound GA4 aggregate request escaped")

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(AdhelmError) as timezone:
        client.run_aggregate_report(_traffic_query(source_timezone="UTC"), identity=_identity())
    assert timezone.value.code == "TIMEZONE_MISMATCH"

    with pytest.raises(AdhelmError) as currency:
        client.run_aggregate_report(_traffic_query(currency="CAD"), identity=_identity())
    assert currency.value.code == "CURRENCY_MISMATCH"

    with pytest.raises(AdhelmError) as window:
        client.run_aggregate_report(
            _traffic_query(window_start=date(2026, 9, 5), window_end=date(2026, 9, 5)),
            identity=_identity(),
        )
    assert window.value.code == "INVALID_REPORT_WINDOW"
    assert calls == []


def test_metadata_timezone_or_currency_drift_fails_closed() -> None:
    bodies: list[dict[str, Any]] = []
    drifted = _traffic_page([_traffic_row("brand", "google", "cpc", ("4", "3", "1"))], 1)
    drifted["metadata"]["timeZone"] = "UTC"
    with pytest.raises(AdhelmError) as raised:
        _client(_serve([drifted], bodies)).run_aggregate_report(
            _traffic_query(), identity=_identity()
        )
    assert raised.value.code == "TIMEZONE_MISMATCH"


def test_metric_header_mismatch_refuses_the_response() -> None:
    bodies: list[dict[str, Any]] = []
    page = _traffic_page([_traffic_row("brand", "google", "cpc", ("4", "3", "1"))], 1)
    page["metricHeaders"] = [
        {"name": "sessions"},
        {"name": "activeUsers"},
        {"name": "keyEvents"},
    ]
    with pytest.raises(AdhelmError) as raised:
        _client(_serve([page], bodies)).run_aggregate_report(_traffic_query(), identity=_identity())
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_dimension_header_mismatch_refuses_the_response() -> None:
    bodies: list[dict[str, Any]] = []
    page = _traffic_page([_traffic_row("brand", "google", "cpc", ("4", "3", "1"))], 1)
    page["dimensionHeaders"] = [
        {"name": "sessionCampaignId"},
        {"name": "sessionSource"},
        {"name": "sessionMedium"},
    ]
    with pytest.raises(AdhelmError) as raised:
        _client(_serve([page], bodies)).run_aggregate_report(_traffic_query(), identity=_identity())
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_undeclared_event_name_in_the_response_fails_closed() -> None:
    bodies: list[dict[str, Any]] = []
    page = _key_event_page(
        [{"dimensionValues": [{"value": "checkout_step"}], "metricValues": [{"value": "3"}]}], 1
    )
    with pytest.raises(AdhelmError) as raised:
        _client(_serve([page], bodies)).run_aggregate_report(
            _key_event_query(), identity=_identity()
        )
    assert raised.value.code == "SENSITIVE_DATA_RISK"


def test_duplicate_dimension_combination_fails_closed() -> None:
    bodies: list[dict[str, Any]] = []
    page = _traffic_page(
        [
            _traffic_row("brand", "google", "cpc", ("4", "3", "1")),
            _traffic_row("brand", "google", "cpc", ("5", "4", "2")),
        ],
        2,
    )
    with pytest.raises(AdhelmError) as raised:
        _client(_serve([page], bodies)).run_aggregate_report(
            _traffic_query(page_size=2), identity=_identity()
        )
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_oversized_response_is_refused_by_the_safe_transport() -> None:
    bodies: list[dict[str, Any]] = []
    page = _traffic_page(
        [
            _traffic_row(f"campaign-{index:04d}", "google", "cpc", ("4", "3", "1"))
            for index in range(200)
        ],
        200,
    )
    with pytest.raises(AdhelmError) as raised:
        _client(_serve([page], bodies), maximum_response_bytes=2_048).run_aggregate_report(
            _traffic_query(page_size=1_000), identity=_identity()
        )
    assert raised.value.code == "RESPONSE_TOO_LARGE"


def test_sensitive_dimension_text_never_reaches_a_persisted_row() -> None:
    bodies: list[dict[str, Any]] = []
    page = _traffic_page(
        [_traffic_row("nurture jane.doe@example.com", "google", "cpc", ("4", "3", "1"))], 1
    )
    with pytest.raises(AdhelmError) as raised:
        _client(_serve([page], bodies)).run_aggregate_report(_traffic_query(), identity=_identity())
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "jane.doe" not in raised.value.message


def test_persisted_ledger_payload_passes_the_privacy_validator() -> None:
    bodies: list[dict[str, Any]] = []
    page = _traffic_page(
        [
            _traffic_row("brand", "google", "cpc", ("4", "3", "1")),
            _traffic_row("(not set)", "(direct)", "(none)", ("9", "8", "0")),
        ],
        2,
    )
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _traffic_query(page_size=2), identity=_identity()
    )
    payload = report.to_normalized_report().to_ledger_payload(
        observation_key="observation:test",
        observation_revision=1,
        source_report_digest="b" * 64,
    )

    validate_data(payload)
    assert "authorization" not in json.dumps(payload)
    assert "test-token-not-to-be-persisted" not in json.dumps(payload)
    unset = next(row for row in report.rows if row.session_campaign_name == "(not set)")
    assert unset.dimension_statuses == ("CAMPAIGN_NOT_SET",)
    assert unset.join_status == "UNMAPPED"
    assert "UNMAPPED" in unset.data_quality_statuses


def test_normalized_output_is_byte_identical_for_identical_input() -> None:
    page = _traffic_page(
        [
            _traffic_row("demand", "linkedin", "paid", ("5", "4", "0")),
            _traffic_row("brand", "google", "cpc", ("4", "3", "1")),
        ],
        2,
    )
    first = _client(_serve([page], [])).run_aggregate_report(
        _traffic_query(page_size=2), identity=_identity()
    )
    second = _client(_serve([page], [])).run_aggregate_report(
        _traffic_query(page_size=2), identity=_identity()
    )

    assert canonical_bytes(first) == canonical_bytes(second)
    assert canonical_bytes(first.to_normalized_report()) == canonical_bytes(
        second.to_normalized_report()
    )
    # A fixed total order makes the projection independent of provider row order.
    assert [row.session_campaign_name for row in first.rows] == ["brand", "demand"]


def test_attribution_freshness_and_key_event_semantics_are_explicit() -> None:
    bodies: list[dict[str, Any]] = []
    page = _traffic_page([_traffic_row("brand", "google", "cpc", ("4", "3", "1"))], 1)
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _traffic_query(), identity=_identity()
    )

    # The Data API exposes no attribution model and no freshness instant.
    assert report.attribution_disclosure == "GA4_DATA_API_PROPERTY_DEFAULT_NOT_DOCUMENTED"
    assert report.envelope.attribution_model == "GA4_DATA_API_PROPERTY_DEFAULT_NOT_DOCUMENTED"
    assert report.envelope.attribution_window is None
    assert report.freshness_disclosure == "NO_PROVIDER_FRESHNESS_FIELD_OBSERVED_AT_ONLY"
    assert report.envelope.data_fresh_through is None
    assert report.envelope.observed_at == datetime(2026, 9, 4, 12, tzinfo=UTC)
    # AGENTS rule 16: a GA4 key event is not a provider conversion.
    assert report.key_event_disclosure == "GA4_KEY_EVENT_IS_NOT_A_PROVIDER_CONVERSION"
    summary = report.quality_summary()
    assert summary["join_disclosure"] == "DETERMINISTIC_PROVIDER_JOIN_DEFERRED"
    assert "rows" not in summary


def test_campaign_key_event_shape_keeps_every_join_dimension_unmodified() -> None:
    bodies: list[dict[str, Any]] = []
    page = {
        "dimensionHeaders": [
            {"name": "sessionCampaignName"},
            {"name": "sessionSource"},
            {"name": "sessionMedium"},
            {"name": "eventName"},
        ],
        "metricHeaders": [{"name": "keyEvents"}],
        "rows": [
            {
                "dimensionValues": [
                    {"value": "Q3 Brand | EMEA"},
                    {"value": "google"},
                    {"value": "cpc"},
                    {"value": "generate_lead"},
                ],
                "metricValues": [{"value": "2"}],
            }
        ],
        "rowCount": 1,
        "metadata": {"timeZone": _TZ, "currencyCode": "USD"},
    }
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _traffic_query(
            aggregate_shape="CAMPAIGN_SOURCE_MEDIUM_KEY_EVENT",
            key_event_names=("generate_lead",),
        ),
        identity=_identity(),
    )

    row = report.rows[0]
    assert row.session_campaign_name == "Q3 Brand | EMEA"
    assert row.session_source == "google"
    assert row.session_medium == "cpc"
    assert row.event_name == "generate_lead"
    extensions = report.to_normalized_report().performance_rows[0].provider_extensions
    assert extensions["ga4_sessionCampaignName"] == "Q3 Brand | EMEA"
    assert extensions["ga4_sessionSource"] == "google"
    assert extensions["ga4_sessionMedium"] == "cpc"
    assert extensions["ga4_eventName"] == "generate_lead"
    # No campaign resource is minted here: the deterministic join is M00.2.
    assert report.to_normalized_report().performance_rows[0].campaign_ref is None
    assert report.to_normalized_report().conversion_rows == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"aggregate_shape": "KEY_EVENT_TOTALS", "key_event_names": ()},
        {"key_event_names": ("generate_lead",)},
        {"key_event_names": ("bad name",), "aggregate_shape": "KEY_EVENT_TOTALS"},
        {"window_start": date(2026, 9, 3), "window_end": date(2026, 9, 1)},
        {"page_size": 10_000, "page_bound": 50},
    ],
)
def test_invalid_fixed_aggregate_requests_fail_before_transport(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _traffic_query(**overrides)


def _ga4_profile() -> Profile:
    return Profile(
        profile_id="ga4.read",
        provider="ga4",
        account_ref="123",
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["read"],
        purpose="READ",
        currency="USD",
        timezone=_TZ,
        credential_ref="oauth.fixture",
    )


def _wire(monkeypatch: pytest.MonkeyPatch, handler: httpx.MockTransport) -> None:
    actual = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: actual(transport=handler, **kwargs))
    monkeypatch.setattr(workflows, "credential", lambda *a, **k: "oauth-fixture")
    monkeypatch.setattr(workflows, "export_key", lambda: b"r" * 32)


def _identity_handler(
    pages: list[dict[str, Any]], sink: list[httpx.Request]
) -> httpx.MockTransport:
    data_calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sink.append(request)
        if request.url.host == "analyticsadmin.googleapis.com":
            if request.url.path.endswith("/properties/123"):
                return httpx.Response(
                    200,
                    json={"name": "properties/123", "timeZone": _TZ, "currencyCode": "USD"},
                )
            return httpx.Response(
                200,
                json={
                    "dataStreams": [
                        {"name": "properties/123/dataStreams/456", "type": "WEB_DATA_STREAM"}
                    ]
                },
            )
        data_calls.append(1)
        return httpx.Response(200, json=pages[min(len(data_calls) - 1, len(pages) - 1)])

    return httpx.MockTransport(handler)


def test_workflow_routes_the_fixed_aggregate_and_persists_a_normalized_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_ga4_profile()]))
    query_file = tmp_path / "ga4-aggregate.json"
    query_file.write_text(
        json.dumps(
            {
                "aggregate_shape": "KEY_EVENT_TOTALS",
                "property_id": "123",
                "account_ref": "123",
                "report_id": "report.ga4.aggregate",
                "query_contract_hash": "c" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-02",
                "key_event_names": ["generate_lead", "purchase"],
                "source_timezone": _TZ,
                "currency": "USD",
            }
        )
    )
    requests: list[httpx.Request] = []
    page = _key_event_page(
        [{"dimensionValues": [{"value": "purchase"}], "metricValues": [{"value": "7"}]}],
        1,
        subjectToThresholding=True,
    )
    _wire(monkeypatch, _identity_handler([page], requests))

    result = workflows.run_report(tmp_path, "ga4", "ga4.read", query_file, stream_id="456")

    assert [request.url.host for request in requests] == [
        "analyticsadmin.googleapis.com",
        "analyticsadmin.googleapis.com",
        "analyticsdata.googleapis.com",
    ]
    summary = result["ga4_aggregate"]
    assert summary["aggregate_shape"] == "KEY_EVENT_TOTALS"
    # Every provider row was consumed, but thresholding withheld a configured
    # key event, so the read is not every matching row.
    assert summary["completeness"] == "WITHHELD"
    assert summary["exhaustive"] is False
    assert summary["subject_to_thresholding"] is True
    assert summary["attribution_disclosure"] == "GA4_DATA_API_PROPERTY_DEFAULT_NOT_DOCUMENTED"
    assert summary["freshness_disclosure"] == "NO_PROVIDER_FRESHNESS_FIELD_OBSERVED_AT_ONLY"
    assert summary["key_event_disclosure"] == "GA4_KEY_EVENT_IS_NOT_A_PROVIDER_CONVERSION"
    counts = [row["conversion_count"] for row in result["conversion_rows"]]
    # A thresholded key event is withheld, never rendered as a zero count.
    assert sorted(counts, key=lambda item: (item is not None, item)) == [None, 7]

    records = [
        record
        for record in Ledger(tmp_path).records("performance")
        if record["record_kind"] == "PERFORMANCE_OBSERVATION"
    ]
    assert len(records) == 1
    validate_data(records[0]["payload"])
    persisted = records[0]["payload"]["performance_rows"]
    assert sorted(row["key_events"] for row in persisted if row["key_events"] is not None) == [7]
    assert any(row["key_events"] is None for row in persisted)


def test_existing_ga4_report_input_still_routes_to_the_unchanged_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_ga4_profile()]))
    query_file = tmp_path / "ga4-report.json"
    query_file.write_text(
        json.dumps(
            {
                "property_id": "123",
                "account_ref": "123",
                "report_id": "report.ga4.legacy",
                "query_contract_hash": "d" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
                "metrics": ["sessions"],
                "source_timezone": _TZ,
                "currency": "USD",
            }
        )
    )
    page = {
        "dimensionHeaders": [],
        "metricHeaders": [{"name": "sessions"}],
        "rows": [{"dimensionValues": [], "metricValues": [{"value": "2"}]}],
        "rowCount": 1,
        "metadata": {"timeZone": _TZ, "currencyCode": "USD"},
    }
    _wire(monkeypatch, _identity_handler([page], []))

    result = workflows.run_report(tmp_path, "ga4", "ga4.read", query_file, stream_id="456")

    assert "ga4_aggregate" not in result
    assert len(result["performance_rows"]) == 1
    assert result["performance_rows"][0]["sessions"] == 2


def test_pages_disagreeing_about_sampling_are_recorded_not_truncated() -> None:
    bodies: list[dict[str, Any]] = []
    pages = [
        _traffic_page(
            [_traffic_row("brand", "google", "cpc", ("4", "3", "1"))],
            2,
            samplingMetadatas=[{"samplesReadCount": "5", "samplingSpaceSize": "100"}],
        ),
        _traffic_page(
            [_traffic_row("demand", "linkedin", "paid", ("5", "4", "0"))],
            2,
            samplingMetadatas=[{"samplesReadCount": "9", "samplingSpaceSize": "100"}],
        ),
    ]
    report = _client(_serve(pages, bodies)).run_aggregate_report(
        _traffic_query(page_size=1, page_bound=3), identity=_identity()
    )

    # One request date range yields one window; disagreement is a recorded
    # fact rather than a silently concatenated or truncated list.
    assert len(report.sampling) == 1
    assert report.sampling_consistency == "SAMPLING_METADATA_INCONSISTENT"
    assert "PROVIDER_REPORTED_UNKNOWN" in report.envelope.data_quality_statuses


def test_consistent_sampling_across_pages_is_recorded_once() -> None:
    bodies: list[dict[str, Any]] = []
    window = {"samplesReadCount": "5", "samplingSpaceSize": "100"}
    pages = [
        _traffic_page(
            [_traffic_row("brand", "google", "cpc", ("4", "3", "1"))],
            2,
            samplingMetadatas=[window],
        ),
        _traffic_page(
            [_traffic_row("demand", "linkedin", "paid", ("5", "4", "0"))],
            2,
            samplingMetadatas=[window],
        ),
    ]
    report = _client(_serve(pages, bodies)).run_aggregate_report(
        _traffic_query(page_size=1, page_bound=3), identity=_identity()
    )

    assert report.pages_consumed == 2
    assert len(report.sampling) == 1
    assert report.sampling_consistency == "CONSISTENT"


def test_report_level_completeness_facts_reach_the_canonical_ledger() -> None:
    bodies: list[dict[str, Any]] = []
    pages = [
        _traffic_page([_traffic_row("brand", "google", "cpc", ("4", "3", "1"))], 9),
        _traffic_page([_traffic_row("demand", "linkedin", "paid", ("5", "4", "0"))], 9),
    ]
    report = _client(_serve(pages, bodies)).run_aggregate_report(
        _traffic_query(page_size=1, page_bound=2), identity=_identity()
    )
    payload = report.to_normalized_report().to_ledger_payload(
        observation_key="observation:test",
        observation_revision=1,
        source_report_digest="b" * 64,
    )

    validate_data(payload)
    extensions = payload["performance_rows"][0]["provider_extensions"]
    # AGENTS rule 22: the append-only history is canonical, so an incomplete
    # read must stay identifiable as incomplete after the process exits.
    assert extensions["ga4_completeness"] == "INCOMPLETE_PAGE_BOUND_EXCEEDED"
    assert extensions["ga4_declared_row_count"] == "9"
    assert extensions["ga4_consumed_row_count"] == "2"
    assert extensions["ga4_pages_consumed"] == "2"
    assert extensions["ga4_page_bound"] == "2"
    assert extensions["ga4_subject_to_thresholding"] == "false"
    assert extensions["ga4_sampling_consistency"] == "NOT_SAMPLED"
    assert extensions["ga4_cell_status_sessions"] == "OBSERVED"


def test_event_other_row_is_an_aggregation_fact_not_a_named_key_event() -> None:
    bodies: list[dict[str, Any]] = []
    page = _key_event_page(
        [
            {"dimensionValues": [{"value": "purchase"}], "metricValues": [{"value": "4"}]},
            {"dimensionValues": [{"value": "(other)"}], "metricValues": [{"value": "9"}]},
        ],
        2,
        dataLossFromOtherRow=True,
    )
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _key_event_query(page_size=2), identity=_identity()
    )

    rolled_up = next(row for row in report.rows if row.event_name == "(other)")
    assert rolled_up.dimension_statuses == ("EVENT_OTHER_ROW",)
    # The bucket is not a configured key event, so it mints no action
    # reference and does not satisfy a declared key event.
    assert rolled_up.conversion_action_ref is None
    assert any(
        row.event_name == "generate_lead" and row.dimension_statuses == ("KEY_EVENT_NOT_RETURNED",)
        for row in report.rows
    )
    conversions = {
        row.conversion_kind: row for row in report.to_normalized_report().conversion_rows
    }
    assert conversions["KEY_EVENT_OTHER_ROW"].conversion_count == 9
    assert conversions["KEY_EVENT_OTHER_ROW"].conversion_action_ref is None
    assert report.exhaustive() is False


def test_more_sampling_windows_than_requested_date_ranges_fails_closed() -> None:
    bodies: list[dict[str, Any]] = []
    page = _traffic_page(
        [_traffic_row("brand", "google", "cpc", ("4", "3", "1"))],
        1,
        samplingMetadatas=[
            {"samplesReadCount": "5", "samplingSpaceSize": "100"},
            {"samplesReadCount": "7", "samplingSpaceSize": "100"},
        ],
    )
    # Exactly one dateRange is sent, and GA4 documents one entry per range.
    with pytest.raises(AdhelmError) as raised:
        _client(_serve([page], bodies)).run_aggregate_report(_traffic_query(), identity=_identity())
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


def test_a_roll_up_row_alone_blocks_exhaustive_coverage() -> None:
    bodies: list[dict[str, Any]] = []
    # GA4 did not set dataLossFromOtherRow, but a returned "(other)" bucket is
    # itself proof that members were hidden.
    page = _traffic_page([_traffic_row("brand", "(other)", "cpc", ("4", "3", "1"))], 1)
    report = _client(_serve([page], bodies)).run_aggregate_report(
        _traffic_query(), identity=_identity()
    )

    assert report.rows[0].dimension_statuses == ("SOURCE_OTHER_ROW",)
    assert report.rolled_up() is True
    assert report.exhaustive() is False
    assert report.quality_summary()["other_row_aggregation"] is True
    extensions = report.to_normalized_report().performance_rows[0].provider_extensions
    assert extensions["ga4_other_row_aggregation"] == "true"
