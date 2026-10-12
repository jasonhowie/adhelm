"""Deterministic offline vectors for the provider-neutral approval kinds.

Run ``uv run python tests/generate_composite_execution_plan_approval_vectors.py``
to rewrite ``tests/fixtures/composite-execution-plan-approval-v1.json``.
``tests/test_composite_execution_plan_approval.py`` reruns this module and
requires byte-for-byte equality with the checked-in file, so the Mac-side A01
port can regenerate the same bytes and cross-check its own encoder.

The file deliberately contains **no signature and no key material**. Every value
in it is derived from public canonical inputs, so it stays reproducible without
committing a private key (`AGENTS.md` rule 1). The signature path is covered by
ephemeral in-memory keys in the test module and by the existing signed vectors
for the older kinds; what a Swift port needs from this file is the exact
canonical challenge, review-facts, and unsigned-receipt bytes it must produce.

Nothing here is host, installation, enrollment, authorization, or provider
evidence.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalHostConfiguration,
    approval_review_facts,
    build_attempt_recovery_challenge,
    build_composite_execution_plan_challenge,
    compiler_review_lines,
    composite_subplan_review_lines,
    receipt_signing_payload,
)
from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.safety import (
    AccountIdentity,
    CompositePlan,
    EgressTarget,
    Money,
    OperationKind,
    StateRef,
    SubPlan,
    attempt_recovery_subject,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures/composite-execution-plan-approval-v1.json"

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_PLAN_EXPIRES = _NOW + timedelta(hours=1)
_CHALLENGE_EXPIRES = _NOW + timedelta(minutes=4)
_RECEIPT_ISSUED_AT = _NOW + timedelta(seconds=1)
_RECEIPT_ID = "approval-receipt:" + "A" * 43
_RECOVERY_RECEIPT_ID = "approval-receipt:" + "B" * 43
_STARTED_RECORD_DIGEST = "9" * 64
_HOST_REF = "host:adhelm-macos-approval-v1"
_KEY_REF = "key:approval-test-v1"

#: One provider's already-redacted compiler review, as a plain mapping so this
#: generator stays hermetic. The two digest-shaped values prove the derivation
#: omits them rather than rendering something the line validator refuses.
_REDACTED_REVIEW: dict[str, Any] = {
    "account_currency": "USD",
    "account_ref": "google_ads.account.1234567890",
    "account_timezone": "America/Chicago",
    "campaign_duration_days": 31,
    "campaign_handle": "campaign",
    "campaign_total_budget": {"minor_units": 25_000, "currency": "USD"},
    "component_level_attribution": "NOT_AVAILABLE_FROM_PROVIDER",
    "creative_content_digest": "e" * 64,
    "creative_version_id": "creative:sha256:" + "e" * 64,
    "execution_status": "OFFLINE_CANDIDATE_NOT_EXECUTABLE",
    "initial_state": "PAUSED",
    "keyword_count": 1,
    "location_count": 1,
    "objective": "SEARCH_TRAFFIC",
}


def configuration() -> ApprovalHostConfiguration:
    return ApprovalHostConfiguration(host_ref=_HOST_REF, signing_key_ref=_KEY_REF)


def _account(provider: str) -> AccountIdentity:
    return AccountIdentity(
        account_ref=f"{provider}.alias",
        stable_account_id=f"{provider}.stable.1",
        auth_profile_ref=f"{provider}.edit-profile",
        principal_ref=f"{provider}.principal",
        client_application_ref=f"{provider}.client",
        scopes=("ads.write",),
    )


def _subplan(
    *,
    provider: str,
    operation: OperationKind,
    spend: Money,
    host: str,
    children: int = 1,
) -> SubPlan:
    return SubPlan(
        subplan_id=f"{provider}-{operation.value.lower()}",
        provider=provider,
        operation=operation,
        account=_account(provider),
        provider_api_version="v1",
        capability_version="cap-v1",
        before=StateRef(fingerprint="a" * 64, revision="r1"),
        after=StateRef(fingerprint="b" * 64, revision="r2"),
        max_spend=spend,
        egress=(EgressTarget(host=host, purpose="planned campaign operation"),),
        recovery_actions=(OperationKind.PAUSE,),
        expected_child_egress_count=children,
        expires_at=_PLAN_EXPIRES,
    )


def composite_plan() -> CompositePlan:
    """Four providers, two currencies, one multi-request and one zero-cap PAUSE."""

    return CompositePlan(
        plan_id="composite-vector-plan",
        profile_ref="workspace-ledger-1",
        subplans=(
            _subplan(
                provider="google_ads",
                operation=OperationKind.CREATE,
                spend=Money(minor_units=25_000, currency="USD"),
                host="googleads.googleapis.com",
                children=3,
            ),
            _subplan(
                provider="meta_ads",
                operation=OperationKind.CREATE,
                spend=Money(minor_units=5_000, currency="USD"),
                host="graph.facebook.com",
            ),
            _subplan(
                provider="linkedin_ads",
                operation=OperationKind.CREATE,
                spend=Money(minor_units=1_000, currency="EUR"),
                host="api.linkedin.com",
            ),
            _subplan(
                provider="openai_ads",
                operation=OperationKind.PAUSE,
                spend=Money(minor_units=0, currency="USD"),
                host="api.openai.com",
            ),
        ),
        expires_at=_PLAN_EXPIRES,
    )


def _vector(
    challenge: ApprovalChallenge, *, receipt_id: str, extra: dict[str, Any]
) -> dict[str, Any]:
    facts = approval_review_facts(challenge, configuration=configuration())
    payload = receipt_signing_payload(
        challenge,
        configuration=configuration(),
        receipt_id=receipt_id,
        issued_at=_RECEIPT_ISSUED_AT,
    )
    return {
        "kind": challenge.kind.value,
        **extra,
        "challenge": parse_json(challenge.canonical_bytes),
        "challenge_digest": challenge.digest,
        "review_facts": parse_json(facts.canonical_bytes),
        "review_digest": facts.digest,
        "receipt_id": receipt_id,
        "receipt_issued_at": payload.issued_at_text,
        "unsigned_receipt": payload.unsigned_data(),
        "unsigned_receipt_digest": digest(payload.unsigned_data()),
        "receipt_signing_domain_bytes_b64url": base64.urlsafe_b64encode(
            payload.domain_separated_bytes
        )
        .decode("ascii")
        .rstrip("="),
    }


def build() -> dict[str, Any]:
    plan = composite_plan()
    config = configuration()
    composite_lines = composite_subplan_review_lines(plan.subplans[0])
    composite = build_composite_execution_plan_challenge(
        plan,
        grantor_ref="customer-grantor-1",
        review_lines=composite_lines,
        expires_at=_CHALLENGE_EXPIRES,
        configuration=config,
        nonce=bytes(range(32)),
        issued_at=_NOW,
        now=_NOW,
    )
    attempts = ((plan.subplans[1], _STARTED_RECORD_DIGEST),)
    recovery_lines = (
        "One unresolved attempt has no journaled result",
        "Recording the observed state settles and attributes nothing",
        "This grant releases only the exact attempt below",
    )
    recovery = build_attempt_recovery_challenge(
        plan,
        attempts,
        grantor_ref="customer-grantor-1",
        review_lines=recovery_lines,
        expires_at=_CHALLENGE_EXPIRES,
        configuration=config,
        nonce=bytes(range(32, 64)),
        issued_at=_NOW,
        now=_NOW,
    )
    return {
        "schema_version": 1,
        "vector_kind": "COMPOSITE_EXECUTION_PLAN_APPROVAL_V1",
        "notice": (
            "Synthetic canonical bytes only. No signature, private key, public key, "
            "host identity, enrollment, authorization, or provider evidence."
        ),
        "host_configuration": config.to_data(),
        "configuration_digest": config.configuration_digest,
        "plan": parse_json(canonical_bytes(plan.model_dump(mode="json"))),
        "plan_digest": plan.plan_digest,
        "subplan_digests": list(plan.subplan_digests),
        "compiler_review_line_derivation": {
            "provider": "google_ads",
            "redacted_review": _REDACTED_REVIEW,
            "review_lines": list(compiler_review_lines("google_ads", _REDACTED_REVIEW)),
        },
        "vectors": [
            _vector(
                composite, receipt_id=_RECEIPT_ID, extra={"review_lines": list(composite_lines)}
            ),
            _vector(
                recovery,
                receipt_id=_RECOVERY_RECEIPT_ID,
                extra={
                    "review_lines": list(recovery_lines),
                    "attempts": [
                        {
                            "subplan_digest": subplan.subplan_digest,
                            "started_record_digest": started,
                            "attempt_subject_digest": attempt_recovery_subject(
                                plan, subplan, started_record_digest=started
                            ),
                        }
                        for subplan, started in attempts
                    ],
                },
            ),
        ],
    }


def main() -> None:
    FIXTURE.write_bytes(json.dumps(build(), indent=2, sort_keys=True).encode("utf-8") + b"\n")
    print(json.dumps({"status": "WROTE", "path": str(FIXTURE)}))


if __name__ == "__main__":
    main()
