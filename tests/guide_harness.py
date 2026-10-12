"""Run a provider setup guide's commands against a fake provider.

`docs/setup/<provider>.md` is both the text a person follows and the test of that
text. This module parses a guide, refuses it if it could do anything other than
enroll, read and revoke on a throwaway data directory, and executes every command it
shows, in order, through the real command line application. The only things replaced
are the places a command would leave the process: the operating-system credential
store, the system browser, the hidden secret prompt and the provider's HTTP API. A
provider fake supplies those (`guide_fakes.py`). This module runs no provider code; the one
provider-specific thing it holds is `_CREDENTIAL_SHAPES`, the list every guide is checked
against, which grows with each provider (see `docs/setup/README.md`).

The guide format is documented for authors in `docs/setup/README.md`. In short,
fenced blocks whose info string carries a `guide:` word are executable:

    ```sh guide:run          one `adhelm ...` command per line, run in order
    ```json guide:file=NAME  a file the reader creates, written to the working directory
    ```json guide:expect     fields the previous command's JSON result must contain

A fenced block that is not marked may not contain an `adhelm` command, so a command a
reader sees is a command CI ran.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import pytest
from typer.testing import CliRunner

from adhelm.cli import app

#: The only commands a setup guide may run. Enrollment, local credential handling and
#: the read commands that prove an enrollment works; nothing that plans, compiles,
#: executes or recovers. `config add-profile` is here because five providers enroll
#: against a profile the reader adds first; it writes local configuration only and takes
#: no credential. `auth reddit-token refresh` is the one refresh a guide runs: it
#: exchanges a stored refresh token at the provider's token endpoint and changes nothing
#: in the ad account. `auth tiktok-token refresh` is the other: it always refuses with
#: exit code 4 and contacts no provider. The set is checked against the installed command
#: list and the release policy in `test_setup_guide_harness.py`, and a guide author adds a
#: command here deliberately, in review.
GUIDE_COMMANDS: frozenset[str] = frozenset(
    {
        "init",
        "schema",
        "doctor",
        "config add-profile",
        "auth store",
        "auth remove-local",
        "auth google-oauth enroll",
        "auth google-oauth status",
        "auth meta-token enroll",
        "auth meta-token inspect",
        "auth meta-token status",
        "auth meta-token remove-local",
        "auth linkedin-token enroll",
        "auth linkedin-token status",
        "auth linkedin-token remove-local",
        "auth reddit-token enroll",
        "auth reddit-token refresh",
        "auth reddit-token status",
        "auth reddit-token remove-local",
        "auth tiktok-token enroll",
        "auth tiktok-token refresh",
        "auth tiktok-token status",
        "auth tiktok-token remove-local",
        "auth openai-key status",
        "inventory",
        "report ga4",
        "report google-ads",
        "report meta-ads",
        "report linkedin-ads",
        "report tiktok-ads",
        "report reddit-ads",
        "report openai-ads",
        "measurement ga4-admin",
        "measurement gtm",
        "history verify",
    }
)

#: Headings every guide carries: the items the release definition requires of a
#: bring-your-own-app guide, plus its sources.
REQUIRED_HEADINGS: tuple[str, ...] = (
    "Create the developer app",
    "Scopes",
    "Redirect and callback",
    "Access tiers and limits",
    "Enroll",
    "Verify",
    "Revoke",
    "Sources",
)

#: Text that must never appear in a guide: the shapes of real credentials, one group per
#: provider plus the generic private key. A guide shows placeholders only (AGENTS.md
#: rule 1). Add a provider's shapes here when its guide is added. TikTok's tokens, codes
#: and secrets carry no distinguishing prefix, so no shape can single them out; its fake
#: supplies the values it hands out through `secrets()` instead, and a guide that
#: printed one would fail there.
_CREDENTIAL_SHAPES = (
    # Generic.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Google.
    re.compile(r"GOCSPX-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"\b1//[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),
    # Meta: user, page and app tokens start with EAA; an app access token is `id|secret`.
    re.compile(r"\bEAA[A-Za-z0-9]{20,}"),
    re.compile(r"\b\d{10,20}\|[A-Za-z0-9_-]{20,}"),
    # LinkedIn: access and refresh tokens and authorization codes start with AQ; a client
    # secret starts with WPL_AP.
    re.compile(r"\bAQ[A-Za-z0-9_-]{60,}"),
    re.compile(r"\bWPL_AP\d\.[A-Za-z0-9]{8,}\.[A-Za-z0-9+/=]{4,}"),
    # Reddit: access and refresh tokens are `<digits>-<token>`. A Google client ID has the
    # same start and is not a credential, so it is left alone.
    re.compile(r"\b\d{6,}-[A-Za-z0-9_-]{20,}\b(?!\.apps\.googleusercontent\.com)"),
    # OpenAI keys start with sk-.
    re.compile(r"\bsk-(?:proj-|admin-|svcacct-)?[A-Za-z0-9_-]{20,}"),
)
_SHELL_OPERATORS = re.compile(r"[;|&<>$`]")
_EXIT_COMMENT = re.compile(r"\s+#\s*exit\s+(\d+)\s*$")
_FENCE = re.compile(r"^(\s*)```(.*)$")
_HEADING = re.compile(r"^## (.+?)\s*$")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_SOURCE_ITEM = re.compile(
    r"^- \[[^\]]+\]\((https://[^)\s]+)\) \(fetched (\d{4}-\d{2}-\d{2})(?:; [^()]+)?\)$"
)
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_OUTPUT_ECHO = 600


class GuideError(AssertionError):
    """A setup guide is unsafe, malformed, stale, or its commands did not behave as shown."""


@dataclass(frozen=True)
class Command:
    line: int
    text: str
    arguments: tuple[str, ...]
    expected_exit: int


@dataclass(frozen=True)
class GuideFile:
    line: int
    name: str
    content: str


@dataclass(frozen=True)
class Expectation:
    line: int
    expected: Any


Step = Command | GuideFile | Expectation


@dataclass(frozen=True)
class Guide:
    steps: tuple[Step, ...]
    sources: tuple[tuple[str, date], ...]

    @property
    def commands(self) -> tuple[Command, ...]:
        return tuple(step for step in self.steps if isinstance(step, Command))


@dataclass(frozen=True)
class CommandResult:
    command: Command
    exit_code: int
    output: str
    envelope: dict[str, Any] | None


class GuideEnvironment(Protocol):
    """What a provider fake supplies to run a guide without leaving the process."""

    #: Files the reader obtains from the provider (for example a downloaded client file),
    #: written private to the current user before the first command.
    fixtures: Mapping[str, str]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None: ...

    def secrets(self) -> tuple[str, ...]: ...

    def problems(self) -> list[str]: ...


def command_path(arguments: tuple[str, ...]) -> str | None:
    """Return the longest allowed command name that starts the arguments."""
    for size in range(min(3, len(arguments)), 0, -1):
        name = " ".join(arguments[:size])
        if name in GUIDE_COMMANDS:
            return name
    return None


def parse_guide(text: str, *, today: date | None = None) -> Guide:
    """Parse and vet a guide; raise `GuideError` naming the first line at fault."""
    lines = text.splitlines()
    _refuse_credentials(lines)
    steps: list[Step] = []
    headings: list[str] = []
    prose: list[str] = []
    index = 0
    while index < len(lines):
        number = index + 1
        line = lines[index]
        fence = _FENCE.match(line)
        if fence is None:
            heading = _HEADING.match(line)
            if heading is not None:
                headings.append(heading.group(1))
            prose.append(line)
            index += 1
            continue
        end = next((n for n in range(index + 1, len(lines)) if lines[n].lstrip() == "```"), None)
        if end is None:
            raise GuideError(f"line {number}: the code fence is never closed")
        words = fence.group(2).split()
        if fence.group(1) and any(word.startswith("guide:") for word in words):
            raise GuideError(f"line {number}: a guide: block starts in the first column")
        steps.extend(_block_steps(number, words, lines[index + 1 : end], steps))
        index = end + 1
    _check_headings(headings)
    sources = _check_sources(prose, today or date.today())
    if not any(isinstance(step, Command) for step in steps):
        raise GuideError("the guide has no `guide:run` command, so nothing proves it works")
    return Guide(tuple(steps), sources)


def _refuse_credentials(lines: list[str]) -> None:
    for number, line in enumerate(lines, start=1):
        if any(shape.search(line) for shape in _CREDENTIAL_SHAPES):
            raise GuideError(f"line {number}: the text has the shape of a real credential")


def _block_steps(number: int, words: list[str], body: list[str], earlier: list[Step]) -> list[Step]:
    marks = [word for word in words if word.startswith("guide:")]
    if len(marks) > 1:
        raise GuideError(f"line {number}: a code fence carries at most one guide: mark")
    if not marks:
        for offset, line in enumerate(body, start=1):
            if re.match(r"\s*adhelm(\s|$)", line):
                raise GuideError(
                    f"line {number + offset}: an adhelm command in a block that is not "
                    "marked guide:run is never executed"
                )
        return []
    mark = marks[0]
    if mark == "guide:run":
        return list(_commands(number, body))
    if mark.startswith("guide:file="):
        name = mark.removeprefix("guide:file=")
        if _FILE_NAME.fullmatch(name) is None:
            raise GuideError(f"line {number}: {name!r} is not a plain file name")
        if any(isinstance(step, GuideFile) and step.name == name for step in earlier):
            raise GuideError(f"line {number}: the guide writes {name} twice")
        return [GuideFile(number, name, "\n".join(body) + "\n")]
    if mark == "guide:expect":
        if not earlier or not isinstance(earlier[-1], Command):
            raise GuideError(f"line {number}: guide:expect must directly follow a guide:run block")
        try:
            expected = json.loads("\n".join(body))
        except ValueError:
            raise GuideError(f"line {number}: the guide:expect block is not valid JSON") from None
        if not isinstance(expected, dict):
            raise GuideError(f"line {number}: the guide:expect block must be a JSON object")
        return [Expectation(number, expected)]
    raise GuideError(f"line {number}: unknown mark {mark!r}")


def _commands(number: int, body: list[str]) -> list[Command]:
    commands: list[Command] = []
    for offset, raw in enumerate(body, start=1):
        line = number + offset
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        expected_exit = 0
        comment = _EXIT_COMMENT.search(text)
        if comment is not None:
            expected_exit = int(comment.group(1))
            text = text[: comment.start()]
        if text.endswith("\\"):
            raise GuideError(
                f"line {line}: put the command on one line; a continuation breaks in PowerShell"
            )
        if _SHELL_OPERATORS.search(text):
            raise GuideError(f"line {line}: a guide command is one adhelm call, not a shell script")
        try:
            tokens = shlex.split(text)
        except ValueError:
            raise GuideError(f"line {line}: the command has unbalanced quotes") from None
        if not tokens or tokens[0] != "adhelm":
            raise GuideError(f"line {line}: only adhelm commands may be run")
        arguments = tuple(tokens[1:])
        if any(a == "--data-dir" or a.startswith("--data-dir=") for a in arguments):
            raise GuideError(f"line {line}: a guide uses the default data directory")
        if command_path(arguments) is None:
            raise GuideError(
                f"line {line}: {' '.join(arguments[:3])!r} is not an allowed guide command"
            )
        commands.append(Command(line, text, arguments, expected_exit))
    return commands


def _check_headings(headings: list[str]) -> None:
    missing = [name for name in REQUIRED_HEADINGS if name not in headings]
    if missing:
        raise GuideError(f"the guide lacks the section(s): {', '.join(missing)}")
    if headings[-1] != "Sources":
        raise GuideError("Sources is the guide's last section")


def _check_sources(prose: list[str], today: date) -> tuple[tuple[str, date], ...]:
    try:
        start = prose.index("## Sources") + 1
    except ValueError:
        raise GuideError("the guide lacks a Sources section") from None
    sources: list[tuple[str, date]] = []
    for line in prose[start:]:
        if line.startswith("## "):
            break
        if not line.startswith("- "):
            if "http" in line:
                raise GuideError(f"Sources text {line!r} names a URL outside a list entry")
            continue
        item = _SOURCE_ITEM.match(line)
        if item is None:
            raise GuideError(
                f"Sources entry {line!r} must read: - [Title](https://...) (fetched YYYY-MM-DD)"
            )
        try:
            fetched = date.fromisoformat(item.group(2))
        except ValueError:
            raise GuideError(f"Sources entry {line!r} has an impossible fetch date") from None
        if fetched > today:
            raise GuideError(f"Sources entry {line!r} is fetched in the future")
        sources.append((item.group(1), fetched))
    if not sources:
        raise GuideError("the Sources section lists no primary source")
    cited = {url for url, _ in sources}
    for line in prose[: start - 1]:
        for target in _LINK.findall(line):
            if target.startswith("http") and target not in cited:
                raise GuideError(f"{target} is linked but not listed in Sources with a fetch date")
    return tuple(sources)


def check_relative_links(text: str, directory: Path) -> None:
    """Raise unless every relative link in the guide names a file that exists."""
    for target in _LINK.findall(text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        if not (directory / target.split("#")[0]).resolve().is_file():
            raise GuideError(f"{target} is linked but does not exist")


def subset_mismatch(expected: Any, actual: Any, path: str = "result") -> str | None:
    """Return a description of the first way `actual` lacks `expected`, or `None`.

    An object matches when every expected key is present and matches; a list matches
    when every expected element matches some element; anything else must be equal.
    """
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return f"{path} is not an object"
        for key, value in expected.items():
            if key not in actual:
                return f"{path}.{key} is missing"
            found = subset_mismatch(value, actual[key], f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return f"{path} is not a list"
        for position, item in enumerate(expected):
            if all(subset_mismatch(item, entry, path) is not None for entry in actual):
                return f"{path}[{position}] matches no element of the result"
        return None
    if type(expected) is not type(actual) or expected != actual:
        return f"{path} is {actual!r}, the guide says {expected!r}"
    return None


def run_guide(
    guide: Guide,
    environment: GuideEnvironment,
    *,
    workdir: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[CommandResult]:
    """Execute the guide's steps in order; raise `GuideError` at the first divergence."""
    environment.install(monkeypatch)
    monkeypatch.chdir(workdir)
    for name, content in environment.fixtures.items():
        path = workdir / name
        path.write_text(content)
        path.chmod(0o600)
    runner = CliRunner()
    results: list[CommandResult] = []
    for step in guide.steps:
        if isinstance(step, GuideFile):
            path = workdir / step.name
            if path.exists():
                raise GuideError(f"line {step.line}: {step.name} already exists in the work area")
            path.write_text(step.content)
        elif isinstance(step, Command):
            result = _run(runner, step, data_dir)
            _check_command(step, result, environment)
            results.append(result)
        else:
            problem = subset_mismatch(step.expected, results[-1].envelope)
            if problem is not None:
                raise GuideError(f"line {step.line}: {problem} after `{results[-1].command.text}`")
    unused = [
        name
        for name in environment.fixtures
        if not any(name in command.arguments for command in guide.commands)
    ]
    if unused:
        raise GuideError(f"the guide never uses the provider file(s): {', '.join(unused)}")
    problems = environment.problems()
    if problems:
        raise GuideError("the fake provider saw: " + "; ".join(problems))
    leaked = secrets_on_disk(data_dir, environment.secrets())
    if leaked:
        raise GuideError("a credential was left in the data directory: " + ", ".join(leaked))
    return results


def _run(runner: CliRunner, command: Command, data_dir: Path) -> CommandResult:
    outcome = runner.invoke(app, ["--data-dir", str(data_dir), *command.arguments])
    try:
        envelope = json.loads(outcome.output)
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict):
        envelope = None
    return CommandResult(command, outcome.exit_code, outcome.output, envelope)


def _check_command(command: Command, result: CommandResult, environment: GuideEnvironment) -> None:
    for secret in environment.secrets():
        if secret in result.output:
            raise GuideError(f"line {command.line}: `{command.text}` printed a credential")
    if result.exit_code != command.expected_exit:
        shown = result.output[:_MAX_OUTPUT_ECHO]
        raise GuideError(
            f"line {command.line}: `{command.text}` exited {result.exit_code}, "
            f"the guide says {command.expected_exit}: {shown}"
        )


def secrets_on_disk(directory: Path, secrets: tuple[str, ...]) -> list[str]:
    """Return the files under `directory` that contain any of `secrets` (names only).

    Paths are relative to `directory`; `run_guide` raises `GuideError` naming them.
    """
    found: list[str] = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            content = path.read_bytes()
            if any(secret.encode() in content for secret in secrets):
                found.append(str(path.relative_to(directory)))
    return found
