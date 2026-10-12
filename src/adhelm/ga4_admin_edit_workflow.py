"""Configured EDIT-profile GET inventory with detached configuration and one token."""

import re
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, field_validator

from adhelm.canonical import digest
from adhelm.config import load_configuration
from adhelm.credential_setup import _validate_all_references
from adhelm.credentials import credential
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, Profile, SafeRef, StrictModel, validate_configuration
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditInventoryTarget,
    Ga4AdminEditKeyEventInventory,
    Ga4AdminEditKeyEventInventoryClient,
    revalidate_ga4_admin_edit_key_event_inventory,
)
from adhelm.transport import SafeHttpClient

_EDIT_SCOPE = "https://www.googleapis.com/auth/analytics.edit"


class Ga4AdminEditInventoryRequest(StrictModel):
    schema_version: Literal[1] = 1
    profile_id: SafeRef
    stream_id: str = Field(pattern=r"^[0-9]{1,20}$")
    ci_enabled: bool = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


def _configuration(directory: Path) -> Configuration:
    try:
        return validate_configuration(load_configuration(directory))
    except Exception:
        raise AdhelmError(
            "CONFIGURATION_INVALID", "Local configuration failed validation.", exit_code=2
        ) from None


def validate_ga4_admin_edit_inventory(
    directory: Path, profile_id: str, *, stream_id: str, ci_enabled: bool = False
) -> tuple[Configuration, Profile, Ga4AdminEditInventoryTarget]:
    """Validate the complete configured read before any private key or credential access."""
    try:
        if not isinstance(directory, Path):
            raise ValueError("root")
        request = Ga4AdminEditInventoryRequest(
            profile_id=profile_id, stream_id=stream_id, ci_enabled=ci_enabled
        )
    except Exception:
        raise AdhelmError(
            "INVALID_WORKFLOW_INPUT", "GA4 EDIT inventory input is invalid.", exit_code=2
        ) from None
    configuration = _configuration(directory)
    _validate_all_references(configuration.profiles)
    selected = [p for p in configuration.profiles if p.profile_id == request.profile_id]
    if (
        len(selected) != 1
        or selected[0].provider != "ga4_admin"
        or selected[0].purpose != "EDIT"
        or selected[0].scopes != [_EDIT_SCOPE]
    ):
        raise AdhelmError(
            "GA4_EDIT_PROFILE_REQUIRED",
            "Select a GA4 Admin EDIT profile with exactly the analytics.edit scope.",
            exit_code=2,
        )
    profile = selected[0]
    if profile.currency is None:
        raise AdhelmError(
            "GA4_EDIT_PROFILE_REQUIRED",
            "The EDIT profile requires an expected currency.",
            exit_code=2,
        )
    try:
        target = Ga4AdminEditInventoryTarget(
            property_ref=f"ga4_admin.property.{profile.account_ref}",
            property_client_ref=profile.account_ref,
            web_stream_ref=f"ga4_admin.web_stream.{request.stream_id}",
            web_stream_client_ref=request.stream_id,
            expected_timezone=profile.timezone,
            expected_currency=profile.currency,
        )
    except Exception:
        raise AdhelmError(
            "GA4_EDIT_PROFILE_REQUIRED",
            "The EDIT profile requires a native property, expected currency and timezone.",
            exit_code=2,
        ) from None
    if profile.credential_source == "EXPLICIT_CI" and request.ci_enabled is not True:
        raise AdhelmError(
            "CI_AUTH_NOT_ENABLED", "Explicit CI credential use must be enabled.", exit_code=4
        )
    return configuration, profile, target


def _require_unchanged(directory: Path, expected: str) -> None:
    if digest(_configuration(directory)) != expected:
        raise AdhelmError(
            "CONFIGURATION_DRIFT",
            "Configuration changed during the EDIT inventory read.",
            exit_code=3,
        )


def read_ga4_admin_edit_inventory(
    directory: Path,
    profile_id: str,
    *,
    stream_id: str,
    fingerprint_key: bytes,
    ci_enabled: bool = False,
    expected_configuration_digest: str | None = None,
) -> Ga4AdminEditKeyEventInventory:
    configuration, profile, target = validate_ga4_admin_edit_inventory(
        directory, profile_id, stream_id=stream_id, ci_enabled=ci_enabled
    )
    if type(fingerprint_key) is not bytes or not 32 <= len(fingerprint_key) <= 4096:
        raise AdhelmError("FINGERPRINT_KEY_REQUIRED", "A private fingerprint key is required.")
    if expected_configuration_digest is not None and (
        type(expected_configuration_digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", expected_configuration_digest) is None
    ):
        raise AdhelmError("INVALID_WORKFLOW_INPUT", "Configuration digest is invalid.", exit_code=2)
    expected = expected_configuration_digest or digest(configuration)
    if digest(configuration) != expected:
        raise AdhelmError(
            "CONFIGURATION_DRIFT", "Configuration differs from read preflight.", exit_code=3
        )
    _require_unchanged(directory, expected)
    try:
        token = credential(profile, ci_enabled=ci_enabled)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED", "GA4 Admin EDIT authorization is unavailable.", exit_code=4
        ) from None
    _require_unchanged(directory, expected)
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        reader = Ga4AdminEditKeyEventInventoryClient(
            SafeHttpClient(client, {"analyticsadmin.googleapis.com"}),
            lambda: token,
            fingerprint_key=fingerprint_key,
        )
        inventory = reader.read_inventory(target)
    _require_unchanged(directory, expected)
    inventory = revalidate_ga4_admin_edit_key_event_inventory(
        inventory, fingerprint_key=fingerprint_key
    )
    if inventory.target != target:
        raise AdhelmError("IDENTITY_MISMATCH", "EDIT inventory differs from the configured target.")
    return inventory
