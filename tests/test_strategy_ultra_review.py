"""Adversarial regressions for the closed offline strategy compiler."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from test_strategy import _six_strategy, _strategy

import adhelm.strategy as strategy_module
from adhelm.models import Money
from adhelm.strategy import (
    CompiledStrategy,
    ProviderStrategyBrief,
    StrategyBrief,
    StrategyCandidate,
    compile_strategy,
)

_NOW = datetime(2026, 9, 4, tzinfo=UTC)


def _recreate(brief: StrategyBrief, **updates: object) -> StrategyBrief:
    """Exercise the public factory with a model-copy/construct child."""
    values: dict[str, object] = {
        "business_intent": brief.business_intent,
        "expected_accounts": brief.expected_accounts,
        "provider_briefs": brief.provider_briefs,
        "total_caps_by_currency": brief.total_caps_by_currency,
        "assumptions": brief.assumptions,
        "provenance": brief.provenance,
        "approval_owner": brief.approval_owner,
        "expires_at": brief.expires_at,
    }
    values.update(updates)
    return StrategyBrief.create(**values)  # type: ignore[arg-type]


def _rebuild(compiled: CompiledStrategy, **updates: object) -> CompiledStrategy:
    """Use normal construction, which must not trust nested model instances."""
    values = {**compiled.__dict__, **updates}
    return CompiledStrategy(**values)


def test_factory_revalidates_forged_intent_account_native_brief_and_money() -> None:
    original = _strategy()
    invalid_intent = original.business_intent.model_copy(
        update={"requires_native_conversion": True}
    )
    invalid_account = original.expected_accounts[0].model_copy(
        update={"account_ref": "unsafe account ref"}
    )
    wrapper = original.provider_briefs[0]
    invalid_native = wrapper.brief.model_copy(update={"campaign_name": ""})
    invalid_wrapper = wrapper.model_copy(update={"brief": invalid_native})
    invalid_cap = Money.model_construct(minor_units=2_500.0, currency="USD")

    for updates in (
        {"business_intent": invalid_intent},
        {"expected_accounts": (invalid_account,)},
        {"provider_briefs": (invalid_wrapper,)},
        {"total_caps_by_currency": (invalid_cap,)},
    ):
        with pytest.raises(ValidationError):
            _recreate(original, **updates)


@pytest.mark.parametrize("field", ("assumptions", "provenance"))
def test_factory_rejects_blank_metadata_and_non_utc_expiry(field: str) -> None:
    original = _strategy()
    with pytest.raises(ValidationError, match="non-blank"):
        _recreate(original, **{field: ("   ",)})
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        _recreate(
            original,
            expires_at=datetime(2026, 10, 1, tzinfo=timezone(timedelta(hours=-5))),
        )


@pytest.mark.parametrize("field", ("maximum_spend_by_currency", "total_caps_by_currency"))
def test_compiled_strategy_revalidates_constructed_money(field: str) -> None:
    compiled = compile_strategy(_strategy(), now=lambda: _NOW)
    invalid_money = Money.model_construct(minor_units=2_500.0, currency="USD")
    with pytest.raises(ValidationError):
        _rebuild(compiled, **{field: (invalid_money,)})


def test_compiled_strategy_revalidates_forged_nested_candidate_spec() -> None:
    compiled = compile_strategy(_strategy(), now=lambda: _NOW)
    candidate = compiled.candidates[0]
    invalid_spend = Money.model_construct(minor_units=2_500.0, currency="USD")
    invalid_spec = candidate.spec.model_copy(update={"maximum_spend": invalid_spend})
    invalid_candidate = candidate.model_copy(update={"spec": invalid_spec})

    with pytest.raises(ValidationError):
        _rebuild(compiled, candidates=(invalid_candidate,))


def test_compile_boundary_rejects_a_constructed_brief_before_any_child_compile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _strategy()
    forged_intent = original.business_intent.model_copy(
        update={"requires_unreviewed_tracking": True}
    )
    forged = StrategyBrief.model_construct(
        **{**original.__dict__, "business_intent": forged_intent}
    )
    calls: list[str] = []

    def no_child_compile(item: ProviderStrategyBrief) -> StrategyCandidate:
        calls.append(item.provider)
        raise AssertionError("a forged strategy must fail before child compilation")

    monkeypatch.setattr(strategy_module, "_compile_one", no_child_compile)
    with pytest.raises(ValidationError):
        compile_strategy(forged, now=lambda: _NOW)

    assert calls == []


def test_late_selected_provider_returns_no_partial_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real_compile_one = strategy_module._compile_one

    def fail_only_after_the_first_five(item: ProviderStrategyBrief) -> StrategyCandidate:
        provider = item.provider
        calls.append(provider)
        if provider == "openai_ads":
            raise ValueError("forced late offline compiler failure")
        return real_compile_one(item)

    monkeypatch.setattr(strategy_module, "_compile_one", fail_only_after_the_first_five)
    result: CompiledStrategy | None = None
    with pytest.raises(ValueError, match="forced late offline compiler failure"):
        result = compile_strategy(_six_strategy(), now=lambda: _NOW)

    assert result is None
    assert calls == [
        "google_ads",
        "meta_ads",
        "linkedin_ads",
        "tiktok_ads",
        "reddit_ads",
        "openai_ads",
    ]


def test_child_reviews_preserve_offline_status_without_raw_copy_or_destination() -> None:
    compiled = compile_strategy(_six_strategy(), now=lambda: _NOW)
    reviews = json.dumps(
        [candidate.review.model_dump(mode="json") for candidate in compiled.candidates],
        sort_keys=True,
    )

    assert compiled.status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert compiled.fixture_only_status == "FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    assert all(
        candidate.spec.planning_boundary.status == compiled.status
        for candidate in compiled.candidates
    )
    assert "Customer supplied" not in reviews
    assert "https://example.test" not in reviews
    assert "Customer Google campaign" not in reviews
