# ruff: noqa: E501
"""Fixture-only tests for the Reddit Ads provider audit rule table.

Every case here is offline.  No test performs or simulates a Reddit request,
and no case reconciles against the Reddit Ads interface.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from adhelm.audit import AuditPolicy, audit_provider, audit_report
from adhelm.audits import (
    MAXIMUM_INLINE_FINDING_IDS,
    NoRecommendation,
    ProviderAuditContext,
    ProviderAuditCoverage,
    ProviderAuditEvidence,
    ProviderAuditSections,
    ProviderRecommendation,
    ResourceSnapshotState,
    RetainedCreativeCatalog,
    RetainedCreativeCatalogEntry,
    RetainedInventory,
    RetainedSnapshotRecord,
    provider_rule,
    snapshot_state_deltas,
)
from adhelm.audits.reddit_ads import RULE_TABLE_VERSION, RULES, evaluate
from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.models import Money
from adhelm.privacy import validate_data
from adhelm.providers.reddit_ads import REDDIT_ADS_READ_SURFACES, RedditAdsClient
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope

REPORT_DIGEST = "a" * 64
OBSERVED = datetime(2026, 9, 1, 12, tzinfo=UTC)
EARLIER_OBSERVED = datetime(2026, 8, 25, 12, tzinfo=UTC)
AS_OF = datetime(2026, 9, 1, 18, tzinfo=UTC)
ACCOUNT = "reddit_ads.account.a2_fixture"
CAMPAIGN = "reddit_ads.campaign.c1"
AD_GROUP = "reddit_ads.ad_group.g1"
AD = "reddit_ads.ad.ad1"


def _digest(seed: int) -> str:
    return format(seed, "064x")


def _envelope(provider: str = "reddit_ads") -> ReportEnvelope:
    return ReportEnvelope(
        report_id="report:reddit-fixture",
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
        data_quality_statuses=["PROCESSING_PENDING", "NOT_AVAILABLE_FROM_PROVIDER"],
        provider_api_version="v3",
        connector_version="test",
    )


def _row(
    *,
    resource_ref: str = CAMPAIGN,
    impressions: int | None = 100,
    clicks: int | None = 4,
    micros: str | None = "1250000",
    minor_units: int = 125,
) -> PerformanceRow:
    extensions: dict[str, str | int | bool | None] = {}
    if micros is not None:
        extensions = {
            "reddit_ads_spend_micros": micros,
            "reddit_ads_rounding": "HALF_EVEN_TO_MINOR",
        }
    return PerformanceRow(
        report_id="report:reddit-fixture",
        resource_level="CAMPAIGN",
        metric_grain="CAMPAIGN",
        resource_type="REDDIT_CAMPAIGN",
        resource_ref=resource_ref,
        impressions=impressions,
        clicks=clicks,
        spend=Money(minor_units=minor_units, currency="USD"),
        join_status="UNMAPPED",
        data_quality_statuses=["NOT_AVAILABLE_FROM_PROVIDER"],
        provider_extensions=extensions,
    )


def _report(
    rows: list[PerformanceRow] | None = None, provider: str = "reddit_ads"
) -> NormalizedReport:
    if rows is None:
        rows = [_row()] if provider == "reddit_ads" else [_row(micros=None)]
    return NormalizedReport(envelope=_envelope(provider), performance_rows=rows)


def _record(
    *,
    seed: int,
    resource_type: str,
    resource_ref: str,
    parents: tuple[str, ...] = (),
    state: dict[str, Any] | None = None,
    status: str = "ACTIVE",
    observed_at: datetime = OBSERVED,
) -> RetainedSnapshotRecord:
    normalized = state if state is not None else {"status": status, "currency": "USD"}
    return RetainedSnapshotRecord(
        record_digest=_digest(seed),
        observed_at=observed_at,
        snapshot=ResourceSnapshotState(
            provider_api_version="v3",
            resource_type=resource_type,  # type: ignore[arg-type]
            resource_ref=resource_ref,
            parent_resource_refs=parents,
            normalized_state=normalized,
            normalized_state_digest=digest(normalized),
            effective_status=status,
            data_quality_statuses=(),
        ),
    )


def _campaign_record(
    *,
    status: str = "ACTIVE",
    state: dict[str, Any] | None = None,
    observed_at: datetime = OBSERVED,
    seed: int = 2,
) -> RetainedSnapshotRecord:
    return _record(
        seed=seed,
        resource_type="CAMPAIGN",
        resource_ref=CAMPAIGN,
        parents=(ACCOUNT,),
        state=state or {"status": status, "objective": "TRAFFIC", "currency": "USD"},
        status=status,
        observed_at=observed_at,
    )


def _healthy_records(observed_at: datetime = OBSERVED) -> tuple[RetainedSnapshotRecord, ...]:
    return (
        _record(
            seed=1,
            resource_type="ACCOUNT",
            resource_ref=ACCOUNT,
            state={"status": "NOT_AVAILABLE_FROM_PROVIDER", "currency": "USD"},
            status="NOT_AVAILABLE_FROM_PROVIDER",
            observed_at=observed_at,
        ),
        _campaign_record(observed_at=observed_at),
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=(CAMPAIGN,),
            observed_at=observed_at,
        ),
        _record(
            seed=4,
            resource_type="AD",
            resource_ref=AD,
            parents=(CAMPAIGN, AD_GROUP),
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
        provider="reddit_ads",
        account_ref=ACCOUNT,
        observed_at=observed_at,
        completeness=completeness,  # type: ignore[arg-type]
        records=records,
    )


def _policy(**changes: object) -> AuditPolicy:
    values: dict[str, object] = {
        "maximum_observation_age_seconds": 604_800,
        "maximum_cpc": None,
        "minimum_impressions": None,
        "require_conversion_evidence": False,
    }
    values.update(changes)
    return AuditPolicy(**values)


def _sections(
    *,
    rows: list[PerformanceRow] | None = None,
    inventory: RetainedInventory | None = None,
    earlier: RetainedInventory | None = None,
    catalog: RetainedCreativeCatalog | None = None,
) -> ProviderAuditSections:
    context = ProviderAuditContext(
        provider="reddit_ads",
        report=_report(rows),
        source_record_digest=REPORT_DIGEST,
        policy=_policy(),
        as_of=AS_OF,
        evidence=ProviderAuditEvidence(
            inventory=inventory, earlier_inventory=earlier, creative_catalog=catalog
        ),
    )
    return evaluate(context)


def _rule_ids(sections: ProviderAuditSections) -> set[str]:
    return {item.rule_id for item in sections.findings}


def _healthy() -> ProviderAuditSections:
    return _sections(inventory=_inventory(_healthy_records()))


def _catalog(*entries: RetainedCreativeCatalogEntry) -> RetainedCreativeCatalog:
    return RetainedCreativeCatalog(entries=entries)


def _binding(seed: int, ref: str | None) -> RetainedCreativeCatalogEntry:
    return RetainedCreativeCatalogEntry(
        creative_version_id=f"creative:version:{seed}",
        record_digest=_digest(40 + seed),
        provider="reddit_ads",
        provider_resource_ref=ref,
        binding_status="LOCAL_DECLARED_UNVERIFIED" if ref is not None else "NOT_CREATED",
    )


# --- registry seam -----------------------------------------------------------


def test_the_registry_dispatches_the_reddit_table_for_reddit_only() -> None:
    assert provider_rule("reddit_ads") is evaluate
    report = _report()
    result = audit_provider(
        report,
        source_record_digest=REPORT_DIGEST,
        policy=_policy(),
        as_of=AS_OF,
        evidence=ProviderAuditEvidence(inventory=_inventory(_healthy_records())),
    )
    assert result.rules_status == "EVALUATED"
    assert result.sections is not None
    assert result.sections.provider == "reddit_ads"
    # The provider-neutral audit is embedded unchanged.
    assert result.audit == audit_report(
        report, source_record_digest=REPORT_DIGEST, policy=_policy(), as_of=AS_OF
    )


def test_provider_audit_rejects_inventory_from_another_provider() -> None:
    other = RetainedInventory(
        provider="meta_ads",
        account_ref="meta_ads.account.1",
        observed_at=OBSERVED,
        completeness="COMPLETE_BOUNDED_READ",
        records=(),
    )
    with pytest.raises(AdhelmError) as error:
        audit_provider(
            _report(),
            source_record_digest=REPORT_DIGEST,
            policy=_policy(),
            as_of=AS_OF,
            evidence=ProviderAuditEvidence(inventory=other),
        )
    assert error.value.code == "INVALID_AUDIT_INPUT"
    assert error.value.exit_code == 2


def test_provider_audit_rejects_inventory_for_another_reddit_account() -> None:
    other = RetainedInventory(
        provider="reddit_ads",
        account_ref="a2_other",
        observed_at=OBSERVED,
        completeness="COMPLETE_BOUNDED_READ",
        records=(),
    )
    with pytest.raises(AdhelmError) as error:
        audit_provider(
            _report(),
            source_record_digest=REPORT_DIGEST,
            policy=_policy(),
            as_of=AS_OF,
            evidence=ProviderAuditEvidence(inventory=other),
        )
    assert error.value.code == "INVALID_AUDIT_INPUT"


def test_the_bare_and_canonical_spellings_of_one_reddit_account_join() -> None:
    bare = RetainedInventory(
        provider="reddit_ads",
        account_ref="a2_fixture",
        observed_at=OBSERVED,
        completeness="COMPLETE_BOUNDED_READ",
        records=_healthy_records(),
    )
    result = audit_provider(
        _report(),
        source_record_digest=REPORT_DIGEST,
        policy=_policy(),
        as_of=AS_OF,
        evidence=ProviderAuditEvidence(inventory=bare),
    )
    assert result.rules_status == "EVALUATED"


def test_rule_table_evaluates_reddit_evidence_only() -> None:
    context = ProviderAuditContext(
        provider="meta_ads",
        report=_report(provider="meta_ads"),
        source_record_digest=REPORT_DIGEST,
        policy=_policy(),
        as_of=AS_OF,
        evidence=ProviderAuditEvidence(),
    )
    with pytest.raises(ValueError):
        evaluate(context)


# --- determinism -------------------------------------------------------------


def test_identical_inputs_produce_identical_bytes() -> None:
    catalog = _catalog(_binding(1, AD))
    earlier = _inventory(_healthy_records(EARLIER_OBSERVED), observed_at=EARLIER_OBSERVED)
    first = _sections(inventory=_inventory(_healthy_records()), earlier=earlier, catalog=catalog)
    second = _sections(inventory=_inventory(_healthy_records()), earlier=earlier, catalog=catalog)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert [item.finding.finding_id for item in first.findings] == sorted(
        item.finding.finding_id for item in first.findings
    )
    assert first.rule_table_version == RULE_TABLE_VERSION


def test_every_rule_identifier_is_unique_and_self_consistent() -> None:
    assert all(rule_id == rule.rule_id for rule_id, rule in RULES.items())
    assert len({rule.finding_type for rule in RULES.values()}) == len(RULES)
    assert all(rule_id.startswith("RD_") for rule_id in RULES)


def test_no_rule_is_dead() -> None:
    """Every rule in the table can fire; none is a leftover from another provider."""
    records = list(_healthy_records())
    catalog = _catalog(_binding(1, AD), _binding(2, "reddit_ads.ad.absent"))
    scenarios = [
        _sections(inventory=_inventory(_healthy_records()), catalog=catalog),
        _sections(
            rows=[
                _row(impressions=2, clicks=9),
                _row(resource_ref="reddit_ads.campaign.zero", impressions=0, clicks=0),
            ],
            inventory=_inventory(
                (
                    records[0],
                    _campaign_record(state={"status": "ACTIVE", "currency": "USD"}),
                    _record(
                        seed=3,
                        resource_type="AD_GROUP",
                        resource_ref=AD_GROUP,
                        parents=(CAMPAIGN,),
                        status="ARCHIVED",
                    ),
                    _record(
                        seed=4,
                        resource_type="AD",
                        resource_ref=AD,
                        parents=("reddit_ads.campaign.other", AD_GROUP),
                    ),
                    _record(
                        seed=8,
                        resource_type="CAMPAIGN",
                        resource_ref="reddit_ads.campaign.other",
                        parents=(ACCOUNT,),
                    ),
                    _record(
                        seed=5,
                        resource_type="AD",
                        resource_ref="reddit_ads.ad.orphan",
                        parents=(CAMPAIGN, "reddit_ads.ad_group.missing"),
                        status="NOT_AVAILABLE_FROM_PROVIDER",
                    ),
                    _record(
                        seed=6,
                        resource_type="AD_GROUP",
                        resource_ref="reddit_ads.ad_group.lost",
                        parents=("reddit_ads.campaign.missing",),
                    ),
                    _record(
                        seed=7,
                        resource_type="CAMPAIGN",
                        resource_ref="reddit_ads.campaign.empty",
                        parents=(ACCOUNT,),
                    ),
                )
            ),
            earlier=_inventory(
                (
                    records[0],
                    _campaign_record(status="PAUSED", seed=12),
                    _record(
                        seed=13,
                        resource_type="AD_GROUP",
                        resource_ref="reddit_ads.ad_group.gone",
                        parents=(CAMPAIGN,),
                    ),
                ),
                observed_at=EARLIER_OBSERVED,
            ),
        ),
    ]
    fired = set().union(*(_rule_ids(item) for item in scenarios))
    assert fired == set(RULES)


# --- coverage map ------------------------------------------------------------


def test_full_evidence_coverage_statuses_are_exact() -> None:
    sections = _sections(
        inventory=_inventory(_healthy_records()),
        earlier=_inventory(_healthy_records(EARLIER_OBSERVED), observed_at=EARLIER_OBSERVED),
    )
    assert {item.dimension: item.status for item in sections.coverage} == {
        "STRUCTURE": "COVERED",
        "DELIVERY": "PARTIAL",
        "SPEND": "PARTIAL",
        "BIDS": "NOT_READ",
        "OBJECTIVE": "PARTIAL",
        "TARGETING": "NOT_READ",
        "PLACEMENT": "NOT_READ",
        "CREATIVE": "PARTIAL",
        "DESTINATION_CONFIGURATION": "NOT_READ",
        "TRACKING": "NOT_READ",
        "NATIVE_CONVERSION": "NOT_READ",
        "BROWSER_MEASUREMENT": "NOT_APPLICABLE",
        "POLICY": "NOT_READ",
        "DRIFT": "COVERED",
    }


def test_report_only_coverage_marks_every_configuration_dimension_unread() -> None:
    sections = _sections()
    statuses = {item.dimension: item.status for item in sections.coverage}
    assert statuses["STRUCTURE"] == "NOT_READ"
    assert statuses["OBJECTIVE"] == "NOT_READ"
    assert statuses["CREATIVE"] == "NOT_READ"
    assert statuses["DRIFT"] == "NOT_READ"
    assert statuses["DELIVERY"] == "PARTIAL"
    assert statuses["SPEND"] == "PARTIAL"
    assert statuses["BROWSER_MEASUREMENT"] == "NOT_APPLICABLE"
    unread = [item for item in sections.coverage if item.status == "NOT_READ"]
    assert all(not item.evidence_refs and item.evidence_record_count == 0 for item in unread)


def test_a_partial_inventory_downgrades_structure_coverage_and_absence_confidence() -> None:
    records = _healthy_records()
    partial = _sections(
        inventory=_inventory(records[:3], completeness="BOUNDED_NON_EXHAUSTIVE"),
    )
    assert {item.dimension: item.status for item in partial.coverage}["STRUCTURE"] == "PARTIAL"
    absence = [
        item for item in partial.findings if item.rule_id == "RD_STRUCTURE_AD_GROUP_WITHOUT_AD"
    ]
    assert [item.finding.confidence for item in absence] == ["LOW"]
    complete = _sections(inventory=_inventory(records[:3]))
    absence = [
        item for item in complete.findings if item.rule_id == "RD_STRUCTURE_AD_GROUP_WITHOUT_AD"
    ]
    assert [item.finding.confidence for item in absence] == ["HIGH"]


def test_no_coverage_status_or_finding_claims_an_exhaustive_report() -> None:
    sections = _sections(inventory=_inventory(_healthy_records()))
    assert sections.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"
    assert all(
        item.status != "COVERED"
        for item in sections.coverage
        if item.dimension in {"DELIVERY", "SPEND"}
    )
    # `AnalyticsCompleteness` has no exhaustive value, so assert the closed set
    # the findings may use and that the bounded value is actually present.
    values = {item.analytics_completeness for item in _report_derived_sections().findings}
    assert values <= {"BOUNDED_NON_EXHAUSTIVE", "NOT_APPLICABLE"}
    assert "BOUNDED_NON_EXHAUSTIVE" in values


def test_coverage_is_capped_by_the_adapters_read_surface_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dimension whose read surface is not implemented can never be reported as read."""
    for surface in ("CAMPAIGN_DELIVERY_REPORT", "AD_DELIVERY_REPORT"):
        monkeypatch.setitem(REDDIT_ADS_READ_SURFACES, surface, "NOT_IN_REVIEWED_FIXED_READ")
    sections = _sections(inventory=_inventory(_healthy_records()))
    statuses = {item.dimension: item.status for item in sections.coverage}
    assert statuses["DELIVERY"] == "NOT_READ"
    assert statuses["SPEND"] == "NOT_READ"
    assert statuses["STRUCTURE"] == "COVERED"


_REPORT_DERIVED_RULES = {
    "RD_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS",
    "RD_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
    "RD_SPEND_WITHOUT_IMPRESSIONS",
}


def _report_derived_sections() -> ProviderAuditSections:
    """Sections in which every report-derived rule fires."""
    return _sections(
        rows=[
            _row(resource_ref="reddit_ads.campaign.998", impressions=2, clicks=9),
            _row(
                resource_ref="reddit_ads.campaign.997",
                impressions=0,
                clicks=0,
                micros="4000",
                minor_units=0,
            ),
            _row(resource_ref="reddit_ads.campaign.999"),
        ],
        inventory=_inventory(_healthy_records()),
    )


def test_every_report_derived_finding_is_labeled_bounded_and_agrees_with_the_section() -> None:
    sections = _report_derived_sections()

    report_derived = [item for item in sections.findings if item.rule_id in _REPORT_DERIVED_RULES]
    assert {item.rule_id for item in report_derived} == _REPORT_DERIVED_RULES
    assert all(item.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE" for item in report_derived)
    # The section label is the same literal; it is never derived from the adapter table.
    assert sections.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"
    # Everything that is not report-derived says so rather than claiming a bound.
    others = [item for item in sections.findings if item.rule_id not in _REPORT_DERIVED_RULES]
    assert others
    assert {item.analytics_completeness for item in others} == {"NOT_APPLICABLE"}


def test_the_section_label_does_not_depend_on_the_adapters_continuation_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The label is a fixed bounded literal, so a table edit cannot make it disagree."""
    monkeypatch.setitem(REDDIT_ADS_READ_SURFACES, "REPORT_CONTINUATION", "IMPLEMENTED_FIXED_READ")
    sections = _report_derived_sections()

    assert sections.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"
    assert {
        item.analytics_completeness
        for item in sections.findings
        if item.rule_id in _REPORT_DERIVED_RULES
    } == {"BOUNDED_NON_EXHAUSTIVE"}


# --- structure rules ---------------------------------------------------------


def test_structure_rules_do_not_fire_on_a_consistent_inventory() -> None:
    fired = _rule_ids(_healthy())
    assert not fired & {
        "RD_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP",
        "RD_STRUCTURE_AD_GROUP_WITHOUT_AD",
        "RD_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED",
        "RD_STRUCTURE_AD_PARENT_NOT_RETAINED",
        "RD_STRUCTURE_AD_CAMPAIGN_MISMATCH",
        "RD_STRUCTURE_SERVING_INTENT_MISMATCH",
        "RD_STRUCTURE_STATUS_NOT_RETURNED",
        "RD_STRUCTURE_STATUS_NOT_REVIEWED",
    }


def test_childless_campaigns_and_ad_groups_fire_their_rules() -> None:
    campaign_only = _sections(inventory=_inventory(_healthy_records()[:2]))
    assert "RD_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP" in _rule_ids(campaign_only)
    group_only = _sections(inventory=_inventory(_healthy_records()[:3]))
    fired = [
        item for item in group_only.findings if item.rule_id == "RD_STRUCTURE_AD_GROUP_WITHOUT_AD"
    ]
    assert [item.finding.resource_refs for item in fired] == [(AD_GROUP,)]
    assert fired[0].finding.evidence_refs == (_digest(3),)


def test_orphaned_ad_group_and_ad_fire_their_rules() -> None:
    records = (
        _healthy_records()[0],
        _record(
            seed=3,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=("reddit_ads.campaign.missing",),
        ),
        _record(
            seed=4,
            resource_type="AD",
            resource_ref=AD,
            parents=("reddit_ads.campaign.missing", "reddit_ads.ad_group.missing"),
        ),
    )
    fired = _rule_ids(_sections(inventory=_inventory(records)))
    assert "RD_STRUCTURE_AD_GROUP_PARENT_NOT_RETAINED" in fired
    assert "RD_STRUCTURE_AD_PARENT_NOT_RETAINED" in fired


def test_an_ad_whose_campaign_is_not_its_ad_groups_campaign_is_inconsistent() -> None:
    other = "reddit_ads.campaign.other"
    records = (
        *_healthy_records()[:3],
        _record(seed=5, resource_type="CAMPAIGN", resource_ref=other, parents=(ACCOUNT,)),
        _record(seed=4, resource_type="AD", resource_ref=AD, parents=(other, AD_GROUP)),
    )
    fired = [
        item
        for item in _sections(inventory=_inventory(records)).findings
        if item.rule_id == "RD_STRUCTURE_AD_CAMPAIGN_MISMATCH"
    ]
    assert [item.finding.resource_refs for item in fired] == [(AD, AD_GROUP)]
    assert fired[0].finding.evidence_refs == (_digest(4), _digest(3))


def test_parent_child_status_mismatch_fires_for_both_parents_of_an_ad() -> None:
    records = list(_healthy_records())
    records[1] = _campaign_record(status="PAUSED")
    records[2] = _record(
        seed=3,
        resource_type="AD_GROUP",
        resource_ref=AD_GROUP,
        parents=(CAMPAIGN,),
        status="PAUSED",
    )
    sections = _sections(inventory=_inventory(tuple(records)))
    fired = sorted(
        item.finding.resource_refs
        for item in sections.findings
        if item.rule_id == "RD_STRUCTURE_SERVING_INTENT_MISMATCH"
    )
    # The paused campaign and the paused ad group each contradict the active ad.
    # A paused ad group under a paused campaign declares no intent to serve, so
    # it contradicts nothing.
    assert fired == sorted([(AD, CAMPAIGN), (AD, AD_GROUP)])


def test_a_parent_with_no_returned_status_is_not_a_serving_contradiction() -> None:
    records = list(_healthy_records())
    records[1] = _campaign_record(
        status="NOT_AVAILABLE_FROM_PROVIDER", state={"currency": "USD", "objective": "TRAFFIC"}
    )
    fired = _rule_ids(_sections(inventory=_inventory(tuple(records))))
    assert "RD_STRUCTURE_SERVING_INTENT_MISMATCH" not in fired
    assert "RD_STRUCTURE_STATUS_NOT_RETURNED" in fired
    # A missing status is not an unreviewed status value.
    assert "RD_STRUCTURE_STATUS_NOT_REVIEWED" not in fired


def test_an_unreviewed_status_value_is_surfaced_without_being_called_invalid() -> None:
    records = list(_healthy_records())
    records[2] = _record(
        seed=3,
        resource_type="AD_GROUP",
        resource_ref=AD_GROUP,
        parents=(CAMPAIGN,),
        status="SOME_NEW_STATUS",
    )
    fired = [
        item
        for item in _sections(inventory=_inventory(tuple(records))).findings
        if item.rule_id == "RD_STRUCTURE_STATUS_NOT_REVIEWED"
    ]
    assert [item.finding.resource_refs for item in fired] == [(AD_GROUP,)]
    assert fired[0].finding.severity == "INFO"
    assert "ACTIVE and PAUSED" in fired[0].finding.observation


# --- delivery and spend rules -------------------------------------------------


def test_delivery_rules_do_not_fire_on_a_consistent_report() -> None:
    fired = _rule_ids(_healthy())
    assert not fired & {
        "RD_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
        "RD_SPEND_WITHOUT_IMPRESSIONS",
        "RD_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS",
    }


def test_zero_impression_active_campaign_fires_with_bounded_completeness() -> None:
    sections = _sections(
        rows=[_row(resource_ref="reddit_ads.campaign.999")],
        inventory=_inventory(_healthy_records()),
    )
    fired = [
        item
        for item in sections.findings
        if item.rule_id == "RD_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"
    ]
    assert len(fired) == 1
    assert fired[0].analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"
    assert fired[0].finding.confidence == "LOW"
    assert fired[0].finding.evidence_refs == (_digest(2), REPORT_DIGEST)
    assert "not proof of zero delivery" in fired[0].finding.observation


def test_a_paused_campaign_with_no_rows_is_not_a_delivery_finding() -> None:
    records = list(_healthy_records())
    records[1] = _campaign_record(status="PAUSED")
    sections = _sections(
        rows=[_row(resource_ref="reddit_ads.campaign.999")], inventory=_inventory(tuple(records))
    )
    assert "RD_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS" not in _rule_ids(sections)


def test_spend_without_impressions_uses_the_retained_native_micros() -> None:
    # The cent projection rounds to zero while the retained native micros are
    # nonzero; the rule must still fire.
    rows = [_row(impressions=0, clicks=0, micros="4000", minor_units=0)]
    sections = _sections(rows=rows, inventory=_inventory(_healthy_records()))
    assert "RD_SPEND_WITHOUT_IMPRESSIONS" in _rule_ids(sections)
    quiet = _sections(
        rows=[_row(impressions=0, clicks=0, micros="0", minor_units=0)],
        inventory=_inventory(_healthy_records()),
    )
    assert "RD_SPEND_WITHOUT_IMPRESSIONS" not in _rule_ids(quiet)


def test_spend_without_impressions_falls_back_to_minor_units_when_micros_are_absent() -> None:
    rows = [_row(impressions=0, clicks=0, micros=None, minor_units=3)]
    assert "RD_SPEND_WITHOUT_IMPRESSIONS" in _rule_ids(_sections(rows=rows))


def test_clicks_exceeding_impressions_fires_its_rule() -> None:
    assert "RD_DELIVERY_CLICKS_EXCEED_IMPRESSIONS" in _rule_ids(
        _sections(rows=[_row(impressions=2, clicks=9)])
    )


def test_an_ad_grain_report_does_not_evaluate_campaign_delivery() -> None:
    row = _row().model_copy(
        update={"metric_grain": "AD", "resource_level": "AD", "resource_ref": AD}
    )
    sections = _sections(rows=[row], inventory=_inventory(_healthy_records()))
    assert "RD_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS" not in _rule_ids(sections)


# --- unread dimensions --------------------------------------------------------


def test_every_unread_dimension_emits_its_explicit_finding_when_an_account_is_retained() -> None:
    assert {
        "RD_SPEND_PACING_INPUTS_NOT_READ",
        "RD_BIDS_NOT_READ",
        "RD_TARGETING_NOT_READ",
        "RD_PLACEMENT_NOT_READ",
        "RD_CREATIVE_CONTENT_NOT_READ",
        "RD_DESTINATION_NOT_READ",
        "RD_TRACKING_NOT_READ",
        "RD_NATIVE_CONVERSION_NOT_READ",
        "RD_BROWSER_NOT_APPLICABLE",
        "RD_POLICY_REVIEW_STATE_NOT_READ",
    } <= _rule_ids(_healthy())


def test_unread_dimension_findings_do_not_fire_without_a_retained_account() -> None:
    fired = _rule_ids(_sections())
    assert not fired & {
        "RD_BIDS_NOT_READ",
        "RD_TARGETING_NOT_READ",
        "RD_CREATIVE_CONTENT_NOT_READ",
        "RD_BROWSER_NOT_APPLICABLE",
        "RD_POLICY_REVIEW_STATE_NOT_READ",
    }


def test_targeting_and_creative_findings_need_the_resources_they_describe() -> None:
    campaign_only = _rule_ids(_sections(inventory=_inventory(_healthy_records()[:2])))
    assert "RD_TARGETING_NOT_READ" not in campaign_only
    assert "RD_CREATIVE_CONTENT_NOT_READ" not in campaign_only
    group_only = _rule_ids(_sections(inventory=_inventory(_healthy_records()[:3])))
    assert "RD_TARGETING_NOT_READ" in group_only
    assert "RD_CREATIVE_CONTENT_NOT_READ" not in group_only


# --- objective and creative rules ---------------------------------------------


def test_objective_rules_separate_a_missing_value_from_an_unreviewed_one() -> None:
    assert "RD_OBJECTIVE_ENUM_NOT_REVIEWED" in _rule_ids(_healthy())
    assert "RD_OBJECTIVE_NOT_RETURNED" not in _rule_ids(_healthy())
    records = list(_healthy_records())
    records[1] = _campaign_record(state={"status": "ACTIVE", "currency": "USD"})
    fired = _rule_ids(_sections(inventory=_inventory(tuple(records))))
    assert "RD_OBJECTIVE_NOT_RETURNED" in fired
    assert "RD_OBJECTIVE_ENUM_NOT_REVIEWED" not in fired


def test_local_creative_bindings_are_reported_as_declarations_only() -> None:
    catalog = _catalog(_binding(1, AD), _binding(2, "reddit_ads.ad.absent"), _binding(3, None))
    sections = _sections(inventory=_inventory(_healthy_records()), catalog=catalog)
    unverified = [
        item for item in sections.findings if item.rule_id == "RD_CREATIVE_LOCAL_BINDING_UNVERIFIED"
    ]
    absent = [
        item
        for item in sections.findings
        if item.rule_id == "RD_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE"
    ]
    assert [item.finding.resource_refs for item in unverified] == [("creative:version:1", AD)]
    assert unverified[0].finding.evidence_refs == (_digest(41), _digest(4))
    assert [item.finding.resource_refs for item in absent] == [
        ("creative:version:2", "reddit_ads.ad.absent")
    ]
    # An unbound local creative version produces no Reddit claim at all.
    assert not any("creative:version:3" in item.finding.resource_refs for item in sections.findings)
    assert "RD_CREATIVE_LOCAL_BINDING_UNVERIFIED" not in _rule_ids(_healthy())


def test_a_local_binding_with_no_retained_inventory_is_not_called_absent() -> None:
    sections = _sections(catalog=_catalog(_binding(1, AD)))
    assert not _rule_ids(sections) & {
        "RD_CREATIVE_LOCAL_BINDING_UNVERIFIED",
        "RD_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
    }
    # With nothing to check it against, the binding is not creative coverage.
    assert {item.dimension: item.status for item in sections.coverage}["CREATIVE"] == "NOT_READ"


def test_a_binding_for_another_provider_is_ignored() -> None:
    foreign = RetainedCreativeCatalogEntry(
        creative_version_id="creative:version:foreign",
        record_digest=_digest(60),
        provider="linkedin_ads",
        provider_resource_ref=AD,
        binding_status="LOCAL_DECLARED_UNVERIFIED",
    )
    sections = _sections(inventory=_inventory(_healthy_records()), catalog=_catalog(foreign))
    assert not _rule_ids(sections) & {
        "RD_CREATIVE_LOCAL_BINDING_UNVERIFIED",
        "RD_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE",
    }


# --- drift -------------------------------------------------------------------


def test_drift_reports_an_unknown_actor_and_an_observation_interval() -> None:
    earlier_records = list(_healthy_records(EARLIER_OBSERVED))
    earlier_records[1] = _campaign_record(status="PAUSED", observed_at=EARLIER_OBSERVED, seed=12)
    sections = _sections(
        inventory=_inventory(_healthy_records()),
        earlier=_inventory(tuple(earlier_records), observed_at=EARLIER_OBSERVED),
    )
    assert "RD_DRIFT_STATE_CHANGED" in _rule_ids(sections)
    observation = next(
        item for item in sections.drift_observations if item.change_kind == "STATE_CHANGED"
    )
    assert observation.observed_actor == "UNKNOWN"
    assert observation.observation_interval_start == EARLIER_OBSERVED
    assert observation.observation_interval_end == OBSERVED
    assert observation.changed_paths == ("normalized_state.status",)
    assert observation.before_record_digest == _digest(12)
    assert observation.after_record_digest == _digest(2)
    # Only paths, never values, reach the drift observation.
    assert "PAUSED" not in canonical_bytes(observation).decode()


def test_appearing_and_disappearing_resources_are_reported_separately() -> None:
    earlier = _inventory(_healthy_records(EARLIER_OBSERVED)[:3], observed_at=EARLIER_OBSERVED)
    later = _inventory((_healthy_records()[0], _healthy_records()[1], _healthy_records()[3]))
    sections = _sections(inventory=later, earlier=earlier)
    kinds = {item.change_kind for item in sections.drift_observations}
    assert kinds == {
        "RESOURCE_ABSENT_FROM_LATER_INVENTORY",
        "RESOURCE_ABSENT_FROM_EARLIER_INVENTORY",
    }
    absent_later = next(
        item
        for item in sections.drift_observations
        if item.change_kind == "RESOURCE_ABSENT_FROM_LATER_INVENTORY"
    )
    assert absent_later.observation_interval_start == EARLIER_OBSERVED
    assert absent_later.observation_interval_end == OBSERVED
    assert absent_later.resource_ref == AD_GROUP


def test_drift_does_not_fire_without_a_snapshot_pair() -> None:
    sections = _healthy()
    assert not sections.drift_observations
    assert not _rule_ids(sections) & {
        "RD_DRIFT_STATE_CHANGED",
        "RD_DRIFT_ABSENT_FROM_LATER_INVENTORY",
        "RD_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
    }


def test_absence_drift_is_suppressed_when_a_read_boundary_is_unproven() -> None:
    """Set differences over records with no proven boundary invent both sides."""
    later = _inventory(_healthy_records(), completeness="READ_BOUNDARY_UNVERIFIED")
    earlier = _inventory(
        _healthy_records(EARLIER_OBSERVED)[:2],
        completeness="READ_BOUNDARY_UNVERIFIED",
        observed_at=EARLIER_OBSERVED,
    )
    sections = _sections(inventory=later, earlier=earlier)
    assert not [item for item in sections.drift_observations if item.change_kind != "STATE_CHANGED"]
    assert not _rule_ids(sections) & {
        "RD_DRIFT_ABSENT_FROM_LATER_INVENTORY",
        "RD_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
    }
    drift = next(item for item in sections.coverage if item.dimension == "DRIFT")
    assert drift.status == "PARTIAL"
    assert "no proven read boundary" in drift.rationale


def test_a_state_comparison_still_runs_without_a_proven_read_boundary() -> None:
    earlier_records = list(_healthy_records(EARLIER_OBSERVED))
    earlier_records[1] = _campaign_record(status="PAUSED", observed_at=EARLIER_OBSERVED, seed=12)
    sections = _sections(
        inventory=_inventory(_healthy_records(), completeness="READ_BOUNDARY_UNVERIFIED"),
        earlier=_inventory(
            tuple(earlier_records),
            completeness="READ_BOUNDARY_UNVERIFIED",
            observed_at=EARLIER_OBSERVED,
        ),
    )
    assert "RD_DRIFT_STATE_CHANGED" in _rule_ids(sections)


def test_snapshot_delta_paths_match_the_local_history_snapshot_diff() -> None:
    from adhelm.inventory import _snapshot_deltas

    before = _campaign_record(status="PAUSED").snapshot
    after = _record(
        seed=4,
        resource_type="CAMPAIGN",
        resource_ref=CAMPAIGN,
        parents=("reddit_ads.account.other",),
        state={"status": "ACTIVE", "currency": "USD", "extra": "value"},
    ).snapshot

    def _payload(state: ResourceSnapshotState) -> dict[str, Any]:
        return {
            "normalized_state": dict(state.normalized_state),
            "parent_resource_refs": list(state.parent_resource_refs),
        }

    expected = tuple(item["path"] for item in _snapshot_deltas(_payload(before), _payload(after)))
    assert snapshot_state_deltas(before, after) == expected


# --- recommendations ----------------------------------------------------------


def _idle_sections() -> ProviderAuditSections:
    return _sections(
        rows=[_row(resource_ref="reddit_ads.campaign.999")],
        inventory=_inventory(_healthy_records()),
    )


def test_recommendations_are_bound_to_findings_and_never_execute() -> None:
    sections = _idle_sections()
    assert sections.recommendations
    finding_ids = {item.finding.finding_id for item in sections.findings}
    for recommendation in sections.recommendations:
        assert set(recommendation.finding_ids) <= finding_ids
        assert recommendation.execution_status == "NOT_EXECUTED"
        assert recommendation.plan_status == "NOT_A_PLAN"
        assert recommendation.authorization_status == "NO_AUTHORIZATION_DIGEST_PRESENT"
        assert recommendation.recommendation.proposed_plan_ref is None
        assert recommendation.operation_kind == "OBSERVE"
        assert recommendation.recommendation.expires_at == AS_OF + timedelta(seconds=604_800)


def test_report_derived_recommendations_carry_the_bounded_completeness() -> None:
    delivery = [
        item
        for item in _idle_sections().recommendations
        if "RD_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS" in item.bound_rule_ids
    ]
    assert len(delivery) == 1
    assert delivery[0].analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"


def test_the_only_operation_this_audit_emits_is_observe() -> None:
    """Reddit compiles only a complete new candidate, so no mutation is ever recommended."""
    sections = _sections(
        rows=[_row(impressions=0, clicks=0, micros="9", minor_units=0)],
        inventory=_inventory(_healthy_records()),
        earlier=_inventory((_healthy_records(EARLIER_OBSERVED)[0],), observed_at=EARLIER_OBSERVED),
    )
    assert sections.recommendations
    assert {item.operation_kind for item in sections.recommendations} == {"OBSERVE"}


def test_no_recommendation_outcomes_state_their_reason() -> None:
    sections = _idle_sections()
    reasons = {item.reason for item in sections.no_recommendations}
    assert "COVERAGE_NOT_READ" in reasons
    pause = next(
        item for item in sections.no_recommendations if item.considered_operation_kind == "PAUSE"
    )
    assert pause.reason == "OPERATION_NOT_COMPILABLE_FOR_PROVIDER"
    assert "compiles no Reddit pause" in pause.detail


def test_childless_structure_is_declined_as_a_create_the_compiler_cannot_scope() -> None:
    sections = _sections(inventory=_inventory(_healthy_records()[:2]))
    declined = next(
        item for item in sections.no_recommendations if item.considered_operation_kind == "CREATE"
    )
    assert declined.reason == "OPERATION_NOT_COMPILABLE_FOR_PROVIDER"
    assert declined.bound_rule_ids == (
        "RD_STRUCTURE_AD_GROUP_WITHOUT_AD",
        "RD_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP",
    )


def test_an_uncompilable_operation_cannot_become_a_recommendation() -> None:
    template = _idle_sections().recommendations[0]
    with pytest.raises(ValidationError):
        ProviderRecommendation(
            recommendation=template.recommendation,
            finding_ids=template.finding_ids,
            bound_finding_count=template.bound_finding_count,
            bound_rule_ids=template.bound_rule_ids,
            operation_kind="PAUSE",
            provider="reddit_ads",
            analytics_completeness="NOT_APPLICABLE",
        )


def test_a_recommendation_may_not_carry_a_digest_like_value() -> None:
    template = _idle_sections().recommendations[0]
    poisoned = template.recommendation.model_copy(update={"hypothesis": "Authorize " + "f" * 64})
    with pytest.raises(ValidationError):
        ProviderRecommendation(
            recommendation=poisoned,
            finding_ids=template.finding_ids,
            bound_finding_count=template.bound_finding_count,
            bound_rule_ids=template.bound_rule_ids,
            operation_kind="OBSERVE",
            provider="reddit_ads",
            analytics_completeness="NOT_APPLICABLE",
        )


def test_a_no_recommendation_outcome_requires_a_finding() -> None:
    with pytest.raises(ValidationError):
        NoRecommendation(finding_ids=(), reason="EVIDENCE_INSUFFICIENT", detail="none")


def test_a_recommendation_records_its_own_bound_instead_of_dropping_findings() -> None:
    """Above the inline cap the row still states its true total and its rules."""
    campaigns = MAXIMUM_INLINE_FINDING_IDS + 3
    records: list[RetainedSnapshotRecord] = [
        _record(
            seed=1,
            resource_type="ACCOUNT",
            resource_ref=ACCOUNT,
            state={"status": "NOT_AVAILABLE_FROM_PROVIDER", "currency": "USD"},
            status="NOT_AVAILABLE_FROM_PROVIDER",
        )
    ]
    records.extend(
        _record(
            seed=1000 + index,
            resource_type="CAMPAIGN",
            resource_ref=f"reddit_ads.campaign.{index}",
            parents=(ACCOUNT,),
            state={"status": "ACTIVE", "objective": "TRAFFIC", "currency": "USD"},
        )
        for index in range(campaigns)
    )
    sections = _sections(inventory=_inventory(tuple(records)))
    rule = "RD_STRUCTURE_CAMPAIGN_WITHOUT_AD_GROUP"
    # The subject ends with the first rule of the declined row's membership in
    # content order, which names the exact row.
    entry = next(
        item
        for item in sections.bound_exceeded
        if item.subject
        == "OPERATION_NOT_COMPILABLE_FOR_PROVIDER:CREATE:RD_STRUCTURE_AD_GROUP_WITHOUT_AD"
    )
    assert entry.scope == "DECLINED_FINDING_IDS"
    assert entry.bound == MAXIMUM_INLINE_FINDING_IDS
    assert entry.total_finding_count == campaigns
    assert entry.omitted_finding_count == campaigns - MAXIMUM_INLINE_FINDING_IDS
    assert entry.rule_ids == ("RD_STRUCTURE_AD_GROUP_WITHOUT_AD", rule)
    declined = next(
        item for item in sections.no_recommendations if item.considered_operation_kind == "CREATE"
    )
    assert len(declined.finding_ids) == MAXIMUM_INLINE_FINDING_IDS
    assert declined.bound_finding_count == campaigns
    # Nothing is dropped: every finding is still its own row.
    reachable = {
        item.finding.finding_id
        for item in sections.findings
        if item.rule_id in declined.bound_rule_ids
    }
    assert set(declined.finding_ids) <= reachable
    assert len(reachable) == campaigns


# --- privacy and evidence boundaries -----------------------------------------


def test_findings_and_recommendations_pass_the_privacy_validator() -> None:
    sections = _sections(
        rows=[_row(resource_ref="reddit_ads.campaign.999")],
        inventory=_inventory(_healthy_records()),
        earlier=_inventory(_healthy_records(EARLIER_OBSERVED), observed_at=EARLIER_OBSERVED),
        catalog=_catalog(_binding(1, AD)),
    )
    validate_data(sections.model_dump(mode="json"))
    for item in sections.findings:
        assert item.finding.automatic_write_allowed is False
        for reference in item.finding.evidence_refs:
            assert len(reference) == 64 and set(reference) <= set("0123456789abcdef")
        assert "http" not in item.finding.observation
        assert "?" not in item.finding.observation


def test_coverage_refuses_to_cite_evidence_for_an_unread_dimension() -> None:
    with pytest.raises(ValidationError):
        ProviderAuditCoverage(
            dimension="TARGETING",
            status="NOT_READ",
            evidence_sources=("RETAINED_INVENTORY",),
            evidence_record_count=1,
            rationale="invalid",
        )
    with pytest.raises(ValidationError):
        ProviderAuditCoverage(dimension="DELIVERY", status="PARTIAL", rationale="invalid")


# --- no writes ----------------------------------------------------------------


def test_the_rule_module_performs_no_provider_read_and_no_ledger_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import adhelm.audits.reddit_ads as rules
    from adhelm.ledger import Ledger
    from adhelm.transport import SafeHttpClient

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("A local audit performs no request and appends no record")

    monkeypatch.setattr(SafeHttpClient, "request", _forbidden)
    monkeypatch.setattr(Ledger, "append", _forbidden)
    sections = _sections(inventory=_inventory(_healthy_records()))
    assert sections.live_provider_reads == "NONE_RETAINED_RECORDS_ONLY"
    # The rule module holds no transport, client, or ledger reference at all.
    assert not {
        name
        for name, value in vars(rules).items()
        if isinstance(value, type) and name.endswith(("Client", "Ledger", "SafeHttpClient"))
    }


def test_the_reddit_read_client_exposes_no_mutation_method() -> None:
    exposed = {name for name in dir(RedditAdsClient) if not name.startswith("_")}
    assert exposed == {
        "discover_identity",
        "list_campaigns",
        "list_ad_groups",
        "list_ads",
        "run_campaign_report",
        "run_ad_report",
    }


def test_the_read_surface_table_pins_what_the_fixed_read_returns() -> None:
    # A report is one provider response; its continuation is undocumented, which
    # is what forces every report-derived finding to BOUNDED_NON_EXHAUSTIVE.
    assert REDDIT_ADS_READ_SURFACES["CAMPAIGN_DELIVERY_REPORT"] == (
        "IMPLEMENTED_FIXED_READ_SINGLE_RESPONSE"
    )
    assert REDDIT_ADS_READ_SURFACES["AD_DELIVERY_REPORT"] == (
        "IMPLEMENTED_FIXED_READ_SINGLE_RESPONSE"
    )
    assert REDDIT_ADS_READ_SURFACES["REPORT_CONTINUATION"] == "NOT_DOCUMENTED"
    for surface in (
        "ACCOUNT_SERVING_STATUS",
        "CAMPAIGN_TARGETING",
        "CAMPAIGN_BID_AND_BUDGET",
        "CAMPAIGN_RUN_SCHEDULE",
        "PLACEMENT",
        "AD_CREATIVE_CONTENT",
        "AD_REVIEW_STATE",
        "LANDING_PAGE_AND_UTM",
        "CONVERSION_TRACKING",
        "NATIVE_CONVERSION_METRICS",
    ):
        assert REDDIT_ADS_READ_SURFACES[surface] == "NOT_IN_REVIEWED_FIXED_READ"
