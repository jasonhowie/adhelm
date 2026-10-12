"""Adversarial checks for typed compiler money and offline preview boundaries."""

from __future__ import annotations

import json
import warnings
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from rich.text import Text
from typer.testing import CliRunner

from adhelm import additional_compile_cli, compile_cli
from adhelm.cli import app
from adhelm.models import Money

_CASES = (
    ("openai", "compile_openai_ads_campaign", "lifetime_budget", 100, 900_719_925_474),
    ("meta", "compile_meta_ads_campaign", "lifetime_budget", 1, 9_007_199_254_740_991),
    (
        "google_ads",
        "compile_google_ads_search_campaign",
        "total_budget",
        1,
        900_719_925_474,
    ),
    (
        "linkedin",
        "compile_linkedin_ads_campaign",
        "group_total_budget",
        1,
        9_007_199_254_740_991,
    ),
    ("reddit", "compile_reddit_ads_campaign", "lifetime_budget", 1, 900_719_925_474),
    ("tiktok", "compile_tiktok_ads_campaign", "total_budget", 1, 9_007_199_254_740_991),
)

_PREVIEW_MONEY_INPUTS = (
    ("openai", "openai-ads", "lifetime_budget"),
    ("meta", "meta-ads", "lifetime_budget"),
    ("google_ads", "google-ads", "total_budget"),
    ("linkedin", "linkedin-ads", "group_total_budget"),
    ("linkedin", "linkedin-ads", "daily_budget"),
    ("linkedin", "linkedin-ads", "unit_cost"),
    ("reddit", "reddit-ads", "lifetime_budget"),
    ("tiktok", "tiktok-ads", "total_budget"),
)


def _brief(provider: str) -> Any:
    return import_module(f"test_{provider}_compiler")._brief()


def _compiler(provider: str, name: str) -> Any:
    return getattr(import_module(f"adhelm.providers.{provider}_compiler"), name)


def _native_minor_units(provider: str, spec: Any) -> int:
    if provider == "openai":
        return spec.campaign_request.budget.lifetime_spend_limit_micros // 10_000
    if provider == "meta":
        return spec.campaign_request.lifetime_budget
    if provider == "google_ads":
        return spec.budget.request.total_amount_micros // 10_000
    if provider == "linkedin":
        return int(Decimal(spec.campaign_group_request.totalBudget.amount) * 100)
    if provider == "reddit":
        return spec.campaign_request.goal_value // 10_000
    return int(spec.adgroup_request.budget * 100)


@pytest.mark.parametrize(("provider", "compiler_name", "field", "minimum", "maximum"), _CASES)
def test_every_compiler_rejects_constructed_non_money_without_serializer_warnings(
    provider: str, compiler_name: str, field: str, minimum: int, maximum: int
) -> None:
    brief = _brief(provider)
    compile_brief = _compiler(provider, compiler_name)
    for minor_units, currency in (
        (True, "USD"),
        (float(minimum), "USD"),
        (minimum, "EUR"),
        (maximum + 1, "USD"),
    ):
        forged = Money.model_construct(minor_units=minor_units, currency=currency)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with pytest.raises(ValidationError):
                compile_brief(brief.model_copy(update={field: forged}))


@pytest.mark.parametrize(("provider", "compiler_name", "field", "minimum", "maximum"), _CASES)
def test_every_native_budget_is_exact_at_typed_money_boundaries(
    provider: str, compiler_name: str, field: str, minimum: int, maximum: int
) -> None:
    brief = _brief(provider)
    compile_brief = _compiler(provider, compiler_name)
    for minor_units in (minimum, maximum):
        updates: dict[str, Money] = {field: Money(minor_units=minor_units, currency="USD")}
        if provider == "linkedin":
            updates.update(
                {
                    "daily_budget": Money(minor_units=minimum, currency="USD"),
                    "unit_cost": Money(minor_units=minimum, currency="USD"),
                }
            )
        spec = compile_brief(brief.model_copy(update=updates))
        assert _native_minor_units(provider, spec) == minor_units
        assert type(spec).model_validate_json(spec.model_dump_json()) == spec


@pytest.mark.parametrize(
    ("provider", "compiler_name"), tuple((item[0], item[1]) for item in _CASES)
)
def test_rehashed_serialized_specs_cannot_diverge_from_typed_maximum_spend(
    provider: str, compiler_name: str
) -> None:
    spec = _compiler(provider, compiler_name)(_brief(provider))
    replacement = Money(minor_units=1, currency="USD")
    if provider == "openai":
        forged = spec.model_copy(
            update={
                "activation_boundary": spec.activation_boundary.model_copy(
                    update={"maximum_spend": replacement}
                )
            }
        )
    else:
        forged = spec.model_copy(update={"maximum_spend": replacement})
    with pytest.raises(ValidationError):
        type(spec).model_validate_json(forged.model_dump_json())


def test_openai_serialized_spec_requires_reviewed_usd_exponent_and_native_binding() -> None:
    spec = _compiler("openai", "compile_openai_ads_campaign")(_brief("openai"))
    serialized = spec.model_dump(mode="json")
    serialized["account_currency"] = "EUR"
    serialized["activation_boundary"]["maximum_spend"]["currency"] = "EUR"
    with pytest.raises(ValidationError):
        type(spec).model_validate_json(json.dumps(serialized))


def test_linkedin_reparse_rejects_unit_cost_outside_the_typed_money_ceiling() -> None:
    spec = _compiler("linkedin", "compile_linkedin_ads_campaign")(_brief("linkedin"))
    too_large = "90071992547409.92"
    forged_money = spec.campaign_template.request.unitCost.model_copy(update={"amount": too_large})
    forged_request = spec.campaign_template.request.model_copy(update={"unitCost": forged_money})
    forged = spec.model_copy(
        update={
            "campaign_template": spec.campaign_template.model_copy(
                update={"request": forged_request}
            )
        }
    )
    with pytest.raises(ValidationError, match="native money"):
        type(spec).model_validate(forged.model_dump(mode="python", warnings=False))

    serialized = spec.model_dump(mode="json")
    serialized["campaign_template"]["request"]["unitCost"]["amount"] = too_large
    with pytest.raises(ValidationError, match="canonical USD bounds"):
        type(spec).model_validate_json(json.dumps(serialized))


@pytest.mark.parametrize(("provider", "command", "field"), _PREVIEW_MONEY_INPUTS)
@pytest.mark.parametrize("invalid_value", ("float", "bool"))
def test_every_preview_rejects_non_strict_json_money_before_key_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    command: str,
    field: str,
    invalid_value: str,
) -> None:
    data = _brief(provider).model_dump(mode="json")
    original = data[field]["minor_units"]
    data[field]["minor_units"] = float(original) if invalid_value == "float" else True
    input_file = tmp_path / f"{provider}-{invalid_value}.json"
    input_file.write_text(json.dumps(data))

    def no_key() -> bytes:
        pytest.fail("Rejected money input must not access the sharing key")

    module = compile_cli if provider in {"openai", "meta", "google_ads"} else additional_compile_cli
    monkeypatch.setattr(module, "export_key", no_key)
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            command,
            "--input",
            str(input_file),
        ],
    )

    assert result.exit_code == 2, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["data"] == {}
    assert envelope["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("provider", ("linkedin", "reddit", "tiktok"))
def test_additional_preview_keeps_the_full_gate_set_and_redacts_local_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    brief = _brief(provider)
    spec = _compiler(provider, f"compile_{provider}_ads_campaign")(brief)
    input_file = tmp_path / "brief.json"
    input_file.write_text(brief.model_dump_json())
    monkeypatch.setattr(additional_compile_cli, "export_key", lambda: b"x" * 32)

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "compile",
            f"{provider}-ads",
            "--input",
            str(input_file),
        ],
    )

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    data = envelope["data"]
    assert envelope["outcome"] == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert data["planning_boundary"] == spec.planning_boundary.model_dump(mode="json")
    assert data["review"]["maximum_spend"] == spec.maximum_spend.model_dump(mode="json")
    assert data["persisted"] is False and data["remote_side_effects"] is False
    assert data["activation_requires_new_plan_and_authorization"] is True
    assert brief.campaign_name not in result.output
    assert '"campaign_request"' not in result.output
    for handle in (
        value
        for key, value in spec.redacted_review.model_dump(mode="json").items()
        if key.endswith("_handle")
    ):
        assert json.dumps(handle) not in result.output
    assert (
        "--retain"
        in Text.from_ansi(
            CliRunner().invoke(app, ["compile", f"{provider}-ads", "--help"]).output
        ).plain
    )
