"""Internal, one-snapshot Google OAuth binding for a future GA4 Admin EDIT plan.

This module has no provider request, credential-store call, configuration read,
plan, authorization, or generic serialization surface of its own. Its resolver
delegates all protected-store reads and possible fixed refresh transport to the
credential lifecycle, then receives one already validated private snapshot and
keeps its bearer in a private, single-use in-process registry.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from threading import Lock
from typing import Final, Literal, Never
from weakref import WeakKeyDictionary

from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_oauth_credentials import (
    _checked_google_oauth_execution_snapshot,
    _checked_now,
    _resolve_google_oauth_execution_snapshot,
)
from adhelm.google_oauth_protocol import GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE
from adhelm.models import Profile, validate_configured_profile

_PROVIDER: Final = "ga4_admin"
_PURPOSE: Final = "EDIT"
_SCOPE: Final = "https://www.googleapis.com/auth/analytics.edit"
_SOURCE: Final = "OS_OAUTH_GOOGLE"
_BINDING_ISSUER = object()
_BINDINGS_LOCK = Lock()


class Ga4AdminEditOAuthExecutionBinding:
    """A nonserializable, single-use local handoff for one future typed plan.

    The object exposes only nonsecret facts.  Its bearer stays in a private
    registry and is released only by :func:`bearer_for_ga4_admin_edit` after
    exact profile revalidation.  The issuer and registry are in-process misuse
    checks; they are not a defense against arbitrary code with the same Python
    process access and do not establish authority or Google CREATE permission.
    """

    _issuer: object
    profile_ref: str
    profile_digest: str
    provider: Literal["ga4_admin"]
    purpose: Literal["EDIT"]
    credential_source: Literal["OS_OAUTH_GOOGLE"]
    account_ref: str
    principal_ref: str
    client_ref: str
    api_scope: Literal["https://www.googleapis.com/auth/analytics.edit"]

    __slots__ = (
        "_issuer",
        "profile_ref",
        "profile_digest",
        "provider",
        "purpose",
        "credential_source",
        "account_ref",
        "principal_ref",
        "client_ref",
        "api_scope",
        "__weakref__",
    )

    def __init__(
        self,
        *,
        _issuer: object,
        profile_ref: str,
        profile_digest: str,
        provider: Literal["ga4_admin"],
        purpose: Literal["EDIT"],
        credential_source: Literal["OS_OAUTH_GOOGLE"],
        account_ref: str,
        principal_ref: str,
        client_ref: str,
        api_scope: Literal["https://www.googleapis.com/auth/analytics.edit"],
    ) -> None:
        if _issuer is not _BINDING_ISSUER:
            raise TypeError("Google OAuth execution bindings are lifecycle-issued only")
        object.__setattr__(self, "_issuer", _issuer)
        object.__setattr__(self, "profile_ref", profile_ref)
        object.__setattr__(self, "profile_digest", profile_digest)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "credential_source", credential_source)
        object.__setattr__(self, "account_ref", account_ref)
        object.__setattr__(self, "principal_ref", principal_ref)
        object.__setattr__(self, "client_ref", client_ref)
        object.__setattr__(self, "api_scope", api_scope)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Google OAuth execution bindings are immutable")

    def __repr__(self) -> str:
        return "<Ga4AdminEditOAuthExecutionBinding redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google OAuth execution bindings cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google OAuth execution bindings cannot be serialized")

    def __copy__(self) -> Never:
        raise TypeError("Google OAuth execution bindings cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Never:
        del memo
        raise TypeError("Google OAuth execution bindings cannot be copied")


class _BindingRecord:
    """Private, nonserializable bearer record held only by this module."""

    _bearer_token: str
    profile_ref: str
    profile_digest: str
    account_ref: str
    principal_ref: str
    client_ref: str
    _access_token_expires_at: datetime
    _resolved_at: datetime

    __slots__ = (
        "_bearer_token",
        "profile_ref",
        "profile_digest",
        "account_ref",
        "principal_ref",
        "client_ref",
        "_access_token_expires_at",
        "_resolved_at",
    )

    def __init__(
        self,
        *,
        _bearer_token: str,
        profile_ref: str,
        profile_digest: str,
        account_ref: str,
        principal_ref: str,
        client_ref: str,
        _access_token_expires_at: datetime,
        _resolved_at: datetime,
    ) -> None:
        object.__setattr__(self, "_bearer_token", _bearer_token)
        object.__setattr__(self, "profile_ref", profile_ref)
        object.__setattr__(self, "profile_digest", profile_digest)
        object.__setattr__(self, "account_ref", account_ref)
        object.__setattr__(self, "principal_ref", principal_ref)
        object.__setattr__(self, "client_ref", client_ref)
        object.__setattr__(self, "_access_token_expires_at", _access_token_expires_at)
        object.__setattr__(self, "_resolved_at", _resolved_at)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Google OAuth execution records are immutable")

    def __repr__(self) -> str:
        return "<_BindingRecord redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google OAuth execution records cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google OAuth execution records cannot be serialized")


_BINDINGS: WeakKeyDictionary[Ga4AdminEditOAuthExecutionBinding, _BindingRecord] = (
    WeakKeyDictionary()
)


def resolve_ga4_admin_edit_execution_binding(
    profile: Profile,
) -> Ga4AdminEditOAuthExecutionBinding:
    """Resolve exactly one sealed GA4 Admin EDIT bearer/binding pair.

    This validates the detached profile before the lifecycle can touch its
    protected bundle.  The lifecycle then owns one credential-reference lock
    for bundle decoding, exact binding checks, and any necessary refresh.
    This proves local protected-grant association only; it does not prove a
    current provider permission, reservation, remote state, or authorization.
    """

    checked_profile = _checked_edit_profile(profile)
    snapshot = _checked_google_oauth_execution_snapshot(
        _resolve_google_oauth_execution_snapshot(checked_profile), checked_profile
    )
    binding_now = _checked_now()
    if binding_now < snapshot._resolved_at.astimezone(
        UTC
    ) or binding_now >= snapshot._access_token_expires_at.astimezone(UTC):
        raise _expired_binding()
    binding = Ga4AdminEditOAuthExecutionBinding(
        _issuer=_BINDING_ISSUER,
        profile_ref=checked_profile.profile_id,
        profile_digest=snapshot.profile_digest,
        provider=_PROVIDER,
        purpose=_PURPOSE,
        credential_source=_SOURCE,
        account_ref=snapshot.account_ref,
        principal_ref=snapshot.principal_ref,
        client_ref=snapshot.client_ref,
        api_scope=_SCOPE,
    )
    record = _BindingRecord(
        _bearer_token=snapshot._bearer_token,
        profile_ref=checked_profile.profile_id,
        profile_digest=snapshot.profile_digest,
        account_ref=snapshot.account_ref,
        principal_ref=snapshot.principal_ref,
        client_ref=snapshot.client_ref,
        _access_token_expires_at=snapshot._access_token_expires_at,
        _resolved_at=binding_now,
    )
    with _BINDINGS_LOCK:
        _BINDINGS[binding] = record
    return binding


def bearer_for_ga4_admin_edit(binding: Ga4AdminEditOAuthExecutionBinding, profile: Profile) -> str:
    """Consume the one local bearer handoff after exact profile comparison.

    A future plan builder calls the resolver itself, then consumes its returned
    binding exactly once to construct fixed GA4 Admin request headers.  It must
    never accept a caller-created binding as authorization.
    """

    checked_profile = _checked_edit_profile(profile)
    if type(binding) is not Ga4AdminEditOAuthExecutionBinding:
        raise _invalid_binding()
    with _BINDINGS_LOCK:
        record = _BINDINGS.get(binding)
        if record is None or not _binding_matches(binding, record, checked_profile):
            raise _invalid_binding()
        try:
            now = _checked_now()
            expires_at = record._access_token_expires_at
            resolved_at = record._resolved_at
            if (
                type(expires_at) is not datetime
                or expires_at.tzinfo is None
                or expires_at.utcoffset() is None
                or type(resolved_at) is not datetime
                or resolved_at.tzinfo is None
                or resolved_at.utcoffset() is None
                or now < resolved_at.astimezone(UTC)
                or now >= expires_at.astimezone(UTC)
            ):
                raise _expired_binding()
        except AdhelmError:
            _BINDINGS.pop(binding, None)
            raise
        except Exception:
            _BINDINGS.pop(binding, None)
            raise _invalid_binding() from None
        del _BINDINGS[binding]
        return record._bearer_token


def _checked_edit_profile(value: object) -> Profile:
    """Reject constructed, static, CI, READ, and widened profiles pre-store."""

    if type(value) is not Profile:
        raise _profile_required()
    try:
        checked = validate_configured_profile(value)
    except Exception:
        raise _profile_required() from None
    if (
        type(checked) is not Profile
        or checked.credential_source != _SOURCE
        or checked.provider != _PROVIDER
        or checked.purpose != _PURPOSE
        or checked.scopes != [GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[(_PROVIDER, _PURPOSE)]]
    ):
        raise _profile_required()
    return checked


def _binding_matches(
    binding: Ga4AdminEditOAuthExecutionBinding,
    record: _BindingRecord,
    profile: Profile,
) -> bool:
    """Reject mutated, mismatched, or re-used bindings without exposing values."""

    try:
        expected = (
            (binding.profile_ref, record.profile_ref),
            (binding.profile_ref, profile.profile_id),
            (binding.profile_digest, record.profile_digest),
            (binding.profile_digest, _profile_digest(profile)),
            (binding.provider, _PROVIDER),
            (binding.purpose, _PURPOSE),
            (binding.credential_source, _SOURCE),
            (binding.account_ref, record.account_ref),
            (binding.account_ref, profile.account_ref),
            (binding.principal_ref, record.principal_ref),
            (binding.principal_ref, profile.principal_ref),
            (binding.client_ref, record.client_ref),
            (binding.client_ref, profile.client_ref),
            (binding.api_scope, _SCOPE),
        )
        return binding._issuer is _BINDING_ISSUER and all(
            type(actual) is str
            and type(expected_value) is str
            and hmac.compare_digest(actual, expected_value)
            for actual, expected_value in expected
        )
    except Exception:
        return False


def _profile_digest(profile: Profile) -> str:
    """Use lifecycle's canonical profile digest without a public serializer."""

    from adhelm.canonical import digest

    return digest(profile)


def _profile_required() -> AdhelmError:
    return AdhelmError(
        "GA4_ADMIN_EDIT_OAUTH_PROFILE_REQUIRED",
        "A configured Google OAuth GA4 Admin EDIT profile is required.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Reconnect the configured Google OAuth GA4 Admin EDIT profile locally.",
    )


def _invalid_binding() -> AdhelmError:
    return AdhelmError(
        "GA4_ADMIN_EDIT_OAUTH_BINDING_INVALID",
        "The protected Google OAuth GA4 Admin EDIT binding cannot be used.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Resolve a fresh configured Google OAuth GA4 Admin EDIT binding locally.",
    )


def _expired_binding() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_OAUTH_RECONNECT_REQUIRED",
        "The protected Google OAuth access token is no longer current.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Resolve a fresh configured Google OAuth GA4 Admin EDIT binding locally.",
    )


__all__ = [
    "Ga4AdminEditOAuthExecutionBinding",
    "bearer_for_ga4_admin_edit",
    "resolve_ga4_admin_edit_execution_binding",
]
