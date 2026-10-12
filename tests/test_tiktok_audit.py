"""The deterministic TikTok Ads rule table over retained local records.

Every case is offline: retained snapshot and report models only.  No provider
request, no credential, no ledger, no network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from adhelm.audit import AuditDimension, AuditPolicy
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
from adhelm.audits.tiktok_ads import (
    RULE_OUTCOME_GROUPS,
    RULE_TABLE_VERSION,
    RULES,
    evaluate,
)
from adhelm.canonical import canonical_bytes, digest
from adhelm.models import Money
from adhelm.privacy import validate_data
from adhelm.providers.tiktok_ads import (
    TIKTOK_ADS_PAGINATION_TOTAL_DECLARED,
    TIKTOK_ADS_READ_SURFACES,
    TIKTOK_ADS_STATE_SHAPE_STATUS,
)
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope

ACCOUNT = "tiktok_ads.advertiser.123"
CAMPAIGN = "tiktok_ads.campaign.11"
AD_GROUP = "tiktok_ads.adgroup.12"
AD = "tiktok_ads.ad.13"
OBSERVED = datetime(2026, 9, 1, 12, tzinfo=UTC)
EARLIER = datetime(2026, 8, 25, 12, tzinfo=UTC)


def _record(
    *,
    seed: int,
    resource_type: str,
    resource_ref: str,
    parents: tuple[str, ...] = (),
    state: dict[str, Any] | None = None,
    quality: tuple[str, ...] = (),
    status: str | None = None,
    observed_at: datetime = OBSERVED,
) -> RetainedSnapshotRecord:
    payload = dict(state or {"status": "ENABLE"})
    state_digest = digest(payload)
    return RetainedSnapshotRecord(
        record_digest=digest({"seed": seed, "resource": resource_ref}),
        observed_at=observed_at,
        snapshot=ResourceSnapshotState(
            provider_api_version="v1.3",
            resource_type=resource_type,  # type: ignore[arg-type]
            resource_ref=resource_ref,
            parent_resource_refs=parents,
            normalized_state=payload,
            normalized_state_digest=state_digest,
            effective_status=status or str(payload.get("status", "ENABLE")),
            data_quality_statuses=quality,
        ),
    )


def _account(quality: tuple[str, ...] = (), seed: int = 1) -> RetainedSnapshotRecord:
    return _record(
        seed=seed,
        resource_type="ACCOUNT",
        resource_ref=ACCOUNT,
        state={"status": "STATUS_ENABLE", "currency": "USD"},
        quality=(
            TIKTOK_ADS_STATE_SHAPE_STATUS,
            TIKTOK_ADS_PAGINATION_TOTAL_DECLARED,
            *quality,
        ),
    )


def _healthy_records() -> tuple[RetainedSnapshotRecord, ...]:
    return (
        _account(),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state={
                "status": "ENABLE",
                "currency": "USD",
                "objective": "TRAFFIC",
                "tiktok_ads_budget_mode": "BUDGET_MODE_DAY",
                "budget_minor": 5_000,
                "budget_type": "DAILY",
            },
            quality=(
                TIKTOK_ADS_STATE_SHAPE_STATUS,
                "TIKTOK_OBJECTIVE_RETURNED",
                "TIKTOK_BUDGET_MODE_DAY",
                "TIKTOK_BUDGET_AMOUNT_PRESENT",
                "TIKTOK_OPERATION_STATUS_REVIEWED",
            ),
        ),
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=(CAMPAIGN,),
            state={
                "status": "ENABLE",
                "currency": "USD",
                "bid_strategy": "BID_TYPE_NO_BID",
                "tiktok_ads_budget_mode": "BUDGET_MODE_TOTAL",
                "budget_minor": 20_000,
                "budget_type": "LIFETIME",
            },
            quality=(
                TIKTOK_ADS_STATE_SHAPE_STATUS,
                "TIKTOK_BUDGET_MODE_TOTAL",
                "TIKTOK_BUDGET_AMOUNT_PRESENT",
                "TIKTOK_BID_TYPE_RETURNED",
                "TIKTOK_BID_PRICE_ABSENT",
                "TIKTOK_OPERATION_STATUS_REVIEWED",
            ),
        ),
        _record(
            seed=4,
            resource_type="AD",
            resource_ref=AD,
            parents=(CAMPAIGN, AD_GROUP),
            state={"status": "ENABLE", "currency": "USD"},
            quality=(
                "CREATIVE_BINDING_NOT_AVAILABLE_FROM_PROVIDER",
                TIKTOK_ADS_STATE_SHAPE_STATUS,
                "TIKTOK_OPERATION_STATUS_REVIEWED",
            ),
        ),
    )


def _inventory(
    records: tuple[RetainedSnapshotRecord, ...],
    *,
    completeness: str = "COMPLETE_BOUNDED_READ",
    observed_at: datetime = OBSERVED,
) -> RetainedInventory:
    return RetainedInventory(
        provider="tiktok_ads",
        account_ref=ACCOUNT,
        observed_at=observed_at,
        completeness=completeness,  # type: ignore[arg-type]
        records=records,
    )


def _report(
    *,
    impressions: int | None = 100,
    clicks: int | None = 4,
    spend_minor: int = 125,
    grain: str = "CAMPAIGN",
    statuses: list[str] | None = None,
    rows: bool = True,
) -> NormalizedReport:
    envelope = ReportEnvelope(
        report_id="report:tiktok",
        provider="tiktok_ads",
        source_account_ref=ACCOUNT,
        query_contract_hash="c" * 64,
        window_start=date(2026, 8, 25),
        window_end=date(2026, 8, 31),
        source_timezone="UTC",
        report_timezone="UTC",
        currency="USD",
        attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
        attribution_window=None,
        observed_at=OBSERVED,
        data_fresh_through=None,
        data_quality_statuses=(
            ["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"] if statuses is None else statuses
        ),
        provider_api_version="v1.3",
        connector_version="test",
    )
    if not rows:
        return NormalizedReport(envelope=envelope, performance_rows=[])
    return NormalizedReport(
        envelope=envelope,
        performance_rows=[
            PerformanceRow(
                report_id="report:tiktok",
                resource_level=grain,  # type: ignore[arg-type]
                metric_grain=grain,  # type: ignore[arg-type]
                resource_type=f"TIKTOK_ADS_{grain}",
                resource_ref=CAMPAIGN if grain == "CAMPAIGN" else AD,
                campaign_ref=CAMPAIGN if grain == "CAMPAIGN" else None,
                dimension_type="DATE",
                dimension_value="2026-08-25",
                impressions=impressions,
                clicks=clicks,
                spend=Money(minor_units=spend_minor, currency="USD"),
                data_quality_statuses=["COMPLETE"],
            )
        ],
    )


def _context(
    *,
    evidence: ProviderAuditEvidence | None = None,
    report: NormalizedReport | None = None,
) -> ProviderAuditContext:
    return ProviderAuditContext(
        provider="tiktok_ads",
        report=report if report is not None else _report(),
        source_record_digest="d" * 64,
        policy=AuditPolicy(
            maximum_observation_age_seconds=604_800, require_conversion_evidence=False
        ),
        as_of=datetime(2026, 9, 2, 12, tzinfo=UTC),
        evidence=evidence if evidence is not None else ProviderAuditEvidence(),
    )


def _fired(sections: ProviderAuditSections) -> set[str]:
    return {finding.rule_id for finding in sections.findings}


def _healthy_sections(**overrides: Any) -> ProviderAuditSections:
    records = list(_healthy_records())
    if "campaign_state" in overrides or "campaign_quality" in overrides:
        records[1] = _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state=overrides.get("campaign_state", {"status": "ENABLE"}),
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS, *overrides.get("campaign_quality", ())),
        )
    if "ad_group_state" in overrides or "ad_group_quality" in overrides:
        records[2] = _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=(CAMPAIGN,),
            state=overrides.get("ad_group_state", {"status": "ENABLE"}),
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS, *overrides.get("ad_group_quality", ())),
        )
    return evaluate(
        _context(
            evidence=ProviderAuditEvidence(inventory=_inventory(tuple(records))),
            report=overrides.get("report"),
        )
    )


# --------------------------------------------------------- table invariants


def test_the_registry_reaches_the_tiktok_rule_module() -> None:
    assert provider_rule("tiktok_ads") is evaluate


def test_every_rule_belongs_to_exactly_one_outcome_group() -> None:
    covered: set[str] = set()
    for group in RULE_OUTCOME_GROUPS:
        assert not covered & group, covered & group
        covered |= set(group)
    assert covered == set(RULES)


def test_every_rule_id_matches_its_key_and_names_a_real_dimension() -> None:
    dimensions = set(AuditDimension.__args__)  # type: ignore[attr-defined]
    for rule_id, rule in RULES.items():
        assert rule.rule_id == rule_id
        assert rule.dimension in dimensions


def test_the_tiktok_table_recommends_no_mutation_at_all() -> None:
    # There is no TikTok pause, status-update, or any other route in this build
    # that changes an existing TikTok resource, so `OBSERVE` is the only kind the
    # table may ever emit even though `CREATE` is compilable.
    assert COMPILABLE_OPERATION_KINDS["tiktok_ads"] == frozenset({"CREATE"})
    sections = _healthy_sections(
        campaign_state={"status": "ENABLE"},
        campaign_quality=("TIKTOK_BUDGET_MODE_INFINITE",),
        report=_report(impressions=0, clicks=0, spend_minor=50),
    )
    assert sections.recommendations
    assert {item.operation_kind for item in sections.recommendations} == {"OBSERVE"}


def test_a_serving_contradiction_yields_an_explicit_declined_pause_not_a_pause() -> None:
    records = (
        _account(),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state={"status": "DISABLE", "budget_minor": 5_000},
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS, "TIKTOK_OPERATION_STATUS_REVIEWED"),
        ),
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=(CAMPAIGN,),
            state={"status": "ENABLE", "budget_minor": 1_000},
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS, "TIKTOK_OPERATION_STATUS_REVIEWED"),
        ),
    )
    sections = evaluate(_context(evidence=ProviderAuditEvidence(inventory=_inventory(records))))

    assert "TT_STRUCTURE_SERVING_INTENT_CONTRADICTS_PARENT" in _fired(sections)
    declined = [
        item for item in sections.no_recommendations if item.considered_operation_kind == "PAUSE"
    ]
    assert len(declined) == 1
    assert declined[0].reason == "OPERATION_NOT_COMPILABLE_FOR_PROVIDER"
    assert "no TikTok pause" in declined[0].detail
    assert "PAUSE" not in {item.operation_kind for item in sections.recommendations}


def test_a_paused_parent_does_not_contradict_a_paused_child() -> None:
    records = (
        _account(),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state={"status": "DISABLE", "budget_minor": 5_000},
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS, "TIKTOK_OPERATION_STATUS_REVIEWED"),
        ),
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=(CAMPAIGN,),
            state={"status": "DISABLE", "budget_minor": 1_000},
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS, "TIKTOK_OPERATION_STATUS_REVIEWED"),
        ),
    )
    sections = evaluate(_context(evidence=ProviderAuditEvidence(inventory=_inventory(records))))
    assert "TT_STRUCTURE_SERVING_INTENT_CONTRADICTS_PARENT" not in _fired(sections)


# ------------------------------------------------------- rules fire / do not


def test_a_healthy_hierarchy_fires_no_resource_scoped_rule() -> None:
    sections = _healthy_sections()
    fired = _fired(sections)

    for rule_id in (
        "TT_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP",
        "TT_STRUCTURE_AD_GROUP_WITHOUT_AD",
        "TT_STRUCTURE_STATUS_NOT_REVIEWED",
        "TT_STRUCTURE_SERVING_INTENT_CONTRADICTS_PARENT",
        "TT_STRUCTURE_PAGINATION_TOTAL_NOT_DECLARED",
        "TT_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET",
        "TT_SPEND_INFINITE_BUDGET_MODE",
        "TT_SPEND_BUDGET_AMOUNT_ABSENT",
        "TT_SPEND_BUDGET_MODE_NOT_RETURNED",
        "TT_OBJECTIVE_NOT_RETURNED",
        "TT_BIDS_TYPE_NOT_RETURNED",
        # BID_TYPE_NO_BID is the documented no-bid mode, so an absent price is
        # expected there and is not reported.
        "TT_BIDS_PRICE_ABSENT",
        "TT_DELIVERY_METRICS_MISSING",
        "TT_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
        "TT_SPEND_WITHOUT_IMPRESSIONS",
        "TT_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS",
        "TT_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
    ):
        assert rule_id not in fired, rule_id


def test_the_read_contract_disclosures_are_reported_even_with_no_retained_inventory() -> None:
    sections = evaluate(_context())
    fired = _fired(sections)

    # These describe the fixed read, not one retained record, so an audit with no
    # inventory at all still reports them.  An unread surface is never silent.
    for rule_id in (
        "TT_SPEND_SCHEDULE_NOT_READ",
        "TT_TARGETING_NOT_READ",
        "TT_PLACEMENT_NOT_READ",
        "TT_TRACKING_PIXEL_AND_EVENT_HEALTH_NOT_READ",
        "TT_NATIVE_CONVERSION_NOT_READ",
        "TT_BROWSER_NOT_APPLICABLE",
        "TT_POLICY_AD_REVIEW_STATE_NOT_READ",
        "TT_POLICY_APP_APPROVAL_AND_PERMISSIONS_NOT_DOCUMENTED",
    ):
        assert rule_id in fired, rule_id


@pytest.mark.parametrize(
    ("quality", "state", "rule_id"),
    [
        (
            ("TIKTOK_BUDGET_MODE_NOT_RETURNED",),
            {"status": "DISABLE"},
            "TT_SPEND_BUDGET_MODE_NOT_RETURNED",
        ),
        (("TIKTOK_BUDGET_MODE_INFINITE",), {"status": "DISABLE"}, "TT_SPEND_INFINITE_BUDGET_MODE"),
        (("TIKTOK_BUDGET_AMOUNT_ABSENT",), {"status": "DISABLE"}, "TT_SPEND_BUDGET_AMOUNT_ABSENT"),
        (("TIKTOK_OBJECTIVE_NOT_RETURNED",), {"status": "DISABLE"}, "TT_OBJECTIVE_NOT_RETURNED"),
        (
            ("TIKTOK_OPERATION_STATUS_NOT_IN_REVIEWED_ENUM",),
            {"status": "SOMETHING_ELSE"},
            "TT_STRUCTURE_STATUS_NOT_REVIEWED",
        ),
    ],
)
def test_each_campaign_rule_fires_on_exactly_its_own_retained_fact(
    quality: tuple[str, ...], state: dict[str, Any], rule_id: str
) -> None:
    sections = _healthy_sections(campaign_state=state, campaign_quality=quality)
    assert rule_id in _fired(sections)


def test_an_unbounded_serving_campaign_is_reported_only_when_no_level_bounds_spend() -> None:
    bounded = _healthy_sections(
        campaign_state={"status": "ENABLE"},
        campaign_quality=("TIKTOK_BUDGET_MODE_NOT_RETURNED",),
    )
    # The retained ad group still carries a lifetime amount, so the campaign is
    # bounded somewhere and the high-severity rule stays silent.
    assert "TT_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET" not in _fired(bounded)

    records = (
        _account(),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state={"status": "ENABLE"},
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS, "TIKTOK_BUDGET_MODE_INFINITE"),
        ),
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=(CAMPAIGN,),
            state={"status": "ENABLE"},
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS, "TIKTOK_BUDGET_MODE_INFINITE"),
        ),
    )
    unbounded = evaluate(_context(evidence=ProviderAuditEvidence(inventory=_inventory(records))))
    assert "TT_SPEND_ACTIVE_CAMPAIGN_WITHOUT_ANY_RETAINED_BUDGET" in _fired(unbounded)


def test_a_bid_price_is_only_reported_absent_for_a_type_other_than_the_no_bid_mode() -> None:
    silent = _healthy_sections(
        ad_group_state={"status": "ENABLE", "bid_strategy": "BID_TYPE_NO_BID"},
        ad_group_quality=("TIKTOK_BID_TYPE_RETURNED", "TIKTOK_BID_PRICE_ABSENT"),
    )
    assert "TT_BIDS_PRICE_ABSENT" not in _fired(silent)

    fired = _healthy_sections(
        ad_group_state={"status": "ENABLE", "bid_strategy": "BID_TYPE_CUSTOM"},
        ad_group_quality=("TIKTOK_BID_TYPE_RETURNED", "TIKTOK_BID_PRICE_ABSENT"),
    )
    assert "TT_BIDS_PRICE_ABSENT" in _fired(fired)


def test_an_absent_bid_type_suppresses_the_price_rule_it_could_not_interpret() -> None:
    sections = _healthy_sections(
        ad_group_state={"status": "ENABLE"},
        ad_group_quality=("TIKTOK_BID_TYPE_NOT_RETURNED", "TIKTOK_BID_PRICE_ABSENT"),
    )
    fired = _fired(sections)
    assert "TT_BIDS_TYPE_NOT_RETURNED" in fired
    assert "TT_BIDS_PRICE_ABSENT" not in fired


def test_a_legacy_read_without_a_proven_declared_total_is_reported() -> None:
    records = (
        _record(
            seed=1,
            resource_type="ACCOUNT",
            resource_ref=ACCOUNT,
            state={"status": "STATUS_ENABLE", "currency": "USD"},
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS,),
        ),
    )
    sections = evaluate(_context(evidence=ProviderAuditEvidence(inventory=_inventory(records))))
    assert "TT_STRUCTURE_PAGINATION_TOTAL_NOT_DECLARED" in _fired(sections)
    assert any(
        item.bound_rule_ids == ("TT_STRUCTURE_PAGINATION_TOTAL_NOT_DECLARED",)
        for item in sections.recommendations
    )


@pytest.mark.parametrize(
    ("report_kwargs", "rule_id"),
    [
        ({"impressions": None}, "TT_DELIVERY_METRICS_MISSING"),
        ({"clicks": None}, "TT_DELIVERY_METRICS_MISSING"),
        ({"impressions": 2, "clicks": 5}, "TT_DELIVERY_CLICKS_EXCEED_IMPRESSIONS"),
        ({"impressions": 0, "clicks": 0, "spend_minor": 50}, "TT_SPEND_WITHOUT_IMPRESSIONS"),
        ({"impressions": 0, "clicks": 0}, "TT_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"),
        (
            {"statuses": ["NOT_AVAILABLE_FROM_PROVIDER"]},
            "TT_DELIVERY_REPORT_COMPLETENESS_NOT_DECLARED",
        ),
    ],
)
def test_each_report_rule_fires_on_exactly_its_own_retained_row(
    report_kwargs: dict[str, Any], rule_id: str
) -> None:
    sections = _healthy_sections(report=_report(**report_kwargs))
    assert rule_id in _fired(sections)


def test_zero_spend_against_zero_impressions_is_not_a_billing_finding() -> None:
    sections = _healthy_sections(report=_report(impressions=0, clicks=0, spend_minor=0))
    assert "TT_SPEND_WITHOUT_IMPRESSIONS" not in _fired(sections)


def test_an_ad_grain_report_never_drives_the_campaign_delivery_rule() -> None:
    # The reviewed ad report uses one date dimension and one ad ID and infers no
    # campaign join, so campaign delivery is evaluated from campaign rows alone.
    sections = _healthy_sections(report=_report(grain="AD", impressions=0, clicks=0))
    assert "TT_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS" not in _fired(sections)


def test_structure_rules_fire_for_a_childless_and_an_orphaned_resource() -> None:
    records = (
        _account(),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state={"status": "DISABLE", "budget_minor": 1},
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS,),
        ),
        _record(
            seed=4,
            resource_type="AD",
            resource_ref=AD,
            parents=("tiktok_ads.adgroup.missing",),
            state={"status": "DISABLE"},
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS,),
        ),
    )
    fired = _fired(
        evaluate(_context(evidence=ProviderAuditEvidence(inventory=_inventory(records))))
    )
    assert "TT_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP" in fired
    assert "TT_STRUCTURE_AD_PARENT_NOT_RETAINED" in fired


def test_an_ad_group_whose_campaign_is_not_retained_is_reported_as_an_orphan() -> None:
    records = (
        _account(),
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=("tiktok_ads.campaign.missing",),
            state={"status": "DISABLE"},
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS,),
        ),
    )
    sections = evaluate(_context(evidence=ProviderAuditEvidence(inventory=_inventory(records))))

    # The ad group is the only retained resource a structure finding names: its
    # missing campaign is reported as an orphan, and having no ad is its own
    # finding.  There is no AD record, so no ad-scoped structure rule can fire.
    structure = {
        (item.rule_id, item.finding.resource_refs)
        for item in sections.findings
        if item.rule_id.startswith("TT_STRUCTURE_")
    }
    assert structure == {
        ("TT_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED", (AD_GROUP,)),
        ("TT_STRUCTURE_AD_GROUP_WITHOUT_AD", (AD_GROUP,)),
    }
    orphan = next(
        item
        for item in sections.findings
        if item.rule_id == "TT_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED"
    )
    ad_group_digest = digest({"seed": 3, "resource": AD_GROUP})
    assert orphan.finding.evidence_refs == (ad_group_digest,)
    # The unretained campaign is never named as if it had been read.
    for item in sections.findings:
        assert "tiktok_ads.campaign.missing" not in item.finding.resource_refs


def test_an_absence_rule_drops_to_low_confidence_on_a_declared_subset() -> None:
    records = (
        _account(),
        _record(
            seed=2,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state={"status": "DISABLE", "budget_minor": 1},
            quality=(TIKTOK_ADS_STATE_SHAPE_STATUS,),
        ),
    )
    subset = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(records, completeness="BOUNDED_NON_EXHAUSTIVE")
            )
        )
    )
    childless = [
        item for item in subset.findings if item.rule_id == "TT_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP"
    ]
    assert childless and childless[0].finding.confidence == "LOW"
    whole = evaluate(_context(evidence=ProviderAuditEvidence(inventory=_inventory(records))))
    whole_childless = [
        item for item in whole.findings if item.rule_id == "TT_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP"
    ]
    assert whole_childless and whole_childless[0].finding.confidence == "HIGH"


# ---------------------------------------------------------------- creative


def _catalog(resource_ref: str | None) -> RetainedCreativeCatalog:
    return RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:tiktok:v1",
                record_digest=digest({"creative": resource_ref}),
                provider="tiktok_ads",
                provider_resource_ref=resource_ref,
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
        )
    )


def test_a_local_binding_naming_a_retained_ad_is_reported_as_unverified() -> None:
    sections = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records()), creative_catalog=_catalog(AD)
            )
        )
    )
    assert "TT_CREATIVE_LOCAL_BINDING_UNVERIFIED" in _fired(sections)


def test_a_local_binding_with_no_retained_ad_is_reported_only_with_a_proven_boundary() -> None:
    proven = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records()),
                creative_catalog=_catalog("tiktok_ads.ad.999"),
            )
        )
    )
    assert "TT_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" in _fired(proven)

    unproven = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(_healthy_records(), completeness="READ_BOUNDARY_UNVERIFIED"),
                creative_catalog=_catalog("tiktok_ads.ad.999"),
            )
        )
    )
    assert "TT_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" not in _fired(unproven)

    no_inventory = evaluate(
        _context(evidence=ProviderAuditEvidence(creative_catalog=_catalog("tiktok_ads.ad.999")))
    )
    assert "TT_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" not in _fired(no_inventory)


# ------------------------------------------------------------------- drift


def test_a_value_change_is_a_drift_observation_with_an_unknown_actor() -> None:
    later = _healthy_records()
    earlier = list(_healthy_records())
    earlier[1] = _record(
        seed=2,
        resource_type="CAMPAIGN",
        resource_ref=CAMPAIGN,
        parents=(ACCOUNT,),
        state={
            "status": "DISABLE",
            "currency": "USD",
            "objective": "TRAFFIC",
            "tiktok_ads_budget_mode": "BUDGET_MODE_DAY",
            "budget_minor": 5_000,
            "budget_type": "DAILY",
        },
        quality=(TIKTOK_ADS_STATE_SHAPE_STATUS, "TIKTOK_OPERATION_STATUS_REVIEWED"),
        observed_at=EARLIER,
    )
    sections = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(later),
                earlier_inventory=_inventory(tuple(earlier), observed_at=EARLIER),
            )
        )
    )

    assert "TT_DRIFT_STATE_CHANGED" in _fired(sections)
    observation = next(
        item for item in sections.drift_observations if item.change_kind == "STATE_CHANGED"
    )
    assert observation.observed_actor == "UNKNOWN"
    assert observation.changed_paths == ("normalized_state.status",)
    assert observation.observation_interval_start == EARLIER
    assert observation.observation_interval_end == OBSERVED


def test_a_connector_upgrade_that_adds_a_retained_field_is_not_a_change_at_tiktok() -> None:
    later = _healthy_records()
    earlier = list(_healthy_records())
    state = dict(earlier[1].snapshot.normalized_state)
    state.pop("objective")
    earlier[1] = _record(
        seed=2,
        resource_type="CAMPAIGN",
        resource_ref=CAMPAIGN,
        parents=(ACCOUNT,),
        state=state,
        quality=("TIKTOK_ADS_STATE_SHAPE_2026-01-01.1",),
        observed_at=EARLIER,
    )
    sections = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(later),
                earlier_inventory=_inventory(tuple(earlier), observed_at=EARLIER),
            )
        )
    )
    fired = _fired(sections)

    assert "TT_DRIFT_LOCAL_STATE_SHAPE_CHANGED" in fired
    # A key-set difference is never published as an out-of-band change at TikTok.
    assert "TT_DRIFT_STATE_CHANGED" not in fired
    assert not [item for item in sections.drift_observations if item.resource_ref == CAMPAIGN]
    shape = next(
        item for item in sections.findings if item.rule_id == "TT_DRIFT_LOCAL_STATE_SHAPE_CHANGED"
    )
    assert "2026-01-01.1" in shape.finding.suggested_action
    assert "2026-09-09.1" in shape.finding.suggested_action


def test_absence_drift_is_suppressed_without_a_proven_boundary_on_both_sides() -> None:
    later = _healthy_records()
    earlier = (
        *later,
        _record(
            seed=9,
            resource_type="AD",
            resource_ref="tiktok_ads.ad.99",
            parents=(CAMPAIGN, AD_GROUP),
            observed_at=EARLIER,
        ),
    )
    proven = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(later),
                earlier_inventory=_inventory(earlier, observed_at=EARLIER),
            )
        )
    )
    assert "TT_DRIFT_ABSENT_FROM_LATER_INVENTORY" in _fired(proven)

    unproven = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(later, completeness="READ_BOUNDARY_UNVERIFIED"),
                earlier_inventory=_inventory(earlier, observed_at=EARLIER),
            )
        )
    )
    fired = _fired(unproven)
    assert "TT_DRIFT_ABSENT_FROM_LATER_INVENTORY" not in fired
    assert "TT_DRIFT_ABSENT_FROM_EARLIER_INVENTORY" not in fired
    drift_coverage = next(item for item in unproven.coverage if item.dimension == "DRIFT")
    # The omission is stated, not left to look like a cleaner result.
    assert "not evaluated at all" in drift_coverage.rationale


def test_a_resource_new_in_the_later_inventory_is_absent_from_the_earlier_one() -> None:
    later = (
        *_healthy_records(),
        _record(
            seed=9,
            resource_type="AD",
            resource_ref="tiktok_ads.ad.99",
            parents=(CAMPAIGN, AD_GROUP),
        ),
    )
    earlier = _healthy_records()
    sections = evaluate(
        _context(
            evidence=ProviderAuditEvidence(
                inventory=_inventory(later),
                earlier_inventory=_inventory(earlier, observed_at=EARLIER),
            )
        )
    )

    appeared = [
        item
        for item in sections.findings
        if item.rule_id == "TT_DRIFT_ABSENT_FROM_EARLIER_INVENTORY"
    ]
    assert [item.finding.resource_refs for item in appeared] == [("tiktok_ads.ad.99",)]
    observation = next(
        item for item in sections.drift_observations if item.resource_ref == "tiktok_ads.ad.99"
    )
    assert observation.change_kind == "RESOURCE_ABSENT_FROM_EARLIER_INVENTORY"
    # The interval is the two observations; no exact external change time or
    # actor is invented.
    assert observation.observation_interval_start == EARLIER
    assert observation.observation_interval_end == OBSERVED
    assert "TT_DRIFT_ABSENT_FROM_LATER_INVENTORY" not in _fired(sections)


# ---------------------------------------------------------------- coverage


def test_every_dimension_carries_exactly_one_status_derived_from_the_read_surfaces() -> None:
    sections = _healthy_sections()
    statuses = {item.dimension: item.status for item in sections.coverage}

    assert len(statuses) == 14
    assert statuses["BROWSER_MEASUREMENT"] == "NOT_APPLICABLE"
    # A dimension no implemented fixed read backs is NOT_READ whatever the
    # caller supplies.
    for dimension in (
        "TARGETING",
        "PLACEMENT",
        "DESTINATION_CONFIGURATION",
        "TRACKING",
        "NATIVE_CONVERSION",
    ):
        assert statuses[dimension] == "NOT_READ", dimension
    assert statuses["STRUCTURE"] == "COVERED"
    assert statuses["DELIVERY"] == "PARTIAL"


def test_an_unread_dimension_cannot_become_readable_by_supplying_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import adhelm.audits.tiktok_ads as module

    surfaces = dict(TIKTOK_ADS_READ_SURFACES)
    surfaces["AD_GROUP_TARGETING_PRESENCE"] = "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY"
    monkeypatch.setattr(module, "TIKTOK_ADS_READ_SURFACES", surfaces)
    promoted = {item.dimension: item.status for item in _healthy_sections().coverage}
    # Proof the ceiling really is read from the surface table rather than a
    # literal: flipping the surface is the only thing that moves the dimension.
    assert promoted["TARGETING"] == "PARTIAL"


def test_an_unread_or_inapplicable_dimension_cites_no_evidence() -> None:
    for item in _healthy_sections().coverage:
        if item.status in {"NOT_READ", "NOT_APPLICABLE"}:
            assert item.evidence_sources == ()
            assert item.evidence_record_count == 0
            assert item.evidence_refs == ()
        else:
            assert item.evidence_sources
            assert item.evidence_record_count >= 1


# ------------------------------------------------- bounds, determinism, safety


def test_a_recommendation_that_exceeds_the_inline_bound_records_it() -> None:
    records: list[RetainedSnapshotRecord] = [_account()]
    for index in range(40):
        records.append(
            _record(
                seed=1_000 + index,
                resource_type="CAMPAIGN",
                resource_ref=f"tiktok_ads.campaign.c{index}",
                parents=(ACCOUNT,),
                state={"status": "ENABLE"},
                quality=(TIKTOK_ADS_STATE_SHAPE_STATUS, "TIKTOK_BUDGET_MODE_INFINITE"),
            )
        )
    sections = evaluate(
        _context(evidence=ProviderAuditEvidence(inventory=_inventory(tuple(records))))
    )

    assert sections.bound_exceeded
    subjects = [item.subject for item in sections.bound_exceeded]
    assert len(subjects) == len(set(subjects))
    for entry in sections.bound_exceeded:
        assert entry.bound == MAXIMUM_INLINE_FINDING_IDS
        assert (
            entry.inlined_finding_count + entry.omitted_finding_count == entry.total_finding_count
        )
        assert entry.resolution == "PAGE_FINDINGS_AND_MATCH_RULE_ID"
        assert not _has_digest_like_run(entry.subject)


def test_more_than_six_hundred_findings_stay_individually_addressable() -> None:
    records: list[RetainedSnapshotRecord] = [_account()]
    for index in range(220):
        records.append(
            _record(
                seed=2_000 + index,
                resource_type="CAMPAIGN",
                resource_ref=f"tiktok_ads.campaign.c{index}",
                parents=(ACCOUNT,),
                state={"status": "ENABLE"},
                quality=(
                    TIKTOK_ADS_STATE_SHAPE_STATUS,
                    "TIKTOK_BUDGET_MODE_NOT_RETURNED",
                    "TIKTOK_OBJECTIVE_NOT_RETURNED",
                    "TIKTOK_OPERATION_STATUS_NOT_IN_REVIEWED_ENUM",
                ),
            )
        )
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
        campaign_state={"status": "ENABLE"},
        campaign_quality=("TIKTOK_BUDGET_MODE_NOT_RETURNED",),
    )

    validate_data(sections.model_dump(mode="json"))
    for finding in sections.findings:
        assert not _has_digest_like_run(finding.finding.observation)
        assert not _has_digest_like_run(finding.finding.suggested_action)
    for recommendation in sections.recommendations:
        assert not _has_digest_like_run(recommendation.recommendation.hypothesis)
        assert not _has_digest_like_run(recommendation.recommendation.expected_impact)
        assert recommendation.recommendation.proposed_plan_ref is None
        assert recommendation.execution_status == "NOT_EXECUTED"
        assert recommendation.plan_status == "NOT_A_PLAN"
        assert recommendation.authorization_status == "NO_AUTHORIZATION_DIGEST_PRESENT"


def test_the_rule_table_version_is_part_of_every_finding_identity() -> None:
    import adhelm.audits.tiktok_ads as module

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
    assert first.rule_table_version == RULE_TABLE_VERSION


def test_no_retained_report_is_ever_published_as_exhaustive() -> None:
    sections = _healthy_sections()
    assert sections.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"
    assert sections.live_provider_reads == "NONE_RETAINED_RECORDS_ONLY"


def test_the_table_refuses_another_providers_evidence() -> None:
    context = ProviderAuditContext(
        provider="meta_ads",
        report=_report(),
        source_record_digest="d" * 64,
        policy=AuditPolicy(
            maximum_observation_age_seconds=604_800, require_conversion_evidence=False
        ),
        as_of=datetime(2026, 9, 2, 12, tzinfo=UTC),
        evidence=ProviderAuditEvidence(),
    )
    with pytest.raises(ValueError):
        evaluate(context)


def test_the_recommendation_expiry_comes_from_the_user_declared_policy() -> None:
    sections = _healthy_sections(report=_report(impressions=0, clicks=0))
    expires = datetime(2026, 9, 2, 12, tzinfo=UTC) + timedelta(seconds=604_800)
    assert sections.recommendations
    for recommendation in sections.recommendations:
        assert recommendation.recommendation.expires_at == expires
