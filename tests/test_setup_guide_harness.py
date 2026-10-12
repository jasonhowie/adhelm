"""The setup-guide harness refuses what it must refuse, and the fakes are strict.

`test_setup_guides.py` shows the real guides pass. This module shows the harness would
notice if they did not: each case is a deliberately bad guide or a deliberately bad
request, and the harness or fake must say so. Nothing here reaches a provider.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx
import pytest
from guide_fakes import (
    ADWORDS,
    ANALYTICS_READONLY,
    CLIENT_ID,
    FAKES,
    LINKEDIN_CLIENT_ID,
    LINKEDIN_CLIENT_SECRET,
    LINKEDIN_REDIRECT_URI,
    META_INSPECTOR_TOKEN,
    META_USER_TOKEN,
    OPENAI_ADS_KEY,
    REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET,
    REDDIT_REDIRECT_URI,
    TIKTOK_APP_ID,
    TIKTOK_APP_SECRET,
    TIKTOK_AUTH_CODE,
    TIKTOK_TOKEN_URL,
    FakeGoogle,
    FakeProvider,
    ga4,
    google_ads,
    linkedin_ads,
    meta_ads,
    openai_ads,
    reddit_ads,
    tiktok_ads,
)
from guide_harness import (
    GUIDE_COMMANDS,
    REQUIRED_HEADINGS,
    GuideError,
    check_relative_links,
    command_path,
    parse_guide,
    run_guide,
    subset_mismatch,
)

from adhelm.cli import _COMMANDS
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import GoogleOAuthTarget
from adhelm.google_oauth_setup import GoogleInstalledClient
from adhelm.linkedin_token_profiles import LINKEDIN_OAUTH_CONTRACT
from adhelm.oauth_enrollment import OAuthAuthorizationTarget, start_authorization
from adhelm.reddit_token_profiles import REDDIT_OAUTH_CONTRACT
from adhelm.release_policy import unreleased_commands

TODAY = date(2026, 10, 11)
SOURCE = (
    "- [Doc](https://developers.google.com/x) (fetched 2026-10-11; page last updated 2026-10-01)"
)
INIT = "```sh guide:run\nadhelm init\n```\n"


def guide_text(body: str = INIT, *, sources: str = SOURCE, omit: tuple[str, ...] = ()) -> str:
    """A minimal valid guide with `body` in the Enroll section."""
    parts = ["# Test setup\n"]
    for heading in REQUIRED_HEADINGS:
        if heading in omit:
            continue
        parts.append(f"## {heading}\n")
        if heading == "Enroll":
            parts.append(body)
        if heading == "Sources":
            parts.append(sources + "\n")
    return "\n".join(parts)


def test_a_minimal_guide_is_valid() -> None:
    guide = parse_guide(guide_text(), today=TODAY)
    assert [c.text for c in guide.commands] == ["adhelm init"]
    assert guide.sources == (("https://developers.google.com/x", date(2026, 10, 11)),)


# --- what a guide may run ------------------------------------------------------------------


def test_the_allowed_commands_are_real_and_none_is_a_remote_mutation() -> None:
    assert GUIDE_COMMANDS <= set(_COMMANDS)
    assert not GUIDE_COMMANDS & set(unreleased_commands())
    # Planning, compiling, executing and recovering are never part of a setup guide.
    assert not {
        command
        for command in GUIDE_COMMANDS
        if command.split()[0] in {"plan", "compile", "execute", "recover", "artifacts"}
    }


@pytest.mark.parametrize(
    "command",
    [
        "adhelm execute plan abc",
        "adhelm plan google-ads-campaign-pause create",
        "adhelm compile google-ads",
        "adhelm recover attempt abc",
        "adhelm auth google-oauth reconnect --profile x",
        "adhelm auth meta-token rotate --profile x --interactive",
        "adhelm auth linkedin-token refresh --profile x",
        "adhelm auth openai-key rotate --profile x --interactive",
        "adhelm config show",
        "adhelm pilot run --provider ga4",
        "adhelm bogus",
        "adhelm",
    ],
)
def test_a_command_outside_the_allowlist_is_refused(command: str) -> None:
    with pytest.raises(GuideError, match="not an allowed guide command"):
        parse_guide(guide_text(f"```sh guide:run\n{command}\n```\n"), today=TODAY)


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("rm -rf ~", "only adhelm commands"),
        ("sudo adhelm init", "only adhelm commands"),
        ("adhelm init; rm -rf ~", "not a shell script"),
        ("adhelm init && adhelm doctor", "not a shell script"),
        ("adhelm init | tee out", "not a shell script"),
        ("adhelm init > out", "not a shell script"),
        ("adhelm schema $(whoami)", "not a shell script"),
        ("adhelm schema `whoami`", "not a shell script"),
        ("adhelm init \\", "one line"),
        ("adhelm --data-dir /tmp/real init", "default data directory"),
        ("adhelm init --data-dir=/tmp/real", "default data directory"),
        ("adhelm schema 'unclosed", "unbalanced quotes"),
    ],
)
def test_a_command_that_is_not_one_plain_adhelm_call_is_refused(command: str, message: str) -> None:
    with pytest.raises(GuideError, match=message):
        parse_guide(guide_text(f"```sh guide:run\n{command}\n```\n"), today=TODAY)


def test_config_add_profile_is_an_allowed_guide_command() -> None:
    body = "```sh guide:run\nadhelm config add-profile --input profile.json\n```\n"
    assert parse_guide(guide_text(body), today=TODAY).commands
    assert command_path(("config", "add-profile", "--input", "x.json")) == "config add-profile"


def test_command_names_resolve_to_the_longest_allowed_prefix() -> None:
    assert command_path(("auth", "google-oauth", "enroll", "--input", "x")) == (
        "auth google-oauth enroll"
    )
    assert command_path(("auth", "store", "--profile", "x")) == "auth store"
    assert command_path(("auth",)) is None


def test_an_adhelm_command_in_a_block_that_is_not_run_is_refused() -> None:
    for fence in ("```sh", "```", "```text", "   ```sh"):
        body = f"{INIT}\n{fence}\nadhelm doctor\n{fence.strip()[:3]}\n"
        with pytest.raises(GuideError, match="never executed"):
            parse_guide(guide_text(body), today=TODAY)


def test_shell_commands_that_are_not_adhelm_may_be_shown_unrun() -> None:
    body = f"{INIT}\n```sh\nchmod 600 client_secret.json\nuv tool install adhelm\n```\n"
    assert parse_guide(guide_text(body), today=TODAY).commands


# --- structure -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("```sh guide:run\nadhelm init\n", "never closed"),
        ("   ```sh guide:run\n   adhelm init\n   ```\n", "first column"),
        ("```sh guide:other\nx\n```\n", "unknown mark"),
        ("```sh guide:run guide:file=a.json\nx\n```\n", "at most one"),
        (f"{INIT}\n```json guide:file=../x.json\n{{}}\n```\n", "plain file name"),
        (f"{INIT}\n```json guide:file=/etc/x\n{{}}\n```\n", "plain file name"),
        (
            f"{INIT}\n```json guide:file=a.json\n{{}}\n```\n```json guide:file=a.json\n{{}}\n```\n",
            "twice",
        ),
        ("```json guide:expect\n{}\n```\n", "directly follow"),
        (f"{INIT}\n```json guide:expect\n{{}}\n```\n```json guide:expect\n{{}}\n```\n", "directly"),
        (f"{INIT}\n```json guide:expect\n{{not json\n```\n", "not valid JSON"),
        (f"{INIT}\n```json guide:expect\n[1]\n```\n", "must be a JSON object"),
        ("```sh guide:run\n# nothing but a comment\n```\n", "no `guide:run` command"),
        ("", "no `guide:run` command"),
    ],
)
def test_a_malformed_block_is_refused(body: str, message: str) -> None:
    with pytest.raises(GuideError, match=message):
        parse_guide(guide_text(body), today=TODAY)


def test_a_guide_must_carry_every_required_section_and_end_with_its_sources() -> None:
    for heading in REQUIRED_HEADINGS:
        with pytest.raises(GuideError, match="lacks the section|lacks a Sources"):
            parse_guide(guide_text(omit=(heading,)), today=TODAY)
    reordered = guide_text() + "\n## Afterword\n\nText.\n"
    with pytest.raises(GuideError, match="last section"):
        parse_guide(reordered, today=TODAY)


# --- sources and credentials ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("sources", "message"),
    [
        ("", "lists no primary source"),
        ("- [Doc](https://developers.google.com/x)", "must read"),
        ("- [Doc](http://developers.google.com/x) (fetched 2026-10-11)", "must read"),
        ("- https://developers.google.com/x (fetched 2026-10-11)", "must read"),
        ("- [Doc](https://developers.google.com/x) (fetched 2026-13-40)", "impossible fetch date"),
        ("- [Doc](https://developers.google.com/x) (fetched 2026-10-12)", "in the future"),
        ("* [Doc](https://developers.google.com/x) (fetched 2026-10-11)", "outside a list entry"),
    ],
)
def test_a_source_needs_a_secure_url_and_a_real_past_fetch_date(sources: str, message: str) -> None:
    with pytest.raises(GuideError, match=message):
        parse_guide(guide_text(sources=sources), today=TODAY)


def test_a_linked_page_must_be_listed_in_sources() -> None:
    body = f"{INIT}\nSee [Google](https://developers.google.com/other) for more.\n"
    with pytest.raises(GuideError, match="not listed in Sources"):
        parse_guide(guide_text(body), today=TODAY)
    listed = f"{INIT}\nSee [Google](https://developers.google.com/x) for more.\n"
    assert parse_guide(guide_text(listed), today=TODAY).sources


def test_a_relative_link_must_name_a_file_that_exists(tmp_path: Path) -> None:
    (tmp_path / "there.md").write_text("x")
    check_relative_links("[ok](there.md#part) [web](https://x.test) [here](#a)", tmp_path)
    with pytest.raises(GuideError, match="does not exist"):
        check_relative_links("[gone](missing.md)", tmp_path)


# Built from pieces so that this file holds no string a secret scanner would report.
@pytest.mark.parametrize(
    "line",
    [
        "client secret " + "GOCSPX" + "-" + "a" * 28,
        "access token " + "ya29" + "." + "a0AfH6SMB" + "b" * 20,
        "refresh token " + "1/" + "/0g" + "c" * 30,
        "key " + "AIza" + "Sy" + "d" * 33,
        "-----" + "BEGIN PRIVATE KEY" + "-----",
        "meta token " + "EAA" + "B" + "c" * 30,
        "meta app token " + "1" * 15 + "|" + "a" * 28,
        "linkedin code " + "AQ" + "T" + "x" * 80,
        "linkedin secret " + "WPL_" + "AP1" + "." + "Ab1Cd2Ef3" + "." + "Gh4Ij5==",
        "reddit token " + "1" * 9 + "-" + "aB3" * 9,
        "openai key " + "sk" + "-" + "proj-" + "a" * 30,
    ],
)
def test_a_guide_that_contains_the_shape_of_a_real_credential_is_refused(line: str) -> None:
    with pytest.raises(GuideError, match="shape of a real credential"):
        parse_guide(guide_text(f"{INIT}\n{line}\n"), today=TODAY)


def test_a_google_client_id_is_not_mistaken_for_a_reddit_token() -> None:
    line = '"client_ref": "123456789012-exampleexampleexample.apps.googleusercontent.com",'
    assert parse_guide(guide_text(f"{INIT}\n{line}\n"), today=TODAY).commands


# --- expectations --------------------------------------------------------------------------


def test_expectations_match_objects_by_key_lists_by_element_and_scalars_exactly() -> None:
    actual = {"a": 1, "b": {"c": [{"d": 1, "e": 2}, {"d": 3}], "f": True}, "g": "x"}
    assert subset_mismatch({}, actual) is None
    assert subset_mismatch({"b": {"c": [{"d": 3}], "f": True}}, actual) is None
    assert subset_mismatch({"b": {"c": [{"d": 3}, {"d": 1}]}}, actual) is None
    assert "a is 1" in (subset_mismatch({"a": 2}, actual) or "")
    assert "missing" in (subset_mismatch({"z": 1}, actual) or "")
    assert "matches no element" in (subset_mismatch({"b": {"c": [{"d": 9}]}}, actual) or "")
    assert "not a list" in (subset_mismatch({"g": ["x"]}, actual) or "")
    assert "not an object" in (subset_mismatch({"a": {"x": 1}}, actual) or "")
    assert "not an object" in (subset_mismatch({"a": 1}, None) or "")


def test_an_expectation_does_not_confuse_a_boolean_a_number_and_a_string() -> None:
    assert subset_mismatch({"v": True}, {"v": 1}) is not None
    assert subset_mismatch({"v": 1}, {"v": True}) is not None
    assert subset_mismatch({"v": 0}, {"v": False}) is not None
    assert subset_mismatch({"v": "1"}, {"v": 1}) is not None
    assert subset_mismatch({"v": None}, {"v": False}) is not None


# --- running -------------------------------------------------------------------------------


@dataclass
class Environment:
    """A provider fake that touches nothing, with knobs for the cases below."""

    fixtures: dict[str, str] = field(default_factory=dict)
    leaked: tuple[str, ...] = ()
    seen: list[str] = field(default_factory=list)
    #: Files (path, text) written when the fake is installed, to stand for a command that
    #: left a credential behind.
    planted: dict[Path, str] = field(default_factory=dict)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for path, text in self.planted.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def secrets(self) -> tuple[str, ...]:
        return self.leaked

    def problems(self) -> list[str]:
        return self.seen


def execute(text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **environment):
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    return run_guide(
        parse_guide(text, today=TODAY),
        Environment(**environment),
        workdir=work,
        data_dir=tmp_path / "data",
        monkeypatch=monkeypatch,
    )


def test_a_command_runs_in_a_throwaway_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = execute(guide_text(), tmp_path, monkeypatch)
    assert [r.exit_code for r in results] == [0]
    assert results[0].envelope is not None and results[0].envelope["command"] == "init"
    assert (tmp_path / "data").is_dir()


def test_a_command_that_exits_otherwise_than_the_guide_says_fails_the_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `doctor` exits 3 here (no GA4 profile); the guide says nothing, so it expects 0.
    with pytest.raises(GuideError, match=r"exited 3, the guide says 0"):
        execute(guide_text("```sh guide:run\nadhelm doctor\n```\n"), tmp_path, monkeypatch)


def test_an_exit_annotation_that_the_command_does_not_honor_fails_the_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = guide_text("```sh guide:run\nadhelm init # exit 3\n```\n")
    with pytest.raises(GuideError, match=r"exited 0, the guide says 3"):
        execute(wrong, tmp_path / "wrong", monkeypatch)
    right = guide_text("```sh guide:run\nadhelm init\nadhelm doctor # exit 3\n```\n")
    ok = execute(right, tmp_path / "right", monkeypatch)
    assert [r.exit_code for r in ok] == [0, 3]


def test_a_flag_the_command_does_not_have_fails_the_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(GuideError, match="exited 2"):
        execute(
            guide_text("```sh guide:run\nadhelm init --no-such-flag\n```\n"), tmp_path, monkeypatch
        )


def test_a_wrong_expectation_fails_the_guide_and_a_right_one_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    right = f'{INIT}\n```json guide:expect\n{{"data": {{"configured": true}}}}\n```\n'
    execute(guide_text(right), tmp_path, monkeypatch)
    wrong = f'{INIT}\n```json guide:expect\n{{"data": {{"configured": false}}}}\n```\n'
    with pytest.raises(GuideError, match=r"data\.configured is True, the guide says False"):
        execute(guide_text(wrong), tmp_path / "again", monkeypatch)


def test_an_expectation_about_a_result_that_is_not_json_fails_the_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A usage error prints help text, not a result envelope.
    body = "```sh guide:run\nadhelm init --no-such-flag # exit 2\n```\n"
    body += '```json guide:expect\n{"data": {}}\n```\n'
    with pytest.raises(GuideError, match="result is not an object"):
        execute(guide_text(body), tmp_path, monkeypatch)


def test_a_credential_in_a_command_result_fails_the_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(GuideError, match="printed a credential"):
        execute(guide_text(), tmp_path, monkeypatch, leaked=("configured",))


def test_a_credential_left_on_disk_in_the_data_directory_fails_the_guide_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "data" / "vault" / "deep" / "token.txt"
    with pytest.raises(GuideError, match=r"data directory: vault[/\\]deep[/\\]token\.txt"):
        execute(
            guide_text(),
            tmp_path,
            monkeypatch,
            leaked=("hunter2-secret",),
            planted={nested: "refresh=hunter2-secret\n"},
        )


def test_a_file_on_disk_that_holds_no_credential_passes_the_data_directory_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harmless = tmp_path / "data" / "notes.txt"
    execute(
        guide_text(),
        tmp_path,
        monkeypatch,
        leaked=("hunter2-secret",),
        planted={harmless: "nothing to see\n"},
    )


def test_a_credential_on_disk_only_in_the_work_area_does_not_fail_the_data_directory_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reader's own working directory legitimately holds the downloaded client file.
    work_file = tmp_path / "work" / "client_secret.json"
    execute(
        guide_text(),
        tmp_path,
        monkeypatch,
        leaked=("hunter2-secret",),
        planted={work_file: '{"client_secret": "hunter2-secret"}\n'},
    )


def test_a_provider_file_the_guide_never_uses_fails_the_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(GuideError, match=r"never uses the provider file\(s\): client.json"):
        execute(guide_text(), tmp_path, monkeypatch, fixtures={"client.json": "{}"})
    body = "```sh guide:run\nadhelm schema google-oauth-profile-draft --root-only\n```\n"
    execute(guide_text(body), tmp_path / "again", monkeypatch)


def test_a_file_block_is_written_before_the_commands_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = INIT + '\n```json guide:file=draft.json\n{"a": 1}\n```\n'
    execute(guide_text(body), tmp_path, monkeypatch)
    assert (tmp_path / "work" / "draft.json").read_text() == '{"a": 1}\n'


def test_a_file_block_may_not_overwrite_a_file_the_provider_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = INIT + '\n```json guide:file=client.json\n{"a": 1}\n```\n'
    with pytest.raises(GuideError, match="already exists"):
        execute(guide_text(body), tmp_path, monkeypatch, fixtures={"client.json": "{}"})


def test_a_request_the_provider_fake_flagged_fails_the_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(GuideError, match="the fake provider saw: PUT example.test/x"):
        execute(guide_text(), tmp_path, monkeypatch, seen=["PUT example.test/x"])


# --- the fake provider is strict -----------------------------------------------------------


def client(fake: FakeProvider) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake.serve))


def grant(fake: FakeGoogle, scope: str) -> str:
    fake._grants["fixture-access-token-x"] = scope
    return "fixture-access-token-x"


def test_the_fake_flags_a_host_that_is_not_the_providers_api() -> None:
    fake = google_ads()
    response = client(fake).get("https://evil.example/v25/customers")
    assert response.status_code == 403
    assert fake.problems() == ["a request to evil.example, which is not this API"]


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_the_fake_flags_a_method_that_is_not_a_read(method: str) -> None:
    fake = google_ads()
    token = grant(fake, ADWORDS)
    response = client(fake).request(
        method,
        "https://googleads.googleapis.com/v25/customers/1234567890/campaigns/1",
        headers={"authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert fake.problems() == [
        f"{method} googleads.googleapis.com/v25/customers/1234567890/campaigns/1"
    ]


def test_the_fake_flags_a_post_that_is_not_a_read_only_query() -> None:
    fake = google_ads()
    token = grant(fake, ADWORDS)
    response = client(fake).post(
        "https://googleads.googleapis.com/v25/customers/1234567890/googleAds:mutate",
        headers={"authorization": f"Bearer {token}"},
        json={"operations": []},
    )
    assert response.status_code == 403
    assert fake.problems() == [
        "POST googleads.googleapis.com/v25/customers/1234567890/googleAds:mutate"
    ]


def test_the_fake_refuses_a_request_without_the_bearer_token_it_issued() -> None:
    fake = ga4()
    url = "https://analyticsadmin.googleapis.com/v1beta/properties/123"
    assert client(fake).get(url).status_code == 401
    assert client(fake).get(url, headers={"authorization": "Bearer guessed"}).status_code == 401
    assert client(fake).get(url, headers={"authorization": "Basic abc"}).status_code == 401
    assert fake.problems() == []


def test_the_fake_refuses_a_token_granted_for_another_scope() -> None:
    fake = ga4()
    token = grant(fake, ADWORDS)
    response = client(fake).get(
        "https://analyticsadmin.googleapis.com/v1beta/properties/123",
        headers={"authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_the_google_ads_fake_flags_and_refuses_a_developer_token_header() -> None:
    """Google retired developer tokens on 2026-09-09; AdHelm must never send one."""
    fake = google_ads()
    token = grant(fake, ADWORDS)
    url = "https://googleads.googleapis.com/v25/customers/1234567890/googleAds:search"
    body = {"query": "SELECT customer.currency_code FROM customer"}
    sent = client(fake).post(
        url, headers={"authorization": f"Bearer {token}", "developer-token": "any"}, json=body
    )
    assert sent.status_code == 400
    assert fake.problems() == [
        "a developer-token header to googleads.googleapis.com"
        "/v25/customers/1234567890/googleAds:search"
    ]
    clean = client(fake).post(url, headers={"authorization": f"Bearer {token}"}, json=body)
    assert clean.status_code == 200


def test_a_google_ads_guide_has_no_secret_prompt_to_answer() -> None:
    fake = google_ads()
    assert fake.prompt_answers == []
    with pytest.raises(AdhelmError, match="unexpected secret") as raised:
        fake._prompt()
    assert raised.value.code == "FAKE_PROMPT_EXHAUSTED"


def test_a_client_file_for_another_client_is_not_granted_a_token() -> None:
    """The browser seam grants only for the fixture's own client secret."""
    fake = ga4()
    target = GoogleOAuthTarget(CLIENT_ID, ANALYTICS_READONLY)
    with pytest.raises(AdhelmError, match="rejects this client") as raised:
        fake._browser_grant(target, GoogleInstalledClient(CLIENT_ID, "another-secret"))
    assert raised.value.code == "FAKE_GOOGLE_CLIENT_REJECTED"
    assert fake.consents == []


def test_every_registered_fake_is_a_fresh_instance() -> None:
    assert all(factory() is not factory() for factory in FAKES.values())


# --- every fake is strict about what a read-only guide may send ----------------------------


@pytest.fixture(params=sorted(FAKES))
def any_fake(request: pytest.FixtureRequest) -> FakeProvider:
    return FAKES[str(request.param)]()


def api_url(fake: FakeProvider, path: str = "/x") -> str:
    return f"https://{next(iter(fake.apis))}{path}"


def test_every_fake_flags_a_host_that_is_not_the_providers_api(any_fake: FakeProvider) -> None:
    response = client(any_fake).get("https://evil.example/v1/campaigns")
    assert response.status_code == 403
    assert any_fake.problems() == ["a request to evil.example, which is not this API"]


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_every_fake_flags_a_method_that_is_not_a_read(any_fake: FakeProvider, method: str) -> None:
    response = client(any_fake).request(method, api_url(any_fake, "/v1/campaigns/1"))
    assert response.status_code == 403
    assert len(any_fake.problems()) == 1
    assert any_fake.problems()[0].startswith(f"{method} ")


def test_every_fake_flags_a_post_that_is_not_one_of_its_read_only_queries(
    any_fake: FakeProvider,
) -> None:
    response = client(any_fake).post(api_url(any_fake, "/v1/campaigns"), json={"name": "x"})
    assert response.status_code == 403
    assert len(any_fake.problems()) == 1
    assert any_fake.problems()[0].startswith("POST ")


@pytest.mark.parametrize(
    "factory",
    [meta_ads, linkedin_ads, tiktok_ads, reddit_ads, openai_ads],
    ids=lambda f: f.__name__,
)
def test_a_fake_that_issues_credentials_refuses_a_read_that_carries_none_it_issued(
    factory: Callable[[], FakeProvider],
) -> None:
    fake = factory()
    url = api_url(fake, "/v26.0/act_1234567890")
    assert client(fake).get(url).status_code == 401
    wrong = {"authorization": "Bearer guessed", "access-token": "guessed"}
    assert client(fake).get(url, headers=wrong).status_code == 401
    assert client(fake).get(url, headers={"authorization": "Basic abc"}).status_code == 401
    assert fake.problems() == []


# --- the OAuth fakes check the browser step and the token endpoint -------------------------


def attempt(contract, client_id: str, redirect: str, scopes: tuple[str, ...]):
    target = OAuthAuthorizationTarget(contract=contract, client_ref=client_id, scopes=scopes)
    return start_authorization(target, redirect)


def test_the_loopback_fakes_refuse_an_authorization_for_another_client_or_redirect() -> None:
    fake = linkedin_ads()
    scopes = ("r_ads", "r_ads_reporting")
    with pytest.raises(AdhelmError, match="refused"):
        fake._browser(
            attempt(LINKEDIN_OAUTH_CONTRACT, "another-client", LINKEDIN_REDIRECT_URI, scopes)
        )
    with pytest.raises(AdhelmError, match="refused"):
        fake._browser(
            attempt(
                LINKEDIN_OAUTH_CONTRACT,
                LINKEDIN_CLIENT_ID,
                "http://127.0.0.1:1/oauth/callback",
                scopes,
            )
        )
    assert fake.problems() == [
        "an authorization request for a client the user did not enter",
        "an authorization request for a redirect that is not registered",
    ]
    assert fake.authorizations == []
    fake._browser(
        attempt(LINKEDIN_OAUTH_CONTRACT, LINKEDIN_CLIENT_ID, LINKEDIN_REDIRECT_URI, scopes)
    )
    assert fake.authorizations[0]["client_id"] == LINKEDIN_CLIENT_ID


def authorized(fake, contract, client_id, redirect, scopes) -> str:
    fake._browser(attempt(contract, client_id, redirect, scopes))
    return dict(pair.split("=") for pair in fake.approve().split("&"))["code"]


def linkedin_form(code: str, /, **replace: str) -> dict[str, str]:
    return {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": LINKEDIN_CLIENT_ID,
        "client_secret": LINKEDIN_CLIENT_SECRET,
        "redirect_uri": LINKEDIN_REDIRECT_URI,
        **replace,
    }


LINKEDIN_TOKEN = "https://www.linkedin.com/oauth/v2/accessToken"


def test_the_linkedin_token_endpoint_issues_a_token_once_for_the_right_client_and_code() -> None:
    fake = linkedin_ads()
    scopes = ("r_ads", "r_ads_reporting")
    code = authorized(
        fake, LINKEDIN_OAUTH_CONTRACT, LINKEDIN_CLIENT_ID, LINKEDIN_REDIRECT_URI, scopes
    )
    assert client(fake).post(LINKEDIN_TOKEN, data=linkedin_form(code)).status_code == 200
    assert fake.problems() == []
    # A code is single-use.
    assert client(fake).post(LINKEDIN_TOKEN, data=linkedin_form(code)).status_code == 400
    assert fake.problems() == ["the token request carries a code the provider did not issue"]


@pytest.mark.parametrize(
    ("replace", "problem"),
    [
        (
            {"client_secret": "wrong"},
            "the token request does not authenticate the client the user entered",
        ),
        (
            {"client_id": "wrong"},
            "the token request does not authenticate the client the user entered",
        ),
        (
            {"redirect_uri": "http://127.0.0.1:1/oauth/callback"},
            "the token request names a redirect that was not used",
        ),
        ({"code": "guessed"}, "the token request carries a code the provider did not issue"),
        ({"grant_type": "password"}, "a token request with grant_type=password"),
        ({"grant_type": "refresh_token"}, "a token request with grant_type=refresh_token"),
    ],
)
def test_the_linkedin_token_endpoint_refuses_what_linkedin_would(
    replace: dict[str, str], problem: str
) -> None:
    fake = linkedin_ads()
    scopes = ("r_ads", "r_ads_reporting")
    code = authorized(
        fake, LINKEDIN_OAUTH_CONTRACT, LINKEDIN_CLIENT_ID, LINKEDIN_REDIRECT_URI, scopes
    )
    response = client(fake).post(LINKEDIN_TOKEN, data=linkedin_form(code, **replace))
    assert response.status_code == 400
    assert fake.problems() == [problem]


def test_the_token_endpoint_is_a_post_only_route() -> None:
    fake = linkedin_ads()
    assert client(fake).get(LINKEDIN_TOKEN).status_code == 405
    assert fake.problems() == ["GET www.linkedin.com/oauth/v2/accessToken"]


def reddit_basic(user: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"authorization": f"Basic {raw}", "user-agent": "adhelm-test"}


def test_the_reddit_token_endpoint_wants_the_app_id_and_secret_as_basic_and_a_user_agent() -> None:
    fake = reddit_ads()
    url = "https://www.reddit.com/api/v1/access_token"
    code = authorized(
        fake, REDDIT_OAUTH_CONTRACT, REDDIT_CLIENT_ID, REDDIT_REDIRECT_URI, ("adsread",)
    )
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDDIT_REDIRECT_URI}
    basic_problem = "the token request does not authenticate the client with HTTP Basic"
    other_app = "the token request does not authenticate the app the user entered"

    for headers, problem in (
        ({}, basic_problem),
        ({"authorization": "Bearer abc", "user-agent": "adhelm-test"}, basic_problem),
        ({"authorization": "Basic not-base64!", "user-agent": "adhelm-test"}, basic_problem),
        # The 0.1.0 behavior this fake exists to refuse: the app ID with no secret.
        (
            reddit_basic(REDDIT_CLIENT_ID, ""),
            "the token request authenticates with an empty client secret",
        ),
        (reddit_basic(REDDIT_CLIENT_ID, "another-secret"), other_app),
        (reddit_basic("another-app", REDDIT_CLIENT_SECRET), other_app),
        (
            {**reddit_basic(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET), "user-agent": ""},
            "the token request has no User-Agent, which Reddit rejects",
        ),
    ):
        fake.violations.clear()
        assert client(fake).post(url, data=form, headers=headers).status_code == 400
        assert fake.problems() == [problem]
    fake.violations.clear()
    accepted = reddit_basic(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET)
    assert client(fake).post(url, data=form, headers=accepted).status_code == 200
    assert fake.problems() == []


def test_the_reddit_refresh_grant_wants_the_secret_and_the_refresh_token_it_issued() -> None:
    fake = reddit_ads()
    url = "https://www.reddit.com/api/v1/access_token"
    form = {"grant_type": "refresh_token", "refresh_token": "guessed"}
    empty = reddit_basic(REDDIT_CLIENT_ID, "")
    assert client(fake).post(url, data=form, headers=empty).status_code == 400
    assert fake.problems() == ["the token request authenticates with an empty client secret"]
    fake.violations.clear()
    full = reddit_basic(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET)
    assert client(fake).post(url, data=form, headers=full).status_code == 400
    assert fake.problems() == ["the refresh request carries a token the provider did not issue"]


def test_the_reddit_fake_counts_the_app_secret_among_the_credentials_no_output_may_carry() -> None:
    assert REDDIT_CLIENT_SECRET in reddit_ads().secrets()


def test_a_provider_that_issues_no_refresh_token_refuses_a_refresh_grant() -> None:
    fake = linkedin_ads()
    form = linkedin_form("x", grant_type="refresh_token", refresh_token="guessed")
    assert client(fake).post(LINKEDIN_TOKEN, data=form).status_code == 400
    assert fake.problems() == ["a token request with grant_type=refresh_token"]


# --- TikTok, Meta and OpenAI ---------------------------------------------------------------


def tiktok_body(**replace: str) -> dict[str, str]:
    return {
        "app_id": TIKTOK_APP_ID,
        "secret": TIKTOK_APP_SECRET,
        "auth_code": TIKTOK_AUTH_CODE,
        **replace,
    }


def test_the_tiktok_token_endpoint_accepts_the_code_once() -> None:
    fake = tiktok_ads()
    assert client(fake).post(TIKTOK_TOKEN_URL, json=tiktok_body()).status_code == 200
    assert fake.problems() == []
    assert client(fake).post(TIKTOK_TOKEN_URL, json=tiktok_body()).status_code == 400
    assert fake.problems() == ["a token request with an authorization code that is wrong or used"]


@pytest.mark.parametrize(
    ("replace", "problem"),
    [
        ({"secret": "wrong"}, "a token request that does not name the app the user entered"),
        ({"app_id": "1"}, "a token request that does not name the app the user entered"),
        (
            {"auth_code": "guessed"},
            "a token request with an authorization code that is wrong or used",
        ),
    ],
)
def test_the_tiktok_token_endpoint_refuses_what_tiktok_would(
    replace: dict[str, str], problem: str
) -> None:
    fake = tiktok_ads()
    assert client(fake).post(TIKTOK_TOKEN_URL, json=tiktok_body(**replace)).status_code == 400
    assert fake.problems() == [problem]


def test_the_tiktok_token_endpoint_wants_exactly_the_documented_json_body() -> None:
    fake = tiktok_ads()
    extra = tiktok_body(grant_type="authorization_code")
    assert client(fake).post(TIKTOK_TOKEN_URL, json=extra).status_code == 400
    assert fake.problems() == [
        "a token request whose body is not exactly app_id, auth_code and secret"
    ]
    fake.violations.clear()
    form = client(fake).post(TIKTOK_TOKEN_URL, data=tiktok_body())
    assert form.status_code == 400
    assert fake.problems() == ["a token request that is not a JSON POST"]


def test_a_tiktok_read_needs_the_access_token_header_it_issued() -> None:
    fake = tiktok_ads()
    body = client(fake).post(TIKTOK_TOKEN_URL, json=tiktok_body()).json()
    token = body["data"]["access_token"]
    url = api_url(fake, "/open_api/v1.3/advertiser/info/")
    assert client(fake).get(url, headers={"authorization": f"Bearer {token}"}).status_code == 401
    assert client(fake).get(url, headers={"access-token": token}).status_code == 200


def test_meta_inspects_with_the_inspector_token_and_reads_with_the_user_token() -> None:
    fake = meta_ads()
    inspect_url = api_url(fake, "/v26.0/debug_token")
    inspector = {"authorization": f"Bearer {META_INSPECTOR_TOKEN}"}
    user = {"authorization": f"Bearer {META_USER_TOKEN}"}
    assert (
        client(fake)
        .get(inspect_url, params={"input_token": META_USER_TOKEN}, headers=user)
        .status_code
        == 401
    )
    assert (
        client(fake)
        .get(inspect_url, params={"input_token": "other"}, headers=inspector)
        .status_code
        == 400
    )
    assert (
        client(fake)
        .get(inspect_url, params={"input_token": META_USER_TOKEN}, headers=inspector)
        .status_code
        == 200
    )
    read = api_url(fake, "/v26.0/act_1234567890")
    assert client(fake).get(read, headers=inspector).status_code == 401
    assert client(fake).get(read, headers=user).status_code == 200
    assert fake.problems() == []


@pytest.mark.parametrize("parameter", ["access_token", "input_token"])
def test_meta_flags_a_token_in_the_query_string_of_a_read(parameter: str) -> None:
    fake = meta_ads()
    read = api_url(fake, "/v26.0/act_1234567890")
    response = client(fake).get(read, params={parameter: META_USER_TOKEN})
    assert response.status_code == 400
    assert fake.problems() == ["a token in the query string of a read"]


def test_openai_accepts_only_the_key_the_user_stored() -> None:
    fake = openai_ads()
    url = api_url(fake, "/v1/ad_account")
    assert client(fake).get(url, headers={"authorization": "Bearer other"}).status_code == 401
    assert (
        client(fake).get(url, headers={"authorization": f"Bearer {OPENAI_ADS_KEY}"}).status_code
        == 200
    )
    assert fake.problems() == []


def test_every_fake_prompt_answers_in_order_and_refuses_an_unexpected_prompt(
    any_fake: FakeProvider,
) -> None:
    answers = list(any_fake.prompt_answers)
    for answer in answers:
        assert any_fake._prompt() == answer
    with pytest.raises(AdhelmError, match="unexpected secret"):
        any_fake._prompt()
    # Everything it answered counts as a credential that must never reach output or disk.
    assert set(answers) <= set(any_fake.secrets())
