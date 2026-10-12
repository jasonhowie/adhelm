"""Explicit OS credential-store boundary, never a plaintext fallback."""

import hmac
import os
import secrets

import keyring
from pydantic import ValidationError

from adhelm.credential_lock import credential_mutation_lock
from adhelm.errors import AdhelmError
from adhelm.models import (
    EXPORT_PSEUDONYMIZATION_REFERENCE,
    OPENAI_PAUSE_BINDING_KEY_REFERENCE,
    ConfiguredProfile,
    MetaAccountTokenProfile,
    OpenAIAccountKeyProfile,
    Profile,
    validate_configured_profile,
)

_BACKENDS = frozenset(
    {
        "keyring.backends.macOS",
        "keyring.backends.Windows",
        "keyring.backends.SecretService",
        "keyring.backends.libsecret",
    }
)
_SERVICE = "adhelm"
_EXPORT_KEY_REFERENCE = EXPORT_PSEUDONYMIZATION_REFERENCE
_OPENAI_PAUSE_BINDING_KEY_REFERENCE = OPENAI_PAUSE_BINDING_KEY_REFERENCE
_PRIVATE_KEY_BYTES = 32
_PRIVATE_KEY_HEX_CHARS = _PRIVATE_KEY_BYTES * 2


def _backend() -> keyring.backend.KeyringBackend:
    backend = keyring.get_keyring()
    if type(backend).__module__ not in _BACKENDS:
        raise AdhelmError(
            "OS_CREDENTIAL_STORE_REQUIRED",
            "An approved operating-system store is needed.",
            next_action="Configure the OS credential store; plaintext stores are refused.",
        )
    return backend


def credential(
    profile: ConfiguredProfile, *, ci_enabled: bool = False, secondary: bool = False
) -> str:
    try:
        profile = validate_configured_profile(profile)
    except (ValidationError, ValueError) as exc:
        raise AdhelmError(
            "PROFILE_INVALID",
            "The selected authentication profile failed validation.",
            exit_code=2,
        ) from exc
    if isinstance(profile, MetaAccountTokenProfile):
        raise AdhelmError(
            "META_TOKEN_CREDENTIAL_LIFECYCLE_REQUIRED",
            "Meta token bundles require the dedicated local Meta credential lifecycle.",
            exit_code=4,
            next_action="Use the dedicated Meta token setup before a deliberate inspection.",
        )
    if secondary:
        if not isinstance(profile, Profile):
            raise AdhelmError(
                "SECONDARY_CREDENTIAL_UNSUPPORTED",
                "Only Google Ads supports a secondary credential.",
            )
        reference = profile.secondary_credential_ref
    else:
        reference = profile.credential_ref
    if reference is None:
        raise AdhelmError(
            "SECONDARY_CREDENTIAL_REQUIRED",
            "A separate developer-token reference is required.",
            exit_code=4,
        )
    if profile.credential_source == "OS_OAUTH_GOOGLE" and not secondary:
        # The protected OAuth bundle is private local state, never a bearer
        # token itself.  Resolve it only after detached Profile validation.
        from adhelm.google_oauth_credentials import resolve_google_oauth_access_token

        if not isinstance(profile, Profile):
            raise AdhelmError(
                "GOOGLE_OAUTH_PROFILE_REQUIRED",
                "Google OAuth requires its configured scoped profile.",
                exit_code=2,
            )
        return resolve_google_oauth_access_token(profile)
    try:
        if profile.credential_source == "EXPLICIT_CI":
            if not ci_enabled or os.environ.get("CI", "").lower() not in {"true", "1"}:
                raise AdhelmError(
                    "CI_OPT_IN_REQUIRED", "Environment credentials require explicit CI."
                )
            value = os.environ.get(reference)
        else:
            value = _backend().get_password(_SERVICE, reference)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "CREDENTIAL_STORE_UNAVAILABLE", "The OS credential store is unavailable."
        ) from None
    if not isinstance(value, str) or not value:
        raise AdhelmError(
            "AUTHENTICATION_REQUIRED",
            "Profile has no usable locally stored credential.",
            exit_code=4,
            next_action="Complete the supported provider authentication locally.",
        )
    return value


def export_key() -> bytes:
    """Create/reuse the private OS key for requested local fingerprinting and sharing."""
    try:
        with credential_mutation_lock(_EXPORT_KEY_REFERENCE):
            backend = _backend()
            value = backend.get_password(_SERVICE, _EXPORT_KEY_REFERENCE)
            if value is None:
                value = secrets.token_hex(32)
                backend.set_password(_SERVICE, _EXPORT_KEY_REFERENCE, value)
                observed = backend.get_password(_SERVICE, _EXPORT_KEY_REFERENCE)
                # Do not use a generated key until the approved store proves that
                # exact value is now durable. Otherwise this one export can acquire
                # opaque references that a later local export cannot reproduce.
                if observed != value:
                    raise ValueError("Export key write was not retained")
                value = observed
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError("Invalid export key")
        return bytes.fromhex(value)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "EXPORT_KEY_UNAVAILABLE", "Private export key is unavailable in the OS store."
        ) from None


def openai_pause_binding_key(
    profile: OpenAIAccountKeyProfile, *, ci_enabled: bool = False
) -> bytes:
    """Return the separate local key used to bind a future OpenAI pause plan.

    This is deliberately distinct from :func:`export_key`: the latter only
    seals normalized inventory and pseudonymizes local projections in separate
    HMAC domains, while this key is input to the
    domain-separated credential-binding HMAC in the pause adapter.
    """
    # A provider credential may use the explicitly enabled CI environment, but
    # this internal binding key has no separately configured CI lifecycle.
    # Never probe ambient environment variables as a substitute.
    del ci_enabled
    try:
        validated = validate_configured_profile(profile)
    except Exception:
        raise AdhelmError(
            "PROFILE_INVALID", "The selected authentication profile failed validation.", exit_code=2
        ) from None
    if not isinstance(validated, OpenAIAccountKeyProfile):
        raise AdhelmError(
            "OPENAI_PAUSE_ACCOUNT_KEY_PROFILE_REQUIRED",
            "OpenAI pause planning requires an OpenAI account-key profile.",
            exit_code=2,
        )
    if validated.credential_source == "EXPLICIT_CI":
        raise AdhelmError(
            "OPENAI_PAUSE_BINDING_KEY_CI_UNSUPPORTED",
            "OpenAI pause planning has no configured CI lifecycle for its private binding key.",
            exit_code=4,
            next_action="Use an OS-store OpenAI account-key profile for future pause planning.",
        )
    return _openai_pause_binding_key_from_store()


def _openai_pause_binding_key_from_store() -> bytes:
    """Create or read the fixed binding-key slot with exact readback proof."""
    try:
        with credential_mutation_lock(_OPENAI_PAUSE_BINDING_KEY_REFERENCE):
            backend = _backend()
            value = backend.get_password(_SERVICE, _OPENAI_PAUSE_BINDING_KEY_REFERENCE)
            if value is None:
                generated = secrets.token_hex(_PRIVATE_KEY_BYTES)
                _private_key_bytes(generated)
                backend.set_password(_SERVICE, _OPENAI_PAUSE_BINDING_KEY_REFERENCE, generated)
                observed = backend.get_password(_SERVICE, _OPENAI_PAUSE_BINDING_KEY_REFERENCE)
                if type(observed) is not str or not hmac.compare_digest(observed, generated):
                    raise ValueError("OpenAI pause binding key write was not retained")
                value = observed
        return _private_key_bytes(value)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "OPENAI_PAUSE_BINDING_KEY_UNAVAILABLE",
            "Private OpenAI pause credential-binding key is unavailable in the OS store.",
        ) from None


def _private_key_bytes(value: object) -> bytes:
    """Decode one exact 32-byte private key without exposing its stored value."""
    if type(value) is not str or len(value) != _PRIVATE_KEY_HEX_CHARS:
        raise ValueError("Invalid private key")
    decoded = bytes.fromhex(value)
    if len(decoded) != _PRIVATE_KEY_BYTES:
        raise ValueError("Invalid private key")
    return decoded
