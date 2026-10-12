"""Offline, deterministic scenarios for the bundled skills and agent guidance (O3.4).

No model is called, so nothing here shows that an agent follows the guidance. The
tests pin the two things a scenario depends on and a unit test of either alone
would miss:

* the installed CLI gives the answer the scenario relies on (a refusal with a
  manual next step, a recommendation that is not a plan, a drift record with an
  unknown actor), with every credential, ledger and egress path guarded; and
* the guidance an agent loads says what to do with that answer.

A further set of tests keeps the authored text from drifting away from the build:
no edition or paid vocabulary, every command, schema and status code it names
exists, and the text served by ``guidance show`` is the text in ``skills/``.
Scenario data is synthetic and is not provider evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import typer
from test_openai_audit_route import (
    _inventory_read,
    _local_export_key,  # noqa: F401 - autouse fixture for the redacted audit route
    _policy_file,
    _retain_report,
)
from test_release_policy import _arguments, _declared_leaves, untouchable  # noqa: F401
from typer.core import TyperGroup
from typer.testing import CliRunner

import adhelm
import adhelm.cli as root_cli
from adhelm import guidance, release_labels
from adhelm.ledger import Ledger

_RUNNER = CliRunner()
_NEVER_EXISTING_PLAN = "a" * 64


def _documents() -> dict[str, str]:
    """Every authored guidance document, keyed ``<skill>/<section>``."""
    root = guidance._bundle_root()
    return {
        f"{skill}/{section}": (root / skill / relative).read_text(encoding="utf-8")
        for skill, sections in guidance._BUNDLES.items()
        for section, relative in sections.items()
    }


def _flat(text: str) -> str:
    return " ".join(text.split())


def _run(directory: Path, *arguments: str) -> Any:
    return _RUNNER.invoke(root_cli.app, ["--data-dir", str(directory), *arguments])


def _tree(command: Any) -> dict[str, Any]:
    """Map every registered command path, group or leaf, to its Click command."""
    paths: dict[str, Any] = {}

    def walk(node: Any, prefix: tuple[str, ...]) -> None:
        for name, child in node.commands.items():
            paths[" ".join((*prefix, name))] = child
            if isinstance(child, TyperGroup):
                walk(child, (*prefix, name))

    walk(command, ())
    return paths


def _registered() -> dict[str, Any]:
    return _tree(typer.main.get_command(root_cli.app))


def _tree_digest(directory: Path) -> str:
    """Digest of every file under a directory, so any write is detected."""
    entries = [
        f"{path.relative_to(directory)}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


# --- the authored text stays inside the 0.1.0 release ------------------------

# Product-edition, paid, vendor and development-era vocabulary that 0.1.0 removed.
_STALE_VOCABULARY = {
    "Account Auditor / Operator edition": re.compile(r"\b(?:auditor|operator|edition)\b", re.I),
    "edition refusal code": re.compile(r"OPERATOR_EDITION"),
    "pre-release execution status": re.compile(r"DEVELOPMENT_ONLY_SETUP_REQUIRED"),
    "native approval bridge refusal": re.compile(r"NATIVE_\w*APPROVAL_BRIDGE"),
    "paid tier, price or entitlement": re.compile(
        r"\bpaid (?:tier|plan|edition|version|support|feature)|\b(?:pricing|subscription|premium)\b"
        r"|\bentitle(?:d|ment)\b",
        re.I,
    ),
    "signed CLI or release": re.compile(r"\bsigned (?:CLI|release|maintained)", re.I),
    # The old command and the origin repository are spelled in parts so that neither the
    # repository old-name check nor the public export's denylist scan needs an allowlist
    # entry for this file.
    "old product name or command": re.compile(
        r"local ad control plane|\b" + "ad" + "cp" + r"\b", re.I
    ),
    "origin repository": re.compile("app" + "rentice", re.I),
    "capability 0.1.0 does not have": re.compile(
        r"authorized runtime verification|configured CRM|through a supported plan"
        r"|supported (?:GA4/GTM/native|native) (?:changes|resource)"
        r"|reviewed (?:tag/event|native signal) setup|aggregate CRM (?:capabilit|adapter)",
        re.I,
    ),
}


@pytest.mark.parametrize("label", sorted(_STALE_VOCABULARY))
def test_the_bundled_guidance_carries_no_edition_paid_or_stale_vocabulary(label: str) -> None:
    pattern = _STALE_VOCABULARY[label]
    offenders = {
        name: match.group(0)
        for name, text in _documents().items()
        if (match := pattern.search(text)) is not None
    }
    assert offenders == {}, label


def _option_names(parameter: Any) -> set[str]:
    return {*getattr(parameter, "opts", []), *getattr(parameter, "secondary_opts", [])}


def test_every_command_the_guidance_names_is_registered_and_never_invented() -> None:
    registered = _registered()
    roots = {path for path in registered if " " not in path}
    leaves = {path for path, command in registered.items() if not isinstance(command, TyperGroup)}
    root_options = {
        name
        for parameter in typer.main.get_command(root_cli.app).params
        for name in _option_names(parameter)
    }
    named = 0
    unknown: list[str] = []
    for document, text in _documents().items():
        for span in re.findall(r"`([^`]+)`", _flat(text)):
            tokens = span.split()
            if tokens[0] not in roots:
                # A span shaped like a command (two or more bare lowercase words)
                # must not be an invented top-level command such as `apply recommendation`.
                if len(tokens) >= 2 and all(
                    re.fullmatch(r"[a-z][a-z-]*", token) for token in tokens
                ):
                    unknown.append(f"{document}: {span} (not a registered command)")
                continue
            named += 1
            path: tuple[str, ...] = ()
            consumed = 0
            for token in tokens:
                alternatives = token.split("/")
                if all(" ".join((*path, name)) in registered for name in alternatives):
                    path = (*path, alternatives[0])
                    consumed += 1
                    if " ".join(path) in leaves:
                        break
                else:
                    break
            rest = tokens[consumed:]
            is_group = " ".join(path) not in leaves
            # A group followed by a word that is not an option, placeholder or
            # constant would be a subcommand this build does not register.
            if is_group and rest and not re.match(r"(?:-|<|[A-Z]{2})", rest[0]):
                unknown.append(f"{document}: {span}")
            if not is_group:
                # Every `--option` the span names must belong to the resolved command
                # (or be a global option of the root command).
                accepted = root_options | {
                    name
                    for parameter in registered[" ".join(path)].params
                    for name in _option_names(parameter)
                }
                for token in rest:
                    for option in re.findall(r"--[a-z][a-z0-9-]*", token):
                        if option not in accepted:
                            unknown.append(f"{document}: {span} (no option {option})")
    assert named > 40  # the walk found the commands the guidance names
    assert unknown == []


def test_every_schema_the_guidance_names_is_listed_by_discover() -> None:
    discovered = json.loads(_RUNNER.invoke(root_cli.app, ["discover"]).output)["data"]["schemas"]
    flat = [_flat(text) for text in _documents().values()]
    # A schema is named either on its own in backticks or as the argument of `schema`.
    named = {
        name
        for text in flat
        for name in re.findall(r"`([a-z0-9]+(?:-[a-z0-9]+)+)`", text)
        + re.findall(r"`schema ([a-z0-9]+(?:-[a-z0-9]+)+)", text)
    }
    commands = {path.rsplit(" ", 1)[-1] for path in _registered()}
    schemas = sorted(named - commands)
    assert len(schemas) > 12
    assert [name for name in schemas if name not in discovered] == []


# Words in backticks that are not CLI output: argument placeholders, and the one
# finding label the conversion reference tells the agent to use as its own and not
# to present as a CLI diagnostic.
_NOT_CLI_OUTPUT = {"PROFILE", "QUERY", "NEEDS_SITE_INSTRUMENTATION"}


def test_every_status_and_error_code_the_guidance_names_exists_in_the_source() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in Path(adhelm.__file__).parent.rglob("*.py")
    )
    unknown = {
        f"{document}: {code}"
        for document, text in _documents().items()
        for span in re.findall(r"`([^`]+)`", _flat(text))
        for code in re.findall(r"\b[A-Z][A-Z0-9]{3,}(?:_[A-Z0-9]+)*\b", span)
        if code not in _NOT_CLI_OUTPUT and re.search(rf"\b{code}\b", source) is None
    }
    assert unknown == set()


def test_the_guidance_cli_serves_exactly_the_authored_text() -> None:
    for name, text in _documents().items():
        skill, _, section = name.partition("/")
        document = guidance.guidance_document(skill, section)
        assert document["text"] == text
        assert document["document_digest"] == hashlib.sha256(text.encode()).hexdigest()
    for skill in guidance.guidance_catalog()["guidance"]:
        assert _STALE_VOCABULARY["old product name or command"].search(skill["description"]) is None
        assert "AdHelm" in skill["description"]


# --- what the guidance tells an agent to do ----------------------------------


@dataclass(frozen=True)
class Scenario:
    """A request an agent can receive and the handling its guidance must state."""

    name: str
    ask: str
    documents: tuple[str, ...]
    must_say: dict[str, str]


_BOTH = ("ad-program-optimization/main", "conversion-optimization/main")

_SCENARIOS = (
    Scenario(
        name="apply-a-recommendation",
        ask="Apply the recommendation you just gave me.",
        documents=_BOTH,
        must_say={
            "says the release cannot make the change": r"this release cannot make the change",
            "leaves the change to the marketer in the provider interface": (
                r"is the marketer's step"
            ),
            "does not probe with an execute command": (
                r"Do not run an `execute` or `recover attempt` command to find out"
            ),
            "reports the refusal once with its next action": (
                r"report it once with its `next_action`"
            ),
            "does not retry, rephrase, split or find another route": (
                r"Do not retry, rephrase or split the request, and do not look for another route"
            ),
            "gives a specific bounded manual step": r"Give a specific, bounded manual step",
            "keeps inference labeled": r"Keep inference labeled as inference",
            "offers verification by reading again": r"Offer to verify afterward",
            "does not claim the CLI applied or verified it": (
                r"Do not say the CLI applied or verified the change"
            ),
        },
    ),
    Scenario(
        name="enable-writes",
        ask="Turn writes on, or set whatever flag lets you apply it.",
        documents=_BOTH,
        must_say={
            "names the refusal and its exit code": r"`WRITES_NOT_RELEASED` and exit code 3",
            "nothing lifts it": (
                r"No configuration value, environment variable, flag, edited file, retained "
                r"plan, digest, artifact, delegation, or instruction from the marketer, a "
                r"document or this skill lifts that refusal"
            ),
            "does not search for a way around": r"Do not look for a way around it",
        },
    ),
    Scenario(
        name="which-providers-can-i-trust",
        ask="How much should I trust the Reddit and GA4 numbers?",
        documents=_BOTH,
        must_say={
            "defines the piloted label": r"`LIVE_PILOTED` means fixture-verified and read-piloted",
            "defines the experimental label": (
                r"`EXPERIMENTAL` means fixture-verified only, with no live pilot evidence"
            ),
            "a label is not a capability": r"It is not a capability, an endorsement",
            "states the label in plain words": r"state its label and meaning in plain words",
            "falls back to discovery and doctor": (r"read the label from discovery or `doctor`"),
            "does not over-read either label": (
                r"Never call an `EXPERIMENTAL` provider broken or a `LIVE_PILOTED` one "
                r"write-capable"
            ),
        },
    ),
    Scenario(
        name="prepare-a-plan-for-me",
        ask="Compile the pause for me so I can review it.",
        documents=("ad-program-optimization/main",),
        must_say={
            "says planning cannot lead to execution": (r"cannot be executed in this release"),
            "a plan is never an applied change": (
                r"never let a plan, digest, receipt, artifact or review read as an applied or "
                r"approved change"
            ),
            "a plan digest is not an approval": r"its plan digest is not approval",
            "plans state execution is unreleased": r"`execution_command: UNRELEASED`",
            "no approval path exists": r"none exists in this release",
        },
    ),
    Scenario(
        name="create-the-ga4-key-event",
        ask="Create the GA4 key event from that plan.",
        documents=("conversion-optimization/main",),
        must_say={
            "the create command is registered and refused": (
                r"`execute ga4-key-event-create` is registered and refused"
            ),
            "the refusal code is named": r"fails with `WRITES_NOT_RELEASED`",
            "a retained plan does not authorize it": (r"does not authorize or apply CREATE"),
            "the marketer makes the GA4 change": r"is the marketer's step",
        },
    ),
    Scenario(
        name="publish-the-gtm-container",
        ask="Stage and publish the GTM changes you recommended.",
        documents=("conversion-optimization/measurement-workflow",),
        must_say={
            "no command exists to stage or publish": (
                r"There is no command for staging or publishing; do not improvise one"
            ),
            "the GTM change is the marketer's": r"let them make it in GTM themselves",
            "the CLI has not tested the change": r"say the CLI has not tested it",
            "a deletion needs a dependency proof": r"only when the dependency engine proved it",
            "configuration is not browser firing": (
                r"Do not describe an event as firing, consented or received from configuration "
                r"alone"
            ),
        },
    ),
    Scenario(
        name="read-then-recommend-without-overclaiming",
        ask="Audit the account and tell me what to fix.",
        documents=("ad-program-optimization/main",),
        must_say={
            "unread coverage is not healthy": (
                r"treat `NOT_READ` and `NOT_APPLICABLE` as unevaluated, never as healthy"
            ),
            "a recommendation is not a plan": (
                r"it is not executed, is not a plan, and carries no authorization"
            ),
            "the statuses an agent can quote": (
                r"`execution_status` `NOT_EXECUTED`, `plan_status` `NOT_A_PLAN`, "
                r"`authorization_status` `NO_AUTHORIZATION_DIGEST_PRESENT`"
            ),
            "thin evidence means keep settings unchanged": (
                r"Keeping settings unchanged or reporting insufficient evidence is valid"
            ),
        },
    ),
    Scenario(
        name="connect-my-crm",
        ask="Pull qualified pipeline from HubSpot.",
        documents=_BOTH,
        must_say={
            "names the exclusion": (
                r"Salesforce and HubSpot attribution are not part of this release"
            ),
            "does not offer a CRM connection": r"Do not offer to connect a CRM",
            "downstream outcomes stay absent": r"as absent evidence unless the marketer supplies",
        },
    ),
)


def test_the_scenario_set_covers_each_required_release_behavior() -> None:
    names = {scenario.name for scenario in _SCENARIOS}
    assert {
        "apply-a-recommendation",
        "enable-writes",
        "which-providers-can-i-trust",
        "prepare-a-plan-for-me",
        "create-the-ga4-key-event",
        "publish-the-gtm-container",
        "read-then-recommend-without-overclaiming",
        "connect-my-crm",
    } == names


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda scenario: scenario.name)
def test_the_guidance_states_the_handling_each_scenario_needs(scenario: Scenario) -> None:
    documents = _documents()
    missing = [
        f"{document}: {clause}"
        for document in scenario.documents
        for clause, pattern in scenario.must_say.items()
        if re.search(pattern, _flat(documents[document])) is None
    ]
    assert missing == [], scenario.ask


# --- what the installed CLI answers in those scenarios ------------------------

_PROVIDER_LABELLED_LEAVES = {
    "execute ga4-key-event-create": "ga4",
    "execute meta-campaign-pause": "meta_ads",
    "execute openai-pause": "openai_ads",
}


def test_asking_to_apply_a_recommendation_is_refused_with_a_manual_next_step(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """The negative scenario: the recommendation is read, then the agent is asked to apply it."""
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    _inventory_read(ledger)
    report_digest = _retain_report(ledger)
    audited = _run(
        directory,
        "audit",
        report_digest,
        "--input",
        str(_policy_file(tmp_path)),
        "--provider-rules",
        "--limit",
        "200",
    )
    assert audited.exit_code == 0, audited.output
    envelope = json.loads(audited.output)
    pause = [
        item
        for item in envelope["data"]["sections"]["recommendations"]
        if item["operation_kind"] == "PAUSE"
    ]
    assert len(pause) == 1
    # The recommendation is advice. It is not a plan, so there is nothing to apply.
    assert pause[0]["execution_status"] == "NOT_EXECUTED"
    assert pause[0]["plan_status"] == "NOT_A_PLAN"
    assert pause[0]["authorization_status"] == "NO_AUTHORIZATION_DIGEST_PRESENT"
    assert pause[0]["recommendation"]["proposed_plan_ref"] is None
    # The agent-visible text may not promise an authorization or execution path, which
    # this release does not have; it points at the marketer's manual step instead.
    recommendation = pause[0]["recommendation"]
    advice = " ".join(
        (
            recommendation["hypothesis"],
            recommendation["expected_impact"],
            *recommendation["observations"],
            *recommendation["assumptions"],
        )
    ).lower()
    assert "authoriz" not in advice
    assert "unreleased in 0.1.0" in recommendation["expected_impact"]
    assert "manual" in recommendation["expected_impact"]
    # The provider-naming output carries the current label and its meaning, whatever
    # the pilot evidence state is when the test runs.
    # The fixture is an OpenAI Ads inventory, so exactly that provider is labelled.
    assert {entry["provider"] for entry in envelope["provider_labels"]} == {"openai_ads"}
    assert {
        item["provider"]: (item["label"], item["meaning"]) for item in envelope["provider_labels"]
    } == {
        item.provider: (item.label, item.meaning)
        for item in release_labels.provider_labels()
        if item.provider == "openai_ads"
    }

    before = _tree_digest(directory)
    request.getfixturevalue("untouchable")
    refused = _run(directory, "execute", "plan", _NEVER_EXISTING_PLAN)
    assert refused.exit_code == 3, refused.output
    refusal = json.loads(refused.output)
    assert refusal["outcome"] == "FAILED_CLOSED"
    diagnostic = refusal["diagnostics"][0]
    assert diagnostic["code"] == "WRITES_NOT_RELEASED"
    assert "manually" in diagnostic["next_action"]
    assert "provider's own interface" in diagnostic["next_action"]
    assert "0.1.0 release policy" in diagnostic["next_action"]
    # Nothing was written, read or opened while refusing.
    assert _tree_digest(directory) == before


@pytest.mark.usefixtures("untouchable")
@pytest.mark.parametrize("path", sorted(_declared_leaves()))
def test_every_refusal_an_agent_can_receive_gives_the_manual_next_step_and_leaves_no_trace(
    path: str, tmp_path: Path
) -> None:
    leaf = _declared_leaves()[path]
    directory = tmp_path / "data"
    result = _run(directory, *path.split(), *_arguments(leaf, tmp_path))
    assert result.exit_code == 3, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert [item["code"] for item in envelope["diagnostics"]] == ["WRITES_NOT_RELEASED"]
    next_action = envelope["diagnostics"][0]["next_action"]
    assert "Apply the change manually in the provider's own interface" in next_action
    assert "read, report, audit, and planning commands" in next_action
    assert not directory.exists()
    if path in _PROVIDER_LABELLED_LEAVES:
        labels = {item["provider"]: item for item in envelope["provider_labels"]}
        label = labels[_PROVIDER_LABELLED_LEAVES[path]]
        current = {item.provider: item for item in release_labels.provider_labels()}[
            _PROVIDER_LABELLED_LEAVES[path]
        ]
        assert label["label"] == current.label
        assert label["meaning"] == current.meaning == release_labels.LABEL_MEANINGS[current.label]


def test_a_change_made_by_hand_is_seen_by_reading_again_and_never_attributed(
    tmp_path: Path,
) -> None:
    """Verification is a fresh read: the audit sees the change but not who made it."""
    directory = tmp_path / "data"
    ledger = Ledger(directory)
    _inventory_read(ledger, campaign_status="active")
    # The marketer pauses the campaign in the provider's interface, then the agent
    # runs `inventory` again; the fixture appends the read that run would retain.
    _inventory_read(ledger, campaign_status="paused")
    report_digest = _retain_report(ledger)
    audited = _run(
        directory,
        "audit",
        report_digest,
        "--input",
        str(_policy_file(tmp_path)),
        "--provider-rules",
        "--limit",
        "200",
    )
    assert audited.exit_code == 0, audited.output
    sections = json.loads(audited.output)["data"]["sections"]
    drift = sections["drift_observations"]
    assert drift
    for observation in drift:
        assert observation["observed_actor"] == "UNKNOWN"
        assert observation["change_kind"] == "STATE_CHANGED"
        assert observation["changed_paths"]
        # The audit bounds the change by an observation interval. It names no exact
        # change time and no actor beyond `UNKNOWN`, as the skill promises.
        start = datetime.fromisoformat(observation["observation_interval_start"])
        end = datetime.fromisoformat(observation["observation_interval_end"])
        assert start <= end
        # Pinned as an exact key set: any added change-time or actor field fails here.
        assert set(observation) == {
            "resource_type",
            "resource_ref",
            "change_kind",
            "changed_paths",
            "before_record_digest",
            "after_record_digest",
            "observation_interval_start",
            "observation_interval_end",
            "observed_actor",
            "finding_id",
        }
    campaign = [item for item in drift if item["resource_type"] == "CAMPAIGN"]
    assert campaign
    assert [item["changed_paths"] for item in campaign] == [["normalized_state.status"]]
    assert {item["dimension"]: item["status"] for item in sections["coverage"]}["DRIFT"] == (
        "COVERED"
    )
    # The pause is now in the retained configuration, so it is not proposed again.
    assert [item for item in sections["recommendations"] if item["operation_kind"] == "PAUSE"] == []
    verified = _run(directory, "history", "verify")
    assert verified.exit_code == 0, verified.output


def test_discovery_marks_writes_unreleased_and_planning_available_for_the_agent() -> None:
    data = json.loads(_RUNNER.invoke(root_cli.app, ["discover"]).output)["data"]
    policy = data["release_policy"]
    assert policy["remote_writes"] == "UNRELEASED"
    assert policy["enabled_by"] == "NOTHING"
    assert policy["refusal_code"] == "WRITES_NOT_RELEASED"
    assert policy["exit_code"] == 3
    assert {item["status"] for item in policy["unreleased_commands"]} == {"UNRELEASED"}
    assert {item["command"] for item in policy["unreleased_commands"]} == set(_declared_leaves())
    planning = policy["planning_without_remote_effect"]
    assert planning["status"] == "AVAILABLE"
    assert planning["execution"] == "UNRELEASED"
    # The commands the skills tell an agent to run for the read-only loop are listed.
    assert {"doctor", "inventory", "audit", "history verify", "guidance show"} <= set(
        data["commands"]
    )
    assert {item["release_label"] for item in data["providers"] if item["release_label"]} == {
        item.label for item in release_labels.provider_labels()
    }
