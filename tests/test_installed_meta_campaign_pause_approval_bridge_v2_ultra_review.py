"""Independent fake-only checks for the private Meta PAUSE V2 handoff.

These cases use locally signed synthetic receipt bytes and temporary canonical
ledgers.  They do not invoke a host, XPC, pipe, helper, credential store, or
provider.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
from test_installed_meta_campaign_pause_approval_bridge_v2 import (
    _T2,
    _accept,
    _candidate,
    _claim_rows,
    _fixture_action,
    _Material,
    _pending,
)
from test_installed_meta_campaign_pause_approval_bridge_v2 import material as _base_material

import adhelm.approval_acceptance as approval_acceptance
from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    NativeMetaCampaignPauseApprovalBridgeCandidateV2,
)
from adhelm.errors import AdhelmError
from adhelm.saved_meta_pause_plans import retain_meta_campaign_pause_plan


@pytest.fixture
def material(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Material:
    """Reuse only the fake local base setup; fixture registration stays local."""
    factory = cast(
        Callable[[Path, pytest.MonkeyPatch], _Material],
        _base_material.__wrapped__,  # type: ignore[attr-defined]
    )
    return factory(tmp_path, monkeypatch)


def test_invalid_candidate_after_pending_consumption_is_ambiguous_and_non_retryable(
    material: _Material,
) -> None:
    """A malformed result cannot leave a usable pending exchange behind."""
    pending = _pending(material)
    candidate = _candidate(pending, material)
    object.__setattr__(
        candidate,
        "_NativeMetaCampaignPauseApprovalBridgeCandidateV2__canonical_bytes",
        b'{"invalid":true}',
    )
    material.clock.now = _T2

    with pytest.raises(AdhelmError) as rejected:
        _accept(material, pending, candidate)
    assert (
        rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_ACCEPTANCE_UNVERIFIED"
    )
    assert _claim_rows(material.ledger) == []

    with pytest.raises(AdhelmError) as replay:
        _accept(material, pending, candidate)
    assert replay.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED"
    assert _claim_rows(material.ledger) == []


@pytest.mark.parametrize("stream", ("creative", "performance"))
def test_non_changes_stream_replacement_after_candidate_is_ambiguous_and_consumed(
    material: _Material,
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
) -> None:
    """Every retained stream prefix matters, even where the plan was not stored."""
    pending = _pending(material)
    candidate = _candidate(pending, material)
    material.clock.now = _T2
    checkpoint = deepcopy(material.ledger.checkpoint())
    streams = checkpoint["streams"]
    assert type(streams) is dict
    facts = streams[stream]
    assert type(facts) is dict
    facts["stream_id"] = "replacement:" + stream
    monkeypatch.setattr(material.ledger, "checkpoint", lambda: checkpoint)

    with pytest.raises(AdhelmError) as rejected:
        _accept(material, pending, candidate)
    assert (
        rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_ACCEPTANCE_UNVERIFIED"
    )
    assert _claim_rows(material.ledger) == []

    with pytest.raises(AdhelmError) as replay:
        _accept(material, pending, candidate)
    assert replay.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED"


def test_append_only_retained_history_extension_preserves_exact_source_claim(
    material: _Material,
) -> None:
    """A valid suffix does not invalidate the fixed retained plan prefix."""
    pending = _pending(material)
    candidate = _candidate(pending, material)
    retain_meta_campaign_pause_plan(material.ledger, _fixture_action(material.configuration))
    material.clock.now = _T2

    accepted = _accept(material, pending, candidate)

    assert accepted.direct_grant is not None
    expected = _fixture_action(material.configuration).plan.plan_digest
    assert accepted.direct_grant.plan_digest == expected
    assert len(_claim_rows(material.ledger)) == 1


def test_v2_candidate_acceptance_never_uses_the_legacy_pinned_trust_path(
    material: _Material,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The V2 handoff supplies its verified current key only to the private core."""
    pending = _pending(material)
    candidate = _candidate(pending, material)
    material.clock.now = _T2

    def forbidden_legacy_trust(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("V2 candidate acceptance must not construct legacy pinned trust")

    monkeypatch.setattr(approval_acceptance, "_checked_trust", forbidden_legacy_trust)
    accepted = _accept(material, pending, candidate)

    assert accepted.direct_grant is not None
    assert accepted.kind.value == "META_CAMPAIGN_PAUSE_PLAN"
    assert len(_claim_rows(material.ledger)) == 1


def test_constructed_candidate_with_no_checked_wire_cannot_create_a_claim(
    material: _Material,
) -> None:
    """A type-shaped candidate is still a post-consumption ambiguous result."""
    pending = _pending(material)
    forged = object.__new__(NativeMetaCampaignPauseApprovalBridgeCandidateV2)
    object.__setattr__(
        forged,
        "_NativeMetaCampaignPauseApprovalBridgeCandidateV2__canonical_bytes",
        b'{"sentinel":true}',
    )
    material.clock.now = _T2

    with pytest.raises(AdhelmError) as rejected:
        _accept(
            material,
            pending,
            forged,
        )
    assert (
        rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_ACCEPTANCE_UNVERIFIED"
    )
    assert _claim_rows(material.ledger) == []

    with pytest.raises(AdhelmError) as replay:
        _accept(
            material,
            pending,
            cast(Any, forged),
        )
    assert replay.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED"
