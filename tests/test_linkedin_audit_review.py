# ruff: noqa: E501
"""Independent review of the P01-LI.2 LinkedIn provider audit rule table.

Every case is offline.  No test performs or simulates a LinkedIn request, and
no case reconciles against the LinkedIn interface.  Each test states the
behaviour the audit contract and `AGENTS.md` require; a failure here is a
finding against `src/adhelm/audits/`, never a request to weaken the contract.
"""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from adhelm.audit import AuditPolicy, build_recommendation
from adhelm.audits import (
    DriftObservation,
    ProviderAuditContext,
    ProviderAuditEvidence,
    ProviderAuditSections,
    ProviderRecommendation,
    ResourceSnapshotState,
    RetainedCreativeCatalog,
    RetainedCreativeCatalogEntry,
    RetainedInventory,
    RetainedSnapshotRecord,
)
from adhelm.audits.linkedin_ads import evaluate
from adhelm.canonical import digest
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope

ACCOUNT = "linkedin_ads.account.123"
GROUP = "linkedin_ads.campaign_group.900"
CAMPAIGN = "linkedin_ads.campaign.456"
CREATIVE = "linkedin_ads.creative.789"
OBSERVED = datetime(2026, 9, 1, 12, tzinfo=UTC)
EARLIER_OBSERVED = datetime(2026, 8, 25, 12, tzinfo=UTC)
AS_OF = datetime(2026, 9, 1, 18, tzinfo=UTC)
REPORT_DIGEST = "a" * 64


def _digest(seed: int) -> str:
    return format(seed, "064x")


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


def _envelope() -> ReportEnvelope:
    return ReportEnvelope(
        report_id="report:linkedin-review",
        provider="linkedin_ads",
        source_account_ref=ACCOUNT,
        query_contract_hash="b" * 64,
        window_start=date(2026, 8, 25),
        window_end=date(2026, 8, 31),
        source_timezone="UTC",
        report_timezone="UTC",
        currency="USD",
        attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
        observed_at=OBSERVED,
        data_quality_statuses=["NOT_AVAILABLE_FROM_PROVIDER"],
        provider_api_version="202608",
        connector_version="test",
    )


def _report(rows: list[PerformanceRow] | None = None) -> NormalizedReport:
    return NormalizedReport(envelope=_envelope(), performance_rows=rows or [])


def _policy() -> AuditPolicy:
    return AuditPolicy(
        maximum_observation_age_seconds=604_800,
        maximum_cpc=None,
        minimum_impressions=None,
        require_conversion_evidence=False,
    )


def _sections(
    *,
    rows: list[PerformanceRow] | None = None,
    inventory: RetainedInventory | None = None,
    earlier: RetainedInventory | None = None,
    catalog: RetainedCreativeCatalog | None = None,
) -> ProviderAuditSections:
    return evaluate(
        ProviderAuditContext(
            provider="linkedin_ads",
            report=_report(rows),
            source_record_digest=REPORT_DIGEST,
            policy=_policy(),
            as_of=AS_OF,
            evidence=ProviderAuditEvidence(
                inventory=inventory, earlier_inventory=earlier, creative_catalog=catalog
            ),
        )
    )


def _many_campaign_records(
    count: int, *, status: str, observed_at: datetime
) -> tuple[RetainedSnapshotRecord, ...]:
    records = [
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
    ]
    for index in range(count):
        records.append(
            _record(
                seed=100 + index,
                resource_type="CAMPAIGN",
                resource_ref=f"linkedin_ads.campaign.{index}",
                parents=(GROUP,),
                state={"status": status, "objective": "WEBSITE_VISIT", "currency": "USD"},
                status=status,
                observed_at=observed_at,
            )
        )
    return tuple(records)


# --- Finding 1: a recommendation cannot bind more than sixteen findings ------


def test_a_recommendation_binds_every_finding_it_rests_on_beyond_sixteen() -> None:
    """Seventeen out-of-band campaign changes must not crash the rule table.

    `_recommend._observe` passes every bound finding identifier to
    `ProviderRecommendation.finding_ids`, which caps at sixteen items.  An
    account with seventeen changed campaigns therefore raises `ValidationError`
    from inside the audit instead of producing a bounded, honest result.
    A rule table must either bind every finding or state its own bound; it may
    never fail on the size of the customer's account.
    """
    earlier = _inventory(
        _many_campaign_records(17, status="PAUSED", observed_at=EARLIER_OBSERVED),
        observed_at=EARLIER_OBSERVED,
    )
    later = _inventory(_many_campaign_records(17, status="ACTIVE", observed_at=OBSERVED))
    sections = _sections(inventory=later, earlier=earlier)
    drift = [item for item in sections.recommendations if item.operation_kind == "OBSERVE"]
    assert drift, "the drift observation recommendation is still produced"


def test_declined_findings_are_all_named_and_never_silently_truncated() -> None:
    """Every finding gets a recommendation or an explicit reasoned absence.

    `_recommend._no_recommendation` slices its bound identifiers with
    `bound[:16]`.  With more than sixteen matching findings the surplus
    findings appear in no recommendation and in no `NoRecommendation`, so the
    output silently drops the record that they were considered at all.
    """
    records = _many_campaign_records(20, status="ACTIVE", observed_at=OBSERVED)
    sections = _sections(inventory=_inventory(records))
    accounted = {
        finding_id
        for item in (*sections.recommendations, *sections.no_recommendations)
        for finding_id in item.finding_ids
    }
    unaccounted = sorted(
        item.rule_id for item in sections.findings if item.finding.finding_id not in accounted
    )
    assert unaccounted == [], (
        f"every finding is recommended on or explicitly declined; these were dropped: {unaccounted}"
    )


# --- Finding 2: drift over a declared subset is not covered ------------------


def test_drift_over_a_declared_subset_is_not_reported_as_covered() -> None:
    """A `BOUNDED_NON_EXHAUSTIVE` snapshot pair cannot cover the drift dimension.

    `_coverage_map` marks `DRIFT` `COVERED` whenever an earlier inventory is
    supplied, regardless of either inventory's declared completeness.  The
    structure rules already downgrade absence-based confidence to `LOW` for a
    declared subset; the drift dimension must be at most `PARTIAL` for the same
    evidence, because a resource missing from a declared subset was never
    proven absent from the account.
    """
    complete = _many_campaign_records(1, status="ACTIVE", observed_at=OBSERVED)
    subset = complete[:2]
    sections = _sections(
        inventory=_inventory(complete, completeness="BOUNDED_NON_EXHAUSTIVE"),
        earlier=_inventory(
            subset, completeness="BOUNDED_NON_EXHAUSTIVE", observed_at=EARLIER_OBSERVED
        ),
    )
    status = next(item.status for item in sections.coverage if item.dimension == "DRIFT")
    assert status != "COVERED", "a declared subset pair cannot cover drift"


def test_absence_drift_from_a_declared_subset_is_low_confidence() -> None:
    """An absence rule over a declared subset carries the same caveat everywhere.

    `_structure_findings` lowers absence confidence to `LOW` when the retained
    inventory is `BOUNDED_NON_EXHAUSTIVE`.  `_drift` uses the rule table's fixed
    `MEDIUM` confidence for `RESOURCE_ABSENT_FROM_*` regardless, so identical
    evidence yields two different confidence claims.
    """
    complete = _many_campaign_records(1, status="ACTIVE", observed_at=OBSERVED)
    subset = complete[:2]
    sections = _sections(
        inventory=_inventory(complete, completeness="BOUNDED_NON_EXHAUSTIVE"),
        earlier=_inventory(
            subset, completeness="BOUNDED_NON_EXHAUSTIVE", observed_at=EARLIER_OBSERVED
        ),
    )
    absence = [
        item
        for item in sections.findings
        if item.rule_id == "LI_DRIFT_ABSENT_FROM_EARLIER_INVENTORY"
    ]
    assert absence, "the fixture produces an appearing resource"
    assert [item.finding.confidence for item in absence] == ["LOW"] * len(absence)


# --- Finding 3: creative-catalog evidence is missing from the coverage map ---


def test_creative_coverage_names_the_catalog_it_actually_evaluated() -> None:
    """A dimension that produced findings from evidence cannot report `NOT_READ`.

    With a local creative catalog but no retained `AD` snapshot,
    `_coverage_map` reports `CREATIVE` as `NOT_READ` with no evidence source
    and a zero record count, while `_creative_findings` still emits
    `LI_CREATIVE_LOCAL_BINDING_WITHOUT_RESOURCE` citing the deployment record
    digest.  The coverage entry must name the local creative catalog it read.
    """
    catalog = RetainedCreativeCatalog(
        entries=(
            RetainedCreativeCatalogEntry(
                creative_version_id="creative:sha256:" + "c" * 64,
                record_digest=_digest(77),
                provider="linkedin_ads",
                provider_resource_ref=CREATIVE,
                binding_status="LOCAL_DECLARED_UNVERIFIED",
            ),
        )
    )
    records = _many_campaign_records(1, status="ACTIVE", observed_at=OBSERVED)
    sections = _sections(inventory=_inventory(records), catalog=catalog)
    creative_findings = [item for item in sections.findings if item.dimension == "CREATIVE"]
    coverage = next(item for item in sections.coverage if item.dimension == "CREATIVE")
    assert creative_findings, "the fixture produces a creative-catalog finding"
    assert coverage.status not in {"NOT_READ", "NOT_APPLICABLE"}, (
        "a dimension with findings drawn from retained evidence is not unread"
    )
    assert "LOCAL_CREATIVE_CATALOG" in coverage.evidence_sources
    assert coverage.evidence_record_count >= 1


# --- Finding 4: model invariants the package docstring promises --------------


def test_no_provider_audit_model_accepts_an_exhaustive_analytics_claim() -> None:
    """`COMPLETE` is rejected everywhere, not only on a finding.

    `ProviderAuditFinding` refuses `analytics_completeness="COMPLETE"` because
    no reviewed analytics read is proven exhaustive.  `ProviderAuditSections`
    and `ProviderRecommendation` accept it, so a future rule module can publish
    an exhaustive claim through either of them.
    """
    recommendation = build_recommendation(
        hypothesis="A repeated bounded read may resolve this row.",
        evidence_refs=(REPORT_DIGEST,),
        observations=("A retained row is inconsistent.",),
        assumptions=(),
        confidence="LOW",
        expected_impact="A second compatible read resolves the row.",
        risk="LOW",
        expires_at=AS_OF,
        identity={"rule_table_version": "review"},
    )
    with pytest.raises(ValidationError):
        ProviderRecommendation(
            recommendation=recommendation,
            finding_ids=("finding:sha256:" + "b" * 64,),
            operation_kind="OBSERVE",
            provider="linkedin_ads",
            analytics_completeness="COMPLETE",
        )
    with pytest.raises(ValidationError):
        ProviderAuditSections(
            provider="linkedin_ads",
            rule_table_version="review",
            analytics_completeness="COMPLETE",
        )


def test_provider_audit_sections_require_a_status_for_every_dimension() -> None:
    """Invariant 2 of `adhelm.audits` is enforced by the model, not by convention.

    The package docstring promises that every audit dimension gets an explicit
    coverage status and that a dimension is "never quietly omitted".
    `ProviderAuditSections` only checks that the statuses it was given are
    unique, so a rule module that omits a dimension entirely validates.
    """
    with pytest.raises(ValidationError):
        ProviderAuditSections(
            provider="linkedin_ads",
            rule_table_version="review",
            analytics_completeness="BOUNDED_NON_EXHAUSTIVE",
        )


def test_a_state_change_observation_carries_both_record_digests() -> None:
    """A `STATE_CHANGED` observation cites the two snapshots it compared.

    `DriftObservation` requires changed paths for a state change but leaves
    both record digests optional, so an observation can claim an out-of-band
    change while citing no retained snapshot at all.
    """
    with pytest.raises(ValidationError):
        DriftObservation(
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            change_kind="STATE_CHANGED",
            changed_paths=("normalized_state.status",),
            observation_interval_start=EARLIER_OBSERVED,
            observation_interval_end=OBSERVED,
            finding_id="finding:sha256:" + "b" * 64,
        )


# --- Finding: the compilable-operation table under-declares one provider -----


def test_every_shipped_compiler_appears_in_the_compilable_operation_table() -> None:
    """`COMPILABLE_OPERATION_KINDS` states what this build can actually compile.

    The table gates whether a rule module may name an operation kind at all, so
    an entry that omits a shipped compiler silently forbids an honest
    recommendation.  `compile openai-ads` produces the same kind of offline
    paused `CREATE` candidate as the other five compilers, but the OpenAI entry
    lists `PAUSE` only.
    """
    from adhelm.audits import COMPILABLE_OPERATION_KINDS
    from adhelm.cli import _COMMANDS

    compilers = {
        command.removeprefix("compile ").replace("-", "_")
        for command in _COMMANDS
        if command.startswith("compile ") and command != "compile strategy"
    }
    missing = sorted(
        provider
        for provider in compilers
        if "CREATE" not in COMPILABLE_OPERATION_KINDS.get(provider, frozenset())  # type: ignore[arg-type]
    )
    assert missing == [], f"a shipped compiler is not declared compilable: {missing}"


# --- Re-review: the smallest page the route can serve must fit the bound -----


def _campaign_records(
    count: int, *, status: str, observed_at: datetime
) -> tuple[RetainedSnapshotRecord, ...]:
    return _many_campaign_records(count, status=status, observed_at=observed_at)


def test_the_smallest_provider_audit_page_fits_the_default_output_bound() -> None:
    """A large account must be servable one row at a time.

    `MAXIMUM_BOUND_FINDINGS` keeps one row finite and `BoundExceeded` names the
    findings it could not bind, but the route pages only `findings`,
    `recommendations`, `no_recommendations`, `drift_observations`, and the two
    embedded audit lists.  `sections.bound_exceeded` is never paged and never
    counted in `row_counts`, and one recommendation row may itself carry 512
    finding identifiers, so with 600 out-of-band campaign changes even a
    one-row page exceeds the default saved output bound and the whole route
    fails closed with `OUTPUT_LIMIT_EXCEEDED`.
    """
    from adhelm.audit import audit_provider
    from adhelm.measurement_cli import _fits
    from adhelm.models import OutputPreferences

    count = 600
    earlier = _inventory(
        _campaign_records(count, status="PAUSED", observed_at=EARLIER_OBSERVED),
        observed_at=EARLIER_OBSERVED,
    )
    later = _inventory(_campaign_records(count, status="ACTIVE", observed_at=OBSERVED))
    result = audit_provider(
        _report(),
        source_record_digest=REPORT_DIGEST,
        policy=_policy(),
        as_of=AS_OF,
        evidence=ProviderAuditEvidence(inventory=later, earlier_inventory=earlier),
    )
    data = result.model_dump(mode="json")
    # Exactly the lists `audit_command` slices, at the smallest page it serves.
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
    ), "a one-row provider audit page must fit the default saved output bound"


# --- Re-review: an unproven drift baseline is suppressed without saying so ---


def test_an_unproven_drift_baseline_states_that_absence_was_not_evaluated() -> None:
    """Suppressing absence-based drift is an omission, so it must be named.

    When the drift baseline has no proven read boundary -- the ordinary state
    of any history retained before this product recorded read completions --
    `_drift` correctly stops reporting appearing and disappearing resources.
    The coverage entry appends its unproven-boundary caveat only when the
    *selected* inventory is unproven, so a proven current read paired with a
    legacy baseline reports `DRIFT` `PARTIAL` with no statement anywhere that
    resource appearance and disappearance were not evaluated at all.
    """
    from adhelm.audits.linkedin_ads import _UNPROVEN_BOUNDARY_RATIONALE

    records = _campaign_records(1, status="ACTIVE", observed_at=OBSERVED)
    baseline = (
        *_campaign_records(1, status="PAUSED", observed_at=EARLIER_OBSERVED),
        _record(
            seed=900,
            resource_type="CAMPAIGN",
            resource_ref="linkedin_ads.campaign.gone",
            parents=(GROUP,),
            state={"status": "ACTIVE", "objective": "WEBSITE_VISIT", "currency": "USD"},
            observed_at=EARLIER_OBSERVED,
        ),
    )
    sections = _sections(
        inventory=_inventory(records),
        earlier=_inventory(
            baseline, completeness="READ_BOUNDARY_UNVERIFIED", observed_at=EARLIER_OBSERVED
        ),
    )
    assert not [
        item for item in sections.drift_observations if item.change_kind != "STATE_CHANGED"
    ], "absence-based drift is correctly suppressed for an unproven baseline"
    coverage = next(item for item in sections.coverage if item.dimension == "DRIFT")
    assert _UNPROVEN_BOUNDARY_RATIONALE in coverage.rationale
