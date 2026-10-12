"""Independent review findings for ticket M00.1 (GA4 fixed aggregate reporting).

Every test here is a *finding*: it states the behaviour the reviewed contract
already promises and demonstrates that the implementation at 685bb5d does not
deliver it.  No product code is repaired by this file.

Like ``tests/test_ga4_data_aggregates.py`` these drive the adapter through an
in-memory ``httpx.MockTransport``.  They are fixture evidence for the
request/response contract only: no socket, credential, live GA4 property, or UI
reconciliation is involved.

Contract citations come from ``docs/research/ga4-data-aggregate-contract.md``
(S3 ``ResponseMetaData``, S7 reporting-data expectations), independently
re-verified on 2026-09-08 against the Google Analytics Data API v1beta
reference.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.providers.ga4 import Ga4AggregateQuery, Ga4DataClient, Ga4Identity
from adhelm.transport import SafeHttpClient

_TZ = "America/Chicago"


def _client(handler: httpx.MockTransport) -> Ga4DataClient:
    return Ga4DataClient(
        SafeHttpClient(
            httpx.Client(transport=handler, trust_env=False),
            {"analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"},
            read_attempts=1,
        ),
        lambda: "test-token-not-to-be-persisted",
        now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
        connector_version="test",
    )


def _identity() -> Ga4Identity:
    return Ga4Identity(
        property_ref="ga4.property.123",
        property_client_ref="123",
        web_stream_ref="ga4.stream.456",
        web_stream_client_ref="456",
        timezone=_TZ,
        currency="USD",
    )


def _query(**overrides: Any) -> Ga4AggregateQuery:
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


def _traffic_row(campaign: str, source: str, medium: str, values: tuple[str, str, str]) -> Any:
    return {
        "dimensionValues": [{"value": campaign}, {"value": source}, {"value": medium}],
        "metricValues": [{"value": item} for item in values],
    }


def _key_event_page(rows: list[Any], row_count: int, **metadata: Any) -> dict[str, Any]:
    return {
        "dimensionHeaders": [{"name": "eventName"}],
        "metricHeaders": [{"name": "keyEvents"}],
        "rows": rows,
        "rowCount": row_count,
        "metadata": {"timeZone": _TZ, "currencyCode": "USD", **metadata},
    }


def _key_event_row(name: str, value: str) -> Any:
    return {"dimensionValues": [{"value": name}], "metricValues": [{"value": value}]}


def _serve(pages: list[dict[str, Any]], sink: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        sink.append(json.loads(request.content))
        return httpx.Response(200, json=pages[min(len(sink) - 1, len(pages) - 1)])

    return httpx.MockTransport(handler)


def _key_event_query() -> Ga4AggregateQuery:
    return _query(aggregate_shape="KEY_EVENT_TOTALS", key_event_names=("generate_lead", "purchase"))


# ---------------------------------------------------------------------------
# Finding 1 (BLOCKING)
# ---------------------------------------------------------------------------
def test_other_row_on_event_name_is_not_reported_as_a_privacy_incident() -> None:
    """A documented GA4 ``(other)`` row must not be reported as sensitive data.

    ``docs/research/ga4-data-aggregate-contract.md`` records Google's own
    statement that high-cardinality dimensions are rolled into a row labelled
    ``(other)`` and that "filters don't look inside the ``(other)`` row".  The
    adapter's ``dimensionFilter``/``inListFilter`` on ``eventName`` therefore
    cannot exclude it, so ``eventName == "(other)"`` is an expected response for
    a busy property.

    ``_aggregate_rows`` (``src/adhelm/providers/ga4.py:1065``) instead raises
    ``SENSITIVE_DATA_RISK`` -- the code AGENTS rule 12 reserves for detected
    person-level or undeclared collection -- and discards the whole report.  The
    expected behaviour is an explicit aggregation fact (a row or dimension
    status recording the roll-up alongside ``other_row_aggregation``), never a
    fabricated privacy incident that makes a required v1 capability unusable on
    exactly the properties that need it.
    """
    bodies: list[dict[str, Any]] = []
    page = _key_event_page(
        [_key_event_row("generate_lead", "5"), _key_event_row("(other)", "9")],
        2,
        dataLossFromOtherRow=True,
    )

    try:
        report = _client(_serve([page], bodies)).run_aggregate_report(
            _key_event_query(), identity=_identity()
        )
    except AdhelmError as error:  # pragma: no cover - the finding path
        pytest.fail(
            "a documented GA4 (other) roll-up row aborted the fixed aggregate report "
            f"with {error.code}: {error}"
        )

    assert report.other_row_aggregation is True


# ---------------------------------------------------------------------------
# Finding 2 (BLOCKING)
# ---------------------------------------------------------------------------
def test_a_thresholded_empty_result_is_not_reported_as_exhaustive() -> None:
    """Thresholding must not be presented as "there is nothing to report".

    ``exhaustive()`` (``src/adhelm/reporting.py:738``) documents itself as
    "Whether this read may be presented as every matching GA4 row" and returns
    ``True`` for ``EMPTY``.  ``run_aggregate_report`` assigns ``EMPTY`` from the
    consumed row count alone (``src/adhelm/providers/ga4.py:920``), without
    consulting ``subjectToThresholding``.  S3 states thresholding "only returns
    data that meets the minimum aggregation thresholds", so a thresholded
    zero-row response means *rows were suppressed*, not that none exist.

    ``docs/definition-of-done.md`` row L104 requires that thresholded states
    never masquerade as zero, and ``docs/reporting-schema.md`` requires that
    thresholding is "never reported as healthy zero performance".
    """
    bodies: list[dict[str, Any]] = []
    page = _traffic_page([], 0, subjectToThresholding=True)

    report = _client(_serve([page], bodies)).run_aggregate_report(_query(), identity=_identity())

    assert report.subject_to_thresholding is True
    assert report.exhaustive() is False, (
        f"completeness={report.completeness!r} claims exhaustive coverage of a response "
        "GA4 declared subject to thresholding"
    )


def test_a_provider_declared_empty_reason_is_not_reported_as_exhaustive() -> None:
    """``emptyReason`` means GA4 declined to report, not that nothing matched.

    S3 documents ``emptyReason`` as "If empty reason is specified, the report is
    empty for this reason" and, as
    ``docs/research/ga4-data-aggregate-contract.md`` correctly records, does not
    enumerate the possible values.  An unenumerated provider reason is therefore
    never evidence that zero rows match the query.

    The adapter records the reason and raises ``NOT_AVAILABLE_FROM_PROVIDER``,
    yet still sets ``completeness = "EMPTY"`` so ``exhaustive()`` -- and the
    ``"exhaustive"`` key of the agent-visible ``quality_summary()`` -- both
    report ``True``.
    """
    bodies: list[dict[str, Any]] = []
    page = _traffic_page([], 0, emptyReason="NO_DATA_AVAILABLE")

    report = _client(_serve([page], bodies)).run_aggregate_report(_query(), identity=_identity())

    assert report.empty_reason == "NO_DATA_AVAILABLE"
    assert "NOT_AVAILABLE_FROM_PROVIDER" in report.envelope.data_quality_statuses
    assert report.quality_summary()["exhaustive"] is False, (
        "a report GA4 declared empty for an unenumerated reason is presented to the "
        "agent as every matching GA4 row"
    )


def test_an_empty_report_does_not_also_carry_withheld_rows() -> None:
    """``EMPTY`` and a table of withheld key events cannot both be true.

    With ``KEY_EVENT_TOTALS``, a thresholded response returning no rows makes
    ``_aggregate_rows`` synthesise one ``THRESHOLD_WITHHELD`` row per configured
    key event -- correct, and the never-zero requirement working.  But
    ``run_aggregate_report`` derives ``completeness`` from the consumed provider
    row count before those rows exist (``src/adhelm/providers/ga4.py:920``), so
    the same object simultaneously reports ``completeness == "EMPTY"``,
    ``consumed_row_count == 0``, ``exhaustive() is True`` and two rows that
    ``to_normalized_report()`` persists as two ``PerformanceRow`` plus two
    ``ConversionBreakdownRow`` records.

    ``Ga4AggregateReport.completeness_is_provable``
    (``src/adhelm/reporting.py:706``) enforces ``EMPTY`` against
    ``consumed_row_count`` only, so it does not catch the contradiction.
    """
    bodies: list[dict[str, Any]] = []
    page = _key_event_page([], 0, subjectToThresholding=True)

    report = _client(_serve([page], bodies)).run_aggregate_report(
        _key_event_query(), identity=_identity()
    )
    normalized = report.to_normalized_report()

    assert len(report.rows) == 2
    assert len(normalized.performance_rows) == 2
    assert not (report.completeness == "EMPTY" and report.rows), (
        f"completeness={report.completeness!r} with {len(report.rows)} withheld rows and "
        f"{len(normalized.conversion_rows)} persisted conversion rows"
    )


# ---------------------------------------------------------------------------
# Finding 3 (SHOULD_FIX)
# ---------------------------------------------------------------------------
def test_other_row_on_source_and_medium_is_flagged_like_the_campaign_dimension() -> None:
    """``(other)`` on any session dimension must be visible on the row.

    ``_aggregate_rows`` (``src/adhelm/providers/ga4.py:1079``) flags
    ``CAMPAIGN_OTHER_ROW`` when the campaign name is ``(other)`` but tests only
    ``_NOT_SET`` for ``sessionSource``/``sessionMedium``, and
    ``Ga4DimensionStatus`` (``src/adhelm/reporting.py:552``) has no
    ``SOURCE_OTHER_ROW``/``MEDIUM_OTHER_ROW`` member.  A rolled-up bucket is
    therefore persisted as ``provider_extensions["ga4_sessionSource"] ==
    "(other)"`` with no status, indistinguishable from a real source literally
    named ``(other)``.

    The pilot path treats the same marker as unmapped
    (``src/adhelm/providers/ga4.py:1313``), so the two GA4 surfaces disagree about
    what ``(other)`` means.
    """
    bodies: list[dict[str, Any]] = []
    page = _traffic_page(
        [_traffic_row("brand", "(other)", "(other)", ("4", "3", "1"))], 1, dataLossFromOtherRow=True
    )

    report = _client(_serve([page], bodies)).run_aggregate_report(_query(), identity=_identity())

    (row,) = report.rows
    assert row.session_source == "(other)"
    assert row.dimension_statuses != (), (
        "a (other) roll-up on sessionSource/sessionMedium carries no dimension status; "
        "it is persisted as if it were an observed traffic source"
    )


# ---------------------------------------------------------------------------
# Finding 4 (SHOULD_FIX)
# ---------------------------------------------------------------------------
def test_withheld_key_event_row_does_not_repeat_a_quality_status() -> None:
    """A withheld key-event row must not carry ``THRESHOLDED`` twice.

    ``_aggregate_rows`` builds the synthesised row's statuses as
    ``[*base_quality, missing_quality, "UNMAPPED"]``
    (``src/adhelm/providers/ga4.py:1130``).  When the response sets
    ``subjectToThresholding``, ``base_quality`` already contains ``THRESHOLDED``
    and ``missing_quality`` adds it again, so the duplicate reaches both the
    ``PerformanceRow`` and the ``ConversionBreakdownRow`` appended to the
    canonical ledger (AGENTS rule 22).  Every other row in the same report
    carries a de-duplicated, sorted list.
    """
    bodies: list[dict[str, Any]] = []
    page = _key_event_page([_key_event_row("generate_lead", "5")], 1, subjectToThresholding=True)

    report = _client(_serve([page], bodies)).run_aggregate_report(
        _key_event_query(), identity=_identity()
    )
    normalized = report.to_normalized_report()

    withheld = next(row for row in report.rows if row.event_name == "purchase")
    statuses = withheld.data_quality_statuses
    assert len(statuses) == len(set(statuses)), f"duplicated quality statuses: {statuses}"
    persisted = [row.data_quality_statuses for row in normalized.performance_rows]
    persisted += [row.data_quality_statuses for row in normalized.conversion_rows]
    for stored in persisted:
        assert len(stored) == len(set(stored)), f"duplicated statuses reached the ledger: {stored}"


# ---------------------------------------------------------------------------
# Finding 5 (SHOULD_FIX)
# ---------------------------------------------------------------------------
def test_sampling_windows_are_not_multiplied_by_the_page_count() -> None:
    """One request date range must yield at most one sampling window.

    S3 states "One ``samplingMetadatas`` is populated for each date range", and
    ``Ga4SamplingWindow.date_range_index`` (``src/adhelm/reporting.py:578``) is
    documented as "positionally bound to its date range".  The adapter always
    sends exactly one ``dateRanges`` entry, but ``run_aggregate_report``
    concatenates every page's metadata (``src/adhelm/providers/ga4.py:923``), so
    an N-page read reports N windows that all claim ``date_range_index == 0``.
    Any consumer computing ``samplesReadCount / samplingSpaceSize`` sees the
    same window repeated.  The same expression then silently truncates at 8, so
    a 9+ page sampled read drops windows with no recorded fact.
    """
    bodies: list[dict[str, Any]] = []
    sampled = [
        _traffic_page(
            [_traffic_row(f"campaign-{index}", "google", "cpc", ("1", "1", "1"))],
            3,
            samplingMetadatas=[{"samplesReadCount": "5", "samplingSpaceSize": "100"}],
        )
        for index in range(3)
    ]

    report = _client(_serve(sampled, bodies)).run_aggregate_report(
        _query(page_size=1, page_bound=3), identity=_identity()
    )

    assert report.pages_consumed == 3
    assert len(report.sampling) == 1, (
        f"one date range produced {len(report.sampling)} sampling windows with indexes "
        f"{[item.date_range_index for item in report.sampling]}"
    )


# ---------------------------------------------------------------------------
# Finding 6 (SHOULD_FIX)
# ---------------------------------------------------------------------------
def test_restricted_and_withheld_measurements_stay_distinguishable_once_persisted() -> None:
    """The five cell statuses must survive the persistence projection.

    ``Ga4MetricCell`` exists precisely so a ``RESTRICTED`` measurement (the
    caller lacks the metric permission; a retry will not help) is not confused
    with ``NOT_RETURNED`` or ``THRESHOLD_WITHHELD``.  ``to_normalized_report``
    (``src/adhelm/reporting.py:773``) keeps only the absent number and drops the
    status, and the report-level ``restricted_metrics``, ``completeness`` and
    ``consumed_row_count`` facts live only in ``quality_summary()``, which
    ``run_report`` attaches to the shared output *after* ``_share_report`` and
    never persists (``src/adhelm/workflows.py:344``).

    The append-only ledger is the canonical history under AGENTS rule 22, so a
    later reader of a stored GA4 observation cannot tell a metric GA4 refused to
    disclose from one it simply did not return.
    """
    bodies: list[dict[str, Any]] = []
    page = _traffic_page(
        [_traffic_row("brand", "google", "cpc", ("4", "3", "1"))],
        1,
        schemaRestrictionResponse={
            "activeMetricRestrictions": [
                {"metricName": "totalUsers", "restrictedMetricTypes": ["COST_DATA"]}
            ]
        },
    )

    report = _client(_serve([page], bodies)).run_aggregate_report(_query(), identity=_identity())
    (aggregate_row,) = report.rows
    assert {cell.metric: cell.status for cell in aggregate_row.cells}["totalUsers"] == "RESTRICTED"

    payload = report.to_normalized_report().to_ledger_payload(
        observation_key="observation:review",
        observation_revision=1,
        source_report_digest="b" * 64,
        query_as_of=datetime(2026, 9, 4, 12, tzinfo=UTC),
        supersedes_record_id=None,
    )

    assert "RESTRICTED" in json.dumps(payload), (
        "neither the RESTRICTED cell status nor the schemaRestrictionResponse metric name "
        "reaches the persisted ledger payload; a restricted metric is stored identically "
        "to one GA4 simply did not return"
    )
