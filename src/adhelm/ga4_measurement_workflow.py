"""Bounded GA4 Admin read workflow with same-session identity binding.

This module deliberately performs no ledger, artifact, CLI, or remote-mutation
work.  Its caller owns the observation interval and durable measurement receipt.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field, ValidationError

from adhelm.config import load_configuration
from adhelm.credentials import credential
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, Profile, SafeRef, StrictModel
from adhelm.providers.ga4 import Ga4DataClient, Ga4Identity
from adhelm.providers.ga4_admin import Ga4AdminIdentity, Ga4AdminInventory, Ga4AdminReadClient
from adhelm.transport import SafeHttpClient

_ADMIN_HOST = "analyticsadmin.googleapis.com"
_DATA_HOST = "analyticsdata.googleapis.com"
_READONLY_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"


class Ga4AdminReadRequest(StrictModel):
    """Validated public arguments which must not cause credential access."""

    schema_version: Literal[1] = 1
    profile_id: SafeRef
    stream_id: str = Field(pattern=r"^[0-9]{1,20}$")
    ci_enabled: bool = False


def _request(
    root: Path,
    profile_id: str,
    stream_id: str,
    ci_enabled: bool,
) -> Ga4AdminReadRequest:
    if not isinstance(root, Path):
        raise AdhelmError(
            "INVALID_WORKFLOW_INPUT", "GA4 Admin workflow root is invalid.", exit_code=2
        )
    try:
        return Ga4AdminReadRequest(
            profile_id=profile_id,
            stream_id=stream_id,
            ci_enabled=ci_enabled,
        )
    except Exception as exc:
        raise AdhelmError(
            "INVALID_WORKFLOW_INPUT", "GA4 Admin workflow input is invalid.", exit_code=2
        ) from exc


def _fingerprint_key(fingerprint_key: bytes) -> None:
    if not isinstance(fingerprint_key, bytes) or not 32 <= len(fingerprint_key) <= 4_096:
        raise AdhelmError(
            "FINGERPRINT_KEY_REQUIRED",
            "GA4 Admin inventory requires a private local fingerprint key.",
            exit_code=2,
        )


def _configuration(root: Path) -> Configuration:
    """Detach and revalidate local configuration before authentication can occur."""
    try:
        configured = load_configuration(root)
        return Configuration.model_validate(configured.model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError) as exc:
        raise AdhelmError(
            "CONFIGURATION_INVALID",
            "Local configuration failed validation.",
            exit_code=2,
            next_action="Compare configuration with `adhelm schema config`.",
        ) from exc


def _selected_profile(root: Path, request: Ga4AdminReadRequest) -> Profile:
    configuration = _configuration(root)
    profiles = [
        profile for profile in configuration.profiles if profile.profile_id == request.profile_id
    ]
    if len(profiles) != 1:
        raise AdhelmError(
            "READ_PROFILE_REQUIRED",
            "An explicit matching GA4 Admin read profile is required.",
            next_action="Configure one GA4 Admin read profile using the installed schema.",
        )
    # Revalidate a detached copy before it can be used as authentication or
    # identity input.  Never let an in-memory caller alter configuration after
    # selection.
    try:
        profile = Profile.model_validate(profiles[0].model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError) as exc:
        raise AdhelmError(
            "PROFILE_INVALID",
            "The selected GA4 Admin authentication profile failed validation.",
            exit_code=2,
        ) from exc
    if profile.provider != "ga4_admin" or profile.purpose != "READ":
        raise AdhelmError(
            "READ_PROFILE_REQUIRED",
            "An explicit matching GA4 Admin read profile is required.",
            next_action="Select a GA4 Admin profile whose purpose is READ.",
        )
    if tuple(profile.scopes) != (_READONLY_SCOPE,):
        raise AdhelmError(
            "READ_SCOPE_REQUIRED",
            "GA4 Admin inventory requires exactly the configured readonly scope.",
            next_action="Configure only the GA4 readonly scope on the selected read profile.",
        )
    if re.fullmatch(r"[0-9]{1,20}", profile.account_ref) is None:
        raise AdhelmError(
            "INVALID_GA4_PROPERTY",
            "GA4 Admin profile property identity is invalid.",
            exit_code=2,
        )
    if profile.currency is None:
        raise AdhelmError(
            "EXPECTED_CURRENCY_REQUIRED",
            "GA4 Admin read profile needs an expected property currency.",
            exit_code=2,
        )
    try:
        ZoneInfo(profile.timezone)
    except ZoneInfoNotFoundError as exc:
        raise AdhelmError(
            "INVALID_PROFILE_TIMEZONE", "GA4 Admin profile timezone is invalid.", exit_code=2
        ) from exc
    return profile


def validate_ga4_admin_read(
    root: Path,
    profile_id: str,
    *,
    stream_id: str,
    ci_enabled: bool = False,
) -> Profile:
    """Validate a bounded GA4 Admin read before any key or credential access."""
    request = _request(root, profile_id, stream_id, ci_enabled)
    return _selected_profile(root, request)


def _credential_snapshot(profile: Profile, *, ci_enabled: bool) -> str:
    """Read one printable bearer credential before fixed-host egress."""
    value = credential(profile, ci_enabled=ci_enabled)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16_384
        or any(not 33 <= ord(character) <= 126 for character in value)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "GA4 Admin authorization is unavailable.",
            next_action="Authorize the configured local GA4 Admin read profile.",
        )
    return value


def _admin_identity(identity: Ga4Identity) -> Ga4AdminIdentity:
    """Convert the Data API's verified native identity to Admin references."""
    return Ga4AdminIdentity(
        property_ref=f"ga4_admin.property.{identity.property_client_ref}",
        property_client_ref=identity.property_client_ref,
        web_stream_ref=f"ga4_admin.web_stream.{identity.web_stream_client_ref}",
        web_stream_client_ref=identity.web_stream_client_ref,
    )


def read_ga4_admin(
    root: Path,
    profile_id: str,
    *,
    stream_id: str,
    fingerprint_key: bytes,
    ci_enabled: bool = False,
) -> Ga4AdminInventory:
    """Read the fixed GA4 Admin inventory for one fully bound read profile.

    Identity is read from GA4's v1beta Admin property/data-stream endpoints in
    the same ``httpx.Client`` session used for the v1alpha inventory.  The
    v1alpha reader receives a closure over that observed identity, never a
    declaration-only or unconditional verifier.
    """
    _fingerprint_key(fingerprint_key)
    profile = validate_ga4_admin_read(
        root,
        profile_id,
        stream_id=stream_id,
        ci_enabled=ci_enabled,
    )
    credential_value = _credential_snapshot(profile, ci_enabled=ci_enabled)

    def token_getter() -> str:
        # Identity discovery and every fixed inventory page must run under the
        # same credential snapshot. A mid-session credential reread could bind
        # discovery to one principal and inventory to another.
        return credential_value

    with httpx.Client(trust_env=False) as client:
        transport = SafeHttpClient(client, {_DATA_HOST, _ADMIN_HOST})
        data = Ga4DataClient(transport, token_getter)
        observed = data.discover_identity(profile.account_ref, expected_web_stream_id=stream_id)
        if observed.timezone != profile.timezone or observed.currency != profile.currency:
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "GA4 property settings differ from the selected read profile.",
            )
        expected_identity = _admin_identity(observed)

        def session_verifier(candidate: Ga4AdminIdentity) -> bool:
            # Compare every native and canonical identity component to the
            # result actually observed by this client session.  The timezone
            # and currency checks above bind the profile to that same result.
            return candidate == expected_identity

        admin = Ga4AdminReadClient(
            transport,
            token_getter,
            session_verifier,
            fingerprint_key=fingerprint_key,
        )
        return admin.inventory(identity=expected_identity)
