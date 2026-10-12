"""Closed local evidence for one consumed native approval receipt.

An embedded public key proves only internal signature consistency. It never
enrolls or selects a trusted host. The acceptance workflow must separately
verify the same bytes against the installed trust configuration before append.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import canonical_bytes, digest
from adhelm.models import StrictModel

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ApprovalKind = Literal[
    "OPENAI_PAUSE_PLAN",
    "GA4_KEY_EVENT_CREATE_PLAN",
    "META_CAMPAIGN_PAUSE_PLAN",
    # The two provider-neutral kinds added by ADR 0012. Their claim evidence is
    # identical in shape to the existing kinds': the challenge and receipt are
    # retained as bounded canonical wire data, and the claim itself remains a
    # one-use consumption record rather than a grant.
    "COMPOSITE_EXECUTION_PLAN",
    "ATTEMPT_RECOVERY",
    "DELEGATION_POLICY_INSTALL",
    "DELEGATION_POLICY_REVOKE",
]


def _original_wire_types(value: object, *, depth: int = 0) -> None:
    """Check before JCS can turn integer-looking floats into integer values."""
    if depth > 16:
        raise ValueError("Approval evidence exceeds its closed nesting limit")
    if value is None or type(value) in {str, bool}:
        return
    if type(value) is int and abs(value) <= 9_007_199_254_740_991:
        return
    if type(value) is list and len(value) <= 256:
        for child in value:
            _original_wire_types(child, depth=depth + 1)
        return
    if type(value) is dict and len(value) <= 256:
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError("Approval evidence keys must be strings")
            _original_wire_types(child, depth=depth + 1)
        return
    raise ValueError("Approval evidence requires closed JSON wire types")


class ApprovalReceiptClaimRecord(StrictModel):
    """One-use receipt consumption, separate from any durable grant event."""

    schema_version: Literal[1] = 1
    event_type: Literal["APPROVAL_RECEIPT_CLAIMED"] = "APPROVAL_RECEIPT_CLAIMED"
    evidence_scope: Literal["SIGNATURE_CONSISTENCY_NOT_HOST_ENROLLMENT"] = (
        "SIGNATURE_CONSISTENCY_NOT_HOST_ENROLLMENT"
    )
    workspace_profile_ref: str = Field(min_length=1, max_length=160)
    source_plan_record_digest: Digest | None = None
    challenge: dict[str, Any]
    receipt: dict[str, Any]
    host_configuration: dict[str, Any]
    verification_public_key_x963_hex: str = Field(pattern=r"^04[0-9a-f]{128}$")
    configuration_digest: Digest
    public_key_fingerprint: Digest
    challenge_digest: Digest
    challenge_nonce_digest: Digest
    receipt_digest: Digest
    receipt_id: str = Field(pattern=r"^approval-receipt:[A-Za-z0-9_-]{43}$")
    receipt_kind: ApprovalKind
    accepted_at: datetime

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Approval evidence schema_version must be integer 1")
        return value

    @field_validator("challenge", "receipt", mode="before")
    @classmethod
    def closed_wire_types(cls, value: object) -> object:
        _original_wire_types(value)
        return value

    @field_validator("accepted_at")
    @classmethod
    def aware_acceptance(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Approval acceptance time must identify an instant")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def exact_receipt_consistency(self) -> ApprovalReceiptClaimRecord:
        # Local import keeps the wire implementation independent of ledger
        # schemas. Neither this constructor nor a recorded pin is a trust root.
        from adhelm.approval_protocol import (
            ApprovalHostConfiguration,
            decode_approval_challenge,
            verify_approval_receipt,
        )

        try:
            configuration = ApprovalHostConfiguration(**self.host_configuration)
            configuration_data = asdict(configuration)
            if canonical_bytes(configuration_data) != canonical_bytes(self.host_configuration):
                raise ValueError("Incomplete configuration")
            challenge_bytes = canonical_bytes(self.challenge)
            receipt_bytes = canonical_bytes(self.receipt)
            if len(challenge_bytes) > 65_536 or len(receipt_bytes) > 16_384:
                raise ValueError("Oversized evidence")
            # Structural reconstruction uses the original issue instant. The
            # receipt verifier separately checks eligibility at acceptance, so
            # a valid long review does not acquire a second initial-age gate.
            issued_text = self.challenge.get("issued_at")
            if type(issued_text) is not str:
                raise ValueError("Missing issue time")
            issued_at = datetime.fromisoformat(issued_text.replace("Z", "+00:00"))
            challenge = decode_approval_challenge(
                challenge_bytes, now=issued_at, configuration=configuration
            )
            public_key = bytes.fromhex(self.verification_public_key_x963_hex)
            receipt = verify_approval_receipt(
                receipt_bytes,
                expected_challenge=challenge,
                configuration=configuration,
                trusted_public_key_x963=public_key,
                now=self.accepted_at,
            )
            if (
                self.configuration_digest != digest(configuration_data)
                or self.public_key_fingerprint != hashlib.sha256(public_key).hexdigest()
                or self.challenge_digest != challenge.digest
                or self.challenge_nonce_digest != hashlib.sha256(challenge.nonce).hexdigest()
                or self.receipt_digest != receipt.digest
                or self.receipt_id != receipt.receipt_id
                or self.receipt_kind != receipt.kind
            ):
                raise ValueError("Inconsistent receipt bindings")
            if receipt.kind == "OPENAI_PAUSE_PLAN":
                if (
                    challenge.action is None
                    or self.source_plan_record_digest is None
                    or challenge.action.plan.profile_ref != self.workspace_profile_ref
                ):
                    raise ValueError("Missing local plan binding")
            elif receipt.kind == "GA4_KEY_EVENT_CREATE_PLAN":
                if (
                    challenge.ga4_action is None
                    or self.source_plan_record_digest is None
                    or challenge.ga4_action.plan.profile_ref != self.workspace_profile_ref
                ):
                    raise ValueError("Missing local GA4 plan binding")
            elif receipt.kind == "META_CAMPAIGN_PAUSE_PLAN":
                if (
                    challenge.meta_pause_action is None
                    or self.source_plan_record_digest is None
                    or challenge.meta_pause_action.plan.profile_ref != self.workspace_profile_ref
                ):
                    raise ValueError("Missing local Meta PAUSE plan binding")
            elif self.source_plan_record_digest is not None:
                raise ValueError("A policy operation has no campaign plan reference")
        except Exception:
            raise ValueError("Approval evidence failed closed receipt validation") from None
        return self
