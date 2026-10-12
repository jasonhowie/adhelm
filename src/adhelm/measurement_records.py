"""Closed normalized configuration observations, without mutation or receipt authority."""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.gtm_inventory import GtmFullInventory
from adhelm.models import SafeRef, StrictModel
from adhelm.providers.ga4_admin import Ga4AdminInventory
from adhelm.providers.gtm_identity import GtmIdentityDiscovery
from adhelm.providers.gtm_read import GtmReadIdentity, GtmReadInventory
from adhelm.providers.openai_ads import OpenAIAdsIdentity
from adhelm.providers.openai_conversions import OpenAIAdsConversionSetting

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Ga4MeasurementSnapshot(StrictModel):
    provider: Literal["ga4_admin"] = "ga4_admin"
    capability_version: Literal["ga4-admin-fixed-inventory-1"] = "ga4-admin-fixed-inventory-1"
    inventory: Ga4AdminInventory

    @model_validator(mode="after")
    def bound_resources(self) -> "Ga4MeasurementSnapshot":
        identity = self.inventory.identity
        if (
            identity.property_ref != f"ga4_admin.property.{identity.property_client_ref}"
            or identity.web_stream_ref != f"ga4_admin.web_stream.{identity.web_stream_client_ref}"
        ):
            raise ValueError("GA4 measurement identity references must match native identifiers")
        collections = (
            ("key_event", self.inventory.key_events),
            ("event_create_rule", self.inventory.event_create_rules),
            ("event_edit_rule", self.inventory.event_edit_rules),
        )
        for kind, resources in collections:
            seen: set[str] = set()
            for resource in resources:
                if (
                    re.fullmatch(r"[0-9]{1,120}", resource.resource_client_ref) is None
                    or resource.resource_ref != f"ga4_admin.{kind}.{resource.resource_client_ref}"
                    or resource.property_ref != identity.property_ref
                    or resource.resource_ref in seen
                ):
                    raise ValueError("GA4 measurement resource identity is inconsistent")
                seen.add(resource.resource_ref)
        for kind, rules in (
            ("EVENT_CREATE_RULE", self.inventory.event_create_rules),
            ("EVENT_EDIT_RULE", self.inventory.event_edit_rules),
        ):
            if any(
                rule.web_stream_ref != identity.web_stream_ref or rule.rule_kind != kind
                for rule in rules
            ):
                raise ValueError("GA4 measurement rules must match their stream and collection")
        return self

    @property
    def account_ref(self) -> str:
        return self.inventory.identity.property_ref

    @property
    def row_count(self) -> int:
        return sum(
            len(rows)
            for rows in (
                self.inventory.key_events,
                self.inventory.event_create_rules,
                self.inventory.event_edit_rules,
            )
        )


class OpenAIMeasurementSnapshot(StrictModel):
    provider: Literal["openai_ads"] = "openai_ads"
    capability_version: Literal["openai-standard-conversion-inventory-1"] = (
        "openai-standard-conversion-inventory-1"
    )
    identity: OpenAIAdsIdentity
    settings: tuple[OpenAIAdsConversionSetting, ...] = Field(max_length=5_000)
    pagination_verified: Literal[True] = True
    list_response_conformance: Literal["FIXTURE_CONTRACT_LIVE_UNVERIFIED"] = (
        "FIXTURE_CONTRACT_LIVE_UNVERIFIED"
    )

    @model_validator(mode="after")
    def bound_resources(self) -> "OpenAIMeasurementSnapshot":
        native = self.identity.account_client_ref
        if (
            re.fullmatch(r"[A-Za-z0-9_-]{1,120}", native) is None
            or self.identity.account_ref != f"openai_ads.account.{native}"
        ):
            raise ValueError("OpenAI measurement account identity is inconsistent")
        seen: set[str] = set()
        for item in self.settings:
            if (
                re.fullmatch(r"[A-Za-z0-9_-]{1,120}", item.conversion_setting_client_ref) is None
                or item.account_ref != self.identity.account_ref
                or item.conversion_setting_ref
                != f"openai_ads.conversion_setting.{item.conversion_setting_client_ref}"
                or item.conversion_setting_ref in seen
            ):
                raise ValueError("OpenAI measurement setting identity is inconsistent")
            seen.add(item.conversion_setting_ref)
        return self

    @property
    def account_ref(self) -> str:
        return self.identity.account_ref

    @property
    def row_count(self) -> int:
        return len(self.settings)


class GtmMeasurementSnapshot(StrictModel):
    provider: Literal["gtm"] = "gtm"
    capability_version: Literal["gtm-versions-workspace-metadata-1"] = (
        "gtm-versions-workspace-metadata-1"
    )
    discovery: GtmIdentityDiscovery
    inventory: GtmReadInventory
    inventory_coverage: Literal["VERSION_COUNTS_AND_WORKSPACE_METADATA"] = (
        "VERSION_COUNTS_AND_WORKSPACE_METADATA"
    )
    dependency_completeness: Literal["NOT_PROVEN"] = "NOT_PROVEN"

    @model_validator(mode="after")
    def bound_resources(self) -> "GtmMeasurementSnapshot":
        identity = self.discovery.identity
        account, container = identity.account_client_ref, identity.container_client_ref
        if (
            identity.account_ref != f"gtm.account.{account}"
            or identity.container_ref != f"gtm.container.{account}.{container}"
            or self.inventory.identity != identity
        ):
            raise ValueError("GTM measurement identities must bind the account and container")
        for context, version in (("live", self.inventory.live), ("latest", self.inventory.latest)):
            prefix = f"gtm.container_version.{account}.{container}."
            if (
                version.context != context
                or not version.version_ref.startswith(prefix)
                or re.fullmatch(r"[0-9]{1,20}", version.version_ref.removeprefix(prefix)) is None
                or sum(version.resource_counts.model_dump().values()) > 4_000
                or len(version.opaque_code) > 4_000
            ):
                raise ValueError(
                    "GTM version observation is inconsistent with its parent or bounds"
                )
            html_count = sum(item.kind == "CUSTOM_HTML_TAG" for item in version.opaque_code)
            template_count = sum(item.kind == "CUSTOM_TEMPLATE" for item in version.opaque_code)
            if (
                html_count > version.resource_counts.tags
                or template_count != version.resource_counts.custom_templates
            ):
                raise ValueError("GTM opaque-code summaries must agree with resource counts")
        if len(self.inventory.workspaces) > 1_000:
            raise ValueError("GTM workspace observations exceed the fixed inventory bound")
        seen: set[str] = set()
        for workspace in self.inventory.workspaces:
            prefix = f"gtm.workspace.{account}.{container}."
            if (
                not workspace.workspace_ref.startswith(prefix)
                or re.fullmatch(r"[0-9]{1,20}", workspace.workspace_ref.removeprefix(prefix))
                is None
                or workspace.workspace_ref in seen
            ):
                raise ValueError("GTM workspace must have a unique parent-scoped reference")
            seen.add(workspace.workspace_ref)
        return self

    @property
    def identity(self) -> GtmReadIdentity:
        return self.discovery.identity

    @property
    def account_ref(self) -> str:
        return self.identity.account_ref

    @property
    def row_count(self) -> int:
        return (
            2
            + len(self.inventory.workspaces)
            + sum(
                len(version.opaque_code) for version in (self.inventory.live, self.inventory.latest)
            )
        )


class GtmFullInventoryMeasurementSnapshot(StrictModel):
    """The complete GTM Web fixed inventory as a retained configuration observation.

    This is a separate provider-discriminated snapshot rather than an extra
    field on `GtmMeasurementSnapshot`, so every already retained GTM version and
    workspace-metadata observation keeps its exact canonical bytes and digest.
    """

    provider: Literal["gtm_web_inventory"] = "gtm_web_inventory"
    capability_version: Literal["gtm-full-fixed-inventory-1"] = "gtm-full-fixed-inventory-1"
    discovery: GtmIdentityDiscovery
    inventory: GtmFullInventory
    inventory_coverage: Literal["VERSIONS_WORKSPACES_AND_DOCUMENTED_COLLECTIONS"] = (
        "VERSIONS_WORKSPACES_AND_DOCUMENTED_COLLECTIONS"
    )
    dependency_completeness: Literal["NOT_PROVEN"] = "NOT_PROVEN"
    ui_reconciliation: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"

    @model_validator(mode="after")
    def bound_resources(self) -> "GtmFullInventoryMeasurementSnapshot":
        identity = self.discovery.identity
        account, container = identity.account_client_ref, identity.container_client_ref
        if (
            identity.account_ref != f"gtm.account.{account}"
            or identity.container_ref != f"gtm.container.{account}.{container}"
            or self.inventory.identity != identity
            or self.inventory.base.identity != identity
        ):
            raise ValueError("GTM measurement identities must bind the account and container")
        version_prefix = f"gtm.container_version.{account}.{container}."
        workspace_prefix = f"gtm.workspace.{account}.{container}."
        container_ref = identity.container_ref
        for reference in (
            self.inventory.live_latest.live_version_ref,
            self.inventory.live_latest.latest_version_ref,
        ):
            if (
                not reference.startswith(version_prefix)
                or re.fullmatch(r"[0-9]{1,20}", reference.removeprefix(version_prefix)) is None
            ):
                raise ValueError("GTM inventory version references must bind their container")
        allowed_scopes = {
            self.inventory.live_latest.live_version_ref,
            self.inventory.live_latest.latest_version_ref,
            container_ref,
            *(workspace.workspace_ref for workspace in self.inventory.base.workspaces),
        }
        for workspace in self.inventory.base.workspaces:
            if (
                not workspace.workspace_ref.startswith(workspace_prefix)
                or re.fullmatch(
                    r"[0-9]{1,20}", workspace.workspace_ref.removeprefix(workspace_prefix)
                )
                is None
            ):
                raise ValueError("GTM workspace must have a unique parent-scoped reference")
        for scope_ref in (
            *(item.scope_ref for item in self.inventory.coverage),
            *(item.scope_ref for item in self.inventory.resources),
            *(item.scope_ref for item in self.inventory.edges),
        ):
            if scope_ref not in allowed_scopes:
                raise ValueError("GTM inventory records must bind a read container surface")
        for resource in self.inventory.resources:
            for marker in resource.code_markers:
                if marker.source_status == "DIGESTED_NEVER_PERSISTED" and (
                    marker.source_digest is None or marker.source_bytes is None
                ):
                    raise ValueError("GTM custom code must be represented by a digest and length")
        for edge in self.inventory.edges:
            # The retention boundary re-checks the one rule that lets arbitrary
            # customer text into an observation: a name-form selector is text
            # only when it named a resource this read actually saw.  Retention
            # is the last point before the append-only ledger, so this is
            # enforced here as well as where the edge is built (AGENTS rule 13).
            if edge.selector_form == "NAME" and edge.resolution != "RESOLVED":
                if edge.selector is not None:
                    raise ValueError("An unresolved GTM name selector must not be retained as text")
                if edge.selector_status != "WITHHELD_UNRESOLVED_REFERENCE":
                    raise ValueError("An unresolved GTM name selector must record why it was held")
        return self

    @property
    def identity(self) -> GtmReadIdentity:
        return self.discovery.identity

    @property
    def account_ref(self) -> str:
        return self.identity.account_ref

    @property
    def row_count(self) -> int:
        return (
            len(self.inventory.coverage) + len(self.inventory.resources) + len(self.inventory.edges)
        )


MeasurementSnapshot = Annotated[
    Ga4MeasurementSnapshot
    | OpenAIMeasurementSnapshot
    | GtmMeasurementSnapshot
    | GtmFullInventoryMeasurementSnapshot,
    Field(discriminator="provider"),
]


class MeasurementObservation(StrictModel):
    schema_version: Literal[1] = 1
    event_type: Literal["MEASUREMENT_OBSERVATION"] = "MEASUREMENT_OBSERVATION"
    profile_ref: SafeRef
    read_started_at: datetime
    read_finished_at: datetime
    snapshot: MeasurementSnapshot
    snapshot_digest: Digest
    evidence_kind: Literal["CONFIGURATION_READ"] = "CONFIGURATION_READ"
    consistency: Literal["SEQUENTIAL_NONATOMIC"] = "SEQUENTIAL_NONATOMIC"
    runtime_firing: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    processed_receipt: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    native_optimization_association: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    change_attribution: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    execution_authority: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Measurement schema_version must be the integer 1")
        return value

    @field_validator("execution_authority", mode="before")
    @classmethod
    def exact_false_authority(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("Measurement execution_authority must be a false JSON boolean")
        return value

    @field_validator("read_started_at", "read_finished_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Measurement read times must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bound_observation(self) -> "MeasurementObservation":
        if self.read_finished_at < self.read_started_at:
            raise ValueError("Measurement read interval cannot be reversed")
        if self.snapshot_digest != digest(self.snapshot):
            raise ValueError("Measurement snapshot digest must match its exact normalized content")
        return self
