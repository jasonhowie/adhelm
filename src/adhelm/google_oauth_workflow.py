"""Explicit local Google enrollment/reconnection with no advertising operations."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal, assert_never

from adhelm.canonical import digest
from adhelm.config import configuration_transaction, load_configuration
from adhelm.credential_setup import _validate_all_references
from adhelm.errors import AdhelmError
from adhelm.google_oauth_browser import GoogleOAuthLoopbackReceiver, open_system_browser
from adhelm.google_oauth_protocol import (
    GoogleOAuthInitialGrant,
    GoogleOAuthTarget,
    complete_google_oauth_authorization,
    consume_google_oauth_callback,
    revalidate_google_oauth_initial_grant,
    start_google_oauth_authorization,
)
from adhelm.google_oauth_setup import (
    GoogleInstalledClient,
    GoogleOAuthProfileDraft,
    read_google_installed_client,
    read_google_oauth_draft,
)
from adhelm.google_oauth_transport import FixedGoogleOAuthTransport
from adhelm.models import Configuration, Profile, validate_configuration


def _require_interactive(interactive: bool) -> None:
    if interactive is not True:
        raise AdhelmError(
            "EXPLICIT_BROWSER_HANDOFF_REQUIRED",
            "Google enrollment requires an explicitly requested local browser handoff.",
            exit_code=4,
            next_action=(
                "Run locally with --interactive; keep client files and secrets out of chat."
            ),
        )
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise AdhelmError(
            "INTERACTIVE_TERMINAL_REQUIRED",
            "Google enrollment requires a local interactive terminal and desktop browser.",
            exit_code=4,
        )


def _configuration(directory: Path) -> Configuration:
    try:
        return validate_configuration(load_configuration(directory))
    except Exception:
        raise AdhelmError(
            "CONFIGURATION_INVALID", "Local configuration failed validation.", exit_code=2
        ) from None


def _check_new_profile(configuration: Configuration, draft: GoogleOAuthProfileDraft) -> None:
    _validate_all_references(configuration.profiles)
    if len(configuration.profiles) >= 100 or any(
        profile.profile_id == draft.profile_id for profile in configuration.profiles
    ):
        raise AdhelmError(
            "GOOGLE_OAUTH_PROFILE_EXISTS",
            "The requested profile cannot be added to the current configuration.",
            exit_code=2,
            next_action="Use reconnect for an existing Google OAuth profile or select a new ID.",
        )
    references = {draft.credential_ref.casefold()}
    for profile in configuration.profiles:
        if profile.credential_source not in {"OS_STORE", "OS_OAUTH_GOOGLE"}:
            continue
        existing = {profile.credential_ref.casefold()}
        if existing & references:
            raise AdhelmError(
                "CREDENTIAL_REFERENCE_ALIAS",
                "Google enrollment requires distinct local credential references.",
                exit_code=2,
            )


def _browser_grant(
    target: GoogleOAuthTarget, client: GoogleInstalledClient
) -> GoogleOAuthInitialGrant:
    with GoogleOAuthLoopbackReceiver() as receiver:
        attempt = start_google_oauth_authorization(target, receiver.redirect_uri)
        open_system_browser(attempt)
        raw_query = receiver.receive_query()
        receiver.require_unexpired()
        callback = consume_google_oauth_callback(attempt, raw_query)
        with FixedGoogleOAuthTransport.open() as transport:
            receiver.require_unexpired()
            grant = complete_google_oauth_authorization(
                attempt,
                callback,
                token_transport=transport,
                certificate_transport=transport,
                client_secret=client.client_secret,
            )
    checked = revalidate_google_oauth_initial_grant(grant)
    if checked.target != target:
        raise AdhelmError(
            "GOOGLE_OAUTH_TARGET_MISMATCH",
            "The authorization grant differs from the selected client and scope.",
            exit_code=4,
        )
    return checked


def _result(profile: Profile, *, operation: str) -> dict[str, object]:
    return {
        "provider": profile.provider,
        "purpose": profile.purpose,
        "operation": operation,
        "profile_configuration_digest": digest(profile),
        "credential_slot": "PRIMARY",
        "credential_state": "READY",
        "oidc_identity_verification": "VERIFIED_DURING_THIS_AUTHORIZATION",
        "resource_access_verified": False,
        "execution_authority": False,
    }


# The closed provider set, identical to the `provider` fields in
# `google_oauth_credentials` and `google_oauth_cli`.
GoogleOAuthProvider = Literal["google_ads", "ga4", "ga4_admin", "gtm"]

# The first two lines and the local-only lines were checked against Google's
# published account help on 2026-09-09; the Tag Manager permissions line against
# Tag Manager's user-permissions help on 2026-10-10.  Sources:
#   https://support.google.com/accounts/answer/13533235
#   https://support.google.com/tagmanager/answer/6107011  (user permissions)
#   https://developers.google.com/tag-platform/tag-manager/api/v2/limits-quotas
#     (fetched 2026-10-10, page last updated 2026-07-16; quota and the 403)
# Local removal is deliberately described separately: it is never remote revocation.
_PROVIDER_SIDE_GUIDANCE: tuple[str, ...] = (
    "Provider-side: Google Account -> Security -> Your connections to third-party apps & "
    "services, open the app you enrolled, then remove its access.",
    "Provider-side reference: https://support.google.com/accounts/answer/13533235",
)
# Google retired developer tokens on 2026-09-09; access now belongs to the Cloud
# project that owns the OAuth client.  Source:
#   https://developers.google.com/google-ads/api/docs/api-policy/developer-token
#   (fetched 2026-10-12, page last updated 2026-10-07)
_CLOUD_PROJECT_ACCESS_GUIDANCE = (
    "Provider-side: Google Ads API access belongs to the Google Cloud project that owns the "
    "OAuth client, not to this grant. Revoking the grant leaves the project's access level in "
    "place, and the project's access level is managed on its Google Ads API Overview page."
)
_GTM_PERMISSION_GUIDANCE = (
    "Provider-side: Tag Manager account and container access is granted to the Google user in "
    "Tag Manager Admin -> User management, not by this OAuth grant. Revoking the grant leaves "
    "those permissions in place, and removing them does not revoke the grant."
)
_LOCAL_GUIDANCE: tuple[str, ...] = (
    "Local-only: `adhelm auth remove-local --profile <id>` deletes the stored local credential "
    "entry for this profile.",
    "Local removal never revokes the grant at Google and is never evidence that it was revoked; "
    "removing a shared application's access can also interrupt other uses of it.",
)


def google_oauth_revocation_guidance(provider: GoogleOAuthProvider) -> tuple[str, ...]:
    """Return the revocation guidance that applies to one Google provider profile.

    The match is exhaustive over the closed provider set: a provider added to
    `GoogleOAuthProvider` is a type error here until it is given a decision,
    even if that decision is to add nothing.
    """
    extra: tuple[str, ...]
    match provider:
        case "google_ads":
            extra = (_CLOUD_PROJECT_ACCESS_GUIDANCE,)
        case "gtm":
            extra = (_GTM_PERMISSION_GUIDANCE,)
        case "ga4" | "ga4_admin":
            extra = ()
        case _:
            assert_never(provider)
    return (*_PROVIDER_SIDE_GUIDANCE, *extra, *_LOCAL_GUIDANCE)


def google_oauth_status(directory: Path, profile_id: str) -> object:
    """Return bounded local Google OAuth status; this makes no Google request."""
    from adhelm.google_oauth_credentials import google_oauth_local_status

    return google_oauth_local_status(_configuration(directory), profile_id)


def enroll_google_oauth(
    directory: Path, input_file: Path, client_file: Path, *, interactive: bool = False
) -> dict[str, object]:
    """Derive a final subject-bound profile only after verified browser authorization."""
    from adhelm.google_oauth_credentials import (
        install_google_oauth_grant,
        prepare_google_oauth_slot,
    )

    _require_interactive(interactive)
    draft = read_google_oauth_draft(input_file)
    configuration = _configuration(directory)
    _check_new_profile(configuration, draft)
    expected_configuration = digest(configuration)
    client = read_google_installed_client(client_file, expected_client_ref=draft.client_ref)
    target = GoogleOAuthTarget(client_ref=draft.client_ref, api_scope=draft.scopes[0])
    expected_slot = prepare_google_oauth_slot(draft.credential_ref, replace=False)
    grant = _browser_grant(target, client)
    profile = Profile.model_validate(
        {
            **draft.model_dump(mode="python", warnings="error"),
            "principal_ref": grant.identity.principal_ref,
            "credential_source": "OS_OAUTH_GOOGLE",
        }
    )
    with configuration_transaction(directory) as transaction:
        current = transaction.require_unchanged(expected_configuration)
        _check_new_profile(current, draft)
        updated = Configuration.model_validate(
            {**current.model_dump(mode="python"), "profiles": [*current.profiles, profile]}
        )
        # Save only the final nonsecret profile first. A following credential
        # failure leaves a recoverable configured-but-unusable profile, never a
        # credential silently detached from its declared principal/configuration.
        try:
            transaction.save(updated, replace=True)
            transaction.require_unchanged(digest(updated))
            install_google_oauth_grant(
                updated,
                profile.profile_id,
                grant,
                client_secret=client.client_secret,
                replace=False,
                expected_slot=expected_slot,
            )
            transaction.require_unchanged(digest(updated))
        except Exception:
            raise AdhelmError(
                "GOOGLE_OAUTH_ENROLLMENT_LOCAL_STATE_UNVERIFIED",
                "Authorization succeeded, but final local enrollment could not be verified.",
                exit_code=6,
                next_action=(
                    "Inspect local config and auth status, then reconnect the configured profile "
                    "deliberately. Do not automatically repeat enrollment."
                ),
            ) from None
    return _result(profile, operation="ENROLL")


def reconnect_google_oauth(
    directory: Path, profile_id: str, client_file: Path, *, interactive: bool = False
) -> dict[str, object]:
    """Replace one protected grant only for the same configured principal/client/scope."""
    from adhelm.google_oauth_credentials import (
        install_google_oauth_grant,
        prepare_google_oauth_install,
    )

    _require_interactive(interactive)
    configuration = _configuration(directory)
    selected = [p for p in configuration.profiles if p.profile_id == profile_id]
    if (
        len(selected) != 1
        or not isinstance(selected[0], Profile)
        or selected[0].credential_source != "OS_OAUTH_GOOGLE"
    ):
        raise AdhelmError(
            "GOOGLE_OAUTH_PROFILE_REQUIRED",
            "Reconnect requires one configured Google OAuth profile.",
            exit_code=2,
        )
    profile = selected[0]
    client = read_google_installed_client(client_file, expected_client_ref=profile.client_ref)
    target = GoogleOAuthTarget(client_ref=profile.client_ref, api_scope=profile.scopes[0])
    expected_slot = prepare_google_oauth_install(configuration, profile_id, replace=True)
    grant = _browser_grant(target, client)
    if grant.identity.principal_ref != profile.principal_ref:
        raise AdhelmError(
            "GOOGLE_OAUTH_PRINCIPAL_MISMATCH",
            "The authorized Google subject differs from the existing profile.",
            exit_code=4,
            next_action="Reconnect using the original Google subject or enroll a separate profile.",
        )
    with configuration_transaction(directory) as transaction:
        current = transaction.require_unchanged(digest(configuration))
        install_google_oauth_grant(
            current,
            profile.profile_id,
            grant,
            client_secret=client.client_secret,
            replace=True,
            expected_slot=expected_slot,
        )
        transaction.require_unchanged(digest(current))
    return _result(profile, operation="RECONNECT")
