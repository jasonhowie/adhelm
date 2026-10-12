"""Private fixed Meta token inspection, without account access or write authority.

Graph requires the inspected token in a query parameter. This narrow boundary
does not relax the generic reporting transport's prohibition on secret data.
No configuration, OS-store, OAuth enrollment or CLI route invokes it here.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import httpx

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError

_ENDPOINT = "https://graph.facebook.com/v26.0/debug_token"
_MAX_BYTES = 65_536
_ID = re.compile(r"[1-9][0-9]{0,39}\Z", re.ASCII)
_SCOPE = re.compile(r"[a-z][a-z0-9_]{0,99}\Z", re.ASCII)
_PRINCIPAL_DOMAIN = b"adhelm/meta/token-principal/v1\x00"
_CREDENTIAL_DOMAIN = b"adhelm/meta/inspected-credential/v1\x00"
_PUBLIC_CODES = frozenset(
    {
        "META_TOKEN_INSPECTION_UNVERIFIED",
        "CLOCK_INVALID",
        "META_TOKEN_EXPIRY_UNSUPPORTED",
        "META_TOKEN_EXPIRED",
        "META_TOKEN_APPLICATION_MISMATCH",
        "META_TOKEN_TYPE_UNSUPPORTED",
        "META_TOKEN_SCOPE_REQUIRED",
        "META_TOKEN_OBSERVATION_EXPIRED",
        "META_TOKEN_CREDENTIAL_CHANGED",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class MetaTokenInspectionCredentials:
    """Private in-memory values; not a configured credential lifecycle."""

    profile_ref: str
    subject_token: str
    inspector_token: str
    binding_key: bytes


@dataclass(frozen=True, slots=True, repr=False)
class MetaTokenObservation:
    """One bounded observation, never a grant or proof of account access."""

    client_application_ref: str
    principal_ref: str
    profile_ref: str
    credential_binding_ref: str
    scopes: tuple[str, ...]
    observed_at: datetime
    expires_at: datetime
    data_access_expires_at: datetime


# One refusal needs its own words.  Meta issues long-lived system-user tokens
# that report no expiry at all, and a token whose expiry semantics this
# protocol has never reviewed is refused for that reason, not because identity
# or permissions failed a check.  The refusal still fails closed and still
# carries no provider text.
_UNSUPPORTED_EXPIRY_MESSAGE: Final = (
    "Meta reported an expiry this reviewed inspection protocol does not interpret."
)
_UNSUPPORTED_EXPIRY_NEXT_ACTION: Final = (
    "A zero or unbounded expiry -- as a long-lived system-user token reports -- is undocumented "
    "for this protocol rather than invalid, and is refused rather than guessed at. Use a "
    "customer user token with a documented expiry, or wait for that semantics to be reviewed."
)


def _failure(code: str = "META_TOKEN_INSPECTION_UNVERIFIED") -> AdhelmError:
    if code == "META_TOKEN_EXPIRY_UNSUPPORTED":
        return AdhelmError(
            code,
            _UNSUPPORTED_EXPIRY_MESSAGE,
            exit_code=4,
            next_action=_UNSUPPORTED_EXPIRY_NEXT_ACTION,
        )
    return AdhelmError(
        code,
        "Meta token identity and permissions could not be verified.",
        exit_code=4,
        next_action="Review the configured Meta app and credential locally before a new attempt.",
    )


def _monotonic() -> float:
    return time.monotonic()


def _require_deadline(started: float) -> None:
    elapsed = _monotonic() - started
    if not 0 <= elapsed < 30.0:
        raise _failure("META_TOKEN_OBSERVATION_EXPIRED")


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise _failure("CLOCK_INVALID")
    return value.astimezone(UTC)


def _credentials(value: object) -> MetaTokenInspectionCredentials:
    if type(value) is not MetaTokenInspectionCredentials:
        raise _failure()
    if (
        type(value.profile_ref) is not str
        or not 1 <= len(value.profile_ref) <= 160
        or any(not 33 <= ord(c) <= 126 for c in value.profile_ref)
        or type(value.binding_key) is not bytes
        or len(value.binding_key) != 32
    ):
        raise _failure()
    for token in (value.subject_token, value.inspector_token):
        if type(token) is not str or not 1 <= len(token) <= 16_384:
            raise _failure()
        if any(not 33 <= ord(c) <= 126 for c in token):
            raise _failure()
    return value


def _credential_digest(value: MetaTokenInspectionCredentials) -> str:
    payload = (
        _CREDENTIAL_DOMAIN
        + value.profile_ref.encode("ascii")
        + b"\x00"
        + value.subject_token.encode("ascii")
    )
    return (
        "credential-binding:sha256:"
        + hmac.new(value.binding_key, payload, hashlib.sha256).hexdigest()
    )


def _unix_expiry(value: object, now: datetime) -> datetime:
    # This initial protocol supports expiring user tokens only. Zero/unbounded
    # expiry is explicitly unsupported until its provider semantics are reviewed.
    if type(value) is not int or not 0 < value <= 4_102_444_800:
        raise _failure("META_TOKEN_EXPIRY_UNSUPPORTED")
    expiry = datetime.fromtimestamp(value, UTC)
    if expiry <= now:
        raise _failure("META_TOKEN_EXPIRED")
    return expiry


def _observation(
    raw: bytes,
    *,
    credentials: MetaTokenInspectionCredentials,
    expected_client: str,
    required_scope: str,
    now: datetime,
) -> MetaTokenObservation:
    value = parse_json(raw, maximum_bytes=_MAX_BYTES)
    if type(value) is not dict or set(value) != {"data"}:
        raise _failure()
    data = value["data"]
    allowed = {
        "app_id",
        "application",
        "is_valid",
        "issued_at",
        "expires_at",
        "data_access_expires_at",
        "user_id",
        "scopes",
        "type",
        "granular_scopes",
    }
    if type(data) is not dict or not set(data) <= allowed or data.get("is_valid") is not True:
        raise _failure()
    app, user = data.get("app_id"), data.get("user_id")
    if type(app) is not str or _ID.fullmatch(app) is None or app != expected_client:
        raise _failure("META_TOKEN_APPLICATION_MISMATCH")
    if type(user) is not str or _ID.fullmatch(user) is None:
        raise _failure()
    if type(data.get("type")) is not str or data["type"] != "USER":
        raise _failure("META_TOKEN_TYPE_UNSUPPORTED")
    if "application" in data and (
        type(data["application"]) is not str or len(data["application"]) > 512
    ):
        raise _failure()
    if "issued_at" in data and (
        type(data["issued_at"]) is not int or not 0 <= data["issued_at"] <= int(now.timestamp())
    ):
        raise _failure()
    scopes = data.get("scopes")
    if (
        type(scopes) is not list
        or not 1 <= len(scopes) <= 100
        or any(type(s) is not str or _SCOPE.fullmatch(s) is None for s in scopes)
        or len(set(scopes)) != len(scopes)
        or required_scope not in scopes
    ):
        raise _failure("META_TOKEN_SCOPE_REQUIRED")
    if "granular_scopes" in data:
        granular = data["granular_scopes"]
        if type(granular) is not list or len(granular) > 100:
            raise _failure()
        for item in granular:
            if type(item) is not dict or set(item) != {"scope", "target_ids"}:
                raise _failure()
            if item["scope"] not in scopes or type(item["target_ids"]) is not list:
                raise _failure()
            targets = item["target_ids"]
            if len(targets) > 500 or any(
                type(t) is not str or _ID.fullmatch(t) is None for t in targets
            ):
                raise _failure()
    principal = hashlib.sha256(
        _PRINCIPAL_DOMAIN + app.encode() + b"\x00" + user.encode()
    ).hexdigest()
    return MetaTokenObservation(
        client_application_ref=app,
        principal_ref="meta_token_principal_v1." + principal,
        profile_ref=credentials.profile_ref,
        credential_binding_ref=_credential_digest(credentials),
        scopes=tuple(sorted(scopes)),
        observed_at=now,
        expires_at=_unix_expiry(data.get("expires_at"), now),
        data_access_expires_at=_unix_expiry(data.get("data_access_expires_at"), now),
    )


def inspect_meta_token(
    *,
    credential_supplier: Callable[[], MetaTokenInspectionCredentials],
    expected_client_application_ref: str,
    required_scope: str,
) -> MetaTokenObservation:
    """Perform one fixed private GET; retain no HTTP response or token-bearing URL.

    No token retry, app-secret exchange, permissions widening or account request
    is implied. A future configured workflow must independently own the supplier
    and reobserve identity immediately before an exact guarded mutation.
    """
    try:
        if (
            type(expected_client_application_ref) is not str
            or _ID.fullmatch(expected_client_application_ref) is None
            or type(required_scope) is not str
            or required_scope not in {"ads_read", "ads_management"}
        ):
            raise _failure()
        captured = _credentials(credential_supplier())
        started = _time(_now())
        started_monotonic = _monotonic()
        with httpx.Client(trust_env=False) as client:
            _require_deadline(started_monotonic)
            with client.stream(
                "GET",
                _ENDPOINT,
                headers={"authorization": "Bearer " + captured.inspector_token},
                params={"input_token": captured.subject_token},
                follow_redirects=False,
                timeout=30.0,
            ) as response:
                if response.status_code != 200:
                    raise _failure()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    _require_deadline(started_monotonic)
                    if len(raw) + len(chunk) > _MAX_BYTES:
                        raise _failure()
                    raw.extend(chunk)
        _require_deadline(started_monotonic)
        current = _credentials(credential_supplier())
        if current.profile_ref != captured.profile_ref or any(
            not hmac.compare_digest(a, b)
            for a, b in (
                (current.subject_token.encode("ascii"), captured.subject_token.encode("ascii")),
                (current.inspector_token.encode("ascii"), captured.inspector_token.encode("ascii")),
                (current.binding_key, captured.binding_key),
            )
        ):
            raise _failure("META_TOKEN_CREDENTIAL_CHANGED")
        _require_deadline(started_monotonic)
        observed_at = _time(_now())
        if observed_at < started or (observed_at - started).total_seconds() > 30:
            raise _failure("META_TOKEN_OBSERVATION_EXPIRED")
        observation = _observation(
            bytes(raw),
            credentials=captured,
            expected_client=expected_client_application_ref,
            required_scope=required_scope,
            now=observed_at,
        )
        _require_deadline(started_monotonic)
        return observation
    except AdhelmError as error:
        # Never propagate parser details, provider text, request URLs or causes.
        code = (
            error.code
            if type(error.code) is str and error.code in _PUBLIC_CODES
            else "META_TOKEN_INSPECTION_UNVERIFIED"
        )
        raise _failure(code) from None
    except Exception:
        raise _failure() from None
