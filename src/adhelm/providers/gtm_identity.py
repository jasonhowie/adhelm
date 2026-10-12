"""Fixed GTM v2 account and Web-container identity discovery.

This module proves only the selected account/container binding for one local
read session.  It exposes no inventory, staging, mutation, or generic request
surface.  Provider display metadata remains transient and is never included in
the normalized discovery result.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal, cast

import httpx

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError
from adhelm.models import StrictModel
from adhelm.providers.gtm_read import GtmReadIdentity, request_gtm_read
from adhelm.transport import SafeHttpClient

_HOST = "tagmanager.googleapis.com"
_API_ROOT = "tagmanager/v2"
_READONLY_SCOPE: Literal["https://www.googleapis.com/auth/tagmanager.readonly"] = (
    "https://www.googleapis.com/auth/tagmanager.readonly"
)
_NATIVE_ID = re.compile(r"^[0-9]{1,20}$")
_MAX_RESPONSE_BYTES = 8_388_608
_MAX_READ_ATTEMPTS = 3
_MAX_METADATA_STRING = 4_096
_MAX_METADATA_ITEMS = 1_000

_ACCOUNT_FIELDS = {
    "path",
    "accountId",
    "name",
    "shareData",
    "fingerprint",
    "tagManagerUrl",
    "features",
}
_ACCOUNT_FEATURE_FIELDS = {"supportUserPermissions", "supportMultipleContainers"}
_CONTAINER_FIELDS = {
    "path",
    "accountId",
    "containerId",
    "name",
    "domainName",
    "publicId",
    "tagIds",
    "features",
    "notes",
    "usageContext",
    "fingerprint",
    "tagManagerUrl",
    "taggingServerUrls",
}
_CONTAINER_FEATURE_FIELDS = {
    "supportUserPermissions",
    "supportEnvironments",
    "supportWorkspaces",
    "supportGtagConfigs",
    "supportBuiltInVariables",
    "supportClients",
    "supportFolders",
    "supportTags",
    "supportTemplates",
    "supportTriggers",
    "supportVariables",
    "supportVersions",
    "supportZones",
    "supportTransformations",
}
_ACCOUNT_STRING_FIELDS = ("name", "fingerprint", "tagManagerUrl")
_CONTAINER_STRING_FIELDS = ("name", "publicId", "notes", "fingerprint", "tagManagerUrl")
_CONTAINER_STRING_LIST_FIELDS = ("domainName", "tagIds", "taggingServerUrls")

TokenGetter = Callable[[], str]
ReferenceFor = Callable[[Literal["account", "container"], str], str]
FeatureAvailability = Literal["TRUE", "FALSE", "UNKNOWN"]


def _default_reference_for(kind: Literal["account", "container"], value: str) -> str:
    return f"gtm.{kind}.{value}"


class GtmAccountFeatureAvailability(StrictModel):
    support_user_permissions: FeatureAvailability = "UNKNOWN"
    support_multiple_containers: FeatureAvailability = "UNKNOWN"


class GtmContainerFeatureAvailability(StrictModel):
    support_user_permissions: FeatureAvailability = "UNKNOWN"
    support_environments: FeatureAvailability = "UNKNOWN"
    support_workspaces: FeatureAvailability = "UNKNOWN"
    support_gtag_configs: FeatureAvailability = "UNKNOWN"
    support_built_in_variables: FeatureAvailability = "UNKNOWN"
    support_clients: FeatureAvailability = "UNKNOWN"
    support_folders: FeatureAvailability = "UNKNOWN"
    support_tags: FeatureAvailability = "UNKNOWN"
    support_templates: FeatureAvailability = "UNKNOWN"
    support_triggers: FeatureAvailability = "UNKNOWN"
    support_variables: FeatureAvailability = "UNKNOWN"
    support_versions: FeatureAvailability = "UNKNOWN"
    support_zones: FeatureAvailability = "UNKNOWN"
    support_transformations: FeatureAvailability = "UNKNOWN"


class GtmIdentityDiscovery(StrictModel):
    """Bound identity plus the feature values that GTM explicitly exposed."""

    schema_version: Literal[1] = 1
    identity: GtmReadIdentity
    account_features: GtmAccountFeatureAvailability
    container_features: GtmContainerFeatureAvailability
    usage_context: Literal["WEB"] = "WEB"


class GtmIdentityClient:
    """Closed read-only discovery that can prove an identity to ``GtmReadClient``."""

    def __init__(
        self,
        transport: SafeHttpClient,
        token_getter: TokenGetter,
        *,
        reference_for: ReferenceFor = _default_reference_for,
    ) -> None:
        _validate_transport(transport)
        self._transport = transport
        self._token_getter = token_getter
        self._reference_for = reference_for
        self._identities: dict[tuple[str, str], GtmReadIdentity] = {}

    def discover_identity(self, account_id: str, container_id: str) -> GtmIdentityDiscovery:
        """Read and bind exactly one configured account and Web container.

        Cache proof is discarded before validating inputs, obtaining a token, or
        making either request.  A failed refresh therefore cannot leave an old
        identity valid for a later inventory call.
        """

        self._identities.clear()
        _validate_transport(self._transport)
        account = _public_native_id(account_id, "GTM account ID")
        container = _public_native_id(container_id, "GTM container ID")
        identity = self._bound_identity(account, container)
        token = _token_snapshot(self._token_getter)
        headers = {"authorization": f"Bearer {token}"}

        account_raw = self._get(f"accounts/{account}", headers, "account")
        _account_metadata(account_raw, account)
        container_raw = self._get(
            f"accounts/{account}/containers/{container}", headers, "container"
        )
        _container_metadata(container_raw, account, container)

        discovery = GtmIdentityDiscovery(
            identity=identity,
            account_features=_account_features(account_raw.get("features")),
            container_features=_container_features(container_raw.get("features")),
        )
        self._identities[(account, container)] = identity
        return discovery

    def _bound_identity(self, account: str, container: str) -> GtmReadIdentity:
        """Validate local reference construction before credential or HTTP access."""

        try:
            return GtmReadIdentity(
                account_ref=self._reference_for("account", account),
                account_client_ref=account,
                container_ref=self._reference_for("container", container),
                container_client_ref=container,
            )
        except AdhelmError:
            raise
        except Exception:
            raise AdhelmError(
                "IDENTITY_BINDING_INVALID", "GTM identity references cannot be safely bound."
            ) from None

    def is_current_identity(self, candidate: GtmReadIdentity) -> bool:
        """Return true only for the exact object returned by the latest discovery."""

        if not isinstance(candidate, GtmReadIdentity):
            return False
        try:
            GtmReadIdentity.model_validate(candidate.model_dump(mode="python"))
            account = _configured_native_id(candidate.account_client_ref)
            container = _configured_native_id(candidate.container_client_ref)
            expected_account_ref = self._reference_for("account", account)
            expected_container_ref = self._reference_for("container", container)
        except Exception:
            return False
        return (
            candidate.account_ref == expected_account_ref
            and candidate.container_ref == expected_container_ref
            and self._identities.get((account, container)) is candidate
        )

    def _get(self, path: str, headers: dict[str, str], context: str) -> dict[str, Any]:
        response = request_gtm_read(
            self._transport, f"https://{_HOST}/{_API_ROOT}/{path}", headers=headers
        )
        return _json(response, context)


def _validate_transport(transport: SafeHttpClient) -> None:
    """Keep mutable transport settings inside this reader's fixed read contract."""

    if not isinstance(transport, SafeHttpClient):
        raise AdhelmError(
            "TRANSPORT_CONFIG_INVALID", "GTM identity discovery requires SafeHttpClient."
        )
    if not isinstance(transport.allowed_hosts, frozenset) or _HOST not in transport.allowed_hosts:
        raise AdhelmError(
            "TRANSPORT_CONFIG_INVALID", "GTM identity discovery requires its fixed API host."
        )
    maximum_response_bytes = transport.maximum_response_bytes
    if (
        isinstance(maximum_response_bytes, bool)
        or not isinstance(maximum_response_bytes, int)
        or not 1 <= maximum_response_bytes <= _MAX_RESPONSE_BYTES
    ):
        raise AdhelmError(
            "TRANSPORT_CONFIG_INVALID",
            "GTM identity discovery requires a bounded response transport.",
        )
    read_attempts = transport.read_attempts
    if (
        isinstance(read_attempts, bool)
        or not isinstance(read_attempts, int)
        or not 1 <= read_attempts <= _MAX_READ_ATTEMPTS
    ):
        raise AdhelmError(
            "TRANSPORT_CONFIG_INVALID",
            "GTM identity discovery requires bounded read retries.",
        )


def _token_snapshot(getter: TokenGetter) -> str:
    try:
        token = getter()
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
        not isinstance(token, str)
        or not token
        or len(token) > 16_384
        or any(not char.isascii() or not 33 <= ord(char) <= 126 for char in token)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "GTM authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local GTM read profile.",
        )
    return token


def _json(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        value = parse_json(response.content, maximum_bytes=8_388_608)
    except (AdhelmError, UnicodeError, ValueError):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GTM {context} response is invalid."
        ) from None
    if not isinstance(value, dict):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GTM {context} response is invalid.")
    return cast(dict[str, Any], value)


def _public_native_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or _NATIVE_ID.fullmatch(value) is None:
        raise AdhelmError(
            "INVALID_INPUT", f"{context} must be an ASCII numeric identifier.", exit_code=2
        )
    return value


def _configured_native_id(value: Any) -> str:
    if not isinstance(value, str) or _NATIVE_ID.fullmatch(value) is None:
        raise AdhelmError("IDENTITY_BINDING_INVALID", "GTM identity is invalid.")
    return value


def _response_native_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or _NATIVE_ID.fullmatch(value) is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GTM {context} is invalid.")
    return value


def _keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    if set(value) - allowed:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GTM {context} contains an unsupported field."
        )


def _exact_path(value: Any, expected: str, context: str) -> None:
    if value != expected:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GTM {context} is outside the configured parent."
        )


def _metadata_string(value: Any, context: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_METADATA_STRING
        or any(ord(character) < 32 for character in value)
    ):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GTM {context} metadata is invalid.")


def _optional_strings(value: dict[str, Any], fields: tuple[str, ...], context: str) -> None:
    for field in fields:
        if field in value:
            _metadata_string(value[field], context)


def _optional_string_lists(value: dict[str, Any], fields: tuple[str, ...], context: str) -> None:
    for field in fields:
        if field not in value:
            continue
        values = value[field]
        if not isinstance(values, list) or len(values) > _MAX_METADATA_ITEMS:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GTM {context} metadata is invalid.")
        for item in values:
            _metadata_string(item, context)


def _features(value: Any, allowed: set[str], context: str) -> dict[str, FeatureAvailability]:
    if value is None:
        return {field: "UNKNOWN" for field in allowed}
    if not isinstance(value, dict):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GTM {context} features are invalid.")
    _keys(value, allowed, f"{context} features")
    normalized: dict[str, FeatureAvailability] = {field: "UNKNOWN" for field in allowed}
    for field, enabled in value.items():
        if not isinstance(enabled, bool):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GTM {context} features are invalid.")
        normalized[field] = "TRUE" if enabled else "FALSE"
    return normalized


def _account_features(value: Any) -> GtmAccountFeatureAvailability:
    raw = _features(value, _ACCOUNT_FEATURE_FIELDS, "account")
    return GtmAccountFeatureAvailability(
        support_user_permissions=raw["supportUserPermissions"],
        support_multiple_containers=raw["supportMultipleContainers"],
    )


def _container_features(value: Any) -> GtmContainerFeatureAvailability:
    raw = _features(value, _CONTAINER_FEATURE_FIELDS, "container")
    return GtmContainerFeatureAvailability(
        support_user_permissions=raw["supportUserPermissions"],
        support_environments=raw["supportEnvironments"],
        support_workspaces=raw["supportWorkspaces"],
        support_gtag_configs=raw["supportGtagConfigs"],
        support_built_in_variables=raw["supportBuiltInVariables"],
        support_clients=raw["supportClients"],
        support_folders=raw["supportFolders"],
        support_tags=raw["supportTags"],
        support_templates=raw["supportTemplates"],
        support_triggers=raw["supportTriggers"],
        support_variables=raw["supportVariables"],
        support_versions=raw["supportVersions"],
        support_zones=raw["supportZones"],
        support_transformations=raw["supportTransformations"],
    )


def _account_metadata(value: dict[str, Any], account: str) -> None:
    _keys(value, _ACCOUNT_FIELDS, "account")
    if _response_native_id(value.get("accountId"), "account ID") != account:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "GTM account differs from the configured account."
        )
    _exact_path(value.get("path"), f"accounts/{account}", "account path")
    _optional_strings(value, _ACCOUNT_STRING_FIELDS, "account")
    if "shareData" in value and not isinstance(value["shareData"], bool):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM account metadata is invalid.")
    _features(value.get("features"), _ACCOUNT_FEATURE_FIELDS, "account")


def _container_metadata(value: dict[str, Any], account: str, container: str) -> None:
    _keys(value, _CONTAINER_FIELDS, "container")
    if _response_native_id(value.get("accountId"), "container account ID") != account:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "GTM container differs from the configured account."
        )
    if _response_native_id(value.get("containerId"), "container ID") != container:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "GTM container differs from the configured container."
        )
    _exact_path(value.get("path"), f"accounts/{account}/containers/{container}", "container path")
    _optional_strings(value, _CONTAINER_STRING_FIELDS, "container")
    _optional_string_lists(value, _CONTAINER_STRING_LIST_FIELDS, "container")
    usage_context = value.get("usageContext")
    if usage_context != ["web"]:
        raise AdhelmError(
            "GTM_WEB_CONTAINER_REQUIRED",
            "Configured GTM container is not an explicitly declared Web container.",
        )
    _features(value.get("features"), _CONTAINER_FEATURE_FIELDS, "container")
