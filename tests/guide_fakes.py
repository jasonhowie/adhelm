"""Fake providers for the setup-guide harness, one per provider with a guide.

Everything a setup guide touches outside the process is replaced here and nowhere
else: the operating-system credential store (a dict), the system browser (a seam that
records what the command asked for and grants it), the loopback listener that would
receive the browser's redirect, the hidden secret prompt (a queue of answers) and the
provider's HTTP APIs (`httpx.MockTransport` in front of the same canned API responses
the pilot and OAuth suites use). Nothing reaches a provider, the real credential store
or owner data. `FakeProvider` holds what every fake shares; each subclass adds the
provider's own authentication.

The fakes are strict on purpose. A Google fake issues each access token for the one
scope the command requested and answers `401` or `403` to a request that does not carry
it; the OAuth fakes check the client, redirect and authorization code at the token
endpoint the way the provider documents it; the key and token fakes answer `401` to a
request that does not carry the credential the guide stored. So a guide whose draft
names the wrong scope, profile or credential fails its own `inventory`. Each fake
records, rather than answers, any request that a read-only guide must never make: a
host outside the provider's API, a method other than `GET`, or a `POST` that is not one
of the provider's documented read-only queries or its token endpoint.

One check is stricter than Google on purpose. Every Google fake records, and refuses,
a request that carries a `developer-token` header. Google has ignored that header since
it retired developer tokens on 2026-09-09 and will reject it in a future major version
(Developer token page, fetched 2026-10-12), so the refusal is a fake-only check, not
Google behavior. It proves AdHelm never sends one, and a Google Ads guide never has the
reader enter one: the fake's hidden prompt has no answer to give.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import ModuleType
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest
from test_gtm_auth_lifecycle import _gtm_handler
from test_pilot_run import _ga4_handler
from test_pilot_run_providers import (
    _google_handler,
    _linkedin_handler,
    _openai_handler,
    _tiktok_handler,
)
from test_reddit_audit_route import _Provider as _RedditHandler

from adhelm import (
    credential_setup,
    credentials,
    linkedin_token_workflow,
    meta_token_cli,
    reddit_token_workflow,
    tiktok_token_workflow,
)
from adhelm import google_oauth_workflow as enrollment
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import (
    GOOGLE_OIDC_CANONICAL_ISSUER,
    GoogleOAuthInitialGrant,
    GoogleOAuthTarget,
    VerifiedGoogleOidcIdentity,
    derive_google_oidc_principal_ref,
)
from adhelm.google_oauth_setup import GoogleInstalledClient
from adhelm.oauth_enrollment import OAuthAuthorizationAttempt, authorization_url

#: The client the guides name. The downloaded file below is for this client.
CLIENT_ID = "123456789012-exampleexampleexample.apps.googleusercontent.com"
CLIENT_SECRET = "fixture-client-secret-value"
REFRESH_TOKEN = "fixture-refresh-token-value"
GOOGLE_SUBJECT = "fixture-google-subject"

ADWORDS = "https://www.googleapis.com/auth/adwords"
ANALYTICS_READONLY = "https://www.googleapis.com/auth/analytics.readonly"
TAGMANAGER_READONLY = "https://www.googleapis.com/auth/tagmanager.readonly"

#: The file Google's console offers to download for a desktop client, with the fields a
#: real download carries. The secret is a fixture value.
CLIENT_FILE = json.dumps(
    {
        "installed": {
            "client_id": CLIENT_ID,
            "project_id": "adhelm-example-project",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": CLIENT_SECRET,
            "redirect_uris": ["http://localhost"],
        }
    },
    indent=2,
)

#: Values the guides for the other providers have the reader enter. All are fixtures.
LINKEDIN_CLIENT_ID = "86fixtureclientid"
LINKEDIN_CLIENT_SECRET = "fixture-linkedin-client-secret"
LINKEDIN_REDIRECT_URI = "http://127.0.0.1:8765/oauth/callback"
REDDIT_CLIENT_ID = "fixtureRedditAppId"
REDDIT_CLIENT_SECRET = "fixtureRedditAppSecret"
REDDIT_REDIRECT_URI = "http://127.0.0.1:8765/oauth/callback"
TIKTOK_APP_ID = "7000000000000000001"
TIKTOK_APP_SECRET = "fixture-tiktok-app-secret"
TIKTOK_AUTH_CODE = "fixture-tiktok-auth-code"
META_APP_ID = "987654321012345"
META_USER_TOKEN = "fixture-meta-user-token"
META_INSPECTOR_TOKEN = "fixture-meta-inspector-token"
OPENAI_ADS_KEY = "fixture-openai-ads-key"

Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class _MemoryStore:
    """The credential store: three methods and a dict, as the keyring backends offer."""

    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


@dataclass(frozen=True)
class _Api:
    """One provider API host: its canned handler and its read-only POSTs.

    `scope` is the one OAuth scope a Google API needs; the other providers leave it unset.
    """

    handler: Handler
    scope: str | None = None
    read_only_posts: tuple[str, ...] = ()


@dataclass
class FakeProvider:
    """The parts every fake shares: the store, the prompt, the HTTP boundary and the log."""

    apis: Mapping[str, _Api]
    #: Answers to the hidden secret prompt, in the order a guide asks for them.
    prompt_answers: list[str] = field(default_factory=list)
    #: Files the reader gets from the provider, written to the working directory.
    fixtures: Mapping[str, str] = field(default_factory=dict)
    store: _MemoryStore = field(default_factory=_MemoryStore)
    requests: list[httpx.Request] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    _prompted: list[str] = field(default_factory=list)

    # --- the harness contract ---------------------------------------------------------

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        class MockedClient(httpx.Client):
            """Every client the commands build talks to `serve`, never to a socket."""

            def __init__(self, **kwargs: object) -> None:
                super().__init__(transport=httpx.MockTransport(fake.serve), **kwargs)

        monkeypatch.setattr(credential_setup, "os_store_backend", lambda: self.store)
        monkeypatch.setattr(credentials, "_backend", lambda: self.store)
        monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", self._prompt)
        # A subclass, not a function: the token transports check `type(client) is
        # httpx.Client`, which holds for a client built from the patched name.
        monkeypatch.setattr(httpx, "Client", MockedClient)
        self._install_seams(monkeypatch)

    def secrets(self) -> tuple[str, ...]:
        """Every credential this fake has handed out; none may reach output or disk."""
        return (*self._prompted, *self._issued())

    def problems(self) -> list[str]:
        return list(self.violations)

    def _allow_interactive(
        self, monkeypatch: pytest.MonkeyPatch, module: ModuleType, name: str
    ) -> None:
        """Bypass only the terminal check of `module.name`, keeping AdHelm's own refusal.

        A guide that leaves out `--interactive` still meets AdHelm's explicit-handoff
        refusal, as it would for a reader; only the check that stdin is a terminal is
        replaced, because the harness has none.
        """
        real = getattr(module, name)
        monkeypatch.setattr(
            module, name, lambda interactive: None if interactive is True else real(interactive)
        )

    # --- what a subclass supplies -----------------------------------------------------

    def _install_seams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace the places this provider's commands would leave the process."""

    def _issued(self) -> tuple[str, ...]:
        """Credentials the provider issued during the guide."""
        return ()

    def _token_endpoint(self, request: httpx.Request) -> httpx.Response | None:
        """Answer the provider's OAuth token endpoint; `None` if this is not a token request."""
        return None

    def _authenticate(self, api: _Api, request: httpx.Request) -> httpx.Response | None:
        """Refuse a request that lacks what the provider requires; `None` to accept it."""
        return None

    # --- the places a command would leave the process --------------------------------

    def _prompt(self, *_: object, **__: object) -> str:
        if not self.prompt_answers:
            raise AdhelmError(
                "FAKE_PROMPT_EXHAUSTED", "The guide prompted for an unexpected secret."
            )
        answer = self.prompt_answers.pop(0)
        self._prompted.append(answer)
        return answer

    def _refuse(self, reason: str) -> AdhelmError:
        """Record that the provider would have refused this step, and stop the command."""
        self.violations.append(reason)
        return AdhelmError("FAKE_PROVIDER_REFUSED", "The fake provider refused this step.")

    def serve(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        token = self._token_endpoint(request)
        if token is not None:
            return token
        api = self.apis.get(request.url.host)
        if api is None:
            self.violations.append(f"a request to {request.url.host}, which is not this API")
            return httpx.Response(403, json={"error": "unexpected host"})
        if request.method == "POST":
            if not request.url.path.endswith(api.read_only_posts):
                self.violations.append(f"POST {request.url.host}{request.url.path}")
                return httpx.Response(403, json={"error": "not a read-only query"})
        elif request.method != "GET":
            self.violations.append(f"{request.method} {request.url.host}{request.url.path}")
            return httpx.Response(403, json={"error": "not a read"})
        denied = self._authenticate(api, request)
        if denied is not None:
            return denied
        return api.handler(request)


def _bearer(request: httpx.Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token if scheme == "Bearer" else ""


# --- Google ------------------------------------------------------------------------------


@dataclass
class FakeGoogle(FakeProvider):
    """A Google Cloud project, a signed-in user, and the APIs one guide reads."""

    fixtures: Mapping[str, str] = field(default_factory=lambda: {"client_secret.json": CLIENT_FILE})
    #: Scopes the user was asked to consent to, one entry per browser handoff.
    consents: list[str] = field(default_factory=list)
    _grants: dict[str, str] = field(default_factory=dict)

    def _install_seams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(enrollment, "_browser_grant", self._browser_grant)
        self._allow_interactive(monkeypatch, enrollment, "_require_interactive")

    def _issued(self) -> tuple[str, ...]:
        return (CLIENT_SECRET, REFRESH_TOKEN, GOOGLE_SUBJECT, *self._grants)

    def _browser_grant(
        self, target: GoogleOAuthTarget, client: GoogleInstalledClient
    ) -> GoogleOAuthInitialGrant:
        if client.client_secret != CLIENT_SECRET:
            raise AdhelmError("FAKE_GOOGLE_CLIENT_REJECTED", "The fake rejects this client.")
        self.consents.append(target.api_scope)
        token = f"fixture-access-token-{len(self._grants) + 1}"
        self._grants[token] = target.api_scope
        return GoogleOAuthInitialGrant(
            target=target,
            access_token=token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            refresh_token=REFRESH_TOKEN,
            refresh_token_expires_at=None,
            requested_scopes=("openid", target.api_scope),
            returned_scopes=("openid", target.api_scope),
            identity=VerifiedGoogleOidcIdentity(
                canonical_issuer=GOOGLE_OIDC_CANONICAL_ISSUER,
                raw_subject=GOOGLE_SUBJECT,
                principal_ref=derive_google_oidc_principal_ref(
                    GOOGLE_OIDC_CANONICAL_ISSUER, GOOGLE_SUBJECT
                ),
                client_ref=target.client_ref,
            ),
        )

    def _authenticate(self, api: _Api, request: httpx.Request) -> httpx.Response | None:
        token = _bearer(request)
        if token not in self._grants:
            return httpx.Response(401, json={"error": "no valid bearer token"})
        if self._grants[token] != api.scope:
            return httpx.Response(403, json={"error": "insufficient authentication scopes"})
        # Fake-only check, stricter than Google (which ignores the header since
        # 2026-09-09): AdHelm must never send a developer token.
        if "developer-token" in request.headers:
            self.violations.append(
                f"a developer-token header to {request.url.host}{request.url.path}"
            )
            return httpx.Response(
                400, json={"error": "fake-only check: no developer-token header is expected"}
            )
        return None


def google_ads() -> FakeGoogle:
    """Google Ads read API for customer 1234567890; a guide stores no second credential."""
    return FakeGoogle(
        apis={
            "googleads.googleapis.com": _Api(_google_handler, ADWORDS, (":search", ":searchStream"))
        }
    )


def ga4() -> FakeGoogle:
    """Analytics Data and Admin APIs for property 123, web stream 456."""
    return FakeGoogle(
        apis={
            "analyticsdata.googleapis.com": _Api(_ga4_handler, ANALYTICS_READONLY, (":runReport",)),
            "analyticsadmin.googleapis.com": _Api(_ga4_handler, ANALYTICS_READONLY),
        }
    )


def gtm() -> FakeGoogle:
    """Tag Manager API for account 12345, web container 2."""
    return FakeGoogle(
        apis={
            "tagmanager.googleapis.com": _Api(_gtm_handler([]), TAGMANAGER_READONLY),
        }
    )


# --- providers whose enrollment is a browser authorization code on a fixed loopback port ---


class _LoopbackReceiver:
    """Stands in for the listener on the registered port; it binds nothing."""

    def __init__(self, fake: FakeLoopbackOAuth, *, port: int, path: str) -> None:
        self._fake = fake
        self.redirect_uri = f"http://127.0.0.1:{port}{path}"

    def __enter__(self) -> _LoopbackReceiver:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def receive_query(self) -> str:
        """What the browser's redirect would carry back after the user approves."""
        return self._fake.approve()

    def require_unexpired(self) -> None:
        return None


@dataclass(kw_only=True)
class FakeLoopbackOAuth(FakeProvider):
    """An OAuth 2.0 authorization-code provider that redirects to a registered loopback URL.

    It checks what the provider would: the authorization request names the entered client,
    the registered redirect and a state, and the token endpoint accepts a code only once,
    from the client that asked, for the redirect that was used.
    """

    workflow: ModuleType
    #: Names of the workflow's hidden-prompt functions, in the order they are asked.
    prompts: tuple[str, ...]
    authorize_url: str
    token_url: str
    client_id: str
    redirect_uri: str
    #: Fields every token response carries (lifetime, scope echo, token type).
    token_fields: Mapping[str, object]
    #: Whether the initial grant carries a refresh token.
    refresh_token: str | None = None
    #: The query of each authorization request the user was sent to.
    authorizations: list[dict[str, str]] = field(default_factory=list)
    #: The form of each token request, with credentials left out.
    token_requests: list[dict[str, str]] = field(default_factory=list)
    _state: str = ""
    _codes: set[str] = field(default_factory=set)
    _access: set[str] = field(default_factory=set)

    def _install_seams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        workflow = self.workflow
        self._allow_interactive(monkeypatch, workflow, "_require_interactive")
        monkeypatch.setattr(workflow, "open_system_browser", self._browser)
        monkeypatch.setattr(
            workflow,
            "LoopbackRedirectReceiver",
            lambda *, port, path, **_: _LoopbackReceiver(self, port=port, path=path),
        )
        for name in self.prompts:
            monkeypatch.setattr(workflow, name, self._prompt)

    def _issued(self) -> tuple[str, ...]:
        return (*self._codes, *self._access, *([self.refresh_token] if self.refresh_token else []))

    def _browser(self, attempt: OAuthAuthorizationAttempt) -> None:
        parts = urlsplit(authorization_url(attempt))
        query = {name: values[0] for name, values in parse_qs(parts.query).items()}
        if f"{parts.scheme}://{parts.netloc}{parts.path}" != self.authorize_url:
            raise self._refuse(f"an authorization request to {parts.netloc}{parts.path}")
        if query.get("response_type") != "code" or not query.get("state"):
            raise self._refuse("an authorization request without response_type=code and a state")
        if query.get("client_id") != self.client_id:
            raise self._refuse("an authorization request for a client the user did not enter")
        if query.get("redirect_uri") != self.redirect_uri:
            raise self._refuse("an authorization request for a redirect that is not registered")
        self.authorizations.append(query)
        self._state = query["state"]

    def approve(self) -> str:
        """The user approves: the provider redirects with a one-time code and the state."""
        code = f"fixture-authorization-code-{len(self._codes) + 1}"
        self._codes.add(code)
        return f"code={code}&state={self._state}"

    def _client_problem(self, request: httpx.Request, form: dict[str, str]) -> str | None:
        """How the token request fails to authenticate the client, or `None`."""
        raise NotImplementedError

    def _token_endpoint(self, request: httpx.Request) -> httpx.Response | None:
        url = request.url
        if f"{url.scheme}://{url.host}{url.path}" != self.token_url:
            return None
        if request.method != "POST":
            self.violations.append(f"{request.method} {url.host}{url.path}")
            return httpx.Response(405)
        form = dict(parse_qsl(request.content.decode()))
        problem = self._client_problem(request, form)
        grant = form.get("grant_type")
        body: dict[str, object] = dict(self.token_fields)
        if problem is None and grant == "authorization_code":
            if form.get("redirect_uri") != self.redirect_uri:
                problem = "the token request names a redirect that was not used"
            elif form.get("code") not in self._codes:
                problem = "the token request carries a code the provider did not issue"
            else:
                self._codes.discard(form["code"])
                if self.refresh_token is not None:
                    body["refresh_token"] = self.refresh_token
        elif problem is None and grant == "refresh_token" and self.refresh_token is not None:
            if form.get("refresh_token") != self.refresh_token:
                problem = "the refresh request carries a token the provider did not issue"
        elif problem is None:
            problem = f"a token request with grant_type={grant}"
        if problem is not None:
            self.violations.append(problem)
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.token_requests.append(
            {k: v for k, v in form.items() if k not in {"code", "client_secret", "refresh_token"}}
        )
        token = f"fixture-access-token-{len(self._access) + 1}"
        self._access.add(token)
        body["access_token"] = token
        return httpx.Response(200, json=body)

    def _authenticate(self, api: _Api, request: httpx.Request) -> httpx.Response | None:
        if _bearer(request) not in self._access:
            return httpx.Response(401, json={"error": "no valid bearer token"})
        return None


@dataclass(kw_only=True)
class FakeLinkedIn(FakeLoopbackOAuth):
    """A confidential client: the form carries the client secret."""

    client_secret: str

    def _issued(self) -> tuple[str, ...]:
        return (*super()._issued(), self.client_secret)

    def _client_problem(self, request: httpx.Request, form: dict[str, str]) -> str | None:
        if (
            form.get("client_id") != self.client_id
            or form.get("client_secret") != self.client_secret
        ):
            return "the token request does not authenticate the client the user entered"
        return None


@dataclass(kw_only=True)
class FakeReddit(FakeLoopbackOAuth):
    """A confidential client: HTTP Basic with the app ID as user name and the secret as password."""

    client_secret: str

    def _issued(self) -> tuple[str, ...]:
        return (*super()._issued(), self.client_secret)

    def _client_problem(self, request: httpx.Request, form: dict[str, str]) -> str | None:
        scheme, _, payload = request.headers.get("authorization", "").partition(" ")
        try:
            decoded = base64.b64decode(payload, validate=True).decode()
        except ValueError:
            decoded = ""
        user, separator, password = decoded.partition(":")
        if scheme != "Basic" or not separator:
            return "the token request does not authenticate the client with HTTP Basic"
        if not password:
            return "the token request authenticates with an empty client secret"
        if (user, password) != (self.client_id, self.client_secret):
            return "the token request does not authenticate the app the user entered"
        if not request.headers.get("user-agent"):
            return "the token request has no User-Agent, which Reddit rejects"
        return None


def linkedin_ads() -> FakeLinkedIn:
    """LinkedIn OAuth and Marketing API for ad account 456, without programmatic refresh."""
    return FakeLinkedIn(
        apis={"api.linkedin.com": _Api(_linkedin_handler)},
        prompt_answers=[LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET],
        workflow=linkedin_token_workflow,
        prompts=("_prompt_client_id", "_prompt_client_secret"),
        authorize_url="https://www.linkedin.com/oauth/v2/authorization",
        token_url="https://www.linkedin.com/oauth/v2/accessToken",
        client_id=LINKEDIN_CLIENT_ID,
        client_secret=LINKEDIN_CLIENT_SECRET,
        redirect_uri=LINKEDIN_REDIRECT_URI,
        token_fields={"expires_in": 5_184_000, "scope": "r_ads,r_ads_reporting"},
    )


def reddit_ads() -> FakeReddit:
    """Reddit OAuth and Ads API for ad account a2_fixture, with a permanent refresh token."""
    return FakeReddit(
        apis={"ads-api.reddit.com": _Api(_RedditHandler(), read_only_posts=("/reports",))},
        prompt_answers=[REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET],
        workflow=reddit_token_workflow,
        prompts=("_prompt_client_id", "_prompt_client_secret"),
        authorize_url="https://www.reddit.com/api/v1/authorize",
        token_url="https://www.reddit.com/api/v1/access_token",
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        redirect_uri=REDDIT_REDIRECT_URI,
        token_fields={"token_type": "bearer", "expires_in": 3600, "scope": "adsread"},
        refresh_token="fixture-reddit-refresh-token",
    )


# --- TikTok: an authorization code the user pastes, exchanged for a long-term token --------

TIKTOK_TOKEN_URL = "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"


@dataclass(kw_only=True)
class FakeTikTok(FakeProvider):
    """TikTok API for Business: one JSON token exchange, then `Access-Token` reads."""

    #: The body of the one token request, without the secret and the code.
    token_requests: list[dict[str, str]] = field(default_factory=list)
    _access: set[str] = field(default_factory=set)
    _code_used: bool = False

    def _install_seams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        workflow = tiktok_token_workflow
        self._allow_interactive(monkeypatch, workflow, "_require_interactive")
        for name in ("_prompt_app_id", "_prompt_app_secret", "_prompt_auth_code"):
            monkeypatch.setattr(workflow, name, self._prompt)

    def _issued(self) -> tuple[str, ...]:
        return (TIKTOK_APP_SECRET, TIKTOK_AUTH_CODE, *self._access)

    def _token_endpoint(self, request: httpx.Request) -> httpx.Response | None:
        url = request.url
        if f"{url.scheme}://{url.host}{url.path}" != TIKTOK_TOKEN_URL:
            return None
        problem = None
        body: dict[str, str] = {}
        if request.method != "POST" or request.headers.get("content-type") != "application/json":
            problem = "a token request that is not a JSON POST"
        else:
            body = json.loads(request.content)
            if set(body) != {"app_id", "auth_code", "secret"}:
                problem = "a token request whose body is not exactly app_id, auth_code and secret"
            elif body["app_id"] != TIKTOK_APP_ID or body["secret"] != TIKTOK_APP_SECRET:
                problem = "a token request that does not name the app the user entered"
            elif body["auth_code"] != TIKTOK_AUTH_CODE or self._code_used:
                problem = "a token request with an authorization code that is wrong or used"
        if problem is not None:
            self.violations.append(problem)
            return httpx.Response(400, json={"code": 40001, "message": "invalid auth_code"})
        self._code_used = True
        self.token_requests.append({"app_id": body["app_id"]})
        token = f"fixture-tiktok-access-token-{len(self._access) + 1}"
        self._access.add(token)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "OK",
                "request_id": "2026101112000000000000000000000",
                "data": {"access_token": token, "scope": [1, 2, 4], "advertiser_ids": ["123"]},
            },
        )

    def _authenticate(self, api: _Api, request: httpx.Request) -> httpx.Response | None:
        if request.headers.get("access-token") not in self._access:
            return httpx.Response(401, json={"code": 40105, "message": "invalid access token"})
        return None


def tiktok_ads() -> FakeTikTok:
    """TikTok API for Business advertiser 123, authorized with a one-hour code."""
    return FakeTikTok(
        apis={"business-api.tiktok.com": _Api(_tiktok_handler)},
        prompt_answers=[TIKTOK_APP_ID, TIKTOK_APP_SECRET, TIKTOK_AUTH_CODE],
    )


# --- Meta: two tokens the user generates, one to read with and one to inspect it ----------------


@dataclass(kw_only=True)
class FakeMeta(FakeProvider):
    """Graph API: reads with the user token, `debug_token` with an app or developer token."""

    def _install_seams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._allow_interactive(monkeypatch, meta_token_cli, "_require_interactive_terminal")

    def _issued(self) -> tuple[str, ...]:
        return (META_USER_TOKEN, META_INSPECTOR_TOKEN)

    def _authenticate(self, api: _Api, request: httpx.Request) -> httpx.Response | None:
        params = request.url.params
        if request.url.path.endswith("/debug_token"):
            if _bearer(request) != META_INSPECTOR_TOKEN:
                return httpx.Response(401, json={"error": {"code": 190}})
            if params.get("input_token") != META_USER_TOKEN:
                return httpx.Response(400, json={"error": {"code": 100}})
            return None
        if "access_token" in params or "input_token" in params:
            self.violations.append("a token in the query string of a read")
            return httpx.Response(400, json={"error": {"code": 100}})
        if _bearer(request) != META_USER_TOKEN:
            return httpx.Response(401, json={"error": {"code": 190}})
        return None


def _meta_handler(request: httpx.Request) -> httpx.Response:
    """Graph API for ad account 1234567890. Expiries are relative to now, as a real token's are."""
    path = request.url.path
    if path == "/v26.0/debug_token":
        now = datetime.now(UTC)
        data: dict[str, object] = {
            "data": {
                "app_id": META_APP_ID,
                "user_id": "99999",
                "is_valid": True,
                "type": "USER",
                "scopes": ["ads_read"],
                "expires_at": int((now + timedelta(hours=1)).timestamp()),
                "data_access_expires_at": int((now + timedelta(days=90)).timestamp()),
            }
        }
    elif path == "/v26.0/act_1234567890":
        data = {
            "id": "1234567890",
            "account_status": 1,
            "currency": "USD",
            "timezone_name": "UTC",
        }
    elif path.endswith("/campaigns"):
        data = {"data": [{"id": "11", "effective_status": "ACTIVE"}]}
    elif path.endswith("/adsets"):
        data = {"data": [{"id": "12", "campaign_id": "11", "effective_status": "ACTIVE"}]}
    elif path.endswith("/ads"):
        data = {
            "data": [
                {"id": "13", "campaign_id": "11", "adset_id": "12", "effective_status": "ACTIVE"}
            ]
        }
    else:
        assert path == "/v26.0/act_1234567890/insights", path
        data = {
            "data": [
                {
                    "campaign_id": "11",
                    "date_start": "2026-09-01",
                    "date_stop": "2026-09-01",
                    "impressions": "10",
                    "clicks": "2",
                    "spend": "1.25",
                    "reach": "8",
                }
            ]
        }
    return httpx.Response(200, json=data)


def meta_ads() -> FakeMeta:
    """Graph API v26.0 for ad account 1234567890 and the two tokens a guide stores."""
    return FakeMeta(
        apis={"graph.facebook.com": _Api(_meta_handler)},
        prompt_answers=[META_USER_TOKEN, META_INSPECTOR_TOKEN],
    )


# --- OpenAI: one account key the user issues in Ads Manager -------------------------------------


@dataclass(kw_only=True)
class FakeOpenAI(FakeProvider):
    """The Advertiser API: a bearer key scoped to one ad account, and nothing else."""

    def _issued(self) -> tuple[str, ...]:
        return (OPENAI_ADS_KEY,)

    def _authenticate(self, api: _Api, request: httpx.Request) -> httpx.Response | None:
        if _bearer(request) != OPENAI_ADS_KEY:
            return httpx.Response(401, json={"error": {"message": "invalid key"}})
        return None


def openai_ads() -> FakeOpenAI:
    """Advertiser API for ad account account_fixture and the key a guide stores."""
    return FakeOpenAI(
        apis={"api.ads.openai.com": _Api(_openai_handler)},
        prompt_answers=[OPENAI_ADS_KEY],
    )


FAKES: Mapping[str, Callable[[], FakeProvider]] = {
    "google_ads": google_ads,
    "ga4": ga4,
    "gtm": gtm,
    "meta_ads": meta_ads,
    "linkedin_ads": linkedin_ads,
    "tiktok_ads": tiktok_ads,
    "reddit_ads": reddit_ads,
    "openai_ads": openai_ads,
}
