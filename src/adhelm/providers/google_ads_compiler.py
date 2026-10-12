"""Offline compiler for one paused Google Ads v25 Search/RSA hierarchy."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.creative import CreativeVersion
from adhelm.models import Money, SafeRef, StrictModel
from adhelm.privacy import validate_destination

_API_VERSION: Literal["v25"] = "v25"
_CAPABILITY_VERSION: Literal["google-ads-v25-search-rsa-contract-2026-09-04"] = (
    "google-ads-v25-search-rsa-contract-2026-09-04"
)
_FORMAT: Literal["GOOGLE_ADS_RESPONSIVE_SEARCH_AD"] = "GOOGLE_ADS_RESPONSIVE_SEARCH_AD"
_CAMPAIGN_DATE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
GoogleAdsDayOfWeek = Literal[
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"
]
_DAYS_OF_WEEK: tuple[GoogleAdsDayOfWeek, ...] = (
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
)
GoogleAdsNativeMinuteOfHour = Literal["ZERO", "FIFTEEN", "THIRTY", "FORTY_FIVE"]
_NATIVE_MINUTES: dict[int, GoogleAdsNativeMinuteOfHour] = {
    0: "ZERO",
    15: "FIFTEEN",
    30: "THIRTY",
    45: "FORTY_FIVE",
}
_MINUTE_VALUES: dict[GoogleAdsNativeMinuteOfHour, int] = {
    value: key for key, value in _NATIVE_MINUTES.items()
}
_USD_MINOR_TO_MICROS = 10_000
_MAX_CANONICAL_NATIVE_MICROS = 9_007_199_254_740_991
_MAX_MINOR_UNITS = _MAX_CANONICAL_NATIVE_MICROS // _USD_MINOR_TO_MICROS


def _destination_ref(url: str) -> str:
    return "destination:sha256:" + digest({"target_url": url})


def _require_native_name(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must include a non-space character")
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError(f"{field_name} must not contain NUL, newline, or carriage return")
    if len(value.encode("utf-8")) > 255:
        raise ValueError(f"{field_name} must not exceed 255 UTF-8 bytes")
    return value


def _campaign_boundary_date_time(value: date, *, end_of_day: bool) -> str:
    time = "23:59:59" if end_of_day else "00:00:00"
    return f"{value.year:04d}-{value.month:02d}-{value.day:02d} {time}"


def _require_iana_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("account_timezone must be an IANA timezone") from exc
    return value


def _rsa_content_digest(
    *,
    headlines: tuple[str, ...],
    descriptions: tuple[str, ...],
    call_to_action_headline: str,
    destination_url: str,
    creative_version_id: str,
) -> str:
    return digest(
        {
            "format": "responsive_search_ad",
            "headlines": headlines,
            "descriptions": descriptions,
            "cta_headline": call_to_action_headline,
            "cta_intent": "LEARN_MORE",
            "destination_url": destination_url,
            "creative_version_id": creative_version_id,
        }
    )


class GoogleAdsDestinationPolicy(StrictModel):
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=100)
    allowed_paths: tuple[str, ...] = Field(min_length=1, max_length=500)

    @field_validator("allowed_hosts")
    @classmethod
    def canonical_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values or any(
            re.fullmatch(r"[a-z0-9.-]{1,253}", value) is None for value in values
        ):
            raise ValueError("allowed hosts must be unique, sorted lowercase host names")
        return values

    @field_validator("allowed_paths")
    @classmethod
    def canonical_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values or any(
            not value.startswith("/")
            or len(value) > 512
            or "?" in value
            or "#" in value
            or any(ord(character) < 33 for character in value)
            for value in values
        ):
            raise ValueError("allowed paths must be unique, sorted, bounded absolute paths")
        return values


class GoogleAdsRsaCreative(StrictModel):
    """Exact local text-only creative; an RSA has no uploaded asset in this subset."""

    creative_version: CreativeVersion
    headlines: tuple[str, ...] = Field(min_length=3, max_length=15)
    descriptions: tuple[str, ...] = Field(min_length=2, max_length=4)
    call_to_action_headline: str = Field(min_length=1, max_length=30)
    call_to_action_intent: Literal["LEARN_MORE"] = "LEARN_MORE"
    destination_url: str = Field(min_length=1, max_length=2_000)

    @field_validator("headlines")
    @classmethod
    def valid_headlines(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or any(
            not value.strip() or len(value) > 30 for value in values
        ):
            raise ValueError(
                "RSA headlines must be unique non-blank strings of at most 30 characters"
            )
        return values

    @field_validator("descriptions")
    @classmethod
    def valid_descriptions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or any(
            not value.strip() or len(value) > 90 for value in values
        ):
            raise ValueError(
                "RSA descriptions must be unique non-blank strings of at most 90 characters"
            )
        return values

    @model_validator(mode="after")
    def exact_text_only_manifest(self) -> GoogleAdsRsaCreative:
        manifest = self.creative_version.manifest
        if (
            manifest.provider_format != _FORMAT
            or manifest.surface_kind != "TEXT_ONLY"
            or manifest.rendition_refs
            or manifest.destination_ref != _destination_ref(self.destination_url)
        ):
            raise ValueError("creative version must be the exact text-only Google Ads RSA surface")
        if self.call_to_action_headline not in self.headlines:
            raise ValueError("the declared CTA must be one of the RSA headlines")

        expected = (
            [
                (
                    "HEADLINE",
                    text,
                    "responsive_search_ad.headlines",
                    None,
                    None,
                    None,
                    None,
                    None,
                )
                for text in self.headlines
            ]
            + [
                (
                    "DESCRIPTION",
                    text,
                    "responsive_search_ad.descriptions",
                    None,
                    None,
                    None,
                    None,
                    None,
                )
                for text in self.descriptions
            ]
            + [
                (
                    "CTA_BUTTON",
                    self.call_to_action_headline,
                    "responsive_search_ad.headlines",
                    "LEARN_MORE",
                    _destination_ref(self.destination_url),
                    None,
                    None,
                    None,
                )
            ]
        )
        actual = [
            (
                component.component_role,
                component.exact_text,
                component.provider_slot,
                component.normalized_cta_intent,
                component.cta_destination_ref,
                component.pinned_position,
                component.start_ms,
                component.end_ms,
            )
            for component in manifest.components
        ]
        if actual != expected:
            raise ValueError(
                "creative manifest must contain exactly the ordered RSA headline, "
                "description, and CTA components"
            )
        return self

    @property
    def content_digest(self) -> str:
        return _rsa_content_digest(
            headlines=self.headlines,
            descriptions=self.descriptions,
            call_to_action_headline=self.call_to_action_headline,
            destination_url=self.destination_url,
            creative_version_id=self.creative_version.creative_version_id,
        )


class GoogleAdsLocationTarget(StrictModel):
    handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    geo_target_constant: str = Field(pattern=r"^geoTargetConstants/[1-9][0-9]{0,19}$")


class GoogleAdsLanguageTarget(StrictModel):
    handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    language_constant: str = Field(pattern=r"^languageConstants/[1-9][0-9]{0,19}$")


class GoogleAdsAdSchedule(StrictModel):
    handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    day_of_week: GoogleAdsDayOfWeek
    start_hour: int = Field(ge=0, le=23)
    start_minute: Literal[0, 15, 30, 45]
    end_hour: int = Field(ge=0, le=24)
    end_minute: Literal[0, 15, 30, 45]

    @model_validator(mode="after")
    def increasing_interval(self) -> GoogleAdsAdSchedule:
        start = self.start_hour * 60 + self.start_minute
        end = self.end_hour * 60 + self.end_minute
        if self.end_hour == 24 and self.end_minute != 0:
            raise ValueError("24:00 is the only permitted schedule end at hour 24")
        if end <= start:
            raise ValueError("ad schedule end must follow its start on the declared account day")
        return self


class GoogleAdsKeyword(StrictModel):
    handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    text: str = Field(min_length=1, max_length=80)
    match_type: Literal["EXACT", "PHRASE"]

    @field_validator("text")
    @classmethod
    def bounded_keyword(cls, value: str) -> str:
        if not value.strip() or len(value.split()) > 10:
            raise ValueError("keyword text must be non-blank and contain at most 10 words")
        return value


class GoogleAdsSearchCampaignBrief(StrictModel):
    """One bounded, non-conversion Search/RSA candidate in customer-declared USD."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    account_currency: Literal["USD"] = "USD"
    currency_minor_unit_exponent: Literal[2] = 2
    account_timezone: str = Field(min_length=1, max_length=80)
    budget_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    campaign_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    ad_group_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    ad_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    campaign_name: str = Field(min_length=1, max_length=255)
    ad_group_name: str = Field(min_length=1, max_length=255)
    ad_name: str = Field(min_length=1, max_length=255)
    total_budget: Money
    start_date: date
    end_date: date
    locations: tuple[GoogleAdsLocationTarget, ...] = Field(min_length=1, max_length=20)
    languages: tuple[GoogleAdsLanguageTarget, ...] = Field(min_length=1, max_length=20)
    ad_schedules: tuple[GoogleAdsAdSchedule, ...] = Field(min_length=1, max_length=21)
    keywords: tuple[GoogleAdsKeyword, ...] = Field(min_length=1, max_length=50)
    destination_policy: GoogleAdsDestinationPolicy
    creative: GoogleAdsRsaCreative

    @field_validator("account_timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        return _require_iana_timezone(value)

    @model_validator(mode="after")
    def complete_bounded_hierarchy(self) -> GoogleAdsSearchCampaignBrief:
        if (
            type(self.total_budget.minor_units) is not int
            or self.total_budget.currency != self.account_currency
            or not 1 <= self.total_budget.minor_units <= _MAX_MINOR_UNITS
        ):
            raise ValueError("total budget must be positive USD within native limits")
        _require_native_name(self.campaign_name, "campaign name")
        _require_native_name(self.ad_group_name, "ad group name")
        _require_native_name(self.ad_name, "local ad name")
        if self.end_date < self.start_date:
            raise ValueError("campaign end_date must not precede start_date")
        if (self.end_date - self.start_date).days > 365:
            raise ValueError("campaign total budget duration is limited to 366 calendar days")
        handles = [
            self.budget_handle,
            self.campaign_handle,
            self.ad_group_handle,
            self.ad_handle,
            *(item.handle for item in self.locations),
            *(item.handle for item in self.languages),
            *(item.handle for item in self.ad_schedules),
            *(item.handle for item in self.keywords),
        ]
        if len(handles) != len(set(handles)):
            raise ValueError("all local role handles must be unique across the compiled hierarchy")
        if len({item.geo_target_constant for item in self.locations}) != len(self.locations):
            raise ValueError("location targets must be unique")
        if len({item.language_constant for item in self.languages}) != len(self.languages):
            raise ValueError("language targets must be unique")
        schedule_counts = {
            day: sum(item.day_of_week == day for item in self.ad_schedules) for day in _DAYS_OF_WEEK
        }
        if any(count > 6 for count in schedule_counts.values()):
            raise ValueError("Google Ads permits at most six ad schedules per day")
        if len({(item.text, item.match_type) for item in self.keywords}) != len(self.keywords):
            raise ValueError("keywords must be unique by exact text and match type")
        return self


class GoogleAdsNativeCampaignBudgetPayload(StrictModel):
    explicitly_shared: Literal[False] = False
    period: Literal["CUSTOM_PERIOD"] = "CUSTOM_PERIOD"
    delivery_method: Literal["STANDARD"] = "STANDARD"
    # The offline canonical document uses JSON integers. Bound this candidate
    # to the exact RFC 8785 integer domain before its digest is requested.
    total_amount_micros: int = Field(ge=_USD_MINOR_TO_MICROS, le=_MAX_CANONICAL_NATIVE_MICROS)


class GoogleAdsNativeNetworkSettings(StrictModel):
    target_google_search: Literal[True] = True
    target_search_network: Literal[False] = False
    target_content_network: Literal[False] = False
    target_partner_search_network: Literal[False] = False
    target_youtube: Literal[False] = False
    target_google_tv_network: Literal[False] = False


class GoogleAdsNativeTargetSpend(StrictModel):
    """Empty native click strategy with no deprecated target-spend or bid ceiling."""


class GoogleAdsNativeCampaignPayload(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    advertising_channel_type: Literal["SEARCH"] = "SEARCH"
    status: Literal["PAUSED"] = "PAUSED"
    start_date_time: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2} 00:00:00$")
    end_date_time: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2} 23:59:59$")
    network_settings: GoogleAdsNativeNetworkSettings
    target_spend: GoogleAdsNativeTargetSpend

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return _require_native_name(value, "campaign name")

    @model_validator(mode="after")
    def valid_custom_period_boundaries(self) -> GoogleAdsNativeCampaignPayload:
        try:
            start = datetime.strptime(self.start_date_time, _CAMPAIGN_DATE_TIME_FORMAT)
            end = datetime.strptime(self.end_date_time, _CAMPAIGN_DATE_TIME_FORMAT)
        except ValueError as exc:
            raise ValueError("campaign date times must be valid calendar values") from exc
        if end <= start:
            raise ValueError("campaign end date time must follow its start date time")
        if (end.date() - start.date()).days > 365:
            raise ValueError("campaign total budget duration is limited to 366 calendar days")
        return self


class GoogleAdsNativeLocationInfo(StrictModel):
    geo_target_constant: str = Field(pattern=r"^geoTargetConstants/[1-9][0-9]{0,19}$")


class GoogleAdsNativeLanguageInfo(StrictModel):
    language_constant: str = Field(pattern=r"^languageConstants/[1-9][0-9]{0,19}$")


class GoogleAdsNativeAdScheduleInfo(StrictModel):
    day_of_week: GoogleAdsDayOfWeek
    start_hour: int = Field(ge=0, le=23)
    start_minute: GoogleAdsNativeMinuteOfHour
    end_hour: int = Field(ge=0, le=24)
    end_minute: GoogleAdsNativeMinuteOfHour

    @model_validator(mode="after")
    def increasing_interval(self) -> GoogleAdsNativeAdScheduleInfo:
        start = self.start_hour * 60 + _MINUTE_VALUES[self.start_minute]
        end = self.end_hour * 60 + _MINUTE_VALUES[self.end_minute]
        if self.end_hour == 24 and self.end_minute != "ZERO":
            raise ValueError("24:00 is the only permitted schedule end at hour 24")
        if end <= start:
            raise ValueError("ad schedule end must follow its start")
        return self


class GoogleAdsNativeLocationCriterionPayload(StrictModel):
    status: Literal["PAUSED"] = "PAUSED"
    negative: Literal[False] = False
    location: GoogleAdsNativeLocationInfo


class GoogleAdsNativeLanguageCriterionPayload(StrictModel):
    status: Literal["PAUSED"] = "PAUSED"
    negative: Literal[False] = False
    language: GoogleAdsNativeLanguageInfo


class GoogleAdsNativeScheduleCriterionPayload(StrictModel):
    status: Literal["PAUSED"] = "PAUSED"
    negative: Literal[False] = False
    ad_schedule: GoogleAdsNativeAdScheduleInfo


GoogleAdsNativeCampaignCriterionPayload = (
    GoogleAdsNativeLocationCriterionPayload
    | GoogleAdsNativeLanguageCriterionPayload
    | GoogleAdsNativeScheduleCriterionPayload
)


class GoogleAdsNativeAdGroupPayload(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    type: Literal["SEARCH_STANDARD"] = "SEARCH_STANDARD"
    status: Literal["PAUSED"] = "PAUSED"

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return _require_native_name(value, "ad group name")


class GoogleAdsNativeKeywordInfo(StrictModel):
    text: str = Field(min_length=1, max_length=80)
    match_type: Literal["EXACT", "PHRASE"]

    @field_validator("text")
    @classmethod
    def bounded_keyword(cls, value: str) -> str:
        if not value.strip() or len(value.split()) > 10:
            raise ValueError("keyword text must be non-blank and contain at most 10 words")
        return value


class GoogleAdsNativeKeywordPayload(StrictModel):
    status: Literal["PAUSED"] = "PAUSED"
    negative: Literal[False] = False
    keyword: GoogleAdsNativeKeywordInfo


class GoogleAdsNativeAdTextAsset(StrictModel):
    text: str = Field(min_length=1, max_length=90)


class GoogleAdsNativeResponsiveSearchAdInfo(StrictModel):
    headlines: tuple[GoogleAdsNativeAdTextAsset, ...] = Field(min_length=3, max_length=15)
    descriptions: tuple[GoogleAdsNativeAdTextAsset, ...] = Field(min_length=2, max_length=4)

    @field_validator("headlines")
    @classmethod
    def valid_headlines(
        cls, values: tuple[GoogleAdsNativeAdTextAsset, ...]
    ) -> tuple[GoogleAdsNativeAdTextAsset, ...]:
        texts = tuple(item.text for item in values)
        if len(set(texts)) != len(texts) or any(
            not text.strip() or len(text) > 30 for text in texts
        ):
            raise ValueError(
                "RSA headlines must be unique non-blank strings of at most 30 characters"
            )
        return values

    @field_validator("descriptions")
    @classmethod
    def valid_descriptions(
        cls, values: tuple[GoogleAdsNativeAdTextAsset, ...]
    ) -> tuple[GoogleAdsNativeAdTextAsset, ...]:
        texts = tuple(item.text for item in values)
        if len(set(texts)) != len(texts) or any(
            not text.strip() or len(text) > 90 for text in texts
        ):
            raise ValueError(
                "RSA descriptions must be unique non-blank strings of at most 90 characters"
            )
        return values


class GoogleAdsNativeAdPayload(StrictModel):
    final_urls: tuple[str, ...] = Field(min_length=1, max_length=1)
    responsive_search_ad: GoogleAdsNativeResponsiveSearchAdInfo

    @field_validator("final_urls")
    @classmethod
    def bounded_final_url(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 2_000 for value in values):
            raise ValueError("final URL must be a non-blank string of at most 2,000 characters")
        return values


class GoogleAdsNativeAdGroupAdPayload(StrictModel):
    status: Literal["PAUSED"] = "PAUSED"
    ad: GoogleAdsNativeAdPayload


class GoogleAdsCompiledBudget(StrictModel):
    budget_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    request: GoogleAdsNativeCampaignBudgetPayload


class GoogleAdsCompiledCampaignCriterion(StrictModel):
    criterion_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    parent_campaign_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    request: GoogleAdsNativeCampaignCriterionPayload


class GoogleAdsCompiledAdGroup(StrictModel):
    ad_group_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    parent_campaign_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    request: GoogleAdsNativeAdGroupPayload


class GoogleAdsCompiledKeyword(StrictModel):
    keyword_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    parent_ad_group_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    request: GoogleAdsNativeKeywordPayload


class GoogleAdsCompiledResponsiveSearchAd(StrictModel):
    ad_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    parent_ad_group_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    local_name: str = Field(min_length=1, max_length=255)
    request: GoogleAdsNativeAdGroupAdPayload
    call_to_action_headline: str = Field(min_length=1, max_length=30)
    call_to_action_intent: Literal["LEARN_MORE"] = "LEARN_MORE"
    creative_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    creative_family_id: str = Field(min_length=1, max_length=200)
    creative_version_id: str = Field(pattern=r"^creative:sha256:[0-9a-f]{64}$")

    @field_validator("local_name")
    @classmethod
    def valid_local_name(cls, value: str) -> str:
        return _require_native_name(value, "local ad name")


class GoogleAdsPlanningBoundary(StrictModel):
    status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    requires_current_account_identity: Literal[True] = True
    requires_account_currency_and_timezone_readback: Literal[True] = True
    requires_campaign_total_budget_eligibility_readback: Literal[True] = True
    requires_geo_language_schedule_readback: Literal[True] = True
    requires_geo_target_type_readback: Literal[True] = True
    requires_creative_ledger_check: Literal[True] = True
    requires_destination_and_tracking_validation: Literal[True] = True
    requires_asset_automation_and_ai_max_readback: Literal[True] = True
    requires_account_default_conversion_goal_readback: Literal[True] = True
    requires_policy_eligibility_readback: Literal[True] = True
    requires_drift_check_and_authorization: Literal[True] = True


class GoogleAdsRedactedReview(StrictModel):
    account_ref: SafeRef
    account_currency: Literal["USD"]
    account_timezone: str
    budget_handle: str
    campaign_handle: str
    ad_group_handle: str
    ad_handle: str
    initial_state: Literal["PAUSED"] = "PAUSED"
    objective: Literal["SEARCH_TRAFFIC"] = "SEARCH_TRAFFIC"
    campaign_total_budget: Money
    campaign_duration_days: int = Field(ge=1, le=366)
    location_count: int = Field(ge=1)
    language_count: int = Field(ge=1)
    schedule_count: int = Field(ge=1)
    keyword_count: int = Field(ge=1)
    rsa_component_composition: Literal["DYNAMIC_COMBINATION_UNKNOWN"] = (
        "DYNAMIC_COMBINATION_UNKNOWN"
    )
    component_level_attribution: Literal["NOT_AVAILABLE_FROM_PROVIDER"] = (
        "NOT_AVAILABLE_FROM_PROVIDER"
    )
    creative_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    creative_version_id: str = Field(pattern=r"^creative:sha256:[0-9a-f]{64}$")
    execution_status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"]
    spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class GoogleAdsCompiledCampaignSpec(StrictModel):
    schema_version: Literal[1] = 1
    provider: Literal["google_ads"] = "google_ads"
    provider_api_version: Literal["v25"] = _API_VERSION
    capability_version: Literal["google-ads-v25-search-rsa-contract-2026-09-04"] = (
        _CAPABILITY_VERSION
    )
    account_ref: SafeRef
    account_currency: Literal["USD"]
    currency_minor_unit_exponent: Literal[2]
    account_timezone: str
    destination_policy: GoogleAdsDestinationPolicy
    budget: GoogleAdsCompiledBudget
    campaign_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    campaign_budget_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    campaign_request: GoogleAdsNativeCampaignPayload
    campaign_criteria: tuple[GoogleAdsCompiledCampaignCriterion, ...] = Field(
        min_length=3, max_length=61
    )
    ad_group: GoogleAdsCompiledAdGroup
    keywords: tuple[GoogleAdsCompiledKeyword, ...] = Field(min_length=1, max_length=50)
    responsive_search_ad: GoogleAdsCompiledResponsiveSearchAd
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    maximum_spend: Money
    planning_boundary: GoogleAdsPlanningBoundary

    @field_validator("account_timezone")
    @classmethod
    def account_timezone_is_iana(cls, value: str) -> str:
        return _require_iana_timezone(value)

    @model_validator(mode="after")
    def exact_local_hierarchy_creative_and_spend_bound(self) -> GoogleAdsCompiledCampaignSpec:
        handles = (
            self.budget.budget_handle,
            self.campaign_handle,
            self.ad_group.ad_group_handle,
            self.responsive_search_ad.ad_handle,
            *(item.criterion_handle for item in self.campaign_criteria),
            *(item.keyword_handle for item in self.keywords),
        )
        if len(handles) != len(set(handles)):
            raise ValueError("compiled Google Ads local handles must be globally unique")
        if (
            self.campaign_budget_handle != self.budget.budget_handle
            or any(
                item.parent_campaign_handle != self.campaign_handle
                for item in self.campaign_criteria
            )
            or self.ad_group.parent_campaign_handle != self.campaign_handle
            or any(
                item.parent_ad_group_handle != self.ad_group.ad_group_handle
                for item in self.keywords
            )
            or self.responsive_search_ad.parent_ad_group_handle != self.ad_group.ad_group_handle
        ):
            raise ValueError("compiled Google Ads parent handle bindings are invalid")

        locations = tuple(
            item.request.location.geo_target_constant
            for item in self.campaign_criteria
            if isinstance(item.request, GoogleAdsNativeLocationCriterionPayload)
        )
        languages = tuple(
            item.request.language.language_constant
            for item in self.campaign_criteria
            if isinstance(item.request, GoogleAdsNativeLanguageCriterionPayload)
        )
        schedules = tuple(
            item.request.ad_schedule
            for item in self.campaign_criteria
            if isinstance(item.request, GoogleAdsNativeScheduleCriterionPayload)
        )
        if (
            not 1 <= len(locations) <= 20
            or not 1 <= len(languages) <= 20
            or not 1 <= len(schedules) <= 21
            or len(set(locations)) != len(locations)
            or len(set(languages)) != len(languages)
            or any(sum(item.day_of_week == day for item in schedules) > 6 for day in _DAYS_OF_WEEK)
        ):
            raise ValueError("compiled Google Ads campaign criteria are outside the fixed bounds")

        keyword_pairs = tuple(
            (item.request.keyword.text, item.request.keyword.match_type) for item in self.keywords
        )
        if len(set(keyword_pairs)) != len(keyword_pairs):
            raise ValueError("compiled Google Ads keywords must be unique by text and match type")

        ad = self.responsive_search_ad.request.ad
        final_url = ad.final_urls[0]
        validate_destination(
            final_url,
            allowed_hosts=set(self.destination_policy.allowed_hosts),
            allowed_paths=set(self.destination_policy.allowed_paths),
        )
        headlines = tuple(item.text for item in ad.responsive_search_ad.headlines)
        descriptions = tuple(item.text for item in ad.responsive_search_ad.descriptions)
        if self.responsive_search_ad.call_to_action_headline not in headlines:
            raise ValueError("compiled RSA CTA must be one of the native headlines")
        if self.responsive_search_ad.creative_content_digest != _rsa_content_digest(
            headlines=headlines,
            descriptions=descriptions,
            call_to_action_headline=self.responsive_search_ad.call_to_action_headline,
            destination_url=final_url,
            creative_version_id=self.responsive_search_ad.creative_version_id,
        ):
            raise ValueError("compiled RSA text, destination, or creative identity is invalid")
        if (
            type(self.maximum_spend.minor_units) is not int
            or type(self.budget.request.total_amount_micros) is not int
            or self.maximum_spend.currency != self.account_currency
            or not 1 <= self.maximum_spend.minor_units <= _MAX_MINOR_UNITS
            or self.budget.request.total_amount_micros
            != self.maximum_spend.minor_units * _USD_MINOR_TO_MICROS
        ):
            raise ValueError("compiled Google Ads total budget does not match maximum spend")
        return self

    @property
    def spec_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def redacted_review(self) -> GoogleAdsRedactedReview:
        return GoogleAdsRedactedReview(
            account_ref=self.account_ref,
            account_currency=self.account_currency,
            account_timezone=self.account_timezone,
            budget_handle=self.budget.budget_handle,
            campaign_handle=self.campaign_handle,
            ad_group_handle=self.ad_group.ad_group_handle,
            ad_handle=self.responsive_search_ad.ad_handle,
            campaign_total_budget=self.maximum_spend,
            campaign_duration_days=(
                datetime.strptime(self.campaign_request.end_date_time, _CAMPAIGN_DATE_TIME_FORMAT)
                .date()
                .toordinal()
                - datetime.strptime(
                    self.campaign_request.start_date_time, _CAMPAIGN_DATE_TIME_FORMAT
                )
                .date()
                .toordinal()
                + 1
            ),
            location_count=sum(
                isinstance(item.request, GoogleAdsNativeLocationCriterionPayload)
                for item in self.campaign_criteria
            ),
            language_count=sum(
                isinstance(item.request, GoogleAdsNativeLanguageCriterionPayload)
                for item in self.campaign_criteria
            ),
            schedule_count=sum(
                isinstance(item.request, GoogleAdsNativeScheduleCriterionPayload)
                for item in self.campaign_criteria
            ),
            keyword_count=len(self.keywords),
            creative_content_digest=self.responsive_search_ad.creative_content_digest,
            creative_version_id=self.responsive_search_ad.creative_version_id,
            execution_status=self.planning_boundary.status,
            spec_digest=self.spec_digest,
        )


def compile_google_ads_search_campaign(
    brief: GoogleAdsSearchCampaignBrief,
) -> GoogleAdsCompiledCampaignSpec:
    """Compile a closed Search/RSA candidate without transport or mutation capability."""
    brief = GoogleAdsSearchCampaignBrief.model_validate(
        brief.model_dump(mode="python", warnings=False)
    )
    validate_destination(
        brief.creative.destination_url,
        allowed_hosts=set(brief.destination_policy.allowed_hosts),
        allowed_paths=set(brief.destination_policy.allowed_paths),
    )
    criteria = tuple(
        [
            GoogleAdsCompiledCampaignCriterion(
                criterion_handle=item.handle,
                parent_campaign_handle=brief.campaign_handle,
                request=GoogleAdsNativeLocationCriterionPayload(
                    location=GoogleAdsNativeLocationInfo(
                        geo_target_constant=item.geo_target_constant
                    )
                ),
            )
            for item in brief.locations
        ]
        + [
            GoogleAdsCompiledCampaignCriterion(
                criterion_handle=item.handle,
                parent_campaign_handle=brief.campaign_handle,
                request=GoogleAdsNativeLanguageCriterionPayload(
                    language=GoogleAdsNativeLanguageInfo(language_constant=item.language_constant)
                ),
            )
            for item in brief.languages
        ]
        + [
            GoogleAdsCompiledCampaignCriterion(
                criterion_handle=item.handle,
                parent_campaign_handle=brief.campaign_handle,
                request=GoogleAdsNativeScheduleCriterionPayload(
                    ad_schedule=GoogleAdsNativeAdScheduleInfo(
                        day_of_week=item.day_of_week,
                        start_hour=item.start_hour,
                        start_minute=_NATIVE_MINUTES[item.start_minute],
                        end_hour=item.end_hour,
                        end_minute=_NATIVE_MINUTES[item.end_minute],
                    )
                ),
            )
            for item in brief.ad_schedules
        ]
    )
    total_budget_micros = brief.total_budget.minor_units * _USD_MINOR_TO_MICROS
    return GoogleAdsCompiledCampaignSpec(
        account_ref=brief.account_ref,
        account_currency=brief.account_currency,
        currency_minor_unit_exponent=brief.currency_minor_unit_exponent,
        account_timezone=brief.account_timezone,
        destination_policy=brief.destination_policy,
        budget=GoogleAdsCompiledBudget(
            budget_handle=brief.budget_handle,
            request=GoogleAdsNativeCampaignBudgetPayload(total_amount_micros=total_budget_micros),
        ),
        campaign_handle=brief.campaign_handle,
        campaign_budget_handle=brief.budget_handle,
        campaign_request=GoogleAdsNativeCampaignPayload(
            name=brief.campaign_name,
            start_date_time=_campaign_boundary_date_time(brief.start_date, end_of_day=False),
            end_date_time=_campaign_boundary_date_time(brief.end_date, end_of_day=True),
            network_settings=GoogleAdsNativeNetworkSettings(),
            target_spend=GoogleAdsNativeTargetSpend(),
        ),
        campaign_criteria=criteria,
        ad_group=GoogleAdsCompiledAdGroup(
            ad_group_handle=brief.ad_group_handle,
            parent_campaign_handle=brief.campaign_handle,
            request=GoogleAdsNativeAdGroupPayload(name=brief.ad_group_name),
        ),
        keywords=tuple(
            GoogleAdsCompiledKeyword(
                keyword_handle=item.handle,
                parent_ad_group_handle=brief.ad_group_handle,
                request=GoogleAdsNativeKeywordPayload(
                    keyword=GoogleAdsNativeKeywordInfo(text=item.text, match_type=item.match_type)
                ),
            )
            for item in brief.keywords
        ),
        responsive_search_ad=GoogleAdsCompiledResponsiveSearchAd(
            ad_handle=brief.ad_handle,
            parent_ad_group_handle=brief.ad_group_handle,
            local_name=brief.ad_name,
            request=GoogleAdsNativeAdGroupAdPayload(
                ad=GoogleAdsNativeAdPayload(
                    final_urls=(brief.creative.destination_url,),
                    responsive_search_ad=GoogleAdsNativeResponsiveSearchAdInfo(
                        headlines=tuple(
                            GoogleAdsNativeAdTextAsset(text=text)
                            for text in brief.creative.headlines
                        ),
                        descriptions=tuple(
                            GoogleAdsNativeAdTextAsset(text=text)
                            for text in brief.creative.descriptions
                        ),
                    ),
                )
            ),
            call_to_action_headline=brief.creative.call_to_action_headline,
            creative_content_digest=brief.creative.content_digest,
            creative_family_id=brief.creative.creative_version.creative_family_id,
            creative_version_id=brief.creative.creative_version.creative_version_id,
        ),
        input_digest=digest(brief.model_dump(mode="json")),
        maximum_spend=brief.total_budget,
        planning_boundary=GoogleAdsPlanningBoundary(),
    )
