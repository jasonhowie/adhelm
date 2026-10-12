"""Installed agent guidance is discoverable without invoking an agent or a provider."""

import hashlib
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adhelm import guidance, guidance_cli
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, OutputPreferences


def test_guidance_catalog_and_all_declared_sections_are_shipped() -> None:
    catalog = guidance.guidance_catalog()
    assert catalog["execution_authority"] is False
    assert catalog["capability_evidence"] is False
    assert len(catalog["guidance"]) == 2
    for skill in catalog["guidance"]:
        assert (
            skill["guidance_version"]
            == {
                "ad-program-optimization": "0.3.0",
                "conversion-optimization": "0.3.0",
            }[skill["name"]]
        )
        for section in skill["sections"]:
            doc = guidance.guidance_document(skill["name"], section)
            assert doc["text"]
            assert doc["total_characters"] == len(doc["text"])
            assert doc["document_digest"] == hashlib.sha256(doc["text"].encode()).hexdigest()


def test_ad_program_guidance_describes_closed_local_creative_registration() -> None:
    text = guidance.guidance_document("ad-program-optimization")["text"]
    for phrase in (
        "creative analyze",
        "creative register",
        "creative-registration-request",
        "IDENTITY_IMAGE",
        "TEXT_ONLY",
        "creative catalog",
        "creative show",
        "no provider object",
    ):
        assert phrase in text


def test_minimum_output_catalog_paginates_without_hiding_a_skill(tmp_path: Path) -> None:
    save_configuration(
        tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024, max_rows=1))
    )
    runner = CliRunner()
    cursor = "0"
    names = []
    while cursor is not None:
        result = runner.invoke(
            app, ["--data-dir", str(tmp_path), "guidance", "catalog", "--cursor", cursor]
        )
        assert result.exit_code == 0, result.output
        assert len(result.output.encode()) <= 1024
        output = json.loads(result.output)
        assert output["total_count"] == 2
        names.extend(item["name"] for item in output["data"]["guidance"])
        cursor = output["next_cursor"]
    assert names == ["ad-program-optimization", "conversion-optimization"]


def test_progressive_pages_preserve_unicode_text_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_output_bytes=1024)))
    doc = guidance.guidance_document("ad-program-optimization", "program-analysis")
    text = 'Unicode: 日本語 😀 "quoted" \\ backslash\n' * 100
    doc.update(
        text=text,
        total_characters=len(text),
        document_digest=hashlib.sha256(text.encode()).hexdigest(),
    )
    monkeypatch.setattr(guidance_cli, "guidance_document", lambda *args: dict(doc))
    runner = CliRunner()
    cursor = "0"
    chunks = []
    while cursor is not None:
        result = runner.invoke(
            app,
            [
                "--data-dir",
                str(tmp_path),
                "guidance",
                "show",
                "ad-program-optimization",
                "--section",
                "program-analysis",
                "--cursor",
                cursor,
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(result.output.encode()) <= 1024
        output = json.loads(result.output)
        assert output["data"]["document_digest"] == doc["document_digest"]
        assert output["data"]["cursor"] == int(cursor)
        assert output["total_count"] == len(text)
        chunks.append(output["data"]["text"])
        if output["next_cursor"] is not None:
            assert int(output["next_cursor"]) > int(cursor)
        cursor = output["next_cursor"]
    assert len(chunks) > 1 and "".join(chunks) == text


@pytest.mark.parametrize(
    "name,section",
    [
        ("../config.json", "main"),
        ("ad-program-optimization", "../../config.json"),
        ("conversion-optimization", "program-analysis"),
    ],
)
def test_arbitrary_local_paths_are_not_guidance(name: str, section: str) -> None:
    with pytest.raises(AdhelmError, match="installed skill and section"):
        guidance.guidance_document(name, section)


def test_guidance_rejects_symlink_fifo_and_oversized_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_link
) -> None:
    root = tmp_path / "guidance"
    package = root / "ad-program-optimization"
    package.mkdir(parents=True)
    path = package / "SKILL.md"
    monkeypatch.setattr(guidance, "_bundle_root", lambda: root)
    target = tmp_path / "private"
    target.write_text("not a bundled skill")
    make_link(path, target)
    with pytest.raises(AdhelmError, match="symbolic links"):
        guidance.guidance_document("ad-program-optimization")
    path.unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(path)
        with pytest.raises(AdhelmError, match="regular file"):
            guidance.guidance_document("ad-program-optimization")
        path.unlink()
    path.write_bytes(b"x" * 65537)
    with pytest.raises(AdhelmError, match="bounded regular file"):
        guidance.guidance_document("ad-program-optimization")


def test_guidance_cli_discovery_is_portable() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["discover"])
    assert {"guidance catalog", "guidance show"} <= set(
        json.loads(result.output)["data"]["commands"]
    )


def test_guidance_cli_is_discovered_and_rejects_cursor_without_creating_state(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["discover"])
    assert {"guidance catalog", "guidance show"} <= set(
        json.loads(result.output)["data"]["commands"]
    )
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "guidance",
            "show",
            "ad-program-optimization",
            "--cursor",
            "999999",
        ],
    )
    assert result.exit_code == 2
    assert "INVALID_GUIDANCE_CURSOR" in result.output
    assert not (tmp_path / "data").exists()
