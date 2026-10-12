"""Offline compiler for one paused TikTok Ads v1.3 website-traffic hierarchy.

The compiler produces local, wire-shaped field fragments rather than provider
requests or identifiers. It neither contacts TikTok nor registers, decodes,
uploads, or verifies a video asset. Its only reviewed exposure form is a
single USD total-budget ad group beneath a campaign with CBO disabled.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.creative import CreativeVersion
from adhelm.models import Money, SafeRef, StrictModel
from adhelm.privacy import validate_destination
from adhelm.video_creative import CustomerDeclaredVideoAsset, CustomerDeclaredVideoRendition

_API_VERSION: Literal["v1.3"] = "v1.3"
_CAPABILITY_VERSION: Literal["tiktok-marketing-api-v1.3-traffic-video-contract-2026-09-04"] = (
    "tiktok-marketing-api-v1.3-traffic-video-contract-2026-09-04"
)
_FORMAT: Literal["TIKTOK_ADS_IN_FEED_VIDEO"] = "TIKTOK_ADS_IN_FEED_VIDEO"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HANDLE = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
_NATIVE_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_MAX_MINOR_UNITS = 9_007_199_254_740_991
# TikTok's current v1.3 ad-group reference documents this upper bound. A
# promotion still performs a current eligibility readback before any egress.
_MAX_DOCUMENTED_SCHEDULE_START = datetime(2028, 1, 1, tzinfo=UTC)


def _destination_ref(url: str) -> str:
    return "destination:sha256:" + digest({"target_url": url})


def _non_blank(value: str, field_name: str) -> str:
    if not value.strip() or not value.isprintable():
        raise ValueError(f"{field_name} must be non-blank printable text")
    return value


def _native_name(value: str, field_name: str) -> str:
    _non_blank(value, field_name)
    if any(unicodedata.category(character) == "So" for character in value):
        raise ValueError(f"{field_name} cannot contain an emoji or symbol character")
    return value


def _iana_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("account_timezone must be an IANA timezone") from exc
    return value


def _opaque_provider_ref(value: str, field_name: str) -> str:
    if not value.strip() or any(
        not character.isascii() or not 33 <= ord(character) <= 126 for character in value
    ):
        raise ValueError(f"{field_name} must be a printable opaque provider reference")
    return value


def _native_utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("schedule requires timezone-aware times")
    utc_value = value.astimezone(UTC)
    if utc_value.microsecond:
        raise ValueError("TikTok v1.3 schedule times require whole UTC seconds")
    return utc_value.strftime("%Y-%m-%d %H:%M:%S")


def _parse_native_utc_timestamp(value: str) -> datetime:
    if _NATIVE_UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("native schedule time must use YYYY-MM-DD HH:MM:SS UTC")
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("native schedule time is invalid") from exc


def _native_budget_from_minor_units(minor_units: int) -> Decimal:
    # String construction is exact even if an embedding application changes
    # the process-local Decimal precision or enables arithmetic traps.
    return Decimal(f"{minor_units // 100}.{minor_units % 100:02d}")


def _creative_content_digest(
    *,
    identity_id: str,
    video_id: str,
    ad_text: str,
    destination_url: str,
    creative_version_id: str,
    video_asset_version_id: str,
    declared_video_content_digest: str,
    video_rendition_id: str,
) -> str:
    return digest(
        {
            "format": "in_feed_video",
            "identity_id": identity_id,
            "identity_type": "CUSTOMIZED_USER",
            "video_id": video_id,
            "ad_text": ad_text,
            "call_to_action": "LEARN_MORE",
            "destination_url": destination_url,
            "creative_version_id": creative_version_id,
            "video_asset_version_id": video_asset_version_id,
            "declared_video_content_digest": declared_video_content_digest,
            "video_rendition_id": video_rendition_id,
        }
    )


class TikTokAdsDestinationPolicy(StrictModel):
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=100)
    allowed_paths: tuple[str, ...] = Field(min_length=1, max_length=500)

    @field_validator("allowed_hosts")
    @classmethod
    def canonical_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        def valid_host(value: str) -> bool:
            labels = value.split(".")
            return bool(
                value
                and len(value) <= 253
                and all(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
                )
            )

        if tuple(sorted(set(values))) != values or any(not valid_host(value) for value in values):
            raise ValueError("allowed_hosts must be unique sorted lowercase host names")
        return values

    @field_validator("allowed_paths")
    @classmethod
    def canonical_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values or any(
            not value.startswith("/")
            or len(value) > 500
            or "?" in value
            or "#" in value
            or not value.isprintable()
            for value in values
        ):
            raise ValueError("allowed_paths must be unique sorted printable absolute paths")
        return values


class TikTokAdsMediaReferenceEvidence(StrictModel):
    """A declared existing TikTok video reference, never upload or readiness proof."""

    account_ref: SafeRef
    video_id: str = Field(min_length=1, max_length=512)
    video_asset_version_id: str = Field(pattern=r"^video_asset:sha256:[0-9a-f]{64}$")
    declared_video_content_digest: str = Field(pattern=_DIGEST.pattern)
    evidence_ref: str = Field(pattern=r"^evidence:sha256:[0-9a-f]{64}$")

    @field_validator("video_id")
    @classmethod
    def opaque_video_id(cls, value: str) -> str:
        return _opaque_provider_ref(value, "video_id")


class TikTokAdsIdentityEvidence(StrictModel):
    account_ref: SafeRef
    identity_id: str = Field(min_length=1, max_length=512)
    evidence_ref: str = Field(pattern=r"^evidence:sha256:[0-9a-f]{64}$")

    @field_validator("identity_id")
    @classmethod
    def opaque_identity_id(cls, value: str) -> str:
        return _opaque_provider_ref(value, "identity_id")


class TikTokAdsCreativeAssociation(StrictModel):
    """One manifest-bound declared video rendition; image renditions cannot substitute."""

    creative_version: CreativeVersion
    video_asset: CustomerDeclaredVideoAsset
    video_rendition: CustomerDeclaredVideoRendition

    @model_validator(mode="after")
    def exact_declared_video_rendition(self) -> TikTokAdsCreativeAssociation:
        manifest = self.creative_version.manifest
        asset = self.video_asset
        rendition = self.video_rendition
        if (
            manifest.provider_format != _FORMAT
            or manifest.surface_kind != "MEDIA"
            or rendition.provider_format != _FORMAT
            or rendition.locale != manifest.locale
            or rendition.video_asset_version_ref != asset.video_asset_version_id
            or rendition.declared_content_digest != asset.declared_content_digest
            or rendition.declared_width_px != asset.declared_width_px
            or rendition.declared_height_px != asset.declared_height_px
            or len(manifest.rendition_refs) != 1
            or manifest.rendition_refs[0].rendition_id != rendition.rendition_id
            or manifest.rendition_refs[0].role != "HERO"
        ):
            raise ValueError(
                "creative association must bind one declared VIDEO HERO rendition to its manifest"
            )
        return self


class TikTokAdsInFeedVideoCreative(StrictModel):
    ad_text: str = Field(min_length=1, max_length=100)
    destination_url: str = Field(min_length=1, max_length=2_000)
    call_to_action: Literal["LEARN_MORE"] = "LEARN_MORE"
    association: TikTokAdsCreativeAssociation
    media_reference: TikTokAdsMediaReferenceEvidence
    identity: TikTokAdsIdentityEvidence

    @field_validator("ad_text")
    @classmethod
    def valid_ad_text(cls, value: str) -> str:
        return _non_blank(value, "ad_text")

    @field_validator("destination_url")
    @classmethod
    def valid_destination_text(cls, value: str) -> str:
        return _non_blank(value, "destination_url")

    @model_validator(mode="after")
    def exact_serving_surface(self) -> TikTokAdsInFeedVideoCreative:
        manifest = self.association.creative_version.manifest
        captions = [item for item in manifest.components if item.component_role == "CAPTION"]
        ctas = [item for item in manifest.components if item.component_role == "CTA_BUTTON"]
        asset = self.association.video_asset
        if (
            self.media_reference.video_asset_version_id != asset.video_asset_version_id
            or self.media_reference.declared_video_content_digest != asset.declared_content_digest
            or manifest.destination_ref != _destination_ref(self.destination_url)
            or len(manifest.components) != 2
            or len(captions) != 1
            or captions[0].exact_text != self.ad_text
            or captions[0].provider_slot != "ad_text"
            or len(ctas) != 1
            or ctas[0].provider_slot != "call_to_action"
            or ctas[0].normalized_cta_intent != self.call_to_action
            or ctas[0].cta_destination_ref != manifest.destination_ref
        ):
            raise ValueError(
                "creative version must exactly match fixed TikTok copy, CTA, VIDEO, and destination"
            )
        return self

    @property
    def content_digest(self) -> str:
        association = self.association
        return _creative_content_digest(
            identity_id=self.identity.identity_id,
            video_id=self.media_reference.video_id,
            ad_text=self.ad_text,
            destination_url=self.destination_url,
            creative_version_id=association.creative_version.creative_version_id,
            video_asset_version_id=association.video_asset.video_asset_version_id,
            declared_video_content_digest=association.video_asset.declared_content_digest,
            video_rendition_id=association.video_rendition.rendition_id,
        )


def _reference_binding_digest(account_ref: str, creative: TikTokAdsInFeedVideoCreative) -> str:
    association = creative.association
    return digest(
        {
            "account_ref": account_ref,
            "media_account_ref": creative.media_reference.account_ref,
            "identity_account_ref": creative.identity.account_ref,
            "identity_id": creative.identity.identity_id,
            "identity_evidence_ref": creative.identity.evidence_ref,
            "video_id": creative.media_reference.video_id,
            "media_evidence_ref": creative.media_reference.evidence_ref,
            "video_asset_version_id": association.video_asset.video_asset_version_id,
            "declared_video_content_digest": association.video_asset.declared_content_digest,
            "video_rendition_id": association.video_rendition.rendition_id,
        }
    )


class TikTokAdsCampaignBrief(StrictModel):
    """A fixed US traffic candidate with one declared in-feed video creative."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    account_currency: Literal["USD"] = "USD"
    currency_minor_unit_exponent: Literal[2] = 2
    account_timezone: str = Field(min_length=1, max_length=80)
    campaign_handle: str = Field(pattern=_HANDLE.pattern)
    adgroup_handle: str = Field(pattern=_HANDLE.pattern)
    ad_handle: str = Field(pattern=_HANDLE.pattern)
    campaign_name: str = Field(min_length=1, max_length=512)
    adgroup_name: str = Field(min_length=1, max_length=512)
    ad_name: str = Field(min_length=1, max_length=512)
    total_budget: Money
    start_time: datetime
    stop_time: datetime
    location_ids: tuple[Literal["6252001"], ...] = ("6252001",)
    destination_policy: TikTokAdsDestinationPolicy
    creative: TikTokAdsInFeedVideoCreative

    @field_validator("location_ids")
    @classmethod
    def reviewed_us_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != ("6252001",):
            raise ValueError("only the reviewed US location ID is supported")
        return value

    @model_validator(mode="after")
    def schedule_and_account_bindings(self) -> TikTokAdsCampaignBrief:
        for field_name, value in (
            ("campaign_name", self.campaign_name),
            ("adgroup_name", self.adgroup_name),
            ("ad_name", self.ad_name),
        ):
            _native_name(value, field_name)
        if (
            self.start_time.tzinfo is None
            or self.start_time.utcoffset() is None
            or self.stop_time.tzinfo is None
            or self.stop_time.utcoffset() is None
            or self.stop_time <= self.start_time
        ):
            raise ValueError("schedule requires timezone-aware increasing times")
        start_utc = self.start_time.astimezone(UTC)
        if start_utc.microsecond or self.stop_time.astimezone(UTC).microsecond:
            raise ValueError("TikTok v1.3 schedule times require whole UTC seconds")
        if start_utc > _MAX_DOCUMENTED_SCHEDULE_START:
            raise ValueError("schedule start exceeds the current documented TikTok v1.3 bound")
        if (
            type(self.total_budget.minor_units) is not int
            or self.total_budget.currency != self.account_currency
            or self.total_budget.minor_units < 1
            or self.total_budget.minor_units > _MAX_MINOR_UNITS
        ):
            raise ValueError("total_budget must be a positive Money value in the account currency")
        _iana_timezone(self.account_timezone)
        handles = (self.campaign_handle, self.adgroup_handle, self.ad_handle)
        if len(set(handles)) != len(handles):
            raise ValueError("local hierarchy handles must be globally unique")
        if (
            self.creative.media_reference.account_ref != self.account_ref
            or self.creative.identity.account_ref != self.account_ref
        ):
            raise ValueError("media and identity evidence must be bound to the compiled account")
        return self


class TikTokAdsNativeCampaignPayload(StrictModel):
    """A local campaign field fragment, not an executable create request."""

    campaign_name: str = Field(min_length=1, max_length=512)
    objective_type: Literal["TRAFFIC"] = "TRAFFIC"
    budget_optimize_on: Literal[False] = False
    operation_status: Literal["DISABLE"] = "DISABLE"

    @field_validator("campaign_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return _native_name(value, "campaign_name")


class TikTokAdsNativeTargeting(StrictModel):
    location_ids: tuple[Literal["6252001"], ...] = Field(min_length=1, max_length=1)
    gender: Literal["GENDER_UNLIMITED"] = "GENDER_UNLIMITED"
    age_groups: tuple[
        Literal["AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"], ...
    ] = ("AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100")

    @model_validator(mode="after")
    def complete_fixed_age_range(self) -> TikTokAdsNativeTargeting:
        if self.location_ids != ("6252001",) or self.age_groups != (
            "AGE_18_24",
            "AGE_25_34",
            "AGE_35_44",
            "AGE_45_54",
            "AGE_55_100",
        ):
            raise ValueError("targeting must retain the reviewed US and complete adult age range")
        return self


class TikTokAdsNativeAdGroupPayload(StrictModel):
    """A local ad-group field fragment using TikTok's major-unit total budget."""

    adgroup_name: str = Field(min_length=1, max_length=512)
    promotion_type: Literal["WEBSITE"] = "WEBSITE"
    placement_type: Literal["PLACEMENT_TYPE_NORMAL"] = "PLACEMENT_TYPE_NORMAL"
    placements: tuple[Literal["PLACEMENT_TIKTOK"], ...] = ("PLACEMENT_TIKTOK",)
    targeting: TikTokAdsNativeTargeting
    budget_mode: Literal["BUDGET_MODE_TOTAL"] = "BUDGET_MODE_TOTAL"
    budget: Decimal
    schedule_type: Literal["SCHEDULE_START_END"] = "SCHEDULE_START_END"
    schedule_start_time: str = Field(pattern=_NATIVE_UTC_TIMESTAMP.pattern)
    schedule_end_time: str = Field(pattern=_NATIVE_UTC_TIMESTAMP.pattern)
    optimization_goal: Literal["CLICK"] = "CLICK"
    billing_event: Literal["CPC"] = "CPC"
    bid_type: Literal["BID_TYPE_NO_BID"] = "BID_TYPE_NO_BID"
    pacing: Literal["PACING_MODE_SMOOTH"] = "PACING_MODE_SMOOTH"
    operation_status: Literal["DISABLE"] = "DISABLE"

    @field_validator("adgroup_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return _native_name(value, "adgroup_name")

    @field_validator("budget")
    @classmethod
    def exact_positive_usd_major_units(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("native total budget must be finite and positive")
        parts = value.as_tuple()
        if parts.exponent != -2 or len(parts.digits) > 16:
            raise ValueError("native total budget must use exactly two USD decimal places")
        minor_units = int("".join(str(digit) for digit in parts.digits))
        if minor_units > _MAX_MINOR_UNITS:
            raise ValueError("native total budget must use exactly two USD decimal places")
        return value

    @field_validator("schedule_start_time", "schedule_end_time")
    @classmethod
    def valid_native_schedule_time(cls, value: str) -> str:
        _parse_native_utc_timestamp(value)
        return value

    @model_validator(mode="after")
    def valid_schedule_and_placement(self) -> TikTokAdsNativeAdGroupPayload:
        if self.placements != ("PLACEMENT_TIKTOK",):
            raise ValueError("the local candidate supports only TikTok placement")
        if _parse_native_utc_timestamp(self.schedule_end_time) <= _parse_native_utc_timestamp(
            self.schedule_start_time
        ):
            raise ValueError("native schedule requires increasing UTC times")
        return self


class TikTokAdsNativeAdPayload(StrictModel):
    """A local creative field fragment, not a complete native ad request body."""

    ad_name: str = Field(min_length=1, max_length=512)
    identity_type: Literal["CUSTOMIZED_USER"] = "CUSTOMIZED_USER"
    identity_id: str = Field(min_length=1, max_length=512)
    ad_format: Literal["SINGLE_VIDEO"] = "SINGLE_VIDEO"
    video_id: str = Field(min_length=1, max_length=512)
    ad_text: str = Field(min_length=1, max_length=100)
    call_to_action: Literal["LEARN_MORE"] = "LEARN_MORE"
    landing_page_url: str = Field(min_length=1, max_length=2_000)
    operation_status: Literal["DISABLE"] = "DISABLE"

    @field_validator("ad_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return _native_name(value, "ad_name")

    @field_validator("ad_text", "landing_page_url")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return _non_blank(value, "native creative text")

    @field_validator("identity_id", "video_id")
    @classmethod
    def valid_ref(cls, value: str) -> str:
        return _opaque_provider_ref(value, "native provider reference")


class TikTokAdsPlanningBoundary(StrictModel):
    status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    fixture_only_status: Literal["FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = (
        "FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    )
    requires_current_account_identity: Literal[True] = True
    requires_account_currency_readback: Literal[True] = True
    requires_media_and_identity_readback: Literal[True] = True
    requires_verified_canonical_video_asset_history: Literal[True] = True
    requires_schedule_eligibility_readback: Literal[True] = True
    requires_objective_targeting_and_placement_eligibility_readback: Literal[True] = True
    requires_creative_transformation_readback: Literal[True] = True
    requires_creative_ledger_check: Literal[True] = True
    requires_destination_and_tracking_validation: Literal[True] = True
    requires_native_conversion_check: Literal[True] = True
    requires_provider_id_binding_before_request: Literal[True] = True
    requires_remote_drift_check_and_authorization: Literal[True] = True


class TikTokAdsRedactedReview(StrictModel):
    account_ref: SafeRef
    account_currency: Literal["USD"]
    account_timezone: str
    account_identity_status: Literal["CUSTOMER_DECLARED_REQUIRES_READBACK"] = (
        "CUSTOMER_DECLARED_REQUIRES_READBACK"
    )
    campaign_handle: str = Field(pattern=_HANDLE.pattern)
    adgroup_handle: str = Field(pattern=_HANDLE.pattern)
    ad_handle: str = Field(pattern=_HANDLE.pattern)
    initial_state: Literal["DISABLE"] = "DISABLE"
    objective: Literal["TRAFFIC"] = "TRAFFIC"
    requested_placement: Literal["PLACEMENT_TIKTOK"] = "PLACEMENT_TIKTOK"
    placement_eligibility_status: Literal["REQUIRES_API_READBACK"] = "REQUIRES_API_READBACK"
    video_asset_status: Literal["DECLARED_UNVERIFIED_NO_LOCAL_DECODE"] = (
        "DECLARED_UNVERIFIED_NO_LOCAL_DECODE"
    )
    video_execution_eligibility: Literal["REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET"] = (
        "REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET"
    )
    creative_transformation_status: Literal["REQUIRES_API_READBACK"] = "REQUIRES_API_READBACK"
    creative_content_digest: str = Field(pattern=_DIGEST.pattern)
    creative_version_id: str = Field(pattern=r"^creative:sha256:[0-9a-f]{64}$")
    maximum_spend: Money
    execution_status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"]
    spec_digest: str = Field(pattern=_DIGEST.pattern)


class TikTokAdsCompiledCampaignSpec(StrictModel):
    provider: Literal["tiktok_ads"] = "tiktok_ads"
    provider_api_version: Literal["v1.3"] = _API_VERSION
    capability_version: Literal["tiktok-marketing-api-v1.3-traffic-video-contract-2026-09-04"] = (
        _CAPABILITY_VERSION
    )
    source_brief: TikTokAdsCampaignBrief
    account_ref: SafeRef
    account_currency: Literal["USD"]
    currency_minor_unit_exponent: Literal[2]
    account_timezone: str
    campaign_handle: str = Field(pattern=_HANDLE.pattern)
    campaign_request: TikTokAdsNativeCampaignPayload
    adgroup_handle: str = Field(pattern=_HANDLE.pattern)
    adgroup_parent_campaign_handle: str = Field(pattern=_HANDLE.pattern)
    adgroup_request: TikTokAdsNativeAdGroupPayload
    ad_handle: str = Field(pattern=_HANDLE.pattern)
    ad_parent_adgroup_handle: str = Field(pattern=_HANDLE.pattern)
    ad_request: TikTokAdsNativeAdPayload
    destination_policy: TikTokAdsDestinationPolicy
    destination_ref: str = Field(pattern=r"^destination:sha256:[0-9a-f]{64}$")
    creative_content_digest: str = Field(pattern=_DIGEST.pattern)
    creative_version_id: str = Field(pattern=r"^creative:sha256:[0-9a-f]{64}$")
    video_asset_version_id: str = Field(pattern=r"^video_asset:sha256:[0-9a-f]{64}$")
    declared_video_content_digest: str = Field(pattern=_DIGEST.pattern)
    video_rendition_id: str = Field(pattern=r"^video_rendition:sha256:[0-9a-f]{64}$")
    media_reference_evidence_ref: str = Field(pattern=r"^evidence:sha256:[0-9a-f]{64}$")
    identity_evidence_ref: str = Field(pattern=r"^evidence:sha256:[0-9a-f]{64}$")
    reference_binding_digest: str = Field(pattern=_DIGEST.pattern)
    input_digest: str = Field(pattern=_DIGEST.pattern)
    maximum_spend: Money
    planning_boundary: TikTokAdsPlanningBoundary

    @field_validator("account_timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        return _iana_timezone(value)

    @model_validator(mode="after")
    def hierarchy_content_and_spend_are_exact(self) -> TikTokAdsCompiledCampaignSpec:
        brief = self.source_brief
        creative = brief.creative
        association = creative.association
        if (
            type(brief.total_budget.minor_units) is not int
            or type(self.maximum_spend.minor_units) is not int
            or brief.total_budget.currency != self.account_currency
            or self.maximum_spend.currency != self.account_currency
            or not 1 <= brief.total_budget.minor_units <= _MAX_MINOR_UNITS
            or not 1 <= self.maximum_spend.minor_units <= _MAX_MINOR_UNITS
        ):
            raise ValueError(
                "compiled TikTok total budget must be bounded Money in the account currency"
            )
        validate_destination(
            creative.destination_url,
            allowed_hosts=set(self.destination_policy.allowed_hosts),
            allowed_paths=set(self.destination_policy.allowed_paths),
        )
        expected_start = _native_utc_timestamp(brief.start_time)
        expected_end = _native_utc_timestamp(brief.stop_time)
        expected_budget = _native_budget_from_minor_units(brief.total_budget.minor_units)
        expected_content_digest = creative.content_digest
        if (
            self.account_ref != brief.account_ref
            or self.account_currency != brief.account_currency
            or self.currency_minor_unit_exponent != brief.currency_minor_unit_exponent
            or self.account_timezone != brief.account_timezone
            or self.destination_policy != brief.destination_policy
            or self.destination_ref != _destination_ref(creative.destination_url)
            or self.campaign_handle != brief.campaign_handle
            or self.adgroup_handle != brief.adgroup_handle
            or self.ad_handle != brief.ad_handle
            or self.adgroup_parent_campaign_handle != self.campaign_handle
            or self.ad_parent_adgroup_handle != self.adgroup_handle
            or self.campaign_request.campaign_name != brief.campaign_name
            or self.adgroup_request.adgroup_name != brief.adgroup_name
            or self.adgroup_request.targeting.location_ids != brief.location_ids
            or self.adgroup_request.budget != expected_budget
            or self.adgroup_request.schedule_start_time != expected_start
            or self.adgroup_request.schedule_end_time != expected_end
            or self.ad_request.ad_name != brief.ad_name
            or self.ad_request.identity_id != creative.identity.identity_id
            or self.ad_request.video_id != creative.media_reference.video_id
            or self.ad_request.ad_text != creative.ad_text
            or self.ad_request.landing_page_url != creative.destination_url
            or self.maximum_spend != brief.total_budget
            or self.creative_content_digest != expected_content_digest
            or self.creative_version_id != association.creative_version.creative_version_id
            or self.video_asset_version_id != association.video_asset.video_asset_version_id
            or self.declared_video_content_digest != association.video_asset.declared_content_digest
            or self.video_rendition_id != association.video_rendition.rendition_id
            or self.media_reference_evidence_ref != creative.media_reference.evidence_ref
            or self.identity_evidence_ref != creative.identity.evidence_ref
            or self.reference_binding_digest
            != _reference_binding_digest(self.account_ref, creative)
            or self.input_digest != digest(brief.model_dump(mode="json"))
        ):
            raise ValueError(
                "compiled TikTok hierarchy, creative binding, destination, or exposure limit "
                "is invalid"
            )
        return self

    @property
    def spec_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def redacted_review(self) -> TikTokAdsRedactedReview:
        video_asset = self.source_brief.creative.association.video_asset
        return TikTokAdsRedactedReview(
            account_ref=self.account_ref,
            account_currency=self.account_currency,
            account_timezone=self.account_timezone,
            campaign_handle=self.campaign_handle,
            adgroup_handle=self.adgroup_handle,
            ad_handle=self.ad_handle,
            video_asset_status=video_asset.local_media_status,
            video_execution_eligibility=video_asset.execution_eligibility,
            creative_content_digest=self.creative_content_digest,
            creative_version_id=self.creative_version_id,
            maximum_spend=self.maximum_spend,
            execution_status=self.planning_boundary.status,
            spec_digest=self.spec_digest,
        )


def compile_tiktok_ads_campaign(brief: TikTokAdsCampaignBrief) -> TikTokAdsCompiledCampaignSpec:
    """Compile a local non-executable candidate with no network or mutation path."""
    brief = TikTokAdsCampaignBrief.model_validate(brief.model_dump(mode="python", warnings=False))
    validate_destination(
        brief.creative.destination_url,
        allowed_hosts=set(brief.destination_policy.allowed_hosts),
        allowed_paths=set(brief.destination_policy.allowed_paths),
    )
    creative = brief.creative
    association = creative.association
    return TikTokAdsCompiledCampaignSpec(
        source_brief=brief,
        account_ref=brief.account_ref,
        account_currency=brief.account_currency,
        currency_minor_unit_exponent=brief.currency_minor_unit_exponent,
        account_timezone=brief.account_timezone,
        campaign_handle=brief.campaign_handle,
        campaign_request=TikTokAdsNativeCampaignPayload(campaign_name=brief.campaign_name),
        adgroup_handle=brief.adgroup_handle,
        adgroup_parent_campaign_handle=brief.campaign_handle,
        adgroup_request=TikTokAdsNativeAdGroupPayload(
            adgroup_name=brief.adgroup_name,
            targeting=TikTokAdsNativeTargeting(location_ids=brief.location_ids),
            budget=_native_budget_from_minor_units(brief.total_budget.minor_units),
            schedule_start_time=_native_utc_timestamp(brief.start_time),
            schedule_end_time=_native_utc_timestamp(brief.stop_time),
        ),
        ad_handle=brief.ad_handle,
        ad_parent_adgroup_handle=brief.adgroup_handle,
        ad_request=TikTokAdsNativeAdPayload(
            ad_name=brief.ad_name,
            identity_id=creative.identity.identity_id,
            video_id=creative.media_reference.video_id,
            ad_text=creative.ad_text,
            landing_page_url=creative.destination_url,
        ),
        destination_policy=brief.destination_policy,
        destination_ref=_destination_ref(creative.destination_url),
        creative_content_digest=creative.content_digest,
        creative_version_id=association.creative_version.creative_version_id,
        video_asset_version_id=association.video_asset.video_asset_version_id,
        declared_video_content_digest=association.video_asset.declared_content_digest,
        video_rendition_id=association.video_rendition.rendition_id,
        media_reference_evidence_ref=creative.media_reference.evidence_ref,
        identity_evidence_ref=creative.identity.evidence_ref,
        reference_binding_digest=_reference_binding_digest(brief.account_ref, creative),
        input_digest=digest(brief.model_dump(mode="json")),
        maximum_spend=brief.total_budget,
        planning_boundary=TikTokAdsPlanningBoundary(),
    )
