"""Closed sharing projection for local GA4 key-event candidate history."""

from __future__ import annotations

from typing import Any

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.ga4_key_event_candidates import revalidate_ga4_key_event_candidate_record

_PLANNING_FIELDS = frozenset(
    {
        "status",
        "requires_verified_live_property_and_stream_identity",
        "requires_verified_event_collection_and_reserved_name_check",
        "requires_existing_key_event_inventory",
        "requires_immutable_event_name_and_duplicate_check",
        "requires_pre_apply_drift_recheck",
        "requires_semantics_privacy_and_consent_validation",
        "requires_exact_authorization",
        "requires_configuration_readback",
        "requires_browser_network_verification",
        "requires_realtime_verification",
        "requires_processed_report_verification",
        "requires_separate_native_conversion_mapping",
    }
)
_EFFECT_FIELDS = frozenset(
    {
        "configuration_surface",
        "applies_to",
        "historical_backfill",
        "native_conversion_mapping",
        "browser_or_receipt_proven",
    }
)


def ga4_key_event_candidate_review(value: object) -> dict[str, Any]:
    """Return only reviewed facts; callers still pseudonymize the local references."""
    record = revalidate_ga4_key_event_candidate_record(value)
    compilation = record.compilation
    candidate = compilation.compiler_candidate
    binding = compilation.conversion_binding
    planning = candidate.planning_boundary.model_dump(mode="json")
    effects = candidate.prospective_effects.model_dump(mode="json")
    if set(planning) != _PLANNING_FIELDS or set(effects) != _EFFECT_FIELDS:
        raise AdhelmError(
            "GA4_KEY_EVENT_CANDIDATE_REVIEW_UNSUPPORTED",
            "The installed review cannot preserve this candidate's complete planning boundary.",
            exit_code=2,
        )
    return {
        "schema_version": record.schema_version,
        "event_type": record.event_type,
        "candidate_digest": record.candidate_digest,
        "compilation": {
            "provider": "ga4_admin",
            "capability_version": candidate.capability_version,
            "compiler_candidate_digest": digest(candidate),
            "compiler_input_digest": candidate.input_digest,
            "compiler_evidence": compilation.compiler_evidence,
            "execution_status": compilation.execution_status,
            "semantic_accuracy": compilation.semantic_accuracy,
            "source_event_mapping": compilation.source_event_mapping,
            "effective_time_status": compilation.effective_time_status,
            "requires_new_compilation_for_planning": (
                compilation.requires_new_compilation_for_planning
            ),
            "requires_exact_authorization": compilation.requires_exact_authorization,
            "remote_side_effects": False,
            "execution_authority": False,
            "declared_field_count": len(candidate.declared_field_names),
            "conversion_binding": {
                "conversion_contract_record_digest": binding.conversion_contract_record_digest,
                "content_digest": binding.content_digest,
                "contract_ref": binding.contract_id,
                "contract_version": binding.contract_version,
                "contract_effective_from": binding.contract_effective_from.isoformat(),
                "declared_destination_profile_ref": binding.declared_destination_profile_ref,
                "consent_contract_ref": binding.consent_contract_ref,
                "bound_boolean_field_count": len(binding.bound_boolean_fields),
                "binding_scope": "DECLARED_STRUCTURAL_BOOLEAN_SUBSET",
                "counting_binding": binding.counting_binding,
                "value_binding": binding.value_binding,
                "semantic_accuracy": binding.semantic_accuracy,
                "source_event_mapping": binding.source_event_mapping,
                "effective_time_status": binding.effective_time_status,
            },
            "prospective_effects": effects,
            "planning_boundary": planning,
        },
    }
