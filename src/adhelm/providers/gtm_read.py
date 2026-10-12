"""Fixed, read-only GTM v2 inventory for safe future staging preconditions."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from typing import Any, Literal, cast

import httpx
from pydantic import Field, model_validator

from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import SafeRef, StrictModel
from adhelm.transport import SafeHttpClient

_HOST = "tagmanager.googleapis.com"
_API_ROOT = "tagmanager/v2"
_READONLY_SCOPE: Literal["https://www.googleapis.com/auth/tagmanager.readonly"] = (
    "https://www.googleapis.com/auth/tagmanager.readonly"
)
_MAX_WORKSPACE_PAGES = 20
_MAX_WORKSPACES = 1_000
_MAX_VERSION_RESOURCES = 4_000
_NATIVE_ID = re.compile(r"^[0-9]{1,20}$")
_SAFE_REFERENCE = re.compile(r"^[a-zA-Z0-9_.:/-]{1,160}$")
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
_CONTAINER_STRING_FIELDS = (
    "name",
    "publicId",
    "notes",
    "fingerprint",
    "tagManagerUrl",
)
_CONTAINER_STRING_LIST_FIELDS = ("domainName", "tagIds", "taggingServerUrls")

GtmHeaderCounter = Literal[
    "numTags",
    "numTriggers",
    "numVariables",
    "numZones",
    "numCustomTemplates",
    "numClients",
    "numGtagConfigs",
    "numTransformations",
]
_HEADER_COUNTERS: tuple[GtmHeaderCounter, ...] = (
    "numTags",
    "numTriggers",
    "numVariables",
    "numZones",
    "numCustomTemplates",
    "numClients",
    "numGtagConfigs",
    "numTransformations",
)


# Google's Tag Manager limits page (fetched 2026-10-10, page last updated
# 2026-07-16) enforces the 0.25 QPS project limit over a 100-second sliding
# window.  A quota refusal is a 403, so the wait guidance below is built on it.
_QUOTA_WINDOW_SECONDS = 100


def classify_gtm_transport_error(error: AdhelmError) -> AdhelmError:
    """Turn one shared-transport authorization refusal into an exact GTM diagnostic.

    The shared transport records the observed HTTP status and nothing from the
    response body, so this reads the status only.  A `401` means Google did not
    accept the bearer.  A `403` is deliberately not narrowed to one cause:
    Google documents the Tag Manager quota refusal as a `403` too
    (https://developers.google.com/tag-platform/tag-manager/api/v2/limits-quotas,
    fetched 2026-10-10), so a missing container permission, a Cloud project without the
    Tag Manager API, and an exhausted project quota are indistinguishable here.
    A status this contract does not classify is returned unchanged.
    """
    status = getattr(error, "status_code", None)
    retry_after = getattr(error, "retry_after_seconds", None)
    if status == 401:
        return AdhelmError(
            "GTM_AUTHORIZATION_REJECTED",
            "Google did not accept the stored credential for this GTM read.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action=(
                "Run `adhelm auth google-oauth status --profile <id>`. If the access token is "
                "expired, repeat the read once: it refreshes before any request. If the state "
                "is RECONNECT_REQUIRED, or Google keeps rejecting a READY grant, run "
                "`adhelm auth google-oauth reconnect --profile <id> --client-config <file> "
                "--interactive` as the original Google user."
            ),
            status_code=status,
            retry_after_seconds=retry_after,
        )
    if status == 403:
        wait = (
            f"Google asked for a wait of {retry_after} second(s); wait that long, then repeat "
            "the bounded read once; do not retry in a loop."
            if retry_after is not None
            else (
                "Google sent no Retry-After hint, so wait at least "
                f"{_QUOTA_WINDOW_SECONDS} seconds (the 0.25 requests per second limit is "
                "enforced over a 100-second sliding window; an exhausted daily quota clears "
                "only at midnight Pacific time), then repeat the bounded read once; do not "
                "retry in a loop."
            )
        )
        return AdhelmError(
            "GTM_ACCESS_NOT_PERMITTED",
            "Tag Manager refused the configured read profile for this account or container.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action=(
                "Google returns 403 for several causes and the response body is not read, so "
                "this cannot say which. Check that the enrolled Google user has at least Read "
                "access to the container (GTM Admin, User management), that the Tag Manager API "
                "is enabled in the Google Cloud project that owns the OAuth client, and that "
                "the project's documented quota (10,000 requests per day and 0.25 requests per "
                "second) is not exhausted. Run `adhelm auth google-oauth status --profile <id>` "
                f"to confirm the local grant. {wait} Quota source: "
                "https://developers.google.com/tag-platform/tag-manager/api/v2/limits-quotas"
            ),
            status_code=status,
            retry_after_seconds=retry_after,
        )
    return error


def request_gtm_read(
    transport: SafeHttpClient,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Issue one fixed GTM `GET` and classify an authorization refusal."""
    try:
        return transport.request("GET", url, headers=headers, params=params)
    except AdhelmError as error:
        classified = classify_gtm_transport_error(error)
        if classified is error:
            raise
        raise classified from None


TokenGetter = Callable[[], str]
IdentityVerifier = Callable[["GtmReadIdentity"], bool]
ReferenceFor = Callable[
    [Literal["account", "container", "workspace", "container_version"], str], str
]


def _default_reference_for(
    kind: Literal["account", "container", "workspace", "container_version"], value: str
) -> str:
    return f"gtm.{kind}.{value}"


class GtmReadIdentity(StrictModel):
    """A configured GTM account/container, bound to a verified local session."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    account_client_ref: str = Field(pattern=r"^[0-9]{1,20}$")
    container_ref: SafeRef
    container_client_ref: str = Field(pattern=r"^[0-9]{1,20}$")
    api_version: Literal["v2"] = "v2"
    required_scope: Literal["https://www.googleapis.com/auth/tagmanager.readonly"] = _READONLY_SCOPE


class GtmResourceCounts(StrictModel):
    tags: int = Field(ge=0, le=_MAX_VERSION_RESOURCES)
    triggers: int = Field(ge=0, le=_MAX_VERSION_RESOURCES)
    variables: int = Field(ge=0, le=_MAX_VERSION_RESOURCES)
    folders: int = Field(ge=0, le=_MAX_VERSION_RESOURCES)
    built_in_variables: int = Field(ge=0, le=_MAX_VERSION_RESOURCES)
    zones: int = Field(ge=0, le=_MAX_VERSION_RESOURCES)
    custom_templates: int = Field(ge=0, le=_MAX_VERSION_RESOURCES)
    clients: int = Field(ge=0, le=_MAX_VERSION_RESOURCES)
    gtag_configs: int = Field(ge=0, le=_MAX_VERSION_RESOURCES)
    transformations: int = Field(ge=0, le=_MAX_VERSION_RESOURCES)


class GtmOpaqueCodeMetadata(StrictModel):
    """An HMAC-backed marker; source content is never returned or retained here."""

    kind: Literal["CUSTOM_HTML_TAG", "CUSTOM_TEMPLATE"]
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    inspection_status: Literal["OPAQUE_SOURCE_UNSUPPORTED"] = "OPAQUE_SOURCE_UNSUPPORTED"


class GtmVersionSnapshot(StrictModel):
    context: Literal["live", "latest"]
    version_ref: SafeRef
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    resource_counts: GtmResourceCounts
    opaque_code: tuple[GtmOpaqueCodeMetadata, ...] = Field(default_factory=tuple)
    compiler_status: Literal["NOT_PROVEN_BY_INVENTORY"] = "NOT_PROVEN_BY_INVENTORY"
    browser_status: Literal["NOT_PROVEN_BY_INVENTORY"] = "NOT_PROVEN_BY_INVENTORY"


class GtmWorkspaceMetadata(StrictModel):
    workspace_ref: SafeRef
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    contents_status: Literal["NOT_INSPECTED"] = "NOT_INSPECTED"
    dependency_status: Literal["UNKNOWN_NOT_INSPECTED"] = "UNKNOWN_NOT_INSPECTED"


class GtmLatestHeaderCountStatus(StrictModel):
    """Whether all documented latest-header counters were available and consistent."""

    status: Literal["ALL_HEADER_COUNTS_MATCH", "HEADER_COUNTS_UNAVAILABLE"]
    missing_counter_fields: tuple[GtmHeaderCounter, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def status_matches_missing_fields(self) -> GtmLatestHeaderCountStatus:
        if len(self.missing_counter_fields) != len(set(self.missing_counter_fields)):
            raise ValueError("Missing GTM header counter fields must be unique")
        if self.status == "ALL_HEADER_COUNTS_MATCH" and self.missing_counter_fields:
            raise ValueError("Matching GTM header counts cannot have missing counters")
        if self.status == "HEADER_COUNTS_UNAVAILABLE" and not self.missing_counter_fields:
            raise ValueError("Unavailable GTM header counts require named missing counters")
        return self


class GtmReadInventory(StrictModel):
    schema_version: Literal[1] = 1
    identity: GtmReadIdentity
    live: GtmVersionSnapshot
    latest: GtmVersionSnapshot
    latest_header_count_status: GtmLatestHeaderCountStatus
    workspaces: tuple[GtmWorkspaceMetadata, ...] = Field(default_factory=tuple)
    observation_status: Literal["SEQUENTIALLY_REREAD_NONATOMIC"] = "SEQUENTIALLY_REREAD_NONATOMIC"
    staging_eligibility: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"


class GtmReadClient:
    """Closed GTM inventory. It deliberately exposes no generic request or write method."""

    def __init__(
        self,
        transport: SafeHttpClient,
        token_getter: TokenGetter,
        identity_verifier: IdentityVerifier,
        *,
        fingerprint_key: bytes,
        reference_for: ReferenceFor = _default_reference_for,
    ) -> None:
        if _HOST not in transport.allowed_hosts:
            raise AdhelmError("TRANSPORT_CONFIG_INVALID", "GTM reader requires its fixed API host.")
        if not isinstance(fingerprint_key, bytes) or len(fingerprint_key) != 32:
            raise AdhelmError(
                "FINGERPRINT_KEY_REQUIRED", "GTM inventory requires a local fingerprint key."
            )
        self._transport = transport
        self._token_getter = token_getter
        self._identity_verifier = identity_verifier
        self._fingerprint_key = fingerprint_key
        self._reference_for = reference_for

    def inventory(self, *, identity: GtmReadIdentity) -> GtmReadInventory:
        return self.inventory_with_sources(identity=identity)[0]

    def inventory_with_sources(
        self, *, identity: GtmReadIdentity
    ) -> tuple[GtmReadInventory, dict[str, Any], dict[str, Any]]:
        """Return the fixed inventory plus its transient live and latest version bodies.

        The raw bodies remain in memory for one in-session caller that must
        resolve documented references inside the same observed identity.  They
        are never emitted, logged, or persisted, and `inventory()` discards
        them without changing its own request sequence or result.
        """

        account, container = self._assert_session_identity(identity)
        parent = f"accounts/{account}/containers/{container}"
        headers = {"authorization": f"Bearer {_safe_token(self._token_getter)}"}

        latest_header_before = self._latest_header(parent, headers, identity)
        live_raw = self._get(f"{parent}/versions:live", headers, "live version")
        latest_raw = self._get(
            f"{parent}/versions/{latest_header_before['containerVersionId']}",
            headers,
            "latest version",
        )
        workspaces = self._workspaces(parent, headers, identity)
        latest_header_after = self._latest_header(parent, headers, identity)
        live_after = self._get(f"{parent}/versions:live", headers, "live version reread")

        if (
            latest_header_before != latest_header_after
            or _identity_tuple(live_raw) != _identity_tuple(live_after)
            or _fingerprint(self._fingerprint_key, "version", live_raw)
            != _fingerprint(self._fingerprint_key, "version", live_after)
        ):
            raise AdhelmError(
                "INVENTORY_INCONSISTENT",
                "GTM changed during sequential inventory; retry before making a staging decision.",
            )
        live = self._snapshot(live_raw, identity, "live")
        latest = self._snapshot(latest_raw, identity, "latest")
        if latest.version_ref != self._reference_for(
            "container_version", latest_header_before["containerVersionId"]
        ):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GTM latest version identity is inconsistent."
            )
        latest_header_count_status = self._latest_header_count_status(latest_header_before, latest)
        return (
            GtmReadInventory(
                identity=identity,
                live=live,
                latest=latest,
                latest_header_count_status=latest_header_count_status,
                workspaces=tuple(workspaces),
            ),
            live_raw,
            latest_raw,
        )

    def _assert_session_identity(self, identity: GtmReadIdentity) -> tuple[str, str]:
        if not isinstance(identity, GtmReadIdentity):
            raise AdhelmError("IDENTITY_BINDING_INVALID", "Configured GTM identity is invalid.")
        if identity.api_version != "v2" or identity.required_scope != _READONLY_SCOPE:
            raise AdhelmError(
                "IDENTITY_BINDING_INVALID", "Configured GTM identity has an invalid API binding."
            )
        account = _configured_native_id(identity.account_client_ref)
        container = _configured_native_id(identity.container_client_ref)
        try:
            expected_account_ref = self._reference_for("account", account)
            expected_container_ref = self._reference_for("container", container)
        except Exception as exc:
            raise AdhelmError(
                "IDENTITY_BINDING_INVALID", "Configured GTM references cannot be verified."
            ) from exc
        if (
            not _safe_reference(identity.account_ref)
            or not _safe_reference(identity.container_ref)
            or identity.account_ref != expected_account_ref
            or identity.container_ref != expected_container_ref
        ):
            raise AdhelmError(
                "IDENTITY_BINDING_INVALID",
                "Configured GTM references do not bind to their native account and container IDs.",
            )
        try:
            verified = self._identity_verifier(identity)
        except Exception:
            verified = False
        if verified is not True:
            raise AdhelmError(
                "IDENTITY_VERIFICATION_REQUIRED",
                "GTM inventory requires a verified current account and container identity.",
                next_action="Verify the configured GTM account and container before inventory.",
            )
        return account, container

    def _get(self, path: str, headers: dict[str, str], context: str) -> dict[str, Any]:
        response = request_gtm_read(
            self._transport, f"https://{_HOST}/{_API_ROOT}/{path}", headers=headers
        )
        return _json(response, context)

    def _latest_header(
        self, parent: str, headers: dict[str, str], identity: GtmReadIdentity
    ) -> dict[str, str]:
        value = self._get(f"{parent}/version_headers:latest", headers, "latest version header")
        _keys(
            value,
            {
                "path",
                "accountId",
                "containerId",
                "containerVersionId",
                "name",
                "numTags",
                "numTriggers",
                "deleted",
                "numVariables",
                "numZones",
                "numCustomTemplates",
                "numClients",
                "numGtagConfigs",
                "numTransformations",
            },
            "latest version header",
        )
        account, container, version = _version_identity(value)
        if (account, container) != (identity.account_client_ref, identity.container_client_ref):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "GTM latest version header is outside the configured container.",
            )
        _exact_path(
            value.get("path"),
            f"accounts/{account}/containers/{container}/versions/{version}",
            "GTM latest version header",
        )
        deleted = value.get("deleted", False)
        if not isinstance(deleted, bool) or deleted:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM latest version header is invalid.")
        counts: dict[str, str] = {}
        for count in _HEADER_COUNTERS:
            raw_count = value.get(count)
            if raw_count is None:
                continue
            _bounded_uint(raw_count, f"GTM latest {count}")
            counts[count] = raw_count
        return {
            "accountId": account,
            "containerId": container,
            "containerVersionId": version,
            **counts,
            "observation_fingerprint": _fingerprint(
                self._fingerprint_key, "latest-version-header", value
            ),
        }

    def _latest_header_count_status(
        self, latest_header: dict[str, str], latest: GtmVersionSnapshot
    ) -> GtmLatestHeaderCountStatus:
        latest_counts = {
            "numTags": latest.resource_counts.tags,
            "numTriggers": latest.resource_counts.triggers,
            "numVariables": latest.resource_counts.variables,
            "numZones": latest.resource_counts.zones,
            "numCustomTemplates": latest.resource_counts.custom_templates,
            "numClients": latest.resource_counts.clients,
            "numGtagConfigs": latest.resource_counts.gtag_configs,
            "numTransformations": latest.resource_counts.transformations,
        }
        missing = tuple(counter for counter in _HEADER_COUNTERS if counter not in latest_header)
        for header_name, count in latest_counts.items():
            supplied_count = latest_header.get(header_name)
            if supplied_count is not None and int(supplied_count) != count:
                raise AdhelmError(
                    "INVENTORY_INCONSISTENT",
                    "GTM latest header counts differ from its version snapshot.",
                )
        if missing:
            return GtmLatestHeaderCountStatus(
                status="HEADER_COUNTS_UNAVAILABLE", missing_counter_fields=missing
            )
        return GtmLatestHeaderCountStatus(status="ALL_HEADER_COUNTS_MATCH")

    def _workspaces(
        self, parent: str, headers: dict[str, str], identity: GtmReadIdentity
    ) -> list[GtmWorkspaceMetadata]:
        workspaces: list[GtmWorkspaceMetadata] = []
        seen_ids: set[str] = set()
        seen_tokens: set[str] = set()
        token: str | None = None
        for _ in range(_MAX_WORKSPACE_PAGES):
            params = {"pageToken": token} if token is not None else None
            response = request_gtm_read(
                self._transport,
                f"https://{_HOST}/{_API_ROOT}/{parent}/workspaces",
                headers=headers,
                params=params,
            )
            payload = _json(response, "workspace list")
            _keys(payload, {"workspace", "nextPageToken"}, "workspace list")
            page = payload.get("workspace", [])
            if not isinstance(page, list):
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM workspace page is invalid.")
            if len(page) > _MAX_WORKSPACES - len(workspaces):
                raise AdhelmError(
                    "PAGINATION_LIMIT", "GTM workspace inventory exceeds the item bound."
                )
            for raw in page:
                if not isinstance(raw, dict):
                    raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM workspace item is invalid.")
                _keys(
                    raw,
                    {
                        "path",
                        "accountId",
                        "containerId",
                        "workspaceId",
                        "name",
                        "description",
                        "fingerprint",
                        "tagManagerUrl",
                    },
                    "workspace",
                )
                account = _native_id(raw.get("accountId"), "GTM workspace account ID")
                container = _native_id(raw.get("containerId"), "GTM workspace container ID")
                workspace = _native_id(raw.get("workspaceId"), "GTM workspace ID")
                if (account, container) != (
                    identity.account_client_ref,
                    identity.container_client_ref,
                ):
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID",
                        "GTM workspace is outside the configured container.",
                    )
                _exact_path(raw.get("path"), f"{parent}/workspaces/{workspace}", "GTM workspace")
                _bounded_optional_strings(
                    raw, ("name", "description", "fingerprint", "tagManagerUrl"), "GTM workspace"
                )
                if workspace in seen_ids:
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "GTM workspace list has a duplicate item."
                    )
                seen_ids.add(workspace)
                workspaces.append(
                    GtmWorkspaceMetadata(
                        workspace_ref=self._reference_for("workspace", workspace),
                        configuration_fingerprint=_fingerprint(
                            self._fingerprint_key, "workspace", raw
                        ),
                    )
                )
            next_token = payload.get("nextPageToken")
            if next_token is None:
                return workspaces
            token = _page_token(next_token)
            if token in seen_tokens:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "GTM workspace pagination made no safe progress."
                )
            seen_tokens.add(token)
        raise AdhelmError("PAGINATION_LIMIT", "GTM workspace inventory exceeded the page bound.")

    def _snapshot(
        self, raw: dict[str, Any], identity: GtmReadIdentity, context: Literal["live", "latest"]
    ) -> GtmVersionSnapshot:
        _keys(
            raw,
            {
                "path",
                "accountId",
                "containerId",
                "containerVersionId",
                "name",
                "deleted",
                "description",
                "container",
                "tag",
                "trigger",
                "variable",
                "folder",
                "builtInVariable",
                "fingerprint",
                "tagManagerUrl",
                "zone",
                "customTemplate",
                "client",
                "gtagConfig",
                "transformation",
            },
            "container version",
        )
        account, container, version = _version_identity(raw)
        if (account, container) != (identity.account_client_ref, identity.container_client_ref):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GTM version is outside the configured container."
            )
        _exact_path(
            raw.get("path"),
            f"accounts/{account}/containers/{container}/versions/{version}",
            "GTM version",
        )
        deleted = raw.get("deleted", False)
        if not isinstance(deleted, bool) or deleted:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GTM inventory cannot use a deleted version."
            )
        _bounded_optional_strings(
            raw, ("name", "description", "fingerprint", "tagManagerUrl"), "GTM version"
        )
        container_metadata = raw.get("container")
        if not isinstance(container_metadata, dict):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GTM version container metadata is invalid."
            )
        _container_metadata(container_metadata, identity)
        counts: dict[str, int] = {}
        opaque_code: list[GtmOpaqueCodeMetadata] = []
        collections = {
            "tag": "tags",
            "trigger": "triggers",
            "variable": "variables",
            "folder": "folders",
            "builtInVariable": "built_in_variables",
            "zone": "zones",
            "customTemplate": "custom_templates",
            "client": "clients",
            "gtagConfig": "gtag_configs",
            "transformation": "transformations",
        }
        for wire_name, field_name in collections.items():
            resources = raw.get(wire_name, [])
            if not isinstance(resources, list) or len(resources) > _MAX_VERSION_RESOURCES:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "GTM version resource collection is invalid."
                )
            for resource in resources:
                if not isinstance(resource, dict):
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "GTM version resource is invalid."
                    )
                _resource_parent(resource, identity)
                _bounded_json(resource, "GTM version resource")
                if wire_name == "tag" and resource.get("type") == "html":
                    opaque_code.append(
                        GtmOpaqueCodeMetadata(
                            kind="CUSTOM_HTML_TAG",
                            configuration_fingerprint=_fingerprint(
                                self._fingerprint_key, "custom-html", resource
                            ),
                        )
                    )
                if wire_name == "customTemplate":
                    opaque_code.append(
                        GtmOpaqueCodeMetadata(
                            kind="CUSTOM_TEMPLATE",
                            configuration_fingerprint=_fingerprint(
                                self._fingerprint_key, "custom-template", resource
                            ),
                        )
                    )
            counts[field_name] = len(resources)
        if sum(counts.values()) > _MAX_VERSION_RESOURCES:
            raise AdhelmError("PAGINATION_LIMIT", "GTM version exceeds the resource bound.")
        return GtmVersionSnapshot(
            context=context,
            version_ref=self._reference_for("container_version", version),
            configuration_fingerprint=_fingerprint(self._fingerprint_key, "version", raw),
            resource_counts=GtmResourceCounts(**counts),
            opaque_code=tuple(opaque_code),
        )


def _safe_token(getter: TokenGetter) -> str:
    token = getter()
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 16_384
        or any(ord(c) < 33 for c in token)
    ):
        raise AdhelmError("PROVIDER_ACCESS_REQUIRED", "GTM authorization is unavailable.")
    return token


def _json(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        value = parse_json(response.content, maximum_bytes=8_388_608)
    except (AdhelmError, UnicodeError, ValueError) as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GTM {context} response is invalid."
        ) from exc
    if not isinstance(value, dict):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"GTM {context} response is invalid.")
    return cast(dict[str, Any], value)


def _keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    if set(value) - allowed:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"GTM {context} contains an unsupported field."
        )


def _native_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or _NATIVE_ID.fullmatch(value) is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"{context} is invalid.")
    return value


def _configured_native_id(value: Any) -> str:
    if not isinstance(value, str) or _NATIVE_ID.fullmatch(value) is None:
        raise AdhelmError("IDENTITY_BINDING_INVALID", "Configured GTM identity is invalid.")
    return value


def _safe_reference(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_REFERENCE.fullmatch(value) is not None


def _version_identity(value: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _native_id(value.get("accountId"), "GTM version account ID"),
        _native_id(value.get("containerId"), "GTM version container ID"),
        _native_id(value.get("containerVersionId"), "GTM version ID"),
    )


def _identity_tuple(value: dict[str, Any]) -> tuple[str, str, str]:
    return _version_identity(value)


def _exact_path(value: Any, expected: str, context: str) -> None:
    if value != expected:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"{context} path is outside the configured parent."
        )


def _container_metadata(value: dict[str, Any], identity: GtmReadIdentity) -> None:
    _keys(value, _CONTAINER_FIELDS, "GTM version container metadata")
    account_present = "accountId" in value
    container_present = "containerId" in value
    if account_present != container_present:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM version container metadata is invalid.")
    if account_present:
        account = _native_id(value.get("accountId"), "GTM version container account ID")
        container = _native_id(value.get("containerId"), "GTM version container ID")
        if (account, container) != (
            identity.account_client_ref,
            identity.container_client_ref,
        ):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "GTM version container metadata is outside the configured container.",
            )
    path = value.get("path")
    if path is not None:
        _exact_path(
            path,
            f"accounts/{identity.account_client_ref}/containers/{identity.container_client_ref}",
            "GTM version container metadata",
        )
    _bounded_optional_strings(value, _CONTAINER_STRING_FIELDS, "GTM version container metadata")
    for name in _CONTAINER_STRING_LIST_FIELDS:
        _bounded_string_list(value.get(name), "GTM version container metadata")
    usage_context = value.get("usageContext")
    if usage_context is not None:
        if not isinstance(usage_context, list) or any(
            not isinstance(item, str)
            or item
            not in {
                "usageContextUnspecified",
                "web",
                "android",
                "ios",
                "androidSdk5",
                "iosSdk5",
                "amp",
                "server",
            }
            for item in usage_context
        ):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GTM version container metadata is invalid."
            )
    features = value.get("features")
    if features is not None:
        if not isinstance(features, dict):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GTM version container metadata is invalid."
            )
        _keys(features, _CONTAINER_FEATURE_FIELDS, "GTM version container features")
        if any(not isinstance(enabled, bool) for enabled in features.values()):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "GTM version container metadata is invalid."
            )
    _bounded_json(value, "GTM version container metadata")


def _resource_parent(resource: dict[str, Any], identity: GtmReadIdentity) -> None:
    account_present = "accountId" in resource
    container_present = "containerId" in resource
    if account_present != container_present:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM version resource is invalid.")
    if account_present:
        account = _native_id(resource.get("accountId"), "GTM version resource account ID")
        container = _native_id(resource.get("containerId"), "GTM version resource container ID")
        if (account, container) != (
            identity.account_client_ref,
            identity.container_client_ref,
        ):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "GTM version resource is outside the configured container.",
            )
    workspace = resource.get("workspaceId")
    if workspace is not None:
        _native_id(workspace, "GTM version resource workspace ID")
    path = resource.get("path")
    if path is not None:
        _resource_path(path, identity)


def _resource_path(value: Any, identity: GtmReadIdentity) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 65_536
        or any(ord(character) < 32 for character in value)
        or not value.startswith(
            f"accounts/{identity.account_client_ref}/containers/{identity.container_client_ref}/"
        )
        or any(segment in {"", ".", ".."} for segment in value.split("/"))
    ):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM version resource path is invalid.")


def _bounded_optional_strings(value: dict[str, Any], names: tuple[str, ...], context: str) -> None:
    for name in names:
        item = value.get(name)
        if item is not None and (
            not isinstance(item, str) or len(item) > 65_536 or any(ord(c) < 32 for c in item)
        ):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"{context} metadata is invalid.")


def _bounded_string_list(value: Any, context: str) -> None:
    if value is None:
        return
    if (
        not isinstance(value, list)
        or len(value) > _MAX_VERSION_RESOURCES
        or any(
            not isinstance(item, str)
            or len(item) > 65_536
            or any(ord(character) < 32 for character in item)
            for item in value
        )
    ):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"{context} metadata is invalid.")


def _bounded_uint(value: Any, context: str) -> None:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or int(value) > _MAX_VERSION_RESOURCES
    ):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"{context} is invalid.")


def _page_token(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or any(ord(c) < 33 for c in value)
    ):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GTM workspace pagination token is invalid.")
    return value


def _bounded_json(value: dict[str, Any], context: str) -> None:
    try:
        if len(canonical_bytes(value)) > 1_048_576:
            raise ValueError
    except (AdhelmError, ValueError, RecursionError) as exc:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"{context} is invalid.") from exc


def _fingerprint(key: bytes, domain: str, value: Any) -> str:
    return hmac.new(
        key,
        b"adhelm.gtm-read.v1/" + domain.encode("ascii") + b"\0" + canonical_bytes(value),
        hashlib.sha256,
    ).hexdigest()
