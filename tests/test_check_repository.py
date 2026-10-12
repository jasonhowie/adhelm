"""The repository check rejects the retired product names outside a narrow historical allowlist."""

import fnmatch
import importlib.util
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_repository_under_test", _ROOT / "scripts/check_repository.py"
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _write(root: Path, relative: str, content: str | bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())


@pytest.mark.parametrize(
    "line",
    [
        "import adcp",
        "from adcp.errors import AdcpError",
        "raise AdcpError('x')",
        "ADCP_HOME=/tmp",
        "bundle com.example.adcp.host",
        "service local-ad-control-plane.google-oauth-v1",
        "wheel local_ad_control_plane-0.1.0-py3-none-any.whl",
        "adcp-macos-approval-v1",
        "Adcp",
    ],
)
def test_every_retired_spelling_in_file_content_is_reported_with_its_line(
    tmp_path: Path, line: str
) -> None:
    _write(tmp_path, "src/module.py", f"fine = 1\n{line}\n")
    findings = _checker().find_retired_names(tmp_path, ["src/module.py"])
    assert findings == ["src/module.py:2"]


def test_a_retired_name_in_a_path_is_reported_even_when_the_content_is_clean(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/adcp/__init__.py", "")
    _write(tmp_path, "docs/local-ad-control-plane-notes.md", "clean\n")
    findings = _checker().find_retired_names(
        tmp_path, ["src/adcp/__init__.py", "docs/local-ad-control-plane-notes.md"]
    )
    assert findings == [
        "src/adcp/__init__.py: path",
        "docs/local-ad-control-plane-notes.md: path",
    ]


def test_current_names_and_unrelated_words_are_not_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/module.py",
        "import adhelm\nfrom adhelm.errors import AdhelmError\n"
        "ADHELM_HOME = 'com.aptsonic.adhelm.host'\n"
        "padding = 'adc p ad-cp a d c p'\n",
    )
    assert _checker().find_retired_names(tmp_path, ["src/module.py"]) == []


def test_findings_name_locations_and_never_echo_file_content(tmp_path: Path) -> None:
    secret_looking = "adcp-credential-ref-0123456789abcdef"
    _write(tmp_path, "docs/note.md", f"token {secret_looking}\n")
    findings = _checker().find_retired_names(tmp_path, ["docs/note.md"])
    assert findings == ["docs/note.md:1"]
    assert secret_looking not in "".join(findings)


def test_a_binary_file_carrying_a_retired_name_is_reported_and_a_clean_one_is_not(
    tmp_path: Path,
) -> None:
    retired = "ad" + "cp"
    _write(
        tmp_path,
        "tests/fixtures/image.png",
        f"\x89PNG\r\n\xff\xfe {retired} \x00\x80".encode("latin-1"),
    )
    _write(
        tmp_path,
        "tests/fixtures/archive.bin",
        b"PK\x03\x04\xff\xfe" + f"Local_AD-Control_Plane/{retired.upper()}".encode() + b"\x00\x80",
    )
    _write(tmp_path, "tests/fixtures/clean.png", b"\x89PNG\r\n\xff\xfe adhelm \x00\x80")
    findings = _checker().find_retired_names(
        tmp_path,
        [
            "tests/fixtures/image.png",
            "tests/fixtures/archive.bin",
            "tests/fixtures/clean.png",
            "tests/fixtures/removed.json",  # listed but absent: neither a crash nor a finding
        ],
    )
    assert findings == [
        "tests/fixtures/image.png: binary",
        "tests/fixtures/archive.bin: binary",
    ]


def test_a_symbolic_link_is_not_followed_to_content_outside_the_listing(
    tmp_path: Path, make_link: Callable[[Path, Path], None]
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("adcp\n")
    root = tmp_path / "root"
    root.mkdir()
    make_link(root / "link.txt", outside)
    assert _checker().find_retired_names(root, ["link.txt"]) == []


def test_only_allowlisted_paths_are_exempt_and_the_exemption_is_exact(tmp_path: Path) -> None:
    for relative in (
        "docs/adr/0001-local-runtime.md",
        "docs/adr/0003-another-adr.md",
        "docs/implementation/finishing-baseline.md",
        "docs/implementation/other-record.md",
        "docs/implementation/native-validation.json",
        "src/adhelm/module.py",
    ):
        _write(tmp_path, relative, "adcp\n")
    paths = [
        "docs/adr/0001-local-runtime.md",
        "docs/adr/0003-another-adr.md",
        "docs/implementation/finishing-baseline.md",
        "docs/implementation/other-record.md",
        "docs/implementation/native-validation.json",
        "src/adhelm/module.py",
    ]
    assert _checker().find_retired_names(tmp_path, paths) == [
        "docs/adr/0003-another-adr.md:1",
        "docs/implementation/other-record.md:1",
        "src/adhelm/module.py:1",
    ]


def test_the_allowlist_never_exempts_source_tests_or_the_agent_surface() -> None:
    allowlist = _checker().RETIRED_NAME_ALLOWLIST
    exempt_code = [
        pattern
        for pattern in allowlist
        if pattern.startswith(("src/", "skills/", "native/", "examples/", "README", "AGENTS"))
        or (pattern.startswith("tests/") and pattern != "tests/test_check_repository.py")
        or (pattern.startswith("scripts/") and pattern != "scripts/check_repository.py")
        or pattern.startswith("*")
    ]
    assert exempt_code == []
    for sample in ("src/adhelm/cli.py", "tests/test_cli.py", "skills/x/SKILL.md", "README.md"):
        assert not any(fnmatch.fnmatchcase(sample, pattern) for pattern in allowlist)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def test_the_file_listing_includes_unstaged_files_and_skips_ignored_ones(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--quiet")
    _write(tmp_path, ".gitignore", "ignored/\n")
    _write(tmp_path, "tracked.py", "adcp = 1\n")
    _write(tmp_path, "ignored/cache.py", "adcp = 1\n")
    _git(tmp_path, "add", ".gitignore", "tracked.py")
    _write(tmp_path, "new_untracked.py", "adcp = 1\n")
    checker = _checker()
    listed = checker.repository_files(tmp_path)
    assert listed == [".gitignore", "new_untracked.py", "tracked.py"]
    assert checker.find_retired_names(tmp_path, listed) == [
        "new_untracked.py:1",
        "tracked.py:1",
    ]


def test_this_repository_carries_no_retired_name_outside_the_allowlist() -> None:
    checker = _checker()
    assert checker.find_retired_names(_ROOT, checker.repository_files(_ROOT)) == []


@pytest.mark.parametrize(
    "line",
    [
        'SCHEMA_URI = "https://adhelm.dev/schema/ledger-record"',
        "see adhelm.dev for the home page",
        "host_bundle_identifier = 'dev.adhelm.host'",
        "access group ABCDE12345.dev.adhelm.host",
        "Contents/Library/LaunchAgents/dev.adhelm.approvalhost.plist",
        "DEV.ADHELM.CLIENT",
    ],
)
def test_the_retired_domain_and_identifier_root_are_reported_with_their_line(
    tmp_path: Path, line: str
) -> None:
    _write(tmp_path, "src/module.py", f"fine = 1\n{line}\n")
    assert _checker().find_retired_identifiers(tmp_path, ["src/module.py"]) == ["src/module.py:2"]


def test_the_current_domain_and_identifier_root_are_not_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/module.py",
        'URI = "https://aptsonic.com/adhelm/schema/ledger-record"\n'
        "host = 'com.aptsonic.adhelm.host'\n"
        "group = 'ABCDE12345.com.aptsonic.adhelm.host'\n"
        "import adhelm.devtools\n"
        "mydev.adhelm_other = 1\n",
    )
    assert _checker().find_retired_identifiers(tmp_path, ["src/module.py"]) == []


def test_a_retired_identifier_in_a_path_or_binary_file_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, "native/dev.adhelm.approvalhost.plist", "clean\n")
    _write(tmp_path, "tests/fixtures/blob.bin", b"\xff\xfe com.x dev.adhelm.host \x00\x80")
    _write(tmp_path, "tests/fixtures/clean.bin", b"\xff\xfe com.aptsonic.adhelm.host \x00\x80")
    findings = _checker().find_retired_identifiers(
        tmp_path,
        [
            "native/dev.adhelm.approvalhost.plist",
            "tests/fixtures/blob.bin",
            "tests/fixtures/clean.bin",
        ],
    )
    assert findings == [
        "native/dev.adhelm.approvalhost.plist: path",
        "tests/fixtures/blob.bin: binary",
    ]


_ADR = "docs/adr/0013-open-source-release.md"
_NOTE_HEADING = "### Domain and identifier root (2026-10-12)"


def test_only_this_check_and_its_test_are_exempt_by_file_from_the_retired_identifiers(
    tmp_path: Path,
) -> None:
    allowlist = _checker().RETIRED_IDENTIFIER_ALLOWLIST
    assert set(allowlist) == {"scripts/check_repository.py", "tests/test_check_repository.py"}
    paths = [*allowlist, "docs/adr/0003-another-adr.md", "docs/implementation/oss-0.1.0-plan.md"]
    for relative in paths:
        _write(tmp_path, relative, "dev.adhelm.host\n")
    assert _checker().find_retired_identifiers(tmp_path, paths) == [
        "docs/adr/0003-another-adr.md:1",
        "docs/implementation/oss-0.1.0-plan.md:1",
    ]


def test_the_adr_exemption_covers_only_the_dated_note_section(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _ADR,
        "# ADR\n"  # 1
        "| Native identifiers | `dev.adhelm.*` |\n"  # 2: rename table row, reported
        "## Rename rules\n"  # 3
        f"{_NOTE_HEADING}\n"  # 4
        "The owner dropped `adhelm.dev` and `dev.adhelm.*`.\n"  # 5: inside the note, exempt
        "\n"  # 6
        "More note text with dev.adhelm.host.\n"  # 7: still inside the note, exempt
        "## Consequences\n"  # 8
        "After the note: https://adhelm.dev/schema/x\n",  # 9: after the note, reported
    )
    assert _checker().find_retired_identifiers(tmp_path, [_ADR]) == [f"{_ADR}:2", f"{_ADR}:9"]


def test_a_retired_identifier_in_a_following_subsection_of_the_note_is_reported(
    tmp_path: Path,
) -> None:
    _write(tmp_path, _ADR, f"{_NOTE_HEADING}\ndev.adhelm.a\n#### Detail\ndev.adhelm.b\n")
    assert _checker().find_retired_identifiers(tmp_path, [_ADR]) == [f"{_ADR}:4"]


def test_the_adr_without_the_note_heading_has_no_exemption(tmp_path: Path) -> None:
    _write(tmp_path, _ADR, "### Some other heading\ndev.adhelm.host\n")
    assert _checker().find_retired_identifiers(tmp_path, [_ADR]) == [f"{_ADR}:2"]


def test_this_repository_carries_no_retired_domain_or_identifier_outside_the_adr_note() -> None:
    checker = _checker()
    assert checker.find_retired_identifiers(_ROOT, checker.repository_files(_ROOT)) == []
