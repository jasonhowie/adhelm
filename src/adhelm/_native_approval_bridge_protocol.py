"""Closed, private parity checks for the future macOS approval bridge.

This module only validates bounded canonical request and candidate bytes.  It
cannot launch a helper, resolve an installed identity, create
``PinnedApprovalTrust``, write a ledger record, issue a grant, or execute a
provider operation.  Its metadata digest is a pure caller-supplied expected
fact until a later reviewed installed binding supplies it.
"""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import UTC, datetime
from typing import Any, Final, SupportsIndex

from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalChallengeKind,
    ApprovalHostConfiguration,
    decode_approval_challenge,
    parse_approval_challenge_wire,
    verify_approval_receipt,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError, ExitCode

_REQUEST_MAXIMUM_BYTES: Final = 98_304
_CANDIDATE_MAXIMUM_BYTES: Final = 131_072
_CHALLENGE_MAXIMUM_BYTES: Final = 65_536
_RECEIPT_MAXIMUM_BYTES: Final = 16_384
_REQUEST_IDENTIFIER_BYTES: Final = 32
_REQUEST_MESSAGE_TYPE: Final = "NATIVE_APPROVAL_BRIDGE_REQUEST_V1"
_CANDIDATE_MESSAGE_TYPE: Final = "NATIVE_APPROVAL_BRIDGE_CANDIDATE_V1"
_POST_CONSUMPTION_STATUS: Final = "NATIVE_CURRENT_RECEIPT_VERIFIED_AND_NONCE_CONSUMED"
_SOURCE_CONTEXT_SCOPE: Final = "ADHELM_NATIVE_APPROVAL_BRIDGE_SOURCE_CONTEXT_V1"
_SOURCE_STREAM: Final = "changes"
_SCHEMA_VERSION: Final = 1
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_SIX_FRACTION_UTC = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"\.(?P<microsecond>[0-9]{6})Z$"
)
#: V1 bridge bytes carry only these two kinds.  ADR 0012's provider-neutral
#: ``COMPOSITE_EXECUTION_PLAN`` and ``ATTEMPT_RECOVERY`` kinds are deliberately
#: absent: this codec must keep refusing them until a reviewed installed host
#: build renders them, exactly as it refuses the Meta V2 kind.
_DIRECT_KINDS: Final = frozenset(
    {
        ApprovalChallengeKind.OPENAI_PAUSE_PLAN,
        ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN,
    }
)
_ISSUER = object()

_REQUEST_FIELDS: Final = frozenset(
    {
        "schema_version",
        "message_type",
        "request_id",
        "source_context_digest",
        "metadata_digest",
        "protocol_configuration_digest",
        "canonical_challenge_b64url",
    }
)
_CANDIDATE_FIELDS: Final = frozenset(
    {
        "schema_version",
        "message_type",
        "native_observation_status",
        "request_id_digest",
        "request_digest",
        "source_context_digest",
        "metadata_digest",
        "protocol_configuration_digest",
        "canonical_challenge_b64url",
        "challenge_digest",
        "challenge_nonce_digest",
        "receipt_kind",
        "expires_at",
        "canonical_receipt_b64url",
        "receipt_digest",
        "receipt_id",
        "active_public_key_x963_b64url",
        "active_public_key_fingerprint",
        "native_observed_at",
    }
)


class NativeApprovalBridgeRequestV1:
    """Opaque private request facts with no host, trust, or grant capability."""

    __slots__ = (
        "__canonical_bytes",
        "__digest",
        "__request_id",
        "__source_context_digest",
        "__metadata_digest",
        "__protocol_configuration_digest",
        "__canonical_challenge",
        "__challenge",
    )

    def __init__(
        self,
        issuer: object,
        *,
        canonical_request: bytes,
        request_id: bytes,
        source_context_digest: str,
        metadata_digest: str,
        protocol_configuration_digest: str,
        canonical_challenge: bytes,
        challenge: ApprovalChallenge,
    ) -> None:
        if issuer is not _ISSUER:
            raise TypeError("Native bridge requests are issued only by this module.")
        self.__canonical_bytes = canonical_request
        self.__digest = _sha256(canonical_request)
        self.__request_id = request_id
        self.__source_context_digest = source_context_digest
        self.__metadata_digest = metadata_digest
        self.__protocol_configuration_digest = protocol_configuration_digest
        self.__canonical_challenge = canonical_challenge
        self.__challenge = challenge

    @property
    def canonical_bytes(self) -> bytes:
        """Return canonical request bytes for the private one-frame transport."""
        return self.__canonical_bytes

    @property
    def digest(self) -> str:
        """Return the SHA-256 digest of the canonical request frame."""
        return self.__digest

    @property
    def source_context_digest(self) -> str:
        """Return opaque Python-local source context evidence."""
        return self.__source_context_digest

    @property
    def metadata_digest(self) -> str:
        """Return the expected full installed-metadata digest evidence."""
        return self.__metadata_digest

    @property
    def protocol_configuration_digest(self) -> str:
        """Return the expected seven-field configuration digest evidence."""
        return self.__protocol_configuration_digest

    @property
    def kind(self) -> ApprovalChallengeKind:
        """Return the checked direct-plan kind without granting authority."""
        return self.__challenge.kind

    def __repr__(self) -> str:
        return "<NativeApprovalBridgeRequestV1 private non-authorizing evidence>"

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("Native bridge request evidence is not serializable.")

    def __reduce_ex__(self, protocol: SupportsIndex) -> str | tuple[Any, ...]:
        del protocol
        raise TypeError("Native bridge request evidence is not serializable.")


class NativeApprovalBridgeCandidateV1:
    """Opaque private candidate facts, not a trust value or execution permit."""

    __slots__ = (
        "__canonical_bytes",
        "__canonical_challenge",
        "__canonical_receipt",
        "__active_public_key_x963",
        "__request_digest",
        "__source_context_digest",
        "__metadata_digest",
        "__protocol_configuration_digest",
        "__receipt_digest",
        "__receipt_id",
        "__kind",
        "__expires_at",
        "__native_observed_at",
    )

    def __init__(
        self,
        issuer: object,
        *,
        canonical_candidate: bytes,
        canonical_challenge: bytes,
        canonical_receipt: bytes,
        active_public_key_x963: bytes,
        request_digest: str,
        source_context_digest: str,
        metadata_digest: str,
        protocol_configuration_digest: str,
        receipt_digest: str,
        receipt_id: str,
        kind: ApprovalChallengeKind,
        expires_at: datetime,
        native_observed_at: datetime,
    ) -> None:
        if issuer is not _ISSUER:
            raise TypeError("Native bridge candidates are issued only by this module.")
        self.__canonical_bytes = canonical_candidate
        self.__canonical_challenge = canonical_challenge
        self.__canonical_receipt = canonical_receipt
        self.__active_public_key_x963 = active_public_key_x963
        self.__request_digest = request_digest
        self.__source_context_digest = source_context_digest
        self.__metadata_digest = metadata_digest
        self.__protocol_configuration_digest = protocol_configuration_digest
        self.__receipt_digest = receipt_digest
        self.__receipt_id = receipt_id
        self.__kind = kind
        self.__expires_at = expires_at
        self.__native_observed_at = native_observed_at

    @property
    def request_digest(self) -> str:
        """Return the checked request digest as private bridge evidence."""
        return self.__request_digest

    @property
    def receipt_digest(self) -> str:
        """Return the checked receipt digest as private bridge evidence."""
        return self.__receipt_digest

    @property
    def receipt_id(self) -> str:
        """Return the checked receipt identifier as private bridge evidence."""
        return self.__receipt_id

    @property
    def kind(self) -> ApprovalChallengeKind:
        """Return the checked direct-plan kind without granting authority."""
        return self.__kind

    @property
    def expires_at(self) -> datetime:
        """Return the receipt expiry bound by the candidate."""
        return self.__expires_at

    @property
    def native_observed_at(self) -> datetime:
        """Return non-authorizing native observation evidence."""
        return self.__native_observed_at

    def __repr__(self) -> str:
        return "<NativeApprovalBridgeCandidateV1 private non-authorizing evidence>"

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("Native bridge candidate evidence is not serializable.")

    def __reduce_ex__(self, protocol: SupportsIndex) -> str | tuple[Any, ...]:
        del protocol
        raise TypeError("Native bridge candidate evidence is not serializable.")


def derive_native_approval_bridge_source_context_digest_v1(
    *,
    workspace_profile_ref: str,
    source_plan_record_digest: str,
    checkpoint_digest: str,
) -> str:
    """Derive the opaque, domain-separated source-context digest.

    The raw workspace and record facts stay on the Python side.  This helper
    creates no trust, grant, persistence, or provider-facing value.
    """
    try:
        if not _safe_workspace_profile_ref(workspace_profile_ref):
            raise ValueError("workspace profile is invalid")
        _require_digest(source_plan_record_digest)
        _require_digest(checkpoint_digest)
        return _sha256(
            canonical_bytes(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "scope": _SOURCE_CONTEXT_SCOPE,
                    "workspace_profile_ref": workspace_profile_ref,
                    "source_stream": _SOURCE_STREAM,
                    "source_plan_record_digest": source_plan_record_digest,
                    "checkpoint_digest": checkpoint_digest,
                }
            )
        )
    except Exception:
        raise _invalid_protocol() from None


def build_native_approval_bridge_request_v1(
    expected_challenge: ApprovalChallenge,
    *,
    configuration: ApprovalHostConfiguration,
    request_id: bytes,
    source_context_digest: str,
    expected_metadata_digest: str,
) -> NativeApprovalBridgeRequestV1:
    """Build one bounded canonical private request from checked local facts."""
    try:
        checked_configuration = _checked_configuration(configuration)
        checked_challenge = _checked_direct_challenge(expected_challenge, checked_configuration)
        if type(request_id) is not bytes or len(request_id) != _REQUEST_IDENTIFIER_BYTES:
            raise ValueError("request identifier is invalid")
        _require_digest(source_context_digest)
        _require_digest(expected_metadata_digest)
        raw = canonical_bytes(
            {
                "schema_version": _SCHEMA_VERSION,
                "message_type": _REQUEST_MESSAGE_TYPE,
                "request_id": _encode_base64url(request_id),
                "source_context_digest": source_context_digest,
                "metadata_digest": expected_metadata_digest,
                "protocol_configuration_digest": checked_configuration.configuration_digest,
                "canonical_challenge_b64url": _encode_base64url(checked_challenge.canonical_bytes),
            }
        )
        return decode_native_approval_bridge_request_v1(
            raw,
            configuration=checked_configuration,
            expected_metadata_digest=expected_metadata_digest,
        )
    except AdhelmError as exc:
        if exc.code.startswith("NATIVE_APPROVAL_BRIDGE_"):
            raise
        raise _invalid_protocol() from None
    except Exception:
        raise _invalid_protocol() from None


def decode_native_approval_bridge_request_v1(
    raw: bytes,
    *,
    configuration: ApprovalHostConfiguration,
    expected_metadata_digest: str,
) -> NativeApprovalBridgeRequestV1:
    """Decode an exact canonical request without resolving installed trust."""
    try:
        checked_configuration = _checked_configuration(configuration)
        _require_digest(expected_metadata_digest)
        data = _canonical_object(raw, maximum_bytes=_REQUEST_MAXIMUM_BYTES)
        if set(data) != _REQUEST_FIELDS:
            raise ValueError("request fields are invalid")
        if _exact_integer(data["schema_version"]) != _SCHEMA_VERSION:
            raise ValueError("request schema is invalid")
        if _exact_string(data["message_type"]) != _REQUEST_MESSAGE_TYPE:
            raise ValueError("request message type is invalid")
        request_id = _decode_base64url(
            data["request_id"],
            maximum_bytes=_REQUEST_IDENTIFIER_BYTES,
            exact_bytes=_REQUEST_IDENTIFIER_BYTES,
        )
        source_context_digest = _exact_string(data["source_context_digest"])
        metadata_digest = _exact_string(data["metadata_digest"])
        protocol_digest = _exact_string(data["protocol_configuration_digest"])
        _require_digest(source_context_digest)
        _require_digest(metadata_digest)
        _require_digest(protocol_digest)
        if (
            metadata_digest != expected_metadata_digest
            or protocol_digest != checked_configuration.configuration_digest
        ):
            raise _binding_error()
        canonical_challenge = _decode_base64url(
            data["canonical_challenge_b64url"], maximum_bytes=_CHALLENGE_MAXIMUM_BYTES
        )
        challenge = _decode_direct_challenge(canonical_challenge, checked_configuration)
        return NativeApprovalBridgeRequestV1(
            _ISSUER,
            canonical_request=raw,
            request_id=request_id,
            source_context_digest=source_context_digest,
            metadata_digest=metadata_digest,
            protocol_configuration_digest=protocol_digest,
            canonical_challenge=canonical_challenge,
            challenge=challenge,
        )
    except AdhelmError as exc:
        if exc.code.startswith("NATIVE_APPROVAL_BRIDGE_"):
            raise
        raise _invalid_protocol() from None
    except Exception:
        raise _invalid_protocol() from None


def revalidate_native_approval_bridge_request_v1_at_exchange(
    value: object,
    *,
    configuration: ApprovalHostConfiguration,
    expected_metadata_digest: str,
    exchange_started_at: datetime,
) -> NativeApprovalBridgeRequestV1:
    """Rebuild a request and enforce its direct challenge window at exchange start.

    This remains a pure, private wire check.  It returns no raw challenge bytes,
    trust pin, receipt, grant, or execution capability.
    """
    try:
        checked_configuration = _checked_configuration(configuration)
        _require_digest(expected_metadata_digest)
        checked_request = _request_from_instance(
            value,
            configuration=checked_configuration,
            expected_metadata_digest=expected_metadata_digest,
        )
        exchange_started = _trusted_exchange_time(exchange_started_at)
        _decode_direct_challenge_at_exchange(
            _request_challenge_bytes(checked_request),
            checked_configuration,
            exchange_started,
        )
        return checked_request
    except AdhelmError as exc:
        if exc.code.startswith("NATIVE_APPROVAL_BRIDGE_"):
            raise
        raise _invalid_protocol() from None
    except Exception:
        raise _invalid_protocol() from None


def validate_native_approval_bridge_candidate_v1(
    raw: bytes,
    *,
    request: NativeApprovalBridgeRequestV1,
    configuration: ApprovalHostConfiguration,
    expected_metadata_digest: str,
    exchange_started_at: datetime,
    current_now: datetime,
) -> NativeApprovalBridgeCandidateV1:
    """Return checked private candidate facts without accepting authorization.

    The signature check proves only self-consistency with the candidate's
    returned X9.63 key.  This module does not resolve an installed pin or claim
    that the key is currently trusted.
    """
    try:
        checked_configuration = _checked_configuration(configuration)
        _require_digest(expected_metadata_digest)
        checked_request = _request_from_instance(
            request,
            configuration=checked_configuration,
            expected_metadata_digest=expected_metadata_digest,
        )
        exchange_started = _trusted_exchange_time(exchange_started_at)
        now = _trusted_exchange_time(current_now)
        if now < exchange_started:
            raise _time_error()
        challenge = _decode_direct_challenge_at_exchange(
            _request_challenge_bytes(checked_request),
            checked_configuration,
            exchange_started,
        )
        data = _canonical_object(raw, maximum_bytes=_CANDIDATE_MAXIMUM_BYTES)
        if set(data) != _CANDIDATE_FIELDS:
            raise ValueError("candidate fields are invalid")
        if _exact_integer(data["schema_version"]) != _SCHEMA_VERSION:
            raise ValueError("candidate schema is invalid")
        if _exact_string(data["message_type"]) != _CANDIDATE_MESSAGE_TYPE:
            raise ValueError("candidate message type is invalid")
        if _exact_string(data["native_observation_status"]) != _POST_CONSUMPTION_STATUS:
            raise _binding_error()

        request_id_digest = _exact_string(data["request_id_digest"])
        request_digest = _exact_string(data["request_digest"])
        source_context_digest = _exact_string(data["source_context_digest"])
        metadata_digest = _exact_string(data["metadata_digest"])
        protocol_digest = _exact_string(data["protocol_configuration_digest"])
        challenge_digest = _exact_string(data["challenge_digest"])
        challenge_nonce_digest = _exact_string(data["challenge_nonce_digest"])
        receipt_digest = _exact_string(data["receipt_digest"])
        active_key_fingerprint = _exact_string(data["active_public_key_fingerprint"])
        for value in (
            request_id_digest,
            request_digest,
            source_context_digest,
            metadata_digest,
            protocol_digest,
            challenge_digest,
            challenge_nonce_digest,
            receipt_digest,
            active_key_fingerprint,
        ):
            _require_digest(value)

        canonical_challenge = _decode_base64url(
            data["canonical_challenge_b64url"], maximum_bytes=_CHALLENGE_MAXIMUM_BYTES
        )
        if canonical_challenge != _request_challenge_bytes(checked_request):
            raise _binding_error()
        if canonical_challenge != challenge.canonical_bytes:
            raise _binding_error()
        if (
            request_id_digest != _sha256(_request_identifier_bytes(checked_request))
            or request_digest != checked_request.digest
            or source_context_digest != checked_request.source_context_digest
            or metadata_digest != expected_metadata_digest
            or protocol_digest != checked_configuration.configuration_digest
            or challenge_digest != challenge.digest
            or challenge_nonce_digest != _sha256(challenge.nonce)
        ):
            raise _binding_error()

        receipt_kind = _exact_string(data["receipt_kind"])
        expires_at_text = _exact_string(data["expires_at"])
        receipt_id = _exact_string(data["receipt_id"])
        if (
            receipt_kind != challenge.kind.value
            or expires_at_text != challenge.expires_at_text
            or not _is_receipt_identifier(receipt_id)
        ):
            raise _binding_error()
        canonical_receipt = _decode_base64url(
            data["canonical_receipt_b64url"], maximum_bytes=_RECEIPT_MAXIMUM_BYTES
        )
        if _sha256(canonical_receipt) != receipt_digest:
            raise _binding_error()
        active_public_key_x963 = _decode_base64url(
            data["active_public_key_x963_b64url"], maximum_bytes=65, exact_bytes=65
        )
        if (
            active_public_key_x963[0] != 4
            or _sha256(active_public_key_x963) != active_key_fingerprint
        ):
            raise _binding_error()

        native_observed_at = _parse_six_fraction_utc(data["native_observed_at"])
        # Python retains its own live expiry check.  Native observation is
        # point-in-time evidence, rather than a replacement for this clock
        # boundary.
        if now >= challenge.expires_at or not (
            exchange_started <= native_observed_at <= now
            and native_observed_at < challenge.expires_at
        ):
            raise _time_error()

        # This is intentionally not an installed-key assertion.  It verifies
        # the returned receipt against the returned key solely to detect a
        # malformed or internally inconsistent candidate wire record.
        verified = verify_approval_receipt(
            canonical_receipt,
            expected_challenge=challenge,
            configuration=checked_configuration,
            trusted_public_key_x963=active_public_key_x963,
            now=now,
        )
        if (
            verified.digest != receipt_digest
            or verified.receipt_id != receipt_id
            or verified.kind != challenge.kind
            or verified.challenge_digest != challenge.digest
            or verified.expires_at_text != challenge.expires_at_text
            or verified.pinned_public_key_fingerprint != active_key_fingerprint
        ):
            raise _binding_error()
        if verified.issued_at > native_observed_at:
            raise _time_error()
        return NativeApprovalBridgeCandidateV1(
            _ISSUER,
            canonical_candidate=raw,
            canonical_challenge=canonical_challenge,
            canonical_receipt=canonical_receipt,
            active_public_key_x963=active_public_key_x963,
            request_digest=request_digest,
            source_context_digest=source_context_digest,
            metadata_digest=metadata_digest,
            protocol_configuration_digest=protocol_digest,
            receipt_digest=receipt_digest,
            receipt_id=receipt_id,
            kind=verified.kind,
            expires_at=verified.expires_at,
            native_observed_at=native_observed_at,
        )
    except AdhelmError as exc:
        if exc.code.startswith("NATIVE_APPROVAL_BRIDGE_"):
            raise
        raise _invalid_protocol() from None
    except Exception:
        raise _invalid_protocol() from None


def revalidate_native_approval_bridge_candidate_v1(
    value: object,
    *,
    request: NativeApprovalBridgeRequestV1,
    configuration: ApprovalHostConfiguration,
    expected_metadata_digest: str,
    exchange_started_at: datetime,
    current_now: datetime,
) -> NativeApprovalBridgeCandidateV1:
    """Reparse opaque candidate bytes before any future private consumer use."""
    try:
        if type(value) is not NativeApprovalBridgeCandidateV1:
            raise ValueError("candidate type is invalid")
        raw = object.__getattribute__(value, "_NativeApprovalBridgeCandidateV1__canonical_bytes")
        if type(raw) is not bytes:
            raise ValueError("candidate bytes are invalid")
    except Exception:
        raise _invalid_protocol() from None
    return validate_native_approval_bridge_candidate_v1(
        raw,
        request=request,
        configuration=configuration,
        expected_metadata_digest=expected_metadata_digest,
        exchange_started_at=exchange_started_at,
        current_now=current_now,
    )


def _canonical_object(raw: object, *, maximum_bytes: int) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > maximum_bytes:
        raise ValueError("wire bytes are invalid")
    data = parse_json(raw)
    if type(data) is not dict or canonical_bytes(data) != raw:
        raise ValueError("wire is not canonical")
    return data


def _checked_configuration(value: object) -> ApprovalHostConfiguration:
    try:
        if type(value) is not ApprovalHostConfiguration:
            raise ValueError("configuration type is invalid")
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
        raise _invalid_protocol() from None


def _checked_direct_challenge(
    value: object, configuration: ApprovalHostConfiguration
) -> ApprovalChallenge:
    try:
        if type(value) is not ApprovalChallenge:
            raise ValueError("challenge type is invalid")
        raw = value.canonical_bytes
        if type(raw) is not bytes:
            raise ValueError("challenge bytes are invalid")
        return _decode_direct_challenge(raw, configuration)
    except AdhelmError:
        raise
    except Exception:
        raise _invalid_protocol() from None


def _decode_direct_challenge(
    raw: bytes, configuration: ApprovalHostConfiguration
) -> ApprovalChallenge:
    try:
        if type(raw) is not bytes or not raw or len(raw) > _CHALLENGE_MAXIMUM_BYTES:
            raise ValueError("challenge bytes are invalid")
        challenge = parse_approval_challenge_wire(raw, configuration=configuration)
        if challenge.kind not in _DIRECT_KINDS:
            raise ValueError("challenge kind is not supported")
        if challenge.canonical_bytes != raw or challenge.digest != _sha256(raw):
            raise ValueError("challenge is not canonical")
        return challenge
    except AdhelmError:
        raise _invalid_protocol() from None
    except Exception:
        raise _invalid_protocol() from None


def _decode_direct_challenge_at_exchange(
    raw: bytes, configuration: ApprovalHostConfiguration, exchange_started_at: datetime
) -> ApprovalChallenge:
    try:
        challenge = decode_approval_challenge(
            raw, configuration=configuration, now=exchange_started_at
        )
        if challenge.kind not in _DIRECT_KINDS or challenge.canonical_bytes != raw:
            raise ValueError("challenge binding is invalid")
        return challenge
    except Exception:
        raise _time_error() from None


def _request_from_instance(
    value: object,
    *,
    configuration: ApprovalHostConfiguration,
    expected_metadata_digest: str,
) -> NativeApprovalBridgeRequestV1:
    try:
        if type(value) is not NativeApprovalBridgeRequestV1:
            raise ValueError("request type is invalid")
        raw = object.__getattribute__(value, "_NativeApprovalBridgeRequestV1__canonical_bytes")
        if type(raw) is not bytes:
            raise ValueError("request bytes are invalid")
    except Exception:
        raise _invalid_protocol() from None
    return decode_native_approval_bridge_request_v1(
        raw,
        configuration=configuration,
        expected_metadata_digest=expected_metadata_digest,
    )


def _request_identifier_bytes(value: NativeApprovalBridgeRequestV1) -> bytes:
    try:
        raw = object.__getattribute__(value, "_NativeApprovalBridgeRequestV1__request_id")
        if type(raw) is not bytes or len(raw) != _REQUEST_IDENTIFIER_BYTES:
            raise ValueError("request identifier is invalid")
        return raw
    except Exception:
        raise _invalid_protocol() from None


def _request_challenge_bytes(value: NativeApprovalBridgeRequestV1) -> bytes:
    try:
        raw = object.__getattribute__(value, "_NativeApprovalBridgeRequestV1__canonical_challenge")
        if type(raw) is not bytes:
            raise ValueError("challenge bytes are invalid")
        return raw
    except Exception:
        raise _invalid_protocol() from None


def _decode_base64url(
    value: object, *, maximum_bytes: int, exact_bytes: int | None = None
) -> bytes:
    if type(value) is not str or _BASE64URL.fullmatch(value) is None:
        raise ValueError("base64url is invalid")
    try:
        result = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except Exception:
        raise ValueError("base64url is invalid") from None
    if (
        not result
        or len(result) > maximum_bytes
        or (exact_bytes is not None and len(result) != exact_bytes)
        or _encode_base64url(result) != value
    ):
        raise ValueError("base64url is invalid")
    return result


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _exact_string(value: object) -> str:
    if type(value) is not str:
        raise ValueError("string is invalid")
    return value


def _exact_integer(value: object) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ValueError("integer is invalid")
    return value


def _require_digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("digest is invalid")
    return value


def _is_receipt_identifier(value: str) -> bool:
    if not value.startswith("approval-receipt:"):
        return False
    suffix = value.removeprefix("approval-receipt:")
    return len(suffix) == 43 and _BASE64URL.fullmatch(suffix) is not None


def _parse_six_fraction_utc(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp is invalid")
    match = _SIX_FRACTION_UTC.fullmatch(value)
    if match is None:
        raise ValueError("timestamp is invalid")
    try:
        return datetime(
            int(match["year"]),
            int(match["month"]),
            int(match["day"]),
            int(match["hour"]),
            int(match["minute"]),
            int(match["second"]),
            int(match["microsecond"]),
            tzinfo=UTC,
        )
    except ValueError:
        raise ValueError("timestamp is invalid") from None


def _trusted_exchange_time(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise _time_error()
    return value.astimezone(UTC)


def _safe_workspace_profile_ref(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value.encode("utf-8")) <= 160
        and "/" not in value
        and "\\" not in value
        and all(ord(character) >= 32 for character in value)
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _invalid_protocol() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_PROTOCOL_INVALID",
        "Native approval bridge evidence violates the closed local protocol.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _binding_error() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_BINDING_INVALID",
        "Native approval bridge evidence does not bind the expected local request.",
        exit_code=ExitCode.PRECONDITION,
    )


def _time_error() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_TIME_INVALID",
        "Native approval bridge evidence is outside the checked local time window.",
        exit_code=ExitCode.PRECONDITION,
    )
