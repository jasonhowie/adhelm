"""Closed EDIT-role configuration observations, separate from READ inventory."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import (
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm.canonical import digest
from adhelm.ga4_admin_execution_observation import Ga4AdminEditExecutionInventoryObservation
from adhelm.measurement_records import Digest
from adhelm.models import SafeRef, StrictModel
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditKeyEventInventory,
    _inventory_json_mapping,
)
from adhelm.strict_json import validate_json_value


def _execution_observation_schema(schema: dict[str, Any]) -> None:
    # Legacy omission is valid; explicit null is not a proof. Preserve the
    # generated model reference while removing the optional-type null branch.
    field = schema["properties"]["execution_observation"]
    branches = field.pop("anyOf")
    field.pop("default", None)
    field.update(next(branch for branch in branches if branch.get("type") != "null"))


class Ga4AdminEditInventoryObservation(StrictModel):
    model_config = ConfigDict(json_schema_extra=_execution_observation_schema)
    schema_version: Literal[1] = 1
    event_type: Literal["GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"] = (
        "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
    )
    profile_ref: SafeRef
    configuration_digest: Digest
    profile_configuration_digest: Digest
    read_started_at: datetime
    read_finished_at: datetime
    inventory: Ga4AdminEditKeyEventInventory
    inventory_digest: Digest
    evidence_kind: Literal["EDIT_PROFILE_CONFIGURATION_READ"] = "EDIT_PROFILE_CONFIGURATION_READ"
    consistency: Literal["SEQUENTIAL_NONATOMIC"] = "SEQUENTIAL_NONATOMIC"
    granted_scopes: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    create_permission: Literal["NOT_PREPROVABLE"] = "NOT_PREPROVABLE"
    runtime_firing: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    processed_receipt: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False
    execution_observation: Ga4AdminEditExecutionInventoryObservation | None = Field(
        default=None, validate_default=False
    )

    @field_validator("execution_observation", mode="before")
    @classmethod
    def closed_execution_observation(
        cls, value: object
    ) -> Ga4AdminEditExecutionInventoryObservation:
        if type(value) is Ga4AdminEditExecutionInventoryObservation:
            value = value.model_dump(mode="json", warnings="error")
        if type(value) is not dict or set(value) != set(
            Ga4AdminEditExecutionInventoryObservation.model_fields
        ):
            raise ValueError("execution observation must be a complete closed object")
        return validate_json_value(Ga4AdminEditExecutionInventoryObservation, value)

    @model_serializer(mode="wrap")
    def preserve_legacy_wire(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        output = handler(self)
        if not isinstance(output, dict):
            raise ValueError("EDIT observation must serialize as an object")
        if "execution_observation" not in self.model_fields_set:
            output.pop("execution_observation", None)
        return output

    @field_validator("inventory", mode="before")
    @classmethod
    def exact_normalized_inventory(cls, value: object) -> Ga4AdminEditKeyEventInventory:
        # Retained wire input must keep its original scalar kinds. In particular,
        # parsing a numeric JSON value into Decimal must not launder a malformed
        # normalized inventory before the digest/content checks can inspect it.
        return validate_json_value(Ga4AdminEditKeyEventInventory, _inventory_json_mapping(value))

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("execution_authority", "remote_mutations", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("observation authority and mutations must be false")
        return value

    @field_validator("read_started_at", "read_finished_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("read times must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bound_content(self) -> "Ga4AdminEditInventoryObservation":
        if self.read_finished_at < self.read_started_at:
            raise ValueError("read interval cannot be reversed")
        if self.inventory_digest != digest(self.inventory):
            raise ValueError("inventory digest must bind the exact normalized content")
        proof = self.execution_observation
        if "execution_observation" in self.model_fields_set and (
            proof is None
            or proof.configuration_digest != self.configuration_digest
            or proof.oauth_binding.profile_ref != self.profile_ref
            or proof.oauth_binding.profile_configuration_digest != self.profile_configuration_digest
            or proof.inventory_digest != self.inventory_digest
            or proof.inventory != self.inventory
            or proof.read_started_at != self.read_started_at
            or proof.read_finished_at != self.read_finished_at
        ):
            raise ValueError("execution observation must match the complete retained inventory")
        return self
