# ruff: noqa: E501
"""Fixture-only tests for the LinkedIn Ads provider audit rule table.

Every case here is offline.  No test performs or simulates a LinkedIn request,
and no case reconciles against the LinkedIn interface.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from adhelm.audit import AuditPolicy, audit_provider, audit_report
from adhelm.audits import (
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
from adhelm.audits.linkedin_ads import RULE_TABLE_VERSION, RULES, evaluate
from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.models import Money
from adhelm.privacy import validate_data
from adhelm.providers.linkedin_ads import LinkedInAdsClient
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope

REPORT_DIGEST = "a" * 64
OBSERVED = datetime(2026, 9, 1, 12, tzinfo=UTC)
EARLIER_OBSERVED = datetime(2026, 8, 25, 12, tzinfo=UTC)
AS_OF = datetime(2026, 9, 1, 18, tzinfo=UTC)
ACCOUNT = "linkedin_ads.account.123"
GROUP = "linkedin_ads.campaign_group.900"
CAMPAIGN = "linkedin_ads.campaign.456"
CREATIVE = "linkedin_ads.creative.789"


def _digest(seed: int) -> str:
    return format(seed, "064x")


def _envelope(provider: str = "linkedin_ads") -> ReportEnvelope:
    return ReportEnvelope(
        report_id="report:linkedin-fixture",
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
        data_quality_statuses=["NOT_AVAILABLE_FROM_PROVIDER"],
        provider_api_version="202608",
        connector_version="test",
    )


def _row(
    *,
    resource_ref: str = CAMPAIGN,
    impressions: int | None = 100,
    clicks: int | None = 4,
    native_spend: str | None = "1.25",
    minor_units: int = 125,
) -> PerformanceRow:
    extensions: dict[str, str | int | bool | None] = {}
    if native_spend is not None:
        extensions = {
            "linkedin_ads_cost_in_local_currency": native_spend,
            "linkedin_ads_rounding": "HALF_EVEN_TO_MINOR",
        }
    return PerformanceRow(
        report_id="report:linkedin-fixture",
        resource_level="CAMPAIGN",
        metric_grain="CAMPAIGN",
        resource_type="LINKEDIN_SPONSORED_CAMPAIGN",
        resource_ref=resource_ref,
        impressions=impressions,
        clicks=clicks,
        spend=Money(minor_units=minor_units, currency="USD"),
        join_status="EXACT_PROVIDER_ID",
        data_quality_statuses=["NOT_AVAILABLE_FROM_PROVIDER"],
        provider_extensions=extensions,
    )


def _report(
    rows: list[PerformanceRow] | None = None, provider: str = "linkedin_ads"
) -> NormalizedReport:
    if rows is None:
        rows = [_row()] if provider == "linkedin_ads" else [_row(native_spend=None)]
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
            provider_api_version="202608",
            resource_type=resource_type,  # type: ignore[arg-type]
            resource_ref=resource_ref,
            parent_resource_refs=parents,
            normalized_state=normalized,
            normalized_state_digest=digest(normalized),
            effective_status=status,
            data_quality_statuses=(),
        ),
    )


def _healthy_records(observed_at: datetime = OBSERVED) -> tuple[RetainedSnapshotRecord, ...]:
    return (
        _record(
            seed=1,
            resource_type="ACCOUNT",
            resource_ref=ACCOUNT,
            state={"status": "ACTIVE", "currency": "USD"},
            observed_at=observed_at,
        ),
        _record(
            seed=2,
            resource_type="CAMPAIGN_GROUP",
            resource_ref=GROUP,
            parents=(ACCOUNT,),
            observed_at=observed_at,
        ),
        _record(
            seed=3,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(GROUP,),
            state={"status": "ACTIVE", "objective": "WEBSITE_VISIT", "currency": "USD"},
            observed_at=observed_at,
        ),
        _record(
            seed=4,
            resource_type="AD",
            resource_ref=CREATIVE,
            parents=(CAMPAIGN,),
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
        provider="linkedin_ads",
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
        provider="linkedin_ads",
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


# --- registry seam -----------------------------------------------------------


def test_registry_dispatches_per_provider_and_leaves_the_rest_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each registered table answers only for its own provider.

    OpenAI Ads, Google Ads, Meta Ads, TikTok Ads and Reddit Ads now have their own
    rule modules, so the registry holds six entries with LinkedIn.  Registering
    them changes nothing for a provider that has none: it keeps returning the
    unchanged report-only audit.
    """
    from adhelm.audits.google_ads import evaluate as evaluate_google
    from adhelm.audits.meta_ads import evaluate as evaluate_meta
    from adhelm.audits.openai_ads import evaluate as evaluate_openai
    from adhelm.audits.reddit_ads import evaluate as evaluate_reddit
    from adhelm.audits.tiktok_ads import evaluate as evaluate_tiktok

    assert provider_rule("linkedin_ads") is evaluate
    assert provider_rule("openai_ads") is evaluate_openai
    assert provider_rule("google_ads") is evaluate_google
    assert provider_rule("meta_ads") is evaluate_meta
    assert provider_rule("tiktok_ads") is evaluate_tiktok
    assert provider_rule("reddit_ads") is evaluate_reddit
    assert provider_rule("linkedin_ads") is not evaluate_openai
    assert provider_rule("linkedin_ads") is not evaluate_google
    assert provider_rule("linkedin_ads") is not evaluate_meta
    assert provider_rule("linkedin_ads") is not evaluate_tiktok
    assert provider_rule("linkedin_ads") is not evaluate_reddit
    # Every advertising provider is registered now, so withdraw one registration
    # (restored on teardown) to prove an unregistered provider is unchanged.
    import adhelm.audits as audits

    monkeypatch.delitem(audits._REGISTERED, "reddit_ads", raising=False)
    monkeypatch.setattr(
        audits,
        "_RULE_MODULES",
        {key: value for key, value in audits._RULE_MODULES.items() if key != "reddit_ads"},
    )
    assert provider_rule("reddit_ads") is None
    report = _report(provider="reddit_ads")
    result = audit_provider(
        report, source_record_digest=REPORT_DIGEST, policy=_policy(), as_of=AS_OF
    )
    assert result.rules_status == "NO_PROVIDER_RULE_MODULE"
    assert result.sections is None
    assert result.audit == audit_report(
        report, source_record_digest=REPORT_DIGEST, policy=_policy(), as_of=AS_OF
    )


def test_provider_audit_embeds_the_unchanged_report_only_audit_result() -> None:
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
    assert result.audit == audit_report(
        report, source_record_digest=REPORT_DIGEST, policy=_policy(), as_of=AS_OF
    )
    # The report-only coverage vocabulary and dimension list are untouched.
    assert {item.dimension for item in result.audit.coverage} == {
        "STRUCTURE",
        "DELIVERY",
        "SPEND",
        "BIDS",
        "TARGETING",
        "PLACEMENT",
        "CREATIVE",
        "DESTINATION_CONFIGURATION",
        "TRACKING",
        "NATIVE_CONVERSION",
        "BROWSER_MEASUREMENT",
        "POLICY",
    }


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


def test_rule_table_evaluates_linkedin_evidence_only() -> None:
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
    catalog = RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:version:one",
                record_digest=_digest(41),
                provider="linkedin_ads",
                provider_resource_ref=CREATIVE,
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
        )
    )
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
    statuses = {item.dimension: item.status for item in partial.coverage}
    assert statuses["STRUCTURE"] == "PARTIAL"
    absence = [
        item
        for item in partial.findings
        if item.rule_id == "LI_STRUCTURE_CAMPAIGN_WITHOUT_CREATIVE"
    ]
    assert [item.finding.confidence for item in absence] == ["LOW"]
    complete = _sections(inventory=_inventory(records[:3]))
    absence = [
        item
        for item in complete.findings
        if item.rule_id == "LI_STRUCTURE_CAMPAIGN_WITHOUT_CREATIVE"
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
    assert all(item.analytics_completeness != "COMPLETE" for item in sections.findings)


# --- structure rules ---------------------------------------------------------


def test_structure_rules_do_not_fire_on_a_consistent_inventory() -> None:
    fired = _rule_ids(_healthy())
    assert not fired & {
        "LI_STRUCTURE_GROUP_WITHOUT_CAMPAIGN",
        "LI_STRUCTURE_CAMPAIGN_WITHOUT_CREATIVE",
        "LI_STRUCTURE_CREATIVE_PARENT_NOT_RETAINED",
        "LI_STRUCTURE_CAMPAIGN_PARENT_NOT_RETAINED",
        "LI_STRUCTURE_SERVING_INTENT_MISMATCH",
        "LI_STRUCTURE_STATUS_NOT_REVIEWED",
    }


def test_empty_campaign_group_fires_its_rule() -> None:
    records = (
        *_healthy_records(),
        _record(
            seed=5,
            resource_type="CAMPAIGN_GROUP",
            resource_ref="linkedin_ads.campaign_group.901",
            parents=(ACCOUNT,),
        ),
    )
    sections = _sections(inventory=_inventory(records))
    fired = [
        item for item in sections.findings if item.rule_id == "LI_STRUCTURE_GROUP_WITHOUT_CAMPAIGN"
    ]
    assert [item.finding.resource_refs for item in fired] == [("linkedin_ads.campaign_group.901",)]
    assert fired[0].finding.evidence_refs == (_digest(5),)


def test_campaign_without_creative_fires_its_rule() -> None:
    records = _healthy_records()[:3]
    sections = _sections(inventory=_inventory(records))
    assert "LI_STRUCTURE_CAMPAIGN_WITHOUT_CREATIVE" in _rule_ids(sections)


def test_orphaned_creative_and_campaign_fire_their_rules() -> None:
    records = (
        _healthy_records()[0],
        _record(
            seed=3,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=("linkedin_ads.campaign_group.missing",),
            state={"status": "ACTIVE", "objective": "WEBSITE_VISIT", "currency": "USD"},
        ),
        _record(
            seed=4,
            resource_type="AD",
            resource_ref=CREATIVE,
            parents=("linkedin_ads.campaign.missing",),
        ),
    )
    fired = _rule_ids(_sections(inventory=_inventory(records)))
    assert "LI_STRUCTURE_CAMPAIGN_PARENT_NOT_RETAINED" in fired
    assert "LI_STRUCTURE_CREATIVE_PARENT_NOT_RETAINED" in fired


def test_parent_child_status_mismatch_fires_and_cites_both_records() -> None:
    records = list(_healthy_records())
    records[1] = _record(
        seed=2,
        resource_type="CAMPAIGN_GROUP",
        resource_ref=GROUP,
        parents=(ACCOUNT,),
        status="PAUSED",
    )
    sections = _sections(inventory=_inventory(tuple(records)))
    fired = [
        item for item in sections.findings if item.rule_id == "LI_STRUCTURE_SERVING_INTENT_MISMATCH"
    ]
    assert len(fired) == 1
    assert fired[0].finding.resource_refs == (CAMPAIGN, GROUP)
    assert fired[0].finding.evidence_refs == (_digest(3), _digest(2))


def test_unreviewed_status_value_is_surfaced_and_creative_status_is_not_guessed() -> None:
    records = list(_healthy_records())
    records[2] = _record(
        seed=3,
        resource_type="CAMPAIGN",
        resource_ref=CAMPAIGN,
        parents=(GROUP,),
        state={"status": "SOME_NEW_STATUS", "objective": "WEBSITE_VISIT", "currency": "USD"},
        status="SOME_NEW_STATUS",
    )
    records[3] = _record(
        seed=4,
        resource_type="AD",
        resource_ref=CREATIVE,
        parents=(CAMPAIGN,),
        status="UNREVIEWED_CREATIVE_STATUS",
    )
    sections = _sections(inventory=_inventory(tuple(records)))
    fired = [
        item for item in sections.findings if item.rule_id == "LI_STRUCTURE_STATUS_NOT_REVIEWED"
    ]
    # The campaign status enumeration is reviewed; the creative one is not, so
    # the creative value is never classified as unsupported.
    assert [item.finding.resource_refs for item in fired] == [(CAMPAIGN,)]


# --- delivery and spend rules -------------------------------------------------


def test_delivery_rules_do_not_fire_on_a_consistent_report() -> None:
    fired = _rule_ids(_healthy())
    assert not fired & {
        "LI_DELIVERY_CLICKS_EXCEED_IMPRESSIONS",
        "LI_DELIVERY_METRICS_MISSING",
        "LI_SPEND_WITHOUT_IMPRESSIONS",
        "LI_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS",
    }


def test_zero_impression_active_campaign_fires_with_bounded_completeness() -> None:
    sections = _sections(
        rows=[_row(resource_ref="linkedin_ads.campaign.999")],
        inventory=_inventory(_healthy_records()),
    )
    fired = [
        item
        for item in sections.findings
        if item.rule_id == "LI_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS"
    ]
    assert len(fired) == 1
    assert fired[0].analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"
    assert fired[0].finding.confidence == "LOW"
    assert fired[0].finding.evidence_refs == (_digest(3), REPORT_DIGEST)
    assert "not proof of zero delivery" in fired[0].finding.observation


def test_spend_without_impressions_uses_the_retained_native_decimal() -> None:
    # The cent projection rounds to zero while the retained native decimal is
    # nonzero; the rule must still fire.
    rows = [_row(impressions=0, clicks=0, native_spend="0.004", minor_units=0)]
    sections = _sections(rows=rows, inventory=_inventory(_healthy_records()))
    assert "LI_SPEND_WITHOUT_IMPRESSIONS" in _rule_ids(sections)
    quiet = _sections(
        rows=[_row(impressions=0, clicks=0, native_spend="0", minor_units=0)],
        inventory=_inventory(_healthy_records()),
    )
    assert "LI_SPEND_WITHOUT_IMPRESSIONS" not in _rule_ids(quiet)


def test_click_and_metric_anomalies_fire_their_rules() -> None:
    assert "LI_DELIVERY_CLICKS_EXCEED_IMPRESSIONS" in _rule_ids(
        _sections(rows=[_row(impressions=2, clicks=9)])
    )
    assert "LI_DELIVERY_METRICS_MISSING" in _rule_ids(
        _sections(rows=[_row(impressions=None, clicks=None)])
    )


def test_an_ad_grain_report_does_not_evaluate_campaign_delivery() -> None:
    row = _row().model_copy(
        update={"metric_grain": "AD", "resource_level": "AD", "resource_ref": CREATIVE}
    )
    sections = _sections(rows=[row], inventory=_inventory(_healthy_records()))
    assert "LI_DELIVERY_ACTIVE_CAMPAIGN_WITHOUT_IMPRESSIONS" not in _rule_ids(sections)


# --- unread dimensions --------------------------------------------------------


def test_every_unread_dimension_emits_its_explicit_finding_when_an_account_is_retained() -> None:
    fired = _rule_ids(_healthy())
    assert {
        "LI_SPEND_PACING_INPUTS_NOT_READ",
        "LI_BIDS_NOT_READ",
        "LI_TARGETING_NOT_READ",
        "LI_PLACEMENT_NOT_READ",
        "LI_CREATIVE_CONTENT_NOT_READ",
        "LI_DESTINATION_NOT_READ",
        "LI_TRACKING_NOT_READ",
        "LI_NATIVE_CONVERSION_NOT_READ",
        "LI_BROWSER_NOT_APPLICABLE",
        "LI_POLICY_REVIEW_STATE_NOT_READ",
    } <= fired


def test_unread_dimension_findings_do_not_fire_without_a_retained_account() -> None:
    fired = _rule_ids(_sections())
    assert not fired & {
        "LI_BIDS_NOT_READ",
        "LI_TARGETING_NOT_READ",
        "LI_CREATIVE_CONTENT_NOT_READ",
        "LI_BROWSER_NOT_APPLICABLE",
        "LI_POLICY_REVIEW_STATE_NOT_READ",
    }


def test_creative_content_finding_needs_a_retained_creative() -> None:
    assert "LI_CREATIVE_CONTENT_NOT_READ" not in _rule_ids(
        _sections(inventory=_inventory(_healthy_records()[:3]))
    )


# --- objective and creative rules ---------------------------------------------


def test_objective_rules_separate_a_missing_value_from_an_unreviewed_one() -> None:
    assert "LI_OBJECTIVE_ENUM_NOT_REVIEWED" in _rule_ids(_healthy())
    assert "LI_OBJECTIVE_NOT_RETURNED" not in _rule_ids(_healthy())
    records = list(_healthy_records())
    records[2] = _record(
        seed=3,
        resource_type="CAMPAIGN",
        resource_ref=CAMPAIGN,
        parents=(GROUP,),
        state={"status": "ACTIVE", "currency": "USD"},
    )
    fired = _rule_ids(_sections(inventory=_inventory(tuple(records))))
    assert "LI_OBJECTIVE_NOT_RETURNED" in fired
    assert "LI_OBJECTIVE_ENUM_NOT_REVIEWED" not in fired


def test_missing_creative_status_fires_and_a_present_status_does_not() -> None:
    assert "LI_CREATIVE_STATUS_NOT_RETURNED" not in _rule_ids(_healthy())
    records = list(_healthy_records())
    records[3] = _record(
        seed=4,
        resource_type="AD",
        resource_ref=CREATIVE,
        parents=(CAMPAIGN,),
        state={"currency": "USD"},
        status="NOT_AVAILABLE_FROM_PROVIDER",
    )
    assert "LI_CREATIVE_STATUS_NOT_RETURNED" in _rule_ids(
        _sections(inventory=_inventory(tuple(records)))
    )


def test_local_creative_bindings_are_reported_as_declarations_only() -> None:
    catalog = RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:version:one",
                record_digest=_digest(41),
                provider="linkedin_ads",
                provider_resource_ref=CREATIVE,
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:version:two",
                record_digest=_digest(42),
                provider="linkedin_ads",
                provider_resource_ref="linkedin_ads.creative.absent",
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:version:three",
                record_digest=_digest(43),
                provider="linkedin_ads",
                binding_status="NOT_CREATED",
            ),
        )
    )
    fired = _rule_ids(_sections(inventory=_inventory(_healthy_records()), catalog=catalog))
    assert "LI_CREATIVE_LOCAL_BINDING_UNVERIFIED" in fired
    assert "LI_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE" in fired
    # An unbound local creative version produces no LinkedIn claim at all.
    assert not fired & {"LI_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE"} - {
        "LI_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE"
    }
    assert "LI_CREATIVE_LOCAL_BINDING_UNVERIFIED" not in _rule_ids(_healthy())


# --- drift -------------------------------------------------------------------


def test_drift_reports_an_unknown_actor_and_an_observation_interval() -> None:
    earlier_records = list(_healthy_records(EARLIER_OBSERVED))
    earlier_records[2] = _record(
        seed=13,
        resource_type="CAMPAIGN",
        resource_ref=CAMPAIGN,
        parents=(GROUP,),
        state={"status": "PAUSED", "objective": "WEBSITE_VISIT", "currency": "USD"},
        status="PAUSED",
        observed_at=EARLIER_OBSERVED,
    )
    sections = _sections(
        inventory=_inventory(_healthy_records()),
        earlier=_inventory(tuple(earlier_records), observed_at=EARLIER_OBSERVED),
    )
    assert "LI_DRIFT_STATE_CHANGED" in _rule_ids(sections)
    observation = next(
        item for item in sections.drift_observations if item.change_kind == "STATE_CHANGED"
    )
    assert observation.observed_actor == "UNKNOWN"
    assert observation.observation_interval_start == EARLIER_OBSERVED
    assert observation.observation_interval_end == OBSERVED
    assert observation.changed_paths == ("normalized_state.status",)
    assert observation.before_record_digest == _digest(13)
    assert observation.after_record_digest == _digest(3)
    # Only paths, never values, reach the drift observation.
    assert "PAUSED" not in canonical_bytes(observation).decode()


def test_appearing_and_disappearing_resources_are_reported_separately() -> None:
    earlier = _inventory(_healthy_records(EARLIER_OBSERVED)[:3], observed_at=EARLIER_OBSERVED)
    later = _inventory(_healthy_records()[1:])
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


def test_drift_does_not_fire_without_a_snapshot_pair() -> None:
    sections = _healthy()
    assert not sections.drift_observations
    assert not _rule_ids(sections) & {
        "LI_DRIFT_STATE_CHANGED",
        "LI_DRIFT_ABSENT_FROM_LATER_INVENTORY",
        "LI_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
    }


def test_snapshot_delta_paths_match_the_local_history_snapshot_diff() -> None:
    from adhelm.inventory import _snapshot_deltas

    before = _record(
        seed=3,
        resource_type="CAMPAIGN",
        resource_ref=CAMPAIGN,
        parents=(GROUP,),
        state={"status": "PAUSED", "objective": "WEBSITE_VISIT", "currency": "USD"},
        status="PAUSED",
    ).snapshot
    after = _record(
        seed=4,
        resource_type="CAMPAIGN",
        resource_ref=CAMPAIGN,
        parents=("linkedin_ads.campaign_group.other",),
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


def test_recommendations_are_bound_to_findings_and_never_execute() -> None:
    sections = _sections(
        rows=[_row(resource_ref="linkedin_ads.campaign.999")],
        inventory=_inventory(_healthy_records()),
    )
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
    sections = _sections(
        rows=[_row(resource_ref="linkedin_ads.campaign.999")],
        inventory=_inventory(_healthy_records()),
    )
    delivery = [
        item
        for item in sections.recommendations
        if "not exhaustive" in item.recommendation.observations[0]
        or item.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE"
    ]
    assert delivery
    assert all(item.analytics_completeness == "BOUNDED_NON_EXHAUSTIVE" for item in delivery)


def test_no_recommendation_outcomes_state_their_reason() -> None:
    sections = _sections(
        rows=[_row(resource_ref="linkedin_ads.campaign.999")],
        inventory=_inventory(_healthy_records()),
    )
    reasons = {item.reason for item in sections.no_recommendations}
    assert "COVERAGE_NOT_READ" in reasons
    assert "OPERATION_NOT_COMPILABLE_FOR_PROVIDER" in reasons
    pause = next(
        item for item in sections.no_recommendations if item.considered_operation_kind == "PAUSE"
    )
    assert pause.reason == "OPERATION_NOT_COMPILABLE_FOR_PROVIDER"
    assert "compiles no LinkedIn pause" in pause.detail


def test_an_uncompilable_operation_cannot_become_a_recommendation() -> None:
    template = _sections(
        rows=[_row(resource_ref="linkedin_ads.campaign.999")],
        inventory=_inventory(_healthy_records()),
    ).recommendations[0]
    with pytest.raises(ValidationError):
        ProviderRecommendation(
            recommendation=template.recommendation,
            finding_ids=template.finding_ids,
            operation_kind="PAUSE",
            provider="linkedin_ads",
            analytics_completeness="NOT_APPLICABLE",
        )


def test_a_recommendation_may_not_carry_a_digest_like_value() -> None:
    template = _sections(
        rows=[_row(resource_ref="linkedin_ads.campaign.999")],
        inventory=_inventory(_healthy_records()),
    ).recommendations[0]
    poisoned = template.recommendation.model_copy(update={"hypothesis": "Authorize " + "f" * 64})
    with pytest.raises(ValidationError):
        ProviderRecommendation(
            recommendation=poisoned,
            finding_ids=template.finding_ids,
            operation_kind="OBSERVE",
            provider="linkedin_ads",
            analytics_completeness="NOT_APPLICABLE",
        )


def test_a_no_recommendation_outcome_requires_a_finding() -> None:
    with pytest.raises(ValidationError):
        NoRecommendation(finding_ids=(), reason="EVIDENCE_INSUFFICIENT", detail="none")


# --- privacy and evidence boundaries -----------------------------------------


def test_findings_and_recommendations_pass_the_privacy_validator() -> None:
    catalog = RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:version:one",
                record_digest=_digest(41),
                provider="linkedin_ads",
                provider_resource_ref=CREATIVE,
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
        )
    )
    sections = _sections(
        rows=[_row(resource_ref="linkedin_ads.campaign.999")],
        inventory=_inventory(_healthy_records()),
        earlier=_inventory(_healthy_records(EARLIER_OBSERVED), observed_at=EARLIER_OBSERVED),
        catalog=catalog,
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import adhelm.audits.linkedin_ads as rules
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


def test_the_linkedin_read_client_still_exposes_no_mutation_method() -> None:
    exposed = {name for name in dir(LinkedInAdsClient) if not name.startswith("__")}
    assert not exposed & {
        "post",
        "put",
        "patch",
        "delete",
        "create",
        "update",
        "pause",
        "activate",
        "archive",
        "mutate",
    }
    assert all("write" not in name and "mutate" not in name for name in exposed)


def test_the_read_surface_table_pins_what_the_fixed_read_returns() -> None:
    from adhelm.providers.linkedin_ads import LINKEDIN_ADS_READ_SURFACES

    # A creative-level delivery read exists: the public AD grain compiles to
    # LinkedIn's documented CREATIVE reporting pivot on the same finder.
    assert (
        LINKEDIN_ADS_READ_SURFACES["CREATIVE_DELIVERY_REPORT"]
        == "IMPLEMENTED_FIXED_READ_BOUNDED_NON_EXHAUSTIVE"
    )
    # Pagination for that finder is undocumented, which is what forces every
    # report-derived finding to BOUNDED_NON_EXHAUSTIVE.
    assert LINKEDIN_ADS_READ_SURFACES["ANALYTICS_PAGINATION"] == "NOT_DOCUMENTED"
    for surface in (
        "CAMPAIGN_TARGETING_CRITERIA",
        "CAMPAIGN_BID_AND_BUDGET",
        "CAMPAIGN_RUN_SCHEDULE",
        "PLACEMENT",
        "CREATIVE_CONTENT",
        "CREATIVE_REVIEW_STATE",
        "LANDING_PAGE_AND_UTM",
        "CONVERSION_TRACKING",
        "NATIVE_CONVERSION_METRICS",
    ):
        assert LINKEDIN_ADS_READ_SURFACES[surface] == "NOT_IN_REVIEWED_FIXED_READ"


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
        "LI_DRIFT_ABSENT_FROM_LATER_INVENTORY",
        "LI_DRIFT_ABSENT_FROM_EARLIER_INVENTORY",
    }
    drift = next(item for item in sections.coverage if item.dimension == "DRIFT")
    assert drift.status == "PARTIAL"
    assert "no proven read boundary" in drift.rationale


def test_a_state_comparison_still_runs_without_a_proven_read_boundary() -> None:
    """A per-resource change is a real observation even when a boundary is not."""
    records = list(_healthy_records())
    earlier_records = list(_healthy_records(EARLIER_OBSERVED))
    earlier_records[2] = _record(
        seed=13,
        resource_type="CAMPAIGN",
        resource_ref=CAMPAIGN,
        parents=(GROUP,),
        state={"status": "PAUSED", "objective": "WEBSITE_VISIT", "currency": "USD"},
        status="PAUSED",
        observed_at=EARLIER_OBSERVED,
    )
    sections = _sections(
        inventory=_inventory(tuple(records), completeness="READ_BOUNDARY_UNVERIFIED"),
        earlier=_inventory(
            tuple(earlier_records),
            completeness="READ_BOUNDARY_UNVERIFIED",
            observed_at=EARLIER_OBSERVED,
        ),
    )
    assert "LI_DRIFT_STATE_CHANGED" in _rule_ids(sections)


def test_a_recommendation_records_its_own_bound_instead_of_dropping_findings() -> None:
    """Above the inline cap the row still states its true total and its rules."""
    from adhelm.audits import MAXIMUM_INLINE_FINDING_IDS

    campaigns = MAXIMUM_INLINE_FINDING_IDS + 3
    records: list[RetainedSnapshotRecord] = [
        _record(
            seed=1,
            resource_type="ACCOUNT",
            resource_ref=ACCOUNT,
            state={"status": "ACTIVE", "currency": "USD"},
        ),
        _record(seed=2, resource_type="CAMPAIGN_GROUP", resource_ref=GROUP, parents=(ACCOUNT,)),
    ]
    records.extend(
        _record(
            seed=1000 + index,
            resource_type="CAMPAIGN",
            resource_ref=f"linkedin_ads.campaign.{index}",
            parents=(GROUP,),
            state={"status": "ACTIVE", "objective": "WEBSITE_VISIT", "currency": "USD"},
        )
        for index in range(campaigns)
    )
    sections = _sections(inventory=_inventory(tuple(records)))
    entry = next(
        item
        for item in sections.bound_exceeded
        if item.subject == "OPERATION_NOT_COMPILABLE_FOR_PROVIDER:ASSOCIATE"
    )
    assert entry.scope == "DECLINED_FINDING_IDS"
    assert entry.bound == MAXIMUM_INLINE_FINDING_IDS
    assert entry.total_finding_count == campaigns
    assert entry.inlined_finding_count == MAXIMUM_INLINE_FINDING_IDS
    assert entry.omitted_finding_count == campaigns - MAXIMUM_INLINE_FINDING_IDS
    assert entry.rule_ids == ("LI_STRUCTURE_CAMPAIGN_WITHOUT_CREATIVE",)
    assert entry.resolution == "PAGE_FINDINGS_AND_MATCH_RULE_ID"
    declined = next(
        item
        for item in sections.no_recommendations
        if item.considered_operation_kind == "ASSOCIATE"
    )
    assert len(declined.finding_ids) == MAXIMUM_INLINE_FINDING_IDS
    assert declined.bound_finding_count == campaigns
    assert declined.bound_rule_ids == ("LI_STRUCTURE_CAMPAIGN_WITHOUT_CREATIVE",)
    # Nothing is dropped: every finding above the inline cap is still its own
    # row, reachable by paging and matching the rule identifiers the row names.
    reachable = {
        item.finding.finding_id
        for item in sections.findings
        if item.rule_id in declined.bound_rule_ids
    }
    assert set(declined.finding_ids) <= reachable
    assert len(reachable) == campaigns
    accounted = {
        finding_id
        for item in (*sections.recommendations, *sections.no_recommendations)
        for finding_id in item.finding_ids
    } | {
        item.finding.finding_id
        for item in sections.findings
        for row in (*sections.recommendations, *sections.no_recommendations)
        if item.rule_id in row.bound_rule_ids
    }
    assert {item.finding.finding_id for item in sections.findings} <= accounted
