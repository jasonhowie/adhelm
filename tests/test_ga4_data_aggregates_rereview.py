"""Re-review findings for the M00.1 repair (commit 3d1c320).

Scope is the repair only.  Everything else the repair claims was confirmed by
direct attack and is not restated here.

As with the other GA4 aggregate suites these are in-memory fixture assertions
about the model contract; no socket, credential, or live GA4 property is used.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from adhelm.reporting import (
    Ga4AggregateReport,
    Ga4AggregateRow,
    Ga4MetricCell,
    ReportEnvelope,
)


def _envelope() -> ReportEnvelope:
    return ReportEnvelope(
        report_id="report.rereview",
        provider="ga4",
        source_account_ref="ga4.property.123",
        query_contract_hash="a" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 2),
        source_timezone="America/Chicago",
        report_timezone="America/Chicago",
        currency="USD",
        attribution_model="GA4_DATA_API_PROPERTY_DEFAULT_NOT_DOCUMENTED",
        attribution_window=None,
        observed_at=datetime(2026, 9, 4, 12, tzinfo=UTC),
        data_fresh_through=None,
        data_quality_statuses=["PROVIDER_REPORTED_UNKNOWN"],
        provider_api_version="v1beta",
        connector_version="test",
    )


def _report(row: Ga4AggregateRow, shape: str) -> Ga4AggregateReport:
    return Ga4AggregateReport(
        envelope=_envelope(),
        aggregate_shape=shape,  # type: ignore[arg-type]
        completeness="COMPLETE",
        declared_row_count=1,
        consumed_row_count=1,
        pages_consumed=1,
        page_bound=10,
        page_size=1_000,
        window_start_instant=datetime(2026, 9, 1, 5, tzinfo=UTC),
        window_end_exclusive_instant=datetime(2026, 9, 3, 5, tzinfo=UTC),
        property_timezone="America/Chicago",
        property_currency="USD",
        subject_to_thresholding=False,
        # The provider flag is absent even though the row below is a roll-up.
        other_row_aggregation=False,
        rows=(row,),
    )


def test_event_roll_up_rows_require_the_other_row_aggregation_flag() -> None:
    """A roll-up row must force ``other_row_aggregation``, like a withheld cell.

    The repair correctly derives ``other_row = other_row or any(... _OTHER_ROW
    ...)`` inside the adapter (``src/adhelm/providers/ga4.py:963``), so no report
    produced by ``run_aggregate_report`` is dishonest today.  But it also chose
    ``completeness_is_provable`` (``src/adhelm/reporting.py:791-796``) as the
    enforcement point for the *analogous* withheld invariant -- a
    ``THRESHOLD_WITHHELD`` cell is rejected unless ``subject_to_thresholding``
    is set, and is rejected outright for ``COMPLETE``/``EMPTY``.

    The matching other-row invariant is missing, so the model still admits a
    report whose rows are explicitly marked ``EVENT_OTHER_ROW`` while
    ``other_row_aggregation`` is ``False`` -- and ``exhaustive()`` then returns
    ``True``, presenting a table that contains a GA4 roll-up bucket as every
    matching GA4 row.  Ticket M00.2 reconstructs these rows for the
    deterministic join, so the invariant belongs on the model, not only in the
    adapter that happens to build them today.
    """
    rolled_up = Ga4AggregateRow(
        report_id="report.rereview",
        aggregate_shape="KEY_EVENT_TOTALS",
        event_name="(other)",
        conversion_action_ref=None,
        dimension_statuses=("EVENT_OTHER_ROW",),
        cells=(Ga4MetricCell(metric="keyEvents", status="OBSERVED", value=9),),
        data_quality_statuses=["PROVIDER_REPORTED_UNKNOWN", "UNMAPPED"],
    )

    report = _report(rolled_up, "KEY_EVENT_TOTALS")

    assert report.exhaustive() is False, (
        "a report carrying a GA4 (other) roll-up row is presented as exhaustive because "
        "other_row_aggregation was not forced by the row's EVENT_OTHER_ROW status"
    )


def test_session_roll_up_rows_require_the_other_row_aggregation_flag() -> None:
    """The same gap applies to ``SOURCE_OTHER_ROW``/``MEDIUM_OTHER_ROW``.

    ``exhaustive()`` (``src/adhelm/reporting.py:810``) consults only the
    report-level ``other_row_aggregation`` flag, never the per-row dimension
    statuses that prove a roll-up happened.
    """
    rolled_up = Ga4AggregateRow(
        report_id="report.rereview",
        aggregate_shape="CAMPAIGN_SOURCE_MEDIUM",
        session_campaign_name="brand",
        session_source="(other)",
        session_medium="(other)",
        dimension_statuses=("SOURCE_OTHER_ROW", "MEDIUM_OTHER_ROW"),
        cells=(Ga4MetricCell(metric="sessions", status="OBSERVED", value=4),),
        data_quality_statuses=["PROVIDER_REPORTED_UNKNOWN", "UNMAPPED"],
    )

    report = _report(rolled_up, "CAMPAIGN_SOURCE_MEDIUM")

    assert report.exhaustive() is False, (
        "a report whose rows are marked SOURCE_OTHER_ROW/MEDIUM_OTHER_ROW is presented "
        "as exhaustive because other_row_aggregation was not forced by those statuses"
    )
