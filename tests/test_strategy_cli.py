"""Agent output contains a complete six-provider review without its private source."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.text import Text
from test_strategy import _six_strategy
from typer.testing import CliRunner, Result

from adhelm import strategy_cli
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.models import Configuration, OutputPreferences
from adhelm.projections import _redact_payload
from adhelm.strategy import OpenAIStrategyCandidate, compile_strategy


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_cli, "_utc_now", lambda: datetime(2026, 9, 5, tzinfo=UTC))


def _run(tmp_path: Path, path: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            "strategy",
            "--input",
            str(path),
            *extra,
        ],
    )


def _no_key() -> bytes:
    pytest.fail("Rejected strategy must not access the sharing key")


def test_all_six_preview_exact_totals_digests_and_every_provider_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief = _six_strategy()
    compiled = compile_strategy(brief, now=strategy_cli._utc_now)
    path = tmp_path / "strategy.json"
    path.write_text(brief.model_dump_json())
    monkeypatch.setattr(strategy_cli, "export_key", lambda: b"k" * 32)
    result = _run(tmp_path, path)
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    data = envelope["data"]
    assert envelope["outcome"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert data["strategy_spec_digest"] == compiled.strategy_spec_digest
    assert data["input_digest"] == digest(brief)
    assert data["strategy_brief_digest"] == brief.brief_id.removeprefix("strategy:sha256:")
    assert data["total_caps_by_currency"] == [
        cap.model_dump(mode="json") for cap in brief.total_caps_by_currency
    ]
    assert data["maximum_spend_by_currency"] == [
        spend.model_dump(mode="json") for spend in compiled.maximum_spend_by_currency
    ]
    assert data["selected_provider_count"] == 6
    assert data["ordered_child_spec_digests"] == list(compiled.ordered_child_spec_digests)
    assert [child["provider"] for child in data["candidates"]] == [
        item.provider for item in compiled.candidates
    ]
    for child, candidate in zip(data["candidates"], compiled.candidates, strict=True):
        assert child["spec_digest"] == candidate.spec.spec_digest
        assert child["review"] == _redact_payload(
            candidate.review.model_dump(mode="json"), b"k" * 32, False
        )
        assert child["planning_boundary"] == candidate.spec.planning_boundary.model_dump(
            mode="json"
        )
        assert child["activation_requires_new_plan_and_authorization"] is True
        if isinstance(candidate, OpenAIStrategyCandidate):
            assert child["activation_boundary"] == candidate.spec.activation_boundary.model_dump(
                mode="json"
            )
            assert child["lossy_mappings"] == [] and child["unsupported_mappings"] == []
    assert data["persisted"] is False and data["remote_side_effects"] is False
    assert data["authorization_status"] == "NO_AUTHORITY_GRANTED"
    assert not (tmp_path / "data").exists()


def test_source_text_ids_and_native_requests_never_enter_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief = _six_strategy()
    path = tmp_path / "strategy.json"
    path.write_text(brief.model_dump_json())
    monkeypatch.setattr(strategy_cli, "export_key", lambda: b"k" * 32)
    result = _run(tmp_path, path)
    assert result.exit_code == 0, result.output
    for value in (
        brief.business_intent.statement,
        brief.approval_owner,
        *brief.assumptions,
        *brief.provenance,
    ):
        assert json.dumps(value) not in result.output
    for provider in brief.provider_briefs:
        assert json.dumps(provider.brief.account_ref) not in result.output
        label = provider.brief.model_dump(mode="json").get("campaign_name")
        if label is not None:
            assert json.dumps(label) not in result.output
    for field in (
        "source_brief",
        "campaign_request",
        "provider_briefs",
        "destination_url",
        "statement",
        "spec",
    ):
        assert json.dumps(field) + ":" not in result.output
    data = json.loads(result.output)["data"]
    assert data["approval_owner_ref"].startswith("opaque:")
    assert data["source_context"]["assumptions_count"] == len(brief.assumptions)
    assert data["source_context"]["provenance_count"] == len(brief.provenance)


def test_preview_bound_is_exact_and_precedes_sharing_key_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "strategy.json"
    path.write_text(_six_strategy().model_dump_json())
    monkeypatch.setattr(strategy_cli, "export_key", lambda: b"k" * 32)
    first = _run(tmp_path, path)
    assert first.exit_code == 0, first.output
    exact = len(first.output.encode())
    save_configuration(
        tmp_path / "data", Configuration(output=OutputPreferences(max_output_bytes=exact))
    )
    accepted = _run(tmp_path, path)
    assert accepted.exit_code == 0 and len(accepted.output.encode()) == exact
    save_configuration(
        tmp_path / "data",
        Configuration(output=OutputPreferences(max_output_bytes=exact - 1)),
        replace=True,
    )
    monkeypatch.setattr(strategy_cli, "export_key", _no_key)
    rejected = _run(tmp_path, path)
    assert rejected.exit_code == 3, rejected.output
    assert json.loads(rejected.output)["diagnostics"][0]["code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert sorted(p.name for p in (tmp_path / "data").iterdir()) == ["config.json"]


@pytest.mark.parametrize(
    "case", ["expired", "last_child_invalid", "over_cap", "oversize", "duplicate_key"]
)
def test_invalid_strategy_returns_no_partial_candidate_and_never_accesses_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    data = _six_strategy().model_dump(mode="json")
    if case == "expired":
        data["expires_at"] = "2026-09-05T00:00:00Z"
    elif case == "last_child_invalid":
        data["provider_briefs"][-1]["brief"]["audience_upload"] = ["private@example.test"]
    elif case == "over_cap":
        data["total_caps_by_currency"][0]["minor_units"] -= 1
    data["brief_id"] = "strategy:sha256:" + digest(
        {k: v for k, v in data.items() if k != "brief_id"}
    )
    raw = json.dumps(data)
    if case == "oversize":
        raw = " " * 1_048_577
    elif case == "duplicate_key":
        raw = '{"schema_version":1,"schema_version":1}'
    path = tmp_path / "invalid.json"
    path.write_text(raw)
    monkeypatch.setattr(strategy_cli, "export_key", _no_key)
    result = _run(tmp_path, path)
    assert result.exit_code == 2, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["data"] == {}
    assert "private@example.test" not in result.output
    if case == "expired":
        assert envelope["diagnostics"][0]["code"] == "STRATEGY_EXPIRED"
    assert not (tmp_path / "data").exists()


def test_discovery_schema_and_preview_expose_closed_retention_without_apply() -> None:
    runner = CliRunner()
    discovery = json.loads(runner.invoke(app, ["discover"]).output)["data"]
    assert "compile strategy" in discovery["commands"]
    assert "strategy-brief" in discovery["schemas"]
    assert "strategy-candidate-record" in discovery["schemas"]
    assert "strategy catalog" in discovery["commands"]
    assert "strategy show" in discovery["commands"]
    result = runner.invoke(app, ["schema", "strategy-brief"])
    assert result.exit_code == 0, result.output
    schema = json.loads(result.output)["data"]["schema"]
    assert schema["additionalProperties"] is False
    assert (
        schema["properties"]["provider_briefs"]["items"]["discriminator"]["propertyName"]
        == "provider"
    )
    help_text = Text.from_ansi(runner.invoke(app, ["compile", "strategy", "--help"]).output).plain
    assert "--retain" in help_text and "--apply" not in help_text
