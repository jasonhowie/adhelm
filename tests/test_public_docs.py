"""The public root documents are linked, consistent with the code, and free of retired text.

O4.2c writes the documents a stranger reads first: the README, changelog, contribution
and security policies, the issue templates, the public `AGENTS.md`, and the rewritten
distribution text of the product brief and definition of done. A document that drifts
from the code it describes misleads exactly that reader, so these tests tie each claim to
the thing that decides it: the release definition, the command registry, the provider
labels, the CI workflow and the requirement index.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shlex
import tomllib
from collections.abc import Callable, Iterable
from fnmatch import fnmatchcase
from functools import partial
from pathlib import Path
from types import ModuleType

import pytest
from test_pilot_records import sealed_record
from typer.testing import CliRunner

from adhelm import __version__
from adhelm.cli import app
from adhelm.errors import AdhelmError
from adhelm.pilot_records import RELEASE_PROVIDERS, pilot_record_bytes
from adhelm.providers import google_ads, linkedin_ads, meta_ads, openai_ads, reddit_ads, tiktok_ads
from adhelm.release_labels import ProviderLabel, pilot_record_inventory, provider_labels

_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = CliRunner()

PUBLIC_DOCUMENTS = (
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "AGENTS.md",
    "docs/product-brief.md",
    "docs/definition-of-done.md",
    "docs/traceability-matrix.md",
)
#: What the public tree omits (`docs/implementation/oss-0.1.0-plan.md`, "Public tree
#: manifest"). A public document may not link to any of it.
OMITTED_FROM_PUBLIC_TREE = (
    "BUILD_PROMPT.md",
    "docs/implementation/oss-0.1.0-plan.md",
    "docs/implementation/terra-*",
    "docs/implementation/finishing-*",
    "docs/implementation/status.md",
    "docs/implementation/own-account-read-pilot.md",
    "docs/implementation/pilot-reconciliation-handoff.md",
    "docs/implementation/*-validation*.json",
    "docs/implementation/public-tree-export.md",
    "docs/adr/0009-editions.md",
    "docs/edition-manifest.md",
    "docs/distribution-model.md",
    "backlog/phase-2-vendor-hosted-mcp.md",
    "docs/research/provider-access-2026-09-04.md",
    "docs/research/f03-*",
    "docs/research/hubspot-*",
    "docs/research/salesforce-*",
)

#: Provider setup guides that have not landed yet. A link to a pending guide is exempt from
#: the link check, so the set must stay exact: `test_the_pending_set_is_exactly_the_absent_guides`
#: fails when a pending guide exists or an absent guide is not listed. O4.2c removed
#: O4.2a's three entries (google_ads, ga4, gtm) when it integrated after O4.2a, and O4.2b
#: removed the remaining five (meta_ads, linkedin_ads, tiktok_ads, reddit_ads, openai_ads)
#: when it added their guides. All eight guides exist, so nothing is pending.
_PENDING_SETUP_GUIDES: frozenset[str] = frozenset()


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", _ROOT / f"scripts/{name}.py"
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


# --- Markdown link checking -----------------------------------------------------------

_links = _script("check_markdown_links")
_EXTERNAL = _links.EXTERNAL
_FENCE = _links.FENCE
_link_targets = _links.link_targets
broken_links = partial(
    _links.broken_links, omitted=OMITTED_FROM_PUBLIC_TREE, pending=_PENDING_SETUP_GUIDES
)


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _linked_documents(root: Path, documents: Iterable[str]) -> list[str]:
    """The Markdown files the given documents link to that the public tree ships."""
    base = root.resolve()
    linked: set[str] = set()
    for relative in documents:
        source = base / relative
        for target in _link_targets(source.read_text(encoding="utf-8")):
            path_part = target.partition("#")[0]
            if _EXTERNAL.match(target) or not path_part:
                continue
            resolved = (source.parent / path_part).resolve()
            if resolved.is_dir():
                resolved = resolved / "README.md"
            if resolved.suffix != ".md" or not resolved.is_file() or base not in resolved.parents:
                continue
            destination = resolved.relative_to(base).as_posix()
            if not any(fnmatchcase(destination, pattern) for pattern in OMITTED_FROM_PUBLIC_TREE):
                linked.add(destination)
    return sorted(linked - set(documents))


def test_the_public_documents_link_only_to_files_the_public_tree_ships() -> None:
    assert broken_links(_ROOT, PUBLIC_DOCUMENTS) == []


def test_every_document_the_public_documents_link_to_links_only_to_shipped_files() -> None:
    """A reader who follows a link from the README or `AGENTS.md` must not reach a dead one.

    The whole-tree link check belongs to the export (O5); this checks one hop from the
    root documents.
    """
    linked = _linked_documents(_ROOT, PUBLIC_DOCUMENTS)
    assert "docs/release-0.1.0.md" in linked and "docs/capability-matrix.md" in linked
    assert broken_links(_ROOT, linked) == []


def test_the_directory_a_public_document_links_to_is_checked_through_its_readme() -> None:
    assert "docs/adr/README.md" in _linked_documents(_ROOT, PUBLIC_DOCUMENTS)


def test_a_link_to_a_missing_file_or_heading_is_a_finding(tmp_path: Path) -> None:
    _write(tmp_path, "docs/there.md", "# Title\n\n## A second heading\n")
    _write(
        tmp_path,
        "README.md",
        "[ok](docs/there.md#a-second-heading) [gone](docs/gone.md) [bad](docs/there.md#nope)\n"
        "[same page](#missing-section)\n",
    )
    assert broken_links(tmp_path, ["README.md"]) == [
        "README.md: docs/gone.md: missing",
        "README.md: docs/there.md#nope: no such heading",
        "README.md: #missing-section: no such heading",
    ]


def test_reference_style_links_are_checked(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "[doc][ref]\n\n[ref]: docs/gone.md\n")
    assert broken_links(tmp_path, ["README.md"]) == ["README.md: docs/gone.md: missing"]


def test_a_link_out_of_the_tree_is_a_finding_even_when_the_file_exists(tmp_path: Path) -> None:
    _write(tmp_path, "outside.md", "# Outside\n")
    _write(tmp_path / "repo", "README.md", "[up](../outside.md)\n")
    assert broken_links(tmp_path / "repo", ["README.md"]) == [
        "README.md: ../outside.md: leaves the repository"
    ]


def test_a_link_to_a_file_the_public_tree_omits_is_a_finding(tmp_path: Path) -> None:
    _write(tmp_path, "docs/distribution-model.md", "# Pricing\n")
    _write(tmp_path, "docs/implementation/finishing-plan.md", "# Plan\n")
    _write(
        tmp_path,
        "README.md",
        "[a](docs/distribution-model.md) [b](docs/implementation/finishing-plan.md)\n",
    )
    assert broken_links(tmp_path, ["README.md"]) == [
        "README.md: docs/distribution-model.md: omitted from the public tree",
        "README.md: docs/implementation/finishing-plan.md: omitted from the public tree",
    ]


def test_external_links_code_spans_and_fenced_examples_are_not_checked(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "README.md",
        "[web](https://example.test/x) <mailto:a@example.test>\n"
        "`[inline](gone.md)`\n\n```md\n[fenced](gone.md)\n# Not a heading\n```\n"
        "[the fence](#not-a-heading)\n",
    )
    assert broken_links(tmp_path, ["README.md"]) == ["README.md: #not-a-heading: no such heading"]


def test_repeated_headings_get_the_numbered_anchors_github_gives_them(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "# One\n\n# One\n\n# What 0.1.0 does `not` do\n")
    _write(
        tmp_path,
        "README.md",
        "[1](a.md#one) [2](a.md#one-1) [3](a.md#one-2) [4](a.md#what-010-does-not-do)\n",
    )
    assert broken_links(tmp_path, ["README.md"]) == ["README.md: a.md#one-2: no such heading"]


def test_only_the_named_pending_setup_guides_may_be_missing(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "[g](docs/setup/ga4.md) [h](docs/setup/gtm.md)\n")
    pending = {"docs/setup/ga4.md", "docs/setup/gtm.md"}
    assert broken_links(tmp_path, ["README.md"], pending=pending) == []
    # One guide lands while its entry is still pending: the link resolves and passes.
    _write(tmp_path, "docs/setup/ga4.md", "# GA4\n")
    assert broken_links(tmp_path, ["README.md"], pending=pending) == []
    # An entry removed while its guide is absent is a finding, so is any unlisted guide.
    assert broken_links(tmp_path, ["README.md"], pending={"docs/setup/ga4.md"}) == [
        "README.md: docs/setup/gtm.md: missing"
    ]
    assert broken_links(tmp_path, ["README.md"], pending=()) == [
        "README.md: docs/setup/gtm.md: missing"
    ]


def test_the_pending_set_is_exactly_the_absent_guides() -> None:
    """Every absent release guide is pending and every pending guide is absent.

    The pending set shrinks as O4.2b lands guides; no state of it may exempt a guide that
    exists or leave an absent guide to fail the link check unlisted.
    """
    pending = {
        name.removeprefix("docs/setup/").removesuffix(".md") for name in _PENDING_SETUP_GUIDES
    }
    assert pending <= set(RELEASE_PROVIDERS), "pending names must be release providers"
    absent = {p for p in RELEASE_PROVIDERS if not (_ROOT / f"docs/setup/{p}.md").exists()}
    assert absent == pending, (
        f"absent but not pending: {sorted(absent - pending)}; "
        f"pending but present (delete the entry): {sorted(pending - absent)}"
    )


# --- Release claim and provider table ------------------------------------------------


def _section(text: str, heading: str) -> str:
    start = re.search(rf"(?m)^(#+) {re.escape(heading)}\s*$", text)
    assert start is not None, heading
    following = re.search(rf"(?m)^#{{1,{len(start[1])}}} ", text[start.end() :])
    return text[start.end() : start.end() + following.start()] if following else text[start.end() :]


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _quote(section: str) -> str:
    return _words(" ".join(line[1:] for line in section.splitlines() if line.startswith(">")))


def test_the_readme_opens_with_the_release_claim_word_for_word() -> None:
    claim = _quote(_section((_ROOT / "docs/release-0.1.0.md").read_text(), "Release claim"))
    readme = (_ROOT / "README.md").read_text()
    assert claim.startswith("AdHelm 0.1.0 lets a marketer")
    assert _quote(readme.split("\n## ", 1)[0]) == claim


_PROVIDER_NAMES = {
    "google_ads": "Google Ads",
    "meta_ads": "Meta Ads",
    "linkedin_ads": "LinkedIn Ads",
    "tiktok_ads": "TikTok Ads",
    "reddit_ads": "Reddit Ads",
    "openai_ads": "OpenAI Ads",
    "ga4": "GA4",
    "gtm": "GTM",
}


def _readme_provider_rows(readme: str) -> list[tuple[str, str, str]]:
    """List every provider row of the README table as (name, label, setup guide path).

    A list rather than a mapping, so a provider row that appears twice stays visible. The
    table is the block from the `| Provider |` header to the first blank line, and every line
    of it is parsed after stripping, so a row GitHub renders but that has no leading pipe or
    is indented up to three spaces is a row here too, never skipped. A line outside the block
    that opens with a pipe is refused, so a second table cannot hold a claim unseen.
    """
    lines = _section(readme, "Providers").splitlines()
    headers = [n for n, line in enumerate(lines) if re.match(r"\|\s*Provider\s*\|", line.strip())]
    assert len(headers) == 1, "the Providers section needs exactly one `| Provider |` table"
    block: list[str] = []
    for line in lines[headers[0] :]:
        if not line.strip():
            break
        block.append(line.strip())
    outside = lines[: headers[0]] + lines[headers[0] + len(block) :]
    assert [line for line in outside if line.strip().startswith("|")] == []
    rows: list[tuple[str, str, str]] = []
    for line in block:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] == "Provider" or all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        assert len(cells) == 4, line
        guide = re.search(r"\((docs/setup/[^)]+)\)", cells[-1])
        assert guide is not None, line
        rows.append((cells[0], cells[1].strip("`"), guide[1]))
    return rows


def _expected_readme_rows() -> list[tuple[str, str, str]]:
    return sorted(
        (_PROVIDER_NAMES[item.provider], item.label, f"docs/setup/{item.provider}.md")
        for item in provider_labels()
    )


def test_the_readme_provider_table_has_every_released_provider_with_its_computed_label(
    committed_pilot_records: Path,
) -> None:
    assert set(_PROVIDER_NAMES) == set(RELEASE_PROVIDERS)
    readme = (_ROOT / "README.md").read_text()
    assert sorted(_readme_provider_rows(readme)) == _expected_readme_rows()


def test_a_hand_edited_label_or_a_dropped_or_repeated_provider_no_longer_matches_the_labels(
    committed_pilot_records: Path,
) -> None:
    readme = (_ROOT / "README.md").read_text()
    expected = _expected_readme_rows()
    rows = _readme_provider_rows(readme)
    assert sorted(rows) == expected
    # The row is taken from the committed table, so the test holds whichever providers a
    # committed pilot record has promoted.
    name, label, _guide = rows[0]
    flipped = "LIVE_PILOTED" if label == "EXPERIMENTAL" else "EXPERIMENTAL"
    row = next(line for line in readme.splitlines() if line.startswith(f"| {name} | `{label}` |"))
    flipped_row = row.replace(f"`{label}`", f"`{flipped}`", 1)
    assert readme.count(row) == 1
    variants = {
        # A README that claims the other label than the one the records give the provider.
        "flipped": readme.replace(row, flipped_row),
        # A README that omits a provider.
        "dropped": readme.replace(row + "\n", ""),
        # A README that repeats a provider row, with its own label or with the other one,
        # above or below the real row.
        "repeated": readme.replace(row, f"{row}\n{row}"),
        "repeated_flipped_above": readme.replace(row, f"{flipped_row}\n{row}"),
        "repeated_flipped_below": readme.replace(row, f"{row}\n{flipped_row}"),
        # GitHub renders an indented row, or one without the outer pipes, as a table row.
        "indented_flipped": readme.replace(row, f" {flipped_row}\n{row}"),
        "indented_three_flipped": readme.replace(row, f"   {flipped_row}\n{row}"),
        "no_leading_pipe_flipped": readme.replace(row, f"{flipped_row[1:].lstrip()}\n{row}"),
        "no_outer_pipes_flipped": readme.replace(row, f"{flipped_row.strip('|').strip()}\n{row}"),
    }
    assert len({*variants.values(), readme}) == len(variants) + 1
    for case, edited in variants.items():
        assert sorted(_readme_provider_rows(edited)) != expected, case


# --- The currency limitation matches the adapters ------------------------------------


def _currency_bullet(text: str) -> str:
    """The published statement of which currencies the provider reads accept."""
    matches = re.findall(
        r"(?:^|\n)(?:- \**)?(Money is read in a few currencies only\..*?)(?=\n- |\n\n|$)",
        text,
        re.S,
    )
    assert len(matches) == 1
    return _words(matches[0])


def _codes(statement: str, provider: str) -> list[str]:
    """The currency codes the statement lists after naming `provider`."""
    match = re.search(rf"{provider} accepts ((?:[A-Z]{{3}}(?:, and |, | and )?)+)", statement)
    assert match is not None, provider
    return re.findall(r"[A-Z]{3}", match[1])


def _refuses(call: Callable[[], object]) -> bool:
    try:
        call()
    except AdhelmError as error:
        return error.code == "UNSUPPORTED_CURRENCY"
    return False


@pytest.mark.parametrize("document", ["README.md", "CHANGELOG.md"])
def test_the_published_currency_limitation_matches_what_each_adapter_accepts(
    document: str,
) -> None:
    statement = _currency_bullet((_ROOT / document).read_text())
    assert _codes(statement, "Meta Ads") == sorted(meta_ads._CURRENCY_EXPONENTS)
    assert _codes(statement, "TikTok Ads") == sorted(tiktok_ads._CURRENCY_EXPONENTS)
    for code in sorted(meta_ads._CURRENCY_EXPONENTS):
        assert meta_ads._currency(code) == code
    for code in sorted(tiktok_ads._CURRENCY_EXPONENTS):
        assert tiktok_ads._currency(code) == code
    assert _refuses(lambda: meta_ads._currency("ZAR"))
    assert _refuses(lambda: tiktok_ads._currency("CHF"))
    assert (
        "Google Ads, LinkedIn Ads, Reddit Ads and OpenAI Ads project money exactly only for USD"
        in statement
    )
    assert "`UNSUPPORTED_CURRENCY`" in statement


@pytest.mark.parametrize("currency", ["EUR", "GBP", "JPY", "CAD"])
def test_the_providers_documented_as_usd_only_refuse_other_currencies(currency: str) -> None:
    refusals = {
        "google_ads": lambda: google_ads._exact_micros("10000", currency, "budget"),
        "linkedin_ads": lambda: linkedin_ads._money("1.00", currency, "spend"),
        "reddit_ads": lambda: reddit_ads._currency(currency),
        "openai_ads": lambda: openai_ads._minor_units("1.00", currency, "spend"),
    }
    assert {name for name, call in refusals.items() if not _refuses(call)} == set()


def test_the_providers_documented_as_usd_only_accept_usd() -> None:
    assert google_ads._exact_micros("10000", "USD", "budget") is not None
    assert linkedin_ads._money("1.00", "USD", "spend")[0].currency == "USD"
    assert reddit_ads._currency("USD") == "USD"
    assert openai_ads._minor_units("1.00", "USD", "spend").currency == "USD"


# --- Commands the documents name exist -----------------------------------------------


def _registered_commands(tmp_path: Path) -> set[str]:
    result = _RUNNER.invoke(app, ["--data-dir", str(tmp_path / "data"), "discover"])
    commands = json.loads(result.output)["data"]["commands"]
    return {re.sub(r"\s+-.*$", "", command) for command in commands}


def _command_words(line: str) -> str:
    """The command path of an `adhelm ...` invocation, without global options or flags."""
    words = shlex.split(line)[1:]
    path: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word == "--data-dir":
            skip = True
        elif word.startswith(("-", "<")):
            break
        else:
            path.append(word)
    return " ".join(path)


def _named_commands(text: str) -> list[str]:
    fenced: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        elif in_fence and line.startswith("adhelm "):
            fenced.append(_command_words(line))
    inline = [_command_words(match[1]) for match in re.finditer(r"`(adhelm [a-z][^`]*)`", text)]
    return [*fenced, *inline]


@pytest.mark.parametrize("document", PUBLIC_DOCUMENTS)
def test_every_command_a_public_document_names_is_registered(document: str, tmp_path: Path) -> None:
    registered = _registered_commands(tmp_path)
    named = _named_commands((_ROOT / document).read_text())
    assert [path for path in named if path not in registered] == []


def test_the_readme_first_run_commands_are_the_ones_it_promises() -> None:
    assert _named_commands((_ROOT / "README.md").read_text())[:5] == [
        "version",
        "init",
        "doctor",
        "discover",
        "guidance catalog",
    ]


def test_a_command_that_is_not_registered_is_caught(tmp_path: Path) -> None:
    registered = _registered_commands(tmp_path)
    named = _named_commands("```sh\nadhelm apply --provider google_ads\n```\nRun `adhelm sync`.\n")
    assert named == ["apply", "sync"]
    assert all(path not in registered for path in named)


# --- Validation commands agree with CI -----------------------------------------------


def _ci_commands() -> list[str]:
    workflow = (_ROOT / ".github/workflows/ci.yml").read_text()
    offline = workflow.split("\n  offline:\n", 1)[1].split("\n  native-macos-offline:", 1)[0]
    return re.findall(r"(?m)^ {6}- run: (.+)$", offline)


def _fenced_uv_commands(text: str) -> list[str]:
    commands: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        elif in_fence and line.startswith("uv "):
            commands.append(line)
    return commands


@pytest.mark.parametrize("document", ["AGENTS.md", "CONTRIBUTING.md"])
def test_the_validation_commands_are_the_ones_ci_runs(document: str) -> None:
    ci = _ci_commands()
    assert ci[0] == "uv sync --frozen --python 3.12.13"
    assert "uv run python scripts/check_repository.py" in ci
    assert _fenced_uv_commands((_ROOT / document).read_text()) == ci


# --- Changelog ------------------------------------------------------------------------


def test_the_changelog_has_a_0_1_0_entry_for_the_version_the_project_declares() -> None:
    declared = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert declared == __version__
    changelog = (_ROOT / "CHANGELOG.md").read_text()
    assert re.search(rf"(?m)^## \[{re.escape(declared)}\] - ", changelog)


def _changelog_entry(changelog: str) -> str:
    """Return the 0.1.0 entry, whether it is still `Unreleased` or already dated."""
    heading = re.search(r"(?m)^## (\[0\.1\.0\] - .*?)\s*$", changelog)
    assert heading is not None, "CHANGELOG.md has no 0.1.0 entry"
    return _section(changelog, heading[1])


def test_the_changelog_entry_states_the_scope_the_labels_and_every_published_limitation() -> None:
    entry = _changelog_entry((_ROOT / "CHANGELOG.md").read_text())
    assert "read-only" in entry
    assert "`LIVE_PILOTED`" in entry and "`EXPERIMENTAL`" in entry
    # Keep a Changelog reserves "Security" for vulnerability fixes; the write gate and the
    # no-telemetry runtime are added properties.
    assert "### Security" not in entry
    assert "`WRITES_NOT_RELEASED`" in _section(entry, "Added")
    limitations = _section(
        (_ROOT / "docs/release-0.1.0.md").read_text(), "Known limitations to publish"
    )
    published = [_words(item) for item in re.split(r"(?m)^- ", limitations)[1:]]
    assert len(published) == 6
    entry_limitations = _words(_section(entry, "Known limitations"))
    assert [item for item in published if item not in entry_limitations] == []


_CHANGELOG_ROW = re.compile(r"- (.+): `([A-Z_]+)`\s*")
_LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])(?:\s|$)")


def _changelog_label_rows(changelog: str) -> list[tuple[str, str]]:
    """Return each (provider name, label) bullet of the 0.1.0 provider labels list, in order.

    Every list item of the section must be a row in the canonical form: a `*` or `+` bullet,
    an indented or numbered item, or a malformed row raises instead of being dropped.
    """
    section = _section(_changelog_entry(changelog), "Provider labels in this release")
    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        if not _LIST_ITEM.match(line):
            continue
        row = _CHANGELOG_ROW.fullmatch(line)
        assert row is not None, f"not a provider label row: {line!r}"
        rows.append((row[1], row[2]))
    return rows


def _expected_label_rows(labels: tuple[ProviderLabel, ...]) -> list[tuple[str, str]]:
    return [(_PROVIDER_NAMES[item.provider], item.label) for item in labels]


def test_the_changelog_lists_the_computed_label_of_every_released_provider(
    committed_pilot_records: Path,
) -> None:
    changelog = (_ROOT / "CHANGELOG.md").read_text()
    assert sorted(_changelog_label_rows(changelog)) == sorted(
        _expected_label_rows(provider_labels())
    )
    meaning = _words(_section(_changelog_entry(changelog), "Provider labels in this release"))
    assert "`EXPERIMENTAL` means the provider is fixture-verified only" in meaning
    assert "has no live evidence" in meaning
    assert "`LIVE_PILOTED` means it is also read-piloted on a live account" in meaning


def test_a_missing_mislabelled_or_repeated_provider_in_the_changelog_no_longer_matches(
    committed_pilot_records: Path,
) -> None:
    changelog = (_ROOT / "CHANGELOG.md").read_text()
    expected = sorted(_expected_label_rows(provider_labels()))
    # The row is taken from the committed list, so the test holds whichever providers a
    # committed pilot record has promoted.
    name, label = _changelog_label_rows(changelog)[0]
    flipped = "LIVE_PILOTED" if label == "EXPERIMENTAL" else "EXPERIMENTAL"
    row = f"- {name}: `{label}`\n"
    assert changelog.count(row) == 1
    omitted = changelog.replace(row, "")
    repeated = changelog.replace(row, row * 2)
    promoted = changelog.replace(row, f"- {name}: `{flipped}`\n")
    assert len({omitted, repeated, promoted, changelog}) == 4
    for edited in (omitted, repeated, promoted):
        assert sorted(_changelog_label_rows(edited)) != expected


def test_a_malformed_provider_row_in_the_changelog_is_refused_not_skipped(
    committed_pilot_records: Path,
) -> None:
    changelog = (_ROOT / "CHANGELOG.md").read_text()
    name, label = _changelog_label_rows(changelog)[0]
    flipped = "LIVE_PILOTED" if label == "EXPERIMENTAL" else "EXPERIMENTAL"
    row = f"- {name}: `{label}`\n"
    assert changelog.count(row) == 1
    # Each form renders as a list item, so a false label written that way is a claim.
    malformed = {
        "asterisk": f"* {name}: `{flipped}`\n",
        "plus": f"+ {name}: `{flipped}`\n",
        "indented": f"  - {name}: `{flipped}`\n",
        "numbered": f"1. {name}: `{flipped}`\n",
        "no_colon": f"- {name} `{flipped}`\n",
        "lower_case_label": f"- {name}: `{flipped.lower()}`\n",
        "trailing_text": f"- {name}: `{flipped}` (piloted)\n",
    }
    for case, bullet in malformed.items():
        for edited in (changelog.replace(row, bullet + row), changelog.replace(row, row + bullet)):
            with pytest.raises(AssertionError, match="not a provider label row"):
                _changelog_label_rows(edited)
            assert edited != changelog, case
    # Trailing whitespace on a canonical row is harmless.
    padded = changelog.replace(row, row.rstrip("\n") + "  \n")
    assert sorted(_changelog_label_rows(padded)) == sorted(_changelog_label_rows(changelog))


def test_a_provider_row_outside_the_readme_table_block_is_refused(
    committed_pilot_records: Path,
) -> None:
    readme = (_ROOT / "README.md").read_text()
    rows = _readme_provider_rows(readme)
    name, _label, guide = rows[0]
    stray = f"| {name} | `LIVE_PILOTED` | read | [setup]({guide}) |"
    # After a blank line the row no longer belongs to the table, but it still opens with a pipe.
    edited = readme.replace("\n\n`doctor` expects GA4.", f"\n\n{stray}\n\n`doctor` expects GA4.")
    assert edited != readme
    with pytest.raises(AssertionError):
        _readme_provider_rows(edited)
    # A second `| Provider |` table is refused too.
    second = readme.replace(
        "\n\n`doctor` expects GA4.", "\n\n| Provider | Label |\n\n`doctor` expects GA4."
    )
    with pytest.raises(AssertionError):
        _readme_provider_rows(second)


def test_a_pilot_record_that_changes_a_label_no_longer_matches_the_changelog(
    tmp_pilot_records: Path,
) -> None:
    # Start from the records the repository commits, so the committed CHANGELOG matches.
    for committed in sorted((_ROOT / "docs" / "pilots").glob("*.json")):
        (tmp_pilot_records / committed.name).write_bytes(committed.read_bytes())
    pilot_record_inventory.cache_clear()
    changelog = (_ROOT / "CHANGELOG.md").read_text()
    before = provider_labels()
    assert sorted(_changelog_label_rows(changelog)) == sorted(_expected_label_rows(before))
    # Change one label: pilot a provider that is still `EXPERIMENTAL`, or, when every
    # provider is already piloted, withdraw the records.
    unpiloted = [item.provider for item in before if item.label == "EXPERIMENTAL"]
    if unpiloted:
        provider = unpiloted[0]
        (tmp_pilot_records / f"{provider}.json").write_bytes(
            pilot_record_bytes(sealed_record(provider))
        )
    else:
        provider = before[0].provider
        for record in tmp_pilot_records.glob("*.json"):
            record.unlink()
    pilot_record_inventory.cache_clear()
    after = provider_labels()
    assert {item.provider: item.label for item in after}[provider] != {
        item.provider: item.label for item in before
    }[provider]
    assert sorted(_changelog_label_rows(changelog)) != sorted(_expected_label_rows(after))


def test_the_changelog_entry_is_found_once_it_is_dated(committed_pilot_records: Path) -> None:
    changelog = (_ROOT / "CHANGELOG.md").read_text()
    heading = r"(?m)^## \[0\.1\.0\] - .*$"
    unreleased = re.sub(heading, lambda _: "## [0.1.0] - Unreleased", changelog)
    dated = re.sub(heading, lambda _: "## [0.1.0] - 2026-10-20", changelog)
    assert "## [0.1.0] - Unreleased\n" in unreleased
    assert "## [0.1.0] - 2026-10-20\n" in dated
    assert "## [0.1.0] - Unreleased\n" not in dated
    expected = sorted(_expected_label_rows(provider_labels()))
    for variant in (changelog, unreleased, dated):
        assert _changelog_entry(variant) == _changelog_entry(changelog)
        assert sorted(_changelog_label_rows(variant)) == expected


def test_the_root_documents_state_no_per_provider_exception_to_enroll_and_remove_locally() -> None:
    """Neither the Meta nor the Reddit limitation remains, and the promise carries no exception.

    Meta Ads has a local-removal command and Reddit enrolls as a confidential client with the
    app secret, so the per-provider promise (enroll, remove locally) holds for every provider.
    Neither document may still list either gap as a limitation, and the promise may not be
    qualified. If a gap reopens, change the code, the setup guide, this
    test and these two documents together.
    """
    readme = (_ROOT / "README.md").read_text()
    changelog = (_ROOT / "CHANGELOG.md").read_text()
    entry = _changelog_entry(changelog)
    sections = {
        "README What 0.1.0 does not do": _words(_section(readme, "What 0.1.0 does not do")),
        "CHANGELOG Known limitations": _words(_section(entry, "Known limitations")),
    }
    for name, text in sections.items():
        for needle in (
            "local-removal",
            "META_TOKEN_CREDENTIAL_LIFECYCLE_REQUIRED",
            "Reddit Ads enrollment",
            "empty secret",
            "OAUTH_TOKEN_REJECTED",
            "OAUTH_TOKEN_ERROR_RESPONSE",
        ):
            assert needle not in text, f"{name} still carries: {needle}"
    promise = _words(_section(readme, "What you can do"))
    assert "exception" not in promise
    assert "Known limitations" not in promise
    assert "does not do" not in promise
    for needle in ("Meta", "Reddit", "local-removal", "unverified"):
        assert needle not in promise, f"the promise still qualifies: {needle}"
    added = _words(_section(entry, "Added"))
    assert "For each provider:" in added
    assert "except where Known limitations" not in added


# --- Contribution, security and conduct policies ---------------------------------------

_ADVISORY_URL = "https://github.com/jasonhowie/adhelm/security/advisories/new"


def test_contributing_requires_a_dco_sign_off_and_states_the_certificate() -> None:
    contributing = (_ROOT / "CONTRIBUTING.md").read_text()
    certificate = _words(
        " ".join(line[1:] for line in contributing.splitlines() if line.startswith(">"))
    )
    assert "Signed-off-by:" in contributing and "git commit -s" in contributing
    assert "Developer's Certificate of Origin 1.1" in certificate
    for clause in (
        "(a) The contribution was created",
        "(b) The contribution is based",
        "(c) The contribution was provided",
        "(d) I understand and agree",
    ):
        assert clause in certificate
    assert "instead of a contributor license agreement" in _words(contributing)


def test_security_policy_names_private_reporting_and_forbids_public_disclosure() -> None:
    security = (_ROOT / "SECURITY.md").read_text()
    assert _ADVISORY_URL in security
    assert "Do not open a public issue" in security
    assert "Do not send real credentials" in security
    config = (_ROOT / ".github/ISSUE_TEMPLATE/config.yml").read_text()
    assert "blank_issues_enabled: false" in config
    assert re.findall(r"(?m)^\s+url: (\S+)$", config) == [_ADVISORY_URL]


def test_the_code_of_conduct_is_the_contributor_covenant_2_1_with_a_real_contact() -> None:
    conduct = (_ROOT / "CODE_OF_CONDUCT.md").read_text()
    assert "version 2.1" in _words(conduct)
    assert re.findall(r"(?m)^### \d\. (.+)$", conduct) == [
        "Correction",
        "Warning",
        "Temporary Ban",
        "Permanent Ban",
    ]
    assert "INSERT" not in conduct and _ADVISORY_URL in conduct


# --- Issue templates -----------------------------------------------------------------


def _form_elements(name: str) -> list[dict[str, object]]:
    """Split an issue form into its body elements; the repository has no YAML parser."""
    text = (_ROOT / ".github/ISSUE_TEMPLATE" / name).read_text()
    blocks = re.split(r"(?m)^  - type: ", text.split("\nbody:\n", 1)[1])[1:]
    elements: list[dict[str, object]] = []
    for block in blocks:
        kind, _, rest = block.partition("\n")
        ident = re.search(r"(?m)^    id: (\S+)$", rest)
        elements.append(
            {
                "type": kind.strip(),
                "id": ident[1] if ident else None,
                "required": "required: true" in rest,
                "text": rest,
            }
        )
    return elements


def test_the_bug_report_requires_version_provider_platform_and_doctor_output() -> None:
    elements = _form_elements("bug_report.yml")
    required = {element["id"] for element in elements if element["required"]}
    assert {"version", "provider", "platform", "doctor"} <= required
    by_id = {element["id"]: str(element["text"]) for element in elements}
    assert "adhelm version" in by_id["version"]
    assert "adhelm doctor" in by_id["doctor"]
    for provider in _PROVIDER_NAMES.values():
        assert f"- {provider}\n" in by_id["provider"]
    for platform in ("macOS", "Linux", "Windows"):
        assert f"- {platform}\n" in by_id["platform"]


def test_no_issue_form_asks_for_a_credential_and_each_requires_a_redaction_confirmation() -> None:
    for name in ("bug_report.yml", "feature_request.yml"):
        elements = _form_elements(name)
        identifiers = [str(element["id"]) for element in elements if element["id"]]
        assert len(identifiers) == len(set(identifiers))
        assert not [i for i in identifiers if re.search(r"token|secret|password|credential|key", i)]
        (confirmation,) = [element for element in elements if element["type"] == "checkboxes"]
        assert confirmation["required"]
        assert "removed every credential" in str(confirmation["text"])


def test_the_security_pointer_in_the_bug_report_is_not_a_public_report_channel() -> None:
    markdown = str(_form_elements("bug_report.yml")[0]["text"])
    assert "do not file it here" in _words(markdown)
    assert "SECURITY.md" in markdown


# --- Public AGENTS.md ----------------------------------------------------------------

#: First 16 hex digits of the SHA-256 of each rule's text, whitespace-normalized. The rules
#: are non-negotiable: changing one means changing its digest here, in view of a reviewer.
#: Rule 2 differs from the private repository's wording only in that it no longer names a
#: sibling project.
_RULE_DIGESTS = {
    1: "31dd2419de6f6d08",
    2: "c5a9fe695aa2a6a6",
    3: "fa962d93f7244847",
    4: "2f8f6537dcf4850c",
    5: "8de294a237192de1",
    6: "b323c045a9ae17c1",
    7: "f3aeec480a341017",
    8: "d24344cdd6449cd7",
    9: "428f6b7151906313",
    10: "19c349028bb7e961",
    11: "44f73555a13d5b47",
    12: "1688a5b3747655c0",
    13: "02822cee2c157504",
    14: "4de83ff751382928",
    15: "4dc118b7b32ae33c",
    16: "3a699cb1b3274330",
    17: "494dbb380f27125d",
    18: "8c9a4a067eb33796",
    19: "04481b704abdaa05",
    20: "70bdfe5ed721282e",
    21: "a21539ecec6be6d4",
    22: "36e85b646cd1f857",
    23: "6124e23830fd88c5",
    24: "d9bdb18840113654",
    25: "80d5fbe2afc6aac8",
    26: "72cb5d628096aba3",
    27: "e59e08b8b171b471",
    28: "e7abdab3056de45f",
    29: "46e1d876dad9ebf2",
}


def _rules(agents: str) -> dict[int, str]:
    section = _section(agents, "Non-negotiable rules").split("\nDo not claim", 1)[0]
    rules: dict[int, str] = {}
    current = 0
    for line in section.splitlines():
        numbered = re.match(r"(\d+)\. (.*)", line)
        if numbered:
            current = int(numbered[1])
            rules[current] = numbered[2]
        elif current and line.startswith("   "):
            rules[current] += " " + line.strip()
    return {number: _words(text) for number, text in rules.items()}


def test_the_public_agents_file_keeps_all_29_rules_exactly() -> None:
    rules = _rules((_ROOT / "AGENTS.md").read_text())
    assert sorted(rules) == list(range(1, 30))
    assert {
        number: hashlib.sha256(text.encode()).hexdigest()[:16] for number, text in rules.items()
    } == _RULE_DIGESTS


def test_the_public_agents_file_states_the_0_1_0_release_boundary() -> None:
    agents = _words((_ROOT / "AGENTS.md").read_text())
    for statement in (
        "`0.1.0`, is read-only",
        "refused at runtime in `0.1.0` with `WRITES_NOT_RELEASED` (exit code `3`)",
        "Salesforce and HubSpot are out of `0.1.0`",
        "remains the `v1.0.0` target",
        "All 29 non-negotiable rules below still apply",
    ):
        assert statement in agents
    assert "Do not report `COMPLETE` until every required item" in agents


# --- Nothing retired remains (the private-name scan is the public export's denylist) ---

_RETIRED_COMMERCIAL_TEXT = re.compile(
    r"account auditor|paid operator|full-operator|operator edition|edition manifest|"
    r"distribution-and-pricing|pricing hypothesis|alternative acquisition|"
    r"\$\s?200\b|\$\s?2,000|per month|/month|BUILD_PROMPT|oss-0\.1\.0-plan|"
    r"\bTerra\b|0009-editions",
    re.IGNORECASE,
)


@pytest.mark.parametrize("document", PUBLIC_DOCUMENTS)
def test_no_public_document_carries_pricing_edition_or_build_plan_text(document: str) -> None:
    text = (_ROOT / document).read_text()
    assert [match[0] for match in _RETIRED_COMMERCIAL_TEXT.finditer(text)] == []


def test_the_scanners_catch_what_they_are_meant_to() -> None:
    assert _RETIRED_COMMERCIAL_TEXT.search("the Account Auditor is free")
    assert _RETIRED_COMMERCIAL_TEXT.search("an Operator at $200/month")
    assert not _RETIRED_COMMERCIAL_TEXT.search("There is no paid tier, edition or pricing.")


# --- Definition of done and product brief ---------------------------------------------


def test_section_8_of_the_definition_of_done_describes_a_free_build_without_editions() -> None:
    dod = (_ROOT / "docs/definition-of-done.md").read_text()
    assert "### 8. Installation and distribution contract" in dod
    section = _words(_section(dod, "8. Installation and distribution contract"))
    for statement in (
        "There are no editions or tiers",
        "refuses every remote mutation at one release-policy gate",
        "AdHelm is free software under Apache-2.0",
        "no runtime license service, heartbeat, or meter",
        "bring their own credentials",
    ):
        assert statement in section
    assert "commercial" not in section.lower()


def test_the_traceability_matrix_keeps_the_section_8_ids_under_the_revised_title() -> None:
    matrix = (_ROOT / "docs/traceability-matrix.md").read_text()
    section = _words(_section(matrix, "8. Installation and distribution contract"))
    assert "ADR 0013" in section and "Each keeps its ID" in section
    for identifier in ("DOD-08-04", "DOD-08-05", "DOD-08-06", "DOD-08-16"):
        assert f"`{identifier}`" in section


def test_the_product_brief_replaces_pricing_with_licensing_and_keeps_the_v1_gate() -> None:
    brief = (_ROOT / "docs/product-brief.md").read_text()
    headings = re.findall(r"(?m)^#{2,3} (.+)$", brief)
    assert "Distribution and licensing" in headings
    assert not [
        heading
        for heading in headings
        if re.search(r"pric|edition|auditor|acquisition", heading, re.I)
    ]
    gate = _words(_section(brief, "Public release gate"))
    assert gate.startswith("Do not ship `v1.0.0` until every criterion")


def _traceability() -> ModuleType:
    return _script("traceability")


def test_the_requirement_index_is_exactly_what_the_generator_derives_from_the_definition(
    tmp_path: Path,
) -> None:
    index = tmp_path / "traceability.json"
    committed = (_ROOT / "docs/implementation/traceability.json").read_text()
    index.write_text(committed)
    generator = _traceability()
    generator.TARGET = index
    generator.generate()
    assert index.read_text() == committed
