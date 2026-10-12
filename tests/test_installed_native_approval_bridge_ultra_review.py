"""Independent adversarial coverage for the private installed approval bridge."""

from __future__ import annotations

import copy
import pickle
from pathlib import Path

import pytest
from test_installed_native_approval_bridge import (
    _candidate_for_pending,
    _FakeAdapter,
    _prepared,
)

import adhelm._installed_native_approval_bridge as bridge
import adhelm.approval_acceptance as acceptance
from adhelm.errors import AdhelmError


def _pending_data(value: object) -> object:
    return object.__getattribute__(value, "_PendingInstalledApprovalBridgeRequest__data")


def test_corrupted_pending_source_rejects_before_native_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed opaque pending value must not burn a native nonce first."""
    ledger, kernel, pending, configuration, _source_digest = _prepared(tmp_path, monkeypatch)
    candidate = _candidate_for_pending(pending, configuration=configuration)
    data = _pending_data(pending)
    object.__setattr__(data, "source", object())
    adapter = _FakeAdapter(candidate)
    monkeypatch.setattr(bridge, "_resolve_pipe_adapter", lambda: adapter)

    with pytest.raises(AdhelmError) as raised:
        bridge._exchange_and_record_pending_installed_native_approval(
            kernel, ledger, pending=pending
        )

    assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_PENDING_INVALID"
    assert adapter.calls == []


def test_creative_stream_prefix_change_after_candidate_blocks_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every checkpoint stream, not only changes, remains part of the exchange bind."""
    ledger, kernel, pending, configuration, _source_digest = _prepared(tmp_path, monkeypatch)
    adapter = _FakeAdapter(_candidate_for_pending(pending, configuration=configuration))
    monkeypatch.setattr(bridge, "_resolve_pipe_adapter", lambda: adapter)
    checkpoint = copy.deepcopy(ledger.checkpoint())
    checkpoint["streams"]["creative"]["stream_id"] = "replaced-creative-stream"
    monkeypatch.setattr(ledger, "checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        bridge,
        "_accept_native_approval_bridge_candidate",
        lambda *args, **kwargs: pytest.fail("must not claim after a stream-prefix mismatch"),
    )

    with pytest.raises(AdhelmError) as raised:
        bridge._exchange_and_record_pending_installed_native_approval(
            kernel, ledger, pending=pending
        )

    assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_ACCEPTANCE_UNVERIFIED"
    assert adapter.calls


def test_private_pending_is_redacted_and_not_pickleable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request wrapper must not turn source facts into an export surface."""
    _ledger, _kernel, pending, _configuration, source_digest = _prepared(tmp_path, monkeypatch)

    assert repr(pending) == "<pending installed approval bridge request>"
    assert source_digest not in repr(pending)
    with pytest.raises(TypeError):
        pickle.dumps(pending)


def test_native_handoff_never_enters_the_legacy_pinned_trust_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opaque native candidate cannot be reinterpreted as caller-supplied trust."""
    ledger, kernel, pending, configuration, _source_digest = _prepared(tmp_path, monkeypatch)
    adapter = _FakeAdapter(_candidate_for_pending(pending, configuration=configuration))
    monkeypatch.setattr(bridge, "_resolve_pipe_adapter", lambda: adapter)
    monkeypatch.setattr(
        acceptance,
        "_checked_trust",
        lambda *args, **kwargs: pytest.fail("legacy pinned-trust validation must not run"),
    )

    accepted = bridge._exchange_and_record_pending_installed_native_approval(
        kernel, ledger, pending=pending
    )

    assert accepted.direct_grant is not None
    assert adapter.calls
