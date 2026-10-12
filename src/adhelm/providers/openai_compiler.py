"""Offline compiler for the narrow, documented OpenAI Ads chat-card hierarchy.

This module has no HTTP client and cannot apply a provider mutation.  It turns a
closed local brief into a content-addressed *paused* Campaign -> Ad Group -> Ad
specification.  Parent references are local logical handles, not invented OpenAI
resource IDs; a future guarded executor must bind them to verified create results.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.creative import CreativeAssetVersion, CreativeRendition, CreativeVersion
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Currency, Money, SafeRef, StrictModel
from adhelm.privacy import validate_destination

_API_VERSION: Literal["v1"] = "v1"
_CAPABILITY_VERSION: Literal["openai-ads-write-contract-2026-09-04"] = (
    "openai-ads-write-contract-2026-09-04"
)
_MIN_UNIX_TIME = 946_684_800
_MAX_UNIX_TIME = 4_102_444_800
_MIN_NATIVE_MICROS = 1_000_000
_MAX_NATIVE_MICROS = 9_007_199_254_740_991
_USD_MINOR_TO_MICROS = 10_000
_MIN_USD_MINOR_UNITS = _MIN_NATIVE_MICROS // _USD_MINOR_TO_MICROS
_MAX_USD_MINOR_UNITS = _MAX_NATIVE_MICROS // _USD_MINOR_TO_MICROS
_HANDLE = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OPENAI_CHAT_CARD_FORMAT: Literal["OPENAI_ADS_CHAT_CARD"] = "OPENAI_ADS_CHAT_CARD"

# The only reviewed OpenAI Ads reporting currency in this build is USD.  The
# corresponding read adapter also fails closed for other account currencies.
# Do not accept a caller-supplied exponent as an authority-bearing conversion.
_CURRENCY_MINOR_EXPONENTS = {"USD": 2}


def _non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must include a non-space character")
    return value


def _destination_ref(url: str) -> str:
    """Bind the exact native destination to the canonical creative-version domain."""
    return "destination:sha256:" + digest({"target_url": url})


class OpenAIAdsDestinationPolicy(StrictModel):
    """Customer-declared routes that a chat-card may send traffic to."""

    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=100)
    allowed_paths: tuple[str, ...] = Field(min_length=1, max_length=500)

    @field_validator("allowed_hosts")
    @classmethod
    def canonical_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values or any(
            not re.fullmatch(r"[a-z0-9.-]{1,253}", value) for value in values
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


class OpenAIAdsFileReferenceEvidence(StrictModel):
    """Customer-supplied local evidence for an opaque provider file reference.

    This is deliberately not a claim that the provider has verified the file.
    A later planner must resolve ``evidence_ref`` in the local ledger and
    perform an account-bound file readback before any create request is sent.
    """

    account_ref: SafeRef
    file_id: str = Field(min_length=1, max_length=512)
    asset_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ref: str = Field(pattern=r"^evidence:sha256:[0-9a-f]{64}$")

    @field_validator("file_id")
    @classmethod
    def opaque_file_reference(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("file_id must include a non-space character")
        return value


class OpenAIAdsCreativeAssociation(StrictModel):
    """Local creative records that must agree with one chat-card serving input."""

    creative_version: CreativeVersion
    asset: CreativeAssetVersion
    rendition: CreativeRendition

    @model_validator(mode="after")
    def exact_local_creative_chain(self) -> OpenAIAdsCreativeAssociation:
        asset = self.asset
        rendition = self.rendition
        manifest = self.creative_version.manifest
        expected_asset_id = "asset:sha256:" + digest(
            {
                "content_digest": asset.content_digest,
                "mime_type": asset.mime_type,
                "width_px": asset.width_px,
                "height_px": asset.height_px,
            }
        )
        if (
            _DIGEST.fullmatch(asset.content_digest) is None
            or asset.asset_version_id != expected_asset_id
        ):
            raise ValueError(
                "creative asset identity must match its content and intrinsic metadata"
            )
        if (
            manifest.provider_format != _OPENAI_CHAT_CARD_FORMAT
            or manifest.surface_kind != "MEDIA"
            or rendition.provider_format != _OPENAI_CHAT_CARD_FORMAT
            or rendition.locale != manifest.locale
        ):
            raise ValueError("creative association must use the OpenAI Ads chat-card format")
        if (
            rendition.source_relation_status != "PROVEN_SOURCE_ASSET"
            or rendition.source_asset_version_ref != asset.asset_version_id
            or rendition.content_digest != asset.content_digest
        ):
            raise ValueError("chat-card rendition must prove its local source asset")
        expected_rendition_id = "rendition:sha256:" + digest(
            {
                "asset_version_id": asset.asset_version_id,
                "provider_format": rendition.provider_format,
                "locale": rendition.locale,
                "transformation_digest": rendition.transformation_digest,
            }
        )
        if rendition.rendition_id != expected_rendition_id:
            raise ValueError("chat-card rendition identity does not match its local source asset")
        if rendition.rendition_id not in {item.rendition_id for item in manifest.rendition_refs}:
            raise ValueError("creative version does not include the chat-card rendition")
        return self


class OpenAIAdsChatCardCreative(StrictModel):
    """The complete documented serving input for one supported chat-card ad."""

    title: str = Field(min_length=3, max_length=50)
    body: str = Field(min_length=1, max_length=100)
    destination_url: str = Field(min_length=1, max_length=2_000)
    association: OpenAIAdsCreativeAssociation
    file_reference: OpenAIAdsFileReferenceEvidence

    @field_validator("title", "body")
    @classmethod
    def visible_text_is_not_blank(cls, value: str) -> str:
        return _non_blank(value, "creative text")

    @model_validator(mode="after")
    def exact_serving_components_and_asset(self) -> OpenAIAdsChatCardCreative:
        manifest = self.association.creative_version.manifest
        components = manifest.components
        title_matches = [
            item
            for item in components
            if item.component_role in {"HEADLINE", "LONG_HEADLINE"}
            and item.exact_text == self.title
        ]
        body_matches = [
            item
            for item in components
            if item.component_role in {"PRIMARY_TEXT", "DESCRIPTION"}
            and item.exact_text == self.body
        ]
        if len(title_matches) != 1 or len(body_matches) != 1:
            raise ValueError(
                "creative version must contain exactly the chat-card title and body components"
            )
        if self.file_reference.asset_content_digest != self.association.asset.content_digest:
            raise ValueError("file-reference evidence does not match the local creative asset")
        if manifest.destination_ref != _destination_ref(self.destination_url):
            raise ValueError("creative version destination does not match the chat-card target URL")
        return self

    @property
    def content_digest(self) -> str:
        """Covers exact copy, destination, and the local serving-asset chain."""
        return digest(
            {
                "type": "chat_card",
                "title": self.title,
                "body": self.body,
                "target_url": self.destination_url,
                "file_id": self.file_reference.file_id,
                "creative_version_id": self.association.creative_version.creative_version_id,
                "rendition_id": self.association.rendition.rendition_id,
                "asset_content_digest": self.association.asset.content_digest,
            }
        )


class OpenAIAdsAdBrief(StrictModel):
    ad_handle: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=3, max_length=1_000)
    creative: OpenAIAdsChatCardCreative

    @field_validator("ad_handle")
    @classmethod
    def local_handle(cls, value: str) -> str:
        if _HANDLE.fullmatch(value) is None:
            raise ValueError("ad_handle must be a local lowercase logical handle")
        return value

    @field_validator("name")
    @classmethod
    def non_blank_name(cls, value: str) -> str:
        return _non_blank(value, "ad name")


class OpenAIAdsAdGroupBrief(StrictModel):
    ad_group_handle: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=3, max_length=1_000)
    max_bid_micros: int = Field(ge=1, le=9_007_199_254_740_991)
    ads: tuple[OpenAIAdsAdBrief, ...] = Field(min_length=1, max_length=100)

    @field_validator("ad_group_handle")
    @classmethod
    def local_handle(cls, value: str) -> str:
        if _HANDLE.fullmatch(value) is None:
            raise ValueError("ad_group_handle must be a local lowercase logical handle")
        return value

    @field_validator("name")
    @classmethod
    def non_blank_name(cls, value: str) -> str:
        return _non_blank(value, "ad group name")

    @model_validator(mode="after")
    def unique_ad_handles(self) -> OpenAIAdsAdGroupBrief:
        handles = tuple(ad.ad_handle for ad in self.ads)
        if len(set(handles)) != len(handles):
            raise ValueError("ad handles must be unique within an ad group")
        return self


class OpenAIAdsCampaignBrief(StrictModel):
    """Narrow, complete, non-serving OpenAI Ads campaign input.

    Conversion bidding, custom audiences, product feeds, context hints, and bid
    multipliers are deliberately absent.  They require additional data and/or
    capability verification and therefore cannot silently enter a native plan.
    """

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    account_currency: Currency
    currency_minor_unit_exponent: int = Field(ge=0, le=6)
    account_timezone: str = Field(min_length=1, max_length=80)
    campaign_handle: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=3, max_length=1_000)
    start_time: int = Field(ge=_MIN_UNIX_TIME, le=_MAX_UNIX_TIME)
    end_time: int = Field(ge=_MIN_UNIX_TIME, le=_MAX_UNIX_TIME)
    lifetime_budget: Money
    bidding_type: Literal["impressions", "clicks", "conversions"] = "impressions"
    location_ids: tuple[str, ...] = Field(min_length=1, max_length=500)
    destination_policy: OpenAIAdsDestinationPolicy
    ad_groups: tuple[OpenAIAdsAdGroupBrief, ...] = Field(min_length=1, max_length=100)

    @field_validator("campaign_handle")
    @classmethod
    def local_handle(cls, value: str) -> str:
        if _HANDLE.fullmatch(value) is None:
            raise ValueError("campaign_handle must be a local lowercase logical handle")
        return value

    @field_validator("name")
    @classmethod
    def non_blank_name(cls, value: str) -> str:
        return _non_blank(value, "campaign name")

    @field_validator("location_ids")
    @classmethod
    def canonical_location_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values or any(
            re.fullmatch(r"[0-9]{1,20}", value) is None for value in values
        ):
            raise ValueError("location IDs must be unique, sorted documented numeric IDs")
        return values

    @model_validator(mode="after")
    def complete_and_ordered_hierarchy(self) -> OpenAIAdsCampaignBrief:
        expected_exponent = _CURRENCY_MINOR_EXPONENTS.get(self.account_currency)
        if expected_exponent is None:
            raise ValueError(
                "OpenAI Ads compiler has no reviewed minor-unit exponent for this currency"
            )
        if self.currency_minor_unit_exponent != expected_exponent:
            raise ValueError(
                "currency minor-unit exponent does not match the reviewed account currency"
            )
        if type(self.lifetime_budget.minor_units) is not int:
            raise ValueError("lifetime budget must use integer minor units")
        native_micros = self.lifetime_budget.minor_units * 10 ** (6 - expected_exponent)
        if (
            self.lifetime_budget.currency != self.account_currency
            or not _MIN_NATIVE_MICROS <= native_micros <= _MAX_NATIVE_MICROS
        ):
            raise ValueError("lifetime budget must match the account currency and native limits")
        if self.end_time <= self.start_time:
            raise ValueError("campaign end_time must follow start_time")
        handles = tuple(group.ad_group_handle for group in self.ad_groups)
        if len(set(handles)) != len(handles):
            raise ValueError("ad group handles must be unique")
        if any(
            ad.creative.file_reference.account_ref != self.account_ref
            for group in self.ad_groups
            for ad in group.ads
        ):
            raise ValueError("file-reference evidence must be bound to the compiled account")
        return self


class OpenAIAdsNativeBudget(StrictModel):
    lifetime_spend_limit_micros: int = Field(ge=_MIN_NATIVE_MICROS, le=_MAX_NATIVE_MICROS)


class OpenAIAdsNativeLocation(StrictModel):
    id: str = Field(pattern=r"^[0-9]{1,20}$")


class OpenAIAdsNativeLocations(StrictModel):
    include: tuple[OpenAIAdsNativeLocation, ...] = Field(min_length=1)


class OpenAIAdsNativeTargeting(StrictModel):
    locations: OpenAIAdsNativeLocations


class OpenAIAdsNativeCampaignPayload(StrictModel):
    name: str = Field(min_length=3, max_length=1_000)
    start_time: int = Field(ge=_MIN_UNIX_TIME, le=_MAX_UNIX_TIME)
    end_time: int = Field(ge=_MIN_UNIX_TIME, le=_MAX_UNIX_TIME)
    status: Literal["paused"] = "paused"
    budget: OpenAIAdsNativeBudget
    bidding_type: Literal["impressions", "clicks"]
    targeting: OpenAIAdsNativeTargeting


class OpenAIAdsNativeBiddingConfig(StrictModel):
    billing_event_type: Literal["impression", "click"]
    max_bid_micros: int = Field(ge=1, le=9_007_199_254_740_991)


class OpenAIAdsNativeAdGroupPayload(StrictModel):
    name: str = Field(min_length=3, max_length=1_000)
    status: Literal["paused"] = "paused"
    bidding_config: OpenAIAdsNativeBiddingConfig


class OpenAIAdsNativeChatCard(StrictModel):
    type: Literal["chat_card"] = "chat_card"
    title: str = Field(min_length=3, max_length=50)
    body: str = Field(min_length=1, max_length=100)
    target_url: str = Field(min_length=1, max_length=2_000)
    file_id: str = Field(min_length=1, max_length=512)


class OpenAIAdsNativeAdPayload(StrictModel):
    name: str = Field(min_length=3, max_length=1_000)
    status: Literal["paused"] = "paused"
    creative: OpenAIAdsNativeChatCard


class OpenAIAdsCompiledAd(StrictModel):
    """An ad request plus its local parent binding, never a provider ad ID."""

    ad_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    parent_ad_group_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    request: OpenAIAdsNativeAdPayload
    creative_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    creative_family_id: str = Field(min_length=1, max_length=200)
    creative_version_id: str = Field(pattern=r"^creative:sha256:[0-9a-f]{64}$")
    rendition_id: str = Field(min_length=1, max_length=200)
    asset_version_id: str = Field(min_length=1, max_length=200)
    asset_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_reference_evidence_ref: str = Field(pattern=r"^evidence:sha256:[0-9a-f]{64}$")


class OpenAIAdsCompiledAdGroup(StrictModel):
    """An ad-group request plus its local campaign binding."""

    ad_group_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    parent_campaign_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    request: OpenAIAdsNativeAdGroupPayload
    ads: tuple[OpenAIAdsCompiledAd, ...] = Field(min_length=1)


class OpenAIAdsActivationBoundary(StrictModel):
    """Facts a distinct future activation plan must bind and authorize."""

    initial_state: Literal["PAUSED"] = "PAUSED"
    activation_operation: Literal["ACTIVATE"] = "ACTIVATE"
    activation_compiled: Literal[False] = False
    maximum_spend_micros: int = Field(ge=_MIN_NATIVE_MICROS, le=_MAX_NATIVE_MICROS)
    maximum_spend: Money
    requires_new_digest_and_authorization: Literal[True] = True


class OpenAIAdsPlanningBoundary(StrictModel):
    """Facts that prevent this offline candidate from becoming a write plan alone."""

    status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    requires_verified_account_identity: Literal[True] = True
    requires_creative_ledger_check: Literal[True] = True
    requires_account_bound_file_readback: Literal[True] = True


class OpenAIAdsRedactedReview(StrictModel):
    """Default review output: no exact marketing copy, file ID, or destination URL."""

    account_ref: SafeRef
    campaign_handle: str
    initial_state: Literal["PAUSED"] = "PAUSED"
    location_count: int = Field(ge=1)
    ad_group_count: int = Field(ge=1)
    ad_count: int = Field(ge=1)
    creative_content_digests: tuple[str, ...]
    creative_version_ids: tuple[str, ...]
    maximum_spend_micros: int = Field(ge=_MIN_NATIVE_MICROS, le=_MAX_NATIVE_MICROS)
    maximum_spend: Money
    activation_required: Literal[True] = True
    execution_status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"]
    spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class OpenAIAdsCompiledCampaignSpec(StrictModel):
    """Immutable local compilation result; it contains no executable transport."""

    schema_version: Literal[1] = 1
    provider: Literal["openai_ads"] = "openai_ads"
    provider_api_version: Literal["v1"] = _API_VERSION
    capability_version: Literal["openai-ads-write-contract-2026-09-04"] = _CAPABILITY_VERSION
    account_ref: SafeRef
    account_currency: Literal["USD"]
    currency_minor_unit_exponent: Literal[2]
    account_timezone: str
    campaign_handle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    campaign_request: OpenAIAdsNativeCampaignPayload
    ad_groups: tuple[OpenAIAdsCompiledAdGroup, ...] = Field(min_length=1)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    activation_boundary: OpenAIAdsActivationBoundary
    planning_boundary: OpenAIAdsPlanningBoundary
    lossy_mappings: tuple[str, ...] = ()
    unsupported_mappings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def exact_spend_boundary(self) -> OpenAIAdsCompiledCampaignSpec:
        maximum_spend = self.activation_boundary.maximum_spend
        boundary_micros = self.activation_boundary.maximum_spend_micros
        native_micros = self.campaign_request.budget.lifetime_spend_limit_micros
        if (
            type(maximum_spend.minor_units) is not int
            or type(boundary_micros) is not int
            or type(native_micros) is not int
            or maximum_spend.currency != self.account_currency
            or not _MIN_USD_MINOR_UNITS <= maximum_spend.minor_units <= _MAX_USD_MINOR_UNITS
            or boundary_micros != maximum_spend.minor_units * _USD_MINOR_TO_MICROS
            or native_micros != boundary_micros
        ):
            raise ValueError(
                "compiled OpenAI lifetime budget must exactly bind the typed maximum spend "
                "to the native micros cap"
            )
        return self

    @property
    def spec_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    @property
    def redacted_review(self) -> OpenAIAdsRedactedReview:
        ads = tuple(ad for group in self.ad_groups for ad in group.ads)
        return OpenAIAdsRedactedReview(
            account_ref=self.account_ref,
            campaign_handle=self.campaign_handle,
            location_count=len(self.campaign_request.targeting.locations.include),
            ad_group_count=len(self.ad_groups),
            ad_count=len(ads),
            creative_content_digests=tuple(ad.creative_content_digest for ad in ads),
            creative_version_ids=tuple(ad.creative_version_id for ad in ads),
            maximum_spend_micros=self.activation_boundary.maximum_spend_micros,
            maximum_spend=self.activation_boundary.maximum_spend,
            execution_status=self.planning_boundary.status,
            spec_digest=self.spec_digest,
        )


def compile_openai_ads_campaign(brief: OpenAIAdsCampaignBrief) -> OpenAIAdsCompiledCampaignSpec:
    """Compile the documented paused chat-card hierarchy without any remote effect."""
    # Pydantic's model_copy/model_construct APIs can bypass validators.  Treat
    # this public compiler boundary as untrusted even when callers annotate it
    # with the typed brief.
    brief = OpenAIAdsCampaignBrief.model_validate(brief.model_dump(mode="python", warnings=False))
    if brief.bidding_type == "conversions":
        raise AdhelmError(
            "UNSUPPORTED_CONVERSION_OPTIMIZATION",
            "Conversion bidding needs a separately verified active standard conversion setting.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action=(
                "Use impressions or clicks until native conversion planning is implemented."
            ),
        )

    for group in brief.ad_groups:
        for ad in group.ads:
            validate_destination(
                ad.creative.destination_url,
                allowed_hosts=set(brief.destination_policy.allowed_hosts),
                allowed_paths=set(brief.destination_policy.allowed_paths),
            )

    billing_event_type: Literal["impression", "click"] = (
        "impression" if brief.bidding_type == "impressions" else "click"
    )
    maximum_spend = brief.lifetime_budget
    lifetime_micros = maximum_spend.minor_units * _USD_MINOR_TO_MICROS
    compiled_groups: list[OpenAIAdsCompiledAdGroup] = []
    for group in brief.ad_groups:
        compiled_ads = tuple(
            OpenAIAdsCompiledAd(
                ad_handle=ad.ad_handle,
                parent_ad_group_handle=group.ad_group_handle,
                request=OpenAIAdsNativeAdPayload(
                    name=ad.name,
                    creative=OpenAIAdsNativeChatCard(
                        title=ad.creative.title,
                        body=ad.creative.body,
                        target_url=ad.creative.destination_url,
                        file_id=ad.creative.file_reference.file_id,
                    ),
                ),
                creative_content_digest=ad.creative.content_digest,
                creative_family_id=ad.creative.association.creative_version.creative_family_id,
                creative_version_id=ad.creative.association.creative_version.creative_version_id,
                rendition_id=ad.creative.association.rendition.rendition_id,
                asset_version_id=ad.creative.association.asset.asset_version_id,
                asset_content_digest=ad.creative.association.asset.content_digest,
                file_reference_evidence_ref=ad.creative.file_reference.evidence_ref,
            )
            for ad in group.ads
        )
        compiled_groups.append(
            OpenAIAdsCompiledAdGroup(
                ad_group_handle=group.ad_group_handle,
                parent_campaign_handle=brief.campaign_handle,
                request=OpenAIAdsNativeAdGroupPayload(
                    name=group.name,
                    bidding_config=OpenAIAdsNativeBiddingConfig(
                        billing_event_type=billing_event_type,
                        max_bid_micros=group.max_bid_micros,
                    ),
                ),
                ads=compiled_ads,
            )
        )

    return OpenAIAdsCompiledCampaignSpec(
        account_ref=brief.account_ref,
        # Brief revalidation proves the only reviewed OpenAI Ads account pair.
        account_currency="USD",
        currency_minor_unit_exponent=2,
        account_timezone=brief.account_timezone,
        campaign_handle=brief.campaign_handle,
        campaign_request=OpenAIAdsNativeCampaignPayload(
            name=brief.name,
            start_time=brief.start_time,
            end_time=brief.end_time,
            budget=OpenAIAdsNativeBudget(lifetime_spend_limit_micros=lifetime_micros),
            bidding_type=brief.bidding_type,
            targeting=OpenAIAdsNativeTargeting(
                locations=OpenAIAdsNativeLocations(
                    include=tuple(OpenAIAdsNativeLocation(id=value) for value in brief.location_ids)
                )
            ),
        ),
        ad_groups=tuple(compiled_groups),
        input_digest=digest(brief.model_dump(mode="json")),
        activation_boundary=OpenAIAdsActivationBoundary(
            maximum_spend_micros=lifetime_micros,
            maximum_spend=maximum_spend,
        ),
        planning_boundary=OpenAIAdsPlanningBoundary(),
    )
