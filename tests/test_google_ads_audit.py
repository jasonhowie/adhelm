# ruff: noqa: E501
"""Fixture-only tests for the Google Ads provider audit rule table.

Every case here is offline.  No test performs or simulates a Google Ads
request, and no case reconciles against the Google Ads interface.
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
    provider_rule,
)
from adhelm.audits.google_ads import RULE_TABLE_VERSION, RULES, evaluate
from adhelm.canonical import canonical_bytes, digest
from adhelm.models import Money
from adhelm.privacy import validate_data
from adhelm.providers.google_ads import (
    GOOGLE_ADS_ACCELERATED_BUDGET_DELIVERY,
    GOOGLE_ADS_AD_SCHEDULE_ABSENT,
    GOOGLE_ADS_AD_SCHEDULE_PRESENT,
    GOOGLE_ADS_BUDGET_DELIVERY_METHOD_PREFIX,
    GOOGLE_ADS_BUDGET_EXPLICITLY_SHARED,
    GOOGLE_ADS_BUDGET_NOT_EXPLICITLY_SHARED,
    GOOGLE_ADS_BUDGET_NOT_RETURNED,
    GOOGLE_ADS_BUDGET_PERIOD_PREFIX,
    GOOGLE_ADS_BUDGET_RETURNED,
    GOOGLE_ADS_BUDGET_TYPE_NOT_RETURNED,
    GOOGLE_ADS_BUDGET_TYPE_RETURNED,
    GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED,
    GOOGLE_ADS_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED,
    GOOGLE_ADS_FINAL_URL_ABSENT,
    GOOGLE_ADS_FINAL_URL_PRESENT,
    GOOGLE_ADS_GEO_TARGET_TYPE_NOT_RETURNED,
    GOOGLE_ADS_GEO_TARGET_TYPE_RETURNED,
    GOOGLE_ADS_NETWORK_CONTENT_ENABLED,
    GOOGLE_ADS_NETWORK_PARTNER_SEARCH_ENABLED,
    GOOGLE_ADS_NETWORK_SETTINGS_NOT_RETURNED,
    GOOGLE_ADS_NETWORK_SETTINGS_RETURNED,
    GOOGLE_ADS_PAGINATION_TOTAL_DECLARED,
    GOOGLE_ADS_PAGINATION_TOTAL_NOT_DECLARED,
    GOOGLE_ADS_POLICY_APPROVAL_PREFIX,
    GOOGLE_ADS_POLICY_APPROVAL_STATUS_NOT_RETURNED,
    GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED,
    GOOGLE_ADS_POLICY_REVIEW_PREFIX,
    GOOGLE_ADS_POLICY_REVIEW_STATUS_NOT_RETURNED,
    GOOGLE_ADS_POLICY_REVIEW_STATUS_RETURNED,
    GOOGLE_ADS_PRODUCTION_ACCOUNT_DECLARED,
    GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED,
    GOOGLE_ADS_RSA_DESCRIPTIONS_AT_DOCUMENTED_MINIMUM,
    GOOGLE_ADS_RSA_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM,
    GOOGLE_ADS_RSA_DESCRIPTIONS_PRESENT,
    GOOGLE_ADS_RSA_HEADLINES_AT_DOCUMENTED_MINIMUM,
    GOOGLE_ADS_RSA_HEADLINES_BELOW_DOCUMENTED_MINIMUM,
    GOOGLE_ADS_RSA_HEADLINES_PRESENT,
    GOOGLE_ADS_TEST_ACCOUNT_DECLARED,
)
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope

REPORT_DIGEST = "a" * 64
OBSERVED = datetime(2026, 9, 1, 12, tzinfo=UTC)
EARLIER_OBSERVED = datetime(2026, 8, 25, 12, tzinfo=UTC)
AS_OF = datetime(2026, 9, 1, 18, tzinfo=UTC)
ACCOUNT = "google_ads.customer.1234567890"
CAMPAIGN = "google_ads.campaign.11"
AD_GROUP = "google_ads.ad_group.21"
AD = "google_ads.ad.31"

HEALTHY_ACCOUNT_QUALITY = (
    GOOGLE_ADS_PAGINATION_TOTAL_DECLARED,
    GOOGLE_ADS_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED,
    GOOGLE_ADS_PRODUCTION_ACCOUNT_DECLARED,
)
HEALTHY_CAMPAIGN_QUALITY = (
    GOOGLE_ADS_AD_SCHEDULE_PRESENT,
    GOOGLE_ADS_BUDGET_RETURNED,
    GOOGLE_ADS_BUDGET_TYPE_RETURNED,
    GOOGLE_ADS_NETWORK_SETTINGS_RETURNED,
    GOOGLE_ADS_GEO_TARGET_TYPE_RETURNED,
    GOOGLE_ADS_BUDGET_PERIOD_PREFIX + "DAILY",
    GOOGLE_ADS_BUDGET_NOT_EXPLICITLY_SHARED,
)
HEALTHY_AD_QUALITY = (
    "CREATIVE_NOT_EXPOSED",
    GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED,
    GOOGLE_ADS_RSA_HEADLINES_PRESENT,
    GOOGLE_ADS_RSA_HEADLINES_AT_DOCUMENTED_MINIMUM,
    GOOGLE_ADS_RSA_DESCRIPTIONS_PRESENT,
    GOOGLE_ADS_RSA_DESCRIPTIONS_AT_DOCUMENTED_MINIMUM,
    GOOGLE_ADS_FINAL_URL_PRESENT,
    GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED,
    GOOGLE_ADS_POLICY_APPROVAL_PREFIX + "APPROVED",
    GOOGLE_ADS_POLICY_REVIEW_STATUS_RETURNED,
    GOOGLE_ADS_POLICY_REVIEW_PREFIX + "REVIEWED",
)


def _digest(seed: int) -> str:
    return format(seed, "064x")


def _policy(**overrides: Any) -> AuditPolicy:
    return AuditPolicy(
        maximum_observation_age_seconds=86_400,
        require_conversion_evidence=False,
        **overrides,
    )


def _envelope(provider: str = "google_ads", quality: list[str] | None = None) -> ReportEnvelope:
    return ReportEnvelope(
        report_id="report:google-fixture",
        provider=provider,  # type: ignore[arg-type]
        source_account_ref=ACCOUNT,
        query_contract_hash="b" * 64,
        window_start=date(2026, 8, 25),
        window_end=date(2026, 8, 31),
        source_timezone="UTC",
        report_timezone="UTC",
        currency="USD",
        attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
        attribution_window=None,
        observed_at=OBSERVED,
        data_quality_statuses=(
            ["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"] if quality is None else quality
        ),
        provider_api_version="v25",
        connector_version="test",
    )


def _row(
    *,
    resource_ref: str = CAMPAIGN,
    impressions: int | None = 100,
    clicks: int | None = 4,
    minor_units: int = 125,
    cost_micros: str | None = "1250000",
    day: str = "2026-08-25",
) -> PerformanceRow:
    extensions: dict[str, str] = {"google_ads_rounding": "HALF_EVEN_TO_MINOR"}
    if cost_micros is not None:
        extensions["google_ads_cost_micros"] = cost_micros
    return PerformanceRow(
        report_id="report:google-fixture",
        resource_level="CAMPAIGN",
        metric_grain="CAMPAIGN",
        resource_type="GOOGLE_ADS_CAMPAIGN",
        resource_ref=resource_ref,
        campaign_ref=resource_ref,
        dimension_type="DATE",
        dimension_value=day,
        impressions=impressions,
        clicks=clicks,
        spend=Money(minor_units=minor_units, currency="USD"),
        data_quality_statuses=["COMPLETE"],
        provider_extensions=extensions,
    )


def _report(
    rows: list[PerformanceRow] | None = None,
    provider: str = "google_ads",
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
    status: str = "ENABLED",
    quality: tuple[str, ...] = (),
    observed_at: datetime = OBSERVED,
) -> RetainedSnapshotRecord:
    normalized = state if state is not None else {"status": status, "currency": "USD"}
    return RetainedSnapshotRecord(
        record_digest=_digest(seed),
        observed_at=observed_at,
        snapshot=ResourceSnapshotState(
            provider_api_version="v25",
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
    campaign_status: str = "ENABLED",
    campaign_state: dict[str, Any] | None = None,
    campaign_quality: tuple[str, ...] = HEALTHY_CAMPAIGN_QUALITY,
    account_quality: tuple[str, ...] = HEALTHY_ACCOUNT_QUALITY,
    ad_quality: tuple[str, ...] = HEALTHY_AD_QUALITY,
    ad_group_state: dict[str, Any] | None = None,
    observed_at: datetime = OBSERVED,
) -> tuple[RetainedSnapshotRecord, ...]:
    campaign = campaign_state or {
        "status": campaign_status,
        "objective": "SEARCH",
        "currency": "USD",
        "budget_minor": 5000,
        "budget_type": "DAILY",
        "bid_strategy": "MANUAL_CPC",
        "schedule_start": "2026-08-01",
        "schedule_end": "2026-09-30",
    }
    group = ad_group_state or {
        "status": campaign_status,
        "objective": "SEARCH_STANDARD",
        "currency": "USD",
        "bid_amount_minor": 150,
    }
    return (
        _record(
            seed=1,
            resource_type="ACCOUNT",
            resource_ref=ACCOUNT,
            status="NOT_AVAILABLE_FROM_PROVIDER",
            quality=account_quality,
            observed_at=observed_at,
        ),
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
            observed_at=observed_at,
        ),
        _record(
            seed=4,
            resource_type="AD",
            resource_ref=AD,
            parents=(CAMPAIGN, AD_GROUP),
            state={"status": campaign_status, "objective": "RESPONSIVE_SEARCH_AD"},
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
        provider="google_ads",
        account_ref=ACCOUNT,
        observed_at=observed_at,
        completeness=completeness,  # type: ignore[arg-type]
        records=records,
    )


def _sections(
    *,
    report: NormalizedReport | None = None,
    evidence: ProviderAuditEvidence | None = None,
    policy: AuditPolicy | None = None,
) -> ProviderAuditSections:
    return evaluate(
        ProviderAuditContext(
            provider="google_ads",
            report=report if report is not None else _report(),
            source_record_digest=REPORT_DIGEST,
            policy=policy if policy is not None else _policy(),
            as_of=AS_OF,
            evidence=evidence if evidence is not None else ProviderAuditEvidence(),
        )
    )


def _rule_ids(sections: ProviderAuditSections) -> set[str]:
    return {item.rule_id for item in sections.findings}


def _coverage(sections: ProviderAuditSections) -> dict[str, str]:
    return {item.dimension: item.status for item in sections.coverage}


# --- registration and the seam ------------------------------------------------


def test_the_seam_resolves_the_google_rule_module_without_an_import() -> None:
    assert provider_rule("google_ads") is evaluate
    assert "PAUSE" in COMPILABLE_OPERATION_KINDS["google_ads"]
    assert "CREATE" in COMPILABLE_OPERATION_KINDS["google_ads"]


def test_the_rule_table_evaluates_only_google_ads_evidence() -> None:
    with pytest.raises(ValueError, match="Google Ads evidence only"):
        evaluate(
            ProviderAuditContext(
                provider="meta_ads",
                report=_report([], provider="meta_ads"),
                source_record_digest=REPORT_DIGEST,
                policy=_policy(),
                as_of=AS_OF,
                evidence=ProviderAuditEvidence(),
            )
        )


def test_retained_evidence_must_belong_to_the_audited_account() -> None:
    """Account binding: one audit never mixes two customers' records."""
    other = RetainedInventory(
        provider="google_ads",
        account_ref="google_ads.customer.9999999999",
        observed_at=OBSERVED,
        completeness="COMPLETE_BOUNDED_READ",
        records=(
            _record(seed=9, resource_type="ACCOUNT", resource_ref="google_ads.customer.9999999999"),
        ),
    )
    with pytest.raises(ValueError, match="belong to the audited account"):
        _sections(evidence=ProviderAuditEvidence(inventory=other))


# --- healthy baseline ---------------------------------------------------------


def test_a_healthy_account_reports_only_read_boundary_findings() -> None:
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())))
    fired = _rule_ids(sections)
    # Nothing structural, nothing about spend, bids, creative, destination or
    # policy restriction fires for a complete, consistent, ENABLED account.
    assert not fired & {
        "GA_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP",
        "GA_STRUCTURE_AD_GROUP_WITHOUT_AD",
        "GA_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED",
        "GA_STRUCTURE_AD_PARENT_NOT_RETAINED",
        "GA_STRUCTURE_SERVING_INTENT_MISMATCH",
        "GA_STRUCTURE_STATUS_NOT_REVIEWED",
        "GA_STRUCTURE_INVENTORY_PAGINATION_NOT_PROVEN",
        "GA_SPEND_BUDGET_NOT_RETURNED",
        "GA_SPEND_BUDGET_PERIOD_NOT_RETURNED",
        "GA_SPEND_BUDGET_EXPLICITLY_SHARED",
        "GA_SPEND_SCHEDULE_END_NOT_RETURNED",
        "GA_BIDS_AD_GROUP_WITHOUT_CPC_BID",
        "GA_BIDS_CPC_BID_ABOVE_DECLARED_MAXIMUM",
        "GA_BIDS_AUTOMATED_STRATEGY_NOT_LOCALLY_BOUNDED",
        "GA_OBJECTIVE_CHANNEL_OUTSIDE_COMPILABLE_SUBSET",
        "GA_TARGETING_AD_SCHEDULE_ABSENT",
        "GA_TARGETING_CONTENT_NETWORK_ENABLED",
        "GA_TARGETING_PARTNER_SEARCH_ENABLED",
        "GA_TARGETING_NETWORK_SETTINGS_NOT_RETURNED",
        "GA_TARGETING_GEO_TARGET_TYPE_NOT_RETURNED",
        "GA_CREATIVE_RESPONSIVE_SEARCH_AD_NOT_RETURNED",
        "GA_CREATIVE_HEADLINES_BELOW_DOCUMENTED_MINIMUM",
        "GA_CREATIVE_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM",
        "GA_DESTINATION_FINAL_URL_ABSENT",
        "GA_POLICY_APPROVAL_RESTRICTED",
        "GA_POLICY_APPROVAL_STATUS_NOT_RETURNED",
        "GA_POLICY_APPROVAL_STATUS_NOT_REVIEWED",
        "GA_POLICY_REVIEW_STATUS_NOT_RETURNED",
        "GA_POLICY_REVIEW_STATUS_NOT_REVIEWED",
        "GA_SPEND_BUDGET_ACCELERATED_DELIVERY",
        "GA_DRIFT_LOCAL_STATE_SHAPE_CHANGED",
        "GA_POLICY_TEST_ACCOUNT_DECLARED",
        "GA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
    }
    # The unread-surface disclosures always fire, because they describe the
    # read rather than the account.
    assert {
        "GA_BIDS_STRATEGY_DETAIL_NOT_READ",
        "GA_OBJECTIVE_CONVERSION_GOAL_NOT_READ",
        "GA_TARGETING_CRITERION_VALUES_NOT_READ",
        "GA_PLACEMENT_NOT_READ",
        "GA_CREATIVE_CONTENT_TEXT_NOT_READ",
        "GA_DESTINATION_VALUES_NOT_READ",
        "GA_TRACKING_CONVERSION_ACTION_NOT_READ",
        "GA_TRACKING_TEMPLATE_AND_PARAMETERS_NOT_READ",
        "GA_NATIVE_CONVERSION_NOT_READ",
        "GA_BROWSER_NOT_APPLICABLE",
        "GA_POLICY_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED",
        "GA_POLICY_ACCOUNT_STATUS_NOT_READ",
    } <= fired


def test_every_rule_in_the_table_is_reachable_from_this_suite() -> None:
    """The table is the contract; an unreachable rule is dead prose."""
    covered = set()
    for sections in _all_fixture_sections():
        covered |= _rule_ids(sections)
    assert set(RULES) - covered == set()


# --- structure ----------------------------------------------------------------


def test_structure_rules_fire_for_a_broken_hierarchy() -> None:
    records = (
        _record(
            seed=1, resource_type="ACCOUNT", resource_ref=ACCOUNT, quality=HEALTHY_ACCOUNT_QUALITY
        ),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state={
                "status": "PAUSED",
                "objective": "SEARCH",
                "currency": "USD",
                "budget_minor": 100,
                "budget_type": "DAILY",
                "bid_strategy": "MANUAL_CPC",
            },
            status="PAUSED",
            quality=HEALTHY_CAMPAIGN_QUALITY,
        ),
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=(CAMPAIGN,),
            state={"status": "ENABLED", "currency": "USD", "bid_amount_minor": 10},
            status="ENABLED",
        ),
        _record(
            seed=5,
            resource_type="AD",
            resource_ref="google_ads.ad.99",
            parents=("google_ads.ad_group.404",),
            state={"status": "SOMETHING_ELSE"},
            status="SOMETHING_ELSE",
            quality=HEALTHY_AD_QUALITY,
        ),
    )
    fired = _rule_ids(_sections(evidence=ProviderAuditEvidence(inventory=_inventory(records))))
    assert {
        "GA_STRUCTURE_AD_GROUP_WITHOUT_AD",
        "GA_STRUCTURE_AD_PARENT_NOT_RETAINED",
        "GA_STRUCTURE_SERVING_INTENT_MISMATCH",
        "GA_STRUCTURE_STATUS_NOT_REVIEWED",
    } <= fired


def test_an_unproven_inventory_pagination_is_reported_and_lowers_structure_coverage() -> None:
    records = _healthy_records(
        account_quality=(
            GOOGLE_ADS_PAGINATION_TOTAL_NOT_DECLARED,
            GOOGLE_ADS_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED,
            GOOGLE_ADS_PRODUCTION_ACCOUNT_DECLARED,
        )
    )
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(records)))
    assert "GA_STRUCTURE_INVENTORY_PAGINATION_NOT_PROVEN" in _rule_ids(sections)
    # A read whose provider never declared its total is not COVERED even when
    # the local completion marker exists.
    assert _coverage(sections)["STRUCTURE"] == "PARTIAL"
    rationale = next(item.rationale for item in sections.coverage if item.dimension == "STRUCTURE")
    assert "not proven whole" in rationale


# --- spend, bids, objective, targeting ---------------------------------------


def test_spend_and_targeting_rules_fire_only_for_a_campaign_that_declares_serving_intent() -> None:
    exposed_state = {
        "status": "ENABLED",
        "objective": "DISPLAY",
        "currency": "USD",
        "bid_strategy": "MAXIMIZE_CONVERSIONS",
    }
    quality = (
        GOOGLE_ADS_AD_SCHEDULE_ABSENT,
        GOOGLE_ADS_BUDGET_NOT_RETURNED,
        GOOGLE_ADS_BUDGET_TYPE_NOT_RETURNED,
        GOOGLE_ADS_NETWORK_SETTINGS_NOT_RETURNED,
        GOOGLE_ADS_GEO_TARGET_TYPE_NOT_RETURNED,
        GOOGLE_ADS_NETWORK_CONTENT_ENABLED,
        GOOGLE_ADS_NETWORK_PARTNER_SEARCH_ENABLED,
        GOOGLE_ADS_BUDGET_EXPLICITLY_SHARED,
    )
    serving = _rule_ids(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    _healthy_records(campaign_state=exposed_state, campaign_quality=quality)
                )
            )
        )
    )
    assert {
        "GA_SPEND_BUDGET_NOT_RETURNED",
        "GA_SPEND_BUDGET_PERIOD_NOT_RETURNED",
        "GA_SPEND_BUDGET_EXPLICITLY_SHARED",
        "GA_SPEND_SCHEDULE_END_NOT_RETURNED",
        "GA_BIDS_AUTOMATED_STRATEGY_NOT_LOCALLY_BOUNDED",
        "GA_OBJECTIVE_CHANNEL_OUTSIDE_COMPILABLE_SUBSET",
        "GA_TARGETING_AD_SCHEDULE_ABSENT",
        "GA_TARGETING_CONTENT_NETWORK_ENABLED",
        "GA_TARGETING_PARTNER_SEARCH_ENABLED",
        "GA_TARGETING_NETWORK_SETTINGS_NOT_RETURNED",
        "GA_TARGETING_GEO_TARGET_TYPE_NOT_RETURNED",
    } <= serving

    paused_state = {**exposed_state, "status": "PAUSED"}
    paused = _rule_ids(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    _healthy_records(
                        campaign_status="PAUSED",
                        campaign_state=paused_state,
                        campaign_quality=quality,
                    )
                )
            )
        )
    )
    # Serving-intent guard: a paused campaign is not spending or delivering
    # anywhere, so the exposure rules are silent while the read-fact rules stay.
    assert not paused & {
        "GA_SPEND_BUDGET_NOT_RETURNED",
        "GA_SPEND_SCHEDULE_END_NOT_RETURNED",
        "GA_TARGETING_AD_SCHEDULE_ABSENT",
        "GA_TARGETING_CONTENT_NETWORK_ENABLED",
        "GA_TARGETING_PARTNER_SEARCH_ENABLED",
    }
    assert {
        "GA_SPEND_BUDGET_PERIOD_NOT_RETURNED",
        "GA_TARGETING_NETWORK_SETTINGS_NOT_RETURNED",
        "GA_TARGETING_GEO_TARGET_TYPE_NOT_RETURNED",
    } <= paused


def test_bid_rules_use_only_the_customers_declared_maximum() -> None:
    records = _healthy_records(
        ad_group_state={"status": "ENABLED", "currency": "USD", "bid_amount_minor": 500}
    )
    evidence = ProviderAuditEvidence(inventory=_inventory(records))
    assert "GA_BIDS_CPC_BID_ABOVE_DECLARED_MAXIMUM" not in _rule_ids(_sections(evidence=evidence))
    declared = _sections(
        evidence=evidence,
        policy=_policy(maximum_cpc=Money(minor_units=200, currency="USD")),
    )
    assert "GA_BIDS_CPC_BID_ABOVE_DECLARED_MAXIMUM" in _rule_ids(declared)
    # A bid in another currency is not compared at all.
    other_currency = _sections(
        evidence=evidence,
        policy=_policy(maximum_cpc=Money(minor_units=1, currency="EUR")),
    )
    assert "GA_BIDS_CPC_BID_ABOVE_DECLARED_MAXIMUM" not in _rule_ids(other_currency)


def test_an_ad_group_without_a_retained_bid_reports_that_instead_of_a_threshold() -> None:
    records = _healthy_records(ad_group_state={"status": "ENABLED", "currency": "USD"})
    fired = _rule_ids(
        _sections(
            evidence=ProviderAuditEvidence(inventory=_inventory(records)),
            policy=_policy(maximum_cpc=Money(minor_units=1, currency="USD")),
        )
    )
    assert "GA_BIDS_AD_GROUP_WITHOUT_CPC_BID" in fired
    assert "GA_BIDS_CPC_BID_ABOVE_DECLARED_MAXIMUM" not in fired


# --- creative, destination, policy -------------------------------------------


def test_creative_destination_and_policy_rules_fire_from_presence_facts_only() -> None:
    ad_quality = (
        "CREATIVE_NOT_EXPOSED",
        GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED,
        GOOGLE_ADS_RSA_HEADLINES_PRESENT,
        GOOGLE_ADS_RSA_HEADLINES_BELOW_DOCUMENTED_MINIMUM,
        GOOGLE_ADS_RSA_DESCRIPTIONS_PRESENT,
        GOOGLE_ADS_RSA_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM,
        GOOGLE_ADS_FINAL_URL_ABSENT,
        GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED,
        GOOGLE_ADS_POLICY_APPROVAL_PREFIX + "DISAPPROVED",
    )
    fired = _rule_ids(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records(ad_quality=ad_quality))
            )
        )
    )
    assert {
        "GA_CREATIVE_HEADLINES_BELOW_DOCUMENTED_MINIMUM",
        "GA_CREATIVE_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM",
        "GA_DESTINATION_FINAL_URL_ABSENT",
        "GA_POLICY_APPROVAL_RESTRICTED",
    } <= fired


def test_an_absent_or_unreviewed_policy_status_is_never_an_approval() -> None:
    absent = _rule_ids(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    _healthy_records(
                        ad_quality=(
                            "CREATIVE_NOT_EXPOSED",
                            GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED,
                            GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED,
                            GOOGLE_ADS_FINAL_URL_PRESENT,
                            GOOGLE_ADS_POLICY_APPROVAL_STATUS_NOT_RETURNED,
                            GOOGLE_ADS_POLICY_REVIEW_STATUS_NOT_RETURNED,
                        )
                    )
                )
            )
        )
    )
    assert {
        "GA_POLICY_APPROVAL_STATUS_NOT_RETURNED",
        "GA_CREATIVE_RESPONSIVE_SEARCH_AD_NOT_RETURNED",
    } <= absent
    assert "GA_POLICY_APPROVAL_RESTRICTED" not in absent

    unreviewed = _rule_ids(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    _healthy_records(
                        ad_quality=(
                            GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED,
                            GOOGLE_ADS_POLICY_APPROVAL_PREFIX + "SOMETHING_NEW",
                            GOOGLE_ADS_FINAL_URL_PRESENT,
                            GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED,
                        )
                    )
                )
            )
        )
    )
    assert "GA_POLICY_APPROVAL_STATUS_NOT_REVIEWED" in unreviewed
    assert "GA_POLICY_APPROVAL_RESTRICTED" not in unreviewed


def test_a_declared_test_account_is_reported_as_no_evidence_of_live_performance() -> None:
    fired = _rule_ids(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    _healthy_records(
                        account_quality=(
                            GOOGLE_ADS_PAGINATION_TOTAL_DECLARED,
                            GOOGLE_ADS_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED,
                            GOOGLE_ADS_TEST_ACCOUNT_DECLARED,
                        )
                    )
                )
            )
        )
    )
    assert "GA_POLICY_TEST_ACCOUNT_DECLARED" in fired


# --- delivery and spend from the retained report ------------------------------


def test_delivery_and_spend_report_rules_fire_on_inconsistent_rows() -> None:
    rows = [
        _row(impressions=0, clicks=0, minor_units=125, cost_micros="1250000"),
        _row(impressions=1, clicks=5, day="2026-08-26"),
        _row(impressions=None, clicks=None, day="2026-08-27"),
    ]
    fired = _rule_ids(
        _sections(
            report=_report(rows, quality=["NOT_AVAILABLE_FROM_PROVIDER"]),
            evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())),
        )
    )
    assert {
        "GA_SPEND_WITHOUT_IMPRESSIONS",
        "GA_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
        "GA_DELIVERY_METRICS_MISSING",
        "GA_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
        "GA_SPEND_ROW_COST_ROUNDED_TO_MINOR_UNITS",
    } <= fired


def test_the_rounding_disclosure_is_reported_once_for_a_whole_retained_report() -> None:
    rows = [_row(), _row(day="2026-08-26"), _row(day="2026-08-27")]
    sections = _sections(
        report=_report(rows),
        evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())),
    )
    rounded = [
        item
        for item in sections.findings
        if item.rule_id == "GA_SPEND_ROW_COST_ROUNDED_TO_MINOR_UNITS"
    ]
    assert len(rounded) == 1


def test_an_enabled_campaign_with_no_retained_rows_is_not_reported_as_zero_delivery() -> None:
    sections = _sections(
        report=_report([_row(resource_ref="google_ads.campaign.99")]),
        evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())),
    )
    matched = [
        item
        for item in sections.findings
        if item.rule_id == "GA_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"
    ]
    assert len(matched) == 1
    # The rule states its own limit rather than asserting zero delivery.
    assert "not proof of zero delivery" in matched[0].finding.observation
    assert matched[0].finding.confidence == "LOW"
    assert matched[0].analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"


def test_the_declared_minimum_impression_threshold_is_the_customers_only() -> None:
    report = _report([_row(impressions=5, clicks=1)])
    evidence = ProviderAuditEvidence(inventory=_inventory(_healthy_records()))
    assert "GA_DELIVERY_IMPRESSIONS_BELOW_DECLARED_MINIMUM" not in _rule_ids(
        _sections(report=report, evidence=evidence)
    )
    assert "GA_DELIVERY_IMPRESSIONS_BELOW_DECLARED_MINIMUM" in _rule_ids(
        _sections(report=report, evidence=evidence, policy=_policy(minimum_impressions=100))
    )


# --- drift --------------------------------------------------------------------


def test_drift_reports_a_state_change_with_both_digests_and_an_unknown_actor() -> None:
    earlier = _inventory(
        _healthy_records(campaign_status="PAUSED", observed_at=EARLIER_OBSERVED),
        observed_at=EARLIER_OBSERVED,
    )
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_healthy_records()), earlier_inventory=earlier
        )
    )
    assert "GA_DRIFT_STATE_CHANGED" in _rule_ids(sections)
    observation = next(
        item for item in sections.drift_observations if item.change_kind == "STATE_CHANGED"
    )
    assert observation.observed_actor == "UNKNOWN"
    assert observation.before_record_digest and observation.after_record_digest
    assert observation.observation_interval_start == EARLIER_OBSERVED
    assert _coverage(sections)["DRIFT"] == "COVERED"


def test_absence_drift_needs_a_proven_read_boundary_on_both_sides() -> None:
    """A legacy read with no completion marker fabricates nothing."""
    later = _inventory(_healthy_records())
    earlier_records = (
        *_healthy_records(observed_at=EARLIER_OBSERVED),
        _record(
            seed=7,
            resource_type="CAMPAIGN",
            resource_ref="google_ads.campaign.777",
            parents=(ACCOUNT,),
            state={
                "status": "ENABLED",
                "objective": "SEARCH",
                "currency": "USD",
                "budget_minor": 1,
                "budget_type": "DAILY",
                "bid_strategy": "MANUAL_CPC",
            },
            quality=HEALTHY_CAMPAIGN_QUALITY,
            observed_at=EARLIER_OBSERVED,
        ),
    )
    proven = _sections(
        evidence=ProviderAuditEvidence(
            inventory=later,
            earlier_inventory=_inventory(earlier_records, observed_at=EARLIER_OBSERVED),
        )
    )
    assert "GA_DRIFT_ABSENT_FROM_LATER_INVENTORY" in _rule_ids(proven)

    legacy = _sections(
        evidence=ProviderAuditEvidence(
            inventory=later,
            earlier_inventory=_inventory(
                earlier_records,
                completeness="READ_BOUNDARY_UNVERIFIED",
                observed_at=EARLIER_OBSERVED,
            ),
        )
    )
    assert "GA_DRIFT_ABSENT_FROM_LATER_INVENTORY" not in _rule_ids(legacy)
    rationale = next(item.rationale for item in legacy.coverage if item.dimension == "DRIFT")
    assert "not evaluated at all" in rationale


def test_absence_drift_from_a_declared_subset_is_low_confidence() -> None:
    later = _inventory(_healthy_records())
    earlier_records = (
        *_healthy_records(observed_at=EARLIER_OBSERVED),
        _record(
            seed=7,
            resource_type="AD",
            resource_ref="google_ads.ad.777",
            parents=(CAMPAIGN, AD_GROUP),
            state={"status": "ENABLED"},
            quality=HEALTHY_AD_QUALITY,
            observed_at=EARLIER_OBSERVED,
        ),
    )
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=later,
            earlier_inventory=_inventory(
                earlier_records,
                completeness="BOUNDED_NON_EXHAUSTIVE",
                observed_at=EARLIER_OBSERVED,
            ),
        )
    )
    absent = next(
        item for item in sections.findings if item.rule_id == "GA_DRIFT_ABSENT_FROM_LATER_INVENTORY"
    )
    assert absent.finding.confidence == "LOW"
    assert _coverage(sections)["DRIFT"] == "PARTIAL"


def test_a_resource_only_in_the_later_read_is_reported_as_an_unknown_creation() -> None:
    earlier_records = _healthy_records(observed_at=EARLIER_OBSERVED)[:3]
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_healthy_records()),
            earlier_inventory=_inventory(earlier_records, observed_at=EARLIER_OBSERVED),
        )
    )
    assert "GA_DRIFT_ABSENT_FROM_EARLIER_INVENTORY" in _rule_ids(sections)


def test_a_state_shape_difference_is_typed_separately_from_a_value_change() -> None:
    """A field on only one side is a retention difference, not a Google change.

    The earlier campaign below is what the previous connector retained for the
    same remote campaign: no `budget_type`.  Its status also differs, which is
    a real value change both reads returned.  Both facts are reported, and the
    drift observation names only the value path.
    """
    earlier_state = {
        "status": "PAUSED",
        "objective": "SEARCH",
        "currency": "USD",
        "budget_minor": 5000,
        "bid_strategy": "MANUAL_CPC",
        "schedule_start": "2026-08-01",
        "schedule_end": "2026-09-30",
    }
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_healthy_records()),
            earlier_inventory=_inventory(
                _healthy_records(
                    campaign_status="PAUSED",
                    campaign_state=earlier_state,
                    observed_at=EARLIER_OBSERVED,
                ),
                observed_at=EARLIER_OBSERVED,
            ),
        )
    )
    fired = _rule_ids(sections)
    assert {"GA_DRIFT_LOCAL_STATE_SHAPE_CHANGED", "GA_DRIFT_STATE_CHANGED"} <= fired
    changed = [item for item in sections.drift_observations if item.change_kind == "STATE_CHANGED"]
    # Only the field both reads returned is compared as a value; the field
    # only the later read retained is not in the observation at all.
    campaign = next(item for item in changed if item.resource_ref == CAMPAIGN)
    assert campaign.changed_paths == ("normalized_state.status",)
    assert "normalized_state.budget_type" not in campaign.changed_paths
    shape = [
        item for item in sections.findings if item.rule_id == "GA_DRIFT_LOCAL_STATE_SHAPE_CHANGED"
    ]
    assert [item.finding.resource_refs for item in shape] == [(CAMPAIGN,)]


def test_a_shape_only_difference_produces_no_drift_observation_at_all() -> None:
    earlier_state = {
        "status": "ENABLED",
        "objective": "SEARCH",
        "currency": "USD",
        "budget_minor": 5000,
        "bid_strategy": "MANUAL_CPC",
        "schedule_start": "2026-08-01",
        "schedule_end": "2026-09-30",
    }
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_healthy_records()),
            earlier_inventory=_inventory(
                _healthy_records(campaign_state=earlier_state, observed_at=EARLIER_OBSERVED),
                observed_at=EARLIER_OBSERVED,
            ),
        )
    )
    assert "GA_DRIFT_LOCAL_STATE_SHAPE_CHANGED" in _rule_ids(sections)
    assert "GA_DRIFT_STATE_CHANGED" not in _rule_ids(sections)
    assert sections.drift_observations == ()


def test_a_parent_chain_change_is_still_a_real_out_of_band_change() -> None:
    """Parents are on both sides of every comparison, so they stay value paths."""
    earlier = list(_healthy_records(observed_at=EARLIER_OBSERVED))
    moved = earlier[3]
    earlier[3] = _record(
        seed=44,
        resource_type="AD",
        resource_ref=AD,
        parents=(CAMPAIGN,),
        state=dict(moved.snapshot.normalized_state),
        quality=HEALTHY_AD_QUALITY,
        observed_at=EARLIER_OBSERVED,
    )
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_healthy_records()),
            earlier_inventory=_inventory(tuple(earlier), observed_at=EARLIER_OBSERVED),
        )
    )
    changed = [item for item in sections.drift_observations if item.resource_ref == AD]
    assert changed and changed[0].changed_paths == ("parent_resource_refs",)


def test_the_policy_review_status_is_evaluated_beside_the_approval_status() -> None:
    absent = _rule_ids(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    _healthy_records(
                        ad_quality=(
                            GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED,
                            GOOGLE_ADS_POLICY_APPROVAL_PREFIX + "APPROVED",
                            GOOGLE_ADS_POLICY_REVIEW_STATUS_NOT_RETURNED,
                            GOOGLE_ADS_FINAL_URL_PRESENT,
                            GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED,
                        )
                    )
                )
            )
        )
    )
    assert "GA_POLICY_REVIEW_STATUS_NOT_RETURNED" in absent
    assert "GA_POLICY_REVIEW_STATUS_NOT_REVIEWED" not in absent

    unreviewed = _rule_ids(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    _healthy_records(
                        ad_quality=(
                            GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED,
                            GOOGLE_ADS_POLICY_APPROVAL_PREFIX + "APPROVED",
                            GOOGLE_ADS_POLICY_REVIEW_STATUS_RETURNED,
                            GOOGLE_ADS_POLICY_REVIEW_PREFIX + "NEVER_REVIEWED_HERE",
                            GOOGLE_ADS_FINAL_URL_PRESENT,
                            GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED,
                        )
                    )
                )
            )
        )
    )
    assert "GA_POLICY_REVIEW_STATUS_NOT_REVIEWED" in unreviewed
    assert "GA_POLICY_REVIEW_STATUS_NOT_RETURNED" not in unreviewed


def test_accelerated_budget_delivery_fires_only_for_a_serving_campaign() -> None:
    quality = (
        *HEALTHY_CAMPAIGN_QUALITY,
        GOOGLE_ADS_BUDGET_DELIVERY_METHOD_PREFIX + GOOGLE_ADS_ACCELERATED_BUDGET_DELIVERY,
    )
    serving = _rule_ids(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records(campaign_quality=quality))
            )
        )
    )
    assert "GA_SPEND_BUDGET_ACCELERATED_DELIVERY" in serving
    paused = _rule_ids(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    _healthy_records(campaign_status="PAUSED", campaign_quality=quality)
                )
            )
        )
    )
    assert "GA_SPEND_BUDGET_ACCELERATED_DELIVERY" not in paused


def test_an_inventory_with_no_account_snapshot_keeps_every_read_contract_disclosure() -> None:
    """The unread-surface disclosures describe the read, not one record."""
    records = tuple(
        record for record in _healthy_records() if record.snapshot.resource_type != "ACCOUNT"
    )
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(records)))
    fired = _rule_ids(sections)
    assert {
        "GA_BIDS_STRATEGY_DETAIL_NOT_READ",
        "GA_OBJECTIVE_CONVERSION_GOAL_NOT_READ",
        "GA_TARGETING_CRITERION_VALUES_NOT_READ",
        "GA_PLACEMENT_NOT_READ",
        "GA_CREATIVE_CONTENT_TEXT_NOT_READ",
        "GA_DESTINATION_VALUES_NOT_READ",
        "GA_TRACKING_CONVERSION_ACTION_NOT_READ",
        "GA_TRACKING_TEMPLATE_AND_PARAMETERS_NOT_READ",
        "GA_NATIVE_CONVERSION_NOT_READ",
        "GA_BROWSER_NOT_APPLICABLE",
        "GA_POLICY_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED",
        "GA_POLICY_ACCOUNT_STATUS_NOT_READ",
        # An unrecorded pagination total is not a proven one.
        "GA_STRUCTURE_INVENTORY_PAGINATION_NOT_PROVEN",
    } <= fired
    # The account-level *value* rule stays silent: nothing declared it.
    assert "GA_POLICY_TEST_ACCOUNT_DECLARED" not in fired
    # Those findings are anchored to the account the audited report names and
    # cited to that report's own record digest, not to a snapshot never read.
    placement = next(item for item in sections.findings if item.rule_id == "GA_PLACEMENT_NOT_READ")
    assert placement.finding.resource_refs == (ACCOUNT,)
    assert placement.finding.evidence_refs == (REPORT_DIGEST,)
    structure = next(item for item in sections.coverage if item.dimension == "STRUCTURE")
    assert structure.status == "PARTIAL"
    assert "not proven whole" in structure.rationale
    assert "no account snapshot" in structure.rationale


# --- local creative bindings --------------------------------------------------


def test_a_local_binding_absence_needs_a_proven_read_boundary() -> None:
    catalog = RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative.v1",
                record_digest=_digest(50),
                provider="google_ads",
                provider_resource_ref="google_ads.ad.404",
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
        )
    )
    # No retained inventory at all: nothing was read, so nothing is absent.
    assert "GA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" not in _rule_ids(
        _sections(evidence=ProviderAuditEvidence(creative_catalog=catalog))
    )
    # An unproven boundary suppresses the claim too.
    assert "GA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" not in _rule_ids(
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records(), completeness="READ_BOUNDARY_UNVERIFIED"),
                creative_catalog=catalog,
            )
        )
    )
    proven = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_healthy_records()), creative_catalog=catalog
        )
    )
    assert "GA_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" in _rule_ids(proven)


def test_a_matched_local_binding_is_reported_as_a_declaration_only() -> None:
    catalog = RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative.v1",
                record_digest=_digest(51),
                provider="google_ads",
                provider_resource_ref=AD,
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
        )
    )
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_healthy_records()), creative_catalog=catalog
        )
    )
    assert "GA_CREATIVE_LOCAL_BINDING_UNVERIFIED" in _rule_ids(sections)
    creative = next(item for item in sections.coverage if item.dimension == "CREATIVE")
    assert "LOCAL_CREATIVE_CATALOG" in creative.evidence_sources


# --- coverage -----------------------------------------------------------------


def test_every_dimension_has_exactly_one_status_and_no_evidence_yields_not_read() -> None:
    sections = _sections(report=_report([]))
    statuses = _coverage(sections)
    assert len(statuses) == 14
    assert statuses["BROWSER_MEASUREMENT"] == "NOT_APPLICABLE"
    assert set(statuses.values()) <= {"NOT_READ", "NOT_APPLICABLE"}


def test_coverage_statuses_are_exact_for_a_complete_read_with_a_report() -> None:
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_healthy_records()),
            earlier_inventory=_inventory(
                _healthy_records(observed_at=EARLIER_OBSERVED), observed_at=EARLIER_OBSERVED
            ),
        )
    )
    assert _coverage(sections) == {
        "STRUCTURE": "COVERED",
        "DELIVERY": "PARTIAL",
        "SPEND": "PARTIAL",
        "BIDS": "PARTIAL",
        "OBJECTIVE": "PARTIAL",
        "TARGETING": "PARTIAL",
        "PLACEMENT": "NOT_READ",
        "CREATIVE": "PARTIAL",
        "DESTINATION_CONFIGURATION": "PARTIAL",
        "TRACKING": "NOT_READ",
        "NATIVE_CONVERSION": "NOT_READ",
        "BROWSER_MEASUREMENT": "NOT_APPLICABLE",
        "POLICY": "PARTIAL",
        "DRIFT": "COVERED",
    }


def test_an_unread_dimension_stays_unread_however_much_evidence_a_caller_supplies() -> None:
    """The read-surface table is the ceiling, not the supplied records."""
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())))
    statuses = _coverage(sections)
    for dimension in ("PLACEMENT", "TRACKING", "NATIVE_CONVERSION"):
        assert statuses[dimension] == "NOT_READ"
    for entry in sections.coverage:
        if entry.status in {"NOT_READ", "NOT_APPLICABLE"}:
            assert entry.evidence_sources == () and entry.evidence_record_count == 0


# --- recommendations ----------------------------------------------------------


def test_the_pause_recommendation_rests_only_on_campaign_scoped_serving_findings() -> None:
    records = _healthy_records(
        campaign_state={
            "status": "ENABLED",
            "objective": "SEARCH",
            "currency": "USD",
            "bid_strategy": "MANUAL_CPC",
            "schedule_end": "2026-12-31",
        },
        campaign_quality=(
            GOOGLE_ADS_AD_SCHEDULE_PRESENT,
            GOOGLE_ADS_BUDGET_NOT_RETURNED,
            GOOGLE_ADS_BUDGET_TYPE_RETURNED,
            GOOGLE_ADS_NETWORK_SETTINGS_RETURNED,
            GOOGLE_ADS_GEO_TARGET_TYPE_RETURNED,
            GOOGLE_ADS_BUDGET_PERIOD_PREFIX + "DAILY",
            GOOGLE_ADS_BUDGET_NOT_EXPLICITLY_SHARED,
        ),
    )
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(records)))
    pause = [item for item in sections.recommendations if item.operation_kind == "PAUSE"]
    assert len(pause) == 1
    assert set(pause[0].bound_rule_ids) == {
        "GA_SPEND_BUDGET_NOT_RETURNED",
        "GA_TARGETING_CONTENT_NETWORK_ENABLED",
    }
    assert "plan google-ads-campaign-pause create" in pause[0].recommendation.hypothesis
    assert pause[0].execution_status == "NOT_EXECUTED"
    assert pause[0].plan_status == "NOT_A_PLAN"
    assert pause[0].authorization_status == "NO_AUTHORIZATION_DIGEST_PRESENT"
    assert pause[0].recommendation.proposed_plan_ref is None
    # Every finding the row rests on names a campaign resource this route can act on.
    bound = {
        item.finding.finding_id: item
        for item in sections.findings
        if item.rule_id in set(pause[0].bound_rule_ids)
    }
    for finding_id in pause[0].finding_ids:
        assert bound[finding_id].finding.resource_refs == (CAMPAIGN,)


def test_no_pause_is_proposed_for_a_paused_campaign() -> None:
    records = _healthy_records(
        campaign_status="PAUSED",
        campaign_state={
            "status": "PAUSED",
            "objective": "SEARCH",
            "currency": "USD",
            "bid_strategy": "MANUAL_CPC",
        },
        campaign_quality=(
            GOOGLE_ADS_AD_SCHEDULE_PRESENT,
            GOOGLE_ADS_BUDGET_NOT_RETURNED,
            GOOGLE_ADS_BUDGET_TYPE_RETURNED,
            GOOGLE_ADS_NETWORK_SETTINGS_RETURNED,
            GOOGLE_ADS_GEO_TARGET_TYPE_RETURNED,
            GOOGLE_ADS_NETWORK_CONTENT_ENABLED,
            GOOGLE_ADS_BUDGET_PERIOD_PREFIX + "DAILY",
            GOOGLE_ADS_BUDGET_NOT_EXPLICITLY_SHARED,
        ),
    )
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(records)))
    assert [item for item in sections.recommendations if item.operation_kind == "PAUSE"] == []


def test_create_and_update_are_declined_as_explicitly_considered() -> None:
    records = (
        _record(
            seed=1, resource_type="ACCOUNT", resource_ref=ACCOUNT, quality=HEALTHY_ACCOUNT_QUALITY
        ),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state={
                "status": "ENABLED",
                "objective": "PERFORMANCE_MAX",
                "currency": "USD",
                "budget_minor": 10,
                "budget_type": "DAILY",
                "bid_strategy": "MANUAL_CPC",
                "schedule_end": "2026-12-31",
            },
            quality=HEALTHY_CAMPAIGN_QUALITY,
        ),
    )
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(records)))
    declined = {
        (item.reason, item.considered_operation_kind) for item in sections.no_recommendations
    }
    assert ("OPERATION_NOT_COMPILABLE_FOR_PROVIDER", "CREATE") in declined
    assert ("COVERAGE_NOT_READ", None) in declined


def test_every_bound_exceeded_row_has_a_distinct_subject() -> None:
    """Two rows sharing a subject would make one of them unresolvable."""
    campaigns: list[RetainedSnapshotRecord] = [
        _record(
            seed=1, resource_type="ACCOUNT", resource_ref=ACCOUNT, quality=HEALTHY_ACCOUNT_QUALITY
        )
    ]
    for index in range(40):
        campaigns.append(
            _record(
                seed=100 + index,
                resource_type="CAMPAIGN",
                resource_ref=f"google_ads.campaign.{index}",
                parents=(ACCOUNT,),
                state={
                    "status": "ENABLED",
                    "objective": "DISPLAY",
                    "currency": "USD",
                    "bid_strategy": "MAXIMIZE_CONVERSIONS",
                },
                quality=(
                    GOOGLE_ADS_AD_SCHEDULE_ABSENT,
                    GOOGLE_ADS_BUDGET_NOT_RETURNED,
                    GOOGLE_ADS_BUDGET_TYPE_NOT_RETURNED,
                    GOOGLE_ADS_NETWORK_SETTINGS_RETURNED,
                    GOOGLE_ADS_GEO_TARGET_TYPE_NOT_RETURNED,
                    GOOGLE_ADS_NETWORK_CONTENT_ENABLED,
                ),
            )
        )
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(tuple(campaigns))))
    subjects = [item.subject for item in sections.bound_exceeded]
    assert subjects
    assert len(set(subjects)) == len(subjects)
    for row in sections.bound_exceeded:
        assert row.inlined_finding_count == MAXIMUM_INLINE_FINDING_IDS
        assert row.inlined_finding_count + row.omitted_finding_count == row.total_finding_count
        assert row.resolution == "PAGE_FINDINGS_AND_MATCH_RULE_ID"
    for item in sections.recommendations:
        assert len(item.finding_ids) <= MAXIMUM_INLINE_FINDING_IDS
        assert item.bound_finding_count >= len(item.finding_ids)


def _many_campaign_records(
    count: int, *, status: str, observed_at: datetime
) -> tuple[RetainedSnapshotRecord, ...]:
    records: list[RetainedSnapshotRecord] = [
        _record(
            seed=1,
            resource_type="ACCOUNT",
            resource_ref=ACCOUNT,
            status="NOT_AVAILABLE_FROM_PROVIDER",
            quality=HEALTHY_ACCOUNT_QUALITY,
            observed_at=observed_at,
        )
    ]
    for index in range(count):
        records.append(
            _record(
                seed=10_000 + index,
                resource_type="CAMPAIGN",
                resource_ref=f"google_ads.campaign.{index}",
                parents=(ACCOUNT,),
                state={
                    "status": status,
                    "objective": "SEARCH",
                    "currency": "USD",
                    "budget_minor": 100,
                    "budget_type": "DAILY",
                    "bid_strategy": "MANUAL_CPC",
                    "schedule_end": "2026-12-31",
                },
                status=status,
                quality=HEALTHY_CAMPAIGN_QUALITY,
                observed_at=observed_at,
            )
        )
    return tuple(records)


def test_the_smallest_provider_audit_page_fits_the_default_output_bound() -> None:
    """A large Google Ads account must be servable one row at a time.

    The route pages `findings`, `recommendations`, `no_recommendations`,
    `drift_observations`, and the two embedded audit lists.  A recommendation
    row is bounded to `MAXIMUM_INLINE_FINDING_IDS` inline identifiers, so even
    with 600 out-of-band campaign changes the smallest page the route can serve
    stays inside the default saved output bound.
    """
    from adhelm.audit import audit_provider
    from adhelm.measurement_cli import _fits
    from adhelm.models import OutputPreferences

    count = 600
    earlier = _inventory(
        _many_campaign_records(count, status="PAUSED", observed_at=EARLIER_OBSERVED),
        observed_at=EARLIER_OBSERVED,
    )
    later = _inventory(_many_campaign_records(count, status="ENABLED", observed_at=OBSERVED))
    result = audit_provider(
        _report(),
        source_record_digest=REPORT_DIGEST,
        policy=_policy(),
        as_of=AS_OF,
        evidence=ProviderAuditEvidence(inventory=later, earlier_inventory=earlier),
    )
    assert result.sections is not None
    assert len(result.sections.findings) >= count
    data = result.model_dump(mode="json")
    for key in ("findings", "recommendations", "no_recommendations", "drift_observations"):
        data["sections"][key] = data["sections"][key][:1]
    for key in ("findings", "recommendations"):
        data["audit"][key] = data["audit"][key][:1]
    assert _fits(
        "audit --provider-rules",
        data,
        OutputPreferences().max_output_bytes,
        outcome="INCOMPLETE_COVERAGE",
        total_count=count,
        next_cursor="1",
    ), "a one-row Google Ads provider audit page must fit the default saved output bound"


# --- output invariants --------------------------------------------------------


def test_the_result_is_deterministic_and_carries_no_digest_like_prose() -> None:
    evidence = ProviderAuditEvidence(
        inventory=_inventory(_healthy_records()),
        earlier_inventory=_inventory(
            _healthy_records(campaign_status="PAUSED", observed_at=EARLIER_OBSERVED),
            observed_at=EARLIER_OBSERVED,
        ),
    )
    first = _sections(evidence=evidence)
    second = _sections(evidence=evidence)
    assert canonical_bytes(first.model_dump(mode="json")) == canonical_bytes(
        second.model_dump(mode="json")
    )
    assert first.rule_table_version == RULE_TABLE_VERSION
    assert first.live_provider_reads == "NONE_RETAINED_RECORDS_ONLY"
    assert first.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"


def test_the_result_passes_the_privacy_validator_and_names_no_customer_content() -> None:
    sections = _sections(
        evidence=ProviderAuditEvidence(
            inventory=_inventory(_healthy_records()),
            creative_catalog=RetainedCreativeCatalog(
                entries=(
                    RetainedCreativeCatalogEntry(
                        creative_version_id="creative.v1",
                        record_digest=_digest(52),
                        provider="google_ads",
                        provider_resource_ref=AD,
                        binding_status="LOCAL_DECLARED_UNVERIFIED",
                    ),
                )
            ),
        )
    )
    validate_data(sections.model_dump(mode="json"))


def test_no_section_may_claim_an_exhaustive_retained_report() -> None:
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())))
    with pytest.raises(ValidationError):
        ProviderAuditSections(
            provider="google_ads",
            rule_table_version=RULE_TABLE_VERSION,
            analytics_completeness="COMPLETE",  # type: ignore[arg-type]
            coverage=sections.coverage,
        )


def test_audit_provider_dispatches_to_this_table_and_embeds_the_neutral_audit() -> None:
    result = audit_provider(
        _report(),
        source_record_digest=REPORT_DIGEST,
        policy=_policy(),
        as_of=AS_OF,
        evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())),
    )
    assert result.rules_status == "EVALUATED"
    assert result.sections is not None
    assert result.sections.provider == "google_ads"


def _all_fixture_sections() -> list[ProviderAuditSections]:
    """Every fixture shape this suite exercises, for the reachability check."""
    catalog = RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative.v1",
                record_digest=_digest(53),
                provider="google_ads",
                provider_resource_ref="google_ads.ad.404",
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
            RetainedCreativeCatalogEntry(
                creative_version_id="creative.v2",
                record_digest=_digest(54),
                provider="google_ads",
                provider_resource_ref=AD,
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
        )
    )
    broken = (
        _record(
            seed=1,
            resource_type="ACCOUNT",
            resource_ref=ACCOUNT,
            quality=(
                GOOGLE_ADS_PAGINATION_TOTAL_NOT_DECLARED,
                GOOGLE_ADS_DEVELOPER_TOKEN_ACCESS_LEVEL_NOT_DISCLOSED,
                GOOGLE_ADS_TEST_ACCOUNT_DECLARED,
            ),
        ),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state={
                "status": "PAUSED",
                "objective": "DISPLAY",
                "currency": "USD",
                "bid_strategy": "MAXIMIZE_CONVERSIONS",
            },
            status="PAUSED",
            quality=(
                GOOGLE_ADS_AD_SCHEDULE_ABSENT,
                GOOGLE_ADS_BUDGET_NOT_RETURNED,
                GOOGLE_ADS_BUDGET_TYPE_NOT_RETURNED,
                GOOGLE_ADS_NETWORK_SETTINGS_NOT_RETURNED,
                GOOGLE_ADS_GEO_TARGET_TYPE_NOT_RETURNED,
            ),
        ),
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=(CAMPAIGN,),
            state={"status": "ENABLED", "currency": "USD"},
            status="ENABLED",
        ),
        _record(
            seed=5,
            resource_type="AD",
            resource_ref="google_ads.ad.99",
            parents=("google_ads.ad_group.404",),
            state={"status": "SOMETHING_ELSE"},
            status="SOMETHING_ELSE",
            quality=(
                GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED,
                GOOGLE_ADS_FINAL_URL_ABSENT,
                GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED,
                GOOGLE_ADS_POLICY_APPROVAL_PREFIX + "SOMETHING_NEW",
                GOOGLE_ADS_POLICY_REVIEW_STATUS_RETURNED,
                GOOGLE_ADS_POLICY_REVIEW_PREFIX + "SOMETHING_NEW",
            ),
        ),
    )
    exposed = _healthy_records(
        campaign_state={
            "status": "ENABLED",
            "objective": "SEARCH",
            "currency": "USD",
            "bid_strategy": "MANUAL_CPC",
        },
        campaign_quality=(
            GOOGLE_ADS_AD_SCHEDULE_ABSENT,
            GOOGLE_ADS_BUDGET_NOT_RETURNED,
            GOOGLE_ADS_BUDGET_TYPE_RETURNED,
            GOOGLE_ADS_NETWORK_SETTINGS_RETURNED,
            GOOGLE_ADS_GEO_TARGET_TYPE_RETURNED,
            GOOGLE_ADS_NETWORK_CONTENT_ENABLED,
            GOOGLE_ADS_NETWORK_PARTNER_SEARCH_ENABLED,
            GOOGLE_ADS_BUDGET_EXPLICITLY_SHARED,
            GOOGLE_ADS_BUDGET_PERIOD_PREFIX + "DAILY",
            GOOGLE_ADS_BUDGET_DELIVERY_METHOD_PREFIX + GOOGLE_ADS_ACCELERATED_BUDGET_DELIVERY,
        ),
        ad_quality=(
            "CREATIVE_NOT_EXPOSED",
            GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED,
            GOOGLE_ADS_RSA_HEADLINES_PRESENT,
            GOOGLE_ADS_RSA_HEADLINES_BELOW_DOCUMENTED_MINIMUM,
            GOOGLE_ADS_RSA_DESCRIPTIONS_PRESENT,
            GOOGLE_ADS_RSA_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM,
            GOOGLE_ADS_FINAL_URL_ABSENT,
            GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED,
            GOOGLE_ADS_POLICY_APPROVAL_PREFIX + "DISAPPROVED",
        ),
    )
    earlier_records = (
        *_healthy_records(observed_at=EARLIER_OBSERVED),
        _record(
            seed=7,
            resource_type="AD",
            resource_ref="google_ads.ad.777",
            parents=(CAMPAIGN, AD_GROUP),
            state={"status": "ENABLED"},
            quality=HEALTHY_AD_QUALITY,
            observed_at=EARLIER_OBSERVED,
        ),
    )
    return [
        _sections(evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records()))),
        _sections(evidence=ProviderAuditEvidence(inventory=_inventory(broken))),
        _sections(
            evidence=ProviderAuditEvidence(inventory=_inventory(exposed), creative_catalog=catalog),
            policy=_policy(
                maximum_cpc=Money(minor_units=1, currency="USD"), minimum_impressions=1_000_000
            ),
        ),
        _sections(
            report=_report(
                [
                    _row(impressions=0, clicks=0),
                    _row(impressions=1, clicks=5, day="2026-08-26"),
                    _row(impressions=None, clicks=None, day="2026-08-27"),
                    _row(resource_ref="google_ads.campaign.404"),
                ],
                quality=["NOT_AVAILABLE_FROM_PROVIDER"],
            ),
            evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())),
        ),
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records()),
                earlier_inventory=_inventory(earlier_records, observed_at=EARLIER_OBSERVED),
            )
        ),
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records()[:3]),
                earlier_inventory=_inventory(
                    _healthy_records(observed_at=EARLIER_OBSERVED), observed_at=EARLIER_OBSERVED
                ),
            )
        ),
        # A snapshot pair whose earlier campaign state omits one field the
        # later connector retains.
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records()),
                earlier_inventory=_inventory(
                    _healthy_records(
                        campaign_state={
                            "status": "ENABLED",
                            "objective": "SEARCH",
                            "currency": "USD",
                            "budget_minor": 5000,
                            "bid_strategy": "MANUAL_CPC",
                            "schedule_start": "2026-08-01",
                            "schedule_end": "2026-09-30",
                        },
                        observed_at=EARLIER_OBSERVED,
                    ),
                    observed_at=EARLIER_OBSERVED,
                ),
            )
        ),
        # A resource present only in the later read, and a per-resource state
        # change, over two proven read boundaries.
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records()),
                earlier_inventory=_inventory(
                    _healthy_records(campaign_status="PAUSED", observed_at=EARLIER_OBSERVED)[:3],
                    observed_at=EARLIER_OBSERVED,
                ),
            )
        ),
        # A campaign with no ad group at all, and an ad group whose parent
        # campaign the same read never returned.
        _sections(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    (
                        _record(
                            seed=1,
                            resource_type="ACCOUNT",
                            resource_ref=ACCOUNT,
                            quality=HEALTHY_ACCOUNT_QUALITY,
                        ),
                        _record(
                            seed=2,
                            resource_type="CAMPAIGN",
                            resource_ref=CAMPAIGN,
                            parents=(ACCOUNT,),
                            state={
                                "status": "PAUSED",
                                "objective": "SEARCH",
                                "currency": "USD",
                                "budget_minor": 10,
                                "budget_type": "DAILY",
                                "bid_strategy": "MANUAL_CPC",
                            },
                            status="PAUSED",
                            quality=HEALTHY_CAMPAIGN_QUALITY,
                        ),
                        _record(
                            seed=8,
                            resource_type="AD_GROUP",
                            resource_ref="google_ads.ad_group.888",
                            parents=("google_ads.campaign.404",),
                            state={"status": "PAUSED", "currency": "USD", "bid_amount_minor": 5},
                            status="PAUSED",
                        ),
                    )
                )
            )
        ),
        # An ENABLED campaign the retained report returned no row for, beside
        # an ad whose policy summary was not returned at all.
        _sections(
            report=_report([_row(resource_ref="google_ads.campaign.404")]),
            evidence=ProviderAuditEvidence(
                inventory=_inventory(
                    _healthy_records(
                        ad_quality=(
                            "CREATIVE_NOT_EXPOSED",
                            GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED,
                            GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED,
                            GOOGLE_ADS_FINAL_URL_PRESENT,
                            GOOGLE_ADS_POLICY_APPROVAL_STATUS_NOT_RETURNED,
                            GOOGLE_ADS_POLICY_REVIEW_STATUS_NOT_RETURNED,
                        )
                    )
                )
            ),
        ),
    ]


def test_a_google_local_state_shape_finding_names_both_retained_shape_versions() -> None:
    """The suggested action carries the evidence that resolves the difference.

    Telling a reader to "compare the two snapshots' retained connector
    state-shape versions" without naming them is not an actionable
    instruction.  The finding reads them and says which one each side
    declared, and which reading that supports.
    """
    from adhelm.audits.google_ads import _state_shape_action, _state_shape_version
    from adhelm.providers.google_ads import (
        GOOGLE_ADS_STATE_SHAPE_STATUS,
        GOOGLE_ADS_STATE_SHAPE_VERSION,
    )

    def _record(quality: tuple[str, ...]) -> RetainedSnapshotRecord:
        state = {"status": "ENABLED"}
        return RetainedSnapshotRecord(
            record_digest="d" * 64,
            observed_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            snapshot=ResourceSnapshotState(
                provider_api_version="v21",
                resource_type="CAMPAIGN",
                resource_ref="google_ads.campaign.1",
                normalized_state=state,
                normalized_state_digest=digest(state),
                effective_status="ENABLED",
                data_quality_statuses=quality,
            ),
        )

    current = _record((GOOGLE_ADS_STATE_SHAPE_STATUS,))
    assert _state_shape_version(current) == GOOGLE_ADS_STATE_SHAPE_VERSION
    older = _record(("GOOGLE_ADS_STATE_SHAPE_2026-01-01.1",))
    changed = _state_shape_action(older, current)
    assert "earlier 2026-01-01.1" in changed
    assert f"later {GOOGLE_ADS_STATE_SHAPE_VERSION}" in changed
    assert "this product changed" in changed

    unrecorded = _record(())
    assert _state_shape_version(unrecorded) is None
    same = _state_shape_action(unrecorded, unrecorded)
    assert "earlier NOT_RECORDED, later NOT_RECORDED" in same
    assert "a value became absent" in same
