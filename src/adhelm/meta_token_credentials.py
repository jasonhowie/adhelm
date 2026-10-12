"""Protected local credential lifecycle for fixed Meta token inspection.

The sole stored value is a private canonical bundle containing the inspected
USER token, the customer app/developer inspector token, and the local binding
key.  This module has no provider request, account discovery, refresh, token
exchange, generic credential resolver, or mutation capability.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal, Never, Protocol

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.config import configuration_transaction
from adhelm.credential_lock import credential_mutation_lock
from adhelm.errors import AdhelmError, ExitCode
from adhelm.meta_token_inspection import MetaTokenInspectionCredentials
from adhelm.meta_token_inspection import _credential_digest as _inspection_credential_digest
from adhelm.meta_token_observation_records import (
    OBSERVATIONS_BUSY_CODE,
    remove_meta_token_observation,
)
from adhelm.meta_token_profiles import (
    MetaAccountTokenProfileDraft,
    finalize_meta_account_token_profile,
    validate_meta_account_token_profile_draft,
)
from adhelm.models import (
    Configuration,
    MetaAccountTokenProfile,
    validate_configuration,
    validate_configured_profile,
)

_SERVICE = "adhelm.meta-token-v1"
_MAX_BUNDLE_BYTES = 65_536
_MAX_TOKEN_CHARS = 16_384
_DIGEST = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_SLOT_HMAC_DOMAIN = b"adhelm/meta/token/install-slot/v1\x00"
_SLOT_ISSUER = object()
_SLOT_HMAC_KEY: bytes | None = None
_SLOT_HMAC_LOCK = Lock()


class _CredentialStore(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class MetaTokenCredentialInstallSlot:
    """Opaque pre-prompt snapshot for one fixed protected bundle selector."""

    _configuration_digest: str
    _profile: MetaAccountTokenProfile
    _expected_value_hmac: str
    _replace: bool
    _issuer: object

    def __repr__(self) -> str:
        return "<MetaTokenCredentialInstallSlot redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Meta token install slots cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Meta token install slots cannot be serialized")


@dataclass(frozen=True, slots=True)
class MetaTokenCredentialLifecycleResult:
    """Safe local outcome; it is not provider authentication or authority."""

    profile_configuration_digest: str
    purpose: Literal["READ", "EDIT"]
    operation: Literal["ENROLL", "ROTATE"]
    provider: Literal["meta_ads"] = "meta_ads"
    credential_source: Literal["OS_META_TOKEN"] = "OS_META_TOKEN"
    local_state: Literal["READY"] = "READY"
    provider_authentication_verified: Literal[False] = False
    account_access_verified: Literal[False] = False
    execution_authority: Literal[False] = False


@dataclass(frozen=True, slots=True)
class MetaTokenLocalRemovalResult:
    """Safe local outcome of a removal; it is never a provider revocation.

    Both flags say whether there was something to delete, so a repeated removal
    or a profile that was never inspected reads as what it is rather than as a
    deletion that did not happen.
    """

    profile_configuration_digest: str
    purpose: Literal["READ", "EDIT"]
    bundle_removed: bool
    retained_inspection_removed: bool
    provider: Literal["meta_ads"] = "meta_ads"
    credential_source: Literal["OS_META_TOKEN"] = "OS_META_TOKEN"
    remote_revocation_verified: Literal[False] = False


@dataclass(frozen=True, slots=True)
class MetaTokenCredentialStatus:
    """Bounded local bundle status without a secret, reference, or provider fact.

    `credential_binding_ref` is an HMAC over the installed bundle's own private
    key and its subject token.  It names no token, cannot be reversed into one,
    and exists so a caller can tell whether a retained inspection describes the
    bundle that is installed *now*.  It is deliberately not part of any emitted
    status projection.
    """

    profile_configuration_digest: str
    purpose: Literal["READ", "EDIT"]
    local_state: Literal["READY", "MISSING", "UNVERIFIED"]
    credential_binding_ref: str | None = None
    provider: Literal["meta_ads"] = "meta_ads"
    credential_source: Literal["OS_META_TOKEN"] = "OS_META_TOKEN"
    provider_authentication_verified: Literal[False] = False
    account_access_verified: Literal[False] = False
    execution_authority: Literal[False] = False


@dataclass(frozen=True, slots=True, repr=False)
class _MetaTokenCredentialBundle:
    profile_id: str
    profile_digest: str
    subject_token: str
    inspector_token: str
    binding_key: bytes

    def _data(self) -> dict[str, object]:
        return {
            "binding_key": self.binding_key.hex(),
            "inspector_token": self.inspector_token,
            "profile_digest": self.profile_digest,
            "profile_id": self.profile_id,
            "schema_version": 1,
            "subject_token": self.subject_token,
        }

    def _encoded(self) -> str:
        try:
            return canonical_bytes(self._data()).decode("utf-8")
        except Exception:
            raise _bundle_invalid() from None


@dataclass(frozen=True, slots=True, repr=False)
class _MetaTokenInspectionContext:
    _configuration_digest: str
    _profile_id: str
    _profile_digest: str
    _client_application_ref: str
    _required_scope: Literal["ads_read", "ads_management"]

    def __repr__(self) -> str:
        return "<_MetaTokenInspectionContext redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Meta token inspection contexts cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Meta token inspection contexts cannot be serialized")


def prepare_meta_token_enrollment(
    directory: Path, draft: MetaAccountTokenProfileDraft
) -> MetaTokenCredentialInstallSlot:
    """Snapshot an empty fixed bundle slot before a hidden local token prompt."""
    profile = finalize_meta_account_token_profile(validate_meta_account_token_profile_draft(draft))
    with configuration_transaction(directory) as transaction:
        configuration = _configuration(transaction.read())
        _require_new_profile(configuration, profile)
        expected_configuration = digest(configuration)
        with credential_mutation_lock(profile.credential_ref):
            existing = _read_raw_slot(_credential_backend(), profile.credential_ref)
        if existing is not None:
            # Without a retained profile there is no safe way to attribute an
            # old selector value to this new declaration.  Do not offer a
            # rotation path that could overwrite uncertain local state.
            raise _bundle_invalid()
    return MetaTokenCredentialInstallSlot(
        _configuration_digest=expected_configuration,
        _profile=profile,
        _expected_value_hmac=_slot_value_hmac(profile.credential_ref, existing),
        _replace=False,
        _issuer=_SLOT_ISSUER,
    )


def prepare_meta_token_rotation(directory: Path, profile_id: str) -> MetaTokenCredentialInstallSlot:
    """Snapshot one current bundle before an explicit full replacement prompt."""
    with configuration_transaction(directory) as transaction:
        configuration = _configuration(transaction.read())
        profile = _selected_profile(configuration, profile_id)
        expected_configuration = digest(configuration)
        with credential_mutation_lock(profile.credential_ref):
            existing = _read_raw_slot(_credential_backend(), profile.credential_ref)
            if existing is None:
                raise _credential_missing()
            _bind_bundle(_decode_bundle(existing), profile)
    return MetaTokenCredentialInstallSlot(
        _configuration_digest=expected_configuration,
        _profile=profile,
        _expected_value_hmac=_slot_value_hmac(profile.credential_ref, existing),
        _replace=True,
        _issuer=_SLOT_ISSUER,
    )


def install_meta_token_credentials(
    directory: Path,
    expected_slot: MetaTokenCredentialInstallSlot,
    *,
    subject_token_supplier: Callable[[], str],
    inspector_token_supplier: Callable[[], str],
) -> MetaTokenCredentialLifecycleResult:
    """Commit one config-bound protected bundle after hidden token collection.

    The final profile configuration is saved only after the fixed credential
    slot lock is available and the pre-prompt snapshot still matches.  One
    bundle write/readback replaces both tokens and the private binding key as a
    unit.  A local error after a config or bundle write starts is deliberately
    ambiguous; this function never rolls back or retries it.
    """
    slot = _checked_install_slot(expected_slot)
    subject_token = _secret_from_supplier(subject_token_supplier)
    inspector_token = _secret_from_supplier(inspector_token_supplier)
    configuration_write_started = False
    bundle_write_started = False
    try:
        with configuration_transaction(directory) as transaction:
            current = _configuration(transaction.require_unchanged(slot._configuration_digest))
            profile, active_configuration = _current_profile_for_install(current, slot)
            active_digest = digest(active_configuration)
            with credential_mutation_lock(profile.credential_ref):
                backend = _credential_backend()
                before = _read_raw_slot(backend, profile.credential_ref)
                if not hmac.compare_digest(
                    _slot_value_hmac(profile.credential_ref, before), slot._expected_value_hmac
                ):
                    raise _credential_changed()
                if not slot._replace and before is not None:
                    raise _credential_changed()
                if not slot._replace:
                    configuration_write_started = True
                    transaction.save(active_configuration, replace=True)
                    transaction.require_unchanged(active_digest)
                # A cooperating writer cannot alter the slot while its lock is
                # held; reread anyway to fail closed against observed external
                # changes before replacing the bundle.
                reread = _read_raw_slot(backend, profile.credential_ref)
                if not hmac.compare_digest(
                    _slot_value_hmac(profile.credential_ref, reread), slot._expected_value_hmac
                ):
                    raise _credential_changed()
                bundle = _new_bundle(profile, subject_token, inspector_token)
                encoded = bundle._encoded()
                bundle_write_started = True
                backend.set_password(_SERVICE, profile.credential_ref, encoded)
                observed = _read_raw_slot(backend, profile.credential_ref)
                if type(observed) is not str or not hmac.compare_digest(observed, encoded):
                    raise _transaction_unverified()
                transaction.require_unchanged(active_digest)
    except AdhelmError:
        if configuration_write_started or bundle_write_started:
            raise _transaction_unverified() from None
        raise
    except Exception:
        if configuration_write_started or bundle_write_started:
            raise _transaction_unverified() from None
        raise _store_unavailable() from None
    return MetaTokenCredentialLifecycleResult(
        profile_configuration_digest=digest(slot._profile),
        purpose=slot._profile.purpose,
        operation="ROTATE" if slot._replace else "ENROLL",
    )


def enroll_meta_token_profile(
    directory: Path,
    draft: MetaAccountTokenProfileDraft,
    *,
    subject_token_supplier: Callable[[], str],
    inspector_token_supplier: Callable[[], str],
) -> MetaTokenCredentialLifecycleResult:
    """Create one nonsecret profile and its protected bundle in a local flow."""
    slot = prepare_meta_token_enrollment(directory, draft)
    return install_meta_token_credentials(
        directory,
        slot,
        subject_token_supplier=subject_token_supplier,
        inspector_token_supplier=inspector_token_supplier,
    )


def rotate_meta_token_credentials(
    directory: Path,
    profile_id: str,
    *,
    subject_token_supplier: Callable[[], str],
    inspector_token_supplier: Callable[[], str],
) -> MetaTokenCredentialLifecycleResult:
    """Explicitly replace the complete local bundle; it never refreshes Meta."""
    slot = prepare_meta_token_rotation(directory, profile_id)
    return install_meta_token_credentials(
        directory,
        slot,
        subject_token_supplier=subject_token_supplier,
        inspector_token_supplier=inspector_token_supplier,
    )


def meta_token_credential_status(directory: Path, profile_id: str) -> MetaTokenCredentialStatus:
    """Inspect bounded local bundle state without returning its reference or contents."""
    binding: str | None = None
    try:
        with configuration_transaction(directory) as transaction:
            configuration = _configuration(transaction.read())
            profile = _selected_profile(configuration, profile_id)
            expected_configuration = digest(configuration)
            with credential_mutation_lock(profile.credential_ref):
                try:
                    raw = _read_raw_slot(_credential_backend(), profile.credential_ref)
                    if raw is None:
                        state: Literal["READY", "MISSING", "UNVERIFIED"] = "MISSING"
                    else:
                        bundle = _bind_bundle(_decode_bundle(raw), profile)
                        # Derived under the same lock that proved the bundle
                        # readable, from values that never leave this function.
                        binding = _credential_binding_ref(bundle)
                        state = "READY"
                except AdhelmError as error:
                    if error.code != "META_TOKEN_CREDENTIAL_UNVERIFIED":
                        raise
                    state = "UNVERIFIED"
                except Exception:
                    state = "UNVERIFIED"
            transaction.require_unchanged(expected_configuration)
    except AdhelmError:
        raise
    except Exception:
        raise _store_unavailable() from None
    return MetaTokenCredentialStatus(
        profile_configuration_digest=digest(profile),
        purpose=profile.purpose,
        local_state=state,
        credential_binding_ref=binding,
    )


def remove_meta_token_local(directory: Path, profile_id: str) -> MetaTokenLocalRemovalResult:
    """Delete one profile's local bundle and retained inspection; never revoke it.

    Only the selected profile's `adhelm.meta-token-v1` entry and its own record
    in the shared retained-inspection file are touched, so every other Meta
    profile keeps both.  The bundle is never decoded: removal has to work on a
    bundle that is malformed, bound to a changed profile, or otherwise
    unreadable, and it needs nothing a token would supply.  The configured
    profile stays; after removal it reads as `MISSING`, and nothing here sends
    a request to Meta.
    """
    try:
        with configuration_transaction(directory) as transaction:
            configuration = _configuration(transaction.read())
            profile = _selected_profile(configuration, profile_id)
            expected_configuration = digest(configuration)
            with credential_mutation_lock(profile.credential_ref):
                bundle_removed = _delete_bundle(_credential_backend(), profile.credential_ref)
                try:
                    inspection_removed = remove_meta_token_observation(directory, profile)
                except AdhelmError as error:
                    if error.code == OBSERVATIONS_BUSY_CODE:
                        raise _inspection_removal_busy(profile.profile_id) from None
                    raise _inspection_removal_unverified() from None
            transaction.require_unchanged(expected_configuration)
    except AdhelmError:
        raise
    except Exception:
        raise _store_unavailable() from None
    return MetaTokenLocalRemovalResult(
        profile_configuration_digest=digest(profile),
        purpose=profile.purpose,
        bundle_removed=bundle_removed,
        retained_inspection_removed=inspection_removed,
    )


def _delete_bundle(backend: _CredentialStore, reference: str) -> bool:
    """Delete one bundle entry and prove by readback that it is gone.

    The only read is a presence check whose value is discarded at once.  A
    failed delete is not trusted either way: the readback decides.
    """
    try:
        present = backend.get_password(_SERVICE, reference) is not None
    except Exception:
        raise _store_unavailable() from None
    if not present:
        return False
    try:
        backend.delete_password(_SERVICE, reference)
    except Exception:
        pass
    try:
        remaining = backend.get_password(_SERVICE, reference)
    except Exception:
        raise _transaction_unverified() from None
    if remaining is not None:
        raise _transaction_unverified()
    return True


def _credential_binding_ref(bundle: _MetaTokenCredentialBundle) -> str:
    """The installed bundle's binding reference, exactly as inspection derives it.

    `adhelm.meta_token_inspection` computes this over the same private key and
    subject token when it records an observation, so a retained observation can
    be matched against the bundle installed now without either side holding the
    other's secret.  A rotation replaces both the key and the token, so the
    reference changes and a stale observation stops matching.
    """
    return _inspection_credential_digest(
        MetaTokenInspectionCredentials(
            profile_ref=bundle.profile_id,
            subject_token=bundle.subject_token,
            inspector_token=bundle.inspector_token,
            binding_key=bundle.binding_key,
        )
    )


def _prepare_meta_token_inspection(directory: Path, profile_id: str) -> _MetaTokenInspectionContext:
    """Capture the exact nonsecret configuration expected by both bundle reads."""
    with configuration_transaction(directory) as transaction:
        configuration = _configuration(transaction.read())
        profile = _selected_profile(configuration, profile_id)
        return _MetaTokenInspectionContext(
            _configuration_digest=digest(configuration),
            _profile_id=profile.profile_id,
            _profile_digest=digest(profile),
            _client_application_ref=profile.client_application_ref,
            _required_scope=profile.required_scope,
        )


def _resolve_meta_token_inspection_credentials(
    directory: Path, context: _MetaTokenInspectionContext
) -> MetaTokenInspectionCredentials:
    """Read one exact protected bundle after rechecking its full configuration."""
    if type(context) is not _MetaTokenInspectionContext:
        raise _credential_invalid()
    try:
        with configuration_transaction(directory) as transaction:
            configuration = _configuration(
                transaction.require_unchanged(context._configuration_digest)
            )
            profile = _selected_profile(configuration, context._profile_id)
            if (
                not hmac.compare_digest(digest(profile), context._profile_digest)
                or profile.client_application_ref != context._client_application_ref
                or profile.required_scope != context._required_scope
            ):
                raise _credential_changed()
            with credential_mutation_lock(profile.credential_ref):
                raw = _read_raw_slot(_credential_backend(), profile.credential_ref)
                if raw is None:
                    raise _credential_missing()
                bundle = _bind_bundle(_decode_bundle(raw), profile)
            transaction.require_unchanged(context._configuration_digest)
    except AdhelmError:
        raise
    except Exception:
        raise _store_unavailable() from None
    return MetaTokenInspectionCredentials(
        profile_ref=profile.profile_id,
        subject_token=bundle.subject_token,
        inspector_token=bundle.inspector_token,
        binding_key=bundle.binding_key,
    )


def _configuration(value: Configuration) -> Configuration:
    try:
        return validate_configuration(value)
    except Exception:
        raise AdhelmError(
            "META_TOKEN_PROFILE_CONFIGURATION_INVALID",
            "Meta token setup requires valid local configuration.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _selected_profile(configuration: Configuration, profile_id: str) -> MetaAccountTokenProfile:
    if type(profile_id) is not str:
        raise _profile_required()
    matches = [profile for profile in configuration.profiles if profile.profile_id == profile_id]
    if len(matches) != 1:
        raise _profile_required()
    try:
        checked = validate_configured_profile(matches[0])
    except Exception:
        raise _profile_required() from None
    if not isinstance(checked, MetaAccountTokenProfile):
        raise _profile_required()
    return checked


def _require_new_profile(configuration: Configuration, profile: MetaAccountTokenProfile) -> None:
    if len(configuration.profiles) >= 100 or any(
        existing.profile_id == profile.profile_id for existing in configuration.profiles
    ):
        raise AdhelmError(
            "META_TOKEN_PROFILE_EXISTS",
            "The requested Meta token profile cannot be added to this configuration.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action=(
                "Use a new profile ID or rotate the existing Meta token profile deliberately."
            ),
        )


def _current_profile_for_install(
    configuration: Configuration, slot: MetaTokenCredentialInstallSlot
) -> tuple[MetaAccountTokenProfile, Configuration]:
    if slot._replace:
        profile = _selected_profile(configuration, slot._profile.profile_id)
        if not hmac.compare_digest(digest(profile), digest(slot._profile)):
            raise _credential_changed()
        return profile, configuration
    _require_new_profile(configuration, slot._profile)
    updated = Configuration.model_validate(
        {
            **configuration.model_dump(mode="python", warnings="error"),
            "profiles": [*configuration.profiles, slot._profile],
        }
    )
    return slot._profile, updated


def _checked_install_slot(value: object) -> MetaTokenCredentialInstallSlot:
    if type(value) is not MetaTokenCredentialInstallSlot:
        raise _credential_invalid()
    try:
        configuration_digest = value._configuration_digest
        profile = value._profile
        expected_value_hmac = value._expected_value_hmac
        replace = value._replace
        issuer = value._issuer
    except Exception:
        raise _credential_invalid() from None
    if (
        issuer is not _SLOT_ISSUER
        or type(configuration_digest) is not str
        or _DIGEST.fullmatch(configuration_digest) is None
        or type(expected_value_hmac) is not str
        or _DIGEST.fullmatch(expected_value_hmac) is None
        or type(replace) is not bool
    ):
        raise _credential_invalid()
    try:
        checked_profile = validate_configured_profile(profile)
    except Exception:
        raise _credential_invalid() from None
    if not isinstance(checked_profile, MetaAccountTokenProfile):
        raise _credential_invalid()
    try:
        supplied_profile_digest = digest(profile.model_dump(mode="python", warnings="error"))
        profile_digest = digest(checked_profile)
    except Exception:
        raise _credential_invalid() from None
    # Rebuild the opaque capability from its revalidated closed facts rather
    # than retaining a potentially model-constructed nested profile.
    if not hmac.compare_digest(supplied_profile_digest, profile_digest):
        raise _credential_invalid()
    return MetaTokenCredentialInstallSlot(
        _configuration_digest=configuration_digest,
        _profile=checked_profile,
        _expected_value_hmac=expected_value_hmac,
        _replace=replace,
        _issuer=_SLOT_ISSUER,
    )


def _new_bundle(
    profile: MetaAccountTokenProfile, subject_token: str, inspector_token: str
) -> _MetaTokenCredentialBundle:
    try:
        key = secrets.token_bytes(32)
    except Exception:
        raise _transaction_unverified() from None
    if type(key) is not bytes or len(key) != 32:
        raise _transaction_unverified()
    return _MetaTokenCredentialBundle(
        profile_id=profile.profile_id,
        profile_digest=digest(profile),
        subject_token=subject_token,
        inspector_token=inspector_token,
        binding_key=key,
    )


def _decode_bundle(raw: str) -> _MetaTokenCredentialBundle:
    try:
        if type(raw) is not str or not 1 <= len(raw.encode("utf-8")) <= _MAX_BUNDLE_BYTES:
            raise ValueError("raw")
        data = parse_json(raw, maximum_bytes=_MAX_BUNDLE_BYTES)
        if type(data) is not dict or set(data) != {
            "binding_key",
            "inspector_token",
            "profile_digest",
            "profile_id",
            "schema_version",
            "subject_token",
        }:
            raise ValueError("shape")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("schema")
        profile_id = data["profile_id"]
        if type(profile_id) is not str or not 1 <= len(profile_id) <= 160:
            raise ValueError("profile")
        profile_digest = data["profile_digest"]
        if type(profile_digest) is not str or _DIGEST.fullmatch(profile_digest) is None:
            raise ValueError("digest")
        subject_token = _checked_secret(data["subject_token"])
        inspector_token = _checked_secret(data["inspector_token"])
        binding_key = _decode_key(data["binding_key"])
        if canonical_bytes(data).decode("utf-8") != raw:
            raise ValueError("canonical")
        return _MetaTokenCredentialBundle(
            profile_id=profile_id,
            profile_digest=profile_digest,
            subject_token=subject_token,
            inspector_token=inspector_token,
            binding_key=binding_key,
        )
    except AdhelmError:
        raise _bundle_invalid() from None
    except Exception:
        raise _bundle_invalid() from None


def _bind_bundle(
    bundle: _MetaTokenCredentialBundle, profile: MetaAccountTokenProfile
) -> _MetaTokenCredentialBundle:
    if type(bundle) is not _MetaTokenCredentialBundle:
        raise _bundle_invalid()
    if not (
        hmac.compare_digest(bundle.profile_id, profile.profile_id)
        and hmac.compare_digest(bundle.profile_digest, digest(profile))
    ):
        raise _credential_changed()
    return bundle


def _secret_from_supplier(supplier: Callable[[], str]) -> str:
    if not callable(supplier):
        raise _credential_input_invalid()
    try:
        supplied = supplier()
    except Exception:
        raise _credential_input_invalid() from None
    # This validation follows the untrusted prompt callback.  Its errors are
    # ours and therefore already fixed/redacted.
    return _checked_secret(supplied)


def _checked_secret(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= _MAX_TOKEN_CHARS
        or any(not 33 <= ord(character) <= 126 for character in value)
    ):
        raise _credential_input_invalid()
    return value


def _decode_key(value: object) -> bytes:
    if type(value) is not str or len(value) != 64:
        raise ValueError("key")
    key = bytes.fromhex(value)
    if len(key) != 32:
        raise ValueError("key")
    return key


def _credential_backend() -> _CredentialStore:
    from adhelm.credentials import _backend

    try:
        return _backend()
    except Exception:
        # The generic backend boundary may carry platform/keyring details.  Do
        # not let those cross the dedicated Meta-token lifecycle.
        raise _store_unavailable() from None


def _read_raw_slot(backend: _CredentialStore, reference: str) -> str | None:
    try:
        value = backend.get_password(_SERVICE, reference)
    except Exception:
        raise _store_unavailable() from None
    if value is None:
        return None
    try:
        valid = type(value) is str and 1 <= len(value.encode("utf-8")) <= _MAX_BUNDLE_BYTES
    except Exception:
        valid = False
    if not valid:
        raise _bundle_invalid()
    return value


def _slot_value_hmac(reference: str, value: str | None) -> str:
    if type(reference) is not str or (value is not None and type(value) is not str):
        raise _credential_invalid()
    try:
        state = b"M" if value is None else b"V" + value.encode("utf-8")
        material = _SLOT_HMAC_DOMAIN + reference.casefold().encode("ascii") + b"\x00" + state
    except Exception:
        raise _credential_invalid() from None
    return hmac.new(_slot_hmac_key(), material, hashlib.sha256).hexdigest()


def _slot_hmac_key() -> bytes:
    global _SLOT_HMAC_KEY
    with _SLOT_HMAC_LOCK:
        if _SLOT_HMAC_KEY is None:
            try:
                key = secrets.token_bytes(32)
            except Exception:
                raise _credential_invalid() from None
            if type(key) is not bytes or len(key) != 32:
                raise _credential_invalid()
            _SLOT_HMAC_KEY = key
        return _SLOT_HMAC_KEY


def _profile_required() -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_PROFILE_REQUIRED",
        "Select one configured Meta token profile.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action="Configure the exact Meta account token profile before local setup.",
    )


def _credential_input_invalid() -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_CREDENTIAL_INPUT_INVALID",
        "Meta token setup requires bounded local credential input.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action="Use the supported local hidden credential prompt.",
    )


def _credential_invalid() -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_CREDENTIAL_SNAPSHOT_INVALID",
        "The Meta token local setup state is invalid.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect local Meta credential status before a deliberate follow-up.",
    )


def _credential_missing() -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_CREDENTIAL_MISSING",
        "The configured Meta token bundle is missing.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Complete the dedicated local Meta token setup before inspection.",
    )


def _credential_changed() -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_CREDENTIAL_CHANGED",
        "The Meta token local state changed during the requested operation.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect local Meta credential status before a deliberate follow-up.",
    )


def _bundle_invalid() -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_CREDENTIAL_UNVERIFIED",
        "The configured Meta token bundle could not be verified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect local Meta credential status before a deliberate follow-up.",
    )


def _transaction_unverified() -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_CREDENTIAL_TRANSACTION_UNVERIFIED",
        "The Meta token local credential change could not be verified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect local Meta credential status before any deliberate replacement.",
    )


def _inspection_removal_unverified() -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_INSPECTION_REMOVAL_UNVERIFIED",
        "The Meta token bundle is not stored, but its retained inspection record could not be "
        "removed or verified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Run `adhelm auth meta-token status --profile <id>`. The record holds no token and "
            "describes nothing installed; if the file is unreadable, delete "
            "`meta-token-observations.json` from the AdHelm data directory."
        ),
    )


def _inspection_removal_busy(profile_id: str) -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_INSPECTION_REMOVAL_BUSY",
        "The Meta token bundle is not stored, but another AdHelm command was updating the "
        "retained inspection file, so this profile's record was not removed.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            f"Run `adhelm auth meta-token remove-local --profile {profile_id}` again once "
            "that command has finished. The bundle is already gone, so the rerun removes only "
            "the record."
        ),
    )


def _store_unavailable() -> AdhelmError:
    return AdhelmError(
        "CREDENTIAL_STORE_UNAVAILABLE",
        "The OS credential store is unavailable.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Check the local OS credential store before a deliberate follow-up.",
    )
