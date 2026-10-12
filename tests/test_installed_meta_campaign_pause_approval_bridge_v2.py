"""Fake-only private Meta PAUSE V2 preparation and acceptance boundaries."""

from __future__ import annotations

import base64
import copy
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import adhelm._installed_meta_campaign_pause_approval_bridge_v2 as bridge
import adhelm.ledger as ledger_module
from adhelm._installed_meta_campaign_pause_approval_bridge_v2 import (
    _MetaCampaignPauseApprovalBridgeReviewedMetadataV2,
    _PendingInstalledMetaCampaignPauseApprovalBridgeV2,
    _PendingMetaCampaignPauseApprovalBridgeDataV2,
)
from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    NativeMetaCampaignPauseApprovalBridgeRequestV2,
    validate_meta_campaign_pause_approval_bridge_candidate_v2,
)
from adhelm._native_approval_bridge_protocol import build_native_approval_bridge_request_v1
from adhelm.approval_acceptance import AcceptedNativeApproval
from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalHostConfiguration,
    parse_approval_challenge_wire,
    receipt_signing_payload,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.providers.meta_pause_contract import MetaCampaignPausePlannedAction
from adhelm.safety import SafetyKernel
from adhelm.saved_meta_pause_plans import retain_meta_campaign_pause_plan

_ROOT = Path(__file__).resolve().parents[1]
_META_FIXTURE = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/MetaPause/"
    "meta-campaign-pause-approval-v1.json"
)
_V2_FIXTURE = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/"
    "NativeApprovalBridgeV2MetaPause/meta-campaign-pause-approval-bridge-v2.json"
)
_T0 = datetime(2030, 1, 1, 0, 0, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(seconds=1)
_T2 = _T0 + timedelta(seconds=2)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class _Material:
    ledger: Ledger
    kernel: SafetyKernel
    clock: _Clock
    configuration: ApprovalHostConfiguration
    metadata_digest: str
    record_digest: str
    reviewed_metadata: _MetaCampaignPauseApprovalBridgeReviewedMetadataV2


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _fixture_data(path: Path) -> dict[str, object]:
    value = parse_json(path.read_bytes())
    assert type(value) is dict
    return value


def _configuration() -> ApprovalHostConfiguration:
    fixture = _fixture_data(_META_FIXTURE)
    raw = fixture["configuration"]
    assert type(raw) is dict
    return ApprovalHostConfiguration(**raw)


def _fixture_action(configuration: ApprovalHostConfiguration) -> MetaCampaignPausePlannedAction:
    fixture = _fixture_data(_META_FIXTURE)
    raw = fixture["challenge"]
    assert type(raw) is dict
    challenge = parse_approval_challenge_wire(canonical_bytes(raw), configuration=configuration)
    assert challenge.meta_pause_action is not None
    return challenge.meta_pause_action


def _metadata_digest() -> str:
    fixture = _fixture_data(_V2_FIXTURE)
    cases = fixture["source_cases"]
    assert type(cases) is list and cases
    first = cases[0]
    assert type(first) is dict
    metadata = first["metadata_fixture"]
    assert type(metadata) is dict
    return hashlib.sha256(canonical_bytes(metadata)).hexdigest()


@pytest.fixture
def material(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Material:
    configuration = _configuration()
    action = _fixture_action(configuration)
    ledger = Ledger(tmp_path, profile_ref=action.plan.profile_ref)
    source = retain_meta_campaign_pause_plan(ledger, action)
    clock = _Clock(_T0)
    kernel = SafetyKernel(tmp_path, ledger, clock=clock)
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(_T2))
    metadata_digest = _metadata_digest()
    reviewed_metadata = bridge._make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2(
        configuration,
        metadata_digest,
    )
    return _Material(
        ledger=ledger,
        kernel=kernel,
        clock=clock,
        configuration=configuration,
        metadata_digest=metadata_digest,
        record_digest=source["record_digest"],
        reviewed_metadata=reviewed_metadata,
    )


def _pending(material: _Material) -> _PendingInstalledMetaCampaignPauseApprovalBridgeV2:
    return bridge._prepare_installed_meta_campaign_pause_approval_bridge_v2(
        material.kernel,
        material.ledger,
        source_plan_record_digest=material.record_digest,
        reviewed_metadata=material.reviewed_metadata,
        nonce=bytes(range(32)),
        request_id=bytes(range(32, 64)),
    )


def _pending_data(
    pending: _PendingInstalledMetaCampaignPauseApprovalBridgeV2,
) -> _PendingMetaCampaignPauseApprovalBridgeDataV2:
    data = object.__getattribute__(
        pending,
        "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__data",
    )
    assert type(data) is _PendingMetaCampaignPauseApprovalBridgeDataV2
    return data


def _candidate(
    pending: _PendingInstalledMetaCampaignPauseApprovalBridgeV2,
    material: _Material,
    *,
    native_observed_at: datetime = _T2,
) -> NativeMetaCampaignPauseApprovalBridgeCandidateV2:
    data = _pending_data(pending)
    challenge = object.__getattribute__(data, "challenge")
    request = object.__getattribute__(data, "request")
    assert type(challenge) is ApprovalChallenge
    assert type(request) is NativeMetaCampaignPauseApprovalBridgeRequestV2
    request_data = parse_json(request.canonical_bytes)
    assert type(request_data) is dict
    request_id_text = request_data["request_id"]
    assert type(request_id_text) is str
    request_id = base64.urlsafe_b64decode(request_id_text + "=" * (-len(request_id_text) % 4))

    key = ec.generate_private_key(ec.SECP256R1())
    signing = receipt_signing_payload(
        challenge,
        configuration=material.configuration,
        receipt_id="approval-receipt:" + "M" * 43,
        issued_at=_T1,
    )
    receipt_data = parse_json(signing.canonical_unsigned_receipt)
    assert type(receipt_data) is dict
    receipt_data["signature"] = _b64(
        key.sign(signing.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
    )
    receipt = canonical_bytes(receipt_data)
    public_key = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    raw = canonical_bytes(
        {
            "schema_version": 2,
            "message_type": "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_CANDIDATE_V2",
            "native_observation_status": "NATIVE_CURRENT_RECEIPT_VERIFIED_AND_NONCE_CONSUMED",
            "request_id_digest": hashlib.sha256(request_id).hexdigest(),
            "request_digest": request.digest,
            "source_context_digest": request.source_context_digest,
            "metadata_digest": material.metadata_digest,
            "protocol_configuration_digest": material.configuration.configuration_digest,
            "canonical_challenge_b64url": _b64(challenge.canonical_bytes),
            "challenge_digest": challenge.digest,
            "challenge_nonce_digest": hashlib.sha256(challenge.nonce).hexdigest(),
            "receipt_kind": challenge.kind.value,
            "expires_at": challenge.expires_at_text,
            "canonical_receipt_b64url": _b64(receipt),
            "receipt_digest": hashlib.sha256(receipt).hexdigest(),
            "receipt_id": receipt_data["receipt_id"],
            "active_public_key_x963_b64url": _b64(public_key),
            "active_public_key_fingerprint": hashlib.sha256(public_key).hexdigest(),
            "native_observed_at": _timestamp(native_observed_at),
        }
    )
    return validate_meta_campaign_pause_approval_bridge_candidate_v2(
        raw,
        request=request,
        configuration=material.configuration,
        expected_metadata_digest=material.metadata_digest,
        exchange_started_at=_T0,
        current_now=native_observed_at,
    )


def _accept(
    material: _Material,
    pending: _PendingInstalledMetaCampaignPauseApprovalBridgeV2,
    candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2,
) -> AcceptedNativeApproval:
    return bridge._accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
        material.kernel,
        material.ledger,
        pending=pending,
        candidate=candidate,
        exchange_started_at=_T0,
        minimum_current_time=_T2,
    )


def _claim_rows(ledger: Ledger) -> list[dict[str, Any]]:
    return [
        row for row in ledger.records("changes") if row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
    ]


def test_prepare_derives_fixed_logical_grantor_and_detaches_metadata(
    material: _Material,
) -> None:
    pending = _pending(material)
    data = _pending_data(pending)
    challenge = object.__getattribute__(data, "challenge")
    request = object.__getattribute__(data, "request")
    assert type(challenge) is ApprovalChallenge
    assert type(request) is NativeMetaCampaignPauseApprovalBridgeRequestV2
    assert challenge.grantor_ref == (
        "logical:adhelm-meta-campaign-pause-approval-bridge-v2:" + material.record_digest
    )
    assert request.kind.value == "META_CAMPAIGN_PAUSE_PLAN"
    assert material.metadata_digest not in repr(material.reviewed_metadata)
    assert material.configuration.host_ref not in repr(material.reviewed_metadata)


def test_real_prepared_lock_consumes_and_forged_lock_rejects_before_candidate(
    material: _Material, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _pending(material)
    # This crosses the actual `_thread.LockType` path rather than a test double.
    assert bridge._pending_data(pending)
    with pytest.raises(AdhelmError) as replay:
        bridge._pending_data(pending)
    assert replay.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED"

    forged = _pending(material)
    object.__setattr__(forged, "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__lock", object())
    monkeypatch.setattr(
        bridge,
        "_accept_native_meta_campaign_pause_approval_bridge_candidate_v2",
        lambda *args, **kwargs: pytest.fail("candidate handling must not start for forged pending"),
    )
    with pytest.raises(AdhelmError) as rejected:
        bridge._accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
            material.kernel,
            material.ledger,
            pending=forged,
            candidate=cast(Any, object()),
            exchange_started_at=_T0,
            minimum_current_time=_T2,
        )
    assert rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_INVALID"


def test_candidate_is_claimed_once_without_provider_apply(material: _Material) -> None:
    pending = _pending(material)
    candidate = _candidate(pending, material)
    material.clock.now = _T2

    accepted = _accept(material, pending, candidate)

    assert accepted.direct_grant is not None
    assert accepted.kind.value == "META_CAMPAIGN_PAUSE_PLAN"
    assert len(_claim_rows(material.ledger)) == 1
    events = [row["payload"] for row in material.ledger.records("changes")]
    assert any(event.get("event_type") == "APPROVAL_GRANTED" for event in events)
    assert not any(event.get("event_type") == "APPLY_STARTED" for event in events)


def test_source_prefix_change_after_candidate_is_ambiguous_and_non_retryable(
    material: _Material, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _pending(material)
    candidate = _candidate(pending, material)
    material.clock.now = _T2
    checkpoint = copy.deepcopy(material.ledger.checkpoint())
    streams = checkpoint["streams"]
    assert type(streams) is dict
    changes = streams["changes"]
    assert type(changes) is dict
    changes["stream_id"] = "replaced:changes"
    monkeypatch.setattr(material.ledger, "checkpoint", lambda: checkpoint)

    with pytest.raises(AdhelmError) as rejected:
        _accept(material, pending, candidate)
    assert (
        rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_ACCEPTANCE_UNVERIFIED"
    )
    assert _claim_rows(material.ledger) == []
    with pytest.raises(AdhelmError) as reused:
        _accept(material, pending, candidate)
    assert reused.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED"


def test_coordinator_and_current_time_failures_after_candidate_are_ambiguous(
    material: _Material,
) -> None:
    pending = _pending(material)
    candidate = _candidate(pending, material)
    material.clock.now = _T2
    foreign_ledger = Ledger(
        material.ledger.root / "foreign", profile_ref=material.ledger.profile_ref
    )
    foreign_kernel = SafetyKernel(foreign_ledger.root, foreign_ledger, clock=lambda: _T2)
    with pytest.raises(AdhelmError) as mismatch:
        bridge._accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
            foreign_kernel,
            material.ledger,
            pending=pending,
            candidate=candidate,
            exchange_started_at=_T0,
            minimum_current_time=_T2,
        )
    assert (
        mismatch.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_ACCEPTANCE_UNVERIFIED"
    )

    pending = _pending(material)
    candidate = _candidate(pending, material)
    material.clock.now = _T1
    with pytest.raises(AdhelmError) as rollback:
        _accept(material, pending, candidate)
    assert (
        rollback.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_ACCEPTANCE_UNVERIFIED"
    )
    assert _claim_rows(material.ledger) == []


def test_pending_data_corruption_rejects_before_candidate_handling(
    material: _Material, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _pending(material)
    object.__setattr__(
        pending,
        "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__data",
        object(),
    )
    monkeypatch.setattr(
        bridge,
        "_accept_native_meta_campaign_pause_approval_bridge_candidate_v2",
        lambda *args, **kwargs: pytest.fail(
            "candidate handling must not start for corrupt pending"
        ),
    )
    with pytest.raises(AdhelmError) as rejected:
        bridge._accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
            material.kernel,
            material.ledger,
            pending=pending,
            candidate=cast(Any, object()),
            exchange_started_at=_T0,
            minimum_current_time=_T2,
        )
    assert rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_INVALID"


def test_v1_route_refuses_meta_and_v2_private_module_has_no_transport_or_resolver(
    material: _Material,
) -> None:
    pending = _pending(material)
    data = _pending_data(pending)
    challenge = object.__getattribute__(data, "challenge")
    assert type(challenge) is ApprovalChallenge
    with pytest.raises(AdhelmError):
        build_native_approval_bridge_request_v1(
            challenge,
            configuration=material.configuration,
            request_id=bytes(range(32)),
            source_context_digest="a" * 64,
            expected_metadata_digest=material.metadata_digest,
        )
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "_resolve_pipe_adapter",
        "exchange_native_approval_bridge_once_v1",
        "PinnedApprovalTrust",
        "subprocess",
        "httpx",
    ):
        assert forbidden not in source
