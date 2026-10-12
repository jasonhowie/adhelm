"""Reddit's confidential-client token wire contract through the real fixed transport.

The transport is the production ``FixedOAuthTokenTransport`` driven by
``httpx.MockTransport``: no socket is opened and no Reddit request is made.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest

from adhelm.errors import AdhelmError
from adhelm.linkedin_token_profiles import LINKEDIN_OAUTH_CONTRACT
from adhelm.oauth_enrollment import (
    FixedOAuthTokenTransport,
    OAuthAuthorizationTarget,
    OAuthInitialGrant,
    OAuthProviderContract,
    OAuthTokenDispatchState,
    OAuthTransportError,
    consume_callback,
    exchange_authorization_code,
    refresh_access_token,
    start_authorization,
)
from adhelm.reddit_token_profiles import REDDIT_OAUTH_CONTRACT

CLIENT_ID = "reddit-fixture-client-id"
CLIENT_SECRET = "reddit-fixture-app-secret"
REDIRECT_URI = "http://127.0.0.1:51234/oauth/callback"
TOKEN_BODY = {
    "access_token": "reddit-fixture-access-value",
    "token_type": "bearer",
    "expires_in": 3600,
    "scope": "adsread",
    "refresh_token": "reddit-fixture-refresh-value",
}


def target() -> OAuthAuthorizationTarget:
    return OAuthAuthorizationTarget(
        contract=REDDIT_OAUTH_CONTRACT, client_ref=CLIENT_ID, scopes=("adsread",)
    )


def transport_for(
    contract: OAuthProviderContract,
    respond: Callable[[httpx.Request], httpx.Response] = lambda _r: httpx.Response(
        200, json=TOKEN_BODY
    ),
) -> tuple[FixedOAuthTokenTransport, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    return FixedOAuthTokenTransport(contract, client), requests


def basic_credentials(request: httpx.Request) -> tuple[str, str, str]:
    """Decode the scheme, user name and password the request authenticated with."""
    scheme, _, payload = request.headers["authorization"].partition(" ")
    user, _, password = base64.b64decode(payload).decode().partition(":")
    return scheme, user, password


def exchange(
    transport: FixedOAuthTokenTransport, *, client_id: str = CLIENT_ID, secret: str | None
) -> OAuthInitialGrant:
    reddit = OAuthAuthorizationTarget(
        contract=REDDIT_OAUTH_CONTRACT, client_ref=client_id, scopes=("adsread",)
    )
    attempt = start_authorization(reddit, REDIRECT_URI)
    callback = consume_callback(attempt, f"code=fixture-code&state={attempt.state}")
    return exchange_authorization_code(
        attempt, callback, token_transport=transport, client_secret=secret
    )


def test_the_reddit_contract_is_a_permanent_confidential_client_contract() -> None:
    assert REDDIT_OAUTH_CONTRACT.client_authentication == "BASIC_SECRET"
    assert REDDIT_OAUTH_CONTRACT.authorization_parameters == (("duration", "permanent"),)
    assert REDDIT_OAUTH_CONTRACT.pkce_method == "NONE"
    assert REDDIT_OAUTH_CONTRACT.token_endpoint == "https://www.reddit.com/api/v1/access_token"
    assert REDDIT_OAUTH_CONTRACT.authorization_endpoint == (
        "https://www.reddit.com/api/v1/authorize"
    )
    assert REDDIT_OAUTH_CONTRACT.supported_scopes == frozenset({"adsread", "adsedit"})


def test_the_authorization_exchange_authenticates_with_the_app_id_and_secret_as_basic() -> None:
    transport, requests = transport_for(REDDIT_OAUTH_CONTRACT)

    grant = exchange(transport, secret=CLIENT_SECRET)

    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://www.reddit.com/api/v1/access_token"
    expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    # Decode what the request actually carried: the user name is the app ID and the
    # password is the app secret, as in Reddit's `-u '<app ID>:<secret>'`.
    assert basic_credentials(request) == ("Basic", CLIENT_ID, CLIENT_SECRET)
    assert request.headers["user-agent"] == "adhelm-local/0.1"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    form = parse_qs(request.content.decode())
    assert form == {
        "grant_type": ["authorization_code"],
        "code": ["fixture-code"],
        "redirect_uri": [REDIRECT_URI],
    }
    # Neither half of the credential is in the body or the URL.
    assert CLIENT_ID not in request.content.decode()
    assert CLIENT_SECRET not in request.content.decode()
    assert CLIENT_SECRET not in str(request.url)
    # A lower-case token type is the RFC 6749 section 7.1 case-insensitive form.
    assert grant.refresh_status == "GRANTED"
    assert grant.granted_scopes == ("adsread",)
    assert grant.refresh_token_expires_at is None


def test_the_refresh_grant_uses_the_same_basic_authentication() -> None:
    transport, requests = transport_for(REDDIT_OAUTH_CONTRACT)

    grant = refresh_access_token(
        target(),
        refresh_token="reddit-fixture-refresh-value",
        client_secret=CLIENT_SECRET,
        token_transport=transport,
    )

    request = requests[0]
    assert basic_credentials(request) == ("Basic", CLIENT_ID, CLIENT_SECRET)
    assert parse_qs(request.content.decode()) == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["reddit-fixture-refresh-value"],
    }
    assert CLIENT_SECRET not in request.content.decode()
    assert grant.access_token == TOKEN_BODY["access_token"]


@pytest.mark.parametrize("secret", [None, "", "has space", "tab\there", "caf\u00e9", "x" * 16_385])
def test_the_contract_refuses_a_missing_or_malformed_secret_before_any_request(
    secret: str | None,
) -> None:
    transport, requests = transport_for(REDDIT_OAUTH_CONTRACT)

    with pytest.raises(ValueError, match="secret text"):
        exchange(transport, secret=secret)
    with pytest.raises(ValueError, match="secret text"):
        refresh_access_token(
            target(),
            refresh_token="reddit-fixture-refresh-value",
            client_secret=secret,
            token_transport=transport,
        )

    # In particular an empty password, which is what 0.1.0 used to send, never goes out.
    assert requests == []


def test_a_colon_in_the_app_id_is_refused_by_the_authorization_target_for_basic_auth() -> None:
    with pytest.raises(ValueError):
        OAuthAuthorizationTarget(
            contract=REDDIT_OAUTH_CONTRACT, client_ref="app:secret", scopes=("adsread",)
        )


def test_the_transport_still_refuses_a_colon_in_the_app_id_as_defense_in_depth() -> None:
    transport, requests = transport_for(REDDIT_OAUTH_CONTRACT)

    with pytest.raises(OAuthTransportError) as error:
        transport.post_token_form(
            (
                ("client_id", "app:id"),
                ("client_secret", CLIENT_SECRET),
                ("grant_type", "refresh_token"),
                ("refresh_token", "reddit-fixture-refresh-value"),
            )
        )

    assert error.value.dispatch_state is OAuthTokenDispatchState.NOT_DISPATCHED
    assert requests == []


def test_a_colon_in_the_client_id_is_accepted_for_a_form_secret_contract() -> None:
    linkedin = OAuthAuthorizationTarget(
        contract=LINKEDIN_OAUTH_CONTRACT, client_ref="app:id", scopes=("r_ads",)
    )

    assert linkedin.client_ref == "app:id"


def test_a_secret_containing_a_colon_is_sent_whole() -> None:
    transport, requests = transport_for(REDDIT_OAUTH_CONTRACT)

    exchange(transport, secret="part:two")

    assert basic_credentials(requests[0]) == ("Basic", CLIENT_ID, "part:two")


def test_a_confidential_contract_still_sends_its_secret_in_the_form_and_no_basic_header() -> None:
    transport, requests = transport_for(
        LINKEDIN_OAUTH_CONTRACT,
        lambda _r: httpx.Response(
            200, json={"access_token": "linkedin-access-value", "expires_in": 3600}
        ),
    )
    linkedin = OAuthAuthorizationTarget(
        contract=LINKEDIN_OAUTH_CONTRACT, client_ref=CLIENT_ID, scopes=("r_ads", "r_ads_reporting")
    )

    refresh_access_token(
        linkedin,
        refresh_token="linkedin-refresh-value",
        client_secret="linkedin-client-secret",
        token_transport=transport,
    )

    request = requests[0]
    assert "authorization" not in request.headers
    assert "adhelm-local" not in request.headers["user-agent"]
    form = parse_qs(request.content.decode())
    assert form["client_id"] == [CLIENT_ID]
    assert form["client_secret"] == ["linkedin-client-secret"]


def test_a_confidential_contract_refuses_to_run_without_its_secret() -> None:
    transport, requests = transport_for(LINKEDIN_OAUTH_CONTRACT)
    linkedin = OAuthAuthorizationTarget(
        contract=LINKEDIN_OAUTH_CONTRACT, client_ref=CLIENT_ID, scopes=("r_ads", "r_ads_reporting")
    )

    with pytest.raises(ValueError, match="secret text"):
        refresh_access_token(
            linkedin, refresh_token="linkedin-refresh-value", token_transport=transport
        )

    assert requests == []


@pytest.mark.parametrize(
    "fields",
    [
        (("client_secret", CLIENT_SECRET), ("grant_type", "refresh_token")),
        (("client_id", CLIENT_ID), ("grant_type", "refresh_token")),
        (("grant_type", "refresh_token"), ("refresh_token", "x")),
    ],
    ids=["no client ID", "no client secret", "neither"],
)
def test_a_basic_contract_transport_refuses_a_form_without_both_halves_of_the_credential(
    fields: tuple[tuple[str, str], ...],
) -> None:
    transport, requests = transport_for(REDDIT_OAUTH_CONTRACT)

    with pytest.raises(OAuthTransportError) as error:
        transport.post_token_form(fields)

    assert error.value.dispatch_state is OAuthTokenDispatchState.NOT_DISPATCHED
    assert requests == []


@pytest.mark.parametrize("token_type", ["bearer", "Bearer", "BEARER"])
def test_the_token_type_is_case_insensitive(token_type: str) -> None:
    body = {**TOKEN_BODY, "token_type": token_type}
    transport, _ = transport_for(REDDIT_OAUTH_CONTRACT, lambda _r: httpx.Response(200, json=body))

    grant = refresh_access_token(
        target(), refresh_token="x" * 12, client_secret=CLIENT_SECRET, token_transport=transport
    )

    assert grant.access_token == TOKEN_BODY["access_token"]


@pytest.mark.parametrize("token_type", ["mac", "", 7, None])
def test_any_other_token_type_is_refused(token_type: object) -> None:
    body = {**TOKEN_BODY, "token_type": token_type}
    transport, _ = transport_for(REDDIT_OAUTH_CONTRACT, lambda _r: httpx.Response(200, json=body))

    with pytest.raises(AdhelmError) as error:
        refresh_access_token(
            target(), refresh_token="x" * 12, client_secret=CLIENT_SECRET, token_transport=transport
        )

    assert error.value.code == "OAUTH_TOKEN_RESPONSE_UNSUPPORTED"


def test_an_oauth_error_object_with_status_200_is_a_definite_authorization_failure() -> None:
    transport, _ = transport_for(
        REDDIT_OAUTH_CONTRACT,
        lambda _r: httpx.Response(200, json={"error": "invalid_grant"}),
    )

    with pytest.raises(AdhelmError) as error:
        refresh_access_token(
            target(), refresh_token="x" * 12, client_secret=CLIENT_SECRET, token_transport=transport
        )

    assert error.value.exit_code == 4


@pytest.mark.parametrize("status", [400, 401, 403])
def test_a_definite_rejection_status_is_classified_as_a_provider_answer(status: int) -> None:
    transport, _ = transport_for(REDDIT_OAUTH_CONTRACT, lambda _r: httpx.Response(status))

    with pytest.raises(OAuthTransportError) as error:
        refresh_access_token(
            target(), refresh_token="x" * 12, client_secret=CLIENT_SECRET, token_transport=transport
        )

    assert error.value.code == "OAUTH_TOKEN_REJECTED"
    assert error.value.dispatch_state is OAuthTokenDispatchState.DEFINITE_RESPONSE


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_throttle_or_server_error_is_an_uncertain_outcome_not_a_rejection(status: int) -> None:
    transport, requests = transport_for(
        REDDIT_OAUTH_CONTRACT,
        lambda _r: httpx.Response(status, headers={"retry-after": "60"}),
    )

    with pytest.raises(OAuthTransportError) as error:
        refresh_access_token(
            target(), refresh_token="x" * 12, client_secret=CLIENT_SECRET, token_transport=transport
        )

    assert error.value.dispatch_state is OAuthTokenDispatchState.UNCERTAIN
    assert error.value.exit_code == 6
    assert len(requests) == 1


def test_a_redirect_from_the_token_endpoint_is_never_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    transport, _ = transport_for(REDDIT_OAUTH_CONTRACT, handler)

    with pytest.raises(OAuthTransportError) as error:
        refresh_access_token(
            target(), refresh_token="x" * 12, client_secret=CLIENT_SECRET, token_transport=transport
        )

    assert error.value.dispatch_state is OAuthTokenDispatchState.UNCERTAIN
    assert seen == ["https://www.reddit.com/api/v1/access_token"]


def test_a_non_json_success_body_is_refused() -> None:
    transport, _ = transport_for(
        REDDIT_OAUTH_CONTRACT,
        lambda _r: httpx.Response(200, text="<html>", headers={"content-type": "text/html"}),
    )

    with pytest.raises(OAuthTransportError):
        refresh_access_token(
            target(), refresh_token="x" * 12, client_secret=CLIENT_SECRET, token_transport=transport
        )


def test_an_unknown_response_field_is_refused_rather_than_ignored() -> None:
    body = TOKEN_BODY | {"id_token": "unexpected"}
    transport, _ = transport_for(REDDIT_OAUTH_CONTRACT, lambda _r: httpx.Response(200, json=body))

    with pytest.raises(AdhelmError) as error:
        refresh_access_token(
            target(), refresh_token="x" * 12, client_secret=CLIENT_SECRET, token_transport=transport
        )

    assert error.value.code == "OAUTH_TOKEN_RESPONSE_UNSUPPORTED"


def test_a_contract_rejects_an_invalid_client_authentication_or_user_agent() -> None:
    with pytest.raises(ValueError, match="client authentication"):
        OAuthProviderContract(
            provider="reddit_ads",
            authorization_endpoint="https://www.reddit.com/api/v1/authorize",
            token_endpoint="https://www.reddit.com/api/v1/access_token",
            supported_scopes=frozenset({"adsread"}),
            client_authentication="NONE",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="user agent"):
        OAuthProviderContract(
            provider="reddit_ads",
            authorization_endpoint="https://www.reddit.com/api/v1/authorize",
            token_endpoint="https://www.reddit.com/api/v1/access_token",
            supported_scopes=frozenset({"adsread"}),
            user_agent="bad\r\nagent",
        )
