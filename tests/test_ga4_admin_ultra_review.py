"""Adversarial checks for GA4 Admin's fixed, private inventory boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.providers.ga4_admin import Ga4AdminIdentity, Ga4AdminReadClient
from adhelm.transport import SafeHttpClient


def _identity() -> Ga4AdminIdentity:
    return Ga4AdminIdentity(
        property_ref="ga4_admin.property.123",
        property_client_ref="123",
        web_stream_ref="ga4_admin.web_stream.456",
        web_stream_client_ref="456",
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    fingerprint_key: bytes = b"a" * 32,
    identity_verifier: Callable[[Ga4AdminIdentity], bool] = lambda _: True,
    reference_for: Callable[[str, str], str] | None = None,
    token_getter: Callable[[], str] = lambda: "fixture-token",
) -> Ga4AdminReadClient:
    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
        {"analyticsadmin.googleapis.com"},
        read_attempts=1,
    )
    kwargs: dict[str, Any] = {"fingerprint_key": fingerprint_key}
    if reference_for is not None:
        kwargs["reference_for"] = reference_for
    return Ga4AdminReadClient(transport, token_getter, identity_verifier, **kwargs)


def _documented_edit_rule() -> dict[str, object]:
    return {
        "name": "properties/123/dataStreams/456/eventEditRules/789",
        "displayName": "Private rule person@example.test",
        "eventConditions": [
            {
                "field": "eventName",
                "comparisonType": "REGULAR_EXPRESSION_CASE_INSENSITIVE",
                "value": "https://customer.example/private?email=person@example.test",
                "negated": False,
            }
        ],
        "parameterMutations": [{"parameter": "eventName", "parameterValue": "purchase_complete"}],
        "processingOrder": "-1",
    }


def _inventory_handler(edit_rule: dict[str, object]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(200, json={"keyEvents": []})
        if request.url.path.endswith("/eventCreateRules"):
            return httpx.Response(200, json={"eventCreateRules": []})
        if request.url.path.endswith("/eventEditRules"):
            return httpx.Response(200, json={"eventEditRules": [edit_rule]})
        raise AssertionError(request.url.path)

    return handler


def test_documented_edit_rule_shape_is_private_and_uses_local_keyed_fingerprints() -> None:
    rule = _documented_edit_rule()
    inventory = _client(_inventory_handler(rule)).inventory(identity=_identity())
    edit = inventory.event_edit_rules[0]

    assert edit.rule_kind == "EVENT_EDIT_RULE"
    assert edit.source_copy_parameters is None
    assert edit.processing_order == "-1"
    assert edit.fingerprint_scheme == "HMAC_SHA256_LOCAL_V1"
    assert edit.configuration_fingerprint != hashlib.sha256(canonical_bytes(rule)).hexdigest()

    rendered = json.dumps(inventory.model_dump(mode="json"), sort_keys=True)
    assert "person@example.test" not in rendered
    assert "customer.example" not in rendered
    assert "Private rule" not in rendered


def test_fingerprint_key_changes_private_fingerprint_without_changing_normalized_fact() -> None:
    rule = _documented_edit_rule()
    first = _client(_inventory_handler(rule), fingerprint_key=b"a" * 32).inventory(
        identity=_identity()
    )
    second = _client(_inventory_handler(rule), fingerprint_key=b"b" * 32).inventory(
        identity=_identity()
    )

    assert (
        first.event_edit_rules[0].configuration_fingerprint
        != second.event_edit_rules[0].configuration_fingerprint
    )
    assert first.event_edit_rules[0].processing_order == second.event_edit_rules[0].processing_order


def test_identity_reference_mismatch_stops_before_verifier_token_or_egress() -> None:
    calls: list[str] = []

    def never_request(_: httpx.Request) -> httpx.Response:
        calls.append("http")
        raise AssertionError("identity mismatch must stop before HTTP")

    def verifier(_: Ga4AdminIdentity) -> bool:
        calls.append("verifier")
        return True

    def token() -> str:
        calls.append("token")
        return "fixture-token"

    bad_identity = _identity().model_copy(update={"property_ref": "ga4_admin.property.999"})
    with pytest.raises(AdhelmError, match="references differ"):
        _client(never_request, identity_verifier=verifier, token_getter=token).inventory(
            identity=bad_identity
        )
    assert calls == []


def test_custom_reference_mapping_must_bind_both_native_identity_components() -> None:
    def references(kind: str, value: str) -> str:
        return f"local.{kind}.{value}"

    identity = Ga4AdminIdentity(
        property_ref="local.property.123",
        property_client_ref="123",
        web_stream_ref="local.web_stream.456",
        web_stream_client_ref="456",
    )

    inventory = _client(
        _inventory_handler(_documented_edit_rule()), reference_for=references
    ).inventory(identity=identity)
    assert inventory.event_edit_rules[0].resource_ref == "local.event_edit_rule.789"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rule: rule.update({"eventConditions": []}),
        lambda rule: rule["eventConditions"].__setitem__(
            0,
            {
                "field": "eventName",
                "comparisonType": "UNREVIEWED_COMPARISON",
                "value": "purchase",
                "negated": False,
            },
        ),
        lambda rule: rule.update(
            {
                "parameterMutations": [
                    {"parameter": "value", "parameterValue": "one"},
                    {"parameter": "value", "parameterValue": "two"},
                ]
            }
        ),
    ],
)
def test_invalid_documented_rule_semantics_fail_before_inventory_output(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    rule: dict[str, Any] = {
        "name": "properties/123/dataStreams/456/eventCreateRules/789",
        "destinationEvent": "purchase_complete",
        "eventConditions": [
            {
                "field": "eventName",
                "comparisonType": "EQUALS",
                "value": "purchase",
                "negated": False,
            }
        ],
        "sourceCopyParameters": False,
        "parameterMutations": [{"parameter": "value", "parameterValue": "100"}],
    }
    mutate(rule)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(200, json={"keyEvents": []})
        if request.url.path.endswith("/eventCreateRules"):
            return httpx.Response(200, json={"eventCreateRules": [rule]})
        raise AssertionError("invalid create rule must stop before later inventory calls")

    with pytest.raises(AdhelmError, match="invalid"):
        _client(handler).inventory(identity=_identity())


def test_private_fingerprint_key_is_required_before_any_provider_read() -> None:
    called = False

    def never_request(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("invalid local key must stop before HTTP")

    with pytest.raises(AdhelmError, match="fingerprint key"):
        _client(never_request, fingerprint_key=b"too-short")
    assert called is False
