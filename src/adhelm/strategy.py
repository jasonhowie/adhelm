"""Closed offline compilation of selected provider-native campaign briefs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

from pydantic import Field, TypeAdapter, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.models import Money, SafeRef, StrictModel
from adhelm.providers.google_ads_compiler import (
    GoogleAdsCompiledCampaignSpec,
    GoogleAdsRedactedReview,
    GoogleAdsSearchCampaignBrief,
    compile_google_ads_search_campaign,
)
from adhelm.providers.linkedin_compiler import (
    LinkedInAdsCampaignBrief,
    LinkedInAdsCompiledCampaignSpec,
    LinkedInAdsRedactedReview,
    compile_linkedin_ads_campaign,
)
from adhelm.providers.meta_compiler import (
    MetaAdsCampaignBrief,
    MetaAdsCompiledCampaignSpec,
    MetaAdsRedactedReview,
    compile_meta_ads_campaign,
)
from adhelm.providers.openai_compiler import (
    OpenAIAdsCampaignBrief,
    OpenAIAdsCompiledCampaignSpec,
    OpenAIAdsRedactedReview,
    compile_openai_ads_campaign,
)
from adhelm.providers.reddit_compiler import (
    RedditAdsCampaignBrief,
    RedditAdsCompiledCampaignSpec,
    RedditAdsRedactedReview,
    compile_reddit_ads_campaign,
)
from adhelm.providers.tiktok_compiler import (
    TikTokAdsCampaignBrief,
    TikTokAdsCompiledCampaignSpec,
    TikTokAdsRedactedReview,
    compile_tiktok_ads_campaign,
)

Provider = Literal[
    "google_ads", "meta_ads", "linkedin_ads", "tiktok_ads", "reddit_ads", "openai_ads"
]
_ID_PREFIX = "strategy:sha256:"
_MONEY_TUPLE_ADAPTER: TypeAdapter[tuple[Money, ...]] = TypeAdapter(tuple[Money, ...])


def _revalidate_money_instances(value: object) -> object:
    """Do not let a forged nested ``Money`` instance skip strict validation."""
    if not isinstance(value, (tuple, list)):
        return value
    if any(isinstance(item, Money) for item in value):
        return tuple(
            Money.model_validate(item.model_dump(mode="python", warnings=False))
            if isinstance(item, Money)
            else item
            for item in value
        )
    # ``model_dump(mode="python")`` retains date/time values in sibling
    # fields.  Let the typed adapter produce JSON rather than calling
    # ``json.dumps`` on that Python representation, then parse it strictly.
    return _MONEY_TUPLE_ADAPTER.validate_json(
        _MONEY_TUPLE_ADAPTER.dump_json(cast(Any, value), warnings=False)
    )


class CustomerDeclaredBusinessIntent(StrictModel):
    """Intent is customer text, never an objective-translation instruction."""

    statement: str = Field(min_length=1, max_length=1_000)
    requires_native_conversion: Literal[False] = False
    requires_unreviewed_targeting: Literal[False] = False
    requires_unreviewed_tracking: Literal[False] = False

    @model_validator(mode="after")
    def non_blank(self) -> CustomerDeclaredBusinessIntent:
        if not self.statement.strip():
            raise ValueError("business intent statement must include a non-space character")
        return self


class ExpectedAccountBinding(StrictModel):
    provider: Provider
    account_ref: SafeRef


_EXPECTED_ACCOUNT_TUPLE_ADAPTER: TypeAdapter[tuple[ExpectedAccountBinding, ...]] = TypeAdapter(
    tuple[ExpectedAccountBinding, ...]
)


def _revalidate_expected_account_instances(value: object) -> object:
    """Do not let a forged expected-account instance skip its closed fields."""
    if not isinstance(value, (tuple, list)):
        return value
    if any(isinstance(item, ExpectedAccountBinding) for item in value):
        return tuple(
            _EXPECTED_ACCOUNT_TUPLE_ADAPTER.validate_python(
                (item.model_dump(mode="python", warnings=False),)
            )[0]
            if isinstance(item, ExpectedAccountBinding)
            else item
            for item in value
        )
    return _EXPECTED_ACCOUNT_TUPLE_ADAPTER.validate_json(
        _EXPECTED_ACCOUNT_TUPLE_ADAPTER.dump_json(cast(Any, value), warnings=False)
    )


class GoogleStrategyBrief(StrictModel):
    provider: Literal["google_ads"] = "google_ads"
    advertising_channel_type: Literal["SEARCH"] = "SEARCH"
    bidding_strategy_field: Literal["target_spend"] = "target_spend"
    maximum_spend: Money
    brief: GoogleAdsSearchCampaignBrief

    @model_validator(mode="after")
    def exact_native_budget(self) -> GoogleStrategyBrief:
        if self.maximum_spend != self.brief.total_budget:
            raise ValueError("Google maximum spend must exactly equal the native total budget")
        return self


class MetaStrategyBrief(StrictModel):
    provider: Literal["meta_ads"] = "meta_ads"
    native_objective: Literal["OUTCOME_TRAFFIC"] = "OUTCOME_TRAFFIC"
    maximum_spend: Money
    brief: MetaAdsCampaignBrief

    @model_validator(mode="after")
    def exact_native_budget(self) -> MetaStrategyBrief:
        if self.maximum_spend != self.brief.lifetime_budget:
            raise ValueError("Meta maximum spend must exactly equal the native lifetime budget")
        return self


class LinkedInStrategyBrief(StrictModel):
    provider: Literal["linkedin_ads"] = "linkedin_ads"
    native_objective: Literal["ENGAGEMENT"] = "ENGAGEMENT"
    maximum_spend: Money
    brief: LinkedInAdsCampaignBrief

    @model_validator(mode="after")
    def exact_native_budget(self) -> LinkedInStrategyBrief:
        if self.maximum_spend != self.brief.group_total_budget:
            raise ValueError("LinkedIn maximum spend must exactly equal the group total budget")
        return self


class TikTokStrategyBrief(StrictModel):
    provider: Literal["tiktok_ads"] = "tiktok_ads"
    native_objective: Literal["TRAFFIC"] = "TRAFFIC"
    maximum_spend: Money
    brief: TikTokAdsCampaignBrief

    @model_validator(mode="after")
    def exact_native_budget(self) -> TikTokStrategyBrief:
        if self.maximum_spend != self.brief.total_budget:
            raise ValueError("TikTok maximum spend must exactly equal the native total budget")
        return self


class RedditStrategyBrief(StrictModel):
    provider: Literal["reddit_ads"] = "reddit_ads"
    native_objective: Literal["CLICKS"] = "CLICKS"
    maximum_spend: Money
    brief: RedditAdsCampaignBrief

    @model_validator(mode="after")
    def exact_native_budget(self) -> RedditStrategyBrief:
        if self.maximum_spend != self.brief.lifetime_budget:
            raise ValueError("Reddit maximum spend must exactly equal the native lifetime budget")
        return self


class OpenAIStrategyBrief(StrictModel):
    provider: Literal["openai_ads"] = "openai_ads"
    bidding_type: Literal["impressions", "clicks"]
    maximum_spend: Money
    brief: OpenAIAdsCampaignBrief

    @model_validator(mode="after")
    def exact_native_budget_and_objective(self) -> OpenAIStrategyBrief:
        if self.maximum_spend != self.brief.lifetime_budget:
            raise ValueError("OpenAI maximum spend must exactly equal the native lifetime budget")
        if self.bidding_type != self.brief.bidding_type:
            raise ValueError(
                "OpenAI strategy bidding type must exactly equal the native bidding type"
            )
        return self


ProviderStrategyBrief = Annotated[
    GoogleStrategyBrief
    | MetaStrategyBrief
    | LinkedInStrategyBrief
    | TikTokStrategyBrief
    | RedditStrategyBrief
    | OpenAIStrategyBrief,
    Field(discriminator="provider"),
]
_PROVIDER_BRIEF_TUPLE_ADAPTER: TypeAdapter[tuple[ProviderStrategyBrief, ...]] = TypeAdapter(
    tuple[ProviderStrategyBrief, ...]
)
_PROVIDER_STRATEGY_BRIEF_TYPES = (
    GoogleStrategyBrief,
    MetaStrategyBrief,
    LinkedInStrategyBrief,
    TikTokStrategyBrief,
    RedditStrategyBrief,
    OpenAIStrategyBrief,
)


def _revalidate_provider_brief_instances(value: object) -> object:
    """Rebuild nested native briefs rather than trusting wrapper instances."""
    if not isinstance(value, (tuple, list)):
        return value
    if any(isinstance(item, _PROVIDER_STRATEGY_BRIEF_TYPES) for item in value):
        return tuple(
            _PROVIDER_BRIEF_TUPLE_ADAPTER.validate_python(
                (item.model_dump(mode="python", warnings=False),)
            )[0]
            if isinstance(item, _PROVIDER_STRATEGY_BRIEF_TYPES)
            else item
            for item in value
        )
    return _PROVIDER_BRIEF_TUPLE_ADAPTER.validate_json(
        _PROVIDER_BRIEF_TUPLE_ADAPTER.dump_json(cast(Any, value), warnings=False)
    )


class StrategyBrief(StrictModel):
    """Immutable local aggregate; it is neither a plan nor an authorization."""

    schema_version: Literal[1] = 1
    brief_id: str = Field(pattern=r"^strategy:sha256:[0-9a-f]{64}$")
    business_intent: CustomerDeclaredBusinessIntent
    expected_accounts: tuple[ExpectedAccountBinding, ...] = Field(min_length=1, max_length=6)
    provider_briefs: tuple[ProviderStrategyBrief, ...] = Field(min_length=1, max_length=6)
    total_caps_by_currency: tuple[Money, ...] = Field(min_length=1, max_length=6)
    assumptions: tuple[str, ...] = Field(max_length=50)
    provenance: tuple[str, ...] = Field(min_length=1, max_length=20)
    approval_owner: SafeRef
    expires_at: datetime

    @field_validator("business_intent", mode="before")
    @classmethod
    def revalidate_business_intent_instance(cls, value: object) -> object:
        """A model-copy/construct intent must obey the closed requirement flags."""
        if isinstance(value, CustomerDeclaredBusinessIntent):
            return CustomerDeclaredBusinessIntent.model_validate_json(
                value.model_dump_json(warnings=False)
            )
        return value

    @field_validator("total_caps_by_currency", mode="before")
    @classmethod
    def revalidate_total_cap_instances(cls, value: object) -> object:
        return _revalidate_money_instances(value)

    @field_validator("expected_accounts", mode="before")
    @classmethod
    def revalidate_expected_account_instances(cls, value: object) -> object:
        return _revalidate_expected_account_instances(value)

    @field_validator("provider_briefs", mode="before")
    @classmethod
    def revalidate_provider_brief_instances(cls, value: object) -> object:
        return _revalidate_provider_brief_instances(value)

    @field_validator("assumptions", "provenance")
    @classmethod
    def non_blank_metadata(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("strategy assumptions and provenance entries must be non-blank")
        return value

    @classmethod
    def create(
        cls,
        *,
        business_intent: CustomerDeclaredBusinessIntent,
        expected_accounts: tuple[ExpectedAccountBinding, ...],
        provider_briefs: tuple[ProviderStrategyBrief, ...],
        total_caps_by_currency: tuple[Money, ...],
        assumptions: tuple[str, ...],
        provenance: tuple[str, ...],
        approval_owner: str,
        expires_at: datetime,
    ) -> StrategyBrief:
        """Create the only accepted immutable ID from a complete brief payload."""
        draft = cls.model_construct(
            brief_id="",
            business_intent=business_intent,
            expected_accounts=expected_accounts,
            provider_briefs=provider_briefs,
            total_caps_by_currency=total_caps_by_currency,
            assumptions=assumptions,
            provenance=provenance,
            approval_owner=approval_owner,
            expires_at=expires_at,
        )
        identifier = _ID_PREFIX + digest(
            draft.model_dump(mode="json", exclude={"brief_id"}, warnings=False)
        )
        return cls(
            brief_id=identifier,
            business_intent=business_intent,
            expected_accounts=expected_accounts,
            provider_briefs=provider_briefs,
            total_caps_by_currency=total_caps_by_currency,
            assumptions=assumptions,
            provenance=provenance,
            approval_owner=approval_owner,
            expires_at=expires_at,
        )

    @model_validator(mode="after")
    def exact_selection_identity_and_caps(self) -> StrategyBrief:
        providers = tuple(item.provider for item in self.provider_briefs)
        accounts = tuple(item.provider for item in self.expected_accounts)
        currencies = tuple(item.currency for item in self.total_caps_by_currency)
        if len(set(providers)) != len(providers):
            raise ValueError("a strategy may select each provider only once")
        if len(set(accounts)) != len(accounts) or set(accounts) != set(providers):
            raise ValueError("expected accounts must contain each selected provider exactly once")
        if len(set(currencies)) != len(currencies) or any(
            type(cap.minor_units) is not int or not 1 <= cap.minor_units <= 9_007_199_254_740_991
            for cap in self.total_caps_by_currency
        ):
            raise ValueError("currency total caps must be positive and unique")
        account_by_provider = {item.provider: item.account_ref for item in self.expected_accounts}
        spends: dict[str, int] = {}
        for item in self.provider_briefs:
            if account_by_provider[item.provider] != item.brief.account_ref:
                raise ValueError("selected provider brief does not match its expected account")
            spends[item.maximum_spend.currency] = (
                spends.get(item.maximum_spend.currency, 0) + item.maximum_spend.minor_units
            )
        caps = {item.currency: item.minor_units for item in self.total_caps_by_currency}
        if set(caps) != set(spends) or any(
            spends[currency] > caps[currency] for currency in spends
        ):
            raise ValueError("selected provider budgets exceed or lack a typed currency total cap")
        identifier_fields = self.model_dump(mode="json", exclude={"brief_id"})
        if self.brief_id != _ID_PREFIX + digest(identifier_fields):
            raise ValueError("brief_id must be the immutable digest of the complete strategy brief")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() != timedelta(0):
            raise ValueError("strategy expiry must be timezone-aware UTC")
        return self


class GoogleStrategyCandidate(StrictModel):
    provider: Literal["google_ads"] = "google_ads"
    spec: GoogleAdsCompiledCampaignSpec
    review: GoogleAdsRedactedReview


class MetaStrategyCandidate(StrictModel):
    provider: Literal["meta_ads"] = "meta_ads"
    spec: MetaAdsCompiledCampaignSpec
    review: MetaAdsRedactedReview


class LinkedInStrategyCandidate(StrictModel):
    provider: Literal["linkedin_ads"] = "linkedin_ads"
    spec: LinkedInAdsCompiledCampaignSpec
    review: LinkedInAdsRedactedReview


class TikTokStrategyCandidate(StrictModel):
    provider: Literal["tiktok_ads"] = "tiktok_ads"
    spec: TikTokAdsCompiledCampaignSpec
    review: TikTokAdsRedactedReview


class RedditStrategyCandidate(StrictModel):
    provider: Literal["reddit_ads"] = "reddit_ads"
    spec: RedditAdsCompiledCampaignSpec
    review: RedditAdsRedactedReview


class OpenAIStrategyCandidate(StrictModel):
    provider: Literal["openai_ads"] = "openai_ads"
    spec: OpenAIAdsCompiledCampaignSpec
    review: OpenAIAdsRedactedReview


StrategyCandidate = Annotated[
    GoogleStrategyCandidate
    | MetaStrategyCandidate
    | LinkedInStrategyCandidate
    | TikTokStrategyCandidate
    | RedditStrategyCandidate
    | OpenAIStrategyCandidate,
    Field(discriminator="provider"),
]
_STRATEGY_CANDIDATE_TUPLE_ADAPTER: TypeAdapter[tuple[StrategyCandidate, ...]] = TypeAdapter(
    tuple[StrategyCandidate, ...]
)
_STRATEGY_CANDIDATE_TYPES = (
    GoogleStrategyCandidate,
    MetaStrategyCandidate,
    LinkedInStrategyCandidate,
    TikTokStrategyCandidate,
    RedditStrategyCandidate,
    OpenAIStrategyCandidate,
)

_STRATEGY_BRIEF_ADAPTER: TypeAdapter[StrategyBrief] = TypeAdapter(StrategyBrief)


def _revalidate_candidate_instances(value: object) -> object:
    """Rebuild typed child specs and reviews before comparing them to source."""
    if not isinstance(value, (tuple, list)):
        return value
    if any(isinstance(item, _STRATEGY_CANDIDATE_TYPES) for item in value):
        return tuple(
            _STRATEGY_CANDIDATE_TUPLE_ADAPTER.validate_python(
                (item.model_dump(mode="python", warnings=False),)
            )[0]
            if isinstance(item, _STRATEGY_CANDIDATE_TYPES)
            else item
            for item in value
        )
    return _STRATEGY_CANDIDATE_TUPLE_ADAPTER.validate_json(
        _STRATEGY_CANDIDATE_TUPLE_ADAPTER.dump_json(cast(Any, value), warnings=False)
    )


def _candidate_maximum_spend(item: StrategyCandidate) -> Money:
    if isinstance(item, OpenAIStrategyCandidate):
        return item.spec.activation_boundary.maximum_spend
    return item.spec.maximum_spend


class CompiledStrategy(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    fixture_only_status: Literal["FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = (
        "FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    )
    # This is a closed local input model, not a raw provider response or a
    # transport payload.  Retaining it makes JSON revalidation able to prove
    # every compiled child still corresponds to the reviewed source.
    source_brief: StrategyBrief
    strategy_brief_id: str = Field(pattern=r"^strategy:sha256:[0-9a-f]{64}$")
    business_intent: CustomerDeclaredBusinessIntent
    expected_accounts: tuple[ExpectedAccountBinding, ...] = Field(min_length=1, max_length=6)
    total_caps_by_currency: tuple[Money, ...] = Field(min_length=1, max_length=6)
    assumptions: tuple[str, ...] = Field(max_length=50)
    provenance: tuple[str, ...] = Field(min_length=1, max_length=20)
    approval_owner: SafeRef
    expires_at: datetime
    candidates: tuple[StrategyCandidate, ...] = Field(min_length=1, max_length=6)
    ordered_child_spec_digests: tuple[str, ...] = Field(min_length=1, max_length=6)
    maximum_spend_by_currency: tuple[Money, ...] = Field(min_length=1, max_length=6)
    unmet_live_proof: Literal["ALL_SELECTED_PROVIDER_BOUNDARIES_UNMET"] = (
        "ALL_SELECTED_PROVIDER_BOUNDARIES_UNMET"
    )
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_brief", mode="before")
    @classmethod
    def revalidate_source_brief_instance(cls, value: object) -> object:
        """The aggregate must not trust a model-copy/construct source brief."""
        if isinstance(value, StrategyBrief):
            return _STRATEGY_BRIEF_ADAPTER.validate_json(
                _STRATEGY_BRIEF_ADAPTER.dump_json(cast(Any, value), warnings=False)
            )
        if isinstance(value, dict):
            return _STRATEGY_BRIEF_ADAPTER.validate_json(
                _STRATEGY_BRIEF_ADAPTER.dump_json(cast(Any, value), warnings=False)
            )
        return value

    @field_validator("maximum_spend_by_currency", mode="before")
    @classmethod
    def revalidate_compiled_total_instances(cls, value: object) -> object:
        return _revalidate_money_instances(value)

    @field_validator("total_caps_by_currency", mode="before")
    @classmethod
    def revalidate_compiled_cap_instances(cls, value: object) -> object:
        return _revalidate_money_instances(value)

    @field_validator("candidates", mode="before")
    @classmethod
    def revalidate_candidate_instances(cls, value: object) -> object:
        return _revalidate_candidate_instances(value)

    @model_validator(mode="after")
    def exact_children_digests_and_totals(self) -> CompiledStrategy:
        source = self.source_brief
        if (
            self.strategy_brief_id != source.brief_id
            or self.business_intent != source.business_intent
            or self.expected_accounts != source.expected_accounts
            or self.total_caps_by_currency != source.total_caps_by_currency
            or self.assumptions != source.assumptions
            or self.provenance != source.provenance
            or self.approval_owner != source.approval_owner
            or self.expires_at != source.expires_at
            or self.input_digest != digest(source.model_dump(mode="json"))
        ):
            raise ValueError("compiled strategy context must exactly bind the closed source brief")
        providers = tuple(item.provider for item in self.candidates)
        digests = tuple(item.spec.spec_digest for item in self.candidates)
        if len(set(providers)) != len(providers) or self.ordered_child_spec_digests != digests:
            raise ValueError("compiled strategy children or ordered digests are invalid")
        expected_candidates = tuple(_compile_one(item) for item in source.provider_briefs)
        if self.candidates != expected_candidates:
            raise ValueError(
                "compiled strategy children must exactly match the closed source brief"
            )
        spends: dict[str, int] = {}
        for item in self.candidates:
            spend = _candidate_maximum_spend(item)
            spends[spend.currency] = spends.get(spend.currency, 0) + spend.minor_units
            if item.spec.planning_boundary.status != self.status:
                raise ValueError("every child must preserve its offline planning boundary")
        totals = {item.currency: item.minor_units for item in self.maximum_spend_by_currency}
        expected_spends: dict[str, int] = {}
        for item in expected_candidates:
            spend = _candidate_maximum_spend(item)
            expected_spends[spend.currency] = (
                expected_spends.get(spend.currency, 0) + spend.minor_units
            )
        if (
            len(totals) != len(self.maximum_spend_by_currency)
            or totals != spends
            or totals != expected_spends
        ):
            raise ValueError("compiled per-currency maximum spends are invalid")
        return self

    @property
    def strategy_spec_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


def _compile_one(item: ProviderStrategyBrief) -> StrategyCandidate:
    if isinstance(item, GoogleStrategyBrief):
        google_spec = compile_google_ads_search_campaign(item.brief)
        return GoogleStrategyCandidate(spec=google_spec, review=google_spec.redacted_review)
    if isinstance(item, MetaStrategyBrief):
        meta_spec = compile_meta_ads_campaign(item.brief)
        return MetaStrategyCandidate(spec=meta_spec, review=meta_spec.redacted_review)
    if isinstance(item, LinkedInStrategyBrief):
        linkedin_spec = compile_linkedin_ads_campaign(item.brief)
        return LinkedInStrategyCandidate(spec=linkedin_spec, review=linkedin_spec.redacted_review)
    if isinstance(item, TikTokStrategyBrief):
        tiktok_spec = compile_tiktok_ads_campaign(item.brief)
        return TikTokStrategyCandidate(spec=tiktok_spec, review=tiktok_spec.redacted_review)
    if isinstance(item, RedditStrategyBrief):
        reddit_spec = compile_reddit_ads_campaign(item.brief)
        return RedditStrategyCandidate(spec=reddit_spec, review=reddit_spec.redacted_review)
    openai_spec = compile_openai_ads_campaign(item.brief)
    return OpenAIStrategyCandidate(spec=openai_spec, review=openai_spec.redacted_review)


def compile_strategy(brief: StrategyBrief, *, now: Callable[[], datetime]) -> CompiledStrategy:
    """Compile all selected native candidates before returning a local aggregate."""
    brief = StrategyBrief.model_validate_json(brief.model_dump_json(warnings=False))
    current = now()
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ValueError("injected strategy clock must return a timezone-aware UTC time")
    if brief.expires_at.tzinfo is None or brief.expires_at <= current.astimezone(UTC):
        raise ValueError("strategy brief is expired")
    candidates = tuple(_compile_one(item) for item in brief.provider_briefs)
    totals: dict[str, int] = {}
    for item in candidates:
        spend = _candidate_maximum_spend(item)
        totals[spend.currency] = totals.get(spend.currency, 0) + spend.minor_units
    result = CompiledStrategy(
        source_brief=brief,
        strategy_brief_id=brief.brief_id,
        business_intent=brief.business_intent,
        expected_accounts=brief.expected_accounts,
        total_caps_by_currency=brief.total_caps_by_currency,
        assumptions=brief.assumptions,
        provenance=brief.provenance,
        approval_owner=brief.approval_owner,
        expires_at=brief.expires_at,
        candidates=candidates,
        ordered_child_spec_digests=tuple(item.spec.spec_digest for item in candidates),
        maximum_spend_by_currency=tuple(
            Money(currency=currency, minor_units=totals[currency]) for currency in sorted(totals)
        ),
        input_digest=digest(brief.model_dump(mode="json")),
    )
    return CompiledStrategy.model_validate_json(result.model_dump_json())
