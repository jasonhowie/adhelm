"""Pinned receipt acceptance into the local authorization history.

This library does not enroll a host, invoke its UI, or execute a provider. Its
trust value must come from a separately reviewed installed configuration; no
command or file loader lets a receipt select that value. The installed trust
and revocation resolver remains a separate integration requirement.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    NativeMetaCampaignPauseApprovalBridgeRequestV2,
    revalidate_meta_campaign_pause_approval_bridge_candidate_v2,
)
from adhelm._native_approval_bridge_protocol import (
    NativeApprovalBridgeCandidateV1,
    NativeApprovalBridgeRequestV1,
    revalidate_native_approval_bridge_candidate_v1,
)
from adhelm.approval_protocol import (
    COMPOSITE_APPROVAL_KINDS,
    ApprovalChallenge,
    ApprovalChallengeKind,
    ApprovalHostConfiguration,
    CompositeExecutionApprovalSubject,
    VerifiedApprovalReceipt,
    composite_approval_subject_bytes,
    expected_composite_approval_subject,
    parse_approval_challenge_wire,
    verify_approval_receipt,
)
from adhelm.approval_records import ApprovalReceiptClaimRecord
from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.safety import (
    CompositePlan,
    DelegationPolicy,
    DirectGrant,
    DirectGrantSource,
    SafetyKernel,
    SubPlan,
    UserApprovalHost,
)
from adhelm.saved_ga4_create_plans import load_ga4_key_event_create_plan
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan
from adhelm.saved_plans import load_openai_pause_plan


@dataclass(frozen=True, slots=True)
class PinnedApprovalTrust:
    """Already configured trust supplied by the future installed integration.

    Constructing this Python value does not establish enrollment or defeat a
    hostile process with equivalent OS permissions. No receipt supplies its
    own accepted pin, host identity, or coordinating workspace.
    """

    configuration: ApprovalHostConfiguration
    public_key_x963: bytes
    workspace_profile_ref: str

    def __post_init__(self) -> None:
        if (
            type(self.configuration) is not ApprovalHostConfiguration
            or type(self.public_key_x963) is not bytes
            or len(self.public_key_x963) != 65
            or self.public_key_x963[0] != 4
            or type(self.workspace_profile_ref) is not str
            or not 1 <= len(self.workspace_profile_ref) <= 160
            or "/" in self.workspace_profile_ref
            or "\\" in self.workspace_profile_ref
        ):
            raise ValueError("Pinned approval trust requires fixed closed configuration")
        # Reconstruct rather than trust a dataclass instance made with low-level
        # allocation or object.__setattr__. Signature verification validates the
        # actual P-256 point before any append.
        ApprovalHostConfiguration(**asdict(self.configuration))


@dataclass(frozen=True, slots=True)
class _CompositeApprovalBinding:
    """The caller's exact local plan, and the attempts a recovery may release."""

    plan: CompositePlan
    attempts: tuple[tuple[SubPlan, str], ...] = ()


@dataclass(frozen=True, slots=True)
class AcceptedNativeApproval:
    """Observed local recording result; neither a receipt verifier nor executor."""

    receipt_claim_record_digest: str
    receipt_digest: str
    kind: ApprovalChallengeKind
    direct_grant: DirectGrant | None
    policy: DelegationPolicy | None
    #: Present only for :attr:`ApprovalChallengeKind.ATTEMPT_RECOVERY`, whose
    #: grant binds recovery subject digests rather than a composite plan, so no
    #: authorization event is appended here.  The caller passes this host to
    #: ``SafetyKernel.record_attempt_recovery``; it proves only that exact grant
    #: and only while the accepted receipt is still current.
    approval_host: UserApprovalHost | None = None


class _ExactReceiptHost:
    """Private, one-subject adapter used only after pinned verification/claim."""

    def __init__(
        self,
        receipt: VerifiedApprovalReceipt,
        direct_grant: DirectGrant | None,
        current_proof: Callable[[], bool],
    ) -> None:
        self.host_ref = receipt.host_ref
        self._kind = receipt.kind
        self._grant_digest = digest(direct_grant) if direct_grant is not None else None
        self._policy_digest = receipt.policy_digest
        self._current_proof = current_proof

    def verifies_direct_grant(self, grant: DirectGrant) -> bool:
        return (
            self._kind
            in {
                ApprovalChallengeKind.OPENAI_PAUSE_PLAN,
                ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN,
                ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN,
                *COMPOSITE_APPROVAL_KINDS,
            }
            and self._grant_digest is not None
            and digest(grant) == self._grant_digest
            and self._current_proof()
        )

    def verifies_policy_configuration(self, policy: DelegationPolicy) -> bool:
        # This integration intentionally cannot use the generic legacy proof.
        return False

    def verifies_policy_installation(self, policy: DelegationPolicy) -> bool:
        return (
            self._kind == ApprovalChallengeKind.DELEGATION_POLICY_INSTALL
            and policy.policy_digest == self._policy_digest
            and self._current_proof()
        )

    def verifies_policy_revocation(self, policy: DelegationPolicy) -> bool:
        return (
            self._kind == ApprovalChallengeKind.DELEGATION_POLICY_REVOKE
            and policy.policy_digest == self._policy_digest
            and self._current_proof()
        )


def _current_time(kernel: SafetyKernel) -> datetime:
    try:
        value = kernel.clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Invalid local time")
        return value.astimezone(UTC)
    except Exception:
        raise AdhelmError(
            "APPROVAL_CLOCK_INVALID", "Approval recording requires an aware local clock."
        ) from None


def _require_current_subject(receipt: VerifiedApprovalReceipt, now: datetime) -> None:
    if receipt.kind == ApprovalChallengeKind.DELEGATION_POLICY_INSTALL:
        if receipt.policy is None or receipt.policy.expires_at <= now:
            raise AdhelmError(
                "DELEGATION_POLICY_EXPIRED",
                "An expired policy cannot be newly installed.",
                exit_code=ExitCode.AUTHORIZATION,
            )
    elif receipt.kind == ApprovalChallengeKind.DELEGATION_POLICY_REVOKE:
        # There is no scheduled revocation executor. The requested effective
        # instant stays in the signed claim; actual local effect starts at the
        # durable revocation event, never earlier than that signed instant.
        if receipt.revoked_at is None or receipt.revoked_at > now:
            raise AdhelmError(
                "APPROVAL_REVOCATION_NOT_YET_EFFECTIVE",
                "This receipt requests a revocation instant that has not arrived.",
                exit_code=ExitCode.AUTHORIZATION,
            )


def _checked_trust(trust: PinnedApprovalTrust, ledger: Ledger) -> PinnedApprovalTrust:
    try:
        if type(trust) is not PinnedApprovalTrust:
            raise ValueError("Unexpected trust value")
        checked = PinnedApprovalTrust(
            configuration=ApprovalHostConfiguration(**asdict(trust.configuration)),
            public_key_x963=trust.public_key_x963,
            workspace_profile_ref=trust.workspace_profile_ref,
        )
        if checked.workspace_profile_ref != ledger.profile_ref:
            raise ValueError("Wrong coordinating workspace")
    except Exception:
        raise AdhelmError(
            "APPROVAL_TRUST_NOT_CONFIGURED",
            "A fixed pinned approval configuration for this coordinating workspace is required.",
            exit_code=ExitCode.AUTHORIZATION,
        ) from None
    return checked


def _checked_exact_challenge(
    expected_challenge: object, configuration: ApprovalHostConfiguration
) -> ApprovalChallenge:
    try:
        if type(expected_challenge) is not ApprovalChallenge:
            raise ValueError("Invalid challenge value")
        return parse_approval_challenge_wire(
            expected_challenge.canonical_bytes, configuration=configuration
        )
    except Exception:
        raise AdhelmError(
            "APPROVAL_CHALLENGE_INVALID", "An exact local challenge is required."
        ) from None


def _checked_receipt_key(value: object) -> bytes:
    if type(value) is not bytes or len(value) != 65 or value[0] != 4:
        raise AdhelmError(
            "APPROVAL_TRUST_NOT_CONFIGURED",
            "A fixed pinned approval configuration for this coordinating workspace is required.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    return value


def _accept_exact_receipt(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    receipt: bytes,
    expected_challenge: ApprovalChallenge,
    configuration: ApprovalHostConfiguration,
    verification_public_key_x963: bytes,
    source_plan_record_digest: str | None = None,
    composite_binding: _CompositeApprovalBinding | None = None,
    before_receipt_verification: Callable[[datetime], None] | None = None,
) -> AcceptedNativeApproval:
    """Shared exact receipt claim/grant core for private approval integrations.

    Callers must have fixed the receipt source and verifier before reaching this
    private routine. It deliberately exposes neither a candidate nor a generic
    verifier surface. A crash after claim and before grant remains non-retryable
    with the same nonce.
    """
    if kernel.ledger is not ledger or kernel.root.resolve() != ledger.root.resolve():
        raise AdhelmError(
            "APPROVAL_COORDINATOR_MISMATCH",
            "Receipt recording and the safety kernel must use the same canonical ledger.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    if type(receipt) is not bytes or not receipt:
        raise AdhelmError("APPROVAL_RECEIPT_INVALID", "An exact local receipt is required.")
    try:
        checked_configuration = ApprovalHostConfiguration(**asdict(configuration))
    except Exception:
        raise AdhelmError(
            "APPROVAL_TRUST_NOT_CONFIGURED",
            "A fixed pinned approval configuration for this coordinating workspace is required.",
            exit_code=ExitCode.AUTHORIZATION,
        ) from None
    challenge = _checked_exact_challenge(expected_challenge, checked_configuration)
    public_key = _checked_receipt_key(verification_public_key_x963)
    if before_receipt_verification is not None and not callable(before_receipt_verification):
        raise AdhelmError("APPROVAL_RECEIPT_INVALID", "An exact local receipt is required.")

    def verify_current() -> tuple[VerifiedApprovalReceipt, datetime]:
        now = _current_time(kernel)
        if before_receipt_verification is not None:
            before_receipt_verification(now)
        verified = verify_approval_receipt(
            receipt,
            expected_challenge=challenge,
            configuration=checked_configuration,
            trusted_public_key_x963=public_key,
            now=now,
        )
        _require_current_subject(verified, now)
        return verified, now

    verified, _ = verify_current()
    # Preserve the public acceptance boundary's independent subject-currentness
    # sample. Policy transition preflight intentionally has its own later
    # locked sample, and callers rely on that ordering to fail before claim.
    _require_current_subject(verified, _current_time(kernel))
    # Full local history is checked before any consumption. A direct receipt
    # must name the exact earlier retained plan in the same workspace.
    ledger.checkpoint()
    direct_grant: DirectGrant | None = None
    policy = verified.policy
    if verified.kind == ApprovalChallengeKind.OPENAI_PAUSE_PLAN:
        if source_plan_record_digest is None or verified.action is None:
            raise AdhelmError(
                "APPROVAL_SOURCE_PLAN_REQUIRED", "A retained exact pause plan is required."
            )
        action, _ = load_openai_pause_plan(ledger, source_plan_record_digest)
        if canonical_bytes(action.model_dump(mode="json")) != canonical_bytes(
            verified.action.model_dump(mode="json")
        ):
            raise AdhelmError(
                "APPROVAL_SOURCE_PLAN_MISMATCH",
                "The receipt differs from the retained pause plan.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        direct_grant = DirectGrant(
            authorization_id=verified.receipt_id,
            source=DirectGrantSource.USER_CONTROLLED_HOST,
            grantor_ref=verified.grantor_ref,
            host_ref=verified.host_ref,
            user_interaction_ref="evidence:sha256:" + verified.digest,
            plan_digest=action.plan.plan_digest,
            subplan_digests=action.plan.subplan_digests,
            expires_at=verified.expires_at,
        )
    elif verified.kind == ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN:
        if source_plan_record_digest is None or verified.ga4_action is None:
            raise AdhelmError(
                "APPROVAL_SOURCE_PLAN_REQUIRED", "A retained exact GA4 CREATE plan is required."
            )
        ga4_action, _ = load_ga4_key_event_create_plan(ledger, source_plan_record_digest)
        if canonical_bytes(ga4_action.model_dump(mode="json", warnings="error")) != canonical_bytes(
            verified.ga4_action.model_dump(mode="json", warnings="error")
        ):
            raise AdhelmError(
                "APPROVAL_SOURCE_PLAN_MISMATCH",
                "The receipt differs from the retained GA4 CREATE plan.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        direct_grant = DirectGrant(
            authorization_id=verified.receipt_id,
            source=DirectGrantSource.USER_CONTROLLED_HOST,
            grantor_ref=verified.grantor_ref,
            host_ref=verified.host_ref,
            user_interaction_ref="evidence:sha256:" + verified.digest,
            plan_digest=ga4_action.plan.plan_digest,
            subplan_digests=ga4_action.plan.subplan_digests,
            expires_at=verified.expires_at,
        )
    elif verified.kind == ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN:
        if source_plan_record_digest is None or verified.meta_pause_action is None:
            raise AdhelmError(
                "APPROVAL_SOURCE_PLAN_REQUIRED",
                "A retained exact Meta PAUSE plan is required.",
            )
        meta_pause_action, _ = load_meta_campaign_pause_plan(ledger, source_plan_record_digest)
        if canonical_bytes(meta_pause_action.to_data()) != canonical_bytes(
            verified.meta_pause_action.to_data()
        ):
            raise AdhelmError(
                "APPROVAL_SOURCE_PLAN_MISMATCH",
                "The receipt differs from the retained Meta PAUSE plan.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        direct_grant = DirectGrant(
            authorization_id=verified.receipt_id,
            source=DirectGrantSource.USER_CONTROLLED_HOST,
            grantor_ref=verified.grantor_ref,
            host_ref=verified.host_ref,
            user_interaction_ref="evidence:sha256:" + verified.digest,
            plan_digest=meta_pause_action.plan.plan_digest,
            subplan_digests=meta_pause_action.plan.subplan_digests,
            expires_at=verified.expires_at,
        )
    elif verified.kind in COMPOSITE_APPROVAL_KINDS:
        subject = _required_composite_subject(
            verified, composite_binding, configuration=checked_configuration
        )
        direct_grant = DirectGrant(
            authorization_id=verified.receipt_id,
            source=DirectGrantSource.USER_CONTROLLED_HOST,
            grantor_ref=verified.grantor_ref,
            host_ref=verified.host_ref,
            user_interaction_ref="evidence:sha256:" + verified.digest,
            plan_digest=subject.plan_digest,
            subplan_digests=subject.subplan_digests,
            expires_at=verified.expires_at,
        )
    elif source_plan_record_digest is not None or policy is None:
        raise AdhelmError(
            "APPROVAL_POLICY_BINDING_INVALID", "A policy receipt has no campaign plan."
        )
    # The acceptance instant is measured here, not supplied by receipt/caller.
    # Reverification closes time spent scanning history. Ledger also checks its
    # clock while holding the append lock and at the final claim append handoff.
    verified, accepted_at = verify_current()
    claim = ApprovalReceiptClaimRecord(
        workspace_profile_ref=ledger.profile_ref,
        source_plan_record_digest=source_plan_record_digest,
        challenge=parse_json(challenge.canonical_bytes),
        receipt=parse_json(verified.canonical_bytes),
        host_configuration=asdict(checked_configuration),
        verification_public_key_x963_hex=public_key.hex(),
        configuration_digest=checked_configuration.configuration_digest,
        public_key_fingerprint=hashlib.sha256(public_key).hexdigest(),
        challenge_digest=challenge.digest,
        challenge_nonce_digest=hashlib.sha256(challenge.nonce).hexdigest(),
        receipt_digest=verified.digest,
        receipt_id=verified.receipt_id,
        receipt_kind=verified.kind.value,
        accepted_at=accepted_at,
    )
    # One policy lock spans state preflight, receipt consumption and the final
    # authorization event. Invalid transitions fail before burning a nonce;
    # another cooperating caller cannot change that policy between the steps.
    with kernel._policy_lock(policy.policy_id) if policy is not None else nullcontext():
        if policy is not None:
            try:
                kernel._require_policy_transition(
                    policy, revoke=verified.kind == ApprovalChallengeKind.DELEGATION_POLICY_REVOKE
                )
            except AdhelmError:
                raise
            except Exception:
                raise AdhelmError(
                    "APPROVAL_POLICY_STATE_UNVERIFIED",
                    "Current local policy state could not be verified; no receipt was consumed.",
                    exit_code=ExitCode.AUTHORIZATION,
                    next_action="Inspect local policy history and clock before a new attempt.",
                ) from None
        try:
            claim_record = ledger.append(
                "changes",
                "APPROVAL_RECEIPT_CLAIMED",
                claim.model_dump(mode="json"),
                _before_durable_append=lambda: verify_current(),
            )
        except AdhelmError:
            raise
        except Exception:
            raise _recording_unverified() from None

        def current_proof() -> bool:
            verify_current()
            return True

        host = _ExactReceiptHost(verified, direct_grant, current_proof)
        try:
            # Do not let time spent appending turn an expired receipt into a new
            # policy installation or revocation. A consumed failed handoff stays
            # consumed; a new local review needs a new challenge nonce.
            verify_current()
            if direct_grant is not None and verified.action is not None:
                kernel.record_direct_grant(verified.action.plan, direct_grant, host)
            elif (
                direct_grant is not None
                and verified.kind == ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN
            ):
                if composite_binding is None:  # pragma: no cover - set with the grant
                    raise ValueError("Unsupported verified operation")
                kernel.record_direct_grant(composite_binding.plan, direct_grant, host)
            elif (
                direct_grant is not None and verified.kind == ApprovalChallengeKind.ATTEMPT_RECOVERY
            ):
                # A recovery grant binds recovery subject digests rather than the
                # composite's own plan and subplan digests, so there is no
                # APPROVAL_GRANTED event to append for it here.  The durable
                # one-use consumption is the claim record appended above, and
                # SafetyKernel.record_attempt_recovery applies its own host
                # proof, expiry, subject and one-use checks before it records
                # anything.  Returning the bound host keeps that proof current.
                pass
            elif (
                policy is not None
                and verified.kind == ApprovalChallengeKind.DELEGATION_POLICY_INSTALL
            ):
                kernel._record_policy_transition_locked(policy, host, revoke=False)
            elif (
                policy is not None
                and verified.kind == ApprovalChallengeKind.DELEGATION_POLICY_REVOKE
            ):
                kernel._record_policy_transition_locked(policy, host, revoke=True)
            else:
                raise ValueError("Unsupported verified operation")
        except Exception:
            # A durable grant append can fail after its write. Report uncertainty
            # without retrying, claiming absence, or beginning provider execution.
            raise _recording_unverified() from None
        return AcceptedNativeApproval(
            receipt_claim_record_digest=claim_record["record_digest"],
            receipt_digest=verified.digest,
            kind=verified.kind,
            direct_grant=direct_grant,
            policy=policy,
            approval_host=(
                host if verified.kind == ApprovalChallengeKind.ATTEMPT_RECOVERY else None
            ),
        )


def accept_approval_receipt(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    receipt: bytes,
    expected_challenge: ApprovalChallenge,
    trust: PinnedApprovalTrust,
    source_plan_record_digest: str | None = None,
) -> AcceptedNativeApproval:
    """Verify, durably consume, then record exactly one grant/policy operation.

    The public pinned route retains its existing inputs and semantics. It is
    not an installed-bridge pin resolver; a later private bridge consumes its
    opaque candidate through the shared core without constructing this value.
    """
    checked_trust = _checked_trust(trust, ledger)
    return _accept_exact_receipt(
        kernel,
        ledger,
        receipt=receipt,
        expected_challenge=expected_challenge,
        configuration=checked_trust.configuration,
        verification_public_key_x963=checked_trust.public_key_x963,
        source_plan_record_digest=source_plan_record_digest,
    )


def _required_composite_subject(
    verified: VerifiedApprovalReceipt,
    binding: _CompositeApprovalBinding | None,
    *,
    configuration: ApprovalHostConfiguration,
) -> CompositeExecutionApprovalSubject:
    """Require the verified subject to restate the caller's exact local plan.

    The signed subject carries only digests and bounded summaries, so it cannot
    prove on its own which plan it describes.  This rebuilds the subject that
    the caller's plan -- and, for a recovery, its exact unresolved attempts --
    must produce, and requires byte equality through the builder's own
    derivation.
    """

    subject = verified.composite_subject
    if subject is None or binding is None:
        raise AdhelmError(
            "APPROVAL_SOURCE_PLAN_REQUIRED",
            "A provider-neutral receipt requires the exact local composite plan.",
        )
    try:
        expected = expected_composite_approval_subject(
            binding.plan,
            kind=verified.kind,
            grantor_ref=verified.grantor_ref,
            review_lines=subject.review_lines,
            configuration=configuration,
            attempts=binding.attempts,
        )
        matches = composite_approval_subject_bytes(expected) == composite_approval_subject_bytes(
            subject
        )
    except AdhelmError:
        matches = False
    if not matches:
        raise AdhelmError(
            "APPROVAL_SOURCE_PLAN_MISMATCH",
            "The receipt differs from the supplied local composite plan.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    return subject


def accept_composite_execution_plan_receipt(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    receipt: bytes,
    expected_challenge: ApprovalChallenge,
    trust: PinnedApprovalTrust,
    plan: CompositePlan,
) -> AcceptedNativeApproval:
    """Verify, consume, and record one provider-neutral composite plan grant.

    The recorded grant binds the composite digest and every subplan digest, so
    it is the same one-use, digest-bound direct authorization the kernel already
    requires.  This records an authorization; it starts no provider work.
    """

    checked_trust = _checked_trust(trust, ledger)
    return _accept_exact_receipt(
        kernel,
        ledger,
        receipt=receipt,
        expected_challenge=expected_challenge,
        configuration=checked_trust.configuration,
        verification_public_key_x963=checked_trust.public_key_x963,
        composite_binding=_CompositeApprovalBinding(plan=_checked_plan(plan)),
    )


def accept_attempt_recovery_receipt(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    receipt: bytes,
    expected_challenge: ApprovalChallenge,
    trust: PinnedApprovalTrust,
    plan: CompositePlan,
    attempts: Sequence[tuple[SubPlan, str]],
) -> AcceptedNativeApproval:
    """Verify and consume one receipt that authorizes recording exact attempts.

    ``attempts`` pairs each unresolved subplan with the durable ``APPLY_STARTED``
    record digest it never resolved.  The returned grant and approval host are
    the inputs to ``SafetyKernel.record_attempt_recovery``; this function itself
    performs no provider read, records no observation, and releases no barrier.
    """

    checked_trust = _checked_trust(trust, ledger)
    return _accept_exact_receipt(
        kernel,
        ledger,
        receipt=receipt,
        expected_challenge=expected_challenge,
        configuration=checked_trust.configuration,
        verification_public_key_x963=checked_trust.public_key_x963,
        composite_binding=_CompositeApprovalBinding(
            plan=_checked_plan(plan), attempts=_checked_attempt_pairs(attempts)
        ),
    )


def _checked_plan(value: object) -> CompositePlan:
    if type(value) is not CompositePlan:
        raise AdhelmError(
            "APPROVAL_SOURCE_PLAN_REQUIRED",
            "A provider-neutral receipt requires the exact local composite plan.",
        )
    return value


def _checked_attempt_pairs(value: object) -> tuple[tuple[SubPlan, str], ...]:
    if (type(value) is not tuple and type(value) is not list) or not value:
        raise AdhelmError(
            "APPROVAL_SOURCE_PLAN_REQUIRED",
            "A recovery receipt requires the exact unresolved attempts.",
        )
    items: list[Any] = list(value)
    pairs: list[tuple[SubPlan, str]] = []
    for item in items:
        if (
            (type(item) is not tuple and type(item) is not list)
            or len(item) != 2
            or type(item[0]) is not SubPlan
            or type(item[1]) is not str
        ):
            raise AdhelmError(
                "APPROVAL_SOURCE_PLAN_REQUIRED",
                "A recovery receipt requires the exact unresolved attempts.",
            )
        pairs.append((item[0], item[1]))
    return tuple(pairs)


def _accept_native_approval_bridge_candidate(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    candidate: NativeApprovalBridgeCandidateV1,
    request: NativeApprovalBridgeRequestV1,
    expected_challenge: ApprovalChallenge,
    configuration: ApprovalHostConfiguration,
    expected_metadata_digest: str,
    exchange_started_at: datetime,
    minimum_current_time: datetime,
    source_plan_record_digest: str,
) -> AcceptedNativeApproval:
    """Consume a freshly revalidated private bridge candidate without a pin.

    This is intentionally an internal handoff. The returned candidate key is
    evidence from a completed native current-verification/consumption exchange,
    not a caller-selected trust pin. It never constructs ``PinnedApprovalTrust``
    or returns receipt/key bytes to a caller.
    """

    def revalidate_at(now: datetime) -> NativeApprovalBridgeCandidateV1:
        try:
            if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("current time is invalid")
            normalized = now.astimezone(UTC)
            if (
                type(minimum_current_time) is not datetime
                or minimum_current_time.tzinfo is None
                or minimum_current_time.utcoffset() is None
                or normalized < minimum_current_time.astimezone(UTC)
            ):
                raise ValueError("local bridge clock rolled back")
            return revalidate_native_approval_bridge_candidate_v1(
                candidate,
                request=request,
                configuration=configuration,
                expected_metadata_digest=expected_metadata_digest,
                exchange_started_at=exchange_started_at,
                current_now=normalized,
            )
        except AdhelmError:
            raise
        except Exception:
            raise AdhelmError(
                "NATIVE_APPROVAL_BRIDGE_BINDING_INVALID",
                "Native approval bridge evidence does not bind the expected local request.",
                exit_code=ExitCode.PRECONDITION,
            ) from None

    checked = revalidate_at(_current_time(kernel))
    # Receipt/key bytes are obtained only from a fresh full candidate
    # revalidation and stay inside this exact acceptance handoff. M32 exposes
    # no generic raw-candidate extractor.
    try:
        checked_challenge = _checked_exact_challenge(expected_challenge, configuration)
        candidate_challenge = object.__getattribute__(
            checked, "_NativeApprovalBridgeCandidateV1__canonical_challenge"
        )
        receipt = object.__getattribute__(
            checked, "_NativeApprovalBridgeCandidateV1__canonical_receipt"
        )
        public_key = object.__getattribute__(
            checked, "_NativeApprovalBridgeCandidateV1__active_public_key_x963"
        )
        if (
            type(candidate_challenge) is not bytes
            or candidate_challenge != checked_challenge.canonical_bytes
            or type(receipt) is not bytes
            or type(public_key) is not bytes
        ):
            raise ValueError("candidate material is invalid")
    except Exception:
        raise AdhelmError(
            "NATIVE_APPROVAL_BRIDGE_BINDING_INVALID",
            "Native approval bridge evidence does not bind the expected local request.",
            exit_code=ExitCode.PRECONDITION,
        ) from None

    def before_receipt_verification(now: datetime) -> None:
        revalidate_at(now)

    return _accept_exact_receipt(
        kernel,
        ledger,
        receipt=receipt,
        expected_challenge=checked_challenge,
        configuration=configuration,
        verification_public_key_x963=public_key,
        source_plan_record_digest=source_plan_record_digest,
        before_receipt_verification=before_receipt_verification,
    )


def _accept_native_meta_campaign_pause_approval_bridge_candidate_v2(
    kernel: SafetyKernel,
    ledger: Ledger,
    *,
    candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    expected_challenge: ApprovalChallenge,
    configuration: ApprovalHostConfiguration,
    expected_metadata_digest: str,
    exchange_started_at: datetime,
    minimum_current_time: datetime,
    source_plan_record_digest: str,
) -> AcceptedNativeApproval:
    """Consume a freshly revalidated Meta-only V2 candidate without a pin.

    This is intentionally a separate private handoff from the V1 bridge.  It
    exposes no generic candidate decoder, raw receipt/key extractor, installed
    binding resolver, or provider capability.  The receipt and current native
    key stay within this stack until the shared exact local claim path consumes
    them for the already-retained Meta PAUSE source.
    """

    def revalidate_at(now: datetime) -> NativeMetaCampaignPauseApprovalBridgeCandidateV2:
        try:
            if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("current time is invalid")
            normalized = now.astimezone(UTC)
            if (
                type(minimum_current_time) is not datetime
                or minimum_current_time.tzinfo is None
                or minimum_current_time.utcoffset() is None
                or normalized < minimum_current_time.astimezone(UTC)
            ):
                raise ValueError("local bridge clock rolled back")
            return revalidate_meta_campaign_pause_approval_bridge_candidate_v2(
                candidate,
                request=request,
                configuration=configuration,
                expected_metadata_digest=expected_metadata_digest,
                exchange_started_at=exchange_started_at,
                current_now=normalized,
            )
        except AdhelmError:
            raise
        except Exception:
            raise _meta_campaign_pause_bridge_v2_binding_invalid() from None

    checked = revalidate_at(_current_time(kernel))
    # Candidate material is intentionally accessed only after a complete V2
    # revalidation.  It remains local to this exact receipt-claim handoff.
    try:
        checked_challenge = _checked_exact_challenge(expected_challenge, configuration)
        candidate_challenge = object.__getattribute__(
            checked, "_NativeMetaCampaignPauseApprovalBridgeCandidateV2__canonical_challenge"
        )
        receipt = object.__getattribute__(
            checked, "_NativeMetaCampaignPauseApprovalBridgeCandidateV2__canonical_receipt"
        )
        public_key = object.__getattribute__(
            checked, "_NativeMetaCampaignPauseApprovalBridgeCandidateV2__active_public_key_x963"
        )
        if (
            type(candidate_challenge) is not bytes
            or candidate_challenge != checked_challenge.canonical_bytes
            or type(receipt) is not bytes
            or type(public_key) is not bytes
        ):
            raise ValueError("candidate material is invalid")
    except Exception:
        raise _meta_campaign_pause_bridge_v2_binding_invalid() from None

    def before_receipt_verification(now: datetime) -> None:
        revalidate_at(now)

    return _accept_exact_receipt(
        kernel,
        ledger,
        receipt=receipt,
        expected_challenge=checked_challenge,
        configuration=configuration,
        verification_public_key_x963=public_key,
        source_plan_record_digest=source_plan_record_digest,
        before_receipt_verification=before_receipt_verification,
    )


def _meta_campaign_pause_bridge_v2_binding_invalid() -> AdhelmError:
    return AdhelmError(
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_BINDING_INVALID",
        "Meta campaign PAUSE bridge evidence does not bind the expected local request.",
        exit_code=ExitCode.PRECONDITION,
    )


def _recording_unverified() -> AdhelmError:
    return AdhelmError(
        "APPROVAL_RECORDING_UNVERIFIED",
        "Local approval recording could not be verified; no provider operation was attempted.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Verify canonical history before a new review; never replay this receipt.",
    )
