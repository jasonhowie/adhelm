"""Independent fake-only adversarial checks for the M48 pending exchange."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from test_meta_campaign_pause_pending_exchange_v2 import (
    _Cancellation,
    _claims,
    _exchange,
    _FakePosixOperations,
    _Material,
    _pending,
)
from test_meta_campaign_pause_pending_exchange_v2 import material as _base_material

import adhelm._installed_meta_campaign_pause_approval_bridge_v2 as bridge
import adhelm._meta_campaign_pause_approval_bridge_transport_v2 as transport
import adhelm._native_approval_bridge_transport as pipes
from adhelm.errors import AdhelmError, ExitCode


@pytest.fixture
def material(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Material:
    """Reuse the owner's temporary ledger and synthetic V2 material only."""
    factory = cast(
        Callable[[Path, pytest.MonkeyPatch], _Material],
        _base_material.__wrapped__,  # type: ignore[attr-defined]
    )
    return factory(tmp_path, monkeypatch)


def _assert_spent_without_second_launch(
    material: _Material,
    pending: bridge._PendingInstalledMetaCampaignPauseApprovalBridgeV2,
) -> None:
    replay_operations = _FakePosixOperations(material)
    with pytest.raises(AdhelmError) as replay:
        _exchange(material, pending, replay_operations)
    assert replay.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED"
    assert replay_operations.launch_specs == []


def test_malformed_pending_rejects_before_m47_or_fake_helper_launch(
    material: _Material,
) -> None:
    """A type-shaped pending cannot reach the exchange or disclose its state."""
    pending = _pending(material)
    object.__setattr__(
        pending,
        "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__data",
        object(),
    )
    operations = _FakePosixOperations(material)

    with pytest.raises(AdhelmError) as rejected:
        _exchange(material, pending, operations)

    assert rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_INVALID"
    assert rejected.value.exit_code == ExitCode.PRECONDITION
    assert operations.launch_specs == []
    assert operations.writes == []
    assert _claims(material.ledger) == []


def test_prelaunch_cancellation_spends_pending_without_starting_fixed_exchange(
    material: _Material,
) -> None:
    """Cancellation after consumption cannot preserve a retryable pending value."""
    pending = _pending(material)
    material.clock.now = material.clock.now.replace(second=2)
    operations = _FakePosixOperations(material)

    with pytest.raises(AdhelmError) as cancelled:
        _exchange(material, pending, operations, cancellation=_Cancellation(True))

    assert (
        cancelled.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_TRANSPORT_CANCELLED"
    )
    assert cancelled.value.exit_code == ExitCode.PRECONDITION
    assert operations.launch_specs == []
    assert operations.writes == []
    assert _claims(material.ledger) == []
    _assert_spent_without_second_launch(material, pending)


def test_default_v1_adapter_is_refused_after_consumption_without_v1_launch(
    material: _Material,
) -> None:
    """The composition cannot silently route a Meta V2 pending request to V1."""
    pending = _pending(material)
    material.clock.now = material.clock.now.replace(second=2)
    v1_operations = _FakePosixOperations(material)
    v1_adapter = pipes._PosixNativeApprovalBridgePipeAdapter(
        cast(pipes._NativeApprovalBridgePosixOperations, v1_operations)
    )

    with pytest.raises(AdhelmError) as rejected:
        bridge._exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
            material.kernel,
            material.ledger,
            pending=pending,
            adapter=cast(transport._MetaCampaignPauseApprovalBridgePipeAdapterV2, v1_adapter),
            cancellation=_Cancellation(),
        )

    assert (
        rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_TRANSPORT_UNAVAILABLE"
    )
    assert rejected.value.exit_code == ExitCode.PRECONDITION
    assert v1_operations.launch_specs == []
    assert v1_operations.writes == []
    assert _claims(material.ledger) == []
    _assert_spent_without_second_launch(material, pending)
