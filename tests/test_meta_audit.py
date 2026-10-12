# ruff: noqa: E501
"""Fixture-only tests for the Meta Ads provider audit rule table.

Every case here is offline.  No test performs or simulates a Meta request, and
no case reconciles against Meta Ads Manager.
"""

from datetime import UTC, date, datetime
from typing import Any

import pytest

from adhelm.audit import AuditPolicy, audit_provider
from adhelm.audits import (
    COMPILABLE_OPERATION_KINDS,
    MAXIMUM_INLINE_FINDING_IDS,
    ProviderAuditContext,
    ProviderAuditEvidence,
    ResourceSnapshotState,
    RetainedCreativeCatalog,
    RetainedCreativeCatalogEntry,
    RetainedInventory,
    RetainedSnapshotRecord,
    _has_digest_like_run,
    provider_rule,
)
from adhelm.audits.meta_ads import (
    PAUSE_PLAN_ROUTE,
    RULE_OUTCOME_GROUPS,
    RULE_TABLE_VERSION,
    RULES,
    evaluate,
)
from adhelm.canonical import canonical_bytes, digest
from adhelm.models import Money
from adhelm.privacy import validate_data
from adhelm.providers.meta_ads import (
    META_ADS_BID_STRATEGY_NOT_RETURNED,
    META_ADS_BID_STRATEGY_RETURNED,
    META_ADS_BUDGET_MODE_DAILY,
    META_ADS_BUDGET_MODE_DAILY_AND_LIFETIME,
    META_ADS_BUDGET_MODE_LIFETIME,
    META_ADS_BUDGET_MODE_NOT_RETURNED,
    META_ADS_BUYING_TYPE_NOT_RETURNED,
    META_ADS_BUYING_TYPE_RESERVED,
    META_ADS_BUYING_TYPE_RETURNED,
    META_ADS_CREATIVE_ASSOCIATED,
    META_ADS_CREATIVE_IMAGE_HASH_ABSENT,
    META_ADS_CREATIVE_IMAGE_HASH_PRESENT,
    META_ADS_CREATIVE_LINK_ABSENT,
    META_ADS_CREATIVE_LINK_PRESENT,
    META_ADS_CREATIVE_NOT_ASSOCIATED,
    META_ADS_OBJECTIVE_NOT_RETURNED,
    META_ADS_OBJECTIVE_RETURNED,
    META_ADS_SCHEDULE_END_ABSENT,
    META_ADS_SCHEDULE_END_PRESENT,
    META_ADS_TARGETING_AGE_ABSENT,
    META_ADS_TARGETING_AGE_PRESENT,
    META_ADS_TARGETING_DECLARED,
    META_ADS_TARGETING_GEO_ABSENT,
    META_ADS_TARGETING_GEO_PRESENT,
    META_ADS_TARGETING_INTEREST_ABSENT,
    META_ADS_TARGETING_INTEREST_PRESENT,
    META_ADS_TARGETING_NOT_DECLARED,
)
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope

REPORT_DIGEST = "a" * 64
OBSERVED = datetime(2026, 9, 1, 12, tzinfo=UTC)
EARLIER_OBSERVED = datetime(2026, 8, 25, 12, tzinfo=UTC)
AS_OF = datetime(2026, 9, 1, 18, tzinfo=UTC)
ACCOUNT = "meta_ads.account.act_123"
CAMPAIGN = "meta_ads.campaign.11"
AD_SET = "meta_ads.adset.12"
AD = "meta_ads.ad.13"

HEALTHY_CAMPAIGN_QUALITY = (
    META_ADS_OBJECTIVE_RETURNED,
    META_ADS_BUYING_TYPE_RETURNED,
    META_ADS_BID_STRATEGY_RETURNED,
    META_ADS_BUDGET_MODE_DAILY,
    META_ADS_SCHEDULE_END_PRESENT,
)
HEALTHY_AD_SET_QUALITY = (
    META_ADS_TARGETING_DECLARED,
    META_ADS_TARGETING_GEO_PRESENT,
    META_ADS_TARGETING_AGE_PRESENT,
    META_ADS_TARGETING_INTEREST_PRESENT,
    META_ADS_BUDGET_MODE_DAILY,
    META_ADS_SCHEDULE_END_PRESENT,
    META_ADS_BID_STRATEGY_RETURNED,
)
HEALTHY_AD_QUALITY = (
    META_ADS_CREATIVE_ASSOCIATED,
    META_ADS_CREATIVE_IMAGE_HASH_PRESENT,
    META_ADS_CREATIVE_LINK_PRESENT,
    "META_AD_REVIEW_NOT_FLAGGED",
)


def _digest(seed: int) -> str:
    return format(seed, "064x")


def _envelope(provider: str = "meta_ads", quality: list[str] | None = None) -> ReportEnvelope:
    return ReportEnvelope(
        report_id="report:meta-fixture",
        provider=provider,  # type: ignore[arg-type]
        source_account_ref=ACCOUNT,
        query_contract_hash="b" * 64,
        window_start=date(2026, 8, 25),
        window_end=date(2026, 8, 31),
        source_timezone="UTC",
        report_timezone="UTC",
        currency="USD",
        attribution_model="META_ACTION_ATTRIBUTION_WINDOWS",
        attribution_window="7d_click,1d_view",
        observed_at=OBSERVED,
        data_quality_statuses=["COMPLETE"] if quality is None else quality,
        provider_api_version="v26.0",
        connector_version="test",
    )


def _row(
    *,
    resource_ref: str = CAMPAIGN,
    impressions: int | None = 100,
    clicks: int | None = 4,
    reach: int | None = 80,
    minor_units: int = 125,
) -> PerformanceRow:
    return PerformanceRow(
        report_id="report:meta-fixture",
        resource_level="CAMPAIGN",
        metric_grain="CAMPAIGN",
        resource_type="META_ADS_CAMPAIGN",
        resource_ref=resource_ref,
        campaign_ref=resource_ref,
        dimension_type="DATE",
        dimension_value="2026-08-25",
        impressions=impressions,
        clicks=clicks,
        reach=reach,
        spend=Money(minor_units=minor_units, currency="USD"),
        data_quality_statuses=["COMPLETE"],
    )


def _report(
    rows: list[PerformanceRow] | None = None,
    provider: str = "meta_ads",
    quality: list[str] | None = None,
) -> NormalizedReport:
    return NormalizedReport(
        envelope=_envelope(provider, quality),
        performance_rows=[_row()] if rows is None else rows,
    )


def _record(
    *,
    seed: int,
    resource_type: str,
    resource_ref: str,
    parents: tuple[str, ...] = (),
    state: dict[str, Any] | None = None,
    status: str = "ACTIVE",
    quality: tuple[str, ...] = (),
    observed_at: datetime = OBSERVED,
) -> RetainedSnapshotRecord:
    normalized = state if state is not None else {"status": status, "currency": "USD"}
    return RetainedSnapshotRecord(
        record_digest=_digest(seed),
        observed_at=observed_at,
        snapshot=ResourceSnapshotState(
            provider_api_version="v26.0",
            resource_type=resource_type,  # type: ignore[arg-type]
            resource_ref=resource_ref,
            parent_resource_refs=parents,
            normalized_state=normalized,
            normalized_state_digest=digest(normalized),
            effective_status=status,
            data_quality_statuses=quality,
        ),
    )


def _healthy_records(
    *,
    status: str = "ACTIVE",
    campaign_state: dict[str, Any] | None = None,
    campaign_quality: tuple[str, ...] = HEALTHY_CAMPAIGN_QUALITY,
    ad_set_state: dict[str, Any] | None = None,
    ad_set_quality: tuple[str, ...] = HEALTHY_AD_SET_QUALITY,
    ad_quality: tuple[str, ...] = HEALTHY_AD_QUALITY,
    ad_status: str | None = None,
    observed_at: datetime = OBSERVED,
) -> tuple[RetainedSnapshotRecord, ...]:
    campaign = campaign_state or {
        "status": status,
        "currency": "USD",
        "objective": "OUTCOME_TRAFFIC",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "budget_minor": 5_000,
        "budget_type": "DAILY",
        "schedule_start": "2026-08-01T00:00:00+00:00",
        "schedule_end": "2026-09-30T00:00:00+00:00",
    }
    ad_set = ad_set_state or {
        "status": status,
        "currency": "USD",
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "budget_minor": 2_000,
        "budget_type": "DAILY",
        "schedule_start": "2026-08-01T00:00:00+00:00",
        "schedule_end": "2026-09-30T00:00:00+00:00",
    }
    return (
        _record(
            seed=1,
            resource_type="ACCOUNT",
            resource_ref=ACCOUNT,
            status="1",
            observed_at=observed_at,
        ),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state=campaign,
            status=status,
            quality=campaign_quality,
            observed_at=observed_at,
        ),
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_SET,
            parents=(CAMPAIGN,),
            state=ad_set,
            status=status,
            quality=ad_set_quality,
            observed_at=observed_at,
        ),
        _record(
            seed=4,
            resource_type="AD",
            resource_ref=AD,
            parents=(CAMPAIGN, AD_SET),
            status=ad_status or status,
            quality=ad_quality,
            observed_at=observed_at,
        ),
    )


def _inventory(
    records: tuple[RetainedSnapshotRecord, ...],
    *,
    completeness: str = "COMPLETE_BOUNDED_READ",
    observed_at: datetime = OBSERVED,
) -> RetainedInventory:
    return RetainedInventory(
        provider="meta_ads",
        account_ref=ACCOUNT,
        observed_at=observed_at,
        completeness=completeness,  # type: ignore[arg-type]
        records=records,
    )


def _policy() -> AuditPolicy:
    return AuditPolicy(maximum_observation_age_seconds=604_800, require_conversion_evidence=False)


def _context(
    *,
    report: NormalizedReport | None = None,
    evidence: ProviderAuditEvidence | None = None,
) -> ProviderAuditContext:
    return ProviderAuditContext(
        provider="meta_ads",
        report=report if report is not None else _report(),
        source_record_digest=REPORT_DIGEST,
        policy=_policy(),
        as_of=AS_OF,
        evidence=evidence if evidence is not None else ProviderAuditEvidence(),
    )


def _fired(sections: Any) -> set[str]:
    return {item.rule_id for item in sections.findings}


def _healthy_sections(**kwargs: Any) -> Any:
    return evaluate(
        _context(evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records(**kwargs))))
    )


# ------------------------------------------------------------------ registry


def test_the_meta_rule_module_is_reachable_through_the_registry() -> None:
    assert provider_rule("meta_ads") is evaluate


def test_the_rule_table_refuses_evidence_from_another_provider() -> None:
    with pytest.raises(ValueError, match="Meta Ads evidence only"):
        evaluate(
            ProviderAuditContext(
                provider="linkedin_ads",
                report=_report(provider="linkedin_ads"),
                source_record_digest=REPORT_DIGEST,
                policy=_policy(),
                as_of=AS_OF,
                evidence=ProviderAuditEvidence(),
            )
        )


# ------------------------------------------------------------------ coverage


def test_every_audit_dimension_carries_exactly_one_status_and_the_ceiling_is_derived() -> None:
    sections = _healthy_sections()
    coverage = {item.dimension: item.status for item in sections.coverage}
    assert len(sections.coverage) == 14
    assert coverage == {
        "STRUCTURE": "COVERED",
        "DELIVERY": "PARTIAL",
        "SPEND": "PARTIAL",
        "BIDS": "PARTIAL",
        "OBJECTIVE": "PARTIAL",
        "TARGETING": "PARTIAL",
        # Capped by `META_ADS_READ_SURFACES` whatever a caller supplies.
        "PLACEMENT": "NOT_READ",
        "CREATIVE": "PARTIAL",
        "DESTINATION_CONFIGURATION": "PARTIAL",
        "TRACKING": "NOT_READ",
        "NATIVE_CONVERSION": "NOT_READ",
        "BROWSER_MEASUREMENT": "NOT_APPLICABLE",
        "POLICY": "PARTIAL",
        "DRIFT": "NOT_READ",
    }
    assert sections.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"
    assert sections.live_provider_reads == "NONE_RETAINED_RECORDS_ONLY"
    assert sections.rule_table_version == RULE_TABLE_VERSION


def test_an_unread_dimension_stays_unread_even_when_a_caller_supplies_records() -> None:
    """The surface map is the ceiling, not the supplied evidence."""
    import adhelm.audits.meta_ads as module

    for dimension in ("PLACEMENT", "TRACKING", "NATIVE_CONVERSION"):
        assert not module._surface_backed(dimension)
    for dimension in ("STRUCTURE", "SPEND", "BIDS", "TARGETING", "CREATIVE", "POLICY"):
        assert module._surface_backed(dimension)


def test_coverage_without_any_evidence_is_unread_rather_than_healthy() -> None:
    sections = evaluate(
        _context(report=NormalizedReport(envelope=_envelope(), performance_rows=[]))
    )
    statuses = {item.status for item in sections.coverage}
    assert statuses <= {"NOT_READ", "NOT_APPLICABLE"}
    for item in sections.coverage:
        if item.status in {"NOT_READ", "NOT_APPLICABLE"}:
            assert item.evidence_refs == ()
            assert item.evidence_record_count == 0
    # The unread-surface disclosures describe the fixed read contract, not any
    # retained record, so they survive an audit with no inventory at all and
    # anchor to the audited report instead of vanishing with the account
    # snapshot.  Nothing that describes a retained resource fires.
    fired = _fired(sections)
    assert {
        "MA_PLACEMENT_NOT_READ",
        "MA_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ",
        "MA_NATIVE_CONVERSION_NOT_READ",
        "MA_BROWSER_NOT_APPLICABLE",
        "MA_POLICY_APP_REVIEW_AND_VERIFICATION_NOT_READ",
    } == fired
    for finding in sections.findings:
        assert finding.finding.resource_refs == (ACCOUNT,)
        assert finding.finding.evidence_refs == (REPORT_DIGEST,)


def test_a_declared_subset_lowers_structure_coverage_and_says_why() -> None:
    sections = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records(), completeness="BOUNDED_NON_EXHAUSTIVE")
            )
        )
    )
    structure = next(item for item in sections.coverage if item.dimension == "STRUCTURE")
    assert structure.status == "PARTIAL"
    assert "subset" in structure.rationale


def test_an_unproven_read_boundary_states_that_absence_was_not_evaluated() -> None:
    records = _healthy_records()
    sections = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(records, completeness="READ_BOUNDARY_UNVERIFIED"),
                earlier_inventory=_inventory(
                    _healthy_records(observed_at=EARLIER_OBSERVED)[:2],
                    completeness="READ_BOUNDARY_UNVERIFIED",
                    observed_at=EARLIER_OBSERVED,
                ),
            )
        )
    )
    drift = next(item for item in sections.coverage if item.dimension == "DRIFT")
    assert drift.status == "PARTIAL"
    assert "proves nothing" in drift.rationale
    assert "not evaluated at all" in drift.rationale
    assert "MA_DRIFT_ABSENT_FROM_LATER_INVENTORY" not in _fired(sections)


# --------------------------------------------------------------- rule firing


def test_a_healthy_retained_account_fires_only_the_unread_and_boundary_rules() -> None:
    fired = _fired(_healthy_sections())
    assert fired == {
        "MA_BIDS_BILLING_EVENT_NOT_RETAINED",
        "MA_BROWSER_NOT_APPLICABLE",
        "MA_CREATIVE_CONTENT_TEXT_NOT_READ",
        "MA_CREATIVE_IMAGE_BYTES_NOT_PROVABLE",
        "MA_DESTINATION_VALUES_NOT_READ",
        "MA_NATIVE_CONVERSION_NOT_READ",
        "MA_PLACEMENT_NOT_READ",
        "MA_POLICY_ACCOUNT_STATUS_NOT_REVIEWED",
        "MA_POLICY_APP_REVIEW_AND_VERIFICATION_NOT_READ",
        "MA_TARGETING_VALUES_NOT_READ",
        "MA_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ",
        # The one report-scoped disclosure: the fixed insights read returns no
        # freshness timestamp at all.
        "MA_DELIVERY_REPORT_FRESHNESS_NOT_READ",
    }


@pytest.mark.parametrize(
    ("rule_id", "kwargs"),
    [
        (
            "MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET",
            {
                "campaign_state": {"status": "ACTIVE", "schedule_end": "2026-09-30T00:00:00+00:00"},
                "ad_set_state": {"status": "ACTIVE", "schedule_end": "2026-09-30T00:00:00+00:00"},
                "campaign_quality": (META_ADS_BUDGET_MODE_NOT_RETURNED,),
                "ad_set_quality": (META_ADS_TARGETING_DECLARED, META_ADS_BUDGET_MODE_NOT_RETURNED),
            },
        ),
        (
            "MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_SCHEDULE_END",
            {
                "campaign_state": {
                    "status": "ACTIVE",
                    "currency": "USD",
                    "budget_minor": 500,
                    "budget_type": "DAILY",
                }
            },
        ),
        (
            "MA_SPEND_LIFETIME_BUDGET_WITHOUT_END_TIME",
            {"ad_set_quality": (META_ADS_BUDGET_MODE_LIFETIME, META_ADS_SCHEDULE_END_ABSENT)},
        ),
        (
            "MA_SPEND_BOTH_BUDGET_FIELDS_RETAINED",
            {"ad_set_quality": (META_ADS_BUDGET_MODE_DAILY_AND_LIFETIME,)},
        ),
        (
            "MA_BIDS_STRATEGY_NOT_RETURNED",
            {"ad_set_quality": (META_ADS_BID_STRATEGY_NOT_RETURNED,)},
        ),
        (
            "MA_BIDS_STRATEGY_NOT_REVIEWED",
            {"ad_set_quality": ("META_BID_STRATEGY_NOT_IN_REVIEWED_ENUM",)},
        ),
        (
            "MA_BIDS_CAPPED_STRATEGY_WITHOUT_BID_AMOUNT",
            {
                "ad_set_state": {
                    "status": "ACTIVE",
                    "currency": "USD",
                    "bid_strategy": "COST_CAP",
                    "budget_minor": 2_000,
                    "budget_type": "DAILY",
                    "schedule_end": "2026-09-30T00:00:00+00:00",
                }
            },
        ),
        ("MA_OBJECTIVE_NOT_RETURNED", {"campaign_quality": (META_ADS_OBJECTIVE_NOT_RETURNED,)}),
        (
            "MA_OBJECTIVE_NOT_REVIEWED",
            {"campaign_quality": ("META_OBJECTIVE_NOT_IN_REVIEWED_ENUM",)},
        ),
        (
            "MA_OBJECTIVE_BUYING_TYPE_NOT_RETURNED",
            {"campaign_quality": (META_ADS_BUYING_TYPE_NOT_RETURNED,)},
        ),
        (
            "MA_OBJECTIVE_RESERVED_BUYING_TYPE_UNSUPPORTED",
            {"campaign_quality": (META_ADS_BUYING_TYPE_RESERVED,)},
        ),
        ("MA_TARGETING_NOT_DECLARED", {"ad_set_quality": (META_ADS_TARGETING_NOT_DECLARED,)}),
        (
            "MA_TARGETING_GEO_ABSENT",
            {"ad_set_quality": (META_ADS_TARGETING_DECLARED, META_ADS_TARGETING_GEO_ABSENT)},
        ),
        (
            "MA_TARGETING_AGE_ABSENT",
            {"ad_set_quality": (META_ADS_TARGETING_DECLARED, META_ADS_TARGETING_AGE_ABSENT)},
        ),
        (
            "MA_TARGETING_INTEREST_ABSENT",
            {"ad_set_quality": (META_ADS_TARGETING_DECLARED, META_ADS_TARGETING_INTEREST_ABSENT)},
        ),
        ("MA_CREATIVE_NOT_ASSOCIATED", {"ad_quality": (META_ADS_CREATIVE_NOT_ASSOCIATED,)}),
        ("MA_CREATIVE_IMAGE_HASH_ABSENT", {"ad_quality": (META_ADS_CREATIVE_IMAGE_HASH_ABSENT,)}),
        ("MA_DESTINATION_LINK_ABSENT", {"ad_quality": (META_ADS_CREATIVE_LINK_ABSENT,)}),
        ("MA_POLICY_AD_REVIEW_BLOCKED", {"ad_quality": ("META_AD_REVIEW_BLOCKED",)}),
        ("MA_POLICY_AD_REVIEW_PENDING", {"ad_quality": ("META_AD_REVIEW_PENDING",)}),
        ("MA_POLICY_AD_NEEDS_ATTENTION", {"ad_quality": ("META_AD_STATUS_NEEDS_ATTENTION",)}),
        ("MA_STRUCTURE_PARENT_PAUSE_PROPAGATED", {"ad_status": "CAMPAIGN_PAUSED"}),
        ("MA_STRUCTURE_STATUS_NOT_REVIEWED", {"ad_status": "SOMETHING_NEW"}),
    ],
)
def test_each_settings_rule_fires_only_for_the_state_it_names(
    rule_id: str, kwargs: dict[str, Any]
) -> None:
    assert rule_id not in _fired(_healthy_sections())
    assert rule_id in _fired(_healthy_sections(**kwargs))


def test_the_structure_rules_fire_for_a_broken_hierarchy_and_not_for_a_whole_one() -> None:
    healthy = _fired(_healthy_sections())
    for rule_id in (
        "MA_STRUCTURE_CAMPAIGN_WITHOUT_AD_SET",
        "MA_STRUCTURE_AD_SET_WITHOUT_AD",
        "MA_STRUCTURE_AD_SET_PARENT_NOT_RETAINED",
        "MA_STRUCTURE_AD_PARENT_NOT_RETAINED",
    ):
        assert rule_id not in healthy
    records = _healthy_records()
    account, campaign, ad_set, ad = records
    lonely = evaluate(
        _context(evidence=ProviderAuditEvidence(inventory=_inventory((account, campaign))))
    )
    assert "MA_STRUCTURE_CAMPAIGN_WITHOUT_AD_SET" in _fired(lonely)
    childless = evaluate(
        _context(evidence=ProviderAuditEvidence(inventory=_inventory((account, campaign, ad_set))))
    )
    assert "MA_STRUCTURE_AD_SET_WITHOUT_AD" in _fired(childless)
    orphans = evaluate(
        _context(evidence=ProviderAuditEvidence(inventory=_inventory((account, ad_set, ad))))
    )
    fired = _fired(orphans)
    assert "MA_STRUCTURE_AD_SET_PARENT_NOT_RETAINED" in fired
    assert "MA_STRUCTURE_AD_PARENT_NOT_RETAINED" not in fired


def test_a_campaign_bounded_only_by_an_ad_set_budget_is_not_reported_as_unbounded() -> None:
    """Meta allows the budget at either level, so either one bounds the campaign."""
    sections = _healthy_sections(
        campaign_state={"status": "ACTIVE", "schedule_end": "2026-09-30T00:00:00+00:00"},
        campaign_quality=(META_ADS_BUDGET_MODE_NOT_RETURNED,),
    )
    assert "MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET" not in _fired(sections)


@pytest.mark.parametrize(
    ("rule_id", "kwargs"),
    [
        (
            "MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET",
            {
                "campaign_state": {"status": "PAUSED", "schedule_end": "2026-09-30T00:00:00+00:00"},
                "ad_set_state": {"status": "PAUSED"},
                "campaign_quality": (META_ADS_BUDGET_MODE_NOT_RETURNED,),
                "ad_set_quality": (META_ADS_TARGETING_DECLARED, META_ADS_BUDGET_MODE_NOT_RETURNED),
            },
        ),
        (
            "MA_SPEND_ACTIVE_CAMPAIGN_WITHOUT_SCHEDULE_END",
            {"campaign_state": {"status": "PAUSED", "currency": "USD", "budget_minor": 500}},
        ),
        ("MA_TARGETING_NOT_DECLARED", {"ad_set_quality": (META_ADS_TARGETING_NOT_DECLARED,)}),
        (
            "MA_TARGETING_GEO_ABSENT",
            {"ad_set_quality": (META_ADS_TARGETING_DECLARED, META_ADS_TARGETING_GEO_ABSENT)},
        ),
    ],
)
def test_a_serving_guarded_rule_stays_silent_for_a_resource_that_declares_no_intent_to_serve(
    rule_id: str, kwargs: dict[str, Any]
) -> None:
    paused = dict(kwargs)
    paused.setdefault("status", "PAUSED")
    assert rule_id not in _fired(_healthy_sections(**paused))


# ---------------------------------------------------------- delivery / spend


def test_the_delivery_and_spend_row_rules_fire_only_for_the_rows_they_name() -> None:
    healthy = _fired(evaluate(_context()))
    for rule_id in (
        "MA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
        "MA_DELIVERY_REACH_EXCEEDS_IMPRESSIONS",
        "MA_DELIVERY_METRICS_MISSING",
        "MA_SPEND_WITHOUT_IMPRESSIONS",
        "MA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
    ):
        assert rule_id not in healthy
    assert "MA_DELIVERY_REPORT_FRESHNESS_NOT_READ" in healthy
    assert "MA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS" in _fired(
        evaluate(_context(report=_report([_row(impressions=1, clicks=9)])))
    )
    assert "MA_DELIVERY_REACH_EXCEEDS_IMPRESSIONS" in _fired(
        evaluate(_context(report=_report([_row(impressions=5, clicks=1, reach=9)])))
    )
    assert "MA_DELIVERY_METRICS_MISSING" in _fired(
        evaluate(_context(report=_report([_row(impressions=None)])))
    )
    assert "MA_SPEND_WITHOUT_IMPRESSIONS" in _fired(
        evaluate(_context(report=_report([_row(impressions=0, clicks=0, reach=0)])))
    )
    assert "MA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED" in _fired(
        evaluate(_context(report=_report(quality=["PARTIAL_PAGE_FAILURE"])))
    )


def test_an_active_campaign_with_no_retained_impressions_is_low_confidence_only() -> None:
    sections = evaluate(
        _context(
            report=_report([_row(resource_ref="meta_ads.campaign.99", impressions=3)]),
            evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())),
        )
    )
    matched = [
        item
        for item in sections.findings
        if item.rule_id == "MA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"
    ]
    assert len(matched) == 1
    assert matched[0].finding.confidence == "LOW"
    assert "not proof of zero delivery" in matched[0].finding.observation
    assert matched[0].analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"


# ------------------------------------------------------------ local bindings


def _catalog(resource_ref: str | None) -> RetainedCreativeCatalog:
    return RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:version-1",
                record_digest=_digest(9),
                provider="meta_ads",
                provider_resource_ref=resource_ref,
                binding_status="LOCAL_DECLARED_UNVERIFIED"
                if resource_ref is not None
                else "NOT_CREATED",
            ),
        )
    )


def test_a_local_binding_that_matches_a_retained_ad_is_reported_as_unverified() -> None:
    sections = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records()), creative_catalog=_catalog(AD)
            )
        )
    )
    assert "MA_CREATIVE_LOCAL_BINDING_UNVERIFIED" in _fired(sections)
    assert "MA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" not in _fired(sections)


def test_a_local_binding_absence_needs_a_proven_read_boundary() -> None:
    missing = _catalog("meta_ads.ad.404")
    proven = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records()), creative_catalog=missing
            )
        )
    )
    assert "MA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" in _fired(proven)
    unproven = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records(), completeness="READ_BOUNDARY_UNVERIFIED"),
                creative_catalog=missing,
            )
        )
    )
    assert "MA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" not in _fired(unproven)
    without_inventory = evaluate(_context(evidence=ProviderAuditEvidence(creative_catalog=missing)))
    assert "MA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" not in _fired(without_inventory)


def test_a_declared_subset_lowers_an_absence_claim_to_low_confidence() -> None:
    sections = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records(), completeness="BOUNDED_NON_EXHAUSTIVE"),
                creative_catalog=_catalog("meta_ads.ad.404"),
            )
        )
    )
    matched = [
        item
        for item in sections.findings
        if item.rule_id == "MA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE"
    ]
    assert matched and matched[0].finding.confidence == "LOW"


# -------------------------------------------------------------------- drift


def test_drift_reports_changed_paths_with_an_unknown_actor_and_both_digests() -> None:
    later = _inventory(_healthy_records(status="PAUSED"))
    earlier = _inventory(
        _healthy_records(observed_at=EARLIER_OBSERVED), observed_at=EARLIER_OBSERVED
    )
    sections = evaluate(
        _context(evidence=ProviderAuditEvidence(inventory=later, earlier_inventory=earlier))
    )
    assert sections.drift_observations
    for observation in sections.drift_observations:
        assert observation.observed_actor == "UNKNOWN"
        assert observation.observation_interval_start == EARLIER_OBSERVED
        assert observation.observation_interval_end == OBSERVED
        if observation.change_kind == "STATE_CHANGED":
            assert observation.before_record_digest and observation.after_record_digest
            assert all(path.startswith("normalized_state.") for path in observation.changed_paths)
    coverage = {item.dimension: item.status for item in sections.coverage}
    assert coverage["DRIFT"] == "COVERED"


def test_absence_drift_from_two_whole_reads_is_medium_confidence_at_most() -> None:
    later = _inventory(_healthy_records()[:3])
    earlier = _inventory(
        _healthy_records(observed_at=EARLIER_OBSERVED), observed_at=EARLIER_OBSERVED
    )
    sections = evaluate(
        _context(evidence=ProviderAuditEvidence(inventory=later, earlier_inventory=earlier))
    )
    matched = [
        item for item in sections.findings if item.rule_id == "MA_DRIFT_ABSENT_FROM_LATER_INVENTORY"
    ]
    assert matched and matched[0].finding.confidence == "MEDIUM"


# ---------------------------------------------------------- recommendations


def test_the_audit_emits_only_observe_and_pause_recommendations() -> None:
    sections = _healthy_sections(
        campaign_state={"status": "ACTIVE"},
        campaign_quality=(META_ADS_BUDGET_MODE_NOT_RETURNED,),
        ad_set_state={"status": "ACTIVE"},
        ad_set_quality=(META_ADS_TARGETING_DECLARED, META_ADS_BUDGET_MODE_NOT_RETURNED),
    )
    kinds = {item.operation_kind for item in sections.recommendations}
    # This fixture's retained report is internally consistent, so only the one
    # compilable mutation is proposed.  An inconsistent row adds `OBSERVE` and
    # nothing else.
    assert kinds == {"PAUSE"}
    inconsistent = evaluate(
        _context(
            report=_report([_row(impressions=1, clicks=9)]),
            evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())),
        )
    )
    assert {item.operation_kind for item in inconsistent.recommendations} == {"OBSERVE"}
    assert "CREATE" in COMPILABLE_OPERATION_KINDS["meta_ads"]
    assert "CREATE" not in kinds
    # `CREATE` is what this build could compile, never what this audit
    # recommends: it appears only as a declined `considered_operation_kind`.
    records = _healthy_records()
    childless = evaluate(
        _context(evidence=ProviderAuditEvidence(inventory=_inventory((records[0], records[1]))))
    )
    considered = {item.considered_operation_kind for item in childless.no_recommendations}
    assert "CREATE" in considered
    assert "CREATE" not in {item.operation_kind for item in childless.recommendations}
    pause = [item for item in sections.recommendations if item.operation_kind == "PAUSE"]
    assert len(pause) == 1
    assert pause[0].execution_status == "NOT_EXECUTED"
    assert pause[0].plan_status == "NOT_A_PLAN"
    assert pause[0].authorization_status == "NO_AUTHORIZATION_DIGEST_PRESENT"
    assert pause[0].recommendation.proposed_plan_ref is None
    assert pause[0].recommendation.spend_impact_minor is None
    assert PAUSE_PLAN_ROUTE in pause[0].recommendation.hypothesis
    # No digest, and no reference to a compiled plan or an approval.
    assert "digest" not in pause[0].recommendation.hypothesis.lower()
    # In 0.1.0 no authorization can release a write, so the prose may not promise
    # an authorization path, and the manual step must be stated plainly.
    prose = (
        pause[0].recommendation.hypothesis,
        pause[0].recommendation.expected_impact,
        *pause[0].recommendation.observations,
        *pause[0].recommendation.assumptions,
    )
    assert not any("authoriz" in item.lower() for item in prose)
    assert "unreleased in 0.1.0" in pause[0].recommendation.expected_impact
    assert "manual step in Meta Ads Manager" in pause[0].recommendation.expected_impact


def test_a_pause_recommendation_is_not_produced_for_an_already_paused_campaign() -> None:
    sections = _healthy_sections(
        status="PAUSED",
        campaign_state={"status": "PAUSED"},
        campaign_quality=(META_ADS_BUDGET_MODE_NOT_RETURNED,),
        ad_set_state={"status": "PAUSED"},
        ad_set_quality=(META_ADS_TARGETING_DECLARED, META_ADS_BUDGET_MODE_NOT_RETURNED),
    )
    assert not [item for item in sections.recommendations if item.operation_kind == "PAUSE"]


def test_a_pause_recommendation_only_rests_on_campaign_scoped_serving_guarded_findings() -> None:
    import adhelm.audits.meta_ads as module

    sections = _healthy_sections(
        campaign_state={"status": "ACTIVE"},
        campaign_quality=(META_ADS_BUDGET_MODE_NOT_RETURNED,),
        ad_set_state={"status": "ACTIVE"},
        ad_set_quality=(META_ADS_TARGETING_DECLARED, META_ADS_BUDGET_MODE_NOT_RETURNED),
    )
    pause = next(item for item in sections.recommendations if item.operation_kind == "PAUSE")
    bound = set(pause.bound_rule_ids)
    assert bound == module._PAUSE_RULES
    by_id = {item.finding.finding_id: item for item in sections.findings}
    for finding_id in pause.finding_ids:
        finding = by_id[finding_id]
        assert finding.finding.resource_refs == (CAMPAIGN,)
    # The ad-grain spend rule keeps its OBSERVE row and never enters this one.
    assert "MA_SPEND_WITHOUT_IMPRESSIONS" not in bound


def test_every_rule_belongs_to_exactly_one_recommendation_or_declined_group() -> None:
    union: set[str] = set()
    for group in RULE_OUTCOME_GROUPS:
        assert not union & group
        union |= set(group)
    assert union == set(RULES)


def test_a_disapproved_ad_is_declined_rather_than_bound_to_the_campaign_pause_route() -> None:
    sections = _healthy_sections(ad_quality=("META_AD_REVIEW_BLOCKED",))
    declined = [
        item for item in sections.no_recommendations if item.considered_operation_kind == "PAUSE"
    ]
    assert len(declined) == 1
    assert declined[0].reason == "OPERATION_NOT_COMPILABLE_FOR_PROVIDER"
    assert "acts on a campaign" in declined[0].detail
    assert not [item for item in sections.recommendations if item.operation_kind == "PAUSE"]


def test_every_bound_exceeded_row_has_a_distinct_subject() -> None:
    records: list[RetainedSnapshotRecord] = [
        _record(seed=1, resource_type="ACCOUNT", resource_ref=ACCOUNT, status="1")
    ]
    seed = 100
    for index in range(40):
        campaign = f"meta_ads.campaign.c{index}"
        records.append(
            _record(
                seed=seed,
                resource_type="CAMPAIGN",
                resource_ref=campaign,
                parents=(ACCOUNT,),
                state={"status": "ACTIVE"},
                quality=(META_ADS_BUDGET_MODE_NOT_RETURNED, META_ADS_OBJECTIVE_NOT_RETURNED),
            )
        )
        seed += 1
        records.append(
            _record(
                seed=seed,
                resource_type="AD_GROUP",
                resource_ref=f"meta_ads.adset.s{index}",
                parents=(campaign,),
                state={"status": "ACTIVE"},
                quality=(
                    META_ADS_TARGETING_NOT_DECLARED,
                    META_ADS_BID_STRATEGY_NOT_RETURNED,
                    META_ADS_BUDGET_MODE_NOT_RETURNED,
                ),
            )
        )
        seed += 1
        records.append(
            _record(
                seed=seed,
                resource_type="AD",
                resource_ref=f"meta_ads.ad.a{index}",
                parents=(campaign, f"meta_ads.adset.s{index}"),
                state={"status": "ACTIVE"},
                quality=(
                    META_ADS_CREATIVE_NOT_ASSOCIATED,
                    META_ADS_CREATIVE_IMAGE_HASH_ABSENT,
                    META_ADS_CREATIVE_LINK_ABSENT,
                    "META_AD_STATUS_NEEDS_ATTENTION",
                ),
            )
        )
        seed += 1
    sections = evaluate(
        _context(evidence=ProviderAuditEvidence(inventory=_inventory(tuple(records))))
    )
    assert sections.bound_exceeded
    subjects = [item.subject for item in sections.bound_exceeded]
    assert len(subjects) == len(set(subjects))
    for entry in sections.bound_exceeded:
        assert entry.bound == MAXIMUM_INLINE_FINDING_IDS
        assert entry.inlined_finding_count + entry.omitted_finding_count == (
            entry.total_finding_count
        )
        assert entry.resolution == "PAGE_FINDINGS_AND_MATCH_RULE_ID"
        assert not _has_digest_like_run(entry.subject)


def test_more_than_six_hundred_findings_stay_individually_addressable() -> None:
    records: list[RetainedSnapshotRecord] = [
        _record(seed=1, resource_type="ACCOUNT", resource_ref=ACCOUNT, status="1")
    ]
    seed = 1_000
    for index in range(220):
        campaign = f"meta_ads.campaign.c{index}"
        records.append(
            _record(
                seed=seed,
                resource_type="CAMPAIGN",
                resource_ref=campaign,
                parents=(ACCOUNT,),
                state={"status": "ACTIVE"},
                quality=(META_ADS_BUDGET_MODE_NOT_RETURNED, META_ADS_OBJECTIVE_NOT_RETURNED),
            )
        )
        seed += 1
    sections = evaluate(
        _context(evidence=ProviderAuditEvidence(inventory=_inventory(tuple(records))))
    )
    assert len(sections.findings) > 600
    identifiers = [item.finding.finding_id for item in sections.findings]
    assert len(identifiers) == len(set(identifiers))
    for recommendation in sections.recommendations:
        assert len(recommendation.finding_ids) <= MAXIMUM_INLINE_FINDING_IDS
        assert recommendation.bound_finding_count >= len(recommendation.finding_ids)
    for declined in sections.no_recommendations:
        assert len(declined.finding_ids) <= MAXIMUM_INLINE_FINDING_IDS


# ------------------------------------------------------- determinism, safety


def test_the_same_evidence_produces_a_byte_identical_result() -> None:
    evidence = ProviderAuditEvidence(inventory=_inventory(_healthy_records()))
    first = evaluate(_context(evidence=evidence))
    second = evaluate(_context(evidence=evidence))
    assert canonical_bytes(first) == canonical_bytes(second)
    assert [item.finding.finding_id for item in first.findings] == sorted(
        item.finding.finding_id for item in first.findings
    )


def test_no_finding_or_recommendation_carries_prohibited_or_digest_like_prose() -> None:
    sections = _healthy_sections(
        campaign_state={"status": "ACTIVE"},
        campaign_quality=(META_ADS_BUDGET_MODE_NOT_RETURNED,),
        ad_quality=(META_ADS_CREATIVE_NOT_ASSOCIATED, META_ADS_CREATIVE_LINK_ABSENT),
    )
    validate_data(sections.model_dump(mode="json"))
    for finding in sections.findings:
        assert not _has_digest_like_run(finding.finding.observation)
        assert not _has_digest_like_run(finding.finding.suggested_action)
    for recommendation in sections.recommendations:
        assert not _has_digest_like_run(recommendation.recommendation.hypothesis)
        assert not _has_digest_like_run(recommendation.recommendation.expected_impact)


def test_the_rule_table_version_is_part_of_every_finding_identity() -> None:
    import adhelm.audits.meta_ads as module

    first = _healthy_sections()
    original = module.RULE_TABLE_VERSION
    try:
        module.RULE_TABLE_VERSION = original + ".changed"
        second = _healthy_sections()
    finally:
        module.RULE_TABLE_VERSION = original
    assert {item.finding.finding_id for item in first.findings}.isdisjoint(
        item.finding.finding_id for item in second.findings
    )


def test_the_seam_refuses_evidence_bound_to_another_account() -> None:
    from adhelm.errors import AdhelmError

    other = RetainedInventory(
        provider="meta_ads",
        account_ref="meta_ads.account.act_999",
        observed_at=OBSERVED,
        completeness="COMPLETE_BOUNDED_READ",
        records=_healthy_records(),
    )
    with pytest.raises(AdhelmError) as raised:
        audit_provider(
            _report(),
            source_record_digest=REPORT_DIGEST,
            policy=_policy(),
            as_of=AS_OF,
            evidence=ProviderAuditEvidence(inventory=other),
        )
    assert raised.value.code == "INVALID_AUDIT_INPUT"


def test_the_provider_neutral_audit_is_embedded_unchanged() -> None:
    from adhelm.audit import audit_report

    result = audit_provider(
        _report(),
        source_record_digest=REPORT_DIGEST,
        policy=_policy(),
        as_of=AS_OF,
        evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())),
    )
    assert result.rules_status == "EVALUATED"
    assert result.sections is not None
    assert result.sections.provider == "meta_ads"
    assert result.audit == audit_report(
        _report(), source_record_digest=REPORT_DIGEST, policy=_policy(), as_of=result.audit.as_of
    )


def test_every_rule_in_the_table_is_reachable_from_some_retained_fixture() -> None:
    """No rule may be unreachable: each of the 50 must fire for some evidence.

    An unreachable rule is dead prose that an operator would read as a check
    this audit performs.  The union below is assembled from fixtures only, so
    this also pins that the whole table is exercised offline.
    """
    account, campaign, ad_set, ad = _healthy_records()
    fired: set[str] = set()
    fired |= _fired(_healthy_sections())
    # Structure: a childless campaign, a childless ad set, and two orphans.
    fired |= _fired(
        evaluate(
            _context(evidence=ProviderAuditEvidence(inventory=_inventory((account, campaign))))
        )
    )
    fired |= _fired(
        evaluate(
            _context(
                evidence=ProviderAuditEvidence(inventory=_inventory((account, campaign, ad_set)))
            )
        )
    )
    fired |= _fired(
        evaluate(_context(evidence=ProviderAuditEvidence(inventory=_inventory((account, ad_set)))))
    )
    fired |= _fired(
        evaluate(_context(evidence=ProviderAuditEvidence(inventory=_inventory((account, ad)))))
    )
    # Delivery and spend rows.
    for rows in (
        [_row(impressions=1, clicks=9)],
        [_row(impressions=5, clicks=1, reach=9)],
        [_row(impressions=None)],
        [_row(impressions=0, clicks=0, reach=0)],
        [_row(resource_ref="meta_ads.campaign.99")],
    ):
        fired |= _fired(
            evaluate(
                _context(
                    report=_report(rows),
                    evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())),
                )
            )
        )
    fired |= _fired(evaluate(_context(report=_report(quality=["PARTIAL_PAGE_FAILURE"]))))
    # Settings, targeting, creative, destination, and policy variants.
    for kwargs in (
        {
            "campaign_state": {"status": "ACTIVE"},
            "campaign_quality": (
                META_ADS_BUDGET_MODE_NOT_RETURNED,
                META_ADS_OBJECTIVE_NOT_RETURNED,
                META_ADS_BUYING_TYPE_NOT_RETURNED,
                META_ADS_BUYING_TYPE_RESERVED,
                "META_OBJECTIVE_NOT_IN_REVIEWED_ENUM",
                "META_BID_STRATEGY_NOT_IN_REVIEWED_ENUM",
            ),
            "ad_set_state": {"status": "ACTIVE"},
            "ad_set_quality": (
                META_ADS_TARGETING_DECLARED,
                META_ADS_TARGETING_GEO_ABSENT,
                META_ADS_TARGETING_AGE_ABSENT,
                META_ADS_TARGETING_INTEREST_ABSENT,
                META_ADS_BUDGET_MODE_NOT_RETURNED,
                META_ADS_BID_STRATEGY_NOT_RETURNED,
            ),
        },
        {
            "ad_set_quality": (
                META_ADS_TARGETING_NOT_DECLARED,
                META_ADS_BUDGET_MODE_DAILY_AND_LIFETIME,
                META_ADS_SCHEDULE_END_ABSENT,
                META_ADS_BUDGET_MODE_LIFETIME,
            ),
            "ad_set_state": {
                "status": "ACTIVE",
                "currency": "USD",
                "bid_strategy": "COST_CAP",
            },
        },
        {
            "ad_quality": (
                META_ADS_CREATIVE_NOT_ASSOCIATED,
                META_ADS_CREATIVE_IMAGE_HASH_ABSENT,
                META_ADS_CREATIVE_LINK_ABSENT,
                "META_AD_REVIEW_BLOCKED",
            )
        },
        {"ad_quality": ("META_AD_REVIEW_PENDING",)},
        {"ad_quality": ("META_AD_STATUS_NEEDS_ATTENTION",)},
        {"ad_status": "CAMPAIGN_PAUSED"},
        {"ad_status": "SOMETHING_NEW"},
    ):
        fired |= _fired(_healthy_sections(**kwargs))
    # A creative value the local privacy screen withheld.
    fired |= _fired(
        _healthy_sections(
            ad_quality=(*HEALTHY_AD_QUALITY, "META_CREATIVE_VALUE_WITHHELD_BY_LOCAL_SCREEN")
        )
    )
    # A connector release that began retaining one more field.  It is a local
    # state-shape difference, never an out-of-band change at Meta.
    shape_earlier = _inventory(
        _healthy_records(
            observed_at=EARLIER_OBSERVED,
            campaign_state={"status": "ACTIVE", "currency": "USD", "objective": "OUTCOME_TRAFFIC"},
        ),
        observed_at=EARLIER_OBSERVED,
    )
    shape_later = _inventory(
        _healthy_records(
            campaign_state={
                "status": "ACTIVE",
                "currency": "USD",
                "objective": "OUTCOME_TRAFFIC",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            }
        )
    )
    shape_sections = evaluate(
        _context(
            evidence=ProviderAuditEvidence(inventory=shape_later, earlier_inventory=shape_earlier)
        )
    )
    assert "MA_DRIFT_LOCAL_STATE_SHAPE_CHANGED" in _fired(shape_sections)
    assert "MA_DRIFT_STATE_CHANGED" not in _fired(shape_sections)
    assert not shape_sections.drift_observations
    fired |= _fired(shape_sections)
    # Local creative bindings, both directions.
    for resource in (AD, "meta_ads.ad.404"):
        fired |= _fired(
            evaluate(
                _context(
                    evidence=ProviderAuditEvidence(
                        inventory=_inventory(_healthy_records()),
                        creative_catalog=_catalog(resource),
                    )
                )
            )
        )
    # Drift, in all three directions.
    earlier = _inventory(
        _healthy_records(observed_at=EARLIER_OBSERVED), observed_at=EARLIER_OBSERVED
    )
    for later in (
        _inventory(_healthy_records(status="PAUSED")),
        _inventory(_healthy_records()[:3]),
        _inventory(
            (
                *_healthy_records(),
                _record(
                    seed=77,
                    resource_type="AD",
                    resource_ref="meta_ads.ad.new",
                    parents=(CAMPAIGN, AD_SET),
                ),
            )
        ),
    ):
        fired |= _fired(
            evaluate(
                _context(evidence=ProviderAuditEvidence(inventory=later, earlier_inventory=earlier))
            )
        )
    assert fired == set(RULES), sorted(set(RULES) - fired)


def test_a_local_state_shape_finding_names_both_retained_shape_versions() -> None:
    """The suggested action carries the evidence that resolves the difference.

    Telling a reader to "compare the two snapshots' state-shape versions"
    without naming them is not an actionable instruction; the finding reads
    them and says which one each side declared.
    """
    from adhelm.providers.meta_ads import META_ADS_STATE_SHAPE_STATUS

    earlier = _inventory(
        _healthy_records(
            observed_at=EARLIER_OBSERVED,
            campaign_state={"status": "ACTIVE", "currency": "USD"},
            campaign_quality=(*HEALTHY_CAMPAIGN_QUALITY, "META_ADS_STATE_SHAPE_2026-01-01.1"),
        ),
        observed_at=EARLIER_OBSERVED,
    )
    later = _inventory(
        _healthy_records(
            campaign_state={
                "status": "ACTIVE",
                "currency": "USD",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            },
            campaign_quality=(*HEALTHY_CAMPAIGN_QUALITY, META_ADS_STATE_SHAPE_STATUS),
        )
    )
    sections = evaluate(
        _context(evidence=ProviderAuditEvidence(inventory=later, earlier_inventory=earlier))
    )
    shape = [
        item for item in sections.findings if item.rule_id == "MA_DRIFT_LOCAL_STATE_SHAPE_CHANGED"
    ]
    assert len(shape) == 1
    action = shape[0].finding.suggested_action
    assert "earlier 2026-01-01.1" in action
    assert "later 2026-09-09.1" in action
    assert "this product changed" in action
    assert not sections.drift_observations


def test_a_state_shape_finding_says_so_when_neither_read_recorded_a_version() -> None:
    earlier = _inventory(
        _healthy_records(
            observed_at=EARLIER_OBSERVED,
            campaign_state={"status": "ACTIVE", "currency": "USD"},
        ),
        observed_at=EARLIER_OBSERVED,
    )
    later = _inventory(
        _healthy_records(
            campaign_state={"status": "ACTIVE", "currency": "USD", "bid_strategy": "COST_CAP"}
        )
    )
    sections = evaluate(
        _context(evidence=ProviderAuditEvidence(inventory=later, earlier_inventory=earlier))
    )
    shape = next(
        item for item in sections.findings if item.rule_id == "MA_DRIFT_LOCAL_STATE_SHAPE_CHANGED"
    )
    action = shape.finding.suggested_action
    assert "earlier NOT_RECORDED, later NOT_RECORDED" in action
    # Same (absent) version on both sides, so the value-absence reading is named.
    assert "a value became absent" in action


def test_a_state_shape_finding_identity_changes_with_the_versions_it_names() -> None:
    """The named versions are content, so they are part of the finding identity."""
    import adhelm.audits.meta_ads as module

    def _shape_finding(version: str) -> str:
        earlier = _inventory(
            _healthy_records(
                observed_at=EARLIER_OBSERVED,
                campaign_state={"status": "ACTIVE", "currency": "USD"},
                campaign_quality=(*HEALTHY_CAMPAIGN_QUALITY, f"META_ADS_STATE_SHAPE_{version}"),
            ),
            observed_at=EARLIER_OBSERVED,
        )
        later = _inventory(
            _healthy_records(
                campaign_state={"status": "ACTIVE", "currency": "USD", "bid_strategy": "COST_CAP"}
            )
        )
        sections = evaluate(
            _context(evidence=ProviderAuditEvidence(inventory=later, earlier_inventory=earlier))
        )
        return next(
            item.finding.finding_id
            for item in sections.findings
            if item.rule_id == "MA_DRIFT_LOCAL_STATE_SHAPE_CHANGED"
        )

    assert module._NO_STATE_SHAPE_VERSION == "NOT_RECORDED"
    assert _shape_finding("2026-01-01.1") != _shape_finding("2026-02-02.1")
