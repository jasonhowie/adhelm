from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

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


def _rule(kind: str) -> dict[str, object]:
    rule: dict[str, object] = {
        "name": f"properties/123/dataStreams/456/{kind}/789",
        "eventConditions": [
            {
                "field": "eventName",
                "comparisonType": "EQUALS",
                "value": "purchase",
                "negated": False,
            },
            {
                "field": "customer_parameter",
                "comparisonType": "CONTAINS",
                "value": "https://customer.example/private?email=person@example.test",
                "negated": False,
            },
        ],
        "parameterMutations": [{"parameter": "value", "parameterValue": "100.00"}],
    }
    if kind == "eventEditRules":
        rule["displayName"] = "Customer purchase naming"
        rule["processingOrder"] = "9007199254740991"
    else:
        rule["destinationEvent"] = "purchase_complete"
        rule["sourceCopyParameters"] = True
    return rule


def _client(
    handler: httpx.MockTransport, *, identity_verifier: object = lambda _: True
) -> Ga4AdminReadClient:
    http = httpx.Client(transport=handler, trust_env=False)
    transport = SafeHttpClient(http, {"analyticsadmin.googleapis.com"}, read_attempts=1)
    assert callable(identity_verifier)
    return Ga4AdminReadClient(
        transport,
        lambda: "fixture-token",
        identity_verifier,
        fingerprint_key=b"f" * 32,
    )


def test_fixed_inventory_normalizes_pages_and_never_retains_rule_content() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        token = request.url.params.get("pageToken")
        if path.endswith("/keyEvents") and token is None:
            payload = {
                "keyEvents": [
                    {
                        "name": "properties/123/keyEvents/111",
                        "eventName": "purchase",
                        "createTime": "2026-09-04T01:02:03.120Z",
                        "deletable": True,
                        "custom": True,
                        "countingMethod": "ONCE_PER_EVENT",
                        "defaultValue": {"numericValue": 1.25, "currencyCode": "USD"},
                    }
                ],
                "nextPageToken": "key-page-2",
            }
            raw = json.dumps(payload, separators=(",", ":")).replace(
                '"numericValue":1.25', '"numericValue":1.2500'
            )
            return httpx.Response(
                200,
                content=raw.encode(),
                headers={"content-type": "application/json"},
            )
        if path.endswith("/keyEvents"):
            return httpx.Response(200, json={"keyEvents": []})
        if path.endswith("/eventCreateRules"):
            return httpx.Response(200, json={"eventCreateRules": [_rule("eventCreateRules")]})
        if path.endswith("/eventEditRules"):
            return httpx.Response(200, json={"eventEditRules": [_rule("eventEditRules")]})
        raise AssertionError(path)

    inventory = _client(httpx.MockTransport(handler)).inventory(identity=_identity())
    assert [request.method for request in requests] == ["GET", "GET", "GET", "GET"]
    assert all(request.url.path.startswith("/v1alpha/properties/123") for request in requests)
    assert requests[0].url.params["pageSize"] == "200"
    assert requests[1].url.params["pageToken"] == "key-page-2"
    key_event = inventory.key_events[0]
    assert key_event.default_value is not None
    assert key_event.default_value.numeric_value == Decimal("1.2500")
    assert key_event.default_value.currency == "USD"
    edit = inventory.event_edit_rules[0]
    assert edit.inspection_status == "OPAQUE_RULE_CONTENT_UNSUPPORTED"
    assert edit.condition_count == 2
    assert edit.processing_order == "9007199254740991"
    assert edit.source_copy_parameters is None
    encoded = json.dumps(inventory.model_dump(mode="json"), sort_keys=True)
    assert "person@example.test" not in encoded
    assert "customer_parameter" not in encoded
    assert "https://customer.example" not in encoded


def test_unknown_fields_parent_mismatches_and_repeated_cursors_fail_closed() -> None:
    responses = iter(
        [
            {"keyEvents": [], "unexpected": True},
        ]
    )

    def unknown_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/keyEvents")
        return httpx.Response(200, json=next(responses))

    with pytest.raises(AdhelmError, match="unsupported field"):
        _client(httpx.MockTransport(unknown_handler)).inventory(identity=_identity())

    def parent_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(
                200,
                json={
                    "keyEvents": [
                        {
                            "name": "properties/999/keyEvents/111",
                            "eventName": "purchase",
                            "deletable": False,
                            "custom": False,
                            "countingMethod": "ONCE_PER_EVENT",
                        }
                    ]
                },
            )
        raise AssertionError("must not continue after an identity mismatch")

    with pytest.raises(AdhelmError, match="outside the configured parent"):
        _client(httpx.MockTransport(parent_handler)).inventory(identity=_identity())

    def cursor_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(200, json={"keyEvents": [], "nextPageToken": "repeat"})
        raise AssertionError("must not continue after incomplete key-event pagination")

    with pytest.raises(AdhelmError, match="pagination made no safe progress"):
        _client(httpx.MockTransport(cursor_handler)).inventory(identity=_identity())


def test_response_bounds_and_unsupported_rule_shapes_fail_before_output() -> None:
    def oversized_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(200, json={"keyEvents": [{}] * 201})
        raise AssertionError("must not continue after an oversized page")

    with pytest.raises(AdhelmError, match="list page"):
        _client(httpx.MockTransport(oversized_handler)).inventory(identity=_identity())

    def rule_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/keyEvents"):
            return httpx.Response(200, json={"keyEvents": []})
        if request.url.path.endswith("/eventCreateRules"):
            rule = _rule("eventCreateRules")
            rule["rawCustomerPayload"] = {"email": "person@example.test"}
            return httpx.Response(200, json={"eventCreateRules": [rule]})
        raise AssertionError("must not continue after unknown event-rule content")

    with pytest.raises(AdhelmError, match="unsupported field"):
        _client(httpx.MockTransport(rule_handler)).inventory(identity=_identity())


def test_transport_and_token_boundaries_are_fixed() -> None:
    with pytest.raises(AdhelmError, match="fixed API host"):
        Ga4AdminReadClient(
            SafeHttpClient(
                httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))), {"x"}
            ),
            lambda: "fixture-token",
            lambda _: True,
            fingerprint_key=b"f" * 32,
        )

    def no_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid token must stop before egress")

    client = Ga4AdminReadClient(
        SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(no_request)),
            {"analyticsadmin.googleapis.com"},
        ),
        lambda: "bad\ntoken",
        lambda _: True,
        fingerprint_key=b"f" * 32,
    )
    with pytest.raises(AdhelmError, match="authorization is unavailable"):
        client.inventory(identity=_identity())

    verifier_client = _client(httpx.MockTransport(no_request), identity_verifier=lambda _: False)
    with pytest.raises(AdhelmError, match="requires a verified"):
        verifier_client.inventory(identity=_identity())


@pytest.mark.parametrize("token", ["token space", "token\x7f", "token\u0100"])
def test_malformed_bearer_token_fails_before_ga4_admin_egress(token: str) -> None:
    calls: list[str] = []

    def never_request(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("malformed token reached HTTP")

    client = Ga4AdminReadClient(
        SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(never_request), trust_env=False),
            {"analyticsadmin.googleapis.com"},
            read_attempts=1,
        ),
        lambda: token,
        lambda _: True,
        fingerprint_key=b"f" * 32,
    )

    with pytest.raises(AdhelmError) as raised:
        client.inventory(identity=_identity())

    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert token not in str(raised.value)
    assert calls == []
