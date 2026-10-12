"""Independent adversarial review of the Google Ads ACTIVATE/MAINTAIN routes (P05-GA).

Written by a reviewer who did not implement the change.  Nothing here reaches a
provider, a credential store, or a network.
"""

from __future__ import annotations

from adhelm.providers.google_ads_maintain_contract import (
    GoogleAdsCampaignArchiveIntent,
    GoogleAdsMaintainPreconditions,
)

_DEPENDENCY_PROOF_TERMS = ("unreferenced", "dependen", "referencing", "referenced_by")


def _proof_bearing_fields(model: type) -> list[str]:
    """Return fields that could carry a rule 11 dependency proof, by name or value space."""

    found: list[str] = []
    for name, field in model.model_fields.items():
        haystack = f"{name} {field.annotation!r}".lower()
        if any(term in haystack for term in _DEPENDENCY_PROOF_TERMS):
            found.append(name)
    return found


def test_an_archive_requires_a_dependency_proof_not_paused_status() -> None:
    """ARCHIVE is delete-class, so rule 11's dependency proof applies to it.

    ``CampaignStatus.REMOVED`` is Google's removal state, and this build treats
    it as exactly that: the intent demands ``disposition="IRREVERSIBLE_ARCHIVE"``
    and ``acknowledged_irreversible=True``, the contract and adapter call the
    transition irreversible in four places, and the review line tells the
    reviewer it cannot be undone.  A build cannot both require an
    irreversibility acknowledgement *because* the resource is being removed and
    argue the removal is not delete-class.

    ``google-ads-maintain-execution.md`` lines 207-215 rest rule 11 on the wire
    shape -- ``CampaignOperation.update`` with ``updateMask=status`` rather than
    ``CampaignOperation.remove`` -- but both produce ``REMOVED``, and the v25
    ``campaign_status.proto`` comment ("Campaign has been removed") promises no
    reversibility either way.  Rule 11 is about the effect: "A resource may be
    deleted only when the dependency engine proves it is ``PROVEN_UNREFERENCED``.
    Age, naming, **paused status**, or apparent inactivity is never enough."

    The substantive gate here is precisely paused status plus an operator
    acknowledgement.  ``observed_removed_members`` is a read-consistency check
    on the hierarchy, not a proof that nothing references the campaign.  So some
    field on the archive intent or the closed preconditions must carry that
    proof; which field, and how the proof is produced, is the implementer's
    choice.
    """

    carriers = _proof_bearing_fields(GoogleAdsCampaignArchiveIntent) + _proof_bearing_fields(
        GoogleAdsMaintainPreconditions
    )
    assert carriers != []
