"""Adversarial checks for offline compiler-preview CLI boundaries."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

import pytest
from typer.testing import CliRunner

from adhelm import __version__, compile_cli
from adhelm.canonical import canonical_bytes
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.models import Configuration, OutputPreferences, ResultEnvelope
from adhelm.saved_candidates import compile_candidate


def _builder_brief(module_name: str) -> Any:
    """Load builder fixtures dynamically so this review stays type-isolated."""
    return import_module(module_name)._brief()


def meta_brief() -> Any:
    return _builder_brief("test_meta_compiler")


def google_brief() -> Any:
    return _builder_brief("test_google_ads_compiler")


def openai_brief() -> Any:
    return _builder_brief("test_openai_compiler")


class _CliResult(Protocol):
    @property
    def exit_code(self) -> int: ...

    @property
    def output(self) -> str: ...


def _invoke_preview(tmp_path: Path, provider: str, input_file: Path) -> _CliResult:
    return CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "compile", provider, "--input", str(input_file)],
    )


def test_meta_preview_hides_role_labels_account_and_serving_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = meta_brief()
    account_ref = "meta_ads.account.privateoperator"
    image_reference = original.creative.image_reference.model_copy(
        update={"account_ref": account_ref}
    )
    brief = original.model_copy(
        update={
            "account_ref": account_ref,
            "campaign_handle": "private-campaign-label",
            "adset_handle": "private-adset-label",
            "ad_handle": "private-ad-label",
            "campaign_name": "Private campaign copy",
            "adset_name": "Private ad set copy",
            "ad_name": "Private ad copy",
            "creative": original.creative.model_copy(update={"image_reference": image_reference}),
        }
    )
    input_file = tmp_path / "meta.json"
    input_file.write_text(brief.model_dump_json())
    monkeypatch.setattr(compile_cli, "export_key", lambda: b"k" * 32)

    result = _invoke_preview(tmp_path, "meta-ads", input_file)

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["persisted"] is False
    assert data["remote_side_effects"] is False
    assert data["activation_requires_new_plan_and_authorization"] is True
    for field in ("account_ref", "campaign_ref", "adset_ref", "creative_ref", "ad_ref"):
        assert data["review"][field].startswith("opaque:")
    for private in (
        account_ref,
        brief.campaign_handle,
        brief.adset_handle,
        brief.ad_handle,
        brief.campaign_name,
        brief.adset_name,
        brief.ad_name,
        brief.creative.page_id,
        brief.creative.image_reference.image_hash,
        brief.creative.primary_text,
        brief.creative.headline,
        brief.creative.destination_url,
        brief.creative.association.creative_version.creative_version_id,
    ):
        assert private not in result.output
    assert not (tmp_path / "data").exists()


def test_google_preview_hides_all_role_labels_native_refs_and_serving_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = google_brief()
    labels = {
        "budget_handle": "private-budget-label",
        "campaign_handle": "private-campaign-label",
        "ad_group_handle": "private-adgroup-label",
        "ad_handle": "private-rsa-label",
        "location": "private-location-label",
        "language": "private-language-label",
        "schedule": "private-schedule-label",
        "keyword": "private-keyword-label",
    }
    brief = original.model_copy(
        update={
            "account_ref": "google_ads.account.privateoperator",
            "budget_handle": labels["budget_handle"],
            "campaign_handle": labels["campaign_handle"],
            "ad_group_handle": labels["ad_group_handle"],
            "ad_handle": labels["ad_handle"],
            "campaign_name": "Private campaign copy",
            "ad_group_name": "Private ad group copy",
            "ad_name": "Private ad copy",
            "locations": (original.locations[0].model_copy(update={"handle": labels["location"]}),),
            "languages": (original.languages[0].model_copy(update={"handle": labels["language"]}),),
            "ad_schedules": (
                original.ad_schedules[0].model_copy(update={"handle": labels["schedule"]}),
            ),
            "keywords": (original.keywords[0].model_copy(update={"handle": labels["keyword"]}),),
        }
    )
    input_file = tmp_path / "google.json"
    input_file.write_text(brief.model_dump_json())
    monkeypatch.setattr(compile_cli, "export_key", lambda: b"k" * 32)

    result = _invoke_preview(tmp_path, "google-ads", input_file)

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["persisted"] is False
    assert data["remote_side_effects"] is False
    assert data["activation_requires_new_plan_and_authorization"] is True
    for field in ("account_ref", "budget_ref", "campaign_ref", "ad_group_ref", "ad_ref"):
        assert data["review"][field].startswith("opaque:")
    for private in (
        brief.account_ref,
        *labels.values(),
        brief.campaign_name,
        brief.ad_group_name,
        brief.ad_name,
        brief.locations[0].geo_target_constant,
        brief.languages[0].language_constant,
        brief.keywords[0].text,
        *brief.creative.headlines,
        *brief.creative.descriptions,
        brief.creative.destination_url,
        brief.creative.creative_version.creative_version_id,
    ):
        assert private not in result.output
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    ("provider", "brief_json"),
    [
        ("meta-ads", meta_brief().model_dump_json()),
        ("google-ads", google_brief().model_dump_json()),
        ("openai-ads", openai_brief().model_dump_json()),
    ],
)
def test_preview_counts_newline_and_rejects_over_limit_before_key_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, brief_json: str
) -> None:
    input_file = tmp_path / "brief.json"
    input_file.write_text(brief_json)
    monkeypatch.setattr(compile_cli, "export_key", lambda: b"k" * 32)
    initial = _invoke_preview(tmp_path, provider, input_file)
    assert initial.exit_code == 0, initial.output
    exact_limit = len(initial.output.encode())

    save_configuration(
        tmp_path / "data",
        Configuration(output=OutputPreferences(max_output_bytes=exact_limit)),
    )
    exact = _invoke_preview(tmp_path, provider, input_file)
    assert exact.exit_code == 0, exact.output
    assert len(exact.output.encode()) == exact_limit

    save_configuration(
        tmp_path / "data",
        Configuration(output=OutputPreferences(max_output_bytes=exact_limit - 1)),
        replace=True,
    )

    def forbidden() -> bytes:
        pytest.fail("An over-limit preview must not access or create the sharing key")

    monkeypatch.setattr(compile_cli, "export_key", forbidden)
    over_limit = _invoke_preview(tmp_path, provider, input_file)
    assert over_limit.exit_code == 3, over_limit.output
    diagnostic = json.loads(over_limit.output)["diagnostics"][0]
    assert diagnostic["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert diagnostic["next_action"] == (
        "Increase saved max_output_bytes or reduce the brief; no candidate was retained "
        "and no provider action was sent."
    )
    assert sorted(item.name for item in (tmp_path / "data").iterdir()) == ["config.json"]


def test_meta_malformed_input_fails_before_sharing_key_or_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_file = tmp_path / "broken.json"
    input_file.write_text('{"schema_version": 1,')

    def forbidden() -> bytes:
        pytest.fail("Malformed input must not access the sharing key")

    monkeypatch.setattr(compile_cli, "export_key", forbidden)
    result = _invoke_preview(tmp_path, "meta-ads", input_file)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "INVALID_JSON"
    assert not (tmp_path / "data").exists()


def _openai_retained_receipt_bytes() -> bytes:
    compilation = compile_candidate(openai_brief())
    data = {
        "candidate_digest": compilation.candidate_digest,
        "spec_digest": compilation.spec_digest,
        "record_digest": "0" * 64,
        "maximum_spend": compilation.maximum_spend.model_dump(mode="json"),
        "persisted": True,
        "remote_side_effects": False,
        "requires_new_compilation_for_planning": True,
    }
    return canonical_bytes(
        ResultEnvelope(
            tool_version=__version__,
            command="compile openai-ads",
            outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
            data=data,
        )
    )


def test_openai_retain_preflight_counts_the_newline_before_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_file = tmp_path / "openai.json"
    input_file.write_text(openai_brief().model_dump_json())
    receipt = _openai_retained_receipt_bytes()
    calls: list[object] = []

    class NoLedger:
        def __init__(self, *_: object) -> None:
            pass

    def retain(*_: object) -> dict[str, str]:
        calls.append(object())
        return {"record_digest": "f" * 64}

    def output(limit: int) -> SimpleNamespace:
        return SimpleNamespace(output=SimpleNamespace(max_output_bytes=limit))

    monkeypatch.setattr(compile_cli, "Ledger", NoLedger)
    monkeypatch.setattr(compile_cli, "retain_candidate", retain)
    monkeypatch.setattr(compile_cli, "load_configuration", lambda _: output(len(receipt)))
    too_small = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            "openai-ads",
            "--input",
            str(input_file),
            "--retain",
        ],
    )
    assert too_small.exit_code == 3, too_small.output
    assert json.loads(too_small.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert calls == []
    assert not (tmp_path / "data").exists()

    monkeypatch.setattr(compile_cli, "load_configuration", lambda _: output(len(receipt) + 1))
    exact = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            "openai-ads",
            "--input",
            str(input_file),
            "--retain",
        ],
    )
    assert exact.exit_code == 0, exact.output
    assert len(exact.output.encode()) == len(receipt) + 1
    assert len(calls) == 1
