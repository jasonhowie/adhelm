"""Deterministic GA4 <-> provider campaign joins (ticket M00.2).

Every test is hermetic: no socket, no credential, no live GA4 property and no
live advertising account.  These are fixture evidence for the declared-join
contract and its explicit heuristic/conflict/unmapped states only.  They are
not GA4-UI or provider-UI reconciliation.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import typer
from pydantic import ValidationError
from typer.testing import CliRunner

import adhelm.ledger as ledger_module
from adhelm import join_cli
from adhelm.campaign_joins import (
    SIDE_BY_SIDE_DISCLOSURE,
    CampaignJoinContract,
    CampaignJoinMap,
    DeclaredCampaignJoin,
    Ga4JoinDimensionKey,
    Ga4JoinRow,
    Ga4PropertyBinding,
    ProviderAccountBinding,
    apply_join_map,
    ga4_join_rows_from_normalized,
    ga4_join_rows_from_report,
    refuse_cross_source_derivation,
    resolve_campaign_joins,
    validate_predecessor,
)
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import global_options
from adhelm.errors import AdhelmError
from adhelm.join_cli import register_commands
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import build_side_by_side_comparison
from adhelm.models import Money
from adhelm.privacy import validate_data
from adhelm.reporting import (
    GA4_JOIN_APPLIED_DISCLOSURE,
    GA4_JOIN_DISCLOSURE,
    Ga4AggregateReport,
    Ga4AggregateRow,
    Ga4MetricCell,
    LedgerCheckpoint,
    NormalizedReport,
    PerformanceRow,
    ReportEnvelope,
)
from adhelm.saved_campaign_joins import (
    JOIN_MAP_RECORD_KIND,
    join_map_catalog,
    join_map_retention_installed,
    load_join_map,
    retain_join_map,
)
from adhelm.workflows import persist_report

_TZ = "America/Chicago"
_PROPERTY = "ga4.property.123"
_ACCOUNT = "google_ads.customer.1234567890"
_CAMPAIGN = "google_ads.campaign.1234567890.55"
_OTHER_CAMPAIGN = "google_ads.campaign.1234567890.66"

runner = CliRunner()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _key(kind: str = "SESSION_CAMPAIGN_NAME", **values: Any) -> Ga4JoinDimensionKey:
    defaults: dict[str, Any] = {"match_kind": kind}
    if kind == "SESSION_CAMPAIGN_NAME":
        defaults["session_campaign_name"] = values.pop("name", "brand-search")
    elif kind == "SESSION_SOURCE_MEDIUM":
        defaults["session_source"] = values.pop("source", "google")
        defaults["session_medium"] = values.pop("medium", "cpc")
    elif kind == "SESSION_CAMPAIGN_NAME_SOURCE_MEDIUM":
        defaults["session_campaign_name"] = values.pop("name", "brand-search")
        defaults["session_source"] = values.pop("source", "google")
        defaults["session_medium"] = values.pop("medium", "cpc")
    else:
        defaults["utm_campaign_id"] = values.pop("campaign_id", "1234567890-55")
    defaults.update(values)
    return Ga4JoinDimensionKey(**defaults)


def _entry(**overrides: Any) -> DeclaredCampaignJoin:
    values: dict[str, Any] = {
        "provider": "google_ads",
        "provider_campaign_ref": _CAMPAIGN,
        "ga4_keys": (_key(),),
    }
    values.update(overrides)
    return DeclaredCampaignJoin(**values)


def _contract(
    *,
    entries: tuple[DeclaredCampaignJoin, ...] | None = None,
    account_timezone: str = _TZ,
    version: int = 1,
    supersedes: str | None = None,
    proposals_enabled: bool = True,
) -> CampaignJoinContract:
    return CampaignJoinContract(
        contract_id="join.fixture-private",
        version=version,
        supersedes_ref=supersedes,
        ga4_property=Ga4PropertyBinding(property_ref=_PROPERTY, property_timezone=_TZ),
        provider_accounts=(
            ProviderAccountBinding(
                provider="google_ads", account_ref=_ACCOUNT, account_timezone=account_timezone
            ),
        ),
        declared_joins=entries if entries is not None else (_entry(),),
        heuristic_proposals_enabled=proposals_enabled,
        effective_from=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=version - 1),
    )


def _row(name: str = "brand-search", source: str = "google", medium: str = "cpc") -> Ga4JoinRow:
    return Ga4JoinRow(session_campaign_name=name, session_source=source, session_medium=medium)


def _checkpoints(ledger: Ledger | None) -> list[LedgerCheckpoint]:
    if ledger is None:
        return []
    return [
        LedgerCheckpoint(
            stream_id=value["stream_id"],
            high_watermark_sequence=value["high_watermark"],
            head_digest=value["head_digest"],
        )
        for value in ledger.checkpoint()["streams"].values()
    ]


def _ga4_envelope(
    ledger: Ledger | None = None, report_id: str = "report.ga4-private"
) -> ReportEnvelope:
    return ReportEnvelope(
        report_id=report_id,
        provider="ga4",
        source_account_ref=_PROPERTY,
        query_contract_hash="a" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 2),
        source_timezone=_TZ,
        report_timezone=_TZ,
        currency="USD",
        attribution_model="GA4_DATA_API_PROPERTY_DEFAULT_NOT_DOCUMENTED",
        observed_at=datetime(2026, 9, 4, 12, tzinfo=UTC),
        data_quality_statuses=["COMPLETE"],
        provider_api_version="v1beta",
        connector_version="test",
        source_ledger_checkpoints=_checkpoints(ledger),
    )


def _aggregate_report(
    rows: tuple[tuple[str, str, str, int, int], ...], ledger: Ledger | None = None
) -> Ga4AggregateReport:
    envelope = _ga4_envelope(ledger)
    return Ga4AggregateReport(
        envelope=envelope,
        aggregate_shape="CAMPAIGN_SOURCE_MEDIUM",
        completeness="COMPLETE",
        declared_row_count=len(rows),
        consumed_row_count=len(rows),
        pages_consumed=1,
        page_bound=10,
        page_size=1_000,
        window_start_instant=datetime(2026, 9, 1, 5, tzinfo=UTC),
        window_end_exclusive_instant=datetime(2026, 9, 3, 5, tzinfo=UTC),
        property_timezone=_TZ,
        property_currency="USD",
        subject_to_thresholding=False,
        other_row_aggregation=False,
        rows=tuple(
            Ga4AggregateRow(
                report_id=envelope.report_id,
                aggregate_shape="CAMPAIGN_SOURCE_MEDIUM",
                session_campaign_name=name,
                session_source=source,
                session_medium=medium,
                cells=(
                    Ga4MetricCell(metric="sessions", status="OBSERVED", value=sessions),
                    Ga4MetricCell(metric="keyEvents", status="OBSERVED", value=key_events),
                ),
                data_quality_statuses=["UNMAPPED"],
            )
            for name, source, medium, sessions, key_events in rows
        ),
    )


def _provider_report(
    *, campaign_ref: str = _CAMPAIGN, clicks: int | None = 7, ledger: Ledger | None = None
) -> NormalizedReport:
    envelope = ReportEnvelope(
        report_id="report.google-private",
        provider="google_ads",
        source_account_ref=_ACCOUNT,
        query_contract_hash="b" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 2),
        source_timezone=_TZ,
        report_timezone=_TZ,
        currency="USD",
        attribution_model="GOOGLE_ADS_LAST_CLICK",
        observed_at=datetime(2026, 9, 4, 12, tzinfo=UTC),
        data_quality_statuses=["COMPLETE"],
        provider_api_version="v25",
        connector_version="test",
        source_ledger_checkpoints=_checkpoints(ledger),
    )
    return NormalizedReport(
        envelope=envelope,
        performance_rows=[
            PerformanceRow(
                report_id=envelope.report_id,
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="CAMPAIGN",
                resource_ref=campaign_ref,
                campaign_ref=campaign_ref,
                dimension_type="DATE",
                dimension_value=day.isoformat(),
                impressions=100,
                clicks=clicks,
                spend=Money(minor_units=2_500, currency="USD"),
            )
            for day in (date(2026, 9, 1), date(2026, 9, 2))
        ],
    )


# --------------------------------------------------------------------------
# 1. Declared joins, and only declared joins
# --------------------------------------------------------------------------


def test_a_declared_dimension_value_joins_and_records_its_source_and_map() -> None:
    join_map = resolve_campaign_joins(_contract(), (_row(), _row("winback", "bing", "cpc")))

    joined = join_map.joined_resolutions()
    assert len(joined) == 1
    assert joined[0].join_status == "EXPLICIT_MAPPING"
    assert joined[0].join_source == "DECLARED"
    assert joined[0].provider_campaign_ref == _CAMPAIGN
    assert joined[0].match_kind == "SESSION_CAMPAIGN_NAME"
    assert join_map.status_counts() == {
        "EXPLICIT_MAPPING": 1,
        "EXACT_CONTROLLED_KEY": 0,
        "HEURISTIC": 0,
        "CONFLICT": 0,
        "UNMAPPED": 1,
    }


def test_a_declared_utm_campaign_id_convention_is_a_controlled_key_join() -> None:
    entry = _entry(ga4_keys=(_key("UTM_CAMPAIGN_ID", campaign_id="1234567890-55"),))
    join_map = resolve_campaign_joins(_contract(entries=(entry,)), (_row("1234567890-55"),))

    resolution = join_map.resolutions[0]
    assert resolution.join_status == "EXACT_CONTROLLED_KEY"
    assert resolution.campaign_join_key == "1234567890-55"
    assert resolution.provider_campaign_ref == _CAMPAIGN


def test_a_source_medium_declaration_joins_without_a_campaign_name() -> None:
    entry = _entry(ga4_keys=(_key("SESSION_SOURCE_MEDIUM", source="linkedin", medium="paid"),))
    join_map = resolve_campaign_joins(
        _contract(entries=(entry,)), (_row("anything", "linkedin", "paid"),)
    )

    assert join_map.resolutions[0].join_status == "EXPLICIT_MAPPING"
    assert join_map.resolutions[0].match_kind == "SESSION_SOURCE_MEDIUM"


def test_an_accepted_heuristic_declaration_is_a_declared_join_with_its_own_source() -> None:
    entry = _entry(join_source="ACCEPTED_HEURISTIC", provider_campaign_name="brand-search")
    join_map = resolve_campaign_joins(_contract(entries=(entry,)), (_row(),))

    resolution = join_map.resolutions[0]
    assert resolution.join_status == "EXPLICIT_MAPPING"
    assert resolution.join_source == "ACCEPTED_HEURISTIC"
    # An acceptance is a declaration, so no proposal is repeated for it.
    assert join_map.proposals == ()


def test_case_and_whitespace_differences_are_never_normalized_into_a_join() -> None:
    join_map = resolve_campaign_joins(_contract(), (_row("Brand-Search"), _row(" brand-search")))

    assert {row.join_status for row in join_map.resolutions} == {"UNMAPPED"}


def test_a_ga4_sentinel_value_cannot_be_declared_as_a_join_key() -> None:
    for sentinel in ("(not set)", "(other)", "(direct)", "(none)"):
        with pytest.raises(ValidationError, match="sentinel"):
            _key(name=sentinel)


# --------------------------------------------------------------------------
# 2. The heuristic tier proposes; it never joins
# --------------------------------------------------------------------------


def test_an_exact_name_match_is_proposed_and_never_applied() -> None:
    entry = _entry(ga4_keys=(), provider_campaign_name="brand-search")
    join_map = resolve_campaign_joins(_contract(entries=(entry,)), (_row(),))

    assert join_map.joined_resolutions() == ()
    resolution = join_map.resolutions[0]
    assert resolution.join_status == "HEURISTIC"
    assert resolution.provider_campaign_ref is None
    assert resolution.join_source is None
    assert resolution.candidate_provider_campaign_refs == (_CAMPAIGN,)
    assert len(join_map.proposals) == 1
    assert join_map.proposals[0].status == "HEURISTIC_PROPOSED"
    assert join_map.proposals[0].applied is False
    assert join_map.summary()["heuristic_proposals_applied"] is False


def test_a_heuristic_candidate_never_competes_with_an_explicit_declaration() -> None:
    declared = _entry()
    competing = _entry(
        provider_campaign_ref=_OTHER_CAMPAIGN, ga4_keys=(), provider_campaign_name="brand-search"
    )
    join_map = resolve_campaign_joins(_contract(entries=(declared, competing)), (_row(),))

    assert join_map.resolutions[0].join_status == "EXPLICIT_MAPPING"
    assert join_map.resolutions[0].provider_campaign_ref == _CAMPAIGN
    assert join_map.proposals == ()


def test_disabling_the_proposal_tier_leaves_the_row_unmapped_not_joined() -> None:
    entry = _entry(ga4_keys=(), provider_campaign_name="brand-search")
    join_map = resolve_campaign_joins(
        _contract(entries=(entry,), proposals_enabled=False), (_row(),)
    )

    assert join_map.proposals == ()
    assert join_map.resolutions[0].join_status == "UNMAPPED"


# --------------------------------------------------------------------------
# 3. Conflicts in both directions
# --------------------------------------------------------------------------


def test_two_provider_campaigns_declaring_one_ga4_value_is_a_conflict() -> None:
    first = _entry()
    second = _entry(provider_campaign_ref=_OTHER_CAMPAIGN)
    join_map = resolve_campaign_joins(_contract(entries=(first, second)), (_row(),))

    resolution = join_map.resolutions[0]
    assert resolution.join_status == "CONFLICT"
    assert resolution.provider_campaign_ref is None
    assert resolution.conflict_reasons == ("MULTIPLE_PROVIDER_CAMPAIGNS_DECLARE_ONE_GA4_VALUE",)
    assert resolution.candidate_provider_campaign_refs == (_CAMPAIGN, _OTHER_CAMPAIGN)
    assert join_map.joined_resolutions() == ()


def test_one_provider_campaign_matching_several_ga4_values_is_a_conflict() -> None:
    entry = _entry(ga4_keys=(_key("SESSION_SOURCE_MEDIUM"),))
    join_map = resolve_campaign_joins(
        _contract(entries=(entry,)), (_row("brand-search"), _row("winback"))
    )

    assert {row.join_status for row in join_map.resolutions} == {"CONFLICT"}
    assert join_map.resolutions[0].conflict_reasons == (
        "ONE_PROVIDER_CAMPAIGN_MATCHES_SEVERAL_GA4_VALUES_WITHOUT_DECLARED_MULTI_MAPPING",
    )
    assert join_map.joined_resolutions() == ()


def test_a_declared_multi_mapping_joins_several_ga4_values_on_purpose() -> None:
    entry = _entry(ga4_keys=(_key("SESSION_SOURCE_MEDIUM"),), multi_mapping_declared=True)
    join_map = resolve_campaign_joins(
        _contract(entries=(entry,)), (_row("brand-search"), _row("winback"))
    )

    assert {row.join_status for row in join_map.resolutions} == {"EXPLICIT_MAPPING"}
    assert len(join_map.joined_resolutions()) == 2


# --------------------------------------------------------------------------
# 4. Unmapped stays unmapped and visible
# --------------------------------------------------------------------------


def test_an_undeclared_ga4_value_is_unmapped_and_carries_the_quality_status() -> None:
    join_map = resolve_campaign_joins(_contract(), (_row("winback", "bing", "organic"),))

    resolution = join_map.resolutions[0]
    assert resolution.join_status == "UNMAPPED"
    assert resolution.data_quality_statuses == ("UNMAPPED",)
    assert resolution.provider_campaign_ref is None


def test_a_declared_campaign_absent_from_ga4_is_reported_not_dropped() -> None:
    join_map = resolve_campaign_joins(_contract(), (_row("winback", "bing", "organic"),))

    assert join_map.unmatched_declared_campaign_refs == (_CAMPAIGN,)
    assert join_map.summary()["unmatched_declared_campaign_count"] == 1


# --------------------------------------------------------------------------
# 5. Timezone / date-boundary mismatch is a typed status
# --------------------------------------------------------------------------


def test_a_provider_timezone_that_differs_from_the_property_is_a_typed_status() -> None:
    join_map = resolve_campaign_joins(_contract(account_timezone="Europe/London"), (_row(),))

    assert join_map.boundary_status == "TIMEZONE_MISMATCH"
    assert join_map.timezone_mismatches[0].provider_account_timezone == "Europe/London"
    assert join_map.timezone_mismatches[0].ga4_property_timezone == _TZ
    assert join_map.timezone_mismatches[0].alignment == "NOT_ALIGNED_DATE_BOUNDARIES_DIFFER"
    assert "TIMEZONE_MISMATCH" in join_map.resolutions[0].data_quality_statuses


def test_matching_timezones_stay_aligned_without_a_mismatch_record() -> None:
    join_map = resolve_campaign_joins(_contract(), (_row(),))

    assert join_map.boundary_status == "ALIGNED"
    assert join_map.timezone_mismatches == ()


# --------------------------------------------------------------------------
# 6. Determinism
# --------------------------------------------------------------------------


def test_the_join_map_is_byte_identical_for_the_same_declaration_and_rows() -> None:
    rows = (_row("winback", "bing", "cpc"), _row(), _row("demand", "linkedin", "paid"))
    first = resolve_campaign_joins(_contract(), rows)
    second = resolve_campaign_joins(_contract(), tuple(reversed(rows)))

    assert canonical_bytes(first) == canonical_bytes(second)
    assert first.join_map_digest == second.join_map_digest
    orders = [
        (row.session_campaign_name, row.session_source, row.session_medium)
        for row in first.resolutions
    ]
    assert orders == sorted(orders)


def test_a_changed_declaration_changes_both_digests() -> None:
    first = resolve_campaign_joins(_contract(), (_row(),))
    changed = _contract(entries=(_entry(ga4_keys=(_key(name="winback"),)),))
    second = resolve_campaign_joins(changed, (_row(),))

    assert first.content_digest != second.content_digest
    assert first.join_map_digest != second.join_map_digest


def test_a_tampered_digest_is_refused_by_the_model() -> None:
    join_map = resolve_campaign_joins(_contract(), (_row(),))
    payload = join_map.model_dump(mode="json")
    payload["join_map_digest"] = "f" * 64

    with pytest.raises(ValidationError, match="Join-map digest"):
        CampaignJoinMap.model_validate_json(canonical_bytes(payload))


# --------------------------------------------------------------------------
# 7. Privacy validation at this boundary too
# --------------------------------------------------------------------------


def test_an_email_in_a_declared_join_key_is_refused() -> None:
    with pytest.raises(AdhelmError, match="Prohibited sensitive text"):
        _contract(entries=(_entry(ga4_keys=(_key(name="lead@example.com"),)),))


def test_an_email_in_a_ga4_dimension_value_is_refused_before_it_is_joined() -> None:
    # The GA4 read boundary refuses this already; the join engine re-validates
    # because a retained row is not the only way a caller can reach it.
    with pytest.raises(AdhelmError, match="Prohibited sensitive text"):
        resolve_campaign_joins(_contract(), (_row("owner@example.com"),))


def test_the_retained_join_map_passes_the_central_privacy_validator() -> None:
    join_map = resolve_campaign_joins(_contract(), (_row(), _row("winback", "bing", "cpc")))

    validate_data(join_map.model_dump(mode="json"))


# --------------------------------------------------------------------------
# 8. Predecessor chain
# --------------------------------------------------------------------------


def test_a_restatement_must_name_the_exact_prior_content_digest() -> None:
    first = resolve_campaign_joins(_contract(), (_row(),))
    successor = _contract(
        entries=(_entry(ga4_keys=(_key(name="winback"),)),),
        version=2,
        supersedes=first.content_digest,
    )

    validate_predecessor(first, successor)
    second = resolve_campaign_joins(successor, (_row("winback"),))
    assert second.contract.supersedes_ref == first.content_digest
    assert second.content_digest != first.content_digest


def test_a_restatement_that_skips_or_misnames_its_predecessor_is_refused() -> None:
    first = resolve_campaign_joins(_contract(), (_row(),))

    with pytest.raises(AdhelmError, match="exact prior content digest"):
        validate_predecessor(first, _contract(version=2, supersedes="0" * 64))
    with pytest.raises(AdhelmError, match="increment the retained version"):
        validate_predecessor(first, _contract(version=3, supersedes=first.content_digest))


def test_version_one_cannot_supersede_and_later_versions_must() -> None:
    with pytest.raises(ValidationError, match="cannot supersede"):
        _contract(version=1, supersedes="0" * 64)
    with pytest.raises(ValidationError, match="require the prior content digest"):
        _contract(version=2, supersedes=None)


# --------------------------------------------------------------------------
# 9. Attaching join results to GA4 aggregate rows
# --------------------------------------------------------------------------


def test_applying_a_join_map_labels_rows_and_leaves_the_rest_visible() -> None:
    report = _aggregate_report(
        (("brand-search", "google", "cpc", 40, 3), ("winback", "bing", "organic", 5, 0))
    )
    join_map = resolve_campaign_joins(_contract(), ga4_join_rows_from_report(report))

    joined = apply_join_map(report, join_map)

    assert joined.join_disclosure() == GA4_JOIN_APPLIED_DISCLOSURE
    assert joined.join_map_digest() == join_map.join_map_digest
    assert joined.join_status_counts() == {"EXPLICIT_MAPPING": 1, "UNMAPPED": 1}
    brand = next(row for row in joined.rows if row.session_campaign_name == "brand-search")
    assert brand.join_source == "DECLARED"
    assert brand.provider_campaign_ref == _CAMPAIGN
    assert "UNMAPPED" not in brand.data_quality_statuses
    other = next(row for row in joined.rows if row.session_campaign_name == "winback")
    assert other.join_status == "UNMAPPED"
    assert other.provider_campaign_ref is None
    # The unjoined read still declares the deferred disclosure.
    assert report.join_disclosure() == GA4_JOIN_DISCLOSURE


def test_a_join_map_for_another_property_is_refused_before_it_is_applied() -> None:
    report = _aggregate_report((("brand-search", "google", "cpc", 40, 3),))
    contract = CampaignJoinContract.model_validate_json(
        canonical_bytes(
            {
                **_contract().model_dump(mode="json"),
                "ga4_property": {
                    "schema_version": 1,
                    "property_ref": "ga4.property.999",
                    "property_timezone": _TZ,
                },
            }
        )
    )
    join_map = resolve_campaign_joins(contract, (_row(),))

    with pytest.raises(AdhelmError, match="different GA4 property"):
        apply_join_map(report, join_map)


def test_the_normalized_projection_carries_the_join_state_into_history() -> None:
    report = _aggregate_report(
        (("brand-search", "google", "cpc", 40, 3), ("winback", "bing", "organic", 5, 0))
    )
    join_map = resolve_campaign_joins(_contract(), ga4_join_rows_from_report(report))

    normalized = apply_join_map(report, join_map).to_normalized_report()

    joined_row = next(row for row in normalized.performance_rows if row.campaign_ref == _CAMPAIGN)
    assert joined_row.join_status == "EXPLICIT_MAPPING"
    assert joined_row.provider_extensions["ga4_join_source"] == "DECLARED"
    assert joined_row.provider_extensions["ga4_join_map_digest"] == join_map.join_map_digest
    unjoined = next(row for row in normalized.performance_rows if row.campaign_ref is None)
    assert unjoined.join_status == "UNMAPPED"
    validate_data(normalized.model_dump(mode="json"))


def test_a_ga4_row_cannot_claim_a_campaign_without_a_declared_join_map() -> None:
    with pytest.raises(ValidationError, match="requires a join map digest"):
        Ga4AggregateRow(
            report_id="report.ga4-private",
            aggregate_shape="CAMPAIGN_SOURCE_MEDIUM",
            session_campaign_name="brand-search",
            session_source="google",
            session_medium="cpc",
            join_status="EXPLICIT_MAPPING",
            join_source="DECLARED",
            provider_campaign_ref=_CAMPAIGN,
            cells=(Ga4MetricCell(metric="sessions", status="OBSERVED", value=1),),
        )


def test_a_retained_ga4_report_supplies_the_join_dimensions_without_re_reading_ga4() -> None:
    report = _aggregate_report((("brand-search", "google", "cpc", 40, 3),))

    rows = ga4_join_rows_from_normalized(report.to_normalized_report())

    assert rows == (_row(),)


# --------------------------------------------------------------------------
# 10. Side-by-side rows with no derived cross-source measure
# --------------------------------------------------------------------------


def _joined_ga4_normalized(
    join_map: CampaignJoinMap, ledger: Ledger | None = None
) -> NormalizedReport:
    report = _aggregate_report(
        (("brand-search", "google", "cpc", 40, 3), ("winback", "bing", "organic", 5, 0)),
        ledger,
    )
    return apply_join_map(report, join_map).to_normalized_report()


def test_side_by_side_rows_keep_each_source_in_its_own_fields() -> None:
    join_map = resolve_campaign_joins(_contract(), (_row(), _row("winback", "bing", "organic")))

    comparison = build_side_by_side_comparison(
        join_map=join_map,
        provider_report=_provider_report(),
        ga4_report=_joined_ga4_normalized(join_map),
    )

    assert len(comparison.rows) == 1
    row = comparison.rows[0]
    assert row.provider_clicks == 14
    assert row.provider_spend == Money(minor_units=5_000, currency="USD")
    assert row.ga4_sessions == 40
    assert row.ga4_key_events == 3
    assert row.provider_attribution_model != row.ga4_attribution_model
    assert comparison.excluded_join_status_counts == {"UNMAPPED": 1}
    assert comparison.causal_claim is False
    assert comparison.derived_cross_source_metrics is False


def test_no_cross_source_ratio_or_causal_label_appears_anywhere_in_the_output() -> None:
    join_map = resolve_campaign_joins(_contract(), (_row(), _row("winback", "bing", "organic")))
    comparison = build_side_by_side_comparison(
        join_map=join_map,
        provider_report=_provider_report(),
        ga4_report=_joined_ga4_normalized(join_map),
    )

    emitted = json.dumps(
        {
            "join_map": join_map.model_dump(mode="json"),
            "summary": join_map.summary(),
            "comparison": comparison.model_dump(mode="json"),
        }
    ).lower()
    # The disclosure constant names the claims that are absent; strip it before
    # asserting that no other output field names one.
    emitted = emitted.replace(SIDE_BY_SIDE_DISCLOSURE.lower(), "")
    # `causal_claim` and `derived_cross_source_metrics` are the two explicit
    # negations.  Every other appearance of a derived or causal term would be a
    # claim this product must never make.
    assert emitted.count("causal") == emitted.count('"causal_claim": false')
    assert emitted.count("derived") == emitted.count('"derived_cross_source_metrics": false')
    for prohibited in (
        "incremental",
        "lift",
        "roas",
        "cpa",
        "cost_per",
        "conversion_rate",
        "ratio",
        "per_session",
        "per_key_event",
        "attributed_credit",
        "blended",
        "true_conversions",
    ):
        assert prohibited not in emitted, prohibited


def test_a_derived_cross_source_field_is_refused_before_it_can_be_emitted() -> None:
    with pytest.raises(AdhelmError, match="derived cross-source"):
        refuse_cross_source_derivation({"cost_per_session": 12})
    with pytest.raises(AdhelmError, match="derived cross-source"):
        refuse_cross_source_derivation({"conflicts": [{"sessions_per_click_ratio": 2}]})
    # A closed product label may not state the claim either.
    with pytest.raises(AdhelmError, match="derived cross-source"):
        refuse_cross_source_derivation({"basis": "GA4 sessions show incremental lift"})
    with pytest.raises(AdhelmError, match="claim is absent"):
        refuse_cross_source_derivation({"causal_claim": True})


def test_retained_source_data_is_never_censored_by_the_derivation_guard() -> None:
    """An advertising campaign may be called `brand-lift-q4`; that is data.

    The guard exists to stop the *product* presenting a derived or causal
    measure.  Refusing a customer's own campaign name, or a GA4 dimension
    value the read boundary already retained, would make a lawfully retained
    record permanently unreviewable.
    """
    refuse_cross_source_derivation(
        {
            "session_campaign_name": "brand-lift-q4",
            "provider_campaign_name": "Q4 CPA test",
            "conflicts": [{"session_campaign_name": "blended-ratio-q4"}],
            "candidate_provider_campaign_refs": ["google_ads.campaign.1.roas-test"],
            "join_status": "CONFLICT",
        }
    )


def test_a_null_provider_measurement_never_becomes_a_zero_beside_ga4() -> None:
    join_map = resolve_campaign_joins(_contract(), (_row(),))

    comparison = build_side_by_side_comparison(
        join_map=join_map,
        provider_report=_provider_report(clicks=None),
        ga4_report=_joined_ga4_normalized(join_map),
    )

    assert comparison.rows[0].provider_clicks is None
    assert comparison.rows[0].provider_impressions == 200


def test_a_timezone_mismatch_reaches_the_side_by_side_rows() -> None:
    join_map = resolve_campaign_joins(_contract(account_timezone="Europe/London"), (_row(),))

    comparison = build_side_by_side_comparison(
        join_map=join_map,
        provider_report=_provider_report(),
        ga4_report=_joined_ga4_normalized(join_map),
    )

    assert comparison.boundary_status == "TIMEZONE_MISMATCH"
    assert "TIMEZONE_MISMATCH" in comparison.rows[0].data_quality_statuses


def test_an_unbound_advertising_account_cannot_be_placed_beside_the_property() -> None:
    join_map = resolve_campaign_joins(_contract(), (_row(),))
    other = _provider_report()
    unbound = NormalizedReport.model_validate_json(
        canonical_bytes(
            {
                **other.model_dump(mode="json"),
                "envelope": {
                    **other.envelope.model_dump(mode="json"),
                    "source_account_ref": "google_ads.customer.9999999999",
                },
            }
        )
    )

    with pytest.raises(AdhelmError, match="does not bind this advertising account"):
        build_side_by_side_comparison(
            join_map=join_map,
            provider_report=unbound,
            ga4_report=_joined_ga4_normalized(join_map),
        )


# --------------------------------------------------------------------------
# 11. Retention, selection and the CLI
# --------------------------------------------------------------------------


@pytest.fixture()
def installed_join_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the additive ledger record kind this ticket does not own.

    `src/adhelm/ledger.py` belongs to the integration coordinator.  This fixture
    applies the exact additive `_KINDS` entry described in
    `docs/implementation/ga4-campaign-joins.md`, so the retention, catalog and
    predecessor paths are evidenced rather than assumed.
    """
    monkeypatch.setitem(ledger_module._KINDS["changes"], JOIN_MAP_RECORD_KIND, CampaignJoinMap)


@pytest.fixture()
def uninstalled_join_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the behaviour of a build whose ledger has no join-map record kind."""
    monkeypatch.delitem(ledger_module._KINDS["changes"], JOIN_MAP_RECORD_KIND, raising=False)


def test_retention_refuses_explicitly_while_the_ledger_kind_is_absent(
    tmp_path: Path, uninstalled_join_retention: None
) -> None:
    join_map = resolve_campaign_joins(_contract(), (_row(),))

    assert join_map_retention_installed() is False
    with pytest.raises(AdhelmError, match="does not yet accept"):
        retain_join_map(Ledger(tmp_path), join_map)
    assert join_map_catalog(Ledger(tmp_path))["retention_installed"] is False


def test_a_retained_chain_is_selectable_and_keeps_every_prior_version(
    tmp_path: Path, installed_join_retention: None
) -> None:
    ledger = Ledger(tmp_path)
    first = resolve_campaign_joins(_contract(), (_row(),))
    first_record = retain_join_map(ledger, first)
    successor = _contract(
        entries=(_entry(ga4_keys=(_key(name="winback"),)),),
        version=2,
        supersedes=first.content_digest,
    )
    second = resolve_campaign_joins(successor, (_row("winback"),))
    second_record = retain_join_map(ledger, second)

    catalog = join_map_catalog(ledger)
    assert catalog["total_count"] == 2
    assert catalog["retention_installed"] is True
    assert catalog["join_maps"][0]["record_digest"] == second_record["record_digest"]
    assert catalog["join_maps"][0]["supersedes_digest"] == first.content_digest
    restored, provenance = load_join_map(ledger, first_record["record_digest"])
    assert restored.join_map_digest == first.join_map_digest
    assert provenance["record_digest"] == first_record["record_digest"]
    with pytest.raises(AdhelmError, match="absent from verified history"):
        load_join_map(ledger, "0" * 64)


def _cli_app() -> typer.Typer:
    """Mirror the installed root application without editing `cli.py`.

    Registration of `measurement join` on the shipped tree is the additive
    `cli.py` hunk submitted with this ticket; the command implementation is
    complete and exercised here through the real global option callback.
    """
    root_app = typer.Typer(no_args_is_help=True)
    root_app.callback()(global_options)
    measurement_app = typer.Typer(no_args_is_help=True)
    register_commands(measurement_app)
    root_app.add_typer(measurement_app, name="measurement")
    return root_app


def _retain_ga4_report(ledger: Ledger, join_map: CampaignJoinMap | None = None) -> str:
    report = _aggregate_report(
        (("brand-search", "google", "cpc", 40, 3), ("winback", "bing", "organic", 5, 0)),
        ledger,
    )
    if join_map is not None:
        report = apply_join_map(report, join_map)
    return str(persist_report(ledger, report.to_normalized_report())["record_digest"])


def _write_contract(path: Path, contract: CampaignJoinContract) -> Path:
    target = path / "join-contract.json"
    target.write_text(json.dumps(contract.model_dump(mode="json")))
    return target


def test_join_create_reports_declared_conflicting_and_proposed_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uninstalled_join_retention: None
) -> None:
    monkeypatch.setattr(join_cli, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    report_digest = _retain_ga4_report(ledger)
    contract = _contract(
        entries=(
            _entry(),
            _entry(
                provider_campaign_ref=_OTHER_CAMPAIGN,
                ga4_keys=(),
                provider_campaign_name="winback",
            ),
        )
    )
    path = _write_contract(tmp_path, contract)

    result = runner.invoke(
        _cli_app(),
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "join",
            "create",
            "--input",
            str(path),
            "--ga4-report",
            report_digest,
        ],
    )

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["persisted"] is False
    assert data["retention_installed"] is False
    assert data["status_counts"]["EXPLICIT_MAPPING"] == 1
    assert data["status_counts"]["HEURISTIC"] == 1
    assert data["heuristic_proposals"][0]["applied"] is False
    assert data["causal_claim"] is False
    assert data["remote_side_effects"] is False


def test_join_create_retains_and_show_renders_side_by_side_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed_join_retention: None
) -> None:
    monkeypatch.setattr(join_cli, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    unjoined_digest = _retain_ga4_report(ledger)
    path = _write_contract(tmp_path, _contract())
    base = ["--data-dir", str(tmp_path), "measurement", "join"]

    created = runner.invoke(
        _cli_app(),
        [*base, "create", "--input", str(path), "--ga4-report", unjoined_digest, "--retain"],
    )
    assert created.exit_code == 0, created.stdout
    record_digest = json.loads(created.stdout)["data"]["record_digest"]
    assert json.loads(created.stdout)["data"]["persisted"] is True

    join_map, _ = load_join_map(ledger, record_digest)
    joined_ga4 = str(
        persist_report(ledger, _joined_ga4_normalized(join_map, ledger))["record_digest"]
    )
    provider_digest = str(persist_report(ledger, _provider_report(ledger=ledger))["record_digest"])

    catalog = runner.invoke(_cli_app(), [*base, "catalog"])
    assert catalog.exit_code == 0, catalog.stdout
    assert json.loads(catalog.stdout)["data"]["total_count"] == 1

    shown = runner.invoke(
        _cli_app(),
        [
            *base,
            "show",
            record_digest,
            "--provider-report",
            provider_digest,
            "--ga4-report",
            joined_ga4,
        ],
    )
    assert shown.exit_code == 0, shown.stdout
    data = json.loads(shown.stdout)["data"]
    assert len(data["side_by_side"]["rows"]) == 1
    side = data["side_by_side"]["rows"][0]
    assert side["ga4_sessions"] == 40
    assert side["provider_clicks"] == 14
    assert "ratio" not in shown.stdout.lower()
    assert data["side_by_side"]["causal_claim"] is False


def test_join_create_refuses_a_report_from_another_property(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(join_cli, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    report_digest = _retain_ga4_report(ledger)
    contract = CampaignJoinContract.model_validate_json(
        canonical_bytes(
            {
                **_contract().model_dump(mode="json"),
                "ga4_property": {
                    "schema_version": 1,
                    "property_ref": "ga4.property.999",
                    "property_timezone": _TZ,
                },
            }
        )
    )
    path = _write_contract(tmp_path, contract)

    result = runner.invoke(
        _cli_app(),
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "join",
            "create",
            "--input",
            str(path),
            "--ga4-report",
            report_digest,
        ],
    )

    assert result.exit_code != 0
    envelope = json.loads(result.stdout)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["diagnostics"][0]["code"] == "JOIN_MAP_PROPERTY_MISMATCH"


def test_join_create_refuses_a_provider_report_as_the_ga4_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(join_cli, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    provider_digest = str(persist_report(ledger, _provider_report(ledger=ledger))["record_digest"])
    path = _write_contract(tmp_path, _contract())

    result = runner.invoke(
        _cli_app(),
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "join",
            "create",
            "--input",
            str(path),
            "--ga4-report",
            provider_digest,
        ],
    )

    assert result.exit_code != 0
    envelope = json.loads(result.stdout)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["diagnostics"][0]["code"] == "JOIN_SOURCE_INVALID"


def test_the_retained_record_payload_is_the_exact_canonical_join_map(
    tmp_path: Path, installed_join_retention: None
) -> None:
    ledger = Ledger(tmp_path)
    join_map = resolve_campaign_joins(_contract(), (_row(),))
    record = retain_join_map(ledger, join_map)

    stored = next(
        item
        for item in ledger.checkpoint()["records_by_stream"]["changes"]
        if item["record_digest"] == record["record_digest"]
    )
    assert stored["record_kind"] == JOIN_MAP_RECORD_KIND
    assert digest(stored["payload"]) == digest(join_map.model_dump(mode="json"))
    validate_data(stored["payload"])


def test_join_create_refuses_to_claim_retention_the_ledger_cannot_provide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uninstalled_join_retention: None
) -> None:
    """`--retain` fails closed rather than emitting an unretained receipt."""
    monkeypatch.setattr(join_cli, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    report_digest = _retain_ga4_report(ledger)
    path = _write_contract(tmp_path, _contract())

    result = runner.invoke(
        _cli_app(),
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "join",
            "create",
            "--input",
            str(path),
            "--ga4-report",
            report_digest,
            "--retain",
        ],
    )

    assert result.exit_code != 0
    envelope = json.loads(result.stdout)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["diagnostics"][0]["code"] == "JOIN_MAP_RETENTION_NOT_INSTALLED"
    assert join_map_catalog(ledger)["total_count"] == 0


def test_the_adapter_seam_attaches_declared_joins_without_changing_the_request() -> None:
    """The join map changes no GA4 request, adds no egress, and is optional."""
    import httpx

    from adhelm.providers.ga4 import Ga4AggregateQuery, Ga4DataClient, Ga4Identity
    from adhelm.transport import SafeHttpClient

    page = {
        "dimensionHeaders": [
            {"name": "sessionCampaignName"},
            {"name": "sessionSource"},
            {"name": "sessionMedium"},
        ],
        "metricHeaders": [{"name": "sessions"}, {"name": "totalUsers"}, {"name": "keyEvents"}],
        "rows": [
            {
                "dimensionValues": [
                    {"value": "brand-search"},
                    {"value": "google"},
                    {"value": "cpc"},
                ],
                "metricValues": [{"value": "40"}, {"value": "30"}, {"value": "3"}],
            },
            {
                "dimensionValues": [{"value": "winback"}, {"value": "bing"}, {"value": "organic"}],
                "metricValues": [{"value": "5"}, {"value": "4"}, {"value": "0"}],
            },
        ],
        "rowCount": 2,
        "metadata": {"timeZone": _TZ, "currencyCode": "USD"},
    }
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=page)

    def client() -> Ga4DataClient:
        return Ga4DataClient(
            SafeHttpClient(
                httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
                {"analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"},
                read_attempts=1,
            ),
            lambda: "test-token-not-to-be-persisted",
            now=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
            connector_version="test",
        )

    query = Ga4AggregateQuery(
        aggregate_shape="CAMPAIGN_SOURCE_MEDIUM",
        property_id="123",
        account_ref=_PROPERTY,
        report_id="report.ga4-private",
        query_contract_hash="a" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 2),
        source_timezone=_TZ,
        currency="USD",
    )
    identity = Ga4Identity(
        property_ref=_PROPERTY,
        property_client_ref="123",
        web_stream_ref="ga4.stream.456",
        web_stream_client_ref="456",
        timezone=_TZ,
        currency="USD",
    )
    join_map = resolve_campaign_joins(_contract(), (_row(),))

    unjoined = client().run_aggregate_report(query, identity=identity)
    joined = client().run_aggregate_report(query, identity=identity, join_map=join_map)

    assert bodies[0] == bodies[1]
    assert unjoined.join_disclosure() == GA4_JOIN_DISCLOSURE
    assert unjoined.join_status_counts() == {"UNMAPPED": 2}
    assert joined.join_disclosure() == GA4_JOIN_APPLIED_DISCLOSURE
    assert joined.join_status_counts() == {"EXPLICIT_MAPPING": 1, "UNMAPPED": 1}
    brand = next(row for row in joined.rows if row.session_campaign_name == "brand-search")
    assert brand.provider_campaign_ref == _CAMPAIGN
    assert brand.join_map_digest == join_map.join_map_digest


def test_a_ga4_sentinel_row_is_never_presented_for_joining_and_stays_unmapped() -> None:
    """`(not set)`, `(other)`, `(direct)` and `(none)` are not identities.

    A sentinel can be neither a declared key nor a heuristic candidate, so the
    row is not offered to the engine at all and keeps its `UNMAPPED` state on
    the report itself.
    """
    report = _aggregate_report(
        (
            ("(not set)", "(direct)", "(none)", 9, 0),
            ("brand-search", "google", "(other)", 4, 0),
            ("brand-search", "google", "cpc", 40, 3),
        )
    )

    rows = ga4_join_rows_from_report(report)

    assert rows == (_row(),)
    join_map = resolve_campaign_joins(_contract(), rows)
    joined = apply_join_map(report, join_map)
    sentinel = next(row for row in joined.rows if row.session_campaign_name == "(not set)")
    assert sentinel.join_status == "UNMAPPED"
    assert sentinel.provider_campaign_ref is None
    assert sentinel.join_map_digest is None
    assert "UNMAPPED" in sentinel.data_quality_statuses


def test_a_superseding_contract_must_keep_its_bound_advertising_accounts() -> None:
    first = resolve_campaign_joins(_contract(), (_row(),))
    successor = CampaignJoinContract.model_validate_json(
        canonical_bytes(
            {
                **_contract(version=2, supersedes=first.content_digest).model_dump(mode="json"),
                "provider_accounts": [
                    {
                        "schema_version": 1,
                        "provider": "google_ads",
                        "account_ref": "google_ads.customer.9999999999",
                        "account_timezone": _TZ,
                    }
                ],
            }
        )
    )

    with pytest.raises(AdhelmError, match="bound advertising accounts"):
        validate_predecessor(first, successor)


def test_a_retained_version_needs_a_strictly_later_effective_time(
    tmp_path: Path, installed_join_retention: None
) -> None:
    """The ledger chain validator mirrors the conversion-definition rule."""
    ledger = Ledger(tmp_path)
    first = resolve_campaign_joins(_contract(), (_row(),))
    retain_join_map(ledger, first)
    stale = CampaignJoinContract.model_validate_json(
        canonical_bytes(
            {
                **_contract(version=2, supersedes=first.content_digest).model_dump(mode="json"),
                "effective_from": first.contract.effective_from.isoformat().replace("+00:00", "Z"),
            }
        )
    )

    with pytest.raises(AdhelmError, match="already retained"):
        retain_join_map(ledger, resolve_campaign_joins(stale, (_row(),)))


def _retained_map_with_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> tuple[list[str], str]:
    monkeypatch.setattr(join_cli, "export_key", lambda: b"p" * 32)
    ledger = Ledger(tmp_path)
    names = [f"camp-{index:03d}" for index in range(count)]
    report = _aggregate_report(tuple((name, "google", "cpc", 10, 1) for name in names), ledger)
    report_digest = str(persist_report(ledger, report.to_normalized_report())["record_digest"])
    contract = _contract(
        entries=tuple(
            _entry(
                provider_campaign_ref=f"google_ads.campaign.1234567890.{index:03d}",
                ga4_keys=(),
                provider_campaign_name=name,
            )
            for index, name in enumerate(names)
        )
    )
    path = _write_contract(tmp_path, contract)
    created = runner.invoke(
        _cli_app(),
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "join",
            "create",
            "--input",
            str(path),
            "--ga4-report",
            report_digest,
            "--retain",
        ],
    )
    assert created.exit_code == 0, created.stdout
    return names, str(json.loads(created.stdout)["data"]["record_digest"])


def test_join_show_pages_proposals_as_their_own_declared_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed_join_retention: None
) -> None:
    """`--section proposals` makes the proposal list define exhaustion."""
    names, record_digest = _retained_map_with_proposals(tmp_path, monkeypatch, 4)
    base = ["--data-dir", str(tmp_path), "measurement", "join", "show", record_digest]

    seen: list[str] = []
    cursor: str | None = "0"
    while cursor is not None:
        page = runner.invoke(
            _cli_app(), [*base, "--section", "proposals", "--limit", "2", "--cursor", cursor]
        )
        assert page.exit_code == 0, page.stdout
        envelope = json.loads(page.stdout)
        assert envelope["total_count"] == len(names)
        assert envelope["data"]["section"] == "proposals"
        seen.extend(
            item["session_campaign_name"] for item in envelope["data"]["heuristic_proposals"]
        )
        cursor = envelope["next_cursor"]

    assert seen == names


def test_join_show_refuses_an_undeclared_review_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed_join_retention: None
) -> None:
    _, record_digest = _retained_map_with_proposals(tmp_path, monkeypatch, 1)

    result = runner.invoke(
        _cli_app(),
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "join",
            "show",
            record_digest,
            "--section",
            "everything",
        ],
    )

    assert result.exit_code != 0
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "INVALID_JOIN_MAP_SELECTION"
