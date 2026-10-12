"""Bounded GTM Web inventory composition with same-session identity proof.

This library-only workflow performs no ledger append, CLI registration,
artifact work, workspace operation, or remote mutation.  Its caller owns any
observation interval and durable measurement receipt.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, ValidationError

from adhelm.config import load_configuration
from adhelm.credentials import credential
from adhelm.errors import AdhelmError
from adhelm.gtm_inventory import GtmFullInventory, GtmInventoryClient
from adhelm.models import Configuration, Profile, SafeRef, StrictModel
from adhelm.providers.gtm_identity import GtmIdentityClient, GtmIdentityDiscovery
from adhelm.providers.gtm_read import GtmReadClient, GtmReadInventory
from adhelm.transport import SafeHttpClient

_HOST = "tagmanager.googleapis.com"
_READONLY_SCOPE = "https://www.googleapis.com/auth/tagmanager.readonly"
_ReferenceKind = Literal["account", "container", "workspace", "container_version"]


class GtmReadRequest(StrictModel):
    """Validated public input which cannot reach credentials or HTTP on failure."""

    schema_version: Literal[1] = 1
    profile_id: SafeRef
    container_id: str = Field(pattern=r"^[0-9]{1,20}$")
    ci_enabled: bool = False
    # Opt-in discriminator.  An input that omits it routes byte-identically to
    # the existing fixed version/workspace-metadata read.
    full_inventory: bool = False


def _request(
    root: Path,
    profile_id: str,
    container_id: str,
    ci_enabled: bool,
    full_inventory: bool = False,
) -> GtmReadRequest:
    if not isinstance(root, Path):
        raise AdhelmError(
            "INVALID_WORKFLOW_INPUT", "GTM read workflow root is invalid.", exit_code=2
        )
    try:
        return GtmReadRequest(
            profile_id=profile_id,
            container_id=container_id,
            ci_enabled=ci_enabled,
            full_inventory=full_inventory,
        )
    except (AdhelmError, TypeError, ValidationError, ValueError):
        raise AdhelmError(
            "INVALID_WORKFLOW_INPUT", "GTM read workflow input is invalid.", exit_code=2
        ) from None


def _fingerprint_key(fingerprint_key: bytes) -> None:
    if type(fingerprint_key) is not bytes or len(fingerprint_key) != 32:
        raise AdhelmError(
            "FINGERPRINT_KEY_REQUIRED",
            "GTM inventory requires a private local fingerprint key.",
            exit_code=2,
        )


def _configuration(root: Path) -> Configuration:
    """Detach and revalidate local configuration before authentication can occur."""

    try:
        configured = load_configuration(root)
        return Configuration.model_validate(configured.model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError):
        raise AdhelmError(
            "CONFIGURATION_INVALID",
            "Local configuration failed validation.",
            exit_code=2,
            next_action="Compare configuration with `adhelm schema config`.",
        ) from None


def _selected_profile(root: Path, request: GtmReadRequest) -> Profile:
    configuration = _configuration(root)
    selected = [
        profile for profile in configuration.profiles if profile.profile_id == request.profile_id
    ]
    if not selected:
        raise AdhelmError(
            "NOT_CONFIGURED_OPTIONAL",
            "The optional GTM Web read profile is not configured.",
            next_action="Configure a GTM Web READ profile when this customer uses GTM.",
        )
    if len(selected) != 1:
        raise AdhelmError(
            "READ_PROFILE_REQUIRED",
            "An explicit matching GTM Web read profile is required.",
            next_action="Configure one GTM Web profile whose purpose is READ.",
        )
    try:
        profile = Profile.model_validate(selected[0].model_dump(mode="python"))
    except (AttributeError, TypeError, ValidationError):
        raise AdhelmError(
            "PROFILE_INVALID",
            "The selected GTM Web authentication profile failed validation.",
            exit_code=2,
        ) from None
    if profile.provider != "gtm" or profile.purpose != "READ":
        raise AdhelmError(
            "READ_PROFILE_REQUIRED",
            "An explicit matching GTM Web read profile is required.",
            next_action="Select a GTM profile whose purpose is READ.",
        )
    if tuple(profile.scopes) != (_READONLY_SCOPE,):
        raise AdhelmError(
            "READ_SCOPE_REQUIRED",
            "GTM inventory requires exactly the configured readonly scope.",
            next_action="Configure only the GTM readonly scope on the selected read profile.",
        )
    if re.fullmatch(r"[0-9]{1,20}", profile.account_ref) is None:
        raise AdhelmError(
            "INVALID_GTM_ACCOUNT",
            "GTM read profile account identity is invalid.",
            exit_code=2,
        )
    return profile


def _scoped_reference_for(account: str, container: str) -> Callable[[_ReferenceKind, str], str]:
    """Create ledger-safe references for GTM's parent-scoped native IDs."""

    def reference_for(kind: _ReferenceKind, value: str) -> str:
        if kind == "account":
            if value != account:
                raise ValueError("GTM account reference is outside the configured identity")
            return f"gtm.account.{account}"
        if kind == "container":
            if value != container:
                raise ValueError("GTM container reference is outside the configured identity")
            return f"gtm.container.{account}.{container}"
        if kind == "workspace":
            return f"gtm.workspace.{account}.{container}.{value}"
        if kind == "container_version":
            return f"gtm.container_version.{account}.{container}.{value}"
        raise ValueError("Unsupported GTM reference kind")

    return reference_for


def validate_gtm_read(
    root: Path,
    profile_id: str,
    *,
    container_id: str,
    ci_enabled: bool = False,
) -> Profile:
    """Validate a GTM Web read before fingerprint-key or credential access."""

    request = _request(root, profile_id, container_id, ci_enabled)
    return _selected_profile(root, request)


def _credential_snapshot(profile: Profile, *, ci_enabled: bool) -> str:
    """Read one printable bearer credential before fixed-host egress."""

    try:
        value = credential(profile, ci_enabled=ci_enabled)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "GTM authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local GTM read profile.",
        ) from None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16_384
        or any(not character.isascii() or not 33 <= ord(character) <= 126 for character in value)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "GTM authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local GTM read profile.",
        )
    return value


def read_gtm(
    root: Path,
    profile_id: str,
    *,
    container_id: str,
    fingerprint_key: bytes,
    ci_enabled: bool = False,
) -> tuple[GtmIdentityDiscovery, GtmReadInventory]:
    """Read a bounded GTM Web inventory under one observed identity session.

    The profile's raw numeric account identifier and supplied numeric container
    identifier are first proven by the fixed account/container reads.  Only the
    identity client that observed them can vouch for the exact identity object
    passed to the inventory reader.  Any identity or later inventory failure
    raises before this function exposes a result.
    """

    request = _request(root, profile_id, container_id, ci_enabled)
    _fingerprint_key(fingerprint_key)
    profile = _selected_profile(root, request)
    reference_for = _scoped_reference_for(profile.account_ref, request.container_id)
    credential_value = _credential_snapshot(profile, ci_enabled=request.ci_enabled)

    def token_getter() -> str:
        # The discovery and every inventory page must use the same local
        # credential snapshot.  Rereading during a session could bind the
        # identity and inventory to different principals.
        return credential_value

    with httpx.Client(trust_env=False) as client:
        transport = SafeHttpClient(client, {_HOST})
        identity_client = GtmIdentityClient(transport, token_getter, reference_for=reference_for)
        discovery = identity_client.discover_identity(profile.account_ref, request.container_id)
        reader = GtmReadClient(
            transport,
            token_getter,
            identity_verifier=identity_client.is_current_identity,
            fingerprint_key=fingerprint_key,
            reference_for=reference_for,
        )
        inventory = reader.inventory(identity=discovery.identity)
    return discovery, inventory


def read_gtm_full_inventory(
    root: Path,
    profile_id: str,
    *,
    container_id: str,
    fingerprint_key: bytes,
    ci_enabled: bool = False,
) -> tuple[GtmIdentityDiscovery, GtmReadInventory, GtmFullInventory]:
    """Read the complete fixed GTM Web inventory under one observed identity.

    This performs the existing fixed version and workspace-metadata read first,
    then the documented per-workspace and container collections.  Custom-code
    sources are classified and hashed in memory only; no source, name, or raw
    provider payload leaves this call.  Coverage is reported per collection, so
    a bounded read is INCOMPLETE rather than silently complete.
    """

    request = _request(root, profile_id, container_id, ci_enabled, True)
    _fingerprint_key(fingerprint_key)
    profile = _selected_profile(root, request)
    reference_for = _scoped_reference_for(profile.account_ref, request.container_id)
    credential_value = _credential_snapshot(profile, ci_enabled=request.ci_enabled)

    def token_getter() -> str:
        return credential_value

    with httpx.Client(trust_env=False) as client:
        transport = SafeHttpClient(client, {_HOST})
        identity_client = GtmIdentityClient(transport, token_getter, reference_for=reference_for)
        discovery = identity_client.discover_identity(profile.account_ref, request.container_id)
        reader = GtmReadClient(
            transport,
            token_getter,
            identity_verifier=identity_client.is_current_identity,
            fingerprint_key=fingerprint_key,
            reference_for=reference_for,
        )
        inventory_client = GtmInventoryClient(
            transport,
            token_getter,
            reader,
            fingerprint_key=fingerprint_key,
            reference_for=reference_for,
        )
        full = inventory_client.full_inventory(identity=discovery.identity)
    # `full.base` is already ordering-normalized by the inventory reader: its
    # workspaces are sorted by `workspace_ref` exactly as the default
    # `measurement gtm` path sorts them before retention, so the retained
    # observation depends on the container rather than on provider page order.
    return discovery, full.base, full


def read_gtm_selected(
    root: Path,
    profile_id: str,
    *,
    container_id: str,
    fingerprint_key: bytes,
    full_inventory: bool = False,
    ci_enabled: bool = False,
) -> tuple[GtmIdentityDiscovery, GtmReadInventory, GtmFullInventory | None]:
    """Route one GTM read by its request discriminator.

    `full_inventory=False` is the existing fixed read, byte for byte and request
    for request.  Only an explicit opt-in reaches the complete inventory.
    """

    request = _request(root, profile_id, container_id, ci_enabled, full_inventory)
    if not request.full_inventory:
        discovery, inventory = read_gtm(
            root,
            profile_id,
            container_id=container_id,
            fingerprint_key=fingerprint_key,
            ci_enabled=ci_enabled,
        )
        return discovery, inventory, None
    return read_gtm_full_inventory(
        root,
        profile_id,
        container_id=container_id,
        fingerprint_key=fingerprint_key,
        ci_enabled=ci_enabled,
    )
