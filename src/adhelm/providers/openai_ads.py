"""Bounded, read-only OpenAI Ads inventory and aggregate-report adapter.

This adapter has fixture verification only.  It does not create, update, pause,
activate, upload, or publish any remote resource.  Responses are parsed in
memory into closed models and are never persisted as raw provider payloads.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field, ValidationError, field_validator, model_validator

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Currency, Money, SafeRef, StrictModel
from adhelm.privacy import validate_data
from adhelm.reporting import NormalizedReport, PerformanceRow, QualityStatus, ReportEnvelope
from adhelm.transport import SafeHttpClient

# The fixed host and the one pinned API version for every OpenAI Ads read in
# this build.  `adhelm.providers.openai_conversions` imports both rather than
# repeating them, so the product can never send two versions at once.
OPENAI_ADS_API_HOST: Final = "api.ads.openai.com"
OPENAI_ADS_API_VERSION: Final = "v1"
# The Advertiser API overview documents limits enforced by both ad account and
# IP address: 600 requests per minute per endpoint and 1,200 requests per
# minute overall.  These numbers are the documented model this build discloses
# in a quota diagnostic; the adapter does not meter, budget, or pace requests
# against them, and it never presents them as a live-observed limit.
OPENAI_ADS_ENDPOINT_REQUESTS_PER_MINUTE: Final = 600
OPENAI_ADS_ACCOUNT_REQUESTS_PER_MINUTE: Final = 1_200

_HOST = OPENAI_ADS_API_HOST
_API_VERSION = OPENAI_ADS_API_VERSION
_MAX_PAGES = 10
_MAX_SIGNED_INT64 = 9_223_372_036_854_775_807
_CAMPAIGN_FIELDS = (
    "metadata.readable_time",
    "campaign.id",
    "campaign.impressions",
    "campaign.clicks",
    "campaign.spend",
)
_AD_FIELDS = (
    "metadata.readable_time",
    "campaign.id",
    "ad.id",
    "ad.impressions",
    "ad.clicks",
    "ad.spend",
)

# What this build's fixed OpenAI Ads reads actually request, per audit-relevant
# surface.  A provider audit reads this map instead of guessing what a missing
# value means: a surface that is not requested is unread, never healthy.
OpenAIAdsReadSurfaceStatus = Literal[
    "IMPLEMENTED_FIXED_READ",
    "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "NOT_IN_REVIEWED_FIXED_READ",
    "NOT_DOCUMENTED",
]
OPENAI_ADS_READ_SURFACES: Final[dict[str, OpenAIAdsReadSurfaceStatus]] = {
    "ACCOUNT_IDENTITY": "IMPLEMENTED_FIXED_READ",
    "ACCOUNT_REVIEW_STATUS": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "AD_GROUP_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "AD_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_BIDDING_TYPE": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_LIFETIME_SPEND_LIMIT": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_RUN_SCHEDULE": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_TARGETING_CRITERIA": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "CAMPAIGN_CONVERSION_EVENT_SETTINGS": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "AD_GROUP_BILLING_EVENT": "IMPLEMENTED_FIXED_READ",
    "AD_GROUP_MAXIMUM_BID": "IMPLEMENTED_FIXED_READ",
    "AD_GROUP_CONTEXT_HINTS": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "AD_REVIEW_STATUS": "IMPLEMENTED_FIXED_READ",
    "AD_CREATIVE_FILE_ASSOCIATION": "IMPLEMENTED_FIXED_READ",
    "AD_CREATIVE_COPY_PRESENCE": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "AD_CREATIVE_COPY_TEXT": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_DESTINATION_URL_PRESENCE": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "AD_DESTINATION_URL_VALUE": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_CREATIVE_IMAGE_URL_PRESENCE": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "AD_CREATIVE_IMAGE_URL_VALUE": "NOT_IN_REVIEWED_FIXED_READ",
    "PLACEMENT": "NOT_DOCUMENTED",
    "CAMPAIGN_DELIVERY_REPORT": "IMPLEMENTED_FIXED_READ",
    "AD_DELIVERY_REPORT": "IMPLEMENTED_FIXED_READ",
    "NATIVE_CONVERSION_METRICS": "NOT_IN_REVIEWED_FIXED_READ",
    "HIERARCHY_PAGINATION": "IMPLEMENTED_FIXED_READ",
    "INSIGHTS_PAGINATION": "IMPLEMENTED_FIXED_READ",
    "REPORT_FRESHNESS": "NOT_DOCUMENTED",
    "REPORT_ATTRIBUTION_WINDOW": "NOT_DOCUMENTED",
}
# The Campaign reference documents that campaign state accepts `active` or
# `paused`.  No reviewed enumeration exists for an ad group or an ad, so both
# reviewed sets are deliberately empty and this build makes no claim about
# whether one of their status values is valid.
OPENAI_ADS_REVIEWED_CAMPAIGN_STATUSES: Final[tuple[str, ...]] = ("active", "paused")
OPENAI_ADS_REVIEWED_AD_GROUP_STATUSES: Final[tuple[str, ...]] = ()
OPENAI_ADS_REVIEWED_AD_STATUSES: Final[tuple[str, ...]] = ()
# The one documented status that declares an intent to serve.  A declared
# status is not an observation of delivery.
OPENAI_ADS_SERVING_INTENT_STATUS: Final = "active"


def openai_ads_declares_serving_intent(status: str | None) -> bool:
    """Whether a retained OpenAI status declares the documented serving intent."""
    return isinstance(status, str) and status.casefold() == OPENAI_ADS_SERVING_INTENT_STATUS


def classify_openai_ads_transport_error(error: AdhelmError) -> AdhelmError:
    """Turn one shared-transport refusal into an exact OpenAI Ads diagnostic.

    The shared transport records the observed HTTP status and, when the
    provider sent a whole-second `Retry-After`, its bounded value.  Nothing
    from the response body is available here, so no classification can leak
    one.  A status this contract does not classify is returned unchanged
    rather than relabelled into a rate limit or a credential problem it was
    never shown to be.
    """
    status = getattr(error, "status_code", None)
    retry_after = getattr(error, "retry_after_seconds", None)
    if status == 401:
        return AdhelmError(
            "OPENAI_ADS_KEY_INVALID_OR_EXPIRED",
            "OpenAI Ads rejected the configured account key.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action=(
                "Run `adhelm auth openai-key status --profile <id>`, issue a replacement key in "
                "the Settings tab of OpenAI Ads Manager, then rotate with "
                "`adhelm auth openai-key rotate --profile <id>` in a local terminal."
            ),
            status_code=status,
            retry_after_seconds=retry_after,
        )
    if status == 403:
        return AdhelmError(
            "OPENAI_ADS_ACCESS_NOT_PERMITTED",
            "OpenAI Ads refused this account key for the requested surface.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action=(
                "Confirm in the Settings tab of OpenAI Ads Manager that this ad account still "
                "grants the key access to the requested surface before retrying."
            ),
            status_code=status,
            retry_after_seconds=retry_after,
        )
    if status == 429:
        disclosed = (
            f"The provider asked for a wait of {retry_after} second(s) before the next request."
            if retry_after is not None
            else "The provider sent no whole-second Retry-After hint."
        )
        return AdhelmError(
            "OPENAI_ADS_RATE_LIMITED",
            "OpenAI Ads refused the fixed read for exceeding its documented request rate.",
            exit_code=int(ExitCode.PRECONDITION),
            next_action=(
                f"{disclosed} OpenAI documents "
                f"{OPENAI_ADS_ENDPOINT_REQUESTS_PER_MINUTE} requests per minute per endpoint and "
                f"{OPENAI_ADS_ACCOUNT_REQUESTS_PER_MINUTE} per minute overall, enforced by ad "
                "account and by IP address; wait and run the same bounded read again."
            ),
            status_code=status,
            retry_after_seconds=retry_after,
        )
    return error


CampaignBiddingType = Literal["impressions", "clicks", "conversions"]
BillingEventType = Literal["impression", "click"]
CreativeType = Literal["chat_card"]
_BILLING_EVENT_FOR_BIDDING_TYPE: dict[CampaignBiddingType, BillingEventType] = {
    "impressions": "impression",
    "clicks": "click",
    "conversions": "click",
}

TokenGetter = Callable[[], str]
ReferenceFor = Callable[[Literal["account", "campaign", "ad_group", "ad"], str], str]


def _default_reference_for(
    kind: Literal["account", "campaign", "ad_group", "ad"], value: str
) -> str:
    return f"openai_ads.{kind}.{value}"


def _safe_token(getter: TokenGetter) -> str:
    token = getter()
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 16_384
        or any(not 33 <= ord(char) <= 126 for char in token)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "OpenAI Ads authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local OpenAI Ads read profile.",
        )
    return token


def _json(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        payload = parse_json(response.content, maximum_bytes=8_388_608, decimal_numbers=True)
    except (ValueError, UnicodeDecodeError, AdhelmError) as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} response is not JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} response has an invalid shape."
        )
    # The payload remains transient, but scan every allowed response value
    # before extracting a small aggregate subset.  This prevents a sensitive
    # name, URL, targeting field, or accidental secret from entering a model.
    try:
        validate_data(payload)
    except AdhelmError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID",
            f"OpenAI Ads {context} response contains prohibited data.",
        ) from exc
    return payload


def _keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} included an unsupported field."
        )


def _safe_string(value: Any, context: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} is invalid.")
    try:
        validate_data(value)
    except AdhelmError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "OpenAI Ads response contains prohibited data."
        ) from exc
    return value


def _timezone(value: Any, context: str) -> str:
    timezone = _safe_string(value, context, maximum=80)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} is not an IANA timezone."
        ) from exc
    return timezone


def _client_id(value: Any, context: str) -> str:
    parsed = _safe_string(value, context, maximum=120)
    if not all(char.isascii() and (char.isalnum() or char in "_-") for char in parsed):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} is invalid.")
    return parsed


def _input_client_id(value: object, context: str) -> str:
    """Validate a caller-supplied native identifier without calling transport."""
    try:
        return _client_id(value, context)
    except AdhelmError as exc:
        raise AdhelmError(
            "INVALID_INPUT",
            f"OpenAI Ads {context} is invalid.",
            exit_code=2,
        ) from exc


def _declared_object(value: Any, context: str) -> bool:
    """Whether a documented nested object is present and non-empty.

    Only presence is returned.  The object's contents are never read into a
    model, so a targeting declaration cannot carry audience material into local
    state.  An empty object is `False`: the write contract records that an
    omitted targeting selection can reach every available location, so an empty
    declaration is exactly the case an audit needs to see as undeclared.
    """
    if value is None:
        return False
    if not isinstance(value, dict):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} is invalid.")
    return bool(value)


def _declared_count(value: Any, context: str) -> int:
    """Count a documented bounded list without retaining any of its elements."""
    if value is None:
        return 0
    if not isinstance(value, list) or len(value) > 1_000:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} is invalid.")
    return len(value)


def _integer(value: Any, context: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > 9_007_199_254_740_991
    ):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID",
            f"OpenAI Ads {context} is not a supported nonnegative integer.",
        )
    return int(value)


def _native_micros(value: Any, context: str) -> str:
    if type(value) is not int or not 1 <= value <= _MAX_SIGNED_INT64:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} is outside the local micros bound."
        )
    return str(value)


def _minor_units(value: Any, currency: Currency, context: str) -> Money:
    if currency != "USD":
        raise AdhelmError(
            "UNSUPPORTED_CURRENCY",
            "OpenAI Ads spend conversion is currently implemented only for documented USD.",
        )
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} is invalid.")
    try:
        amount = Decimal(str(value))
        minor = amount * 100
    except (InvalidOperation, ValueError) as exc:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} is invalid.") from exc
    if not amount.is_finite() or amount < 0 or minor != minor.to_integral_value():
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} has unsupported precision."
        )
    try:
        return Money(minor_units=int(minor), currency=currency)
    except ValueError as exc:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} is invalid.") from exc


def _timestamp(value: Any, context: str) -> datetime:
    seconds = _integer(value, context)
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} is invalid.") from exc


def _revalidate_input[T: StrictModel](value: object, model: type[T], context: str) -> T:
    """Reject model-copy/construct query values before they can reach transport."""
    try:
        if not isinstance(value, model):
            raise TypeError("unexpected public input type")
        return model.model_validate_json(value.model_dump_json(warnings=False))
    except (TypeError, ValidationError, ValueError) as exc:
        raise AdhelmError(
            "INVALID_INPUT",
            f"OpenAI Ads {context} is invalid.",
            exit_code=2,
        ) from exc


class OpenAIAdsIdentity(StrictModel):
    """Readonly account identity returned by ``GET /ad_account``."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    account_client_ref: SafeRef
    timezone: str = Field(min_length=1, max_length=80)
    currency: Currency
    status: str = Field(min_length=1, max_length=80)
    review_status: str | None = Field(default=None, max_length=80)
    # The one pinned API version this build reads, so a retained record can
    # never claim a version the adapter did not request.
    provider_api_version: Literal["v1"] = OPENAI_ADS_API_VERSION


class OpenAIAdsCampaign(StrictModel):
    """The safe inventory subset of an OpenAI Ads campaign."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    campaign_ref: SafeRef
    campaign_client_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)
    bidding_type: CampaignBiddingType | None = None
    lifetime_spend_limit_micros: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    start_at: datetime | None = None
    end_at: datetime | None = None
    # Presence facts only.  The documented targeting object and conversion
    # association list are read to establish that a campaign declares them at
    # all; their contents are never retained, because a targeting object can
    # carry audience material and an association element schema is not
    # documented.  `False`/`0` means "not declared in this response", never
    # "targets everyone" or "no conversion setting exists".
    targeting_declared: bool = False
    conversion_event_setting_count: int = Field(default=0, ge=0, le=1_000)

    @field_validator("start_at", "end_at")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Campaign timestamps must include a timezone")
        return value

    @field_validator("lifetime_spend_limit_micros")
    @classmethod
    def native_micros_within_local_bound(cls, value: str | None) -> str | None:
        if value is not None and int(value) > _MAX_SIGNED_INT64:
            raise ValueError("Campaign native micros exceed the local signed-int64 bound")
        return value

    @model_validator(mode="after")
    def valid_schedule(self) -> OpenAIAdsCampaign:
        if self.start_at is not None and self.end_at is not None and self.end_at < self.start_at:
            raise ValueError("Campaign end must not precede campaign start")
        return self


class OpenAIAdsAdGroup(StrictModel):
    """Safe lower-level inventory state scoped to one discovered campaign."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    campaign_ref: SafeRef
    campaign_client_ref: SafeRef
    ad_group_ref: SafeRef
    ad_group_client_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)
    billing_event_type: BillingEventType
    max_bid_micros: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    # Bounded count only.  Context hints are free customer text and are never
    # retained; the count establishes that a group declares contextual inputs.
    context_hint_count: int = Field(default=0, ge=0, le=1_000)

    @field_validator("max_bid_micros")
    @classmethod
    def native_micros_within_local_bound(cls, value: str | None) -> str | None:
        if value is not None and int(value) > _MAX_SIGNED_INT64:
            raise ValueError("Ad-group native micros exceed the local signed-int64 bound")
        return value


class OpenAIAdsAd(StrictModel):
    """Safe ad inventory state scoped to one discovered ad group."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    campaign_ref: SafeRef
    campaign_client_ref: SafeRef
    ad_group_ref: SafeRef
    ad_group_client_ref: SafeRef
    ad_ref: SafeRef
    ad_client_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)
    review_status: str | None = Field(default=None, max_length=80)
    creative_type: CreativeType
    # The opaque provider file identifier this ad's creative associates.  It is
    # an association fact, not content: it proves neither that the provider
    # holds a particular file nor that its bytes equal any local asset, per the
    # file read contract.  Copy and destination values stay presence flags so
    # that no ad copy, image URL, or landing page enters a local record.
    creative_file_client_ref: str | None = Field(default=None, max_length=120)
    creative_title_present: bool = False
    creative_body_present: bool = False
    creative_target_url_present: bool = False
    creative_image_url_present: bool = False


class OpenAIAdsHierarchyInventory(StrictModel):
    """One complete bounded hierarchy; never a partial lower-level collection."""

    schema_version: Literal[1] = 1
    campaigns: tuple[OpenAIAdsCampaign, ...]
    ad_groups: tuple[OpenAIAdsAdGroup, ...]
    ads: tuple[OpenAIAdsAd, ...]

    @model_validator(mode="after")
    def known_parent_references(self) -> OpenAIAdsHierarchyInventory:
        campaigns = {item.campaign_ref: item for item in self.campaigns}
        groups = {item.ad_group_ref: item for item in self.ad_groups}
        if len(campaigns) != len(self.campaigns) or len(groups) != len(self.ad_groups):
            raise ValueError("hierarchy contains duplicate native parent references")
        ad_refs = {item.ad_ref for item in self.ads}
        if len(ad_refs) != len(self.ads):
            raise ValueError("hierarchy contains duplicate native ad references")
        if len({item.account_ref for item in self.campaigns}) > 1:
            raise ValueError("campaigns belong to different account references")
        if any(item.campaign_ref not in campaigns for item in self.ad_groups):
            raise ValueError("ad group parent campaign is absent from hierarchy")
        if any(item.ad_group_ref not in groups for item in self.ads):
            raise ValueError("ad parent ad group is absent from hierarchy")
        for group in self.ad_groups:
            campaign = campaigns[group.campaign_ref]
            if (
                group.account_ref != campaign.account_ref
                or group.campaign_client_ref != campaign.campaign_client_ref
            ):
                raise ValueError("ad group parent campaign binding is inconsistent")
        for ad in self.ads:
            group = groups[ad.ad_group_ref]
            if (
                ad.account_ref != group.account_ref
                or ad.campaign_ref != group.campaign_ref
                or ad.campaign_client_ref != group.campaign_client_ref
                or ad.ad_group_client_ref != group.ad_group_client_ref
            ):
                raise ValueError("ad parent ad-group binding is inconsistent")
        return self


# The exact normalized data-quality vocabulary the OpenAI inventory branch
# appends to a retained snapshot.  These are the presence facts the provider
# audit reads; the shared closed snapshot state model has no field for them, so
# they are recorded as explicit statuses rather than invented state keys.  Every
# fact is stated in both directions so that an absent status never has to be
# read as either "present" or "absent".
OPENAI_ADS_TARGETING_DECLARED: Final = "TARGETING_DECLARED"
OPENAI_ADS_TARGETING_NOT_DECLARED: Final = "TARGETING_NOT_DECLARED"
OPENAI_ADS_CONVERSION_SETTINGS_ASSOCIATED: Final = "CONVERSION_EVENT_SETTINGS_ASSOCIATED"
OPENAI_ADS_CONVERSION_SETTINGS_NOT_ASSOCIATED: Final = "CONVERSION_EVENT_SETTINGS_NOT_ASSOCIATED"
OPENAI_ADS_CONTEXT_HINTS_DECLARED: Final = "CONTEXT_HINTS_DECLARED"
OPENAI_ADS_CONTEXT_HINTS_NOT_DECLARED: Final = "CONTEXT_HINTS_NOT_DECLARED"
OPENAI_ADS_CREATIVE_FILE_ASSOCIATED: Final = "CREATIVE_FILE_ASSOCIATED"
OPENAI_ADS_CREATIVE_FILE_NOT_ASSOCIATED: Final = "CREATIVE_FILE_NOT_ASSOCIATED"
OPENAI_ADS_CREATIVE_TITLE_PRESENT: Final = "CREATIVE_TITLE_PRESENT"
OPENAI_ADS_CREATIVE_TITLE_ABSENT: Final = "CREATIVE_TITLE_ABSENT"
OPENAI_ADS_CREATIVE_BODY_PRESENT: Final = "CREATIVE_BODY_PRESENT"
OPENAI_ADS_CREATIVE_BODY_ABSENT: Final = "CREATIVE_BODY_ABSENT"
OPENAI_ADS_CREATIVE_TARGET_URL_PRESENT: Final = "CREATIVE_TARGET_URL_PRESENT"
OPENAI_ADS_CREATIVE_TARGET_URL_ABSENT: Final = "CREATIVE_TARGET_URL_ABSENT"
OPENAI_ADS_CREATIVE_IMAGE_URL_PRESENT: Final = "CREATIVE_IMAGE_URL_PRESENT"
OPENAI_ADS_CREATIVE_IMAGE_URL_ABSENT: Final = "CREATIVE_IMAGE_URL_ABSENT"
OPENAI_ADS_AD_REVIEW_STATUS_RETURNED: Final = "AD_REVIEW_STATUS_RETURNED"
OPENAI_ADS_AD_REVIEW_STATUS_NOT_RETURNED: Final = "AD_REVIEW_STATUS_NOT_RETURNED"


def openai_ads_campaign_quality_statuses(campaign: OpenAIAdsCampaign) -> tuple[str, ...]:
    """Deterministic presence statuses for one retained campaign snapshot."""
    return (
        OPENAI_ADS_TARGETING_DECLARED
        if campaign.targeting_declared
        else OPENAI_ADS_TARGETING_NOT_DECLARED,
        OPENAI_ADS_CONVERSION_SETTINGS_ASSOCIATED
        if campaign.conversion_event_setting_count
        else OPENAI_ADS_CONVERSION_SETTINGS_NOT_ASSOCIATED,
    )


def openai_ads_ad_group_quality_statuses(ad_group: OpenAIAdsAdGroup) -> tuple[str, ...]:
    """Deterministic presence statuses for one retained ad-group snapshot."""
    return (
        OPENAI_ADS_CONTEXT_HINTS_DECLARED
        if ad_group.context_hint_count
        else OPENAI_ADS_CONTEXT_HINTS_NOT_DECLARED,
    )


def openai_ads_ad_quality_statuses(ad: OpenAIAdsAd) -> tuple[str, ...]:
    """Deterministic presence statuses for one retained ad snapshot.

    The creative file identifier itself is deliberately not recorded here: an
    association fact is what an audit needs, and repeating an opaque provider
    file ID into projected local history would not prove account ownership or
    byte equality anyway.
    """
    return (
        OPENAI_ADS_CREATIVE_FILE_ASSOCIATED
        if ad.creative_file_client_ref is not None
        else OPENAI_ADS_CREATIVE_FILE_NOT_ASSOCIATED,
        OPENAI_ADS_CREATIVE_TITLE_PRESENT
        if ad.creative_title_present
        else OPENAI_ADS_CREATIVE_TITLE_ABSENT,
        OPENAI_ADS_CREATIVE_BODY_PRESENT
        if ad.creative_body_present
        else OPENAI_ADS_CREATIVE_BODY_ABSENT,
        OPENAI_ADS_CREATIVE_TARGET_URL_PRESENT
        if ad.creative_target_url_present
        else OPENAI_ADS_CREATIVE_TARGET_URL_ABSENT,
        # Presence only.  The image URL itself is never read into a record, and
        # the fact is stated in both directions like every other presence fact
        # here, so an absent status is never read as "present" or "absent".
        OPENAI_ADS_CREATIVE_IMAGE_URL_PRESENT
        if ad.creative_image_url_present
        else OPENAI_ADS_CREATIVE_IMAGE_URL_ABSENT,
        OPENAI_ADS_AD_REVIEW_STATUS_RETURNED
        if ad.review_status is not None
        else OPENAI_ADS_AD_REVIEW_STATUS_NOT_RETURNED,
    )


class OpenAIAdsInventoryQuery(StrictModel):
    account_ref: SafeRef
    maximum_records: int = Field(default=500, ge=1, le=5_000)
    page_size: int = Field(default=500, ge=1, le=500)
    order: Literal["asc", "desc"] = "desc"

    @model_validator(mode="after")
    def bounded_pages(self) -> OpenAIAdsInventoryQuery:
        if self.maximum_records > self.page_size * _MAX_PAGES:
            raise ValueError("OpenAI Ads inventory exceeds the ten-page limit")
        return self


class OpenAIAdsHierarchyInventoryQuery(OpenAIAdsInventoryQuery):
    """A single global resource bound for campaign, group, and ad inventory."""


class OpenAIAdsReportQuery(StrictModel):
    """A fixed daily campaign or native-ad performance query.

    This deliberately has no filters, free-form projected fields, or segments.
    Those can expose unsupported grain or sensitive
    dimensions and need a separately reviewed capability contract.
    """

    level: Literal["CAMPAIGN", "AD"] = "CAMPAIGN"
    account_ref: SafeRef
    report_id: SafeRef
    query_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_start: date
    window_end: date
    source_timezone: str = Field(min_length=1, max_length=80)
    currency: Currency | None = None
    maximum_rows: int = Field(default=2_000, ge=1, le=20_000)
    page_size: int = Field(default=2_000, ge=1, le=2_000)

    @model_validator(mode="after")
    def valid_window_and_bound(self) -> OpenAIAdsReportQuery:
        if self.window_end < self.window_start:
            raise ValueError("OpenAI Ads report window end must not precede its start")
        if self.maximum_rows > self.page_size * _MAX_PAGES:
            raise ValueError("OpenAI Ads report exceeds the ten-page limit")
        return self


class OpenAIAdsClient:
    """Fixture-tested fixed OpenAI Ads reads.

    ``token_getter`` supplies a bearer token only when sending an in-memory
    header.  No token is represented in a model, result, diagnostic, or error.
    """

    def __init__(
        self,
        transport: SafeHttpClient,
        token_getter: TokenGetter,
        *,
        reference_for: ReferenceFor = _default_reference_for,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        connector_version: str = "0.1.0.dev0",
    ) -> None:
        if _HOST not in transport.allowed_hosts:
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID", "OpenAI Ads adapter requires its fixed API host."
            )
        self._transport = transport
        self._token_getter = token_getter
        self._reference_for = reference_for
        self._now = now
        self._connector_version = connector_version
        # Child reads are allowed only from parent objects returned by this
        # client after a complete bounded list.  Canonical-looking caller-made
        # models do not establish that a parent was discovered for this account.
        self._identities: dict[str, tuple[OpenAIAdsIdentity, OpenAIAdsIdentity]] = {}
        self._campaigns: dict[tuple[str, str], OpenAIAdsCampaign] = {}
        self._ad_groups: dict[tuple[str, str], OpenAIAdsAdGroup] = {}

    def discover_identity(
        self, *, expected_account_client_ref: str | None = None
    ) -> OpenAIAdsIdentity:
        # A later discovery attempt supersedes this client's prior session
        # state even if the new response is rejected. Otherwise a failed
        # refresh could leave an older identity eligible for a separately
        # composed reader after current identity proof is unavailable.
        self._identities.clear()
        self._campaigns.clear()
        self._ad_groups.clear()
        expected = (
            _input_client_id(expected_account_client_ref, "expected account ID")
            if expected_account_client_ref is not None
            else None
        )
        data = _json(self._get("/ad_account"), "account")
        _keys(
            data,
            {"id", "name", "url", "preview_url", "status", "timezone", "currency_code", "review"},
            "account",
        )
        account_client_ref = _client_id(data.get("id"), "account ID")
        if expected is not None and expected != account_client_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "OpenAI Ads account differs from the configured profile."
            )
        timezone = _timezone(data.get("timezone"), "account timezone")
        currency = _safe_string(data.get("currency_code"), "account currency", maximum=3)
        if (
            len(currency) != 3
            or not currency.isascii()
            or not currency.isupper()
            or not currency.isalpha()
        ):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "OpenAI Ads account currency is invalid."
            )
        review = data.get("review")
        if review is not None:
            if not isinstance(review, dict):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "OpenAI Ads account review is invalid."
                )
            _keys(review, {"status"}, "account review")
            review_status = _safe_string(review.get("status"), "account review status", maximum=80)
        else:
            review_status = None
        identity = OpenAIAdsIdentity(
            account_ref=self._reference_for("account", account_client_ref),
            account_client_ref=account_client_ref,
            timezone=timezone,
            currency=currency,
            status=_safe_string(data.get("status"), "account status", maximum=80),
            review_status=review_status,
        )
        snapshot = OpenAIAdsIdentity.model_validate(identity.model_dump(mode="python"))
        self._identities[identity.account_ref] = (identity, snapshot)
        return identity

    def is_current_identity(self, identity: OpenAIAdsIdentity) -> bool:
        """Return whether ``identity`` is this session's validated account object.

        This is intentionally a side-effect-free composition helper for a
        separately bounded reader.  It revalidates public-model state before
        checking object identity, so ``model_copy`` or ``model_construct``
        cannot turn a cached object with forged fields into a session proof.
        A fresh discovery replaces the cache entry, invalidating the prior
        object for subsequent child reads.
        """

        if not isinstance(identity, OpenAIAdsIdentity):
            return False
        try:
            validated = OpenAIAdsIdentity.model_validate(identity.model_dump(mode="python"))
            account = _client_id(validated.account_client_ref, "identity account ID")
            canonical_account_ref = self._reference_for("account", account)
        except (AdhelmError, AttributeError, ValidationError, TypeError, ValueError):
            return False
        cached = self._identities.get(validated.account_ref)
        return (
            validated.account_ref == canonical_account_ref
            and cached is not None
            and cached[0] is identity
            and validated == cached[1]
        )

    def list_campaigns(
        self, query: OpenAIAdsInventoryQuery, *, identity: OpenAIAdsIdentity
    ) -> list[OpenAIAdsCampaign]:
        query = _revalidate_input(query, OpenAIAdsInventoryQuery, "inventory query")
        self._identity_matches(query.account_ref, identity)
        self._invalidate_campaign_cache(identity.account_ref)
        campaigns: list[OpenAIAdsCampaign] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {"limit": query.page_size, "order": query.order}
            if after is not None:
                params["after"] = after
            data = _json(self._get("/campaigns", params=params), "campaign list")
            page, after = self._parse_campaign_page(data, account_ref=identity.account_ref)
            self._page_within_requested_limit(page, query.page_size, "campaign list")
            if len(campaigns) + len(page) > query.maximum_records:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "OpenAI Ads inventory exceeds the configured complete-result bound.",
                )
            if len({item.campaign_ref for item in [*campaigns, *page]}) != len(campaigns) + len(
                page
            ):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads campaign inventory contains duplicate native identifiers.",
                )
            campaigns.extend(page)
            if after is None:
                self._campaigns.update(
                    {
                        (identity.account_ref, campaign.campaign_client_ref): campaign
                        for campaign in campaigns
                    }
                )
                return campaigns
        raise AdhelmError(
            "PAGINATION_LIMIT", "OpenAI Ads inventory exceeded the bounded page count."
        )

    def get_campaign(self, identity: OpenAIAdsIdentity, campaign_id: str) -> OpenAIAdsCampaign:
        """Read one current campaign only after complete same-session discovery.

        This fixed GET does not create a revision or conditional-write token.  Its
        result is an allowlisted observation that remains account-bound to the
        campaign first returned by ``list_campaigns`` in this client session.
        """
        self._identity_matches(identity.account_ref, identity)
        identifier = _input_client_id(campaign_id, "campaign ID")
        if (identity.account_ref, identifier) not in self._campaigns:
            raise AdhelmError(
                "PARENT_NOT_DISCOVERED",
                "OpenAI Ads campaign was not returned by this bounded discovery session.",
            )
        # A current read supersedes its listed parent. Clear the parent and its
        # dependent child cache before transport so a failed, malformed, or
        # mismatched GET cannot leave an older parent eligible for child reads.
        self._invalidate_campaign_cache(identity.account_ref, identifier)
        current = self._parse_campaign(
            _json(self._get(f"/campaigns/{identifier}"), "campaign"),
            account_ref=identity.account_ref,
            expected_account_client_ref=identity.account_client_ref,
        )
        if current.campaign_client_ref != identifier:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "OpenAI Ads returned a different campaign resource."
            )
        self._campaigns[(identity.account_ref, identifier)] = current
        return current

    def list_ad_groups(
        self,
        query: OpenAIAdsInventoryQuery,
        *,
        identity: OpenAIAdsIdentity,
        campaign: OpenAIAdsCampaign,
    ) -> list[OpenAIAdsAdGroup]:
        """List every bounded child group of one exact discovered campaign."""
        query = _revalidate_input(query, OpenAIAdsInventoryQuery, "inventory query")
        self._identity_matches(query.account_ref, identity)
        self._campaign_matches_identity(campaign, identity)
        self._invalidate_ad_group_cache(identity.account_ref, campaign.campaign_client_ref)
        groups: list[OpenAIAdsAdGroup] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {
                "campaign_id": campaign.campaign_client_ref,
                "limit": query.page_size,
                "order": query.order,
            }
            if after is not None:
                params["after"] = after
            data = _json(self._get("/ad_groups", params=params), "ad group list")
            page, after = self._parse_ad_group_page(data, campaign=campaign)
            self._page_within_requested_limit(page, query.page_size, "ad group list")
            if len(groups) + len(page) > query.maximum_records:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "OpenAI Ads ad-group inventory exceeds the configured complete-result bound.",
                )
            if len({item.ad_group_ref for item in [*groups, *page]}) != len(groups) + len(page):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads ad-group inventory contains duplicate native identifiers.",
                )
            groups.extend(page)
            if after is None:
                self._ad_groups = {
                    key: item
                    for key, item in self._ad_groups.items()
                    if not (
                        key[0] == identity.account_ref
                        and item.campaign_client_ref == campaign.campaign_client_ref
                    )
                }
                self._ad_groups.update(
                    {(identity.account_ref, group.ad_group_client_ref): group for group in groups}
                )
                return groups
        raise AdhelmError(
            "PAGINATION_LIMIT", "OpenAI Ads ad-group inventory exceeded the bounded page count."
        )

    def list_ads(
        self,
        query: OpenAIAdsInventoryQuery,
        *,
        identity: OpenAIAdsIdentity,
        ad_group: OpenAIAdsAdGroup,
    ) -> list[OpenAIAdsAd]:
        """List every bounded child ad of one exact discovered ad group."""
        query = _revalidate_input(query, OpenAIAdsInventoryQuery, "inventory query")
        self._identity_matches(query.account_ref, identity)
        self._ad_group_matches_identity(ad_group, identity)
        ads: list[OpenAIAdsAd] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {
                "ad_group_id": ad_group.ad_group_client_ref,
                "limit": query.page_size,
                "order": query.order,
            }
            if after is not None:
                params["after"] = after
            data = _json(self._get("/ads", params=params), "ad list")
            page, after = self._parse_ad_page(data, ad_group=ad_group)
            self._page_within_requested_limit(page, query.page_size, "ad list")
            if len(ads) + len(page) > query.maximum_records:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "OpenAI Ads ad inventory exceeds the configured complete-result bound.",
                )
            if len({item.ad_ref for item in [*ads, *page]}) != len(ads) + len(page):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads ad inventory contains duplicate native identifiers.",
                )
            ads.extend(page)
            if after is None:
                return ads
        raise AdhelmError(
            "PAGINATION_LIMIT", "OpenAI Ads ad inventory exceeded the bounded page count."
        )

    def list_hierarchy(
        self, query: OpenAIAdsHierarchyInventoryQuery, *, identity: OpenAIAdsIdentity
    ) -> OpenAIAdsHierarchyInventory:
        """Read campaign, ad-group, and ad collections under one total resource cap."""
        query = _revalidate_input(query, OpenAIAdsHierarchyInventoryQuery, "hierarchy query")
        self._identity_matches(query.account_ref, identity)
        remaining = query.maximum_records

        def bounded_query() -> OpenAIAdsInventoryQuery:
            return OpenAIAdsInventoryQuery(
                account_ref=query.account_ref,
                # A zero remaining budget still needs one bounded request to
                # prove that the next native child collection is empty.
                maximum_records=max(1, remaining),
                page_size=query.page_size,
                order=query.order,
            )

        def consume(
            items: list[OpenAIAdsCampaign] | list[OpenAIAdsAdGroup] | list[OpenAIAdsAd],
        ) -> None:
            nonlocal remaining
            if len(items) > remaining:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "OpenAI Ads hierarchy exceeds the configured complete-result bound.",
                )
            remaining -= len(items)

        campaigns = self.list_campaigns(bounded_query(), identity=identity)
        consume(campaigns)
        groups: list[OpenAIAdsAdGroup] = []
        ads: list[OpenAIAdsAd] = []
        for campaign in campaigns:
            child_groups = self.list_ad_groups(
                bounded_query(), identity=identity, campaign=campaign
            )
            consume(child_groups)
            groups.extend(child_groups)
            for group in child_groups:
                child_ads = self.list_ads(bounded_query(), identity=identity, ad_group=group)
                consume(child_ads)
                ads.extend(child_ads)
        return OpenAIAdsHierarchyInventory(
            campaigns=tuple(campaigns), ad_groups=tuple(groups), ads=tuple(ads)
        )

    def run_campaign_report(
        self, query: OpenAIAdsReportQuery, *, identity: OpenAIAdsIdentity
    ) -> NormalizedReport:
        return self._run_report(query, identity=identity, level="campaign", fields=_CAMPAIGN_FIELDS)

    def run_ad_report(
        self, query: OpenAIAdsReportQuery, *, identity: OpenAIAdsIdentity
    ) -> NormalizedReport:
        """Run the fixed daily ad-grain aggregate report without inferred child joins."""
        return self._run_report(query, identity=identity, level="ad", fields=_AD_FIELDS)

    def _run_report(
        self,
        query: OpenAIAdsReportQuery,
        *,
        identity: OpenAIAdsIdentity,
        level: Literal["campaign", "ad"],
        fields: tuple[str, ...],
    ) -> NormalizedReport:
        query = _revalidate_input(query, OpenAIAdsReportQuery, "report query")
        self._identity_matches(query.account_ref, identity)
        if query.level.lower() != level:
            raise AdhelmError(
                "QUERY_GRAIN_MISMATCH", "OpenAI Ads query differs from selected report method."
            )
        if query.source_timezone != identity.timezone:
            raise AdhelmError(
                "TIMEZONE_MISMATCH", "OpenAI Ads report timezone differs from account identity."
            )
        if query.currency is not None and query.currency != identity.currency:
            raise AdhelmError(
                "CURRENCY_MISMATCH", "OpenAI Ads report currency differs from account identity."
            )
        if identity.currency != "USD":
            raise AdhelmError(
                "UNSUPPORTED_CURRENCY",
                "OpenAI Ads spend conversion is currently implemented only for documented USD.",
            )
        self._validate_report_window(query)

        rows: list[PerformanceRow] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {
                "time_granularity": "daily",
                "aggregation_level": level,
                "fields[]": list(fields),
                "time_ranges[]": json.dumps(
                    {
                        "type": "date_range",
                        "since": query.window_start.isoformat(),
                        "until": query.window_end.isoformat(),
                        "timezone": query.source_timezone,
                    },
                    separators=(",", ":"),
                ),
                "limit": query.page_size,
            }
            if after is not None:
                params["after"] = after
            context = f"{level} insights"
            payload = _json(self._get("/ad_account/insights", params=params), context)
            page, after = self._parse_insights_page(payload, query, identity, level=level)
            self._page_within_requested_limit(page, query.page_size, context)
            if len(rows) + len(page) > query.maximum_rows:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "OpenAI Ads report exceeds the configured complete-result bound.",
                )
            rows.extend(page)
            if after is None:
                return NormalizedReport(
                    envelope=ReportEnvelope(
                        report_id=query.report_id,
                        provider="openai_ads",
                        source_account_ref=query.account_ref,
                        query_contract_hash=query.query_contract_hash,
                        window_start=query.window_start,
                        window_end=query.window_end,
                        source_timezone=identity.timezone,
                        report_timezone=identity.timezone,
                        currency=identity.currency,
                        attribution_model="OPENAI_ADS_PROVIDER_REPORTED",
                        attribution_window=None,
                        observed_at=self._observed_at(),
                        data_fresh_through=None,
                        data_quality_statuses=["COMPLETE"],
                        provider_api_version=_API_VERSION,
                        connector_version=self._connector_version,
                    ),
                    performance_rows=rows,
                )
        raise AdhelmError("PAGINATION_LIMIT", "OpenAI Ads report exceeded the bounded page count.")

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            return self._transport.request(
                "GET",
                f"https://{_HOST}/{_API_VERSION}{path}",
                headers={"authorization": f"Bearer {_safe_token(self._token_getter)}"},
                params=params,
            )
        except AdhelmError as error:
            # Classify the observed status only. The refusal carries no response
            # body here, so an OpenAI-typed quota or credential diagnostic can
            # never disclose one, and an unclassified status is re-raised as-is.
            raise classify_openai_ads_transport_error(error) from None

    def _identity_matches(self, account_ref: str, identity: OpenAIAdsIdentity) -> None:
        account = _client_id(identity.account_client_ref, "identity account ID")
        if account_ref != identity.account_ref or identity.account_ref != self._reference_for(
            "account", account
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "OpenAI Ads request differs from discovered account identity."
            )
        if not self.is_current_identity(identity):
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "OpenAI Ads identity was not returned by this bounded discovery session.",
            )

    def _campaign_matches_identity(
        self, campaign: OpenAIAdsCampaign, identity: OpenAIAdsIdentity
    ) -> None:
        identifier = _client_id(campaign.campaign_client_ref, "parent campaign ID")
        if (
            campaign.account_ref != identity.account_ref
            or campaign.campaign_ref != self._reference_for("campaign", identifier)
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "OpenAI Ads campaign differs from discovered account identity."
            )
        if self._campaigns.get((identity.account_ref, identifier)) is not campaign:
            raise AdhelmError(
                "PARENT_NOT_DISCOVERED",
                "OpenAI Ads campaign was not returned by this bounded discovery session.",
            )

    def _invalidate_campaign_cache(
        self, account_ref: SafeRef, campaign_id: str | None = None
    ) -> None:
        """Remove stale discovered parents, plus children that depend on them."""
        if campaign_id is None:
            self._campaigns = {
                key: item for key, item in self._campaigns.items() if key[0] != account_ref
            }
        else:
            self._campaigns.pop((account_ref, campaign_id), None)
        self._invalidate_ad_group_cache(account_ref, campaign_id)

    def _invalidate_ad_group_cache(
        self, account_ref: SafeRef, campaign_id: str | None = None
    ) -> None:
        self._ad_groups = {
            key: item
            for key, item in self._ad_groups.items()
            if key[0] != account_ref
            or (campaign_id is not None and item.campaign_client_ref != campaign_id)
        }

    def _ad_group_matches_identity(
        self, ad_group: OpenAIAdsAdGroup, identity: OpenAIAdsIdentity
    ) -> None:
        campaign = _client_id(ad_group.campaign_client_ref, "parent campaign ID")
        identifier = _client_id(ad_group.ad_group_client_ref, "parent ad group ID")
        if (
            ad_group.account_ref != identity.account_ref
            or ad_group.campaign_ref != self._reference_for("campaign", campaign)
            or ad_group.ad_group_ref != self._reference_for("ad_group", identifier)
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "OpenAI Ads ad group differs from discovered account identity."
            )
        if self._ad_groups.get((identity.account_ref, identifier)) is not ad_group:
            raise AdhelmError(
                "PARENT_NOT_DISCOVERED",
                "OpenAI Ads ad group was not returned by this bounded discovery session.",
            )

    def _validate_report_window(self, query: OpenAIAdsReportQuery) -> None:
        today = self._observed_at().date()
        if query.window_end > today:
            raise AdhelmError(
                "INVALID_REPORT_WINDOW", "OpenAI Ads report window may not end in the future."
            )
        try:
            earliest = today.replace(year=today.year - 5)
        except ValueError:  # February 29 five years after a leap year.
            earliest = date(today.year - 5, 2, 28)
        if query.window_end < earliest:
            raise AdhelmError(
                "INVALID_REPORT_WINDOW",
                "OpenAI Ads report window needs a bound within the last five years.",
            )

    def _parse_campaign_page(
        self, data: dict[str, Any], *, account_ref: SafeRef
    ) -> tuple[list[OpenAIAdsCampaign], str | None]:
        _keys(data, {"object", "data", "first_id", "last_id", "has_more"}, "campaign list")
        page = self._list_page(data, "campaign list")
        campaigns: list[OpenAIAdsCampaign] = []
        for item in page:
            campaigns.append(self._parse_campaign(item, account_ref=account_ref))
        return campaigns, self._next_cursor(data, "campaign list", bool(campaigns))

    def _parse_campaign(
        self,
        item: Any,
        *,
        account_ref: SafeRef,
        expected_account_client_ref: SafeRef | None = None,
    ) -> OpenAIAdsCampaign:
        if not isinstance(item, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "OpenAI Ads campaign is invalid.")
        _keys(
            item,
            {
                "id",
                "ad_account_id",
                "created_at",
                "status",
                "bidding_type",
                "budget",
                "conversion_event_setting_ids",
                "description",
                "end_time",
                "mode",
                "name",
                "start_time",
                "targeting",
                "updated_at",
            },
            "campaign",
        )
        returned_account = item.get("ad_account_id")
        if returned_account is not None and (
            expected_account_client_ref is None
            or _client_id(returned_account, "campaign account ID") != expected_account_client_ref
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "OpenAI Ads campaign differs from the discovered account."
            )
        campaign_id = _client_id(item.get("id"), "campaign ID")
        start = item.get("start_time")
        end = item.get("end_time")
        bidding_type = item.get("bidding_type")
        if bidding_type is not None:
            bidding_type = _safe_string(bidding_type, "campaign bidding type", maximum=80)
            if bidding_type not in _BILLING_EVENT_FOR_BIDDING_TYPE:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads campaign bidding type is not in the reviewed response contract.",
                )
        budget = item.get("budget")
        if budget is not None and not isinstance(budget, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "OpenAI Ads budget is invalid.")
        if isinstance(budget, dict):
            _keys(budget, {"lifetime_spend_limit_micros"}, "campaign budget")
        amount = budget.get("lifetime_spend_limit_micros") if budget else None
        return OpenAIAdsCampaign(
            account_ref=account_ref,
            campaign_ref=self._reference_for("campaign", campaign_id),
            campaign_client_ref=campaign_id,
            targeting_declared=_declared_object(item.get("targeting"), "campaign targeting"),
            conversion_event_setting_count=_declared_count(
                item.get("conversion_event_setting_ids"), "campaign conversion event settings"
            ),
            lifetime_spend_limit_micros=(
                _native_micros(amount, "campaign lifetime spend") if amount is not None else None
            ),
            status=_safe_string(item.get("status"), "campaign status", maximum=80),
            bidding_type=bidding_type,
            start_at=_timestamp(start, "campaign start") if start is not None else None,
            end_at=_timestamp(end, "campaign end") if end is not None else None,
        )

    def _parse_ad_group_page(
        self, data: dict[str, Any], *, campaign: OpenAIAdsCampaign
    ) -> tuple[list[OpenAIAdsAdGroup], str | None]:
        _keys(data, {"object", "data", "first_id", "last_id", "has_more"}, "ad group list")
        page = self._list_page(data, "ad group list")
        groups: list[OpenAIAdsAdGroup] = []
        for item in page:
            if not isinstance(item, dict):
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "OpenAI Ads ad group is invalid.")
            _keys(
                item,
                {
                    "id",
                    "created_at",
                    "updated_at",
                    "name",
                    "description",
                    "context_hints",
                    "status",
                    "bidding_config",
                },
                "ad group",
            )
            ad_group_id = _client_id(item.get("id"), "ad group ID")
            bidding_config = item.get("bidding_config")
            if not isinstance(bidding_config, dict):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads ad group bidding configuration is invalid.",
                )
            _keys(
                bidding_config,
                {"billing_event_type", "max_bid_micros"},
                "ad group bidding configuration",
            )
            billing_event_type = _safe_string(
                bidding_config.get("billing_event_type"),
                "ad group billing event type",
                maximum=80,
            )
            if billing_event_type not in {"impression", "click"}:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads ad-group billing event is not in the reviewed response contract.",
                )
            if (
                campaign.bidding_type is not None
                and billing_event_type != _BILLING_EVENT_FOR_BIDDING_TYPE[campaign.bidding_type]
            ):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads ad-group billing event disagrees with its discovered campaign.",
                )
            max_bid_micros = _native_micros(
                bidding_config.get("max_bid_micros"), "ad group maximum bid micros"
            )
            groups.append(
                OpenAIAdsAdGroup(
                    account_ref=campaign.account_ref,
                    campaign_ref=campaign.campaign_ref,
                    campaign_client_ref=campaign.campaign_client_ref,
                    ad_group_ref=self._reference_for("ad_group", ad_group_id),
                    ad_group_client_ref=ad_group_id,
                    status=_safe_string(item.get("status"), "ad group status", maximum=80),
                    billing_event_type=cast(BillingEventType, billing_event_type),
                    max_bid_micros=max_bid_micros,
                    context_hint_count=_declared_count(
                        item.get("context_hints"), "ad group context hints"
                    ),
                )
            )
        return groups, self._next_cursor(data, "ad group list", bool(groups))

    def _parse_ad_page(
        self, data: dict[str, Any], *, ad_group: OpenAIAdsAdGroup
    ) -> tuple[list[OpenAIAdsAd], str | None]:
        _keys(data, {"object", "data", "first_id", "last_id", "has_more"}, "ad list")
        page = self._list_page(data, "ad list")
        ads: list[OpenAIAdsAd] = []
        for item in page:
            if not isinstance(item, dict):
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "OpenAI Ads ad is invalid.")
            _keys(
                item,
                {
                    "id",
                    "name",
                    "created_at",
                    "updated_at",
                    "creative",
                    "status",
                    "review_status",
                },
                "ad",
            )
            ad_id = _client_id(item.get("id"), "ad ID")
            creative = item.get("creative")
            if not isinstance(creative, dict):
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "OpenAI Ads ad creative is invalid.")
            _keys(
                creative,
                {"type", "title", "body", "file_id", "image_url", "target_url"},
                "ad creative",
            )
            creative_type = _safe_string(creative.get("type"), "ad creative type", maximum=80)
            if creative_type != "chat_card":
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads creative is outside the reviewed chat-card response contract.",
                )
            review_status = item.get("review_status")
            creative_file = creative.get("file_id")
            ads.append(
                OpenAIAdsAd(
                    account_ref=ad_group.account_ref,
                    campaign_ref=ad_group.campaign_ref,
                    campaign_client_ref=ad_group.campaign_client_ref,
                    ad_group_ref=ad_group.ad_group_ref,
                    ad_group_client_ref=ad_group.ad_group_client_ref,
                    ad_ref=self._reference_for("ad", ad_id),
                    ad_client_ref=ad_id,
                    status=_safe_string(item.get("status"), "ad status", maximum=80),
                    review_status=(
                        _safe_string(review_status, "ad review status", maximum=80)
                        if review_status is not None
                        else None
                    ),
                    creative_type=cast(CreativeType, creative_type),
                    creative_file_client_ref=(
                        _client_id(creative_file, "ad creative file ID")
                        if creative_file is not None
                        else None
                    ),
                    creative_title_present=creative.get("title") is not None,
                    creative_body_present=creative.get("body") is not None,
                    creative_target_url_present=creative.get("target_url") is not None,
                    creative_image_url_present=creative.get("image_url") is not None,
                )
            )
        return ads, self._next_cursor(data, "ad list", bool(ads))

    def _parse_insights_page(
        self,
        data: dict[str, Any],
        query: OpenAIAdsReportQuery,
        identity: OpenAIAdsIdentity,
        *,
        level: Literal["campaign", "ad"],
    ) -> tuple[list[PerformanceRow], str | None]:
        context = f"{level} insights"
        _keys(
            data,
            {"object", "data", "count", "first_id", "last_id", "has_more"},
            context,
        )
        page = self._list_page(data, context)
        count = data.get("count")
        if _integer(count, f"{level} insight count") != len(page):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "OpenAI Ads insight count differs from returned rows."
            )
        rows: list[PerformanceRow] = []
        for item in page:
            if not isinstance(item, dict):
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "OpenAI Ads insight row is invalid.")
            allowed = {
                "id",
                "start_time",
                "end_time",
                "readable_time",
                "campaign_id",
                "impressions",
                "clicks",
                "spend",
                "timezone",
            }
            if level == "campaign":
                allowed.add("campaign_name")
            else:
                allowed.add("ad_id")
            _keys(item, allowed, f"{level} insight row")
            campaign_id = _client_id(item.get("campaign_id"), f"{level} insight campaign ID")
            readable_day = _safe_string(
                item.get("readable_time"), f"{level} insight day", maximum=10
            )
            try:
                row_day = date.fromisoformat(readable_day)
            except ValueError as exc:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "OpenAI Ads insight day is invalid."
                ) from exc
            if not query.window_start <= row_day <= query.window_end:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads insight day is outside the requested window.",
                )
            if _timestamp(item.get("end_time"), f"{level} insight end") < _timestamp(
                item.get("start_time"), f"{level} insight start"
            ):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "OpenAI Ads insight time range is invalid."
                )
            timezone = item.get("timezone")
            if (
                timezone is not None
                and _safe_string(timezone, f"{level} insight timezone", maximum=80)
                != identity.timezone
            ):
                raise AdhelmError(
                    "TIMEZONE_MISMATCH",
                    "OpenAI Ads report row timezone differs from account identity.",
                )
            if level == "campaign":
                resource_level: Literal["CAMPAIGN", "AD"] = "CAMPAIGN"
                metric_grain: Literal["CAMPAIGN", "AD"] = "CAMPAIGN"
                resource_type = "OPENAI_ADS_CAMPAIGN"
                resource_ref = self._reference_for("campaign", campaign_id)
                parent_resource_ref = None
            else:
                ad_id = _client_id(item.get("ad_id"), "ad insight ID")
                resource_level = "AD"
                metric_grain = "AD"
                resource_type = "OPENAI_ADS_AD"
                resource_ref = self._reference_for("ad", ad_id)
                # The documented AD-grain response does not establish an ad-group
                # relationship or a creative version.  Do not manufacture either.
                parent_resource_ref = None
            quality_statuses: list[QualityStatus] = (
                ["COMPLETE"]
                if level == "campaign"
                else ["CREATIVE_NOT_EXPOSED", "NOT_AVAILABLE_FROM_PROVIDER"]
            )
            rows.append(
                PerformanceRow(
                    report_id=query.report_id,
                    resource_level=resource_level,
                    metric_grain=metric_grain,
                    resource_type=resource_type,
                    resource_ref=resource_ref,
                    parent_resource_ref=parent_resource_ref,
                    campaign_ref=self._reference_for("campaign", campaign_id),
                    dimension_type="DATE",
                    dimension_value=readable_day,
                    impressions=_integer(item.get("impressions"), f"{level} impressions"),
                    clicks=_integer(item.get("clicks"), f"{level} clicks"),
                    spend=_minor_units(item.get("spend"), identity.currency, f"{level} spend"),
                    data_quality_statuses=quality_statuses,
                )
            )
        return rows, self._next_cursor(data, context, bool(rows))

    @staticmethod
    def _list_page(data: dict[str, Any], context: str) -> list[Any]:
        if data.get("object") != "list":
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} object is invalid."
            )
        page = data.get("data")
        if not isinstance(page, list) or len(page) > 2_000:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} collection is invalid."
            )
        return page

    @staticmethod
    def _page_within_requested_limit(
        page: list[Any] | list[PerformanceRow], requested: int, context: str
    ) -> None:
        if len(page) > requested:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                f"OpenAI Ads {context} exceeded the requested page limit.",
            )

    @staticmethod
    def _next_cursor(data: dict[str, Any], context: str, has_rows: bool) -> str | None:
        has_more = data.get("has_more")
        if not isinstance(has_more, bool):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} pagination flag is invalid."
            )
        if not has_more:
            return None
        if not has_rows:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads {context} pagination made no progress."
            )
        return _safe_string(data.get("last_id"), f"{context} cursor", maximum=2_048)

    def _observed_at(self) -> datetime:
        observed_at = self._now()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise AdhelmError(
                "CLOCK_INVALID", "OpenAI Ads observation clock must include a timezone."
            )
        return observed_at
