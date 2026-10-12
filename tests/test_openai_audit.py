# ruff: noqa: E501
"""Fixture-only tests for the OpenAI Ads provider audit rule table.

Every case here is offline.  No test performs or simulates an OpenAI request,
and no case reconciles against the OpenAI Ads interface.
"""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from adhelm.audit import AuditPolicy, audit_provider
from adhelm.audits import (
    COMPILABLE_OPERATION_KINDS,
    MAXIMUM_INLINE_FINDING_IDS,
    ProviderAuditContext,
    ProviderAuditEvidence,
    ProviderAuditSections,
    ResourceSnapshotState,
    RetainedCreativeCatalog,
    RetainedCreativeCatalogEntry,
    RetainedInventory,
    RetainedSnapshotRecord,
    _has_digest_like_run,
    provider_rule,
)

# The `_RULE_MODULES` entry that would import this module on demand is a
# pending dependency request against the seam (`adhelm/audits/__init__.py` is
# not this ticket's to edit), so the import below stands in for it.  After the
# entry lands, `provider_rule("openai_ads")` resolves without this import and
# every assertion here is unchanged.
from adhelm.audits.openai_ads import RULE_TABLE_VERSION, RULES, evaluate
from adhelm.canonical import canonical_bytes, digest
from adhelm.models import Money
from adhelm.privacy import validate_data
from adhelm.providers.openai_ads import (
    OPENAI_ADS_AD_REVIEW_STATUS_NOT_RETURNED,
    OPENAI_ADS_AD_REVIEW_STATUS_RETURNED,
    OPENAI_ADS_CONTEXT_HINTS_DECLARED,
    OPENAI_ADS_CONTEXT_HINTS_NOT_DECLARED,
    OPENAI_ADS_CONVERSION_SETTINGS_ASSOCIATED,
    OPENAI_ADS_CONVERSION_SETTINGS_NOT_ASSOCIATED,
    OPENAI_ADS_CREATIVE_BODY_ABSENT,
    OPENAI_ADS_CREATIVE_BODY_PRESENT,
    OPENAI_ADS_CREATIVE_FILE_ASSOCIATED,
    OPENAI_ADS_CREATIVE_FILE_NOT_ASSOCIATED,
    OPENAI_ADS_CREATIVE_TARGET_URL_ABSENT,
    OPENAI_ADS_CREATIVE_TARGET_URL_PRESENT,
    OPENAI_ADS_CREATIVE_TITLE_ABSENT,
    OPENAI_ADS_CREATIVE_TITLE_PRESENT,
    OPENAI_ADS_TARGETING_DECLARED,
    OPENAI_ADS_TARGETING_NOT_DECLARED,
)
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope

REPORT_DIGEST = "a" * 64
OBSERVED = datetime(2026, 9, 1, 12, tzinfo=UTC)
EARLIER_OBSERVED = datetime(2026, 8, 25, 12, tzinfo=UTC)
AS_OF = datetime(2026, 9, 1, 18, tzinfo=UTC)
ACCOUNT = "openai_ads.account.acct_1"
CAMPAIGN = "openai_ads.campaign.camp_1"
AD_GROUP = "openai_ads.ad_group.grp_1"
AD = "openai_ads.ad.ad_1"

HEALTHY_CAMPAIGN_QUALITY = (
    OPENAI_ADS_TARGETING_DECLARED,
    OPENAI_ADS_CONVERSION_SETTINGS_ASSOCIATED,
)
HEALTHY_AD_QUALITY = (
    OPENAI_ADS_CREATIVE_FILE_ASSOCIATED,
    OPENAI_ADS_CREATIVE_TITLE_PRESENT,
    OPENAI_ADS_CREATIVE_BODY_PRESENT,
    OPENAI_ADS_CREATIVE_TARGET_URL_PRESENT,
    OPENAI_ADS_AD_REVIEW_STATUS_RETURNED,
)


def _digest(seed: int) -> str:
    return format(seed, "064x")


def _envelope(provider: str = "openai_ads", quality: list[str] | None = None) -> ReportEnvelope:
    return ReportEnvelope(
        report_id="report:openai-fixture",
        provider=provider,  # type: ignore[arg-type]
        source_account_ref=ACCOUNT,
        query_contract_hash="b" * 64,
        window_start=date(2026, 8, 25),
        window_end=date(2026, 8, 31),
        source_timezone="UTC",
        report_timezone="UTC",
        currency="USD",
        attribution_model="OPENAI_ADS_PROVIDER_REPORTED",
        attribution_window=None,
        observed_at=OBSERVED,
        data_quality_statuses=["COMPLETE"] if quality is None else quality,
        provider_api_version="v1",
        connector_version="test",
    )


def _row(
    *,
    resource_ref: str = CAMPAIGN,
    impressions: int | None = 100,
    clicks: int | None = 4,
    minor_units: int = 125,
) -> PerformanceRow:
    return PerformanceRow(
        report_id="report:openai-fixture",
        resource_level="CAMPAIGN",
        metric_grain="CAMPAIGN",
        resource_type="OPENAI_ADS_CAMPAIGN",
        resource_ref=resource_ref,
        campaign_ref=resource_ref,
        dimension_type="DATE",
        dimension_value="2026-08-25",
        impressions=impressions,
        clicks=clicks,
        spend=Money(minor_units=minor_units, currency="USD"),
        data_quality_statuses=["COMPLETE"],
    )


def _report(
    rows: list[PerformanceRow] | None = None,
    provider: str = "openai_ads",
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
    status: str = "active",
    quality: tuple[str, ...] = (),
    observed_at: datetime = OBSERVED,
) -> RetainedSnapshotRecord:
    normalized = state if state is not None else {"status": status, "currency": "USD"}
    return RetainedSnapshotRecord(
        record_digest=_digest(seed),
        observed_at=observed_at,
        snapshot=ResourceSnapshotState(
            provider_api_version="v1",
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
    campaign_status: str = "active",
    campaign_state: dict[str, Any] | None = None,
    campaign_quality: tuple[str, ...] = HEALTHY_CAMPAIGN_QUALITY,
    ad_quality: tuple[str, ...] = HEALTHY_AD_QUALITY,
    ad_group_quality: tuple[str, ...] = (OPENAI_ADS_CONTEXT_HINTS_DECLARED,),
    ad_group_state: dict[str, Any] | None = None,
    observed_at: datetime = OBSERVED,
) -> tuple[RetainedSnapshotRecord, ...]:
    campaign = campaign_state or {
        "status": campaign_status,
        "currency": "USD",
        "bid_strategy": "clicks",
        "openai_ads_lifetime_spend_limit_micros": "10000000",
        "budget_type": "LIFETIME",
        "budget_minor": 1000,
        "lifetime_budget_minor": 1000,
        "schedule_start": "2026-08-01T00:00:00+00:00",
        "schedule_end": "2026-09-30T00:00:00+00:00",
    }
    group = ad_group_state or {
        "status": campaign_status,
        "currency": "USD",
        "openai_ads_max_bid_micros": "250000",
        "bid_amount_minor": 25,
    }
    return (
        _record(seed=1, resource_type="ACCOUNT", resource_ref=ACCOUNT, observed_at=observed_at),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state=campaign,
            status=campaign_status,
            quality=campaign_quality,
            observed_at=observed_at,
        ),
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=(CAMPAIGN,),
            state=group,
            status=campaign_status,
            quality=ad_group_quality,
            observed_at=observed_at,
        ),
        _record(
            seed=4,
            resource_type="AD",
            resource_ref=AD,
            parents=(CAMPAIGN, AD_GROUP),
            status=campaign_status,
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
        provider="openai_ads",
        account_ref=ACCOUNT,
        observed_at=observed_at,
        completeness=completeness,  # type: ignore[arg-type]
        records=records,
    )


def _policy() -> AuditPolicy:
    return AuditPolicy(maximum_observation_age_seconds=604_800, require_conversion_evidence=False)


def _sections(
    *,
    report: NormalizedReport | None = None,
    evidence: ProviderAuditEvidence | None = None,
) -> ProviderAuditSections:
    return evaluate(
        ProviderAuditContext(
            provider="openai_ads",
            report=report if report is not None else _report(),
            source_record_digest=REPORT_DIGEST,
            policy=_policy(),
            as_of=AS_OF,
            evidence=evidence if evidence is not None else ProviderAuditEvidence(),
        )
    )


def _fired(sections: ProviderAuditSections) -> set[str]:
    return {finding.rule_id for finding in sections.findings}


def _coverage(sections: ProviderAuditSections) -> dict[str, str]:
    return {item.dimension: item.status for item in sections.coverage}


# --- registration and dispatch ------------------------------------------------


def test_the_openai_rule_module_registers_itself_in_the_provider_registry() -> None:
    assert provider_rule("openai_ads") is evaluate


def test_the_rule_table_refuses_evidence_from_another_provider() -> None:
    with pytest.raises(ValueError):
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


def test_audit_provider_dispatches_to_the_openai_table_end_to_end() -> None:
    result = audit_provider(
        _report(),
        source_record_digest=REPORT_DIGEST,
        policy=_policy(),
        as_of=AS_OF,
        evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())),
    )
    assert result.rules_status == "EVALUATED"
    assert result.sections is not None
    assert result.sections.rule_table_version == RULE_TABLE_VERSION
    assert result.sections.live_provider_reads == "NONE_RETAINED_RECORDS_ONLY"


# --- coverage map -------------------------------------------------------------


def test_every_dimension_has_exactly_one_coverage_status_with_no_evidence() -> None:
    sections = _sections(report=_report(rows=[]))
    coverage = _coverage(sections)
    assert coverage == {
        "STRUCTURE": "NOT_READ",
        "DELIVERY": "NOT_READ",
        "SPEND": "NOT_READ",
        "BIDS": "NOT_READ",
        "OBJECTIVE": "NOT_READ",
        "TARGETING": "NOT_READ",
        "PLACEMENT": "NOT_READ",
        "CREATIVE": "NOT_READ",
        "DESTINATION_CONFIGURATION": "NOT_READ",
        "TRACKING": "NOT_READ",
        "NATIVE_CONVERSION": "NOT_READ",
        "BROWSER_MEASUREMENT": "NOT_APPLICABLE",
        "POLICY": "NOT_READ",
        "DRIFT": "NOT_READ",
    }
    assert sections.findings == ()
    assert sections.recommendations == ()


def test_a_complete_read_covers_structure_and_leaves_unread_dimensions_unread() -> None:
    coverage = _coverage(
        _sections(evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())))
    )
    assert coverage["STRUCTURE"] == "COVERED"
    # Read but presence-only or partial; never COVERED, never healthy.
    for dimension in (
        "DELIVERY",
        "SPEND",
        "BIDS",
        "OBJECTIVE",
        "TARGETING",
        "CREATIVE",
        "DESTINATION_CONFIGURATION",
        "TRACKING",
        "POLICY",
    ):
        assert coverage[dimension] == "PARTIAL", dimension
    assert coverage["PLACEMENT"] == "NOT_READ"
    assert coverage["NATIVE_CONVERSION"] == "NOT_READ"
    assert coverage["BROWSER_MEASUREMENT"] == "NOT_APPLICABLE"


def test_a_declared_subset_read_lowers_structure_to_partial_and_says_why() -> None:
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_healthy_records(), completeness="BOUNDED_NON_EXHAUSTIVE")
        )
    )
    entry = next(item for item in sections.coverage if item.dimension == "STRUCTURE")
    assert entry.status == "PARTIAL"
    assert "subset" in entry.rationale


def test_an_unread_dimension_cites_no_evidence() -> None:
    sections = _sections(report=_report(rows=[]))
    for entry in sections.coverage:
        if entry.status in {"NOT_READ", "NOT_APPLICABLE"}:
            assert entry.evidence_refs == ()
            assert entry.evidence_sources == ()
            assert entry.evidence_record_count == 0


def test_delivery_is_never_covered_even_for_a_report_that_declares_complete() -> None:
    """The shared vocabulary has no exhaustive analytics member, so nothing may claim one."""
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())))
    assert _coverage(sections)["DELIVERY"] == "PARTIAL"
    assert sections.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"
    for finding in sections.findings:
        assert finding.analytics_completeness in {"BOUNDED_NON_EXHAUSTIVE", "NOT_APPLICABLE"}


# --- structure rules ----------------------------------------------------------


def test_a_healthy_hierarchy_fires_no_structure_rule() -> None:
    fired = _fired(
        _sections(evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())))
    )
    assert not {rule for rule in fired if rule.startswith("OA_STRUCTURE_")}


def test_a_campaign_without_an_ad_group_and_a_group_without_an_ad_both_fire() -> None:
    records = _healthy_records()
    fired = _fired(_sections(evidence=ProviderAuditEvidence(inventory=_inventory(records[:2]))))
    assert "OA_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP" in fired
    fired = _fired(_sections(evidence=ProviderAuditEvidence(inventory=_inventory(records[:3]))))
    assert "OA_STRUCTURE_AD_GROUP_WITHOUT_AD" in fired
    assert "OA_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP" not in fired


def test_an_absent_parent_fires_the_orphan_rules() -> None:
    records = _healthy_records()
    orphaned = (records[0], records[2], records[3])
    fired = _fired(_sections(evidence=ProviderAuditEvidence(inventory=_inventory(orphaned))))
    assert "OA_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED" in fired
    assert "OA_STRUCTURE_AD_PARENT_NOT_RETAINED" not in fired


def test_an_active_child_under_a_paused_parent_fires_the_intent_mismatch() -> None:
    records = list(_healthy_records(campaign_status="paused"))
    records[3] = _record(
        seed=4,
        resource_type="AD",
        resource_ref=AD,
        parents=(CAMPAIGN, AD_GROUP),
        status="active",
        quality=HEALTHY_AD_QUALITY,
    )
    fired = _fired(_sections(evidence=ProviderAuditEvidence(inventory=_inventory(tuple(records)))))
    assert "OA_STRUCTURE_SERVING_INTENT_MISMATCH" in fired


def test_an_unreviewed_campaign_status_fires_and_a_documented_one_does_not() -> None:
    for status, expected in (("active", False), ("paused", False), ("draft", True)):
        records = _healthy_records(campaign_status=status)
        fired = _fired(_sections(evidence=ProviderAuditEvidence(inventory=_inventory(records))))
        assert ("OA_STRUCTURE_STATUS_NOT_REVIEWED" in fired) is expected, status


# --- setting rules ------------------------------------------------------------


def test_an_active_campaign_without_a_spend_limit_or_end_time_fires_both_spend_rules() -> None:
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(
                _healthy_records(
                    campaign_state={
                        "status": "active",
                        "currency": "USD",
                        "bid_strategy": "clicks",
                    }
                )
            )
        )
    )
    fired = _fired(sections)
    assert "OA_SPEND_LIMIT_NOT_RETURNED" in fired
    assert "OA_SPEND_SCHEDULE_NOT_RETURNED" in fired


def test_a_paused_campaign_without_a_spend_limit_fires_neither_spend_rule() -> None:
    fired = _fired(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    _healthy_records(
                        campaign_status="paused",
                        campaign_state={
                            "status": "paused",
                            "currency": "USD",
                            "bid_strategy": "clicks",
                        },
                    )
                )
            )
        )
    )
    assert "OA_SPEND_LIMIT_NOT_RETURNED" not in fired
    assert "OA_SPEND_SCHEDULE_NOT_RETURNED" not in fired


def test_an_unprojectable_native_amount_fires_the_money_rule() -> None:
    records = _healthy_records()
    subminor = _record(
        seed=3,
        resource_type="AD_GROUP",
        resource_ref=AD_GROUP,
        parents=(CAMPAIGN,),
        state={
            "status": "active",
            "currency": "USD",
            "openai_ads_max_bid_micros": "250001",
        },
        quality=(
            OPENAI_ADS_CONTEXT_HINTS_DECLARED,
            "SUBMINOR_AMOUNT_RETAINED_AS_NATIVE_MICROS",
        ),
    )
    fired = _fired(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory((records[0], records[1], subminor, records[3]))
            )
        )
    )
    assert "OA_SPEND_NATIVE_AMOUNT_NOT_PROJECTED" in fired


def test_a_missing_maximum_bid_and_missing_bidding_type_fire_their_rules() -> None:
    records = _healthy_records(
        campaign_state={"status": "active", "currency": "USD"},
        ad_group_state={"status": "active", "currency": "USD"},
    )
    fired = _fired(_sections(evidence=ProviderAuditEvidence(inventory=_inventory(records))))
    assert "OA_BIDS_MAX_BID_NOT_RETURNED" in fired
    assert "OA_OBJECTIVE_BIDDING_TYPE_NOT_RETURNED" in fired


def test_conversion_bidding_is_surfaced_as_a_mode_this_build_cannot_compile() -> None:
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(
                _healthy_records(
                    campaign_state={
                        "status": "active",
                        "currency": "USD",
                        "bid_strategy": "conversions",
                        "openai_ads_lifetime_spend_limit_micros": "10000000",
                        "schedule_end": "2026-09-30T00:00:00+00:00",
                    }
                )
            )
        )
    )
    assert "OA_OBJECTIVE_CONVERSION_BIDDING_UNSUPPORTED" in _fired(sections)
    declined = [
        item
        for item in sections.no_recommendations
        if "OA_OBJECTIVE_CONVERSION_BIDDING_UNSUPPORTED" in item.bound_rule_ids
    ]
    assert declined and declined[0].reason == "OPERATION_NOT_COMPILABLE_FOR_PROVIDER"


def test_an_undeclared_targeting_object_and_missing_context_hints_fire() -> None:
    fired = _fired(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    _healthy_records(
                        campaign_quality=(
                            OPENAI_ADS_TARGETING_NOT_DECLARED,
                            OPENAI_ADS_CONVERSION_SETTINGS_NOT_ASSOCIATED,
                        ),
                        ad_group_quality=(OPENAI_ADS_CONTEXT_HINTS_NOT_DECLARED,),
                    )
                )
            )
        )
    )
    assert "OA_TARGETING_NOT_DECLARED" in fired
    assert "OA_TARGETING_AD_GROUP_WITHOUT_CONTEXT_HINTS" in fired
    assert "OA_TRACKING_NO_CONVERSION_SETTING_ASSOCIATED" in fired


# --- creative, destination, and policy rules ----------------------------------


def test_an_incomplete_chat_card_fires_creative_destination_and_policy_rules() -> None:
    fired = _fired(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    _healthy_records(
                        ad_quality=(
                            OPENAI_ADS_CREATIVE_FILE_NOT_ASSOCIATED,
                            OPENAI_ADS_CREATIVE_TITLE_ABSENT,
                            OPENAI_ADS_CREATIVE_BODY_ABSENT,
                            OPENAI_ADS_CREATIVE_TARGET_URL_ABSENT,
                            OPENAI_ADS_AD_REVIEW_STATUS_NOT_RETURNED,
                        )
                    )
                )
            )
        )
    )
    assert "OA_CREATIVE_FILE_NOT_ASSOCIATED" in fired
    assert "OA_CREATIVE_COPY_INCOMPLETE" in fired
    assert "OA_DESTINATION_TARGET_URL_ABSENT" in fired
    assert "OA_POLICY_AD_REVIEW_STATUS_NOT_RETURNED" in fired
    assert "OA_POLICY_AD_REVIEW_ENUM_NOT_REVIEWED" not in fired


def test_a_complete_chat_card_reports_only_the_unreviewed_review_enumeration() -> None:
    fired = _fired(
        _sections(evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())))
    )
    assert "OA_POLICY_AD_REVIEW_ENUM_NOT_REVIEWED" in fired
    assert "OA_CREATIVE_FILE_NOT_ASSOCIATED" not in fired
    assert "OA_CREATIVE_COPY_INCOMPLETE" not in fired
    assert "OA_DESTINATION_TARGET_URL_ABSENT" not in fired


def test_a_local_creative_binding_is_never_treated_as_a_provider_association() -> None:
    catalog = RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:version:one",
                record_digest=_digest(11),
                provider="openai_ads",
                provider_resource_ref=AD,
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:version:two",
                record_digest=_digest(12),
                provider="openai_ads",
                provider_resource_ref="openai_ads.ad.ad_missing",
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
        )
    )
    fired = _fired(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records()), creative_catalog=catalog
            )
        )
    )
    assert "OA_CREATIVE_LOCAL_BINDING_UNVERIFIED" in fired
    assert "OA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" in fired


def test_a_local_binding_absence_drops_to_low_confidence_for_a_declared_subset() -> None:
    """A subset is a weaker read boundary, so the absence claim is weaker too."""
    catalog = RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:version:two",
                record_digest=_digest(12),
                provider="openai_ads",
                provider_resource_ref="openai_ads.ad.ad_missing",
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
        )
    )
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_healthy_records(), completeness="BOUNDED_NON_EXHAUSTIVE"),
            creative_catalog=catalog,
        )
    )
    matched = [
        item
        for item in sections.findings
        if item.rule_id == "OA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE"
    ]
    assert len(matched) == 1
    assert matched[0].finding.confidence == "LOW"


def test_a_local_binding_alone_still_produces_creative_coverage() -> None:
    catalog = RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:version:one",
                record_digest=_digest(11),
                provider="openai_ads",
                provider_resource_ref=AD,
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
        )
    )
    sections = _sections(evidence=ProviderAuditEvidence(creative_catalog=catalog))
    # The catalog is evidence about the local side of the binding, so `CREATIVE`
    # is still `PARTIAL` and names it.  It is not evidence about the provider
    # side: with no retained inventory there is no read boundary, so nothing is
    # absent from anything and the absence rule does not fire.
    assert _coverage(sections)["CREATIVE"] == "PARTIAL"
    creative = next(item for item in sections.coverage if item.dimension == "CREATIVE")
    assert "LOCAL_CREATIVE_CATALOG" in creative.evidence_sources
    assert "OA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" not in _fired(sections)


# --- delivery and spend rules -------------------------------------------------


def test_an_active_campaign_with_no_retained_impressions_fires_at_low_confidence() -> None:
    sections = _sections(
        report=_report(rows=[_row(resource_ref="openai_ads.campaign.other", impressions=5)]),
        evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())),
    )
    matched = [
        item
        for item in sections.findings
        if item.rule_id == "OA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"
    ]
    assert len(matched) == 1
    assert matched[0].finding.confidence == "LOW"
    assert "not proof of zero delivery" in matched[0].finding.observation


def test_inconsistent_and_missing_delivery_rows_fire_their_rules() -> None:
    rows = [
        _row(impressions=1, clicks=9),
        _row(resource_ref="openai_ads.campaign.two", impressions=None, clicks=None),
        _row(resource_ref="openai_ads.campaign.three", impressions=0, minor_units=500),
    ]
    fired = _fired(_sections(report=_report(rows=rows)))
    assert "OA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS" in fired
    assert "OA_DELIVERY_METRICS_MISSING" in fired
    assert "OA_SPEND_WITHOUT_IMPRESSIONS" in fired


def test_a_report_that_does_not_declare_complete_is_reported_explicitly() -> None:
    complete = _fired(_sections(report=_report()))
    assert "OA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED" not in complete
    incomplete = _fired(_sections(report=_report(quality=["PARTIAL_PAGE_FAILURE"])))
    assert "OA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED" in incomplete


def test_zero_spend_with_zero_impressions_is_not_a_spend_finding() -> None:
    fired = _fired(_sections(report=_report(rows=[_row(impressions=0, clicks=0, minor_units=0)])))
    assert "OA_SPEND_WITHOUT_IMPRESSIONS" not in fired


# --- drift --------------------------------------------------------------------


def test_a_snapshot_pair_reports_state_change_and_both_absences() -> None:
    earlier = _inventory(
        _healthy_records(campaign_status="paused", observed_at=EARLIER_OBSERVED)[:3],
        observed_at=EARLIER_OBSERVED,
    )
    later = _inventory(_healthy_records())
    sections = _sections(evidence=ProviderAuditEvidence(inventory=later, earlier_inventory=earlier))
    fired = _fired(sections)
    assert "OA_DRIFT_STATE_CHANGED" in fired
    assert "OA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY" in fired
    assert _coverage(sections)["DRIFT"] == "COVERED"
    for observation in sections.drift_observations:
        assert observation.observed_actor == "UNKNOWN"
        assert observation.observation_interval_start <= observation.observation_interval_end
        # Field paths only; never a before/after value.
        assert all(
            path.startswith(("normalized_state.", "parent_resource_refs"))
            for path in observation.changed_paths
        )


def test_absence_drift_is_suppressed_when_the_read_boundary_is_unproven() -> None:
    earlier = _inventory(
        _healthy_records(observed_at=EARLIER_OBSERVED)[:3],
        completeness="READ_BOUNDARY_UNVERIFIED",
        observed_at=EARLIER_OBSERVED,
    )
    later = _inventory(_healthy_records(), completeness="READ_BOUNDARY_UNVERIFIED")
    sections = _sections(evidence=ProviderAuditEvidence(inventory=later, earlier_inventory=earlier))
    assert "OA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY" not in _fired(sections)
    entry = next(item for item in sections.coverage if item.dimension == "DRIFT")
    assert "not evaluated at all" in entry.rationale


def test_a_resource_removed_from_the_later_read_is_reported_without_a_deletion_claim() -> None:
    earlier = _inventory(
        _healthy_records(observed_at=EARLIER_OBSERVED), observed_at=EARLIER_OBSERVED
    )
    later = _inventory(_healthy_records()[:3])
    sections = _sections(evidence=ProviderAuditEvidence(inventory=later, earlier_inventory=earlier))
    matched = [
        item for item in sections.findings if item.rule_id == "OA_DRIFT_ABSENT_FROM_LATER_INVENTORY"
    ]
    assert len(matched) == 1
    assert "not proof of deletion" in matched[0].finding.observation


# --- recommendations ----------------------------------------------------------


def test_every_recommendation_is_non_writing_and_compilable_for_openai() -> None:
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(
                _healthy_records(
                    campaign_state={"status": "active", "currency": "USD", "bid_strategy": "clicks"}
                )
            )
        )
    )
    assert sections.recommendations
    for item in sections.recommendations:
        assert item.execution_status == "NOT_EXECUTED"
        assert item.plan_status == "NOT_A_PLAN"
        assert item.authorization_status == "NO_AUTHORIZATION_DIGEST_PRESENT"
        assert item.recommendation.proposed_plan_ref is None
        assert (
            item.operation_kind == "OBSERVE"
            or item.operation_kind in COMPILABLE_OPERATION_KINDS["openai_ads"]
        )


def test_the_pause_recommendation_names_the_route_but_is_never_a_plan() -> None:
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(
                _healthy_records(
                    campaign_state={"status": "active", "currency": "USD", "bid_strategy": "clicks"}
                )
            )
        )
    )
    pause = [item for item in sections.recommendations if item.operation_kind == "PAUSE"]
    assert len(pause) == 1
    text = canonical_bytes(pause[0].model_dump(mode="json")).decode()
    assert "plan openai-pause" in text
    # In 0.1.0 no authorization can release a write, so the prose may not promise
    # an authorization path, and it may never carry a value that reads as one.
    prose = (
        pause[0].recommendation.hypothesis,
        pause[0].recommendation.expected_impact,
        *pause[0].recommendation.observations,
        *pause[0].recommendation.assumptions,
    )
    assert not any("authoriz" in item.lower() for item in prose)
    assert any("manual" in item for item in prose)
    assert "unreleased in 0.1.0" in pause[0].recommendation.expected_impact
    assert "manual step in OpenAI Ads Manager" in pause[0].recommendation.expected_impact
    assert not any(_has_digest_like_run(item) for item in prose)
    assert pause[0].authorization_status == "NO_AUTHORIZATION_DIGEST_PRESENT"
    assert pause[0].recommendation.proposed_plan_ref is None
    assert pause[0].recommendation.spend_impact_minor is None


def test_no_pause_recommendation_when_every_active_campaign_is_bounded() -> None:
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())))
    assert not [item for item in sections.recommendations if item.operation_kind == "PAUSE"]


def test_unread_dimensions_produce_an_explicit_declined_outcome() -> None:
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())))
    reasons = {item.reason for item in sections.no_recommendations}
    assert "COVERAGE_NOT_READ" in reasons
    coverage_declined = next(
        item for item in sections.no_recommendations if item.reason == "COVERAGE_NOT_READ"
    )
    assert "never reported as healthy" in coverage_declined.detail


def test_a_large_account_records_the_inline_bound_instead_of_truncating_silently() -> None:
    count = MAXIMUM_INLINE_FINDING_IDS + 5
    records = [_record(seed=1, resource_type="ACCOUNT", resource_ref=ACCOUNT)]
    for index in range(count):
        records.append(
            _record(
                seed=100 + index,
                resource_type="CAMPAIGN",
                resource_ref=f"openai_ads.campaign.camp_{index}",
                parents=(ACCOUNT,),
                state={"status": "active", "currency": "USD", "bid_strategy": "clicks"},
                quality=HEALTHY_CAMPAIGN_QUALITY,
            )
        )
    sections = _sections(
        report=_report(rows=[]),
        evidence=ProviderAuditEvidence(inventory=_inventory(tuple(records))),
    )
    pause = next(item for item in sections.recommendations if item.operation_kind == "PAUSE")
    assert len(pause.finding_ids) == MAXIMUM_INLINE_FINDING_IDS
    assert pause.bound_finding_count > MAXIMUM_INLINE_FINDING_IDS
    assert sections.bound_exceeded
    entry = next(
        item for item in sections.bound_exceeded if item.scope == "RECOMMENDATION_FINDING_IDS"
    )
    assert entry.inlined_finding_count + entry.omitted_finding_count == entry.total_finding_count
    assert entry.resolution == "PAGE_FINDINGS_AND_MATCH_RULE_ID"
    # Nothing is dropped: every bound finding is still its own row.
    assert (
        len([item for item in sections.findings if item.rule_id in pause.bound_rule_ids])
        == pause.bound_finding_count
    )


# --- determinism, privacy, and the no-secret boundary -------------------------


def test_the_same_evidence_produces_a_byte_identical_result() -> None:
    evidence = ProviderAuditEvidence(
        inventory=_inventory(_healthy_records()),
        earlier_inventory=_inventory(
            _healthy_records(campaign_status="paused", observed_at=EARLIER_OBSERVED),
            observed_at=EARLIER_OBSERVED,
        ),
    )
    first = _sections(evidence=evidence)
    second = _sections(evidence=evidence)
    assert canonical_bytes(first.model_dump(mode="json")) == canonical_bytes(
        second.model_dump(mode="json")
    )
    assert [item.finding.finding_id for item in first.findings] == sorted(
        item.finding.finding_id for item in first.findings
    )


def test_a_different_rule_table_version_would_change_every_finding_identity() -> None:
    assert RULE_TABLE_VERSION.startswith("openai_ads.rules.")
    identifiers = {
        item.finding.finding_id
        for item in _sections(
            evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records()))
        ).findings
    }
    assert identifiers and all(value.startswith("finding:sha256:") for value in identifiers)


def test_the_whole_result_passes_the_privacy_validator() -> None:
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_healthy_records()),
            earlier_inventory=_inventory(
                _healthy_records(campaign_status="paused", observed_at=EARLIER_OBSERVED),
                observed_at=EARLIER_OBSERVED,
            ),
        )
    )
    validate_data(sections.model_dump(mode="json"))


def test_no_rule_prose_carries_a_secret_a_url_or_a_digest_like_value() -> None:
    for rule in RULES.values():
        for text in (rule.observation, rule.suggested_action):
            validate_data(text)
            assert "http://" not in text and "https://" not in text
            assert "Bearer" not in text


def test_the_rule_table_makes_no_provider_request(monkeypatch: pytest.MonkeyPatch) -> None:
    import adhelm.transport as transport_module

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("A provider audit rule module must not make a request")

    monkeypatch.setattr(transport_module.SafeHttpClient, "request", refuse)
    _sections(evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())))


def test_a_coverage_rationale_may_not_carry_a_digest_like_value() -> None:
    from adhelm.audits import ProviderAuditCoverage

    with pytest.raises(ValidationError):
        ProviderAuditCoverage(
            dimension="STRUCTURE",
            status="NOT_READ",
            rationale="f" * 64,
        )
