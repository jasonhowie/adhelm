"""Adversarial regressions for the installed, read-only guidance surface."""

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adhelm import guidance, guidance_cli
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, OutputPreferences


def _module_path(root: Path, *parts: str) -> Path:
    module = root.joinpath(*parts, "guidance.py")
    module.parent.mkdir(parents=True)
    module.write_text("# synthetic module path\n")
    return module


def _bundle(root: Path, main: bytes) -> Path:
    skill = root / "ad-program-optimization"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(main)
    return root


def _valid_main(*, line_ending: str = "\n") -> bytes:
    return (
        line_ending.join(
            [
                "---",
                "name: ad-program-optimization",
                "description: Safe authored guidance.",
                "metadata:",
                '  version: "0.2.0"',
                "---",
                "# Guidance",
            ]
        )
        + line_ending
    ).encode("utf-8")


def _guidance_document(text: str) -> dict[str, object]:
    return {
        "name": "ad-program-optimization",
        "guidance_version": "0.2.0",
        "description": "Bounded synthetic guidance.",
        "section": "main",
        "document_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
        "total_characters": len(text),
        "provenance": "AUTHORED_INSTALLED_GUIDANCE",
        "capability_evidence": False,
        "execution_authority": False,
    }


def test_bundle_root_uses_only_the_matching_source_or_wheel_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel_module = _module_path(tmp_path, "site-packages", "adhelm")
    wheel_bundle = wheel_module.parent / "bundled_skills"
    wheel_bundle.mkdir()
    monkeypatch.setattr(guidance, "__file__", str(wheel_module))
    assert guidance._bundle_root() == wheel_bundle

    source_root = tmp_path / "source"
    source_module = _module_path(source_root, "src", "adhelm")
    (source_root / "pyproject.toml").write_text("[build-system]\n")
    source_skills = source_root / "skills"
    source_skills.mkdir()
    monkeypatch.setattr(guidance, "__file__", str(source_module))
    assert guidance._bundle_root() == source_skills

    missing_wheel_module = _module_path(tmp_path, "other-site-packages", "adhelm")
    (missing_wheel_module.parent.parent / "skills").mkdir()
    monkeypatch.setattr(guidance, "__file__", str(missing_wheel_module))
    with pytest.raises(AdhelmError) as error:
        guidance._bundle_root()
    assert error.value.code == "GUIDANCE_NOT_INSTALLED"


def test_guidance_rejects_missing_or_symlinked_allowlisted_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    (missing / "ad-program-optimization").mkdir(parents=True)
    monkeypatch.setattr(guidance, "_bundle_root", lambda: missing)
    with pytest.raises(AdhelmError) as error:
        guidance.guidance_document("ad-program-optimization")
    assert error.value.code == "GUIDANCE_INVALID"

    root = tmp_path / "bundle"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("outside content must not be read")
    root.mkdir()
    (root / "ad-program-optimization").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(guidance, "_bundle_root", lambda: root)
    with pytest.raises(AdhelmError) as error:
        guidance.guidance_document("ad-program-optimization")
    assert error.value.code == "GUIDANCE_INVALID"
    assert "outside content" not in error.value.message


@pytest.mark.parametrize(
    "main",
    [
        b"---\n"
        b"name: ad-program-optimization\n"
        b"name: ad-program-optimization\n"
        b"description: x\n"
        b"metadata:\n"
        b'  version: "0.2.0"\n'
        b"---\n",
        b"---\n"
        b"name: ad-program-optimization\n"
        b"description: x\n"
        b"metadata:\n"
        b"other: value\n"
        b'  version: "0.2.0"\n'
        b"---\n",
    ],
)
def test_guidance_metadata_requires_one_truthful_nested_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, main: bytes
) -> None:
    root = _bundle(tmp_path / "bundle", main)
    monkeypatch.setattr(guidance, "_bundle_root", lambda: root)
    with pytest.raises(AdhelmError) as error:
        guidance.guidance_document("ad-program-optimization")
    assert error.value.code == "GUIDANCE_INVALID"


def test_guidance_digest_is_the_exact_utf8_document_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = _valid_main(line_ending="\r\n")
    root = _bundle(tmp_path / "bundle", main)
    monkeypatch.setattr(guidance, "_bundle_root", lambda: root)

    document = guidance.guidance_document("ad-program-optimization")
    assert document["guidance_version"] == "0.2.0"
    assert document["document_digest"] == hashlib.sha256(main).hexdigest()
    assert document["total_characters"] == len(main.decode("utf-8"))


def test_unicode_pages_at_one_kib_progress_without_omission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = ('日本語😀e\u0301 \\"\n' * 400) + "終"
    document = _guidance_document(text)
    monkeypatch.setattr(guidance_cli, "guidance_document", lambda *args: dict(document))
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))

    cursor = 0
    chunks: list[str] = []
    runner = CliRunner()
    while True:
        result = runner.invoke(
            app,
            [
                "--data-dir",
                str(tmp_path),
                "guidance",
                "show",
                "ad-program-optimization",
                "--cursor",
                str(cursor),
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(result.output.encode("utf-8")) <= 1024
        payload = json.loads(result.output)
        page = payload["data"]
        chunk = page["text"]
        assert page["cursor"] == cursor
        assert payload["total_count"] == len(text)
        assert page["document_digest"] == document["document_digest"]
        assert chunk or cursor == len(text)
        chunks.append(chunk)
        next_cursor = payload["next_cursor"]
        if next_cursor is None:
            break
        assert int(next_cursor) == cursor + len(chunk)
        cursor = int(next_cursor)

    assert "".join(chunks) == text


def test_every_shipped_section_pages_without_omission_at_one_kib(tmp_path: Path) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    runner = CliRunner()
    for skill in guidance.guidance_catalog()["guidance"]:
        for section in skill["sections"]:
            document = guidance.guidance_document(skill["name"], section)
            cursor = 0
            chunks: list[str] = []
            while True:
                result = runner.invoke(
                    app,
                    [
                        "--data-dir",
                        str(tmp_path),
                        "guidance",
                        "show",
                        skill["name"],
                        "--section",
                        section,
                        "--cursor",
                        str(cursor),
                    ],
                )
                assert result.exit_code == 0, result.output
                assert len(result.output.encode("utf-8")) <= 1024
                payload = json.loads(result.output)
                page = payload["data"]
                chunk = page["text"]
                assert page["cursor"] == cursor
                assert page["document_digest"] == document["document_digest"]
                assert payload["total_count"] == document["total_characters"]
                chunks.append(chunk)
                if payload["next_cursor"] is None:
                    break
                assert int(payload["next_cursor"]) == cursor + len(chunk)
                cursor = int(payload["next_cursor"])
            assert "".join(chunks) == document["text"]


def test_catalog_rejects_a_cursor_past_the_static_allowlist(tmp_path: Path) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path), "guidance", "catalog", "--cursor", "3"],
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["diagnostics"][0]["code"] == "INVALID_GUIDANCE_CURSOR"
