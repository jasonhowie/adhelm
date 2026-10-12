"""Closed Python verifier for the macOS approval-host receipt protocol.

This module builds and parses only the canonical v1 approval challenges of the
native host contract.  Five of them are provider-specific and predate the
generalized kernel.  The two added by ADR 0012 --
:attr:`ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN` and
:attr:`ApprovalChallengeKind.ATTEMPT_RECOVERY` -- are provider-neutral: their
subject binds digests, a bounded spend summary, and a closed list of redacted
``review_lines`` that a host renders verbatim, so a new guarded write does not
need a new challenge kind, a new review schema, or a second bridge.

A verified receipt is evidence that an independently pinned key signed the
exact reviewed challenge.  It is *not* a grant, replay claim, provider
authorization, or execution path.  No kind in this module adds an installed
capability: the native host must be separately changed, signed, and installed
before any of these bytes can be reviewed by a person.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from pydantic import BaseModel, ValidationError

from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import MAX_CHILD_EGRESS_COUNT
from adhelm.plan_records import revalidate_openai_pause_action
from adhelm.plan_views import _validated_action
from adhelm.privacy import validate_data
from adhelm.providers.ga4_create_contract import (
    Ga4KeyEventCreatePlannedAction,
    revalidate_ga4_key_event_create_planned_action,
)
from adhelm.providers.meta_pause_contract import (
    MetaCampaignPausePlannedAction,
    revalidate_meta_campaign_pause_planned_action,
)
from adhelm.providers.openai_pause_contract import OpenAIPausePlannedAction
from adhelm.safety import (
    CompositePlan,
    DelegationPolicy,
    OperationKind,
    SubPlan,
    attempt_recovery_subject,
)

_SCHEMA_VERSION: Final = 1
_SAFE_INTEGER: Final = 9_007_199_254_740_991
_CHALLENGE_DEPTH: Final = 20
_CHALLENGE_ITEMS: Final = 256
_CHALLENGE_STRING_BYTES: Final = 4_096
_RECEIPT_BYTES: Final = 16_384
_RECEIPT_DEPTH: Final = 4
_RECEIPT_ITEMS: Final = 32
_RECEIPT_STRING_BYTES: Final = 1_024
_NONCE_BYTES: Final = 32
_RECEIPT_DOMAIN: Final = b"adhelm.macos-approval-host.receipt.v1\0"
_RECEIPT_ID = re.compile(r"^approval-receipt:[A-Za-z0-9_-]{43}$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]+$")
_TIMESTAMP = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?:\.(?P<microsecond>[0-9]{6}))?Z$"
)
_RECEIPT_TIMESTAMP = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"\.(?P<millisecond>[0-9]{3})Z$"
)
#: The GA4 CREATE subject keeps its deliberately narrower fixed subset; see
#: ``_validate_ga4_native_subset``.  The OpenAI PAUSE subject no longer has a
#: hard-coded pair: it validates the time zone the plan actually carries.
_SUPPORTED_GA4_TIMEZONES: Final = frozenset({"UTC", "America/Chicago"})
_POLICY_OPERATIONS: Final = frozenset(item.value for item in OperationKind)
_TIMEZONE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,2}$")
_RESERVED_TIMEZONE_PREFIXES: Final = ("posix/", "right/")
_RESERVED_TIMEZONE_NAMES: Final = frozenset({"Factory", "localtime"})
_OPAQUE_REF = re.compile(r"^opaque:sha256:[0-9a-f]{64}$")
#: One review line is printable ASCII from a closed set, so a host can render
#: it verbatim without a bidirectional, control, or homoglyph surprise.
_REVIEW_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,:;()\[\]/+%_-]*$")
_HEX64_RUN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
_REFERENCE_VALUE = re.compile(r"^[a-z][a-z0-9_-]*:sha256:[0-9a-f]{64}$")
_MAXIMUM_REVIEW_LINES: Final = 24
_MAXIMUM_REVIEW_LINE_BYTES: Final = 120
#: A review line making one of these claims is *derived* from the structured
#: subject, never supplied.  Any line containing one of these markers, matched
#: case-insensitively, must be one of the derived lines that lead the list, so
#: a supplied line cannot reuse the spend-summary or authorized-operation
#: phrasing a reviewer is actually being asked about.  This is a phrase guard,
#: not a semantic one; the derived lines lead the list for that reason.
_RESERVED_REVIEW_MARKERS: Final = ("incremental spend", "authorized operation")
_MAXIMUM_CHILD_EGRESS_COUNT: Final = MAX_CHILD_EGRESS_COUNT
#: Domain separation for the subplan resource pseudonym a native review shows
#: instead of the planned resource name.
_RESOURCE_REF_DOMAIN: Final = "adhelm.approval.composite-resource-ref.v1"
#: Operations that cannot increase provider spend.  A subplan using one of them
#: with an explicit zero cap summarizes as ``NOT_SPEND_CAPABLE``; every other
#: operation always shows its integer minor-unit cap, including a zero cap.
_NON_SPEND_OPERATIONS: Final = frozenset(
    {OperationKind.PAUSE, OperationKind.ARCHIVE, OperationKind.RECOVER}
)


class ApprovalChallengeKind(StrEnum):
    """The complete native approval-host v1 challenge family."""

    OPENAI_PAUSE_PLAN = "OPENAI_PAUSE_PLAN"
    GA4_KEY_EVENT_CREATE_PLAN = "GA4_KEY_EVENT_CREATE_PLAN"
    META_CAMPAIGN_PAUSE_PLAN = "META_CAMPAIGN_PAUSE_PLAN"
    DELEGATION_POLICY_INSTALL = "DELEGATION_POLICY_INSTALL"
    DELEGATION_POLICY_REVOKE = "DELEGATION_POLICY_REVOKE"
    #: One compiled composite plan, summarized provider-neutrally.  This is the
    #: kind every future guarded write uses; it replaces a bespoke kind per
    #: operation and per provider.
    COMPOSITE_EXECUTION_PLAN = "COMPOSITE_EXECUTION_PLAN"
    #: Recording the observed state of exact unresolved attempts.  It is a
    #: sibling kind rather than a subject variant of the composite kind because
    #: the receipt-signing domain is per kind, and because its bound digests are
    #: recovery subjects rather than the composite's plan and subplan digests.
    ATTEMPT_RECOVERY = "ATTEMPT_RECOVERY"


#: The two provider-neutral kinds share one subject dataclass, one wire shape,
#: one review projection, and one acceptance branch.
COMPOSITE_APPROVAL_KINDS: Final = frozenset(
    {ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN, ApprovalChallengeKind.ATTEMPT_RECOVERY}
)
_ACTION_BOUND_KINDS: Final = frozenset(
    {
        ApprovalChallengeKind.OPENAI_PAUSE_PLAN,
        ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN,
        ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN,
    }
)
_PLAN_BOUND_KINDS: Final = _ACTION_BOUND_KINDS | COMPOSITE_APPROVAL_KINDS


@dataclass(frozen=True, slots=True)
class ApprovalHostConfiguration:
    """Closed, non-secret verifier configuration supplied by the integration.

    This value is intentionally separate from an enrolled public-key pin.  Its
    digest can be retained as configuration evidence, but a retained digest is
    never a source of trust on a later verifier invocation.
    """

    host_ref: str
    signing_key_ref: str
    maximum_challenge_bytes: int = 65_536
    maximum_challenge_lifetime_seconds: int = 300
    maximum_issued_at_skew_seconds: int = 60
    maximum_subplans: int = 6
    maximum_policy_items: int = 64

    def __post_init__(self) -> None:
        if not _safe_text(self.host_ref, maximum_bytes=256) or not _safe_text(
            self.signing_key_ref, maximum_bytes=256
        ):
            raise ValueError("Approval host configuration references are invalid.")
        _require_exact_bounded_int(self.maximum_challenge_bytes, minimum=1, maximum=65_536)
        _require_exact_bounded_int(self.maximum_challenge_lifetime_seconds, minimum=1, maximum=300)
        _require_exact_bounded_int(self.maximum_issued_at_skew_seconds, minimum=0, maximum=300)
        _require_exact_bounded_int(self.maximum_subplans, minimum=1, maximum=6)
        _require_exact_bounded_int(self.maximum_policy_items, minimum=1, maximum=64)

    def to_data(self) -> dict[str, str | int]:
        """Return closed canonical configuration data without a trust claim."""
        data = asdict(self)
        if not isinstance(data, dict):  # pragma: no cover - dataclasses contract
            raise ValueError("Approval host configuration is invalid.")
        return data

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_data())

    @property
    def configuration_digest(self) -> str:
        return _sha256(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class OpenAIPauseApprovalSubject:
    grantor_ref: str
    action: OpenAIPausePlannedAction


@dataclass(frozen=True, slots=True)
class Ga4KeyEventCreateApprovalSubject:
    """The one closed GA4 CREATE action reviewable by the native host.

    This is deliberately a separate subject, rather than a generic plan
    carrier.  The wire parser revalidates the complete action shape before a
    receipt can bind it.
    """

    grantor_ref: str
    action: Ga4KeyEventCreatePlannedAction


@dataclass(frozen=True, slots=True)
class MetaCampaignPauseApprovalSubject:
    """The exact M40 Meta PAUSE action reviewable by the native host.

    This subject carries one closed, revalidated M40 action only.  It cannot
    promote the plan into a bridge request, provider write, or execution grant.
    """

    grantor_ref: str
    action: MetaCampaignPausePlannedAction


@dataclass(frozen=True, slots=True)
class CompositeSpendBound:
    """One subplan's declared incremental spend cap in integer minor units."""

    minor_units: int
    currency: str


@dataclass(frozen=True, slots=True)
class CompositeCurrencyBound:
    """The maximum declared cap across every spend-capable subplan of one currency."""

    currency: str
    maximum_minor_units: int


@dataclass(frozen=True, slots=True)
class CompositeSubplanSummary:
    """One provider-neutral entry a native host can render without a provider schema."""

    subplan_digest: str
    provider: str
    operation: OperationKind
    resource_ref: str
    expected_child_egress_count: int
    #: ``None`` is the wire's ``NOT_SPEND_CAPABLE``, never an unknown cap.
    spend_bound: CompositeSpendBound | None
    #: Present only for :attr:`ApprovalChallengeKind.ATTEMPT_RECOVERY`.
    attempt_subject_digest: str | None = None
    started_record_digest: str | None = None


@dataclass(frozen=True, slots=True)
class CompositeExecutionApprovalSubject:
    """The one provider-neutral approval subject shared by both new kinds.

    ``plan_digest`` and ``subplan_digests`` are exactly the values a resulting
    :class:`adhelm.safety.DirectGrant` binds, so acceptance never has to invent a
    second mapping.  For a composite they are the compiled plan's own digests;
    for an attempt recovery they are the recovery subject digests that
    :func:`adhelm.safety.attempt_recovery_subject` produced.

    This subject carries digests, bounded counts, an integer spend summary, and
    closed redacted review lines.  It deliberately carries no provider payload,
    no resource name, no credential reference, and no free-form text.
    """

    binding_kind: ApprovalChallengeKind
    grantor_ref: str
    plan_digest: str
    subplan_digests: tuple[str, ...]
    subplans: tuple[CompositeSubplanSummary, ...]
    spend_bounds_by_currency: tuple[CompositeCurrencyBound, ...]
    operations: tuple[OperationKind, ...]
    #: The compiled plan's own expiry.  A ``COMPOSITE_EXECUTION_PLAN`` challenge
    #: may not outlive it, because approving a plan past its expiry would
    #: authorize work the kernel will refuse.  An ``ATTEMPT_RECOVERY`` challenge
    #: deliberately may: an attempt this kernel never finished journaling is
    #: discovered on a later run, when the short-lived plan has long expired,
    #: and the kernel checks only the recovery grant's own expiry.  The wire
    #: therefore names this field ``source_plan_expires_at`` for that kind, so a
    #: port cannot apply the wrong rule by reading the wrong field.
    plan_expires_at: datetime
    plan_expires_at_text: str
    review_lines: tuple[str, ...]
    #: Present only for :attr:`ApprovalChallengeKind.ATTEMPT_RECOVERY`.
    source_plan_digest: str | None = None
    authorized_operation: OperationKind | None = None


@dataclass(frozen=True, slots=True)
class DelegationPolicyInstallSubject:
    policy: DelegationPolicy


@dataclass(frozen=True, slots=True)
class DelegationPolicyRevokeSubject:
    policy: DelegationPolicy
    revoked_at: datetime
    revoked_at_text: str


type ApprovalSubject = (
    OpenAIPauseApprovalSubject
    | Ga4KeyEventCreateApprovalSubject
    | MetaCampaignPauseApprovalSubject
    | CompositeExecutionApprovalSubject
    | DelegationPolicyInstallSubject
    | DelegationPolicyRevokeSubject
)


@dataclass(frozen=True, slots=True)
class ApprovalChallenge:
    """One structurally validated canonical approval challenge.

    Consumers must not treat manual construction of this dataclass as proof.
    Receipt verification reparses :attr:`canonical_bytes` and ignores mutable
    or forged field values on the supplied instance.
    """

    kind: ApprovalChallengeKind
    nonce: bytes
    issued_at: datetime
    issued_at_text: str
    expires_at: datetime
    expires_at_text: str
    subject: ApprovalSubject
    canonical_bytes: bytes
    digest: str

    @property
    def grantor_ref(self) -> str:
        subject = self.subject
        if isinstance(
            subject,
            (
                OpenAIPauseApprovalSubject,
                Ga4KeyEventCreateApprovalSubject,
                MetaCampaignPauseApprovalSubject,
                CompositeExecutionApprovalSubject,
            ),
        ):
            return subject.grantor_ref
        return subject.policy.grantor_ref

    @property
    def composite_subject(self) -> CompositeExecutionApprovalSubject | None:
        subject = self.subject
        return subject if isinstance(subject, CompositeExecutionApprovalSubject) else None

    @property
    def action(
        self,
    ) -> (
        OpenAIPausePlannedAction
        | Ga4KeyEventCreatePlannedAction
        | MetaCampaignPausePlannedAction
        | None
    ):
        subject = self.subject
        return (
            subject.action
            if isinstance(
                subject,
                (
                    OpenAIPauseApprovalSubject,
                    Ga4KeyEventCreateApprovalSubject,
                    MetaCampaignPauseApprovalSubject,
                ),
            )
            else None
        )

    @property
    def ga4_action(self) -> Ga4KeyEventCreatePlannedAction | None:
        subject = self.subject
        return subject.action if isinstance(subject, Ga4KeyEventCreateApprovalSubject) else None

    @property
    def meta_pause_action(self) -> MetaCampaignPausePlannedAction | None:
        subject = self.subject
        return subject.action if isinstance(subject, MetaCampaignPauseApprovalSubject) else None

    @property
    def policy(self) -> DelegationPolicy | None:
        subject = self.subject
        return (
            subject.policy
            if isinstance(subject, (DelegationPolicyInstallSubject, DelegationPolicyRevokeSubject))
            else None
        )

    @property
    def revoked_at(self) -> datetime | None:
        subject = self.subject
        return subject.revoked_at if isinstance(subject, DelegationPolicyRevokeSubject) else None

    @property
    def revoked_at_text(self) -> str | None:
        subject = self.subject
        return (
            subject.revoked_at_text if isinstance(subject, DelegationPolicyRevokeSubject) else None
        )

    def to_data(self) -> dict[str, Any]:
        """Return the nested canonical wire object for redacted local storage."""
        return _wire_object(
            self.canonical_bytes,
            maximum_bytes=65_536,
            maximum_depth=_CHALLENGE_DEPTH,
            maximum_items=_CHALLENGE_ITEMS,
            maximum_string_bytes=_CHALLENGE_STRING_BYTES,
        )


@dataclass(frozen=True, slots=True)
class ApprovalReviewFacts:
    """Canonical deterministic facts the native review binds into a receipt."""

    canonical_bytes: bytes
    digest: str

    def to_data(self) -> dict[str, Any]:
        return _wire_object(
            self.canonical_bytes,
            maximum_bytes=16_384,
            maximum_depth=8,
            maximum_items=64,
            maximum_string_bytes=4_096,
        )


@dataclass(frozen=True, slots=True)
class ExpectedReceiptSigningPayload:
    """Exact unsigned receipt bytes and domain-separated bytes to be signed.

    This is an inspection/testing helper only.  It has no key, permission,
    replay, or authorization capability.
    """

    kind: ApprovalChallengeKind
    receipt_id: str
    issued_at: datetime
    issued_at_text: str
    canonical_unsigned_receipt: bytes
    domain_separated_bytes: bytes
    review_digest: str

    def unsigned_data(self) -> dict[str, Any]:
        return _wire_object(
            self.canonical_unsigned_receipt,
            maximum_bytes=_RECEIPT_BYTES,
            maximum_depth=_RECEIPT_DEPTH,
            maximum_items=_RECEIPT_ITEMS,
            maximum_string_bytes=_RECEIPT_STRING_BYTES,
        )


@dataclass(frozen=True, slots=True)
class VerifiedApprovalReceipt:
    """Cryptographically verified receipt evidence, not an authorization grant."""

    canonical_bytes: bytes
    digest: str
    receipt_id: str
    host_ref: str
    key_ref: str
    grantor_ref: str
    issued_at: datetime
    issued_at_text: str
    expires_at: datetime
    expires_at_text: str
    kind: ApprovalChallengeKind
    subject: ApprovalSubject
    challenge_digest: str
    review_digest: str
    configuration_digest: str
    pinned_public_key_fingerprint: str
    signature: bytes
    action_digest: str | None
    plan_digest: str | None
    subplan_digests: tuple[str, ...]
    policy_digest: str | None
    policy_id: str | None
    policy_version: int | None

    @property
    def action(
        self,
    ) -> (
        OpenAIPausePlannedAction
        | Ga4KeyEventCreatePlannedAction
        | MetaCampaignPausePlannedAction
        | None
    ):
        subject = self.subject
        return (
            subject.action
            if isinstance(
                subject,
                (
                    OpenAIPauseApprovalSubject,
                    Ga4KeyEventCreateApprovalSubject,
                    MetaCampaignPauseApprovalSubject,
                ),
            )
            else None
        )

    @property
    def ga4_action(self) -> Ga4KeyEventCreatePlannedAction | None:
        subject = self.subject
        return subject.action if isinstance(subject, Ga4KeyEventCreateApprovalSubject) else None

    @property
    def meta_pause_action(self) -> MetaCampaignPausePlannedAction | None:
        subject = self.subject
        return subject.action if isinstance(subject, MetaCampaignPauseApprovalSubject) else None

    @property
    def composite_subject(self) -> CompositeExecutionApprovalSubject | None:
        subject = self.subject
        return subject if isinstance(subject, CompositeExecutionApprovalSubject) else None

    @property
    def policy(self) -> DelegationPolicy | None:
        subject = self.subject
        return (
            subject.policy
            if isinstance(subject, (DelegationPolicyInstallSubject, DelegationPolicyRevokeSubject))
            else None
        )

    @property
    def revoked_at(self) -> datetime | None:
        subject = self.subject
        return subject.revoked_at if isinstance(subject, DelegationPolicyRevokeSubject) else None

    @property
    def revoked_at_text(self) -> str | None:
        subject = self.subject
        return (
            subject.revoked_at_text if isinstance(subject, DelegationPolicyRevokeSubject) else None
        )

    def to_data(self) -> dict[str, Any]:
        """Return verified wire data; this does not attest current file state."""
        return parse_approval_receipt_wire(self.canonical_bytes)


def build_openai_pause_challenge(
    action: OpenAIPausePlannedAction,
    *,
    grantor_ref: str,
    expires_at: datetime,
    configuration: ApprovalHostConfiguration,
    nonce: bytes | None = None,
    issued_at: datetime | None = None,
    now: datetime | None = None,
) -> ApprovalChallenge:
    """Build the one closed direct-action challenge supported by v1."""
    try:
        checked_config = _checked_configuration(configuration)
        checked_action = _checked_action(action)
        if not _safe_text(grantor_ref, maximum_bytes=256):
            raise ValueError("grantor reference is invalid")
        return _build_challenge(
            {
                "schema_version": _SCHEMA_VERSION,
                "kind": ApprovalChallengeKind.OPENAI_PAUSE_PLAN.value,
                "nonce": _nonce_text(nonce),
                "issued_at": _challenge_time_text(issued_at, now),
                "expires_at": _timestamp_from_datetime(expires_at),
                "grantor_ref": grantor_ref,
                "action": _action_data(checked_action),
            },
            configuration=checked_config,
            now=now,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _challenge_error() from None


def build_ga4_key_event_create_challenge(
    action: Ga4KeyEventCreatePlannedAction,
    *,
    grantor_ref: str,
    expires_at: datetime,
    configuration: ApprovalHostConfiguration,
    nonce: bytes | None = None,
    issued_at: datetime | None = None,
    now: datetime | None = None,
) -> ApprovalChallenge:
    """Build the closed native-review challenge for one retained GA4 CREATE plan.

    This only binds a fully revalidated local action into a challenge.  It
    does not authenticate its keyed semantic state, prove current provider
    permission, resolve a host pin, or authorize egress.
    """

    try:
        checked_config = _checked_configuration(configuration)
        checked_action = _checked_ga4_action(action)
        if not _safe_text(grantor_ref, maximum_bytes=256):
            raise ValueError("grantor reference is invalid")
        return _build_challenge(
            {
                "schema_version": _SCHEMA_VERSION,
                "kind": ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN.value,
                "nonce": _nonce_text(nonce),
                "issued_at": _challenge_time_text(issued_at, now),
                "expires_at": _timestamp_from_datetime(expires_at),
                "grantor_ref": grantor_ref,
                "action": _ga4_action_data(checked_action),
            },
            configuration=checked_config,
            now=now,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _challenge_error() from None


def build_meta_campaign_pause_challenge(
    action: MetaCampaignPausePlannedAction,
    *,
    grantor_ref: str,
    expires_at: datetime,
    configuration: ApprovalHostConfiguration,
    nonce: bytes | None = None,
    issued_at: datetime | None = None,
    now: datetime | None = None,
) -> ApprovalChallenge:
    """Build one closed native-review challenge for a retained M40 Meta PAUSE plan.

    This binds a fully revalidated local planning action into review evidence.
    It does not establish native-host installation, current provider state,
    execution authority, a bridge route, or a Meta Graph mutation.
    """

    try:
        checked_config = _checked_configuration(configuration)
        checked_action = _checked_meta_pause_action(action)
        if not _safe_text(grantor_ref, maximum_bytes=256):
            raise ValueError("grantor reference is invalid")
        return _build_challenge(
            {
                "schema_version": _SCHEMA_VERSION,
                "kind": ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN.value,
                "nonce": _nonce_text(nonce),
                "issued_at": _challenge_time_text(issued_at, now),
                "expires_at": _timestamp_from_datetime(expires_at),
                "grantor_ref": grantor_ref,
                "action": _meta_pause_action_data(checked_action),
            },
            configuration=checked_config,
            now=now,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _challenge_error() from None


def build_composite_execution_plan_challenge(
    plan: CompositePlan,
    *,
    grantor_ref: str,
    review_lines: Sequence[str],
    expires_at: datetime,
    configuration: ApprovalHostConfiguration,
    nonce: bytes | None = None,
    issued_at: datetime | None = None,
    now: datetime | None = None,
) -> ApprovalChallenge:
    """Build the provider-neutral challenge for one compiled composite plan.

    The subject summarizes the plan; it never carries it.  A native host renders
    the bounded ``review_lines`` verbatim and needs no provider schema, so a new
    guarded write on any provider reuses this kind rather than adding one.

    This binds a locally revalidated plan into review evidence.  It does not
    prove the plan is retained, current, executable, or covered by a provider
    credential, and it authorizes nothing by itself.
    """

    try:
        checked_config = _checked_configuration(configuration)
        checked_plan = _checked_composite_plan(plan)
        if not _safe_text(grantor_ref, maximum_bytes=256):
            raise ValueError("grantor reference is invalid")
        subject = _composite_subject_from_plan(
            checked_plan,
            grantor_ref=grantor_ref,
            review_lines=review_lines,
            configuration=checked_config,
        )
        return _build_challenge(
            {
                "schema_version": _SCHEMA_VERSION,
                "kind": ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN.value,
                "nonce": _nonce_text(nonce),
                "issued_at": _challenge_time_text(issued_at, now),
                "expires_at": _timestamp_from_datetime(expires_at),
                "grantor_ref": grantor_ref,
                "subject": _composite_subject_data(subject),
            },
            configuration=checked_config,
            now=now,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _challenge_error() from None


def build_attempt_recovery_challenge(
    plan: CompositePlan,
    attempts: Sequence[tuple[SubPlan, str]],
    *,
    grantor_ref: str,
    review_lines: Sequence[str],
    expires_at: datetime,
    configuration: ApprovalHostConfiguration,
    nonce: bytes | None = None,
    issued_at: datetime | None = None,
    now: datetime | None = None,
) -> ApprovalChallenge:
    """Build the challenge for recording exact unresolved attempts' current state.

    Each element of ``attempts`` is one subplan of ``plan`` and the durable
    ``APPLY_STARTED`` record digest it never resolved.  The subject binds the
    same :func:`adhelm.safety.attempt_recovery_subject` digests the kernel checks,
    so a receipt for one lost acknowledgement can never release another.

    Recording an observation sends no provider mutation, so every entry
    summarizes as ``NOT_SPEND_CAPABLE`` and the currency summary is empty.
    """

    try:
        checked_config = _checked_configuration(configuration)
        checked_plan = _checked_composite_plan(plan)
        if not _safe_text(grantor_ref, maximum_bytes=256):
            raise ValueError("grantor reference is invalid")
        subject = _composite_subject_from_plan(
            checked_plan,
            grantor_ref=grantor_ref,
            review_lines=review_lines,
            configuration=checked_config,
            attempts=_checked_attempts(checked_plan, attempts, configuration=checked_config),
        )
        return _build_challenge(
            {
                "schema_version": _SCHEMA_VERSION,
                "kind": ApprovalChallengeKind.ATTEMPT_RECOVERY.value,
                "nonce": _nonce_text(nonce),
                "issued_at": _challenge_time_text(issued_at, now),
                "expires_at": _timestamp_from_datetime(expires_at),
                "grantor_ref": grantor_ref,
                "subject": _composite_subject_data(subject),
            },
            configuration=checked_config,
            now=now,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _challenge_error() from None


def expected_composite_approval_subject(
    plan: CompositePlan,
    *,
    kind: ApprovalChallengeKind,
    grantor_ref: str,
    review_lines: Sequence[str],
    configuration: ApprovalHostConfiguration,
    attempts: Sequence[tuple[SubPlan, str]] = (),
) -> CompositeExecutionApprovalSubject:
    """Rebuild the exact subject one local plan and its attempts must produce.

    Acceptance compares a verified subject with the caller's exact local plan
    through this single derivation, rather than a second hand-written
    comparison that could drift from the builder.  It proves nothing about
    retention, currentness, or authority by itself.
    """

    try:
        checked_config = _checked_configuration(configuration)
        checked_plan = _checked_composite_plan(plan)
        if not _safe_text(grantor_ref, maximum_bytes=256):
            raise ValueError("grantor reference is invalid")
        if kind is ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN:
            if attempts:
                raise ValueError("a composite subject has no unresolved attempts")
            checked_attempts = None
        elif kind is ApprovalChallengeKind.ATTEMPT_RECOVERY:
            checked_attempts = _checked_attempts(
                checked_plan, attempts, configuration=checked_config
            )
        else:
            raise ValueError("kind is not provider neutral")
        return _composite_subject_from_plan(
            checked_plan,
            grantor_ref=grantor_ref,
            review_lines=review_lines,
            configuration=checked_config,
            attempts=checked_attempts,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _challenge_error() from None


def composite_approval_subject_bytes(subject: CompositeExecutionApprovalSubject) -> bytes:
    """Return the canonical subject bytes the challenge and receipt digest bind."""

    if type(subject) is not CompositeExecutionApprovalSubject:
        raise _challenge_error()
    try:
        return _composite_subject_bytes(subject)
    except AdhelmError:
        raise
    except Exception:
        raise _challenge_error() from None


def build_delegation_policy_install_challenge(
    policy: DelegationPolicy,
    *,
    expires_at: datetime,
    configuration: ApprovalHostConfiguration,
    nonce: bytes | None = None,
    issued_at: datetime | None = None,
    now: datetime | None = None,
) -> ApprovalChallenge:
    """Build a closed v1 delegation-policy installation challenge."""
    try:
        checked_config = _checked_configuration(configuration)
        checked_policy = _checked_policy(policy, configuration=checked_config)
        return _build_challenge(
            {
                "schema_version": _SCHEMA_VERSION,
                "kind": ApprovalChallengeKind.DELEGATION_POLICY_INSTALL.value,
                "nonce": _nonce_text(nonce),
                "issued_at": _challenge_time_text(issued_at, now),
                "expires_at": _timestamp_from_datetime(expires_at),
                "policy": _policy_data(checked_policy),
            },
            configuration=checked_config,
            now=now,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _challenge_error() from None


def build_delegation_policy_revoke_challenge(
    policy: DelegationPolicy,
    *,
    revoked_at: datetime,
    expires_at: datetime,
    configuration: ApprovalHostConfiguration,
    nonce: bytes | None = None,
    issued_at: datetime | None = None,
    now: datetime | None = None,
) -> ApprovalChallenge:
    """Build a closed v1 delegation-policy revocation challenge."""
    try:
        checked_config = _checked_configuration(configuration)
        checked_policy = _checked_policy(policy, configuration=checked_config)
        return _build_challenge(
            {
                "schema_version": _SCHEMA_VERSION,
                "kind": ApprovalChallengeKind.DELEGATION_POLICY_REVOKE.value,
                "nonce": _nonce_text(nonce),
                "issued_at": _challenge_time_text(issued_at, now),
                "expires_at": _timestamp_from_datetime(expires_at),
                "policy": _policy_data(checked_policy),
                "revoked_at": _timestamp_from_datetime(revoked_at),
            },
            configuration=checked_config,
            now=now,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _challenge_error() from None


def decode_approval_challenge(
    raw: bytes,
    *,
    configuration: ApprovalHostConfiguration,
    now: datetime | None = None,
) -> ApprovalChallenge:
    """Decode a current challenge and enforce its issuance/expiry window."""
    try:
        return _decode_challenge(
            raw,
            configuration=_checked_configuration(configuration),
            now=_checked_now(now),
            enforce_current_window=True,
        )
    except AdhelmError as exc:
        if exc.code in {"APPROVAL_CHALLENGE_NOT_YET_VALID", "APPROVAL_CHALLENGE_EXPIRED"}:
            raise
        raise _challenge_error() from None
    except Exception:
        raise _challenge_error() from None


def parse_approval_challenge_wire(
    raw: bytes, *, configuration: ApprovalHostConfiguration
) -> ApprovalChallenge:
    """Structurally parse canonical historical wire data without current eligibility.

    This helper validates the closed challenge shape and configuration binding.
    It intentionally does not attest freshness, replay status, authorization,
    host installation, or provider eligibility.
    """
    try:
        return _decode_challenge(
            raw,
            configuration=_checked_configuration(configuration),
            now=None,
            enforce_current_window=False,
        )
    except AdhelmError:
        raise _challenge_error() from None
    except Exception:
        raise _challenge_error() from None


def approval_review_facts(
    challenge: ApprovalChallenge, *, configuration: ApprovalHostConfiguration
) -> ApprovalReviewFacts:
    """Reconstruct native-compatible canonical review facts from canonical wire."""
    try:
        checked = _challenge_from_instance(challenge, configuration=configuration)
        data: dict[str, Any] = {
            "approval_kind": checked.kind.value,
            "challenge_digest": checked.digest,
            "challenge_expires_at": checked.expires_at_text,
            "challenge_nonce": _base64url_encode(checked.nonce),
        }
        subject = checked.subject
        if isinstance(subject, OpenAIPauseApprovalSubject):
            data["grantor_ref"] = subject.grantor_ref
            data["openai_pause"] = _pause_review_data(subject.action)
        elif isinstance(subject, Ga4KeyEventCreateApprovalSubject):
            data["grantor_ref"] = subject.grantor_ref
            data["ga4_key_event_create"] = _ga4_key_event_create_review_data(subject.action)
        elif isinstance(subject, MetaCampaignPauseApprovalSubject):
            data["grantor_ref"] = subject.grantor_ref
            data["meta_campaign_pause"] = _meta_campaign_pause_review_data(subject.action)
        elif isinstance(subject, CompositeExecutionApprovalSubject):
            data["grantor_ref"] = subject.grantor_ref
            data["composite_execution"] = _composite_review_data(subject)
        else:
            data["policy"] = _policy_review_data(subject.policy)
            if isinstance(subject, DelegationPolicyRevokeSubject):
                data["revoked_at"] = subject.revoked_at_text
        encoded = canonical_bytes(data)
        return ApprovalReviewFacts(canonical_bytes=encoded, digest=_sha256(encoded))
    except AdhelmError:
        raise
    except Exception:
        raise _challenge_error() from None


def receipt_signing_payload(
    expected_challenge: ApprovalChallenge,
    *,
    configuration: ApprovalHostConfiguration,
    receipt_id: str,
    issued_at: datetime,
) -> ExpectedReceiptSigningPayload:
    """Return the exact v1 unsigned receipt and domain-separated signing bytes.

    This cannot sign, claim a nonce, enroll a key, authorize work, or issue a
    grant.  It exists so a host implementation and offline vectors can bind the
    same complete canonical receipt fields.
    """
    try:
        checked = _challenge_from_instance(expected_challenge, configuration=configuration)
        checked_configuration = _checked_configuration(configuration)
        if not _RECEIPT_ID.fullmatch(receipt_id):
            raise ValueError("receipt identifier is invalid")
        issued_at_text = _receipt_timestamp_from_datetime(issued_at)
        parsed_issued_at = _parse_receipt_timestamp(issued_at_text)
        if parsed_issued_at >= checked.expires_at:
            raise ValueError("receipt issue time is invalid")
        facts = approval_review_facts(checked, configuration=checked_configuration)
        unsigned = _unsigned_receipt_data(
            checked,
            configuration=checked_configuration,
            receipt_id=receipt_id,
            issued_at_text=issued_at_text,
            review_digest=facts.digest,
        )
        encoded = canonical_bytes(unsigned)
        return ExpectedReceiptSigningPayload(
            kind=checked.kind,
            receipt_id=receipt_id,
            issued_at=parsed_issued_at,
            issued_at_text=issued_at_text,
            canonical_unsigned_receipt=encoded,
            domain_separated_bytes=_receipt_domain_bytes(checked.kind, encoded),
            review_digest=facts.digest,
        )
    except AdhelmError as exc:
        if exc.code in {"APPROVAL_CHALLENGE_NOT_YET_VALID", "APPROVAL_CHALLENGE_EXPIRED"}:
            raise
        raise _challenge_error() from None
    except Exception:
        raise _receipt_error() from None


def parse_approval_receipt_wire(raw: bytes) -> dict[str, Any]:
    """Parse a bounded canonical receipt shape without verifying its signature.

    The returned nested data can be retained for redacted local evidence.  It
    does not prove a key was trusted, a signature was valid, a challenge was
    current, or any authority was issued.
    """
    try:
        data = _wire_object(
            raw,
            maximum_bytes=_RECEIPT_BYTES,
            maximum_depth=_RECEIPT_DEPTH,
            maximum_items=_RECEIPT_ITEMS,
            maximum_string_bytes=_RECEIPT_STRING_BYTES,
        )
        _validate_receipt_shape(data)
        return data
    except Exception:
        raise _receipt_error() from None


def verify_approval_receipt(
    receipt: bytes,
    *,
    expected_challenge: ApprovalChallenge,
    configuration: ApprovalHostConfiguration,
    trusted_public_key_x963: bytes,
    now: datetime | None = None,
) -> VerifiedApprovalReceipt:
    """Verify a receipt against an independently supplied P-256 public-key pin.

    The verifier reconstitutes ``expected_challenge`` exclusively from its
    canonical wire bytes.  It does not consume the receipt or nonce, install a
    policy, make a kernel grant, or authorize provider egress.
    """
    try:
        checked_configuration = _checked_configuration(configuration)
        checked = _challenge_from_instance(expected_challenge, configuration=checked_configuration)
        checked_now = _checked_now(now)
        if checked_now >= checked.expires_at:
            raise _receipt_expired_error()
        data = parse_approval_receipt_wire(receipt)
        signature_text = data["signature"]
        if type(signature_text) is not str:
            raise ValueError("signature is invalid")
        signature = _decode_signature(signature_text)
        issued_at_text = data["issued_at"]
        if type(issued_at_text) is not str:
            raise ValueError("receipt issue time is invalid")
        receipt_issued_at = _parse_receipt_timestamp(issued_at_text)
        skew = _seconds(checked_configuration.maximum_issued_at_skew_seconds)
        if (
            receipt_issued_at < checked.issued_at - skew
            or receipt_issued_at > checked_now + skew
            or receipt_issued_at >= checked.expires_at
        ):
            raise _receipt_issue_error()
        receipt_id = data["receipt_id"]
        if type(receipt_id) is not str:
            raise ValueError("receipt identifier is invalid")
        expected = receipt_signing_payload(
            checked,
            configuration=checked_configuration,
            receipt_id=receipt_id,
            issued_at=receipt_issued_at,
        )
        unsigned = {key: value for key, value in data.items() if key != "signature"}
        if canonical_bytes(unsigned) != expected.canonical_unsigned_receipt:
            raise _receipt_binding_error()
        public_key, key_fingerprint = _pinned_public_key(trusted_public_key_x963)
        _verify_signature(public_key, signature, expected.domain_separated_bytes)
        subject = checked.subject
        action_digest: str | None = None
        plan_digest: str | None = None
        subplan_digests: tuple[str, ...] = ()
        policy_digest: str | None = None
        policy_id: str | None = None
        policy_version: int | None = None
        if isinstance(subject, OpenAIPauseApprovalSubject):
            action_digest = _sha256(_action_bytes(subject.action))
            plan_digest = subject.action.plan.plan_digest
            subplan_digests = subject.action.plan.subplan_digests
        elif isinstance(subject, Ga4KeyEventCreateApprovalSubject):
            action_digest = _sha256(_ga4_action_bytes(subject.action))
            plan_digest = subject.action.plan.plan_digest
            subplan_digests = subject.action.plan.subplan_digests
        elif isinstance(subject, MetaCampaignPauseApprovalSubject):
            action_digest = _sha256(_meta_pause_action_bytes(subject.action))
            plan_digest = subject.action.plan.plan_digest
            subplan_digests = subject.action.plan.subplan_digests
        elif isinstance(subject, CompositeExecutionApprovalSubject):
            action_digest = _sha256(_composite_subject_bytes(subject))
            plan_digest = subject.plan_digest
            subplan_digests = subject.subplan_digests
        else:
            policy_digest = _sha256(_policy_bytes(subject.policy))
            policy_id = subject.policy.policy_id
            policy_version = subject.policy.policy_version
        return VerifiedApprovalReceipt(
            canonical_bytes=receipt,
            digest=_sha256(receipt),
            receipt_id=receipt_id,
            host_ref=checked_configuration.host_ref,
            key_ref=checked_configuration.signing_key_ref,
            grantor_ref=checked.grantor_ref,
            issued_at=receipt_issued_at,
            issued_at_text=issued_at_text,
            expires_at=checked.expires_at,
            expires_at_text=checked.expires_at_text,
            kind=checked.kind,
            subject=subject,
            challenge_digest=checked.digest,
            review_digest=expected.review_digest,
            configuration_digest=checked_configuration.configuration_digest,
            pinned_public_key_fingerprint=key_fingerprint,
            signature=signature,
            action_digest=action_digest,
            plan_digest=plan_digest,
            subplan_digests=subplan_digests,
            policy_digest=policy_digest,
            policy_id=policy_id,
            policy_version=policy_version,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _receipt_error() from None


def _build_challenge(
    data: dict[str, Any], *, configuration: ApprovalHostConfiguration, now: datetime | None
) -> ApprovalChallenge:
    try:
        encoded = canonical_bytes(data)
        return _decode_challenge(
            encoded,
            configuration=configuration,
            now=_checked_now(now),
            enforce_current_window=True,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _challenge_error() from None


def _decode_challenge(
    raw: bytes,
    *,
    configuration: ApprovalHostConfiguration,
    now: datetime | None,
    enforce_current_window: bool,
) -> ApprovalChallenge:
    data = _wire_object(
        raw,
        maximum_bytes=configuration.maximum_challenge_bytes,
        maximum_depth=_CHALLENGE_DEPTH,
        maximum_items=_CHALLENGE_ITEMS,
        maximum_string_bytes=_CHALLENGE_STRING_BYTES,
    )
    if type(data.get("schema_version")) is not int or data["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("challenge schema is invalid")
    raw_kind = data.get("kind")
    if type(raw_kind) is not str:
        raise ValueError("challenge kind is invalid")
    try:
        kind = ApprovalChallengeKind(raw_kind)
    except ValueError as exc:
        raise ValueError("challenge kind is invalid") from exc
    nonce = _decode_nonce(data.get("nonce"))
    issued_at_text = _required_string(data, "issued_at")
    expires_at_text = _required_string(data, "expires_at")
    issued_at = _parse_challenge_timestamp(issued_at_text)
    expires_at = _parse_challenge_timestamp(expires_at_text)
    if expires_at <= issued_at or expires_at - issued_at > _seconds(
        configuration.maximum_challenge_lifetime_seconds
    ):
        raise ValueError("challenge timing is invalid")
    if enforce_current_window:
        if now is None:  # pragma: no cover - caller always supplies checked now
            raise ValueError("challenge clock is unavailable")
        offset = issued_at - now
        skew = _seconds(configuration.maximum_issued_at_skew_seconds)
        if offset > skew:
            raise _challenge_not_yet_valid_error()
        if offset < -skew:
            raise ValueError("challenge issue time is stale")
        if now >= expires_at:
            raise _challenge_expired_error()
    subject: ApprovalSubject
    if kind is ApprovalChallengeKind.OPENAI_PAUSE_PLAN:
        _require_exact_keys(
            data,
            {"schema_version", "kind", "nonce", "issued_at", "expires_at", "grantor_ref", "action"},
        )
        grantor_ref = _required_string(data, "grantor_ref")
        if not _safe_text(grantor_ref, maximum_bytes=256):
            raise ValueError("grantor reference is invalid")
        action_value = data.get("action")
        if type(action_value) is not dict:
            raise ValueError("action is invalid")
        action = _action_from_data(action_value)
        if expires_at > action.plan.expires_at:
            raise ValueError("challenge expiry exceeds plan")
        subject = OpenAIPauseApprovalSubject(grantor_ref=grantor_ref, action=action)
    elif kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN:
        _require_exact_keys(
            data,
            {"schema_version", "kind", "nonce", "issued_at", "expires_at", "grantor_ref", "action"},
        )
        grantor_ref = _required_string(data, "grantor_ref")
        if not _safe_text(grantor_ref, maximum_bytes=256):
            raise ValueError("grantor reference is invalid")
        action_value = data.get("action")
        if type(action_value) is not dict:
            raise ValueError("action is invalid")
        ga4_action = _ga4_action_from_data(action_value)
        if expires_at > ga4_action.plan.expires_at:
            raise ValueError("challenge expiry exceeds plan")
        subject = Ga4KeyEventCreateApprovalSubject(grantor_ref=grantor_ref, action=ga4_action)
    elif kind is ApprovalChallengeKind.META_CAMPAIGN_PAUSE_PLAN:
        _require_exact_keys(
            data,
            {"schema_version", "kind", "nonce", "issued_at", "expires_at", "grantor_ref", "action"},
        )
        grantor_ref = _required_string(data, "grantor_ref")
        if not _safe_text(grantor_ref, maximum_bytes=256):
            raise ValueError("grantor reference is invalid")
        action_value = data.get("action")
        if type(action_value) is not dict:
            raise ValueError("action is invalid")
        meta_pause_action = _meta_pause_action_from_data(action_value)
        if expires_at > meta_pause_action.plan.expires_at:
            raise ValueError("challenge expiry exceeds plan")
        subject = MetaCampaignPauseApprovalSubject(
            grantor_ref=grantor_ref,
            action=meta_pause_action,
        )
    elif kind in COMPOSITE_APPROVAL_KINDS:
        _require_exact_keys(
            data,
            {
                "schema_version",
                "kind",
                "nonce",
                "expires_at",
                "issued_at",
                "grantor_ref",
                "subject",
            },
        )
        grantor_ref = _required_string(data, "grantor_ref")
        if not _safe_text(grantor_ref, maximum_bytes=256):
            raise ValueError("grantor reference is invalid")
        subject_value = data.get("subject")
        if type(subject_value) is not dict:
            raise ValueError("subject is invalid")
        composite_subject = _composite_subject_from_data(
            subject_value,
            kind=kind,
            grantor_ref=grantor_ref,
            configuration=configuration,
        )
        if (
            kind is ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN
            and expires_at > composite_subject.plan_expires_at
        ):
            # Approving a plan past its own expiry would authorize work the
            # kernel will refuse.  A recovery approval is deliberately not
            # clamped: an attempt this kernel never finished journaling is found
            # on a later run, when the short-lived plan has long expired, and
            # ``record_attempt_recovery`` checks only the recovery grant's own
            # expiry.  The ordinary challenge-lifetime ceiling still bounds it.
            raise ValueError("challenge expiry exceeds plan")
        subject = composite_subject
    elif kind is ApprovalChallengeKind.DELEGATION_POLICY_INSTALL:
        _require_exact_keys(
            data,
            {"schema_version", "kind", "nonce", "issued_at", "expires_at", "policy"},
        )
        policy_value = data.get("policy")
        if type(policy_value) is not dict:
            raise ValueError("policy is invalid")
        policy = _policy_from_data(policy_value, configuration=configuration)
        if policy.revoked_at is not None:
            raise ValueError("policy is already revoked")
        subject = DelegationPolicyInstallSubject(policy=policy)
    else:
        _require_exact_keys(
            data,
            {
                "schema_version",
                "kind",
                "nonce",
                "issued_at",
                "expires_at",
                "policy",
                "revoked_at",
            },
        )
        policy_value = data.get("policy")
        if type(policy_value) is not dict:
            raise ValueError("policy is invalid")
        policy = _policy_from_data(policy_value, configuration=configuration)
        if policy.revoked_at is not None:
            raise ValueError("policy is already revoked")
        revoked_at_text = _required_string(data, "revoked_at")
        revoked_at = _parse_challenge_timestamp(revoked_at_text)
        if revoked_at < issued_at or revoked_at > expires_at:
            raise ValueError("revocation timing is invalid")
        subject = DelegationPolicyRevokeSubject(
            policy=policy, revoked_at=revoked_at, revoked_at_text=revoked_at_text
        )
    return ApprovalChallenge(
        kind=kind,
        nonce=nonce,
        issued_at=issued_at,
        issued_at_text=issued_at_text,
        expires_at=expires_at,
        expires_at_text=expires_at_text,
        subject=subject,
        canonical_bytes=raw,
        digest=_sha256(raw),
    )


def _challenge_from_instance(
    challenge: ApprovalChallenge, *, configuration: ApprovalHostConfiguration
) -> ApprovalChallenge:
    if type(challenge) is not ApprovalChallenge or type(challenge.canonical_bytes) is not bytes:
        raise _challenge_error()
    try:
        return _decode_challenge(
            challenge.canonical_bytes,
            configuration=_checked_configuration(configuration),
            now=None,
            enforce_current_window=False,
        )
    except Exception:
        raise _challenge_error() from None


def _checked_action(value: object) -> OpenAIPausePlannedAction:
    try:
        action = revalidate_openai_pause_action(value)
        action = _validated_action(action)
        _validated_iana_timezone(action.binding.account_timezone)
        raw = _action_data(action)
        checked = _action_from_data(raw)
        return checked
    except (AdhelmError, ValidationError, TypeError, ValueError):
        raise _challenge_error() from None


def _action_from_data(data: dict[str, Any]) -> OpenAIPausePlannedAction:
    try:
        _validate_action_timestamp_text(data)
        action = revalidate_openai_pause_action(data)
        action = _validated_action(action)
        _validated_iana_timezone(action.binding.account_timezone)
        if canonical_bytes(_action_data(action)) != canonical_bytes(data):
            raise ValueError("action reconstruction does not match wire")
        return action
    except (AdhelmError, ValidationError, TypeError, ValueError):
        raise ValueError("action is invalid") from None


def _action_data(action: OpenAIPausePlannedAction) -> dict[str, Any]:
    try:
        data = action.model_dump(mode="json", warnings="error")
    except Exception:
        raise ValueError("action cannot be serialized") from None
    if type(data) is not dict:
        raise ValueError("action is invalid")
    return data


def _action_bytes(action: OpenAIPausePlannedAction) -> bytes:
    return canonical_bytes(_action_data(action))


def _checked_ga4_action(value: object) -> Ga4KeyEventCreatePlannedAction:
    """Detach the full GA4 action through its own strict JSON boundary."""

    try:
        action = revalidate_ga4_key_event_create_planned_action(value)
        _validate_ga4_native_subset(action)
        raw = _ga4_action_data(action)
        return _ga4_action_from_data(raw)
    except (AdhelmError, ValidationError, TypeError, ValueError):
        raise _challenge_error() from None


def _ga4_action_from_data(data: dict[str, Any]) -> Ga4KeyEventCreatePlannedAction:
    try:
        action = revalidate_ga4_key_event_create_planned_action(data)
        _validate_ga4_native_subset(action)
        if canonical_bytes(_ga4_action_data(action)) != canonical_bytes(data):
            raise ValueError("action reconstruction does not match wire")
        return action
    except (AdhelmError, ValidationError, TypeError, ValueError):
        raise ValueError("GA4 CREATE action is invalid") from None


def _ga4_action_data(action: Ga4KeyEventCreatePlannedAction) -> dict[str, Any]:
    try:
        data = action.model_dump(mode="json", warnings="error")
    except Exception:
        raise ValueError("GA4 CREATE action cannot be serialized") from None
    if type(data) is not dict:
        raise ValueError("GA4 CREATE action is invalid")
    return data


def _ga4_action_bytes(action: Ga4KeyEventCreatePlannedAction) -> bytes:
    return canonical_bytes(_ga4_action_data(action))


def _checked_meta_pause_action(value: object) -> MetaCampaignPausePlannedAction:
    """Detach one full M40 action through its strict canonical boundary."""

    try:
        action = revalidate_meta_campaign_pause_planned_action(value)
        _validate_meta_pause_native_subject(action)
        raw = _meta_pause_action_data(action)
        return _meta_pause_action_from_data(raw)
    except (AdhelmError, ValidationError, TypeError, ValueError):
        raise _challenge_error() from None


def _meta_pause_action_from_data(data: dict[str, Any]) -> MetaCampaignPausePlannedAction:
    try:
        action = revalidate_meta_campaign_pause_planned_action(data)
        _validate_meta_pause_native_subject(action)
        if canonical_bytes(_meta_pause_action_data(action)) != canonical_bytes(data):
            raise ValueError("action reconstruction does not match wire")
        return action
    except (AdhelmError, ValidationError, TypeError, ValueError):
        raise ValueError("Meta PAUSE action is invalid") from None


def _meta_pause_action_data(action: MetaCampaignPausePlannedAction) -> dict[str, Any]:
    try:
        data = action.to_data()
    except Exception:
        raise ValueError("Meta PAUSE action cannot be serialized") from None
    if type(data) is not dict:
        raise ValueError("Meta PAUSE action is invalid")
    return data


def _meta_pause_action_bytes(action: MetaCampaignPausePlannedAction) -> bytes:
    return canonical_bytes(_meta_pause_action_data(action))


def _validate_meta_pause_native_subject(action: MetaCampaignPausePlannedAction) -> None:
    """Keep native review constrained to M40's one no-authority PAUSE subject."""

    plan = action.plan
    if len(plan.subplans) != 1:
        raise ValueError("Meta PAUSE subject requires one subplan")
    subplan = plan.subplans[0]
    campaign = action.observation_binding.campaign
    postcondition = action.postcondition
    if (
        action.status != "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
        or action.execution_authority is not False
        or action.live_validated is not False
        or action.conditional_write_proof is not False
        or action.idempotency_proof is not False
        or action.execution_binding.required_scope != "ads_management"
        or campaign.configured_status != "ACTIVE"
        or postcondition.postcondition_kind != "CONFIGURATION_ONLY"
        or postcondition.configured_status != "PAUSED"
        or postcondition.effective_delivery != "NOT_PREDICTED"
        or subplan.provider != "meta_ads"
        or subplan.operation is not OperationKind.PAUSE
        or subplan.max_spend.minor_units != 0
        or subplan.max_spend.currency != action.account_binding.currency
        or len(subplan.egress) != 1
        or subplan.egress[0].host != "graph.facebook.com"
        or subplan.egress[0].purpose != "fixed Meta Graph v26 campaign pause"
        or subplan.recovery_actions != (OperationKind.PAUSE,)
        or subplan.expires_at != plan.expires_at
    ):
        raise ValueError("Meta PAUSE action is outside the fixed native subject")


def validate_review_lines(
    value: object,
    *,
    maximum_lines: int = _MAXIMUM_REVIEW_LINES,
    derived: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return the closed bounded lines a native host may render verbatim.

    The host renders these strings without interpreting them, so the boundary is
    an allowlist rather than a denylist: printable ASCII from a fixed set, no
    leading, trailing, or repeated spaces, no scheme separator, and no 64-hex
    run, because a digest belongs in a structured field where the receipt binds
    it.  The existing redaction screen then runs over the whole list.

    ``derived`` are the lines this subject's own structured fields produce.  The
    list must begin with exactly those, in order, and no later line may make a
    reserved claim, so a supplied line cannot state a spend allowance or an
    authorized operation that the signed summary does not.

    This is a bounded screen, not a claim that every sensitive meaning is
    detectable in free text.
    """

    if type(value) is not tuple and type(value) is not list:
        raise ValueError("review lines are invalid")
    lines: list[Any] = list(value)
    if (
        type(maximum_lines) is not int
        or not 1 <= maximum_lines <= _MAXIMUM_REVIEW_LINES
        or not lines
        or len(lines) > maximum_lines
        or type(derived) is not tuple
        or len(derived) > len(lines)
    ):
        raise ValueError("review lines are invalid")
    for line in lines:
        if (
            type(line) is not str
            or not 1 <= len(line.encode("utf-8")) <= _MAXIMUM_REVIEW_LINE_BYTES
            or _REVIEW_LINE.fullmatch(line) is None
            or line != line.strip()
            or "  " in line
            or "://" in line
            or _HEX64_RUN.search(line) is not None
        ):
            raise ValueError("review lines are invalid")
    if tuple(lines[: len(derived)]) != derived:
        raise ValueError("review lines do not restate the derived summary")
    if any(_is_reserved_review_line(line) for line in lines[len(derived) :]):
        raise ValueError("a supplied review line cannot make a derived claim")
    try:
        validate_data(lines)
    except AdhelmError:
        # Report one uniform reason: which screen refused a line is not a fact
        # a caller needs, and the codes differ between the two screens.
        raise ValueError("review lines are invalid") from None
    return tuple(lines)


def _is_reserved_review_line(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in _RESERVED_REVIEW_MARKERS)


def _derived_review_lines(
    *,
    kind: ApprovalChallengeKind,
    operations: tuple[OperationKind, ...],
    bounds: tuple[CompositeCurrencyBound, ...],
) -> tuple[str, ...]:
    """Render the review claims the structured subject alone decides.

    These lead every ``review_lines`` list and are recomputed on decode, so the
    spend statement a person reads is the spend summary the receipt binds, and a
    recovery approval cannot be presented as an execution approval.
    """

    lines: list[str] = []
    if kind is ApprovalChallengeKind.ATTEMPT_RECOVERY:
        attempted = ", ".join(item.value for item in operations)
        line = f"Authorized operation RECOVER (attempt of {attempted})"
        if len(line.encode("utf-8")) > _MAXIMUM_REVIEW_LINE_BYTES:
            line = f"Authorized operation RECOVER (attempt of {len(operations)} operation kinds)"
        lines.append(line)
        lines.append("This records the observed state and applies no provider change")
    if not bounds:
        lines.append("Maximum incremental spend NOT_SPEND_CAPABLE")
    else:
        lines.extend(
            f"Maximum incremental spend {bound.maximum_minor_units} minor units {bound.currency}"
            for bound in bounds
        )
    return tuple(lines)


def composite_subplan_review_lines(subplan: SubPlan) -> tuple[str, ...]:
    """Derive review lines from one compiled subplan's own redacted intent.

    This is the source for a PAUSE, ARCHIVE, or recovery subject, which has no
    compiler candidate to summarize.  It restates only facts the subplan already
    contains and adds no provider payload, resource name, or credential fact.

    It deliberately makes no spend statement.  The authoritative one is derived
    from the subject's own per-currency summary by
    :func:`_derived_review_lines`, so the two cannot disagree.
    """

    checked = _checked_subplan(subplan)
    hosts = sorted({target.host for target in checked.egress})
    egress_line = "Egress hosts " + ", ".join(hosts)
    if len(egress_line.encode("utf-8")) > _MAXIMUM_REVIEW_LINE_BYTES:
        egress_line = f"Egress hosts {len(hosts)} declared in the plan allowlist"
    recovery = ", ".join(item.value for item in checked.recovery_actions)
    return validate_review_lines(
        [
            f"Provider {checked.provider} operation {checked.operation.value}",
            f"Planned provider requests {checked.expected_child_egress_count}",
            egress_line,
            "Recovery actions "
            + (recovery if recovery else "NONE_DECLARED_RECONCILE_EXPORT_STOP_ONLY"),
            f"Provider API {checked.provider_api_version} capability {checked.capability_version}",
        ]
    )


def compiler_review_lines(
    provider: str, redacted_review: object, *, maximum_lines: int = _MAXIMUM_REVIEW_LINES
) -> tuple[str, ...]:
    """Derive review lines from any compiler's ``redacted_review()`` value.

    Every provider compiler already produces a closed redacted review model.
    This renders it as ordered ``label: value`` lines so the native host needs
    no provider-specific schema.  Digest-shaped and content-addressed reference
    values are omitted rather than rendered: the challenge and receipt bind the
    plan structurally, and a rendered digest is exactly what the line validator
    refuses.  A review whose remaining fields do not fit the line budget is
    refused rather than truncated.
    """

    if not _safe_text(provider, maximum_bytes=80):
        raise ValueError("provider is invalid")
    data = redacted_review
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json", warnings="error")
    if type(data) is not dict:
        raise ValueError("redacted review is invalid")
    lines = [f"Provider {provider} compiled candidate review"]
    for key in sorted(data):
        rendered = _rendered_review_value(data[key])
        if rendered is None:
            continue
        lines.append(f"{key.replace('_', ' ')}: {rendered}")
    return validate_review_lines(lines, maximum_lines=maximum_lines)


def _rendered_review_value(value: object) -> str | None:
    """Render one already-redacted scalar, or return ``None`` to omit it.

    Only a digest-shaped or content-addressed reference is omitted, because the
    receipt binds it structurally and a rendered digest is what the line
    validator refuses anyway.  Every other unrecognised shape raises: a nested
    field a future compiler adds must fail loudly here rather than disappear
    from the review without a signal.
    """

    if value is None:
        return "NOT_SUPPLIED"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is str:
        if _DIGEST.fullmatch(value) is not None or _REFERENCE_VALUE.fullmatch(value) is not None:
            return None
        return value
    if type(value) is dict:
        if set(value) == {"minor_units", "currency"}:
            minor_units = value["minor_units"]
            currency = value["currency"]
            if type(minor_units) is not int or type(currency) is not str:
                raise ValueError("redacted review money is invalid")
            return f"{minor_units} minor units {currency}"
        raise ValueError("redacted review mapping is not a renderable shape")
    if type(value) is list or type(value) is tuple:
        return f"{len(value)} values"
    raise ValueError("redacted review value is invalid")


def _validated_iana_timezone(value: object) -> str:
    """Validate one current IANA time-zone name carried by the plan itself.

    This replaces a two-entry hard-coded allowlist.  It proves the name resolves
    in the local database; it is not a claim about provider billing time, the
    current offset, or a future database revision.
    """

    if (
        type(value) is not str
        or not 1 <= len(value.encode("utf-8")) <= 64
        or _TIMEZONE_NAME.fullmatch(value) is None
        or value.startswith(_RESERVED_TIMEZONE_PREFIXES)
        or value in _RESERVED_TIMEZONE_NAMES
    ):
        raise ValueError("time zone is invalid")
    try:
        ZoneInfo(value)
    except Exception:
        raise ValueError("time zone is invalid") from None
    return value


def _checked_composite_plan(value: object) -> CompositePlan:
    if type(value) is not CompositePlan:
        raise _challenge_error()
    try:
        return CompositePlan.model_validate(value.model_dump(mode="python", warnings=False))
    except (AdhelmError, ValidationError, TypeError, ValueError):
        raise _challenge_error() from None


def _checked_subplan(value: object) -> SubPlan:
    if type(value) is not SubPlan:
        raise ValueError("subplan is invalid")
    return SubPlan.model_validate(value.model_dump(mode="python", warnings=False))


def _checked_attempts(
    plan: CompositePlan,
    attempts: object,
    *,
    configuration: ApprovalHostConfiguration,
) -> tuple[tuple[SubPlan, str], ...]:
    """Bind each unresolved attempt to a subplan of this exact plan."""

    if type(attempts) is not tuple and type(attempts) is not list:
        raise ValueError("attempts are invalid")
    items: list[Any] = list(attempts)
    if not 1 <= len(items) <= configuration.maximum_subplans:
        raise ValueError("attempts are invalid")
    known = {subplan.subplan_digest for subplan in plan.subplans}
    checked: list[tuple[SubPlan, str]] = []
    seen: set[str] = set()
    for item in items:
        if (type(item) is not tuple and type(item) is not list) or len(item) != 2:
            raise ValueError("attempts are invalid")
        subplan = _checked_subplan(item[0])
        started = item[1]
        if (
            type(started) is not str
            or _DIGEST.fullmatch(started) is None
            or subplan.subplan_digest not in known
            or subplan.subplan_digest in seen
        ):
            raise ValueError("attempts are invalid")
        seen.add(subplan.subplan_digest)
        checked.append((subplan, started))
    ordered = {subplan.subplan_digest: index for index, subplan in enumerate(plan.subplans)}
    checked.sort(key=lambda pair: ordered[pair[0].subplan_digest])
    return tuple(checked)


def _resource_pseudonym(subplan: SubPlan) -> str:
    """A local, domain-separated pseudonym of the resource a subplan names.

    The subplan identifier is the ledger's ``resource_ref``.  It is not a
    provider identifier, but it is still a name; the native review shows this
    stable pseudonym instead so the same resource is recognisable across plans
    without displaying the name itself.
    """

    return "opaque:sha256:" + _sha256(
        canonical_bytes(
            {
                "domain": _RESOURCE_REF_DOMAIN,
                "provider": subplan.provider,
                "resource_ref": subplan.subplan_id,
                "stable_account_id": subplan.account.stable_account_id,
            }
        )
    )


def _spend_bound(subplan: SubPlan) -> CompositeSpendBound | None:
    if subplan.operation in _NON_SPEND_OPERATIONS and subplan.max_spend.minor_units == 0:
        return None
    return CompositeSpendBound(
        minor_units=subplan.max_spend.minor_units, currency=subplan.max_spend.currency
    )


def _currency_bounds(
    entries: Sequence[CompositeSubplanSummary],
) -> tuple[CompositeCurrencyBound, ...]:
    maxima: dict[str, int] = {}
    for entry in entries:
        if entry.spend_bound is None:
            continue
        currency = entry.spend_bound.currency
        maxima[currency] = max(maxima.get(currency, 0), entry.spend_bound.minor_units)
    return tuple(
        CompositeCurrencyBound(currency=currency, maximum_minor_units=maxima[currency])
        for currency in sorted(maxima)
    )


def _composite_subject_from_plan(
    plan: CompositePlan,
    *,
    grantor_ref: str,
    review_lines: object,
    configuration: ApprovalHostConfiguration,
    attempts: tuple[tuple[SubPlan, str], ...] | None = None,
) -> CompositeExecutionApprovalSubject:
    """Summarize one compiled plan, or its unresolved attempts, for native review."""

    subplans: tuple[tuple[SubPlan, str | None], ...]
    if attempts is None:
        subplans = tuple((subplan, None) for subplan in plan.subplans)
        kind = ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN
    else:
        subplans = tuple((subplan, started) for subplan, started in attempts)
        kind = ApprovalChallengeKind.ATTEMPT_RECOVERY
    if not 1 <= len(subplans) <= configuration.maximum_subplans:
        raise ValueError("composite subject size is invalid")
    entries: list[CompositeSubplanSummary] = []
    subjects: list[str] = []
    for subplan, started in subplans:
        if started is None:
            bound = _spend_bound(subplan)
            attempt_subject = None
        else:
            # Recording an observation issues no provider request, so a recovery
            # entry can never present an incremental spend allowance.
            bound = None
            attempt_subject = attempt_recovery_subject(plan, subplan, started_record_digest=started)
            subjects.append(attempt_subject)
        entries.append(
            CompositeSubplanSummary(
                subplan_digest=subplan.subplan_digest,
                provider=subplan.provider,
                operation=subplan.operation,
                resource_ref=_resource_pseudonym(subplan),
                expected_child_egress_count=subplan.expected_child_egress_count,
                spend_bound=bound,
                attempt_subject_digest=attempt_subject,
                started_record_digest=started,
            )
        )
    if attempts is None:
        plan_digest = plan.plan_digest
        subplan_digests = tuple(entry.subplan_digest for entry in entries)
        source_plan_digest = None
        authorized_operation = None
    else:
        # These are exactly the digests ``SafetyKernel._authorize_attempt_recovery``
        # requires of a recovery grant.
        plan_digest = _sha256(canonical_bytes(subjects))
        subplan_digests = tuple(subjects)
        source_plan_digest = plan.plan_digest
        authorized_operation = OperationKind.RECOVER
    bounds = _currency_bounds(entries)
    operations = tuple(
        OperationKind(value) for value in sorted({entry.operation.value for entry in entries})
    )
    return CompositeExecutionApprovalSubject(
        binding_kind=kind,
        grantor_ref=grantor_ref,
        plan_digest=plan_digest,
        subplan_digests=subplan_digests,
        subplans=tuple(entries),
        spend_bounds_by_currency=bounds,
        operations=operations,
        plan_expires_at=plan.expires_at,
        plan_expires_at_text=_timestamp_from_datetime(plan.expires_at),
        review_lines=_composite_review_lines(
            review_lines, kind=kind, operations=operations, bounds=bounds
        ),
        source_plan_digest=source_plan_digest,
        authorized_operation=authorized_operation,
    )


def _composite_review_lines(
    review_lines: object,
    *,
    kind: ApprovalChallengeKind,
    operations: tuple[OperationKind, ...],
    bounds: tuple[CompositeCurrencyBound, ...],
) -> tuple[str, ...]:
    """Lead the supplied lines with the claims the structured subject decides.

    Prepending is idempotent: a caller may pass either its own lines or a
    complete previously validated list, and both produce the same result, so
    acceptance can rebuild an expected subject from a verified one.
    """

    derived = _derived_review_lines(kind=kind, operations=operations, bounds=bounds)
    if type(review_lines) is not tuple and type(review_lines) is not list:
        raise ValueError("review lines are invalid")
    supplied: tuple[Any, ...] = tuple(review_lines)
    if supplied[: len(derived)] == derived:
        supplied = supplied[len(derived) :]
    return validate_review_lines(derived + supplied, derived=derived)


def _composite_entry_data(entry: CompositeSubplanSummary) -> dict[str, Any]:
    data: dict[str, Any] = {
        "expected_child_egress_count": entry.expected_child_egress_count,
        "operation": entry.operation.value,
        "provider": entry.provider,
        "resource_ref": entry.resource_ref,
        "spend_bound": (
            {"spend_capability": "NOT_SPEND_CAPABLE"}
            if entry.spend_bound is None
            else {
                "currency": entry.spend_bound.currency,
                "minor_units": entry.spend_bound.minor_units,
            }
        ),
        "subplan_digest": entry.subplan_digest,
    }
    if entry.attempt_subject_digest is not None:
        data["attempt_subject_digest"] = entry.attempt_subject_digest
        data["started_record_digest"] = entry.started_record_digest
    return data


def _composite_subject_data(subject: CompositeExecutionApprovalSubject) -> dict[str, Any]:
    data: dict[str, Any] = {
        "operations": [item.value for item in subject.operations],
        "plan_digest": subject.plan_digest,
        "review_lines": list(subject.review_lines),
        "spend_bounds_by_currency": [
            {"currency": bound.currency, "maximum_minor_units": bound.maximum_minor_units}
            for bound in subject.spend_bounds_by_currency
        ],
        "subplan_digests": list(subject.subplan_digests),
        "subplans": [_composite_entry_data(entry) for entry in subject.subplans],
    }
    if subject.binding_kind is ApprovalChallengeKind.ATTEMPT_RECOVERY:
        data["authorized_operation"] = OperationKind.RECOVER.value
        data["source_plan_digest"] = subject.source_plan_digest
        # A different field name, because a different rule applies to it.
        data["source_plan_expires_at"] = subject.plan_expires_at_text
    else:
        data["expires_at"] = subject.plan_expires_at_text
    return data


def _composite_subject_bytes(subject: CompositeExecutionApprovalSubject) -> bytes:
    return canonical_bytes(_composite_subject_data(subject))


_COMPOSITE_SUBJECT_FIELDS: Final = {
    "operations",
    "plan_digest",
    "review_lines",
    "spend_bounds_by_currency",
    "subplan_digests",
    "subplans",
}
_COMPOSITE_ENTRY_FIELDS: Final = {
    "expected_child_egress_count",
    "operation",
    "provider",
    "resource_ref",
    "spend_bound",
    "subplan_digest",
}


def _composite_subject_from_data(
    data: dict[str, Any],
    *,
    kind: ApprovalChallengeKind,
    grantor_ref: str,
    configuration: ApprovalHostConfiguration,
) -> CompositeExecutionApprovalSubject:
    """Rebuild and fully cross-check one provider-neutral subject from wire data.

    Every check here is internal consistency.  It proves the summary describes
    itself; it does not prove the summarized plan exists, is retained, or is
    current.  Acceptance compares these digests with the caller's exact plan.
    """

    try:
        recovery = kind is ApprovalChallengeKind.ATTEMPT_RECOVERY
        expected = set(_COMPOSITE_SUBJECT_FIELDS)
        expected |= (
            {"authorized_operation", "source_plan_digest", "source_plan_expires_at"}
            if recovery
            else {"expires_at"}
        )
        _require_exact_keys(data, expected)
        raw_entries = data["subplans"]
        if (
            type(raw_entries) is not list
            or not 1 <= len(raw_entries) <= configuration.maximum_subplans
        ):
            raise ValueError("composite subplans are invalid")
        entries = tuple(_composite_entry_from_data(item, recovery=recovery) for item in raw_entries)
        digests = _bounded_digest_list(
            data["subplan_digests"], maximum=configuration.maximum_subplans
        )
        bound_digests = tuple(
            entry.attempt_subject_digest if recovery else entry.subplan_digest for entry in entries
        )
        if tuple(digests) != bound_digests:
            raise ValueError("composite subplan digests do not match the summarized subplans")
        if len({entry.subplan_digest for entry in entries}) != len(entries):
            raise ValueError("composite subplan digests are not unique")
        plan_digest = _mapping_string(data, "plan_digest")
        if _DIGEST.fullmatch(plan_digest) is None:
            raise ValueError("composite plan digest is invalid")
        operations = _bounded_string_list(
            data["operations"], maximum=len(_POLICY_OPERATIONS), string_bytes=32
        )
        if operations != sorted({entry.operation.value for entry in entries}):
            raise ValueError("composite operations do not restate the summarized subplans")
        bounds = _currency_bounds_from_data(
            data["spend_bounds_by_currency"], maximum=configuration.maximum_subplans
        )
        if bounds != _currency_bounds(entries):
            raise ValueError("composite spend summary does not restate the subplan caps")
        checked_operations = tuple(OperationKind(value) for value in operations)
        # The lines a person reads are recomputed from the fields the receipt
        # binds, so a rendered spend or authorization claim cannot drift from
        # the structured summary.
        review_lines = validate_review_lines(
            data["review_lines"],
            derived=_derived_review_lines(kind=kind, operations=checked_operations, bounds=bounds),
        )
        source_plan_digest: str | None = None
        authorized_operation: OperationKind | None = None
        if recovery:
            if data["authorized_operation"] != OperationKind.RECOVER.value:
                raise ValueError("recovery subject authorizes another operation")
            source_plan_digest = _mapping_string(data, "source_plan_digest")
            if _DIGEST.fullmatch(source_plan_digest) is None:
                raise ValueError("recovery source plan digest is invalid")
            if plan_digest != _sha256(canonical_bytes(list(digests))):
                raise ValueError("recovery subject digest does not bind its exact attempts")
            if bounds or any(entry.spend_bound is not None for entry in entries):
                raise ValueError("recording an observation cannot carry a spend allowance")
            authorized_operation = OperationKind.RECOVER
            expires_at_text = _mapping_string(data, "source_plan_expires_at")
        else:
            expires_at_text = _mapping_string(data, "expires_at")
        expires_at = _parse_challenge_timestamp(expires_at_text)
        subject = CompositeExecutionApprovalSubject(
            binding_kind=kind,
            grantor_ref=grantor_ref,
            plan_digest=plan_digest,
            subplan_digests=tuple(digests),
            subplans=entries,
            spend_bounds_by_currency=bounds,
            operations=checked_operations,
            plan_expires_at=expires_at,
            plan_expires_at_text=expires_at_text,
            review_lines=review_lines,
            source_plan_digest=source_plan_digest,
            authorized_operation=authorized_operation,
        )
        if canonical_bytes(_composite_subject_data(subject)) != canonical_bytes(data):
            raise ValueError("subject reconstruction does not match wire")
        return subject
    except (AdhelmError, ValidationError, TypeError, ValueError):
        raise ValueError("composite approval subject is invalid") from None


def _composite_entry_from_data(value: object, *, recovery: bool) -> CompositeSubplanSummary:
    if type(value) is not dict:
        raise ValueError("composite subplan entry is invalid")
    expected = set(_COMPOSITE_ENTRY_FIELDS)
    if recovery:
        expected |= {"attempt_subject_digest", "started_record_digest"}
    _require_exact_keys(value, expected)
    subplan_digest = _mapping_string(value, "subplan_digest")
    provider = _mapping_string(value, "provider")
    resource_ref = _mapping_string(value, "resource_ref")
    count = value["expected_child_egress_count"]
    if (
        _DIGEST.fullmatch(subplan_digest) is None
        or not _safe_text(provider, maximum_bytes=80)
        or _OPAQUE_REF.fullmatch(resource_ref) is None
        or type(count) is not int
        or not 1 <= count <= _MAXIMUM_CHILD_EGRESS_COUNT
    ):
        raise ValueError("composite subplan entry is invalid")
    try:
        operation = OperationKind(_mapping_string(value, "operation"))
    except ValueError:
        raise ValueError("composite subplan entry is invalid") from None
    attempt_subject_digest: str | None = None
    started_record_digest: str | None = None
    if recovery:
        attempt_subject_digest = _mapping_string(value, "attempt_subject_digest")
        started_record_digest = _mapping_string(value, "started_record_digest")
        if (
            _DIGEST.fullmatch(attempt_subject_digest) is None
            or _DIGEST.fullmatch(started_record_digest) is None
        ):
            raise ValueError("composite subplan entry is invalid")
    bound = _spend_bound_from_data(value["spend_bound"])
    if recovery:
        # Recording an observation issues no provider request, whatever the
        # attempt's own operation was.
        if bound is not None:
            raise ValueError("recording an observation cannot carry a spend allowance")
    else:
        # ``NOT_SPEND_CAPABLE`` is a claim about the operation, not a free
        # choice: without this the wire could present a spend-capable CREATE as
        # incapable and drop it from the currency summary while every
        # restatement check still passed.  The converse keeps one canonical
        # encoding per plan, so Python and a native port cannot disagree on the
        # bytes for the same subplan.
        if bound is None and operation not in _NON_SPEND_OPERATIONS:
            raise ValueError("a spend-capable operation cannot be NOT_SPEND_CAPABLE")
        if bound is not None and operation in _NON_SPEND_OPERATIONS and bound.minor_units == 0:
            raise ValueError("a zero-cap non-spend operation must be NOT_SPEND_CAPABLE")
    return CompositeSubplanSummary(
        subplan_digest=subplan_digest,
        provider=provider,
        operation=operation,
        resource_ref=resource_ref,
        expected_child_egress_count=count,
        spend_bound=bound,
        attempt_subject_digest=attempt_subject_digest,
        started_record_digest=started_record_digest,
    )


def _spend_bound_from_data(value: object) -> CompositeSpendBound | None:
    if type(value) is not dict:
        raise ValueError("spend bound is invalid")
    if set(value) == {"spend_capability"}:
        if value["spend_capability"] != "NOT_SPEND_CAPABLE":
            raise ValueError("spend bound is invalid")
        return None
    _require_exact_keys(value, {"currency", "minor_units"})
    currency = value["currency"]
    minor_units = value["minor_units"]
    if (
        type(currency) is not str
        or _CURRENCY.fullmatch(currency) is None
        or type(minor_units) is not int
        or not 0 <= minor_units <= _SAFE_INTEGER
    ):
        raise ValueError("spend bound is invalid")
    return CompositeSpendBound(minor_units=minor_units, currency=currency)


def _currency_bounds_from_data(
    value: object, *, maximum: int
) -> tuple[CompositeCurrencyBound, ...]:
    if type(value) is not list or len(value) > maximum:
        raise ValueError("spend summary is invalid")
    bounds: list[CompositeCurrencyBound] = []
    for item in value:
        if type(item) is not dict:
            raise ValueError("spend summary is invalid")
        _require_exact_keys(item, {"currency", "maximum_minor_units"})
        currency = item["currency"]
        maximum_minor_units = item["maximum_minor_units"]
        if (
            type(currency) is not str
            or _CURRENCY.fullmatch(currency) is None
            or type(maximum_minor_units) is not int
            or not 0 <= maximum_minor_units <= _SAFE_INTEGER
        ):
            raise ValueError("spend summary is invalid")
        bounds.append(
            CompositeCurrencyBound(currency=currency, maximum_minor_units=maximum_minor_units)
        )
    currencies = [bound.currency for bound in bounds]
    if currencies != sorted(set(currencies)):
        raise ValueError("spend summary currencies are invalid")
    return tuple(bounds)


def _bounded_digest_list(value: object, *, maximum: int) -> list[str]:
    if type(value) is not list or not 1 <= len(value) <= maximum:
        raise ValueError("digest list is invalid")
    if any(type(item) is not str or _DIGEST.fullmatch(item) is None for item in value):
        raise ValueError("digest list is invalid")
    if len(set(value)) != len(value):
        raise ValueError("digest list is invalid")
    return list(value)


def _composite_review_data(subject: CompositeExecutionApprovalSubject) -> dict[str, Any]:
    """Return the closed provider-neutral facts a native reviewer can assess.

    This is the complete subject: it already contains only digests, bounded
    counts, integer caps, and validated review lines, so there is nothing
    further to redact.  It makes no claim about current provider state,
    permission, installation, or a completed operation.
    """

    return {
        "binding_kind": subject.binding_kind.value,
        "subject": _composite_subject_data(subject),
        "subject_digest": _sha256(_composite_subject_bytes(subject)),
    }


def _validate_ga4_native_subset(action: Ga4KeyEventCreatePlannedAction) -> None:
    """Keep the Python builder within the deliberately narrower native subject."""

    candidate = action.candidate.compilation.compiler_candidate
    conversion = action.candidate.compilation.conversion_binding
    if (
        action.execution_binding.target.expected_timezone not in _SUPPORTED_GA4_TIMEZONES
        or candidate.request.countingMethod != "ONCE_PER_EVENT"
        or candidate.request.defaultValue is not None
        or candidate.declared_field_names
        != tuple(field.name for field in conversion.bound_boolean_fields)
    ):
        raise ValueError("GA4 action is outside the implemented approval-host subset")


def _validate_action_timestamp_text(data: dict[str, Any]) -> None:
    """Keep the native decoder's lexical timestamp boundary, not just instants."""
    binding = data.get("binding")
    plan = data.get("plan")
    if type(binding) is not dict or type(plan) is not dict:
        raise ValueError("action is invalid")
    campaign = binding.get("campaign")
    if type(campaign) is not dict:
        raise ValueError("action is invalid")
    _parse_challenge_timestamp(_mapping_string(campaign, "start_at"))
    end_at = campaign.get("end_at")
    if end_at is not None:
        if type(end_at) is not str:
            raise ValueError("action is invalid")
        _parse_challenge_timestamp(end_at)
    _parse_challenge_timestamp(_mapping_string(plan, "expires_at"))
    subplans = plan.get("subplans")
    if type(subplans) is not list:
        raise ValueError("action is invalid")
    for subplan in subplans:
        if type(subplan) is not dict:
            raise ValueError("action is invalid")
        _parse_challenge_timestamp(_mapping_string(subplan, "expires_at"))


def _checked_policy(value: object, *, configuration: ApprovalHostConfiguration) -> DelegationPolicy:
    if type(value) is not DelegationPolicy:
        raise _challenge_error()
    try:
        raw = _policy_data(value)
        return _policy_from_data(raw, configuration=configuration)
    except (AdhelmError, ValidationError, TypeError, ValueError):
        raise _challenge_error() from None


def _policy_from_data(
    data: dict[str, Any], *, configuration: ApprovalHostConfiguration
) -> DelegationPolicy:
    try:
        _validate_policy_data(data, configuration=configuration)
        raw = canonical_bytes(data)
        policy = DelegationPolicy.model_validate_json(raw)
        if canonical_bytes(_policy_data(policy)) != raw:
            raise ValueError("policy reconstruction does not match wire")
        return policy
    except (ValidationError, TypeError, ValueError, AdhelmError):
        raise ValueError("policy is invalid") from None


def _policy_data(policy: DelegationPolicy) -> dict[str, Any]:
    try:
        data = policy.model_dump(mode="json", warnings="error")
    except Exception:
        raise ValueError("policy cannot be serialized") from None
    if type(data) is not dict:
        raise ValueError("policy is invalid")
    return data


def _policy_bytes(policy: DelegationPolicy) -> bytes:
    return canonical_bytes(_policy_data(policy))


def _validate_policy_data(
    data: dict[str, Any], *, configuration: ApprovalHostConfiguration
) -> None:
    _require_exact_keys(
        data,
        {
            "schema_version",
            "policy_id",
            "policy_version",
            "source",
            "grantor_ref",
            "source_authorization_ref",
            "configuration_host_ref",
            "configuration_interaction_ref",
            "authorized_agent_or_host_ref",
            "account_stable_ids",
            "allowed_operations",
            "maximum_change_per_operation",
            "cumulative_change_bounds",
            "effective_from",
            "expires_at",
            "allowed_egress_hosts",
            "revoked_at",
        },
    )
    if type(data["schema_version"]) is not int or data["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("policy schema is invalid")
    if type(data["policy_version"]) is not int or data["policy_version"] < 1:
        raise ValueError("policy version is invalid")
    if data.get("source") != "USER_CONTROLLED_CONFIGURATION":
        raise ValueError("policy source is invalid")
    if not _safe_text(_mapping_string(data, "policy_id"), maximum_bytes=160):
        raise ValueError("policy identifier is invalid")
    for key in (
        "grantor_ref",
        "source_authorization_ref",
        "configuration_host_ref",
        "configuration_interaction_ref",
        "authorized_agent_or_host_ref",
    ):
        if not _safe_text(_mapping_string(data, key), maximum_bytes=256):
            raise ValueError("policy reference is invalid")
    if data["configuration_host_ref"] != configuration.host_ref:
        raise ValueError("policy host binding is invalid")
    account_ids = _bounded_string_list(
        data.get("account_stable_ids"), maximum=configuration.maximum_policy_items, string_bytes=256
    )
    if len(account_ids) != len(set(account_ids)):
        raise ValueError("policy account bindings are invalid")
    operations = _bounded_string_list(
        data.get("allowed_operations"), maximum=len(_POLICY_OPERATIONS), string_bytes=32
    )
    if any(item not in _POLICY_OPERATIONS for item in operations) or len(operations) != len(
        set(operations)
    ):
        raise ValueError("policy operations are invalid")
    _validate_money_limits(
        data.get("maximum_change_per_operation"), maximum=configuration.maximum_policy_items
    )
    _validate_money_limits(
        data.get("cumulative_change_bounds"), maximum=configuration.maximum_policy_items
    )
    effective_from = _parse_challenge_timestamp(_mapping_string(data, "effective_from"))
    expires_at = _parse_challenge_timestamp(_mapping_string(data, "expires_at"))
    if effective_from >= expires_at:
        raise ValueError("policy timing is invalid")
    hosts = _bounded_string_list(
        data.get("allowed_egress_hosts"),
        maximum=configuration.maximum_policy_items,
        string_bytes=253,
    )
    if len(hosts) != len(set(hosts)) or any(_HOST.fullmatch(host) is None for host in hosts):
        raise ValueError("policy egress hosts are invalid")
    revoked_at = data.get("revoked_at")
    if revoked_at is not None:
        if type(revoked_at) is not str:
            raise ValueError("policy revocation is invalid")
        _parse_challenge_timestamp(revoked_at)


def _validate_money_limits(value: object, *, maximum: int) -> None:
    if type(value) is not list or not 1 <= len(value) <= maximum:
        raise ValueError("policy money bounds are invalid")
    currencies: list[str] = []
    for item in value:
        if type(item) is not dict:
            raise ValueError("policy money bounds are invalid")
        _require_exact_keys(item, {"maximum_minor_units", "currency"})
        maximum_minor_units = item["maximum_minor_units"]
        currency = item["currency"]
        if (
            type(maximum_minor_units) is not int
            or not 0 <= maximum_minor_units <= _SAFE_INTEGER
            or type(currency) is not str
            or _CURRENCY.fullmatch(currency) is None
        ):
            raise ValueError("policy money bounds are invalid")
        currencies.append(currency)
    if len(currencies) != len(set(currencies)):
        raise ValueError("policy money bounds are invalid")


def _pause_review_data(action: OpenAIPausePlannedAction) -> dict[str, Any]:
    binding = action.binding
    campaign = binding.campaign
    subplan = action.plan.subplans[0]
    workspace = action.workspace_binding
    return {
        "account": {
            "account_client_ref": binding.account_client_ref,
            "account_currency": binding.account_currency,
            "account_ref": binding.account_ref,
            "account_status": binding.account_status,
            "account_timezone": binding.account_timezone,
            "credential_binding_status": "BOUND_NOT_DISPLAYED",
            "profile_ref": binding.profile_ref,
        },
        "campaign": {
            "bidding_type": campaign.bidding_type,
            "campaign_client_ref": campaign.campaign_client_ref,
            "campaign_ref": campaign.campaign_ref,
            "end_at": _optional_action_timestamp(campaign.end_at),
            "lifetime_spend_limit_micros": campaign.lifetime_spend_limit_micros,
            "start_at": _timestamp_from_datetime(campaign.start_at),
            "status_before": campaign.status,
        },
        "fixture_status": "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED",
        "plan": {
            "capability_version": subplan.capability_version,
            "egress": [
                {"host": target.host, "purpose": target.purpose} for target in subplan.egress
            ],
            "expires_at": _timestamp_from_datetime(action.plan.expires_at),
            "field_changes": [
                {
                    "after_state_digest": _sha256(
                        canonical_bytes(change.after.model_dump(mode="json"))
                    ),
                    "before_state_digest": _sha256(
                        canonical_bytes(change.before.model_dump(mode="json"))
                    ),
                    "path": change.path,
                }
                for change in subplan.field_changes
            ],
            "maximum_incremental_spend": {
                "currency": subplan.max_spend.currency,
                "minor_units": subplan.max_spend.minor_units,
            },
            "plan_digest": action.plan.plan_digest,
            "plan_id": action.plan.plan_id,
            "profile_ref": action.plan.profile_ref,
            "provider_api_version": subplan.provider_api_version,
            "recovery_actions": [item.value for item in subplan.recovery_actions],
            "subplan_digest": subplan.subplan_digest,
            "subplan_id": subplan.subplan_id,
        },
        "residual_delivery_or_billing": "NOT_PROVEN_STOPPED",
        "transition": {
            "operation": "PAUSE",
            "status_after": "paused",
            "status_before": "active",
        },
        "workspace_binding": (
            None
            if workspace is None
            else {
                "binding_kind": workspace.binding_kind,
                "workspace_profile_ref": workspace.workspace_profile_ref,
            }
        ),
    }


def _ga4_key_event_create_review_data(action: Ga4KeyEventCreatePlannedAction) -> dict[str, Any]:
    """Return the closed, non-secret GA4 facts a native reviewer can assess.

    The full local action remains in the challenge and is signed through its
    digest.  A reviewer needs the configured profile, native property and
    stream IDs, and the fixed safe event-name/counting change to assess the
    request.  This surface deliberately does not repeat conversion
    source/prose, a token, raw OAuth subject, request body, or native request
    path.
    """

    selection = action.selection_binding
    observation = action.observation_binding
    execution = action.execution_binding
    native_request = action.native_request_binding
    state = action.semantic_state
    subplan = action.plan.subplans[0]
    candidate_request = action.candidate.compilation.compiler_candidate.request
    return {
        "action_digest": _sha256(_ga4_action_bytes(action)),
        "fixture_status": action.status,
        "source_bindings": {
            "candidate_record_digest": selection.candidate_record_digest,
            "contract_record_digest": selection.contract_record_digest,
            "selection_checkpoint_digest": selection.checkpoint_digest,
            "observation_record_digest": observation.observation_record_digest,
            "observation_checkpoint_digest": observation.checkpoint_digest,
            "inventory_digest": observation.inventory_digest,
            "configuration_digest": execution.configuration_digest,
            "profile_configuration_digest": execution.profile_configuration_digest,
        },
        "oauth_association": {
            "provider": execution.provider,
            "purpose": execution.purpose,
            "credential_source": execution.credential_source,
            "scopes": list(execution.scopes),
            "local_association_status": execution.local_association_status,
            "provider_scope_status": execution.provider_scope_status,
            "provider_create_permission": execution.provider_create_permission,
            "profile_ref": execution.auth_profile_ref,
        },
        "target": {
            "target_digest": _sha256(canonical_bytes(execution.target.model_dump(mode="json"))),
            "admin_api_version": execution.target.admin_api_version,
            "expected_currency": execution.target.expected_currency,
            "expected_timezone": execution.target.expected_timezone,
            "property_client_ref": execution.target.property_client_ref,
            "required_scope": execution.target.required_scope,
            "web_stream_client_ref": execution.target.web_stream_client_ref,
        },
        "semantic_transition": {
            "counting_method": candidate_request.countingMethod,
            "default_value": "NONE",
            "event_name": candidate_request.eventName,
            "before_member_count": state.before_member_count,
            "after_member_count": state.after_member_count,
            "before_state_digest": state.before.fingerprint,
            "after_state_digest": state.after.fingerprint,
            "inventory_scope": state.preconditions.inventory_scope,
            "keyed_content_integrity": state.preconditions.inventory_content_integrity,
            "duplicate_status": state.preconditions.duplicate_status,
            "capacity_status": state.preconditions.capacity_status,
            "documented_key_event_cap": state.preconditions.documented_key_event_cap,
            "remaining_documented_slots": state.preconditions.remaining_documented_slots,
            "current_provider_state": state.preconditions.current_provider_state,
            "reservation_status": state.preconditions.reservation_status,
            "permission_status": state.preconditions.permission_status,
        },
        "native_request": {
            "host": native_request.host,
            "method": native_request.method,
            "api_version": native_request.api_version,
            "target_digest": native_request.target_digest,
            "body_digest": native_request.body_digest,
            "body": "BOUND_NOT_DISPLAYED",
        },
        "plan": {
            "plan_digest": action.plan.plan_digest,
            "subplan_digest": subplan.subplan_digest,
            "operation": subplan.operation.value,
            "provider": subplan.provider,
            "maximum_incremental_spend": {
                "currency": subplan.max_spend.currency,
                "minor_units": subplan.max_spend.minor_units,
            },
            "egress": [
                {"host": target.host, "purpose": target.purpose} for target in subplan.egress
            ],
            "recovery_actions": [item.value for item in subplan.recovery_actions],
            "recovery_disposition": subplan.recovery_disposition,
            "expires_at": _timestamp_from_datetime(action.plan.expires_at),
        },
        "known_limits": {
            "current_state_status": action.current_state_status,
            "configuration_readback": action.configuration_readback,
            "provider_create_permission": action.provider_create_permission,
            "semantic_source_mapping": action.semantic_source_mapping,
            "browser_network_verification": action.browser_network_verification,
            "realtime_verification": action.realtime_verification,
            "processed_report_verification": action.processed_report_verification,
            "native_conversion_mapping": action.native_conversion_mapping,
            "execution_authority": action.execution_authority,
        },
        "workspace_binding": {
            "binding_kind": action.workspace_binding.binding_kind,
            "workspace_profile_ref": action.workspace_binding.workspace_profile_ref,
        },
    }


def _meta_campaign_pause_review_data(action: MetaCampaignPausePlannedAction) -> dict[str, Any]:
    """Return the closed safe M40 facts a local native reviewer can assess.

    The signed challenge contains the complete action.  This review omits the
    credential-binding reference, derived principal, configuration digests,
    and any provider payload.  It also does not claim current provider access,
    a successful pause, no residual delivery, conditional writes, or idempotency.
    """

    workspace = action.workspace_binding
    account = action.account_binding
    execution = action.execution_binding
    observation = action.observation_binding
    campaign = observation.campaign
    postcondition = action.postcondition
    subplan = action.plan.subplans[0]
    return {
        "action_digest": _sha256(_meta_pause_action_bytes(action)),
        "fixture_status": action.status,
        "workspace_binding": {
            "binding_kind": workspace.binding_kind,
            "workspace_profile_ref": workspace.workspace_profile_ref,
        },
        "local_association": {
            "profile_ref": execution.profile_ref,
            "client_application_ref": execution.client_application_ref,
            "required_scope": execution.required_scope,
            "credential_binding_status": "BOUND_NOT_DISPLAYED",
            "local_association_status": execution.local_association_status,
            "provider_write_permission": execution.provider_write_permission,
        },
        "account": {
            "account_ref": account.account_ref,
            "account_client_ref": account.account_client_ref,
            "currency": account.currency,
            "timezone": account.timezone,
            "account_status": account.account_status,
            "provider_api_version": account.provider_api_version,
        },
        "campaign_baseline": {
            "campaign_ref": campaign.campaign_ref,
            "campaign_client_ref": campaign.campaign_client_ref,
            "account_client_ref": campaign.account_client_ref,
            "configured_status": campaign.configured_status,
            "effective_status": campaign.effective_status,
        },
        "observation": {
            "read_started_at": _timestamp_from_datetime(observation.read_started_at),
            "read_finished_at": _timestamp_from_datetime(observation.read_finished_at),
            "consistency": observation.consistency,
        },
        "transition": {
            "operation": subplan.operation.value,
            "configured_status_before": campaign.configured_status,
            "configured_status_after": postcondition.configured_status,
            "postcondition_kind": postcondition.postcondition_kind,
            "effective_delivery": postcondition.effective_delivery,
        },
        "plan": {
            "plan_id": action.plan.plan_id,
            "plan_digest": action.plan.plan_digest,
            "profile_ref": action.plan.profile_ref,
            "subplan_id": subplan.subplan_id,
            "subplan_digest": subplan.subplan_digest,
            "provider": subplan.provider,
            "provider_api_version": subplan.provider_api_version,
            "capability_version": subplan.capability_version,
            "maximum_incremental_spend": {
                "currency": subplan.max_spend.currency,
                "minor_units": subplan.max_spend.minor_units,
            },
            "egress": [
                {"host": target.host, "purpose": target.purpose} for target in subplan.egress
            ],
            "recovery_actions": [item.value for item in subplan.recovery_actions],
            "recovery_semantics": action.recovery_semantics,
            "expires_at": _timestamp_from_datetime(action.plan.expires_at),
        },
        "known_limits": {
            "current_state_status": action.current_state_status,
            "configuration_readback": action.configuration_readback,
            "conditional_write_proof": action.conditional_write_proof,
            "idempotency_proof": action.idempotency_proof,
            "execution_authority": action.execution_authority,
            "live_validated": action.live_validated,
        },
    }


def _policy_review_data(policy: DelegationPolicy) -> dict[str, Any]:
    return {
        "account_stable_ids": list(policy.account_stable_ids),
        "allowed_egress_hosts": list(policy.allowed_egress_hosts),
        "allowed_operations": [item.value for item in policy.allowed_operations],
        "authorized_agent_or_host_ref": policy.authorized_agent_or_host_ref,
        "configuration_host_ref": policy.configuration_host_ref,
        "configuration_interaction_ref": policy.configuration_interaction_ref,
        "cumulative_change_bounds": [
            {"currency": limit.currency, "maximum_minor_units": limit.maximum_minor_units}
            for limit in policy.cumulative_change_bounds
        ],
        "effective_from": _timestamp_from_datetime(policy.effective_from),
        "expires_at": _timestamp_from_datetime(policy.expires_at),
        "grantor_ref": policy.grantor_ref,
        "maximum_change_per_operation": [
            {"currency": limit.currency, "maximum_minor_units": limit.maximum_minor_units}
            for limit in policy.maximum_change_per_operation
        ],
        "policy_digest": _sha256(_policy_bytes(policy)),
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "source_authorization_ref": policy.source_authorization_ref,
    }


def _unsigned_receipt_data(
    challenge: ApprovalChallenge,
    *,
    configuration: ApprovalHostConfiguration,
    receipt_id: str,
    issued_at_text: str,
    review_digest: str,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "authentication_mode": "DEVICE_OWNER_AUTHENTICATION",
        "challenge_digest": challenge.digest,
        "challenge_nonce": _base64url_encode(challenge.nonce),
        "expires_at": challenge.expires_at_text,
        "host_ref": configuration.host_ref,
        "issued_at": issued_at_text,
        "key_ref": configuration.signing_key_ref,
        "receipt_id": receipt_id,
        "receipt_kind": challenge.kind.value,
        "review_digest": review_digest,
        "schema_version": _SCHEMA_VERSION,
        "signature_algorithm": "ECDSA_P256_SHA256_X9_62",
    }
    subject = challenge.subject
    if isinstance(subject, OpenAIPauseApprovalSubject):
        pause_action = subject.action
        data.update(
            {
                "grantor_ref": subject.grantor_ref,
                "action_digest": _sha256(_action_bytes(pause_action)),
                "plan_digest": pause_action.plan.plan_digest,
                "subplan_digests": list(pause_action.plan.subplan_digests),
            }
        )
    elif isinstance(subject, Ga4KeyEventCreateApprovalSubject):
        ga4_action = subject.action
        data.update(
            {
                "grantor_ref": subject.grantor_ref,
                "action_digest": _sha256(_ga4_action_bytes(ga4_action)),
                "plan_digest": ga4_action.plan.plan_digest,
                "subplan_digests": list(ga4_action.plan.subplan_digests),
            }
        )
    elif isinstance(subject, MetaCampaignPauseApprovalSubject):
        meta_pause_action = subject.action
        data.update(
            {
                "grantor_ref": subject.grantor_ref,
                "action_digest": _sha256(_meta_pause_action_bytes(meta_pause_action)),
                "plan_digest": meta_pause_action.plan.plan_digest,
                "subplan_digests": list(meta_pause_action.plan.subplan_digests),
            }
        )
    elif isinstance(subject, CompositeExecutionApprovalSubject):
        # The receipt field names are unchanged: ``plan_digest`` and
        # ``subplan_digests`` are exactly what the resulting direct grant binds.
        data.update(
            {
                "grantor_ref": subject.grantor_ref,
                "action_digest": _sha256(_composite_subject_bytes(subject)),
                "plan_digest": subject.plan_digest,
                "subplan_digests": list(subject.subplan_digests),
            }
        )
    else:
        policy = subject.policy
        data.update(
            {
                "grantor_ref": policy.grantor_ref,
                "policy_digest": _sha256(_policy_bytes(policy)),
                "policy_id": policy.policy_id,
                "policy_version": policy.policy_version,
            }
        )
        if isinstance(subject, DelegationPolicyRevokeSubject):
            data["revoked_at"] = subject.revoked_at_text
    return data


def _validate_receipt_shape(data: dict[str, Any]) -> None:
    if type(data.get("schema_version")) is not int or data["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("receipt schema is invalid")
    raw_kind = data.get("receipt_kind")
    if type(raw_kind) is not str:
        raise ValueError("receipt kind is invalid")
    try:
        kind = ApprovalChallengeKind(raw_kind)
    except ValueError as exc:
        raise ValueError("receipt kind is invalid") from exc
    common = {
        "schema_version",
        "authentication_mode",
        "challenge_digest",
        "challenge_nonce",
        "expires_at",
        "host_ref",
        "issued_at",
        "key_ref",
        "receipt_id",
        "receipt_kind",
        "review_digest",
        "signature_algorithm",
        "signature",
        "grantor_ref",
    }
    expected = set(common)
    if kind in _PLAN_BOUND_KINDS:
        expected.update({"action_digest", "plan_digest", "subplan_digests"})
    else:
        expected.update({"policy_digest", "policy_id", "policy_version"})
        if kind is ApprovalChallengeKind.DELEGATION_POLICY_REVOKE:
            expected.add("revoked_at")
    _require_exact_keys(data, expected)
    if data["authentication_mode"] != "DEVICE_OWNER_AUTHENTICATION":
        raise ValueError("receipt authentication mode is invalid")
    if data["signature_algorithm"] != "ECDSA_P256_SHA256_X9_62":
        raise ValueError("receipt algorithm is invalid")
    for key in ("challenge_digest", "review_digest"):
        value = data[key]
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise ValueError("receipt digest is invalid")
    _decode_nonce(data.get("challenge_nonce"))
    for key in ("host_ref", "key_ref", "grantor_ref"):
        if not _safe_text(_mapping_string(data, key), maximum_bytes=256):
            raise ValueError("receipt reference is invalid")
    if not _RECEIPT_ID.fullmatch(_mapping_string(data, "receipt_id")):
        raise ValueError("receipt identifier is invalid")
    _parse_receipt_timestamp(_mapping_string(data, "issued_at"))
    _parse_challenge_timestamp(_mapping_string(data, "expires_at"))
    signature = _mapping_string(data, "signature")
    _decode_signature(signature)
    if kind in _PLAN_BOUND_KINDS:
        for key in ("action_digest", "plan_digest"):
            value = data[key]
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                raise ValueError("receipt action binding is invalid")
        subplan_digests = data["subplan_digests"]
        if type(subplan_digests) is not list or not 1 <= len(subplan_digests) <= 6:
            raise ValueError("receipt subplan binding is invalid")
        if any(
            type(value) is not str or _DIGEST.fullmatch(value) is None for value in subplan_digests
        ):
            raise ValueError("receipt subplan binding is invalid")
        if len(subplan_digests) != len(set(subplan_digests)):
            raise ValueError("receipt subplan binding is invalid")
    else:
        if (
            type(data["policy_digest"]) is not str
            or _DIGEST.fullmatch(data["policy_digest"]) is None
        ):
            raise ValueError("receipt policy binding is invalid")
        if not _safe_text(_mapping_string(data, "policy_id"), maximum_bytes=160):
            raise ValueError("receipt policy binding is invalid")
        if type(data["policy_version"]) is not int or data["policy_version"] < 1:
            raise ValueError("receipt policy binding is invalid")
        if kind is ApprovalChallengeKind.DELEGATION_POLICY_REVOKE:
            _parse_challenge_timestamp(_mapping_string(data, "revoked_at"))


def _pinned_public_key(value: bytes) -> tuple[ec.EllipticCurvePublicKey, str]:
    if type(value) is not bytes or len(value) != 65 or value[0] != 4:
        raise _pin_error()
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), value)
        if (
            key.public_bytes(
                encoding=serialization.Encoding.X962,
                format=serialization.PublicFormat.UncompressedPoint,
            )
            != value
        ):
            raise ValueError("pin did not round trip")
    except Exception:
        raise _pin_error() from None
    return key, _sha256(value)


def _verify_signature(
    public_key: ec.EllipticCurvePublicKey, signature: bytes, payload: bytes
) -> None:
    try:
        r, s = utils.decode_dss_signature(signature)
        if utils.encode_dss_signature(r, s) != signature:
            raise ValueError("signature DER is not canonical")
        public_key.verify(signature, payload, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        raise _signature_error() from None


def _decode_nonce(value: object) -> bytes:
    if type(value) is not str or _B64URL.fullmatch(value) is None:
        raise ValueError("nonce is invalid")
    try:
        decoded = _base64url_decode(value)
    except ValueError:
        raise ValueError("nonce is invalid") from None
    if len(decoded) != _NONCE_BYTES:
        raise ValueError("nonce is invalid")
    return decoded


def _decode_signature(value: str) -> bytes:
    if not 8 <= len(value.encode("utf-8")) <= 128 or _B64URL.fullmatch(value) is None:
        raise _signature_error()
    try:
        return _base64url_decode(value)
    except ValueError:
        raise _signature_error() from None


def _base64url_decode(value: str) -> bytes:
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except Exception:
        raise ValueError("base64url is invalid") from None
    if _base64url_encode(decoded) != value:
        raise ValueError("base64url is noncanonical")
    return decoded


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _nonce_text(value: bytes | None) -> str:
    if value is None:
        return _base64url_encode(secrets.token_bytes(_NONCE_BYTES))
    if type(value) is not bytes or len(value) != _NONCE_BYTES:
        raise _challenge_error()
    return _base64url_encode(value)


def _challenge_time_text(issued_at: datetime | None, now: datetime | None) -> str:
    if issued_at is not None:
        return _timestamp_from_datetime(issued_at)
    return _timestamp_from_datetime(_checked_now(now))


def _checked_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock is invalid")
    return value.astimezone(UTC)


def _timestamp_from_datetime(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp is invalid")
    normalized = value.astimezone(UTC)
    prefix = (
        f"{normalized.year:04d}-{normalized.month:02d}-{normalized.day:02d}"
        f"T{normalized.hour:02d}:{normalized.minute:02d}:{normalized.second:02d}"
    )
    return (
        f"{prefix}Z" if normalized.microsecond == 0 else f"{prefix}.{normalized.microsecond:06d}Z"
    )


def _receipt_timestamp_from_datetime(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("receipt timestamp is invalid")
    normalized = value.astimezone(UTC)
    if normalized.microsecond % 1_000 != 0:
        raise ValueError("receipt timestamp requires milliseconds")
    return (
        f"{normalized.year:04d}-{normalized.month:02d}-{normalized.day:02d}"
        f"T{normalized.hour:02d}:{normalized.minute:02d}:{normalized.second:02d}."
        f"{normalized.microsecond // 1_000:03d}Z"
    )


def _optional_action_timestamp(value: datetime | None) -> str | None:
    return None if value is None else _timestamp_from_datetime(value)


def _parse_challenge_timestamp(value: str) -> datetime:
    return _parse_timestamp(value, pattern=_TIMESTAMP, fraction_name="microsecond")


def _parse_receipt_timestamp(value: str) -> datetime:
    return _parse_timestamp(value, pattern=_RECEIPT_TIMESTAMP, fraction_name="millisecond")


def _parse_timestamp(value: str, *, pattern: re.Pattern[str], fraction_name: str) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp is invalid")
    match = pattern.fullmatch(value)
    if match is None:
        raise ValueError("timestamp is invalid")
    try:
        fraction = match.group(fraction_name)
        microsecond = (
            0
            if fraction is None
            else int(fraction) * (1_000 if fraction_name == "millisecond" else 1)
        )
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            microsecond,
            tzinfo=UTC,
        )
    except (TypeError, ValueError):
        raise ValueError("timestamp is invalid") from None


def _wire_object(
    raw: bytes,
    *,
    maximum_bytes: int,
    maximum_depth: int,
    maximum_items: int,
    maximum_string_bytes: int,
) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > maximum_bytes:
        raise ValueError("wire payload is invalid")
    parsed = parse_json(raw, maximum_bytes=maximum_bytes)
    _validate_json_value(
        parsed,
        depth=0,
        maximum_depth=maximum_depth,
        maximum_items=maximum_items,
        maximum_string_bytes=maximum_string_bytes,
    )
    if type(parsed) is not dict or canonical_bytes(parsed) != raw:
        raise ValueError("wire payload is noncanonical")
    return parsed


def _validate_json_value(
    value: object,
    *,
    depth: int,
    maximum_depth: int,
    maximum_items: int,
    maximum_string_bytes: int,
) -> None:
    if depth > maximum_depth:
        raise ValueError("JSON nesting is too deep")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not -_SAFE_INTEGER <= value <= _SAFE_INTEGER:
            raise ValueError("JSON integer is outside the safe domain")
        return
    if type(value) is float:
        raise ValueError("JSON decimal is not accepted")
    if type(value) is str:
        if len(value.encode("utf-8")) > maximum_string_bytes:
            raise ValueError("JSON string is too large")
        return
    if type(value) is list:
        if len(value) > maximum_items:
            raise ValueError("JSON array has too many values")
        for item in value:
            _validate_json_value(
                item,
                depth=depth + 1,
                maximum_depth=maximum_depth,
                maximum_items=maximum_items,
                maximum_string_bytes=maximum_string_bytes,
            )
        return
    if type(value) is dict:
        if len(value) > maximum_items:
            raise ValueError("JSON object has too many values")
        for key, item in value.items():
            if type(key) is not str or len(key.encode("utf-8")) > maximum_string_bytes:
                raise ValueError("JSON object key is invalid")
            _validate_json_value(
                item,
                depth=depth + 1,
                maximum_depth=maximum_depth,
                maximum_items=maximum_items,
                maximum_string_bytes=maximum_string_bytes,
            )
        return
    raise ValueError("JSON value is invalid")


def _require_exact_keys(data: dict[str, Any], expected: set[str]) -> None:
    if set(data) != expected or len(data) != len(expected):
        raise ValueError("closed object fields are invalid")


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if type(value) is not str:
        raise ValueError("required string is invalid")
    return value


def _mapping_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if type(value) is not str:
        raise ValueError("required string is invalid")
    return value


def _bounded_string_list(value: object, *, maximum: int, string_bytes: int) -> list[str]:
    if type(value) is not list or not 1 <= len(value) <= maximum:
        raise ValueError("bounded string list is invalid")
    if any(
        type(item) is not str or not _safe_text(item, maximum_bytes=string_bytes) for item in value
    ):
        raise ValueError("bounded string list is invalid")
    return value


def _safe_text(value: object, *, maximum_bytes: int) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value.encode("utf-8")) <= maximum_bytes
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _require_exact_bounded_int(value: object, *, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Approval host configuration limit is invalid.")


def _checked_configuration(value: object) -> ApprovalHostConfiguration:
    if type(value) is not ApprovalHostConfiguration:
        raise _challenge_error()
    try:
        return ApprovalHostConfiguration(
            host_ref=value.host_ref,
            signing_key_ref=value.signing_key_ref,
            maximum_challenge_bytes=value.maximum_challenge_bytes,
            maximum_challenge_lifetime_seconds=value.maximum_challenge_lifetime_seconds,
            maximum_issued_at_skew_seconds=value.maximum_issued_at_skew_seconds,
            maximum_subplans=value.maximum_subplans,
            maximum_policy_items=value.maximum_policy_items,
        )
    except Exception:
        raise _challenge_error() from None


def _seconds(value: int) -> timedelta:
    return timedelta(seconds=value)


def _receipt_domain_bytes(kind: ApprovalChallengeKind, unsigned: bytes) -> bytes:
    return _RECEIPT_DOMAIN + kind.value.encode("utf-8") + b"\0" + unsigned


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _challenge_error() -> AdhelmError:
    return AdhelmError(
        "INVALID_APPROVAL_CHALLENGE",
        "Approval challenge violates the closed local protocol.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _challenge_not_yet_valid_error() -> AdhelmError:
    return AdhelmError(
        "APPROVAL_CHALLENGE_NOT_YET_VALID",
        "Approval challenge is not yet valid at the supplied clock.",
        exit_code=ExitCode.PRECONDITION,
    )


def _challenge_expired_error() -> AdhelmError:
    return AdhelmError(
        "APPROVAL_CHALLENGE_EXPIRED",
        "Approval challenge has expired at the supplied clock.",
        exit_code=ExitCode.PRECONDITION,
    )


def _receipt_error() -> AdhelmError:
    return AdhelmError(
        "INVALID_APPROVAL_RECEIPT",
        "Approval receipt violates the closed local protocol.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _receipt_expired_error() -> AdhelmError:
    return AdhelmError(
        "APPROVAL_RECEIPT_EXPIRED",
        "Approval receipt cannot be accepted after challenge expiry.",
        exit_code=ExitCode.PRECONDITION,
    )


def _receipt_issue_error() -> AdhelmError:
    return AdhelmError(
        "INVALID_APPROVAL_RECEIPT_TIME",
        "Approval receipt issue time is outside the accepted window.",
        exit_code=ExitCode.PRECONDITION,
    )


def _receipt_binding_error() -> AdhelmError:
    return AdhelmError(
        "APPROVAL_RECEIPT_BINDING_MISMATCH",
        "Approval receipt does not bind the expected local challenge.",
        exit_code=ExitCode.PRECONDITION,
    )


def _signature_error() -> AdhelmError:
    return AdhelmError(
        "INVALID_APPROVAL_RECEIPT_SIGNATURE",
        "Approval receipt signature is invalid.",
        exit_code=ExitCode.PRECONDITION,
    )


def _pin_error() -> AdhelmError:
    return AdhelmError(
        "INVALID_APPROVAL_PUBLIC_KEY_PIN",
        "Approval receipt verifier requires a valid independently supplied public-key pin.",
        exit_code=ExitCode.PRECONDITION,
    )
