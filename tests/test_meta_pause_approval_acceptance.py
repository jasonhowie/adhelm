"""Exact Meta plan receipt consumption with synthetic keys and temporary history."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import adhelm.ledger as ledger_module
from adhelm._installed_native_approval_bridge import _selected_source_record
from adhelm._native_approval_bridge_protocol import build_native_approval_bridge_request_v1
from adhelm.approval_acceptance import PinnedApprovalTrust, accept_approval_receipt
from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalChallengeKind,
    ApprovalHostConfiguration,
    build_meta_campaign_pause_challenge,
    receipt_signing_payload,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.projections import export_verified
from adhelm.providers.meta_pause_contract import (
    MetaCampaignPauseAccountBinding,
    MetaCampaignPauseCampaignBaseline,
    MetaCampaignPauseExecutionBinding,
    MetaCampaignPauseObservationBinding,
    MetaCampaignPausePlannedAction,
    build_meta_campaign_pause_plan,
)
from adhelm.safety import SafetyKernel
from adhelm.saved_meta_pause_plans import retain_meta_campaign_pause_plan

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_CONFIGURATION = ApprovalHostConfiguration(
    host_ref="host:adhelm-macos-approval-v1", signing_key_ref="approval-key:synthetic-meta"
)


def _action(workspace: str, *, campaign: str = "987654") -> MetaCampaignPausePlannedAction:
    return build_meta_campaign_pause_plan(
        workspace_profile_ref=workspace,
        account_binding=MetaCampaignPauseAccountBinding(
            account_ref="meta_ads.account.act_123456",
            account_client_ref="act_123456",
            timezone="UTC",
            currency="USD",
            account_status="1",
        ),
        execution_binding=MetaCampaignPauseExecutionBinding(
            profile_ref="meta-edit",
            client_application_ref="555555",
            principal_ref="meta_token_principal_v1." + "a" * 64,
            credential_binding_ref="credential-binding:sha256:" + "b" * 64,
            configuration_digest="c" * 64,
            profile_configuration_digest="d" * 64,
            inspected_at=_NOW - timedelta(seconds=2),
            expires_at=_NOW + timedelta(hours=1),
            data_access_expires_at=_NOW + timedelta(hours=2),
        ),
        observation_binding=MetaCampaignPauseObservationBinding(
            campaign=MetaCampaignPauseCampaignBaseline(
                campaign_ref="meta_ads.campaign." + campaign,
                campaign_client_ref=campaign,
                account_client_ref="123456",
                effective_status="WITH_ISSUES",
            ),
            read_started_at=_NOW - timedelta(seconds=1),
            read_finished_at=_NOW,
        ),
        expires_at=_NOW + timedelta(minutes=10),
    )


def _challenge(
    action: MetaCampaignPausePlannedAction, *, nonce: bytes = bytes(range(32))
) -> ApprovalChallenge:
    return build_meta_campaign_pause_challenge(
        action,
        grantor_ref="synthetic-customer-grantor",
        configuration=_CONFIGURATION,
        nonce=nonce,
        issued_at=_NOW,
        now=_NOW,
        expires_at=_NOW + timedelta(minutes=4),
    )


def _signed_receipt(
    challenge: ApprovalChallenge,
    key: ec.EllipticCurvePrivateKey,
    *,
    identifier: str = "A" * 43,
) -> bytes:
    signing = receipt_signing_payload(
        challenge,
        configuration=_CONFIGURATION,
        receipt_id="approval-receipt:" + identifier,
        issued_at=_NOW,
    )
    payload = parse_json(signing.canonical_unsigned_receipt)
    assert type(payload) is dict
    signature = key.sign(signing.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
    payload["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return canonical_bytes(payload)


@dataclass
class _Material:
    ledger: Ledger
    kernel: SafetyKernel
    action: MetaCampaignPausePlannedAction
    record_digest: str
    key: ec.EllipticCurvePrivateKey
    trust: PinnedApprovalTrust

    def accept(self, *, nonce: bytes = bytes(range(32)), identifier: str = "A" * 43):
        challenge = _challenge(self.action, nonce=nonce)
        return accept_approval_receipt(
            self.kernel,
            self.ledger,
            receipt=_signed_receipt(challenge, self.key, identifier=identifier),
            expected_challenge=challenge,
            trust=self.trust,
            source_plan_record_digest=self.record_digest,
        )


@pytest.fixture
def material(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Material:
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _NOW.isoformat().replace("+00:00", "Z"))
    ledger = Ledger(tmp_path)
    action = _action(ledger.profile_ref)
    record = retain_meta_campaign_pause_plan(ledger, action)["record_digest"]
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _Material(
        ledger,
        SafetyKernel(tmp_path, ledger, clock=lambda: _NOW),
        action,
        record,
        key,
        PinnedApprovalTrust(_CONFIGURATION, public, ledger.profile_ref),
    )


def _claims(ledger: Ledger) -> list[dict[str, Any]]:
    return [
        row for row in ledger.records("changes") if row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
    ]


def test_exact_meta_receipt_consumes_claim_and_records_only_its_plan_grant(
    material: _Material,
) -> None:
    accepted = material.accept()
    assert accepted.kind is ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN
    grant = accepted.direct_grant
    assert grant is not None
    assert grant.plan_digest == material.action.plan.plan_digest
    assert grant.subplan_digests == material.action.plan.subplan_digests
    assert grant.expires_at == _NOW + timedelta(minutes=4)
    assert accepted.policy is None
    claim, event = material.ledger.records("changes")[-2:]
    assert claim["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
    assert claim["payload"]["receipt_kind"] == "META_CAMPAIGN_PAUSE_PLAN"
    assert claim["payload"]["source_plan_record_digest"] == material.record_digest
    assert event["payload"]["event_type"] == "APPROVAL_GRANTED"
    assert event["payload"]["effect_status"] == "NO_REMOTE_WRITE"
    assert b"APPLY_STARTED" not in canonical_bytes(material.ledger.records("changes"))


@pytest.mark.parametrize("source", [None, "f" * 64])
def test_missing_meta_plan_source_prevents_claim(material: _Material, source: str | None) -> None:
    challenge = _challenge(material.action)
    before = material.ledger.checkpoint()["checkpoint_digest"]
    with pytest.raises(AdhelmError):
        accept_approval_receipt(
            material.kernel,
            material.ledger,
            receipt=_signed_receipt(challenge, material.key),
            expected_challenge=challenge,
            trust=material.trust,
            source_plan_record_digest=source,
        )
    assert material.ledger.checkpoint()["checkpoint_digest"] == before
    assert _claims(material.ledger) == []


def test_different_valid_retained_meta_action_does_not_satisfy_receipt(material: _Material) -> None:
    different = _action(material.ledger.profile_ref, campaign="987655")
    source = retain_meta_campaign_pause_plan(material.ledger, different)["record_digest"]
    challenge = _challenge(material.action)
    before = material.ledger.checkpoint()["checkpoint_digest"]
    with pytest.raises(AdhelmError) as refused:
        accept_approval_receipt(
            material.kernel,
            material.ledger,
            receipt=_signed_receipt(challenge, material.key),
            expected_challenge=challenge,
            trust=material.trust,
            source_plan_record_digest=source,
        )
    assert refused.value.code == "APPROVAL_SOURCE_PLAN_MISMATCH"
    assert material.ledger.checkpoint()["checkpoint_digest"] == before


@pytest.mark.parametrize("reused", ["receipt", "nonce", "identifier"])
def test_meta_receipt_nonce_and_identifier_cannot_be_consumed_again(
    material: _Material,
    reused: str,
) -> None:
    material.accept()
    before = material.ledger.checkpoint()["checkpoint_digest"]
    with pytest.raises(AdhelmError) as refused:
        if reused == "receipt":
            original_receipt = _claims(material.ledger)[0]["payload"]["receipt"]
            accept_approval_receipt(
                material.kernel,
                material.ledger,
                receipt=canonical_bytes(original_receipt),
                expected_challenge=_challenge(material.action),
                trust=material.trust,
                source_plan_record_digest=material.record_digest,
            )
        else:
            material.accept(
                nonce=bytes(range(32, 64)) if reused == "identifier" else bytes(range(32)),
                identifier="B" * 43 if reused == "nonce" else "A" * 43,
            )
    assert refused.value.code == "APPROVAL_RECEIPT_REPLAY"
    assert material.ledger.checkpoint()["checkpoint_digest"] == before
    assert len(_claims(material.ledger)) == 1


@pytest.mark.parametrize("prior_plan", ["absent", "different"])
def test_direct_claim_append_requires_an_earlier_exact_meta_source(
    material: _Material,
    tmp_path: Path,
    prior_plan: str,
) -> None:
    material.accept()
    claim = _claims(material.ledger)[0]["payload"]
    target = Ledger(tmp_path / "other-local-history", profile_ref=material.ledger.profile_ref)
    if prior_plan == "different":
        source = retain_meta_campaign_pause_plan(
            target, _action(target.profile_ref, campaign="987655")
        )["record_digest"]
        claim = {**claim, "source_plan_record_digest": source}
    with pytest.raises(AdhelmError) as refused:
        target.append("changes", "APPROVAL_RECEIPT_CLAIMED", claim)
    assert refused.value.code == "APPROVAL_SOURCE_PLAN_MISMATCH"
    assert _claims(target) == []


def test_existing_installed_bridge_v1_still_refuses_meta_plan_and_challenge(
    material: _Material,
) -> None:
    with pytest.raises(AdhelmError):
        _selected_source_record(material.ledger.checkpoint(), material.record_digest)
    with pytest.raises(AdhelmError) as refused:
        build_native_approval_bridge_request_v1(
            _challenge(material.action),
            configuration=_CONFIGURATION,
            request_id=b"r" * 32,
            source_context_digest="e" * 64,
            expected_metadata_digest="f" * 64,
        )
    assert refused.value.code.startswith("NATIVE_APPROVAL_BRIDGE_")
    assert _claims(material.ledger) == []


def test_meta_receipt_export_does_not_carry_a_signed_receipt_or_raw_action(
    material: _Material,
    tmp_path: Path,
) -> None:
    material.accept()
    output = tmp_path / "redacted-export"
    export_verified(material.ledger, output, opaque_key=b"e" * 32)
    rows = [json.loads(line) for line in (output / "records.jsonl").read_text().splitlines()]
    claims = [row["payload"] for row in rows if row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"]
    assert len(claims) == 1
    claim = claims[0]
    assert claim["receipt_kind"] == "META_CAMPAIGN_PAUSE_PLAN"
    assert (
        not {"challenge", "receipt", "host_configuration", "verification_public_key_x963_hex"}
        & claim.keys()
    )
    assert claim["evidence_scope"] == "SIGNATURE_CONSISTENCY_NOT_HOST_ENROLLMENT"
    assert "act_123456" not in json.dumps(claim)
    assert "credential-binding:sha256:" not in json.dumps(claim)
