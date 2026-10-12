"""Explicit paired GA4 resource-read diagnostics; no execution or grant surface."""

from __future__ import annotations

import hmac
import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field, field_validator

from adhelm.canonical import digest
from adhelm.config import load_configuration
from adhelm.credential_setup import _validate_all_references
from adhelm.credentials import credential
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE
from adhelm.models import Configuration, Profile, SafeRef, StrictModel, validate_configuration
from adhelm.providers.ga4_admin_access import (
    Ga4AdminAccessClient,
    Ga4AdminAccessIdentity,
    Ga4ServiceLevel,
)
from adhelm.transport import SafeHttpClient


class Ga4AdminAccessRequest(StrictModel):
    schema_version: Literal[1] = 1
    read_profile_id: SafeRef
    edit_profile_id: SafeRef
    stream_id: str = Field(pattern=r"^[0-9]{1,20}$")
    ci_enabled: bool = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


class Ga4AdminAccessResult(StrictModel):
    provider: Literal["ga4_admin"] = "ga4_admin"
    profile_separation: Literal["VERIFIED_LOCALLY"] = "VERIFIED_LOCALLY"
    read_profile_resource_access: Literal["VERIFIED"] = "VERIFIED"
    edit_profile_resource_access: Literal["VERIFIED"] = "VERIFIED"
    observed_service_level: Ga4ServiceLevel
    granted_scopes: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    create_permission: Literal["NOT_PREPROVABLE"] = "NOT_PREPROVABLE"
    key_event_inventory: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    mutation_readiness: Literal["NOT_EVIDENCED"] = "NOT_EVIDENCED"
    observations_atomic: Literal[False] = False
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False
    persisted: Literal[False] = False


def _configuration(directory: Path) -> Configuration:
    try:
        return validate_configuration(load_configuration(directory))
    except Exception:
        raise AdhelmError(
            "CONFIGURATION_INVALID", "Local configuration failed validation.", exit_code=2
        ) from None


def validate_ga4_admin_access(
    directory: Path, request: Ga4AdminAccessRequest
) -> tuple[Configuration, Profile, Profile]:
    """Check both roles together before any credential or provider access."""
    try:
        if not isinstance(directory, Path):
            raise ValueError("root")
        request = Ga4AdminAccessRequest.model_validate(request.model_dump(mode="python"))
    except Exception:
        raise AdhelmError(
            "INVALID_WORKFLOW_INPUT", "GA4 access-check input is invalid.", exit_code=2
        ) from None
    configuration = _configuration(directory)
    _validate_all_references(configuration.profiles)
    if request.read_profile_id.casefold() == request.edit_profile_id.casefold():
        raise AdhelmError(
            "GA4_DISTINCT_PROFILES_REQUIRED",
            "GA4 access checks require separate READ and EDIT profiles.",
            exit_code=2,
        )
    selected: list[Profile] = []
    for profile_id, purpose in (
        (request.read_profile_id, "READ"),
        (request.edit_profile_id, "EDIT"),
    ):
        matches = [p for p in configuration.profiles if p.profile_id == profile_id]
        if (
            len(matches) != 1
            or not isinstance(matches[0], Profile)
            or matches[0].provider != "ga4_admin"
            or matches[0].purpose != purpose
            or matches[0].scopes
            != [GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("ga4_admin", purpose)]]
        ):
            raise AdhelmError(
                "GA4_PROFILE_PAIR_REQUIRED",
                "Configure distinct GA4 Admin READ and EDIT profiles with their exact scopes.",
                exit_code=2,
            )
        profile = matches[0]
        if re.fullmatch(r"[0-9]{1,20}", profile.account_ref) is None or profile.currency is None:
            raise AdhelmError(
                "GA4_PROFILE_PAIR_REQUIRED",
                "Both GA4 profiles need a native property, expected currency and timezone.",
                exit_code=2,
            )
        try:
            ZoneInfo(profile.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise AdhelmError(
                "GA4_PROFILE_PAIR_REQUIRED", "GA4 profile timezone is invalid.", exit_code=2
            ) from None
        if profile.credential_source == "EXPLICIT_CI" and request.ci_enabled is not True:
            raise AdhelmError(
                "CI_AUTH_NOT_ENABLED",
                "Explicit CI credential use must be enabled for this check.",
                exit_code=4,
            )
        selected.append(profile)
    read_profile, edit_profile = selected
    if (
        read_profile.account_ref != edit_profile.account_ref
        or read_profile.currency != edit_profile.currency
        or read_profile.timezone != edit_profile.timezone
    ):
        raise AdhelmError(
            "GA4_PROFILE_PAIR_MISMATCH",
            "GA4 profiles must name the same property, currency and timezone.",
            exit_code=2,
        )
    if read_profile.credential_ref.casefold() == edit_profile.credential_ref.casefold():
        raise AdhelmError(
            "GA4_DISTINCT_CREDENTIALS_REQUIRED",
            "GA4 READ and EDIT require distinct credential references.",
            exit_code=2,
        )
    return configuration, read_profile, edit_profile


def _require_unchanged(directory: Path, expected: str) -> None:
    if digest(_configuration(directory)) != expected:
        raise AdhelmError(
            "CONFIGURATION_DRIFT", "Configuration changed during the GA4 access check.", exit_code=3
        )


def _token(profile: Profile, *, ci_enabled: bool) -> str:
    value = credential(profile, ci_enabled=ci_enabled)
    if (
        type(value) is not str
        or not 1 <= len(value) <= 16_384
        or any(not 33 <= ord(char) <= 126 for char in value)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED", "GA4 authorization is unavailable.", exit_code=4
        )
    return value


def check_ga4_admin_access(directory: Path, request: Ga4AdminAccessRequest) -> Ga4AdminAccessResult:
    """Read both fixed targets without asserting scopes, create permission or readiness."""
    configuration, read_profile, edit_profile = validate_ga4_admin_access(directory, request)
    request = Ga4AdminAccessRequest.model_validate(request.model_dump(mode="python"))
    expected_configuration = digest(configuration)
    first_token: str | None = None
    observed: list[Ga4AdminAccessIdentity] = []
    for profile in (read_profile, edit_profile):
        _require_unchanged(directory, expected_configuration)
        token = _token(profile, ci_enabled=request.ci_enabled)
        if first_token is None:
            first_token = token
        elif hmac.compare_digest(first_token, token):
            raise AdhelmError(
                "GA4_DISTINCT_CREDENTIALS_REQUIRED",
                "GA4 READ and EDIT resolved to the same credential.",
                exit_code=4,
            )
        _require_unchanged(directory, expected_configuration)

        def token_getter(value: str = token) -> str:
            return value

        try:
            with httpx.Client(trust_env=False, follow_redirects=False) as client:
                transport = SafeHttpClient(client, {"analyticsadmin.googleapis.com"})
                identity = Ga4AdminAccessClient(transport, token_getter).read_identity(
                    property_id=profile.account_ref, stream_id=request.stream_id
                )
        except AdhelmError as error:
            # Role-specific context is closed application text; never reflect a
            # profile ID, provider response, URL, credential, or OIDC subject.
            raise AdhelmError(
                error.code,
                f"GA4 {profile.purpose} profile resource access could not be verified.",
                exit_code=error.exit_code,
                next_action="Check the selected role's access, expected property and web stream.",
            ) from None
        if identity.timezone != profile.timezone or identity.currency != profile.currency:
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                f"GA4 {profile.purpose} property settings differ from configuration.",
                exit_code=3,
            )
        observed.append(identity)
    _require_unchanged(directory, expected_configuration)
    if observed[0] != observed[1]:
        raise AdhelmError(
            "GA4_PROFILE_OBSERVATION_MISMATCH",
            "GA4 role observations differ; repeat diagnostics after resolving drift.",
            exit_code=3,
        )
    return Ga4AdminAccessResult(observed_service_level=observed[0].service_level)
