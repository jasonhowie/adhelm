"""Every provider setup guide, executed command by command against its fake provider.

`docs/setup/README.md` explains the guide format. The harness is `guide_harness.py`; the
fake providers are `guide_fakes.py`. A guide that drifts from the command line, shows a
credential, reads more than it says, or cites a source without a fetch date fails here.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import webbrowser
from pathlib import Path

import pytest
from guide_fakes import (
    ADWORDS,
    ANALYTICS_READONLY,
    FAKES,
    LINKEDIN_REDIRECT_URI,
    META_APP_ID,
    REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET,
    REDDIT_REDIRECT_URI,
    TAGMANAGER_READONLY,
    TIKTOK_APP_ID,
    FakeGoogle,
    FakeLoopbackOAuth,
    FakeMeta,
    FakeTikTok,
)
from guide_harness import (
    REQUIRED_HEADINGS,
    Guide,
    GuideError,
    check_relative_links,
    parse_guide,
    run_guide,
)

from adhelm import credentials, oauth_enrollment, tiktok_token_workflow
from adhelm.pilot_records import RELEASE_PROVIDERS
from adhelm.release_labels import release_label

SETUP = Path(__file__).resolve().parents[1] / "docs" / "setup"
GUIDE_FILES = sorted(path for path in SETUP.glob("*.md") if path.name != "README.md")

#: What each Google guide must ask the user to consent to, one entry per enrollment.
CONSENTS = {
    "google_ads": [ADWORDS],
    "ga4": [ANALYTICS_READONLY, ANALYTICS_READONLY],
    "gtm": [TAGMANAGER_READONLY],
}
#: The hosts each guide's commands may reach: the provider's API and, where enrollment
#: exchanges a code, its token endpoint.
HOSTS = {
    "google_ads": {"googleads.googleapis.com"},
    "ga4": {"analyticsdata.googleapis.com", "analyticsadmin.googleapis.com"},
    "gtm": {"tagmanager.googleapis.com"},
    "meta_ads": {"graph.facebook.com"},
    "linkedin_ads": {"api.linkedin.com", "www.linkedin.com"},
    "tiktok_ads": {"business-api.tiktok.com"},
    "reddit_ads": {"ads-api.reddit.com", "www.reddit.com"},
    "openai_ads": {"api.ads.openai.com"},
}
#: The path suffixes a guide may `POST` to: a provider's documented read-only queries and
#: its token endpoint. Every other request a guide makes is a `GET`.
POSTS = {
    "google_ads": (":search", ":searchStream"),
    "ga4": (":runReport",),
    "gtm": (),
    "meta_ads": (),
    "linkedin_ads": ("/oauth/v2/accessToken",),
    "tiktok_ads": ("/oauth2/access_token/",),
    "reddit_ads": ("/api/v1/access_token", "/reports"),
    "openai_ads": (),
}
#: The documentation sites a guide cites; it cites nothing else.
PRIMARY_HOSTS = {
    "google_ads": {"developers.google.com", "support.google.com"},
    "ga4": {"developers.google.com", "support.google.com"},
    "gtm": {"developers.google.com", "support.google.com"},
    "meta_ads": {"developers.facebook.com"},
    "linkedin_ads": {"learn.microsoft.com", "www.linkedin.com"},
    "tiktok_ads": {"business-api.tiktok.com"},
    "reddit_ads": {"ads-api.reddit.com"},
    "openai_ads": {"developers.openai.com"},
}


def test_every_guide_has_a_fake_provider_and_every_fake_a_guide() -> None:
    stems = {path.stem for path in GUIDE_FILES}
    assert stems == set(FAKES), (
        "a guide without a fake is never run; a fake without a guide is dead"
    )
    assert stems <= set(RELEASE_PROVIDERS), "a guide is named for its provider ID"
    assert stems == set(HOSTS) == set(POSTS) == set(PRIMARY_HOSTS)
    assert set(CONSENTS) <= stems


@pytest.fixture(params=[path.stem for path in GUIDE_FILES])
def provider(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _text(provider: str) -> str:
    return (SETUP / f"{provider}.md").read_text(encoding="utf-8")


def _run(guide: Guide, provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = FAKES[provider]()
    work = tmp_path / "work"
    work.mkdir()
    data = tmp_path / "data"
    results = run_guide(guide, fake, workdir=work, data_dir=data, monkeypatch=monkeypatch)
    return fake, data, results


def test_the_guide_is_complete_and_cites_only_primary_sources(provider: str) -> None:
    text = _text(provider)
    guide = parse_guide(text)
    for heading in REQUIRED_HEADINGS:
        assert f"\n## {heading}\n" in text
    assert guide.sources
    assert {url.split("/")[2] for url, _ in guide.sources} <= PRIMARY_HOSTS[provider]
    check_relative_links(text, SETUP)


def test_the_guide_states_the_providers_release_label(provider: str) -> None:
    """A provider that earns `LIVE_PILOTED` must have its guide say so."""
    label = release_label(provider)
    assert label is not None
    assert f"labelled `{label.label}`" in " ".join(_text(provider).split())


def test_the_guide_runs_as_written_and_leaves_nothing_behind(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guide = parse_guide(_text(provider))

    fake, data, results = _run(guide, provider, tmp_path, monkeypatch)

    assert [r.command.line for r in results] == [c.line for c in guide.commands]
    # The reads happened, reached only the provider's own hosts, and changed nothing.
    assert fake.requests, "the guide proves nothing if it never reads the provider"
    assert {request.url.host for request in fake.requests} == HOSTS[provider]
    for request in fake.requests:
        assert request.method == "GET" or (
            request.method == "POST" and request.url.path.endswith(POSTS[provider])
        ), f"{request.method} {request.url.host}{request.url.path}"
    # `run_guide` already failed the guide had a credential reached the data directory;
    # revocation also removed every local copy from the credential store.
    assert set(fake.store.values) == {("adhelm", credentials._EXPORT_KEY_REFERENCE)}
    if provider == "meta_ads":
        # Meta also keeps a retained-inspection file; its Revoke removes this profile's record.
        assert json.loads((data / "meta-token-observations.json").read_text()) == {}


@pytest.mark.parametrize("provider", sorted(CONSENTS))
def test_the_google_guides_ask_for_the_one_scope_per_enrollment(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _, _ = _run(parse_guide(_text(provider)), provider, tmp_path, monkeypatch)
    assert isinstance(fake, FakeGoogle)
    # Least privilege: the browser was asked for the one scope per enrollment, no more.
    assert fake.consents == CONSENTS[provider]


@pytest.mark.parametrize(
    ("provider", "redirect", "scope", "extra"),
    [
        ("linkedin_ads", LINKEDIN_REDIRECT_URI, "r_ads r_ads_reporting", {}),
        ("reddit_ads", REDDIT_REDIRECT_URI, "adsread", {"duration": "permanent"}),
    ],
)
def test_the_loopback_guides_ask_for_the_read_scopes_only_on_the_registered_redirect(
    provider: str,
    redirect: str,
    scope: str,
    extra: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _text(provider)
    # The redirect the guide tells the reader to register is the one AdHelm uses.
    assert f"`{redirect}`" in text
    fake, _, _ = _run(parse_guide(text), provider, tmp_path, monkeypatch)
    assert isinstance(fake, FakeLoopbackOAuth)
    (authorization,) = fake.authorizations
    assert authorization["scope"] == scope
    assert authorization["redirect_uri"] == redirect
    assert extra.items() <= authorization.items()


def test_the_reddit_guide_sends_the_app_secret_only_as_the_basic_password_of_the_token_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reddit's pages show `-u '<app ID>:<secret>'`; the secret goes there and nowhere else."""
    text = " ".join(_text("reddit_ads").split())
    assert "`-u '<app ID>:<secret>'`" in text
    # The gap the guide used to open with is closed; nothing of it may linger.
    for stale in ("an **empty** password", "asks for no secret", "has no slot to store one"):
        assert stale not in text
    guide = parse_guide(_text("reddit_ads"))
    fake, data, results = _run(guide, "reddit_ads", tmp_path, monkeypatch)
    assert isinstance(fake, FakeLoopbackOAuth)
    assert fake.prompt_answers == [], "the guide prompted for fewer values than the fake holds"

    # The code exchange, then the refresh the Verify section runs.
    exchanges = [r for r in fake.requests if r.url.path == "/api/v1/access_token"]
    assert [r.method for r in exchanges] == ["POST", "POST"]
    for request in exchanges:
        scheme, _, credentials_ = request.headers["authorization"].partition(" ")
        user, _, password = base64.b64decode(credentials_).decode().partition(":")
        assert (scheme, user, password) == ("Basic", REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET)
    # Nowhere else did it travel: not a URL, a body, or a request to the Ads API.
    for request in fake.requests:
        assert REDDIT_CLIENT_SECRET not in str(request.url)
        assert REDDIT_CLIENT_SECRET not in request.content.decode()
        if request not in exchanges:
            assert REDDIT_CLIENT_SECRET not in " ".join(request.headers.values())
    # Nor into argv, command output or the data directory, which holds the ledger.
    for result in results:
        assert REDDIT_CLIENT_SECRET not in result.command.text
        assert REDDIT_CLIENT_SECRET not in result.output
    for path in data.rglob("*"):
        if path.is_file():
            assert REDDIT_CLIENT_SECRET.encode() not in path.read_bytes(), path


def test_the_reddit_guide_stops_when_reddit_rejects_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong secret fails the exchange: the guide fails there, with nothing stored."""
    fake = FAKES["reddit_ads"]()
    assert isinstance(fake, FakeLoopbackOAuth)
    fake.prompt_answers[1] = "notTheSecret"
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(GuideError, match="enroll"):
        run_guide(
            parse_guide(_text("reddit_ads")),
            fake,
            workdir=work,
            data_dir=tmp_path / "data",
            monkeypatch=monkeypatch,
        )

    assert fake.problems() == ["the token request does not authenticate the app the user entered"]
    assert set(fake.store.values) <= {("adhelm", credentials._EXPORT_KEY_REFERENCE)}


def test_no_guide_points_the_reader_at_the_private_release_plan(provider: str) -> None:
    """The release plan is not in the public tree, so a guide must not send readers there.

    Known gaps in `0.1.0` are described in the guides' own words.
    """
    text = " ".join(_text(provider).split()).lower()
    for pattern in (r"release\s+plan", r"release\s+item", r"follow-up\s+item", r"oss-0\.1\.0-p"):
        assert re.search(pattern, text) is None, pattern


def test_the_tiktok_revoke_section_and_the_shipped_guidance_name_the_same_routes() -> None:
    """The guide says `status` prints the same two routes; hold it to that."""
    from adhelm.tiktok_token_cli import TIKTOK_REVOCATION_GUIDANCE

    text = " ".join(_text("tiktok_ads").split())
    revoke = text[text.index("## Revoke") : text.index("## Sources")]
    shipped = " ".join(TIKTOK_REVOCATION_GUIDANCE)
    for route in ("ads.tiktok.com/ac/page/authorizations", "/oauth2/revoke_token/"):
        assert route in revoke
        assert route in shipped
    assert "NOT_VERIFIED_AGAINST_RENDERED_DOCS" not in shipped


def test_the_tiktok_guide_exchanges_the_code_once_for_the_app_the_reader_entered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader supplies the code: no browser handoff and no loopback listener."""
    handoffs: list[str] = []

    def _handoff(name: str) -> object:
        def record(*_: object, **__: object) -> None:
            handoffs.append(name)
            raise AssertionError(f"the TikTok guide reached {name}")

        return record

    # The leaves a browser handoff or a listener would pass through, and the names a
    # TikTok workflow would reach them by if it ever imported them.
    for module, name in (
        (oauth_enrollment, "open_system_browser"),
        (oauth_enrollment, "LoopbackRedirectReceiver"),
        (tiktok_token_workflow, "open_system_browser"),
        (tiktok_token_workflow, "LoopbackRedirectReceiver"),
        (webbrowser, "open"),
        (subprocess, "run"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(module, name, _handoff(f"{module.__name__}.{name}"), raising=False)
    fake, _, _ = _run(parse_guide(_text("tiktok_ads")), "tiktok_ads", tmp_path, monkeypatch)
    assert isinstance(fake, FakeTikTok)
    assert fake.token_requests == [{"app_id": TIKTOK_APP_ID}]
    assert handoffs == []


def test_the_meta_guide_reads_with_the_user_token_and_inspects_with_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _text("meta_ads")
    assert f'"client_application_ref": "{META_APP_ID}"' in text
    fake, _, _ = _run(parse_guide(text), "meta_ads", tmp_path, monkeypatch)
    assert isinstance(fake, FakeMeta)
    inspected = [r for r in fake.requests if r.url.path.endswith("/debug_token")]
    reads = [r for r in fake.requests if not r.url.path.endswith("/debug_token")]
    # The guide says the inspector token is used by `inspect` and at the start of each
    # `inventory` and `report`: three commands, three inspections.
    assert len(inspected) == 3
    assert reads
    assert all("input_token" in r.url.params for r in inspected)
    assert not any("access_token" in r.url.params or "input_token" in r.url.params for r in reads)
    assert fake.problems() == []


@pytest.mark.parametrize(
    ("provider", "old", "new", "message"),
    [
        pytest.param(
            "google_ads",
            "--client-config",
            "--client-file",
            "exited",
            id="google: a flag the command lacks",
        ),
        pytest.param(
            "google_ads",
            "adhelm auth remove-local --profile google-ads-read",
            "adhelm auth remove-local --profile google-ads-read --slot SECONDARY",
            "exited",
            id="google: the retired developer-token slot",
        ),
        pytest.param(
            "google_ads",
            "inventory google_ads google-ads-read",
            "inventory google_ads other",
            "exited",
            id="google: a profile never enrolled",
        ),
        pytest.param(
            "google_ads",
            " --client-config client_secret.json --interactive",
            " --client-config client_secret.json",
            "exited 4",
            id="google: browser handoff not requested",
        ),
        pytest.param(
            "reddit_ads",
            "--redirect-port 8765",
            "--redirect-port 9999",
            "exited",
            id="reddit: a port the app did not register",
        ),
        pytest.param(
            "reddit_ads",
            "--redirect-port 8765 --interactive",
            "--redirect-port 8765",
            "exited 4",
            id="reddit: browser handoff not requested",
        ),
        pytest.param(
            "reddit_ads",
            '"scopes": ["adsread"]',
            '"scopes": ["adsedit"]',
            "exited",
            id="reddit: a write scope on a read profile",
        ),
        pytest.param(
            "linkedin_ads",
            "--redirect-port 8765",
            "--redirect-port 8766",
            "exited",
            id="linkedin: a port the app did not register",
        ),
        pytest.param(
            "linkedin_ads",
            '"scopes": ["r_ads", "r_ads_reporting"]',
            '"scopes": ["r_ads", "rw_ads"]',
            "exited",
            id="linkedin: a write scope on a read profile",
        ),
        pytest.param(
            "linkedin_ads",
            '"account_ref": "456",\n  "principal_ref"',
            '"account_ref": "457",\n  "principal_ref"',
            "exited",
            id="linkedin: an ad account the grant does not reach",
        ),
        pytest.param(
            "tiktok_ads",
            "--environment PRODUCTION",
            "--environment SANDBOX",
            "exited",
            id="tiktok: the environment AdHelm refuses",
        ),
        pytest.param(
            "tiktok_ads",
            '"account_ref": "123",\n  "principal_ref"',
            '"account_ref": "124",\n  "principal_ref"',
            "exited 4",
            id="tiktok: an advertiser the grant does not include",
        ),
        pytest.param(
            "meta_ads",
            '"client_application_ref": "987654321012345"',
            '"client_application_ref": "987654321012346"',
            "exited 4",
            id="meta: a token that belongs to another app",
        ),
        pytest.param(
            "meta_ads",
            "inspect --profile meta-read",
            "inspect --profile other",
            "exited",
            id="meta: a profile never enrolled",
        ),
        pytest.param(
            "openai_ads",
            '"account_ref": "account_fixture",\n  "purpose"',
            '"account_ref": "account_other",\n  "purpose"',
            "exited",
            id="openai: an ad account the key does not reach",
        ),
        pytest.param(
            "openai_ads",
            "store --profile openai-read --interactive",
            "store --profile openai-read",
            "exited 4",
            id="openai: hidden prompt not requested",
        ),
    ],
)
def test_a_guide_that_has_drifted_from_the_command_line_fails(
    provider: str,
    old: str,
    new: str,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _text(provider)
    assert text.count(old) == 1, old
    assert old in text
    drifted = parse_guide(text.replace(old, new))
    with pytest.raises(GuideError, match=message):
        _run(drifted, provider, tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "provider",
    ["google_ads", "ga4", "meta_ads", "linkedin_ads", "tiktok_ads", "reddit_ads", "openai_ads"],
)
def test_the_report_guides_say_what_adhelm_does_with_the_query_contract_hash(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AdHelm replaces the supplied hash with its own digest; the guides must say so."""
    text = _text(provider)
    assert "ignores the value you supply" in " ".join(text.split())
    guide = parse_guide(text)
    _, _, results = _run(guide, provider, tmp_path, monkeypatch)
    report = next(r for r in results if r.command.arguments[:1] == ("report",))
    assert report.envelope is not None
    stored = report.envelope["data"]["envelope"]["query_contract_hash"]
    supplied = "0" * 64
    assert supplied in text
    assert len(stored) == 64
    assert stored != supplied
