"""Bounded Google Ads reads and preconditions for one Search maintenance plan.

The reads themselves are the shared
:class:`adhelm.google_ads_activate_planning_session.GoogleAdsLiveHierarchyReader`.
Two hand-written observers of one hierarchy would be two chances to disagree
about what "the same hierarchy" means, so this module adds no query of its own.
What it adds is the maintenance-specific *derivation*: the bounded
precondition checks each of the four maintenance kinds requires, the AGENTS
rule 11 dependency proof one archive requires, and the closed observation
binding the contract is built from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_ads_activate_planning_session import (
    GoogleAdsLiveHierarchyObservation,
    GoogleAdsLiveHierarchyReader,
)
from adhelm.providers.google_ads_activate_contract import (
    ACTIVATED_STATUS,
    PAUSED_STATUS,
    REMOVED_STATUS,
    fingerprint_key_marker,
)
from adhelm.providers.google_ads_maintain_contract import (
    CAMPAIGN_REFERENCE_SURFACES,
    GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN,
    GoogleAdsAdGroupBidIntent,
    GoogleAdsAdScheduleReplacementIntent,
    GoogleAdsBudgetAmountIntent,
    GoogleAdsCampaignDependencyProof,
    GoogleAdsCampaignReferenceSurfaceReading,
    GoogleAdsMaintainObservationBinding,
    GoogleAdsMaintainPreconditions,
    MaintenanceIntent,
)

_SCHEDULE_MEMBER: Final = "CAMPAIGN_CRITERION_AD_SCHEDULE"

#: Why every reference surface is ``NOT_READ``: ``docs/research/`` cites the v25
#: ``campaign``, ``campaign_budget``, ``campaign_criterion``, ``ad_group``,
#: ``ad_group_criterion``, ``ad_group_ad``, and ``customer`` protos and nothing
#: else, so there is no GAQL resource here this build may name without
#: inventing one.  An honest refusal beats a fabricated emptiness (AGENTS rules
#: 11 and 20).
_NOT_READ_BASIS: Final = "NO_CITABLE_V25_GAQL_RESOURCE_IN_THIS_BUILD"


def _refuse(code: str, message: str, next_action: str = "") -> AdhelmError:
    if not next_action:
        return AdhelmError(code, message, exit_code=ExitCode.PRECONDITION)
    return AdhelmError(code, message, exit_code=ExitCode.PRECONDITION, next_action=next_action)


def campaign_reference_surfaces(
    observation: GoogleAdsLiveHierarchyObservation,
) -> tuple[GoogleAdsCampaignReferenceSurfaceReading, ...]:
    """Classify every reviewed campaign reference surface for one observed campaign.

    This is pure: it issues no request, and it is the one seam a future read of
    these surfaces replaces.  Today none is queryable from a cited source, so
    every surface is ``NOT_READ`` -- never ``READ_EMPTY``, because the absence
    of a read is not the absence of a reference.

    The returned tuple is always the whole closed list in
    :data:`~adhelm.providers.google_ads_maintain_contract.CAMPAIGN_REFERENCE_SURFACES`
    order, so a caller cannot review a subset.
    """

    if type(observation) is not GoogleAdsLiveHierarchyObservation:
        raise _refuse(
            "GOOGLE_ADS_MAINTAIN_FACTS_INVALID",
            "A complete bounded Google Ads hierarchy observation is required.",
        )
    return tuple(
        GoogleAdsCampaignReferenceSurfaceReading(
            surface=surface,
            disposition="NOT_READ",
            basis=_NOT_READ_BASIS,
        )
        for surface in CAMPAIGN_REFERENCE_SURFACES
    )


def unproven_campaign_reference_surfaces(
    observation: GoogleAdsLiveHierarchyObservation,
) -> tuple[GoogleAdsCampaignReferenceSurfaceReading, ...]:
    """Return every reviewed surface that is not ``READ_EMPTY``, without raising.

    This is the observation-side half of the gate: it reports what a fresh read
    established so a caller can surface it as a diagnostic.  Whether that
    refuses anything is the caller's decision, and the guarded adapter's
    ``read_state`` deliberately refuses nothing (see
    :func:`~adhelm.providers.google_ads_maintain_contract.unproven_archive_state`).
    """

    return tuple(
        item
        for item in campaign_reference_surfaces(observation)
        if item.disposition != "READ_EMPTY"
    )


def assert_campaign_dependency_proven(
    observation: GoogleAdsLiveHierarchyObservation,
) -> tuple[GoogleAdsCampaignReferenceSurfaceReading, ...]:
    """Refuse an archive by named surface unless every one is ``READ_EMPTY``.

    An archive is delete-class, so AGENTS rule 11 applies: "A resource may be
    deleted only when the dependency engine proves it is ``PROVEN_UNREFERENCED``.
    Age, naming, paused status, or apparent inactivity is never enough."  A
    ``READ_NONEMPTY`` surface is a live inbound reference; a ``NOT_READ``
    surface is an unproven one, and ``measurement-control.md`` requires an
    unreadable surface to block the proof rather than to read as empty.

    This raises at *planning*, where it is a precondition of building a plan.
    It is not called from any observation path: recovery and post-apply reads
    report state and never enforce this gate.
    """

    surfaces = campaign_reference_surfaces(observation)
    blocking = unproven_campaign_reference_surfaces(observation)
    if blocking:
        raise AdhelmError(
            GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN,
            f"This Google Ads campaign is not proven unreferenced: {describe_surfaces(blocking)}.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Archiving is delete-class and requires a dependency proof, which paused "
                "status and an irreversibility acknowledgement never supply. Resolve or "
                "read every reference surface first; no plan was compiled and nothing was "
                "changed."
            ),
        )
    return surfaces


def describe_surfaces(
    readings: tuple[GoogleAdsCampaignReferenceSurfaceReading, ...],
) -> str:
    """Name each surface and its disposition, for a bounded diagnostic."""

    return ", ".join(f"{item.surface} ({item.disposition})" for item in readings)


def campaign_dependency_proof(
    observation: GoogleAdsLiveHierarchyObservation,
) -> GoogleAdsCampaignDependencyProof:
    """Return the AGENTS rule 11 proof for one campaign, or refuse by surface.

    This is the only producer of a
    :class:`~adhelm.providers.google_ads_maintain_contract.GoogleAdsCampaignDependencyProof`,
    and it derives one from a completed read.  Nothing a request carries can
    reach it, so no operator assertion is ever a proof.  The proof holds only
    what that read established about the reference surfaces; the campaign, the
    hierarchy, the key, and the interval stay in the observation binding that
    carries it.
    """

    return GoogleAdsCampaignDependencyProof(surfaces=assert_campaign_dependency_proven(observation))


def assert_maintainable(
    observation: GoogleAdsLiveHierarchyObservation, intent: MaintenanceIntent
) -> None:
    """Run every maintenance precondition this build performs before planning."""

    statuses = set(observation.projection.statuses) | {observation.campaign_status}
    if REMOVED_STATUS in statuses:
        raise _refuse(
            "GOOGLE_ADS_MAINTAIN_MEMBER_REMOVED",
            "A removed Google Ads entity is never maintained.",
            "Review the current campaign state; nothing was changed.",
        )
    if not statuses <= {PAUSED_STATUS, ACTIVATED_STATUS}:
        raise _refuse(
            "GOOGLE_ADS_MAINTAIN_HIERARCHY_STATUS_UNSUPPORTED",
            "This route maintains a paused or enabled Google Ads hierarchy only.",
        )
    campaign = observation.projection.configured("CAMPAIGN")
    if campaign.get("advertising_channel_type") != "SEARCH":
        raise _refuse(
            "GOOGLE_ADS_MAINTAIN_CAMPAIGN_TYPE_UNSUPPORTED",
            "This route maintains a Search campaign only.",
        )
    kind = intent.maintenance_kind
    if kind == "CAMPAIGN_ARCHIVE":
        if observation.campaign_status != PAUSED_STATUS:
            raise _refuse(
                "GOOGLE_ADS_ARCHIVE_REQUIRES_PAUSED_CAMPAIGN",
                "An ENABLED Google Ads campaign is paused before it is archived.",
                (
                    "Compile and authorize a pause first; archiving is irreversible and is "
                    "never combined with stopping delivery in one step. Nothing was changed."
                ),
            )
        # Paused status is only the sequencing control.  AGENTS rule 11's
        # dependency proof is built, or refused, with the observation binding.
        return
    if kind == "CAMPAIGN_BUDGET_AMOUNT":
        assert isinstance(intent, GoogleAdsBudgetAmountIntent)
        if intent.new_total_amount_micros == observation.budget_total_amount_micros:
            raise _refuse(
                "GOOGLE_ADS_MAINTAIN_NO_CHANGE",
                "The requested campaign budget total is the one already configured.",
                "A no-operation is not a plan; nothing was changed.",
            )
        return
    if kind == "AD_GROUP_CPC_BID":
        assert isinstance(intent, GoogleAdsAdGroupBidIntent)
        if intent.new_cpc_bid_micros == observation.ad_group_cpc_bid_micros:
            raise _refuse(
                "GOOGLE_ADS_MAINTAIN_NO_CHANGE",
                "The requested ad group max CPC bid is the one already configured.",
                "A no-operation is not a plan; nothing was changed.",
            )
        return
    assert isinstance(intent, GoogleAdsAdScheduleReplacementIntent)
    if not observation.ad_schedule_criterion_resource_names:
        raise _refuse(
            "GOOGLE_ADS_MAINTAIN_NO_AD_SCHEDULE_TO_REPLACE",
            "This Google Ads campaign holds no ad schedule criterion to replace.",
            (
                "Compile a campaign with ad schedules, or use a plan that creates them; "
                "nothing was changed."
            ),
        )
    observed = tuple(
        sorted(
            member.identity
            for member in observation.projection.members
            if member.member_kind == _SCHEDULE_MEMBER
        )
    )
    requested = tuple(sorted(item.identity for item in intent.schedules))
    if observed == requested:
        raise _refuse(
            "GOOGLE_ADS_MAINTAIN_NO_CHANGE",
            "The requested ad schedules are the ones already configured.",
            "A no-operation is not a plan; nothing was changed.",
        )


def maintain_observation_binding(
    observation: GoogleAdsLiveHierarchyObservation,
    intent: MaintenanceIntent,
    *,
    campaign_name: str,
    fingerprint_key: bytes,
    read_started_at: datetime,
    read_finished_at: datetime,
) -> GoogleAdsMaintainObservationBinding:
    """Reduce one bounded live read to the contract's closed observation binding.

    An archive additionally carries the rule 11 dependency proof, derived from
    the same observation.  The proof restates no binding fact; the campaign,
    hierarchy, key marker, and interval are this binding's own.  No other
    maintenance kind carries one, and the contract refuses either shape the
    other way round.
    """

    schedules = sum(
        member.member_kind == _SCHEDULE_MEMBER for member in observation.projection.members
    )
    proof = (
        campaign_dependency_proof(observation)
        if intent.maintenance_kind == "CAMPAIGN_ARCHIVE"
        else None
    )
    return GoogleAdsMaintainObservationBinding(
        campaign_name=campaign_name,
        projection=observation.projection,
        observed_budget_total_amount_micros=observation.budget_total_amount_micros,
        observed_cpc_bid_micros=observation.ad_group_cpc_bid_micros,
        observed_start_date_time=observation.start_date_time,
        observed_end_date_time=observation.end_date_time,
        observed_ad_schedule_count=schedules,
        preconditions=GoogleAdsMaintainPreconditions(
            observed_campaign_status=observation.campaign_status,  # type: ignore[arg-type]
            dependency_proof=proof,
        ),
        fingerprint_key_marker=fingerprint_key_marker(fingerprint_key),
        read_started_at=read_started_at,
        read_finished_at=read_finished_at,
    )


__all__ = [
    "GoogleAdsLiveHierarchyObservation",
    "GoogleAdsLiveHierarchyReader",
    "assert_campaign_dependency_proven",
    "assert_maintainable",
    "campaign_dependency_proof",
    "campaign_reference_surfaces",
    "describe_surfaces",
    "maintain_observation_binding",
    "unproven_campaign_reference_surfaces",
]
