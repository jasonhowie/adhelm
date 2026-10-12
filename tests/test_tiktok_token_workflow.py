"""End-to-end TikTok enrollment with a fake store and a fake token transport only.

No test here opens a socket or an OS credential store.  The token endpoint uses
the fake transport seam, and the CLI is driven through a private Typer root so
that these cases stay independent of the root application's own construction.
The installed `auth tiktok-token` group is registered on `adhelm.cli`; its parity
is covered by the command-parity test and by `discover`.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
import pytest
import typer
from test_tiktok_token_credentials import (
    ACCESS,
    ACCESS_REF,
    ADVERTISER,
    APP_ID,
    APP_SECRET,
    AUTH_CODE,
    GENERIC_SERVICE,
    OTHER_ADVERTISER,
    PROFILE_ID,
    SECRETS,
    MemoryStore,
    configured_directory,
    install_fake_store,
    tiktok_profile,
)
from typer.testing import CliRunner

from adhelm import tiktok_token_credentials as lifecycle
from adhelm import tiktok_token_workflow as workflow
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, OutputPreferences
from adhelm.oauth_enrollment import OAuthTokenDispatchState, OAuthTransportError
from adhelm.tiktok_token_cli import register_commands
from adhelm.tiktok_token_profiles import TIKTOK_TOKEN_SERVICE, tiktok_token_reference

runner = CliRunner()
# Captured before any harness patches it, so the real guard can be restored.
REAL_REQUIRE_INTERACTIVE = workflow._require_interactive


class FakeTokenTransport:
    """Stand-in for the fixed token transport; it records each dispatched body."""

    def __init__(self, responses: list[object], events: list[str]) -> None:
        self._responses = responses
        self._events = events
        self.bodies: list[dict[str, str]] = []

    def __enter__(self) -> FakeTokenTransport:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def post_token_body(self, fields: tuple[tuple[str, str], ...]) -> bytes:
        self._events.append("http:token")
        self.bodies.append(dict(fields))
        payload = self._responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, bytes):
            return payload
        return json.dumps(payload).encode()


def token_body(**updates: object) -> dict[str, Any]:
    data: dict[str, Any] = {
        "access_token": ACCESS,
        "advertiser_ids": [ADVERTISER, OTHER_ADVERTISER],
        "scope": [4, 5],
    }
    data.update({key: value for key, value in updates.items() if key in data or True})
    return {
        "code": 0,
        "message": "OK",
        "request_id": "request-1",
        "data": {key: value for key, value in data.items() if value is not None},
    }


class Harness:
    """One assembled fake environment for the TikTok enrollment workflow."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        token_responses: list[object],
    ) -> None:
        self.events: list[str] = []
        self.store = MemoryStore()
        install_fake_store(monkeypatch, self.store, self.events)
        self.directory = configured_directory(tmp_path)
        self.token = FakeTokenTransport(token_responses, self.events)
        self.hosts: list[str] = []

        def open_transport(host: str) -> FakeTokenTransport:
            self.hosts.append(host)
            return self.token

        monkeypatch.setattr(workflow, "_open_token_transport", open_transport)
        monkeypatch.setattr(workflow, "_require_interactive", lambda _i: None)
        monkeypatch.setattr(workflow, "_prompt_app_id", lambda: APP_ID)
        monkeypatch.setattr(workflow, "_prompt_app_secret", lambda: APP_SECRET)
        monkeypatch.setattr(workflow, "_prompt_auth_code", lambda: AUTH_CODE)

    def enroll(self, **updates: object) -> object:
        return workflow.enroll_tiktok_token(
            self.directory, PROFILE_ID, environment="PRODUCTION", interactive=True, **updates
        )

    def assert_no_lock_held_across_http(self) -> None:
        depth = 0
        for event in self.events:
            if event.startswith("lock:"):
                depth += 1
            elif event.startswith("unlock:"):
                depth -= 1
            elif event.startswith("http:"):
                assert depth == 0, f"lock held across {event}: {self.events}"
        assert depth == 0
        assert any(event.startswith("http:") for event in self.events)


def cli_harness() -> typer.Typer:
    """A standalone root app; the real cli.py wiring is the integrator's hunk."""
    root = typer.Typer()

    @root.callback()
    def _global(
        ctx: typer.Context,
        data_dir: Path = typer.Option(..., "--data-dir"),  # noqa: B008
    ) -> None:
        ctx.obj = {"data_dir": str(Path(data_dir).resolve())}

    register_commands(root)
    return root


# ---------------------------------------------------------------- enrollment


def test_enrollment_sends_only_the_documented_json_body_and_stores_distinct_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[token_body()])

    result = harness.enroll()

    # The whole documented request body: three fields, no grant_type, no
    # redirect_uri, no PKCE parameter.
    assert harness.token.bodies == [
        {"app_id": APP_ID, "auth_code": AUTH_CODE, "secret": APP_SECRET}
    ]
    assert harness.hosts == ["business-api.tiktok.com"]
    assert result.operation == "ENROLL"  # type: ignore[attr-defined]
    assert result.advertiser_binding == "PROVEN_IN_EXCHANGE_RESPONSE"  # type: ignore[attr-defined]
    assert harness.store.values[(GENERIC_SERVICE, ACCESS_REF)] == ACCESS
    secret_ref = tiktok_token_reference(PROFILE_ID, "APP_SECRET")
    assert harness.store.values[(TIKTOK_TOKEN_SERVICE, secret_ref)] == APP_SECRET
    harness.assert_no_lock_held_across_http()


def test_enrollment_refuses_without_an_explicit_local_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])
    monkeypatch.setattr(workflow, "_require_interactive", REAL_REQUIRE_INTERACTIVE)

    with pytest.raises(AdhelmError) as error:
        workflow.enroll_tiktok_token(
            harness.directory, PROFILE_ID, environment="PRODUCTION", interactive=False
        )

    assert error.value.code == "EXPLICIT_LOCAL_ENROLLMENT_REQUIRED"
    assert error.value.exit_code == 4
    assert not [event for event in harness.events if event.startswith("http:")]


def test_enrollment_refuses_the_undocumented_sandbox_host_before_any_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])

    with pytest.raises(AdhelmError) as error:
        workflow.enroll_tiktok_token(
            harness.directory, PROFILE_ID, environment="SANDBOX", interactive=True
        )

    assert error.value.code == "TIKTOK_TOKEN_ENVIRONMENT_NOT_DOCUMENTED"
    assert harness.store.set_calls == []
    assert harness.hosts == []


def test_a_non_zero_business_code_is_a_definite_refusal_carrying_no_response_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {
        "code": 40001,
        "message": "auth_code invalid: leaked-secret-value",
        "request_id": "request-1",
        "data": {},
    }
    harness = Harness(tmp_path, monkeypatch, token_responses=[body])

    with pytest.raises(OAuthTransportError) as error:
        harness.enroll()

    assert error.value.code == "TIKTOK_TOKEN_ERROR_RESPONSE"
    assert error.value.dispatch_state is OAuthTokenDispatchState.DEFINITE_RESPONSE
    assert error.value.exit_code == 4
    assert "leaked-secret-value" not in error.value.message + error.value.next_action
    # The single-use code may have been consumed, so no usable state is left,
    # and nothing at all was written to the credential store.
    assert harness.store.set_calls == []
    assert lifecycle.tiktok_token_status(harness.directory, PROFILE_ID).local_state == "MISSING"


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 0, "message": "OK", "request_id": "r", "data": {"access_token": ""}},
        {"code": 0, "message": "OK", "request_id": "r", "data": {}},
        {
            "code": 0,
            "message": "OK",
            "request_id": "r",
            "data": {"access_token": ACCESS, "unknown": 1},
        },
        {
            "code": 0,
            "message": "OK",
            "request_id": "r",
            "data": {"access_token": ACCESS},
            "extra": 1,
        },
        {"code": True, "message": "OK", "request_id": "r", "data": {"access_token": ACCESS}},
        {
            "code": 0,
            "message": "OK",
            "request_id": "r",
            "data": {"access_token": ACCESS, "advertiser_ids": ["123", "123"]},
        },
        {
            "code": 0,
            "message": "OK",
            "request_id": "r",
            "data": {"access_token": ACCESS, "advertiser_ids": ["not-a-number"]},
        },
        {
            "code": 0,
            "message": "OK",
            "request_id": "r",
            "data": {"access_token": ACCESS, "expires_in": 0},
        },
        {
            "code": 0,
            "message": "OK",
            "request_id": "r",
            "data": {"access_token": ACCESS, "expires_in": 999_999_999},
        },
    ],
)
def test_an_unreviewed_response_shape_is_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[payload])

    with pytest.raises(OAuthTransportError) as error:
        harness.enroll()

    assert error.value.code == "TIKTOK_TOKEN_RESPONSE_UNSUPPORTED"
    assert error.value.dispatch_state is OAuthTokenDispatchState.UNCERTAIN
    assert error.value.exit_code == 6
    assert harness.store.set_calls == []


def test_a_declared_lifetime_in_the_response_is_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = token_body()
    body["data"]["expires_in"] = 86_400
    harness = Harness(tmp_path, monkeypatch, token_responses=[body])

    result = harness.enroll()

    assert result.expiry_source == "PROVIDER_DECLARED"  # type: ignore[attr-defined]
    assert result.access_token_expires_at is not None  # type: ignore[attr-defined]


def test_a_response_without_an_advertiser_list_records_an_unproven_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = token_body()
    body["data"].pop("advertiser_ids")
    harness = Harness(tmp_path, monkeypatch, token_responses=[body])

    result = harness.enroll()

    assert result.advertiser_list_status == "NOT_RETURNED"  # type: ignore[attr-defined]
    assert result.advertiser_binding == "NOT_PROVEN_NO_ADVERTISER_LIST_RETURNED"  # type: ignore[attr-defined]


def test_a_returned_empty_advertiser_list_is_a_definite_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty list is an answer, not an unreadable shape.

    An absent `advertiser_ids` is missing information and records an unproven
    binding.  A returned `[]` is the provider saying this grant reaches no
    advertiser account, so it is reported as a definite authorization outcome
    rather than as the uncertain `TIKTOK_TOKEN_RESPONSE_UNSUPPORTED`.
    """
    body = token_body()
    body["data"]["advertiser_ids"] = []
    harness = Harness(tmp_path, monkeypatch, token_responses=[body])

    with pytest.raises(OAuthTransportError) as error:
        harness.enroll()

    assert error.value.code == "TIKTOK_TOKEN_NO_ADVERTISERS_GRANTED"
    assert error.value.dispatch_state is OAuthTokenDispatchState.DEFINITE_RESPONSE
    assert error.value.exit_code == 4
    # The definite refusal happens before any slot is installed, so nothing
    # durable is left looking usable.
    assert harness.store.set_calls == []
    assert lifecycle.tiktok_token_status(harness.directory, PROFILE_ID).local_state == "MISSING"


def test_two_equal_declared_lifetimes_agree_and_are_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both lifetime field names are unverified, so only a disagreement refuses."""
    body = token_body()
    body["data"]["expires_in"] = 86_400
    body["data"]["token_expires_in"] = 86_400
    harness = Harness(tmp_path, monkeypatch, token_responses=[body])

    result = harness.enroll()

    assert result.expiry_source == "PROVIDER_DECLARED"  # type: ignore[attr-defined]


def test_the_fixed_transport_refuses_a_host_the_environment_map_does_not_contain() -> None:
    """Pinning lives in the transport, not in the caller that resolved the host."""
    with httpx.Client(trust_env=False) as client:
        for host in ("attacker.example", "business-api.tiktok.com@evil.example", ""):
            with pytest.raises(OAuthTransportError) as error:
                workflow.FixedTikTokTokenTransport(host, client)
            assert error.value.code == "TIKTOK_TOKEN_TRANSPORT_HOST_NOT_PINNED"
            assert error.value.dispatch_state is OAuthTokenDispatchState.NOT_DISPATCHED


def test_a_failed_exchange_writes_nothing_to_the_credential_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = OAuthTransportError(
        "TIKTOK_TOKEN_BODY_INVALID",
        "The TikTok token request cannot be prepared.",
        dispatch_state=OAuthTokenDispatchState.NOT_DISPATCHED,
    )
    harness = Harness(tmp_path, monkeypatch, token_responses=[failure])

    with pytest.raises(OAuthTransportError):
        harness.enroll()

    assert harness.store.set_calls == []
    assert lifecycle.tiktok_token_status(harness.directory, PROFILE_ID).local_state == "MISSING"


def test_the_workflow_never_constructs_or_opens_an_authorization_url() -> None:
    source = Path(workflow.__file__).read_text()
    # TikTok shows each app its own authorization URL in My Apps and publishes
    # no constructable template, so this module has no browser or listener at
    # all and never builds a URL.
    assert "authorization_url" not in source
    assert "open_system_browser" not in source
    assert "LoopbackRedirectReceiver" not in source


def test_the_fixed_transport_sends_only_the_three_documented_fields() -> None:
    transport = workflow.FixedTikTokTokenTransport(
        "business-api.tiktok.com", httpx.Client(trust_env=False)
    )
    try:
        assert transport._endpoint == (
            "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"
        )
        for body in (
            (("app_id", APP_ID),),
            (("app_id", APP_ID), ("auth_code", AUTH_CODE), ("grant_type", "x")),
            (("app_id", APP_ID), ("auth_code", AUTH_CODE), ("secret", "")),
        ):
            with pytest.raises(OAuthTransportError) as error:
                transport.post_token_body(body)  # type: ignore[arg-type]
            assert error.value.code == "TIKTOK_TOKEN_BODY_INVALID"
            assert error.value.dispatch_state is OAuthTokenDispatchState.NOT_DISPATCHED
    finally:
        transport.close()


def test_the_fixed_transport_classifies_a_rejection_and_a_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fields = (("app_id", APP_ID), ("auth_code", AUTH_CODE), ("secret", APP_SECRET))

    def transport_for(response: httpx.Response) -> workflow.FixedTikTokTokenTransport:
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _request: response), trust_env=False
        )
        return workflow.FixedTikTokTokenTransport("business-api.tiktok.com", client)

    with transport_for(httpx.Response(400, json={"code": 40001})) as rejected:
        with pytest.raises(OAuthTransportError) as error:
            rejected.post_token_body(fields)
        assert error.value.code == "TIKTOK_TOKEN_REJECTED"
        assert error.value.dispatch_state is OAuthTokenDispatchState.DEFINITE_RESPONSE

    with transport_for(
        httpx.Response(302, headers={"location": "https://elsewhere.example/"})
    ) as redirected:
        with pytest.raises(OAuthTransportError) as redirect_error:
            redirected.post_token_body(fields)
        assert redirect_error.value.dispatch_state is OAuthTokenDispatchState.UNCERTAIN

    with transport_for(httpx.Response(200, text="not json")) as wrong_type:
        with pytest.raises(OAuthTransportError):
            wrong_type.post_token_body(fields)


# ----------------------------------------------------------------------- CLI


def _assert_revocation_guidance_routes(guidance: list[str]) -> None:
    """The shipped guidance names the advertiser's route and the endpoint AdHelm never sends.

    `docs/setup/tiktok_ads.md` (Revoke) quotes the same two strings, and
    `tests/test_setup_guides.py` checks that the guide and this tuple stay in step.
    """
    assert any("ads.tiktok.com/ac/page/authorizations" in line for line in guidance)
    assert any("/oauth2/revoke_token/" in line for line in guidance)
    assert not any("NOT_VERIFIED_AGAINST_RENDERED_DOCS" in line for line in guidance)


def test_the_cli_enroll_status_and_remove_local_route_emits_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[token_body()])
    app = cli_harness()
    arguments = ["--data-dir", str(harness.directory)]

    enroll = runner.invoke(
        app,
        [
            *arguments,
            "tiktok-token",
            "enroll",
            "--profile",
            PROFILE_ID,
            "--interactive",
            "--environment",
            "PRODUCTION",
        ],
    )
    assert enroll.exit_code == 0, enroll.output
    payload = json.loads(enroll.output)["data"]
    assert payload["provider"] == "tiktok_ads"
    assert payload["granted_permissions"] == "NOT_DOCUMENTED_LOCALLY"
    assert payload["refresh_route"] == "NOT_DOCUMENTED"
    assert payload["app_approval_verified"] is False
    assert payload["execution_authority"] is False
    assert payload["advertiser_pseudonym_count"] == 2
    assert payload["advertiser_pseudonyms_omitted"] == 0

    status = runner.invoke(app, [*arguments, "tiktok-token", "status", "--profile", PROFILE_ID])
    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)["data"]
    assert status_payload["local_state"] == "READY"
    assert status_payload["remote_revocation_verified"] is False
    assert status_payload["expiry_source"] == "NOT_DOCUMENTED_LOCALLY"
    assert status_payload["access_token_expired"] is None
    assert any("never revokes" in line for line in status_payload["revocation_guidance"])
    _assert_revocation_guidance_routes(status_payload["revocation_guidance"])

    removed = runner.invoke(
        app, [*arguments, "tiktok-token", "remove-local", "--profile", PROFILE_ID]
    )
    assert removed.exit_code == 0, removed.output
    remove_payload = json.loads(removed.output)["data"]
    assert remove_payload["operation"] == "LOCAL_REMOVE_ONLY"
    assert remove_payload["removed_slot_count"] == 3
    assert remove_payload["remote_revocation_verified"] is False
    _assert_revocation_guidance_routes(remove_payload["revocation_guidance"])

    combined = enroll.output + status.output + removed.output
    for secret in SECRETS:
        assert secret not in combined
    # No raw advertiser identifier reaches output either; only pseudonyms do.
    assert f'"{ADVERTISER}"' not in combined


def test_the_cli_refresh_route_refuses_with_the_documented_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[token_body()])
    app = cli_harness()
    arguments = ["--data-dir", str(harness.directory)]
    runner.invoke(
        app,
        [
            *arguments,
            "tiktok-token",
            "enroll",
            "--profile",
            PROFILE_ID,
            "--interactive",
            "--environment",
            "PRODUCTION",
        ],
    )

    before = len([event for event in harness.events if event.startswith("http:")])

    refresh = runner.invoke(app, [*arguments, "tiktok-token", "refresh", "--profile", PROFILE_ID])

    assert refresh.exit_code == 4
    assert "TIKTOK_TOKEN_REFRESH_NOT_DOCUMENTED" in refresh.output
    # The refusal is local: refresh issues no request of its own.
    assert len([event for event in harness.events if event.startswith("http:")]) == before


def test_the_cli_refresh_refusal_survives_the_smallest_saved_output_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal carries no result, so the saved output bound must not displace it."""
    harness = Harness(tmp_path, monkeypatch, token_responses=[token_body()])
    app = cli_harness()
    arguments = ["--data-dir", str(harness.directory)]
    save_configuration(
        harness.directory,
        Configuration(profiles=[tiktok_profile()], output=OutputPreferences(max_output_bytes=1024)),
        replace=True,
    )

    unenrolled = runner.invoke(
        app, [*arguments, "tiktok-token", "refresh", "--profile", PROFILE_ID]
    )
    assert "TIKTOK_TOKEN_ENROLLMENT_REQUIRED" in unenrolled.output
    assert "OUTPUT_LIMIT_EXCEEDED" not in unenrolled.output
    assert unenrolled.exit_code == 4
    assert len(unenrolled.output.encode()) <= 1024

    enrolled = harness.enroll()
    assert enrolled.operation == "ENROLL"  # type: ignore[attr-defined]
    refresh = runner.invoke(app, [*arguments, "tiktok-token", "refresh", "--profile", PROFILE_ID])
    assert refresh.exit_code == 4
    assert "TIKTOK_TOKEN_REFRESH_NOT_DOCUMENTED" in refresh.output
    assert "OUTPUT_LIMIT_EXCEEDED" not in refresh.output
    assert len(refresh.output.encode()) <= 1024


def test_the_cli_status_route_constructs_no_http_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[token_body()])
    app = cli_harness()
    arguments = ["--data-dir", str(harness.directory)]
    runner.invoke(
        app,
        [
            *arguments,
            "tiktok-token",
            "enroll",
            "--profile",
            PROFILE_ID,
            "--interactive",
            "--environment",
            "PRODUCTION",
        ],
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("status must not construct an HTTP client")

    monkeypatch.setattr(httpx, "Client", forbidden)
    result = runner.invoke(app, [*arguments, "tiktok-token", "status", "--profile", PROFILE_ID])

    assert result.exit_code == 0, result.output


def test_the_cli_registers_exactly_the_four_expected_commands() -> None:
    from adhelm.tiktok_token_cli import app as token_app

    assert sorted(command.name for command in token_app.registered_commands) == [
        "enroll",
        "refresh",
        "remove-local",
        "status",
    ]
