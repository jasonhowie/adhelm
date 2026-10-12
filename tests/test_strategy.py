from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from test_google_ads_compiler import _brief as google_campaign_brief
from test_linkedin_compiler import _brief as linkedin_campaign_brief
from test_meta_compiler import _brief as meta_campaign_brief
from test_openai_compiler import _brief as openai_campaign_brief
from test_reddit_compiler import _brief as reddit_campaign_brief
from test_tiktok_compiler import _brief as tiktok_campaign_brief

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.models import Money
from adhelm.strategy import (
    CustomerDeclaredBusinessIntent,
    ExpectedAccountBinding,
    GoogleStrategyBrief,
    LinkedInStrategyBrief,
    MetaStrategyBrief,
    OpenAIStrategyBrief,
    RedditStrategyBrief,
    StrategyBrief,
    TikTokStrategyBrief,
    compile_strategy,
)


def _strategy(*, expires_at: datetime | None = None) -> StrategyBrief:
    campaign = reddit_campaign_brief()
    return StrategyBrief.create(
        business_intent=CustomerDeclaredBusinessIntent(
            statement="Customer requests a bounded existing-post traffic candidate."
        ),
        expected_accounts=(
            ExpectedAccountBinding(provider="reddit_ads", account_ref=campaign.account_ref),
        ),
        provider_briefs=(
            RedditStrategyBrief(
                provider="reddit_ads", maximum_spend=campaign.lifetime_budget, brief=campaign
            ),
        ),
        total_caps_by_currency=(
            Money(minor_units=campaign.lifetime_budget.minor_units, currency="USD"),
        ),
        assumptions=("Existing post, pixel, and funding references require readback.",),
        provenance=("CUSTOMER_DECLARED",),
        approval_owner="owner.strategy",
        expires_at=expires_at or datetime(2026, 10, 1, tzinfo=UTC),
    )


def _six_strategy() -> StrategyBrief:
    google, meta, linkedin = (
        google_campaign_brief(),
        meta_campaign_brief(),
        linkedin_campaign_brief(),
    )
    tiktok, reddit, openai = (
        tiktok_campaign_brief(),
        reddit_campaign_brief(),
        openai_campaign_brief(),
    )
    provider_briefs = (
        GoogleStrategyBrief(maximum_spend=google.total_budget, brief=google),
        MetaStrategyBrief(maximum_spend=meta.lifetime_budget, brief=meta),
        LinkedInStrategyBrief(maximum_spend=linkedin.group_total_budget, brief=linkedin),
        TikTokStrategyBrief(maximum_spend=tiktok.total_budget, brief=tiktok),
        RedditStrategyBrief(maximum_spend=reddit.lifetime_budget, brief=reddit),
        OpenAIStrategyBrief(
            bidding_type="clicks", maximum_spend=openai.lifetime_budget, brief=openai
        ),
    )
    total = sum(item.maximum_spend.minor_units for item in provider_briefs)
    return StrategyBrief.create(
        business_intent=CustomerDeclaredBusinessIntent(
            statement="Customer declared six distinct native campaign requests."
        ),
        expected_accounts=tuple(
            ExpectedAccountBinding(provider=item.provider, account_ref=item.brief.account_ref)
            for item in provider_briefs
        ),
        provider_briefs=provider_briefs,
        total_caps_by_currency=(Money(minor_units=total, currency="USD"),),
        assumptions=("Each provider requires its own live promotion checks.",),
        provenance=("CUSTOMER_DECLARED",),
        approval_owner="owner.strategy",
        expires_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_compiles_all_selected_native_briefs_to_one_offline_aggregate() -> None:
    result = compile_strategy(_strategy(), now=lambda: datetime(2026, 9, 4, tzinfo=UTC))
    assert result.status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert result.unmet_live_proof == "ALL_SELECTED_PROVIDER_BOUNDARIES_UNMET"
    assert result.candidates[0].provider == "reddit_ads"
    assert result.ordered_child_spec_digests == (result.candidates[0].spec.spec_digest,)
    assert result.maximum_spend_by_currency == (Money(minor_units=2_500, currency="USD"),)
    assert result.candidates[0].spec.planning_boundary.status == result.status


def test_compiles_all_six_without_objective_translation_or_money_loss() -> None:
    brief = _six_strategy()
    result = compile_strategy(brief, now=lambda: datetime(2026, 9, 4, tzinfo=UTC))

    assert tuple(item.provider for item in result.candidates) == (
        "google_ads",
        "meta_ads",
        "linkedin_ads",
        "tiktok_ads",
        "reddit_ads",
        "openai_ads",
    )
    assert result.ordered_child_spec_digests == tuple(
        item.spec.spec_digest for item in result.candidates
    )
    assert result.maximum_spend_by_currency == (
        Money(
            currency="USD",
            minor_units=sum(item.maximum_spend.minor_units for item in brief.provider_briefs),
        ),
    )
    assert all(item.spec.planning_boundary.status == result.status for item in result.candidates)

    assert result.source_brief == brief
    assert result.total_caps_by_currency == brief.total_caps_by_currency
    assert result.approval_owner == brief.approval_owner
    assert result.expires_at == brief.expires_at
    assert result.provenance == brief.provenance
    assert result.candidates[0].spec.campaign_request.advertising_channel_type == "SEARCH"
    assert result.candidates[0].spec.campaign_request.target_spend is not None
    assert result.candidates[3].review.video_asset_status == "DECLARED_UNVERIFIED_NO_LOCAL_DECODE"
    assert (
        result.candidates[3].review.video_execution_eligibility
        == "REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET"
    )
    assert result.candidates[5].spec.activation_boundary.maximum_spend == Money(
        minor_units=2_500, currency="USD"
    )
    assert result.candidates[5].spec.lossy_mappings == ()
    assert result.candidates[5].spec.unsupported_mappings == ()


def test_rejects_duplicate_provider_account_mismatch_and_over_cap() -> None:
    original = _strategy()
    campaign = reddit_campaign_brief()
    duplicate = RedditStrategyBrief(maximum_spend=campaign.lifetime_budget, brief=campaign)
    with pytest.raises(ValidationError, match="only once"):
        StrategyBrief.create(
            business_intent=original.business_intent,
            provider_briefs=(original.provider_briefs[0], duplicate),
            expected_accounts=(
                original.expected_accounts[0],
                ExpectedAccountBinding(provider="reddit_ads", account_ref=campaign.account_ref),
            ),
            total_caps_by_currency=original.total_caps_by_currency,
            assumptions=original.assumptions,
            provenance=original.provenance,
            approval_owner=original.approval_owner,
            expires_at=original.expires_at,
        )


def test_six_provider_cap_account_and_order_are_exact() -> None:
    original = _six_strategy()
    total = original.total_caps_by_currency[0].minor_units
    with pytest.raises(ValidationError, match="exceed"):
        StrategyBrief.create(
            business_intent=original.business_intent,
            expected_accounts=original.expected_accounts,
            provider_briefs=original.provider_briefs,
            total_caps_by_currency=(Money(minor_units=total - 1, currency="USD"),),
            assumptions=original.assumptions,
            provenance=original.provenance,
            approval_owner=original.approval_owner,
            expires_at=original.expires_at,
        )
    wrong_account = (
        ExpectedAccountBinding(provider="google_ads", account_ref="google_ads.account.wrong"),
        *original.expected_accounts[1:],
    )
    with pytest.raises(ValidationError, match="does not match"):
        StrategyBrief.create(
            business_intent=original.business_intent,
            expected_accounts=wrong_account,
            provider_briefs=original.provider_briefs,
            total_caps_by_currency=original.total_caps_by_currency,
            assumptions=original.assumptions,
            provenance=original.provenance,
            approval_owner=original.approval_owner,
            expires_at=original.expires_at,
        )

    reversed_brief = StrategyBrief.create(
        business_intent=original.business_intent,
        expected_accounts=tuple(reversed(original.expected_accounts)),
        provider_briefs=tuple(reversed(original.provider_briefs)),
        total_caps_by_currency=original.total_caps_by_currency,
        assumptions=original.assumptions,
        provenance=original.provenance,
        approval_owner=original.approval_owner,
        expires_at=original.expires_at,
    )
    assert reversed_brief.brief_id != original.brief_id
    reversed_result = compile_strategy(reversed_brief, now=lambda: datetime(2026, 9, 4, tzinfo=UTC))
    assert tuple(item.provider for item in reversed_result.candidates) == tuple(
        reversed(tuple(item.provider for item in original.provider_briefs))
    )
    with pytest.raises(ValidationError, match="exceed"):
        StrategyBrief.create(
            business_intent=original.business_intent,
            expected_accounts=original.expected_accounts,
            provider_briefs=original.provider_briefs,
            total_caps_by_currency=(Money(minor_units=2_499, currency="USD"),),
            assumptions=original.assumptions,
            provenance=original.provenance,
            approval_owner=original.approval_owner,
            expires_at=original.expires_at,
        )


def test_fails_closed_for_expiry_unsupported_requirements_and_tamper() -> None:
    with pytest.raises(ValueError, match="expired"):
        compile_strategy(
            _strategy(expires_at=datetime(2026, 9, 4, tzinfo=UTC)),
            now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
        )
    with pytest.raises(ValidationError, match="literal"):
        CustomerDeclaredBusinessIntent(
            statement="Require conversion mapping.", requires_native_conversion=True
        )

    original = _strategy()
    forged = StrategyBrief.model_construct(
        **{**original.__dict__, "expires_at": datetime(2028, 1, 1, tzinfo=UTC)}
    )
    with pytest.raises(ValidationError, match="brief_id"):
        compile_strategy(forged, now=lambda: datetime(2026, 9, 4, tzinfo=UTC))

    compiled = compile_strategy(_strategy(), now=lambda: datetime(2026, 9, 4, tzinfo=UTC))
    serialized = compiled.model_dump(mode="json")
    serialized["ordered_child_spec_digests"] = ["0" * 64]
    with pytest.raises(ValidationError, match="ordered digests"):
        type(compiled).model_validate_json(json.dumps(serialized))


def test_revalidates_nested_money_and_rehashed_serialized_children() -> None:
    original = _six_strategy()
    serialized_brief = original.model_dump(mode="json")
    serialized_brief["total_caps_by_currency"][0]["minor_units"] = True
    with pytest.raises(ValidationError):
        StrategyBrief.model_validate_json(json.dumps(serialized_brief))
    serialized_brief = original.model_dump(mode="json")
    serialized_brief["provider_briefs"][0]["maximum_spend"]["minor_units"] = 25_000.0
    with pytest.raises(ValidationError):
        StrategyBrief.model_validate_json(json.dumps(serialized_brief))

    compiled = compile_strategy(original, now=lambda: datetime(2026, 9, 4, tzinfo=UTC))
    serialized = compiled.model_dump(mode="json")
    reddit_spec = serialized["candidates"][4]["spec"]
    reddit_spec["campaign_request"]["goal_value"] += 10_000
    serialized["ordered_child_spec_digests"][4] = digest(reddit_spec)
    with pytest.raises(ValidationError, match="lifetime exposure|children must exactly match"):
        type(compiled).model_validate_json(json.dumps(serialized))

    serialized = compiled.model_dump(mode="json")
    serialized["candidates"][4]["spec"]["campaign_request"]["name"] = "Substituted candidate"
    serialized["ordered_child_spec_digests"][4] = digest(serialized["candidates"][4]["spec"])
    with pytest.raises(ValidationError, match="children must exactly match"):
        type(compiled).model_validate_json(json.dumps(serialized))

    serialized = compiled.model_dump(mode="json")
    serialized["business_intent"]["statement"] = "Substituted declared context."
    with pytest.raises(ValidationError, match="context must exactly bind"):
        type(compiled).model_validate_json(json.dumps(serialized))


def test_late_invalid_selected_provider_returns_no_aggregate() -> None:
    original = _six_strategy()
    invalid_openai = openai_campaign_brief(destination_url="https://example.test/demo?coupon=yes")
    invalid = OpenAIStrategyBrief(
        bidding_type="clicks", maximum_spend=invalid_openai.lifetime_budget, brief=invalid_openai
    )
    late_failure = StrategyBrief.create(
        business_intent=original.business_intent,
        expected_accounts=original.expected_accounts,
        provider_briefs=(*original.provider_briefs[:-1], invalid),
        total_caps_by_currency=original.total_caps_by_currency,
        assumptions=original.assumptions,
        provenance=original.provenance,
        approval_owner=original.approval_owner,
        expires_at=original.expires_at,
    )
    with pytest.raises(AdhelmError, match="controlled UTMs"):
        compile_strategy(late_failure, now=lambda: datetime(2026, 9, 4, tzinfo=UTC))
