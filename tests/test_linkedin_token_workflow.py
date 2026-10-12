"""End-to-end LinkedIn OAuth lifecycle with a fake store and mock HTTP only.

No test here opens a socket, a browser, or an OS credential store.  The token
endpoint uses the fake token seam and the account identity read uses
``httpx.MockTransport`` through the ordinary ``SafeHttpClient`` boundary.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import TracebackType

import httpx
import pytest
import typer
from test_linkedin_token_credentials import (
    ACCESS,
    ACCOUNT,
    BEARER_REF,
    CLIENT_SECRET,
    GENERIC_SERVICE,
    PROFILE_ID,
    REFRESH,
    MemoryStore,
    configured_directory,
    install_fake_store,
    linkedin_profile,
)
from typer.testing import CliRunner

from adhelm import linkedin_token_credentials as lifecycle
from adhelm import linkedin_token_workflow as workflow
from adhelm.errors import AdhelmError
from adhelm.linkedin_token_cli import register_commands
from adhelm.linkedin_token_profiles import LINKEDIN_TOKEN_SERVICE, linkedin_token_reference

CLIENT_ID = "linkedin-fixture-client-id"
REFRESHED_ACCESS = "linkedin-fixture-refreshed-access-value"
ROTATED_REFRESH = "linkedin-fixture-rotated-refresh-value"
REDIRECT_PORT = 51234
REAL_REQUIRE_INTERACTIVE = workflow._require_interactive
SECRETS = (ACCESS, REFRESH, CLIENT_SECRET, CLIENT_ID, REFRESHED_ACCESS, ROTATED_REFRESH)


class FakeTokenTransport:
    """Stand-in for the fixed token transport; it records each dispatched form."""

    def __init__(self, responses: list[object], events: list[str]) -> None:
        self._responses = responses
        self._events = events
        self.forms: list[dict[str, str]] = []

    def __enter__(self) -> FakeTokenTransport:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def post_token_form(self, fields: tuple[tuple[str, str], ...]) -> bytes:
        self._events.append("http:token")
        self.forms.append(dict(fields))
        payload = self._responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload).encode()


class FakeReceiver:
    """A loopback stand-in; it binds nothing and replays one bound callback."""

    attempts: list[object] = []

    def __init__(self, *, port: int, path: str, **_: object) -> None:
        assert path == "/oauth/callback"
        self.redirect_uri = f"http://127.0.0.1:{port}{path}"

    def __enter__(self) -> FakeReceiver:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def receive_query(self) -> str:
        state = FakeReceiver.attempts[-1].state  # type: ignore[attr-defined]
        return f"code=fixture-authorization-code&state={state}"

    def require_unexpired(self) -> None:
        return None


def account_response(account_id: str = ACCOUNT) -> dict[str, object]:
    return {"id": int(account_id), "currency": "USD", "status": "ACTIVE"}


def initial_token_body(**updates: object) -> dict[str, object]:
    body: dict[str, object] = {
        "access_token": ACCESS,
        "expires_in": 5_184_000,
        "refresh_token": REFRESH,
        "refresh_token_expires_in": 31_536_000,
        "scope": "r_ads r_ads_reporting",
    }
    body.update(updates)
    return {key: value for key, value in body.items() if value is not None}


def refresh_token_body(**updates: object) -> dict[str, object]:
    body: dict[str, object] = {
        "access_token": REFRESHED_ACCESS,
        "expires_in": 5_184_000,
        "refresh_token": ROTATED_REFRESH,
        "refresh_token_expires_in": 31_536_000,
        "scope": "r_ads r_ads_reporting",
    }
    body.update(updates)
    return {key: value for key, value in body.items() if value is not None}


class Harness:
    """One assembled fake environment for the LinkedIn OAuth workflow."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        token_responses: list[object],
        identity_responses: list[object] | None = None,
    ) -> None:
        self.events: list[str] = []
        self.store = MemoryStore()
        install_fake_store(monkeypatch, self.store, self.events)
        self.directory = configured_directory(tmp_path)
        self.token = FakeTokenTransport(token_responses, self.events)
        self.identity_responses = identity_responses or [account_response()]
        self.identity_requests: list[httpx.Request] = []

        def identity_handler(request: httpx.Request) -> httpx.Response:
            self.events.append("http:identity")
            self.identity_requests.append(request)
            payload = self.identity_responses.pop(0)
            if isinstance(payload, int):
                return httpx.Response(payload, json={"message": "denied"})
            return httpx.Response(200, json=payload)

        monkeypatch.setattr(workflow, "_open_token_transport", lambda _c: self.token)
        monkeypatch.setattr(
            workflow,
            "_open_read_client",
            lambda: httpx.Client(transport=httpx.MockTransport(identity_handler), trust_env=False),
        )
        monkeypatch.setattr(workflow, "LoopbackRedirectReceiver", FakeReceiver)
        monkeypatch.setattr(
            workflow, "open_system_browser", lambda attempt: FakeReceiver.attempts.append(attempt)
        )
        monkeypatch.setattr(workflow, "_require_interactive", lambda _i: None)
        monkeypatch.setattr(workflow, "_prompt_client_id", lambda: CLIENT_ID)
        monkeypatch.setattr(workflow, "_prompt_client_secret", lambda: CLIENT_SECRET)
        FakeReceiver.attempts.clear()

    def enroll(self, **updates: object) -> object:
        return workflow.enroll_linkedin_token(
            self.directory, PROFILE_ID, redirect_port=REDIRECT_PORT, interactive=True, **updates
        )

    def refresh(self) -> object:
        return workflow.refresh_linkedin_token(self.directory, PROFILE_ID)

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


def test_enrollment_authorizes_verifies_identity_and_stores_distinct_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])

    result = harness.enroll()

    assert result.operation == "ENROLL"  # type: ignore[attr-defined]
    assert result.granted_scopes == ("r_ads", "r_ads_reporting")  # type: ignore[attr-defined]
    assert result.refresh_status == "GRANTED"  # type: ignore[attr-defined]
    assert harness.token.forms[0]["grant_type"] == "authorization_code"
    assert harness.token.forms[0]["client_id"] == CLIENT_ID
    assert (
        harness.token.forms[0]["redirect_uri"] == f"http://127.0.0.1:{REDIRECT_PORT}/oauth/callback"
    )
    assert harness.store.values[(GENERIC_SERVICE, BEARER_REF)] == ACCESS
    refresh_ref = linkedin_token_reference(PROFILE_ID, "REFRESH_TOKEN")
    assert harness.store.values[(LINKEDIN_TOKEN_SERVICE, refresh_ref)] == REFRESH
    assert harness.identity_requests[0].url.path == f"/rest/adAccounts/{ACCOUNT}"
    assert harness.identity_requests[0].headers["authorization"] == f"Bearer {ACCESS}"
    harness.assert_no_lock_held_across_http()


def test_enrollment_without_a_refresh_token_records_refresh_not_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(refresh_token=None, refresh_token_expires_in=None)],
    )

    result = harness.enroll()

    assert result.refresh_status == "REFRESH_NOT_GRANTED"  # type: ignore[attr-defined]
    assert result.refresh_token_expires_at is None  # type: ignore[attr-defined]
    status = lifecycle.linkedin_token_status(harness.directory, PROFILE_ID)
    assert status.refresh_status == "REFRESH_NOT_GRANTED"
    assert status.refresh_slot_present is False


def test_enrollment_rejects_a_scope_selection_outside_the_read_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])

    with pytest.raises(AdhelmError) as error:
        harness.enroll(scopes=("r_ads", "r_ads_reporting", "rw_ads"))

    assert error.value.code == "LINKEDIN_TOKEN_SCOPE_REJECTED"
    assert error.value.exit_code == 2
    assert not [event for event in harness.events if event.startswith("http:")]
    assert harness.store.set_calls == []


def test_edit_profile_may_request_the_write_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(scope="r_ads r_ads_reporting rw_ads")],
    )
    from adhelm.config import save_configuration
    from adhelm.models import Configuration, OutputPreferences

    save_configuration(
        harness.directory,
        Configuration(
            profiles=[linkedin_profile(purpose="EDIT", scopes=["r_ads", "r_ads_reporting"])],
            output=OutputPreferences(max_output_bytes=8192),
        ),
        replace=True,
    )

    result = harness.enroll(scopes=("rw_ads", "r_ads", "r_ads_reporting"))

    assert result.granted_scopes == ("r_ads", "r_ads_reporting", "rw_ads")  # type: ignore[attr-defined]
    assert result.purpose == "EDIT"  # type: ignore[attr-defined]


def test_a_state_mismatch_stops_enrollment_before_any_token_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    monkeypatch.setattr(
        FakeReceiver, "receive_query", lambda self: "code=stolen&state=attacker-state"
    )

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "OAUTH_CALLBACK_STATE_MISMATCH"
    assert error.value.exit_code == 4
    assert harness.token.forms == []
    assert harness.store.set_calls == []


def test_a_denied_identity_read_reports_the_wrong_slot_diagnosis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body()],
        identity_responses=[401],
    )

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "LINKEDIN_ADS_AUTHORIZATION_REQUIRED"
    assert error.value.exit_code == 4
    assert "access-token slot" in error.value.next_action
    assert harness.store.set_calls == []


def test_an_unexpected_identity_account_stops_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body()],
        identity_responses=[account_response("503099999")],
    )

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "IDENTITY_MISMATCH"
    assert harness.store.set_calls == []


def test_refresh_stores_a_new_bearer_and_never_holds_a_lock_across_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(), refresh_token_body()],
        identity_responses=[account_response(), account_response()],
    )
    harness.enroll()
    harness.events.clear()

    result = harness.refresh()

    assert result.operation == "REFRESH"  # type: ignore[attr-defined]
    assert harness.token.forms[1]["grant_type"] == "refresh_token"
    assert harness.token.forms[1]["refresh_token"] == REFRESH
    assert harness.store.values[(GENERIC_SERVICE, BEARER_REF)] == REFRESHED_ACCESS
    refresh_ref = linkedin_token_reference(PROFILE_ID, "REFRESH_TOKEN")
    assert harness.store.values[(LINKEDIN_TOKEN_SERVICE, refresh_ref)] == ROTATED_REFRESH
    assert harness.identity_requests[1].headers["authorization"] == f"Bearer {REFRESHED_ACCESS}"
    # The refresh POST and the identity read both happen outside every lock.
    assert harness.events.count("http:token") == 1
    assert harness.events.count("http:identity") == 1
    harness.assert_no_lock_held_across_http()


def test_a_rejected_refresh_blocks_the_profile_and_keeps_the_old_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adhelm.oauth_enrollment import OAuthTokenDispatchState, OAuthTransportError

    rejection = OAuthTransportError(
        "OAUTH_TOKEN_REJECTED",
        "The provider rejected the local credential exchange.",
        dispatch_state=OAuthTokenDispatchState.DEFINITE_RESPONSE,
    )
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body(), rejection])
    harness.enroll()

    with pytest.raises(AdhelmError) as error:
        harness.refresh()

    assert error.value.code == "OAUTH_TOKEN_REJECTED"
    assert error.value.exit_code == 4
    assert harness.store.values[(GENERIC_SERVICE, BEARER_REF)] == ACCESS
    status = lifecycle.linkedin_token_status(harness.directory, PROFILE_ID)
    assert status.local_state == "REAUTHORIZATION_REQUIRED"


def test_an_identity_change_after_refresh_is_rejected_before_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(), refresh_token_body()],
        identity_responses=[account_response(), account_response()],
    )
    harness.enroll()
    # Rewrite the stored pseudonym so the post-refresh identity no longer matches.
    state_ref = linkedin_token_reference(PROFILE_ID, "STATE")
    raw = harness.store.values[(LINKEDIN_TOKEN_SERVICE, state_ref)]
    other = lifecycle.linkedin_account_pseudonym(lifecycle.new_identity_salt(), ACCOUNT)
    data = json.loads(raw)
    data["identity_pseudonym"] = other
    harness.store.values[(LINKEDIN_TOKEN_SERVICE, state_ref)] = json.dumps(
        data, separators=(",", ":"), sort_keys=True
    )

    with pytest.raises(AdhelmError) as error:
        harness.refresh()

    assert error.value.code == "LINKEDIN_TOKEN_IDENTITY_MISMATCH"
    assert error.value.exit_code == 4
    assert harness.store.values[(GENERIC_SERVICE, BEARER_REF)] == ACCESS


def test_refresh_without_an_enrollment_requires_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])

    with pytest.raises(AdhelmError) as error:
        harness.refresh()

    assert error.value.code == "LINKEDIN_TOKEN_ENROLLMENT_REQUIRED"
    assert error.value.exit_code == 4
    assert harness.events.count("http:token") == 0


def test_cli_enroll_refresh_and_status_never_echo_secret_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(), refresh_token_body()],
        identity_responses=[account_response(), account_response()],
    )
    runner = CliRunner()
    root = cli_harness()
    base = ["--data-dir", str(harness.directory), "linkedin-token"]

    enroll_result = runner.invoke(
        root,
        [
            *base,
            "enroll",
            "--profile",
            PROFILE_ID,
            "--redirect-port",
            str(REDIRECT_PORT),
            "--interactive",
        ],
    )
    refresh_result = runner.invoke(root, [*base, "refresh", "--profile", PROFILE_ID])
    status_result = runner.invoke(root, [*base, "status", "--profile", PROFILE_ID])

    assert enroll_result.exit_code == 0, enroll_result.output
    assert refresh_result.exit_code == 0, refresh_result.output
    assert status_result.exit_code == 0, status_result.output
    combined = enroll_result.output + refresh_result.output + status_result.output
    for secret in SECRETS:
        assert secret not in combined
    assert ACCOUNT not in combined
    assert BEARER_REF not in combined
    status = json.loads(status_result.output)["data"]
    assert status["local_state"] == "READY"
    assert status["granted_scopes"] == ["r_ads", "r_ads_reporting"]
    assert status["identity_pseudonym"].startswith("linkedin_ads_account_v1.")
    assert status["remote_revocation_verified"] is False
    assert any("Permitted Services" in line for line in status["revocation_guidance"])
    assert any("never revokes" in line for line in status["revocation_guidance"])


def test_cli_reports_exit_code_four_for_a_missing_refresh_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(refresh_token=None, refresh_token_expires_in=None)],
    )
    harness.enroll()
    runner = CliRunner()
    root = cli_harness()

    result = runner.invoke(
        root,
        [
            "--data-dir",
            str(harness.directory),
            "linkedin-token",
            "refresh",
            "--profile",
            PROFILE_ID,
        ],
    )

    assert result.exit_code == 4
    payload = json.loads(result.output)
    assert payload["outcome"] == "FAILED_CLOSED"
    assert payload["diagnostics"][0]["code"] == "REFRESH_NOT_GRANTED"
    for secret in SECRETS:
        assert secret not in result.output


def test_cli_enroll_requires_an_explicit_interactive_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])
    # Restore the real handoff guard that the harness replaced.
    monkeypatch.setattr(workflow, "_require_interactive", REAL_REQUIRE_INTERACTIVE)
    monkeypatch.setattr(
        workflow, "_prompt_client_id", lambda: pytest.fail("no prompt without a handoff")
    )
    runner = CliRunner()
    root = cli_harness()

    result = runner.invoke(
        root,
        [
            "--data-dir",
            str(harness.directory),
            "linkedin-token",
            "enroll",
            "--profile",
            PROFILE_ID,
            "--redirect-port",
            str(REDIRECT_PORT),
        ],
    )

    assert result.exit_code == 4
    assert json.loads(result.output)["diagnostics"][0]["code"] in {
        "EXPLICIT_BROWSER_HANDOFF_REQUIRED",
        "INTERACTIVE_TERMINAL_REQUIRED",
    }


def test_cli_local_removal_is_never_described_as_provider_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.enroll()
    runner = CliRunner()
    root = cli_harness()

    result = runner.invoke(
        root,
        [
            "--data-dir",
            str(harness.directory),
            "linkedin-token",
            "remove-local",
            "--profile",
            PROFILE_ID,
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["operation"] == "LOCAL_REMOVE_ONLY"
    assert payload["removed_slot_count"] == 3
    assert payload["remote_revocation_verified"] is False
    # The configured bearer entry stays with the shared `auth remove-local` contract.
    assert harness.store.values[(GENERIC_SERVICE, BEARER_REF)] == ACCESS


def test_local_redirect_uri_helper_rejects_an_unregisterable_port() -> None:
    assert workflow.local_redirect_uri(51234) == "http://127.0.0.1:51234/oauth/callback"
    with pytest.raises(AdhelmError) as error:
        workflow.local_redirect_uri(80)
    assert error.value.code == "LINKEDIN_TOKEN_REDIRECT_PORT_INVALID"


def test_a_profile_declaring_more_scopes_than_the_selection_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adhelm.config import save_configuration
    from adhelm.models import Configuration, OutputPreferences

    harness = Harness(tmp_path, monkeypatch, token_responses=[])
    save_configuration(
        harness.directory,
        Configuration(
            profiles=[
                linkedin_profile(purpose="EDIT", scopes=["r_ads", "r_ads_reporting", "rw_ads"])
            ],
            output=OutputPreferences(max_output_bytes=8192),
        ),
        replace=True,
    )

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "LINKEDIN_TOKEN_SCOPE_REJECTED"
    assert harness.store.set_calls == []


def test_a_profile_declaring_an_undocumented_scope_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adhelm.config import save_configuration
    from adhelm.models import Configuration, OutputPreferences

    harness = Harness(tmp_path, monkeypatch, token_responses=[])
    save_configuration(
        harness.directory,
        Configuration(
            profiles=[linkedin_profile(scopes=["r_ads", "r_ads_reporting", "r_liteprofile"])],
            output=OutputPreferences(max_output_bytes=8192),
        ),
        replace=True,
    )

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "LINKEDIN_TOKEN_PROFILE_REQUIRED"
    assert harness.store.set_calls == []


def test_scope_check_helper_reports_a_missing_permission() -> None:
    from adhelm.providers.linkedin_ads import check_linkedin_ads_scopes

    assert check_linkedin_ads_scopes(("r_ads", "r_ads_reporting"), required=("r_ads",)) == (
        "r_ads",
        "r_ads_reporting",
    )
    with pytest.raises(AdhelmError) as error:
        check_linkedin_ads_scopes(("r_ads_reporting",), required=("r_ads",))
    assert error.value.code == "LINKEDIN_ADS_SCOPE_MISSING"
    assert error.value.exit_code == 4
    with pytest.raises(AdhelmError) as error:
        check_linkedin_ads_scopes(["r_ads"], required=("r_ads",))
    assert error.value.code == "LINKEDIN_ADS_SCOPE_STATE_INVALID"


def test_no_secret_reaches_configuration_exceptions_or_reprs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adhelm.oauth_enrollment import OAuthTokenDispatchState, OAuthTransportError

    rejection = OAuthTransportError(
        "OAUTH_TOKEN_REJECTED",
        "The provider rejected the local credential exchange.",
        dispatch_state=OAuthTokenDispatchState.DEFINITE_RESPONSE,
    )
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body(), rejection])
    result = harness.enroll()
    context = lifecycle.begin_linkedin_token_refresh(harness.directory, PROFILE_ID)

    with pytest.raises(AdhelmError) as error:
        harness.refresh()

    surfaces = "".join(
        (
            (harness.directory / "config.json").read_text(),
            repr(result),
            str(result),
            repr(context),
            error.value.message,
            error.value.next_action,
            str(error.value),
            repr(error.value),
        )
    )
    for secret in SECRETS:
        assert secret not in surfaces


def test_the_fixed_report_checks_the_recorded_reporting_permission() -> None:
    from adhelm.providers.linkedin_ads import (
        LINKEDIN_ADS_API_HOST,
        LinkedInAdsClient,
        LinkedInAdsIdentity,
        LinkedInAdsReportQuery,
    )
    from adhelm.transport import SafeHttpClient

    client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: pytest.fail("no report request expected")),
        trust_env=False,
    )
    try:
        adapter = LinkedInAdsClient(
            SafeHttpClient(client, {LINKEDIN_ADS_API_HOST}), lambda: "fixture-bearer"
        )
        identity = LinkedInAdsIdentity(
            account_ref=f"linkedin_ads.account.{ACCOUNT}",
            account_client_ref=ACCOUNT,
            currency="USD",
            status="ACTIVE",
        )
        query = LinkedInAdsReportQuery(
            account_ref=f"linkedin_ads.account.{ACCOUNT}",
            report_id="report:linkedin-review-fixture",
            query_contract_hash="a" * 64,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 7),
            level="CAMPAIGN",
        )
        with pytest.raises(AdhelmError) as error:
            adapter.run_campaign_report(query, identity=identity, granted_scopes=("r_ads",))
    finally:
        client.close()

    assert error.value.code == "LINKEDIN_ADS_SCOPE_MISSING"
    assert error.value.exit_code == 4


def test_a_never_dispatched_refresh_can_be_retried_without_re_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adhelm.oauth_enrollment import OAuthTokenDispatchState, OAuthTransportError

    not_dispatched = OAuthTransportError(
        "OAUTH_TOKEN_FORM_INVALID",
        "The OAuth token request cannot be prepared.",
        dispatch_state=OAuthTokenDispatchState.NOT_DISPATCHED,
        exit_code=2,
    )
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(), not_dispatched, refresh_token_body()],
        identity_responses=[account_response(), account_response()],
    )
    harness.enroll()

    with pytest.raises(AdhelmError):
        harness.refresh()
    assert lifecycle.linkedin_token_status(harness.directory, PROFILE_ID).local_state == "READY"

    result = harness.refresh()

    assert result.operation == "REFRESH"  # type: ignore[attr-defined]
    assert harness.store.values[(GENERIC_SERVICE, BEARER_REF)] == REFRESHED_ACCESS


def test_a_broader_grant_is_reported_but_never_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(scope="r_ads r_ads_reporting rw_ads")],
    )
    runner = CliRunner()
    root = cli_harness()

    result = runner.invoke(
        root,
        [
            "--data-dir",
            str(harness.directory),
            "linkedin-token",
            "enroll",
            "--profile",
            PROFILE_ID,
            "--redirect-port",
            str(REDIRECT_PORT),
            "--interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["granted_scopes"] == ["r_ads", "r_ads_reporting"]
    assert payload["unrequested_granted_scopes"] == ["rw_ads"]
    assert (
        "rw_ads"
        not in lifecycle.linkedin_token_status(harness.directory, PROFILE_ID).granted_scopes
    )


def test_a_definite_token_error_body_fails_at_the_authorization_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[{"error": "invalid_grant", "error_description": "expired code"}],
    )
    runner = CliRunner()
    root = cli_harness()

    result = runner.invoke(
        root,
        [
            "--data-dir",
            str(harness.directory),
            "linkedin-token",
            "enroll",
            "--profile",
            PROFILE_ID,
            "--redirect-port",
            str(REDIRECT_PORT),
            "--interactive",
        ],
    )

    assert result.exit_code == 4
    assert json.loads(result.output)["diagnostics"][0]["code"] == "OAUTH_TOKEN_ERROR_RESPONSE"
    assert harness.store.set_calls == []
