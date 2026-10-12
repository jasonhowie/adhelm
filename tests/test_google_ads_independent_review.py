# ruff: noqa: E501
"""Independent review findings for P01-GA (Google Ads audit, reads, OAuth status).

These cases were written by a reviewer who did not write the product code, and
each one fails against `fd93383`.  Every case is offline: no Google endpoint is
contacted, no credential store outside the injected fake is touched, and no
ledger record is appended by the rule table.

The four findings, in the order they appear below:

1. `_google_snapshots` added `normalized_state.budget_type` to every retained
   Google Ads campaign and `normalized_state.objective` to every retained ad.
   A customer whose ledger already holds pre-change snapshots therefore gets
   `GOOGLE_ADS_OUT_OF_BAND_STATE_CHANGE_OBSERVED` for every campaign and every
   ad on the first audit after the upgrade, asserting that "the change occurred
   somewhere inside the recorded observation interval" when nothing changed at
   Google at all.
2. (Retired: the developer-token slot this finding concerned no longer exists, because
   Google retired developer tokens on 2026-09-09.)
3. `ad_group_ad.policy_summary.review_status` is projected, parsed, bounded to
   an enum grammar, retained as a prefixed snapshot status, and declared
   `IMPLEMENTED_FIXED_READ` in `GOOGLE_ADS_READ_SURFACES` -- and then no rule
   in the 52-rule table reads it and no dimension is backed by it.  An ad
   carrying a review status this build never reviewed produces no finding.
4. `_account_scoped_findings` returns nothing when the retained inventory holds
   no `ACCOUNT` snapshot, so the pagination disclosure and the thirteen
   unread-surface disclosures disappear, while `STRUCTURE` silently drops from
   `COVERED` to `PARTIAL` with a rationale that never says why.
"""

from __future__ import annotations

from datetime import timedelta

from test_google_ads_audit import (
    ACCOUNT,
    AD,
    AD_GROUP,
    CAMPAIGN,
    HEALTHY_AD_QUALITY,
    HEALTHY_CAMPAIGN_QUALITY,
    OBSERVED,
    _healthy_records,
    _inventory,
    _record,
    _sections,
)

from adhelm.audits import ProviderAuditEvidence
from adhelm.audits.google_ads import RULES
from adhelm.providers.google_ads import (
    GOOGLE_ADS_POLICY_REVIEW_PREFIX,
    GOOGLE_ADS_POLICY_REVIEW_STATUS_RETURNED,
    GOOGLE_ADS_READ_SURFACES,
)

# --- 1. a connector state-shape change is not an out-of-band Google change ----

# The campaign state this build's *previous* connector retained for exactly the
# same remote campaign: `budget_type` did not exist on a Google Ads campaign
# snapshot before `src/adhelm/inventory.py:776`.
_PRE_CHANGE_CAMPAIGN_STATE = {
    "status": "ENABLED",
    "objective": "SEARCH",
    "currency": "USD",
    "budget_minor": 5000,
    "bid_strategy": "MANUAL_CPC",
    "schedule_start": "2026-08-01",
    "schedule_end": "2026-09-30",
}
# And the ad state it retained: `objective` did not carry the ad type before
# `src/adhelm/inventory.py:808`.
_PRE_CHANGE_AD_STATE = {"status": "ENABLED"}


def _upgrade_evidence() -> ProviderAuditEvidence:
    """One account read twice: once by the old connector, once by the new one.

    Nothing changed at Google between the two reads.  Only the local normalized
    state model grew two fields.
    """
    later = _healthy_records()
    earlier_observed = OBSERVED - timedelta(days=1)
    earlier = (
        _record(
            seed=11,
            resource_type="ACCOUNT",
            resource_ref=ACCOUNT,
            status="NOT_AVAILABLE_FROM_PROVIDER",
            quality=later[0].snapshot.data_quality_statuses,
            observed_at=earlier_observed,
        ),
        _record(
            seed=12,
            resource_type="CAMPAIGN",
            resource_ref=CAMPAIGN,
            parents=(ACCOUNT,),
            state=_PRE_CHANGE_CAMPAIGN_STATE,
            quality=HEALTHY_CAMPAIGN_QUALITY,
            observed_at=earlier_observed,
        ),
        _record(
            seed=13,
            resource_type="AD_GROUP",
            resource_ref=AD_GROUP,
            parents=(CAMPAIGN,),
            state=dict(later[2].snapshot.normalized_state),
            observed_at=earlier_observed,
        ),
        _record(
            seed=14,
            resource_type="AD",
            resource_ref=AD,
            parents=(CAMPAIGN, AD_GROUP),
            state=_PRE_CHANGE_AD_STATE,
            quality=HEALTHY_AD_QUALITY,
            observed_at=earlier_observed,
        ),
    )
    return ProviderAuditEvidence(
        inventory=_inventory(later),
        earlier_inventory=_inventory(earlier, observed_at=earlier_observed),
    )


def test_a_local_state_shape_change_is_not_reported_as_an_out_of_band_change() -> None:
    """A field the previous connector never wrote is not a change at Google.

    `snapshot_state_deltas` compares `normalized_state.get(field)`, so a field
    that exists only in the later snapshot reads as a differing value.  The
    audit then publishes `GOOGLE_ADS_OUT_OF_BAND_STATE_CHANGE_OBSERVED` with
    the observation "the change occurred somewhere inside the recorded
    observation interval", which is a fabricated remote observation:
    `AGENTS.md` rule 20 forbids fabricated results and rule 24 forbids
    inventing an external change this product cannot prove.

    Expected: a delta whose changed paths are all fields absent from one side's
    `normalized_state` key set is a local schema difference and must not be
    emitted as an out-of-band Google Ads change, nor recorded as a
    `STATE_CHANGED` drift observation over that interval.
    """
    sections = _sections(evidence=_upgrade_evidence())

    manufactured = [item for item in sections.findings if item.rule_id == "GA_DRIFT_STATE_CHANGED"]
    assert manufactured == [], (
        "A connector that started retaining `budget_type` and the ad type reported an "
        "out-of-band Google Ads change for every campaign and every ad: "
        f"{sorted(ref for item in manufactured for ref in item.finding.resource_refs)}"
    )
    assert [
        observation
        for observation in sections.drift_observations
        if observation.change_kind == "STATE_CHANGED"
    ] == []


def test_the_state_shape_change_is_disclosed_where_a_reader_would_look() -> None:
    """Adding two retained state fields changes every campaign and ad digest.

    `normalized_state_digest` is part of the retained snapshot, so the first
    read after this change rewrites the digest of every Google Ads campaign and
    ad even when nothing at Google moved.  Neither the read contract nor the
    audit rule document says so.

    Expected: the additive-read research note discloses the retained-state
    shape change and its drift consequence, exactly as it discloses the added
    projections.
    """
    from pathlib import Path

    docs = Path(__file__).resolve().parent.parent / "docs"
    contract = (docs / "research" / "google-ads-read-contract.md").read_text(encoding="utf-8")
    rules = (docs / "implementation" / "google-ads-audit-recommendations.md").read_text(
        encoding="utf-8"
    )
    assert "budget_type" in contract or "budget_type" in rules, (
        "No reviewed document mentions that the retained campaign snapshot gained "
        "`normalized_state.budget_type`, or that the first post-upgrade audit therefore "
        "reports drift for every retained campaign and ad."
    )


# --- 3. an unreviewed policy review status is never reported ------------------


def _ad_quality_with_review_status(value: str) -> tuple[str, ...]:
    return (
        *(
            status
            for status in HEALTHY_AD_QUALITY
            if not status.startswith(GOOGLE_ADS_POLICY_REVIEW_PREFIX)
        ),
        GOOGLE_ADS_POLICY_REVIEW_STATUS_RETURNED,
        GOOGLE_ADS_POLICY_REVIEW_PREFIX + value,
    )


def test_a_policy_review_status_this_build_never_reviewed_is_not_silent() -> None:
    """A read surface with no rule reports a restricted ad as healthy.

    `_AD_QUERY` selects `ad_group_ad.policy_summary.review_status`, `_ad`
    validates it against the enum grammar, `_google_ad_quality`
    (`src/adhelm/inventory.py:741`) retains it as a prefixed snapshot status, and
    `GOOGLE_ADS_READ_SURFACES["AD_POLICY_REVIEW_STATUS"]` is
    `IMPLEMENTED_FIXED_READ`.  No rule in `RULES` reads it, no entry in
    `_DIMENSION_SURFACES` is backed by it, and -- unlike the approval status --
    there is no `GOOGLE_ADS_REVIEWED_POLICY_REVIEW_STATUSES` tuple to compare a
    value against.  An ad whose review status this build has never seen
    therefore produces no ad-scoped finding at all, while the `POLICY` coverage
    rationale tells the reader "the ad policy summary approval **and review**
    statuses ... are read".

    Expected: an unreviewed review-status value produces a finding, exactly as
    `GA_POLICY_APPROVAL_STATUS_NOT_REVIEWED` does for the approval status.
    """
    records = _healthy_records(ad_quality=_ad_quality_with_review_status("A_VALUE_NEVER_REVIEWED"))
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(records)))

    ad_findings = {item.rule_id for item in sections.findings if AD in item.finding.resource_refs}
    assert ad_findings, (
        "An ad carrying a Google Ads policy review status outside anything this build "
        "reviewed produced no finding; an unreviewed value is reported as healthy."
    )


def test_the_policy_review_status_surface_backs_the_policy_dimension() -> None:
    """A surface declared implemented and named by no dimension overstates the read.

    `GOOGLE_ADS_READ_SURFACES` is the one declaration `_coverage_map` derives
    its ceiling from, and `_DIMENSION_SURFACES["POLICY"]` names only
    `AD_POLICY_APPROVAL_STATUS` and `CUSTOMER_TEST_ACCOUNT_FLAG`.  The review
    status is therefore requested from the customer's account, retained, and
    invisible to every coverage claim and every rule.

    Expected: `AD_POLICY_REVIEW_STATUS` backs the `POLICY` dimension, or the
    projection is dropped.
    """
    from adhelm.audits.google_ads import _DIMENSION_SURFACES

    named = {surface for surfaces in _DIMENSION_SURFACES.values() for surface in surfaces}
    implemented = {
        surface
        for surface, status in GOOGLE_ADS_READ_SURFACES.items()
        if status.startswith("IMPLEMENTED")
    }
    assert "AD_POLICY_REVIEW_STATUS" in named, (
        "`AD_POLICY_REVIEW_STATUS` is declared implemented but backs no audit dimension; "
        f"the unused implemented surfaces are {sorted(implemented - named)}."
    )


# --- 4. a retained read with no account snapshot loses its disclosures --------


def test_an_inventory_without_an_account_snapshot_still_states_why_it_is_partial() -> None:
    """Dropping the account record quietly drops fourteen disclosures.

    `_account_scoped_findings` (`src/adhelm/audits/google_ads.py:1372-1374`)
    returns `[]` when no `ACCOUNT` snapshot is retained, so
    `GA_STRUCTURE_INVENTORY_PAGINATION_NOT_PROVEN`,
    `GA_POLICY_CLOUD_PROJECT_ACCESS_LEVEL_NOT_OBSERVABLE`, and every other
    unread-surface disclosure disappear.  `_coverage_map` needs the same record
    to prove pagination, so `STRUCTURE` drops from `COVERED` to `PARTIAL`; but
    `_coverage` also gates its `_UNPROVEN_PAGINATION_RATIONALE` on that record,
    so the rationale never says why.  The module's own comment on
    `_DRIFT_ABSENCE_SUPPRESSED` says a suppressed rule must be named rather
    than left to look like a cleaner result.

    Expected: a retained read whose pagination could not be evaluated says so,
    in the coverage rationale or in a finding.
    """
    records = tuple(
        record for record in _healthy_records() if record.snapshot.resource_type != "ACCOUNT"
    )
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(records)))
    structure = next(item for item in sections.coverage if item.dimension == "STRUCTURE")

    assert structure.status == "PARTIAL"
    assert "not proven" in structure.rationale or any(
        item.rule_id == "GA_STRUCTURE_INVENTORY_PAGINATION_NOT_PROVEN" for item in sections.findings
    ), (
        "STRUCTURE fell to PARTIAL because no account snapshot proved the pagination "
        f"total, and nothing said so: {structure.rationale!r}"
    )


def test_the_unread_surface_disclosures_do_not_depend_on_one_retained_record() -> None:
    """Fourteen `NOT_READ` disclosures hang off a single account snapshot.

    Expected: the disclosures that describe what this build's fixed reads never
    request are properties of the read contract, not of one retained record, so
    a retained inventory that omits the account snapshot still reports them.
    """
    records = tuple(
        record for record in _healthy_records() if record.snapshot.resource_type != "ACCOUNT"
    )
    sections = _sections(evidence=ProviderAuditEvidence(inventory=_inventory(records)))
    reported = {item.rule_id for item in sections.findings}

    unread_rules = {
        rule_id
        for rule_id in RULES
        if rule_id.endswith("_NOT_READ") or rule_id.endswith("_NOT_DISCLOSED")
    }
    assert unread_rules <= reported, (
        "These unread-surface disclosures vanished with the account snapshot: "
        f"{sorted(unread_rules - reported)}"
    )
