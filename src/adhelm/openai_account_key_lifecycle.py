"""Local lifecycle for the OpenAI Ads account-scoped API key.

OpenAI Ads authenticates with one account-scoped API key sent as a bearer
token.  There is no OAuth authorization code, refresh token, or documented
programmatic revocation endpoint, so this module deliberately implements a
smaller lifecycle than the LinkedIn OAuth one:

* **status** reports bounded local facts -- whether the key slot holds a value,
  the canonical account reference the profile is bound to, a masked identity
  label for the account the **currently stored** key was last shown to resolve
  to, and when that verification happened.  The retained record is bound to the
  key it verified by a keyed digest, so a key stored or replaced by the generic
  `auth` commands is reported `UNVERIFIED` rather than inheriting the previous
  key's identity claim.  It performs no provider request and reveals no key
  material.
* **rotate** prompts for a replacement key without echo, verifies with the
  **new** key that the same ad account still answers, and only then replaces
  the stored value.  A verification failure or an account mismatch leaves the
  previous key in place.
* **revocation** is provider-side.  Deleting the local copy is local removal,
  never revocation, and this module never reports otherwise.  No reviewed page
  documents an expiry, a rotation endpoint, or a revocation route, so no result
  here claims the key is either still valid or already retired at OpenAI.
  `auth remove-local` on one of these profiles clears the derived state record
  as well as the key slot; see `remove_openai_account_key_state`.

No credential mutation lock is held across an HTTP call.  The stored value is
snapshotted under a lock, the lock is released for the hidden prompt and the
identity read, and a second locked section refuses to write if the stored value
changed while the operation was in flight.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Never

import httpx

from adhelm.canonical import canonical_bytes, parse_json
from adhelm.config import load_configuration
from adhelm.credential_lock import credential_mutation_lock
from adhelm.credential_setup import prompt_secret_from_terminal
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import (
    Configuration,
    ConfiguredProfile,
    OpenAIAccountKeyProfile,
    validate_configuration,
    validate_configured_profile,
)
from adhelm.providers.openai_ads import (
    OPENAI_ADS_API_HOST,
    OPENAI_ADS_API_VERSION,
    OpenAIAdsClient,
    OpenAIAdsIdentity,
)
from adhelm.transport import SafeHttpClient

_GENERIC_SERVICE: Final = "adhelm"
# The non-secret verification record lives in its own credential-store service
# so that it can never be mistaken for, or overwritten by, a customer's bearer
# slot in the generic service.
OPENAI_ACCOUNT_KEY_STATE_SERVICE: Final = "adhelm.openai-account-key-v1"
_STATE_REFERENCE_PREFIX: Final = "openai-account-key-state.v1."
_STATE_DOMAIN: Final = b"adhelm/openai/account-key/state-reference/v1\x00"
_PSEUDONYM_DOMAIN: Final = b"adhelm/openai/account-key/account-pseudonym/v1\x00"
# The keyed digest that binds one verification record to the exact stored key
# it verified.  It is an HMAC under the record's own local salt, never the key
# and never an unkeyed hash of it, so a state slot leaked on its own discloses
# no material an offline guess could confirm against a candidate key.
_KEY_BINDING_DOMAIN: Final = b"adhelm/openai/account-key/key-binding/v1\x00"
_PSEUDONYM_PREFIX: Final = "openai_ads_account_v1."
_SALT_BYTES: Final = 32
_MAX_STATE_BYTES: Final = 8_192
# Version 2 added `key_binding`.  A version this build does not write is not
# read: an unbound record cannot say which key it verified, so it fails closed
# rather than being read as a verification of whatever is stored now.
_STATE_SCHEMA_VERSION: Final = 2
_MAX_SECRET_CHARS: Final = 16_384
_SECRET_TEXT: Final = re.compile(r"^[\x21-\x7e]{1,16384}$")
_HEX_SALT: Final = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_HEX_BINDING: Final = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_PSEUDONYM: Final = re.compile(r"^openai_ads_account_v1\.[a-f0-9]{64}$", re.ASCII)
_UTC_TIME: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{6})?\+00:00$"
)
_PROFILE_ID: Final = re.compile(r"^[a-zA-Z0-9_.:/-]{1,160}$")
_KEY_PROMPT: Final = "Credential secret: "

# The one honest sentence about what local removal does and does not do.  It is
# repeated verbatim in every result so that no caller can present a local
# deletion as a provider revocation.
OPENAI_ACCOUNT_KEY_REVOCATION_GUIDANCE: Final = (
    "OpenAI documents that an Ads API key is issued in the Settings tab of OpenAI Ads Manager; "
    "retire a key there. UNVERIFIED: no reviewed OpenAI Ads page documents a key expiry, a "
    "rotation endpoint, a revocation screen, or a programmatic revocation route, so no expiry, "
    "rotation, or revocation behaviour is known here. Removing the local copy with "
    "`adhelm auth remove-local` deletes this machine's stored value only; this product cannot "
    "observe the provider-side state of a key, cannot verify a provider-side revocation, and "
    "does not claim the key is either still valid or revoked at OpenAI."
)

OpenAIAccountKeyLocalState = Literal["READY", "MISSING", "UNVERIFIED", "CI_MANAGED"]


@dataclass(frozen=True, slots=True)
class OpenAIAccountKeyStatus:
    """Bounded local status.  It contains no secret and contacts no provider."""

    profile_id: str
    purpose: Literal["READ", "EDIT", "ACTIVATE", "PUBLISH"]
    credential_source: Literal["OS_STORE", "EXPLICIT_CI"]
    local_state: OpenAIAccountKeyLocalState
    key_slot_present: bool
    account_ref: str
    identity_pseudonym: str | None
    last_verified_at: datetime | None
    last_verified_api_version: str | None
    provider: Literal["openai_ads"] = "openai_ads"
    provider_scope_status: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    provider_revocation_verified: Literal[False] = False
    revocation_guidance: str = OPENAI_ACCOUNT_KEY_REVOCATION_GUIDANCE


@dataclass(frozen=True, slots=True)
class OpenAIAccountKeyRotation:
    """A safe local outcome; it is not provider authorization or account authority."""

    profile_id: str
    purpose: Literal["READ", "EDIT", "ACTIVATE", "PUBLISH"]
    account_ref: str
    identity_pseudonym: str
    verified_at: datetime
    verified_api_version: str
    identity_reverified_before_replacement: Literal[True] = True
    operation: Literal["ROTATE"] = "ROTATE"
    provider: Literal["openai_ads"] = "openai_ads"
    credential_source: Literal["OS_STORE"] = "OS_STORE"
    local_state: Literal["READY"] = "READY"
    provider_revocation_verified: Literal[False] = False
    previous_key_revoked_at_provider: Literal[False] = False
    revocation_guidance: str = OPENAI_ACCOUNT_KEY_REVOCATION_GUIDANCE


@dataclass(frozen=True, slots=True, repr=False)
class _State:
    """The one canonical non-secret record stored in the private state slot."""

    profile_id: str
    identity_salt: bytes
    identity_pseudonym: str
    # Binds this record to the exact stored key that was verified.  Without it
    # the record states only that *some* key once resolved an account, which is
    # not a verification of the value the key slot holds now.
    key_binding: str
    verified_at: datetime
    verified_api_version: str

    def __repr__(self) -> str:
        return "<OpenAI account-key state redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("OpenAI account-key state cannot be serialized")

    def encoded(self) -> str:
        data = {
            "identity_pseudonym": self.identity_pseudonym,
            "identity_salt": self.identity_salt.hex(),
            "key_binding": self.key_binding,
            "profile_id": self.profile_id,
            "schema_version": _STATE_SCHEMA_VERSION,
            "verified_api_version": self.verified_api_version,
            "verified_at": _time_text(self.verified_at),
        }
        try:
            return canonical_bytes(data).decode("utf-8")
        except Exception:
            raise _state_invalid() from None


def openai_account_key_state_reference(profile_id: str) -> str:
    """Derive the private state-slot name for one profile without echoing it."""
    if type(profile_id) is not str or _PROFILE_ID.fullmatch(profile_id) is None:
        raise AdhelmError(
            "OPENAI_ACCOUNT_KEY_PROFILE_REQUIRED",
            "The OpenAI account-key lifecycle requires one configured profile identifier.",
            exit_code=int(ExitCode.INVALID_INPUT),
        )
    fingerprint = hashlib.sha256(_STATE_DOMAIN + profile_id.encode("utf-8")).hexdigest()
    return f"{_STATE_REFERENCE_PREFIX}{fingerprint}"


def openai_account_pseudonym(salt: bytes, account_client_ref: str) -> str:
    """Derive the stable, salted, local-only label for one OpenAI ad account."""
    if type(salt) is not bytes or len(salt) != _SALT_BYTES:
        raise _state_invalid()
    if (
        type(account_client_ref) is not str
        or not 1 <= len(account_client_ref) <= 120
        or not all(
            character.isascii() and (character.isalnum() or character in "_-")
            for character in account_client_ref
        )
    ):
        raise _state_invalid()
    material = _PSEUDONYM_DOMAIN + account_client_ref.encode("ascii")
    return _PSEUDONYM_PREFIX + hmac.new(salt, material, hashlib.sha256).hexdigest()


def _key_binding(salt: bytes, key: str) -> str:
    """Derive the keyed digest that binds one verification record to one key.

    The key is read here and nowhere else in this module's record path.  What
    is retained is an HMAC under the record's own local salt; the key itself,
    its length, and any prefix of it stay out of the record, the result, and
    every diagnostic.
    """
    if type(salt) is not bytes or len(salt) != _SALT_BYTES or type(key) is not str or not key:
        raise _state_invalid()
    material = _KEY_BINDING_DOMAIN + key.encode("utf-8")
    return hmac.new(salt, material, hashlib.sha256).hexdigest()


def openai_account_key_status(directory: Path, profile_id: str) -> OpenAIAccountKeyStatus:
    """Return bounded local status.  This makes no OpenAI request."""
    profile = select_openai_account_key_profile(_configuration(directory), profile_id)
    canonical_account = _canonical_account_ref(profile)
    if profile.credential_source == "EXPLICIT_CI":
        # A CI-managed value is not in the OS store and has no local rotation
        # lifecycle, so no presence or verification claim is made about it.
        return OpenAIAccountKeyStatus(
            profile_id=profile.profile_id,
            purpose=profile.purpose,
            credential_source="EXPLICIT_CI",
            local_state="CI_MANAGED",
            key_slot_present=False,
            account_ref=canonical_account,
            identity_pseudonym=None,
            last_verified_at=None,
            last_verified_api_version=None,
        )
    backend = _backend()
    with credential_mutation_lock(profile.credential_ref):
        stored = _read_slot(backend, _GENERIC_SERVICE, profile.credential_ref)
    key_present = stored is not None
    state = _read_state(backend, profile)
    # A retained record is a verification of the key it was written for, not of
    # whatever the slot holds now.  The generic `auth store` and `auth rotate`
    # commands write that slot without touching this record, so the binding is
    # rechecked here before any pseudonym or verification instant is published.
    verified = state is not None and stored is not None and _state_binds(state, stored)
    if not key_present:
        local_state: OpenAIAccountKeyLocalState = "MISSING"
    elif not verified:
        local_state = "UNVERIFIED"
    else:
        local_state = "READY"
    return OpenAIAccountKeyStatus(
        profile_id=profile.profile_id,
        purpose=profile.purpose,
        credential_source="OS_STORE",
        local_state=local_state,
        key_slot_present=key_present,
        account_ref=canonical_account,
        identity_pseudonym=state.identity_pseudonym if verified and state is not None else None,
        last_verified_at=state.verified_at if verified and state is not None else None,
        last_verified_api_version=(
            state.verified_api_version if verified and state is not None else None
        ),
    )


def rotate_openai_account_key(
    directory: Path, profile_id: str, *, interactive: bool = False
) -> OpenAIAccountKeyRotation:
    """Replace the stored key only after the new key re-verifies the same account.

    Ordering matters and is fixed:

    1. Snapshot the stored value under the credential mutation lock, then
       release the lock.
    2. Prompt for the replacement without echo.  No lock is held.
    3. Verify the account identity with the **new** key.  No lock is held
       across this HTTP call, and the previous key is still in place.
    4. Re-acquire the lock, refuse if the stored value changed meanwhile, and
       only then write the replacement and prove the write by readback.
    """
    _require_interactive(interactive)
    profile = select_openai_account_key_profile(_configuration(directory), profile_id)
    account_client_ref = _account_client_ref(profile)
    if profile.credential_source != "OS_STORE":
        raise AdhelmError(
            "OPENAI_ACCOUNT_KEY_CI_ROTATION_UNSUPPORTED",
            "An explicit-CI OpenAI Ads key has no local rotation lifecycle.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action="Rotate the value in the explicitly enabled CI environment instead.",
        )
    backend = _backend()
    reference = profile.credential_ref
    with credential_mutation_lock(reference):
        existing = _read_slot(backend, _GENERIC_SERVICE, reference)
    if existing is None:
        raise AdhelmError(
            "OPENAI_ACCOUNT_KEY_MISSING",
            "This profile has no locally stored OpenAI Ads key to rotate.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action="Store the key first with `adhelm auth store --profile <id>`.",
        )
    state = _read_state(backend, profile)
    # The retained pseudonym is a claim about the key that was verified.  It
    # guards this rotation only when it still binds to the value being
    # replaced; an unbound record keeps its salt, so the same ad account keeps
    # the same label, but it states nothing about the current slot.
    bound_state = state if state is not None and _state_binds(state, existing) else None
    replacement = _validate_secret(_prompt_key())
    if hmac.compare_digest(replacement, existing):
        raise AdhelmError(
            "OPENAI_ACCOUNT_KEY_UNCHANGED",
            "The entered OpenAI Ads key is the value already stored for this profile.",
            exit_code=int(ExitCode.INVALID_INPUT),
            next_action=("Create a new key in OpenAI Ads Manager, then rotate to that new value."),
        )
    # Identity re-verification happens here, with the new key, before the old
    # slot is touched.  No lock is held across this call.
    identity = verify_openai_account_identity(replacement, account_client_ref)
    salt = state.identity_salt if state is not None else _new_identity_salt()
    pseudonym = openai_account_pseudonym(salt, identity.account_client_ref)
    if bound_state is not None and not hmac.compare_digest(
        pseudonym, bound_state.identity_pseudonym
    ):
        raise AdhelmError(
            "OPENAI_ACCOUNT_KEY_IDENTITY_MISMATCH",
            "The replacement OpenAI Ads key resolves to a different ad account.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action=(
                "Do not use this key. Create a key for the configured ad account in OpenAI Ads "
                "Manager and rotate again; the previous key was left in place."
            ),
        )
    verified_at = _utc_now()
    with credential_mutation_lock(reference):
        if _read_slot(backend, _GENERIC_SERVICE, reference) != existing:
            raise AdhelmError(
                "CREDENTIAL_CONCURRENT_CHANGE",
                "The local OpenAI Ads key changed while this rotation was verifying.",
                next_action="Review `adhelm auth openai-key status` before a deliberate retry.",
            )
        _write_slot(backend, _GENERIC_SERVICE, reference, replacement)
        _write_slot(
            backend,
            OPENAI_ACCOUNT_KEY_STATE_SERVICE,
            openai_account_key_state_reference(profile.profile_id),
            _State(
                profile_id=profile.profile_id,
                identity_salt=salt,
                identity_pseudonym=pseudonym,
                key_binding=_key_binding(salt, replacement),
                verified_at=verified_at,
                verified_api_version=OPENAI_ADS_API_VERSION,
            ).encoded(),
        )
    return OpenAIAccountKeyRotation(
        profile_id=profile.profile_id,
        purpose=profile.purpose,
        account_ref=identity.account_ref,
        identity_pseudonym=pseudonym,
        verified_at=verified_at,
        verified_api_version=OPENAI_ADS_API_VERSION,
    )


def remove_openai_account_key_state(profile: OpenAIAccountKeyProfile) -> None:
    """Delete the derived verification record for one profile, and prove it.

    The generic `adhelm auth remove-local` deletes the key slot.  Leaving this
    record behind would leave a masked identity and a verification instant that
    describe a key this machine no longer holds, so local removal clears both
    slots or fails.  This is local removal, never a provider-side revocation.
    """
    if type(profile) is not OpenAIAccountKeyProfile or profile.credential_source != "OS_STORE":
        return
    backend = _backend()
    reference = openai_account_key_state_reference(profile.profile_id)
    # A different reference from the key slot, so this holds its own lock and
    # can be nested inside the caller's lock on the key slot.
    with credential_mutation_lock(reference):
        try:
            backend.delete_password(OPENAI_ACCOUNT_KEY_STATE_SERVICE, reference)
        except Exception:
            # Absence is the outcome this asks for; the readback below decides.
            pass
        if _read_slot(backend, OPENAI_ACCOUNT_KEY_STATE_SERVICE, reference) is not None:
            raise _unverified_local_change()


def verify_openai_account_identity(key: str, account_client_ref: str) -> OpenAIAdsIdentity:
    """Read the one fixed account identity with the exact in-memory bearer.

    The key is passed only through the adapter's in-memory getter.  Nothing
    here writes it to a model, a record, a diagnostic, or an exception.
    """
    client = _open_read_client()
    try:
        adapter = OpenAIAdsClient(
            SafeHttpClient(client, {OPENAI_ADS_API_HOST}),
            lambda: key,
            connector_version="0.1.0.dev0",
        )
        return adapter.discover_identity(expected_account_client_ref=account_client_ref)
    finally:
        client.close()


def select_openai_account_key_profile(
    configuration: Configuration, profile_id: str
) -> OpenAIAccountKeyProfile:
    """Select exactly one configured OpenAI Ads account-key profile."""
    if type(profile_id) is not str or _PROFILE_ID.fullmatch(profile_id) is None:
        raise AdhelmError(
            "OPENAI_ACCOUNT_KEY_PROFILE_REQUIRED",
            "The OpenAI account-key lifecycle requires one configured profile identifier.",
            exit_code=int(ExitCode.INVALID_INPUT),
        )
    matches = [item for item in configuration.profiles if item.profile_id == profile_id]
    if len(matches) != 1 or type(matches[0]) is not OpenAIAccountKeyProfile:
        raise AdhelmError(
            "OPENAI_ACCOUNT_KEY_PROFILE_REQUIRED",
            "This command requires one configured OpenAI Ads account-key profile.",
            exit_code=int(ExitCode.INVALID_INPUT),
            next_action="Add an OPENAI_ADS_ACCOUNT_KEY profile with `adhelm config add-profile`.",
        )
    validated: ConfiguredProfile = validate_configured_profile(matches[0])
    if not isinstance(validated, OpenAIAccountKeyProfile):
        raise AdhelmError(
            "OPENAI_ACCOUNT_KEY_PROFILE_REQUIRED",
            "This command requires one configured OpenAI Ads account-key profile.",
            exit_code=int(ExitCode.INVALID_INPUT),
        )
    return validated


def _account_client_ref(profile: OpenAIAccountKeyProfile) -> str:
    return profile.account_ref


def _canonical_account_ref(profile: OpenAIAccountKeyProfile) -> str:
    return f"openai_ads.account.{profile.account_ref}"


def _configuration(directory: Path) -> Configuration:
    try:
        return validate_configuration(load_configuration(directory))
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "CONFIGURATION_INVALID",
            "Local configuration failed validation.",
            exit_code=int(ExitCode.INVALID_INPUT),
        ) from None


def _prompt_key() -> str:
    """Collect the replacement account key without echo."""
    return prompt_secret_from_terminal(prompt=_KEY_PROMPT)


def _open_read_client() -> httpx.Client:
    """Production identity-read client seam; tests supply a mock transport."""
    return httpx.Client(trust_env=False, follow_redirects=False)


def _require_interactive(interactive: object) -> None:
    if interactive is not True:
        raise AdhelmError(
            "EXPLICIT_LOCAL_ROTATION_REQUIRED",
            "OpenAI Ads key rotation requires an explicitly requested local hidden prompt.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action="Run locally with --interactive; keep API keys out of chat.",
        )
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise AdhelmError(
            "INTERACTIVE_TERMINAL_REQUIRED",
            "OpenAI Ads key rotation requires a local interactive terminal.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action="Run rotation in a local terminal; do not provide a key in chat.",
        )


def _backend() -> Any:
    from adhelm.credentials import _backend as approved_backend

    return approved_backend()


def _read_slot(backend: Any, service: str, reference: str) -> str | None:
    try:
        value = backend.get_password(service, reference)
    except Exception:
        raise _store_unavailable() from None
    if value is None:
        return None
    if type(value) is not str or not value:
        raise _slot_invalid()
    return value


def _write_slot(backend: Any, service: str, reference: str, value: str) -> None:
    try:
        backend.set_password(service, reference, value)
    except Exception:
        raise _store_unavailable() from None
    # A stored credential is durable only when the approved store reproduces the
    # exact value; otherwise the rotation is reported as unverified rather than
    # silently accepted.
    observed = _read_slot(backend, service, reference)
    if type(observed) is not str or not hmac.compare_digest(observed, value):
        raise _unverified_local_change()


def _read_state(backend: Any, profile: OpenAIAccountKeyProfile) -> _State | None:
    reference = openai_account_key_state_reference(profile.profile_id)
    raw = _read_slot(backend, OPENAI_ACCOUNT_KEY_STATE_SERVICE, reference)
    if raw is None:
        return None
    return _decode_state(raw, profile)


def _decode_state(raw: str, profile: OpenAIAccountKeyProfile) -> _State:
    if type(raw) is not str or not 1 <= len(raw.encode("utf-8")) <= _MAX_STATE_BYTES:
        raise _state_invalid()
    try:
        data = parse_json(raw.encode("utf-8"), maximum_bytes=_MAX_STATE_BYTES)
    except Exception:
        raise _state_invalid() from None
    if type(data) is not dict or data.get("schema_version") != _STATE_SCHEMA_VERSION:
        raise _state_invalid()
    salt = data.get("identity_salt")
    pseudonym = data.get("identity_pseudonym")
    binding = data.get("key_binding")
    verified_at = data.get("verified_at")
    api_version = data.get("verified_api_version")
    if (
        data.get("profile_id") != profile.profile_id
        or type(salt) is not str
        or _HEX_SALT.fullmatch(salt) is None
        or type(pseudonym) is not str
        or _PSEUDONYM.fullmatch(pseudonym) is None
        or type(binding) is not str
        or _HEX_BINDING.fullmatch(binding) is None
        or type(verified_at) is not str
        or _UTC_TIME.fullmatch(verified_at) is None
        or type(api_version) is not str
        or not 1 <= len(api_version) <= 40
    ):
        raise _state_invalid()
    return _State(
        profile_id=profile.profile_id,
        identity_salt=bytes.fromhex(salt),
        identity_pseudonym=pseudonym,
        key_binding=binding,
        verified_at=datetime.fromisoformat(verified_at),
        verified_api_version=api_version,
    )


def _state_binds(state: _State, key: str) -> bool:
    """Whether one retained record is the verification of this exact key."""
    return hmac.compare_digest(state.key_binding, _key_binding(state.identity_salt, key))


def _new_identity_salt() -> bytes:
    try:
        value = secrets.token_bytes(_SALT_BYTES)
    except Exception:
        raise _randomness_unavailable() from None
    if type(value) is not bytes or len(value) != _SALT_BYTES:
        raise _randomness_unavailable()
    return value


def _validate_secret(value: str) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= _MAX_SECRET_CHARS
        or _SECRET_TEXT.fullmatch(value) is None
    ):
        raise AdhelmError(
            "CREDENTIAL_VALUE_INVALID",
            "The entered OpenAI Ads key is not a usable bounded secret.",
            exit_code=int(ExitCode.INVALID_INPUT),
            next_action="Paste the key exactly as OpenAI Ads Manager issued it.",
        )
    return value


def _utc_now() -> datetime:
    now = datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise AdhelmError("CLOCK_INVALID", "The local clock must provide an aware UTC time.")
    return now


def _time_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _state_invalid() -> AdhelmError:
    return AdhelmError(
        "OPENAI_ACCOUNT_KEY_STATE_INVALID",
        "The local OpenAI Ads key verification record is unreadable.",
        next_action="Review `adhelm auth openai-key status` before a deliberate new action.",
    )


def _slot_invalid() -> AdhelmError:
    return AdhelmError(
        "OPENAI_ACCOUNT_KEY_SLOT_INVALID",
        "A local OpenAI Ads credential slot holds an unusable value.",
        exit_code=int(ExitCode.AUTHORIZATION),
        next_action="Remove the local value deliberately, then store the key again.",
    )


def _store_unavailable() -> AdhelmError:
    return AdhelmError(
        "CREDENTIAL_STORE_UNAVAILABLE",
        "The OS credential store is unavailable.",
    )


def _randomness_unavailable() -> AdhelmError:
    return AdhelmError(
        "OPENAI_ACCOUNT_KEY_RANDOMNESS_UNAVAILABLE",
        "Secure local randomness is unavailable.",
    )


def _unverified_local_change() -> AdhelmError:
    return AdhelmError(
        "CREDENTIAL_CHANGE_UNVERIFIED",
        "A local OpenAI Ads credential change could not be proven by readback.",
        next_action="Review `adhelm auth openai-key status` before a deliberate new action.",
    )


__all__ = [
    "OPENAI_ACCOUNT_KEY_REVOCATION_GUIDANCE",
    "OPENAI_ACCOUNT_KEY_STATE_SERVICE",
    "OpenAIAccountKeyRotation",
    "OpenAIAccountKeyStatus",
    "openai_account_key_state_reference",
    "openai_account_key_status",
    "openai_account_pseudonym",
    "remove_openai_account_key_state",
    "rotate_openai_account_key",
    "select_openai_account_key_profile",
    "verify_openai_account_identity",
]
