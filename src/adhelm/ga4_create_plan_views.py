"""Allowlisted sharing views for retained GA4 CREATE plans."""

from datetime import UTC, datetime
from typing import Any, cast

from adhelm._installed_approval_execution_status import (
    INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
)
from adhelm.errors import AdhelmError
from adhelm.ga4_create_plan_records import revalidate_ga4_key_event_create_plan_record


def ga4_key_event_create_plan_public_record(value: object) -> dict[str, Any]:
    """Exclude native paths/bodies and markers; callers still pseudonymize refs."""
    record = revalidate_ga4_key_event_create_plan_record(value)
    action = record.action
    candidate = action.candidate.compilation.compiler_candidate
    conversion = action.candidate.compilation.conversion_binding
    target = action.execution_binding.target
    state = action.semantic_state
    subplan = action.plan.subplans[0]
    return {
        "schema_version": 1,
        "event_type": "GA4_KEY_EVENT_CREATE_PLAN",
        "provider": "ga4_admin",
        "operation": "CREATE",
        "capability_status": action.status,
        "action_digest": record.action_digest,
        "plan_digest": record.plan_digest,
        "subplan_digests": list(action.plan.subplan_digests),
        "workspace_profile_ref": action.plan.profile_ref,
        "auth_profile_ref": action.execution_binding.auth_profile_ref,
        "property_ref": target.property_ref,
        "web_stream_ref": target.web_stream_ref,
        "event_ref": candidate.request.eventName,
        "declared_destination_profile_ref": conversion.declared_destination_profile_ref,
        "destination_to_execution_profile_mapping": action.destination_to_execution_profile_mapping,
        "expected_timezone": target.expected_timezone,
        "expected_currency": target.expected_currency,
        "candidate_record_digest": action.selection_binding.candidate_record_digest,
        "contract_record_digest": action.selection_binding.contract_record_digest,
        "contract_version": conversion.contract_version,
        "contract_effective_from": conversion.contract_effective_from.isoformat(),
        "observation_record_digest": action.observation_binding.observation_record_digest,
        "configuration_digest": action.execution_binding.configuration_digest,
        "profile_configuration_digest": action.execution_binding.profile_configuration_digest,
        "inventory_digest": state.inventory_digest,
        "read_started_at": action.observation_binding.read_started_at.isoformat(),
        "read_finished_at": action.observation_binding.read_finished_at.isoformat(),
        "evaluated_at": action.selection_binding.evaluated_at.isoformat(),
        "expires_at": record.expires_at.isoformat(),
        "before": {"member_count": state.before_member_count, "state": state.before.fingerprint},
        "after": {"member_count": state.after_member_count, "state": state.after.fingerprint},
        "counting_method": candidate.request.countingMethod,
        "default_value": "NONE",
        "configuration_effect": "IMMEDIATE_LIVE_GA4_KEY_EVENT_NO_DRAFT",
        "historical_backfill": "NONE",
        "maximum_incremental_spend": record.maximum_incremental_spend.model_dump(mode="json"),
        "egress_hosts": [item.host for item in subplan.egress],
        "recovery_disposition": subplan.recovery_disposition,
        "remote_compensation_actions": [],
        "provider_conditional_write": "NOT_DOCUMENTED",
        "provider_idempotency": "NOT_DOCUMENTED",
        "ambiguous_attempt": "UNRESOLVED_NO_BLIND_RETRY",
        "protected_grant_provenance": "LOCAL_SEALED_OAUTH_ASSOCIATION_ONLY",
        "provider_create_permission": action.provider_create_permission,
        "semantic_source_mapping": action.semantic_source_mapping,
        "current_state_status": action.current_state_status,
        "configuration_readback": action.configuration_readback,
        "browser_network_verification": action.browser_network_verification,
        "realtime_verification": action.realtime_verification,
        "processed_report_verification": action.processed_report_verification,
        "native_conversion_mapping": action.native_conversion_mapping,
        "historical_evidence_status": record.historical_evidence_status,
        "execution_authority": False,
        "requires_current_kernel_checks": True,
        "execution_command": INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
    }


def preview_ga4_key_event_create_plan(
    value: object, *, opaque_key: bytes, reviewed_at: datetime
) -> dict[str, Any]:
    if type(opaque_key) is not bytes or not 32 <= len(opaque_key) <= 4096:
        raise AdhelmError("EXPORT_KEY_REQUIRED", "Plan review requires a private sharing key.")
    if (
        type(reviewed_at) is not datetime
        or reviewed_at.tzinfo is None
        or reviewed_at.utcoffset() is None
    ):
        raise AdhelmError("CLOCK_INVALID", "Plan review requires an aware instant.")
    from adhelm.projections import _redact_payload

    record = revalidate_ga4_key_event_create_plan_record(value)
    data = _redact_payload(record.model_dump(mode="json"), opaque_key, False)
    if type(data) is not dict:
        raise AdhelmError("INVALID_GA4_CREATE_PLAN_REVIEW", "Plan review is invalid.")
    data["reviewed_at"] = reviewed_at.astimezone(UTC).isoformat()
    data["expired_at_review"] = record.expires_at <= reviewed_at
    return cast(dict[str, Any], data)
