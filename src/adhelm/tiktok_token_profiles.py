"""Closed non-secret TikTok Ads app auth-code contract and protected slot selectors.

This module is deliberately free of credential-store, HTTP and CLI dependencies
so the profile validator and the research contract stay reviewable on their own.
It adds no persisted model: a TikTok Ads profile is the existing ``Profile`` with
``provider="tiktok_ads"`` and ``credential_source="OS_STORE"``.  The access token
the read adapter uses stays in that profile's ordinary OS-store slot; the
application secret, the granted advertiser ID list, and the non-secret lifecycle
state live in separate derived slots under a private credential-store service.

Everything here is bounded by what
``docs/research/tiktok-oauth-contract.md`` could actually fetch on 2026-09-09.
TikTok's reference portal renders through JavaScript, so the wire contract comes
from the TikTok-maintained SDK: a JSON body of exactly ``app_id``, ``secret`` and
``auth_code``, the ``{code,message,request_id,data}`` envelope, and **no**
advertiser refresh route at all.  The RFC 6749 form exchange in
:mod:`adhelm.oauth_enrollment` therefore cannot carry this flow; its non-wire
seams are reused instead.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final, Literal

from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import (
    TIKTOK_TOKEN_CREDENTIAL_REFERENCE_PREFIX,
    Configuration,
    Profile,
    validate_configured_profile,
)
from adhelm.providers.tiktok_ads import TIKTOK_ADS_API_HOST, TIKTOK_ADS_API_VERSION

TikTokTokenEnvironment = Literal["PRODUCTION", "SANDBOX"]

# The one proven base host.  `python_sdk/business_api_client/configuration.py`
# in the TikTok-maintained SDK sets `self.host = "https://business-api.tiktok.com"`
# (fetched 2026-09-09).  It is deliberately the same constant the fixed read
# adapter pins, so the enrollment and the reads it enables can never drift onto
# two different hosts.
TIKTOK_OAUTH_HOST: Final = TIKTOK_ADS_API_HOST
TIKTOK_OAUTH_TOKEN_PATH: Final = f"/open_api/{TIKTOK_ADS_API_VERSION}/oauth2/access_token/"

# A sandbox base URL is named only by third-party write-ups; no first-party page
# this review could fetch confirms one.  The environment is therefore a declared
# fact with a closed host map, and the undocumented member is refused rather
# than pointed at a guessed host.
TIKTOK_TOKEN_ENVIRONMENT_HOSTS: Final[dict[str, str]] = {"PRODUCTION": TIKTOK_OAUTH_HOST}
TIKTOK_TOKEN_ENVIRONMENTS: Final[tuple[TikTokTokenEnvironment, ...]] = ("PRODUCTION", "SANDBOX")

# No first-party source documents an expiry for the advertiser access token.
# The SDK's "24 hours" note is about the creator/TikTok-account token obtained
# through a different route, so an absent lifetime is missing information here,
# never "no expiry".
TIKTOK_ACCESS_TOKEN_EXPIRY_NOT_DOCUMENTED: Final = "NOT_DOCUMENTED_LOCALLY"
TIKTOK_ACCESS_TOKEN_EXPIRY_PROVIDER_DECLARED: Final = "PROVIDER_DECLARED"
# An upper bound this build will accept for a declared lifetime, so a hostile or
# mistaken value cannot push an expiry beyond any plausible grant.  One year.
TIKTOK_MAXIMUM_ACCESS_TOKEN_LIFETIME_SECONDS: Final = 31_536_000

# Reserved namespace for the derived TikTok slot selectors.  The "#" separator
# cannot occur in a configured `SafeRef`, so a customer-chosen credential
# reference can never collide with one of these.  The prefix has exactly one
# declaration: `adhelm.models` owns it and `is_reserved_credential_reference`
# reserves that same object, so the reservation and the derived selectors below
# cannot drift apart.  `adhelm.linkedin_token_profiles` re-exports its own prefix
# the same way.
TIKTOK_TOKEN_SERVICE: Final = "adhelm.tiktok-token-v1"
TIKTOK_TOKEN_REFERENCE_PREFIX: Final = TIKTOK_TOKEN_CREDENTIAL_REFERENCE_PREFIX

_APP_SECRET_DOMAIN: Final = b"adhelm/tiktok/token/app-secret-reference/v1\x00"
_ADVERTISERS_DOMAIN: Final = b"adhelm/tiktok/token/advertisers-reference/v1\x00"
_STATE_DOMAIN: Final = b"adhelm/tiktok/token/state-reference/v1\x00"
_PROFILE_ID: Final = re.compile(r"^[a-zA-Z0-9_.:/-]{1,160}$")
_ADVERTISER_ID: Final = re.compile(r"^[1-9][0-9]{0,39}$")
_APP_ID: Final = re.compile(r"^[\x21-\x7e]{1,200}$")

TikTokTokenSlot = Literal["APP_SECRET", "ADVERTISER_IDS", "STATE"]
_SLOT_DOMAINS: Final[dict[str, tuple[bytes, str]]] = {
    "APP_SECRET": (_APP_SECRET_DOMAIN, "app-secret"),
    "ADVERTISER_IDS": (_ADVERTISERS_DOMAIN, "advertisers"),
    "STATE": (_STATE_DOMAIN, "state"),
}
_SUPPORTED_PURPOSES: Final[frozenset[str]] = frozenset({"READ", "EDIT"})


def tiktok_token_reference(profile_id: object, slot: TikTokTokenSlot) -> str:
    """Derive one protected TikTok slot selector for a profile.

    The profile ID is configuration data; the resulting reference is an internal
    credential-store selector that is never accepted from a caller.
    """
    if type(profile_id) is not str or _PROFILE_ID.fullmatch(profile_id) is None:
        raise AdhelmError(
            "TIKTOK_TOKEN_PROFILE_REQUIRED",
            "The TikTok token profile ID is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action="Select one configured TikTok Ads profile.",
        )
    if slot not in _SLOT_DOMAINS:
        raise AdhelmError(
            "TIKTOK_TOKEN_SLOT_INVALID",
            "The requested TikTok token slot is not supported.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    domain, label = _SLOT_DOMAINS[slot]
    suffix = hashlib.sha256(domain + profile_id.encode("ascii")).hexdigest()
    return f"{TIKTOK_TOKEN_REFERENCE_PREFIX}{label}#{suffix}"


def is_tiktok_token_reference(value: object) -> bool:
    """Return whether a reference selects internal TikTok token state."""
    return type(value) is str and value.startswith(TIKTOK_TOKEN_REFERENCE_PREFIX)


def tiktok_environment_host(environment: object) -> str:
    """Resolve the declared environment to its one proven host, or refuse.

    A base URL is never guessed.  `SANDBOX` is a real TikTok environment and a
    real declared value, but no first-party page this review could fetch
    publishes its host, so selecting it is an explicit refusal rather than a
    request to an unproven domain.
    """
    if environment not in TIKTOK_TOKEN_ENVIRONMENTS:
        raise AdhelmError(
            "TIKTOK_TOKEN_ENVIRONMENT_INVALID",
            "TikTok enrollment requires an explicitly declared API environment.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action="Declare --environment PRODUCTION; the environment is never inferred.",
        )
    host = TIKTOK_TOKEN_ENVIRONMENT_HOSTS.get(str(environment))
    if host is None:
        raise AdhelmError(
            "TIKTOK_TOKEN_ENVIRONMENT_NOT_DOCUMENTED",
            "The TikTok sandbox base URL is not documented locally.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Enroll against PRODUCTION. TikTok's reference portal renders through "
                "JavaScript and no first-party page this build could fetch publishes the "
                "sandbox host, so it is refused rather than guessed."
            ),
        )
    return host


def checked_advertiser_id(value: object) -> str:
    """Validate one decimal TikTok advertiser identifier."""
    if type(value) is not str or _ADVERTISER_ID.fullmatch(value) is None:
        raise AdhelmError(
            "TIKTOK_TOKEN_ADVERTISER_ID_INVALID",
            "A TikTok advertiser identifier must be a bounded decimal value.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action="Set the profile account_ref to the decimal TikTok advertiser ID.",
        )
    return value


def checked_app_ref(value: object) -> str:
    """Validate one entered TikTok application identifier."""
    if type(value) is not str or _APP_ID.fullmatch(value) is None:
        raise AdhelmError(
            "TIKTOK_TOKEN_APP_ID_INVALID",
            "The entered TikTok application ID is not a usable application identifier.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action=(
                "Re-run enrollment and enter the App ID exactly as shown on the application's "
                "detail page in TikTok's My Apps console."
            ),
        )
    return value


def select_tiktok_ads_profile(configuration: object, profile_id: object) -> Profile:
    """Select exactly one configured TikTok Ads OS-store profile.

    The profile's declared ``scopes`` are a local label only.  TikTok publishes
    no permission vocabulary this build could fetch, so no scope string here is
    treated as, compared with, or recorded as a provider permission.
    """
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
        or checked.provider != "tiktok_ads"
        or checked.credential_source != "OS_STORE"
        or checked.purpose not in _SUPPORTED_PURPOSES
        or is_tiktok_token_reference(checked.credential_ref)
    ):
        raise _profile_required()
    checked_advertiser_id(checked.account_ref)
    return checked


def _profile_required() -> AdhelmError:
    return AdhelmError(
        "TIKTOK_TOKEN_PROFILE_REQUIRED",
        "Select one configured TikTok Ads profile that stores its access token locally.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action=(
            "Add a tiktok_ads OS_STORE READ or EDIT profile whose account_ref is the decimal "
            "advertiser ID with `config add-profile` before enrolling."
        ),
    )


__all__ = [
    "TIKTOK_ACCESS_TOKEN_EXPIRY_NOT_DOCUMENTED",
    "TIKTOK_ACCESS_TOKEN_EXPIRY_PROVIDER_DECLARED",
    "TIKTOK_MAXIMUM_ACCESS_TOKEN_LIFETIME_SECONDS",
    "TIKTOK_OAUTH_HOST",
    "TIKTOK_OAUTH_TOKEN_PATH",
    "TIKTOK_TOKEN_CREDENTIAL_REFERENCE_PREFIX",
    "TIKTOK_TOKEN_ENVIRONMENTS",
    "TIKTOK_TOKEN_ENVIRONMENT_HOSTS",
    "TIKTOK_TOKEN_REFERENCE_PREFIX",
    "TIKTOK_TOKEN_SERVICE",
    "TikTokTokenEnvironment",
    "TikTokTokenSlot",
    "checked_advertiser_id",
    "checked_app_ref",
    "is_tiktok_token_reference",
    "select_tiktok_ads_profile",
    "tiktok_environment_host",
    "tiktok_token_reference",
]
