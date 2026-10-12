"""Bounded OpenAI Ads conversion-setting read workflow.

This library-only composition intentionally performs no ledger append, CLI
registration, artifact creation, or remote mutation.  Its caller owns any
observation interval and durable measurement receipt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field, ValidationError, model_validator

from adhelm.config import load_configuration
from adhelm.credentials import credential
from adhelm.errors import AdhelmError
from adhelm.models import (
    Configuration,
    MetaAccountTokenProfile,
    OpenAIAccountKeyProfile,
    Profile,
    SafeRef,
    StrictModel,
    validate_configuration,
    validate_configured_profile,
)
from adhelm.providers.openai_ads import OpenAIAdsClient, OpenAIAdsIdentity
from adhelm.providers.openai_conversions import (
    OpenAIAdsConversionSetting,
    OpenAIAdsConversionSettingsAvailability,
    OpenAIAdsConversionSettingsClient,
    OpenAIAdsConversionSettingsQuery,
)
from adhelm.transport import SafeHttpClient

_HOST = "api.ads.openai.com"
_MAX_PAGES = 10
OperationalOpenAIProfile = Profile | OpenAIAccountKeyProfile


class OpenAIConversionsReadRequest(StrictModel):
    """Validated public input which cannot reach credentials or HTTP on failure."""

    schema_version: Literal[1] = 1
    profile_id: SafeRef
    maximum_records: int = Field(default=500, ge=1, le=5_000)
    page_size: int = Field(default=500, ge=1, le=500)
    ci_enabled: bool = False

    @model_validator(mode="after")
    def bounded_pages(self) -> OpenAIConversionsReadRequest:
        if self.maximum_records > self.page_size * _MAX_PAGES:
            raise ValueError("OpenAI Ads conversion inventory exceeds ten pages")
        return self


def _request(
    root: Path,
    profile_id: str,
    maximum_records: int,
    page_size: int,
    ci_enabled: bool,
) -> OpenAIConversionsReadRequest:
    if not isinstance(root, Path):
        raise AdhelmError(
            "INVALID_WORKFLOW_INPUT",
            "OpenAI Ads measurement workflow root is invalid.",
            exit_code=2,
        )
    try:
        return OpenAIConversionsReadRequest(
            profile_id=profile_id,
            maximum_records=maximum_records,
            page_size=page_size,
            ci_enabled=ci_enabled,
        )
    except ValidationError as exc:
        raise AdhelmError(
            "INVALID_WORKFLOW_INPUT",
            "OpenAI Ads measurement workflow input is invalid.",
            exit_code=2,
        ) from exc


def _configuration(root: Path) -> Configuration:
    """Revalidate a detached configuration before authentication can occur."""

    try:
        configured = load_configuration(root)
        return validate_configuration(configured)
    except (AttributeError, TypeError, ValidationError, ValueError) as exc:
        raise AdhelmError(
            "CONFIGURATION_INVALID",
            "Local configuration failed validation.",
            exit_code=2,
            next_action="Compare configuration with `adhelm schema config`.",
        ) from exc


def _selected_profile(
    root: Path, request: OpenAIConversionsReadRequest
) -> OperationalOpenAIProfile:
    configuration = _configuration(root)
    selected = [
        profile
        for profile in configuration.profiles
        if profile.profile_id == request.profile_id
        and profile.provider == "openai_ads"
        and profile.purpose == "READ"
    ]
    if len(selected) != 1:
        raise AdhelmError(
            "READ_PROFILE_REQUIRED",
            "An explicit matching OpenAI Ads read profile is required.",
            next_action="Configure the selected OpenAI Ads provider with a READ profile.",
        )
    try:
        profile = validate_configured_profile(selected[0])
    except (AttributeError, TypeError, ValidationError) as exc:
        raise AdhelmError(
            "PROFILE_INVALID",
            "The selected OpenAI Ads authentication profile failed validation.",
            exit_code=2,
        ) from exc
    if isinstance(profile, MetaAccountTokenProfile):
        raise AdhelmError(
            "META_TOKEN_PROFILE_INSPECTION_ONLY",
            "This Meta token profile supports only the dedicated local token inspection.",
            exit_code=4,
            next_action="Configure an OpenAI Ads read profile for this measurement workflow.",
        )
    if (
        not isinstance(profile.account_ref, str)
        or not 1 <= len(profile.account_ref) <= 120
        or not all(
            character.isascii() and (character.isalnum() or character in "_-")
            for character in profile.account_ref
        )
    ):
        raise AdhelmError(
            "INVALID_OPENAI_ACCOUNT",
            "OpenAI Ads read profile account identity is invalid.",
            exit_code=2,
        )
    if profile.currency is None:
        raise AdhelmError(
            "EXPECTED_CURRENCY_REQUIRED",
            "OpenAI Ads read profile needs an expected account currency.",
            exit_code=2,
        )
    try:
        ZoneInfo(profile.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AdhelmError(
            "INVALID_PROFILE_TIMEZONE",
            "OpenAI Ads read profile timezone is invalid.",
            exit_code=2,
        ) from exc
    return profile


def _credential_snapshot(profile: OperationalOpenAIProfile, *, ci_enabled: bool) -> str:
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
            "OpenAI Ads authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local OpenAI Ads read profile.",
        )
    return value


def _verify_identity(profile: OperationalOpenAIProfile, identity: OpenAIAdsIdentity) -> None:
    """Bind the account returned in this session before conversion inventory."""

    if identity.timezone != profile.timezone or identity.currency != profile.currency:
        raise AdhelmError(
            "IDENTITY_MISMATCH",
            "OpenAI Ads account settings differ from the selected read profile.",
        )


def read_openai_conversion_availability(
    root: Path,
    profile_id: str,
    *,
    maximum_records: int = 500,
    page_size: int = 500,
    ci_enabled: bool = False,
) -> tuple[OpenAIAdsIdentity, OpenAIAdsConversionSettingsAvailability]:
    """Read the account-enabled conversion surface through its typed outcome.

    The conversion-settings route is current-account scoped.  The workflow
    therefore discovers the account first in the same HTTP session and passes
    the discovery client's exact-object verifier to the conversion reader.
    Failure on a later page raises an error rather than exposing a partial
    result.

    The read goes through
    :meth:`OpenAIAdsConversionSettingsClient.read_event_settings`, so a `403`
    or a `404` from this one fixed route is returned as the typed
    `NOT_ENABLED_FOR_ACCOUNT` fact about the ad account instead of the
    authorization diagnostic `list_event_settings` would raise.  A `401`, a
    rate limit, a malformed response, and a pagination bound still propagate
    unchanged: only the two statuses the adapter classifies are a feature fact.
    """

    request = _request(root, profile_id, maximum_records, page_size, ci_enabled)
    profile = _selected_profile(root, request)
    credential_value = _credential_snapshot(profile, ci_enabled=request.ci_enabled)

    def token_getter() -> str:
        # Identity discovery and every fixed conversion page must use the same
        # credential. A local rotation during this session must not bind the
        # current-account discovery to one bearer and inventory to another.
        return credential_value

    with httpx.Client(trust_env=False) as client:
        transport = SafeHttpClient(client, {_HOST})
        ads = OpenAIAdsClient(transport, token_getter)
        identity = ads.discover_identity(expected_account_client_ref=profile.account_ref)
        _verify_identity(profile, identity)
        conversions = OpenAIAdsConversionSettingsClient(
            transport,
            token_getter,
            identity_session_verifier=ads.is_current_identity,
        )
        availability = conversions.read_event_settings(
            OpenAIAdsConversionSettingsQuery(
                account_ref=identity.account_ref,
                maximum_records=request.maximum_records,
                page_size=request.page_size,
            ),
            identity=identity,
        )
    return identity, availability


def read_openai_conversions(
    root: Path,
    profile_id: str,
    *,
    maximum_records: int = 500,
    page_size: int = 500,
    ci_enabled: bool = False,
) -> tuple[OpenAIAdsIdentity, tuple[OpenAIAdsConversionSetting, ...]]:
    """Read complete bounded conversion-setting inventory for one account.

    This is the settings-only projection of
    :func:`read_openai_conversion_availability`.  An account the surface is not
    enabled for has no settings to return and no empty inventory to retain, so
    it is refused with the typed feature diagnostic rather than reported as an
    account with zero conversion settings.
    """
    identity, availability = read_openai_conversion_availability(
        root,
        profile_id,
        maximum_records=maximum_records,
        page_size=page_size,
        ci_enabled=ci_enabled,
    )
    if availability.status == "NOT_ENABLED_FOR_ACCOUNT":
        raise AdhelmError(
            "OPENAI_ADS_CONVERSIONS_NOT_ENABLED_FOR_ACCOUNT",
            "This OpenAI ad account does not expose the conversion-settings surface.",
            exit_code=3,
            next_action=(
                "Enable conversion setup for this ad account in OpenAI Ads Manager, or read a "
                "different account; the stored key was not refused."
            ),
        )
    return identity, availability.settings
