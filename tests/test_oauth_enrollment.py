"""Fake-transport coverage for the reusable confidential-client OAuth flow."""

from __future__ import annotations

import json
import pickle
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from adhelm import oauth_enrollment as flow
from adhelm.errors import AdhelmError

PKCE_CONTRACT = flow.OAuthProviderContract(
    provider="fixture_pkce",
    authorization_endpoint="https://auth.example.test/authorize",
    token_endpoint="https://token.example.test/token",
    supported_scopes=frozenset({"read", "write"}),
    pkce_method="S256",
    authorization_parameters=(("prompt", "consent"),),
)
PLAIN_CONTRACT = flow.OAuthProviderContract(
    provider="fixture_plain",
    authorization_endpoint="https://auth.example.test/authorize",
    token_endpoint="https://token.example.test/token",
    supported_scopes=frozenset({"read", "write"}),
    pkce_method="NONE",
    scope_response_separators=(" ", ","),
    maximum_access_token_lifetime_seconds=5_184_000,
    maximum_refresh_token_lifetime_seconds=31_536_000,
)
REDIRECT = "http://127.0.0.1:51234/oauth/callback"
CLIENT_SECRET = "fixture-client-secret-value"
ACCESS = "fixture-access-token-value"
REFRESH = "fixture-refresh-token-value"


class RecordingTransport:
    """A fake token seam that returns fixed bytes and records each form."""

    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.forms: list[dict[str, str]] = []

    def post_token_form(self, fields: tuple[tuple[str, str], ...]) -> bytes:
        self.forms.append(dict(fields))
        if isinstance(self._payload, Exception):
            raise self._payload
        if isinstance(self._payload, bytes):
            return self._payload
        return json.dumps(self._payload).encode()


def target(contract: flow.OAuthProviderContract = PLAIN_CONTRACT) -> flow.OAuthAuthorizationTarget:
    return flow.OAuthAuthorizationTarget(
        contract=contract, client_ref="fixture-client-id", scopes=("read",)
    )


def attempt(
    contract: flow.OAuthProviderContract = PLAIN_CONTRACT,
) -> flow.OAuthAuthorizationAttempt:
    return flow.start_authorization(target(contract), REDIRECT)


def token_body(**updates: object) -> dict[str, object]:
    body: dict[str, object] = {
        "access_token": ACCESS,
        "expires_in": 3_600,
        "refresh_token": REFRESH,
        "refresh_token_expires_in": 31_536_000,
        "scope": "read",
    }
    body.update(updates)
    return {key: value for key, value in body.items() if value is not None}


def test_contract_rejects_a_non_https_or_query_bearing_endpoint() -> None:
    with pytest.raises(ValueError):
        flow.OAuthProviderContract(
            provider="bad",
            authorization_endpoint="http://auth.example.test/authorize",
            token_endpoint="https://token.example.test/token",
            supported_scopes=frozenset({"read"}),
        )
    with pytest.raises(ValueError):
        flow.OAuthProviderContract(
            provider="bad",
            authorization_endpoint="https://auth.example.test/authorize?x=1",
            token_endpoint="https://token.example.test/token",
            supported_scopes=frozenset({"read"}),
        )


def test_contract_rejects_authorization_parameters_that_shadow_the_fixed_ones() -> None:
    with pytest.raises(ValueError):
        flow.OAuthProviderContract(
            provider="bad",
            authorization_endpoint="https://auth.example.test/authorize",
            token_endpoint="https://token.example.test/token",
            supported_scopes=frozenset({"read"}),
            authorization_parameters=(("state", "guessable"),),
        )


def test_target_rejects_a_scope_outside_the_contract_allowlist() -> None:
    with pytest.raises(ValueError):
        flow.OAuthAuthorizationTarget(
            contract=PLAIN_CONTRACT, client_ref="fixture-client-id", scopes=("admin",)
        )


def test_authorization_url_sends_pkce_only_when_the_contract_documents_it() -> None:
    with_pkce = parse_qs(urlsplit(flow.authorization_url(attempt(PKCE_CONTRACT))).query)
    without_pkce = parse_qs(urlsplit(flow.authorization_url(attempt(PLAIN_CONTRACT))).query)

    assert with_pkce["code_challenge_method"] == ["S256"]
    assert len(with_pkce["code_challenge"][0]) == 43
    assert with_pkce["prompt"] == ["consent"]
    assert "code_challenge" not in without_pkce
    assert "code_challenge_method" not in without_pkce
    assert without_pkce["response_type"] == ["code"]
    assert without_pkce["redirect_uri"] == [REDIRECT]
    assert without_pkce["scope"] == ["read"]
    assert len(without_pkce["state"][0]) == 43


def test_each_attempt_uses_fresh_unguessable_state() -> None:
    assert attempt().state != attempt().state


def test_callback_state_mismatch_is_rejected_before_any_exchange() -> None:
    current = attempt()
    transport = RecordingTransport(token_body())

    with pytest.raises(AdhelmError) as error:
        flow.consume_callback(current, "code=abc&state=someone-elses-state")

    assert error.value.code == "OAUTH_CALLBACK_STATE_MISMATCH"
    assert error.value.exit_code == 4
    assert transport.forms == []


def test_callback_error_response_is_reported_as_a_denied_authorization() -> None:
    current = attempt()

    with pytest.raises(AdhelmError) as error:
        flow.consume_callback(current, f"error=user_cancelled_authorize&state={current.state}")

    assert error.value.code == "OAUTH_AUTHORIZATION_DENIED"
    assert error.value.exit_code == 4


def test_callback_with_both_code_and_error_is_rejected() -> None:
    current = attempt()

    with pytest.raises(AdhelmError) as error:
        flow.consume_callback(current, f"code=abc&error=denied&state={current.state}")

    assert error.value.code == "OAUTH_CALLBACK_INVALID"


def test_expired_attempt_cannot_be_exchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    current = attempt()
    callback = flow.consume_callback(current, f"code=fixture-code&state={current.state}")
    monkeypatch.setattr(flow, "_utc_now", lambda: datetime.now(UTC) + timedelta(minutes=11))

    with pytest.raises(AdhelmError) as error:
        flow.exchange_authorization_code(
            current,
            callback,
            token_transport=RecordingTransport(token_body()),
            client_secret=CLIENT_SECRET,
        )

    assert error.value.code == "OAUTH_AUTHORIZATION_EXPIRED"
    assert error.value.exit_code == 4


def test_confidential_exchange_sends_the_documented_form_and_returns_a_grant() -> None:
    current = attempt()
    callback = flow.consume_callback(current, f"code=fixture-code&state={current.state}")
    transport = RecordingTransport(token_body())

    grant = flow.exchange_authorization_code(
        current, callback, token_transport=transport, client_secret=CLIENT_SECRET
    )

    assert set(transport.forms[0]) == {
        "client_id",
        "client_secret",
        "code",
        "grant_type",
        "redirect_uri",
    }
    assert transport.forms[0]["grant_type"] == "authorization_code"
    assert transport.forms[0]["redirect_uri"] == REDIRECT
    assert grant.access_token == ACCESS
    assert grant.refresh_token == REFRESH
    assert grant.refresh_status == "GRANTED"
    assert grant.granted_scopes == ("read",)


def test_pkce_exchange_includes_the_verifier() -> None:
    current = attempt(PKCE_CONTRACT)
    callback = flow.consume_callback(current, f"code=fixture-code&state={current.state}")
    transport = RecordingTransport(token_body(refresh_token=None, refresh_token_expires_in=None))

    flow.exchange_authorization_code(
        current, callback, token_transport=transport, client_secret=CLIENT_SECRET
    )

    assert transport.forms[0]["code_verifier"] == current.code_verifier
    assert flow.pkce_s256_challenge(current.code_verifier or "") != current.code_verifier


def test_missing_refresh_token_is_explicit_and_never_a_silent_fallback() -> None:
    current = attempt()
    callback = flow.consume_callback(current, f"code=fixture-code&state={current.state}")
    transport = RecordingTransport(token_body(refresh_token=None, refresh_token_expires_in=None))

    grant = flow.exchange_authorization_code(
        current, callback, token_transport=transport, client_secret=CLIENT_SECRET
    )

    assert grant.refresh_status == "REFRESH_NOT_GRANTED"
    assert grant.refresh_token is None
    assert grant.refresh_token_expires_at is None


def test_a_narrower_granted_scope_fails_closed() -> None:
    contract = flow.OAuthProviderContract(
        provider="fixture_plain",
        authorization_endpoint="https://auth.example.test/authorize",
        token_endpoint="https://token.example.test/token",
        supported_scopes=frozenset({"read", "write"}),
    )
    wide = flow.OAuthAuthorizationTarget(
        contract=contract, client_ref="fixture-client-id", scopes=("read", "write")
    )
    current = flow.start_authorization(wide, REDIRECT)
    callback = flow.consume_callback(current, f"code=fixture-code&state={current.state}")

    with pytest.raises(AdhelmError) as error:
        flow.exchange_authorization_code(
            current,
            callback,
            token_transport=RecordingTransport(token_body(scope="read")),
            client_secret=CLIENT_SECRET,
        )

    assert error.value.code == "OAUTH_SCOPE_NOT_GRANTED"
    assert error.value.exit_code == 4


def test_comma_separated_scope_text_is_accepted_when_the_contract_allows_it() -> None:
    contract = PLAIN_CONTRACT
    wide = flow.OAuthAuthorizationTarget(
        contract=contract, client_ref="fixture-client-id", scopes=("read", "write")
    )
    current = flow.start_authorization(wide, REDIRECT)
    callback = flow.consume_callback(current, f"code=fixture-code&state={current.state}")

    grant = flow.exchange_authorization_code(
        current,
        callback,
        token_transport=RecordingTransport(token_body(scope="read,write")),
        client_secret=CLIENT_SECRET,
    )

    assert grant.granted_scopes == ("read", "write")


def test_an_undocumented_token_response_field_fails_closed() -> None:
    current = attempt()
    callback = flow.consume_callback(current, f"code=fixture-code&state={current.state}")

    with pytest.raises(AdhelmError) as error:
        flow.exchange_authorization_code(
            current,
            callback,
            token_transport=RecordingTransport(token_body(id_token="unexpected")),
            client_secret=CLIENT_SECRET,
        )

    assert error.value.code == "OAUTH_TOKEN_RESPONSE_UNSUPPORTED"


def test_a_token_lifetime_beyond_the_contract_bound_fails_closed() -> None:
    current = attempt()
    callback = flow.consume_callback(current, f"code=fixture-code&state={current.state}")

    accepted = flow.exchange_authorization_code(
        current,
        callback,
        token_transport=RecordingTransport(token_body(expires_in=5_184_000)),
        client_secret=CLIENT_SECRET,
    )
    assert accepted.access_token == ACCESS

    with pytest.raises(AdhelmError) as error:
        flow.exchange_authorization_code(
            current,
            callback,
            token_transport=RecordingTransport(token_body(expires_in=5_184_001)),
            client_secret=CLIENT_SECRET,
        )

    assert error.value.code == "OAUTH_TOKEN_RESPONSE_UNSUPPORTED"


def test_refresh_returns_a_new_bearer_and_optional_rotation() -> None:
    transport = RecordingTransport(token_body(scope="read"))

    grant = flow.refresh_access_token(
        target(),
        refresh_token=REFRESH,
        client_secret=CLIENT_SECRET,
        token_transport=transport,
    )

    assert transport.forms[0]["grant_type"] == "refresh_token"
    assert transport.forms[0]["refresh_token"] == REFRESH
    assert grant.access_token == ACCESS
    assert grant.rotated_refresh_token == REFRESH
    assert grant.granted_scopes == ("read",)


def test_refresh_is_refused_when_the_contract_documents_no_refresh_grant() -> None:
    contract = flow.OAuthProviderContract(
        provider="fixture_norefresh",
        authorization_endpoint="https://auth.example.test/authorize",
        token_endpoint="https://token.example.test/token",
        supported_scopes=frozenset({"read"}),
        refresh_grant_documented=False,
    )
    transport = RecordingTransport(token_body())

    with pytest.raises(AdhelmError) as error:
        flow.refresh_access_token(
            flow.OAuthAuthorizationTarget(
                contract=contract, client_ref="fixture-client-id", scopes=("read",)
            ),
            refresh_token=REFRESH,
            client_secret=CLIENT_SECRET,
            token_transport=transport,
        )

    assert error.value.code == "OAUTH_REFRESH_GRANT_NOT_DOCUMENTED"
    assert transport.forms == []


def test_refresh_rejection_is_a_definite_provider_response() -> None:
    transport = RecordingTransport(
        flow.OAuthTransportError(
            "OAUTH_TOKEN_REJECTED",
            "rejected",
            dispatch_state=flow.OAuthTokenDispatchState.DEFINITE_RESPONSE,
        )
    )

    with pytest.raises(flow.OAuthTransportError) as error:
        flow.refresh_access_token(
            target(),
            refresh_token=REFRESH,
            client_secret=CLIENT_SECRET,
            token_transport=transport,
        )

    assert error.value.dispatch_state is flow.OAuthTokenDispatchState.DEFINITE_RESPONSE
    assert error.value.exit_code == 4


def test_an_unexpected_transport_failure_is_reported_as_uncertain() -> None:
    with pytest.raises(flow.OAuthTransportError) as error:
        flow.refresh_access_token(
            target(),
            refresh_token=REFRESH,
            client_secret=CLIENT_SECRET,
            token_transport=RecordingTransport(RuntimeError("boom")),
        )

    assert error.value.code == "OAUTH_TOKEN_OUTCOME_UNCERTAIN"
    assert error.value.dispatch_state is flow.OAuthTokenDispatchState.UNCERTAIN
    assert error.value.exit_code == 6


def _fixed_transport(handler: object) -> flow.FixedOAuthTokenTransport:
    return flow.FixedOAuthTokenTransport(
        PLAIN_CONTRACT,
        httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),  # type: ignore[arg-type]
    )


def test_fixed_transport_posts_only_the_contract_endpoint_form() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=token_body())

    with _fixed_transport(handler) as transport:
        body = transport.post_token_form(
            (("client_id", "fixture-client-id"), ("grant_type", "refresh_token"))
        )

    assert json.loads(body)["access_token"] == ACCESS
    assert str(seen[0].url) == PLAIN_CONTRACT.token_endpoint
    assert seen[0].headers["content-type"] == "application/x-www-form-urlencoded"


def test_fixed_transport_rejects_a_form_key_outside_the_token_contract() -> None:
    with _fixed_transport(lambda _r: pytest.fail("no dispatch expected")) as transport:
        with pytest.raises(flow.OAuthTransportError) as error:
            transport.post_token_form((("assertion", "value"),))

    assert error.value.code == "OAUTH_TOKEN_FORM_INVALID"
    assert error.value.dispatch_state is flow.OAuthTokenDispatchState.NOT_DISPATCHED


def test_fixed_transport_treats_a_400_as_a_definite_rejection() -> None:
    with _fixed_transport(lambda _r: httpx.Response(400, json={"error": "invalid_request"})) as t:
        with pytest.raises(flow.OAuthTransportError) as error:
            t.post_token_form((("grant_type", "refresh_token"),))

    assert error.value.code == "OAUTH_TOKEN_REJECTED"
    assert error.value.dispatch_state is flow.OAuthTokenDispatchState.DEFINITE_RESPONSE


def test_fixed_transport_refuses_a_redirect_or_non_json_response() -> None:
    with _fixed_transport(lambda _r: httpx.Response(302, headers={"location": "/x"})) as t:
        with pytest.raises(flow.OAuthTransportError) as error:
            t.post_token_form((("grant_type", "refresh_token"),))
    assert error.value.code == "OAUTH_TOKEN_OUTCOME_UNCERTAIN"

    with _fixed_transport(lambda _r: httpx.Response(200, text="ok")) as t:
        with pytest.raises(flow.OAuthTransportError) as error:
            t.post_token_form((("grant_type", "refresh_token"),))
    assert error.value.code == "OAUTH_TOKEN_OUTCOME_UNCERTAIN"


def test_secret_bearing_values_are_redacted_and_not_serializable() -> None:
    current = attempt()
    callback = flow.consume_callback(current, f"code=fixture-code&state={current.state}")
    grant = flow.exchange_authorization_code(
        current,
        callback,
        token_transport=RecordingTransport(token_body()),
        client_secret=CLIENT_SECRET,
    )

    for value in (current, callback, grant):
        assert "redacted" in repr(value)
        with pytest.raises(TypeError):
            pickle.dumps(value)
    assert ACCESS not in repr(grant)
    assert REFRESH not in repr(grant)


def test_redirect_uri_must_be_an_ipv4_loopback_url() -> None:
    for bad in (
        "https://example.test/oauth/callback",
        "http://localhost:51234/oauth/callback",
        "http://127.0.0.1/oauth/callback",
        "http://127.0.0.1:51234/oauth/callback?x=1",
    ):
        with pytest.raises(AdhelmError):
            flow.start_authorization(target(), bad)


def test_hidden_prompt_requires_two_terminals(monkeypatch: pytest.MonkeyPatch) -> None:
    class Stream:
        def __init__(self, tty: bool) -> None:
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    with pytest.raises(AdhelmError) as error:
        flow.prompt_hidden_secret(
            "LinkedIn client ID: ",
            stdin=Stream(False),  # type: ignore[arg-type]
            stderr=Stream(True),  # type: ignore[arg-type]
            getpass_function=lambda *_a, **_k: pytest.fail("no prompt expected"),
        )

    assert error.value.code == "INTERACTIVE_TERMINAL_REQUIRED"
    assert error.value.exit_code == 4


def test_hidden_prompt_rejects_an_empty_or_control_character_value() -> None:
    class Stream:
        @staticmethod
        def isatty() -> bool:
            return True

    for bad in ("", "value\nwith-newline"):
        with pytest.raises(AdhelmError) as error:
            flow.prompt_hidden_secret(
                "LinkedIn client ID: ",
                stdin=Stream(),  # type: ignore[arg-type]
                stderr=Stream(),  # type: ignore[arg-type]
                getpass_function=lambda *_a, _bad=bad, **_k: _bad,
            )
        assert error.value.code == "SECRET_VALUE_INVALID"


def test_loopback_receiver_binds_a_requested_port_and_fixed_path() -> None:
    with flow.LoopbackRedirectReceiver(port=0, path="/oauth/callback") as receiver:
        parsed = urlsplit(receiver.redirect_uri)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.path == "/oauth/callback"
        assert parsed.port is not None


def test_loopback_receiver_rejects_an_invalid_port_or_path() -> None:
    with pytest.raises(AdhelmError) as error:
        flow.LoopbackRedirectReceiver(port=70_000)
    assert error.value.code == "OAUTH_LOOPBACK_PORT_INVALID"

    with pytest.raises(AdhelmError) as error:
        flow.LoopbackRedirectReceiver(path="/oauth/callback?x=1")
    assert error.value.code == "OAUTH_LOOPBACK_PATH_INVALID"


class FakeConnection:
    """A socket stand-in for the loopback HTTP parser; it opens nothing."""

    def __init__(self, request: bytes) -> None:
        self._buffer = bytearray(request)
        self.sent = bytearray()
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv(self, size: int) -> bytes:
        chunk = bytes(self._buffer[:size])
        del self._buffer[: len(chunk)]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)


def _receiver() -> flow.LoopbackRedirectReceiver:
    return flow.LoopbackRedirectReceiver(port=0, path="/oauth/callback")


def _request(receiver: flow.LoopbackRedirectReceiver, *lines: str, body: str = "") -> bytes:
    head = "\r\n".join(lines)
    return f"{head}\r\n\r\n{body}".encode()


def test_loopback_parser_accepts_one_exact_callback_request() -> None:
    with _receiver() as receiver:
        connection = FakeConnection(
            _request(
                receiver,
                "GET /oauth/callback?code=abc&state=xyz HTTP/1.1",
                f"Host: {receiver._host}",
                "User-Agent: fixture",
            )
        )

        assert receiver._read_request(connection) == "code=abc&state=xyz"


def test_loopback_parser_rejects_a_request_outside_the_bound_callback() -> None:
    request_lines = (
        "GET /other?code=abc HTTP/1.1",
        "GET /oauth/callback HTTP/1.1",
        "POST /oauth/callback?code=abc HTTP/1.1",
        "GET /oauth/callback?code=abc HTTP/1.0",
        "GET http://evil.test/oauth/callback?code=abc HTTP/1.1",
        "GET /oauth/callback?code=abc#f HTTP/1.1",
    )
    with _receiver() as receiver:
        for request_line in request_lines:
            connection = FakeConnection(_request(receiver, request_line, f"Host: {receiver._host}"))
            with pytest.raises(ValueError):
                receiver._read_request(connection)


def test_loopback_parser_rejects_a_foreign_host_header() -> None:
    with _receiver() as receiver:
        connection = FakeConnection(
            _request(
                receiver, "GET /oauth/callback?code=abc HTTP/1.1", "Host: attacker.example.test"
            )
        )

        with pytest.raises(ValueError):
            receiver._read_request(connection)


def test_loopback_parser_rejects_a_cross_origin_or_duplicated_header() -> None:
    with _receiver() as receiver:
        origin = FakeConnection(
            _request(
                receiver,
                "GET /oauth/callback?code=abc HTTP/1.1",
                f"Host: {receiver._host}",
                "Origin: https://attacker.example.test",
            )
        )
        with pytest.raises(ValueError):
            receiver._read_request(origin)

        duplicated = FakeConnection(
            _request(
                receiver,
                "GET /oauth/callback?code=abc HTTP/1.1",
                f"Host: {receiver._host}",
                f"Host: {receiver._host}",
            )
        )
        with pytest.raises(ValueError):
            receiver._read_request(duplicated)


def test_loopback_parser_rejects_a_request_body_or_transfer_encoding() -> None:
    with _receiver() as receiver:
        with_body = FakeConnection(
            _request(
                receiver,
                "GET /oauth/callback?code=abc HTTP/1.1",
                f"Host: {receiver._host}",
                "Content-Length: 4",
                body="data",
            )
        )
        with pytest.raises(ValueError):
            receiver._read_request(with_body)

        chunked = FakeConnection(
            _request(
                receiver,
                "GET /oauth/callback?code=abc HTTP/1.1",
                f"Host: {receiver._host}",
                "Transfer-Encoding: chunked",
            )
        )
        with pytest.raises(ValueError):
            receiver._read_request(chunked)


def test_loopback_parser_rejects_an_oversize_header_block() -> None:
    with _receiver() as receiver:
        connection = FakeConnection(
            b"GET /oauth/callback?code=abc HTTP/1.1\r\nX-Pad: " + b"a" * 20_000
        )

        with pytest.raises(ValueError):
            receiver._read_request(connection)


def test_loopback_parser_rejects_an_incomplete_request() -> None:
    with _receiver() as receiver:
        with pytest.raises(ValueError):
            receiver._read_request(FakeConnection(b"GET /oauth/callback?code=abc HTTP/1.1\r\n"))


def test_loopback_reply_is_a_no_store_text_response() -> None:
    connection = FakeConnection(b"")
    flow.LoopbackRedirectReceiver._reply(connection, accepted=True)

    assert connection.sent.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Cache-Control: no-store" in connection.sent
    assert b"Content-Security-Policy: default-src 'none'" in connection.sent

    rejected = FakeConnection(b"")
    flow.LoopbackRedirectReceiver._reply(rejected, accepted=False)
    assert rejected.sent.startswith(b"HTTP/1.1 400 Bad Request\r\n")


def test_a_loopback_callback_is_consumed_exactly_once() -> None:
    receiver = _receiver()
    receiver._used = True
    try:
        with pytest.raises(AdhelmError) as error:
            receiver.receive_query()
    finally:
        receiver.close()

    assert error.value.code == "OAUTH_CALLBACK_CONSUMED"


def test_a_closed_loopback_receiver_cannot_be_reused() -> None:
    receiver = _receiver()
    receiver.close()

    with pytest.raises(AdhelmError) as error:
        receiver.receive_query()

    assert error.value.code == "OAUTH_CALLBACK_CONSUMED"
