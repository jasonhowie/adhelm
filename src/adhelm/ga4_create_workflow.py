"""Configured GA4 CREATE plan preparation with local retention and no mutation."""

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator

from adhelm.canonical import digest
from adhelm.config import configuration_transaction
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation
from adhelm.ga4_admin_execution_observation import (
    observe_ga4_admin_edit_execution_inventory,
    revalidate_ga4_admin_edit_execution_observation,
    validate_ga4_admin_edit_execution_observation,
)
from adhelm.ga4_create_selection import select_ga4_key_event_create_candidate
from adhelm.ga4_key_event_candidates import Ga4KeyEventCandidateInput, _revalidate_candidate_input
from adhelm.ledger import Ledger
from adhelm.models import Configuration, SafeRef, StrictModel
from adhelm.providers.ga4_create_contract import (
    Ga4KeyEventCreateExecutionBinding,
    Ga4KeyEventCreateObservationInput,
    Ga4KeyEventCreatePlannedAction,
    Ga4KeyEventCreateSelectionInput,
    build_ga4_key_event_create_plan,
)
from adhelm.saved_ga4_admin_edit_inventories import retain_ga4_admin_edit_inventory
from adhelm.saved_ga4_create_plans import retain_ga4_key_event_create_plan
from adhelm.strict_json import validate_json_value


class Ga4KeyEventCreatePlanRequest(StrictModel):
    schema_version: Literal[1] = 1
    profile_id: SafeRef
    candidate_record_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    fresh: Ga4KeyEventCandidateInput
    expires_in_seconds: int = Field(default=300, ge=60, le=900)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("fresh", mode="before")
    @classmethod
    def complete_fresh_input(cls, value: object) -> Ga4KeyEventCandidateInput:
        return _revalidate_candidate_input(value)


@dataclass(frozen=True, slots=True, repr=False)
class RetainedGa4KeyEventCreatePlan:
    """Internal local result; the action requires redaction before agent output."""

    action: Ga4KeyEventCreatePlannedAction
    configuration: Configuration
    plan_record_digest: str
    observation_record_digest: str


def _now() -> datetime:
    return datetime.now(UTC)


def _checked_now() -> datetime:
    try:
        value = _now()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock")
        return value.astimezone(UTC)
    except Exception:
        raise AdhelmError(
            "CLOCK_INVALID", "CREATE planning requires an aware local clock."
        ) from None


def _require_no_recovery(ledger: Ledger) -> None:
    if ledger.unresolved_starts() or ledger.unresolved_runs():
        raise AdhelmError(
            "RECOVERY_REQUIRED",
            "Resolve outstanding mutation history before preparing a GA4 CREATE plan.",
            next_action="Inspect history verify and the retained recovery evidence.",
        )


def prepare_and_retain_ga4_key_event_create_plan(
    directory: Path,
    request: Ga4KeyEventCreatePlanRequest,
    *,
    expected_configuration_digest: str | None = None,
) -> RetainedGa4KeyEventCreatePlan:
    """Own credential resolution, fixed reads and exact plan sources locally.

    The caller supplies no identity, inventory, OAuth binding, state or grant.
    Only source input and configured selections are accepted. All provider I/O
    finishes before local configuration/ledger retention locks are acquired.
    Both retained records remain observations/plans with no execution authority.
    """
    if not isinstance(directory, Path) or type(request) is not Ga4KeyEventCreatePlanRequest:
        raise AdhelmError("INVALID_GA4_CREATE_PLAN_INPUT", "CREATE planning input is invalid.")
    try:
        checked = validate_json_value(
            Ga4KeyEventCreatePlanRequest, request.model_dump(mode="json", warnings="error")
        )
    except Exception:
        raise AdhelmError(
            "INVALID_GA4_CREATE_PLAN_INPUT", "CREATE planning input is invalid."
        ) from None
    stream_id = checked.fresh.key_event.identity.web_stream_client_ref
    configuration, profile, target, expected = validate_ga4_admin_edit_execution_observation(
        directory,
        checked.profile_id,
        stream_id=stream_id,
        expected_configuration_digest=expected_configuration_digest,
    )
    identity = checked.fresh.key_event.identity
    if (
        identity.property_ref != target.property_ref
        or identity.property_client_ref != target.property_client_ref
        or identity.web_stream_ref != target.web_stream_ref
    ):
        raise AdhelmError("IDENTITY_MISMATCH", "CREATE input differs from the configured target.")
    ledger = Ledger(directory)
    initial_time = _checked_now()
    select_ga4_key_event_create_candidate(
        ledger, checked.candidate_record_digest, fresh=checked.fresh, as_of=initial_time
    )
    _require_no_recovery(ledger)
    with configuration_transaction(directory) as transaction:
        transaction.require_unchanged(expected)
        key = export_key()

    observation = observe_ga4_admin_edit_execution_inventory(
        directory,
        profile.profile_id,
        stream_id=stream_id,
        fingerprint_key=key,
        expected_configuration_digest=expected,
    )
    observation = revalidate_ga4_admin_edit_execution_observation(observation, fingerprint_key=key)
    if (
        observation.configuration_digest != expected
        or observation.oauth_binding.profile_configuration_digest != digest(profile)
        or observation.oauth_binding.profile_ref != profile.profile_id
        or observation.target != target
        or observation.read_started_at < initial_time
    ):
        raise AdhelmError("GA4_CREATE_OBSERVATION_MISMATCH", "CREATE observation binding changed.")
    retained_observation = Ga4AdminEditInventoryObservation(
        profile_ref=profile.profile_id,
        configuration_digest=expected,
        profile_configuration_digest=digest(profile),
        read_started_at=observation.read_started_at,
        read_finished_at=observation.read_finished_at,
        inventory=observation.inventory,
        inventory_digest=observation.inventory_digest,
        execution_observation=observation,
    )
    try:
        with configuration_transaction(directory) as transaction:
            transaction.require_unchanged(expected)
            if not hmac.compare_digest(export_key(), key):
                raise AdhelmError("FINGERPRINT_KEY_CHANGED", "The local fingerprint key changed.")
            _require_no_recovery(ledger)
            observed_record = retain_ga4_admin_edit_inventory(
                ledger, retained_observation, fingerprint_key=key
            )
            evaluated = _checked_now()
            if evaluated < observation.read_finished_at:
                raise AdhelmError("CLOCK_INVALID", "The CREATE planning clock moved backwards.")
            selected = select_ga4_key_event_create_candidate(
                ledger, checked.candidate_record_digest, fresh=checked.fresh, as_of=evaluated
            )
            # A new unresolved start could have landed after the earlier check
            # but before this selected checkpoint. Check after selection; any
            # subsequent append is excluded by the atomic retention checkpoint.
            _require_no_recovery(ledger)
            selection = Ga4KeyEventCreateSelectionInput(
                candidate=selected.candidate,
                contract=selected.contract,
                candidate_record_digest=selected.candidate_record_digest,
                contract_record_digest=selected.contract_record_digest,
                checkpoint_digest=selected.checkpoint_digest,
                evaluated_at=evaluated,
            )
            inventory_input = Ga4KeyEventCreateObservationInput(
                observation_record_digest=observed_record["record_digest"],
                checkpoint_digest=selected.checkpoint_digest,
                profile_ref=profile.profile_id,
                configuration_digest=expected,
                profile_configuration_digest=digest(profile),
                read_started_at=observation.read_started_at,
                read_finished_at=observation.read_finished_at,
                inventory=observation.inventory,
                inventory_digest=observation.inventory_digest,
            )
            facts = observation.oauth_binding
            execution = Ga4KeyEventCreateExecutionBinding(
                auth_profile_ref=facts.profile_ref,
                profile_configuration_digest=facts.profile_configuration_digest,
                configuration_digest=expected,
                principal_ref=facts.principal_ref,
                client_application_ref=facts.client_ref,
                scopes=(facts.api_scope,),
                target=target,
            )
            expiry = evaluated + timedelta(seconds=checked.expires_in_seconds)
            action = build_ga4_key_event_create_plan(
                selection,
                inventory_input,
                execution,
                fresh=checked.fresh,
                fingerprint_key=key,
                workspace_profile_ref=ledger.profile_ref,
                expires_at=expiry,
            )

            def before_append() -> None:
                # Ledger already holds its stream lock. Do not query history,
                # credentials or another coordinator from this callback.
                transaction.require_unchanged(expected)
                current = _checked_now()
                if current < evaluated or current >= expiry:
                    raise AdhelmError(
                        "PLAN_EXPIRED",
                        "CREATE plan is outside its allowed time window.",
                        exit_code=ExitCode.AUTHORIZATION,
                    )

            record: dict[str, Any] = retain_ga4_key_event_create_plan(
                ledger,
                action,
                fresh=checked.fresh,
                inventory=observation.inventory,
                fingerprint_key=key,
                before_durable_append=before_append,
            )
    except OSError:
        raise AdhelmError(
            "GA4_CREATE_RETENTION_UNVERIFIED",
            "The read finished, but local observation or plan retention is unverified.",
            next_action="Inspect history and GA4 plan/EDIT inventory catalogs before repeating.",
        ) from None
    return RetainedGa4KeyEventCreatePlan(
        action=action,
        configuration=configuration,
        plan_record_digest=record["record_digest"],
        observation_record_digest=observed_record["record_digest"],
    )
