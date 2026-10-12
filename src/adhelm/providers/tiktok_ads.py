"""Fixed, read-only TikTok Marketing API v1.3 adapter.

Only the reviewed identity, inventory and synchronous aggregate-report endpoints
are reachable here.  Provider responses are parsed in memory into closed
models; this module has no mutation or arbitrary-field/query surface.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Final, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field, model_validator

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError, ExitCode, copy_provider_diagnostics
from adhelm.models import Currency, Money, SafeRef, StrictModel
from adhelm.privacy import validate_data
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.transport import SafeHttpClient

# The one declaration of the fixed read host and the pinned API version.  Every
# request path, transport allowlist entry, retained snapshot, and report
# envelope reads these two names; there is no second literal anywhere.
TIKTOK_ADS_API_HOST: Final = "business-api.tiktok.com"
TIKTOK_ADS_API_VERSION: Final[Literal["v1.3"]] = "v1.3"
_HOST: Final = TIKTOK_ADS_API_HOST
_API_VERSION: Final[Literal["v1.3"]] = TIKTOK_ADS_API_VERSION
_MAX_PAGES = 10
_MAX_PAGE_SIZE = 500
_ID = re.compile(r"^[1-9][0-9]{0,39}$")
_ENUM = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_CURRENCY_EXPONENTS = {"AUD": 2, "CAD": 2, "EUR": 2, "GBP": 2, "JPY": 0, "USD": 2}

_IDENTITY_FIELDS = ["advertiser_id", "currency", "timezone", "status"]
_CAMPAIGN_FIELDS = ["campaign_id", "operation_status", "objective_type", "budget_mode", "budget"]
_ADGROUP_FIELDS = [
    "adgroup_id",
    "campaign_id",
    "operation_status",
    "budget_mode",
    "budget",
    "bid_type",
    "bid_price",
]
_AD_FIELDS = ["ad_id", "adgroup_id", "campaign_id", "operation_status"]
_CAMPAIGN_DIMENSIONS = ["stat_time_day", "campaign_id"]
_AD_DIMENSIONS = ["stat_time_day", "ad_id"]
_METRICS = ["impressions", "clicks", "spend"]

# --------------------------------------------------------------------------
# What this build's fixed TikTok reads actually return, per audit-relevant
# surface.  A provider audit reads this map instead of guessing what a missing
# value means: a surface that is not requested is unread, never healthy.
#
# TikTok's own reference portal renders through JavaScript, and the
# TikTok-maintained SDK documents the `fields` parameter of `/campaign/get/`,
# `/adgroup/get/` and `/ad/get/` as a bare `list[str]` with **no** enumeration
# of returnable field names.  Adding an unproven field name to a fixed read
# would be a fabricated capability that a fixture would silently accept, so the
# requested projection stays exactly the reviewed one and every other surface is
# declared unread here.  `docs/research/tiktok-oauth-contract.md` and
# `docs/research/tiktok-ads-read-contract.md` record the exact sources.
#
# `NOT_DOCUMENTED` is stronger than `NOT_IN_REVIEWED_FIXED_READ`: it means no
# fetchable first-party source publishes the fact at all, so no future field
# selection could recover it either.
# --------------------------------------------------------------------------
TikTokAdsReadSurfaceStatus = Literal[
    "IMPLEMENTED_FIXED_READ",
    "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "NOT_IN_REVIEWED_FIXED_READ",
    "NOT_DOCUMENTED",
]
TIKTOK_ADS_READ_SURFACES: Final[dict[str, TikTokAdsReadSurfaceStatus]] = {
    "ACCOUNT_IDENTITY": "IMPLEMENTED_FIXED_READ",
    "ACCOUNT_STATUS": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "AD_GROUP_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "AD_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "OPERATION_STATUS": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_OBJECTIVE": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "CAMPAIGN_BUDGET_MODE": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_BUDGET": "IMPLEMENTED_FIXED_READ",
    "AD_GROUP_BUDGET_MODE": "IMPLEMENTED_FIXED_READ",
    "AD_GROUP_BUDGET": "IMPLEMENTED_FIXED_READ",
    "AD_GROUP_BID_TYPE": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "AD_GROUP_BID_PRICE": "IMPLEMENTED_FIXED_READ",
    "INVENTORY_PAGINATION": "IMPLEMENTED_FIXED_READ",
    "REPORT_PAGINATION": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_DELIVERY_REPORT": "IMPLEMENTED_FIXED_READ",
    "AD_DELIVERY_REPORT": "IMPLEMENTED_FIXED_READ",
    # Requested by no reviewed read.  A field name for any of these is either
    # absent from every fetchable first-party source or unproven as a
    # `fields` value, so none of them is requested.
    "SECONDARY_STATUS": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_REVIEW_STATE": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_GROUP_SCHEDULE": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_GROUP_TARGETING_PRESENCE": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_GROUP_TARGETING_VALUES": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_GROUP_PLACEMENT": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_GROUP_OPTIMIZATION_GOAL": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_CREATIVE_ASSOCIATION": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_CREATIVE_VIDEO_PRESENCE": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_CREATIVE_COPY_TEXT": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_DESTINATION_URL_VALUE": "NOT_IN_REVIEWED_FIXED_READ",
    "PIXEL_AND_EVENT_HEALTH": "NOT_IN_REVIEWED_FIXED_READ",
    "NATIVE_CONVERSION_METRICS": "NOT_IN_REVIEWED_FIXED_READ",
    "REPORT_ATTRIBUTION": "NOT_IN_REVIEWED_FIXED_READ",
    "REPORT_FRESHNESS": "NOT_IN_REVIEWED_FIXED_READ",
    # Not published anywhere this review could fetch.
    "OBJECTIVE_ENUMERATION": "NOT_DOCUMENTED",
    "BID_TYPE_ENUMERATION": "NOT_DOCUMENTED",
    "ACCOUNT_STATUS_ENUMERATION": "NOT_DOCUMENTED",
    "ACCOUNT_PERMISSION_SCOPE": "NOT_DOCUMENTED",
    "APP_APPROVAL_STATE": "NOT_DOCUMENTED",
    "RATE_LIMIT_BUSINESS_CODES": "NOT_DOCUMENTED",
}

# The only `operation_status` values any reviewed first-party material pins:
# the TikTok-maintained SDK documents `ENABLE` as the create-body default, and
# this repository's reviewed v1.3 compiler contract pins `DISABLE` as the
# initial non-serving state.  A retained value outside this pair is reported as
# unreviewed; the audit never guesses what it means.
TIKTOK_ADS_REVIEWED_OPERATION_STATUSES: Final[tuple[str, ...]] = ("ENABLE", "DISABLE")
# The one reviewed operation status that declares an intent to serve.  Unlike
# Meta's `effective_status`, TikTok's `operation_status` is the value the
# operator set on *that* object: it does not fold a parent's status in.  The
# value that would -- `secondary_status` -- is not in the reviewed read, so an
# `ENABLE` child under a `DISABLE` parent is a genuine contradiction here and
# the rule table reports it.
TIKTOK_ADS_SERVING_INTENT_STATUS: Final = "ENABLE"

# Which normalized-state fields this connector retains for a TikTok Ads
# snapshot.  It is recorded beside every snapshot, never inside
# `normalized_state`, so a later reader can tell a connector state-shape change
# from a change at TikTok and recording it creates no drift of its own.  Bump it
# in the same commit as any change to what `_tiktok_snapshots` retains.
TIKTOK_ADS_STATE_SHAPE_VERSION: Final = "2026-09-09.1"
TIKTOK_ADS_STATE_SHAPE_PREFIX: Final = "TIKTOK_ADS_STATE_SHAPE_"
TIKTOK_ADS_STATE_SHAPE_STATUS: Final = (
    TIKTOK_ADS_STATE_SHAPE_PREFIX + TIKTOK_ADS_STATE_SHAPE_VERSION
)

# The exact normalized data-quality vocabulary the TikTok inventory branch
# appends to a retained snapshot.  These are the presence and enum facts the
# provider audit reads; the shared closed snapshot-state model has no field for
# them, so they are recorded as explicit statuses rather than invented state
# keys.  Every fact is stated in both directions so an absent status never has
# to be read as either "present" or "absent".
TIKTOK_ADS_OBJECTIVE_RETURNED: Final = "TIKTOK_OBJECTIVE_RETURNED"
TIKTOK_ADS_OBJECTIVE_NOT_RETURNED: Final = "TIKTOK_OBJECTIVE_NOT_RETURNED"
TIKTOK_ADS_BUDGET_MODE_DAY: Final = "TIKTOK_BUDGET_MODE_DAY"
TIKTOK_ADS_BUDGET_MODE_TOTAL: Final = "TIKTOK_BUDGET_MODE_TOTAL"
TIKTOK_ADS_BUDGET_MODE_INFINITE: Final = "TIKTOK_BUDGET_MODE_INFINITE"
TIKTOK_ADS_BUDGET_MODE_NOT_RETURNED: Final = "TIKTOK_BUDGET_MODE_NOT_RETURNED"
TIKTOK_ADS_BUDGET_AMOUNT_PRESENT: Final = "TIKTOK_BUDGET_AMOUNT_PRESENT"
TIKTOK_ADS_BUDGET_AMOUNT_ABSENT: Final = "TIKTOK_BUDGET_AMOUNT_ABSENT"
TIKTOK_ADS_BID_TYPE_RETURNED: Final = "TIKTOK_BID_TYPE_RETURNED"
TIKTOK_ADS_BID_TYPE_NOT_RETURNED: Final = "TIKTOK_BID_TYPE_NOT_RETURNED"
TIKTOK_ADS_BID_PRICE_PRESENT: Final = "TIKTOK_BID_PRICE_PRESENT"
TIKTOK_ADS_BID_PRICE_ABSENT: Final = "TIKTOK_BID_PRICE_ABSENT"
TIKTOK_ADS_OPERATION_STATUS_REVIEWED: Final = "TIKTOK_OPERATION_STATUS_REVIEWED"
TIKTOK_ADS_OPERATION_STATUS_NOT_REVIEWED: Final = "TIKTOK_OPERATION_STATUS_NOT_IN_REVIEWED_ENUM"
# The inventory read validates `page`, `page_size` and `total_number` on every
# numbered page and refuses a set that does not add up, so a read that returned
# at all proved its own total.  Only this marker is written, on the account
# snapshot; a retained snapshot from a build without the proof is recognised by
# the marker being absent, and no "not declared" literal is ever recorded.
TIKTOK_ADS_PAGINATION_TOTAL_DECLARED: Final = "TIKTOK_PAGINATION_TOTAL_DECLARED"
TIKTOK_ADS_REPORT_COMPLETE_STATUS: Final = "COMPLETE"

# TikTok's rate-limit refusal *codes* are not published on any first-party page
# this review could fetch; the documented sandbox tiering (1 QPS / 30 QPM /
# 1,000 QPD) is a limit, not a refusal vocabulary.  This map is therefore
# deliberately empty: a business code is never relabelled into a rate limit it
# was not shown to be.  Classification reads the observed HTTP status instead.
TIKTOK_ADS_RATE_LIMIT_BUSINESS_CODES: Final[dict[int, str]] = {}
TIKTOK_ADS_SANDBOX_QUOTA_NOTE: Final = (
    "TikTok documents sandbox quotas of 1 request/second, 30/minute and 1,000/day. "
    "This product does not meter, budget, or pace TikTok requests."
)


def tiktok_ads_declares_serving_intent(status: str | None) -> bool:
    """Whether a retained TikTok operation status declares the documented serving intent.

    `operation_status` is the value set on that exact object.  It is an intent,
    never an observation of delivery, and it says nothing about the object's
    parents.
    """
    return isinstance(status, str) and status.upper() == TIKTOK_ADS_SERVING_INTENT_STATUS


def tiktok_ads_operation_status_statuses(status: str | None) -> list[str]:
    """State, in both directions, whether one operation status is a reviewed value."""
    reviewed = isinstance(status, str) and status.upper() in TIKTOK_ADS_REVIEWED_OPERATION_STATUSES
    return [
        TIKTOK_ADS_OPERATION_STATUS_REVIEWED
        if reviewed
        else TIKTOK_ADS_OPERATION_STATUS_NOT_REVIEWED
    ]


def tiktok_ads_budget_mode_statuses(budget_type: str | None, budget: object) -> list[str]:
    """Presence facts for one retained budget mode and amount, in both directions."""
    mode = {
        "BUDGET_MODE_DAY": TIKTOK_ADS_BUDGET_MODE_DAY,
        "BUDGET_MODE_TOTAL": TIKTOK_ADS_BUDGET_MODE_TOTAL,
        "BUDGET_MODE_INFINITE": TIKTOK_ADS_BUDGET_MODE_INFINITE,
    }.get(budget_type or "", TIKTOK_ADS_BUDGET_MODE_NOT_RETURNED)
    amount = (
        TIKTOK_ADS_BUDGET_AMOUNT_PRESENT if budget is not None else TIKTOK_ADS_BUDGET_AMOUNT_ABSENT
    )
    return [mode, amount]


def tiktok_ads_campaign_quality_statuses(campaign: TikTokAdsCampaign) -> list[str]:
    """The presence and enum facts one retained TikTok campaign snapshot records."""
    return [
        TIKTOK_ADS_OBJECTIVE_RETURNED
        if campaign.objective is not None
        else TIKTOK_ADS_OBJECTIVE_NOT_RETURNED,
        *tiktok_ads_budget_mode_statuses(campaign.budget_type, campaign.budget),
        *tiktok_ads_operation_status_statuses(campaign.status),
    ]


def tiktok_ads_ad_group_quality_statuses(ad_group: TikTokAdsAdGroup) -> list[str]:
    """The presence and enum facts one retained TikTok ad-group snapshot records."""
    return [
        *tiktok_ads_budget_mode_statuses(ad_group.budget_type, ad_group.budget),
        TIKTOK_ADS_BID_TYPE_RETURNED
        if ad_group.bid_type is not None
        else TIKTOK_ADS_BID_TYPE_NOT_RETURNED,
        TIKTOK_ADS_BID_PRICE_PRESENT
        if ad_group.bid_amount is not None
        else TIKTOK_ADS_BID_PRICE_ABSENT,
        *tiktok_ads_operation_status_statuses(ad_group.status),
    ]


def tiktok_ads_ad_quality_statuses(ad: TikTokAdsAd) -> list[str]:
    """The facts one retained TikTok ad snapshot records.

    The reviewed `/ad/get/` projection returns identity, parents and the
    operation status only.  Creative association, video presence, ad copy and
    destination are not requested at all, so they are declared unread in
    `TIKTOK_ADS_READ_SURFACES` rather than recorded here as absent.
    """
    return [*tiktok_ads_operation_status_statuses(ad.status)]


def classify_tiktok_ads_business_code(code: object) -> AdhelmError:
    """Classify one non-zero v1.3 envelope from its integer `code` alone.

    Only `code` is read.  `message` and `request_id` are never inspected, so no
    provider response text can reach a diagnostic.  No first-party source
    enumerates TikTok's rate-limit codes, so a code this contract cannot
    classify becomes a fixed request-failed diagnostic instead of being
    relabelled into a refusal it was never shown to be.
    """
    if isinstance(code, bool) or not isinstance(code, int):
        return AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "TikTok Ads returned an unclassifiable error envelope."
        )
    reason = TIKTOK_ADS_RATE_LIMIT_BUSINESS_CODES.get(code)
    if reason is None:
        return AdhelmError(
            "PROVIDER_REQUEST_FAILED",
            "TikTok Ads rejected the fixed read with a business error code.",
            exit_code=int(ExitCode.PRECONDITION),
            next_action=(
                "Inspect the failing bounded read locally. TikTok's business error-code "
                "vocabulary is not documented locally, so this refusal is not classified "
                "further and no response text is recorded."
            ),
        )
    return _tiktok_rate_limited(reason)


def classify_tiktok_ads_transport_error(error: AdhelmError) -> AdhelmError:
    """Turn one shared-transport refusal into an exact TikTok Ads diagnostic.

    The shared transport records the observed HTTP status and, when the provider
    sent a whole-second `Retry-After`, its bounded value.  Nothing from the
    response body is available here, so no classification can leak one.  A
    status this contract does not classify is returned unchanged.
    """
    status = getattr(error, "status_code", None)
    retry_after = getattr(error, "retry_after_seconds", None)
    if status == 429:
        return _tiktok_rate_limited("HTTP_429", status_code=status, retry_after_seconds=retry_after)
    if status in {401, 403}:
        return AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "TikTok Ads denied the configured local read profile.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action=(
                "Inspect the stored credential locally with `adhelm auth tiktok-token status "
                "--profile <id>`, then re-enroll with a fresh one-hour authorization code. "
                "TikTok's app approval state and endpoint permissions are selected in My Apps "
                "and are not documented locally, so neither is checked here."
            ),
            status_code=status,
            retry_after_seconds=retry_after,
        )
    return error


def _tiktok_rate_limited(
    reason: str, *, status_code: int | None = None, retry_after_seconds: int | None = None
) -> AdhelmError:
    return AdhelmError(
        "TIKTOK_ADS_RATE_LIMITED",
        "TikTok Ads refused the fixed read for exceeding a request rate.",
        exit_code=int(ExitCode.PRECONDITION),
        next_action=(
            f"The refusal classified as {reason}; wait and run the same bounded read again. "
            f"{TIKTOK_ADS_SANDBOX_QUOTA_NOTE}"
        ),
        status_code=status_code,
        retry_after_seconds=retry_after_seconds,
    )


TokenGetter = Callable[[], str]
ReferenceFor = Callable[[Literal["advertiser", "campaign", "adgroup", "ad"], str], str]


def _default_reference_for(
    kind: Literal["advertiser", "campaign", "adgroup", "ad"], value: str
) -> str:
    return f"tiktok_ads.{kind}.{value}"


def _safe_token(getter: TokenGetter) -> str:
    value = getter()
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16_384
        or any(ord(c) < 33 for c in value)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED", "TikTok Ads authorization is unavailable.", exit_code=4
        )
    return value


def _json(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        value = parse_json(response.content, maximum_bytes=8_388_608, decimal_numbers=True)
        validate_data(value)
    except (ValueError, UnicodeDecodeError, AdhelmError) as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} response is invalid."
        ) from exc
    if not isinstance(value, dict):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} response has an invalid shape."
        )
    return value


def _keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} included an unsupported field."
        )


def _string(value: Any, context: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} is invalid.")
    try:
        validate_data(value)
    except AdhelmError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "TikTok Ads response contains prohibited data."
        ) from exc
    return value


def _id(value: Any, context: str) -> str:
    parsed = _string(value, context, maximum=40)
    if _ID.fullmatch(parsed) is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} is invalid.")
    return parsed


def _enum(value: Any, context: str) -> str:
    parsed = _string(value, context, maximum=80)
    if _ENUM.fullmatch(parsed) is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} is invalid.")
    return parsed


def _integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, str) or not value.isdecimal():
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} is invalid.")
    parsed = int(value)
    if parsed > 9_007_199_254_740_991:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} exceeds the numeric bound."
        )
    return parsed


def _currency(value: Any) -> Currency:
    parsed = _string(value, "advertiser currency", maximum=3)
    if parsed not in _CURRENCY_EXPONENTS:
        raise AdhelmError(
            "UNSUPPORTED_CURRENCY", "TikTok Ads currency has no reviewed exact conversion."
        )
    return parsed


def _money(value: Any, currency: Currency, context: str) -> Money | None:
    if value is None:
        return None
    # Numeric API fields are parsed directly from JSON into Decimal; binary
    # floating-point values never enter this conversion.
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int, Decimal))
        or len(str(value)) > 80
    ):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} lacks exact decimal precision."
        )
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} is invalid.") from exc
    exponent = _CURRENCY_EXPONENTS[currency]
    if (
        not amount.is_finite()
        or amount < 0
        or amount > Decimal(9_007_199_254_740_991) / (Decimal(10) ** exponent)
        or (amount != 0 and amount.adjusted() < -exponent)
    ):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} has unsupported precision."
        )
    with localcontext() as context_precision:
        context_precision.prec = 100
        minor = amount * (Decimal(10) ** exponent)
    if minor != minor.to_integral_value():
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} has unsupported precision."
        )
    return Money(minor_units=int(minor), currency=currency)


class TikTokAdsIdentity(StrictModel):
    schema_version: Literal[1] = 1
    account_ref: SafeRef
    advertiser_client_ref: SafeRef
    timezone: str = Field(min_length=1, max_length=80)
    currency: Currency
    status: str = Field(min_length=1, max_length=80)
    provider_api_version: Literal["v1.3"] = _API_VERSION


class TikTokAdsCampaign(StrictModel):
    campaign_ref: SafeRef
    campaign_client_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)
    objective: str | None = Field(default=None, max_length=80)
    budget_type: str | None = Field(default=None, max_length=80)
    budget: Money | None = None


class TikTokAdsAdGroup(StrictModel):
    adgroup_ref: SafeRef
    adgroup_client_ref: SafeRef
    campaign_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)
    budget_type: str | None = Field(default=None, max_length=80)
    budget: Money | None = None
    bid_type: str | None = Field(default=None, max_length=80)
    bid_amount: Money | None = None


class TikTokAdsAd(StrictModel):
    ad_ref: SafeRef
    ad_client_ref: SafeRef
    campaign_ref: SafeRef
    adgroup_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)


class TikTokAdsInventoryQuery(StrictModel):
    account_ref: SafeRef
    maximum_records: int = Field(default=500, ge=1, le=5_000)
    page_size: int = Field(default=500, ge=1, le=_MAX_PAGE_SIZE)

    @model_validator(mode="after")
    def bounded_pages(self) -> TikTokAdsInventoryQuery:
        if self.maximum_records > self.page_size * _MAX_PAGES:
            raise ValueError("TikTok Ads inventory exceeds the ten-page limit")
        return self


class TikTokAdsReportQuery(StrictModel):
    level: Literal["CAMPAIGN", "AD"] = "CAMPAIGN"
    account_ref: SafeRef
    report_id: SafeRef
    query_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_start: date
    window_end: date
    source_timezone: str = Field(min_length=1, max_length=80)
    currency: Currency | None = None
    maximum_rows: int = Field(default=2_000, ge=1, le=5_000)
    page_size: int = Field(default=500, ge=1, le=_MAX_PAGE_SIZE)

    @model_validator(mode="after")
    def bounded_window(self) -> TikTokAdsReportQuery:
        if self.window_end < self.window_start or (self.window_end - self.window_start).days > 29:
            raise ValueError("TikTok Ads daily reports may include at most 30 ordered days")
        if self.maximum_rows > self.page_size * _MAX_PAGES:
            raise ValueError("TikTok Ads report exceeds the ten-page limit")
        return self


class TikTokAdsClient:
    """Fixed v1.3 GET reads; no generic endpoint or mutation capability exists."""

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
                "TRANSPORT_CONFIG_INVALID", "TikTok Ads adapter requires its fixed API host."
            )
        self._transport, self._token_getter = transport, token_getter
        self._reference_for, self._now, self._connector_version = (
            reference_for,
            now,
            connector_version,
        )

    def discover_identity(
        self, advertiser_client_ref: str, *, expected_account_client_ref: str | None = None
    ) -> TikTokAdsIdentity:
        advertiser = _id(advertiser_client_ref, "configured advertiser ID")
        response = self._envelope(
            self._get(
                "/advertiser/info/",
                {
                    "advertiser_ids": json.dumps([advertiser]),
                    "fields": json.dumps(_IDENTITY_FIELDS),
                },
            ),
            "advertiser identity",
        )
        data = response["data"]
        _keys(data, {"list"}, "advertiser identity data")
        items = data.get("list")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "TikTok Ads identity did not return one advertiser."
            )
        item = items[0]
        _keys(item, set(_IDENTITY_FIELDS), "advertiser identity")
        returned = _id(item.get("advertiser_id"), "advertiser ID")
        if returned != advertiser or (
            expected_account_client_ref is not None
            and returned != _id(expected_account_client_ref, "expected advertiser ID")
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "TikTok Ads advertiser differs from the configured profile."
            )
        timezone = _string(item.get("timezone"), "advertiser timezone", maximum=80)
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "TikTok Ads advertiser timezone is invalid."
            ) from exc
        return TikTokAdsIdentity(
            account_ref=self._reference_for("advertiser", returned),
            advertiser_client_ref=returned,
            timezone=timezone,
            currency=_currency(item.get("currency")),
            status=_enum(item.get("status"), "advertiser status"),
        )

    def list_campaigns(
        self, query: TikTokAdsInventoryQuery, *, identity: TikTokAdsIdentity
    ) -> list[TikTokAdsCampaign]:
        return cast(list[TikTokAdsCampaign], self._inventory(query, identity, "campaign"))

    def list_adgroups(
        self, query: TikTokAdsInventoryQuery, *, identity: TikTokAdsIdentity
    ) -> list[TikTokAdsAdGroup]:
        return cast(list[TikTokAdsAdGroup], self._inventory(query, identity, "adgroup"))

    def list_ads(
        self, query: TikTokAdsInventoryQuery, *, identity: TikTokAdsIdentity
    ) -> list[TikTokAdsAd]:
        return cast(list[TikTokAdsAd], self._inventory(query, identity, "ad"))

    def run_campaign_report(
        self, query: TikTokAdsReportQuery, *, identity: TikTokAdsIdentity
    ) -> NormalizedReport:
        return self._report(query, identity, "CAMPAIGN")

    def run_ad_report(
        self, query: TikTokAdsReportQuery, *, identity: TikTokAdsIdentity
    ) -> NormalizedReport:
        return self._report(query, identity, "AD")

    def _inventory(
        self,
        query: TikTokAdsInventoryQuery,
        identity: TikTokAdsIdentity,
        kind: Literal["campaign", "adgroup", "ad"],
    ) -> list[TikTokAdsCampaign] | list[TikTokAdsAdGroup] | list[TikTokAdsAd]:
        advertiser = self._identity_matches(query.account_ref, identity)
        fields = {"campaign": _CAMPAIGN_FIELDS, "adgroup": _ADGROUP_FIELDS, "ad": _AD_FIELDS}[kind]
        collected: list[Any] = []
        page_size = min(query.page_size, query.maximum_records)
        total: int | None = None
        seen: set[str] = set()
        for page in range(1, _MAX_PAGES + 1):
            remaining = query.maximum_records - len(collected)
            if remaining <= 0:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "TikTok Ads inventory exceeds the configured complete-result bound.",
                )
            response = self._envelope(
                self._get(
                    f"/{kind}/get/",
                    {
                        "advertiser_id": advertiser,
                        "page": page,
                        "page_size": page_size,
                        "fields": json.dumps(fields, separators=(",", ":")),
                    },
                ),
                f"{kind} list",
            )
            items, has_next, total = self._page(
                response["data"], f"{kind} list", page, page_size, total
            )
            if total > query.maximum_records:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED", "TikTok inventory exceeds the result bound."
                )
            for item in items:
                if not isinstance(item, dict):
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "TikTok inventory row is invalid."
                    )
                identifier = _id(item.get(f"{kind}_id"), "inventory resource ID")
                if identifier in seen:
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "TikTok inventory repeats a resource."
                    )
                seen.add(identifier)
            if len(collected) + len(items) > query.maximum_records:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "TikTok Ads inventory exceeds the configured complete-result bound.",
                )
            collected.extend(items)
            if not has_next:
                if kind == "campaign":
                    return [self._campaign(item, identity.currency) for item in collected]
                if kind == "adgroup":
                    return [self._adgroup(item, identity.currency) for item in collected]
                return [self._ad(item) for item in collected]
            if len(collected) == query.maximum_records:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "TikTok Ads inventory exceeds the configured complete-result bound.",
                )
        raise AdhelmError(
            "PAGINATION_LIMIT", "TikTok Ads inventory exceeded the bounded page count."
        )

    def _report(
        self,
        query: TikTokAdsReportQuery,
        identity: TikTokAdsIdentity,
        level: Literal["CAMPAIGN", "AD"],
    ) -> NormalizedReport:
        advertiser = self._identity_matches(query.account_ref, identity)
        if query.level != level:
            raise AdhelmError(
                "QUERY_GRAIN_MISMATCH", "Selected report method differs from query grain."
            )
        if query.source_timezone != identity.timezone:
            raise AdhelmError(
                "TIMEZONE_MISMATCH", "TikTok Ads report timezone differs from account identity."
            )
        if query.currency is not None and query.currency != identity.currency:
            raise AdhelmError(
                "CURRENCY_MISMATCH", "TikTok Ads report currency differs from account identity."
            )
        observed = self._observed_at()
        if query.window_end > observed.astimezone(ZoneInfo(identity.timezone)).date():
            raise AdhelmError(
                "INVALID_REPORT_WINDOW", "TikTok Ads report window may not end in the future."
            )
        dimensions = _CAMPAIGN_DIMENSIONS if level == "CAMPAIGN" else _AD_DIMENSIONS
        data_level = "AUCTION_CAMPAIGN" if level == "CAMPAIGN" else "AUCTION_AD"
        rows: list[PerformanceRow] = []
        page_size = min(query.page_size, query.maximum_rows)
        total: int | None = None
        seen: set[tuple[str, str | None]] = set()
        for page in range(1, _MAX_PAGES + 1):
            remaining = query.maximum_rows - len(rows)
            if remaining <= 0:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "TikTok Ads report exceeds the configured complete-result bound.",
                )
            params: dict[str, Any] = {
                "advertiser_id": advertiser,
                "service_type": "AUCTION",
                "report_type": "BASIC",
                "data_level": data_level,
                "dimensions": json.dumps(dimensions, separators=(",", ":")),
                "metrics": json.dumps(_METRICS, separators=(",", ":")),
                "filtering": json.dumps(
                    [
                        {
                            "field_name": "campaign_status" if level == "CAMPAIGN" else "ad_status",
                            "filter_type": "IN",
                            "filter_value": json.dumps(["STATUS_ALL"], separators=(",", ":")),
                        }
                    ],
                    separators=(",", ":"),
                ),
                "start_date": query.window_start.isoformat(),
                "end_date": query.window_end.isoformat(),
                "page": page,
                "page_size": page_size,
            }
            response = self._envelope(
                self._get("/report/integrated/get/", params), f"{level.lower()} report"
            )
            items, has_next, total = self._page(
                response["data"], f"{level.lower()} report", page, page_size, total
            )
            if total > query.maximum_rows:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED", "TikTok report exceeds the result bound."
                )
            parsed = [self._report_row(item, query, identity, level) for item in items]
            for row in parsed:
                key = (row.resource_ref, row.dimension_value)
                if key in seen:
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "TikTok report repeats a resource day."
                    )
                seen.add(key)
            if len(rows) + len(parsed) > query.maximum_rows:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "TikTok Ads report exceeds the configured complete-result bound.",
                )
            rows.extend(parsed)
            if not has_next:
                return NormalizedReport(
                    envelope=ReportEnvelope(
                        report_id=query.report_id,
                        provider="tiktok_ads",
                        source_account_ref=query.account_ref,
                        query_contract_hash=query.query_contract_hash,
                        window_start=query.window_start,
                        window_end=query.window_end,
                        source_timezone=identity.timezone,
                        report_timezone=identity.timezone,
                        currency=identity.currency,
                        attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
                        attribution_window=None,
                        observed_at=observed,
                        data_fresh_through=None,
                        data_quality_statuses=["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"],
                        provider_api_version=_API_VERSION,
                        connector_version=self._connector_version,
                    ),
                    performance_rows=rows,
                )
            if len(rows) == query.maximum_rows:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "TikTok Ads report exceeds the configured complete-result bound.",
                )
        raise AdhelmError("PAGINATION_LIMIT", "TikTok Ads report exceeded the bounded page count.")

    def _campaign(self, item: Any, currency: Currency) -> TikTokAdsCampaign:
        if not isinstance(item, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "TikTok Ads campaign is invalid.")
        _keys(item, set(_CAMPAIGN_FIELDS), "campaign")
        identifier = _id(item.get("campaign_id"), "campaign ID")
        return TikTokAdsCampaign(
            campaign_ref=self._reference_for("campaign", identifier),
            campaign_client_ref=identifier,
            status=_enum(item.get("operation_status"), "campaign status"),
            objective=None
            if item.get("objective_type") is None
            else _enum(item["objective_type"], "campaign objective"),
            budget_type=None
            if item.get("budget_mode") is None
            else _enum(item["budget_mode"], "campaign budget type"),
            budget=_money(item.get("budget"), currency, "campaign budget"),
        )

    def _adgroup(self, item: Any, currency: Currency) -> TikTokAdsAdGroup:
        if not isinstance(item, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "TikTok Ads ad group is invalid.")
        _keys(item, set(_ADGROUP_FIELDS), "ad group")
        identifier = _id(item.get("adgroup_id"), "ad group ID")
        return TikTokAdsAdGroup(
            adgroup_ref=self._reference_for("adgroup", identifier),
            adgroup_client_ref=identifier,
            campaign_ref=self._reference_for(
                "campaign", _id(item.get("campaign_id"), "ad group campaign ID")
            ),
            status=_enum(item.get("operation_status"), "ad group status"),
            budget_type=None
            if item.get("budget_mode") is None
            else _enum(item["budget_mode"], "ad group budget type"),
            budget=_money(item.get("budget"), currency, "ad group budget"),
            bid_type=None
            if item.get("bid_type") is None
            else _enum(item["bid_type"], "ad group bid type"),
            bid_amount=_money(item.get("bid_price"), currency, "ad group bid"),
        )

    def _ad(self, item: Any) -> TikTokAdsAd:
        if not isinstance(item, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "TikTok Ads ad is invalid.")
        _keys(item, set(_AD_FIELDS), "ad")
        identifier = _id(item.get("ad_id"), "ad ID")
        return TikTokAdsAd(
            ad_ref=self._reference_for("ad", identifier),
            ad_client_ref=identifier,
            campaign_ref=self._reference_for(
                "campaign", _id(item.get("campaign_id"), "ad campaign ID")
            ),
            adgroup_ref=self._reference_for("adgroup", _id(item.get("adgroup_id"), "ad group ID")),
            status=_enum(item.get("operation_status"), "ad status"),
        )

    def _report_row(
        self,
        item: Any,
        query: TikTokAdsReportQuery,
        identity: TikTokAdsIdentity,
        level: Literal["CAMPAIGN", "AD"],
    ) -> PerformanceRow:
        if not isinstance(item, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "TikTok Ads report row is invalid.")
        _keys(item, {"dimensions", "metrics"}, "report row")
        dimensions, metrics = item.get("dimensions"), item.get("metrics")
        if not isinstance(dimensions, dict) or not isinstance(metrics, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "TikTok Ads report row is invalid.")
        allowed_dimensions = set(_CAMPAIGN_DIMENSIONS if level == "CAMPAIGN" else _AD_DIMENSIONS)
        _keys(dimensions, allowed_dimensions, "report dimensions")
        _keys(metrics, set(_METRICS), "report metrics")
        try:
            raw_day = _string(dimensions.get("stat_time_day"), "report day", maximum=10)
            day = date.fromisoformat(raw_day)
            if raw_day != day.isoformat():
                raise ValueError("Report date must have canonical calendar form")
        except ValueError as exc:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "TikTok Ads report day is invalid."
            ) from exc
        if not query.window_start <= day <= query.window_end:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "TikTok Ads report row is outside the requested window.",
            )
        common: dict[str, Any] = {
            "report_id": query.report_id,
            "dimension_type": "DATE",
            "dimension_value": day.isoformat(),
            "impressions": _integer(metrics.get("impressions"), "report impressions"),
            "clicks": _integer(metrics.get("clicks"), "report clicks"),
            "spend": _money(metrics.get("spend"), identity.currency, "report spend"),
        }
        if level == "CAMPAIGN":
            campaign = _id(dimensions.get("campaign_id"), "report campaign ID")
            return PerformanceRow(
                **common,
                campaign_ref=self._reference_for("campaign", campaign),
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="TIKTOK_ADS_CAMPAIGN",
                resource_ref=self._reference_for("campaign", campaign),
                data_quality_statuses=["COMPLETE"],
            )
        ad = _id(dimensions.get("ad_id"), "report ad ID")
        return PerformanceRow(
            **common,
            resource_level="AD",
            metric_grain="AD",
            resource_type="TIKTOK_ADS_AD",
            resource_ref=self._reference_for("ad", ad),
            data_quality_statuses=["COMPLETE", "CREATIVE_NOT_EXPOSED", "COMBINATION_UNKNOWN"],
        )

    def _envelope(self, response: httpx.Response, context: str) -> dict[str, Any]:
        value = _json(response, context)
        _keys(value, {"code", "message", "request_id", "data"}, context)
        code = value.get("code")
        if type(code) is int and code != 0:
            # A declared business failure is classified from its integer code
            # alone.  `message` is never read, so no provider text can reach a
            # local diagnostic.
            diagnostic = classify_tiktok_ads_business_code(code)
            if 0 <= code <= 2_147_483_647:
                diagnostic.provider_code = str(code)
            raise diagnostic
        if (
            type(code) is not int
            or code != 0
            or not isinstance(value.get("message"), str)
            or not isinstance(value.get("request_id"), str)
            or not isinstance(value.get("data"), dict)
        ):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} failed or is invalid."
            )
        return value

    @staticmethod
    def _page(
        data: dict[str, Any], context: str, page: int, page_size: int, prior_total: int | None
    ) -> tuple[list[Any], bool, int]:
        _keys(data, {"list", "page_info"}, context)
        items, info = data.get("list"), data.get("page_info")
        if not isinstance(items, list) or len(items) > _MAX_PAGE_SIZE or not isinstance(info, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} page is invalid.")
        _keys(info, {"page", "page_size", "total_number", "total_page"}, f"{context} page")
        values = [info.get(key) for key in ("page", "page_size", "total_number", "total_page")]
        if (
            any(isinstance(v, bool) or not isinstance(v, int) for v in values)
            or info["page"] != page
            or info["page_size"] != page_size
            or not 0 <= info["total_page"] <= _MAX_PAGES
            or not 0 <= info["total_number"] <= 5_000
        ):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} page is invalid.")
        total = cast(int, info["total_number"])
        total_pages = (total + page_size - 1) // page_size
        if (
            (prior_total is not None and total != prior_total)
            or (total and (info["total_page"] != total_pages or page > total_pages))
            or (not total and (page != 1 or info["total_page"] not in (0, 1)))
            or len(items) != min(page_size, max(0, total - (page - 1) * page_size))
        ):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", f"TikTok Ads {context} pagination is inconsistent."
            )
        return items, page < total_pages, total

    def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        try:
            return self._transport.request(
                "GET",
                f"https://{_HOST}/open_api/{_API_VERSION}{path}",
                headers={"Access-Token": _safe_token(self._token_getter)},
                params=params,
            )
        except AdhelmError as error:
            # Classification is wired into the client, not merely defined beside
            # it: a `429` or a denial reaches the caller as the exact TikTok
            # diagnostic.  Only the observed status and a bounded `Retry-After`
            # are read; no response body is available here.
            raise copy_provider_diagnostics(
                error, classify_tiktok_ads_transport_error(error)
            ) from None

    def _identity_matches(self, account_ref: str, identity: TikTokAdsIdentity) -> str:
        # Typed identities may be supplied directly; SafeRef is not an advertiser-ID type.
        advertiser = _id(identity.advertiser_client_ref, "identity advertiser ID")
        if identity.account_ref != self._reference_for("advertiser", advertiser):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "TikTok Ads identity reference differs from its advertiser ID."
            )
        if account_ref != identity.account_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "TikTok Ads request differs from account identity."
            )
        return advertiser

    def _observed_at(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise AdhelmError(
                "CLOCK_INVALID", "TikTok Ads observation clock must include a timezone."
            )
        return value
