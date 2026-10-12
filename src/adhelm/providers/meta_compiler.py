"""Offline compiler for one paused Meta Ads single-image traffic hierarchy."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.creative import CreativeAssetVersion, CreativeRendition, CreativeVersion
from adhelm.models import Money, SafeRef, StrictModel
from adhelm.privacy import validate_destination

_API_VERSION: Literal["v26.0"] = "v26.0"
_CAPABILITY_VERSION: Literal["meta-ads-sdk-26.0.0-write-contract-2026-09-04"] = (
    "meta-ads-sdk-26.0.0-write-contract-2026-09-04"
)
_FORMAT: Literal["META_ADS_SINGLE_IMAGE_LINK"] = "META_ADS_SINGLE_IMAGE_LINK"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_MINOR_UNITS = 9_007_199_254_740_991


def _destination_ref(url: str) -> str:
    return "destination:sha256:" + digest({"target_url": url})


def _non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must include a non-space character")
    return value


def _iana_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("account timezone must be an IANA timezone") from exc
    return value


def _opaque_image_hash(value: str) -> str:
    if not value.strip() or any(not char.isascii() or not 33 <= ord(char) <= 126 for char in value):
        raise ValueError("image_hash must be a printable opaque provider reference")
    return value


def _creative_content_digest(
    *,
    page_id: str,
    primary_text: str,
    headline: str,
    destination_url: str,
    image_hash: str,
    creative_version_id: str,
    asset_content_digest: str,
) -> str:
    """Bind every native single-image creative input to its local identity."""
    return digest(
        {
            "format": "single_image_link",
            "page_id": page_id,
            "primary_text": primary_text,
            "headline": headline,
            "destination_url": destination_url,
            "cta": "LEARN_MORE",
            "image_hash": image_hash,
            "creative_version_id": creative_version_id,
            "asset_content_digest": asset_content_digest,
        }
    )


class MetaAdsDestinationPolicy(StrictModel):
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
            not value.startswith("/") or "?" in value or "#" in value for value in values
        ):
            raise ValueError("allowed paths must be unique, sorted absolute paths")
        return values


class MetaAdsImageReferenceEvidence(StrictModel):
    """Local evidence for an existing account image; it is not upload proof."""

    account_ref: SafeRef
    image_hash: str = Field(min_length=1, max_length=512)
    asset_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ref: str = Field(pattern=r"^evidence:sha256:[0-9a-f]{64}$")

    @field_validator("image_hash")
    @classmethod
    def opaque_image_hash(cls, value: str) -> str:
        return _opaque_image_hash(value)


class MetaAdsCreativeAssociation(StrictModel):
    creative_version: CreativeVersion
    asset: CreativeAssetVersion
    rendition: CreativeRendition

    @model_validator(mode="after")
    def exact_local_chain(self) -> MetaAdsCreativeAssociation:
        asset, rendition, manifest = self.asset, self.rendition, self.creative_version.manifest
        expected_asset = "asset:sha256:" + digest(
            {
                "content_digest": asset.content_digest,
                "mime_type": asset.mime_type,
                "width_px": asset.width_px,
                "height_px": asset.height_px,
            }
        )
        expected_transformation_digest = digest(rendition.transformation_spec)
        expected_rendition = "rendition:sha256:" + digest(
            {
                "asset_version_id": asset.asset_version_id,
                "provider_format": _FORMAT,
                "locale": manifest.locale,
                "transformation_digest": expected_transformation_digest,
            }
        )
        if (
            _DIGEST.fullmatch(asset.content_digest) is None
            or asset.asset_version_id != expected_asset
        ):
            raise ValueError(
                "creative asset identity must match its content and intrinsic metadata"
            )
        if (
            manifest.provider_format != _FORMAT
            or rendition.provider_format != _FORMAT
            or manifest.surface_kind != "MEDIA"
            or rendition.locale != manifest.locale
            or rendition.source_relation_status != "PROVEN_SOURCE_ASSET"
            or rendition.source_asset_version_ref != asset.asset_version_id
            or rendition.content_digest != asset.content_digest
            or rendition.width_px != asset.width_px
            or rendition.height_px != asset.height_px
            or rendition.transformation_digest != expected_transformation_digest
            or rendition.rendition_id != expected_rendition
            or len(manifest.rendition_refs) != 1
            or manifest.rendition_refs[0].rendition_id != rendition.rendition_id
            or manifest.rendition_refs[0].role != "HERO"
        ):
            raise ValueError("creative association must prove an exact identity rendition")
        return self


class MetaAdsSingleImageLinkCreative(StrictModel):
    page_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    primary_text: str = Field(min_length=1, max_length=500)
    headline: str = Field(min_length=1, max_length=255)
    destination_url: str = Field(min_length=1, max_length=2_000)
    call_to_action: Literal["LEARN_MORE"] = "LEARN_MORE"
    association: MetaAdsCreativeAssociation
    image_reference: MetaAdsImageReferenceEvidence

    @model_validator(mode="after")
    def exact_serving_inputs(self) -> MetaAdsSingleImageLinkCreative:
        manifest = self.association.creative_version.manifest
        primary = [item for item in manifest.components if item.component_role == "PRIMARY_TEXT"]
        headline = [item for item in manifest.components if item.component_role == "HEADLINE"]
        cta = [item for item in manifest.components if item.component_role == "CTA_BUTTON"]
        if (
            self.image_reference.asset_content_digest != self.association.asset.content_digest
            or manifest.destination_ref != _destination_ref(self.destination_url)
            or len(manifest.components) != 3
            or len(primary) != 1
            or primary[0].exact_text != self.primary_text
            or primary[0].provider_slot != "message"
            or len(headline) != 1
            or headline[0].exact_text != self.headline
            or headline[0].provider_slot != "name"
            or len(cta) != 1
            or cta[0].provider_slot != "call_to_action.type"
            or cta[0].normalized_cta_intent != self.call_to_action
            or cta[0].cta_destination_ref != manifest.destination_ref
        ):
            raise ValueError(
                "creative version must exactly match the fixed Meta serving copy, CTA, image, "
                "and destination"
            )
        return self

    @property
    def content_digest(self) -> str:
        return _creative_content_digest(
            page_id=self.page_id,
            primary_text=self.primary_text,
            headline=self.headline,
            destination_url=self.destination_url,
            image_hash=self.image_reference.image_hash,
            creative_version_id=self.association.creative_version.creative_version_id,
            asset_content_digest=self.association.asset.content_digest,
        )


class MetaAdsCampaignBrief(StrictModel):
    """One US, Facebook-Feed, traffic-only candidate with a lifetime cap."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    account_currency: Literal["USD"] = "USD"
    currency_minor_unit_exponent: Literal[2] = 2
    account_timezone: str = Field(min_length=1, max_length=80)
    campaign_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    adset_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    ad_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    campaign_name: str = Field(min_length=1, max_length=512)
    adset_name: str = Field(min_length=1, max_length=512)
    ad_name: str = Field(min_length=1, max_length=512)
    lifetime_budget: Money
    start_time: datetime
    stop_time: datetime
    country_codes: tuple[str, ...] = ("US",)
    special_ad_category: Literal["NONE"] = "NONE"
    destination_policy: MetaAdsDestinationPolicy
    creative: MetaAdsSingleImageLinkCreative

    @field_validator("country_codes")
    @classmethod
    def only_reviewed_country(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != ("US",):
            raise ValueError("only the reviewed US country target is supported")
        return value

    @model_validator(mode="after")
    def valid_schedule_and_bindings(self) -> MetaAdsCampaignBrief:
        if (
            type(self.lifetime_budget.minor_units) is not int
            or self.lifetime_budget.currency != self.account_currency
            or not 1 <= self.lifetime_budget.minor_units <= _MAX_MINOR_UNITS
        ):
            raise ValueError("lifetime budget must be positive USD within native limits")
        for field_name, value in (
            ("campaign name", self.campaign_name),
            ("ad set name", self.adset_name),
            ("ad name", self.ad_name),
        ):
            _non_blank(value, field_name)
        if (
            self.start_time.tzinfo is None
            or self.stop_time.tzinfo is None
            or self.stop_time <= self.start_time
        ):
            raise ValueError("schedule requires timezone-aware increasing times")
        _iana_timezone(self.account_timezone)
        handles = (
            self.campaign_handle,
            self.adset_handle,
            self.ad_handle,
            "creative_" + self.ad_handle,
        )
        if len(set(handles)) != len(handles):
            raise ValueError("local campaign hierarchy handles must be unique")
        if self.creative.image_reference.account_ref != self.account_ref:
            raise ValueError("image-reference evidence must be bound to the compiled account")
        return self


class MetaAdsNativeCampaignPayload(StrictModel):
    name: str = Field(min_length=1, max_length=512)
    objective: Literal["OUTCOME_TRAFFIC"] = "OUTCOME_TRAFFIC"
    buying_type: Literal["AUCTION"] = "AUCTION"
    special_ad_categories: tuple[()] = ()
    lifetime_budget: int = Field(ge=1, le=_MAX_MINOR_UNITS)
    start_time: datetime
    stop_time: datetime
    status: Literal["PAUSED"] = "PAUSED"

    @field_validator("name")
    @classmethod
    def non_blank_name(cls, value: str) -> str:
        return _non_blank(value, "campaign name")

    @model_validator(mode="after")
    def schedule_is_aware_and_increasing(self) -> MetaAdsNativeCampaignPayload:
        if (
            self.start_time.tzinfo is None
            or self.stop_time.tzinfo is None
            or self.stop_time <= self.start_time
        ):
            raise ValueError("schedule requires timezone-aware increasing times")
        return self


class MetaAdsNativeGeoLocations(StrictModel):
    """The sole reviewed geographic target; no audience fields are representable."""

    countries: tuple[Literal["US"], ...] = Field(default=("US",), min_length=1, max_length=1)


class MetaAdsNativeTargeting(StrictModel):
    geo_locations: MetaAdsNativeGeoLocations
    publisher_platforms: tuple[Literal["facebook"], ...] = Field(
        default=("facebook",), min_length=1, max_length=1
    )
    facebook_positions: tuple[Literal["feed"], ...] = Field(
        default=("feed",), min_length=1, max_length=1
    )


class MetaAdsNativeAdSetPayload(StrictModel):
    name: str = Field(min_length=1, max_length=512)
    status: Literal["PAUSED"] = "PAUSED"
    billing_event: Literal["IMPRESSIONS"] = "IMPRESSIONS"
    optimization_goal: Literal["LINK_CLICKS"] = "LINK_CLICKS"
    bid_strategy: Literal["LOWEST_COST_WITHOUT_CAP"] = "LOWEST_COST_WITHOUT_CAP"
    targeting: MetaAdsNativeTargeting

    @field_validator("name")
    @classmethod
    def non_blank_name(cls, value: str) -> str:
        return _non_blank(value, "ad set name")


class MetaAdsNativeCallToAction(StrictModel):
    type: Literal["LEARN_MORE"] = "LEARN_MORE"


class MetaAdsNativeLinkData(StrictModel):
    image_hash: str = Field(min_length=1, max_length=512)
    link: str = Field(min_length=1, max_length=2_000)
    message: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=255)
    call_to_action: MetaAdsNativeCallToAction

    @field_validator("image_hash")
    @classmethod
    def opaque_image_hash(cls, value: str) -> str:
        return _opaque_image_hash(value)

    @field_validator("message", "name")
    @classmethod
    def non_blank_copy(cls, value: str) -> str:
        return _non_blank(value, "native creative text")


class MetaAdsNativeObjectStorySpec(StrictModel):
    page_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    link_data: MetaAdsNativeLinkData


class MetaAdsNativeCreativePayload(StrictModel):
    name: str = Field(min_length=1, max_length=512)
    object_story_spec: MetaAdsNativeObjectStorySpec

    @field_validator("name")
    @classmethod
    def non_blank_name(cls, value: str) -> str:
        return _non_blank(value, "creative name")


class MetaAdsNativeAdPayload(StrictModel):
    name: str = Field(min_length=1, max_length=512)
    status: Literal["PAUSED"] = "PAUSED"

    @field_validator("name")
    @classmethod
    def non_blank_name(cls, value: str) -> str:
        return _non_blank(value, "ad name")


class MetaAdsPlanningBoundary(StrictModel):
    status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    requires_current_account_identity: Literal[True] = True
    requires_account_currency_readback: Literal[True] = True
    requires_image_readback: Literal[True] = True
    requires_page_eligibility_readback: Literal[True] = True
    requires_placement_eligibility_readback: Literal[True] = True
    requires_creative_transformation_readback: Literal[True] = True
    requires_creative_ledger_check: Literal[True] = True
    requires_destination_and_tracking_validation: Literal[True] = True
    requires_native_conversion_check: Literal[True] = True
    requires_drift_check_and_authorization: Literal[True] = True


class MetaAdsRedactedReview(StrictModel):
    """Default review data; never includes copy, destination, Page, or image reference."""

    account_ref: SafeRef
    account_currency: Literal["USD"]
    account_timezone: str
    account_identity_status: Literal["CUSTOMER_DECLARED_REQUIRES_READBACK"] = (
        "CUSTOMER_DECLARED_REQUIRES_READBACK"
    )
    campaign_handle: str
    adset_handle: str
    creative_handle: str
    ad_handle: str
    initial_state: Literal["PAUSED"] = "PAUSED"
    objective: Literal["OUTCOME_TRAFFIC"] = "OUTCOME_TRAFFIC"
    requested_placement: Literal["FACEBOOK_FEED"] = "FACEBOOK_FEED"
    placement_eligibility_status: Literal["REQUIRES_API_READBACK"] = "REQUIRES_API_READBACK"
    creative_transformation_status: Literal["REQUIRES_API_READBACK"] = "REQUIRES_API_READBACK"
    country_count: Literal[1] = 1
    creative_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    creative_version_id: str = Field(pattern=r"^creative:sha256:[0-9a-f]{64}$")
    maximum_spend: Money
    execution_status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"]
    spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class MetaAdsCompiledCampaignSpec(StrictModel):
    provider: Literal["meta_ads"] = "meta_ads"
    provider_api_version: Literal["v26.0"] = _API_VERSION
    capability_version: Literal["meta-ads-sdk-26.0.0-write-contract-2026-09-04"] = (
        _CAPABILITY_VERSION
    )
    account_ref: SafeRef
    # These are customer declarations until a future guarded read proves them.
    account_currency: Literal["USD"]
    currency_minor_unit_exponent: Literal[2]
    account_timezone: str
    campaign_handle: str
    campaign_request: MetaAdsNativeCampaignPayload
    adset_handle: str
    adset_parent_campaign_handle: str
    adset_request: MetaAdsNativeAdSetPayload
    creative_handle: str
    creative_consumed_by_ad_handle: str
    creative_request: MetaAdsNativeCreativePayload
    ad_handle: str
    ad_parent_adset_handle: str
    ad_creative_handle: str
    ad_request: MetaAdsNativeAdPayload
    creative_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    creative_version_id: str = Field(pattern=r"^creative:sha256:[0-9a-f]{64}$")
    asset_version_id: str = Field(pattern=r"^asset:sha256:[0-9a-f]{64}$")
    asset_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendition_id: str = Field(pattern=r"^rendition:sha256:[0-9a-f]{64}$")
    image_reference_evidence_ref: str = Field(pattern=r"^evidence:sha256:[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    maximum_spend: Money
    planning_boundary: MetaAdsPlanningBoundary

    @field_validator("account_timezone")
    @classmethod
    def account_timezone_is_iana(cls, value: str) -> str:
        return _iana_timezone(value)

    @model_validator(mode="after")
    def exact_local_hierarchy_and_spend_bound(self) -> MetaAdsCompiledCampaignSpec:
        handles = (
            self.campaign_handle,
            self.adset_handle,
            self.creative_handle,
            self.ad_handle,
        )
        if (
            len(set(handles)) != len(handles)
            or self.adset_parent_campaign_handle != self.campaign_handle
            or self.creative_handle != "creative_" + self.ad_handle
            or self.creative_consumed_by_ad_handle != self.ad_handle
            or self.ad_parent_adset_handle != self.adset_handle
            or self.ad_creative_handle != self.creative_handle
            or self.creative_request.name != self.ad_request.name
            or type(self.maximum_spend.minor_units) is not int
            or type(self.campaign_request.lifetime_budget) is not int
            or not 1 <= self.maximum_spend.minor_units <= _MAX_MINOR_UNITS
            or self.campaign_request.lifetime_budget != self.maximum_spend.minor_units
            or self.maximum_spend.currency != self.account_currency
            or self.creative_content_digest
            != _creative_content_digest(
                page_id=self.creative_request.object_story_spec.page_id,
                primary_text=self.creative_request.object_story_spec.link_data.message,
                headline=self.creative_request.object_story_spec.link_data.name,
                destination_url=self.creative_request.object_story_spec.link_data.link,
                image_hash=self.creative_request.object_story_spec.link_data.image_hash,
                creative_version_id=self.creative_version_id,
                asset_content_digest=self.asset_content_digest,
            )
        ):
            raise ValueError("compiled Meta hierarchy bindings or spend limit are invalid")
        return self

    @property
    def spec_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def redacted_review(self) -> MetaAdsRedactedReview:
        return MetaAdsRedactedReview(
            account_ref=self.account_ref,
            account_currency=self.account_currency,
            account_timezone=self.account_timezone,
            campaign_handle=self.campaign_handle,
            adset_handle=self.adset_handle,
            creative_handle=self.creative_handle,
            ad_handle=self.ad_handle,
            creative_content_digest=self.creative_content_digest,
            creative_version_id=self.creative_version_id,
            maximum_spend=self.maximum_spend,
            execution_status=self.planning_boundary.status,
            spec_digest=self.spec_digest,
        )


def compile_meta_ads_campaign(brief: MetaAdsCampaignBrief) -> MetaAdsCompiledCampaignSpec:
    """Compile a local candidate. This function has no network or mutation path."""
    brief = MetaAdsCampaignBrief.model_validate(brief.model_dump(mode="python", warnings=False))
    validate_destination(
        brief.creative.destination_url,
        allowed_hosts=set(brief.destination_policy.allowed_hosts),
        allowed_paths=set(brief.destination_policy.allowed_paths),
    )
    creative = brief.creative
    return MetaAdsCompiledCampaignSpec(
        account_ref=brief.account_ref,
        account_currency=brief.account_currency,
        currency_minor_unit_exponent=brief.currency_minor_unit_exponent,
        account_timezone=brief.account_timezone,
        campaign_handle=brief.campaign_handle,
        campaign_request=MetaAdsNativeCampaignPayload(
            name=brief.campaign_name,
            lifetime_budget=brief.lifetime_budget.minor_units,
            start_time=brief.start_time.astimezone(UTC),
            stop_time=brief.stop_time.astimezone(UTC),
        ),
        adset_handle=brief.adset_handle,
        adset_parent_campaign_handle=brief.campaign_handle,
        adset_request=MetaAdsNativeAdSetPayload(
            name=brief.adset_name,
            targeting=MetaAdsNativeTargeting(
                geo_locations=MetaAdsNativeGeoLocations(),
            ),
        ),
        creative_handle="creative_" + brief.ad_handle,
        creative_consumed_by_ad_handle=brief.ad_handle,
        creative_request=MetaAdsNativeCreativePayload(
            name=brief.ad_name,
            object_story_spec=MetaAdsNativeObjectStorySpec(
                page_id=creative.page_id,
                link_data=MetaAdsNativeLinkData(
                    image_hash=creative.image_reference.image_hash,
                    link=creative.destination_url,
                    message=creative.primary_text,
                    name=creative.headline,
                    call_to_action=MetaAdsNativeCallToAction(),
                ),
            ),
        ),
        ad_handle=brief.ad_handle,
        ad_parent_adset_handle=brief.adset_handle,
        ad_creative_handle="creative_" + brief.ad_handle,
        ad_request=MetaAdsNativeAdPayload(name=brief.ad_name),
        creative_content_digest=creative.content_digest,
        creative_version_id=creative.association.creative_version.creative_version_id,
        asset_version_id=creative.association.asset.asset_version_id,
        asset_content_digest=creative.association.asset.content_digest,
        rendition_id=creative.association.rendition.rendition_id,
        image_reference_evidence_ref=creative.image_reference.evidence_ref,
        input_digest=digest(brief.model_dump(mode="json")),
        maximum_spend=brief.lifetime_budget,
        planning_boundary=MetaAdsPlanningBoundary(),
    )
