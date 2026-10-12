# ruff: noqa: E501
"""Cloud-project access for Google Ads reads (Google retired developer tokens 2026-09-09).

Sources, fetched 2026-10-12: the Developer token sunset page (last updated 2026-10-07)
and the Access levels page (last updated 2026-09-30) under
https://developers.google.com/google-ads/api/docs/api-policy/.

Every case is offline. Requests are served by `httpx.MockTransport`; nothing reaches
Google or a credential store.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx
import pytest
from typer.testing import CliRunner

from adhelm.cli import app
from adhelm.errors import AdhelmError, ExitCode
from adhelm.providers.google_ads import (
    GOOGLE_ADS_CLASSIFIABLE_ERROR_CODES,
    GOOGLE_ADS_CLOUD_PROJECT_ACCESS_LEVELS,
    GOOGLE_ADS_CLOUD_PROJECT_ACCESS_NOT_OBSERVABLE,
    GoogleAdsClient,
    GoogleAdsReadConfig,
)
from adhelm.transport import SafeHttpClient

CONFIG = GoogleAdsReadConfig(account_ref="account.google.1", customer_client_ref="1234567890")
SECRET_MESSAGE = "customer 1234567890 secret-provider-text"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> GoogleAdsClient:
    return GoogleAdsClient(
        SafeHttpClient(
            httpx.Client(transport=httpx.MockTransport(handler)),
            {"googleads.googleapis.com"},
            sleep=lambda _: None,
        ),
        lambda: "access-fixture-token",
    )


def _failure(code: str, *, message: str = SECRET_MESSAGE) -> dict[str, object]:
    """The shape Google documents for a failed request: `GoogleAdsFailure` details."""
    return {
        "error": {
            "code": 403,
            "message": message,
            "status": "PERMISSION_DENIED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                    "errors": [{"errorCode": {"authorizationError": code}, "message": message}],
                }
            ],
        }
    }


def _discover(response: httpx.Response) -> AdhelmError:
    client = _client(lambda _request: response)
    with pytest.raises(AdhelmError) as raised:
        client.discover_identity(CONFIG)
    return raised.value


def test_a_project_not_approved_for_production_is_a_typed_actionable_error() -> None:
    code = "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"
    error = _discover(httpx.Response(403, json=_failure(code)))
    assert error.code == "GOOGLE_ADS_CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"
    assert error.exit_code == int(ExitCode.AUTHORIZATION)
    assert error.status_code == 403
    assert error.provider_error_code == code
    action = error.next_action
    assert "Google Cloud project" in action
    assert "Google Ads API Overview page" in action
    # The documented progression is Test, Explorer, Basic, Standard: Explorer is the first
    # production level, and Basic is applied for from it.
    assert "Explorer access" in action
    assert action.index("Explorer") < action.index("Basic")
    assert "from Explorer after brand verification" in action
    # The retired route is named only to be ruled out.
    assert "Do not apply through the Google Ads API Center page" in action
    assert "developer token" not in (error.message + action).lower()
    assert SECRET_MESSAGE not in error.message + action
    assert "1234567890" not in error.message + action


def test_a_401_with_the_same_code_is_classified_the_same_way() -> None:
    error = _discover(
        httpx.Response(401, json=_failure("CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"))
    )
    assert error.code == "GOOGLE_ADS_CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"


def test_authorization_error_cites_the_documented_known_issue_and_no_more() -> None:
    error = _discover(httpx.Response(403, json=_failure("AUTHORIZATION_ERROR")))
    assert error.code == "GOOGLE_ADS_AUTHORIZATION_ERROR"
    assert error.exit_code == int(ExitCode.AUTHORIZATION)
    action = error.next_action
    assert "2026-09-09" in action
    assert "known issue" in action
    assert "apply for Explorer access with a new project" in action
    # It offers the ordinary causes too: it never asserts the known issue is the cause.
    assert "Otherwise" in action and "login-customer-id" in action
    assert "developer-token" in action  # only as the URL of Google's page
    assert "developer token" not in action.lower()
    assert SECRET_MESSAGE not in error.message + action


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}}), id="no-code"
        ),
        pytest.param(httpx.Response(403, content=b"<html>forbidden</html>"), id="not-json"),
        pytest.param(httpx.Response(403, json=_failure("USER_PERMISSION_DENIED")), id="other-code"),
        pytest.param(
            httpx.Response(403, json=_failure("ACTION_NOT_PERMITTED")),
            id="action-not-permitted-is-the-generic-v25-permission-error",
        ),
        pytest.param(httpx.Response(403, content=b"[" * 60_000), id="deeply-nested-open-brackets"),
        pytest.param(
            httpx.Response(403, content=b"[" * 60_000 + b"]" * 60_000),
            id="deeply-nested-balanced-brackets",
        ),
        pytest.param(
            httpx.Response(403, content=b'{"a":' * 30_000 + b"1" + b"}" * 30_000),
            id="deeply-nested-objects",
        ),
        pytest.param(httpx.Response(403, content=b'{"errorCode": {"a": "AUTH'), id="truncated"),
        pytest.param(httpx.Response(403, content=b"\xff\xfe\x00"), id="not-utf8"),
        pytest.param(
            httpx.Response(
                403,
                json={
                    "error": {
                        "details": [
                            {
                                "errors": [
                                    {"errorCode": {"authorizationError": "AUTHORIZATION_ERROR"}},
                                    {
                                        "errorCode": {
                                            "authorizationError": "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"
                                        }
                                    },
                                ]
                            }
                        ]
                    }
                },
            ),
            id="two-allowlisted-codes-is-ambiguous",
        ),
        pytest.param(
            httpx.Response(
                403,
                content=b'{"pad":"' + b"x" * 70_000 + b'","errorCode":{"a":"AUTHORIZATION_ERROR"}}',
            ),
            id="oversized-body",
        ),
        pytest.param(
            httpx.Response(403, json={"message": "AUTHORIZATION_ERROR appears only as text"}),
            id="code-outside-an-errorCode-object",
        ),
    ],
)
def test_an_unclassifiable_refusal_stays_a_generic_access_error(response: httpx.Response) -> None:
    error = _discover(response)
    assert error.code == "GOOGLE_ADS_ACCESS_NOT_PERMITTED"
    assert error.provider_error_code is None
    assert "developer token" not in (error.message + error.next_action).lower()
    assert "login-customer-id" in error.next_action


def test_only_the_closed_allowlist_can_be_recorded_from_a_body() -> None:
    assert GOOGLE_ADS_CLASSIFIABLE_ERROR_CODES == {
        "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION",
        "AUTHORIZATION_ERROR",
    }


def test_no_request_carries_a_developer_token_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"customer": {"id": "1234567890", "currencyCode": "USD", "timeZone": "UTC"}}
                ]
            },
        )

    _client(handler).discover_identity(
        GoogleAdsReadConfig(
            account_ref="account.google.1",
            customer_client_ref="1234567890",
            login_customer_client_ref="0987654321",
        )
    )
    assert seen
    for request in seen:
        assert "developer-token" not in request.headers
        assert request.headers["login-customer-id"] == "0987654321"
        assert request.headers["authorization"] == "Bearer access-fixture-token"


def test_the_client_no_longer_accepts_a_developer_token_getter() -> None:
    with pytest.raises(TypeError):
        GoogleAdsClient(  # type: ignore[call-arg, misc]
            SafeHttpClient(
                httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
                {"googleads.googleapis.com"},
            ),
            lambda: "access",
            lambda: "developer",
        )


def test_the_project_access_level_is_never_assumed() -> None:
    assert GOOGLE_ADS_CLOUD_PROJECT_ACCESS_LEVELS == ("TEST", "EXPLORER", "BASIC", "STANDARD")
    assert GOOGLE_ADS_CLOUD_PROJECT_ACCESS_NOT_OBSERVABLE == "NOT_OBSERVABLE"
    assert (
        GOOGLE_ADS_CLOUD_PROJECT_ACCESS_NOT_OBSERVABLE not in GOOGLE_ADS_CLOUD_PROJECT_ACCESS_LEVELS
    )


@pytest.mark.parametrize("command", ["store", "rotate", "remove-local"])
def test_the_auth_commands_no_longer_offer_a_second_credential_slot(
    tmp_path: object, command: str
) -> None:
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path), "auth", command, "--profile", "p", "--slot", "SECONDARY"],
    )
    assert result.exit_code == 2
    # Terminals that colour the usage error split the option name with escape codes.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "slot" in plain
    assert "developer" not in plain.lower()


@pytest.mark.parametrize(
    "body",
    [b"[" * 60_000, b"[" * 60_000 + b"]" * 60_000, b'{"a":' * 30_000, b'{"errorCode": {"a": "AUTH'],
    ids=["open-brackets", "balanced-brackets", "open-objects", "truncated"],
)
def test_the_transport_scan_is_total_for_any_refusal_body(body: bytes) -> None:
    """`SafeHttpClient` records no code, and raises no `RecursionError`, for a hostile body."""
    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(403, content=body))),
        {"googleads.googleapis.com"},
        sleep=lambda _: None,
    )
    with pytest.raises(AdhelmError) as raised:
        transport.request(
            "POST",
            "https://googleads.googleapis.com/v25/customers/1234567890/googleAds:search",
            json={"query": "SELECT customer.id FROM customer"},
            classifiable_error_codes=GOOGLE_ADS_CLASSIFIABLE_ERROR_CODES,
        )
    assert raised.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert raised.value.status_code == 403
    assert raised.value.provider_error_code is None
