"""Closed non-secret LinkedIn Ads OAuth contract, scope allowlist, and slot selectors.

This module is deliberately free of credential-store, HTTP, browser and CLI
dependencies so the profile validator and the research contract stay reviewable
on their own.  It adds no persisted model: a LinkedIn Ads profile is the
existing ``Profile`` with ``provider="linkedin_ads"`` and
``credential_source="OS_STORE"``.  The bearer that adapter reads stays in that
profile's ordinary OS-store slot; the client secret, refresh token, and
non-secret lifecycle state live in separate derived slots under a private
credential-store service.
"""

from __future__ import annotations

from typing import Final, Literal

from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import (
    LINKEDIN_TOKEN_CREDENTIAL_REFERENCE_PREFIX,
    Configuration,
    Profile,
    validate_configured_profile,
)
from adhelm.oauth_enrollment import OAuthProviderContract
from adhelm.protected_token_references import derive_token_reference

LINKEDIN_OAUTH_HOST: Final = "www.linkedin.com"
LINKEDIN_OAUTH_AUTHORIZATION_ENDPOINT: Final = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_OAUTH_TOKEN_ENDPOINT: Final = "https://www.linkedin.com/oauth/v2/accessToken"

# Documented Advertising API member permissions. See
# docs/research/linkedin-oauth-contract.md for the pinned sources.
LINKEDIN_ADS_READ_SCOPES: Final[tuple[str, ...]] = ("r_ads", "r_ads_reporting")
LINKEDIN_ADS_EDIT_SCOPE: Final = "rw_ads"
LINKEDIN_ADS_SUPPORTED_SCOPES: Final[frozenset[str]] = frozenset(
    {*LINKEDIN_ADS_READ_SCOPES, LINKEDIN_ADS_EDIT_SCOPE}
)
# A READ profile may never request the write permission, even explicitly.
LINKEDIN_ADS_SCOPES_BY_PURPOSE: Final[dict[str, frozenset[str]]] = {
    "READ": frozenset(LINKEDIN_ADS_READ_SCOPES),
    "EDIT": LINKEDIN_ADS_SUPPORTED_SCOPES,
}

# LinkedIn documents a 60-day access token and a 365-day programmatic refresh
# token. Both bounds are upper limits for a response we will accept, not a
# promise about any particular application's grant.
_ACCESS_TOKEN_MAX_SECONDS: Final = 5_184_000
_REFRESH_TOKEN_MAX_SECONDS: Final = 31_536_000

LINKEDIN_OAUTH_CONTRACT: Final = OAuthProviderContract(
    provider="linkedin_ads",
    authorization_endpoint=LINKEDIN_OAUTH_AUTHORIZATION_ENDPOINT,
    token_endpoint=LINKEDIN_OAUTH_TOKEN_ENDPOINT,
    supported_scopes=LINKEDIN_ADS_SUPPORTED_SCOPES,
    # LinkedIn's 3-legged OAuth reference documents no PKCE parameter for the
    # authorization request. Do not send an undocumented parameter.
    pkce_method="NONE",
    refresh_grant_documented=True,
    scope_request_separator=" ",
    # The authorization-code page documents a space-delimited `scope`, while the
    # introspection page documents a comma-separated list. Accept both forms
    # rather than guessing which one a live token response uses.
    scope_response_separators=(" ", ","),
    maximum_access_token_lifetime_seconds=_ACCESS_TOKEN_MAX_SECONDS,
    maximum_refresh_token_lifetime_seconds=_REFRESH_TOKEN_MAX_SECONDS,
)

LINKEDIN_TOKEN_SERVICE: Final = "adhelm.linkedin-token-v1"
LINKEDIN_TOKEN_REFERENCE_PREFIX: Final = LINKEDIN_TOKEN_CREDENTIAL_REFERENCE_PREFIX
_CLIENT_SECRET_DOMAIN: Final = b"adhelm/linkedin/token/client-secret-reference/v1\x00"
_REFRESH_DOMAIN: Final = b"adhelm/linkedin/token/refresh-reference/v1\x00"
_STATE_DOMAIN: Final = b"adhelm/linkedin/token/state-reference/v1\x00"

LinkedInTokenSlot = Literal["CLIENT_SECRET", "REFRESH_TOKEN", "STATE"]

_SLOT_DOMAINS: Final[dict[str, tuple[bytes, str]]] = {
    "CLIENT_SECRET": (_CLIENT_SECRET_DOMAIN, "client-secret"),
    "REFRESH_TOKEN": (_REFRESH_DOMAIN, "refresh"),
    "STATE": (_STATE_DOMAIN, "state"),
}


def linkedin_token_reference(profile_id: object, slot: LinkedInTokenSlot) -> str:
    """Derive one protected LinkedIn slot selector for a profile."""
    return derive_token_reference(
        label="LinkedIn",
        code_prefix="LINKEDIN_TOKEN",
        reference_prefix=LINKEDIN_TOKEN_REFERENCE_PREFIX,
        slot_domains=_SLOT_DOMAINS,
        profile_id=profile_id,
        slot=slot,
    )


def is_linkedin_token_reference(value: object) -> bool:
    """Return whether a reference selects internal LinkedIn token state."""
    return type(value) is str and value.startswith(LINKEDIN_TOKEN_REFERENCE_PREFIX)


def linkedin_ads_scope_selection(
    purpose: str, requested: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    """Resolve the exact requested scope set from the closed per-purpose allowlist.

    ``None`` selects the documented least-privilege read pair.  An explicit
    selection must stay inside this purpose's allowlist; a READ profile can
    never request ``rw_ads``.
    """
    allowed = LINKEDIN_ADS_SCOPES_BY_PURPOSE.get(purpose)
    if allowed is None:
        raise AdhelmError(
            "LINKEDIN_TOKEN_PURPOSE_UNSUPPORTED",
            "LinkedIn Ads local enrollment supports only READ and EDIT profiles.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action="Configure a LinkedIn Ads READ or EDIT profile before enrolling.",
        )
    if requested is None:
        selection = LINKEDIN_ADS_READ_SCOPES
    else:
        if (
            type(requested) is not tuple
            or not 1 <= len(requested) <= len(LINKEDIN_ADS_SUPPORTED_SCOPES)
            or any(type(scope) is not str for scope in requested)
            or len(set(requested)) != len(requested)
        ):
            raise _scope_rejected()
        selection = requested
    if not set(selection) <= allowed:
        raise _scope_rejected()
    if not set(LINKEDIN_ADS_READ_SCOPES) <= set(selection):
        # Account identity and reporting are both required by the implemented
        # read paths; a partial selection would fail later at the provider.
        raise _scope_rejected()
    return tuple(sorted(selection))


def _scope_rejected() -> AdhelmError:
    return AdhelmError(
        "LINKEDIN_TOKEN_SCOPE_REJECTED",
        "The requested LinkedIn scopes are outside this profile's allowlist.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action=(
            "Request r_ads and r_ads_reporting for a READ profile; rw_ads requires an "
            "EDIT profile and a qualifying ad-account role."
        ),
    )


def select_linkedin_ads_profile(configuration: object, profile_id: object) -> Profile:
    """Select exactly one configured LinkedIn Ads OS-store profile."""
    if type(configuration) is not Configuration or type(profile_id) is not str:
        raise _profile_required()
    matches = [profile for profile in configuration.profiles if profile.profile_id == profile_id]
    if len(matches) != 1:
        raise _profile_required()
    try:
        checked = validate_configured_profile(matches[0])
    except Exception:
        raise _profile_required() from None
    if (
        not isinstance(checked, Profile)
        or checked.provider != "linkedin_ads"
        or checked.credential_source != "OS_STORE"
        or checked.purpose not in LINKEDIN_ADS_SCOPES_BY_PURPOSE
        or is_linkedin_token_reference(checked.credential_ref)
        # The declared profile scopes are the local contract this lifecycle
        # honors; an undocumented permission string is rejected before any
        # prompt, browser handoff, or credential-store access.
        or not set(checked.scopes) <= LINKEDIN_ADS_SCOPES_BY_PURPOSE[checked.purpose]
    ):
        raise _profile_required()
    return checked


def require_scope_selection_covers_profile(profile: Profile, selection: tuple[str, ...]) -> None:
    """Refuse a selection narrower than the profile's own declared permissions.

    LinkedIn invalidates previously issued access tokens when the requested
    scope set changes, so enrolling below the declared contract would silently
    break the profile's documented read paths.
    """
    if not set(profile.scopes) <= set(selection):
        raise _scope_rejected()


def _profile_required() -> AdhelmError:
    return AdhelmError(
        "LINKEDIN_TOKEN_PROFILE_REQUIRED",
        "Select one configured LinkedIn Ads profile that stores its bearer locally.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action=(
            "Add a linkedin_ads OS_STORE profile with `config add-profile` before enrolling."
        ),
    )
