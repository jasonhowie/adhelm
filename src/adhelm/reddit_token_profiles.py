"""Closed non-secret Reddit Ads OAuth contract, scope allowlist, and slot selectors.

This module is deliberately free of credential-store, HTTP, browser and CLI
dependencies so the profile validator and the research contract stay reviewable
on their own.  It adds no persisted model: a Reddit Ads profile is the existing
``Profile`` with ``provider="reddit_ads"`` and ``credential_source="OS_STORE"``.
The bearer that adapter reads stays in that profile's ordinary OS-store slot;
the permanent refresh token and the non-secret lifecycle state live in separate
derived slots under a private credential-store service.  The developer
application Reddit issues is a confidential client, so, as for LinkedIn, the
application secret has its own slot there.
"""

from __future__ import annotations

import re
from typing import Final, Literal

from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import (
    REDDIT_TOKEN_CREDENTIAL_REFERENCE_PREFIX,
    Configuration,
    Profile,
    validate_configured_profile,
)
from adhelm.oauth_enrollment import OAuthProviderContract
from adhelm.protected_token_references import derive_token_reference
from adhelm.providers.reddit_ads import REDDIT_ADS_RESOURCE_ID, REDDIT_ADS_USER_AGENT

REDDIT_OAUTH_HOST: Final = "www.reddit.com"
REDDIT_OAUTH_AUTHORIZATION_ENDPOINT: Final = "https://www.reddit.com/api/v1/authorize"
REDDIT_OAUTH_TOKEN_ENDPOINT: Final = "https://www.reddit.com/api/v1/access_token"

# Documented Ads API v3 scopes. See docs/research/reddit-oauth-contract.md for
# the pinned sources.  `adsconversions` is deliberately absent: no conversion
# capability exists in a read-only build, so no profile may request it.
REDDIT_ADS_READ_SCOPE: Final = "adsread"
REDDIT_ADS_EDIT_SCOPE: Final = "adsedit"
REDDIT_ADS_SUPPORTED_SCOPES: Final[frozenset[str]] = frozenset(
    {REDDIT_ADS_READ_SCOPE, REDDIT_ADS_EDIT_SCOPE}
)
# A READ profile may never request the write permission, even explicitly.
REDDIT_ADS_SCOPES_BY_PURPOSE: Final[dict[str, frozenset[str]]] = {
    "READ": frozenset({REDDIT_ADS_READ_SCOPE}),
    "EDIT": REDDIT_ADS_SUPPORTED_SCOPES,
}

# Reddit access tokens last one hour.  The bound is an upper limit for a
# response we will accept, not a promise about any particular grant.  A
# permanent refresh token is documented to carry no expiry, so no refresh
# lifetime is expected; the generic ceiling only bounds an unexpected value.
_ACCESS_TOKEN_MAX_SECONDS: Final = 86_400

REDDIT_OAUTH_CONTRACT: Final = OAuthProviderContract(
    provider="reddit_ads",
    authorization_endpoint=REDDIT_OAUTH_AUTHORIZATION_ENDPOINT,
    token_endpoint=REDDIT_OAUTH_TOKEN_ENDPOINT,
    supported_scopes=REDDIT_ADS_SUPPORTED_SCOPES,
    # Reddit's authorization-code flow documents no PKCE parameter.  Do not send
    # an undocumented parameter.
    pkce_method="NONE",
    refresh_grant_documented=True,
    # `duration=permanent` is what makes Reddit return a refresh token at all;
    # the default `temporary` duration returns an access token only.
    authorization_parameters=(("duration", "permanent"),),
    scope_request_separator=" ",
    scope_response_separators=(" ",),
    maximum_access_token_lifetime_seconds=_ACCESS_TOKEN_MAX_SECONDS,
    # Reddit's authenticate page shows the token exchange as
    # `-u '<app ID>:<secret>'`: the app ID is the HTTP Basic user name and the
    # app secret is the password.
    client_authentication="BASIC_SECRET",
    # Reddit rejects or throttles requests without a descriptive user agent,
    # including at its OAuth endpoints; it matches the Ads adapter's agent.
    user_agent=REDDIT_ADS_USER_AGENT,
)

REDDIT_TOKEN_SERVICE: Final = "adhelm.reddit-token-v1"
REDDIT_TOKEN_REFERENCE_PREFIX: Final = REDDIT_TOKEN_CREDENTIAL_REFERENCE_PREFIX
_CLIENT_SECRET_DOMAIN: Final = b"adhelm/reddit/token/client-secret-reference/v1\x00"
_REFRESH_DOMAIN: Final = b"adhelm/reddit/token/refresh-reference/v1\x00"
_STATE_DOMAIN: Final = b"adhelm/reddit/token/state-reference/v1\x00"
REDDIT_ACCOUNT_ID: Final = re.compile(REDDIT_ADS_RESOURCE_ID)

RedditTokenSlot = Literal["CLIENT_SECRET", "REFRESH_TOKEN", "STATE"]

_SLOT_DOMAINS: Final[dict[str, tuple[bytes, str]]] = {
    "CLIENT_SECRET": (_CLIENT_SECRET_DOMAIN, "client-secret"),
    "REFRESH_TOKEN": (_REFRESH_DOMAIN, "refresh"),
    "STATE": (_STATE_DOMAIN, "state"),
}


def reddit_token_reference(profile_id: object, slot: RedditTokenSlot) -> str:
    """Derive one protected Reddit slot selector for a profile."""
    return derive_token_reference(
        label="Reddit",
        code_prefix="REDDIT_TOKEN",
        reference_prefix=REDDIT_TOKEN_REFERENCE_PREFIX,
        slot_domains=_SLOT_DOMAINS,
        profile_id=profile_id,
        slot=slot,
    )


def is_reddit_token_reference(value: object) -> bool:
    """Return whether a reference selects internal Reddit token state."""
    return type(value) is str and value.startswith(REDDIT_TOKEN_REFERENCE_PREFIX)


def reddit_ads_scope_selection(
    purpose: str, requested: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    """Resolve the exact requested scope set from the closed per-purpose allowlist.

    ``None`` selects the documented least-privilege read scope.  An explicit
    selection must stay inside this purpose's allowlist and must include
    ``adsread``, which every implemented read path needs; a READ profile can
    never request ``adsedit``.
    """
    allowed = REDDIT_ADS_SCOPES_BY_PURPOSE.get(purpose)
    if allowed is None:
        raise AdhelmError(
            "REDDIT_TOKEN_PURPOSE_UNSUPPORTED",
            "Reddit Ads local enrollment supports only READ and EDIT profiles.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action="Configure a Reddit Ads READ or EDIT profile before enrolling.",
        )
    if requested is None:
        selection: tuple[str, ...] = (REDDIT_ADS_READ_SCOPE,)
    else:
        if (
            type(requested) is not tuple
            or not 1 <= len(requested) <= len(REDDIT_ADS_SUPPORTED_SCOPES)
            or any(type(scope) is not str for scope in requested)
            or len(set(requested)) != len(requested)
        ):
            raise _scope_rejected()
        selection = requested
    if not set(selection) <= allowed or REDDIT_ADS_READ_SCOPE not in selection:
        raise _scope_rejected()
    return tuple(sorted(selection))


def _scope_rejected() -> AdhelmError:
    return AdhelmError(
        "REDDIT_TOKEN_SCOPE_REJECTED",
        "The requested Reddit scopes are outside this profile's allowlist.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action=(
            "Request adsread for a READ profile; adsedit requires an EDIT profile and is "
            "never needed for reading."
        ),
    )


def select_reddit_ads_profile(configuration: object, profile_id: object) -> Profile:
    """Select exactly one configured Reddit Ads OS-store profile."""
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
        or checked.provider != "reddit_ads"
        or checked.credential_source != "OS_STORE"
        or checked.purpose not in REDDIT_ADS_SCOPES_BY_PURPOSE
        or is_reddit_token_reference(checked.credential_ref)
        # The declared profile scopes are the local contract this lifecycle
        # honors; an undocumented permission string is rejected before any
        # prompt, browser handoff, or credential-store access.
        or not set(checked.scopes) <= REDDIT_ADS_SCOPES_BY_PURPOSE[checked.purpose]
    ):
        raise _profile_required()
    return checked


def require_scope_selection_covers_profile(profile: Profile, selection: tuple[str, ...]) -> None:
    """Refuse a selection narrower than the profile's own declared permissions.

    Enrolling below the declared contract would leave the profile advertising a
    permission its stored grant does not carry.
    """
    if not set(profile.scopes) <= set(selection):
        raise _scope_rejected()


def checked_account_ref(profile: Profile) -> str:
    """Return the profile's Reddit ad account ID after the adapter's own ID check."""
    if REDDIT_ACCOUNT_ID.fullmatch(profile.account_ref) is None:
        raise AdhelmError(
            "REDDIT_TOKEN_ACCOUNT_REF_INVALID",
            "The Reddit profile account reference is not a Reddit ad account ID.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action=(
                "Set the profile account_ref to the Reddit ad account ID before enrolling."
            ),
        )
    return profile.account_ref


def _profile_required() -> AdhelmError:
    return AdhelmError(
        "REDDIT_TOKEN_PROFILE_REQUIRED",
        # Not "stores its bearer ...": the shared privacy screen reads that phrase as
        # a leaked bearer credential and would turn this diagnostic into a crash.
        "Select one configured Reddit Ads profile that stores its access token locally.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action=(
            "Add a reddit_ads OS_STORE profile with `config add-profile` before enrolling."
        ),
    )
