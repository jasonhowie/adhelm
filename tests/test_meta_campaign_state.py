"""Fake-only contract tests for fixed Meta Graph campaign-state reads."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError, ExitCode
from adhelm.providers.meta_ads import MetaAdsIdentity
from adhelm.providers.meta_campaign_state import MetaCampaignState, MetaCampaignStateReader
from adhelm.transport import SafeHttpClient

_TOKEN = "fixture-subject-token-not-for-output"


def _identity() -> MetaAdsIdentity:
    return MetaAdsIdentity(
        account_ref="meta_ads.account.act_123456",
        account_client_ref="act_123456",
        timezone="UTC",
        currency="USD",
        status="1",
    )


def _reader(
    handler: Callable[[httpx.Request], httpx.Response],
    getter: Callable[[], str] = lambda: _TOKEN,
    *,
    maximum_response_bytes: int = 65_536,
    read_attempts: int = 1,
) -> MetaCampaignStateReader:
    return MetaCampaignStateReader(
        SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
            {"graph.facebook.com"},
            maximum_response_bytes=maximum_response_bytes,
            read_attempts=read_attempts,
        ),
        getter,
    )


def _state(
    *,
    campaign: object = "456789",
    account: object = "123456",
    configured: object = "ACTIVE",
    effective: object = "WITH_ISSUES",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": campaign,
        "account_id": account,
        "configured_status": configured,
        "effective_status": effective,
    }
    if extra:
        result.update(extra)
    return result


def _body_handler(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return handler


def test_reads_exact_fixed_campaign_node_and_normalizes_output() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        assert str(request.url) == (
            "https://graph.facebook.com/v26.0/456789"
            "?fields=id%2Caccount_id%2Cconfigured_status%2Ceffective_status"
        )
        assert request.headers["authorization"] == f"Bearer {_TOKEN}"
        return httpx.Response(200, json=_state())

    observed = _reader(handler).read_campaign_state(_identity(), "456789")

    assert calls == 1
    assert observed.model_dump(mode="json") == {
        "campaign_client_ref": "456789",
        "campaign_ref": "meta_ads.campaign.456789",
        "account_client_ref": "123456",
        "configured_status": "ACTIVE",
        "effective_status": "WITH_ISSUES",
    }


def test_prefixed_provider_account_id_is_rejected_without_wire_evidence() -> None:
    with pytest.raises(AdhelmError) as raised:
        _reader(
            lambda _request: httpx.Response(200, json=_state(account="act_123456"))
        ).read_campaign_state(_identity(), "456789")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("configured", "effective"),
    [
        ("ACTIVE", "ACTIVE"),
        ("ARCHIVED", "ARCHIVED"),
        ("DELETED", "DELETED"),
        ("PAUSED", "IN_PROCESS"),
        ("PAUSED", "PAUSED"),
        ("PAUSED", "WITH_ISSUES"),
    ],
)
def test_accepts_only_reviewed_v26_campaign_status_literals(
    configured: str, effective: str
) -> None:
    observed = _reader(
        lambda _request: httpx.Response(
            200, json=_state(configured=configured, effective=effective)
        )
    ).read_campaign_state(_identity(), "456789")

    assert observed.configured_status == configured
    assert observed.effective_status == effective


@pytest.mark.parametrize(
    ("configured", "effective"),
    [
        ("IN_PROCESS", "PAUSED"),
        ("PAUSED", "CAMPAIGN_PAUSED"),
        ("PAUSED", "PAUSED\n"),
        (True, "PAUSED"),
        ("PAUSED", 1),
    ],
)
def test_unknown_or_nonstring_status_rejects_closed_response(
    configured: object, effective: object
) -> None:
    with pytest.raises(AdhelmError) as raised:
        _reader(
            lambda _request: httpx.Response(
                200, json=_state(configured=configured, effective=effective)
            )
        ).read_campaign_state(_identity(), "456789")

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("campaign_id", ["0", "01", "1/../2", "１２", "", 123])
def test_invalid_campaign_id_stops_before_token_or_http(campaign_id: object) -> None:
    getter_calls = 0
    http_calls = 0

    def getter() -> str:
        nonlocal getter_calls
        getter_calls += 1
        return _TOKEN

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(500)

    with pytest.raises(AdhelmError) as raised:
        _reader(handler, getter).read_campaign_state(_identity(), campaign_id)  # type: ignore[arg-type]

    assert raised.value.code == "INVALID_INPUT"
    assert getter_calls == http_calls == 0


@pytest.mark.parametrize(
    "identity",
    [
        _identity().model_copy(update={"account_client_ref": "act_999999"}),
        _identity().model_copy(update={"account_ref": "meta_ads.account.act_999999"}),
        _identity().model_copy(update={"account_client_ref": "123456"}),
        _identity().model_copy(update={"currency": "not-a-currency"}),
        _identity().model_copy(update={"currency": "ZZZ"}),
        _identity().model_copy(update={"timezone": "Not/AZone"}),
        _identity().model_copy(update={"status": "ACTIVE"}),
    ],
)
def test_constructed_or_mismatched_identity_stops_before_token_or_http(
    identity: MetaAdsIdentity,
) -> None:
    getter_calls = 0
    http_calls = 0

    def getter() -> str:
        nonlocal getter_calls
        getter_calls += 1
        return _TOKEN

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(500)

    with pytest.raises(AdhelmError) as raised:
        _reader(handler, getter).read_campaign_state(identity, "456789")

    assert raised.value.code == "IDENTITY_BINDING_INVALID"
    assert getter_calls == http_calls == 0


@pytest.mark.parametrize("timezone", ["/etc/localtime", "../UTC"])
def test_absolute_or_traversal_timezone_stops_before_token_or_http(timezone: str) -> None:
    getter_calls = 0
    http_calls = 0

    def getter() -> str:
        nonlocal getter_calls
        getter_calls += 1
        return _TOKEN

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(500)

    identity = _identity().model_copy(update={"timezone": timezone})
    with pytest.raises(AdhelmError) as raised:
        _reader(handler, getter).read_campaign_state(identity, "456789")

    assert raised.value.code == "IDENTITY_BINDING_INVALID"
    assert getter_calls == http_calls == 0


@pytest.mark.parametrize(
    "payload",
    [
        _state(campaign="999999"),
        _state(account="999999"),
        {"id": "456789", "account_id": "123456", "configured_status": "ACTIVE"},
        _state(extra={"name": "customer campaign"}),
        [],
    ],
)
def test_wrong_binding_or_closed_payload_shape_is_rejected(payload: object) -> None:
    with pytest.raises(AdhelmError) as raised:
        _reader(lambda _request: httpx.Response(200, json=payload)).read_campaign_state(
            _identity(), "456789"
        )

    assert raised.value.code in {"IDENTITY_MISMATCH", "PROVIDER_RESPONSE_INVALID"}


def test_duplicate_json_and_oversized_or_malformed_bodies_are_not_exposed() -> None:
    bodies = (
        b'{"id":"456789","id":"456789","account_id":"123456",'
        b'"configured_status":"ACTIVE","effective_status":"PAUSED"}',
        b"{",
        json.dumps(_state(extra={"padding": "x" * 65_536})).encode(),
    )
    for body in bodies:
        with pytest.raises(AdhelmError) as raised:
            _reader(_body_handler(body)).read_campaign_state(_identity(), "456789")
        assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
        assert "padding" not in raised.value.message


def test_guarded_token_is_required_once_and_callback_text_is_not_relayed() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    def broken() -> str:
        raise AdhelmError("UNTRUSTED", "Bearer leaked-token-not-for-output")

    with pytest.raises(AdhelmError) as raised:
        _reader(handler, broken).read_campaign_state(_identity(), "456789")

    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert "leaked-token" not in raised.value.message
    assert calls == 0


def test_configuration_drift_from_guarded_token_remains_typed_before_http() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    def drifted() -> str:
        raise AdhelmError("CONFIGURATION_CHANGED", "untrusted raw config text")

    with pytest.raises(AdhelmError) as raised:
        _reader(handler, drifted).read_campaign_state(_identity(), "456789")

    assert raised.value.code == "CONFIGURATION_CHANGED"
    assert "untrusted" not in raised.value.message
    assert calls == 0


@pytest.mark.parametrize(
    ("code", "expected_code"),
    [
        ("META_TOKEN_CREDENTIAL_CHANGED", "META_TOKEN_CREDENTIAL_CHANGED"),
        ("META_TOKEN_EXPIRED", "META_TOKEN_EXPIRED"),
        ("CLOCK_INVALID", "CLOCK_INVALID"),
        ("META_TOKEN_CREDENTIAL_CHANGED ", "PROVIDER_ACCESS_REQUIRED"),
    ],
)
def test_guarded_local_failure_mapping_does_not_relay_callback_text(
    code: str, expected_code: str
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    def broken() -> str:
        raise AdhelmError(
            code,
            "untrusted subject-token-text-not-for-output",
            exit_code=ExitCode.INTERNAL,
            next_action="untrusted next action",
        )

    with pytest.raises(AdhelmError) as raised:
        _reader(handler, broken).read_campaign_state(_identity(), "456789")

    assert raised.value.code == expected_code
    assert "untrusted" not in raised.value.message
    assert "untrusted" not in raised.value.next_action
    assert calls == 0


def test_reader_requires_exact_one_attempt_fixed_transport_contract() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)), trust_env=False
    )
    with pytest.raises(AdhelmError) as retries:
        MetaCampaignStateReader(
            SafeHttpClient(client, {"graph.facebook.com"}, read_attempts=2), lambda: _TOKEN
        )
    assert retries.value.code == "TRANSPORT_CONFIG_INVALID"

    with pytest.raises(AdhelmError) as broad_host:
        MetaCampaignStateReader(
            SafeHttpClient(
                httpx.Client(
                    transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
                    trust_env=False,
                ),
                {"graph.facebook.com", "example.invalid"},
                read_attempts=1,
            ),
            lambda: _TOKEN,
        )
    assert broad_host.value.code == "TRANSPORT_CONFIG_INVALID"

    with pytest.raises(AdhelmError) as environment_transport:
        MetaCampaignStateReader(
            SafeHttpClient(
                httpx.Client(
                    transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
                    trust_env=True,
                ),
                {"graph.facebook.com"},
                read_attempts=1,
            ),
            lambda: _TOKEN,
        )
    assert environment_transport.value.code == "TRANSPORT_CONFIG_INVALID"


def test_reader_rejects_ambient_httpx_credentials_or_callbacks() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    def hook(_request: httpx.Request) -> None:
        return None

    clients = [
        httpx.Client(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            auth=("fixture-user", "fixture-pass"),
        ),
        httpx.Client(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            event_hooks={"request": [hook]},
        ),
    ]
    cookie_client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    cookie_client.cookies.set("fixture", "value", domain="graph.facebook.com")
    clients.append(cookie_client)

    for client in clients:
        try:
            with pytest.raises(AdhelmError) as raised:
                MetaCampaignStateReader(
                    SafeHttpClient(
                        client,
                        {"graph.facebook.com"},
                        maximum_response_bytes=65_536,
                        read_attempts=1,
                    ),
                    lambda: _TOKEN,
                )
            assert raised.value.code == "TRANSPORT_CONFIG_INVALID"
        finally:
            client.close()

    assert calls == 0


def test_mutated_transport_settings_fail_before_token_or_http() -> None:
    getter_calls = 0
    http_calls = 0

    def getter() -> str:
        nonlocal getter_calls
        getter_calls += 1
        return _TOKEN

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(500)

    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
        {"graph.facebook.com"},
        maximum_response_bytes=65_536,
        read_attempts=1,
    )
    reader = MetaCampaignStateReader(transport, getter)
    transport.read_attempts = 2

    with pytest.raises(AdhelmError) as raised:
        reader.read_campaign_state(_identity(), "456789")

    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"
    assert getter_calls == http_calls == 0


def test_token_callback_transport_mutation_stops_before_http() -> None:
    http_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(500)

    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
        {"graph.facebook.com"},
        maximum_response_bytes=65_536,
        read_attempts=1,
    )

    def getter() -> str:
        transport.read_attempts = 2
        return _TOKEN

    reader = MetaCampaignStateReader(transport, getter)
    with pytest.raises(AdhelmError) as raised:
        reader.read_campaign_state(_identity(), "456789")

    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"
    assert http_calls == 0


def test_constructed_reader_rejects_before_identity_token_or_http() -> None:
    reader = object.__new__(MetaCampaignStateReader)

    with pytest.raises(AdhelmError) as raised:
        reader.read_campaign_state(_identity(), "456789")

    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"


def test_constructed_transport_rejects_with_fixed_error() -> None:
    transport = object.__new__(SafeHttpClient)

    with pytest.raises(AdhelmError) as raised:
        MetaCampaignStateReader(transport, lambda: _TOKEN)

    assert raised.value.code == "TRANSPORT_CONFIG_INVALID"


@pytest.mark.parametrize(
    "data",
    [
        {
            "campaign_client_ref": "0456789",
            "campaign_ref": "meta_ads.campaign.0456789",
            "account_client_ref": "123456",
            "configured_status": "PAUSED",
            "effective_status": "PAUSED",
        },
        {
            "campaign_client_ref": "456789",
            "campaign_ref": "meta_ads.campaign.999999",
            "account_client_ref": "123456",
            "configured_status": "PAUSED",
            "effective_status": "PAUSED",
        },
        {
            "campaign_client_ref": "456789",
            "campaign_ref": "meta_ads.campaign.456789",
            "account_client_ref": "123456",
            "configured_status": True,
            "effective_status": "PAUSED",
        },
    ],
)
def test_state_model_is_frozen_and_requires_exact_native_binding(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MetaCampaignState.model_validate(data)


def test_state_model_is_immutable_after_construction() -> None:
    state = MetaCampaignState(
        campaign_client_ref="456789",
        campaign_ref="meta_ads.campaign.456789",
        account_client_ref="123456",
        configured_status="PAUSED",
        effective_status="PAUSED",
    )

    with pytest.raises(ValidationError):
        state.campaign_client_ref = "999999"
