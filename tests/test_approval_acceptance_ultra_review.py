"""Independent adversarial checks for local native-receipt acceptance."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_approval_protocol import (
    _EXPIRES,
    _NOW,
    _config,
    _fixture_action,
    _fixture_policy,
    _private_key,
    _public_x963,
    _signed_receipt,
)
from test_safety import delegated, direct_grant, kernel, plan

import adhelm.ledger as ledger_module
from adhelm.approval_acceptance import (
    PinnedApprovalTrust,
    accept_approval_receipt,
)
from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalChallengeKind,
    build_delegation_policy_install_challenge,
    build_delegation_policy_revoke_challenge,
    build_openai_pause_challenge,
    parse_approval_receipt_wire,
)
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.projections import export_verified
from adhelm.safety import DelegationPolicy, DirectGrant, SafetyKernel
from adhelm.saved_plans import retain_openai_pause_plan


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _pause_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, now: datetime | None = None
) -> tuple[Ledger, SafetyKernel, ApprovalChallenge, bytes, PinnedApprovalTrust, str]:
    accepted_at = _NOW + timedelta(seconds=2) if now is None else now
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(accepted_at))
    action = _fixture_action()
    ledger = Ledger(tmp_path, profile_ref="workspace-ledger-1")
    retained = retain_openai_pause_plan(ledger, action)
    key = _private_key()
    challenge = build_openai_pause_challenge(
        action,
        grantor_ref="customer-grantor-1",
        expires_at=_EXPIRES,
        configuration=_config(),
        nonce=bytes(range(32)),
        issued_at=_NOW,
        now=_NOW,
    )
    receipt = _signed_receipt(challenge, key=key, issued_at=_NOW + timedelta(seconds=1))
    trusted = PinnedApprovalTrust(
        _config(), _public_x963(key), workspace_profile_ref="workspace-ledger-1"
    )
    return (
        ledger,
        SafetyKernel(tmp_path, ledger, clock=lambda: accepted_at),
        challenge,
        receipt,
        trusted,
        retained["record_digest"],
    )


def _policy_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: DelegationPolicy,
    revoke_at: datetime | None = None,
) -> tuple[Ledger, SafetyKernel, ApprovalChallenge, bytes, PinnedApprovalTrust]:
    accepted_at = _NOW + timedelta(seconds=2)
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(accepted_at))
    key = _private_key()
    if revoke_at is None:
        challenge = build_delegation_policy_install_challenge(
            policy,
            expires_at=_EXPIRES,
            configuration=_config(),
            nonce=bytes(range(32)),
            issued_at=_NOW,
            now=_NOW,
        )
    else:
        challenge = build_delegation_policy_revoke_challenge(
            policy,
            revoked_at=revoke_at,
            expires_at=_EXPIRES,
            configuration=_config(),
            nonce=bytes(range(32)),
            issued_at=_NOW,
            now=_NOW,
        )
    receipt = _signed_receipt(challenge, key=key, issued_at=_NOW + timedelta(seconds=1))
    trusted = PinnedApprovalTrust(
        _config(), _public_x963(key), workspace_profile_ref="workspace-ledger-1"
    )
    ledger = Ledger(tmp_path, profile_ref="workspace-ledger-1")
    return (
        ledger,
        SafetyKernel(tmp_path, ledger, clock=lambda: accepted_at),
        challenge,
        receipt,
        trusted,
    )


class _PartialTypedHost:
    host_ref = "local-trusted-host"

    def __init__(self) -> None:
        self.generic_calls = 0
        self.install_calls = 0

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        del grant
        return True

    def verifies_policy_configuration(self, policy: DelegationPolicy) -> bool:
        del policy
        self.generic_calls += 1
        return True

    def verifies_policy_installation(self, policy: DelegationPolicy) -> bool:
        del policy
        self.install_calls += 1
        return True


class _TypedHost:
    host_ref = "local-trusted-host"

    def __init__(self, *, install: bool, revoke: bool) -> None:
        self.allow_install = install
        self.allow_revoke = revoke
        self.generic_calls = 0
        self.install_calls = 0
        self.revoke_calls = 0

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        del grant
        return True

    def verifies_policy_configuration(self, policy: DelegationPolicy) -> bool:
        del policy
        self.generic_calls += 1
        return True

    def verifies_policy_installation(self, policy: DelegationPolicy) -> bool:
        del policy
        self.install_calls += 1
        return self.allow_install

    def verifies_policy_revocation(self, policy: DelegationPolicy) -> bool:
        del policy
        self.revoke_calls += 1
        return self.allow_revoke


class _ExplodingDirectHost:
    @property
    def host_ref(self) -> str:
        raise RuntimeError("PRIVATE-HOST-REF-SENTINEL")

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        del grant
        return True

    def verifies_policy_configuration(self, policy: DelegationPolicy) -> bool:
        del policy
        return True


class _TruthyDirectHost:
    host_ref = "local-trusted-host"

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        del grant
        return 1  # type: ignore[return-value]

    def verifies_policy_configuration(self, policy: DelegationPolicy) -> bool:
        del policy
        return True


def test_partial_typed_policy_host_cannot_fall_back_to_legacy_generic_proof(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    host = _PartialTypedHost()
    policy = delegated(plan()).policy

    for operation in (instance.install_delegation_policy, instance.revoke_delegation_policy):
        with pytest.raises(AdhelmError) as raised:
            operation(policy, host)
        assert raised.value.code == "USER_APPROVAL_HOST_REQUIRED"

    assert host.generic_calls == host.install_calls == 0
    assert ledger.records("changes") == []


def test_complete_typed_policy_host_uses_only_matching_receipt_intent(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    host = _TypedHost(install=True, revoke=False)
    policy = delegated(plan()).policy

    instance.install_delegation_policy(policy, host)
    with pytest.raises(AdhelmError) as raised:
        instance.revoke_delegation_policy(policy, host)

    assert raised.value.code == "USER_APPROVAL_HOST_REQUIRED"
    assert host.install_calls >= 2  # initial proof, locked handoff, and optional final callback
    assert host.revoke_calls == 1
    assert host.generic_calls == 0
    assert [
        record["payload"]["event_type"]
        for record in ledger.records("changes")
        if record["record_kind"] == "CHANGE_EVENT"
    ] == ["APPROVAL_GRANTED"]


@pytest.mark.parametrize("host", (_ExplodingDirectHost(), _TruthyDirectHost()))
def test_direct_host_errors_and_truthy_values_never_create_authority(
    tmp_path: Path, host: object
) -> None:
    instance, ledger = kernel(tmp_path)

    with pytest.raises(AdhelmError) as raised:
        instance.record_direct_grant(plan(), direct_grant(plan()), host)  # type: ignore[arg-type]

    assert raised.value.code == "USER_APPROVAL_HOST_REQUIRED"
    assert "PRIVATE-HOST-REF-SENTINEL" not in str(raised.value)
    assert ledger.records("changes") == []


def test_pause_receipt_claims_before_exact_direct_grant_and_remains_historical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, instance, challenge, receipt, trusted, source_digest = _pause_material(
        tmp_path, monkeypatch
    )

    accepted = accept_approval_receipt(
        instance,
        ledger,
        receipt=receipt,
        expected_challenge=challenge,
        trust=trusted,
        source_plan_record_digest=source_digest,
    )

    records = ledger.records("changes")
    assert [record["record_kind"] for record in records] == [
        "SEGMENT_STARTED",
        "EXECUTION_PLAN",
        "APPROVAL_RECEIPT_CLAIMED",
        "CHANGE_EVENT",
    ]
    claim = records[2]
    assert claim["provider"] is claim["account_ref"] is claim["run_id"] is None
    assert claim["occurred_at"] is claim["observed_at"] is None
    assert claim["payload"]["source_plan_record_digest"] == source_digest
    assert accepted.kind is ApprovalChallengeKind.OPENAI_PAUSE_PLAN
    assert accepted.direct_grant is not None and accepted.policy is None
    assert accepted.direct_grant.authorization_id == claim["payload"]["receipt_id"]

    monkeypatch.setattr(
        ledger_module, "_utc_now", lambda: _timestamp(_EXPIRES + timedelta(seconds=1))
    )
    assert ledger.verify("changes")["status"] == "VERIFIED"


def test_wrong_trusted_pin_fails_before_history_or_source_plan_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, instance, challenge, receipt, _, _ = _pause_material(tmp_path, monkeypatch)
    wrong_key = _public_x963(_private_key())
    wrong_trust = PinnedApprovalTrust(
        _config(), wrong_key, workspace_profile_ref="workspace-ledger-1"
    )
    monkeypatch.setattr(
        ledger,
        "checkpoint",
        lambda: (_ for _ in ()).throw(AssertionError("trust mismatch read history")),
    )

    with pytest.raises(AdhelmError) as raised:
        accept_approval_receipt(
            instance,
            ledger,
            receipt=receipt,
            expected_challenge=challenge,
            trust=wrong_trust,
            source_plan_record_digest="f" * 64,
        )

    assert raised.value.code == "INVALID_APPROVAL_RECEIPT_SIGNATURE"
    assert "trust mismatch read history" not in str(raised.value)


def test_concurrent_receipt_consumption_allows_one_claim_and_one_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, instance, challenge, receipt, trusted, source_digest = _pause_material(
        tmp_path, monkeypatch
    )

    def consume() -> str:
        try:
            accept_approval_receipt(
                instance,
                ledger,
                receipt=receipt,
                expected_challenge=challenge,
                trust=trusted,
                source_plan_record_digest=source_digest,
            )
        except AdhelmError as error:
            return error.code
        return "ACCEPTED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(lambda _: consume(), range(2)))

    assert outcomes == ["ACCEPTED", "APPROVAL_RECEIPT_REPLAY"]
    records = ledger.records("changes")
    assert sum(record["record_kind"] == "APPROVAL_RECEIPT_CLAIMED" for record in records) == 1
    assert (
        sum(
            record["record_kind"] == "CHANGE_EVENT"
            and record["payload"]["event_type"] == "APPROVAL_GRANTED"
            for record in records
        )
        == 1
    )


def test_post_claim_recording_uncertainty_never_retries_the_consumed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, instance, challenge, receipt, trusted, source_digest = _pause_material(
        tmp_path, monkeypatch
    )

    def fail_direct_grant(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("PRIVATE-DURABLE-GRANT-SENTINEL")

    monkeypatch.setattr(instance, "record_direct_grant", fail_direct_grant)
    with pytest.raises(AdhelmError) as uncertain:
        accept_approval_receipt(
            instance,
            ledger,
            receipt=receipt,
            expected_challenge=challenge,
            trust=trusted,
            source_plan_record_digest=source_digest,
        )
    assert uncertain.value.code == "APPROVAL_RECORDING_UNVERIFIED"
    assert "PRIVATE-DURABLE-GRANT-SENTINEL" not in str(uncertain.value)

    with pytest.raises(AdhelmError) as replay:
        accept_approval_receipt(
            instance,
            ledger,
            receipt=receipt,
            expected_challenge=challenge,
            trust=trusted,
            source_plan_record_digest=source_digest,
        )
    assert replay.value.code == "APPROVAL_RECEIPT_REPLAY"
    records = ledger.records("changes")
    assert sum(record["record_kind"] == "APPROVAL_RECEIPT_CLAIMED" for record in records) == 1
    assert not any(record["record_kind"] == "CHANGE_EVENT" for record in records)


def test_constructed_challenge_and_raising_clock_fail_with_bounded_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, instance, challenge, receipt, trusted, source_digest = _pause_material(
        tmp_path, monkeypatch
    )
    forged = object.__new__(ApprovalChallenge)

    with pytest.raises(AdhelmError) as malformed:
        accept_approval_receipt(
            instance,
            ledger,
            receipt=b"PRIVATE-RAW-RECEIPT",
            expected_challenge=forged,
            trust=trusted,
            source_plan_record_digest=source_digest,
        )
    assert malformed.value.code == "APPROVAL_CHALLENGE_INVALID"
    assert "PRIVATE-RAW-RECEIPT" not in str(malformed.value)

    def broken_clock() -> datetime:
        raise RuntimeError("PRIVATE-CLOCK-SENTINEL")

    monkeypatch.setattr(instance, "clock", broken_clock)
    with pytest.raises(AdhelmError) as clock_error:
        accept_approval_receipt(
            instance,
            ledger,
            receipt=receipt,
            expected_challenge=challenge,
            trust=trusted,
            source_plan_record_digest=source_digest,
        )
    assert clock_error.value.code == "APPROVAL_CLOCK_INVALID"
    assert "PRIVATE-CLOCK-SENTINEL" not in str(clock_error.value)
    assert not any(
        record["record_kind"] == "APPROVAL_RECEIPT_CLAIMED" for record in ledger.records("changes")
    )


def test_expired_policy_and_future_scheduled_revoke_cannot_consume_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = _fixture_policy().model_copy(update={"expires_at": _NOW + timedelta(seconds=1)})
    ledger, instance, challenge, receipt, trusted = _policy_material(
        tmp_path / "expired", monkeypatch, policy=expired
    )

    with pytest.raises(AdhelmError):
        accept_approval_receipt(
            instance,
            ledger,
            receipt=receipt,
            expected_challenge=challenge,
            trust=trusted,
        )
    assert ledger.records("changes") == []

    active = _fixture_policy()
    ledger, instance, challenge, receipt, trusted = _policy_material(
        tmp_path / "scheduled",
        monkeypatch,
        policy=active,
        revoke_at=_NOW + timedelta(minutes=1),
    )
    with pytest.raises(AdhelmError):
        accept_approval_receipt(
            instance,
            ledger,
            receipt=receipt,
            expected_challenge=challenge,
            trust=trusted,
        )
    assert ledger.records("changes") == []


def test_expiry_between_direct_host_proof_and_durable_append_leaves_only_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = [_NOW + timedelta(seconds=2)]
    ledger, instance, challenge, receipt, trusted, source_digest = _pause_material(
        tmp_path, monkeypatch, now=current[0]
    )
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(current[0]))
    monkeypatch.setattr(instance, "clock", lambda: current[0])
    original_make_record = ledger._make_record

    def expire_before_callback(*args: object, **kwargs: object) -> dict[str, object]:
        record = original_make_record(*args, **kwargs)  # type: ignore[arg-type]
        if len(args) > 1 and args[1] == "CHANGE_EVENT":
            current[0] = _EXPIRES
        return record

    monkeypatch.setattr(ledger, "_make_record", expire_before_callback)
    with pytest.raises(AdhelmError) as raised:
        accept_approval_receipt(
            instance,
            ledger,
            receipt=receipt,
            expected_challenge=challenge,
            trust=trusted,
            source_plan_record_digest=source_digest,
        )
    assert raised.value.code == "APPROVAL_RECORDING_UNVERIFIED"
    records = ledger.records("changes")
    assert sum(record["record_kind"] == "APPROVAL_RECEIPT_CLAIMED" for record in records) == 1
    assert not any(record["record_kind"] == "CHANGE_EVENT" for record in records)


def test_exported_claim_uses_only_redacted_receipt_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, instance, challenge, receipt, trusted, source_digest = _pause_material(
        tmp_path / "data", monkeypatch
    )
    accepted = accept_approval_receipt(
        instance,
        ledger,
        receipt=receipt,
        expected_challenge=challenge,
        trust=trusted,
        source_plan_record_digest=source_digest,
    )
    receipt_data = parse_approval_receipt_wire(receipt)
    export_dir = tmp_path / "export"
    export_verified(ledger, export_dir, opaque_key=b"x" * 32)
    exported = (export_dir / "records.jsonl").read_text()

    assert accepted.receipt_digest in exported
    for raw in (
        trusted.public_key_x963.hex(),
        receipt_data["challenge_nonce"],
        receipt_data["signature"],
        _config().host_ref,
        _config().signing_key_ref,
    ):
        assert raw not in exported
    claim = next(
        json.loads(line)
        for line in exported.splitlines()
        if json.loads(line)["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
    )
    assert "challenge" not in claim["payload"]
    assert "receipt" not in claim["payload"]
    assert "host_configuration" not in claim["payload"]
    assert "verification_public_key_x963_hex" not in claim["payload"]
