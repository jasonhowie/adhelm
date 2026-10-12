"""Currency-bound budgets must compile into exact provider-native units."""

import json
from importlib import import_module
from typing import Any

import pytest
from pydantic import ValidationError

from adhelm.models import Money

_CASES = (
    ("openai", "compile_openai_ads_campaign", "lifetime_budget", 100, 900_719_925_474),
    ("meta", "compile_meta_ads_campaign", "lifetime_budget", 1, 9_007_199_254_740_991),
    ("google_ads", "compile_google_ads_search_campaign", "total_budget", 1, 900_719_925_474),
    ("reddit", "compile_reddit_ads_campaign", "lifetime_budget", 1, 900_719_925_474),
)


def _fixture(provider: str) -> Any:
    return import_module(f"test_{provider}_compiler")._brief()


@pytest.mark.parametrize(("provider", "compiler", "field", "minimum", "maximum"), _CASES)
def test_typed_money_bounds_have_exact_native_representation(
    provider: str, compiler: str, field: str, minimum: int, maximum: int
) -> None:
    compile_brief = getattr(import_module(f"adhelm.providers.{provider}_compiler"), compiler)
    for minor_units in (minimum, maximum):
        money = Money(minor_units=minor_units, currency="USD")
        brief = _fixture(provider).model_copy(update={field: money})
        spec = compile_brief(brief)
        if provider == "openai":
            assert spec.activation_boundary.maximum_spend == money
            assert spec.campaign_request.budget.lifetime_spend_limit_micros == minor_units * 10_000
        elif provider == "meta":
            assert spec.maximum_spend == money
            assert spec.campaign_request.lifetime_budget == minor_units
        elif provider == "google_ads":
            assert spec.maximum_spend == money
            assert spec.budget.request.total_amount_micros == minor_units * 10_000
        else:
            assert spec.maximum_spend == money
            assert spec.campaign_request.goal_value == minor_units * 10_000
            assert spec.redacted_review.maximum_spend == money
        assert (
            type(spec).model_validate_json(spec.model_dump_json()).spec_digest == spec.spec_digest
        )


@pytest.mark.parametrize(("provider", "compiler", "field", "minimum", "maximum"), _CASES)
def test_budget_currency_scalar_and_nested_validation_bypasses_are_rejected(
    provider: str, compiler: str, field: str, minimum: int, maximum: int
) -> None:
    original = _fixture(provider)
    compile_brief = getattr(import_module(f"adhelm.providers.{provider}_compiler"), compiler)
    for invalid in (12, "12", {"minor_units": 12}, {"minor_units": 12, "currency": "EUR"}):
        raw = original.model_dump(mode="json")
        raw[field] = invalid
        with pytest.raises(ValidationError):
            type(original).model_validate_json(json.dumps(raw))
    # Do not rely on the annotation or a frozen outer brief as proof that the
    # nested currency and amount were validated.
    for minor_units, currency in (
        (minimum - 1, "USD"),
        (maximum + 1, "USD"),
        (minimum, "EUR"),
        (True, "USD"),
        (float(minimum), "USD"),
    ):
        forged = Money.model_construct(minor_units=minor_units, currency=currency)
        brief = original.model_copy(update={field: forged})
        with pytest.raises(ValidationError):
            compile_brief(brief)
