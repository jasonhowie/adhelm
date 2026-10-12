from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from adhelm.errors import AdhelmError, ExitCode
from adhelm.measurement import ConversionSemantics, EventContract, EventField
from adhelm.providers.ga4_admin import (
    Ga4AdminEventRule,
    Ga4AdminIdentity,
    Ga4AdminInventory,
    Ga4AdminKeyEvent,
    Ga4AdminReadClient,
)
from adhelm.providers.ga4_admin_compiler import (
    Ga4AdminKeyEventCreateInput,
    Ga4AdminKeyEventDefaultValueInput,
    Ga4AdminKeyEventIdentity,
    compile_ga4_admin_key_event_create,
)
from adhelm.providers.ga4_admin_preconditions import (
    Ga4AdminKeyEventInventoryPreconditions,
    evaluate_ga4_key_event_create_preconditions,
)
from adhelm.transport import SafeHttpClient

_KEY = b"k" * 32
_OTHER_KEY = b"q" * 32


def _inventory_identity() -> Ga4AdminIdentity:
    return Ga4AdminIdentity(
        property_ref="ga4_admin.property.123",
        property_client_ref="123",
        web_stream_ref="ga4_admin.web_stream.456",
        web_stream_client_ref="456",
    )


def _create_input(
    *,
    property_client_ref: str = "123",
    web_stream_client_ref: str = "456",
    include_default_value: bool = False,
) -> Ga4AdminKeyEventCreateInput:
    return Ga4AdminKeyEventCreateInput(
        event_contract=EventContract(
            event_contract_ref="event.fixture",
            event_name="qualified_request",
            semantics=ConversionSemantics(
                semantics_ref="semantics.fixture",
                customer_definition="A customer-declared qualified request.",
                counting_rule="Count each declared event occurrence.",
            ),
            fields=(EventField(name="qualified", value_type="BOOLEAN", required=True),),
        ),
        identity=Ga4AdminKeyEventIdentity(
            property_ref=f"ga4_admin.property.{property_client_ref}",
            property_client_ref=property_client_ref,
            web_stream_ref=f"ga4_admin.web_stream.{web_stream_client_ref}",
            web_stream_client_ref=web_stream_client_ref,
        ),
        counting_method="ONCE_PER_EVENT",
        default_value=(
            Ga4AdminKeyEventDefaultValueInput(
                numeric_value=Decimal("1.25"),
                currency_code="USD",
            )
            if include_default_value
            else None
        ),
    )


def _inventory(
    key: bytes,
    *,
    key_event_names: tuple[str, ...] = (),
    include_rules: bool = False,
    include_key_event_default_value: bool = False,
) -> Ga4AdminInventory:
    identity = _inventory_identity()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(
                200,
                json={
                    "keyEvents": [
                        {
                            "name": f"properties/123/keyEvents/{700 + index}",
                            "eventName": name,
                            "deletable": True,
                            "custom": True,
                            "countingMethod": "ONCE_PER_EVENT",
                            **(
                                {
                                    "defaultValue": {
                                        "numericValue": 1.25,
                                        "currencyCode": "USD",
                                    }
                                }
                                if include_key_event_default_value
                                else {}
                            ),
                        }
                        for index, name in enumerate(key_event_names)
                    ]
                },
            )
        if request.url.path.endswith("/eventCreateRules"):
            return httpx.Response(
                200,
                json={
                    "eventCreateRules": (
                        [
                            {
                                "name": "properties/123/dataStreams/456/eventCreateRules/800",
                                "destinationEvent": "derived_event",
                                "eventConditions": [
                                    {
                                        "field": "eventName",
                                        "comparisonType": "EQUALS",
                                        "value": "existing_event",
                                        "negated": False,
                                    }
                                ],
                                "sourceCopyParameters": False,
                                "parameterMutations": [],
                            }
                        ]
                        if include_rules
                        else []
                    )
                },
            )
        if request.url.path.endswith("/eventEditRules"):
            return httpx.Response(
                200,
                json={
                    "eventEditRules": (
                        [
                            {
                                "name": "properties/123/dataStreams/456/eventEditRules/801",
                                "displayName": "fixture_rule",
                                "eventConditions": [
                                    {
                                        "field": "eventName",
                                        "comparisonType": "EQUALS",
                                        "value": "existing_event",
                                        "negated": False,
                                    }
                                ],
                                "parameterMutations": [],
                                "processingOrder": "1",
                            }
                        ]
                        if include_rules
                        else []
                    )
                },
            )
        raise AssertionError(request.url.path)

    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
        {"analyticsadmin.googleapis.com"},
        read_attempts=1,
    )
    return Ga4AdminReadClient(
        transport,
        lambda: "fixture-token",
        lambda candidate: candidate == identity,
        fingerprint_key=key,
    ).inventory(identity=identity)


def _constructed_inventory(
    source: Ga4AdminInventory,
    *,
    identity: Ga4AdminIdentity | None = None,
    key_events: tuple[Ga4AdminKeyEvent, ...] | None = None,
    event_create_rules: tuple[Ga4AdminEventRule, ...] | None = None,
    event_edit_rules: tuple[Ga4AdminEventRule, ...] | None = None,
) -> Ga4AdminInventory:
    return Ga4AdminInventory.model_construct(
        schema_version=source.schema_version,
        identity=source.identity if identity is None else identity,
        key_events=source.key_events if key_events is None else key_events,
        event_create_rules=(
            source.event_create_rules if event_create_rules is None else event_create_rules
        ),
        event_edit_rules=source.event_edit_rules if event_edit_rules is None else event_edit_rules,
        pagination_verified=source.pagination_verified,
        fingerprint_key_marker=source.fingerprint_key_marker,
        inventory_content_marker=source.inventory_content_marker,
    )


def _assert_code(error: pytest.ExceptionInfo[AdhelmError], code: str) -> None:
    assert error.value.code == code
    assert "qualified_request" not in error.value.message


def test_returns_only_safe_non_authorizing_precondition_facts() -> None:
    create_input = _create_input()
    supplied_inventory = _inventory(_KEY, include_rules=True)

    result = evaluate_ga4_key_event_create_preconditions(
        create_input,
        supplied_inventory,
        fingerprint_key=_KEY,
    )

    assert (
        result.candidate_digest == compile_ga4_admin_key_event_create(create_input).candidate_digest
    )
    assert result.model_dump(mode="json") == {
        "schema_version": 1,
        "candidate_digest": result.candidate_digest,
        "inventory_scope": "COMPLETE_SUPPLIED_INVENTORY",
        "fingerprint_key_continuity": "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY",
        "inventory_content_integrity": "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT",
        "duplicate_status": "NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY",
        "current_provider_state": "NOT_ESTABLISHED",
        "capacity_status": "NOT_EVALUATED",
        "permission_status": "NOT_EVALUATED",
        "remote_side_effects": False,
        "execution_authority": False,
    }
    rendered = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    assert "qualified_request" not in rendered
    assert "ga4_admin.property.123" not in rendered
    assert _KEY.hex() not in rendered
    assert set(inspect.signature(evaluate_ga4_key_event_create_preconditions).parameters) == {
        "create_input",
        "supplied_inventory",
        "fingerprint_key",
    }
    assert set(Ga4AdminKeyEventInventoryPreconditions.model_fields) == set(result.model_dump())


def test_recompiles_a_valid_supported_input_with_decimal_default_value() -> None:
    create_input = _create_input(include_default_value=True)

    result = evaluate_ga4_key_event_create_preconditions(
        create_input,
        _inventory(_KEY),
        fingerprint_key=_KEY,
    )

    assert (
        result.candidate_digest == compile_ga4_admin_key_event_create(create_input).candidate_digest
    )


def test_accepts_a_normalized_inventory_key_event_default_value() -> None:
    result = evaluate_ga4_key_event_create_preconditions(
        _create_input(),
        _inventory(
            _KEY,
            key_event_names=("existing_event",),
            include_key_event_default_value=True,
        ),
        fingerprint_key=_KEY,
    )

    assert result.duplicate_status == "NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY"


def test_matching_marker_is_required_before_duplicate_event_comparison() -> None:
    duplicate_inventory = _inventory(_OTHER_KEY, key_event_names=("qualified_request",))

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(),
            duplicate_inventory,
            fingerprint_key=_KEY,
        )

    _assert_code(error, "GA4_FINGERPRINT_KEY_CONTINUITY_MISMATCH")
    assert error.value.exit_code == ExitCode.PRECONDITION


def test_missing_legacy_marker_remains_unknown_and_does_not_run_duplicate_check() -> None:
    source = _inventory(_KEY, key_event_names=("qualified_request",))
    legacy_wire = source.model_dump(mode="json")
    legacy_wire.pop("fingerprint_key_marker")
    legacy_wire.pop("inventory_content_marker")
    legacy_inventory = Ga4AdminInventory.model_validate_json(json.dumps(legacy_wire))

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(),
            legacy_inventory,
            fingerprint_key=_KEY,
        )

    _assert_code(error, "GA4_FINGERPRINT_KEY_CONTINUITY_NOT_EVIDENCED")
    assert error.value.exit_code == ExitCode.PRECONDITION


def test_duplicate_uses_current_keyed_event_name_fingerprint_only_after_marker_match() -> None:
    supplied_inventory = _inventory(_KEY, key_event_names=("qualified_request",))

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(),
            supplied_inventory,
            fingerprint_key=_KEY,
        )

    _assert_code(error, "GA4_KEY_EVENT_DUPLICATE_SUPPLIED_INVENTORY")
    assert error.value.exit_code == ExitCode.PRECONDITION


def test_rejects_unsalted_foreign_member_fingerprint_before_absence_claim() -> None:
    source = _inventory(_KEY, key_event_names=("existing_event",))
    original = source.key_events[0]
    forged_event = Ga4AdminKeyEvent.model_construct(
        resource_ref=original.resource_ref,
        resource_client_ref=original.resource_client_ref,
        property_ref=original.property_ref,
        configuration_fingerprint=original.configuration_fingerprint,
        event_name_fingerprint=hashlib.sha256(b"qualified_request").hexdigest(),
        fingerprint_scheme=original.fingerprint_scheme,
        counting_method=original.counting_method,
        default_value=original.default_value,
        custom=original.custom,
        deletable=original.deletable,
    )
    forged_inventory = _constructed_inventory(source, key_events=(forged_event,))

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(),
            forged_inventory,
            fingerprint_key=_KEY,
        )

    _assert_code(error, "GA4_FINGERPRINT_INVENTORY_CONTENT_MISMATCH")


def test_requires_fresh_reader_content_marker_even_when_key_marker_is_present() -> None:
    source = _inventory(_KEY)
    marker_only_wire = source.model_dump(mode="json")
    marker_only_wire.pop("inventory_content_marker")
    marker_only_inventory = Ga4AdminInventory.model_validate_json(json.dumps(marker_only_wire))

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(),
            marker_only_inventory,
            fingerprint_key=_KEY,
        )

    _assert_code(error, "GA4_FINGERPRINT_INVENTORY_CONTENT_NOT_EVIDENCED")


def test_rejects_post_seal_member_reordering_before_duplicate_comparison() -> None:
    source = _inventory(_KEY, key_event_names=("existing_one", "existing_two"))
    reordered_inventory = _constructed_inventory(
        source,
        key_events=tuple(reversed(source.key_events)),
    )

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(),
            reordered_inventory,
            fingerprint_key=_KEY,
        )

    _assert_code(error, "GA4_FINGERPRINT_INVENTORY_CONTENT_MISMATCH")


def test_requires_exact_same_deterministic_property_and_stream_identity() -> None:
    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(property_client_ref="999"),
            _inventory(_KEY),
            fingerprint_key=_KEY,
        )

    _assert_code(error, "GA4_ADMIN_SUPPLIED_INVENTORY_IDENTITY_MISMATCH")


def test_rejects_constructed_create_identity_reference_before_compilation() -> None:
    source = _create_input()
    forged_identity = Ga4AdminKeyEventIdentity.model_construct(
        property_ref="ga4_admin.property.relabelled",
        property_client_ref=source.identity.property_client_ref,
        web_stream_ref=source.identity.web_stream_ref,
        web_stream_client_ref=source.identity.web_stream_client_ref,
        api_version=source.identity.api_version,
        required_scope=source.identity.required_scope,
    )
    forged_input = Ga4AdminKeyEventCreateInput.model_construct(
        event_contract=source.event_contract,
        identity=forged_identity,
        counting_method=source.counting_method,
        default_value=source.default_value,
    )

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            forged_input,
            _inventory(_KEY),
            fingerprint_key=_KEY,
        )

    _assert_code(error, "INVALID_GA4_ADMIN_PRECONDITION")


@pytest.mark.parametrize(
    "identity_factory",
    [
        lambda source: Ga4AdminIdentity.model_construct(
            schema_version=True,
            property_ref=source.identity.property_ref,
            property_client_ref=source.identity.property_client_ref,
            web_stream_ref=source.identity.web_stream_ref,
            web_stream_client_ref=source.identity.web_stream_client_ref,
            admin_api_version=source.identity.admin_api_version,
            required_scope=source.identity.required_scope,
        ),
        lambda source: Ga4AdminIdentity.model_construct(
            schema_version=source.identity.schema_version,
            property_ref="ga4_admin.property.relabelled",
            property_client_ref=source.identity.property_client_ref,
            web_stream_ref=source.identity.web_stream_ref,
            web_stream_client_ref=source.identity.web_stream_client_ref,
            admin_api_version=source.identity.admin_api_version,
            required_scope=source.identity.required_scope,
        ),
    ],
)
def test_rejects_nested_inventory_identity_literals_and_noncanonical_references(
    identity_factory: Callable[[Ga4AdminInventory], Ga4AdminIdentity],
) -> None:
    source = _inventory(_KEY)
    forged_inventory = _constructed_inventory(
        source,
        identity=identity_factory(source),
    )

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(),
            forged_inventory,
            fingerprint_key=_KEY,
        )

    _assert_code(error, "INVALID_GA4_ADMIN_PRECONDITION")


def test_rejects_rehashed_style_key_event_parent_and_duplicate_member_bypasses() -> None:
    source = _inventory(_KEY, key_event_names=("existing_event",))
    original = source.key_events[0]
    forged_parent = Ga4AdminKeyEvent.model_construct(
        resource_ref=original.resource_ref,
        resource_client_ref=original.resource_client_ref,
        property_ref="ga4_admin.property.relabelled",
        configuration_fingerprint=original.configuration_fingerprint,
        event_name_fingerprint=original.event_name_fingerprint,
        fingerprint_scheme=original.fingerprint_scheme,
        counting_method=original.counting_method,
        default_value=original.default_value,
        custom=original.custom,
        deletable=original.deletable,
    )

    for forged_inventory in (
        _constructed_inventory(source, key_events=(forged_parent,)),
        _constructed_inventory(source, key_events=(original, original)),
    ):
        with pytest.raises(AdhelmError) as error:
            evaluate_ga4_key_event_create_preconditions(
                _create_input(),
                forged_inventory,
                fingerprint_key=_KEY,
            )
        _assert_code(error, "INVALID_GA4_ADMIN_PRECONDITION")


def test_rejects_nested_rule_controls_before_canonicalization() -> None:
    source = _inventory(_KEY, include_rules=True)
    original = source.event_create_rules[0]
    forged_rule = Ga4AdminEventRule.model_construct(
        resource_ref=original.resource_ref,
        resource_client_ref=original.resource_client_ref,
        property_ref=original.property_ref,
        web_stream_ref=original.web_stream_ref,
        rule_kind=original.rule_kind,
        configuration_fingerprint=original.configuration_fingerprint,
        fingerprint_scheme=original.fingerprint_scheme,
        condition_count=1.0,
        parameter_mutation_count=original.parameter_mutation_count,
        source_copy_parameters=original.source_copy_parameters,
        processing_order=original.processing_order,
        inspection_status=original.inspection_status,
    )
    forged_inventory = _constructed_inventory(source, event_create_rules=(forged_rule,))

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(),
            forged_inventory,
            fingerprint_key=_KEY,
        )

    _assert_code(error, "INVALID_GA4_ADMIN_PRECONDITION")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("condition_count", 1.0),
        ("web_stream_ref", "ga4_admin.web_stream.relabelled"),
        ("fingerprint_scheme", "UNKEYED_SHA256"),
    ],
)
def test_rejects_event_rule_scalar_and_parent_membership_bypasses(
    field: str,
    replacement: object,
) -> None:
    source = _inventory(_KEY, include_rules=True)
    raw_rule = source.event_edit_rules[0].model_dump(mode="python")
    raw_rule[field] = replacement
    forged_rule = Ga4AdminEventRule.model_construct(**raw_rule)
    forged_inventory = _constructed_inventory(source, event_edit_rules=(forged_rule,))

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(),
            forged_inventory,
            fingerprint_key=_KEY,
        )

    _assert_code(error, "INVALID_GA4_ADMIN_PRECONDITION")


def test_rejects_malformed_constructed_marker_without_exposing_it() -> None:
    source = _inventory(_KEY)
    forged_inventory = Ga4AdminInventory.model_construct(
        schema_version=source.schema_version,
        identity=source.identity,
        key_events=source.key_events,
        event_create_rules=source.event_create_rules,
        event_edit_rules=source.event_edit_rules,
        pagination_verified=source.pagination_verified,
        fingerprint_key_marker="A" * 64,
        inventory_content_marker=source.inventory_content_marker,
    )

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(),
            forged_inventory,
            fingerprint_key=_KEY,
        )

    _assert_code(error, "INVALID_GA4_ADMIN_PRECONDITION")


@pytest.mark.parametrize("fingerprint_key", [b"short", bytearray(_KEY), True])
def test_rejects_invalid_or_non_bytes_fingerprint_key(fingerprint_key: object) -> None:
    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(),
            _inventory(_KEY),
            fingerprint_key=fingerprint_key,  # type: ignore[arg-type]
        )

    _assert_code(error, "FINGERPRINT_KEY_REQUIRED")


def test_does_not_accept_raw_inventory_mappings_or_serialize_a_fingerprint_key() -> None:
    supplied_inventory = _inventory(_KEY)
    wire = supplied_inventory.model_dump(mode="json")

    with pytest.raises(AdhelmError) as error:
        evaluate_ga4_key_event_create_preconditions(
            _create_input(),
            wire,  # type: ignore[arg-type]
            fingerprint_key=_KEY,
        )

    _assert_code(error, "INVALID_GA4_ADMIN_PRECONDITION")
    result = evaluate_ga4_key_event_create_preconditions(
        _create_input(),
        supplied_inventory,
        fingerprint_key=_KEY,
    )
    assert "fingerprint_key" not in result.model_dump(mode="json")


def test_normalized_inventory_has_no_provider_or_execution_surface() -> None:
    result = evaluate_ga4_key_event_create_preconditions(
        _create_input(),
        _inventory(_KEY),
        fingerprint_key=_KEY,
    )

    assert result.current_provider_state == "NOT_ESTABLISHED"
    assert result.capacity_status == "NOT_EVALUATED"
    assert result.permission_status == "NOT_EVALUATED"
    assert result.remote_side_effects is False
    assert result.execution_authority is False
