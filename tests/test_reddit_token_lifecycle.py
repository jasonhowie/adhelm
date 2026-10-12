"""Reddit Ads confidential-client OAuth lifecycle with a fake store and mock HTTP only.

No test here opens a socket, a browser, or an OS credential store.  The token
endpoint uses a fake seam, and the account identity read uses
``httpx.MockTransport`` through the ordinary ``SafeHttpClient`` boundary.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_linkedin_token_credentials import MemoryStore
from typer.testing import CliRunner

from adhelm import config as config_module
from adhelm import reddit_token_credentials as lifecycle
from adhelm import reddit_token_workflow as workflow
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import (
    Configuration,
    OutputPreferences,
    Profile,
    is_reserved_credential_reference,
)
from adhelm.oauth_enrollment import (
    FixedOAuthTokenTransport,
    OAuthTokenDispatchState,
    OAuthTransportError,
    authorization_url,
    prompt_hidden_secret,
)
from adhelm.reddit_token_profiles import (
    REDDIT_TOKEN_SERVICE,
    RedditTokenSlot,
    is_reddit_token_reference,
    reddit_ads_scope_selection,
    reddit_token_reference,
    select_reddit_ads_profile,
)

ACCESS = "reddit-fixture-access-value"
REFRESH = "reddit-fixture-refresh-value"
REFRESHED_ACCESS = "reddit-fixture-refreshed-access-value"
ROTATED_REFRESH = "reddit-fixture-rotated-refresh-value"
CLIENT_ID = "reddit-fixture-client-id"
CLIENT_SECRET = "reddit-fixture-app-secret"
ACCOUNT = "a2_fixture"
OTHER_ACCOUNT = "a2_other"
BEARER_REF = "reddit-read-bearer"
PROFILE_ID = "reddit-read"
GENERIC_SERVICE = "adhelm"
REDIRECT_PORT = 51234
REAL_REQUIRE_INTERACTIVE = workflow._require_interactive
SECRETS = (ACCESS, REFRESH, REFRESHED_ACCESS, ROTATED_REFRESH, CLIENT_ID, CLIENT_SECRET)


def reddit_profile(**updates: object) -> Profile:
    value: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "provider": "reddit_ads",
        "account_ref": ACCOUNT,
        "principal_ref": "reddit-user-local",
        "client_ref": "reddit-app-local",
        "scopes": ["adsread"],
        "purpose": "READ",
        "currency": "USD",
        "timezone": "UTC",
        "credential_source": "OS_STORE",
        "credential_ref": BEARER_REF,
    }
    value.update(updates)
    return Profile.model_validate(value)


def configured_directory(tmp_path: Path, profile: Profile | None = None) -> Path:
    directory = tmp_path / "data"
    save_configuration(
        directory,
        Configuration(
            profiles=[profile or reddit_profile()], output=OutputPreferences(max_output_bytes=8192)
        ),
    )
    return directory


def account_response(account_id: str = ACCOUNT) -> dict[str, object]:
    return {"data": {"id": account_id, "currency": "USD", "time_zone_id": "UTC"}}


def initial_token_body(**updates: object) -> dict[str, object]:
    body: dict[str, object] = {
        "access_token": ACCESS,
        "token_type": "bearer",
        "expires_in": 3600,
        "refresh_token": REFRESH,
        "scope": "adsread",
    }
    body.update(updates)
    return {key: value for key, value in body.items() if value is not None}


def refresh_token_body(**updates: object) -> dict[str, object]:
    body: dict[str, object] = {
        "access_token": REFRESHED_ACCESS,
        "token_type": "bearer",
        "expires_in": 3600,
        "scope": "adsread",
    }
    body.update(updates)
    return {key: value for key, value in body.items() if value is not None}


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


class Harness:
    """One assembled fake environment for the Reddit OAuth workflow."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        token_responses: list[object],
        identity_responses: list[object] | None = None,
        profile: Profile | None = None,
    ) -> None:
        self.events: list[str] = []
        self.store = MemoryStore()
        self.monkeypatch = monkeypatch

        @contextmanager
        def recording_lock(reference: str) -> Iterator[None]:
            self.events.append(f"lock:{reference}")
            try:
                yield
            finally:
                self.events.append(f"unlock:{reference}")

        monkeypatch.setattr(lifecycle, "_credential_backend", lambda: self.store)
        monkeypatch.setattr(lifecycle, "credential_mutation_lock", recording_lock)
        monkeypatch.setattr(config_module, "credential_mutation_lock", recording_lock)
        self.directory = configured_directory(tmp_path, profile)
        self.token = FakeTokenTransport(token_responses, self.events)
        self.identity_responses = identity_responses or [account_response()]
        self.identity_requests: list[httpx.Request] = []
        self.prompts: list[str] = []

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
        monkeypatch.setattr(workflow, "_prompt_client_id", self._prompt_client_id)
        monkeypatch.setattr(workflow, "_prompt_client_secret", self._prompt_client_secret)
        FakeReceiver.attempts.clear()

    def _prompt_client_id(self) -> str:
        self.prompts.append("client-id")
        return CLIENT_ID

    def _prompt_client_secret(self) -> str:
        self.prompts.append("client-secret")
        return CLIENT_SECRET

    def enroll(
        self, *, scopes: tuple[str, ...] | None = None
    ) -> lifecycle.RedditTokenLifecycleResult:
        return workflow.enroll_reddit_token(
            self.directory,
            PROFILE_ID,
            redirect_port=REDIRECT_PORT,
            interactive=True,
            scopes=scopes,
        )

    def refresh(self) -> lifecycle.RedditTokenLifecycleResult:
        return workflow.refresh_reddit_token(self.directory, PROFILE_ID)

    def status(self) -> lifecycle.RedditTokenStatus:
        return lifecycle.reddit_token_status(self.directory, PROFILE_ID)

    def use_real_token_transport(self, *, responses: list[object]) -> list[httpx.Request]:
        """Send token requests through the production transport to a mock endpoint."""
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            self.events.append("http:token")
            requests.append(request)
            return httpx.Response(200, json=responses.pop(0))

        self.monkeypatch.setattr(
            workflow,
            "_open_token_transport",
            lambda contract: FixedOAuthTokenTransport(
                contract, httpx.Client(transport=httpx.MockTransport(respond))
            ),
        )
        return requests

    def slot(self, name: RedditTokenSlot) -> str | None:
        reference = reddit_token_reference(PROFILE_ID, name)
        return self.store.values.get((REDDIT_TOKEN_SERVICE, reference))

    def bearer(self) -> str | None:
        return self.store.values.get((GENERIC_SERVICE, BEARER_REF))

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


# --- profile and scope contract -------------------------------------------------


def test_derived_slots_are_distinct_internal_and_reserved() -> None:
    secret = reddit_token_reference(PROFILE_ID, "CLIENT_SECRET")
    refresh = reddit_token_reference(PROFILE_ID, "REFRESH_TOKEN")
    state = reddit_token_reference(PROFILE_ID, "STATE")

    assert len({secret, refresh, state}) == 3
    assert reddit_token_reference("other", "CLIENT_SECRET") != secret
    assert reddit_token_reference("other", "STATE") != state
    for reference in (secret, refresh, state):
        assert "#" in reference
        assert is_reddit_token_reference(reference)
        assert is_reserved_credential_reference(reference)
        # A configured credential reference can never contain the separator.
        with pytest.raises(ValueError, match="credential_ref"):
            reddit_profile(credential_ref=reference)
    assert not is_reddit_token_reference(BEARER_REF)


def test_slot_derivation_refuses_a_foreign_slot_and_a_malformed_profile_id() -> None:
    with pytest.raises(AdhelmError) as slot:
        reddit_token_reference(PROFILE_ID, "ACCESS")  # type: ignore[arg-type]
    with pytest.raises(AdhelmError) as profile:
        reddit_token_reference("bad id\n", "STATE")

    assert slot.value.code == "REDDIT_TOKEN_SLOT_INVALID"
    assert profile.value.code == "REDDIT_TOKEN_PROFILE_REQUIRED"


def test_the_default_selection_is_the_least_privilege_read_scope() -> None:
    assert reddit_ads_scope_selection("READ") == ("adsread",)
    assert reddit_ads_scope_selection("EDIT", ("adsedit", "adsread")) == ("adsedit", "adsread")


@pytest.mark.parametrize(
    ("purpose", "requested"),
    [
        ("READ", ("adsread", "adsedit")),
        ("READ", ("adsedit",)),
        ("EDIT", ("adsedit",)),
        ("READ", ("adsconversions",)),
        ("READ", ("adsread", "adsread")),
        ("READ", ()),
        ("READ", ["adsread"]),
    ],
)
def test_scope_selection_refuses_anything_outside_the_purpose_allowlist(
    purpose: str, requested: object
) -> None:
    with pytest.raises(AdhelmError) as error:
        reddit_ads_scope_selection(purpose, requested)  # type: ignore[arg-type]

    assert error.value.code == "REDDIT_TOKEN_SCOPE_REJECTED"
    assert error.value.exit_code == 2


def test_activation_and_publication_profiles_cannot_enroll() -> None:
    with pytest.raises(AdhelmError) as error:
        reddit_ads_scope_selection("ACTIVATE")

    assert error.value.code == "REDDIT_TOKEN_PURPOSE_UNSUPPORTED"


@pytest.mark.parametrize(
    "updates",
    [
        {"provider": "linkedin_ads"},
        {"credential_source": "EXPLICIT_CI"},
        {"purpose": "ACTIVATE"},
        {"scopes": ["adsread", "adsedit"]},
        {"scopes": ["adsconversions"]},
    ],
)
def test_only_a_read_scoped_reddit_os_store_profile_can_be_selected(
    updates: dict[str, object],
) -> None:
    configuration = Configuration(profiles=[reddit_profile(**updates)])

    with pytest.raises(AdhelmError) as error:
        select_reddit_ads_profile(configuration, PROFILE_ID)

    assert error.value.code == "REDDIT_TOKEN_PROFILE_REQUIRED"


def test_an_unknown_profile_is_refused() -> None:
    with pytest.raises(AdhelmError) as error:
        select_reddit_ads_profile(Configuration(profiles=[reddit_profile()]), "absent")

    assert error.value.code == "REDDIT_TOKEN_PROFILE_REQUIRED"


# --- enrollment -----------------------------------------------------------------


def test_enrollment_requests_a_permanent_grant_and_stores_the_secret_and_two_more_private_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])

    result = harness.enroll()

    assert result.operation == "ENROLL"
    assert result.granted_scopes == ("adsread",)
    assert result.refresh_status == "GRANTED"
    assert result.refresh_token_expires_at is None
    # The browser URL carries the permanent duration and the exact read scope.
    query = parse_qs(urlsplit(authorization_url(FakeReceiver.attempts[-1])).query)  # type: ignore[arg-type]
    assert query["duration"] == ["permanent"]
    assert query["scope"] == ["adsread"]
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"http://127.0.0.1:{REDIRECT_PORT}/oauth/callback"]
    assert "code_challenge" not in query
    # The exchange authenticates the application with its ID and the secret that was
    # prompted for; the transport turns that pair into the Basic header.
    form = harness.token.forms[0]
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == CLIENT_ID
    assert form["client_secret"] == CLIENT_SECRET
    assert harness.prompts == ["client-id", "client-secret"]
    assert harness.bearer() == ACCESS
    assert harness.slot("CLIENT_SECRET") == CLIENT_SECRET
    assert harness.slot("REFRESH_TOKEN") == REFRESH
    state = json.loads(harness.slot("STATE") or "")
    assert state["local_state"] == "READY"
    assert state["granted_scopes"] == ["adsread"]
    assert not any(secret in json.dumps(state) for secret in (ACCESS, REFRESH, CLIENT_SECRET))
    assert harness.identity_requests[0].url.path == f"/api/v3/ad_accounts/{ACCOUNT}"
    assert harness.identity_requests[0].headers["authorization"] == f"Bearer {ACCESS}"
    assert harness.identity_requests[0].headers["user-agent"] == "adhelm-local/0.1"
    harness.assert_no_lock_held_across_http()


def test_enrollment_without_a_refresh_token_records_refresh_not_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path, monkeypatch, token_responses=[initial_token_body(refresh_token=None)]
    )

    result = harness.enroll()

    assert result.refresh_status == "REFRESH_NOT_GRANTED"
    assert harness.slot("REFRESH_TOKEN") is None
    status = harness.status()
    assert status.refresh_status == "REFRESH_NOT_GRANTED"
    assert status.refresh_slot_present is False
    with pytest.raises(AdhelmError) as error:
        harness.refresh()
    assert error.value.code == "REFRESH_NOT_GRANTED"
    assert error.value.exit_code == 4
    assert harness.events.count("http:token") == 1


def test_enrollment_rejects_a_scope_selection_outside_the_read_allowlist_before_any_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])

    with pytest.raises(AdhelmError) as error:
        harness.enroll(scopes=("adsread", "adsedit"))

    assert error.value.code == "REDDIT_TOKEN_SCOPE_REJECTED"
    assert harness.prompts == []
    assert harness.events == [e for e in harness.events if not e.startswith("http:")]
    assert harness.store.set_calls == []


def test_an_edit_profile_may_request_the_write_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(scope="adsedit adsread")],
        profile=reddit_profile(purpose="EDIT", scopes=["adsread"]),
    )

    result = harness.enroll(scopes=("adsedit", "adsread"))

    assert result.granted_scopes == ("adsedit", "adsread")
    assert result.purpose == "EDIT"


def test_a_broader_grant_is_reported_but_never_recorded_for_a_read_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path, monkeypatch, token_responses=[initial_token_body(scope="adsedit adsread")]
    )

    result = harness.enroll()

    assert result.granted_scopes == ("adsread",)
    assert result.unrequested_granted_scopes == ("adsedit",)
    assert json.loads(harness.slot("STATE") or "")["granted_scopes"] == ["adsread"]


def test_a_scope_outside_the_contract_is_refused_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path, monkeypatch, token_responses=[initial_token_body(scope="adsconversions")]
    )

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "OAUTH_TOKEN_RESPONSE_UNSUPPORTED"
    assert harness.store.set_calls == []
    assert harness.identity_requests == []


def test_a_grant_narrower_than_the_request_is_refused_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(scope="adsread")],
        profile=reddit_profile(purpose="EDIT", scopes=["adsread"]),
    )

    with pytest.raises(AdhelmError) as error:
        harness.enroll(scopes=("adsedit", "adsread"))

    assert error.value.code == "OAUTH_SCOPE_NOT_GRANTED"
    assert error.value.exit_code == 4
    assert harness.store.set_calls == []
    assert harness.identity_requests == []


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


def test_a_user_denial_stops_enrollment_before_any_token_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])

    def denied(self: FakeReceiver) -> str:
        state = FakeReceiver.attempts[-1].state  # type: ignore[attr-defined]
        return f"error=access_denied&state={state}"

    monkeypatch.setattr(FakeReceiver, "receive_query", denied)

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "OAUTH_AUTHORIZATION_DENIED"
    assert harness.token.forms == []
    assert harness.store.set_calls == []


def test_a_denied_identity_read_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path, monkeypatch, token_responses=[initial_token_body()], identity_responses=[403]
    )

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "PROVIDER_ACCESS_REQUIRED"
    assert error.value.status_code == 403
    assert harness.store.set_calls == []


def test_an_unexpected_identity_account_stops_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body()],
        identity_responses=[account_response(OTHER_ACCOUNT)],
    )

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "IDENTITY_MISMATCH"
    assert harness.store.set_calls == []


def test_an_existing_slot_refuses_enrollment_before_any_prompt_or_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.store.values[(GENERIC_SERVICE, BEARER_REF)] = "pasted-by-hand"

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "REDDIT_TOKEN_CREDENTIAL_EXISTS"
    assert harness.prompts == []
    assert FakeReceiver.attempts == []
    assert harness.store.values == {(GENERIC_SERVICE, BEARER_REF): "pasted-by-hand"}


def test_a_mistyped_client_id_is_a_typed_local_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])
    monkeypatch.setattr(workflow, "_prompt_client_id", lambda: "x" * 201)

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "REDDIT_TOKEN_CLIENT_ID_INVALID"
    assert error.value.exit_code == 2
    assert harness.store.set_calls == []
    # Agents relay this verbatim, so it names no provider page and makes no claim
    # about which registration pages were checked.
    assert "prefs/apps" not in error.value.next_action
    assert "not yet checked" not in error.value.next_action
    assert "only the app ID (the client ID)" in error.value.next_action
    assert "not the secret and not ID:secret" in error.value.next_action


def test_a_colon_in_the_client_id_is_refused_before_the_browser_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])
    monkeypatch.setattr(workflow, "_prompt_client_id", lambda: "app:id")

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "REDDIT_TOKEN_CLIENT_ID_INVALID"
    assert error.value.exit_code == 2
    assert FakeReceiver.attempts == []
    assert harness.token.forms == []
    assert harness.store.set_calls == []


@pytest.mark.parametrize("port", [0, 80, 1023, 65536, True, "51234"])
def test_an_unregisterable_redirect_port_is_refused_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: object
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])

    with pytest.raises(AdhelmError) as error:
        workflow.enroll_reddit_token(
            harness.directory,
            PROFILE_ID,
            redirect_port=port,  # type: ignore[arg-type]
            interactive=True,
        )

    assert error.value.code == "REDDIT_TOKEN_REDIRECT_PORT_INVALID"
    assert harness.prompts == []


def test_enrollment_requires_an_explicit_interactive_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])
    monkeypatch.setattr(workflow, "_require_interactive", REAL_REQUIRE_INTERACTIVE)

    with pytest.raises(AdhelmError) as error:
        workflow.enroll_reddit_token(harness.directory, PROFILE_ID, redirect_port=REDIRECT_PORT)

    assert error.value.code == "EXPLICIT_BROWSER_HANDOFF_REQUIRED"
    assert harness.prompts == []


def test_a_dropped_credential_write_is_reported_unverified_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.store.drop_writes = True

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED"
    assert error.value.exit_code == 6


def test_an_existing_secret_slot_alone_refuses_enrollment_before_any_prompt_or_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    reference = reddit_token_reference(PROFILE_ID, "CLIENT_SECRET")
    harness.store.values[(REDDIT_TOKEN_SERVICE, reference)] = "left-over-secret"

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "REDDIT_TOKEN_CREDENTIAL_EXISTS"
    assert harness.prompts == []
    assert FakeReceiver.attempts == []
    assert harness.store.set_calls == []


def test_a_rejected_exchange_stores_nothing_not_even_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rejection = OAuthTransportError(
        "OAUTH_TOKEN_REJECTED",
        "The provider rejected the local credential exchange.",
        dispatch_state=OAuthTokenDispatchState.DEFINITE_RESPONSE,
    )
    harness = Harness(tmp_path, monkeypatch, token_responses=[rejection])

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "OAUTH_TOKEN_REJECTED"
    assert harness.prompts == ["client-id", "client-secret"]
    assert harness.store.values == {}
    assert harness.store.set_calls == []
    assert harness.identity_requests == []


class _Terminal:
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize("answer", ["", "has a space", "x" * 16_385])
def test_an_unusable_secret_at_the_real_prompt_stops_enrollment_before_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    monkeypatch.setattr(
        workflow,
        "_prompt_client_secret",
        lambda: prompt_hidden_secret(
            "Reddit client secret: ",
            stdin=_Terminal(),  # type: ignore[arg-type]
            stderr=_Terminal(),  # type: ignore[arg-type]
            getpass_function=lambda *_a, **_k: answer,
        ),
    )

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.code == "SECRET_VALUE_INVALID"
    assert error.value.exit_code == 2
    assert FakeReceiver.attempts == []
    assert harness.token.forms == []
    assert harness.store.set_calls == []


def test_enrollment_and_refresh_send_the_app_id_and_the_stored_secret_as_http_basic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[],
        identity_responses=[account_response(), account_response()],
    )
    requests = harness.use_real_token_transport(
        responses=[initial_token_body(), refresh_token_body()]
    )
    harness.enroll()
    # The refresh reads the secret from its slot, so a different stored value is what
    # it must send; a secret kept in memory from enrollment would be the old one.
    secret_ref = reddit_token_reference(PROFILE_ID, "CLIENT_SECRET")
    harness.store.values[(REDDIT_TOKEN_SERVICE, secret_ref)] = "reddit-fixture-replaced-secret"

    harness.refresh()

    def credentials(request: httpx.Request) -> tuple[str, str, str]:
        scheme, _, payload = request.headers["authorization"].partition(" ")
        user, _, password = base64.b64decode(payload).decode().partition(":")
        return scheme, user, password

    exchange, refresh = requests
    assert credentials(exchange) == ("Basic", CLIENT_ID, CLIENT_SECRET)
    assert credentials(refresh) == ("Basic", CLIENT_ID, "reddit-fixture-replaced-secret")
    for request in requests:
        assert request.url == "https://www.reddit.com/api/v1/access_token"
        body = parse_qs(request.content.decode())
        assert "client_id" not in body
        assert "client_secret" not in body
        assert CLIENT_SECRET not in request.content.decode()
    assert harness.bearer() == REFRESHED_ACCESS


def test_the_secret_is_kept_only_in_its_own_slot_and_never_in_local_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])

    harness.enroll()

    holders = [key for key, value in harness.store.values.items() if CLIENT_SECRET in value]
    assert holders == [(REDDIT_TOKEN_SERVICE, reddit_token_reference(PROFILE_ID, "CLIENT_SECRET"))]
    files = [path for path in harness.directory.rglob("*") if path.is_file()]
    assert files, "the configuration and any ledger live here"
    for path in files:
        assert CLIENT_SECRET.encode() not in path.read_bytes(), path


# --- refresh --------------------------------------------------------------------


def test_refresh_stores_a_new_bearer_keeps_the_refresh_token_and_holds_no_lock_over_http(
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

    assert result.operation == "REFRESH"
    form = harness.token.forms[1]
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == REFRESH
    # The secret is read back from its slot, not asked for again.
    assert form["client_secret"] == CLIENT_SECRET
    assert harness.prompts == ["client-id", "client-secret"]
    assert harness.bearer() == REFRESHED_ACCESS
    # Reddit documents no refresh-token rotation, so the permanent token stays.
    assert harness.slot("REFRESH_TOKEN") == REFRESH
    assert harness.identity_requests[1].headers["authorization"] == f"Bearer {REFRESHED_ACCESS}"
    assert harness.events.count("http:token") == 1
    assert harness.events.count("http:identity") == 1
    harness.assert_no_lock_held_across_http()
    assert harness.status().local_state == "READY"


def test_a_rotated_refresh_token_is_stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(), refresh_token_body(refresh_token=ROTATED_REFRESH)],
        identity_responses=[account_response(), account_response()],
    )
    harness.enroll()

    harness.refresh()

    assert harness.slot("REFRESH_TOKEN") == ROTATED_REFRESH


def test_a_rejected_refresh_blocks_the_profile_keeps_the_old_bearer_and_refuses_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    assert harness.bearer() == ACCESS
    assert harness.status().local_state == "REAUTHORIZATION_REQUIRED"
    posts = harness.events.count("http:token")
    with pytest.raises(AdhelmError) as retry:
        harness.refresh()
    assert retry.value.code == "REDDIT_TOKEN_REAUTHORIZATION_REQUIRED"
    assert harness.events.count("http:token") == posts


def test_a_refresh_that_never_reached_reddit_leaves_the_profile_refreshable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unsent = OAuthTransportError(
        "OAUTH_TRANSPORT_INVALID",
        "The local OAuth transport is unavailable.",
        dispatch_state=OAuthTokenDispatchState.NOT_DISPATCHED,
    )
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(), unsent, refresh_token_body()],
        identity_responses=[account_response(), account_response()],
    )
    harness.enroll()

    with pytest.raises(AdhelmError):
        harness.refresh()
    assert harness.status().local_state == "READY"

    harness.refresh()
    assert harness.bearer() == REFRESHED_ACCESS


def test_an_uncertain_refresh_outcome_blocks_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(), {"error": "invalid_grant"}],
    )
    harness.enroll()

    with pytest.raises(AdhelmError) as error:
        harness.refresh()

    assert error.value.exit_code == 4
    assert harness.status().local_state == "REAUTHORIZATION_REQUIRED"


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
    state = json.loads(harness.slot("STATE") or "")
    state["identity_pseudonym"] = lifecycle.reddit_account_pseudonym(
        lifecycle.new_identity_salt(), ACCOUNT
    )
    state_ref = reddit_token_reference(PROFILE_ID, "STATE")
    harness.store.values[(REDDIT_TOKEN_SERVICE, state_ref)] = json.dumps(
        state, separators=(",", ":"), sort_keys=True
    )

    with pytest.raises(AdhelmError) as error:
        harness.refresh()

    assert error.value.code == "REDDIT_TOKEN_IDENTITY_MISMATCH"
    assert error.value.exit_code == 4
    assert harness.bearer() == ACCESS
    assert harness.status().local_state == "REAUTHORIZATION_REQUIRED"


def test_a_refresh_that_narrows_the_enrolled_scope_set_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[
            initial_token_body(scope="adsedit adsread"),
            refresh_token_body(scope="adsread"),
        ],
        identity_responses=[account_response(), account_response()],
        profile=reddit_profile(purpose="EDIT", scopes=["adsread"]),
    )
    harness.enroll(scopes=("adsedit", "adsread"))

    with pytest.raises(AdhelmError) as error:
        harness.refresh()

    assert error.value.code == "REDDIT_TOKEN_SCOPE_REDUCED"
    assert harness.bearer() == ACCESS


def test_a_refresh_never_widens_the_recorded_scope_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(), refresh_token_body(scope="adsedit adsread")],
        identity_responses=[account_response(), account_response()],
    )
    harness.enroll()

    result = harness.refresh()

    assert result.granted_scopes == ("adsread",)
    assert result.unrequested_granted_scopes == ("adsedit",)
    assert json.loads(harness.slot("STATE") or "")["granted_scopes"] == ["adsread"]


def test_refresh_without_an_enrollment_requires_authorization_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])

    with pytest.raises(AdhelmError) as error:
        harness.refresh()

    assert error.value.code == "REDDIT_TOKEN_ENROLLMENT_REQUIRED"
    assert error.value.exit_code == 4
    assert harness.events.count("http:token") == 0


def test_refresh_without_the_secret_slot_requires_reenrollment_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.enroll()
    del harness.store.values[
        (REDDIT_TOKEN_SERVICE, reddit_token_reference(PROFILE_ID, "CLIENT_SECRET"))
    ]

    with pytest.raises(AdhelmError) as error:
        harness.refresh()

    assert error.value.code == "REDDIT_TOKEN_ENROLLMENT_REQUIRED"
    assert harness.events.count("http:token") == 1
    assert harness.status().local_state == "READY"


def test_refresh_with_an_unusable_stored_secret_is_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.enroll()
    harness.store.values[
        (REDDIT_TOKEN_SERVICE, reddit_token_reference(PROFILE_ID, "CLIENT_SECRET"))
    ] = "not a usable secret"

    with pytest.raises(AdhelmError) as error:
        harness.refresh()

    assert error.value.code == "REDDIT_TOKEN_STATE_INVALID"
    assert harness.events.count("http:token") == 1


def test_a_refresh_expiry_recorded_by_the_provider_is_enforced_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(refresh_token_expires_in=3600)],
    )
    harness.enroll()
    assert harness.status().refresh_token_expires_at is not None
    monkeypatch.setattr(lifecycle, "_utc_now", lambda: datetime.now(UTC) + timedelta(hours=2))

    with pytest.raises(AdhelmError) as error:
        harness.refresh()

    assert error.value.code == "REDDIT_TOKEN_REFRESH_EXPIRED"
    assert harness.events.count("http:token") == 1


# --- status and removal ---------------------------------------------------------


def test_status_is_local_only_and_reports_expiry_without_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.enroll()
    requests = len(harness.identity_requests)

    fresh = harness.status()
    monkeypatch.setattr(lifecycle, "_utc_now", lambda: datetime.now(UTC) + timedelta(hours=2))
    expired = harness.status()

    assert fresh.local_state == "READY"
    assert fresh.access_token_expired is False
    assert fresh.bearer_slot_present is True
    assert fresh.refresh_slot_present is True
    assert fresh.identity_pseudonym is not None
    assert fresh.identity_pseudonym.startswith("reddit_ads_account_v1.")
    assert ACCOUNT not in fresh.identity_pseudonym
    assert expired.access_token_expired is True
    assert expired.local_state == "READY"
    assert len(harness.identity_requests) == requests
    assert harness.events.count("http:token") == 1


def test_status_for_an_unenrolled_profile_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])

    status = harness.status()

    assert status.local_state == "MISSING"
    assert status.granted_scopes == ()
    assert status.identity_pseudonym is None


def test_status_never_reports_a_missing_bearer_as_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.enroll()
    del harness.store.values[(GENERIC_SERVICE, BEARER_REF)]

    status = harness.status()

    assert status.local_state == "UNVERIFIED"
    assert status.bearer_slot_present is False


def test_corrupt_state_is_unverified_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.enroll()
    state_ref = reddit_token_reference(PROFILE_ID, "STATE")
    harness.store.values[(REDDIT_TOKEN_SERVICE, state_ref)] = "{not json"

    assert harness.status().local_state == "UNVERIFIED"
    with pytest.raises(AdhelmError) as error:
        harness.refresh()
    assert error.value.code == "REDDIT_TOKEN_STATE_INVALID"


def test_recorded_state_outside_the_purpose_allowlist_is_never_reported_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.enroll()
    state = json.loads(harness.slot("STATE") or "")
    state["granted_scopes"] = ["adsedit", "adsread"]
    state_ref = reddit_token_reference(PROFILE_ID, "STATE")
    harness.store.values[(REDDIT_TOKEN_SERVICE, state_ref)] = json.dumps(
        state, separators=(",", ":"), sort_keys=True
    )

    assert harness.status().local_state == "UNVERIFIED"


def test_state_is_bound_to_the_exact_profile_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.enroll()
    save_configuration(
        harness.directory,
        Configuration(
            profiles=[reddit_profile(timezone="America/Chicago")],
            output=OutputPreferences(max_output_bytes=8192),
        ),
        replace=True,
    )

    with pytest.raises(AdhelmError) as error:
        harness.refresh()

    assert error.value.code == "REDDIT_TOKEN_PROFILE_CHANGED"
    assert error.value.exit_code == 4
    assert harness.events.count("http:token") == 1


def test_local_removal_deletes_only_the_three_private_slots_including_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.enroll()
    assert harness.slot("CLIENT_SECRET") == CLIENT_SECRET

    digest, removed = lifecycle.remove_local_reddit_token_slots(harness.directory, PROFILE_ID)
    _, again = lifecycle.remove_local_reddit_token_slots(harness.directory, PROFILE_ID)

    assert len(digest) == 64
    assert removed == 3
    assert again == 0
    assert harness.slot("CLIENT_SECRET") is None
    assert harness.slot("REFRESH_TOKEN") is None
    assert harness.slot("STATE") is None
    assert CLIENT_SECRET not in harness.store.values.values()
    # The bearer belongs to the shared `auth remove-local` contract.
    assert harness.bearer() == ACCESS
    assert harness.status().local_state == "MISSING"


def test_lifecycle_secret_bearing_objects_cannot_be_serialized_or_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.enroll()
    context = lifecycle.begin_reddit_token_refresh(harness.directory, PROFILE_ID)

    assert REFRESH not in repr(context)
    with pytest.raises(TypeError):
        context.__reduce__()
    with pytest.raises(TypeError):
        context.__getstate__()


def test_the_account_pseudonym_is_salted_and_reveals_no_account_id() -> None:
    first = lifecycle.reddit_account_pseudonym(lifecycle.new_identity_salt(), ACCOUNT)
    second = lifecycle.reddit_account_pseudonym(lifecycle.new_identity_salt(), ACCOUNT)

    assert first != second
    assert ACCOUNT not in first
    with pytest.raises(AdhelmError):
        lifecycle.reddit_account_pseudonym(b"short", ACCOUNT)
    with pytest.raises(AdhelmError):
        lifecycle.reddit_account_pseudonym(lifecycle.new_identity_salt(), "../escape")


# --- shipped CLI ----------------------------------------------------------------


def invoke(harness: Harness, *arguments: str) -> object:
    return CliRunner().invoke(
        app, ["--data-dir", str(harness.directory), "auth", "reddit-token", *arguments]
    )


def test_the_cli_group_is_reachable_and_never_echoes_secret_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(), refresh_token_body()],
        identity_responses=[account_response(), account_response()],
    )

    enrolled = invoke(
        harness,
        "enroll",
        "--profile",
        PROFILE_ID,
        "--redirect-port",
        str(REDIRECT_PORT),
        "--interactive",
    )
    refreshed = invoke(harness, "refresh", "--profile", PROFILE_ID)
    status = invoke(harness, "status", "--profile", PROFILE_ID)

    for result in (enrolled, refreshed, status):
        assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    combined = "".join(result.output for result in (enrolled, refreshed, status))  # type: ignore[attr-defined]
    for secret in SECRETS:
        assert secret not in combined
    assert ACCOUNT not in combined
    assert BEARER_REF not in combined
    data = json.loads(status.output)["data"]  # type: ignore[attr-defined]
    assert data["local_state"] == "READY"
    assert data["granted_scopes"] == ["adsread"]
    assert data["identity_pseudonym"].startswith("reddit_ads_account_v1.")
    assert data["provider_authentication_verified"] is False
    assert json.loads(enrolled.output)["data"]["remote_mutations"] is False  # type: ignore[attr-defined]


def test_the_cli_reports_exit_code_four_for_a_refresh_that_was_not_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(
        tmp_path, monkeypatch, token_responses=[initial_token_body(refresh_token=None)]
    )
    harness.enroll()

    result = invoke(harness, "refresh", "--profile", PROFILE_ID)

    assert result.exit_code == 4  # type: ignore[attr-defined]
    payload = json.loads(result.output)  # type: ignore[attr-defined]
    assert payload["outcome"] == "FAILED_CLOSED"
    assert payload["diagnostics"][0]["code"] == "REFRESH_NOT_GRANTED"


def test_the_cli_enrollment_requires_the_interactive_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])
    monkeypatch.setattr(workflow, "_require_interactive", REAL_REQUIRE_INTERACTIVE)

    result = invoke(
        harness, "enroll", "--profile", PROFILE_ID, "--redirect-port", str(REDIRECT_PORT)
    )

    assert result.exit_code == 4  # type: ignore[attr-defined]
    assert json.loads(result.output)["diagnostics"][0]["code"] == (  # type: ignore[attr-defined]
        "EXPLICIT_BROWSER_HANDOFF_REQUIRED"
    )
    assert harness.prompts == []


def test_cli_local_removal_is_never_described_as_provider_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.enroll()

    removed = invoke(harness, "remove-local", "--profile", PROFILE_ID)
    status = invoke(harness, "status", "--profile", PROFILE_ID)

    assert removed.exit_code == 0, removed.output  # type: ignore[attr-defined]
    data = json.loads(removed.output)["data"]  # type: ignore[attr-defined]
    assert data["operation"] == "LOCAL_REMOVE_ONLY"
    assert data["removed_slot_count"] == 3
    assert CLIENT_SECRET not in removed.output  # type: ignore[attr-defined]
    assert data["remote_revocation_verified"] is False
    guidance = " ".join(data["revocation_guidance"])
    assert "application secret" in guidance
    # The provider-side step is unchecked guidance: it must say so, and it must
    # not assert a Reddit page as fact (agents relay these strings verbatim).
    assert "not yet checked against Reddit's current pages" in guidance
    assert "prefs/apps" not in guidance
    assert "never revokes" in guidance
    status_guidance = " ".join(
        json.loads(status.output)["data"]["revocation_guidance"]  # type: ignore[attr-defined]
    )
    assert "not yet checked against Reddit's current pages" in status_guidance
    assert "prefs/apps" not in status_guidance
    assert json.loads(status.output)["data"]["local_state"] == "MISSING"  # type: ignore[attr-defined]


def test_every_reddit_token_leaf_is_discoverable() -> None:
    result = CliRunner().invoke(app, ["discover"])

    assert result.exit_code == 0, result.output
    commands = set(json.loads(result.output)["data"]["commands"])
    assert {
        "auth reddit-token enroll",
        "auth reddit-token refresh",
        "auth reddit-token status",
        "auth reddit-token remove-local",
    } <= commands


def test_the_status_leaf_refuses_an_unconfigured_profile_with_nothing_in_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, monkeypatch, token_responses=[])

    result = invoke(harness, "status", "--profile", "absent")

    assert result.exit_code == 2  # type: ignore[attr-defined]
    assert json.loads(result.output)["diagnostics"][0]["code"] == (  # type: ignore[attr-defined]
        "REDDIT_TOKEN_PROFILE_REQUIRED"
    )
    assert harness.store.values == {}


@pytest.fixture(autouse=True)
def _no_real_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    # Harness installs its own recording lock; this keeps a test that builds no
    # Harness away from the real credential-lock directory.
    monkeypatch.setattr(config_module, "credential_mutation_lock", lambda _r: nullcontext())
