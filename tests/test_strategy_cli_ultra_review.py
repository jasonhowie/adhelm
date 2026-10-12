"""Adversarial offline-boundary checks for strategy compilation and schema views."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_openai_compiler import _brief as openai_campaign_brief
from test_strategy import _six_strategy
from typer.testing import CliRunner, Result

from adhelm import strategy_cli
from adhelm.cli import app
from adhelm.strategy import OpenAIStrategyBrief, StrategyBrief


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_cli, "_utc_now", lambda: datetime(2026, 9, 5, tzinfo=UTC))


def _run(tmp_path: Path, input_file: Path) -> Result:
    return CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            "strategy",
            "--input",
            str(input_file),
        ],
    )


def _reject_key() -> bytes:
    pytest.fail("A rejected strategy must not access the sharing key")


def test_late_sixth_child_failure_emits_no_partial_candidate_or_private_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _six_strategy()
    unsafe_native = openai_campaign_brief(destination_url="https://example.test/demo?coupon=yes")
    unsafe_openai = OpenAIStrategyBrief(
        bidding_type="clicks",
        maximum_spend=unsafe_native.lifetime_budget,
        brief=unsafe_native,
    )
    late_failure = StrategyBrief.create(
        business_intent=source.business_intent,
        expected_accounts=source.expected_accounts,
        provider_briefs=(*source.provider_briefs[:-1], unsafe_openai),
        total_caps_by_currency=source.total_caps_by_currency,
        assumptions=source.assumptions,
        provenance=source.provenance,
        approval_owner=source.approval_owner,
        expires_at=source.expires_at,
    )
    input_file = tmp_path / "late-failure.json"
    input_file.write_text(late_failure.model_dump_json())
    monkeypatch.setattr(strategy_cli, "export_key", _reject_key)

    result = _run(tmp_path, input_file)

    assert result.exit_code == 3, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["data"] == {}
    assert envelope["diagnostics"][0]["code"] == "SENSITIVE_DATA_RISK"
    assert "coupon=yes" not in result.output
    assert "google_ads" not in result.output
    assert not (tmp_path / "data").exists()


def test_exact_one_mebibyte_strategy_input_is_accepted_without_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _six_strategy().model_dump_json().encode()
    input_file = tmp_path / "maximum.json"
    input_file.write_bytes(raw + b" " * (1_048_576 - len(raw)))
    assert input_file.stat().st_size == 1_048_576
    monkeypatch.setattr(strategy_cli, "export_key", lambda: b"k" * 32)

    result = _run(tmp_path, input_file)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["outcome"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert not (tmp_path / "data").exists()


def test_json_float_for_integer_money_is_rejected_before_canonicalization_or_key_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _six_strategy().model_dump(mode="json")
    data["total_caps_by_currency"][0]["minor_units"] = float(
        data["total_caps_by_currency"][0]["minor_units"]
    )
    input_file = tmp_path / "non-strict-money.json"
    input_file.write_text(json.dumps(data))
    monkeypatch.setattr(strategy_cli, "export_key", _reject_key)

    result = _run(tmp_path, input_file)

    assert result.exit_code == 2, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["data"] == {}
    assert envelope["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert not (tmp_path / "data").exists()


def test_strategy_preview_omits_raw_account_urns_copy_and_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _six_strategy()
    input_file = tmp_path / "private-source.json"
    input_file.write_text(source.model_dump_json())
    monkeypatch.setattr(strategy_cli, "export_key", lambda: b"k" * 32)

    result = _run(tmp_path, input_file)

    assert result.exit_code == 0, result.output
    private_fields = {
        "account_ref",
        "account_urn",
        "ad_name",
        "ad_group_name",
        "ad_text",
        "adset_name",
        "author_urn",
        "body",
        "campaign_name",
        "commentary",
        "destination_url",
        "file_id",
        "funding_instrument_id",
        "headline",
        "image_alt_text",
        "image_title",
        "image_urn",
        "name",
        "page_id",
        "pixel_id",
        "post_urn",
        "primary_text",
        "title",
    }
    private_values: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for name, item in value.items():
                if name in private_fields and isinstance(item, str):
                    private_values.add(item)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(source.model_dump(mode="json"))
    assert private_values
    assert all(value not in result.output for value in private_values)


def test_cli_fragments_reassemble_full_strategy_schema_without_reading_configuration(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text("not a valid configuration")
    runner = CliRunner()

    def schema(*arguments: str) -> dict[str, object]:
        result = runner.invoke(
            app,
            ["--data-dir", str(data_dir), "schema", "strategy-brief", *arguments],
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.output)["data"]

    full = schema()
    root = schema("--root-only")
    catalog = schema("--definitions")
    assert root["schema_digest"] == catalog["schema_digest"] == full["schema_digest"]
    assert root["definitions_required"] is True
    rebuilt = dict(root["schema"])
    rebuilt["$defs"] = {}
    for name in catalog["definitions"]:
        fragment = schema("--definition", name)
        assert fragment["schema_digest"] == full["schema_digest"]
        rebuilt["$defs"][name] = fragment["schema"]
    assert rebuilt == full["schema"]
    assert sorted(path.name for path in data_dir.iterdir()) == ["config.json"]


def test_conflicting_cli_schema_views_fail_closed_without_echoing_the_definition(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "schema",
            "strategy-brief",
            "--root-only",
            "--definition",
            "private@example.test",
        ],
    )

    assert result.exit_code == 2, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["diagnostics"][0]["code"] == "CONFLICTING_SCHEMA_VIEWS"
    assert "private@example.test" not in result.output
