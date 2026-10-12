"""Installed schema fragments reassemble exactly without customer configuration."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.errors import AdhelmError
from adhelm.models import Money
from adhelm.schema_views import installed_schema_view
from adhelm.strategy import StrategyBrief


def test_progressive_schema_reassembles_the_exact_full_contract() -> None:
    full = installed_schema_view(StrategyBrief, "strategy-brief")
    root = installed_schema_view(StrategyBrief, "strategy-brief", root_only=True)
    catalog = installed_schema_view(StrategyBrief, "strategy-brief", definitions=True)
    assert root["definitions_required"] is True
    assert "$defs" not in root["schema"]
    assert catalog["definitions"] == sorted(full["schema"]["$defs"])
    reconstructed = dict(root["schema"])
    reconstructed["$defs"] = {}
    for name in catalog["definitions"]:
        fragment = installed_schema_view(StrategyBrief, "strategy-brief", definition=name)
        assert fragment["schema_digest"] == full["schema_digest"]
        reconstructed["$defs"][name] = fragment["schema"]
    assert reconstructed == full["schema"]
    assert digest(reconstructed) == full["schema_digest"]
    assert root["schema_digest"] == catalog["schema_digest"] == full["schema_digest"]


@pytest.mark.parametrize(
    "args",
    [
        {"root_only": True, "definitions": True},
        {"definitions": True, "definition": "Money"},
        {"root_only": True, "definition": "Money"},
    ],
)
def test_conflicting_views_are_rejected(args: dict[str, object]) -> None:
    with pytest.raises(AdhelmError, match="Select only one"):
        installed_schema_view(StrategyBrief, "strategy-brief", **args)


def test_schema_without_definitions_has_an_empty_catalog_and_complete_root() -> None:
    full = installed_schema_view(Money, "money")
    root = installed_schema_view(Money, "money", root_only=True)
    assert root["definitions_required"] is False
    assert root["schema"] == full["schema"]
    assert installed_schema_view(Money, "money", definitions=True)["definitions"] == []


def test_cli_progressive_discovery_works_with_invalid_customer_configuration(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.json").write_text("invalid configuration; no credentials")
    runner = CliRunner()
    for suffix in (["--root-only"], ["--definitions"], ["--definition", "Money"]):
        result = runner.invoke(
            app, ["--data-dir", str(tmp_path), "schema", "strategy-brief", *suffix]
        )
        assert result.exit_code == 0, result.output
        assert len(result.output.encode()) < 65_536
        assert json.loads(result.output)["data"]["schema_digest"] == digest(
            StrategyBrief.model_json_schema()
        )
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json"]
    rejected = runner.invoke(
        app, ["schema", "strategy-brief", "--definition", "private@example.test"]
    )
    assert rejected.exit_code == 2, rejected.output
    assert "private@example.test" not in rejected.output
    assert json.loads(rejected.output)["diagnostics"][0]["code"] == "UNKNOWN_SCHEMA_DEFINITION"
