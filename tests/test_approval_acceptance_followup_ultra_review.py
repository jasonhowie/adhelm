"""Follow-up adversarial checks for policy receipt transition serialization.

These use only ephemeral P-256 test keys and the local JSONL ledger.  They do
not contact a host, Keychain, provider, or external service.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event

import pytest
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from test_approval_acceptance_ultra_review import _timestamp
from test_approval_protocol import (
    _EXPIRES,
    _NOW,
    _config,
    _fixture_policy,
    _private_key,
    _public_x963,
    _signed_receipt,
)

import adhelm.ledger as ledger_module
from adhelm.approval_acceptance import PinnedApprovalTrust, accept_approval_receipt
from adhelm.approval_protocol import (
    ApprovalChallenge,
    build_delegation_policy_install_challenge,
    build_delegation_policy_revoke_challenge,
)
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.safety import DelegationPolicy, SafetyKernel


def _policy_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Ledger, SafetyKernel, EllipticCurvePrivateKey, PinnedApprovalTrust]:
    now = _NOW + timedelta(seconds=2)
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(now))
    key = _private_key()
    ledger = Ledger(tmp_path, profile_ref="workspace-ledger-1")
    return (
        ledger,
        SafetyKernel(tmp_path, ledger, clock=lambda: now),
        key,
        PinnedApprovalTrust(
            _config(), _public_x963(key), workspace_profile_ref="workspace-ledger-1"
        ),
    )


def _policy_receipt(
    policy: DelegationPolicy,
    key: EllipticCurvePrivateKey,
    *,
    nonce_value: int,
    receipt_marker: str,
    revoke: bool,
) -> tuple[ApprovalChallenge, bytes]:
    if revoke:
        challenge = build_delegation_policy_revoke_challenge(
            policy,
            revoked_at=_NOW + timedelta(seconds=1),
            expires_at=_EXPIRES,
            configuration=_config(),
            nonce=bytes([nonce_value]) * 32,
            issued_at=_NOW,
            now=_NOW,
        )
    else:
        challenge = build_delegation_policy_install_challenge(
            policy,
            expires_at=_EXPIRES,
            configuration=_config(),
            nonce=bytes([nonce_value]) * 32,
            issued_at=_NOW,
            now=_NOW,
        )
    return (
        challenge,
        _signed_receipt(
            challenge,
            key=key,
            issued_at=_NOW + timedelta(seconds=1),
            receipt_id="approval-receipt:" + receipt_marker * 43,
        ),
    )


def _accept(
    kernel: SafetyKernel,
    ledger: Ledger,
    challenge: ApprovalChallenge,
    receipt: bytes,
    trust: PinnedApprovalTrust,
) -> None:
    accept_approval_receipt(
        kernel,
        ledger,
        receipt=receipt,
        expected_challenge=challenge,
        trust=trust,
    )


def _claims(ledger: Ledger) -> list[dict[str, object]]:
    return [
        record
        for record in ledger.records("changes")
        if record["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
    ]


def _policy_events(ledger: Ledger) -> list[dict[str, object]]:
    return [
        record for record in ledger.records("changes") if record["record_kind"] == "CHANGE_EVENT"
    ]


def test_orphan_revocation_is_rejected_before_receipt_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, kernel, key, trust = _policy_context(tmp_path, monkeypatch)
    challenge, receipt = _policy_receipt(
        _fixture_policy(), key, nonce_value=1, receipt_marker="A", revoke=True
    )

    with pytest.raises(AdhelmError) as raised:
        _accept(kernel, ledger, challenge, receipt, trust)

    assert raised.value.code == "DELEGATION_NOT_INSTALLED"
    assert ledger.records("changes") == []


def test_stale_install_and_revoke_each_fail_before_consuming_a_new_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, kernel, key, trust = _policy_context(tmp_path, monkeypatch)
    v1 = _fixture_policy()
    v2 = v1.model_copy(update={"policy_version": 2})
    challenge, receipt = _policy_receipt(v2, key, nonce_value=2, receipt_marker="B", revoke=False)
    _accept(kernel, ledger, challenge, receipt, trust)
    assert len(_claims(ledger)) == 1

    stale_install, stale_install_receipt = _policy_receipt(
        v1, key, nonce_value=3, receipt_marker="C", revoke=False
    )
    with pytest.raises(AdhelmError) as install_error:
        _accept(kernel, ledger, stale_install, stale_install_receipt, trust)
    assert install_error.value.code == "DELEGATION_POLICY_VERSION_REPLAY"
    assert len(_claims(ledger)) == 1

    stale_revoke, stale_revoke_receipt = _policy_receipt(
        v1, key, nonce_value=4, receipt_marker="D", revoke=True
    )
    with pytest.raises(AdhelmError) as revoke_error:
        _accept(kernel, ledger, stale_revoke, stale_revoke_receipt, trust)
    assert revoke_error.value.code == "DELEGATION_POLICY_DRIFT"
    assert len(_claims(ledger)) == 1


def test_digest_mismatched_and_already_revoked_policy_cannot_burn_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, kernel, key, trust = _policy_context(tmp_path, monkeypatch)
    policy = _fixture_policy()
    install, install_receipt = _policy_receipt(
        policy, key, nonce_value=5, receipt_marker="E", revoke=False
    )
    _accept(kernel, ledger, install, install_receipt, trust)

    mismatched = policy.model_copy(update={"authorized_agent_or_host_ref": "other-agent-1"})
    mismatch, mismatch_receipt = _policy_receipt(
        mismatched, key, nonce_value=6, receipt_marker="F", revoke=True
    )
    with pytest.raises(AdhelmError) as mismatch_error:
        _accept(kernel, ledger, mismatch, mismatch_receipt, trust)
    assert mismatch_error.value.code == "DELEGATION_POLICY_DRIFT"
    assert len(_claims(ledger)) == 1

    revoke, revoke_receipt = _policy_receipt(
        policy, key, nonce_value=7, receipt_marker="G", revoke=True
    )
    _accept(kernel, ledger, revoke, revoke_receipt, trust)
    assert len(_claims(ledger)) == 2

    repeat, repeat_receipt = _policy_receipt(
        policy, key, nonce_value=8, receipt_marker="H", revoke=True
    )
    with pytest.raises(AdhelmError) as repeat_error:
        _accept(kernel, ledger, repeat, repeat_receipt, trust)
    assert repeat_error.value.code == "DELEGATION_NOT_INSTALLED"
    assert len(_claims(ledger)) == 2
    assert [record["payload"]["event_type"] for record in _policy_events(ledger)] == [
        "APPROVAL_GRANTED",
        "RECOVERY_RESULT",
    ]


def test_concurrent_install_and_revoke_serialize_one_claimed_transition_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, kernel, key, trust = _policy_context(tmp_path, monkeypatch)
    policy = _fixture_policy()
    install, install_receipt = _policy_receipt(
        policy, key, nonce_value=9, receipt_marker="I", revoke=False
    )
    revoke, revoke_receipt = _policy_receipt(
        policy, key, nonce_value=10, receipt_marker="J", revoke=True
    )
    install_claimed = Event()
    release_install = Event()
    original_append = ledger.append

    def append_with_install_barrier(*args: object, **kwargs: object) -> dict[str, object]:
        record = original_append(*args, **kwargs)  # type: ignore[arg-type]
        if args[1] == "APPROVAL_RECEIPT_CLAIMED" and args[2]["receipt_kind"] == (
            "DELEGATION_POLICY_INSTALL"
        ):
            install_claimed.set()
            assert release_install.wait(timeout=5)
        return record

    monkeypatch.setattr(ledger, "append", append_with_install_barrier)

    def accept_install() -> str:
        _accept(kernel, ledger, install, install_receipt, trust)
        return "install"

    def accept_revoke() -> str:
        _accept(kernel, ledger, revoke, revoke_receipt, trust)
        return "revoke"

    with ThreadPoolExecutor(max_workers=2) as pool:
        install_future = pool.submit(accept_install)
        assert install_claimed.wait(timeout=5)
        revoke_future = pool.submit(accept_revoke)
        release_install.set()
        assert {install_future.result(timeout=5), revoke_future.result(timeout=5)} == {
            "install",
            "revoke",
        }

    assert len(_claims(ledger)) == 2
    assert [record["payload"]["event_type"] for record in _policy_events(ledger)] == [
        "APPROVAL_GRANTED",
        "RECOVERY_RESULT",
    ]
