"""Real retained Meta planning into private V2 acceptance, with synthetic native facts."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_meta_pause_kernel import _GuardedMetaAdapter
from test_meta_pause_planning_integration import _INSPECTOR, _SUBJECT, _Runtime
from test_meta_pause_planning_integration import runtime as runtime

from adhelm._installed_meta_campaign_pause_approval_bridge_v2 import (
    _accept_pending_installed_meta_campaign_pause_approval_bridge_v2,
    _make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2,
    _prepare_installed_meta_campaign_pause_approval_bridge_v2,
)
from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    NativeMetaCampaignPauseApprovalBridgeRequestV2,
    validate_meta_campaign_pause_approval_bridge_candidate_v2,
)
from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalHostConfiguration,
    receipt_signing_payload,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.safety import ApplyOutcome, EffectStatus, SafetyKernel
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _synthetic_candidate(
    request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    challenge: ApprovalChallenge,
    configuration: ApprovalHostConfiguration,
    metadata_digest: str,
    exchange_started_at: datetime,
    observed_at: datetime,
) -> NativeMetaCampaignPauseApprovalBridgeCandidateV2:
    """Represent the future native exchange using an ephemeral test key only."""
    key = ec.generate_private_key(ec.SECP256R1())
    signing = receipt_signing_payload(
        challenge,
        configuration=configuration,
        receipt_id="approval-receipt:" + "V" * 43,
        issued_at=observed_at,
    )
    receipt = parse_json(signing.canonical_unsigned_receipt)
    assert type(receipt) is dict
    receipt["signature"] = _b64(key.sign(signing.domain_separated_bytes, ec.ECDSA(hashes.SHA256())))
    receipt_bytes = canonical_bytes(receipt)
    public_key = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    request_data = parse_json(request.canonical_bytes)
    assert type(request_data) is dict
    request_id_text = request_data["request_id"]
    assert type(request_id_text) is str
    request_id = base64.urlsafe_b64decode(request_id_text + "=" * (-len(request_id_text) % 4))
    candidate = {
        "schema_version": 2,
        "message_type": "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_CANDIDATE_V2",
        "native_observation_status": "NATIVE_CURRENT_RECEIPT_VERIFIED_AND_NONCE_CONSUMED",
        "request_id_digest": hashlib.sha256(request_id).hexdigest(),
        "request_digest": request.digest,
        "source_context_digest": request.source_context_digest,
        "metadata_digest": metadata_digest,
        "protocol_configuration_digest": configuration.configuration_digest,
        "canonical_challenge_b64url": _b64(challenge.canonical_bytes),
        "challenge_digest": challenge.digest,
        "challenge_nonce_digest": hashlib.sha256(challenge.nonce).hexdigest(),
        "receipt_kind": challenge.kind.value,
        "expires_at": challenge.expires_at_text,
        "canonical_receipt_b64url": _b64(receipt_bytes),
        "receipt_digest": hashlib.sha256(receipt_bytes).hexdigest(),
        "receipt_id": receipt["receipt_id"],
        "active_public_key_x963_b64url": _b64(public_key),
        "active_public_key_fingerprint": hashlib.sha256(public_key).hexdigest(),
        "native_observed_at": observed_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }
    return validate_meta_campaign_pause_approval_bridge_candidate_v2(
        canonical_bytes(candidate),
        request=request,
        configuration=configuration,
        expected_metadata_digest=metadata_digest,
        exchange_started_at=exchange_started_at,
        current_now=observed_at,
    )


@pytest.mark.parametrize("scenario", ["normal", "history_extension", "expired_after_candidate"])
def test_configured_plan_private_v2_handoff_preserves_source_and_single_use(
    runtime: _Runtime, scenario: str
) -> None:
    prepared = runtime.plan()
    record_digest = prepared["data"]["record_digest"]
    ledger = Ledger(runtime.root)
    action, _provenance = load_meta_campaign_pause_plan(ledger, record_digest)
    now = datetime.now(UTC)
    observed_at = now + timedelta(microseconds=(-now.microsecond) % 1000)
    clock = [now]
    kernel = SafetyKernel(runtime.root, ledger, clock=lambda: clock[0])
    configuration = ApprovalHostConfiguration(
        host_ref="host:synthetic-meta-v2-integration",
        signing_key_ref="key:synthetic-meta-v2-integration",
    )
    metadata_digest = hashlib.sha256(b"synthetic-held-meta-v2-metadata").hexdigest()
    reviewed = _make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2(
        configuration, metadata_digest
    )
    pending = _prepare_installed_meta_campaign_pause_approval_bridge_v2(
        kernel,
        ledger,
        source_plan_record_digest=record_digest,
        reviewed_metadata=reviewed,
    )
    # Test-only inspection of held request facts substitutes for the unbuilt
    # transport. Production exposes no raw receipt/key or pending-data getter.
    data = object.__getattribute__(
        pending, "_PendingInstalledMetaCampaignPauseApprovalBridgeV2__data"
    )
    challenge = data.challenge
    request = data.request
    candidate = _synthetic_candidate(
        request, challenge, configuration, metadata_digest, now, observed_at
    )
    if scenario == "history_extension":
        runtime.plan()
    clock[0] = observed_at
    before = list(ledger.records("changes"))
    if scenario == "expired_after_candidate":
        clock[0] = challenge.expires_at
        with pytest.raises(AdhelmError) as rejected:
            _accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
                kernel,
                ledger,
                pending=pending,
                candidate=candidate,
                exchange_started_at=now,
                minimum_current_time=observed_at,
            )
        assert "UNVERIFIED" in rejected.value.code
        assert list(ledger.records("changes")) == before
    else:
        accepted = _accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
            kernel,
            ledger,
            pending=pending,
            candidate=candidate,
            exchange_started_at=now,
            minimum_current_time=observed_at,
        )
        assert accepted.direct_grant is not None
        assert accepted.direct_grant.plan_digest == prepared["data"]["plan_digest"]
        assert accepted.direct_grant.subplan_digests == action.plan.subplan_digests
        adapter = _GuardedMetaAdapter(
            action, ledger=ledger, outcome=ApplyOutcome.CONFIRMED, state_after_apply=True
        )
        result = kernel.execute(action.plan, accepted.direct_grant, {"meta_ads": adapter})
        assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
        assert adapter.apply_calls == 1
        assert ledger.unresolved_starts() == []
    after = list(ledger.records("changes"))
    with pytest.raises(AdhelmError) as reused:
        _accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
            kernel,
            ledger,
            pending=pending,
            candidate=candidate,
            exchange_started_at=now,
            minimum_current_time=observed_at,
        )
    assert reused.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED"
    assert list(ledger.records("changes")) == after
    expected_reads = ["/v26.0/debug_token", "/v26.0/act_12345", "/v26.0/987654321"]
    assert runtime.paths == expected_reads * (2 if scenario == "history_extension" else 1)
    history = b"".join(path.read_bytes() for path in runtime.root.rglob("*.jsonl"))
    assert _SUBJECT.encode() not in history
    assert _INSPECTOR.encode() not in history
