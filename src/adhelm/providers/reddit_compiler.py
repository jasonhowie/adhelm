"""Offline-only compiler for one paused Reddit Ads v3 standard traffic hierarchy.

The module intentionally produces a typed local graph, never a request body or
provider ID.  A guarded executor must replace the local parent handles after
readback and then perform the lifecycle required by the product contract.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.creative import CreativeAssetVersion, CreativeRendition, CreativeVersion
from adhelm.errors import AdhelmError
from adhelm.models import Money, SafeRef, StrictModel
from adhelm.privacy import validate_destination

_API_VERSION: Literal["v3"] = "v3"
_CAPABILITY_VERSION: Literal["reddit-ads-api-v3-cbo-traffic-post-contract-2026-09-04"] = (
    "reddit-ads-api-v3-cbo-traffic-post-contract-2026-09-04"
)
_FORMAT: Literal["REDDIT_ADS_STANDARD_POST"] = "REDDIT_ADS_STANDARD_POST"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HANDLE = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
_POST_ID = re.compile(r"^t3_[A-Za-z0-9_-]{1,156}$")
_OPAQUE_NATIVE_ID = re.compile(r"^[!-~]{1,160}$")
_MAX_MICROS = 9_007_199_254_740_991
_MICROS_PER_USD_MINOR_UNIT = 10_000
_MAX_MINOR_UNITS = _MAX_MICROS // _MICROS_PER_USD_MINOR_UNIT
_MAX_NAME_LENGTH = 500


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


def _native_utc_timestamp(value: datetime) -> datetime:
    """Native v3 timestamps are emitted as explicit UTC schedule boundaries."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("native schedule timestamps must use a UTC offset")
    return value


def _opaque_native_id(value: str, field_name: str) -> str:
    """Retain an issued identifier without inventing a provider-specific grammar."""
    if _OPAQUE_NATIVE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded printable ASCII provider reference")
    return value


def _post_content_digest(version: CreativeVersion) -> str:
    """Bind the existing native post to its canonical serving surface."""
    manifest = version.manifest
    return digest(
        {
            "provider_format": manifest.provider_format,
            "creative_version_id": version.creative_version_id,
            "destination_ref": manifest.destination_ref,
            "components": [
                {
                    "role": item.component_role,
                    "ordinal": item.ordinal,
                    "text": item.exact_text,
                    "provider_slot": item.provider_slot,
                    "cta": item.normalized_cta_intent,
                    "cta_destination_ref": item.cta_destination_ref,
                }
                for item in manifest.components
            ],
        }
    )


class RedditAdsDestinationPolicy(StrictModel):
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


class RedditAdsCreativeAssociation(StrictModel):
    creative_version: CreativeVersion
    asset: CreativeAssetVersion
    rendition: CreativeRendition

    @model_validator(mode="after")
    def exact_identity_rendition(self) -> RedditAdsCreativeAssociation:
        asset, rendition, manifest = self.asset, self.rendition, self.creative_version.manifest
        expected_asset = "asset:sha256:" + digest(
            {
                "content_digest": asset.content_digest,
                "mime_type": asset.mime_type,
                "width_px": asset.width_px,
                "height_px": asset.height_px,
            }
        )
        expected_transformation = digest(rendition.transformation_spec)
        expected_rendition = "rendition:sha256:" + digest(
            {
                "asset_version_id": asset.asset_version_id,
                "provider_format": _FORMAT,
                "locale": manifest.locale,
                "transformation_digest": expected_transformation,
            }
        )
        if (
            _DIGEST.fullmatch(asset.content_digest) is None
            or asset.asset_version_id != expected_asset
            or manifest.provider_format != _FORMAT
            or manifest.surface_kind != "MEDIA"
            or rendition.provider_format != _FORMAT
            or rendition.locale != manifest.locale
            or rendition.source_relation_status != "PROVEN_SOURCE_ASSET"
            or rendition.source_asset_version_ref != asset.asset_version_id
            or rendition.content_digest != asset.content_digest
            or rendition.width_px != asset.width_px
            or rendition.height_px != asset.height_px
            or rendition.transformation_digest != expected_transformation
            or rendition.rendition_id != expected_rendition
            or len(manifest.rendition_refs) != 1
            or manifest.rendition_refs[0].rendition_id != rendition.rendition_id
            or manifest.rendition_refs[0].role != "HERO"
        ):
            raise ValueError("creative association must prove one exact native-post rendition")
        return self


class RedditAdsPostReferenceEvidence(StrictModel):
    """Customer-local evidence for an existing native post, never upload proof."""

    account_ref: SafeRef
    post_id: str = Field(min_length=4, max_length=160)
    post_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ref: str = Field(pattern=r"^evidence:sha256:[0-9a-f]{64}$")

    @field_validator("post_id")
    @classmethod
    def documented_legacy_post_id(cls, value: str) -> str:
        if _POST_ID.fullmatch(value) is None:
            raise ValueError("post_id must be a documented legacy t3_ post reference")
        return value


class RedditAdsPixelReferenceEvidence(StrictModel):
    """Customer-local pixel binding to verify before any future provider request."""

    account_ref: SafeRef
    pixel_id: str = Field(min_length=4, max_length=160)
    evidence_ref: str = Field(pattern=r"^evidence:sha256:[0-9a-f]{64}$")

    @field_validator("pixel_id")
    @classmethod
    def opaque_pixel_ref(cls, value: str) -> str:
        return _opaque_native_id(value, "pixel_id")


class RedditAdsFundingInstrumentReferenceEvidence(StrictModel):
    """Customer-local funding binding to verify before any future provider request."""

    account_ref: SafeRef
    funding_instrument_id: str = Field(min_length=1, max_length=160)
    evidence_ref: str = Field(pattern=r"^evidence:sha256:[0-9a-f]{64}$")

    @field_validator("funding_instrument_id")
    @classmethod
    def opaque_funding_instrument_ref(cls, value: str) -> str:
        return _opaque_native_id(value, "funding_instrument_id")


class RedditAdsStandardPostCreative(StrictModel):
    destination_url: str = Field(min_length=1, max_length=2_000)
    call_to_action: Literal["LEARN_MORE"] = "LEARN_MORE"
    association: RedditAdsCreativeAssociation
    post_reference: RedditAdsPostReferenceEvidence

    @model_validator(mode="after")
    def exact_post_surface(self) -> RedditAdsStandardPostCreative:
        manifest = self.association.creative_version.manifest
        headlines = [item for item in manifest.components if item.component_role == "HEADLINE"]
        ctas = [item for item in manifest.components if item.component_role == "CTA_BUTTON"]
        if (
            manifest.destination_ref != _destination_ref(self.destination_url)
            or len(headlines) != 1
            or headlines[0].provider_slot != "creative.headline"
            or len(ctas) != 1
            or ctas[0].provider_slot != "creative.destination"
            or ctas[0].normalized_cta_intent != self.call_to_action
            or ctas[0].cta_destination_ref != manifest.destination_ref
            or self.post_reference.post_content_digest
            != _post_content_digest(self.association.creative_version)
        ):
            raise ValueError(
                "canonical creative must exactly bind the native post, CTA, and destination"
            )
        return self

    @property
    def content_digest(self) -> str:
        return digest(
            {
                "type": "standard_post",
                "creative_version_id": self.association.creative_version.creative_version_id,
                "asset_content_digest": self.association.asset.content_digest,
                "post_id": self.post_reference.post_id,
                "post_content_digest": self.post_reference.post_content_digest,
                "destination_url": self.destination_url,
                "call_to_action": self.call_to_action,
            }
        )


class RedditAdsCampaignBrief(StrictModel):
    """One US feed-only CBO traffic candidate with a typed USD lifetime cap."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    account_currency: Literal["USD"] = "USD"
    currency_minor_unit_exponent: Literal[2] = 2
    account_timezone: str = Field(min_length=1, max_length=80)
    campaign_handle: str = Field(min_length=1, max_length=80)
    ad_group_handle: str = Field(min_length=1, max_length=80)
    ad_handle: str = Field(min_length=1, max_length=80)
    campaign_name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    ad_group_name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    ad_name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    lifetime_budget: Money
    start_time: datetime
    end_time: datetime
    pixel_reference: RedditAdsPixelReferenceEvidence
    funding_instrument_reference: RedditAdsFundingInstrumentReferenceEvidence
    destination_policy: RedditAdsDestinationPolicy
    creative: RedditAdsStandardPostCreative

    @field_validator("campaign_handle", "ad_group_handle", "ad_handle")
    @classmethod
    def local_handle(cls, value: str) -> str:
        if _HANDLE.fullmatch(value) is None:
            raise ValueError("hierarchy handles must be local lowercase logical handles")
        return value

    @model_validator(mode="after")
    def schedule_and_bindings(self) -> RedditAdsCampaignBrief:
        if (
            type(self.lifetime_budget.minor_units) is not int
            or self.lifetime_budget.currency != self.account_currency
            or not 1 <= self.lifetime_budget.minor_units <= _MAX_MINOR_UNITS
        ):
            raise ValueError("lifetime budget must be positive USD within native micros bounds")
        for field_name, value in (
            ("campaign name", self.campaign_name),
            ("ad group name", self.ad_group_name),
            ("ad name", self.ad_name),
        ):
            _non_blank(value, field_name)
        if (
            self.start_time.tzinfo is None
            or self.end_time.tzinfo is None
            or self.end_time <= self.start_time
        ):
            raise ValueError("schedule requires timezone-aware increasing times")
        _iana_timezone(self.account_timezone)
        if len({self.campaign_handle, self.ad_group_handle, self.ad_handle}) != 3:
            raise ValueError("local hierarchy handles must be globally unique")
        if (
            self.creative.post_reference.account_ref != self.account_ref
            or self.pixel_reference.account_ref != self.account_ref
            or self.funding_instrument_reference.account_ref != self.account_ref
        ):
            raise ValueError(
                "post, pixel, and funding evidence must be bound to the compiled account"
            )
        return self


class RedditAdsNativeCampaignPayload(StrictModel):
    name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    configured_status: Literal["PAUSED"] = "PAUSED"
    objective: Literal["CLICKS"] = "CLICKS"
    is_campaign_budget_optimization: Literal[True] = True
    goal_type: Literal["LIFETIME_SPEND"] = "LIFETIME_SPEND"
    # Native ``goal_value`` is documented as an integer microcurrency amount.
    goal_value: int = Field(ge=1, le=_MAX_MICROS)
    start_time: datetime
    end_time: datetime
    bid_strategy: Literal["BIDLESS"] = "BIDLESS"
    bid_type: Literal["CPC"] = "CPC"
    conversion_pixel_id: str = Field(min_length=4, max_length=160)
    funding_instrument_id: str = Field(min_length=1, max_length=160)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return _non_blank(value, "campaign name")

    @field_validator("conversion_pixel_id")
    @classmethod
    def opaque_pixel_ref(cls, value: str) -> str:
        return RedditAdsPixelReferenceEvidence.opaque_pixel_ref(value)

    @field_validator("funding_instrument_id")
    @classmethod
    def opaque_funding_instrument_ref(cls, value: str) -> str:
        return RedditAdsFundingInstrumentReferenceEvidence.opaque_funding_instrument_ref(value)

    @field_validator("start_time", "end_time")
    @classmethod
    def utc_schedule(cls, value: datetime) -> datetime:
        return _native_utc_timestamp(value)

    @model_validator(mode="after")
    def scheduled_lifetime_cap(self) -> RedditAdsNativeCampaignPayload:
        if (
            self.start_time.tzinfo is None
            or self.end_time.tzinfo is None
            or self.end_time <= self.start_time
        ):
            raise ValueError("schedule requires timezone-aware increasing times")
        return self


class RedditAdsNativeTargeting(StrictModel):
    """The entire reviewed target surface; audiences and free-form data are absent."""

    geolocations: tuple[Literal["US"], ...] = ("US",)
    locations: tuple[Literal["FEED"], ...] = ("FEED",)
    platforms: tuple[Literal["ALL"], ...] = ("ALL",)
    view_modes: tuple[Literal["ALL"], ...] = ("ALL",)

    @model_validator(mode="after")
    def fixed_native_targeting(self) -> RedditAdsNativeTargeting:
        if (
            self.geolocations != ("US",)
            or self.locations != ("FEED",)
            or self.platforms != ("ALL",)
            or self.view_modes != ("ALL",)
        ):
            raise ValueError("only the reviewed US feed targeting contract is supported")
        return self


class RedditAdsNativeAdGroupPayload(StrictModel):
    campaign_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    configured_status: Literal["PAUSED"] = "PAUSED"
    bid_type: Literal["CPC"] = "CPC"
    start_time: datetime
    end_time: datetime
    bid_strategy: None = None
    bid_value: None = None
    conversion_pixel_id: str = Field(min_length=4, max_length=160)
    targeting: RedditAdsNativeTargeting

    @field_validator("campaign_id", "name")
    @classmethod
    def non_blank(cls, value: str) -> str:
        return _non_blank(value, "ad group field")

    @field_validator("conversion_pixel_id")
    @classmethod
    def opaque_pixel_ref(cls, value: str) -> str:
        return RedditAdsPixelReferenceEvidence.opaque_pixel_ref(value)

    @field_validator("start_time", "end_time")
    @classmethod
    def utc_schedule(cls, value: datetime) -> datetime:
        return _native_utc_timestamp(value)

    @model_validator(mode="after")
    def scheduled_cbo_inheritance(self) -> RedditAdsNativeAdGroupPayload:
        if (
            self.start_time.tzinfo is None
            or self.end_time.tzinfo is None
            or self.end_time <= self.start_time
        ):
            raise ValueError("schedule requires timezone-aware increasing times")
        return self


class RedditAdsNativeAdPayload(StrictModel):
    ad_group_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    post_id: str = Field(min_length=4, max_length=160)
    click_url: str = Field(min_length=1, max_length=2_000)
    configured_status: Literal["PAUSED"] = "PAUSED"

    @field_validator("ad_group_id", "name")
    @classmethod
    def non_blank(cls, value: str) -> str:
        return _non_blank(value, "ad field")

    @field_validator("post_id")
    @classmethod
    def documented_post_ref(cls, value: str) -> str:
        return RedditAdsPostReferenceEvidence.documented_legacy_post_id(value)


class RedditAdsPlanningBoundary(StrictModel):
    status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    fixture_only_status: Literal["FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = (
        "FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    )
    requires_current_account_identity: Literal[True] = True
    requires_account_currency_readback: Literal[True] = True
    requires_funding_instrument_readback: Literal[True] = True
    requires_pixel_readback_and_native_conversion_check: Literal[True] = True
    requires_post_media_ownership_and_eligibility_readback: Literal[True] = True
    requires_objective_targeting_and_placement_eligibility_readback: Literal[True] = True
    requires_creative_transformation_readback: Literal[True] = True
    requires_creative_ledger_check: Literal[True] = True
    requires_destination_and_tracking_validation: Literal[True] = True
    requires_remote_drift_check_and_digest_authorization: Literal[True] = True


class RedditAdsRedactedReview(StrictModel):
    account_ref: SafeRef
    account_currency: Literal["USD"]
    account_timezone: str
    account_identity_status: Literal["CUSTOMER_DECLARED_REQUIRES_READBACK"] = (
        "CUSTOMER_DECLARED_REQUIRES_READBACK"
    )
    campaign_handle: str
    ad_group_handle: str
    ad_handle: str
    initial_state: Literal["PAUSED"] = "PAUSED"
    objective: Literal["CLICKS"] = "CLICKS"
    requested_geolocation: Literal["US"] = "US"
    requested_placement: Literal["FEED"] = "FEED"
    targeting_and_placement_eligibility_status: Literal["REQUIRES_API_READBACK"] = (
        "REQUIRES_API_READBACK"
    )
    creative_transformation_status: Literal["REQUIRES_API_READBACK"] = "REQUIRES_API_READBACK"
    creative_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    creative_version_id: str = Field(pattern=r"^creative:sha256:[0-9a-f]{64}$")
    maximum_spend: Money
    maximum_spend_micros: int = Field(ge=1, le=_MAX_MICROS)
    execution_status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"]
    spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RedditAdsCompiledCampaignSpec(StrictModel):
    provider: Literal["reddit_ads"] = "reddit_ads"
    provider_api_version: Literal["v3"] = _API_VERSION
    capability_version: Literal["reddit-ads-api-v3-cbo-traffic-post-contract-2026-09-04"] = (
        _CAPABILITY_VERSION
    )
    account_ref: SafeRef
    account_currency: Literal["USD"]
    currency_minor_unit_exponent: Literal[2]
    account_timezone: str
    destination_policy: RedditAdsDestinationPolicy
    pixel_reference: RedditAdsPixelReferenceEvidence
    funding_instrument_reference: RedditAdsFundingInstrumentReferenceEvidence
    creative: RedditAdsStandardPostCreative
    campaign_handle: str
    campaign_request: RedditAdsNativeCampaignPayload
    ad_group_handle: str
    ad_group_parent_campaign_handle: str
    ad_group_request: RedditAdsNativeAdGroupPayload
    ad_handle: str
    ad_parent_ad_group_handle: str
    ad_request: RedditAdsNativeAdPayload
    creative_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    creative_version_id: str = Field(pattern=r"^creative:sha256:[0-9a-f]{64}$")
    asset_version_id: str = Field(pattern=r"^asset:sha256:[0-9a-f]{64}$")
    asset_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendition_id: str = Field(pattern=r"^rendition:sha256:[0-9a-f]{64}$")
    post_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    post_reference_evidence_ref: str = Field(pattern=r"^evidence:sha256:[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    maximum_spend: Money
    maximum_spend_micros: int = Field(ge=1, le=_MAX_MICROS)
    planning_boundary: RedditAdsPlanningBoundary

    @field_validator("account_timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        return _iana_timezone(value)

    @model_validator(mode="after")
    def exact_hierarchy_creative_and_spend(self) -> RedditAdsCompiledCampaignSpec:
        try:
            validate_destination(
                self.creative.destination_url,
                allowed_hosts=set(self.destination_policy.allowed_hosts),
                allowed_paths=set(self.destination_policy.allowed_paths),
            )
        except AdhelmError as exc:
            raise ValueError(
                "compiled Reddit destination is outside the declared contract"
            ) from exc
        association = self.creative.association
        if (
            self.pixel_reference.account_ref != self.account_ref
            or self.funding_instrument_reference.account_ref != self.account_ref
            or self.creative.post_reference.account_ref != self.account_ref
        ):
            raise ValueError("compiled Reddit account evidence bindings are invalid")
        if (
            len({self.campaign_handle, self.ad_group_handle, self.ad_handle}) != 3
            or self.ad_group_parent_campaign_handle != self.campaign_handle
            or self.ad_parent_ad_group_handle != self.ad_group_handle
            or self.ad_group_request.campaign_id != self.campaign_handle
            or self.ad_request.ad_group_id != self.ad_group_handle
            or self.campaign_request.start_time != self.ad_group_request.start_time
            or self.campaign_request.end_time != self.ad_group_request.end_time
            or self.campaign_request.goal_value != self.maximum_spend_micros
            or type(self.maximum_spend.minor_units) is not int
            or type(self.maximum_spend_micros) is not int
            or type(self.campaign_request.goal_value) is not int
            or self.maximum_spend.currency != self.account_currency
            or not 1 <= self.maximum_spend.minor_units <= _MAX_MINOR_UNITS
            or self.maximum_spend_micros
            != self.maximum_spend.minor_units * _MICROS_PER_USD_MINOR_UNIT
            or self.campaign_request.objective != "CLICKS"
            or self.campaign_request.is_campaign_budget_optimization is not True
            or self.campaign_request.goal_type != "LIFETIME_SPEND"
            or self.campaign_request.bid_strategy != "BIDLESS"
            or self.campaign_request.bid_type != "CPC"
            or self.campaign_request.configured_status != "PAUSED"
            or self.ad_group_request.configured_status != "PAUSED"
            or self.ad_group_request.bid_type != "CPC"
            or self.ad_group_request.bid_strategy is not None
            or self.ad_group_request.bid_value is not None
            or self.ad_group_request.targeting != RedditAdsNativeTargeting()
            or self.ad_request.configured_status != "PAUSED"
            or self.campaign_request.conversion_pixel_id
            != self.ad_group_request.conversion_pixel_id
            or self.campaign_request.conversion_pixel_id != self.pixel_reference.pixel_id
            or self.campaign_request.funding_instrument_id
            != self.funding_instrument_reference.funding_instrument_id
            or self.ad_request.post_id != self.creative.post_reference.post_id
            or self.ad_request.click_url != self.creative.destination_url
            or self.creative_content_digest != self.creative.content_digest
            or self.creative_version_id != association.creative_version.creative_version_id
            or self.asset_version_id != association.asset.asset_version_id
            or self.asset_content_digest != association.asset.content_digest
            or self.rendition_id != association.rendition.rendition_id
            or self.post_content_digest != self.creative.post_reference.post_content_digest
            or self.post_reference_evidence_ref != self.creative.post_reference.evidence_ref
            or self.creative_content_digest
            != digest(
                {
                    "type": "standard_post",
                    "creative_version_id": self.creative_version_id,
                    "asset_content_digest": self.asset_content_digest,
                    "post_id": self.ad_request.post_id,
                    "post_content_digest": self.post_content_digest,
                    "destination_url": self.ad_request.click_url,
                    "call_to_action": "LEARN_MORE",
                }
            )
        ):
            raise ValueError(
                "compiled Reddit hierarchy, creative/destination binding, schedule, or "
                "native lifetime exposure limit is invalid"
            )
        return self

    @property
    def spec_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def redacted_review(self) -> RedditAdsRedactedReview:
        return RedditAdsRedactedReview(
            account_ref=self.account_ref,
            account_currency=self.account_currency,
            account_timezone=self.account_timezone,
            campaign_handle=self.campaign_handle,
            ad_group_handle=self.ad_group_handle,
            ad_handle=self.ad_handle,
            creative_content_digest=self.creative_content_digest,
            creative_version_id=self.creative_version_id,
            maximum_spend=self.maximum_spend,
            maximum_spend_micros=self.maximum_spend_micros,
            execution_status=self.planning_boundary.status,
            spec_digest=self.spec_digest,
        )


def compile_reddit_ads_campaign(brief: RedditAdsCampaignBrief) -> RedditAdsCompiledCampaignSpec:
    """Compile and revalidate a deterministic, non-executable local candidate."""
    brief = RedditAdsCampaignBrief.model_validate(brief.model_dump(mode="python", warnings=False))
    validate_destination(
        brief.creative.destination_url,
        allowed_hosts=set(brief.destination_policy.allowed_hosts),
        allowed_paths=set(brief.destination_policy.allowed_paths),
    )
    start, end = brief.start_time.astimezone(UTC), brief.end_time.astimezone(UTC)
    creative = brief.creative
    spec = RedditAdsCompiledCampaignSpec(
        account_ref=brief.account_ref,
        account_currency=brief.account_currency,
        currency_minor_unit_exponent=brief.currency_minor_unit_exponent,
        account_timezone=brief.account_timezone,
        destination_policy=brief.destination_policy,
        pixel_reference=brief.pixel_reference,
        funding_instrument_reference=brief.funding_instrument_reference,
        creative=creative,
        campaign_handle=brief.campaign_handle,
        campaign_request=RedditAdsNativeCampaignPayload(
            name=brief.campaign_name,
            goal_value=brief.lifetime_budget.minor_units * _MICROS_PER_USD_MINOR_UNIT,
            start_time=start,
            end_time=end,
            conversion_pixel_id=brief.pixel_reference.pixel_id,
            funding_instrument_id=brief.funding_instrument_reference.funding_instrument_id,
        ),
        ad_group_handle=brief.ad_group_handle,
        ad_group_parent_campaign_handle=brief.campaign_handle,
        ad_group_request=RedditAdsNativeAdGroupPayload(
            campaign_id=brief.campaign_handle,
            name=brief.ad_group_name,
            start_time=start,
            end_time=end,
            conversion_pixel_id=brief.pixel_reference.pixel_id,
            targeting=RedditAdsNativeTargeting(),
        ),
        ad_handle=brief.ad_handle,
        ad_parent_ad_group_handle=brief.ad_group_handle,
        ad_request=RedditAdsNativeAdPayload(
            ad_group_id=brief.ad_group_handle,
            name=brief.ad_name,
            post_id=creative.post_reference.post_id,
            click_url=creative.destination_url,
        ),
        creative_content_digest=creative.content_digest,
        creative_version_id=creative.association.creative_version.creative_version_id,
        asset_version_id=creative.association.asset.asset_version_id,
        asset_content_digest=creative.association.asset.content_digest,
        rendition_id=creative.association.rendition.rendition_id,
        post_content_digest=creative.post_reference.post_content_digest,
        post_reference_evidence_ref=creative.post_reference.evidence_ref,
        input_digest=digest(brief.model_dump(mode="json")),
        maximum_spend=brief.lifetime_budget,
        maximum_spend_micros=brief.lifetime_budget.minor_units * _MICROS_PER_USD_MINOR_UNIT,
        planning_boundary=RedditAdsPlanningBoundary(),
    )
    # Model construction/copy can bypass validators. Re-parse the complete graph
    # so forged nested state and serialized output cannot reach a planner.
    return RedditAdsCompiledCampaignSpec.model_validate_json(spec.model_dump_json())
