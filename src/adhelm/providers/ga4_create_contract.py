"""Closed, pure GA4 Admin key-event CREATE plan reconstruction.

This module turns already selected local facts into one exact, non-authorizing
``CompositePlan``.  It has no ledger, credential lifecycle, configuration,
transport, or provider dependency.  The configured workflow supplies the
facts after it has selected them from verified local history and collected an
EDIT observation; a future guarded adapter must collect them again immediately
before egress.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.conversion_contracts import ConversionContract
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_key_event_candidates import (
    Ga4KeyEventCandidateInput,
    Ga4KeyEventCandidateRecord,
    _revalidate_candidate_input,
    _revalidate_contract,
    compile_ga4_key_event_candidate,
    revalidate_ga4_key_event_candidate_record,
    validate_ga4_key_event_candidate_binding,
)
from adhelm.models import SafeRef, StrictModel
from adhelm.providers.ga4_admin_compiler import (
    Ga4AdminKeyEventCreateCandidate,
    Ga4AdminKeyEventCreateRequest,
    Ga4AdminKeyEventCreateTarget,
)
from adhelm.providers.ga4_admin_create_state import (
    Ga4AdminKeyEventCreateSemanticState,
    _parse_create_state,
    build_ga4_admin_key_event_create_semantic_state,
)
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditInventoryTarget,
    Ga4AdminEditKeyEventInventory,
    revalidate_ga4_admin_edit_key_event_inventory,
)
from adhelm.safety import (
    AccountIdentity,
    CompositePlan,
    EgressTarget,
    FieldChange,
    Money,
    OperationKind,
    SubPlan,
)
from adhelm.strict_json import validate_json_value

GA4_KEY_EVENT_CREATE_PLAN_STATUS: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = (
    "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
)
GA4_KEY_EVENT_CREATE_HOST: Literal["analyticsadmin.googleapis.com"] = (
    "analyticsadmin.googleapis.com"
)
GA4_KEY_EVENT_CREATE_EGRESS_PURPOSE: Literal["fixed GA4 Admin key-event create"] = (
    "fixed GA4 Admin key-event create"
)
GA4_KEY_EVENT_CREATE_SCOPE: Literal["https://www.googleapis.com/auth/analytics.edit"] = (
    "https://www.googleapis.com/auth/analytics.edit"
)
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_MAX_INPUT_BYTES = 1_048_576
_MAX_INPUT_NODES = 20_000
_MAX_INPUT_DEPTH = 20
_MAX_INPUT_STRING_BYTES = 16_384
_MIN_EXPIRY = timedelta(seconds=60)
_MAX_EXPIRY = timedelta(minutes=15)


def _invalid_plan(message: str = "GA4 key-event CREATE plan data is invalid.") -> AdhelmError:
    return AdhelmError(
        "INVALID_GA4_KEY_EVENT_CREATE_PLAN",
        message,
        exit_code=ExitCode.INVALID_INPUT,
    )


def _precondition(message: str, *, code: str) -> AdhelmError:
    return AdhelmError(code, message, exit_code=ExitCode.PRECONDITION)


def _exact_int(value: object, *, expected: int | None = None) -> int:
    if type(value) is not int or (expected is not None and value != expected):
        raise ValueError("expected exact JSON integer")
    return value


def _exact_false(value: object) -> bool:
    if type(value) is not bool or value is not False:
        raise ValueError("expected JSON false")
    return value


def _exact_digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("expected lowercase SHA-256 digest")
    return value


def _aware_utc(value: object, *, label: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be an aware datetime")
    return value.astimezone(UTC)


def _safe_workspace_ref(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 160
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("workspace profile reference is invalid")
    return value


class Ga4KeyEventCreateWorkspaceBinding(StrictModel):
    """The local ledger workspace, intentionally distinct from OAuth profile."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["LOCAL_LEDGER_WORKSPACE"] = "LOCAL_LEDGER_WORKSPACE"
    workspace_profile_ref: str

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator("workspace_profile_ref", mode="before")
    @classmethod
    def exact_workspace_profile_ref(cls, value: object) -> str:
        return _safe_workspace_ref(value)


class Ga4KeyEventCreateSelectionInput(StrictModel):
    """Transient local result of one final checkpoint candidate selection.

    ``contract`` is needed only to rebuild the fresh candidate.  It is not
    retained in the planned action, preserving the candidate boundary's choice
    not to persist customer declaration prose in a plan.
    """

    candidate: Ga4KeyEventCandidateRecord
    contract: ConversionContract
    candidate_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    contract_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    checkpoint_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    evaluated_at: datetime

    @field_validator("candidate", mode="before")
    @classmethod
    def exact_candidate(cls, value: object) -> Ga4KeyEventCandidateRecord:
        if type(value) is not Ga4KeyEventCandidateRecord:
            raise ValueError("selection requires an installed candidate record")
        return value

    @field_validator("contract", mode="before")
    @classmethod
    def exact_contract(cls, value: object) -> ConversionContract:
        if type(value) is not ConversionContract:
            raise ValueError("selection requires an installed conversion contract")
        return value

    @field_validator(
        "candidate_record_digest", "contract_record_digest", "checkpoint_digest", mode="before"
    )
    @classmethod
    def exact_digest(cls, value: object) -> str:
        return _exact_digest(value)

    @field_validator("evaluated_at")
    @classmethod
    def utc_evaluated_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, label="selection evaluation time")

    @model_validator(mode="after")
    def matching_selection(self) -> Ga4KeyEventCreateSelectionInput:
        candidate = revalidate_ga4_key_event_candidate_record(self.candidate)
        contract = _revalidate_contract(self.contract)
        validate_ga4_key_event_candidate_binding(candidate, contract, self.contract_record_digest)
        if contract.definition.effective_from > self.evaluated_at:
            raise ValueError("conversion contract is not effective at selection")
        return self


class Ga4KeyEventCreateObservationInput(StrictModel):
    """Transient, retained EDIT observation plus its final checkpoint provenance."""

    observation_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    checkpoint_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_ref: SafeRef
    configuration_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_configuration_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    read_started_at: datetime
    read_finished_at: datetime
    inventory: Ga4AdminEditKeyEventInventory
    inventory_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_kind: Literal["EDIT_PROFILE_CONFIGURATION_READ"] = "EDIT_PROFILE_CONFIGURATION_READ"
    consistency: Literal["SEQUENTIAL_NONATOMIC"] = "SEQUENTIAL_NONATOMIC"
    granted_scopes: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    create_permission: Literal["NOT_PREPROVABLE"] = "NOT_PREPROVABLE"
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False

    @field_validator(
        "observation_record_digest",
        "checkpoint_digest",
        "configuration_digest",
        "profile_configuration_digest",
        "inventory_digest",
        mode="before",
    )
    @classmethod
    def exact_digest(cls, value: object) -> str:
        return _exact_digest(value)

    @field_validator("read_started_at", "read_finished_at")
    @classmethod
    def utc_read_time(cls, value: datetime) -> datetime:
        return _aware_utc(value, label="EDIT observation time")

    @field_validator("inventory", mode="before")
    @classmethod
    def exact_inventory(cls, value: object) -> Ga4AdminEditKeyEventInventory:
        if type(value) is not Ga4AdminEditKeyEventInventory:
            raise ValueError("action observation requires an installed EDIT inventory")
        return value

    @field_validator("execution_authority", "remote_mutations", mode="before")
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        return _exact_false(value)

    @model_validator(mode="after")
    def observation_interval_and_content(self) -> Ga4KeyEventCreateObservationInput:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("EDIT observation interval is reversed")
        if self.inventory_digest != digest(self.inventory):
            raise ValueError("EDIT observation digest differs from inventory")
        return self


class Ga4KeyEventCreateExecutionBinding(StrictModel):
    """Nonsecret local OAuth/profile facts needed to reconstruct ``AccountIdentity``.

    This binds a configured profile to a locally sealed OAuth association.  It
    is deliberately not a claim that GA4 currently grants CREATE permission,
    that the token has a provider-visible scope, or that the inventory remains
    current.  The workflow maps a lifecycle-issued private binding into this
    public, retained value and the later adapter must resolve it afresh.
    """

    schema_version: Literal[1] = 1
    binding_kind: Literal["GA4_ADMIN_EDIT_LOCAL_OAUTH_ASSOCIATION"] = (
        "GA4_ADMIN_EDIT_LOCAL_OAUTH_ASSOCIATION"
    )
    auth_profile_ref: SafeRef
    profile_configuration_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    configuration_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider: Literal["ga4_admin"] = "ga4_admin"
    purpose: Literal["EDIT"] = "EDIT"
    credential_source: Literal["OS_OAUTH_GOOGLE"] = "OS_OAUTH_GOOGLE"
    principal_ref: SafeRef
    client_application_ref: SafeRef
    scopes: tuple[Literal["https://www.googleapis.com/auth/analytics.edit"], ...] = (
        GA4_KEY_EVENT_CREATE_SCOPE,
    )
    target: Ga4AdminEditInventoryTarget
    local_association_status: Literal["LOCAL_SEALED_OAUTH_ASSOCIATION_ONLY"] = (
        "LOCAL_SEALED_OAUTH_ASSOCIATION_ONLY"
    )
    provider_scope_status: Literal["NOT_PROVEN_BY_RESOURCE_READ"] = "NOT_PROVEN_BY_RESOURCE_READ"
    provider_create_permission: Literal["NOT_PREPROVABLE"] = "NOT_PREPROVABLE"

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator("profile_configuration_digest", "configuration_digest", mode="before")
    @classmethod
    def exact_digest(cls, value: object) -> str:
        return _exact_digest(value)

    @field_validator("scopes", mode="before")
    @classmethod
    def exact_scope_tuple(cls, value: object) -> tuple[str, ...]:
        if type(value) is not tuple or value != (GA4_KEY_EVENT_CREATE_SCOPE,):
            raise ValueError("GA4 Admin CREATE requires one exact EDIT scope")
        return cast(tuple[str, ...], value)

    @field_validator("target", mode="before")
    @classmethod
    def exact_target(cls, value: object) -> Ga4AdminEditInventoryTarget:
        if type(value) is not Ga4AdminEditInventoryTarget:
            raise ValueError("execution binding requires an installed EDIT target")
        return value

    @model_validator(mode="after")
    def fixed_execution_target(self) -> Ga4KeyEventCreateExecutionBinding:
        target = _reparse_target(self.target)
        if (
            target.profile_purpose != "EDIT"
            or target.required_scope != GA4_KEY_EVENT_CREATE_SCOPE
            or target.admin_api_version != "v1alpha"
        ):
            raise ValueError("execution binding target is not the fixed GA4 EDIT target")
        return self


class Ga4KeyEventCreateSelectionBinding(StrictModel):
    """Persisted source references from one final selected candidate checkpoint."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["VERIFIED_LOCAL_CANDIDATE_CONTRACT_SELECTION"] = (
        "VERIFIED_LOCAL_CANDIDATE_CONTRACT_SELECTION"
    )
    candidate_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    contract_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    checkpoint_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    evaluated_at: datetime

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator(
        "candidate_record_digest", "contract_record_digest", "checkpoint_digest", mode="before"
    )
    @classmethod
    def exact_digest(cls, value: object) -> str:
        return _exact_digest(value)

    @field_validator("evaluated_at")
    @classmethod
    def utc_evaluated_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, label="selection evaluation time")


class Ga4KeyEventCreateObservationBinding(StrictModel):
    """Persisted nonsecret provenance for the supplied EDIT inventory."""

    schema_version: Literal[1] = 1
    binding_kind: Literal["GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"] = (
        "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
    )
    observation_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    checkpoint_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_ref: SafeRef
    configuration_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_configuration_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    inventory_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    read_started_at: datetime
    read_finished_at: datetime
    evidence_kind: Literal["EDIT_PROFILE_CONFIGURATION_READ"] = "EDIT_PROFILE_CONFIGURATION_READ"
    consistency: Literal["SEQUENTIAL_NONATOMIC"] = "SEQUENTIAL_NONATOMIC"
    granted_scopes: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    provider_create_permission: Literal["NOT_PREPROVABLE"] = "NOT_PREPROVABLE"

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator(
        "observation_record_digest",
        "checkpoint_digest",
        "configuration_digest",
        "profile_configuration_digest",
        "inventory_digest",
        mode="before",
    )
    @classmethod
    def exact_digest(cls, value: object) -> str:
        return _exact_digest(value)

    @field_validator("read_started_at", "read_finished_at")
    @classmethod
    def utc_read_time(cls, value: datetime) -> datetime:
        return _aware_utc(value, label="EDIT observation time")

    @model_validator(mode="after")
    def ordered_interval(self) -> Ga4KeyEventCreateObservationBinding:
        if self.read_finished_at < self.read_started_at:
            raise ValueError("EDIT observation interval is reversed")
        return self


class Ga4KeyEventCreateNativeRequestBinding(StrictModel):
    """Digest-only fixed POST binding; the closed candidate retains the body locally."""

    schema_version: Literal[1] = 1
    host: Literal["analyticsadmin.googleapis.com"] = GA4_KEY_EVENT_CREATE_HOST
    method: Literal["POST"] = "POST"
    api_version: Literal["v1alpha"] = "v1alpha"
    target_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    body_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator("target_digest", "body_digest", mode="before")
    @classmethod
    def exact_digest(cls, value: object) -> str:
        return _exact_digest(value)


class Ga4KeyEventCreatePlannedAction(StrictModel):
    """One fixed GA4 CREATE action, still without authorization or live proof."""

    schema_version: Literal[1] = 1
    status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = (
        GA4_KEY_EVENT_CREATE_PLAN_STATUS
    )
    workspace_binding: Ga4KeyEventCreateWorkspaceBinding
    candidate: Ga4KeyEventCandidateRecord
    selection_binding: Ga4KeyEventCreateSelectionBinding
    observation_binding: Ga4KeyEventCreateObservationBinding
    execution_binding: Ga4KeyEventCreateExecutionBinding
    native_request_binding: Ga4KeyEventCreateNativeRequestBinding
    semantic_state: Ga4AdminKeyEventCreateSemanticState
    plan: CompositePlan
    declared_destination_relationship: Literal["DECLARED_DESTINATION_SELECTION_ONLY"] = (
        "DECLARED_DESTINATION_SELECTION_ONLY"
    )
    destination_to_execution_profile_mapping: Literal["NOT_EVIDENCED"] = "NOT_EVIDENCED"
    current_state_status: Literal["RECHECK_REQUIRED_BEFORE_EGRESS"] = (
        "RECHECK_REQUIRED_BEFORE_EGRESS"
    )
    provider_create_permission: Literal["NOT_PREPROVABLE"] = "NOT_PREPROVABLE"
    semantic_source_mapping: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    configuration_readback: Literal["REQUIRED_AFTER_CREATE"] = "REQUIRED_AFTER_CREATE"
    browser_network_verification: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    realtime_verification: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    processed_report_verification: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    native_conversion_mapping: Literal["NONE_CREATED"] = "NONE_CREATED"
    execution_authority: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        return _exact_int(value, expected=1)

    @field_validator("candidate", mode="before")
    @classmethod
    def exact_candidate(cls, value: object) -> Ga4KeyEventCandidateRecord:
        if type(value) is not Ga4KeyEventCandidateRecord:
            raise ValueError("planned action requires an installed candidate")
        return value

    @field_validator("semantic_state", mode="before")
    @classmethod
    def exact_semantic_state(cls, value: object) -> Ga4AdminKeyEventCreateSemanticState:
        if type(value) is not Ga4AdminKeyEventCreateSemanticState:
            raise ValueError("planned action requires an installed semantic state")
        return value

    @field_validator("execution_authority", mode="before")
    @classmethod
    def exact_false_authority(cls, value: object) -> bool:
        return _exact_false(value)

    @model_validator(mode="after")
    def exact_reconstructed_plan(self) -> Ga4KeyEventCreatePlannedAction:
        _assert_action_instance(self)
        return self

    @model_serializer(mode="wrap")
    def reject_constructed_or_copied_action(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Do not emit a copied action whose plan or controls no longer bind."""

        try:
            output = handler(self)
            if type(output) is not dict:
                raise ValueError("action serialization")
            _validate_action_raw(output)
            _assert_action_instance(self)
            return output
        except (
            AdhelmError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
            ValidationError,
            UnicodeError,
        ):
            raise ValueError("GA4 key-event CREATE action is invalid") from None


def build_ga4_key_event_create_plan(
    selection: Ga4KeyEventCreateSelectionInput,
    observation: Ga4KeyEventCreateObservationInput,
    execution: Ga4KeyEventCreateExecutionBinding,
    *,
    fresh: Ga4KeyEventCandidateInput,
    fingerprint_key: bytes,
    workspace_profile_ref: str,
    expires_at: datetime,
) -> Ga4KeyEventCreatePlannedAction:
    """Build one exact future GA4 CREATE action from already selected local facts.

    The caller must obtain the selection and observation from its own verified
    checkpoint workflow, and map a lifecycle-issued OAuth binding into
    ``execution``.  This pure function makes neither relationship itself.
    """

    try:
        checked_selection = _checked_selection(selection)
        checked_observation = _checked_observation(observation, fingerprint_key=fingerprint_key)
        checked_execution = _checked_execution(execution)
        checked_fresh = _revalidate_candidate_input(fresh)
        checked_expiry = _checked_expiry(expires_at, checked_selection.evaluated_at)
        workspace = Ga4KeyEventCreateWorkspaceBinding(workspace_profile_ref=workspace_profile_ref)
        _assert_selection_observation_execution(
            checked_selection, checked_observation, checked_execution, checked_expiry
        )
        rebuilt_candidate = compile_ga4_key_event_candidate(
            checked_fresh,
            retained_contract=checked_selection.contract,
            retained_contract_record_digest=checked_selection.contract_record_digest,
        )
        if canonical_bytes(rebuilt_candidate) != canonical_bytes(checked_selection.candidate):
            raise ValueError("fresh candidate mismatch")
        state = build_ga4_admin_key_event_create_semantic_state(
            checked_fresh.key_event,
            checked_observation.inventory,
            fingerprint_key=fingerprint_key,
        )
        if (
            state.candidate_digest != rebuilt_candidate.compilation.compiler_candidate_digest
            or state.inventory_digest != checked_observation.inventory_digest
        ):
            raise ValueError("semantic state binding")
        selection_binding = Ga4KeyEventCreateSelectionBinding(
            candidate_record_digest=checked_selection.candidate_record_digest,
            contract_record_digest=checked_selection.contract_record_digest,
            checkpoint_digest=checked_selection.checkpoint_digest,
            evaluated_at=checked_selection.evaluated_at,
        )
        observation_binding = Ga4KeyEventCreateObservationBinding(
            observation_record_digest=checked_observation.observation_record_digest,
            checkpoint_digest=checked_observation.checkpoint_digest,
            profile_ref=checked_observation.profile_ref,
            configuration_digest=checked_observation.configuration_digest,
            profile_configuration_digest=checked_observation.profile_configuration_digest,
            inventory_digest=checked_observation.inventory_digest,
            read_started_at=checked_observation.read_started_at,
            read_finished_at=checked_observation.read_finished_at,
        )
        native_request = _native_request_binding(rebuilt_candidate.compilation.compiler_candidate)
        action_data: dict[str, object] = {
            "workspace_binding": workspace,
            "candidate": rebuilt_candidate,
            "selection_binding": selection_binding,
            "observation_binding": observation_binding,
            "execution_binding": checked_execution,
            "native_request_binding": native_request,
            "semantic_state": state,
        }
        action_data["plan"] = _build_plan(
            candidate=rebuilt_candidate,
            workspace=workspace,
            selection=selection_binding,
            observation=observation_binding,
            execution=checked_execution,
            state=state,
            native_request=native_request,
            expires_at=checked_expiry,
        )
        return Ga4KeyEventCreatePlannedAction.model_validate(action_data)
    except AdhelmError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_plan() from None


def revalidate_ga4_key_event_create_planned_action(value: object) -> Ga4KeyEventCreatePlannedAction:
    """Strictly parse one persisted action and reconstruct its immutable plan.

    This validates only closed local self-consistency. It cannot establish a
    current provider state, a selected ledger checkpoint, an OAuth lifecycle
    association, permission, or authority. ``validate_ga4_key_event_create_plan_binding``
    repeats the keyed fresh-fact reconstruction at the next trusted boundary.
    """

    try:
        raw = _action_json_mapping(value)
        _validate_action_raw(raw)
        action = _action_from_raw(raw)
        _assert_action_instance(action)
        if canonical_bytes(raw) != canonical_bytes(
            action.model_dump(mode="json", warnings="error")
        ):
            raise ValueError("action canonical shape")
        return action
    except (
        AdhelmError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
        ValidationError,
        UnicodeError,
    ):
        raise _invalid_plan() from None


def validate_ga4_key_event_create_plan_binding(
    action: Ga4KeyEventCreatePlannedAction,
    selection: Ga4KeyEventCreateSelectionInput,
    observation: Ga4KeyEventCreateObservationInput,
    execution: Ga4KeyEventCreateExecutionBinding,
    *,
    fresh: Ga4KeyEventCandidateInput,
    fingerprint_key: bytes,
) -> Ga4KeyEventCreatePlannedAction:
    """Rebuild a stored plan from fresh keyed facts before a future use.

    It neither reads a provider nor consumes OAuth state.  The configured
    workflow and future adapter still need fresh profile/configuration checks,
    current inventory reads, exact authorization, guarded POST and readback.
    """

    checked = revalidate_ga4_key_event_create_planned_action(action)
    expected = build_ga4_key_event_create_plan(
        selection,
        observation,
        execution,
        fresh=fresh,
        fingerprint_key=fingerprint_key,
        workspace_profile_ref=checked.workspace_binding.workspace_profile_ref,
        expires_at=checked.plan.expires_at,
    )
    try:
        if canonical_bytes(checked.model_dump(mode="json", warnings="error")) != canonical_bytes(
            expected.model_dump(mode="json", warnings="error")
        ):
            raise ValueError("fresh facts differ")
        return expected
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise _invalid_plan(
            "GA4 key-event CREATE plan does not match fresh selected facts."
        ) from None


def _checked_selection(value: object) -> Ga4KeyEventCreateSelectionInput:
    if type(value) is not Ga4KeyEventCreateSelectionInput:
        raise ValueError("selection type")
    candidate = revalidate_ga4_key_event_candidate_record(value.candidate)
    contract = _revalidate_contract(value.contract)
    data = {
        "candidate": candidate,
        "contract": contract,
        "candidate_record_digest": value.candidate_record_digest,
        "contract_record_digest": value.contract_record_digest,
        "checkpoint_digest": value.checkpoint_digest,
        "evaluated_at": value.evaluated_at,
    }
    return Ga4KeyEventCreateSelectionInput.model_validate(data)


def _checked_observation(
    value: object, *, fingerprint_key: bytes
) -> Ga4KeyEventCreateObservationInput:
    if type(value) is not Ga4KeyEventCreateObservationInput:
        raise ValueError("observation type")
    inventory = revalidate_ga4_admin_edit_key_event_inventory(
        value.inventory, fingerprint_key=fingerprint_key
    )
    data = {
        "observation_record_digest": value.observation_record_digest,
        "checkpoint_digest": value.checkpoint_digest,
        "profile_ref": value.profile_ref,
        "configuration_digest": value.configuration_digest,
        "profile_configuration_digest": value.profile_configuration_digest,
        "read_started_at": value.read_started_at,
        "read_finished_at": value.read_finished_at,
        "inventory": inventory,
        "inventory_digest": value.inventory_digest,
        "evidence_kind": value.evidence_kind,
        "consistency": value.consistency,
        "granted_scopes": value.granted_scopes,
        "create_permission": value.create_permission,
        "execution_authority": value.execution_authority,
        "remote_mutations": value.remote_mutations,
    }
    return Ga4KeyEventCreateObservationInput.model_validate(data)


def _checked_execution(value: object) -> Ga4KeyEventCreateExecutionBinding:
    if type(value) is not Ga4KeyEventCreateExecutionBinding:
        raise ValueError("execution binding type")
    target = _reparse_target(value.target)
    data = {
        "schema_version": value.schema_version,
        "binding_kind": value.binding_kind,
        "auth_profile_ref": value.auth_profile_ref,
        "profile_configuration_digest": value.profile_configuration_digest,
        "configuration_digest": value.configuration_digest,
        "provider": value.provider,
        "purpose": value.purpose,
        "credential_source": value.credential_source,
        "principal_ref": value.principal_ref,
        "client_application_ref": value.client_application_ref,
        "scopes": value.scopes,
        "target": target,
        "local_association_status": value.local_association_status,
        "provider_scope_status": value.provider_scope_status,
        "provider_create_permission": value.provider_create_permission,
    }
    return Ga4KeyEventCreateExecutionBinding.model_validate(data)


def _checked_expiry(value: object, evaluated_at: datetime) -> datetime:
    expires_at = _aware_utc(value, label="plan expiry")
    delta = expires_at - evaluated_at
    if not _MIN_EXPIRY <= delta <= _MAX_EXPIRY:
        raise ValueError("plan expiry window")
    return expires_at


def _assert_selection_observation_execution(
    selection: Ga4KeyEventCreateSelectionInput,
    observation: Ga4KeyEventCreateObservationInput,
    execution: Ga4KeyEventCreateExecutionBinding,
    expires_at: datetime,
) -> None:
    candidate = selection.candidate
    binding = candidate.compilation.conversion_binding
    if (
        binding.conversion_contract_record_digest != selection.contract_record_digest
        or selection.contract.definition.effective_from > selection.evaluated_at
        or selection.checkpoint_digest != observation.checkpoint_digest
        or observation.read_finished_at > selection.evaluated_at
        or selection.evaluated_at >= expires_at
        or observation.profile_ref != execution.auth_profile_ref
        or observation.configuration_digest != execution.configuration_digest
        or observation.profile_configuration_digest != execution.profile_configuration_digest
        or observation.inventory_digest != digest(observation.inventory)
        or observation.inventory.target != execution.target
    ):
        raise ValueError("selection/observation/execution binding")
    _require_candidate_target(candidate.compilation.compiler_candidate, execution.target)


def _require_candidate_target(
    candidate: Ga4AdminKeyEventCreateCandidate, target: Ga4AdminEditInventoryTarget
) -> None:
    identity = candidate.identity
    if (
        candidate.provider != "ga4_admin"
        or candidate.target.host != GA4_KEY_EVENT_CREATE_HOST
        or candidate.target.method != "POST"
        or candidate.target.api_version != "v1alpha"
        or identity.property_ref != target.property_ref
        or identity.property_client_ref != target.property_client_ref
        or identity.web_stream_ref != target.web_stream_ref
        or identity.web_stream_client_ref != target.web_stream_client_ref
        or identity.api_version != target.admin_api_version
        or identity.required_scope != GA4_KEY_EVENT_CREATE_SCOPE
        or target.profile_purpose != "EDIT"
        or target.required_scope != GA4_KEY_EVENT_CREATE_SCOPE
        or candidate.target.parent != f"properties/{identity.property_client_ref}"
        or candidate.target.path != f"/v1alpha/properties/{identity.property_client_ref}/keyEvents"
    ):
        raise ValueError("candidate/EDIT target mismatch")


def _native_request_binding(
    candidate: Ga4AdminKeyEventCreateCandidate,
) -> Ga4KeyEventCreateNativeRequestBinding:
    identity = candidate.identity
    if (
        candidate.provider != "ga4_admin"
        or candidate.target.host != GA4_KEY_EVENT_CREATE_HOST
        or candidate.target.method != "POST"
        or candidate.target.api_version != "v1alpha"
        or identity.api_version != "v1alpha"
        or identity.required_scope != GA4_KEY_EVENT_CREATE_SCOPE
        or candidate.target.parent != f"properties/{identity.property_client_ref}"
        or candidate.target.path != f"/v1alpha/properties/{identity.property_client_ref}/keyEvents"
    ):
        raise ValueError("fixed native candidate target")
    return Ga4KeyEventCreateNativeRequestBinding(
        target_digest=digest(candidate.target), body_digest=digest(candidate.request)
    )


def _build_plan(
    *,
    candidate: Ga4KeyEventCandidateRecord,
    workspace: Ga4KeyEventCreateWorkspaceBinding,
    selection: Ga4KeyEventCreateSelectionBinding,
    observation: Ga4KeyEventCreateObservationBinding,
    execution: Ga4KeyEventCreateExecutionBinding,
    state: Ga4AdminKeyEventCreateSemanticState,
    native_request: Ga4KeyEventCreateNativeRequestBinding,
    expires_at: datetime,
) -> CompositePlan:
    compiler = candidate.compilation.compiler_candidate
    _require_candidate_target(compiler, execution.target)
    expected_native = _native_request_binding_for_target(compiler.target, compiler.request)
    if native_request != expected_native:
        raise ValueError("native request binding")
    if (
        state.candidate_digest != compiler.candidate_digest
        or state.inventory_digest != observation.inventory_digest
        or state.preconditions.candidate_digest != compiler.candidate_digest
        or state.preconditions.inventory_digest != observation.inventory_digest
    ):
        raise ValueError("semantic state provenance")
    # The CompositePlan is the object a later authorization will bind.  Its
    # identifier therefore commits every mutable source/provenance fact from
    # the outer action, rather than only the native request and semantic state.
    # In particular, a copied action cannot rewrite a configuration, broad
    # inventory-drift, checkpoint, interval, or OAuth-association binding while
    # preserving the same authorizable plan digest.
    action_ref = digest(
        {
            "kind": "ga4-key-event-create-action-v1",
            "workspace": workspace.model_dump(mode="json", warnings="error"),
            "candidate": candidate.model_dump(mode="json", warnings="error"),
            "selection": selection.model_dump(mode="json", warnings="error"),
            "observation": observation.model_dump(mode="json", warnings="error"),
            "execution": execution.model_dump(mode="json", warnings="error"),
            "native_request": native_request.model_dump(mode="json", warnings="error"),
            "semantic_state": state.model_dump(mode="json", warnings="error"),
            "expires_at": expires_at.isoformat(),
        }
    )
    account = AccountIdentity(
        account_ref=compiler.identity.property_ref,
        stable_account_id=compiler.identity.property_client_ref,
        auth_profile_ref=execution.auth_profile_ref,
        principal_ref=execution.principal_ref,
        client_application_ref=execution.client_application_ref,
        scopes=(GA4_KEY_EVENT_CREATE_SCOPE,),
    )
    subplan = SubPlan(
        subplan_id=f"ga4-key-event-create-{action_ref}",
        provider="ga4_admin",
        operation=OperationKind.CREATE,
        account=account,
        provider_api_version="v1alpha",
        capability_version=compiler.capability_version,
        before=state.before,
        after=state.after,
        field_changes=(
            FieldChange(
                path="key-event/semantic-collection",
                before=state.before,
                after=state.after,
            ),
        ),
        max_spend=Money(minor_units=0, currency=execution.target.expected_currency),
        egress=(
            EgressTarget(
                host=GA4_KEY_EVENT_CREATE_HOST,
                purpose=GA4_KEY_EVENT_CREATE_EGRESS_PURPOSE,
            ),
        ),
        recovery_actions=(),
        recovery_disposition="RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION",
        expires_at=expires_at,
    )
    return CompositePlan(
        plan_id=f"ga4-key-event-create-{action_ref}",
        profile_ref=workspace.workspace_profile_ref,
        subplans=(subplan,),
        expires_at=expires_at,
    )


def _native_request_binding_for_target(
    target: Ga4AdminKeyEventCreateTarget, request: Ga4AdminKeyEventCreateRequest
) -> Ga4KeyEventCreateNativeRequestBinding:
    return Ga4KeyEventCreateNativeRequestBinding(
        target_digest=digest(target), body_digest=digest(request)
    )


def _assert_action_instance(action: Ga4KeyEventCreatePlannedAction) -> None:
    if type(action) is not Ga4KeyEventCreatePlannedAction:
        raise ValueError("action type")
    candidate = revalidate_ga4_key_event_candidate_record(action.candidate)
    workspace = _reparse_workspace(action.workspace_binding)
    selection = _reparse_selection_binding(action.selection_binding)
    observation = _reparse_observation_binding(action.observation_binding)
    execution = _reparse_execution_binding(action.execution_binding)
    native_request = _reparse_native_request_binding(action.native_request_binding)
    state = _reparse_semantic_state(action.semantic_state)
    plan = _reparse_plan(action.plan)
    expires_at = _aware_utc(plan.expires_at, label="plan expiry")
    _assert_persisted_bindings(candidate, selection, observation, execution, state, expires_at)
    expected = _build_plan(
        candidate=candidate,
        workspace=workspace,
        selection=selection,
        observation=observation,
        execution=execution,
        state=state,
        native_request=native_request,
        expires_at=expires_at,
    )
    if plan != expected:
        raise ValueError("planned action differs from reconstructed plan")


def _assert_persisted_bindings(
    candidate: Ga4KeyEventCandidateRecord,
    selection: Ga4KeyEventCreateSelectionBinding,
    observation: Ga4KeyEventCreateObservationBinding,
    execution: Ga4KeyEventCreateExecutionBinding,
    state: Ga4AdminKeyEventCreateSemanticState,
    expires_at: datetime,
) -> None:
    conversion = candidate.compilation.conversion_binding
    if (
        conversion.conversion_contract_record_digest != selection.contract_record_digest
        or conversion.contract_effective_from > selection.evaluated_at
        or selection.checkpoint_digest != observation.checkpoint_digest
        or observation.profile_ref != execution.auth_profile_ref
        or observation.configuration_digest != execution.configuration_digest
        or observation.profile_configuration_digest != execution.profile_configuration_digest
        or observation.read_finished_at > selection.evaluated_at
        or selection.evaluated_at >= expires_at
        or not _MIN_EXPIRY <= expires_at - selection.evaluated_at <= _MAX_EXPIRY
    ):
        raise ValueError("persisted action source bindings")
    _require_candidate_target(candidate.compilation.compiler_candidate, execution.target)
    if (
        state.candidate_digest != candidate.compilation.compiler_candidate_digest
        or state.inventory_digest != observation.inventory_digest
        or state.preconditions.candidate_digest != candidate.compilation.compiler_candidate_digest
        or state.preconditions.inventory_digest != observation.inventory_digest
    ):
        raise ValueError("persisted semantic state bindings")


def _json_mapping(value: object, *, expected: type[BaseModel], label: str) -> dict[str, object]:
    try:
        if type(value) is expected:
            raw = value.model_dump_json(warnings="error")
            if len(raw.encode("utf-8")) > _MAX_INPUT_BYTES:
                raise ValueError("size")
            parsed = parse_json(raw, maximum_bytes=_MAX_INPUT_BYTES)
        elif type(value) is dict:
            _validate_json_shape(cast(dict[str, object], value))
            parsed = value
        else:
            raise ValueError("type")
        if type(parsed) is not dict:
            raise ValueError("object")
        return cast(dict[str, object], parsed)
    except (AdhelmError, OverflowError, RecursionError, TypeError, ValueError, UnicodeError):
        raise ValueError(f"{label} JSON shape") from None


def _action_json_mapping(value: object) -> dict[str, object]:
    return _json_mapping(value, expected=Ga4KeyEventCreatePlannedAction, label="action")


def _validate_json_shape(value: dict[str, object]) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    string_bytes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_INPUT_NODES or depth > _MAX_INPUT_DEPTH:
            raise ValueError("JSON nesting")
        if type(current) is dict:
            mapping = cast(dict[object, object], current)
            for key, item in mapping.items():
                if type(key) is not str:
                    raise ValueError("JSON key")
                size = len(key.encode("utf-8"))
                string_bytes += size
                if size > _MAX_INPUT_STRING_BYTES or string_bytes > _MAX_INPUT_BYTES:
                    raise ValueError("JSON key")
                pending.append((item, depth + 1))
        elif type(current) is list:
            pending.extend((item, depth + 1) for item in current)
        elif type(current) is str:
            size = len(current.encode("utf-8"))
            string_bytes += size
            if size > _MAX_INPUT_STRING_BYTES or string_bytes > _MAX_INPUT_BYTES:
                raise ValueError("JSON string")
        elif type(current) is int:
            if current.bit_length() > 53:
                raise ValueError("JSON integer")
        elif type(current) is float:
            if not math.isfinite(current):
                raise ValueError("JSON number")
        elif current is None or type(current) is bool:
            continue
        else:
            raise ValueError("JSON value")


_WORKSPACE_FIELDS = frozenset({"schema_version", "binding_kind", "workspace_profile_ref"})
_SELECTION_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "binding_kind",
        "candidate_record_digest",
        "contract_record_digest",
        "checkpoint_digest",
        "evaluated_at",
    }
)
_OBSERVATION_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "binding_kind",
        "observation_record_digest",
        "checkpoint_digest",
        "profile_ref",
        "configuration_digest",
        "profile_configuration_digest",
        "inventory_digest",
        "read_started_at",
        "read_finished_at",
        "evidence_kind",
        "consistency",
        "granted_scopes",
        "provider_create_permission",
    }
)
_EXECUTION_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "binding_kind",
        "auth_profile_ref",
        "profile_configuration_digest",
        "configuration_digest",
        "provider",
        "purpose",
        "credential_source",
        "principal_ref",
        "client_application_ref",
        "scopes",
        "target",
        "local_association_status",
        "provider_scope_status",
        "provider_create_permission",
    }
)
_NATIVE_REQUEST_FIELDS = frozenset(
    {"schema_version", "host", "method", "api_version", "target_digest", "body_digest"}
)
_TARGET_FIELDS = frozenset(
    {
        "schema_version",
        "profile_purpose",
        "property_ref",
        "property_client_ref",
        "web_stream_ref",
        "web_stream_client_ref",
        "expected_timezone",
        "expected_currency",
        "admin_api_version",
        "required_scope",
    }
)
_PLAN_FIELDS = frozenset(
    {"schema_version", "plan_id", "profile_ref", "subplans", "expires_at", "stop_on_non_verified"}
)
_SUBPLAN_FIELDS = frozenset(
    {
        "subplan_id",
        "provider",
        "operation",
        "account",
        "provider_api_version",
        "capability_version",
        "before",
        "after",
        "field_changes",
        "max_spend",
        "egress",
        "recovery_actions",
        "recovery_disposition",
        "expires_at",
    }
)
_ACCOUNT_FIELDS = frozenset(
    {
        "account_ref",
        "stable_account_id",
        "auth_profile_ref",
        "principal_ref",
        "client_application_ref",
        "scopes",
    }
)
_STATE_REF_FIELDS = frozenset({"fingerprint", "revision"})
_FIELD_CHANGE_FIELDS = frozenset({"path", "before", "after"})
_MONEY_FIELDS = frozenset({"minor_units", "currency"})
_EGRESS_FIELDS = frozenset({"host", "purpose"})
_ACTION_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "workspace_binding",
        "candidate",
        "selection_binding",
        "observation_binding",
        "execution_binding",
        "native_request_binding",
        "semantic_state",
        "plan",
        "declared_destination_relationship",
        "destination_to_execution_profile_mapping",
        "current_state_status",
        "provider_create_permission",
        "semantic_source_mapping",
        "configuration_readback",
        "browser_network_verification",
        "realtime_verification",
        "processed_report_verification",
        "native_conversion_mapping",
        "execution_authority",
    }
)


def _require_mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("expected JSON object")
    return cast(dict[str, object], value)


def _require_keys(value: dict[str, object], expected: frozenset[str]) -> None:
    if set(value) != expected:
        raise ValueError("closed object shape")


def _require_string(value: object, *, maximum: int = _MAX_INPUT_STRING_BYTES) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError("expected bounded JSON string")
    return value


def _require_literal(value: object, expected: str) -> str:
    rendered = _require_string(value)
    if rendered != expected:
        raise ValueError("unexpected literal")
    return rendered


def _require_false(value: object) -> None:
    if type(value) is not bool or value is not False:
        raise ValueError("expected false")


def _require_true(value: object) -> None:
    if type(value) is not bool or value is not True:
        raise ValueError("expected true")


def _require_bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("expected bounded JSON integer")
    return value


def _require_list(value: object, *, maximum: int) -> list[object]:
    if type(value) is not list or len(value) > maximum:
        raise ValueError("expected bounded JSON array")
    return cast(list[object], value)


def _require_exact_model_shape(data: dict[str, object], model: BaseModel) -> None:
    normalized = model.model_dump(mode="json", warnings="error")
    if type(normalized) is not dict or canonical_bytes(data) != canonical_bytes(normalized):
        raise ValueError("noncanonical model shape")


def _reparse_workspace(value: object) -> Ga4KeyEventCreateWorkspaceBinding:
    raw = _json_mapping(value, expected=Ga4KeyEventCreateWorkspaceBinding, label="workspace")
    _validate_workspace_raw(raw)
    try:
        parsed = Ga4KeyEventCreateWorkspaceBinding.model_validate(raw)
        _require_exact_model_shape(raw, parsed)
        return parsed
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("workspace binding") from None


def _reparse_selection_binding(value: object) -> Ga4KeyEventCreateSelectionBinding:
    raw = _json_mapping(value, expected=Ga4KeyEventCreateSelectionBinding, label="selection")
    _validate_selection_binding_raw(raw)
    try:
        parsed = validate_json_value(Ga4KeyEventCreateSelectionBinding, cast(dict[str, Any], raw))
        _require_exact_model_shape(raw, parsed)
        return parsed
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("selection binding") from None


def _reparse_observation_binding(value: object) -> Ga4KeyEventCreateObservationBinding:
    raw = _json_mapping(value, expected=Ga4KeyEventCreateObservationBinding, label="observation")
    _validate_observation_binding_raw(raw)
    try:
        parsed = validate_json_value(Ga4KeyEventCreateObservationBinding, cast(dict[str, Any], raw))
        _require_exact_model_shape(raw, parsed)
        return parsed
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("observation binding") from None


def _reparse_execution_binding(value: object) -> Ga4KeyEventCreateExecutionBinding:
    raw = _json_mapping(value, expected=Ga4KeyEventCreateExecutionBinding, label="execution")
    _validate_execution_binding_raw(raw)
    try:
        typed = dict(raw)
        typed["scopes"] = tuple(cast(list[str], raw["scopes"]))
        typed["target"] = _reparse_target(raw["target"])
        parsed = Ga4KeyEventCreateExecutionBinding.model_validate(typed)
        _require_exact_model_shape(raw, parsed)
        return parsed
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("execution binding") from None


def _reparse_native_request_binding(value: object) -> Ga4KeyEventCreateNativeRequestBinding:
    raw = _json_mapping(
        value, expected=Ga4KeyEventCreateNativeRequestBinding, label="native request"
    )
    _validate_native_request_raw(raw)
    try:
        parsed = Ga4KeyEventCreateNativeRequestBinding.model_validate(raw)
        _require_exact_model_shape(raw, parsed)
        return parsed
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("native request binding") from None


def _reparse_target(value: object) -> Ga4AdminEditInventoryTarget:
    raw = _json_mapping(value, expected=Ga4AdminEditInventoryTarget, label="EDIT target")
    _validate_target_raw(raw)
    try:
        parsed = Ga4AdminEditInventoryTarget.model_validate(raw)
        _require_exact_model_shape(raw, parsed)
        return parsed
    except (OverflowError, RecursionError, TypeError, ValueError, ValidationError, UnicodeError):
        raise ValueError("EDIT target") from None


def _reparse_semantic_state(value: object) -> Ga4AdminKeyEventCreateSemanticState:
    """Use M23's structural parser without claiming keyed reconstruction.

    The state module owns the exact nested raw wire rules.  This parser merely
    makes the closed state safe to retain; the public full binding check below
    rebuilds its HMAC-derived values from fresh input and inventory.
    """

    try:
        parsed = _parse_create_state(value)
        if type(parsed) is not Ga4AdminKeyEventCreateSemanticState:
            raise ValueError("semantic state type")
        return parsed
    except (
        AdhelmError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
        ValidationError,
        UnicodeError,
    ):
        raise ValueError("semantic state") from None


def _reparse_plan(value: object) -> CompositePlan:
    raw = _json_mapping(value, expected=CompositePlan, label="CompositePlan")
    _validate_plan_raw(raw)
    try:
        parsed = validate_json_value(CompositePlan, cast(dict[str, Any], raw))
        _require_exact_model_shape(raw, parsed)
        return parsed
    except (
        AdhelmError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
        ValidationError,
        UnicodeError,
    ):
        raise ValueError("CompositePlan") from None


def _validate_workspace_raw(data: dict[str, object]) -> None:
    _require_keys(data, _WORKSPACE_FIELDS)
    _exact_int(data["schema_version"], expected=1)
    _require_literal(data["binding_kind"], "LOCAL_LEDGER_WORKSPACE")
    _safe_workspace_ref(data["workspace_profile_ref"])


def _validate_selection_binding_raw(data: dict[str, object]) -> None:
    _require_keys(data, _SELECTION_BINDING_FIELDS)
    _exact_int(data["schema_version"], expected=1)
    _require_literal(data["binding_kind"], "VERIFIED_LOCAL_CANDIDATE_CONTRACT_SELECTION")
    for name in ("candidate_record_digest", "contract_record_digest", "checkpoint_digest"):
        _exact_digest(data[name])
    _require_string(data["evaluated_at"])


def _validate_observation_binding_raw(data: dict[str, object]) -> None:
    _require_keys(data, _OBSERVATION_BINDING_FIELDS)
    _exact_int(data["schema_version"], expected=1)
    _require_literal(data["binding_kind"], "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION")
    for name in (
        "observation_record_digest",
        "checkpoint_digest",
        "configuration_digest",
        "profile_configuration_digest",
        "inventory_digest",
    ):
        _exact_digest(data[name])
    _require_string(data["profile_ref"])
    _require_string(data["read_started_at"])
    _require_string(data["read_finished_at"])
    _require_literal(data["evidence_kind"], "EDIT_PROFILE_CONFIGURATION_READ")
    _require_literal(data["consistency"], "SEQUENTIAL_NONATOMIC")
    _require_literal(data["granted_scopes"], "NOT_EVALUATED")
    _require_literal(data["provider_create_permission"], "NOT_PREPROVABLE")


def _validate_execution_binding_raw(data: dict[str, object]) -> None:
    _require_keys(data, _EXECUTION_BINDING_FIELDS)
    _exact_int(data["schema_version"], expected=1)
    _require_literal(data["binding_kind"], "GA4_ADMIN_EDIT_LOCAL_OAUTH_ASSOCIATION")
    for name in ("profile_configuration_digest", "configuration_digest"):
        _exact_digest(data[name])
    for name in ("auth_profile_ref", "principal_ref", "client_application_ref"):
        _require_string(data[name])
    _require_literal(data["provider"], "ga4_admin")
    _require_literal(data["purpose"], "EDIT")
    _require_literal(data["credential_source"], "OS_OAUTH_GOOGLE")
    scopes = _require_list(data["scopes"], maximum=1)
    if scopes != [GA4_KEY_EVENT_CREATE_SCOPE]:
        raise ValueError("execution scope")
    _validate_target_raw(_require_mapping(data["target"]))
    _require_literal(data["local_association_status"], "LOCAL_SEALED_OAUTH_ASSOCIATION_ONLY")
    _require_literal(data["provider_scope_status"], "NOT_PROVEN_BY_RESOURCE_READ")
    _require_literal(data["provider_create_permission"], "NOT_PREPROVABLE")


def _validate_native_request_raw(data: dict[str, object]) -> None:
    _require_keys(data, _NATIVE_REQUEST_FIELDS)
    _exact_int(data["schema_version"], expected=1)
    _require_literal(data["host"], GA4_KEY_EVENT_CREATE_HOST)
    _require_literal(data["method"], "POST")
    _require_literal(data["api_version"], "v1alpha")
    _exact_digest(data["target_digest"])
    _exact_digest(data["body_digest"])


def _validate_target_raw(data: dict[str, object]) -> None:
    _require_keys(data, _TARGET_FIELDS)
    _exact_int(data["schema_version"], expected=1)
    _require_literal(data["profile_purpose"], "EDIT")
    property_id = _require_string(data["property_client_ref"], maximum=20)
    stream_id = _require_string(data["web_stream_client_ref"], maximum=20)
    if (
        not property_id.isascii()
        or not property_id.isdecimal()
        or not stream_id.isascii()
        or not stream_id.isdecimal()
    ):
        raise ValueError("target native identifier")
    if (
        _require_string(data["property_ref"]) != f"ga4_admin.property.{property_id}"
        or _require_string(data["web_stream_ref"]) != f"ga4_admin.web_stream.{stream_id}"
    ):
        raise ValueError("target references")
    _require_string(data["expected_timezone"], maximum=80)
    currency = _require_string(data["expected_currency"], maximum=3)
    if len(currency) != 3 or not currency.isascii() or not currency.isupper():
        raise ValueError("target currency")
    _require_literal(data["admin_api_version"], "v1alpha")
    _require_literal(data["required_scope"], GA4_KEY_EVENT_CREATE_SCOPE)


def _validate_state_ref_raw(data: dict[str, object]) -> None:
    _require_keys(data, _STATE_REF_FIELDS)
    _exact_digest(data["fingerprint"])
    _require_literal(data["revision"], "GA4_KEY_EVENT_CREATE_SEMANTIC_COLLECTION_V1")


def _validate_plan_raw(data: dict[str, object]) -> None:
    _require_keys(data, _PLAN_FIELDS)
    _exact_int(data["schema_version"], expected=1)
    _require_string(data["plan_id"], maximum=160)
    _safe_workspace_ref(data["profile_ref"])
    _require_string(data["expires_at"])
    _require_true(data["stop_on_non_verified"])
    subplans = _require_list(data["subplans"], maximum=1)
    if len(subplans) != 1:
        raise ValueError("fixed plan subplans")
    _validate_subplan_raw(_require_mapping(subplans[0]))


def _validate_subplan_raw(data: dict[str, object]) -> None:
    _require_keys(data, _SUBPLAN_FIELDS)
    _require_string(data["subplan_id"], maximum=160)
    _require_literal(data["provider"], "ga4_admin")
    _require_literal(data["operation"], "CREATE")
    _validate_account_raw(_require_mapping(data["account"]))
    _require_literal(data["provider_api_version"], "v1alpha")
    _require_literal(data["capability_version"], "ga4-admin-key-event-create-contract-2026-09-04")
    _validate_state_ref_raw(_require_mapping(data["before"]))
    _validate_state_ref_raw(_require_mapping(data["after"]))
    changes = _require_list(data["field_changes"], maximum=1)
    if len(changes) != 1:
        raise ValueError("fixed semantic field change")
    change = _require_mapping(changes[0])
    _require_keys(change, _FIELD_CHANGE_FIELDS)
    _require_literal(change["path"], "key-event/semantic-collection")
    _validate_state_ref_raw(_require_mapping(change["before"]))
    _validate_state_ref_raw(_require_mapping(change["after"]))
    money = _require_mapping(data["max_spend"])
    _require_keys(money, _MONEY_FIELDS)
    _require_bounded_int(money["minor_units"], minimum=0, maximum=0)
    currency = _require_string(money["currency"], maximum=3)
    if len(currency) != 3 or not currency.isascii() or not currency.isupper():
        raise ValueError("zero-spend currency")
    egress = _require_list(data["egress"], maximum=1)
    if len(egress) != 1:
        raise ValueError("fixed egress")
    egress_item = _require_mapping(egress[0])
    _require_keys(egress_item, _EGRESS_FIELDS)
    _require_literal(egress_item["host"], GA4_KEY_EVENT_CREATE_HOST)
    _require_literal(egress_item["purpose"], GA4_KEY_EVENT_CREATE_EGRESS_PURPOSE)
    if _require_list(data["recovery_actions"], maximum=0):
        raise ValueError("CREATE recovery actions")
    _require_literal(data["recovery_disposition"], "RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION")
    _require_string(data["expires_at"])


def _validate_account_raw(data: dict[str, object]) -> None:
    _require_keys(data, _ACCOUNT_FIELDS)
    for name in (
        "account_ref",
        "stable_account_id",
        "auth_profile_ref",
        "principal_ref",
        "client_application_ref",
    ):
        _require_string(data[name])
    scopes = _require_list(data["scopes"], maximum=1)
    if scopes != [GA4_KEY_EVENT_CREATE_SCOPE]:
        raise ValueError("fixed account scope")


def _validate_action_raw(data: dict[str, object]) -> None:
    _require_keys(data, _ACTION_FIELDS)
    _exact_int(data["schema_version"], expected=1)
    _require_literal(data["status"], GA4_KEY_EVENT_CREATE_PLAN_STATUS)
    _validate_workspace_raw(_require_mapping(data["workspace_binding"]))
    revalidate_ga4_key_event_candidate_record(data["candidate"])
    _validate_selection_binding_raw(_require_mapping(data["selection_binding"]))
    _validate_observation_binding_raw(_require_mapping(data["observation_binding"]))
    _validate_execution_binding_raw(_require_mapping(data["execution_binding"]))
    _validate_native_request_raw(_require_mapping(data["native_request_binding"]))
    _reparse_semantic_state(data["semantic_state"])
    _validate_plan_raw(_require_mapping(data["plan"]))
    _require_literal(
        data["declared_destination_relationship"], "DECLARED_DESTINATION_SELECTION_ONLY"
    )
    _require_literal(data["destination_to_execution_profile_mapping"], "NOT_EVIDENCED")
    _require_literal(data["current_state_status"], "RECHECK_REQUIRED_BEFORE_EGRESS")
    _require_literal(data["provider_create_permission"], "NOT_PREPROVABLE")
    _require_literal(data["semantic_source_mapping"], "NOT_VERIFIED")
    _require_literal(data["configuration_readback"], "REQUIRED_AFTER_CREATE")
    _require_literal(data["browser_network_verification"], "NOT_EVALUATED")
    _require_literal(data["realtime_verification"], "NOT_EVALUATED")
    _require_literal(data["processed_report_verification"], "NOT_EVALUATED")
    _require_literal(data["native_conversion_mapping"], "NONE_CREATED")
    _require_false(data["execution_authority"])


def _action_from_raw(data: dict[str, object]) -> Ga4KeyEventCreatePlannedAction:
    """Build nested immutable values only after preserving raw scalar checks."""

    try:
        typed = dict(data)
        typed["workspace_binding"] = _reparse_workspace(data["workspace_binding"])
        typed["candidate"] = revalidate_ga4_key_event_candidate_record(data["candidate"])
        typed["selection_binding"] = _reparse_selection_binding(data["selection_binding"])
        typed["observation_binding"] = _reparse_observation_binding(data["observation_binding"])
        typed["execution_binding"] = _reparse_execution_binding(data["execution_binding"])
        typed["native_request_binding"] = _reparse_native_request_binding(
            data["native_request_binding"]
        )
        typed["semantic_state"] = _reparse_semantic_state(data["semantic_state"])
        typed["plan"] = _reparse_plan(data["plan"])
        action = Ga4KeyEventCreatePlannedAction.model_validate(typed)
        _require_exact_model_shape(data, action)
        return action
    except (
        AdhelmError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
        ValidationError,
        UnicodeError,
    ):
        raise ValueError("planned action") from None


__all__ = [
    "GA4_KEY_EVENT_CREATE_EGRESS_PURPOSE",
    "GA4_KEY_EVENT_CREATE_HOST",
    "GA4_KEY_EVENT_CREATE_PLAN_STATUS",
    "GA4_KEY_EVENT_CREATE_SCOPE",
    "Ga4KeyEventCreateExecutionBinding",
    "Ga4KeyEventCreateNativeRequestBinding",
    "Ga4KeyEventCreateObservationBinding",
    "Ga4KeyEventCreateObservationInput",
    "Ga4KeyEventCreatePlannedAction",
    "Ga4KeyEventCreateSelectionBinding",
    "Ga4KeyEventCreateSelectionInput",
    "Ga4KeyEventCreateWorkspaceBinding",
    "build_ga4_key_event_create_plan",
    "revalidate_ga4_key_event_create_planned_action",
    "validate_ga4_key_event_create_plan_binding",
]
