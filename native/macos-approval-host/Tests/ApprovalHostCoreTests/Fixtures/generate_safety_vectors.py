"""Generate canonical native-host fixtures from the real Python safety models.

This script is deliberately offline: its only inputs are fixed synthetic values
below and the checked-out source models.  It reads no configuration, credential
store, customer file, provider, or network.  Run it from the repository root:

  UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run python \
    native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/generate_safety_vectors.py
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import rfc8785

from adhelm.approval_protocol import (
    ApprovalHostConfiguration,
    approval_review_facts,
    build_ga4_key_event_create_challenge,
)
from adhelm.canonical import digest
from adhelm.conversion_contracts import (
    ConversionContract,
    ConversionContractInput,
    compile_conversion_contract,
)
from adhelm.ga4_key_event_candidates import (
    Ga4KeyEventCandidateInput,
    compile_ga4_key_event_candidate,
)
from adhelm.plan_records import revalidate_openai_pause_action
from adhelm.plan_views import preview_openai_pause
from adhelm.providers.ga4_admin_edit_inventory import (
    Ga4AdminEditInventoryTarget,
    Ga4AdminEditKeyEventInventory,
    Ga4AdminEditKeyEventInventoryClient,
)
from adhelm.providers.ga4_create_contract import (
    Ga4KeyEventCreateExecutionBinding,
    Ga4KeyEventCreateObservationInput,
    Ga4KeyEventCreatePlannedAction,
    Ga4KeyEventCreateSelectionInput,
    build_ga4_key_event_create_plan,
    revalidate_ga4_key_event_create_planned_action,
)
from adhelm.providers.openai_pause_contract import (
    OpenAIPauseBinding,
    OpenAIPauseCampaignBaseline,
    build_openai_pause_plan,
)
from adhelm.safety import (
    DelegationPolicy,
    DelegationPolicySource,
    MoneyLimit,
    OperationKind,
)
from adhelm.transport import SafeHttpClient

FIXTURE_DIR = Path(__file__).parent
HOST_REF = "host:adhelm-macos-approval-v1"
NONCE = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
WHOLE_SECONDS_NONCE = base64.urlsafe_b64encode(bytes(range(32, 64))).decode().rstrip("=")
GA4_NONCE = base64.urlsafe_b64encode(bytes(range(64, 96))).decode().rstrip("=")
ISSUED_AT = "2030-01-01T00:00:00Z"
CHALLENGE_EXPIRES_AT = "2030-01-01T00:04:00Z"
PLAN_EXPIRES_AT = datetime(2030, 1, 1, 0, 10, tzinfo=UTC)
GA4_NOW = datetime(2030, 1, 1, tzinfo=UTC)
GA4_FINGERPRINT_KEY = b"k" * 32
GA4_CONTRACT_RECORD_DIGEST = "a" * 64
GA4_PROPERTY_ID = "123"
GA4_STREAM_ID = "456"


def _canonical(value: object) -> bytes:
    return rfc8785.dumps(value)


def _write(name: str, value: object) -> bytes:
    encoded = _canonical(value)
    (FIXTURE_DIR / name).write_bytes(encoded)
    return encoded


def _ga4_contract() -> ConversionContract:
    data = {
        "schema_version": 1,
        "contract_id": "conversion.fixture",
        "version": 1,
        "supersedes_ref": None,
        "business_outcome": {
            "outcome_ref": "outcome.fixture",
            "statement": {"statement": "A qualified request confirmed by the customer"},
        },
        "authoritative_source": {
            "source_kind": "CRM_AGGREGATE",
            "source_ref": "source.fixture",
            "statement": {"statement": "A customer-configured fixed aggregate report"},
        },
        "qualifying_occurrence": {
            "occurrence_ref": "occurrence.fixture",
            "statement": {"statement": "The source confirms each qualified occurrence"},
        },
        "exclusions": [],
        "counting_and_deduplication": {
            "counting_semantics": "EACH_QUALIFYING_OCCURRENCE",
            "deduplication_semantics": "NOT_APPLICABLE",
            "duplicate_occurrence_handling": "COUNT_EACH",
            "declared_window": "NOT_APPLICABLE",
        },
        "value_and_currency": {
            "value_semantics": "NO_MONETARY_VALUE",
            "fixed_value": None,
            "currency": None,
            "null_value_semantics": "NO_MONETARY_VALUE",
        },
        "allowed_parameter_schema": [
            {
                "name": "qualified",
                "value_type": "BOOLEAN",
                "required": True,
                "integer_minimum": None,
                "integer_maximum": None,
                "allowed_values": [],
            }
        ],
        "prohibited_parameters": [
            "PERSONAL_IDENTIFIER",
            "AUTHENTICATION_SECRET",
            "CLICK_IDENTIFIER",
            "URL_OR_QUERY",
            "DOM_OR_PAGE_TEXT",
            "FORM_VALUE",
            "CRM_RECORD_IDENTIFIER",
        ],
        "consent_contract_ref": "consent.fixture",
        "destination_profiles": ["ga4.logical.destination"],
        "effective_from": "2029-12-31T00:00:00Z",
    }
    parsed = ConversionContractInput.model_validate_json(_canonical(data))
    return compile_conversion_contract(parsed)


def _ga4_fresh_input() -> Ga4KeyEventCandidateInput:
    data = {
        "schema_version": 1,
        "conversion_contract_record_digest": GA4_CONTRACT_RECORD_DIGEST,
        "declared_destination_profile_ref": "ga4.logical.destination",
        "key_event": {
            "event_contract": {
                "event_contract_ref": "event.fixture",
                "event_name": "qualified_request",
                "semantics": {
                    "semantics_ref": "semantics.fixture",
                    "customer_definition": "A customer-defined qualified request event.",
                    "counting_rule": "Count each declared event occurrence.",
                },
                "fields": [{"name": "qualified", "value_type": "BOOLEAN", "required": True}],
            },
            "identity": {
                "property_ref": f"ga4_admin.property.{GA4_PROPERTY_ID}",
                "property_client_ref": GA4_PROPERTY_ID,
                "web_stream_ref": f"ga4_admin.web_stream.{GA4_STREAM_ID}",
                "web_stream_client_ref": GA4_STREAM_ID,
            },
            "counting_method": "ONCE_PER_EVENT",
            "default_value": None,
        },
    }
    return Ga4KeyEventCandidateInput.model_validate_json(_canonical(data))


def _ga4_target() -> Ga4AdminEditInventoryTarget:
    return Ga4AdminEditInventoryTarget(
        property_ref=f"ga4_admin.property.{GA4_PROPERTY_ID}",
        property_client_ref=GA4_PROPERTY_ID,
        web_stream_ref=f"ga4_admin.web_stream.{GA4_STREAM_ID}",
        web_stream_client_ref=GA4_STREAM_ID,
        expected_timezone="UTC",
        expected_currency="USD",
    )


def _ga4_inventory() -> Ga4AdminEditKeyEventInventory:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1beta/properties/{GA4_PROPERTY_ID}":
            return httpx.Response(
                200,
                json={
                    "name": f"properties/{GA4_PROPERTY_ID}",
                    "timeZone": "UTC",
                    "currencyCode": "USD",
                    "serviceLevel": "GOOGLE_ANALYTICS_STANDARD",
                },
            )
        if request.url.path == f"/v1beta/properties/{GA4_PROPERTY_ID}/dataStreams/{GA4_STREAM_ID}":
            return httpx.Response(
                200,
                json={
                    "name": f"properties/{GA4_PROPERTY_ID}/dataStreams/{GA4_STREAM_ID}",
                    "type": "WEB_DATA_STREAM",
                },
            )
        assert request.url.path == f"/v1alpha/properties/{GA4_PROPERTY_ID}/keyEvents"
        return httpx.Response(200, json={"keyEvents": []})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        return Ga4AdminEditKeyEventInventoryClient(
            SafeHttpClient(client, {"analyticsadmin.googleapis.com"}),
            lambda: "synthetic-fixture-token",
            fingerprint_key=GA4_FINGERPRINT_KEY,
        ).read_inventory(_ga4_target())


def _ga4_action() -> Ga4KeyEventCreatePlannedAction:
    contract = _ga4_contract()
    fresh = _ga4_fresh_input()
    candidate = compile_ga4_key_event_candidate(
        fresh,
        retained_contract=contract,
        retained_contract_record_digest=GA4_CONTRACT_RECORD_DIGEST,
    )
    inventory = _ga4_inventory()
    selection = Ga4KeyEventCreateSelectionInput(
        candidate=candidate,
        contract=contract,
        candidate_record_digest="b" * 64,
        contract_record_digest=GA4_CONTRACT_RECORD_DIGEST,
        checkpoint_digest="c" * 64,
        evaluated_at=GA4_NOW,
    )
    observation = Ga4KeyEventCreateObservationInput(
        observation_record_digest="d" * 64,
        checkpoint_digest="c" * 64,
        profile_ref="ga4.edit.profile",
        configuration_digest="e" * 64,
        profile_configuration_digest="f" * 64,
        read_started_at=GA4_NOW - timedelta(seconds=3),
        read_finished_at=GA4_NOW - timedelta(seconds=1),
        inventory=inventory,
        inventory_digest=digest(inventory),
    )
    execution = Ga4KeyEventCreateExecutionBinding(
        auth_profile_ref="ga4.edit.profile",
        profile_configuration_digest="f" * 64,
        configuration_digest="e" * 64,
        principal_ref="principal.fixture",
        client_application_ref="client.fixture",
        target=inventory.target,
    )
    return revalidate_ga4_key_event_create_planned_action(
        build_ga4_key_event_create_plan(
            selection,
            observation,
            execution,
            fresh=fresh,
            fingerprint_key=GA4_FINGERPRINT_KEY,
            workspace_profile_ref="workspace.default",
            expires_at=PLAN_EXPIRES_AT,
        )
    )


def _write_configuration_vectors() -> None:
    cases = (
        ("defaults", ApprovalHostConfiguration(HOST_REF, "key:receipt-v1")),
        (
            "minimum_limits",
            ApprovalHostConfiguration(HOST_REF, "key:receipt-v1", 1, 1, 0, 1, 1),
        ),
        (
            "maximum_limits",
            ApprovalHostConfiguration(HOST_REF, "key:receipt-v1", 65_536, 300, 300, 6, 64),
        ),
        (
            "unicode_references",
            ApprovalHostConfiguration('host:review-"é😀', "key:receipt-\\東京"),
        ),
    )
    _write(
        "approval-host-configuration-vectors-v1.json",
        {
            "schema_version": 1,
            "scope": "PROTOCOL_CONFIGURATION_ONLY_NOT_INSTALLATION_ATTESTATION",
            "vectors": [
                {
                    "case_id": name,
                    "configuration": configuration.to_data(),
                    "canonical_json": configuration.canonical_bytes.decode("utf-8"),
                    "configuration_digest": configuration.configuration_digest,
                }
                for name, configuration in cases
            ],
        },
    )


def main() -> None:
    _write_configuration_vectors()
    binding = OpenAIPauseBinding(
        account_ref="openai-account-local-1",
        account_client_ref="openai-account-native-1",
        profile_ref="openai-key-profile-1",
        credential_binding_ref="credential-binding:sha256:" + "a" * 64,
        account_timezone="America/Chicago",
        account_currency="USD",
        account_status="active",
        campaign=OpenAIPauseCampaignBaseline(
            campaign_ref="openai-campaign-local-1",
            campaign_client_ref="openai-campaign-native-1",
            status="active",
            bidding_type="clicks",
            lifetime_spend_limit_micros="2500000",
            start_at=datetime(2029, 12, 31, 18, 0, 0, 123456, tzinfo=UTC),
            end_at=datetime(2030, 1, 31, 18, 0, 0, 654321, tzinfo=UTC),
        ),
    )
    action = build_openai_pause_plan(
        binding,
        expires_at=PLAN_EXPIRES_AT,
        workspace_profile_ref="workspace-ledger-1",
    )
    action = revalidate_openai_pause_action(action)
    # The retained action model alone intentionally preserves legacy wire
    # values. The installed review boundary is stricter: fixture vectors must
    # also be renderable by the same closed review path users receive.
    preview_openai_pause(action, opaque_key=b"\0" * 32, now=datetime(2030, 1, 1, tzinfo=UTC))
    action_json = action.model_dump(mode="json")
    whole_seconds_binding = OpenAIPauseBinding(
        account_ref="openai-account-local-whole-1",
        account_client_ref="openai-account-native-whole-1",
        profile_ref="openai-key-profile-whole-1",
        credential_binding_ref="credential-binding:sha256:" + "d" * 64,
        account_timezone="America/Chicago",
        account_currency="USD",
        account_status="active",
        campaign=OpenAIPauseCampaignBaseline(
            campaign_ref="openai-campaign-local-whole-1",
            campaign_client_ref="openai-campaign-native-whole-1",
            status="active",
            bidding_type="clicks",
            lifetime_spend_limit_micros="2500000",
            start_at=datetime(2029, 12, 31, 18, 0, 0, tzinfo=UTC),
            end_at=datetime(2030, 1, 31, 18, 0, 0, tzinfo=UTC),
        ),
    )
    whole_seconds_action = build_openai_pause_plan(
        whole_seconds_binding,
        expires_at=PLAN_EXPIRES_AT,
        workspace_profile_ref="workspace-ledger-whole-1",
    )
    whole_seconds_action = revalidate_openai_pause_action(whole_seconds_action)
    preview_openai_pause(
        whole_seconds_action,
        opaque_key=b"\0" * 32,
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    whole_seconds_action_json = whole_seconds_action.model_dump(mode="json")
    ga4_action = _ga4_action()
    ga4_action_json = ga4_action.model_dump(mode="json", warnings="error")
    ga4_configuration = ApprovalHostConfiguration(
        host_ref=HOST_REF,
        signing_key_ref="approval-key:fixture-v1",
    )
    ga4_challenge = build_ga4_key_event_create_challenge(
        ga4_action,
        grantor_ref="customer-grantor-1",
        expires_at=datetime(2030, 1, 1, 0, 4, tzinfo=UTC),
        configuration=ga4_configuration,
        nonce=base64.urlsafe_b64decode(GA4_NONCE + "="),
        issued_at=GA4_NOW,
        now=GA4_NOW,
    )
    ga4_review = approval_review_facts(ga4_challenge, configuration=ga4_configuration)
    policy = DelegationPolicy(
        policy_id="native-approval-policy-1",
        policy_version=1,
        source=DelegationPolicySource.USER_CONTROLLED_CONFIGURATION,
        grantor_ref="customer-grantor-1",
        source_authorization_ref="evidence:sha256:" + "b" * 64,
        configuration_host_ref=HOST_REF,
        configuration_interaction_ref="evidence:sha256:" + "c" * 64,
        authorized_agent_or_host_ref="customer-agent-1",
        account_stable_ids=(binding.account_client_ref,),
        allowed_operations=(OperationKind.PAUSE,),
        maximum_change_per_operation=(MoneyLimit(currency="USD", maximum_minor_units=0),),
        cumulative_change_bounds=(MoneyLimit(currency="USD", maximum_minor_units=0),),
        effective_from=datetime(2029, 12, 31, 0, 0, tzinfo=UTC),
        expires_at=datetime(2030, 2, 1, 0, 0, tzinfo=UTC),
        allowed_egress_hosts=("api.ads.openai.com",),
    )
    policy_json = policy.model_dump(mode="json")

    pause_challenge = {
        "schema_version": 1,
        "kind": "OPENAI_PAUSE_PLAN",
        "nonce": NONCE,
        "issued_at": ISSUED_AT,
        "expires_at": CHALLENGE_EXPIRES_AT,
        "grantor_ref": "customer-grantor-1",
        "action": action_json,
    }
    whole_seconds_pause_challenge = {
        "schema_version": 1,
        "kind": "OPENAI_PAUSE_PLAN",
        "nonce": WHOLE_SECONDS_NONCE,
        "issued_at": ISSUED_AT,
        "expires_at": CHALLENGE_EXPIRES_AT,
        "grantor_ref": "customer-grantor-whole-1",
        "action": whole_seconds_action_json,
    }
    install_challenge = {
        "schema_version": 1,
        "kind": "DELEGATION_POLICY_INSTALL",
        "nonce": NONCE,
        "issued_at": ISSUED_AT,
        "expires_at": CHALLENGE_EXPIRES_AT,
        "policy": policy_json,
    }
    revoke_challenge = {
        "schema_version": 1,
        "kind": "DELEGATION_POLICY_REVOKE",
        "nonce": NONCE,
        "issued_at": ISSUED_AT,
        "expires_at": CHALLENGE_EXPIRES_AT,
        "revoked_at": "2030-01-01T00:03:00Z",
        "policy": policy_json,
    }

    _write("openai-pause-action-v1.json", action_json)
    _write("openai-pause-challenge-v1.json", pause_challenge)
    _write("openai-pause-whole-seconds-action-v1.json", whole_seconds_action_json)
    _write("openai-pause-whole-seconds-challenge-v1.json", whole_seconds_pause_challenge)
    _write("ga4-key-event-create-action-v1.json", ga4_action_json)
    _write("ga4-key-event-create-challenge-v1.json", ga4_challenge.to_data())
    _write("ga4-key-event-create-review-v1.json", json.loads(ga4_review.canonical_bytes))
    _write("delegation-policy-v1.json", policy_json)
    _write("delegation-policy-install-challenge-v1.json", install_challenge)
    _write("delegation-policy-revoke-challenge-v1.json", revoke_challenge)
    _write(
        "safety-vector-metadata-v1.json",
        {
            "action_plan_digest": action.plan.plan_digest,
            "action_subplan_digest": action.plan.subplan_digests[0],
            "whole_seconds_action_plan_digest": whole_seconds_action.plan.plan_digest,
            "whole_seconds_action_subplan_digest": whole_seconds_action.plan.subplan_digests[0],
            "ga4_action_plan_digest": ga4_action.plan.plan_digest,
            "ga4_action_subplan_digest": ga4_action.plan.subplan_digests[0],
            "ga4_challenge_digest": ga4_challenge.digest,
            "ga4_review_digest": ga4_review.digest,
            "policy_digest": policy.policy_digest,
            "python_json_shapes": {
                "action": json.loads(_canonical(action_json)),
                "ga4_action": json.loads(_canonical(ga4_action_json)),
                "ga4_review": json.loads(ga4_review.canonical_bytes),
                "policy": json.loads(_canonical(policy_json)),
            },
        },
    )


if __name__ == "__main__":
    main()
