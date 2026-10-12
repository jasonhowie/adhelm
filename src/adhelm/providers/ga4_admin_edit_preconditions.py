"""Pure CREATE preconditions for a supplied GA4 Admin EDIT inventory.

This is a local planning prerequisite only.  It does not read GA4, access a
credential, retain data, reserve a key-event name, prove current provider
state or permission, build a plan, or authorize a mutation.
"""

from __future__ import annotations

import hmac
import re
from typing import Any, Literal

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm.canonical import digest
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import StrictModel
from adhelm.providers.ga4_admin import _fingerprint
from adhelm.providers.ga4_admin_compiler import (
    Ga4AdminKeyEventCreateCandidate,
    Ga4AdminKeyEventCreateInput,
    compile_ga4_admin_key_event_create,
)
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditKeyEventInventory,
    Ga4AdminEditStandardKeyEventCapacity,
    revalidate_ga4_admin_edit_key_event_inventory,
)
from adhelm.providers.ga4_admin_preconditions import _revalidate_create_input

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_EDIT_SCOPE = "https://www.googleapis.com/auth/analytics.edit"
_STANDARD_KEY_EVENT_CAP = 30


class Ga4AdminEditKeyEventCreatePreconditions(StrictModel):
    """Redacted local facts from one complete supplied EDIT inventory.

    The result intentionally omits the event name, request, property, stream,
    inventory members, marker values, and fingerprint key.  A matching marker
    establishes local keyed-content integrity only; it is not a provider-origin
    or freshness claim.
    """

    schema_version: Literal[1] = 1
    candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    inventory_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    inventory_scope: Literal["COMPLETE_SUPPLIED_EDIT_INVENTORY"] = (
        "COMPLETE_SUPPLIED_EDIT_INVENTORY"
    )
    fingerprint_key_continuity: Literal["MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY"] = (
        "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY"
    )
    inventory_content_integrity: Literal["MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"] = (
        "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT"
    )
    observed_service_level: Literal["GOOGLE_ANALYTICS_STANDARD"] = "GOOGLE_ANALYTICS_STANDARD"
    complete_observed_key_event_total: int = Field(ge=0, le=_STANDARD_KEY_EVENT_CAP - 1)
    documented_key_event_cap: Literal[30] = 30
    remaining_documented_slots: int = Field(ge=1, le=_STANDARD_KEY_EVENT_CAP)
    capacity_status: Literal["BELOW_DOCUMENTED_STANDARD_KEY_EVENT_CAP"] = (
        "BELOW_DOCUMENTED_STANDARD_KEY_EVENT_CAP"
    )
    duplicate_status: Literal["NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY"] = (
        "NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY"
    )
    current_provider_state: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    reservation_status: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    permission_status: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    remote_side_effects: Literal[False] = False
    execution_authority: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("candidate_digest", "inventory_digest", mode="before")
    @classmethod
    def exact_digest(cls, value: object) -> str:
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise ValueError("precondition digests must be lowercase SHA-256 hex")
        return value

    @field_validator(
        "complete_observed_key_event_total",
        "documented_key_event_cap",
        "remaining_documented_slots",
        mode="before",
    )
    @classmethod
    def exact_capacity_integer(cls, value: object) -> int:
        if type(value) is not int:
            raise ValueError("capacity facts must be JSON integers")
        return value

    @field_validator("remote_side_effects", "execution_authority", mode="before")
    @classmethod
    def exact_false_controls(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("precondition control flags must be false")
        return value

    @model_validator(mode="after")
    def exact_standard_capacity(self) -> Ga4AdminEditKeyEventCreatePreconditions:
        if (
            self.documented_key_event_cap != _STANDARD_KEY_EVENT_CAP
            or self.complete_observed_key_event_total >= self.documented_key_event_cap
            or self.remaining_documented_slots
            != self.documented_key_event_cap - self.complete_observed_key_event_total
        ):
            raise ValueError("observed Standard capacity facts are inconsistent")
        return self

    @model_serializer(mode="wrap")
    def reject_constructed_or_copied_invalid_result(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Never emit bypassed currentness, authority, or arbitrary result data.

        ``model_copy`` and ``model_construct`` bypass Pydantic validators.  This
        redacted result can reach agent-visible output, so validate both direct
        values and the emitted closed shape before serialization returns.
        """

        _validate_result_data(_result_data_from_instance(self))
        output = handler(self)
        if type(output) is not dict:
            raise ValueError("GA4 Admin EDIT precondition serialization must be an object")
        _validate_result_data(output)
        return output


_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_digest",
        "inventory_digest",
        "inventory_scope",
        "fingerprint_key_continuity",
        "inventory_content_integrity",
        "observed_service_level",
        "complete_observed_key_event_total",
        "documented_key_event_cap",
        "remaining_documented_slots",
        "capacity_status",
        "duplicate_status",
        "current_provider_state",
        "reservation_status",
        "permission_status",
        "remote_side_effects",
        "execution_authority",
    }
)
_MISSING_RESULT_VALUE = object()


def _result_data_from_instance(
    value: Ga4AdminEditKeyEventCreatePreconditions,
) -> dict[str, object]:
    data: dict[str, object] = {}
    for field_name in _RESULT_FIELDS:
        field_value = getattr(value, field_name, _MISSING_RESULT_VALUE)
        if field_value is _MISSING_RESULT_VALUE:
            raise ValueError("GA4 Admin EDIT precondition result has an invalid closed shape")
        data[field_name] = field_value
    return data


def _validate_result_data(data: object) -> None:
    """Check the complete nonsecret result surface without value normalization."""

    if type(data) is not dict or set(data) != _RESULT_FIELDS:
        raise ValueError("GA4 Admin EDIT precondition result has an invalid closed shape")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("GA4 Admin EDIT precondition result schema version is invalid")
    for name in ("candidate_digest", "inventory_digest"):
        value = data[name]
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise ValueError("GA4 Admin EDIT precondition result digest is invalid")
    expected_literals = {
        "inventory_scope": "COMPLETE_SUPPLIED_EDIT_INVENTORY",
        "fingerprint_key_continuity": "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY",
        "inventory_content_integrity": "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT",
        "observed_service_level": "GOOGLE_ANALYTICS_STANDARD",
        "capacity_status": "BELOW_DOCUMENTED_STANDARD_KEY_EVENT_CAP",
        "duplicate_status": "NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY",
        "current_provider_state": "NOT_ESTABLISHED",
        "reservation_status": "NOT_ESTABLISHED",
        "permission_status": "NOT_EVALUATED",
    }
    for name, expected in expected_literals.items():
        if type(data[name]) is not str or data[name] != expected:
            raise ValueError("GA4 Admin EDIT precondition result control is invalid")
    for name in (
        "complete_observed_key_event_total",
        "documented_key_event_cap",
        "remaining_documented_slots",
    ):
        if type(data[name]) is not int:
            raise ValueError("GA4 Admin EDIT precondition result capacity is invalid")
    total = data["complete_observed_key_event_total"]
    cap = data["documented_key_event_cap"]
    remaining = data["remaining_documented_slots"]
    if (
        cap != _STANDARD_KEY_EVENT_CAP
        or not 0 <= total < cap
        or remaining != cap - total
        or not 1 <= remaining <= cap
    ):
        raise ValueError("GA4 Admin EDIT precondition result capacity is inconsistent")
    for name in ("remote_side_effects", "execution_authority"):
        if type(data[name]) is not bool or data[name] is not False:
            raise ValueError("GA4 Admin EDIT precondition result control is invalid")


def evaluate_ga4_edit_key_event_create_preconditions(
    fresh: Ga4AdminKeyEventCreateInput,
    supplied: Ga4AdminEditKeyEventInventory,
    *,
    fingerprint_key: bytes,
) -> Ga4AdminEditKeyEventCreatePreconditions:
    """Check one fresh compile input against a complete supplied EDIT inventory.

    Both marker checks occur in the strict EDIT inventory revalidator before
    the event-name HMAC comparison.  This function has no provider, file,
    credential, ledger, browser, plan, approval, or execution surface.
    """

    checked_fresh = _checked_fresh_input(fresh)
    checked_inventory = _checked_edit_inventory(supplied, fingerprint_key=fingerprint_key)
    try:
        candidate = compile_ga4_admin_key_event_create(checked_fresh)
    except AdhelmError:
        raise
    except (TypeError, ValueError, ValidationError):
        raise _invalid_precondition() from None

    _require_matching_target(candidate, checked_inventory)
    capacity = checked_inventory.capacity
    if type(capacity) is not Ga4AdminEditStandardKeyEventCapacity:
        raise AdhelmError(
            "GA4_KEY_EVENT_CAPACITY_NOT_EVIDENCED",
            (
                "The supplied GA4 Admin EDIT inventory has no documented key-event cap "
                "for its observed service level."
            ),
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Collect supported capacity evidence before planning a GA4 key-event create."
            ),
        )
    if capacity.complete_observed_key_event_total >= capacity.documented_key_event_cap:
        raise AdhelmError(
            "GA4_KEY_EVENT_DOCUMENTED_CAP_REACHED",
            "The supplied GA4 Admin EDIT inventory is at its documented key-event cap.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Remove no key events automatically; collect a new inventory and review capacity."
            ),
        )

    event_name_fingerprint = _fingerprint(
        fingerprint_key,
        "key-event-name",
        candidate.request.eventName,
    )
    if any(
        hmac.compare_digest(item.event_name_fingerprint, event_name_fingerprint)
        for item in checked_inventory.key_events
    ):
        raise AdhelmError(
            "GA4_KEY_EVENT_DUPLICATE_SUPPLIED_INVENTORY",
            "The proposed key event is already observed in the complete supplied inventory.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Select a distinct declared event or collect fresh evidence before planning."
            ),
        )

    return Ga4AdminEditKeyEventCreatePreconditions(
        candidate_digest=candidate.candidate_digest,
        inventory_digest=digest(checked_inventory),
        complete_observed_key_event_total=capacity.complete_observed_key_event_total,
        remaining_documented_slots=capacity.remaining_documented_slots,
    )


def _checked_fresh_input(value: object) -> Ga4AdminKeyEventCreateInput:
    """Reuse the installed raw-JSON/create-input boundary without its READ inventory."""

    try:
        return _revalidate_create_input(value)
    except AdhelmError:
        raise _invalid_precondition() from None


def _checked_edit_inventory(
    value: object, *, fingerprint_key: bytes
) -> Ga4AdminEditKeyEventInventory:
    if type(value) is not Ga4AdminEditKeyEventInventory:
        raise _invalid_precondition()
    try:
        return revalidate_ga4_admin_edit_key_event_inventory(
            value,
            fingerprint_key=fingerprint_key,
        )
    except AdhelmError as exc:
        if exc.code in {
            "FINGERPRINT_KEY_REQUIRED",
            "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH",
            "GA4_FINGERPRINT_EDIT_INVENTORY_CONTENT_MISMATCH",
        }:
            raise
        raise _invalid_precondition() from None


def _require_matching_target(
    candidate: Ga4AdminKeyEventCreateCandidate,
    inventory: Ga4AdminEditKeyEventInventory,
) -> None:
    identity = candidate.identity
    target = inventory.target
    if (
        identity.property_ref != target.property_ref
        or identity.property_client_ref != target.property_client_ref
        or identity.web_stream_ref != target.web_stream_ref
        or identity.web_stream_client_ref != target.web_stream_client_ref
        or identity.api_version != target.admin_api_version
        or identity.required_scope != _EDIT_SCOPE
        or target.required_scope != _EDIT_SCOPE
        or target.profile_purpose != "EDIT"
    ):
        raise AdhelmError(
            "GA4_ADMIN_EDIT_INVENTORY_IDENTITY_MISMATCH",
            "The fresh GA4 key-event input and supplied EDIT inventory target differ.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Collect a matching configured GA4 Admin EDIT inventory before planning.",
        )


def _invalid_precondition() -> AdhelmError:
    return AdhelmError(
        "INVALID_GA4_ADMIN_EDIT_PRECONDITION",
        "GA4 Admin EDIT key-event precondition input is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


__all__ = [
    "Ga4AdminEditKeyEventCreatePreconditions",
    "evaluate_ga4_edit_key_event_create_preconditions",
]
