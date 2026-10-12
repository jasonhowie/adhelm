"""Fixed, read-only Meta Marketing API inventory and aggregate reporting.

Only documented Graph v26.0 GET endpoints are used.  Provider responses are
parsed in memory into closed models and never retained as raw payloads.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Final, Literal, NamedTuple, cast
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field, model_validator

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError, ExitCode, copy_provider_diagnostics
from adhelm.models import Currency, Money, SafeRef, StrictModel
from adhelm.privacy import validate_data
from adhelm.reporting import NormalizedReport, PerformanceRow, QualityStatus, ReportEnvelope
from adhelm.transport import SafeHttpClient

# The one fixed host and the one pinned Graph version every Meta read in this
# build sends.  Nothing else in the product may spell either of them out, so
# the product can never send two Graph versions at once.
META_ADS_API_HOST: Final = "graph.facebook.com"
META_ADS_API_VERSION: Final = "v26.0"

_HOST = META_ADS_API_HOST
_API_VERSION: Literal["v26.0"] = META_ADS_API_VERSION
_MAX_PAGES = 10
_MAX_PAGE_SIZE = 500
_ACCOUNT_FIELDS = "id,account_status,currency,timezone_name"
_CAMPAIGN_FIELDS = (
    "id,effective_status,objective,buying_type,bid_strategy,"
    "daily_budget,lifetime_budget,spend_cap,start_time,stop_time"
)
_ADSET_FIELDS = (
    "id,campaign_id,effective_status,billing_event,bid_amount,bid_strategy,"
    "daily_budget,lifetime_budget,start_time,end_time,targeting"
)
# `creative{...}` is a field-expansion of the ad's associated creative.  Only
# the association identifier and two presence-bearing fields are requested:
# `object_story_spec`, `title`, and `body` carry ad copy and are deliberately
# never requested, so no copy text can reach this process at all.
_AD_FIELDS = "id,campaign_id,adset_id,effective_status,creative{id,image_hash,link_url,object_url}"
_CAMPAIGN_INSIGHT_FIELDS = "campaign_id,date_start,date_stop,impressions,clicks,spend,reach"
_AD_INSIGHT_FIELDS = (
    "campaign_id,adset_id,ad_id,date_start,date_stop,impressions,clicks,spend,reach"
)
_CURRENCY_EXPONENTS = {
    "AUD": 2,
    "CAD": 2,
    "CHF": 2,
    "DKK": 2,
    "EUR": 2,
    "GBP": 2,
    "HKD": 2,
    "JPY": 0,
    "MXN": 2,
    "NOK": 2,
    "NZD": 2,
    "SEK": 2,
    "SGD": 2,
    "USD": 2,
}
_CURSOR = re.compile(r"^[A-Za-z0-9_~+/=-]{1,4096}$")
# `str.isdecimal`, `int` and `Decimal` all accept non-ASCII digits, signs,
# whitespace, digit-group underscores and exponents.  Every number and date
# this adapter reads is matched against an ASCII-only grammar first, so a
# lookalike never reaches a request path, a money amount or a window check.
_DECIMAL_AMOUNT = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_MAX_INTEGER_DIGITS = len("9007199254740991")
_ISO_DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# --------------------------------------------------------------------------
# What this build's fixed Meta reads actually request, per audit-relevant
# surface.  A provider audit reads this map instead of guessing what a missing
# value means: a surface that is not requested is unread, never healthy.
#
# Every entry below was checked against the Meta-published reference pages
# fetched on 2026-09-09; `docs/research/meta-ads-read-contract.md` records the
# exact URLs and what each one returned.
# --------------------------------------------------------------------------
MetaAdsReadSurfaceStatus = Literal[
    "IMPLEMENTED_FIXED_READ",
    "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "NOT_IN_REVIEWED_FIXED_READ",
    "NOT_DOCUMENTED",
]
META_ADS_READ_SURFACES: Final[dict[str, MetaAdsReadSurfaceStatus]] = {
    "ACCOUNT_IDENTITY": "IMPLEMENTED_FIXED_READ",
    "ACCOUNT_STATUS": "IMPLEMENTED_FIXED_READ",
    "ACCOUNT_REVIEW_STATE": "NOT_IN_REVIEWED_FIXED_READ",
    "CAMPAIGN_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "AD_SET_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "AD_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_OBJECTIVE": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_BUYING_TYPE": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_BID_STRATEGY": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_BUDGET": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_SPEND_CAP": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_SCHEDULE": "IMPLEMENTED_FIXED_READ",
    "AD_SET_BUDGET_MODE": "IMPLEMENTED_FIXED_READ",
    "AD_SET_SCHEDULE": "IMPLEMENTED_FIXED_READ",
    "AD_SET_BILLING_EVENT": "IMPLEMENTED_FIXED_READ",
    "AD_SET_BID_AMOUNT": "IMPLEMENTED_FIXED_READ",
    "AD_SET_BID_STRATEGY": "IMPLEMENTED_FIXED_READ",
    "AD_SET_TARGETING_PRESENCE": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "AD_SET_TARGETING_VALUES": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_SET_PLACEMENT": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_CREATIVE_ASSOCIATION": "IMPLEMENTED_FIXED_READ",
    "AD_CREATIVE_IMAGE_HASH_PRESENCE": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "AD_CREATIVE_LINK_PRESENCE": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "AD_CREATIVE_COPY_TEXT": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_CREATIVE_IMAGE_BYTES": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_CREATIVE_LINK_VALUE": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_EFFECTIVE_STATUS": "IMPLEMENTED_FIXED_READ",
    "AD_REVIEW_STATE": "IMPLEMENTED_FIXED_READ",
    "AD_REVIEW_FEEDBACK": "NOT_IN_REVIEWED_FIXED_READ",
    "CAMPAIGN_DELIVERY_REPORT": "IMPLEMENTED_FIXED_READ",
    "AD_DELIVERY_REPORT": "IMPLEMENTED_FIXED_READ",
    "INSIGHTS_ATTRIBUTION_WINDOW": "IMPLEMENTED_FIXED_READ",
    "INSIGHTS_PAGINATION": "IMPLEMENTED_FIXED_READ",
    "INVENTORY_PAGINATION": "IMPLEMENTED_FIXED_READ",
    "NATIVE_CONVERSION_METRICS": "NOT_IN_REVIEWED_FIXED_READ",
    "PIXEL_AND_EVENT_HEALTH": "NOT_IN_REVIEWED_FIXED_READ",
    "DESTINATION_URL_VALUE": "NOT_IN_REVIEWED_FIXED_READ",
    "REPORT_FRESHNESS": "NOT_IN_REVIEWED_FIXED_READ",
}

# The documented `effective_status` enumerations, per node.  A retained value
# outside its node's set is reported as unreviewed; the audit never guesses
# what it means.
META_ADS_REVIEWED_CAMPAIGN_STATUSES: Final[tuple[str, ...]] = (
    "ACTIVE",
    "PAUSED",
    "DELETED",
    "ARCHIVED",
    "IN_PROCESS",
    "WITH_ISSUES",
)
META_ADS_REVIEWED_AD_SET_STATUSES: Final[tuple[str, ...]] = (
    "ACTIVE",
    "PAUSED",
    "DELETED",
    "CAMPAIGN_PAUSED",
    "ARCHIVED",
    "IN_PROCESS",
    "WITH_ISSUES",
)
META_ADS_REVIEWED_AD_STATUSES: Final[tuple[str, ...]] = (
    "ACTIVE",
    "PAUSED",
    "DELETED",
    "PENDING_REVIEW",
    "DISAPPROVED",
    "PREAPPROVED",
    "PENDING_BILLING_INFO",
    "CAMPAIGN_PAUSED",
    "ARCHIVED",
    "ADSET_PAUSED",
    "IN_PROCESS",
    "WITH_ISSUES",
)
# The one documented effective status that declares an intent to serve.  Meta
# documents `effective_status` as already folding a parent's status in, so an
# `ACTIVE` child is not contradicted by a paused parent; a declared status is
# still an intent, never an observation of delivery.
META_ADS_SERVING_INTENT_STATUS: Final = "ACTIVE"
# Documented ad statuses that describe where the ad sits in Meta's review, and
# the ones that describe an account/billing condition needing attention.
META_ADS_REVIEW_PENDING_STATUSES: Final[tuple[str, ...]] = ("PENDING_REVIEW", "PREAPPROVED")
META_ADS_REVIEW_BLOCKING_STATUSES: Final[tuple[str, ...]] = ("DISAPPROVED",)
META_ADS_ATTENTION_STATUSES: Final[tuple[str, ...]] = ("WITH_ISSUES", "PENDING_BILLING_INFO")
META_ADS_REVIEWED_BUYING_TYPES: Final[tuple[str, ...]] = ("AUCTION", "RESERVED")
META_ADS_REVIEWED_BID_STRATEGIES: Final[tuple[str, ...]] = (
    "LOWEST_COST_WITHOUT_CAP",
    "LOWEST_COST_WITH_BID_CAP",
    "COST_CAP",
    "LOWEST_COST_WITH_MIN_ROAS",
)
META_ADS_REVIEWED_OBJECTIVES: Final[tuple[str, ...]] = (
    "APP_INSTALLS",
    "BRAND_AWARENESS",
    "CONVERSIONS",
    "EVENT_RESPONSES",
    "LEAD_GENERATION",
    "LINK_CLICKS",
    "LOCAL_AWARENESS",
    "MESSAGES",
    "OFFER_CLAIMS",
    "OUTCOME_APP_PROMOTION",
    "OUTCOME_AWARENESS",
    "OUTCOME_ENGAGEMENT",
    "OUTCOME_LEADS",
    "OUTCOME_SALES",
    "OUTCOME_TRAFFIC",
    "PAGE_LIKES",
    "POST_ENGAGEMENT",
    "PRODUCT_CATALOG_SALES",
    "REACH",
    "STORE_VISITS",
    "VIDEO_VIEWS",
)
# The only attribution metadata this read has: exactly the windows the caller
# asked for.  Meta returns no attribution declaration of its own in the fixed
# insights fields, so the envelope restates the request and nothing more.
META_ADS_ATTRIBUTION_MODEL: Final = "META_ACTION_ATTRIBUTION_WINDOWS"
META_ADS_SUPPORTED_ATTRIBUTION_WINDOWS: Final[tuple[str, ...]] = (
    "1d_click",
    "7d_click",
    "1d_view",
)
META_ADS_REPORT_COMPLETE_STATUS: Final = "COMPLETE"

# The creative fields whose values are screened one resource at a time rather
# than by the whole-read privacy screen.  They are presence-only facts that
# never enter a record, and an ordinary advertiser landing page can legitimately
# carry a contact address, so one of them must not be able to void a whole
# bounded ad read.
_SCREENED_CREATIVE_FIELDS: Final[frozenset[str]] = frozenset(
    {"image_hash", "link_url", "object_url"}
)

# Which normalized-state fields this connector retains for a Meta Ads snapshot.
# It is recorded beside every snapshot so that a later reader can tell a
# connector state-shape change from a change at Meta.  Bump it in the same
# commit as any change to what `_meta_snapshots` puts in `normalized_state`;
# the retained value never enters `normalized_state` itself, so recording it
# creates no drift of its own.
META_ADS_STATE_SHAPE_VERSION: Final = "2026-09-09.1"
META_ADS_STATE_SHAPE_PREFIX: Final = "META_ADS_STATE_SHAPE_"
META_ADS_STATE_SHAPE_STATUS: Final = META_ADS_STATE_SHAPE_PREFIX + META_ADS_STATE_SHAPE_VERSION

# The exact normalized data-quality vocabulary the Meta inventory branch
# appends to a retained snapshot.  These are the presence and enum facts the
# provider audit reads; the shared closed snapshot-state model has no field for
# them, so they are recorded as explicit statuses rather than invented state
# keys.  Every fact is stated in both directions so an absent status never has
# to be read as either "present" or "absent".
META_ADS_OBJECTIVE_RETURNED: Final = "META_OBJECTIVE_RETURNED"
META_ADS_OBJECTIVE_NOT_RETURNED: Final = "META_OBJECTIVE_NOT_RETURNED"
META_ADS_OBJECTIVE_NOT_REVIEWED: Final = "META_OBJECTIVE_NOT_IN_REVIEWED_ENUM"
META_ADS_BUYING_TYPE_RETURNED: Final = "META_BUYING_TYPE_RETURNED"
META_ADS_BUYING_TYPE_NOT_RETURNED: Final = "META_BUYING_TYPE_NOT_RETURNED"
META_ADS_BUYING_TYPE_NOT_REVIEWED: Final = "META_BUYING_TYPE_NOT_IN_REVIEWED_ENUM"
META_ADS_BUYING_TYPE_RESERVED: Final = "META_BUYING_TYPE_RESERVED"
META_ADS_BID_STRATEGY_RETURNED: Final = "META_BID_STRATEGY_RETURNED"
META_ADS_BID_STRATEGY_NOT_RETURNED: Final = "META_BID_STRATEGY_NOT_RETURNED"
META_ADS_BID_STRATEGY_NOT_REVIEWED: Final = "META_BID_STRATEGY_NOT_IN_REVIEWED_ENUM"
META_ADS_TARGETING_DECLARED: Final = "META_TARGETING_DECLARED"
META_ADS_TARGETING_NOT_DECLARED: Final = "META_TARGETING_NOT_DECLARED"
META_ADS_TARGETING_GEO_PRESENT: Final = "META_TARGETING_GEO_PRESENT"
META_ADS_TARGETING_GEO_ABSENT: Final = "META_TARGETING_GEO_ABSENT"
META_ADS_TARGETING_AGE_PRESENT: Final = "META_TARGETING_AGE_PRESENT"
META_ADS_TARGETING_AGE_ABSENT: Final = "META_TARGETING_AGE_ABSENT"
META_ADS_TARGETING_INTEREST_PRESENT: Final = "META_TARGETING_INTEREST_PRESENT"
META_ADS_TARGETING_INTEREST_ABSENT: Final = "META_TARGETING_INTEREST_ABSENT"
META_ADS_BUDGET_MODE_DAILY: Final = "META_BUDGET_MODE_DAILY"
META_ADS_BUDGET_MODE_LIFETIME: Final = "META_BUDGET_MODE_LIFETIME"
META_ADS_BUDGET_MODE_DAILY_AND_LIFETIME: Final = "META_BUDGET_MODE_DAILY_AND_LIFETIME"
META_ADS_BUDGET_MODE_NOT_RETURNED: Final = "META_BUDGET_MODE_NOT_RETURNED"
META_ADS_SCHEDULE_END_PRESENT: Final = "META_SCHEDULE_END_PRESENT"
META_ADS_SCHEDULE_END_ABSENT: Final = "META_SCHEDULE_END_ABSENT"
META_ADS_CREATIVE_ASSOCIATED: Final = "META_CREATIVE_ASSOCIATED"
META_ADS_CREATIVE_NOT_ASSOCIATED: Final = "META_CREATIVE_NOT_ASSOCIATED"
META_ADS_CREATIVE_IMAGE_HASH_PRESENT: Final = "META_CREATIVE_IMAGE_HASH_PRESENT"
META_ADS_CREATIVE_IMAGE_HASH_ABSENT: Final = "META_CREATIVE_IMAGE_HASH_ABSENT"
META_ADS_CREATIVE_LINK_PRESENT: Final = "META_CREATIVE_LINK_PRESENT"
META_ADS_CREATIVE_LINK_ABSENT: Final = "META_CREATIVE_LINK_ABSENT"
META_ADS_CREATIVE_VALUE_WITHHELD: Final = "META_CREATIVE_VALUE_WITHHELD_BY_LOCAL_SCREEN"
META_ADS_CREATIVE_VALUE_NOT_WITHHELD: Final = "META_CREATIVE_VALUE_NOT_WITHHELD"
META_ADS_AD_REVIEW_PENDING: Final = "META_AD_REVIEW_PENDING"
META_ADS_AD_REVIEW_BLOCKED: Final = "META_AD_REVIEW_BLOCKED"
META_ADS_AD_REVIEW_ATTENTION: Final = "META_AD_STATUS_NEEDS_ATTENTION"
META_ADS_AD_REVIEW_NOT_FLAGGED: Final = "META_AD_REVIEW_NOT_FLAGGED"

# The documented Graph error codes that mean "you are being rate limited".
# Classification reads the integer code only; no `message`, `error_user_title`,
# `error_user_msg`, or trace value is ever read, so no response text can reach
# a diagnostic.
META_ADS_RATE_LIMIT_ERROR_CODES: Final[dict[int, str]] = {
    4: "APPLICATION_REQUEST_LIMIT",
    17: "USER_REQUEST_LIMIT",
    32: "PAGE_REQUEST_LIMIT",
    613: "CUSTOM_RATE_LIMIT",
}
META_ADS_RATE_LIMIT_HINT_NOT_DISCLOSED: Final = "NOT_DISCLOSED"
# `X-Business-Use-Case-Usage` documents `estimated_time_to_regain_access` in
# whole minutes.  Only a bare ASCII-decimal header value is read, and it is
# clamped, so a hostile or structured header can never become an unbounded
# wait instruction or a parse failure.
_MAXIMUM_DISCLOSED_REGAIN_MINUTES: Final = 1_440


def meta_ads_declares_serving_intent(status: str | None) -> bool:
    """Whether a retained Meta effective status declares the documented serving intent."""
    return isinstance(status, str) and status.upper() == META_ADS_SERVING_INTENT_STATUS


def meta_ads_rate_limit_hint(value: str | None) -> int | str:
    """Read a bounded whole-minute regain hint, or say it was not disclosed.

    The shared transport rebuilds every response with a fixed content type and
    drops provider headers, so this build never actually observes the header on
    a completed read.  The parser exists so that a caller that does hold one --
    a future transport change, or a test -- gets the same conservative bound
    rather than a second, looser interpretation.  A structured (JSON) header
    value is deliberately *not* parsed: it is reported as not disclosed.
    """
    if (
        value is None
        or not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or len(value) > 12
    ):
        return META_ADS_RATE_LIMIT_HINT_NOT_DISCLOSED
    return min(int(value), _MAXIMUM_DISCLOSED_REGAIN_MINUTES)


def _rate_limited(
    reason: str,
    *,
    status_code: int | None = None,
    retry_after_seconds: int | None = None,
    business_use_case_usage: str | None = None,
) -> AdhelmError:
    hint = meta_ads_rate_limit_hint(business_use_case_usage)
    disclosed = (
        f"Meta disclosed an estimated {hint} minute(s) until access is regained."
        if isinstance(hint, int)
        else "Meta disclosed no bounded whole-minute regain estimate to this read."
    )
    return AdhelmError(
        "META_ADS_RATE_LIMITED",
        "Meta Ads refused the fixed read for exceeding a documented request rate.",
        exit_code=int(ExitCode.PRECONDITION),
        next_action=(
            f"{disclosed} The refusal classified as {reason}; wait and run the same "
            "bounded read again. This product does not meter, budget, or pace Graph requests."
        ),
        status_code=status_code,
        retry_after_seconds=retry_after_seconds,
    )


def classify_meta_ads_graph_error(
    code: object,
    *,
    business_use_case_usage: str | None = None,
) -> AdhelmError:
    """Classify one Graph error envelope from its integer code alone.

    Only `error.code` is read.  A code this contract does not classify becomes
    a fixed invalid-response diagnostic rather than being relabelled into a
    rate limit it was never shown to be.
    """
    if isinstance(code, bool) or not isinstance(code, int):
        return AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "Meta Ads returned an unclassifiable error envelope."
        )
    reason = META_ADS_RATE_LIMIT_ERROR_CODES.get(code)
    if reason is None:
        return AdhelmError(
            "PROVIDER_REQUEST_FAILED", "Meta Ads rejected the fixed read with a Graph error."
        )
    return _rate_limited(reason, business_use_case_usage=business_use_case_usage)


def classify_meta_ads_transport_error(
    error: AdhelmError, *, business_use_case_usage: str | None = None
) -> AdhelmError:
    """Turn one shared-transport refusal into an exact Meta Ads diagnostic.

    The shared transport records the observed HTTP status and, when the
    provider sent a whole-second `Retry-After`, its bounded value.  Nothing
    from the response body is available here, so no classification can leak
    one.  A status this contract does not classify is returned unchanged.
    """
    status = getattr(error, "status_code", None)
    retry_after = getattr(error, "retry_after_seconds", None)
    if status == 429:
        return _rate_limited(
            "HTTP_429",
            status_code=status,
            retry_after_seconds=retry_after,
            business_use_case_usage=business_use_case_usage,
        )
    if status in {401, 403}:
        return AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "Meta Ads denied the configured local read profile.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action=(
                "Inspect the configured Meta token locally with `adhelm auth meta-token inspect "
                "--profile <id>`, then reauthorize a token that still carries `ads_read` for "
                "this ad account. Meta's app review and Business Verification state is not "
                "documented locally and is not checked here."
            ),
            status_code=status,
            retry_after_seconds=retry_after,
        )
    return error


TokenGetter = Callable[[], str]
ReferenceFor = Callable[[Literal["account", "campaign", "adset", "ad"], str], str]


def _default_reference_for(kind: Literal["account", "campaign", "adset", "ad"], value: str) -> str:
    return f"meta_ads.{kind}.{value}"


def _safe_token(getter: TokenGetter) -> str:
    token = getter()
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 16_384
        or any(ord(char) < 33 for char in token)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "Meta Ads authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local Meta Ads read profile with ads_read.",
        )
    return token


def _json(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        value = parse_json(response.content, maximum_bytes=8_388_608)
    except (ValueError, UnicodeDecodeError, AdhelmError) as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} response is not JSON."
        ) from exc
    if not isinstance(value, dict):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} response has an invalid shape."
        )
    if "error" in value:
        # Read the integer code and nothing else.  `message`, `error_user_msg`,
        # `error_user_title`, and `fbtrace_id` are provider text and are never
        # read, logged, or interpolated into a diagnostic.
        envelope = value.get("error")
        code = envelope.get("code") if isinstance(envelope, dict) else None
        diagnostic = classify_meta_ads_graph_error(code)
        if type(code) is int and 0 <= code <= 2_147_483_647:
            diagnostic.provider_code = str(code)
        raise diagnostic
    try:
        # The creative's three value-bearing fields are removed before the
        # whole-read screen and screened individually in `_creative_presence`.
        # An advertiser landing page legitimately carries a contact address in
        # its query string, and voiding the entire fixed ad read over one such
        # URL would take a whole account's inventory offline for ordinary,
        # legitimate content this product never retains anyway.  Everything
        # else -- including the whole targeting object, where an uploaded
        # audience really could carry a person-level value -- is still screened
        # here and still fails the read closed.
        validate_data(_without_screened_creative_values(value))
    except AdhelmError as exc:
        raise _withheld_by_local_screen(context) from exc
    return value


def _without_screened_creative_values(value: dict[str, Any]) -> dict[str, Any]:
    """A shallow copy of one page with the per-resource screened fields removed."""
    rows = value.get("data")
    if not isinstance(rows, list) or not any(
        isinstance(row, dict) and isinstance(row.get("creative"), dict) for row in rows
    ):
        return value
    return {
        **value,
        "data": [
            {
                **row,
                "creative": {
                    key: item
                    for key, item in row["creative"].items()
                    if key not in _SCREENED_CREATIVE_FIELDS
                },
            }
            if isinstance(row, dict) and isinstance(row.get("creative"), dict)
            else row
            for row in rows
        ],
    }


def _withheld_by_local_screen(context: str) -> AdhelmError:
    """One local screening refusal that does not blame the provider for it.

    The screen is this product's own fail-closed rule, not a Meta error, and
    the diagnostic names the read that stopped and what to do next without
    naming, quoting, or hashing the value that matched.
    """
    return AdhelmError(
        "META_ADS_RESPONSE_WITHHELD_BY_LOCAL_PRIVACY_SCREEN",
        f"A local privacy screen matched a value in the Meta Ads {context} response.",
        exit_code=int(ExitCode.PRECONDITION),
        next_action=(
            f"This is a local screening decision, not a Meta error, and the {context} read was "
            "stopped before any record was written. Review that surface in Meta Ads Manager; "
            "person-level values such as uploaded audience members are refused by design and "
            "the matched value is never named, quoted, or retained here."
        ),
    )


def _report_bound_refusal() -> AdhelmError:
    """The one refusal for a report that would pass its complete-result bound."""
    return AdhelmError(
        "RESULT_LIMIT_REQUIRED",
        "Meta Ads report exceeds the configured complete-result bound.",
        next_action=(
            "Narrow the report window, for example to one day per report. A report that "
            "reaches its bound is refused and never returned truncated."
        ),
    )


def _keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} included an unsupported field."
        )


def _string(value: Any, context: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} is invalid.")
    try:
        validate_data(value)
    except AdhelmError as exc:
        raise _withheld_by_local_screen(context) from exc
    return value


def _id(value: Any, context: str) -> str:
    parsed = _string(value, context, maximum=40)
    if not (parsed.isascii() and parsed.isdecimal()) or parsed.startswith("0"):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} is invalid.")
    return parsed


def _account_id(value: Any, context: str) -> str:
    parsed = _string(value, context, maximum=44)
    if parsed.startswith("act_"):
        parsed = parsed[4:]
    return f"act_{_id(parsed, context)}"


def _integer(value: Any, context: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, str)
        or not (value.isascii() and value.isdecimal())
    ):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} is invalid.")
    # Refuse before converting: `int` raises an unclassified `ValueError` for a
    # digit string over the interpreter's 4300-digit limit, and the bound below
    # has 16 digits, so a longer string can only be over it.
    if len(value) > _MAX_INTEGER_DIGITS:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} exceeds the numeric bound."
        )
    parsed = int(value)
    if parsed > 9_007_199_254_740_991:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} exceeds the numeric bound."
        )
    return parsed


def _currency(value: Any) -> Currency:
    parsed = _string(value, "account currency", maximum=3)
    if parsed not in _CURRENCY_EXPONENTS:
        raise AdhelmError(
            "UNSUPPORTED_CURRENCY",
            "Meta Ads currency has no reviewed exact decimal conversion.",
        )
    return parsed


def _minor_units(value: Any, currency: Currency, context: str) -> Money:
    amount = _integer(value, context)
    return Money(minor_units=amount, currency=currency)


def _decimal_money(value: Any, currency: Currency, context: str) -> Money:
    if not isinstance(value, str) or len(value) > 80 or not _DECIMAL_AMOUNT.fullmatch(value):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} is invalid.")
    exponent = _CURRENCY_EXPONENTS.get(currency)
    if exponent is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} is invalid.")
    # Exact string arithmetic, never `Decimal` multiplication: that runs under
    # the 28-digit context and would round a longer value to a whole number of
    # minor units instead of refusing it.
    whole, _, fraction = value.partition(".")
    fraction = fraction.rstrip("0")
    if len(fraction) > exponent:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} has unsupported precision."
        )
    minor = int(whole + fraction.ljust(exponent, "0"))
    if minor > 9_007_199_254_740_991:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} exceeds the numeric bound."
        )
    return Money(minor_units=minor, currency=currency)


def _timestamp(value: Any, context: str) -> datetime:
    raw = _string(value, context, maximum=80)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} is invalid.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} needs a timezone.")
    return parsed.astimezone(UTC)


class _TargetingPresence(NamedTuple):
    """Presence-only facts about one ad set's targeting object."""

    declared: bool
    geo_present: bool
    age_present: bool
    interest_present: bool


class _CreativePresence(NamedTuple):
    """Presence-only facts about one ad's associated creative."""

    associated: bool
    image_hash_present: bool
    link_present: bool
    screened_value_withheld: bool


def _present_key(value: dict[str, Any], key: str) -> bool:
    """Whether one documented targeting key was returned with a non-empty value.

    Only emptiness is examined, and only for the container and string forms
    Meta uses.  A numeric zero or a boolean false counts as *present*: an
    `age_min` of zero is still a declared bound, and reading it as an absence
    would report a targeting gap the provider did not return.  No country,
    region, city, radius, age bound, interest name, audience identifier, or any
    other targeting value is read, compared, or retained, so nothing that could
    be sensitive can leave this function.
    """
    if key not in value:
        return False
    supplied = value[key]
    if supplied is None:
        return False
    if isinstance(supplied, (dict, list, tuple, str)):
        return len(supplied) > 0
    return True


def _targeting_presence(value: Any) -> _TargetingPresence:
    """Reduce a returned targeting object to four booleans and nothing else.

    Meta documents the targeting spec's top-level keys, including
    `geo_locations`, `age_min`, `age_max`, `interests`, and `flexible_spec`.
    The object is deliberately not key-checked against a closed set: Meta
    documents dozens of targeting keys and grows them per version, and failing
    a whole bounded read because an account used one this build has not
    enumerated would be a worse outcome than reading presence alone.  Its size
    is bounded instead, and no value is ever read.
    """
    if value is None:
        return _TargetingPresence(False, False, False, False)
    if not isinstance(value, dict) or len(value) > 128:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta Ads ad set targeting is invalid.")
    geo = _present_key(value, "geo_locations")
    age = _present_key(value, "age_min") or _present_key(value, "age_max")
    interest = _present_key(value, "interests") or _present_key(value, "flexible_spec")
    return _TargetingPresence(bool(value), geo, age, interest)


def _creative_presence(value: Any) -> _CreativePresence:
    """Reduce a returned ad creative to four booleans and nothing else.

    The fixed read requests `creative{id,image_hash,link_url,object_url}`.  The
    identifier proves association; the other three are read for presence only
    and their values -- an image hash and a destination URL -- are never
    retained.  `object_story_spec`, `title`, and `body` are not requested at
    all, so no ad copy reaches this process.

    Those three values are screened here, one resource at a time, instead of by
    the whole-read screen in `_json`.  A landing page whose query string
    carries a contact address is ordinary advertiser content; the presence fact
    still stands, the value is withheld exactly as it would have been anyway,
    and the ad records that a local screen matched so no reader mistakes the
    withheld value for an absent one.
    """
    if value is None:
        return _CreativePresence(False, False, False, False)
    if not isinstance(value, dict):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta Ads ad creative is invalid.")
    _keys(value, {"id", "image_hash", "link_url", "object_url"}, "ad creative")
    withheld = False

    def _string_present(key: str, maximum: int, *, screened: bool) -> bool:
        nonlocal withheld
        supplied = value.get(key)
        if supplied is None:
            return False
        if not isinstance(supplied, str) or not 1 <= len(supplied) <= maximum:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta Ads ad creative is invalid.")
        if screened:
            try:
                validate_data(supplied)
            except AdhelmError:
                # Presence, and only presence.  The value was never going to be
                # retained; recording that the screen matched keeps a withheld
                # value distinguishable from an absent one.
                withheld = True
        return True

    associated = _string_present("id", 40, screened=False)
    image_hash = _string_present("image_hash", 160, screened=True)
    # Either documented link field proves a destination was returned.  Neither
    # value is read beyond its presence, its length bound, and this screen.
    link = _string_present("link_url", 8_192, screened=True) or _string_present(
        "object_url", 8_192, screened=True
    )
    return _CreativePresence(associated, image_hash, link, withheld)


class MetaAdsIdentity(StrictModel):
    schema_version: Literal[1] = 1
    account_ref: SafeRef
    account_client_ref: SafeRef
    timezone: str = Field(min_length=1, max_length=80)
    currency: Currency
    status: str = Field(min_length=1, max_length=80)
    provider_api_version: Literal["v26.0"] = _API_VERSION


class MetaAdsCampaign(StrictModel):
    campaign_ref: SafeRef
    campaign_client_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)
    objective: str | None = Field(default=None, max_length=80)
    buying_type: str | None = Field(default=None, max_length=80)
    bid_strategy: str | None = Field(default=None, max_length=80)
    daily_budget: Money | None = None
    lifetime_budget: Money | None = None
    spend_cap: Money | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None


class MetaAdsAdSet(StrictModel):
    adset_ref: SafeRef
    adset_client_ref: SafeRef
    campaign_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)
    billing_event: str | None = Field(default=None, max_length=80)
    bid_amount: Money | None = None
    bid_strategy: str | None = Field(default=None, max_length=80)
    daily_budget: Money | None = None
    lifetime_budget: Money | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    # Presence facts only.  No targeting criterion, audience, geography, age
    # bound, or interest name is ever read into this model.
    targeting_declared: bool = False
    targeting_geo_present: bool = False
    targeting_age_present: bool = False
    targeting_interest_present: bool = False


class MetaAdsAd(StrictModel):
    ad_ref: SafeRef
    ad_client_ref: SafeRef
    campaign_ref: SafeRef
    adset_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)
    # Presence facts only.  No image hash, destination URL, headline, or body
    # copy is ever read into this model.
    creative_associated: bool = False
    creative_image_hash_present: bool = False
    creative_link_present: bool = False
    # Set when a local privacy screen matched one of the creative's
    # presence-only values.  The value is withheld either way; this records
    # that it was withheld rather than absent.
    creative_screened_value_withheld: bool = False


def _enum_statuses(
    value: str | None, reviewed: tuple[str, ...], returned: str, absent: str, unreviewed: str
) -> tuple[str, ...]:
    if value is None:
        return (absent,)
    if value.upper() in reviewed:
        return (returned,)
    return (returned, unreviewed)


def meta_ads_campaign_quality_statuses(campaign: MetaAdsCampaign) -> tuple[str, ...]:
    """Deterministic presence and enum statuses for one retained campaign."""
    statuses = [
        *_enum_statuses(
            campaign.objective,
            META_ADS_REVIEWED_OBJECTIVES,
            META_ADS_OBJECTIVE_RETURNED,
            META_ADS_OBJECTIVE_NOT_RETURNED,
            META_ADS_OBJECTIVE_NOT_REVIEWED,
        ),
        *_enum_statuses(
            campaign.buying_type,
            META_ADS_REVIEWED_BUYING_TYPES,
            META_ADS_BUYING_TYPE_RETURNED,
            META_ADS_BUYING_TYPE_NOT_RETURNED,
            META_ADS_BUYING_TYPE_NOT_REVIEWED,
        ),
        *_enum_statuses(
            campaign.bid_strategy,
            META_ADS_REVIEWED_BID_STRATEGIES,
            META_ADS_BID_STRATEGY_RETURNED,
            META_ADS_BID_STRATEGY_NOT_RETURNED,
            META_ADS_BID_STRATEGY_NOT_REVIEWED,
        ),
        meta_ads_budget_mode_status(campaign.daily_budget, campaign.lifetime_budget),
        META_ADS_SCHEDULE_END_PRESENT if campaign.end_at else META_ADS_SCHEDULE_END_ABSENT,
    ]
    if campaign.buying_type is not None and campaign.buying_type.upper() == "RESERVED":
        statuses.append(META_ADS_BUYING_TYPE_RESERVED)
    return tuple(statuses)


def meta_ads_budget_mode_status(daily: Money | None, lifetime: Money | None) -> str:
    """State which documented budget field(s) a retained resource returned."""
    if daily is not None and lifetime is not None:
        return META_ADS_BUDGET_MODE_DAILY_AND_LIFETIME
    if daily is not None:
        return META_ADS_BUDGET_MODE_DAILY
    if lifetime is not None:
        return META_ADS_BUDGET_MODE_LIFETIME
    return META_ADS_BUDGET_MODE_NOT_RETURNED


def meta_ads_ad_set_quality_statuses(ad_set: MetaAdsAdSet) -> tuple[str, ...]:
    """Deterministic presence and enum statuses for one retained ad set."""
    return (
        META_ADS_TARGETING_DECLARED
        if ad_set.targeting_declared
        else META_ADS_TARGETING_NOT_DECLARED,
        META_ADS_TARGETING_GEO_PRESENT
        if ad_set.targeting_geo_present
        else META_ADS_TARGETING_GEO_ABSENT,
        META_ADS_TARGETING_AGE_PRESENT
        if ad_set.targeting_age_present
        else META_ADS_TARGETING_AGE_ABSENT,
        META_ADS_TARGETING_INTEREST_PRESENT
        if ad_set.targeting_interest_present
        else META_ADS_TARGETING_INTEREST_ABSENT,
        meta_ads_budget_mode_status(ad_set.daily_budget, ad_set.lifetime_budget),
        META_ADS_SCHEDULE_END_PRESENT if ad_set.end_at else META_ADS_SCHEDULE_END_ABSENT,
        *_enum_statuses(
            ad_set.bid_strategy,
            META_ADS_REVIEWED_BID_STRATEGIES,
            META_ADS_BID_STRATEGY_RETURNED,
            META_ADS_BID_STRATEGY_NOT_RETURNED,
            META_ADS_BID_STRATEGY_NOT_REVIEWED,
        ),
    )


def meta_ads_ad_review_status(status: str | None) -> str:
    """Classify one documented ad `effective_status` as a review-state fact.

    Meta folds review state into the ad's `effective_status` enumeration.
    `NOT_FLAGGED` means the retained status names no review or billing problem;
    it is never a claim that Meta approved the ad.
    """
    value = status.upper() if isinstance(status, str) else ""
    if value in META_ADS_REVIEW_BLOCKING_STATUSES:
        return META_ADS_AD_REVIEW_BLOCKED
    if value in META_ADS_REVIEW_PENDING_STATUSES:
        return META_ADS_AD_REVIEW_PENDING
    if value in META_ADS_ATTENTION_STATUSES:
        return META_ADS_AD_REVIEW_ATTENTION
    return META_ADS_AD_REVIEW_NOT_FLAGGED


def meta_ads_ad_quality_statuses(ad: MetaAdsAd) -> tuple[str, ...]:
    """Deterministic presence statuses for one retained ad."""
    return (
        META_ADS_CREATIVE_ASSOCIATED
        if ad.creative_associated
        else META_ADS_CREATIVE_NOT_ASSOCIATED,
        META_ADS_CREATIVE_IMAGE_HASH_PRESENT
        if ad.creative_image_hash_present
        else META_ADS_CREATIVE_IMAGE_HASH_ABSENT,
        META_ADS_CREATIVE_LINK_PRESENT
        if ad.creative_link_present
        else META_ADS_CREATIVE_LINK_ABSENT,
        META_ADS_CREATIVE_VALUE_WITHHELD
        if ad.creative_screened_value_withheld
        else META_ADS_CREATIVE_VALUE_NOT_WITHHELD,
        meta_ads_ad_review_status(ad.status),
    )


class MetaAdsInventoryQuery(StrictModel):
    account_ref: SafeRef
    maximum_records: int = Field(default=500, ge=1, le=5_000)
    page_size: int = Field(default=500, ge=1, le=_MAX_PAGE_SIZE)

    @model_validator(mode="after")
    def bounded_pages(self) -> MetaAdsInventoryQuery:
        if self.maximum_records > self.page_size * _MAX_PAGES:
            raise ValueError("Meta Ads inventory exceeds the ten-page limit")
        return self


class MetaAdsReportQuery(StrictModel):
    level: Literal["CAMPAIGN", "AD"] = "CAMPAIGN"
    account_ref: SafeRef
    report_id: SafeRef
    query_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_start: date
    window_end: date
    source_timezone: str = Field(min_length=1, max_length=80)
    currency: Currency | None = None
    action_attribution_windows: tuple[Literal["1d_click", "7d_click", "1d_view"], ...] = Field(
        min_length=1, max_length=3
    )
    maximum_rows: int = Field(default=2_000, ge=1, le=5_000)
    page_size: int = Field(default=500, ge=1, le=_MAX_PAGE_SIZE)

    @model_validator(mode="after")
    def valid_window_and_bound(self) -> MetaAdsReportQuery:
        if self.window_end < self.window_start:
            raise ValueError("Meta Ads report window end must not precede its start")
        if (self.window_end - self.window_start).days > 366:
            raise ValueError("Meta Ads report window may include at most 367 days")
        if len(set(self.action_attribution_windows)) != len(self.action_attribution_windows):
            raise ValueError("Meta Ads attribution windows must be unique")
        if self.maximum_rows > self.page_size * _MAX_PAGES:
            raise ValueError("Meta Ads report exceeds the ten-page limit")
        return self


class MetaAdsClient:
    """Fixed Meta Ads reads; this adapter has no mutation methods."""

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
                "TRANSPORT_CONFIG_INVALID", "Meta Ads adapter requires its fixed Graph API host."
            )
        self._transport = transport
        self._token_getter = token_getter
        self._reference_for = reference_for
        self._now = now
        self._connector_version = connector_version

    def discover_identity(
        self, account_client_ref: str, *, expected_account_client_ref: str | None = None
    ) -> MetaAdsIdentity:
        account = _account_id(account_client_ref, "configured account ID")
        data = _json(self._get(f"/{account}", params={"fields": _ACCOUNT_FIELDS}), "account")
        _keys(data, {"id", "account_status", "currency", "timezone_name"}, "account")
        returned = _account_id(data.get("id"), "account ID")
        if returned != account or (
            expected_account_client_ref is not None
            and returned != _account_id(expected_account_client_ref, "expected account ID")
        ):
            raise AdhelmError("IDENTITY_MISMATCH", "Meta Ads account differs from the profile.")
        timezone = _string(data.get("timezone_name"), "account timezone", maximum=80)
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            # `ZoneInfo` raises `ValueError`, not the not-found error, for a
            # key that is not a normalized relative path (`../x`, `a/`), and
            # an `OSError` (`IsADirectoryError`) for a tzdata directory such as
            # `America`; that error's message carries a local absolute path, so
            # it is replaced rather than chained into the refusal text.
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Meta Ads account timezone is invalid."
            ) from exc
        account_status = data.get("account_status")
        if isinstance(account_status, bool) or not isinstance(account_status, int):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta Ads account status is invalid.")
        return MetaAdsIdentity(
            account_ref=self._reference_for("account", returned),
            account_client_ref=returned,
            timezone=timezone,
            currency=_currency(data.get("currency")),
            status=str(account_status),
        )

    def list_campaigns(
        self, query: MetaAdsInventoryQuery, *, identity: MetaAdsIdentity
    ) -> list[MetaAdsCampaign]:
        return cast(list[MetaAdsCampaign], self._inventory(query, identity, "campaigns"))

    def list_adsets(
        self, query: MetaAdsInventoryQuery, *, identity: MetaAdsIdentity
    ) -> list[MetaAdsAdSet]:
        return cast(list[MetaAdsAdSet], self._inventory(query, identity, "adsets"))

    def list_ads(
        self, query: MetaAdsInventoryQuery, *, identity: MetaAdsIdentity
    ) -> list[MetaAdsAd]:
        return cast(list[MetaAdsAd], self._inventory(query, identity, "ads"))

    def run_campaign_report(
        self, query: MetaAdsReportQuery, *, identity: MetaAdsIdentity
    ) -> NormalizedReport:
        return self._report(query, identity, level="campaign")

    def run_ad_report(
        self, query: MetaAdsReportQuery, *, identity: MetaAdsIdentity
    ) -> NormalizedReport:
        return self._report(query, identity, level="ad")

    def _inventory(
        self,
        query: MetaAdsInventoryQuery,
        identity: MetaAdsIdentity,
        edge: Literal["campaigns", "adsets", "ads"],
    ) -> list[MetaAdsCampaign] | list[MetaAdsAdSet] | list[MetaAdsAd]:
        account = self._identity_matches(query.account_ref, identity)
        fields = {"campaigns": _CAMPAIGN_FIELDS, "adsets": _ADSET_FIELDS, "ads": _AD_FIELDS}[edge]
        collected: list[Any] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_PAGES):
            remaining = query.maximum_records - len(collected)
            if remaining <= 0:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "Meta Ads inventory exceeds the configured complete-result bound.",
                )
            params: dict[str, Any] = {"fields": fields, "limit": min(query.page_size, remaining)}
            if after is not None:
                params["after"] = after
            response = _json(self._get(f"/{account}/{edge}", params=params), f"{edge} list")
            page, after = self._page(response, f"{edge} list", account, edge)
            if after is not None:
                if after in seen_cursors:
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "Meta Ads pagination cursor repeated."
                    )
                seen_cursors.add(after)
            if len(collected) + len(page) > query.maximum_records:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "Meta Ads inventory exceeds the configured complete-result bound.",
                )
            collected.extend(page)
            if after is None:
                if edge == "campaigns":
                    return [self._campaign(item, identity.currency) for item in collected]
                if edge == "adsets":
                    return [self._adset(item, identity.currency) for item in collected]
                return [self._ad(item) for item in collected]
            if len(collected) == query.maximum_records:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "Meta Ads inventory exceeds the configured complete-result bound.",
                )
        raise AdhelmError("PAGINATION_LIMIT", "Meta Ads inventory exceeded the bounded page count.")

    def _report(
        self,
        query: MetaAdsReportQuery,
        identity: MetaAdsIdentity,
        *,
        level: Literal["campaign", "ad"],
    ) -> NormalizedReport:
        account = self._identity_matches(query.account_ref, identity)
        if query.level.lower() != level:
            raise AdhelmError(
                "QUERY_GRAIN_MISMATCH", "Selected report method differs from query grain."
            )
        if query.source_timezone != identity.timezone:
            raise AdhelmError(
                "TIMEZONE_MISMATCH", "Meta Ads report timezone differs from account identity."
            )
        if query.currency is not None and query.currency != identity.currency:
            raise AdhelmError(
                "CURRENCY_MISMATCH", "Meta Ads report currency differs from account identity."
            )
        observed_at = self._observed_at()
        # The account's own calendar day, never the connector's: the account
        # timezone decides which date Meta is still accruing.
        open_day = observed_at.astimezone(ZoneInfo(identity.timezone)).date()
        if query.window_end > open_day:
            raise AdhelmError(
                "INVALID_REPORT_WINDOW", "Meta Ads report window may not end in the future."
            )
        fields = _CAMPAIGN_INSIGHT_FIELDS if level == "campaign" else _AD_INSIGHT_FIELDS
        rows: list[PerformanceRow] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_PAGES):
            remaining = query.maximum_rows - len(rows)
            if remaining <= 0:
                raise _report_bound_refusal()
            params: dict[str, Any] = {
                "fields": fields,
                "level": level,
                "time_increment": 1,
                "time_range": json.dumps(
                    {
                        "since": query.window_start.isoformat(),
                        "until": query.window_end.isoformat(),
                    },
                    separators=(",", ":"),
                ),
                "action_attribution_windows": json.dumps(
                    list(query.action_attribution_windows), separators=(",", ":")
                ),
                "limit": min(query.page_size, remaining),
            }
            if after is not None:
                params["after"] = after
            response = _json(
                self._get(f"/{account}/insights", params=params),
                f"{level} insights",
            )
            page, after = self._page(response, f"{level} insights", account, "insights")
            if after is not None:
                if after in seen_cursors:
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "Meta Ads pagination cursor repeated."
                    )
                seen_cursors.add(after)
            parsed = [self._insight(item, query, identity, level, open_day) for item in page]
            if len(rows) + len(parsed) > query.maximum_rows:
                raise _report_bound_refusal()
            rows.extend(parsed)
            if after is None:
                self._reject_duplicate_rows(rows)
                # The account's current day is still accruing.  Nothing else
                # about freshness is supplied, so nothing else is claimed.
                statuses: list[QualityStatus] = ["COMPLETE", "NOT_AVAILABLE_FROM_PROVIDER"]
                if query.window_end == open_day:
                    statuses.insert(1, "PROCESSING_PENDING")
                return NormalizedReport(
                    envelope=ReportEnvelope(
                        report_id=query.report_id,
                        provider="meta_ads",
                        source_account_ref=query.account_ref,
                        query_contract_hash=query.query_contract_hash,
                        window_start=query.window_start,
                        window_end=query.window_end,
                        source_timezone=identity.timezone,
                        report_timezone=identity.timezone,
                        currency=identity.currency,
                        attribution_model=META_ADS_ATTRIBUTION_MODEL,
                        attribution_window=",".join(query.action_attribution_windows),
                        observed_at=observed_at,
                        data_fresh_through=None,
                        data_quality_statuses=statuses,
                        provider_api_version=_API_VERSION,
                        connector_version=self._connector_version,
                    ),
                    performance_rows=rows,
                )
            if len(rows) == query.maximum_rows:
                raise _report_bound_refusal()
        raise AdhelmError("PAGINATION_LIMIT", "Meta Ads report exceeded the bounded page count.")

    def _campaign(self, item: Any, currency: Currency) -> MetaAdsCampaign:
        if not isinstance(item, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta Ads campaign is invalid.")
        _keys(
            item,
            {
                "id",
                "effective_status",
                "objective",
                "buying_type",
                "bid_strategy",
                "daily_budget",
                "lifetime_budget",
                "spend_cap",
                "start_time",
                "stop_time",
            },
            "campaign",
        )
        identifier = _id(item.get("id"), "campaign ID")
        return MetaAdsCampaign(
            campaign_ref=self._reference_for("campaign", identifier),
            campaign_client_ref=identifier,
            status=_string(item.get("effective_status"), "campaign status", maximum=80),
            objective=(
                _string(item["objective"], "campaign objective", maximum=80)
                if item.get("objective") is not None
                else None
            ),
            buying_type=self._optional_string(item.get("buying_type"), "campaign buying type"),
            bid_strategy=self._optional_string(item.get("bid_strategy"), "campaign bid strategy"),
            daily_budget=self._optional_minor(
                item.get("daily_budget"), currency, "campaign budget"
            ),
            lifetime_budget=self._optional_minor(
                item.get("lifetime_budget"), currency, "campaign budget"
            ),
            spend_cap=self._optional_minor(item.get("spend_cap"), currency, "campaign spend cap"),
            start_at=self._optional_timestamp(item.get("start_time"), "campaign start"),
            end_at=self._optional_timestamp(item.get("stop_time"), "campaign end"),
        )

    def _adset(self, item: Any, currency: Currency) -> MetaAdsAdSet:
        if not isinstance(item, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta Ads ad set is invalid.")
        _keys(
            item,
            {
                "id",
                "campaign_id",
                "effective_status",
                "billing_event",
                "bid_amount",
                "bid_strategy",
                "daily_budget",
                "lifetime_budget",
                "start_time",
                "end_time",
                "targeting",
            },
            "ad set",
        )
        identifier = _id(item.get("id"), "ad set ID")
        campaign_id = _id(item.get("campaign_id"), "ad set campaign ID")
        targeting = _targeting_presence(item.get("targeting"))
        return MetaAdsAdSet(
            adset_ref=self._reference_for("adset", identifier),
            adset_client_ref=identifier,
            campaign_ref=self._reference_for("campaign", campaign_id),
            status=_string(item.get("effective_status"), "ad set status", maximum=80),
            billing_event=self._optional_string(item.get("billing_event"), "ad set billing event"),
            bid_amount=self._optional_minor(item.get("bid_amount"), currency, "ad set bid amount"),
            bid_strategy=self._optional_string(item.get("bid_strategy"), "ad set bid strategy"),
            daily_budget=self._optional_minor(item.get("daily_budget"), currency, "ad set budget"),
            lifetime_budget=self._optional_minor(
                item.get("lifetime_budget"), currency, "ad set budget"
            ),
            start_at=self._optional_timestamp(item.get("start_time"), "ad set start"),
            end_at=self._optional_timestamp(item.get("end_time"), "ad set end"),
            targeting_declared=targeting.declared,
            targeting_geo_present=targeting.geo_present,
            targeting_age_present=targeting.age_present,
            targeting_interest_present=targeting.interest_present,
        )

    def _ad(self, item: Any) -> MetaAdsAd:
        if not isinstance(item, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta Ads ad is invalid.")
        _keys(item, {"id", "campaign_id", "adset_id", "effective_status", "creative"}, "ad")
        identifier = _id(item.get("id"), "ad ID")
        creative = _creative_presence(item.get("creative"))
        return MetaAdsAd(
            ad_ref=self._reference_for("ad", identifier),
            ad_client_ref=identifier,
            campaign_ref=self._reference_for(
                "campaign", _id(item.get("campaign_id"), "ad campaign ID")
            ),
            adset_ref=self._reference_for("adset", _id(item.get("adset_id"), "ad set ID")),
            status=_string(item.get("effective_status"), "ad status", maximum=80),
            creative_associated=creative.associated,
            creative_image_hash_present=creative.image_hash_present,
            creative_link_present=creative.link_present,
            creative_screened_value_withheld=creative.screened_value_withheld,
        )

    def _insight(
        self,
        item: Any,
        query: MetaAdsReportQuery,
        identity: MetaAdsIdentity,
        level: Literal["campaign", "ad"],
        open_day: date,
    ) -> PerformanceRow:
        if not isinstance(item, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta Ads insight row is invalid.")
        allowed = set(_CAMPAIGN_INSIGHT_FIELDS.split(","))
        if level == "ad":
            allowed = set(_AD_INSIGHT_FIELDS.split(","))
        _keys(item, allowed, f"{level} insight row")
        day = self._insight_day(item, query)
        # A row for the day Meta is still accruing is not a settled day.
        row_quality: list[QualityStatus] = ["PROCESSING_PENDING" if day == open_day else "COMPLETE"]
        campaign_id = _id(item.get("campaign_id"), "insight campaign ID")
        common: dict[str, Any] = {
            "report_id": query.report_id,
            "campaign_ref": self._reference_for("campaign", campaign_id),
            "dimension_type": "DATE",
            "dimension_value": day.isoformat(),
            "impressions": _integer(item.get("impressions"), "insight impressions"),
            "clicks": _integer(item.get("clicks"), "insight clicks"),
            "spend": _decimal_money(item.get("spend"), identity.currency, "insight spend"),
            "reach": _integer(item.get("reach"), "insight reach"),
        }
        if level == "campaign":
            return PerformanceRow(
                **common,
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="META_ADS_CAMPAIGN",
                resource_ref=self._reference_for("campaign", campaign_id),
                data_quality_statuses=row_quality,
            )
        ad_id = _id(item.get("ad_id"), "insight ad ID")
        _id(item.get("adset_id"), "insight ad set ID")
        return PerformanceRow(
            **common,
            resource_level="AD",
            metric_grain="AD",
            resource_type="META_ADS_AD",
            resource_ref=self._reference_for("ad", ad_id),
            data_quality_statuses=[*row_quality, "CREATIVE_NOT_EXPOSED", "COMBINATION_UNKNOWN"],
        )

    @staticmethod
    def _reject_duplicate_rows(rows: list[PerformanceRow]) -> None:
        # One resource has at most one daily row at one grain.  A repeated key
        # (for example a row re-served on a later page) would be summed twice by
        # every aggregate, and nothing identifies the authoritative copy, so the
        # whole report is refused rather than repaired.
        if len({(row.resource_ref, row.dimension_value) for row in rows}) != len(rows):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Meta Ads insights repeated a resource day."
            )

    def _insight_day(self, item: dict[str, Any], query: MetaAdsReportQuery) -> date:
        start = _string(item.get("date_start"), "insight start date", maximum=10)
        stop = _string(item.get("date_stop"), "insight stop date", maximum=10)
        # `date.fromisoformat` also reads `20260105` and `2026-W01-1`; Graph
        # documents `YYYY-MM-DD`, so nothing else is read as a day.
        if not (_ISO_DAY.fullmatch(start) and _ISO_DAY.fullmatch(stop)):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta Ads insight day is invalid.")
        try:
            start_day = date.fromisoformat(start)
            stop_day = date.fromisoformat(stop)
        except ValueError as exc:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Meta Ads insight day is invalid."
            ) from exc
        if start_day != stop_day or not query.window_start <= start_day <= query.window_end:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "Meta Ads insight is outside the requested daily window.",
            )
        return start_day

    def _page(
        self,
        response: dict[str, Any],
        context: str,
        account: str,
        edge: Literal["campaigns", "adsets", "ads", "insights"],
    ) -> tuple[list[Any], str | None]:
        _keys(response, {"data", "paging"}, context)
        page = response.get("data")
        if not isinstance(page, list) or len(page) > _MAX_PAGE_SIZE:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} data is invalid.")
        paging = response.get("paging")
        if paging is None:
            return page, None
        if not isinstance(paging, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} paging is invalid.")
        # Meta documents cursor paging as `cursors{before,after}` plus `next`
        # and `previous`.  Every page after the first carries `previous`; it is
        # accepted and then ignored, because this adapter only ever walks
        # forward from the fixed first request.
        _keys(paging, {"cursors", "next", "previous"}, f"{context} paging")
        cursors = paging.get("cursors")
        if cursors is None:
            if paging.get("next") is not None:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} paging is invalid."
                )
            return page, None
        if not isinstance(cursors, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} cursor is invalid.")
        _keys(cursors, {"before", "after"}, f"{context} cursors")
        after = cursors.get("after")
        next_url = paging.get("next")
        if next_url is None:
            return page, None
        # Graph documents that "it is possible that a page may be empty but
        # contain a `next` paging link".  An empty continuing page is followed
        # within the fixed page cap rather than refused: refusing it would end
        # a whole bounded read at the first privacy-filtered page.
        if not isinstance(after, str) or not _CURSOR.fullmatch(after):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} cursor is invalid.")
        self._validate_next(next_url, after, account, edge, context)
        return page, after

    @staticmethod
    def _validate_next(value: Any, after: str, account: str, edge: str, context: str) -> None:
        if not isinstance(value, str) or len(value) > 8_192:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} next URL is invalid."
            )
        try:
            parsed = urlsplit(value)
            params = parse_qs(parsed.query, strict_parsing=True, max_num_fields=32)
        except ValueError as exc:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} next URL is invalid."
            ) from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != _HOST
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.path != f"/{_API_VERSION}/{account}/{edge}"
            or params.get("after") != [after]
        ):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", f"Meta Ads {context} next URL is invalid."
            )

    def _get(self, path: str, *, params: dict[str, Any]) -> httpx.Response:
        try:
            return self._transport.request(
                "GET",
                f"https://{_HOST}/{_API_VERSION}{path}",
                headers={"authorization": f"Bearer {_safe_token(self._token_getter)}"},
                params=params,
            )
        except AdhelmError as error:
            # Classify the observed status only.  The refusal carries no
            # response body here, so a Meta-typed rate-limit or credential
            # diagnostic can never disclose one, and an unclassified status is
            # re-raised unchanged rather than relabelled.
            raise copy_provider_diagnostics(
                error, classify_meta_ads_transport_error(error)
            ) from None

    def _identity_matches(self, account_ref: str, identity: MetaAdsIdentity) -> str:
        # Typed identities may be supplied directly; SafeRef is not an account-path type.
        account = _account_id(identity.account_client_ref, "identity account ID")
        if identity.account_ref != self._reference_for("account", account):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "Meta Ads identity reference differs from its account ID."
            )
        if account_ref != identity.account_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "Meta Ads request differs from account identity."
            )
        return account

    @staticmethod
    def _optional_minor(value: Any, currency: Currency, context: str) -> Money | None:
        return None if value is None else _minor_units(value, currency, context)

    @staticmethod
    def _optional_string(value: Any, context: str) -> str | None:
        return None if value is None else _string(value, context, maximum=80)

    @staticmethod
    def _optional_timestamp(value: Any, context: str) -> datetime | None:
        return None if value is None else _timestamp(value, context)

    def _observed_at(self) -> datetime:
        observed_at = self._now()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise AdhelmError(
                "CLOCK_INVALID", "Meta Ads observation clock must include a timezone."
            )
        return observed_at
