"""Protected local credential lifecycle for the TikTok Ads app auth-code exchange.

The access token the read adapter uses stays in the configured profile's
ordinary OS-store slot, so the existing ``auth status`` / ``auth remove-local``
contract keeps working unchanged.  Everything else this lifecycle needs — the
customer application's secret, the granted advertiser ID list, and the
non-secret expiry/identity/environment state — lives in separate derived slots
under a private credential-store service.  Separate slots are the durable fix
for pasting one credential into another's slot.

No function here performs provider egress.  The one HTTP call this lifecycle
makes lives in :mod:`adhelm.tiktok_token_workflow`, and no credential mutation
lock is ever held across it: this module opens and closes each locked section,
and the network step happens between them.

There is no refresh here on purpose.  The TikTok-maintained SDK documents
exactly two operations in its business Authentication API — the auth-code
exchange and the advertiser list — and neither refreshes an advertiser access
token.  :func:`require_tiktok_token_refresh_route` is the typed refusal that
says so and points at re-enrollment.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Never

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.config import configuration_transaction
from adhelm.credential_lock import credential_mutation_lock
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Configuration, Profile, validate_configuration
from adhelm.providers.tiktok_ads import TIKTOK_ADS_API_VERSION
from adhelm.tiktok_token_profiles import (
    TIKTOK_ACCESS_TOKEN_EXPIRY_NOT_DOCUMENTED,
    TIKTOK_ACCESS_TOKEN_EXPIRY_PROVIDER_DECLARED,
    TIKTOK_TOKEN_SERVICE,
    TikTokTokenEnvironment,
    checked_advertiser_id,
    select_tiktok_ads_profile,
    tiktok_token_reference,
)

_GENERIC_SERVICE = "adhelm"
_MAX_STATE_BYTES = 65_536
_MAX_SECRET_CHARS = 16_384
_MAX_ADVERTISERS = 512
_SECRET_TEXT = re.compile(r"^[\x21-\x7e]{1,16384}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_HEX_SALT = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_UTC_TIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{6})?\+00:00$"
)
_PSEUDONYM_PREFIX = "tiktok_ads_advertiser_v1."
_PSEUDONYM_DOMAIN = b"adhelm/tiktok/advertiser-pseudonym/v1\x00"
_SALT_BYTES = 32
_STATE_ISSUER = object()

TikTokTokenLocalState = Literal["READY", "ENROLL_STARTED"]
TikTokTokenReportedState = Literal["READY", "MISSING", "UNVERIFIED"]
TikTokExpirySource = Literal["PROVIDER_DECLARED", "NOT_DOCUMENTED_LOCALLY"]
TikTokAdvertiserBinding = Literal[
    "PROVEN_IN_EXCHANGE_RESPONSE", "NOT_PROVEN_NO_ADVERTISER_LIST_RETURNED"
]
TikTokAdvertiserListStatus = Literal["RETURNED", "NOT_RETURNED"]


@dataclass(frozen=True, slots=True, repr=False)
class TikTokTokenGrant:
    """One verified TikTok auth-code exchange; never serialize this publicly.

    ``advertiser_ids`` is ``None`` when the response carried no advertiser list
    at all.  That is missing information, never an empty list: the field name is
    recorded by this repository's 2026-09-04 provider-access review and by no
    page this build could fetch, so its absence must stay distinguishable.
    """

    app_ref: str
    environment: TikTokTokenEnvironment
    access_token: str
    access_token_expires_at: datetime | None
    expiry_source: TikTokExpirySource
    advertiser_ids: tuple[str, ...] | None
    scope_field_returned: bool

    def __post_init__(self) -> None:
        _validate_secret(self.access_token)
        if self.expiry_source not in {
            TIKTOK_ACCESS_TOKEN_EXPIRY_PROVIDER_DECLARED,
            TIKTOK_ACCESS_TOKEN_EXPIRY_NOT_DOCUMENTED,
        }:
            raise _state_invalid()
        if (self.expiry_source == TIKTOK_ACCESS_TOKEN_EXPIRY_PROVIDER_DECLARED) != (
            self.access_token_expires_at is not None
        ):
            raise _state_invalid()
        if self.access_token_expires_at is not None:
            object.__setattr__(
                self, "access_token_expires_at", _checked_time(self.access_token_expires_at)
            )
        if self.advertiser_ids is not None:
            if (
                type(self.advertiser_ids) is not tuple
                or not 1 <= len(self.advertiser_ids) <= _MAX_ADVERTISERS
                or len(set(self.advertiser_ids)) != len(self.advertiser_ids)
            ):
                raise _state_invalid()
            for value in self.advertiser_ids:
                checked_advertiser_id(value)
        if type(self.scope_field_returned) is not bool:
            raise _state_invalid()

    def __repr__(self) -> str:
        return "<TikTokTokenGrant redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("TikTok grants cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("TikTok grants cannot be serialized")


@dataclass(frozen=True, slots=True)
class TikTokTokenLifecycleResult:
    """A safe local outcome; it is not provider authorization or account authority."""

    profile_configuration_digest: str
    purpose: Literal["READ", "EDIT"]
    operation: Literal["ENROLL"]
    environment: TikTokTokenEnvironment
    api_version: str
    access_token_expires_at: datetime | None
    expiry_source: TikTokExpirySource
    advertiser_list_status: TikTokAdvertiserListStatus
    advertiser_pseudonyms: tuple[str, ...]
    bound_advertiser_pseudonym: str
    advertiser_binding: TikTokAdvertiserBinding
    provider: Literal["tiktok_ads"] = "tiktok_ads"
    credential_source: Literal["OS_STORE"] = "OS_STORE"
    local_state: Literal["READY"] = "READY"
    granted_permissions: Literal["NOT_DOCUMENTED_LOCALLY"] = "NOT_DOCUMENTED_LOCALLY"
    app_approval_verified: Literal[False] = False
    refresh_route: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    execution_authority: Literal[False] = False


@dataclass(frozen=True, slots=True)
class TikTokTokenStatus:
    """Bounded local status; it contains no secret and contacts no provider."""

    profile_configuration_digest: str
    purpose: Literal["READ", "EDIT"]
    local_state: TikTokTokenReportedState
    environment: TikTokTokenEnvironment | None
    api_version: str | None
    access_token_expires_at: datetime | None
    access_token_expired: bool | None
    expiry_source: TikTokExpirySource | None
    advertiser_list_status: TikTokAdvertiserListStatus | None
    advertiser_pseudonyms: tuple[str, ...]
    bound_advertiser_pseudonym: str | None
    advertiser_binding: TikTokAdvertiserBinding | None
    access_token_slot_present: bool
    app_secret_slot_present: bool
    advertiser_slot_present: bool
    provider: Literal["tiktok_ads"] = "tiktok_ads"
    credential_source: Literal["OS_STORE"] = "OS_STORE"
    granted_permissions: Literal["NOT_DOCUMENTED_LOCALLY"] = "NOT_DOCUMENTED_LOCALLY"
    app_approval_verified: Literal[False] = False
    provider_authentication_verified: Literal[False] = False
    remote_revocation_verified: Literal[False] = False
    refresh_route: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    execution_authority: Literal[False] = False


@dataclass(frozen=True, slots=True, repr=False)
class TikTokTokenEnrollmentSlot:
    """Opaque pre-exchange snapshot of the four exact slots this profile owns."""

    _configuration_digest: str
    _profile: Profile
    _environment: TikTokTokenEnvironment
    _expected: tuple[tuple[str, str], ...]
    _issuer: object

    def __repr__(self) -> str:
        return "<TikTokTokenEnrollmentSlot redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("TikTok token slots cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("TikTok token slots cannot be serialized")

    @property
    def environment(self) -> TikTokTokenEnvironment:
        return self._environment

    @property
    def profile_id(self) -> str:
        return self._profile.profile_id


@dataclass(frozen=True, slots=True, repr=False)
class _TikTokTokenState:
    """The one canonical non-secret record stored in the private state slot."""

    profile_id: str
    profile_digest: str
    app_ref: str
    environment: TikTokTokenEnvironment
    api_version: str
    access_token_expires_at: datetime | None
    expiry_source: TikTokExpirySource
    advertiser_list_status: TikTokAdvertiserListStatus
    advertiser_pseudonyms: tuple[str, ...]
    bound_advertiser_pseudonym: str
    advertiser_binding: TikTokAdvertiserBinding
    scope_field_returned: bool
    identity_salt: bytes
    local_state: TikTokTokenLocalState
    updated_at: datetime

    def _data(self) -> dict[str, object]:
        return {
            "access_token_expires_at": (
                None
                if self.access_token_expires_at is None
                else _time_text(self.access_token_expires_at)
            ),
            "advertiser_binding": self.advertiser_binding,
            "advertiser_list_status": self.advertiser_list_status,
            "advertiser_pseudonyms": list(self.advertiser_pseudonyms),
            "api_version": self.api_version,
            "app_ref": self.app_ref,
            "bound_advertiser_pseudonym": self.bound_advertiser_pseudonym,
            "environment": self.environment,
            "expiry_source": self.expiry_source,
            "identity_salt": self.identity_salt.hex(),
            "local_state": self.local_state,
            "profile_digest": self.profile_digest,
            "profile_id": self.profile_id,
            "schema_version": 1,
            # Only whether the response carried a permission field, never its
            # value: TikTok publishes no scope vocabulary this build could
            # fetch, so a stored value could not be interpreted honestly.
            "scope_field_returned": self.scope_field_returned,
            "updated_at": _time_text(self.updated_at),
        }

    def _encoded(self) -> str:
        try:
            return canonical_bytes(self._data()).decode("utf-8")
        except Exception:
            raise _state_invalid() from None


_STATE_KEYS = frozenset(
    {
        "access_token_expires_at",
        "advertiser_binding",
        "advertiser_list_status",
        "advertiser_pseudonyms",
        "api_version",
        "app_ref",
        "bound_advertiser_pseudonym",
        "environment",
        "expiry_source",
        "identity_salt",
        "local_state",
        "profile_digest",
        "profile_id",
        "schema_version",
        "scope_field_returned",
        "updated_at",
    }
)


def new_identity_salt() -> bytes:
    """Create the private per-profile salt used to pseudonymize advertiser IDs."""
    try:
        value = secrets.token_bytes(_SALT_BYTES)
    except Exception:
        raise _randomness_unavailable() from None
    if type(value) is not bytes or len(value) != _SALT_BYTES:
        raise _randomness_unavailable()
    return value


def tiktok_advertiser_pseudonym(salt: bytes, advertiser_id: str) -> str:
    """Derive the stable, salted, local-only label for one TikTok advertiser."""
    if type(salt) is not bytes or len(salt) != _SALT_BYTES:
        raise _state_invalid()
    checked = checked_advertiser_id(advertiser_id)
    material = _PSEUDONYM_DOMAIN + checked.encode("ascii")
    return _PSEUDONYM_PREFIX + hmac.new(salt, material, hashlib.sha256).hexdigest()


def prepare_tiktok_token_enrollment(
    directory: Path, profile_id: str, environment: TikTokTokenEnvironment
) -> TikTokTokenEnrollmentSlot:
    """Snapshot the four empty slots before any prompt or token exchange."""
    with configuration_transaction(directory) as transaction:
        configuration = _configuration(transaction.read())
        profile = select_tiktok_ads_profile(configuration, profile_id)
        configuration_digest = digest(configuration)
        references = _references(profile)
        expected: list[tuple[str, str]] = []
        with ExitStack() as stack:
            backend = _credential_backend()
            for reference in sorted(references.values()):
                stack.enter_context(credential_mutation_lock(reference))
            for reference in sorted(references.values()):
                existing = _read_slot(backend, _service_for(references, reference), reference)
                if existing is not None:
                    raise _credential_already_exists()
                expected.append((reference, _slot_value_hmac(reference, existing)))
    return TikTokTokenEnrollmentSlot(
        _configuration_digest=configuration_digest,
        _profile=profile,
        _environment=environment,
        _expected=tuple(expected),
        _issuer=_STATE_ISSUER,
    )


def install_tiktok_token_grant(
    directory: Path,
    expected_slot: TikTokTokenEnrollmentSlot,
    grant: TikTokTokenGrant,
    *,
    app_secret: str,
    identity_salt: bytes,
) -> TikTokTokenLifecycleResult:
    """Commit one verified exchange into the four exact local slots.

    The state slot records a durable ``ENROLL_STARTED`` before any secret is
    written, so an interrupted install is reported as unverified local state
    rather than as a usable profile.
    """
    slot = _checked_enrollment_slot(expected_slot)
    _validate_secret(app_secret)
    if type(grant) is not TikTokTokenGrant or grant.environment != slot._environment:
        raise _grant_invalid()
    profile_advertiser = checked_advertiser_id(slot._profile.account_ref)
    bound = tiktok_advertiser_pseudonym(identity_salt, profile_advertiser)
    if grant.advertiser_ids is None:
        binding: TikTokAdvertiserBinding = "NOT_PROVEN_NO_ADVERTISER_LIST_RETURNED"
        list_status: TikTokAdvertiserListStatus = "NOT_RETURNED"
        pseudonyms: tuple[str, ...] = ()
    else:
        if profile_advertiser not in grant.advertiser_ids:
            # The configured advertiser is the account every later read is bound
            # to.  A grant that does not name it is not this profile's grant.
            raise AdhelmError(
                "TIKTOK_TOKEN_ADVERTISER_NOT_GRANTED",
                "The TikTok exchange did not grant this profile's configured advertiser.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    "Authorize the configured advertiser in TikTok, then enroll again with a "
                    "fresh one-hour authorization code."
                ),
            )
        binding = "PROVEN_IN_EXCHANGE_RESPONSE"
        list_status = "RETURNED"
        pseudonyms = tuple(
            sorted(
                tiktok_advertiser_pseudonym(identity_salt, value) for value in grant.advertiser_ids
            )
        )
    now = _checked_now()
    references = _references(slot._profile)
    write_started = False
    try:
        with configuration_transaction(directory) as transaction:
            configuration = _configuration(
                transaction.require_unchanged(slot._configuration_digest)
            )
            profile = select_tiktok_ads_profile(configuration, slot._profile.profile_id)
            if not hmac.compare_digest(digest(profile), digest(slot._profile)):
                raise _credential_changed()
            with ExitStack() as stack:
                backend = _credential_backend()
                for reference in sorted(references.values()):
                    stack.enter_context(credential_mutation_lock(reference))
                _require_expected(backend, references, slot._expected)
                state = _TikTokTokenState(
                    profile_id=profile.profile_id,
                    profile_digest=digest(profile),
                    app_ref=grant.app_ref,
                    environment=grant.environment,
                    api_version=TIKTOK_ADS_API_VERSION,
                    access_token_expires_at=grant.access_token_expires_at,
                    expiry_source=grant.expiry_source,
                    advertiser_list_status=list_status,
                    advertiser_pseudonyms=pseudonyms,
                    bound_advertiser_pseudonym=bound,
                    advertiser_binding=binding,
                    scope_field_returned=grant.scope_field_returned,
                    identity_salt=identity_salt,
                    local_state="ENROLL_STARTED",
                    updated_at=now,
                )
                write_started = True
                _write_slot(backend, TIKTOK_TOKEN_SERVICE, references["STATE"], state._encoded())
                _write_slot(backend, TIKTOK_TOKEN_SERVICE, references["APP_SECRET"], app_secret)
                if grant.advertiser_ids is not None:
                    _write_slot(
                        backend,
                        TIKTOK_TOKEN_SERVICE,
                        references["ADVERTISER_IDS"],
                        _encoded_advertisers(grant.advertiser_ids),
                    )
                _write_slot(backend, _GENERIC_SERVICE, references["ACCESS"], grant.access_token)
                ready = _replace_state(state, local_state="READY", updated_at=_checked_now())
                _write_slot(backend, TIKTOK_TOKEN_SERVICE, references["STATE"], ready._encoded())
            transaction.require_unchanged(slot._configuration_digest)
    except AdhelmError:
        if write_started:
            raise _unverified_local_change() from None
        raise
    except Exception:
        if write_started:
            raise _unverified_local_change() from None
        raise _store_unavailable() from None
    return TikTokTokenLifecycleResult(
        profile_configuration_digest=digest(slot._profile),
        purpose=_purpose(slot._profile),
        operation="ENROLL",
        environment=grant.environment,
        api_version=TIKTOK_ADS_API_VERSION,
        access_token_expires_at=grant.access_token_expires_at,
        expiry_source=grant.expiry_source,
        advertiser_list_status=list_status,
        advertiser_pseudonyms=pseudonyms,
        bound_advertiser_pseudonym=bound,
        advertiser_binding=binding,
    )


def require_tiktok_token_refresh_route(directory: Path, profile_id: str) -> Never:
    """Always refuse: TikTok documents no advertiser refresh route.

    The refusal is still profile-scoped, so an unconfigured profile or a missing
    enrollment is reported as such rather than being hidden behind the generic
    "no refresh" message.  Nothing here contacts TikTok.
    """
    status = tiktok_token_status(directory, profile_id)
    if status.local_state == "MISSING":
        raise _enrollment_required()
    raise AdhelmError(
        "TIKTOK_TOKEN_REFRESH_NOT_DOCUMENTED",
        "TikTok documents no refresh route for an advertiser access token.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action=(
            "Re-enroll with `adhelm auth tiktok-token enroll --profile <id> --interactive`. The "
            "TikTok-maintained SDK documents exactly two business Authentication operations, "
            "the auth-code exchange and the advertiser list, and neither refreshes an "
            "advertiser token; treat any refresh capability as unverified."
        ),
    )


def tiktok_token_status(directory: Path, profile_id: str) -> TikTokTokenStatus:
    """Return bounded local state without a secret, reference, or provider call.

    A credential-store failure is raised, never reported as an absent slot: an
    unreadable store and an empty slot are different facts.
    """
    try:
        with configuration_transaction(directory) as transaction:
            configuration = _configuration(transaction.read())
            profile = select_tiktok_ads_profile(configuration, profile_id)
            configuration_digest = digest(configuration)
            references = _references(profile)
            with ExitStack() as stack:
                backend = _credential_backend()
                for reference in sorted(references.values()):
                    stack.enter_context(credential_mutation_lock(reference))
                raw_state = _read_slot(backend, TIKTOK_TOKEN_SERVICE, references["STATE"])
                access_present = (
                    _read_slot(backend, _GENERIC_SERVICE, references["ACCESS"]) is not None
                )
                secret_present = (
                    _read_slot(backend, TIKTOK_TOKEN_SERVICE, references["APP_SECRET"]) is not None
                )
                advertiser_present = (
                    _read_slot(backend, TIKTOK_TOKEN_SERVICE, references["ADVERTISER_IDS"])
                    is not None
                )
            transaction.require_unchanged(configuration_digest)
    except AdhelmError:
        raise
    except Exception:
        raise _store_unavailable() from None
    presence = (access_present, secret_present, advertiser_present)
    if raw_state is None:
        return _bare_status(profile, "MISSING", presence)
    try:
        state = _decode_state(raw_state, profile)
    except AdhelmError as error:
        if error.code == "TIKTOK_TOKEN_PROFILE_CHANGED":
            raise
        return _bare_status(profile, "UNVERIFIED", presence)
    reported: TikTokTokenReportedState
    if state.local_state == "READY":
        reported = "READY" if access_present else "UNVERIFIED"
    else:
        # An interrupted enroll is never reported as usable.
        reported = "UNVERIFIED"
    return TikTokTokenStatus(
        profile_configuration_digest=digest(profile),
        purpose=_purpose(profile),
        local_state=reported,
        environment=state.environment,
        api_version=state.api_version,
        access_token_expires_at=state.access_token_expires_at,
        # An expiry TikTok never declared is unknown, never "not expired".
        access_token_expired=(
            None
            if state.access_token_expires_at is None
            else state.access_token_expires_at <= _checked_now()
        ),
        expiry_source=state.expiry_source,
        advertiser_list_status=state.advertiser_list_status,
        advertiser_pseudonyms=state.advertiser_pseudonyms,
        bound_advertiser_pseudonym=state.bound_advertiser_pseudonym,
        advertiser_binding=state.advertiser_binding,
        access_token_slot_present=access_present,
        app_secret_slot_present=secret_present,
        advertiser_slot_present=advertiser_present,
    )


def remove_local_tiktok_token_slots(directory: Path, profile_id: str) -> tuple[str, int]:
    """Delete only the protected TikTok-owned slots for one configured profile.

    The configured access-token slot is deliberately untouched: it belongs to the
    shared ``auth remove-local`` contract.  Nothing here contacts TikTok, and a
    successful removal is never evidence of provider-side revocation.
    """
    removed = 0
    try:
        with configuration_transaction(directory) as transaction:
            configuration = _configuration(transaction.read())
            profile = select_tiktok_ads_profile(configuration, profile_id)
            configuration_digest = digest(configuration)
            private = sorted(
                reference for name, reference in _references(profile).items() if name != "ACCESS"
            )
            with ExitStack() as stack:
                backend = _credential_backend()
                for reference in private:
                    stack.enter_context(credential_mutation_lock(reference))
                for reference in private:
                    if _raw_slot(backend, TIKTOK_TOKEN_SERVICE, reference) is None:
                        continue
                    _delete_slot(backend, TIKTOK_TOKEN_SERVICE, reference)
                    removed += 1
            transaction.require_unchanged(configuration_digest)
    except AdhelmError:
        raise
    except Exception:
        raise _store_unavailable() from None
    return digest(profile), removed


def _bare_status(
    profile: Profile, reported: TikTokTokenReportedState, presence: tuple[bool, bool, bool]
) -> TikTokTokenStatus:
    access_present, secret_present, advertiser_present = presence
    return TikTokTokenStatus(
        profile_configuration_digest=digest(profile),
        purpose=_purpose(profile),
        local_state=reported,
        environment=None,
        api_version=None,
        access_token_expires_at=None,
        access_token_expired=None,
        expiry_source=None,
        advertiser_list_status=None,
        advertiser_pseudonyms=(),
        bound_advertiser_pseudonym=None,
        advertiser_binding=None,
        access_token_slot_present=access_present,
        app_secret_slot_present=secret_present,
        advertiser_slot_present=advertiser_present,
    )


def _references(profile: Profile) -> dict[str, str]:
    return {
        "ACCESS": profile.credential_ref,
        "APP_SECRET": tiktok_token_reference(profile.profile_id, "APP_SECRET"),
        "ADVERTISER_IDS": tiktok_token_reference(profile.profile_id, "ADVERTISER_IDS"),
        "STATE": tiktok_token_reference(profile.profile_id, "STATE"),
    }


def _service_for(references: dict[str, str], reference: str) -> str:
    return _GENERIC_SERVICE if reference == references["ACCESS"] else TIKTOK_TOKEN_SERVICE


def _require_expected(
    backend: object, references: dict[str, str], expected: tuple[tuple[str, str], ...]
) -> None:
    if len(expected) != len(references):
        raise _credential_changed()
    for reference, value_hmac in expected:
        observed = _read_slot(backend, _service_for(references, reference), reference)
        if not hmac.compare_digest(_slot_value_hmac(reference, observed), value_hmac):
            raise _credential_changed()


def _slot_value_hmac(reference: str, value: str | None) -> str:
    material = f"{reference}\x00".encode() + (b"" if value is None else value.encode("utf-8"))
    marker = b"absent" if value is None else b"present"
    return hashlib.sha256(_PSEUDONYM_DOMAIN + marker + b"\x00" + material).hexdigest()


def _encoded_advertisers(advertiser_ids: tuple[str, ...]) -> str:
    try:
        return canonical_bytes(
            {"advertiser_ids": sorted(advertiser_ids), "schema_version": 1}
        ).decode("utf-8")
    except Exception:
        raise _state_invalid() from None


def _replace_state(
    state: _TikTokTokenState, *, local_state: TikTokTokenLocalState, updated_at: datetime
) -> _TikTokTokenState:
    return _TikTokTokenState(
        profile_id=state.profile_id,
        profile_digest=state.profile_digest,
        app_ref=state.app_ref,
        environment=state.environment,
        api_version=state.api_version,
        access_token_expires_at=state.access_token_expires_at,
        expiry_source=state.expiry_source,
        advertiser_list_status=state.advertiser_list_status,
        advertiser_pseudonyms=state.advertiser_pseudonyms,
        bound_advertiser_pseudonym=state.bound_advertiser_pseudonym,
        advertiser_binding=state.advertiser_binding,
        scope_field_returned=state.scope_field_returned,
        identity_salt=state.identity_salt,
        local_state=local_state,
        updated_at=updated_at,
    )


def _decode_state(raw: str, profile: Profile) -> _TikTokTokenState:
    if type(raw) is not str or not 1 <= len(raw.encode("utf-8")) <= _MAX_STATE_BYTES:
        raise _state_invalid()
    try:
        data = parse_json(raw.encode("utf-8"), maximum_bytes=_MAX_STATE_BYTES)
    except Exception:
        raise _state_invalid() from None
    if type(data) is not dict or data.get("schema_version") != 1 or set(data) != _STATE_KEYS:
        raise _state_invalid()
    salt = data["identity_salt"]
    pseudonyms = data["advertiser_pseudonyms"]
    bound = data["bound_advertiser_pseudonym"]
    if (
        data["environment"] not in {"PRODUCTION", "SANDBOX"}
        or data["expiry_source"]
        not in {
            TIKTOK_ACCESS_TOKEN_EXPIRY_PROVIDER_DECLARED,
            TIKTOK_ACCESS_TOKEN_EXPIRY_NOT_DOCUMENTED,
        }
        or data["advertiser_list_status"] not in {"RETURNED", "NOT_RETURNED"}
        or data["advertiser_binding"]
        not in {"PROVEN_IN_EXCHANGE_RESPONSE", "NOT_PROVEN_NO_ADVERTISER_LIST_RETURNED"}
        or data["local_state"] not in {"READY", "ENROLL_STARTED"}
        or type(data["scope_field_returned"]) is not bool
        or type(salt) is not str
        or _HEX_SALT.fullmatch(salt) is None
        or not _is_pseudonym(bound)
        or type(pseudonyms) is not list
        or len(pseudonyms) > _MAX_ADVERTISERS
        or any(not _is_pseudonym(value) for value in pseudonyms)
        or list(pseudonyms) != sorted(set(pseudonyms))
        or type(data["app_ref"]) is not str
        or not 1 <= len(data["app_ref"]) <= 200
        or any(not 33 <= ord(character) <= 126 for character in data["app_ref"])
        or type(data["api_version"]) is not str
        or not 1 <= len(data["api_version"]) <= 20
        or type(data["profile_id"]) is not str
        or data["profile_id"] != profile.profile_id
        or type(data["profile_digest"]) is not str
        or _DIGEST.fullmatch(data["profile_digest"]) is None
    ):
        raise _state_invalid()
    if (data["expiry_source"] == TIKTOK_ACCESS_TOKEN_EXPIRY_PROVIDER_DECLARED) != (
        data["access_token_expires_at"] is not None
    ):
        raise _state_invalid()
    if (data["advertiser_list_status"] == "RETURNED") != (
        data["advertiser_binding"] == "PROVEN_IN_EXCHANGE_RESPONSE"
    ):
        raise _state_invalid()
    if not hmac.compare_digest(data["profile_digest"], digest(profile)):
        raise AdhelmError(
            "TIKTOK_TOKEN_PROFILE_CHANGED",
            "The configured TikTok profile changed after this credential was stored.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Remove the local TikTok credential and enroll the current profile again."
            ),
        )
    try:
        return _TikTokTokenState(
            profile_id=data["profile_id"],
            profile_digest=data["profile_digest"],
            app_ref=data["app_ref"],
            environment=data["environment"],
            api_version=data["api_version"],
            access_token_expires_at=(
                None
                if data["access_token_expires_at"] is None
                else _parse_time(data["access_token_expires_at"])
            ),
            expiry_source=data["expiry_source"],
            advertiser_list_status=data["advertiser_list_status"],
            advertiser_pseudonyms=tuple(pseudonyms),
            bound_advertiser_pseudonym=bound,
            advertiser_binding=data["advertiser_binding"],
            scope_field_returned=data["scope_field_returned"],
            identity_salt=bytes.fromhex(salt),
            local_state=data["local_state"],
            updated_at=_parse_time(data["updated_at"]),
        )
    except AdhelmError:
        raise
    except Exception:
        raise _state_invalid() from None


def _is_pseudonym(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith(_PSEUDONYM_PREFIX)
        and _DIGEST.fullmatch(value[len(_PSEUDONYM_PREFIX) :]) is not None
    )


def _checked_enrollment_slot(value: object) -> TikTokTokenEnrollmentSlot:
    if type(value) is not TikTokTokenEnrollmentSlot or value._issuer is not _STATE_ISSUER:
        raise _slot_invalid()
    if (
        type(value._configuration_digest) is not str
        or _DIGEST.fullmatch(value._configuration_digest) is None
        or type(value._profile) is not Profile
        or value._environment not in {"PRODUCTION", "SANDBOX"}
        or type(value._expected) is not tuple
        or not value._expected
    ):
        raise _slot_invalid()
    return value


def _configuration(value: Configuration) -> Configuration:
    try:
        return validate_configuration(value)
    except Exception:
        raise AdhelmError(
            "TIKTOK_TOKEN_CONFIGURATION_INVALID",
            "TikTok token setup requires valid local configuration.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _purpose(profile: Profile) -> Literal["READ", "EDIT"]:
    return "EDIT" if profile.purpose == "EDIT" else "READ"


def _credential_backend() -> object:
    from adhelm.credentials import _backend

    return _backend()


def _read_slot(backend: object, service: str, reference: str) -> str | None:
    try:
        value = backend.get_password(service, reference)  # type: ignore[attr-defined]
    except Exception:
        raise _store_unavailable() from None
    if value is None:
        return None
    if type(value) is not str or not value:
        raise _state_invalid()
    return value


def _write_slot(backend: object, service: str, reference: str, value: str) -> None:
    try:
        backend.set_password(service, reference, value)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        observed = backend.get_password(service, reference)  # type: ignore[attr-defined]
    except Exception:
        raise _unverified_local_change() from None
    if type(observed) is not str or not hmac.compare_digest(observed, value):
        raise _unverified_local_change()


def _raw_slot(backend: object, service: str, reference: str) -> str | None:
    try:
        value = backend.get_password(service, reference)  # type: ignore[attr-defined]
    except Exception:
        raise _store_unavailable() from None
    return value if type(value) is str and value else None


def _delete_slot(backend: object, service: str, reference: str) -> None:
    try:
        backend.delete_password(service, reference)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        observed = backend.get_password(service, reference)  # type: ignore[attr-defined]
    except Exception:
        raise _unverified_local_change() from None
    if observed is not None:
        raise _unverified_local_change()


def _validate_secret(value: object) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= _MAX_SECRET_CHARS
        or _SECRET_TEXT.fullmatch(value) is None
    ):
        raise _state_invalid()


def _parse_time(value: object) -> datetime:
    if type(value) is not str or _UTC_TIME.fullmatch(value) is None:
        raise _state_invalid()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _state_invalid() from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise _state_invalid()
    return parsed.astimezone(UTC)


def _time_text(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise _state_invalid()
    text = value.astimezone(UTC).isoformat()
    if _UTC_TIME.fullmatch(text) is None:
        raise _state_invalid()
    return text


def _checked_time(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise _state_invalid()
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    """Private trusted-time seam for deterministic local-only tests."""
    return datetime.now(UTC)


def _checked_now() -> datetime:
    value = _utc_now()
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise AdhelmError(
            "TIKTOK_TOKEN_CLOCK_INVALID",
            "The TikTok token lifecycle requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        )
    return value.astimezone(UTC)


def _randomness_unavailable() -> AdhelmError:
    return AdhelmError(
        "TIKTOK_TOKEN_RANDOMNESS_UNAVAILABLE",
        "Secure local randomness is unavailable.",
        exit_code=ExitCode.PRECONDITION,
    )


def _credential_already_exists() -> AdhelmError:
    return AdhelmError(
        "TIKTOK_TOKEN_CREDENTIAL_EXISTS",
        "A local TikTok credential already exists for this profile.",
        exit_code=ExitCode.PRECONDITION,
        next_action=(
            "Review `auth tiktok-token status`, then clear both local halves deliberately "
            "before a new enrollment: `auth tiktok-token remove-local --profile <id>` for the "
            "protected TikTok slots and `auth remove-local --profile <id>` for the stored "
            "access-token entry."
        ),
    )


def _credential_changed() -> AdhelmError:
    return AdhelmError(
        "TIKTOK_TOKEN_CREDENTIAL_CHANGED",
        "Local TikTok credential state changed during this operation.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect `auth tiktok-token status` before a deliberate follow-up.",
    )


def _enrollment_required() -> AdhelmError:
    return AdhelmError(
        "TIKTOK_TOKEN_ENROLLMENT_REQUIRED",
        "This TikTok profile has no complete local enrollment.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Run `auth tiktok-token enroll --profile <id> --interactive` locally.",
    )


def _grant_invalid() -> AdhelmError:
    return AdhelmError(
        "TIKTOK_TOKEN_GRANT_INVALID",
        "The TikTok exchange result does not match the selected local profile.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Start a new local TikTok enrollment with a fresh authorization code.",
    )


def _slot_invalid() -> AdhelmError:
    return AdhelmError(
        "TIKTOK_TOKEN_SLOT_INVALID",
        "The TikTok credential lifecycle context is invalid.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Start the TikTok credential operation again from its own command.",
    )


def _state_invalid() -> AdhelmError:
    return AdhelmError(
        "TIKTOK_TOKEN_STATE_INVALID",
        "The protected TikTok credential state cannot be used.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Re-enroll the TikTok profile with a fresh one-hour authorization code.",
    )


def _store_unavailable() -> AdhelmError:
    return AdhelmError(
        "CREDENTIAL_STORE_UNAVAILABLE",
        "The OS credential store is unavailable.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Check the local OS credential store and retry deliberately.",
    )


def _unverified_local_change() -> AdhelmError:
    return AdhelmError(
        "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED",
        "A local TikTok credential change could not be verified after writing.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect `auth tiktok-token status` before a deliberate new action.",
    )
