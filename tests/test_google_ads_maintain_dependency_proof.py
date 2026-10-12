"""AGENTS rule 11 for the one delete-class Google Ads maintenance operation.

An archive sets ``CampaignStatus.REMOVED``, so it is governed by rule 11: "A
resource may be deleted only when the dependency engine proves it is
``PROVEN_UNREFERENCED``.  Age, naming, paused status, or apparent inactivity is
never enough."  These tests attack the proof carrier: what it refuses to hold,
that it restates no fact its observation binding already owns, and that a
retained archive plan cannot be tampered into one that lacks it.

No cited v25 resource reaches any campaign reference surface, so the real seam
reports every surface ``NOT_READ``.  Where a proven archive is needed,
``_read_campaign_references`` stands in for the read a future build would make,
exactly as ``_FakeGoogleAds`` stands in for Google.  Nothing here reaches a
provider, a credential store, or a network.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from test_google_ads_maintain_execution import (
    _observation,
    _plan,
    _prepare,
    _read_campaign_references,
    _retained_action_for_plan,
)

from adhelm import google_ads_maintain_planning_session as maintain_session
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.providers.google_ads_maintain_contract import (
    CAMPAIGN_REFERENCE_SURFACES,
    GoogleAdsCampaignDependencyProof,
    GoogleAdsCampaignReferenceSurfaceReading,
    GoogleAdsMaintainObservationBinding,
    GoogleAdsMaintainRedactedReview,
    GoogleAdsSearchCampaignMaintainPlannedAction,
    build_google_ads_search_campaign_maintain_plan,
    revalidate_google_ads_search_campaign_maintain_planned_action,
)


def _reading(surface: str, disposition: str = "READ_EMPTY", **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "surface": surface,
        "disposition": disposition,
        "referencing_row_count": {"READ_EMPTY": 0, "READ_NONEMPTY": 2}.get(disposition),
        "basis": "FAKE_FUTURE_REFERENCE_READ",
    }
    values.update(overrides)
    return values


def _proof_data(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "surfaces": [_reading(surface) for surface in CAMPAIGN_REFERENCE_SURFACES],
    }
    values.update(overrides)
    return values


def _load[T: BaseModel](model: type[T], data: dict[str, Any]) -> T:
    """Validate JSON-shaped data the way a retained record is read back."""

    return model.model_validate_json(json.dumps(data))


def _archive_action_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    prepared = _prepare(tmp_path, monkeypatch)
    _read_campaign_references(monkeypatch)
    retained = _plan(prepared, "archive")
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    return copy.deepcopy(action.model_dump(mode="json", warnings="error"))


# --- the proof model ---------------------------------------------------------


def test_a_proof_over_every_surface_read_empty_is_accepted() -> None:
    proof = _load(GoogleAdsCampaignDependencyProof, _proof_data())
    assert proof.status == "PROVEN_UNREFERENCED"
    assert proof.live_validated is False
    assert [item.surface for item in proof.surfaces] == list(CAMPAIGN_REFERENCE_SURFACES)


@pytest.mark.parametrize("surface", CAMPAIGN_REFERENCE_SURFACES)
@pytest.mark.parametrize("disposition", ["READ_NONEMPTY", "NOT_READ"])
def test_a_proof_cannot_hold_a_referenced_or_unread_surface(surface: str, disposition: str) -> None:
    surfaces = [
        _reading(item, disposition) if item == surface else _reading(item)
        for item in CAMPAIGN_REFERENCE_SURFACES
    ]
    with pytest.raises(ValidationError, match="not proven empty"):
        _load(GoogleAdsCampaignDependencyProof, _proof_data(surfaces=surfaces))


_FIRST, *_REST = CAMPAIGN_REFERENCE_SURFACES


@pytest.mark.parametrize(
    ("surfaces", "message"),
    [
        pytest.param([_reading(item) for item in CAMPAIGN_REFERENCE_SURFACES[:-1]], "at least"),
        pytest.param(
            [_reading(item) for item in reversed(CAMPAIGN_REFERENCE_SURFACES)],
            "closed surface list",
        ),
        pytest.param(
            [_reading(item) for item in CAMPAIGN_REFERENCE_SURFACES[:-1]] + [_reading(_FIRST)],
            "closed surface list",
        ),
        pytest.param(
            [_reading(item) for item in CAMPAIGN_REFERENCE_SURFACES] + [_reading(_FIRST)],
            "at most",
        ),
        pytest.param([_reading("SOMETHING_ELSE")] + [_reading(item) for item in _REST], "surface"),
    ],
    ids=["dropped", "reordered", "duplicated", "extra", "outside_the_closed_list"],
)
def test_a_proof_carries_the_closed_surface_list_exactly_once(
    surfaces: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _load(GoogleAdsCampaignDependencyProof, _proof_data(surfaces=surfaces))


@pytest.mark.parametrize(
    ("reading", "message"),
    [
        pytest.param(
            _reading("CAMPAIGN_LABELS", "NOT_READ", referencing_row_count=0),
            "cannot carry a row count",
            id="unread_with_zero_count",
        ),
        pytest.param(
            _reading("CAMPAIGN_LABELS", "READ_EMPTY", referencing_row_count=None),
            "must carry its row count",
            id="empty_without_count",
        ),
        pytest.param(
            _reading("CAMPAIGN_LABELS", "READ_EMPTY", referencing_row_count=3),
            "contradicts its disposition",
            id="empty_with_rows",
        ),
        pytest.param(
            _reading("CAMPAIGN_LABELS", "READ_NONEMPTY", referencing_row_count=0),
            "contradicts its disposition",
            id="nonempty_with_zero",
        ),
        pytest.param(
            _reading("CAMPAIGN_LABELS", "READ_NONEMPTY", referencing_row_count=None),
            "must carry its row count",
            id="nonempty_without_count",
        ),
        pytest.param(
            _reading("CAMPAIGN_LABELS", basis="lowercase with spaces"),
            "basis",
            id="basis_is_free_text",
        ),
    ],
)
def test_a_surface_reading_cannot_contradict_itself(reading: dict[str, Any], message: str) -> None:
    """A zero count on an unread surface is the fabricated emptiness rule 11 forbids."""

    with pytest.raises(ValidationError, match=message):
        _load(GoogleAdsCampaignReferenceSurfaceReading, reading)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param({"live_validated": True}, "live validation", id="claims_live_validation"),
        pytest.param({"status": "REFERENCED"}, "status", id="claims_another_status"),
        pytest.param({"consistency": "ATOMIC"}, "consistency", id="claims_atomicity"),
        # The proof restates nothing its binding holds and measures no read
        # interval of its own, so none of these can be carried at all.
        pytest.param({"campaign_name": "Spring Launch"}, "campaign_name", id="restates_name"),
        pytest.param({"projection_digest": "a" * 64}, "projection_digest", id="restates_hierarchy"),
        pytest.param(
            {"fingerprint_key_marker": "b" * 64}, "fingerprint_key_marker", id="restates_key"
        ),
        pytest.param({"read_started_at": "2030-01-02T12:00:00Z"}, "read_started_at", id="interval"),
    ],
)
def test_a_proof_refuses_unbounded_or_overclaiming_metadata(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _load(GoogleAdsCampaignDependencyProof, _proof_data(**overrides))


# --- the archive review ------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "stated"),
    [
        ("CAMPAIGN_ARCHIVE", "NOT_APPLICABLE_NOT_DELETE_CLASS"),
        ("CAMPAIGN_BUDGET_AMOUNT", "PROVEN_UNREFERENCED"),
        ("AD_GROUP_CPC_BID", "PROVEN_UNREFERENCED"),
        ("AD_SCHEDULE_REPLACEMENT", "PROVEN_UNREFERENCED"),
    ],
)
def test_a_review_states_the_proof_only_for_the_delete_class_kind(kind: str, stated: str) -> None:
    """The archive review cannot say "not applicable"; no other kind can claim a proof."""

    review = {
        "maintenance_kind": "CAMPAIGN_ARCHIVE",
        "maintenance_effect": "CAMPAIGN_STATUS_SET_TO_REMOVED_IRREVERSIBLE",
        "campaign_status_before": "PAUSED",
        "campaign_status_after": "REMOVED",
        "dependency_proof": "PROVEN_UNREFERENCED",
        "new_value": "REMOVED",
        "previous_value": "PAUSED",
        "reversibility": "IRREVERSIBLE_ARCHIVE",
    }
    review.update({"maintenance_kind": kind, "dependency_proof": stated})
    with pytest.raises(ValidationError, match="only the delete-class archive"):
        _load(GoogleAdsMaintainRedactedReview, review)


# --- the observation binding and the retained plan ---------------------------


def _proof(data: dict[str, Any]) -> dict[str, Any]:
    proof = data["observation_binding"]["preconditions"]["dependency_proof"]
    assert proof is not None
    return proof  # type: ignore[no-any-return]


def _rebuild(
    action: GoogleAdsSearchCampaignMaintainPlannedAction, binding: dict[str, Any]
) -> GoogleAdsSearchCampaignMaintainPlannedAction:
    """Compile the same action again around a different observation binding.

    Everything else -- account, execution, intent, expiry, and local key -- is
    the retained action's own, so a refusal here is attributable to the binding.
    """

    return build_google_ads_search_campaign_maintain_plan(
        workspace_profile_ref=action.workspace_binding.workspace_profile_ref,
        account_binding=action.account_binding,
        execution_binding=action.execution_binding,
        intent=action.intent,
        observation_binding=_load(GoogleAdsMaintainObservationBinding, binding),
        expires_at=action.plan.expires_at,
        fingerprint_key=export_key(),
    )


def test_the_untampered_archive_action_revalidates_with_its_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _archive_action_data(tmp_path, monkeypatch)
    action = revalidate_google_ads_search_campaign_maintain_planned_action(data)
    proof = action.observation_binding.preconditions.dependency_proof
    assert proof is not None
    assert proof.status == "PROVEN_UNREFERENCED"
    rebuilt = _rebuild(action, data["observation_binding"])
    assert rebuilt.action_id == action.action_id


def test_an_archive_without_its_proof_cannot_be_compiled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paused status and the acknowledgement remain, and are not the gate."""

    data = _archive_action_data(tmp_path, monkeypatch)
    action = revalidate_google_ads_search_campaign_maintain_planned_action(data)
    assert action.intent.acknowledged_irreversible is True  # type: ignore[union-attr]
    assert action.observation_binding.preconditions.observed_campaign_status == "PAUSED"
    binding = copy.deepcopy(data["observation_binding"])
    binding["preconditions"]["dependency_proof"] = None
    with pytest.raises(AdhelmError) as refused:
        _rebuild(action, binding)
    assert refused.value.code == "INVALID_GOOGLE_ADS_MAINTAIN_PLAN"


@pytest.mark.parametrize("kind", ["budget", "bid", "schedule"])
def test_no_other_maintenance_kind_may_carry_a_dependency_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    _read_campaign_references(monkeypatch)
    archive = _retained_action_for_plan(Ledger(tmp_path), _plan(prepared, "archive").record.plan)
    proof = archive.observation_binding.preconditions.dependency_proof
    assert proof is not None
    action = _retained_action_for_plan(Ledger(tmp_path), _plan(prepared, kind).record.plan)
    binding = action.observation_binding.model_dump(mode="json", warnings="error")
    assert _rebuild(action, binding).action_id == action.action_id
    # The only thing wrong with this binding is that its kind has no use for a proof.
    binding["preconditions"]["dependency_proof"] = proof.model_dump(mode="json")
    with pytest.raises(AdhelmError) as refused:
        _rebuild(action, binding)
    assert refused.value.code == "INVALID_GOOGLE_ADS_MAINTAIN_PLAN"


@pytest.mark.parametrize("disposition", ["READ_NONEMPTY", "NOT_READ"])
def test_a_retained_archive_cannot_be_edited_to_carry_an_unproven_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposition: str
) -> None:
    data = _archive_action_data(tmp_path, monkeypatch)
    _proof(data)["surfaces"][0] = _reading(CAMPAIGN_REFERENCE_SURFACES[0], disposition)
    with pytest.raises(AdhelmError) as refused:
        revalidate_google_ads_search_campaign_maintain_planned_action(data)
    assert refused.value.code == "INVALID_GOOGLE_ADS_MAINTAIN_PLAN"


# --- the producer ------------------------------------------------------------


def test_the_real_seam_never_reports_a_surface_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not reading a surface is not the absence of a reference to it."""

    prepared = _prepare(tmp_path, monkeypatch)
    readings = maintain_session.campaign_reference_surfaces(_observation(prepared))
    assert [item.surface for item in readings] == list(CAMPAIGN_REFERENCE_SURFACES)
    assert {item.disposition for item in readings} == {"NOT_READ"}
    assert {item.referencing_row_count for item in readings} == {None}


def test_the_producer_refuses_a_value_that_is_not_an_observation() -> None:
    with pytest.raises(AdhelmError) as refused:
        maintain_session.campaign_reference_surfaces(object())  # type: ignore[arg-type]
    assert refused.value.code == "GOOGLE_ADS_MAINTAIN_FACTS_INVALID"


def test_the_producer_derives_the_proof_from_the_observation_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No caller-supplied campaign, key, or interval can reach the proof."""

    prepared = _prepare(tmp_path, monkeypatch)
    _read_campaign_references(monkeypatch)
    proof = maintain_session.campaign_dependency_proof(_observation(prepared))
    assert set(proof.model_dump()) == {
        "schema_version",
        "proof_kind",
        "status",
        "surfaces",
        "consistency",
        "live_validated",
    }
    assert [item.surface for item in proof.surfaces] == list(CAMPAIGN_REFERENCE_SURFACES)


def test_the_producer_refuses_by_surface_when_nothing_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    with pytest.raises(AdhelmError) as refused:
        maintain_session.campaign_dependency_proof(_observation(prepared))
    assert refused.value.code == "GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN"
    assert "CAMPAIGN_LABELS (NOT_READ)" in refused.value.message
