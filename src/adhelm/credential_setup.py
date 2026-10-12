"""Local OS-store credential setup for already configured profiles."""

from __future__ import annotations

import getpass
import sys
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TextIO

from adhelm.credential_lock import credential_mutation_lock
from adhelm.errors import AdhelmError
from adhelm.models import (
    Configuration,
    ConfiguredProfile,
    MetaAccountTokenProfile,
    OpenAIAccountKeyProfile,
    Profile,
    is_reserved_credential_reference,
    validate_configuration,
    validate_configured_profile,
)

CredentialState = Literal["PRESENT", "MISSING", "CI_MANAGED", "LOCAL_REMOVED"]
CredentialOperation = Literal["STATUS", "STORE", "ROTATE", "LOCAL_REMOVE_ONLY"]
CredentialPrompt = Literal["Credential secret: ", "Meta USER token: ", "Meta inspector token: "]
_SERVICE = "adhelm"
_MAX_SECRET_CHARS = 16_384
_PROMPT_LABELS = frozenset({"Credential secret: ", "Meta USER token: ", "Meta inspector token: "})


class _CredentialStore(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


@dataclass(frozen=True)
class CredentialSetupResult:
    profile_id: str
    provider: str
    # Every profile has exactly one local credential slot; the field stays in the result
    # so that `auth status` and the other credential commands keep their output shape.
    slot: Literal["PRIMARY"]
    credential_ref: str | None
    state: CredentialState
    operation: CredentialOperation
    remote_revocation_verified: Literal[False] = False


def os_store_backend() -> _CredentialStore:
    """Delegate approval of the backend to the existing credential boundary."""
    from adhelm.credentials import _backend

    return _backend()


def prompt_secret_from_terminal(
    *,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
    getpass_function: Callable[..., str] = getpass.getpass,
    prompt: CredentialPrompt = "Credential secret: ",
) -> str:
    """Prompt without echo only when getpass cannot fall back to stdin."""
    if type(prompt) is not str or prompt not in _PROMPT_LABELS:
        raise AdhelmError(
            "SECRET_PROMPT_UNAVAILABLE",
            "The local hidden credential prompt is unavailable.",
            next_action="Use a supported local terminal credential prompt.",
        )
    input_stream = stdin or sys.stdin
    output_stream = stderr or sys.stderr
    if not input_stream.isatty() or not output_stream.isatty():
        raise AdhelmError(
            "INTERACTIVE_TERMINAL_REQUIRED",
            "Credential setup requires a local interactive terminal.",
            next_action="Run setup in a local terminal; do not provide a secret in chat.",
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass_function(prompt, stream=output_stream)
    except Exception:
        raise AdhelmError(
            "SECRET_PROMPT_UNAVAILABLE",
            "The local hidden credential prompt is unavailable.",
            next_action="Use a supported local terminal credential prompt.",
        ) from None
    return _validate_secret(value)


def credential_status(
    configuration: Configuration, profile_id: str
) -> tuple[CredentialSetupResult, ...]:
    """Return bounded local status without exposing credential contents."""
    validated = _validated_configuration(configuration)
    profile = _profile(validated.profiles, profile_id)
    _require_generic_credential_profile(profile)
    _validate_all_references(validated.profiles)
    if profile.credential_source == "EXPLICIT_CI":
        return (
            CredentialSetupResult(
                profile_id=profile.profile_id,
                provider=profile.provider,
                slot="PRIMARY",
                credential_ref=profile.credential_ref,
                state="CI_MANAGED",
                operation="STATUS",
            ),
        )
    if isinstance(profile, Profile) and profile.credential_source == "OS_OAUTH_GOOGLE":
        # OAuth primary state is intentionally stored under a separate fixed
        # credential-store service. Do not probe the ordinary OS-store service
        # and accidentally classify a raw generic secret as a bundle.
        from adhelm.google_oauth_credentials import google_oauth_bundle_present

        return (
            CredentialSetupResult(
                profile_id=profile.profile_id,
                provider=profile.provider,
                slot="PRIMARY",
                credential_ref=profile.credential_ref,
                state="PRESENT" if google_oauth_bundle_present(profile) else "MISSING",
                operation="STATUS",
            ),
        )
    backend = _backend_or_error()
    return (_status_for(profile, backend),)


def store_interactive(configuration: Configuration, profile_id: str) -> CredentialSetupResult:
    """Store a prompted secret without replacing an existing entry."""
    return _write_interactive(
        configuration, profile_id, prompt=prompt_secret_from_terminal, replace=False
    )


def rotate_interactive(configuration: Configuration, profile_id: str) -> CredentialSetupResult:
    """Explicitly replace one local credential after a hidden prompt."""
    return _write_interactive(
        configuration, profile_id, prompt=prompt_secret_from_terminal, replace=True
    )


def local_remove(configuration: Configuration, profile_id: str) -> CredentialSetupResult:
    """Remove only one local entry; this does not claim remote revocation."""
    validated = _validated_configuration(configuration)
    profile = _profile(validated.profiles, profile_id)
    _require_generic_credential_profile(profile)
    _validate_all_references(validated.profiles)
    if isinstance(profile, Profile) and profile.credential_source == "OS_OAUTH_GOOGLE":
        # OAuth primary state is a sealed bundle, not a generic prompt/store
        # secret.  Its source-aware removal holds the same slot lock and proves
        # deletion by exact local readback.
        from adhelm.google_oauth_credentials import remove_google_oauth_bundle

        remove_google_oauth_bundle(profile)
        return CredentialSetupResult(
            profile_id=profile.profile_id,
            provider=profile.provider,
            slot="PRIMARY",
            credential_ref=profile.credential_ref,
            state="LOCAL_REMOVED",
            operation="LOCAL_REMOVE_ONLY",
        )
    reference = _setup_reference(profile)
    with credential_mutation_lock(reference):
        backend = _backend_or_error()
        try:
            backend.delete_password(_SERVICE, reference)
        except Exception:
            pass
        _verify_removed(backend, reference)
        if isinstance(profile, OpenAIAccountKeyProfile):
            # The OpenAI account-key lifecycle keeps a separate non-secret
            # verification record derived from this profile.  Removing the key
            # while leaving that record would leave a masked identity and a
            # verification instant behind for a key this machine no longer
            # holds, and the next stored key would inherit them.  Both slots
            # go, or the removal is reported as unverified.
            from adhelm.openai_account_key_lifecycle import remove_openai_account_key_state

            remove_openai_account_key_state(profile)
        return CredentialSetupResult(
            profile_id=profile.profile_id,
            provider=profile.provider,
            slot="PRIMARY",
            credential_ref=reference,
            state="LOCAL_REMOVED",
            operation="LOCAL_REMOVE_ONLY",
        )


def _write_interactive(
    configuration: Configuration,
    profile_id: str,
    *,
    prompt: Callable[[], str],
    replace: bool,
) -> CredentialSetupResult:
    validated = _validated_configuration(configuration)
    profile = _profile(validated.profiles, profile_id)
    _require_generic_credential_profile(profile)
    _validate_all_references(validated.profiles)
    reference = _setup_reference(profile)
    with credential_mutation_lock(reference):
        backend = _backend_or_error()
        existing = _read_before_change(backend, reference)
        if existing is not None and not replace:
            raise AdhelmError(
                "CREDENTIAL_ALREADY_EXISTS",
                "A local credential already exists for this exact profile reference.",
                next_action="Use the explicit rotate operation to replace the local credential.",
            )
        value = _validate_secret(prompt())
        if _read_before_change(backend, reference) != existing:
            raise AdhelmError(
                "CREDENTIAL_CONCURRENT_CHANGE",
                "The local credential changed while waiting for hidden input.",
                next_action="Review the local credential before a deliberate new setup action.",
            )
        try:
            backend.set_password(_SERVICE, reference, value)
        except Exception:
            pass
        _verify_written(backend, reference, value)
        return CredentialSetupResult(
            profile_id=profile.profile_id,
            provider=profile.provider,
            slot="PRIMARY",
            credential_ref=reference,
            state="PRESENT",
            operation="ROTATE" if replace else "STORE",
        )


def _validated_configuration(configuration: Configuration) -> Configuration:
    try:
        return validate_configuration(configuration)
    except Exception:
        raise AdhelmError(
            "PROFILE_CONFIGURATION_INVALID",
            "Credential setup requires valid configured profiles.",
        ) from None


def _profile(profiles: Sequence[ConfiguredProfile], profile_id: str) -> ConfiguredProfile:
    matches = [profile for profile in profiles if profile.profile_id == profile_id]
    if len(matches) != 1:
        raise AdhelmError(
            "PROFILE_REQUIRED",
            "Credential setup requires one configured profile.",
            next_action="Select an existing configured profile.",
        )
    return validate_configured_profile(matches[0])


def _require_generic_credential_profile(profile: ConfiguredProfile) -> None:
    """Keep protected Meta bundles out of the generic single-secret commands."""
    if isinstance(profile, MetaAccountTokenProfile):
        raise AdhelmError(
            "META_TOKEN_CREDENTIAL_LIFECYCLE_REQUIRED",
            "Meta token bundles require the dedicated local Meta credential lifecycle.",
            exit_code=4,
            next_action=(
                "Use the dedicated `adhelm auth meta-token` commands; "
                "`adhelm auth meta-token remove-local --profile <id>` deletes the local bundle."
            ),
        )


def _setup_reference(profile: ConfiguredProfile) -> str:
    if profile.credential_source == "EXPLICIT_CI":
        raise AdhelmError(
            "CI_CREDENTIAL_SETUP_UNSUPPORTED",
            "OS-store setup is unavailable for an explicit-CI profile.",
            next_action="Configure this credential through the explicitly enabled CI environment.",
        )
    if profile.credential_source == "OS_OAUTH_GOOGLE":
        raise AdhelmError(
            "GOOGLE_OAUTH_INTERACTIVE_SETUP_UNSUPPORTED",
            "Google OAuth primary credentials require the supported local browser flow.",
            next_action="Use the installed Google OAuth enrollment command for this profile.",
        )
    return profile.credential_ref


def _validate_all_references(profiles: Sequence[ConfiguredProfile]) -> None:
    os_store_references: set[str] = set()
    for profile in profiles:
        if isinstance(profile, MetaAccountTokenProfile):
            # This closed profile validates its derived private bundle selector
            # at its model boundary.  Generic slot setup must never probe or
            # mutate that dedicated service/slot.
            continue
        reference = profile.credential_ref
        normalized_reference = reference.casefold()
        if is_reserved_credential_reference(normalized_reference):
            raise AdhelmError(
                "RESERVED_CREDENTIAL_REFERENCE",
                "The configured credential reference is reserved for internal key protection.",
            )
        if (
            profile.credential_source in {"OS_STORE", "OS_OAUTH_GOOGLE"}
            and normalized_reference in os_store_references
        ):
            raise AdhelmError(
                "CREDENTIAL_REFERENCE_ALIAS",
                "Credential references must be unique across configured profile slots.",
                next_action="Configure a distinct reference for each credential slot.",
            )
        if profile.credential_source in {"OS_STORE", "OS_OAUTH_GOOGLE"}:
            os_store_references.add(normalized_reference)


def _status_for(profile: ConfiguredProfile, backend: _CredentialStore) -> CredentialSetupResult:
    reference = profile.credential_ref
    try:
        present = bool(backend.get_password(_SERVICE, reference))
    except Exception:
        raise AdhelmError(
            "CREDENTIAL_STORE_UNAVAILABLE",
            "The OS credential store is unavailable.",
            next_action="Check the local OS credential store and retry status.",
        ) from None
    return CredentialSetupResult(
        profile_id=profile.profile_id,
        provider=profile.provider,
        slot="PRIMARY",
        credential_ref=reference,
        state="PRESENT" if present else "MISSING",
        operation="STATUS",
    )


def _backend_or_error() -> _CredentialStore:
    try:
        return os_store_backend()
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "CREDENTIAL_STORE_UNAVAILABLE",
            "The OS credential store is unavailable.",
            next_action="Configure an approved operating-system credential store.",
        ) from None


def _read_before_change(backend: _CredentialStore, reference: str) -> str | None:
    try:
        return backend.get_password(_SERVICE, reference)
    except Exception:
        raise AdhelmError(
            "CREDENTIAL_STORE_UNAVAILABLE",
            "The OS credential store is unavailable.",
            next_action="Check the local OS credential store before a deliberate follow-up.",
        ) from None


def _verify_written(backend: _CredentialStore, reference: str, expected: str) -> None:
    try:
        observed = backend.get_password(_SERVICE, reference)
    except Exception:
        raise _unverified_local_change() from None
    if observed != expected:
        raise _unverified_local_change()


def _verify_removed(backend: _CredentialStore, reference: str) -> None:
    try:
        observed = backend.get_password(_SERVICE, reference)
    except Exception:
        raise _unverified_local_change() from None
    if observed is not None:
        raise _unverified_local_change()


def _unverified_local_change() -> AdhelmError:
    return AdhelmError(
        "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED",
        "The requested local credential change could not be verified.",
        exit_code=6,
        next_action="Inspect the local credential store before any deliberate follow-up.",
    )


def _validate_secret(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_SECRET_CHARS
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise AdhelmError(
            "INVALID_CREDENTIAL_SECRET",
            "The credential secret has an unsupported local format.",
            next_action="Enter a nonempty credential without control characters.",
        )
    return value
