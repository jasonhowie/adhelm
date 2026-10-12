"""Fixture-only tests for the GA4 provider audit rule table.

Reports come from the real Data API adapter driven through an in-memory
``httpx.MockTransport``, so every case audits exactly the shape the adapter
persists.  No test performs or simulates a live GA4 request, and no case
reconciles against the GA4 interface.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from adhelm.audit import AuditPolicy, audit_provider, audit_report
from adhelm.audits import (
    _RULE_MODULES,
    COMPILABLE_OPERATION_KINDS,
    MAXIMUM_INLINE_FINDING_IDS,
    ProviderAuditContext,
    ProviderAuditEvidence,
    ProviderAuditSections,
    ProviderRecommendation,
    RetainedGa4AdminObservation,
    provider_rule,
)
from adhelm.audits.ga4 import RULE_TABLE_VERSION, evaluate
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.models import Money
from adhelm.providers.ga4 import (
    Ga4AggregateQuery,
    Ga4DataClient,
    Ga4Identity,
    Ga4RealtimeQuery,
    Ga4ReportQuery,
)
from adhelm.providers.ga4_admin import (
    Ga4AdminDefaultValue,
    Ga4AdminEventRule,
    Ga4AdminIdentity,
    Ga4AdminInventory,
    Ga4AdminKeyEvent,
)
from adhelm.reporting import (
    ConversionBreakdownRow,
    Ga4AggregateReport,
    NormalizedReport,
    PerformanceRow,
)
from adhelm.transport import SafeHttpClient

REPORT_DIGEST = "a" * 64
ADMIN_DIGEST = "b" * 64
OBSERVED = datetime(2026, 9, 4, 12, tzinfo=UTC)
AS_OF = datetime(2026, 9, 5, 12, tzinfo=UTC)
ZONE = "America/Chicago"
PROPERTY = "ga4.property.123"
POLICY = AuditPolicy(maximum_observation_age_seconds=7 * 86_400, require_conversion_evidence=False)
STATIC_RULES = {
    "GA4_ATTRIBUTION_SETTINGS_NOT_READ",
    "GA4_DESTINATION_AND_LINKS_NOT_READ",
    "GA4_POLICY_SETTINGS_NOT_READ",
    "GA4_DRIFT_NOT_EVALUATED_BY_AUDIT",
    "GA4_KEY_EVENT_NOT_A_PROVIDER_CONVERSION",
    "GA4_BROWSER_NOT_APPLICABLE",
}


def _client(handler: httpx.MockTransport, *, now: datetime = OBSERVED) -> Ga4DataClient:
    return Ga4DataClient(
        SafeHttpClient(
            httpx.Client(transport=handler, trust_env=False),
            {"analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"},
            read_attempts=1,
        ),
        lambda: "test-token-not-to-be-persisted",
        now=lambda: now,
        connector_version="test",
    )


def _identity() -> Ga4Identity:
    return Ga4Identity(
        property_ref=PROPERTY,
        property_client_ref="123",
        web_stream_ref="ga4.stream.456",
        web_stream_client_ref="456",
        timezone=ZONE,
        currency="USD",
    )


def _serve(*pages: dict[str, Any]) -> httpx.MockTransport:
    served: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        index = min(len(served), len(pages) - 1)
        served.append(index)
        return httpx.Response(200, json=pages[index])

    return httpx.MockTransport(handler)


def _traffic_row(campaign: str, source: str, medium: str, values: tuple[str, str, str]) -> Any:
    return {
        "dimensionValues": [{"value": campaign}, {"value": source}, {"value": medium}],
        "metricValues": [{"value": item} for item in values],
    }


def _traffic_page(rows: list[Any], row_count: int | None = None, **metadata: Any) -> dict[str, Any]:
    return {
        "dimensionHeaders": [
            {"name": "sessionCampaignName"},
            {"name": "sessionSource"},
            {"name": "sessionMedium"},
        ],
        "metricHeaders": [{"name": "sessions"}, {"name": "totalUsers"}, {"name": "keyEvents"}],
        "rows": rows,
        "rowCount": len(rows) if row_count is None else row_count,
        "metadata": {"timeZone": ZONE, "currencyCode": "USD", **metadata},
    }


def _event_row(name: str, count: str) -> Any:
    return {"dimensionValues": [{"value": name}], "metricValues": [{"value": count}]}


def _event_page(rows: list[Any], row_count: int | None = None, **metadata: Any) -> dict[str, Any]:
    return {
        "dimensionHeaders": [{"name": "eventName"}],
        "metricHeaders": [{"name": "keyEvents"}],
        "rows": rows,
        "rowCount": len(rows) if row_count is None else row_count,
        "metadata": {"timeZone": ZONE, "currencyCode": "USD", **metadata},
    }


def _aggregate_report(
    shape: str,
    page: dict[str, Any],
    *,
    key_events: tuple[str, ...] = (),
    window_end: date = date(2026, 9, 3),
    now: datetime = OBSERVED,
    extra_pages: tuple[dict[str, Any], ...] = (),
    page_size: int = 1_000,
    page_bound: int = 10,
) -> Ga4AggregateReport:
    query = Ga4AggregateQuery(
        aggregate_shape=shape,  # type: ignore[arg-type]
        property_id="123",
        account_ref=PROPERTY,
        report_id="report.aggregate",
        query_contract_hash="c" * 64,
        window_start=date(2026, 8, 28),
        window_end=window_end,
        key_event_names=key_events,
        source_timezone=ZONE,
        currency="USD",
        page_size=page_size,
        page_bound=page_bound,
    )
    return _client(_serve(page, *extra_pages), now=now).run_aggregate_report(
        query, identity=_identity()
    )


def _aggregate(shape: str, page: dict[str, Any], **options: Any) -> NormalizedReport:
    return _aggregate_report(shape, page, **options).to_normalized_report()


def _traffic(
    rows: list[Any] | None = None, *, metadata: dict[str, Any] | None = None, **options: Any
) -> NormalizedReport:
    page = _traffic_page(
        rows
        if rows is not None
        else [_traffic_row("spring_sale", "google", "cpc", ("40", "30", "4"))],
        **(metadata or {}),
    )
    return _aggregate("CAMPAIGN_SOURCE_MEDIUM", page, **options)


def _key_events(
    returned: dict[str, str] | None = None,
    declared: tuple[str, ...] = ("generate_lead", "purchase"),
    *,
    metadata: dict[str, Any] | None = None,
    **options: Any,
) -> NormalizedReport:
    page = _event_page(
        [_event_row(name, count) for name, count in (returned or {}).items()], **(metadata or {})
    )
    return _aggregate("KEY_EVENT_TOTALS", page, key_events=declared, **options)


_BOTH_EVENTS = {"generate_lead": "3", "purchase": "5"}


def _key_event(
    resource_id: int,
    counting_method: str = "ONCE_PER_EVENT",
    default_value: Ga4AdminDefaultValue | None = None,
) -> Ga4AdminKeyEvent:
    return Ga4AdminKeyEvent(
        resource_ref=f"ga4_admin.key_event.{resource_id}",
        resource_client_ref=str(resource_id),
        property_ref="ga4_admin.property.123",
        configuration_fingerprint=format(resource_id, "064x"),
        event_name_fingerprint=format(resource_id + 1_000, "064x"),
        counting_method=counting_method,  # type: ignore[arg-type]
        default_value=default_value,
        custom=True,
        deletable=True,
    )


def _rule_resource(kind: str, resource_id: int) -> Ga4AdminEventRule:
    create = kind == "EVENT_CREATE_RULE"
    return Ga4AdminEventRule(
        resource_ref=f"ga4_admin.{kind.lower()}.{resource_id}",
        resource_client_ref=str(resource_id),
        property_ref="ga4_admin.property.123",
        web_stream_ref="ga4_admin.web_stream.456",
        rule_kind=kind,  # type: ignore[arg-type]
        configuration_fingerprint=format(resource_id + 2_000, "064x"),
        condition_count=1,
        parameter_mutation_count=1,
        source_copy_parameters=False if create else None,
        processing_order=None if create else str(resource_id),
    )


def _admin(
    *,
    key_events: tuple[Ga4AdminKeyEvent, ...] = (_key_event(1), _key_event(2)),
    create_rules: tuple[Ga4AdminEventRule, ...] = (),
    edit_rules: tuple[Ga4AdminEventRule, ...] = (),
    observed_at: datetime = OBSERVED,
    property_id: str = "123",
) -> RetainedGa4AdminObservation:
    return RetainedGa4AdminObservation(
        record_digest=ADMIN_DIGEST,
        observed_at=observed_at,
        inventory=Ga4AdminInventory(
            identity=Ga4AdminIdentity(
                property_ref=f"ga4_admin.property.{property_id}",
                property_client_ref=property_id,
                web_stream_ref="ga4_admin.web_stream.456",
                web_stream_client_ref="456",
            ),
            key_events=key_events,
            event_create_rules=create_rules,
            event_edit_rules=edit_rules,
        ),
    )


def _sections(
    report: NormalizedReport,
    admin: RetainedGa4AdminObservation | None = None,
    *,
    as_of: datetime = AS_OF,
    policy: AuditPolicy = POLICY,
) -> ProviderAuditSections:
    return evaluate(
        ProviderAuditContext(
            provider="ga4",
            report=report,
            source_record_digest=REPORT_DIGEST,
            policy=policy,
            as_of=as_of,
            evidence=ProviderAuditEvidence(ga4_admin=admin),
        )
    )


def _fired(sections: ProviderAuditSections) -> set[str]:
    return {item.rule_id for item in sections.findings}


def _coverage(sections: ProviderAuditSections) -> dict[str, str]:
    return {item.dimension: item.status for item in sections.coverage}


def _findings(sections: ProviderAuditSections, rule_id: str) -> list[Any]:
    return [item for item in sections.findings if item.rule_id == rule_id]


# --- registration, coverage and the honesty bounds -------------------------------------------


def test_the_rule_module_is_registered_for_on_demand_import() -> None:
    assert _RULE_MODULES["ga4"] == "adhelm.audits.ga4"
    assert provider_rule("ga4") is evaluate


def test_a_clean_report_with_its_admin_inventory_yields_only_coverage_limit_findings() -> None:
    sections = _sections(_key_events(_BOTH_EVENTS), _admin())

    assert _fired(sections) == STATIC_RULES
    assert all(item.finding.severity == "INFO" for item in sections.findings)
    assert sections.recommendations == ()


def test_every_dimension_has_an_explicit_status_and_tracking_is_never_covered() -> None:
    without_admin = _coverage(_sections(_key_events(_BOTH_EVENTS)))
    with_admin = _coverage(_sections(_key_events(_BOTH_EVENTS), _admin()))

    assert len(without_admin) == 14 == len(with_admin)
    assert without_admin["STRUCTURE"] == "NOT_READ"
    assert with_admin["STRUCTURE"] == "PARTIAL"
    for statuses in (without_admin, with_admin):
        assert statuses["TRACKING"] == "PARTIAL"
        assert statuses["NATIVE_CONVERSION"] == "NOT_APPLICABLE"
        assert statuses["BROWSER_MEASUREMENT"] == "NOT_APPLICABLE"
        assert statuses["DRIFT"] == "NOT_READ"
        assert statuses["DESTINATION_CONFIGURATION"] == "NOT_READ"
        assert statuses["POLICY"] == "NOT_READ"
        assert "COVERED" not in statuses.values()
        for dimension in ("DELIVERY", "SPEND", "BIDS", "OBJECTIVE", "TARGETING", "PLACEMENT"):
            assert statuses[dimension] == "NOT_APPLICABLE"


def test_coverage_cites_the_evidence_it_was_evaluated_from_and_nothing_else() -> None:
    sections = _sections(_key_events(_BOTH_EVENTS), _admin())
    by_dimension = {item.dimension: item for item in sections.coverage}

    tracking, structure = by_dimension["TRACKING"], by_dimension["STRUCTURE"]
    assert tracking.evidence_refs == (REPORT_DIGEST, ADMIN_DIGEST)
    assert set(tracking.evidence_sources) == {
        "PERFORMANCE_OBSERVATION",
        "RETAINED_MEASUREMENT_OBSERVATION",
    }
    assert structure.evidence_refs == (ADMIN_DIGEST,)
    assert structure.evidence_sources == ("RETAINED_MEASUREMENT_OBSERVATION",)
    for dimension in ("DRIFT", "POLICY", "DESTINATION_CONFIGURATION", "NATIVE_CONVERSION"):
        assert by_dimension[dimension].evidence_refs == ()
        assert by_dimension[dimension].evidence_record_count == 0
    # Without an Admin observation the report alone supports tracking.
    alone = {item.dimension: item for item in _sections(_key_events(_BOTH_EVENTS)).coverage}
    assert alone["TRACKING"].evidence_refs == (REPORT_DIGEST,)


def test_a_reconciled_exhaustive_report_is_still_never_labelled_exhaustive() -> None:
    aggregate = _aggregate_report(
        "KEY_EVENT_TOTALS",
        _event_page([_event_row(name, count) for name, count in _BOTH_EVENTS.items()]),
        key_events=("generate_lead", "purchase"),
        window_end=date(2026, 9, 4),
    )
    assert aggregate.exhaustive() is True

    sections = _sections(aggregate.to_normalized_report(), _admin())

    # The window finding rests on the report, which GA4 reconciled completely, yet
    # the audit never labels a retained report exhaustive.
    window = _findings(sections, "GA4_WINDOW_INCLUDES_CURRENT_PROPERTY_DATE")
    assert [item.analytics_completeness for item in window] == ["BOUNDED_NON_EXHAUSTIVE"]
    assert sections.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"
    assert {item.analytics_completeness for item in sections.recommendations} == {
        "BOUNDED_NON_EXHAUSTIVE"
    }


def test_evaluation_is_deterministic_and_makes_no_request_or_ledger_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import adhelm.ledger as ledger_module
    import adhelm.transport as transport_module

    report = _key_events({"generate_lead": "3"})
    admin = _admin(key_events=(_key_event(1),))
    monkeypatch.setattr(
        transport_module.SafeHttpClient,
        "request",
        lambda *_a, **_k: pytest.fail("a GA4 audit must make no provider request"),
    )
    monkeypatch.setattr(
        ledger_module.Ledger,
        "append",
        lambda *_a, **_k: pytest.fail("a GA4 audit must append no record"),
    )

    first, second = _sections(report, admin), _sections(report, admin)

    assert canonical_bytes(first) == canonical_bytes(second)
    assert first.live_provider_reads == "NONE_RETAINED_RECORDS_ONLY"
    assert first.rule_table_version == RULE_TABLE_VERSION


def test_the_rule_table_version_is_part_of_every_finding_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import adhelm.audits.ga4 as module

    report = _key_events(_BOTH_EVENTS)
    before = _sections(report)
    monkeypatch.setattr(module, "RULE_TABLE_VERSION", "ga4.rules.other")
    after = _sections(report)

    assert before.findings == tuple(
        sorted(before.findings, key=lambda item: item.finding.finding_id)
    )
    assert {item.finding.finding_id for item in before.findings}.isdisjoint(
        {item.finding.finding_id for item in after.findings}
    )


# --- Data API report quality: never a zero --------------------------------------------------


def test_thresholding_withholds_a_declared_key_event_and_says_it_is_not_zero() -> None:
    report = _key_events({"purchase": "5"}, metadata={"subjectToThresholding": True})

    sections = _sections(report)

    assert {"GA4_REPORT_THRESHOLDED", "GA4_KEY_EVENT_THRESHOLD_WITHHELD"} <= _fired(sections)
    assert "GA4_KEY_EVENT_NOT_RETURNED" not in _fired(sections)
    withheld = _findings(sections, "GA4_KEY_EVENT_THRESHOLD_WITHHELD")
    assert [item.finding.resource_refs for item in withheld] == [("ga4.event.generate_lead",)]
    assert "not zero" in withheld[0].finding.observation


def test_a_key_event_with_no_row_and_no_thresholding_is_not_returned_rather_than_zero() -> None:
    report = _key_events({"purchase": "5"})

    sections = _sections(report)

    assert "GA4_KEY_EVENT_NOT_RETURNED" in _fired(sections)
    assert "GA4_REPORT_THRESHOLDED" not in _fired(sections)
    assert "GA4_KEY_EVENT_THRESHOLD_WITHHELD" not in _fired(sections)
    missing = _findings(sections, "GA4_KEY_EVENT_NOT_RETURNED")
    assert [item.finding.resource_refs for item in missing] == [("ga4.event.generate_lead",)]
    assert "not a measured zero" in missing[0].finding.observation
    assert all(item.finding.severity == "MEDIUM" for item in missing)


def test_a_key_event_that_ga4_reported_as_zero_is_not_flagged_as_missing() -> None:
    report = _key_events({"generate_lead": "0", "purchase": "5"})

    sections = _sections(report)

    assert "GA4_KEY_EVENT_NOT_RETURNED" not in _fired(sections)
    assert "GA4_KEY_EVENT_THRESHOLD_WITHHELD" not in _fired(sections)


def test_sampling_is_reported_as_estimates() -> None:
    sampled = _traffic(
        metadata={"samplingMetadatas": [{"samplesReadCount": "10", "samplingSpaceSize": "100"}]}
    )
    assert "GA4_REPORT_SAMPLED" in _fired(_sections(sampled))
    assert "GA4_REPORT_SAMPLED" not in _fired(_sections(_traffic()))


def test_an_other_row_rollup_is_reported_without_reading_it_as_a_campaign() -> None:
    report = _traffic(
        [
            _traffic_row("spring_sale", "google", "cpc", ("40", "30", "4")),
            _traffic_row("(other)", "(other)", "(other)", ("9", "8", "0")),
        ]
    )

    assert "GA4_REPORT_OTHER_ROW_ROLLUP" in _fired(_sections(report))
    assert "GA4_REPORT_OTHER_ROW_ROLLUP" not in _fired(_sections(_traffic()))


def test_metadata_flagged_data_loss_from_the_other_row_is_reported_too() -> None:
    report = _traffic(metadata={"dataLossFromOtherRow": True})
    assert "GA4_REPORT_OTHER_ROW_ROLLUP" in _fired(_sections(report))


def test_restricted_metrics_are_unavailable_not_zero() -> None:
    restricted = {
        "schemaRestrictionResponse": {
            "activeMetricRestrictions": [
                {"metricName": "keyEvents", "restrictedMetricTypes": ["REVENUE_DATA"]}
            ]
        }
    }
    report = _traffic(metadata=restricted)

    sections = _sections(report)

    assert "GA4_REPORT_METRICS_RESTRICTED" in _fired(sections)
    assert (
        "unavailable, not zero"
        in _findings(sections, "GA4_REPORT_METRICS_RESTRICTED")[0].finding.observation
    )


def test_a_page_bound_exhausted_before_the_declared_rows_is_an_incomplete_report() -> None:
    row = _traffic_row("a", "google", "cpc", ("4", "3", "1"))
    first = _traffic_page([row], row_count=3)
    report = _aggregate(
        "CAMPAIGN_SOURCE_MEDIUM",
        first,
        extra_pages=(
            _traffic_page([_traffic_row("b", "google", "cpc", ("4", "3", "1"))], row_count=3),
        ),
        page_size=1,
        page_bound=2,
    )

    sections = _sections(report)

    assert "GA4_REPORT_INCOMPLETE" in _fired(sections)
    assert _findings(sections, "GA4_REPORT_INCOMPLETE")[0].finding.severity == "HIGH"
    assert any(
        item.operation_kind == "OBSERVE" and "GA4_REPORT_INCOMPLETE" in item.bound_rule_ids
        for item in sections.recommendations
    )


def test_a_complete_page_walk_is_not_reported_incomplete() -> None:
    assert "GA4_REPORT_INCOMPLETE" not in _fired(_sections(_traffic()))


def test_a_provider_refusal_is_not_evidence_that_nothing_happened() -> None:
    report = _traffic([], metadata={"emptyReason": "QUOTA_OR_POLICY"})
    assert report.performance_rows == []

    sections = _sections(report)

    assert {"GA4_REPORT_NOT_AVAILABLE_FROM_PROVIDER", "GA4_REPORT_NO_ROWS"} <= _fired(sections)


def test_an_empty_thresholded_report_reports_both_facts_from_its_envelope() -> None:
    report = _traffic([], metadata={"subjectToThresholding": True})

    sections = _sections(report)

    # A zero-row report persists no per-row completeness facts, so the audit
    # relies on the envelope statuses that survive.
    assert {"GA4_REPORT_THRESHOLDED", "GA4_REPORT_NO_ROWS"} <= _fired(sections)
    assert "GA4_REPORT_NOT_AVAILABLE_FROM_PROVIDER" not in _fired(sections)


def test_an_empty_report_without_a_reason_is_reported_as_no_data_not_zero() -> None:
    sections = _sections(_traffic([]))
    assert "GA4_REPORT_NO_ROWS" in _fired(sections)
    assert "not a measured zero" in _findings(sections, "GA4_REPORT_NO_ROWS")[0].finding.observation


# --- date boundaries and freshness ------------------------------------------------------------


def test_a_window_ending_on_the_property_local_observation_date_is_provisional() -> None:
    report = _key_events(_BOTH_EVENTS, window_end=date(2026, 9, 4))
    assert "GA4_WINDOW_INCLUDES_CURRENT_PROPERTY_DATE" in _fired(_sections(report))


def test_a_window_that_closed_before_the_observation_date_is_not_provisional() -> None:
    report = _key_events(_BOTH_EVENTS, window_end=date(2026, 9, 3))
    assert "GA4_WINDOW_INCLUDES_CURRENT_PROPERTY_DATE" not in _fired(_sections(report))


def test_the_observation_date_is_taken_in_the_property_timezone_not_utc() -> None:
    # 02:00 UTC on 4 September is 21:00 on 3 September in Chicago, so a window
    # ending on the 3rd still ends on the property-local observation date even
    # though the UTC date has already moved on.
    late_evening = datetime(2026, 9, 4, 2, tzinfo=UTC)
    on_local_date = _key_events(_BOTH_EVENTS, window_end=date(2026, 9, 3), now=late_evening)
    before_local_date = _key_events(_BOTH_EVENTS, window_end=date(2026, 9, 2), now=late_evening)

    assert "GA4_WINDOW_INCLUDES_CURRENT_PROPERTY_DATE" in _fired(_sections(on_local_date))
    assert "GA4_WINDOW_INCLUDES_CURRENT_PROPERTY_DATE" not in _fired(_sections(before_local_date))


def test_an_invalid_report_timezone_is_refused_rather_than_guessed() -> None:
    report = _key_events(_BOTH_EVENTS)
    broken = report.model_copy(
        update={"envelope": report.envelope.model_copy(update={"source_timezone": "Not/A_Zone"})}
    )
    with pytest.raises(AdhelmError) as error:
        _sections(broken)
    assert error.value.code == "INVALID_AUDIT_INPUT"


# --- joins, traffic tagging, and other report kinds -------------------------------------------


def test_unjoined_campaign_rows_stay_visible_and_excluded_from_authoritative_totals() -> None:
    sections = _sections(_traffic())
    assert "GA4_ROWS_JOIN_UNRESOLVED" in _fired(sections)
    assert "excluded from authoritative joined totals" in (
        _findings(sections, "GA4_ROWS_JOIN_UNRESOLVED")[0].finding.observation
    )
    # The property-wide key-event totals are not campaign rows, so they have no join to resolve.
    assert "GA4_ROWS_JOIN_UNRESOLVED" not in _fired(_sections(_key_events(_BOTH_EVENTS)))


def _with_join_status(report: NormalizedReport, status: str) -> NormalizedReport:
    rows = [row.model_copy(update={"join_status": status}) for row in report.performance_rows]
    return report.model_copy(update={"performance_rows": rows})


def test_a_join_conflict_is_reported_and_a_declared_join_is_not() -> None:
    conflict = _sections(_with_join_status(_traffic(), "CONFLICT"))
    declared = _sections(_with_join_status(_traffic(), "EXPLICIT_MAPPING"))
    heuristic = _sections(_with_join_status(_traffic(), "HEURISTIC"))

    assert "GA4_ROWS_JOIN_CONFLICT" in _fired(conflict)
    assert "GA4_ROWS_JOIN_UNRESOLVED" not in _fired(conflict)
    # A heuristic proposal is never applied, so it stays unresolved.
    assert "GA4_ROWS_JOIN_UNRESOLVED" in _fired(heuristic)
    assert not {"GA4_ROWS_JOIN_UNRESOLVED", "GA4_ROWS_JOIN_CONFLICT"} & _fired(declared)


def test_sessions_with_a_source_and_medium_but_no_campaign_name_are_flagged() -> None:
    untagged = _traffic([_traffic_row("(not set)", "facebook", "paid_social", ("12", "10", "0"))])
    assert "GA4_SESSIONS_WITHOUT_CAMPAIGN_NAME" in _fired(_sections(untagged))


@pytest.mark.parametrize(
    ("campaign", "source", "medium"),
    [
        ("(not set)", "(direct)", "(none)"),
        ("(not set)", "(not set)", "(not set)"),
        ("(not set)", "(other)", "(other)"),
        ("spring_sale", "facebook", "paid_social"),
    ],
)
def test_traffic_that_needs_no_campaign_name_is_not_flagged(
    campaign: str, source: str, medium: str
) -> None:
    report = _traffic([_traffic_row(campaign, source, medium, ("12", "10", "0"))])
    assert "GA4_SESSIONS_WITHOUT_CAMPAIGN_NAME" not in _fired(_sections(report))


def test_a_withheld_session_count_is_not_read_as_untagged_traffic() -> None:
    report = _traffic([_traffic_row("(not set)", "facebook", "paid_social", ("12", "10", "0"))])
    withheld = [row.model_copy(update={"sessions": None}) for row in report.performance_rows]
    assert "GA4_SESSIONS_WITHOUT_CAMPAIGN_NAME" not in _fired(
        _sections(report.model_copy(update={"performance_rows": withheld}))
    )


def _standard_report(
    metadata: dict[str, Any] | None = None, *, rows: bool = True
) -> NormalizedReport:
    page = {
        "dimensionHeaders": [{"name": "sessionSource"}],
        "metricHeaders": [{"name": "sessions"}],
        "rows": (
            [{"dimensionValues": [{"value": "google"}], "metricValues": [{"value": "9"}]}]
            if rows
            else []
        ),
        "rowCount": 1 if rows else 0,
        "metadata": {
            "timeZone": ZONE,
            "currencyCode": "USD",
            **({"subjectToThresholding": True} if metadata is None else metadata),
        },
    }
    query = Ga4ReportQuery(
        property_id="123",
        account_ref=PROPERTY,
        report_id="report.standard",
        query_contract_hash="c" * 64,
        window_start=date(2026, 8, 28),
        window_end=date(2026, 9, 3),
        dimensions=["sessionSource"],
        metrics=["sessions"],
        source_timezone=ZONE,
    )
    return _client(_serve(page)).run_report(query, identity=_identity())


def test_a_standard_report_states_that_its_completeness_facts_are_not_retained() -> None:
    sections = _sections(_standard_report())

    assert "GA4_REPORT_COMPLETENESS_FACTS_NOT_RETAINED" in _fired(sections)
    # The envelope status survives, so thresholding is still reported.
    assert "GA4_REPORT_THRESHOLDED" in _fired(sections)
    assert not {"GA4_REPORT_OTHER_ROW_ROLLUP", "GA4_REPORT_INCOMPLETE"} & _fired(sections)
    assert any(
        "GA4_REPORT_COMPLETENESS_FACTS_NOT_RETAINED" in item.bound_rule_ids
        for item in sections.recommendations
    )


_RESTRICTION = {
    "schemaRestrictionResponse": {
        "activeMetricRestrictions": [
            {"metricName": "sessions", "restrictedMetricTypes": ["REVENUE_DATA"]}
        ]
    }
}
_FLAGGED = "GA4_REPORT_PROVIDER_FLAGGED_UNKNOWN"
_FLAGGED_UNATTRIBUTABLE = "GA4_REPORT_PROVIDER_FLAGGED_UNATTRIBUTABLE"


def test_a_standard_report_keeps_a_roll_up_and_a_restriction_ga4_flagged_but_cannot_name() -> None:
    report = _standard_report({"dataLossFromOtherRow": True, **_RESTRICTION})
    assert report.envelope.data_quality_statuses == ["PROVIDER_REPORTED_UNKNOWN"]

    sections = _sections(report)

    assert _FLAGGED in _fired(sections)
    # The standard read persists no roll-up or restriction fact, so the finer rules stay silent.
    assert not {"GA4_REPORT_OTHER_ROW_ROLLUP", "GA4_REPORT_METRICS_RESTRICTED"} & _fired(sections)
    finding = _findings(sections, _FLAGGED)[0]
    assert finding.finding.severity == "MEDIUM"
    assert finding.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"
    assert "not zero" in finding.finding.observation
    assert "standard or Realtime read keeps no roll-up or restriction fact" in (
        finding.finding.observation
    )
    assert "read a fixed aggregate shape" in finding.finding.suggested_action
    assert _FLAGGED_UNATTRIBUTABLE not in _fired(sections)
    declined = [item for item in sections.no_recommendations if _FLAGGED in item.bound_rule_ids]
    assert len(declined) == 1
    assert declined[0].reason == "EVIDENCE_INSUFFICIENT"
    assert not [item for item in sections.recommendations if _FLAGGED in item.bound_rule_ids]


def test_a_zero_row_aggregate_with_a_restriction_keeps_the_flag_it_cannot_attribute() -> None:
    report = _traffic([], metadata=_RESTRICTION)
    assert report.performance_rows == []
    assert report.envelope.data_quality_statuses == ["PROVIDER_REPORTED_UNKNOWN"]

    sections = _sections(report)

    # The empty report has its own variant: the standard-read rule's reason and its
    # advice (read a fixed aggregate shape) would be wrong here.
    assert _FLAGGED not in _fired(sections)
    assert _FLAGGED_UNATTRIBUTABLE in _fired(sections)
    assert "GA4_REPORT_NO_ROWS" in _fired(sections)
    finding = _findings(sections, _FLAGGED_UNATTRIBUTABLE)[0]
    text = f"{finding.finding.observation} {finding.finding.suggested_action}".lower()
    # The aggregate kind does retain the fact; only the missing row loses it, so the
    # prose must say that and must not claim the report kind lacks the fact.
    assert "no row to carry" in finding.finding.observation
    assert "this report kind" not in text
    assert "read a fixed aggregate shape" not in text
    assert "standard or realtime read" not in text
    assert "this read cannot attribute the flag" in finding.finding.suggested_action.lower()
    declined = [
        item
        for item in sections.no_recommendations
        if _FLAGGED_UNATTRIBUTABLE in item.bound_rule_ids
    ]
    assert len(declined) == 1
    assert declined[0].reason == "EVIDENCE_INSUFFICIENT"


def test_a_zero_row_standard_report_with_a_flag_is_unattributable_not_unknown() -> None:
    report = _standard_report({"dataLossFromOtherRow": True}, rows=False)
    assert report.performance_rows == []
    assert report.envelope.data_quality_statuses == ["PROVIDER_REPORTED_UNKNOWN"]

    sections = _sections(report)

    fired = _fired(sections)
    # With no row there is nothing to carry a roll-up fact, so the finer rule stays
    # silent, and the standard-read variant's "read a fixed aggregate shape" advice
    # would send the user to a read this report cannot be attributed by.
    assert "GA4_REPORT_OTHER_ROW_ROLLUP" not in fired
    assert _FLAGGED not in fired
    assert _FLAGGED_UNATTRIBUTABLE in fired
    assert "GA4_REPORT_NO_ROWS" in fired
    finding = _findings(sections, _FLAGGED_UNATTRIBUTABLE)[0]
    text = f"{finding.finding.observation} {finding.finding.suggested_action}".lower()
    assert "read a fixed aggregate shape" not in text
    assert "this read cannot attribute the flag" in text


def test_an_aggregate_with_rows_and_an_unnamed_flag_does_not_send_the_user_to_repeat_it() -> None:
    # The adapter sets PROVIDER_REPORTED_UNKNOWN whenever schemaRestrictionResponse is
    # present, but persists a restriction extension only when a metric is named.
    report = _traffic(metadata={"schemaRestrictionResponse": {"activeMetricRestrictions": []}})
    assert report.performance_rows
    assert report.envelope.data_quality_statuses == ["PROVIDER_REPORTED_UNKNOWN"]
    assert report.performance_rows[0].provider_extensions["ga4_completeness"] == "COMPLETE"

    sections = _sections(report)

    fired = _fired(sections)
    assert (
        not {
            "GA4_REPORT_OTHER_ROW_ROLLUP",
            "GA4_REPORT_METRICS_RESTRICTED",
            "GA4_REPORT_SAMPLING_METADATA_INCONSISTENT",
        }
        & fired
    )
    # This is a fixed aggregate shape, so the standard-read variant and its advice are wrong.
    assert _FLAGGED not in fired
    assert _FLAGGED_UNATTRIBUTABLE in fired
    assert "GA4_REPORT_NO_ROWS" not in fired
    finding = _findings(sections, _FLAGGED_UNATTRIBUTABLE)[0]
    text = f"{finding.finding.observation} {finding.finding.suggested_action}".lower()
    assert "standard or realtime read" not in text
    assert "read a fixed aggregate shape" not in text
    assert "this read cannot attribute the flag" in text
    # Rows are present here, so the advice must not blame missing rows.
    assert "missing rows" not in text
    declined = [
        item
        for item in sections.no_recommendations
        if _FLAGGED_UNATTRIBUTABLE in item.bound_rule_ids
    ]
    assert len(declined) == 1
    assert declined[0].reason == "EVIDENCE_INSUFFICIENT"
    assert not [
        item for item in sections.recommendations if _FLAGGED_UNATTRIBUTABLE in item.bound_rule_ids
    ]


def test_a_flag_that_a_finer_rule_explains_does_not_fire_the_generic_rule_as_well() -> None:
    rolled_up = _traffic(metadata={"dataLossFromOtherRow": True})
    restricted = _traffic(metadata=_RESTRICTION)

    assert "GA4_REPORT_OTHER_ROW_ROLLUP" in _fired(_sections(rolled_up))
    assert "GA4_REPORT_METRICS_RESTRICTED" in _fired(_sections(restricted))
    for explained in (rolled_up, restricted):
        assert "PROVIDER_REPORTED_UNKNOWN" in explained.envelope.data_quality_statuses
        assert _FLAGGED not in _fired(_sections(explained))


def test_a_clean_standard_report_does_not_claim_ga4_flagged_anything() -> None:
    report = _standard_report({})
    assert "PROVIDER_REPORTED_UNKNOWN" not in report.envelope.data_quality_statuses

    fired = _fired(_sections(report))

    assert _FLAGGED not in fired
    assert "GA4_REPORT_SAMPLING_METADATA_INCONSISTENT" not in fired
    assert "GA4_REPORT_COMPLETENESS_FACTS_NOT_RETAINED" in fired


def _paged_sampling(*windows: dict[str, str] | None) -> NormalizedReport:
    row_values = ("4", "3", "1")
    pages = [
        _traffic_page(
            [_traffic_row(f"campaign_{index}", "google", "cpc", row_values)],
            row_count=len(windows),
            **({"samplingMetadatas": [window]} if window is not None else {}),
        )
        for index, window in enumerate(windows)
    ]
    return _aggregate("CAMPAIGN_SOURCE_MEDIUM", pages[0], extra_pages=tuple(pages[1:]), page_size=1)


def test_pages_that_disagree_on_sampling_state_that_the_sample_ratio_is_unreliable() -> None:
    report = _paged_sampling(
        {"samplesReadCount": "10", "samplingSpaceSize": "100"},
        {"samplesReadCount": "20", "samplingSpaceSize": "100"},
    )
    assert {
        row.provider_extensions["ga4_sampling_consistency"] for row in report.performance_rows
    } == {"SAMPLING_METADATA_INCONSISTENT"}

    sections = _sections(report)

    fired = _fired(sections)
    # Sampling is still reported, and the inconsistency is a distinct finding.
    assert {"GA4_REPORT_SAMPLED", "GA4_REPORT_SAMPLING_METADATA_INCONSISTENT"} <= fired
    # The inconsistency explains the envelope status, so the generic flag stays silent.
    assert _FLAGGED not in fired
    assert (
        "unreliable"
        in _findings(sections, "GA4_REPORT_SAMPLING_METADATA_INCONSISTENT")[0].finding.observation
    )
    reread = [
        item
        for item in sections.recommendations
        if "GA4_REPORT_SAMPLING_METADATA_INCONSISTENT" in item.bound_rule_ids
    ]
    assert len(reread) == 1
    assert reread[0].operation_kind == "OBSERVE"


def test_pages_that_agree_on_sampling_are_not_reported_inconsistent() -> None:
    window = {"samplesReadCount": "10", "samplingSpaceSize": "100"}

    fired = _fired(_sections(_paged_sampling(window, window)))

    assert "GA4_REPORT_SAMPLED" in fired
    assert "GA4_REPORT_SAMPLING_METADATA_INCONSISTENT" not in fired
    assert _FLAGGED not in fired


def test_an_aggregate_report_does_not_claim_its_facts_are_missing() -> None:
    assert "GA4_REPORT_COMPLETENESS_FACTS_NOT_RETAINED" not in _fired(_sections(_traffic()))


def test_a_realtime_report_is_not_treated_as_processed_data() -> None:
    page = {
        "dimensionHeaders": [{"name": "eventName"}],
        "metricHeaders": [{"name": "eventCount"}],
        "rows": [{"dimensionValues": [{"value": "purchase"}], "metricValues": [{"value": "2"}]}],
        "rowCount": 1,
        "metadata": {"timeZone": ZONE, "currencyCode": "USD"},
    }
    query = Ga4RealtimeQuery(
        property_id="123",
        account_ref=PROPERTY,
        report_id="report.realtime",
        query_contract_hash="c" * 64,
        allowed_event_names=["purchase"],
        source_timezone=ZONE,
    )
    report = _client(_serve(page)).run_realtime_report(query, identity=_identity())

    sections = _sections(report)

    assert "GA4_REPORT_REALTIME_NOT_PROCESSED" in _fired(sections)
    # Realtime windows are the observation date by construction; that is not a finding.
    assert "GA4_WINDOW_INCLUDES_CURRENT_PROPERTY_DATE" not in _fired(sections)
    assert any(
        "GA4_REPORT_REALTIME_NOT_PROCESSED" in item.bound_rule_ids
        for item in sections.recommendations
    )


# --- Admin configuration and the cross-read -------------------------------------------------


def test_a_property_with_no_key_events_is_reported_and_no_creation_is_recommended() -> None:
    sections = _sections(_key_events({}), _admin(key_events=()))

    assert "GA4_NO_KEY_EVENTS_CONFIGURED" in _fired(sections)
    declined = [
        item for item in sections.no_recommendations if item.considered_operation_kind == "CREATE"
    ]
    assert len(declined) == 1
    assert declined[0].reason == "EVIDENCE_INSUFFICIENT"
    assert "GA4 key event is not a provider conversion" in declined[0].detail
    assert all(item.operation_kind == "OBSERVE" for item in sections.recommendations)


def test_key_events_with_an_unspecified_counting_method_are_flagged_one_by_one() -> None:
    admin = _admin(
        key_events=(
            _key_event(1, "ONCE_PER_EVENT"),
            _key_event(2, "COUNTING_METHOD_UNSPECIFIED"),
            _key_event(3, "ONCE_PER_SESSION"),
            _key_event(4, "COUNTING_METHOD_UNSPECIFIED"),
        )
    )

    sections = _sections(_key_events(_BOTH_EVENTS), admin)

    flagged = _findings(sections, "GA4_KEY_EVENT_COUNTING_METHOD_UNSPECIFIED")
    assert sorted(item.finding.resource_refs for item in flagged) == [
        ("ga4_admin.key_event.2",),
        ("ga4_admin.key_event.4",),
    ]
    assert all(item.finding.evidence_refs == (ADMIN_DIGEST,) for item in flagged)


def test_event_rules_are_reported_as_opaque_and_their_absence_is_not() -> None:
    with_rules = _admin(
        create_rules=(_rule_resource("EVENT_CREATE_RULE", 7),),
        edit_rules=(_rule_resource("EVENT_EDIT_RULE", 8),),
    )
    only_edit = _admin(edit_rules=(_rule_resource("EVENT_EDIT_RULE", 8),))

    assert "GA4_EVENT_RULES_CONTENT_NOT_INSPECTED" in _fired(
        _sections(_key_events(_BOTH_EVENTS), with_rules)
    )
    assert "GA4_EVENT_RULES_CONTENT_NOT_INSPECTED" in _fired(
        _sections(_key_events(_BOTH_EVENTS), only_edit)
    )
    assert "GA4_EVENT_RULES_CONTENT_NOT_INSPECTED" not in _fired(
        _sections(_key_events(_BOTH_EVENTS), _admin())
    )


def test_an_admin_inventory_older_than_the_declared_maximum_age_is_stale() -> None:
    maximum = timedelta(seconds=POLICY.maximum_observation_age_seconds)
    report = _key_events(_BOTH_EVENTS)
    fresh = _admin(observed_at=AS_OF - maximum)
    stale = _admin(observed_at=AS_OF - maximum - timedelta(seconds=1))

    # The boundary is exclusive: exactly the declared maximum age is still fresh.
    assert "GA4_ADMIN_INVENTORY_STALE" not in _fired(_sections(report, fresh))
    assert "GA4_ADMIN_INVENTORY_STALE" in _fired(_sections(report, stale))
    assert any(
        "GA4_ADMIN_INVENTORY_STALE" in item.bound_rule_ids
        for item in _sections(report, stale).recommendations
    )


def test_staleness_comes_only_from_the_customer_declared_policy() -> None:
    report = _key_events(_BOTH_EVENTS)
    old = _admin(observed_at=OBSERVED - timedelta(days=60))
    lenient = AuditPolicy(
        maximum_observation_age_seconds=365 * 86_400, require_conversion_evidence=False
    )
    assert "GA4_ADMIN_INVENTORY_STALE" not in _fired(_sections(report, old, policy=lenient))


@pytest.mark.parametrize("admin_key_events", [1, 3])
def test_a_differing_declared_key_event_count_cites_both_records(admin_key_events: int) -> None:
    # Fewer and more key events in the inventory than the report declared both
    # differ: the report may predate a change, or be built from a subset.
    admin = _admin(key_events=tuple(_key_event(index) for index in range(1, admin_key_events + 1)))

    sections = _sections(_key_events(_BOTH_EVENTS), admin)

    mismatch = _findings(sections, "GA4_KEY_EVENT_COUNT_DIFFERS_FROM_ADMIN")
    assert len(mismatch) == 1
    assert mismatch[0].finding.evidence_refs == (REPORT_DIGEST, ADMIN_DIGEST)
    assert mismatch[0].finding.confidence == "LOW"
    assert "fingerprints" in mismatch[0].finding.observation
    assert any(
        "GA4_KEY_EVENT_COUNT_DIFFERS_FROM_ADMIN" in item.bound_rule_ids
        for item in sections.recommendations
    )


def test_a_matching_count_a_traffic_report_or_a_missing_inventory_has_nothing_to_compare() -> None:
    assert "GA4_KEY_EVENT_COUNT_DIFFERS_FROM_ADMIN" not in _fired(
        _sections(_key_events(_BOTH_EVENTS), _admin())
    )
    # Only the property-wide totals fix the declared universe.
    assert "GA4_KEY_EVENT_COUNT_DIFFERS_FROM_ADMIN" not in _fired(
        _sections(_traffic(), _admin(key_events=(_key_event(1),)))
    )
    assert "GA4_KEY_EVENT_COUNT_DIFFERS_FROM_ADMIN" not in _fired(
        _sections(_key_events(_BOTH_EVENTS))
    )


def test_a_missing_admin_inventory_is_named_not_assumed_healthy() -> None:
    sections = _sections(_key_events(_BOTH_EVENTS))

    assert "GA4_ADMIN_INVENTORY_NOT_RETAINED" in _fired(sections)
    assert _findings(sections, "GA4_ADMIN_INVENTORY_NOT_RETAINED")[0].coverage_status == "NOT_READ"
    assert not {
        "GA4_NO_KEY_EVENTS_CONFIGURED",
        "GA4_KEY_EVENT_COUNTING_METHOD_UNSPECIFIED",
        "GA4_EVENT_RULES_CONTENT_NOT_INSPECTED",
        "GA4_ADMIN_INVENTORY_STALE",
    } & _fired(sections)
    assert "GA4_ADMIN_INVENTORY_NOT_RETAINED" not in _fired(
        _sections(_key_events(_BOTH_EVENTS), _admin())
    )


# --- recommendations are observation-only and bounded -----------------------------------------


def test_every_recommendation_is_an_unexecuted_observation_with_no_plan_or_authority() -> None:
    admin = _admin(
        key_events=(_key_event(1, "COUNTING_METHOD_UNSPECIFIED"),),
        observed_at=OBSERVED - timedelta(days=60),
        edit_rules=(_rule_resource("EVENT_EDIT_RULE", 8),),
    )
    report = _key_events({"purchase": "5"}, window_end=date(2026, 9, 4))

    sections = _sections(report, admin)

    assert sections.recommendations
    for item in sections.recommendations:
        assert item.operation_kind == "OBSERVE"
        assert item.execution_status == "NOT_EXECUTED"
        assert item.plan_status == "NOT_A_PLAN"
        assert item.authorization_status == "NO_AUTHORIZATION_DIGEST_PRESENT"
        assert item.recommendation.proposed_plan_ref is None
        assert item.recommendation.spend_impact_minor is None
        assert item.recommendation.risk == "LOW"
        assert item.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"
        finding_ids = {finding.finding.finding_id for finding in sections.findings}
        assert set(item.finding_ids) <= finding_ids


def test_a_not_returned_key_event_recommends_a_later_processed_read_and_assumes_no_zero() -> None:
    sections = _sections(_key_events({"purchase": "5"}))

    processed = [
        item
        for item in sections.recommendations
        if "GA4_KEY_EVENT_NOT_RETURNED" in item.bound_rule_ids
    ]
    assert len(processed) == 1
    assert any("not a measured zero" in text for text in processed[0].recommendation.assumptions)
    assert "processing" in processed[0].recommendation.hypothesis


def test_quality_properties_of_how_ga4_answered_are_declined_not_recommended() -> None:
    report = _key_events({"purchase": "5"}, metadata={"subjectToThresholding": True})

    sections = _sections(report)

    assert not [
        item for item in sections.recommendations if "GA4_REPORT_THRESHOLDED" in item.bound_rule_ids
    ]
    declined = [
        item
        for item in sections.no_recommendations
        if "GA4_REPORT_THRESHOLDED" in item.bound_rule_ids
    ]
    assert len(declined) == 1
    assert declined[0].reason == "EVIDENCE_INSUFFICIENT"


def test_unread_dimensions_yield_a_coverage_decline_and_never_a_recommendation() -> None:
    sections = _sections(_key_events(_BOTH_EVENTS), _admin())

    declined = [item for item in sections.no_recommendations if item.reason == "COVERAGE_NOT_READ"]
    assert len(declined) == 1
    assert set(declined[0].bound_rule_ids) == STATIC_RULES
    assert sections.recommendations == ()


def test_more_findings_than_one_row_can_name_are_bounded_explicitly_and_none_is_dropped() -> None:
    names = tuple(f"key_event_{index:02d}" for index in range(MAXIMUM_INLINE_FINDING_IDS + 10))
    report = _key_events({}, declared=names)

    sections = _sections(report)

    missing = _findings(sections, "GA4_KEY_EVENT_NOT_RETURNED")
    assert len(missing) == len(names)
    processed = next(
        item
        for item in sections.recommendations
        if "GA4_KEY_EVENT_NOT_RETURNED" in item.bound_rule_ids
    )
    assert len(processed.finding_ids) == MAXIMUM_INLINE_FINDING_IDS
    assert processed.bound_finding_count == len(names)
    exceeded = [
        item for item in sections.bound_exceeded if item.scope == "RECOMMENDATION_FINDING_IDS"
    ]
    assert len(exceeded) == 1
    assert exceeded[0].total_finding_count == len(names)
    assert exceeded[0].omitted_finding_count == 10
    assert {item.finding.finding_id for item in missing} >= set(processed.finding_ids)


# --- the shared dispatch and the provider-neutral audit ----------------------------------------


def _audit(report: NormalizedReport, admin: RetainedGa4AdminObservation | None = None) -> Any:
    return audit_provider(
        report,
        source_record_digest=REPORT_DIGEST,
        policy=POLICY,
        as_of=AS_OF,
        evidence=ProviderAuditEvidence(ga4_admin=admin),
    )


def test_audit_provider_dispatches_a_ga4_report_to_the_rule_table() -> None:
    result = _audit(_key_events(_BOTH_EVENTS), _admin())

    assert result.rules_status == "EVALUATED"
    assert result.provider == "ga4"
    assert result.sections is not None
    assert result.sections.rule_table_version == RULE_TABLE_VERSION
    # The provider-neutral result is embedded unchanged.
    assert result.audit == audit_report(
        _key_events(_BOTH_EVENTS), source_record_digest=REPORT_DIGEST, policy=POLICY, as_of=AS_OF
    )


@pytest.mark.parametrize("conversion_source", ["GA4", "HUBSPOT", "SALESFORCE"])
def test_the_neutral_audit_does_not_count_a_non_provider_outcome_as_native_conversion_evidence(
    conversion_source: str,
) -> None:
    report = _key_events(_BOTH_EVENTS)
    assert report.conversion_rows
    outcome_rows = [
        row.model_copy(update={"conversion_source": conversion_source})
        for row in report.conversion_rows
    ]

    neutral = audit_report(
        report.model_copy(update={"conversion_rows": outcome_rows}),
        source_record_digest=REPORT_DIGEST,
        policy=POLICY,
        as_of=AS_OF,
    )

    status = {item.dimension: item.status for item in neutral.coverage}
    assert status["NATIVE_CONVERSION"] == "EVIDENCE_MISSING"


def test_a_provider_sourced_conversion_row_still_gives_partial_native_conversion_coverage() -> None:
    report = _key_events(_BOTH_EVENTS)
    provider_row = ConversionBreakdownRow(
        report_id=report.envelope.report_id,
        conversion_source="PROVIDER",
        conversion_kind="NATIVE",
        attribution_model="PROVIDER",
        conversion_count=1,
    )

    neutral = audit_report(
        report.model_copy(update={"conversion_rows": [provider_row]}),
        source_record_digest=REPORT_DIGEST,
        policy=POLICY,
        as_of=AS_OF,
    )

    assert {item.dimension: item.status for item in neutral.coverage}[
        "NATIVE_CONVERSION"
    ] == "PARTIAL"


def test_admin_evidence_for_another_property_is_refused_rather_than_mixed() -> None:
    other = _admin(property_id="999")
    with pytest.raises(AdhelmError) as error:
        _audit(_key_events(_BOTH_EVENTS), other)
    assert error.value.code == "INVALID_AUDIT_INPUT"
    assert error.value.exit_code == 2
    assert "belongs to a different property than the report" in str(error.value)


def test_admin_evidence_finished_after_the_evaluation_time_is_refused() -> None:
    with pytest.raises(AdhelmError) as error:
        _audit(_key_events(_BOTH_EVENTS), _admin(observed_at=AS_OF + timedelta(seconds=1)))
    assert error.value.code == "INVALID_AUDIT_INPUT"
    assert error.value.exit_code == 2
    assert "observed after the audit evaluation time" in str(error.value)


def test_admin_evidence_finished_exactly_at_the_evaluation_time_is_accepted() -> None:
    result = _audit(_key_events(_BOTH_EVENTS), _admin(observed_at=AS_OF))

    assert result.rules_status == "EVALUATED"


def test_a_requested_property_other_than_the_reports_is_refused_without_any_observation() -> None:
    evidence = ProviderAuditEvidence(ga4_requested_property_ref="ga4.property.999")
    with pytest.raises(AdhelmError) as error:
        audit_provider(
            _key_events(_BOTH_EVENTS),
            source_record_digest=REPORT_DIGEST,
            policy=POLICY,
            as_of=AS_OF,
            evidence=evidence,
        )
    assert error.value.code == "INVALID_AUDIT_INPUT"
    assert error.value.exit_code == 2
    assert "The requested GA4 property is not the property the report describes." in str(
        error.value
    )


def test_a_requested_property_that_is_the_reports_own_is_audited_without_an_observation() -> None:
    result = audit_provider(
        _key_events(_BOTH_EVENTS),
        source_record_digest=REPORT_DIGEST,
        policy=POLICY,
        as_of=AS_OF,
        evidence=ProviderAuditEvidence(ga4_requested_property_ref=PROPERTY),
    )

    assert result.rules_status == "EVALUATED"
    assert result.sections is not None
    assert "GA4_ADMIN_INVENTORY_NOT_RETAINED" in _fired(result.sections)


@pytest.mark.parametrize(
    "value", ["ga4_admin.property.123", "ga4.property.", "123", "ga4.stream.1"]
)
def test_a_requested_property_reference_must_be_a_report_property_reference(value: str) -> None:
    with pytest.raises(ValueError, match="ga4_requested_property_ref"):
        ProviderAuditEvidence(ga4_requested_property_ref=value)


def test_ga4_lists_the_key_event_create_it_can_compile_but_the_rule_table_only_observes() -> None:
    assert COMPILABLE_OPERATION_KINDS["ga4"] == frozenset({"CREATE"})

    sections = _sections(_key_events({}), _admin(key_events=()))

    assert {item.operation_kind for item in sections.recommendations} <= {"OBSERVE"}
    declined = [
        item
        for item in sections.no_recommendations
        if "GA4_NO_KEY_EVENTS_CONFIGURED" in item.bound_rule_ids
    ]
    assert [(item.reason, item.considered_operation_kind) for item in declined] == [
        ("EVIDENCE_INSUFFICIENT", "CREATE")
    ]


def test_a_ga4_recommendation_that_this_build_cannot_compile_is_still_rejected() -> None:
    sections = _sections(_standard_report(), _admin())
    observe = sections.recommendations[0]

    with pytest.raises(ValueError, match="cannot compile"):
        ProviderRecommendation.model_validate({**observe.model_dump(), "operation_kind": "PAUSE"})


def test_admin_evidence_is_refused_for_a_report_of_another_provider() -> None:
    row = PerformanceRow(
        report_id="report.other",
        resource_level="ACCOUNT",
        metric_grain="ACCOUNT",
        resource_type="LINKEDIN_ACCOUNT",
        resource_ref="linkedin_ads.account.123",
        impressions=1,
        clicks=1,
        spend=Money(minor_units=1, currency="USD"),
    )
    report = _key_events(_BOTH_EVENTS)
    foreign = NormalizedReport(
        envelope=report.envelope.model_copy(
            update={
                "provider": "linkedin_ads",
                "report_id": "report.other",
                "source_account_ref": "linkedin_ads.account.123",
                "attribution_model": "NOT_AVAILABLE_FROM_PROVIDER",
            }
        ),
        performance_rows=[row],
    )
    with pytest.raises(AdhelmError) as error:
        _audit(foreign, _admin())
    assert error.value.code == "INVALID_AUDIT_INPUT"


def test_default_values_and_names_never_reach_the_audit_output() -> None:
    admin = _admin(
        key_events=(
            _key_event(
                1, default_value=Ga4AdminDefaultValue(numeric_value=Decimal("12.5"), currency="USD")
            ),
        )
    )

    sections = _sections(_key_events({"generate_lead": "3"}, declared=("generate_lead",)), admin)

    serialized = json.dumps(sections.model_dump(mode="json"))
    assert "12.5" not in serialized
    assert format(1_001, "064x") not in serialized
    assert format(1, "064x") not in serialized
