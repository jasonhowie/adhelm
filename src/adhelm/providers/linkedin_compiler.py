"""Bounded offline compiler for a LinkedIn existing-dark-image-post candidate.

This module deliberately creates no provider IDs and performs no network I/O.
Local handles express the dependency graph until a guarded apply workflow has
created and read back the provider resources.  Campaign and creative templates
therefore exclude their required native parent URNs until that readback. The
only accepted format is a US, engagement campaign that sponsors one
customer-declared existing dark image post; post creation, publishing, media
upload, and activation are excluded.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict, Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.creative import CreativeAssetVersion, CreativeRendition, CreativeVersion
from adhelm.models import Money, SafeRef, StrictModel

_API_VERSION: Literal["202608"] = "202608"
_CAPABILITY_VERSION: Literal["linkedin-marketing-202608-existing-dark-image-post-2026-09-04"] = (
    "linkedin-marketing-202608-existing-dark-image-post-2026-09-04"
)
_FORMAT: Literal["LINKEDIN_ADS_EXISTING_DARK_IMAGE_POST_ENGAGEMENT"] = (
    "LINKEDIN_ADS_EXISTING_DARK_IMAGE_POST_ENGAGEMENT"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_NATIVE_MONEY_AMOUNT = re.compile(r"^[0-9]+\.[0-9]{2}$")
_MAX_MINOR_UNITS = 9_007_199_254_740_991
_ORG_URN = re.compile(r"^urn:li:organization:[1-9][0-9]{0,19}$")
_ACCOUNT_URN = re.compile(r"^urn:li:sponsoredAccount:[1-9][0-9]{0,19}$")
_CAMPAIGN_GROUP_URN = re.compile(r"^urn:li:sponsoredCampaignGroup:[1-9][0-9]{0,19}$")
_CAMPAIGN_URN = re.compile(r"^urn:li:sponsoredCampaign:[1-9][0-9]{0,19}$")
_POST_URN = re.compile(r"^urn:li:(?:ugcPost|share):[1-9][0-9]{0,19}$")
_IMAGE_URN = re.compile(r"^urn:li:image:[A-Za-z0-9_-]{1,200}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _non_blank(value: str, field_name: str, *, maximum_bytes: int) -> str:
    if not value.strip() or any(char in value for char in ("\x00", "\n", "\r")):
        raise ValueError(f"{field_name} must be non-blank plain text")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{field_name} exceeds its reviewed UTF-8 byte limit")
    return value


def _iana_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("account_timezone must be an IANA timezone") from exc
    return value


def _money_amount(minor_units: int) -> str:
    """Render exact USD cents as a non-floating-point LinkedIn BigDecimal string."""
    return f"{minor_units // 100}.{minor_units % 100:02d}"


def _native_money_minor_units(value: object) -> int:
    """Validate a native decimal string against the canonical typed-money domain."""
    if (
        not isinstance(value, str)
        or len(value) > 17
        or _NATIVE_MONEY_AMOUNT.fullmatch(value) is None
    ):
        raise ValueError("native money amount must be a bounded two-decimal USD string")
    major, cents = value.split(".")
    minor_units = int(major) * 100 + int(cents)
    if not 1 <= minor_units <= _MAX_MINOR_UNITS:
        raise ValueError("native money amount must be positive within canonical USD bounds")
    return minor_units


def _millis(value: datetime) -> int:
    return int((value.astimezone(UTC) - _EPOCH) // timedelta(milliseconds=1))


def _existing_post_digest(
    *,
    post_urn: str,
    author_urn: str,
    commentary: str,
    image_urn: str,
    image_title: str,
    image_alt_text: str,
    visibility: str,
    feed_distribution: str,
    lifecycle_state: str,
    dsc_ad_account_urn: str,
    is_dsc: bool,
    dsc_ad_type: str,
    dsc_status: str,
    call_to_action_status: str,
    landing_page_status: str,
) -> str:
    return digest(
        {
            "post_urn": post_urn,
            "author_urn": author_urn,
            "commentary": commentary,
            "image_urn": image_urn,
            "image_title": image_title,
            "image_alt_text": image_alt_text,
            "visibility": visibility,
            "feed_distribution": feed_distribution,
            "lifecycle_state": lifecycle_state,
            "dsc_ad_account_urn": dsc_ad_account_urn,
            "is_dsc": is_dsc,
            "dsc_ad_type": dsc_ad_type,
            "dsc_status": dsc_status,
            "call_to_action_status": call_to_action_status,
            "landing_page_status": landing_page_status,
        }
    )


def _creative_content_digest(
    *,
    post_digest: str,
    post_urn: str,
    author_urn: str,
    dsc_ad_account_urn: str,
    creative_version_id: str,
    asset_content_digest: str,
) -> str:
    return digest(
        {
            "format": "existing_dark_image_post_engagement",
            "post_digest": post_digest,
            "post_urn": post_urn,
            "author_urn": author_urn,
            "dsc_ad_account_urn": dsc_ad_account_urn,
            "creative_version_id": creative_version_id,
            "asset_content_digest": asset_content_digest,
            "call_to_action": "NOT_APPLICABLE",
            "landing_page": "NOT_APPLICABLE",
        }
    )


class LinkedInExistingDarkImagePost(StrictModel):
    """Customer declaration of an existing published dark image post, not readback proof."""

    schema_version: Literal[1] = 1
    post_urn: str = Field(pattern=_POST_URN.pattern)
    author_urn: str = Field(pattern=_ORG_URN.pattern)
    commentary: str = Field(min_length=1, max_length=3_000)
    image_urn: str = Field(pattern=_IMAGE_URN.pattern)
    image_title: str = Field(min_length=1, max_length=400)
    image_alt_text: str = Field(min_length=1, max_length=4_086)
    visibility: Literal["PUBLIC"] = "PUBLIC"
    feed_distribution: Literal["NONE"] = "NONE"
    lifecycle_state: Literal["PUBLISHED"] = "PUBLISHED"
    dsc_ad_account_urn: str = Field(pattern=_ACCOUNT_URN.pattern)
    is_dsc: Literal[True] = True
    dsc_ad_type: Literal["STANDARD"] = "STANDARD"
    dsc_status: Literal["ACTIVE"] = "ACTIVE"
    call_to_action_status: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    landing_page_status: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    canonical_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("commentary", "image_title", "image_alt_text")
    @classmethod
    def plain_text(cls, value: str) -> str:
        return _non_blank(value, "existing post text", maximum_bytes=4_086)

    @model_validator(mode="after")
    def digest_binds_complete_declared_post(self) -> LinkedInExistingDarkImagePost:
        expected = _existing_post_digest(
            post_urn=self.post_urn,
            author_urn=self.author_urn,
            commentary=self.commentary,
            image_urn=self.image_urn,
            image_title=self.image_title,
            image_alt_text=self.image_alt_text,
            visibility=self.visibility,
            feed_distribution=self.feed_distribution,
            lifecycle_state=self.lifecycle_state,
            dsc_ad_account_urn=self.dsc_ad_account_urn,
            is_dsc=self.is_dsc,
            dsc_ad_type=self.dsc_ad_type,
            dsc_status=self.dsc_status,
            call_to_action_status=self.call_to_action_status,
            landing_page_status=self.landing_page_status,
        )
        if self.canonical_digest != expected:
            raise ValueError("existing post canonical_digest must bind every declared post field")
        return self


class LinkedInCreativeAssociation(StrictModel):
    creative_version: CreativeVersion
    asset: CreativeAssetVersion
    rendition: CreativeRendition
    existing_post: LinkedInExistingDarkImagePost

    @model_validator(mode="after")
    def exact_identity_rendition_and_post_surface(self) -> LinkedInCreativeAssociation:
        asset, rendition, manifest = self.asset, self.rendition, self.creative_version.manifest
        expected_asset = "asset:sha256:" + digest(
            {
                "content_digest": asset.content_digest,
                "mime_type": asset.mime_type,
                "width_px": asset.width_px,
                "height_px": asset.height_px,
            }
        )
        expected_transform = digest(rendition.transformation_spec)
        expected_rendition = "rendition:sha256:" + digest(
            {
                "asset_version_id": asset.asset_version_id,
                "provider_format": _FORMAT,
                "locale": manifest.locale,
                "transformation_digest": expected_transform,
            }
        )
        components = manifest.components
        if (
            _DIGEST.fullmatch(asset.content_digest) is None
            or asset.asset_version_id != expected_asset
        ):
            raise ValueError("creative asset identity must match content and intrinsic metadata")
        if (
            manifest.provider_format != _FORMAT
            or manifest.surface_kind != "MEDIA"
            or manifest.destination_ref != "destination:NOT_APPLICABLE"
            or rendition.provider_format != _FORMAT
            or rendition.locale != manifest.locale
            or rendition.source_relation_status != "PROVEN_SOURCE_ASSET"
            or rendition.source_asset_version_ref != asset.asset_version_id
            or rendition.content_digest != asset.content_digest
            or rendition.width_px != asset.width_px
            or rendition.height_px != asset.height_px
            or rendition.transformation_digest != expected_transform
            or rendition.rendition_id != expected_rendition
            or len(manifest.rendition_refs) != 1
            or manifest.rendition_refs[0].rendition_id != rendition.rendition_id
            or manifest.rendition_refs[0].role != "HERO"
            or len(components) != 1
            or components[0].component_role != "PRIMARY_TEXT"
            or components[0].provider_slot != "post.commentary"
            or components[0].exact_text != self.existing_post.commentary
            or components[0].normalized_cta_intent is not None
            or components[0].cta_destination_ref is not None
        ):
            raise ValueError(
                "creative association must exactly bind the existing post and identity rendition"
            )
        return self


class LinkedInAdsCampaignBrief(StrictModel):
    """One US engagement candidate whose group total budget is its only total spend cap."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    account_urn: str = Field(pattern=_ACCOUNT_URN.pattern)
    account_currency: Literal["USD"] = "USD"
    currency_minor_unit_exponent: Literal[2] = 2
    account_timezone: str = Field(min_length=1, max_length=80)
    campaign_group_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    campaign_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    creative_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    campaign_group_name: str = Field(min_length=1, max_length=200)
    campaign_name: str = Field(min_length=1, max_length=200)
    group_total_budget: Money
    daily_budget: Money
    unit_cost: Money
    start_time: datetime
    stop_time: datetime
    location_urns: tuple[Literal["urn:li:geo:103644278"], ...] = ("urn:li:geo:103644278",)
    interface_locale_urns: tuple[Literal["urn:li:locale:en_US"], ...] = ("urn:li:locale:en_US",)
    political_intent: Literal["NOT_POLITICAL"] = "NOT_POLITICAL"
    creative: LinkedInCreativeAssociation

    @field_validator("campaign_group_name", "campaign_name")
    @classmethod
    def names_are_bounded_plain_text(cls, value: str) -> str:
        return _non_blank(value, "campaign name", maximum_bytes=200)

    @model_validator(mode="after")
    def valid_schedule_budget_and_bindings(self) -> LinkedInAdsCampaignBrief:
        if (
            self.start_time.tzinfo is None
            or self.stop_time.tzinfo is None
            or self.stop_time <= self.start_time
            or self.start_time.microsecond % 1_000 != 0
            or self.stop_time.microsecond % 1_000 != 0
            or self.start_time.astimezone(UTC) < _EPOCH
        ):
            raise ValueError(
                "schedule requires millisecond-aligned timezone-aware increasing times"
            )
        _iana_timezone(self.account_timezone)
        if len({self.campaign_group_handle, self.campaign_handle, self.creative_handle}) != 3:
            raise ValueError("local hierarchy handles must be globally unique")
        budgets = (self.group_total_budget, self.daily_budget, self.unit_cost)
        if any(budget.currency != self.account_currency for budget in budgets):
            raise ValueError("all LinkedIn budget amounts must use the declared account currency")
        if any(
            type(budget.minor_units) is not int
            or budget.minor_units < 1
            or budget.minor_units > _MAX_MINOR_UNITS
            for budget in budgets
        ):
            raise ValueError("all LinkedIn budget amounts must be positive bounded Money values")
        if self.daily_budget.minor_units > self.group_total_budget.minor_units:
            raise ValueError("daily budget cannot exceed the group total spend cap")
        if self.creative.existing_post.dsc_ad_account_urn != self.account_urn:
            raise ValueError("dark post dsc_ad_account_urn must match the compiled account")
        if self.location_urns != ("urn:li:geo:103644278",):
            raise ValueError("only the reviewed US location target is supported")
        if self.interface_locale_urns != ("urn:li:locale:en_US",):
            raise ValueError("only the reviewed en_US interface locale is supported")
        return self


class LinkedInNativeWireModel(StrictModel):
    """Strict native fields that serialize documented aliases by default."""

    model_config = ConfigDict(serialize_by_alias=True)


class LinkedInNativeMoney(LinkedInNativeWireModel):
    amount: str = Field(pattern=_NATIVE_MONEY_AMOUNT.pattern, max_length=17)
    currencyCode: Literal["USD"] = "USD"

    @field_validator("amount")
    @classmethod
    def positive_amount(cls, value: str) -> str:
        _native_money_minor_units(value)
        return value


class LinkedInNativeRunSchedule(LinkedInNativeWireModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def increasing(self) -> LinkedInNativeRunSchedule:
        if self.end <= self.start:
            raise ValueError("run schedule end must be exclusive and follow its start")
        return self


class LinkedInNativeCampaignGroupPayload(LinkedInNativeWireModel):
    account: str = Field(pattern=_ACCOUNT_URN.pattern)
    name: str = Field(min_length=1, max_length=200)
    objectiveType: Literal["ENGAGEMENT"] = "ENGAGEMENT"
    runSchedule: LinkedInNativeRunSchedule
    totalBudget: LinkedInNativeMoney
    status: Literal["DRAFT"] = "DRAFT"

    @field_validator("name")
    @classmethod
    def plain_name(cls, value: str) -> str:
        return _non_blank(value, "campaign group name", maximum_bytes=200)


class LinkedInNativeLocale(LinkedInNativeWireModel):
    country: Literal["US"] = "US"
    language: Literal["en"] = "en"


class LinkedInNativeLocationFacet(LinkedInNativeWireModel):
    locations: tuple[Literal["urn:li:geo:103644278"], ...] = Field(
        alias="urn:li:adTargetingFacet:locations", min_length=1, max_length=1
    )


class LinkedInNativeInterfaceLocaleFacet(LinkedInNativeWireModel):
    interface_locales: tuple[Literal["urn:li:locale:en_US"], ...] = Field(
        alias="urn:li:adTargetingFacet:interfaceLocales", min_length=1, max_length=1
    )


class LinkedInNativeLocationClause(LinkedInNativeWireModel):
    location_or: LinkedInNativeLocationFacet = Field(alias="or")


class LinkedInNativeInterfaceLocaleClause(LinkedInNativeWireModel):
    locale_or: LinkedInNativeInterfaceLocaleFacet = Field(alias="or")


class LinkedInNativeTargetingInclude(LinkedInNativeWireModel):
    all_of: tuple[LinkedInNativeLocationClause, LinkedInNativeInterfaceLocaleClause] = Field(
        alias="and"
    )


class LinkedInNativeTargetingCriteria(LinkedInNativeWireModel):
    include: LinkedInNativeTargetingInclude


class LinkedInNativeCampaignRequestFields(LinkedInNativeWireModel):
    """Documented campaign fields before a created group URN is available."""

    account: str = Field(pattern=_ACCOUNT_URN.pattern)
    associatedEntity: str = Field(pattern=_ORG_URN.pattern)
    audienceExpansionEnabled: Literal[False] = False
    connectedTelevisionOnly: Literal[False] = False
    costType: Literal["CPC"] = "CPC"
    creativeSelection: Literal["OPTIMIZED"] = "OPTIMIZED"
    dailyBudget: LinkedInNativeMoney
    locale: LinkedInNativeLocale = LinkedInNativeLocale()
    name: str = Field(min_length=1, max_length=200)
    offsiteDeliveryEnabled: Literal[False] = False
    optimizationTargetType: Literal["NONE"] = "NONE"
    politicalIntent: Literal["NOT_POLITICAL"] = "NOT_POLITICAL"
    runSchedule: LinkedInNativeRunSchedule
    targetingCriteria: LinkedInNativeTargetingCriteria
    type: Literal["SPONSORED_UPDATES"] = "SPONSORED_UPDATES"
    unitCost: LinkedInNativeMoney
    status: Literal["DRAFT"] = "DRAFT"

    @field_validator("name")
    @classmethod
    def plain_name(cls, value: str) -> str:
        return _non_blank(value, "campaign name", maximum_bytes=200)


class LinkedInNativeCampaignPayload(LinkedInNativeCampaignRequestFields):
    """Complete documented campaign create body after parent-URN readback."""

    campaignGroup: str = Field(pattern=_CAMPAIGN_GROUP_URN.pattern)


class LinkedInNativeCreativeContent(LinkedInNativeWireModel):
    reference: str = Field(pattern=_POST_URN.pattern)


class LinkedInNativeCreativeRequestFields(LinkedInNativeWireModel):
    """Documented creative fields before a created campaign URN is available."""

    content: LinkedInNativeCreativeContent
    intendedStatus: Literal["DRAFT"] = "DRAFT"


class LinkedInNativeCreativePayload(LinkedInNativeCreativeRequestFields):
    """Complete documented creative create body after parent-URN readback."""

    campaign: str = Field(pattern=_CAMPAIGN_URN.pattern)


class LinkedInCampaignRequestTemplate(StrictModel):
    """Offline campaign fields; a future executor must bind a read-back group URN."""

    parent_campaign_group_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    required_native_parent_field: Literal["campaignGroup"] = "campaignGroup"
    required_native_parent_urn_type: Literal["urn:li:sponsoredCampaignGroup"] = (
        "urn:li:sponsoredCampaignGroup"
    )
    resolution_status: Literal["REQUIRES_CREATE_READBACK_AND_REVALIDATION"] = (
        "REQUIRES_CREATE_READBACK_AND_REVALIDATION"
    )
    request: LinkedInNativeCampaignRequestFields


class LinkedInCreativeRequestTemplate(StrictModel):
    """Offline creative fields; a future executor must bind a read-back campaign URN."""

    parent_campaign_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    required_native_parent_field: Literal["campaign"] = "campaign"
    required_native_parent_urn_type: Literal["urn:li:sponsoredCampaign"] = (
        "urn:li:sponsoredCampaign"
    )
    resolution_status: Literal["REQUIRES_CREATE_READBACK_AND_REVALIDATION"] = (
        "REQUIRES_CREATE_READBACK_AND_REVALIDATION"
    )
    request: LinkedInNativeCreativeRequestFields


class LinkedInRestContract(StrictModel):
    schema_version: Literal[1] = 1
    base_path: Literal["https://api.linkedin.com/rest"] = "https://api.linkedin.com/rest"
    linkedin_version: Literal["202608"] = _API_VERSION
    restli_protocol_version: Literal["2.0.0"] = "2.0.0"
    campaign_group_path: Literal["/adAccounts/{accountId}/adCampaignGroups"] = (
        "/adAccounts/{accountId}/adCampaignGroups"
    )
    campaign_path: Literal["/adAccounts/{accountId}/adCampaigns"] = (
        "/adAccounts/{accountId}/adCampaigns"
    )
    creative_path: Literal["/adAccounts/{accountId}/creatives"] = (
        "/adAccounts/{accountId}/creatives"
    )


class LinkedInAdsPlanningBoundary(StrictModel):
    status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    fixture_only_status: Literal["FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = (
        "FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    )
    requires_current_account_identity: Literal[True] = True
    requires_ad_account_page_scope_and_role_readback: Literal[True] = True
    requires_account_currency_readback: Literal[True] = True
    requires_group_campaign_type_and_budget_readback: Literal[True] = True
    requires_parent_urn_create_readback_and_revalidation: Literal[True] = True
    requires_post_page_image_ownership_and_eligibility_readback: Literal[True] = True
    requires_audience_minimum_and_targeting_eligibility_readback: Literal[True] = True
    requires_creative_format_and_transformation_readback: Literal[True] = True
    requires_creative_ledger_check: Literal[True] = True
    requires_native_conversion_check: Literal[True] = True
    requires_remote_drift_check_and_exact_authorization: Literal[True] = True


class LinkedInAdsRedactedReview(StrictModel):
    account_ref: SafeRef
    account_currency: Literal["USD"]
    account_timezone: str
    campaign_group_handle: str
    campaign_handle: str
    creative_handle: str
    initial_state: Literal["DRAFT"] = "DRAFT"
    objective: Literal["ENGAGEMENT"] = "ENGAGEMENT"
    targeting_status: Literal["CUSTOMER_DECLARED_US_EN_REQUIRES_READBACK"] = (
        "CUSTOMER_DECLARED_US_EN_REQUIRES_READBACK"
    )
    existing_post_status: Literal["CUSTOMER_DECLARED_REQUIRES_READBACK"] = (
        "CUSTOMER_DECLARED_REQUIRES_READBACK"
    )
    cta_status: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    landing_page_status: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    creative_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    creative_version_id: str = Field(pattern=r"^creative:sha256:[0-9a-f]{64}$")
    maximum_spend: Money
    execution_status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"]
    spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class LinkedInAdsCompiledCampaignSpec(StrictModel):
    schema_version: Literal[1] = 1
    provider: Literal["linkedin_ads"] = "linkedin_ads"
    provider_api_version: Literal["202608"] = _API_VERSION
    capability_version: Literal["linkedin-marketing-202608-existing-dark-image-post-2026-09-04"] = (
        _CAPABILITY_VERSION
    )
    rest_contract: LinkedInRestContract = LinkedInRestContract()
    account_ref: SafeRef
    account_currency: Literal["USD"]
    currency_minor_unit_exponent: Literal[2]
    account_timezone: str
    campaign_group_handle: str
    campaign_group_request: LinkedInNativeCampaignGroupPayload
    campaign_handle: str
    campaign_template: LinkedInCampaignRequestTemplate
    creative_handle: str
    creative_template: LinkedInCreativeRequestTemplate
    existing_post_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    creative_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    creative_version_id: str = Field(pattern=r"^creative:sha256:[0-9a-f]{64}$")
    asset_version_id: str = Field(pattern=r"^asset:sha256:[0-9a-f]{64}$")
    asset_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendition_id: str = Field(pattern=r"^rendition:sha256:[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    maximum_spend: Money
    planning_boundary: LinkedInAdsPlanningBoundary

    @field_validator("account_timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        return _iana_timezone(value)

    @model_validator(mode="after")
    def hierarchy_content_and_spend_are_exact(self) -> LinkedInAdsCompiledCampaignSpec:
        try:
            group_total_minor_units = _native_money_minor_units(
                self.campaign_group_request.totalBudget.amount
            )
            daily_minor_units = _native_money_minor_units(
                self.campaign_template.request.dailyBudget.amount
            )
            _native_money_minor_units(self.campaign_template.request.unitCost.amount)
        except ValueError as exc:
            raise ValueError("compiled LinkedIn native money amount is invalid") from exc
        if (
            len({self.campaign_group_handle, self.campaign_handle, self.creative_handle}) != 3
            or self.campaign_template.parent_campaign_group_handle != self.campaign_group_handle
            or self.creative_template.parent_campaign_handle != self.campaign_handle
            or self.campaign_group_request.account != self.campaign_template.request.account
            or self.campaign_group_request.totalBudget.currencyCode != self.account_currency
            or self.campaign_template.request.dailyBudget.currencyCode != self.account_currency
            or self.campaign_template.request.unitCost.currencyCode != self.account_currency
            or type(self.maximum_spend.minor_units) is not int
            or self.maximum_spend.currency != self.account_currency
            or not 1 <= self.maximum_spend.minor_units <= _MAX_MINOR_UNITS
            or self.maximum_spend.minor_units != group_total_minor_units
            or daily_minor_units > group_total_minor_units
            or self.campaign_group_request.runSchedule != self.campaign_template.request.runSchedule
            or self.campaign_group_request.status != "DRAFT"
            or self.campaign_template.request.status != "DRAFT"
            or self.creative_template.request.intendedStatus != "DRAFT"
            or self.campaign_template.request.associatedEntity == ""
            or self.creative_content_digest
            != _creative_content_digest(
                post_digest=self.existing_post_digest,
                post_urn=self.creative_template.request.content.reference,
                author_urn=self.campaign_template.request.associatedEntity,
                dsc_ad_account_urn=self.campaign_group_request.account,
                creative_version_id=self.creative_version_id,
                asset_content_digest=self.asset_content_digest,
            )
        ):
            raise ValueError(
                "compiled LinkedIn hierarchy, creative binding, or group spend cap is invalid"
            )
        return self

    @property
    def spec_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def redacted_review(self) -> LinkedInAdsRedactedReview:
        return LinkedInAdsRedactedReview(
            account_ref=self.account_ref,
            account_currency=self.account_currency,
            account_timezone=self.account_timezone,
            campaign_group_handle=self.campaign_group_handle,
            campaign_handle=self.campaign_handle,
            creative_handle=self.creative_handle,
            creative_content_digest=self.creative_content_digest,
            creative_version_id=self.creative_version_id,
            maximum_spend=self.maximum_spend,
            execution_status=self.planning_boundary.status,
            spec_digest=self.spec_digest,
        )


def compile_linkedin_ads_campaign(
    brief: LinkedInAdsCampaignBrief,
) -> LinkedInAdsCompiledCampaignSpec:
    """Compile a non-executable candidate and revalidate every frozen input field."""
    brief = LinkedInAdsCampaignBrief.model_validate(brief.model_dump(mode="python", warnings=False))
    creative = brief.creative
    schedule = LinkedInNativeRunSchedule(
        start=_millis(brief.start_time), end=_millis(brief.stop_time)
    )
    return LinkedInAdsCompiledCampaignSpec(
        account_ref=brief.account_ref,
        account_currency=brief.account_currency,
        currency_minor_unit_exponent=brief.currency_minor_unit_exponent,
        account_timezone=brief.account_timezone,
        campaign_group_handle=brief.campaign_group_handle,
        campaign_group_request=LinkedInNativeCampaignGroupPayload(
            account=brief.account_urn,
            name=brief.campaign_group_name,
            runSchedule=schedule,
            totalBudget=LinkedInNativeMoney(
                amount=_money_amount(brief.group_total_budget.minor_units)
            ),
        ),
        campaign_handle=brief.campaign_handle,
        campaign_template=LinkedInCampaignRequestTemplate(
            parent_campaign_group_handle=brief.campaign_group_handle,
            request=LinkedInNativeCampaignRequestFields(
                account=brief.account_urn,
                associatedEntity=creative.existing_post.author_urn,
                dailyBudget=LinkedInNativeMoney(
                    amount=_money_amount(brief.daily_budget.minor_units)
                ),
                name=brief.campaign_name,
                runSchedule=schedule,
                targetingCriteria=LinkedInNativeTargetingCriteria.model_validate(
                    {
                        "include": {
                            "and": (
                                {"or": {"urn:li:adTargetingFacet:locations": brief.location_urns}},
                                {
                                    "or": {
                                        "urn:li:adTargetingFacet:interfaceLocales": (
                                            brief.interface_locale_urns
                                        )
                                    }
                                },
                            )
                        }
                    }
                ),
                unitCost=LinkedInNativeMoney(amount=_money_amount(brief.unit_cost.minor_units)),
                politicalIntent=brief.political_intent,
            ),
        ),
        creative_handle=brief.creative_handle,
        creative_template=LinkedInCreativeRequestTemplate(
            parent_campaign_handle=brief.campaign_handle,
            request=LinkedInNativeCreativeRequestFields(
                content=LinkedInNativeCreativeContent(reference=creative.existing_post.post_urn)
            ),
        ),
        existing_post_digest=creative.existing_post.canonical_digest,
        creative_content_digest=_creative_content_digest(
            post_digest=creative.existing_post.canonical_digest,
            post_urn=creative.existing_post.post_urn,
            author_urn=creative.existing_post.author_urn,
            dsc_ad_account_urn=creative.existing_post.dsc_ad_account_urn,
            creative_version_id=creative.creative_version.creative_version_id,
            asset_content_digest=creative.asset.content_digest,
        ),
        creative_version_id=creative.creative_version.creative_version_id,
        asset_version_id=creative.asset.asset_version_id,
        asset_content_digest=creative.asset.content_digest,
        rendition_id=creative.rendition.rendition_id,
        input_digest=digest(brief.model_dump(mode="json")),
        maximum_spend=brief.group_total_budget,
        planning_boundary=LinkedInAdsPlanningBoundary(),
    )
