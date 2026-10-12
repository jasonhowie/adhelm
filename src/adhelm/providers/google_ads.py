# ruff: noqa: E501
"""Bounded Google Ads v25 REST reads.

The adapter deliberately exposes no GAQL or resource-path passthrough.  It
compiles a small fixed set of inventory and aggregate-delivery queries, keeps
responses in memory, and fails closed on incomplete pagination or response
shapes outside those queries.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field, model_validator

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError, ExitCode, copy_provider_diagnostics
from adhelm.models import Currency, Money, SafeRef, StrictModel
from adhelm.privacy import validate_data
from adhelm.reporting import (
    ConversionBreakdownRow,
    NormalizedReport,
    PerformanceRow,
    QualityStatus,
    ReportEnvelope,
    canonical_metric_decimal,
)
from adhelm.transport import SafeHttpClient

_HOST = "googleads.googleapis.com"
# The one place this build names the Google Ads REST version.  Every request
# path, every model literal, and every retained snapshot reads it from here, so
# a version bump cannot leave one surface behind.
GOOGLE_ADS_API_VERSION: Final = "v25"
_API_VERSION = GOOGLE_ADS_API_VERSION
_MAX_PAGES = 10
_MAX_PAGE_ROWS = 10_000
# Google serializes every int64 as a canonical decimal string: ASCII digits, no
# sign, no leading zero.  A looser spelling (full-width digits, "007") would
# convert to the same integer yet name a different local reference or retain a
# different provenance string, so it is refused rather than normalized.
_INT64_TEXT_RE = re.compile(r"(?:0|[1-9][0-9]{0,18})", re.ASCII)
_INT64_MAX = 9_223_372_036_854_775_807
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_ENUM_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_OMIT_RESOURCE_NAMES = " PARAMETERS omit_unselected_resource_names = true"
_MAX_METRIC_ATTRIBUTES = 32
_MAX_ATTRIBUTES_PER_METRIC = 16
# Google documents at most 15 headlines and 4 descriptions on a responsive
# search ad.  These local bounds are deliberately larger, so a legitimate
# future increase fails as an explicit bound rather than being silently
# truncated into a smaller retained count.
_MAX_CREATIVE_ASSETS = 64
_MAX_FINAL_URLS = 64

_IDENTITY_QUERY = (
    "SELECT customer.id, customer.currency_code, customer.time_zone, customer.test_account "
    "FROM customer" + _OMIT_RESOURCE_NAMES
)
_CAMPAIGN_QUERY = (
    "SELECT campaign.id, campaign.status, campaign.advertising_channel_type, "
    "campaign.start_date_time, campaign.end_date_time, campaign.bidding_strategy_type, "
    "campaign.network_settings.target_google_search, "
    "campaign.network_settings.target_search_network, "
    "campaign.network_settings.target_content_network, "
    "campaign.network_settings.target_partner_search_network, "
    "campaign.geo_target_type_setting.positive_geo_target_type, "
    "campaign.geo_target_type_setting.negative_geo_target_type, "
    "campaign_budget.amount_micros, campaign_budget.period, "
    "campaign_budget.delivery_method, campaign_budget.explicitly_shared "
    "FROM campaign ORDER BY campaign.id" + _OMIT_RESOURCE_NAMES
)
# Ad scheduling is a campaign criterion, not a campaign field.  This read
# retains only which campaigns declared at least one such criterion; it never
# retains a day, an hour, or a bid modifier.
_AD_SCHEDULE_QUERY = (
    "SELECT campaign.id, campaign_criterion.criterion_id, campaign_criterion.type "
    "FROM campaign_criterion WHERE campaign_criterion.type = 'AD_SCHEDULE' "
    "ORDER BY campaign.id, campaign_criterion.criterion_id" + _OMIT_RESOURCE_NAMES
)
_AD_GROUP_QUERY = (
    "SELECT campaign.id, ad_group.id, ad_group.status, ad_group.type, "
    "ad_group.cpc_bid_micros FROM ad_group ORDER BY campaign.id, ad_group.id" + _OMIT_RESOURCE_NAMES
)
# `final_urls`, `responsive_search_ad.headlines` and
# `responsive_search_ad.descriptions` are selected so that their **counts** can
# be retained.  `_redact_ad_creative` replaces each of those values with its
# length immediately after the response is parsed, before the privacy screen
# and before anything else in this module -- including every error message --
# can observe it.  No headline, description, or destination value is retained,
# projected, screened as text, or reachable from a diagnostic.
_AD_QUERY = (
    "SELECT campaign.id, ad_group.id, ad_group_ad.ad.id, ad_group_ad.ad.type, "
    "ad_group_ad.status, ad_group_ad.policy_summary.approval_status, "
    "ad_group_ad.policy_summary.review_status, ad_group_ad.ad.final_urls, "
    "ad_group_ad.ad.responsive_search_ad.headlines, "
    "ad_group_ad.ad.responsive_search_ad.descriptions FROM ad_group_ad "
    "ORDER BY campaign.id, ad_group.id, ad_group_ad.ad.id" + _OMIT_RESOURCE_NAMES
)

# What this build's fixed Google Ads reads actually request, per audit-relevant
# surface.  `adhelm.audits.google_ads` derives its coverage ceiling from this map
# instead of restating it, so the audit can never report a dimension as read
# because a caller happened to supply records for it.
GoogleAdsReadSurfaceStatus = Literal[
    "IMPLEMENTED_FIXED_READ",
    "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "NOT_IN_REVIEWED_FIXED_READ",
    "NOT_DOCUMENTED",
]
GOOGLE_ADS_READ_SURFACES: Final[dict[str, GoogleAdsReadSurfaceStatus]] = {
    "CUSTOMER_IDENTITY": "IMPLEMENTED_FIXED_READ",
    "CUSTOMER_TEST_ACCOUNT_FLAG": "IMPLEMENTED_FIXED_READ",
    "CUSTOMER_ACCOUNT_STATUS": "NOT_IN_REVIEWED_FIXED_READ",
    "CLOUD_PROJECT_ACCESS_LEVEL": "NOT_IN_REVIEWED_FIXED_READ",
    "CAMPAIGN_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "AD_GROUP_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "AD_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_ADVERTISING_CHANNEL_TYPE": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_BIDDING_STRATEGY_TYPE": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_BUDGET_AMOUNT": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_BUDGET_TYPE": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_SCHEDULE_DATES": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_NETWORK_SETTINGS": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_GEO_TARGET_TYPE_SETTING": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_AD_SCHEDULE_PRESENCE": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "CAMPAIGN_TARGETING_CRITERION_VALUES": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_GROUP_TYPE": "IMPLEMENTED_FIXED_READ",
    "AD_GROUP_CPC_BID": "IMPLEMENTED_FIXED_READ",
    "AD_GROUP_TARGETING_CRITERION_VALUES": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_TYPE": "IMPLEMENTED_FIXED_READ",
    "AD_RSA_HEADLINE_PRESENCE": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "AD_RSA_DESCRIPTION_PRESENCE": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "AD_CREATIVE_TEXT": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_FINAL_URL_PRESENCE": "IMPLEMENTED_FIXED_READ_PRESENCE_ONLY",
    "AD_FINAL_URL_VALUE": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_TRACKING_TEMPLATE_AND_URL_PARAMETERS": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_POLICY_APPROVAL_STATUS": "IMPLEMENTED_FIXED_READ",
    "AD_POLICY_REVIEW_STATUS": "IMPLEMENTED_FIXED_READ",
    "AD_POLICY_TOPIC_ENTRIES": "NOT_IN_REVIEWED_FIXED_READ",
    "CAMPAIGN_DELIVERY_REPORT": "IMPLEMENTED_FIXED_READ",
    "AD_DELIVERY_REPORT": "IMPLEMENTED_FIXED_READ",
    "SEARCH_PAGINATION": "IMPLEMENTED_FIXED_READ",
    "PLACEMENT": "NOT_IN_REVIEWED_FIXED_READ",
    "CONVERSION_ACTION_INVENTORY": "NOT_IN_REVIEWED_FIXED_READ",
    "CAMPAIGN_CONVERSION_GOAL": "NOT_IN_REVIEWED_FIXED_READ",
    "NATIVE_CONVERSION_METRICS": "IMPLEMENTED_FIXED_READ",
    "REPORT_FRESHNESS": "NOT_IN_REVIEWED_FIXED_READ",
    "REPORT_ATTRIBUTION_WINDOW": "NOT_IN_REVIEWED_FIXED_READ",
}

# The v25 enumerations this build reviewed.  A value outside one of these is
# surfaced as unreviewed rather than guessed at.
GOOGLE_ADS_REVIEWED_CAMPAIGN_STATUSES: Final[tuple[str, ...]] = ("ENABLED", "PAUSED", "REMOVED")
GOOGLE_ADS_REVIEWED_AD_GROUP_STATUSES: Final[tuple[str, ...]] = ("ENABLED", "PAUSED", "REMOVED")
GOOGLE_ADS_REVIEWED_AD_STATUSES: Final[tuple[str, ...]] = ("ENABLED", "PAUSED", "REMOVED")
GOOGLE_ADS_REVIEWED_POLICY_APPROVAL_STATUSES: Final[tuple[str, ...]] = (
    "APPROVED",
    "APPROVED_LIMITED",
    "AREA_OF_INTEREST_ONLY",
    "DISAPPROVED",
)
GOOGLE_ADS_REVIEWED_POLICY_REVIEW_STATUSES: Final[tuple[str, ...]] = (
    "ELIGIBLE_MAY_SERVE",
    "REVIEWED",
    "REVIEW_IN_PROGRESS",
    "UNDER_APPEAL",
)
# Google documents accelerated delivery as spending a budget as quickly as
# possible rather than evenly across the period, and has withdrawn it for
# several campaign types.  It is read as an enum only; nothing here claims the
# campaign actually spent faster.
GOOGLE_ADS_ACCELERATED_BUDGET_DELIVERY: Final = "ACCELERATED"
# The approval statuses Google documents as serving with a restriction, or not
# serving at all.  `APPROVED` is deliberately absent.
GOOGLE_ADS_RESTRICTED_POLICY_APPROVAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"APPROVED_LIMITED", "AREA_OF_INTEREST_ONLY", "DISAPPROVED"}
)
# The one documented status that declares an intent to serve.  A declared
# status is never an observation of delivery.
GOOGLE_ADS_SERVING_INTENT_STATUS: Final = "ENABLED"
# Google retired developer tokens on 2026-09-09.  API access now belongs to the
# Google Cloud project that owns the OAuth client, at Test, Explorer, Basic or
# Standard.  No reviewed v25 read returns the level in use, so the connector
# discloses that it cannot observe it, and never assumes a tier.  Sources:
#   https://developers.google.com/google-ads/api/docs/api-policy/developer-token
#   https://developers.google.com/google-ads/api/docs/api-policy/access-levels
#   (both fetched 2026-10-12; last updated 2026-10-07 and 2026-09-30)
GOOGLE_ADS_CLOUD_PROJECT_ACCESS_LEVELS: Final[tuple[str, ...]] = (
    "TEST",
    "EXPLORER",
    "BASIC",
    "STANDARD",
)
GOOGLE_ADS_CLOUD_PROJECT_ACCESS_NOT_OBSERVABLE: Final = "NOT_OBSERVABLE"
# The authorization error-code constants this build classifies.  The first is
# what Google v25 returns when a Test-level project calls a production account;
# the second is the documented known issue for projects upgraded after
# 2026-09-09.  `ACTION_NOT_PERMITTED` is deliberately not here: the Developer
# token page names it as the pre-v25 equivalent, but on v25 (the only version
# AdHelm calls) it is the generic user-permission error, so it is reported as
# the generic access error rather than as a project-approval problem.
GOOGLE_ADS_PROJECT_NOT_APPROVED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"}
)
GOOGLE_ADS_AUTHORIZATION_ERROR_CODE: Final = "AUTHORIZATION_ERROR"
GOOGLE_ADS_CLASSIFIABLE_ERROR_CODES: Final[frozenset[str]] = (
    GOOGLE_ADS_PROJECT_NOT_APPROVED_ERROR_CODES | {GOOGLE_ADS_AUTHORIZATION_ERROR_CODE}
)

# Normalized data-quality statuses the inventory branch retains beside a
# snapshot.  Each presence fact is recorded in both directions, so a retained
# snapshot can never be read as "not evaluated" when it was evaluated and
# found present.
GOOGLE_ADS_PAGINATION_TOTAL_DECLARED: Final = "GOOGLE_ADS_PAGINATION_TOTAL_DECLARED"
GOOGLE_ADS_PAGINATION_TOTAL_NOT_DECLARED: Final = "GOOGLE_ADS_PAGINATION_TOTAL_NOT_DECLARED"
GOOGLE_ADS_TEST_ACCOUNT_DECLARED: Final = "GOOGLE_ADS_TEST_ACCOUNT_DECLARED"
GOOGLE_ADS_PRODUCTION_ACCOUNT_DECLARED: Final = "GOOGLE_ADS_PRODUCTION_ACCOUNT_DECLARED"
GOOGLE_ADS_TEST_ACCOUNT_FLAG_NOT_RETURNED: Final = "GOOGLE_ADS_TEST_ACCOUNT_FLAG_NOT_RETURNED"
GOOGLE_ADS_CLOUD_PROJECT_ACCESS_LEVEL_NOT_OBSERVABLE: Final = (
    "GOOGLE_ADS_CLOUD_PROJECT_ACCESS_LEVEL_NOT_OBSERVABLE"
)
GOOGLE_ADS_AD_SCHEDULE_PRESENT: Final = "GOOGLE_ADS_AD_SCHEDULE_PRESENT"
GOOGLE_ADS_AD_SCHEDULE_ABSENT: Final = "GOOGLE_ADS_AD_SCHEDULE_ABSENT"
GOOGLE_ADS_NETWORK_SETTINGS_RETURNED: Final = "GOOGLE_ADS_NETWORK_SETTINGS_RETURNED"
GOOGLE_ADS_NETWORK_SETTINGS_NOT_RETURNED: Final = "GOOGLE_ADS_NETWORK_SETTINGS_NOT_RETURNED"
GOOGLE_ADS_GEO_TARGET_TYPE_RETURNED: Final = "GOOGLE_ADS_GEO_TARGET_TYPE_RETURNED"
GOOGLE_ADS_GEO_TARGET_TYPE_NOT_RETURNED: Final = "GOOGLE_ADS_GEO_TARGET_TYPE_NOT_RETURNED"
GOOGLE_ADS_BUDGET_RETURNED: Final = "GOOGLE_ADS_BUDGET_RETURNED"
GOOGLE_ADS_BUDGET_NOT_RETURNED: Final = "GOOGLE_ADS_BUDGET_NOT_RETURNED"
GOOGLE_ADS_BUDGET_TYPE_RETURNED: Final = "GOOGLE_ADS_BUDGET_TYPE_RETURNED"
GOOGLE_ADS_BUDGET_TYPE_NOT_RETURNED: Final = "GOOGLE_ADS_BUDGET_TYPE_NOT_RETURNED"
GOOGLE_ADS_BUDGET_EXPLICITLY_SHARED: Final = "GOOGLE_ADS_BUDGET_EXPLICITLY_SHARED"
GOOGLE_ADS_BUDGET_NOT_EXPLICITLY_SHARED: Final = "GOOGLE_ADS_BUDGET_NOT_EXPLICITLY_SHARED"
GOOGLE_ADS_BUDGET_SHARING_NOT_RETURNED: Final = "GOOGLE_ADS_BUDGET_SHARING_NOT_RETURNED"
GOOGLE_ADS_NETWORK_CONTENT_ENABLED: Final = "GOOGLE_ADS_NETWORK_CONTENT_ENABLED"
GOOGLE_ADS_NETWORK_PARTNER_SEARCH_ENABLED: Final = "GOOGLE_ADS_NETWORK_PARTNER_SEARCH_ENABLED"
# Google documents that a responsive search ad requires at least three
# headlines and at least two descriptions.  The retained snapshot keeps the
# bucket, never the components; `GoogleAdsAd` keeps the exact counts for a
# caller that holds the live read.
GOOGLE_ADS_RSA_HEADLINE_MINIMUM: Final = 3
GOOGLE_ADS_RSA_DESCRIPTION_MINIMUM: Final = 2
GOOGLE_ADS_RSA_HEADLINES_BELOW_DOCUMENTED_MINIMUM: Final = (
    "GOOGLE_ADS_RSA_HEADLINES_BELOW_DOCUMENTED_MINIMUM"
)
GOOGLE_ADS_RSA_HEADLINES_AT_DOCUMENTED_MINIMUM: Final = (
    "GOOGLE_ADS_RSA_HEADLINES_AT_DOCUMENTED_MINIMUM"
)
GOOGLE_ADS_RSA_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM: Final = (
    "GOOGLE_ADS_RSA_DESCRIPTIONS_BELOW_DOCUMENTED_MINIMUM"
)
GOOGLE_ADS_RSA_DESCRIPTIONS_AT_DOCUMENTED_MINIMUM: Final = (
    "GOOGLE_ADS_RSA_DESCRIPTIONS_AT_DOCUMENTED_MINIMUM"
)
GOOGLE_ADS_POLICY_REVIEW_STATUS_RETURNED: Final = "GOOGLE_ADS_POLICY_REVIEW_STATUS_RETURNED"
GOOGLE_ADS_POLICY_REVIEW_STATUS_NOT_RETURNED: Final = "GOOGLE_ADS_POLICY_REVIEW_STATUS_NOT_RETURNED"
GOOGLE_ADS_BUDGET_DELIVERY_METHOD_NOT_RETURNED: Final = (
    "GOOGLE_ADS_BUDGET_DELIVERY_METHOD_NOT_RETURNED"
)
# Which normalized-state fields this connector retains for a Google Ads
# snapshot.  It is recorded beside every snapshot so that a later reader can
# tell a connector state-shape change from a change at Google.  Bump it in the
# same commit as any change to what `_google_snapshots` puts in
# `normalized_state`; the retained value never enters `normalized_state`
# itself, so recording it creates no drift of its own.
GOOGLE_ADS_STATE_SHAPE_VERSION: Final = "2026-10-12.1"
GOOGLE_ADS_STATE_SHAPE_PREFIX: Final = "GOOGLE_ADS_STATE_SHAPE_"
GOOGLE_ADS_STATE_SHAPE_STATUS: Final = (
    GOOGLE_ADS_STATE_SHAPE_PREFIX + GOOGLE_ADS_STATE_SHAPE_VERSION
)
# Enum-valued facts become one prefixed status each, because the shared closed
# snapshot-state model has no field for them.  A prefix is fixed here so a rule
# module matches it exactly instead of parsing a free-form string.
GOOGLE_ADS_BUDGET_PERIOD_PREFIX: Final = "GOOGLE_ADS_BUDGET_PERIOD_"
GOOGLE_ADS_POSITIVE_GEO_TARGET_TYPE_PREFIX: Final = "GOOGLE_ADS_POSITIVE_GEO_TARGET_TYPE_"
GOOGLE_ADS_NEGATIVE_GEO_TARGET_TYPE_PREFIX: Final = "GOOGLE_ADS_NEGATIVE_GEO_TARGET_TYPE_"
GOOGLE_ADS_POLICY_APPROVAL_PREFIX: Final = "GOOGLE_ADS_POLICY_APPROVAL_"
GOOGLE_ADS_BUDGET_DELIVERY_METHOD_PREFIX: Final = "GOOGLE_ADS_BUDGET_DELIVERY_METHOD_"
GOOGLE_ADS_POLICY_REVIEW_PREFIX: Final = "GOOGLE_ADS_POLICY_REVIEW_"
GOOGLE_ADS_RSA_HEADLINES_PRESENT: Final = "GOOGLE_ADS_RSA_HEADLINES_PRESENT"
GOOGLE_ADS_RSA_HEADLINES_ABSENT: Final = "GOOGLE_ADS_RSA_HEADLINES_ABSENT"
GOOGLE_ADS_RSA_DESCRIPTIONS_PRESENT: Final = "GOOGLE_ADS_RSA_DESCRIPTIONS_PRESENT"
GOOGLE_ADS_RSA_DESCRIPTIONS_ABSENT: Final = "GOOGLE_ADS_RSA_DESCRIPTIONS_ABSENT"
GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED: Final = "GOOGLE_ADS_RESPONSIVE_SEARCH_AD_NOT_RETURNED"
GOOGLE_ADS_FINAL_URL_PRESENT: Final = "GOOGLE_ADS_FINAL_URL_PRESENT"
GOOGLE_ADS_FINAL_URL_ABSENT: Final = "GOOGLE_ADS_FINAL_URL_ABSENT"
GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED: Final = "GOOGLE_ADS_POLICY_APPROVAL_STATUS_RETURNED"
GOOGLE_ADS_POLICY_APPROVAL_STATUS_NOT_RETURNED: Final = (
    "GOOGLE_ADS_POLICY_APPROVAL_STATUS_NOT_RETURNED"
)
GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED: Final = "GOOGLE_ADS_CREATIVE_TEXT_NOT_RETAINED"


def google_ads_declares_serving_intent(status: str | None) -> bool:
    """Whether a retained Google Ads status declares the documented serving intent."""
    return isinstance(status, str) and status == GOOGLE_ADS_SERVING_INTENT_STATUS


def classify_google_ads_transport_error(error: AdhelmError) -> AdhelmError:
    """Turn one shared-transport refusal into an exact Google Ads diagnostic.

    The shared transport records the observed HTTP status and, when the
    provider sent a whole-second `Retry-After`, its bounded value.  Nothing
    from the response body is available here, so no classification can leak
    one -- and equally, the `RESOURCE_EXHAUSTED` status code Google documents
    in the response body is *not* what this function reads.  It classifies the
    documented HTTP status that carries that code, and says so.  A status this
    contract does not classify is returned unchanged rather than relabelled
    into a quota or credential problem it was never shown to be.
    """
    status = getattr(error, "status_code", None)
    retry_after = getattr(error, "retry_after_seconds", None)
    code = getattr(error, "provider_error_code", None)
    if status in {401, 403} and code in GOOGLE_ADS_PROJECT_NOT_APPROVED_ERROR_CODES:
        return AdhelmError(
            "GOOGLE_ADS_CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION",
            "Google Ads refused a production account because the Google Cloud project that "
            "owns the OAuth client is not approved for production (Test access reaches test "
            "accounts only).",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action=(
                "Apply for Explorer access for that Google Cloud project on the Google Ads API "
                "Overview page in Cloud Console: Explorer is the first access level that "
                "reaches production accounts. AdHelm recommends Basic, which you apply for "
                "from Explorer after brand verification. Do not apply through the Google Ads "
                "API Center page. Then run `adhelm doctor` and read again."
            ),
            status_code=status,
            retry_after_seconds=retry_after,
            provider_error_code=code,
        )
    if status in {401, 403} and code == GOOGLE_ADS_AUTHORIZATION_ERROR_CODE:
        return AdhelmError(
            "GOOGLE_ADS_AUTHORIZATION_ERROR",
            "Google Ads returned AUTHORIZATION_ERROR for this customer.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action=(
                "If this Google Cloud project's access level was upgraded after 2026-09-09, "
                "Google documents a known issue in which AUTHORIZATION_ERROR can persist on "
                "production accounts because the upgrade is not yet honored, with a fix "
                "rolling out; Google's workaround is to apply for Explorer access with a new "
                "project. Otherwise confirm the OAuth grant and the `login-customer-id` "
                "manager route with `adhelm auth google-oauth status --profile <id>`. See "
                "https://developers.google.com/google-ads/api/docs/api-policy/developer-token"
            ),
            status_code=status,
            retry_after_seconds=retry_after,
            provider_error_code=code,
        )
    if status in {401, 403}:
        return AdhelmError(
            "GOOGLE_ADS_ACCESS_NOT_PERMITTED",
            "Google Ads refused the configured read profile for this customer.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action=(
                "Run `adhelm auth google-oauth status --profile <id>`, then confirm the OAuth "
                "grant and, when a manager account routes access, the `login-customer-id` "
                "before another bounded read."
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
            "GOOGLE_ADS_QUOTA_EXHAUSTED",
            "Google Ads refused the fixed read for exceeding a documented quota or request rate.",
            exit_code=int(ExitCode.PRECONDITION),
            next_action=(
                f"{disclosed} Google returns RESOURCE_EXHAUSTED on this status for both the "
                "Cloud project's daily operation allowance and its short-term request rate. "
                "That allowance depends on the project's Test, Explorer, Basic or Standard "
                "access level, which AdHelm cannot observe. Wait, then run the same bounded "
                "read once more; do not retry in a loop."
            ),
            status_code=status,
            retry_after_seconds=retry_after,
        )
    return error


AccessTokenGetter = Callable[[], str]
ReferenceFor = Callable[[Literal["customer", "campaign", "ad_group", "ad"], str], str]


def _default_reference_for(
    kind: Literal["customer", "campaign", "ad_group", "ad"], value: str
) -> str:
    """Return a local operational reference; projection redacts it on export."""
    return f"google_ads.{kind}.{value}"


def _safe_credential(getter: Callable[[], str]) -> str:
    value = getter()
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16_384
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "Google Ads authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local Google Ads read profile.",
        )
    return value


def _json(
    response: httpx.Response,
    context: str,
    *,
    redact: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    try:
        payload = parse_json(response.content, maximum_bytes=8_388_608, decimal_numbers=True)
    except (ValueError, UnicodeDecodeError, AdhelmError) as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Google Ads {context} response is not JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Google Ads {context} response has an invalid shape."
        )
    if redact is not None:
        # Runs before the privacy screen on purpose.  Ad copy and destination
        # values are replaced with their counts here, so the screen never sees
        # customer advertising text, cannot fail a legitimate read on a phone
        # number inside a headline, and no later diagnostic can quote one.
        redact(payload)
    try:
        validate_data(payload)
    except AdhelmError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "Google Ads response contains prohibited data."
        ) from exc
    return payload


def _keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Google Ads {context} included an unsupported field."
        )


def _search_metadata(page: dict[str, Any]) -> None:
    """Validate bounded v25 query metadata without retaining or interpreting it."""

    if "queryResourceConsumption" in page:
        consumption = page["queryResourceConsumption"]
        if type(consumption) is not str or _int64_text(consumption) is None:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Google Ads query resource consumption is invalid."
            )
    metrics = page.get("metricAttributes", [])
    if type(metrics) is not list or len(metrics) > _MAX_METRIC_ATTRIBUTES:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Google Ads metric attributes are invalid.")
    for metric in metrics:
        if type(metric) is not dict:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Google Ads metric attributes are invalid."
            )
        _keys(metric, {"name", "attributes"}, "metric attributes")
        _metadata_text(metric.get("name"), minimum=1, maximum=160)
        attributes = metric.get("attributes", [])
        if type(attributes) is not list or len(attributes) > _MAX_ATTRIBUTES_PER_METRIC:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Google Ads metric attributes are invalid."
            )
        for attribute in attributes:
            if type(attribute) is not dict:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "Google Ads metric attribute is invalid."
                )
            _keys(attribute, {"key", "value"}, "metric attribute")
            _metadata_text(attribute.get("key"), minimum=1, maximum=160)
            _metadata_text(attribute.get("value", ""), minimum=0, maximum=512)


def _metadata_text(value: Any, *, minimum: int, maximum: int) -> None:
    if (
        type(value) is not str
        or not minimum <= len(value) <= maximum
        or any(not character.isprintable() for character in value)
    ):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Google Ads metric metadata is invalid.")


def _string(value: Any, context: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Google Ads {context} is invalid.")
    try:
        validate_data(value)
    except AdhelmError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "Google Ads response contains prohibited data."
        ) from exc
    return value


def _int64_text(value: str) -> int | None:
    """The integer a canonical protobuf-JSON int64 string names, else ``None``."""
    if _INT64_TEXT_RE.fullmatch(value) is None:
        return None
    parsed = int(value)
    return parsed if parsed <= _INT64_MAX else None


def _id(value: Any, context: str) -> str:
    parsed = _string(value, context, maximum=20)
    if _int64_text(parsed) is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Google Ads {context} is invalid.")
    return parsed


def _enum(value: Any, context: str) -> str:
    parsed = _string(value, context, maximum=80)
    if _ENUM_RE.fullmatch(parsed) is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Google Ads {context} is invalid.")
    return parsed


def _optional_enum(value: Any, context: str) -> str | None:
    return None if value is None else _enum(value, context)


def _date(value: Any, context: str) -> date | None:
    if value is None or value == "":
        return None
    parsed = _string(value, context, maximum=10)
    # `date.fromisoformat` also accepts "20260901" and ISO week dates.  Google
    # documents YYYY-MM-DD, and only that spelling is read.
    if _DATE_RE.fullmatch(parsed) is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Google Ads {context} is invalid.")
    try:
        return date.fromisoformat(parsed)
    except ValueError as exc:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Google Ads {context} is invalid.") from exc


def _campaign_datetime(value: Any, timezone: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if type(value) is not str or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", value
        ):
            raise ValueError("Invalid date-time spelling")
        wall = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        zone = ZoneInfo(timezone)
        first = wall.replace(tzinfo=zone, fold=0)
        second = wall.replace(tzinfo=zone, fold=1)
        if (
            first.utcoffset() != second.utcoffset()
            or first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != wall
        ):
            raise ValueError("Ambiguous or nonexistent local time")
        return first
    except (ValueError, OverflowError, ZoneInfoNotFoundError):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "Google Ads campaign date-time is invalid or ambiguous."
        ) from None


def _micros_source(value: Any, context: str) -> tuple[int, str]:
    """Validate the protobuf JSON decimal representation without losing it."""
    parsed = _string(value, context, maximum=19)
    if _INT64_TEXT_RE.fullmatch(parsed) is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Google Ads {context} is invalid.")
    amount = int(parsed)
    if amount > _INT64_MAX:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Google Ads {context} exceeds the numeric bound."
        )
    return amount, parsed


def _exact_micros(value: Any, currency: Currency, context: str) -> Money | None:
    """Use only lossless cents for operational budget and bid metadata."""
    if value is None:
        return None
    if currency != "USD":
        raise AdhelmError(
            "UNSUPPORTED_CURRENCY",
            "Google Ads monetary conversion is currently implemented only for USD.",
        )
    amount, _ = _micros_source(value, context)
    if amount % 10_000:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Google Ads {context} has unsupported precision."
        )
    return Money(minor_units=amount // 10_000, currency=currency)


def _reported_micros(value: Any, currency: Currency, context: str) -> tuple[Money, str]:
    """Round a delivery metric to cents while retaining the exact source micros.

    Google exposes ``metrics.cost_micros`` as an INT64 aggregate.  A shared
    USD Money amount cannot represent all micro-unit results, so this applies
    the documented connector rounding rule to the display-compatible amount
    and returns the unmodified decimal string for row-level provenance.
    """
    if currency != "USD":
        raise AdhelmError(
            "UNSUPPORTED_CURRENCY",
            "Google Ads monetary conversion is currently implemented only for USD.",
        )
    amount, source = _micros_source(value, context)
    rounded = (Decimal(amount) / Decimal(10_000)).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    return Money(minor_units=int(rounded), currency=currency), source


def _timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "Google Ads customer timezone is not an IANA timezone."
        ) from exc


class GoogleAdsReadConfig(StrictModel):
    """Local fixed account selection; IDs deliberately exclude display formatting."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    customer_client_ref: str = Field(pattern=r"^[0-9]{10}$")
    login_customer_client_ref: str | None = Field(default=None, pattern=r"^[0-9]{10}$")


class GoogleAdsIdentity(StrictModel):
    schema_version: Literal[1] = 1
    account_ref: SafeRef
    customer_client_ref: SafeRef
    timezone: str = Field(min_length=1, max_length=80)
    currency: Currency
    # Google documents `customer.test_account`; it is a property of the customer,
    # not of the Cloud project.  An account that does not return it is
    # `None`, never assumed to be production.
    test_account: bool | None = None
    # No reviewed v25 read returns the Cloud project's Test/Explorer/Basic/
    # Standard access level, so this connector discloses that it cannot observe
    # it.  The field exists so that a future reviewed disclosure has one place
    # to land and every reader already handles the honest default.
    cloud_project_access_level: Literal[
        "NOT_OBSERVABLE", "TEST", "EXPLORER", "BASIC", "STANDARD"
    ] = GOOGLE_ADS_CLOUD_PROJECT_ACCESS_NOT_OBSERVABLE
    provider_api_version: Literal["v25"] = GOOGLE_ADS_API_VERSION


class GoogleAdsCampaign(StrictModel):
    schema_version: Literal[1] = 1
    campaign_ref: SafeRef
    campaign_client_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=80)
    bid_strategy: str = Field(min_length=1, max_length=80)
    budget: Money | None = None
    budget_period: str | None = Field(default=None, min_length=1, max_length=80)
    budget_delivery_method: str | None = Field(default=None, min_length=1, max_length=80)
    budget_explicitly_shared: bool | None = None
    start_date: date | None = None
    end_date: date | None = None
    start_date_time: datetime | None = None
    end_date_time: datetime | None = None
    # Presence and enum facts only.  `None` means the fixed read did not return
    # the setting; it never means the setting is off.
    target_google_search: bool | None = None
    target_search_network: bool | None = None
    target_content_network: bool | None = None
    target_partner_search_network: bool | None = None
    positive_geo_target_type: str | None = Field(default=None, min_length=1, max_length=80)
    negative_geo_target_type: str | None = Field(default=None, min_length=1, max_length=80)
    # Whether the separate bounded criterion read returned at least one
    # `AD_SCHEDULE` criterion for this campaign.  No day, hour, or modifier is
    # read or retained.
    ad_schedule_present: bool = False

    @model_validator(mode="after")
    def valid_dates(self) -> GoogleAdsCampaign:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("Campaign end must not precede campaign start")
        return self


class GoogleAdsAdGroup(StrictModel):
    schema_version: Literal[1] = 1
    campaign_ref: SafeRef
    campaign_client_ref: SafeRef
    ad_group_ref: SafeRef
    ad_group_client_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)
    ad_group_type: str = Field(min_length=1, max_length=80)
    bid_amount: Money | None = None


class GoogleAdsAd(StrictModel):
    schema_version: Literal[1] = 1
    campaign_ref: SafeRef
    campaign_client_ref: SafeRef
    ad_group_ref: SafeRef
    ad_group_client_ref: SafeRef
    ad_ref: SafeRef
    ad_client_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)
    ad_type: str = Field(min_length=1, max_length=80)
    # Counts and presence only.  The adapter reads how many responsive-search
    # components and final URLs the ad declared; it retains no headline,
    # description, or destination value, and `creative_capture_status` stays
    # `CREATIVE_NOT_EXPOSED` because a count is not creative capture.
    responsive_search_ad_returned: bool = False
    headline_count: int | None = Field(default=None, ge=0, le=_MAX_CREATIVE_ASSETS)
    description_count: int | None = Field(default=None, ge=0, le=_MAX_CREATIVE_ASSETS)
    final_url_count: int = Field(default=0, ge=0, le=_MAX_FINAL_URLS)
    policy_approval_status: str | None = Field(default=None, min_length=1, max_length=80)
    policy_review_status: str | None = Field(default=None, min_length=1, max_length=80)
    creative_capture_status: Literal["CREATIVE_NOT_EXPOSED"] = "CREATIVE_NOT_EXPOSED"

    @model_validator(mode="after")
    def component_counts_need_their_creative(self) -> GoogleAdsAd:
        if self.responsive_search_ad_returned != (
            self.headline_count is not None and self.description_count is not None
        ):
            raise ValueError("Component counts exist exactly when the creative was returned")
        return self


class GoogleAdsInventoryQuery(StrictModel):
    account_ref: SafeRef
    maximum_records: int = Field(default=10_000, ge=1, le=_MAX_PAGES * _MAX_PAGE_ROWS)


class GoogleAdsReportQuery(StrictModel):
    """Fixed daily delivery query at campaign or native-ad metric grain."""

    account_ref: SafeRef
    report_id: SafeRef
    query_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_start: date
    window_end: date
    source_timezone: str = Field(min_length=1, max_length=80)
    level: Literal["CAMPAIGN", "AD"] = "CAMPAIGN"
    currency: Currency | None = None
    maximum_rows: int = Field(default=10_000, ge=1, le=_MAX_PAGES * _MAX_PAGE_ROWS)
    include_campaign_names: bool = False

    @model_validator(mode="after")
    def valid_window(self) -> GoogleAdsReportQuery:
        if self.window_end < self.window_start:
            raise ValueError("Google Ads report window end must not precede its start")
        if (self.window_end - self.window_start).days > 366:
            raise ValueError("Google Ads report window may include at most 367 days")
        return self


class GoogleAdsInventory(StrictModel):
    schema_version: Literal[1] = 1
    account_ref: SafeRef
    campaigns: list[GoogleAdsCampaign] = Field(
        default_factory=list, max_length=_MAX_PAGES * _MAX_PAGE_ROWS
    )
    ad_groups: list[GoogleAdsAdGroup] = Field(
        default_factory=list, max_length=_MAX_PAGES * _MAX_PAGE_ROWS
    )
    ads: list[GoogleAdsAd] = Field(default_factory=list, max_length=_MAX_PAGES * _MAX_PAGE_ROWS)
    # Every fixed search asks for `searchSettings.returnTotalResultsCount`.
    # `PROVEN_COMPLETE` means every one of this read's searches declared a
    # total and that total equalled the rows retained after the last page
    # token was consumed.  `INCOMPLETE_TOTAL_NOT_DECLARED` means at least one
    # search returned no total, so page-token exhaustion is all this build can
    # show and the read is not proven whole.
    pagination_completeness: Literal["PROVEN_COMPLETE", "INCOMPLETE_TOTAL_NOT_DECLARED"] = (
        "INCOMPLETE_TOTAL_NOT_DECLARED"
    )


@dataclass(frozen=True, slots=True)
class _SearchOutcome:
    """One completed fixed search and how much its page walk proved."""

    rows: list[dict[str, Any]]
    total_declared: bool


class GoogleAdsClient:
    """Fixture-tested, read-only Google Ads API v25 client."""

    def __init__(
        self,
        transport: SafeHttpClient,
        access_token_getter: AccessTokenGetter,
        *,
        reference_for: ReferenceFor = _default_reference_for,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        connector_version: str = "0.1.0.dev0",
    ) -> None:
        if _HOST not in transport.allowed_hosts:
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID", "Google Ads adapter requires its fixed API host."
            )
        self._transport = transport
        self._access_token_getter = access_token_getter
        self._reference_for = reference_for
        self._now = now
        self._connector_version = connector_version

    def discover_identity(self, config: GoogleAdsReadConfig) -> GoogleAdsIdentity:
        # The identity read is fixed, so no caller-selectable bound applies to it.
        # A bound of two lets a second customer reach the exactly-one check below;
        # a response beyond two customers trips the bound, which is reported as
        # the invalid identity it is, never as a remedy the caller cannot act on.
        # No page count is reached first: every continuing page must carry a row,
        # so the bound trips on the third page at the latest.
        try:
            rows = self._search(config, _IDENTITY_QUERY, "identity", maximum_records=2).rows
        except AdhelmError as error:
            if error.code != "RESULT_LIMIT_REQUIRED":
                raise
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Google Ads identity did not return one customer."
            ) from error
        if len(rows) != 1:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Google Ads identity did not return one customer."
            )
        row = rows[0]
        _keys(row, {"customer"}, "identity row")
        customer = row.get("customer")
        if not isinstance(customer, dict):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Google Ads customer identity is invalid."
            )
        _keys(customer, {"id", "currencyCode", "timeZone", "testAccount"}, "customer identity")
        customer_id = _id(customer.get("id"), "customer ID")
        if customer_id != config.customer_client_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "Google Ads customer differs from the configured profile."
            )
        currency = _string(customer.get("currencyCode"), "customer currency", maximum=3)
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Google Ads customer currency is invalid."
            )
        timezone = _string(customer.get("timeZone"), "customer timezone", maximum=80)
        _timezone(timezone)
        return GoogleAdsIdentity(
            account_ref=config.account_ref,
            customer_client_ref=customer_id,
            timezone=timezone,
            currency=currency,
            test_account=_optional_bool(customer.get("testAccount"), "customer test account"),
        )

    def inventory(
        self,
        config: GoogleAdsReadConfig,
        query: GoogleAdsInventoryQuery,
        *,
        identity: GoogleAdsIdentity,
    ) -> GoogleAdsInventory:
        self._matches(config, query.account_ref, identity)
        campaign_search = self._search(
            config, _CAMPAIGN_QUERY, "campaign inventory", query.maximum_records
        )
        # Every campaign row is validated before the next request, so a
        # malformed campaign still fails on the campaign read rather than
        # after a later one.
        campaign_fields = [
            self._campaign_fields(row, identity.currency, identity.timezone)
            for row in campaign_search.rows
        ]
        # The ad-schedule criterion read follows immediately, so a campaign's
        # schedule presence is decided inside the same bounded pass.  A campaign
        # the criterion read names but the campaign read did not return is a
        # mixed response, not a discovery.
        schedule_search = self._search(
            config, _AD_SCHEDULE_QUERY, "campaign ad schedule", query.maximum_records
        )
        scheduled = {self._ad_schedule_campaign(row) for row in schedule_search.rows}
        known = {fields["campaign_client_ref"] for fields in campaign_fields}
        if not scheduled <= known:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "Google Ads ad schedule names a campaign the same read did not return.",
            )
        campaigns = [
            GoogleAdsCampaign(
                **fields, ad_schedule_present=fields["campaign_client_ref"] in scheduled
            )
            for fields in campaign_fields
        ]
        ad_group_search = self._search(
            config, _AD_GROUP_QUERY, "ad group inventory", query.maximum_records
        )
        ad_search = self._search(
            config,
            _AD_QUERY,
            "ad inventory",
            query.maximum_records,
            redact=_redact_ad_creative,
        )
        searches = (campaign_search, schedule_search, ad_group_search, ad_search)
        return GoogleAdsInventory(
            account_ref=query.account_ref,
            campaigns=campaigns,
            ad_groups=[self._ad_group(row, identity.currency) for row in ad_group_search.rows],
            ads=[self._ad(row) for row in ad_search.rows],
            pagination_completeness=(
                "PROVEN_COMPLETE"
                if all(search.total_declared for search in searches)
                else "INCOMPLETE_TOTAL_NOT_DECLARED"
            ),
        )

    def run_daily_report(
        self,
        config: GoogleAdsReadConfig,
        query: GoogleAdsReportQuery,
        *,
        identity: GoogleAdsIdentity,
    ) -> NormalizedReport:
        self._matches(config, query.account_ref, identity)
        if query.source_timezone != identity.timezone:
            raise AdhelmError(
                "TIMEZONE_MISMATCH", "Google Ads report timezone differs from customer identity."
            )
        if query.currency is not None and query.currency != identity.currency:
            raise AdhelmError(
                "CURRENCY_MISMATCH", "Google Ads report currency differs from customer identity."
            )
        if identity.currency != "USD":
            raise AdhelmError(
                "UNSUPPORTED_CURRENCY",
                "Google Ads spend conversion is currently implemented only for USD.",
            )
        # Google stamps `segments.date` in the customer's time zone, so the day
        # still accruing is the customer-local date when the read began, never
        # the UTC date.  It is taken before egress: a walk that crosses
        # customer-local midnight read that day's rows while it was still
        # accruing, so a later clock read must not close it.
        open_day = self._reject_future_window(query, identity)
        gaql = self._report_query(query)
        search = self._search(config, gaql, "daily report", query.maximum_rows)
        observed_at = self._observed_at()
        rows = [self._report_row(row, query, identity.currency, open_day) for row in search.rows]
        self._reject_duplicate_rows(rows)
        statuses: list[QualityStatus] = []
        if search.total_declared:
            statuses.append("COMPLETE")
        if query.window_end >= open_day:
            statuses.append("PROCESSING_PENDING")
        statuses.append("NOT_AVAILABLE_FROM_PROVIDER")
        return NormalizedReport(
            envelope=ReportEnvelope(
                report_id=query.report_id,
                provider="google_ads",
                source_account_ref=query.account_ref,
                query_contract_hash=query.query_contract_hash,
                window_start=query.window_start,
                window_end=query.window_end,
                source_timezone=identity.timezone,
                report_timezone=identity.timezone,
                currency=identity.currency,
                attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
                attribution_window=None,
                observed_at=observed_at,
                data_fresh_through=None,
                # `COMPLETE` is recorded only when the provider declared a
                # total result count that equalled the retained rows after
                # page-token exhaustion.  Without it the envelope stays
                # silent: an audit then reports the row set as having no
                # proven read boundary rather than treating it as the window.
                # It states a read boundary, not final data: a window whose
                # last day is the customer-local current day also carries
                # `PROCESSING_PENDING`.
                data_quality_statuses=statuses,
                provider_api_version=_API_VERSION,
                connector_version=self._connector_version,
            ),
            performance_rows=rows,
            conversion_rows=[
                conversion
                for raw, performance in zip(search.rows, rows, strict=True)
                for conversion in self._conversion_rows(raw, performance, query, identity.currency)
            ],
        )

    def _search(
        self,
        config: GoogleAdsReadConfig,
        gaql: str,
        context: str,
        maximum_records: int,
        *,
        redact: Callable[[dict[str, Any]], None] | None = None,
    ) -> _SearchOutcome:
        results: list[dict[str, Any]] = []
        next_token: str | None = None
        seen_tokens: set[str] = set()
        expected_total: int | None = None
        for _ in range(_MAX_PAGES):
            body: dict[str, Any] = {
                "query": gaql,
                "searchSettings": {"returnTotalResultsCount": True},
            }
            if next_token is not None:
                body["pageToken"] = next_token
            page = _json(self._post_search(config, body), context, redact=redact)
            _keys(
                page,
                {
                    "results",
                    "nextPageToken",
                    "totalResultsCount",
                    "fieldMask",
                    "summaryRow",
                    "queryResourceConsumption",
                    "metricAttributes",
                },
                context,
            )
            _search_metadata(page)
            if page.get("summaryRow") is not None:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "Google Ads unrequested summary row is invalid."
                )
            field_mask = page.get("fieldMask")
            if field_mask is not None:
                _string(field_mask, f"{context} field mask", maximum=4_096)
            page_rows = page.get("results", [])
            if not isinstance(page_rows, list) or len(page_rows) > _MAX_PAGE_ROWS:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", f"Google Ads {context} results are invalid."
                )
            if any(not isinstance(row, dict) for row in page_rows):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", f"Google Ads {context} result is invalid."
                )
            total = page.get("totalResultsCount")
            if total is not None:
                parsed_total = _decimal(total, f"{context} total count")
                if expected_total is None:
                    expected_total = parsed_total
                elif expected_total != parsed_total:
                    raise AdhelmError(
                        "REPORT_CHANGED_DURING_PAGINATION",
                        "Google Ads result count changed during pagination.",
                    )
            if len(results) + len(page_rows) > maximum_records:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "Google Ads result exceeds the configured complete-result bound.",
                    next_action=(
                        "Raise --maximum-records for an inventory or maximum_rows in the report "
                        "query (at most 100,000), or shorten the report window. A result is "
                        "never truncated to fit the bound."
                    ),
                )
            results.extend(page_rows)
            token_value = page.get("nextPageToken")
            if token_value in (None, ""):
                if expected_total is not None and expected_total != len(results):
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "Google Ads result count is incomplete."
                    )
                # Page tokens are exhausted.  Whether that walk is *proven*
                # complete depends on the provider having declared the total it
                # was asked for; an undeclared total is reported, never assumed.
                return _SearchOutcome(rows=results, total_declared=expected_total is not None)
            token = _string(token_value, f"{context} page token", maximum=2_048)
            if not page_rows or token in seen_tokens:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "Google Ads pagination made no safe progress."
                )
            seen_tokens.add(token)
            next_token = token
        raise AdhelmError(
            "PAGINATION_LIMIT",
            "Google Ads result exceeded the bounded page count.",
            next_action=(
                "A fixed read follows at most ten pages of 10,000 rows and is never truncated. "
                "For a report, shorten its window; an inventory above that ceiling cannot be "
                "read completely by this release."
            ),
        )

    def _post_search(self, config: GoogleAdsReadConfig, body: dict[str, Any]) -> httpx.Response:
        headers = {
            "authorization": f"Bearer {_safe_credential(self._access_token_getter)}",
        }
        if config.login_customer_client_ref is not None:
            headers["login-customer-id"] = config.login_customer_client_ref
        try:
            return self._transport.request(
                "POST",
                f"https://{_HOST}/{_API_VERSION}/customers/{config.customer_client_ref}/googleAds:search",
                headers=headers,
                json=body,
                mutation=False,
                classifiable_error_codes=GOOGLE_ADS_CLASSIFIABLE_ERROR_CODES,
            )
        except AdhelmError as error:
            # Classification reads the observed HTTP status and the bounded
            # `Retry-After` the shared transport attached, never a response
            # body.  An unclassified refusal is re-raised unchanged.
            raise copy_provider_diagnostics(
                error, classify_google_ads_transport_error(error)
            ) from None

    def _campaign_fields(
        self, row: dict[str, Any], currency: Currency, timezone: str
    ) -> dict[str, Any]:
        """Validate one campaign row into constructor arguments.

        Ad-schedule presence comes from a second bounded read, so the campaign
        model is built once both are in hand; nothing here defers validation.
        """
        _keys(row, {"campaign", "campaignBudget"}, "campaign inventory row")
        campaign = _object(row.get("campaign"), "campaign")
        _keys(
            campaign,
            {
                "id",
                "status",
                "advertisingChannelType",
                "startDateTime",
                "endDateTime",
                "biddingStrategyType",
                "networkSettings",
                "geoTargetTypeSetting",
            },
            "campaign",
        )
        budget_raw = row.get("campaignBudget")
        budget: Money | None = None
        budget_period: str | None = None
        budget_delivery: str | None = None
        budget_shared: bool | None = None
        if budget_raw is not None:
            budget_obj = _object(budget_raw, "campaign budget")
            _keys(
                budget_obj,
                {"amountMicros", "period", "deliveryMethod", "explicitlyShared"},
                "campaign budget",
            )
            budget = _exact_micros(budget_obj.get("amountMicros"), currency, "campaign budget")
            budget_period = _optional_enum(budget_obj.get("period"), "campaign budget period")
            budget_delivery = _optional_enum(
                budget_obj.get("deliveryMethod"), "campaign budget delivery method"
            )
            budget_shared = _optional_bool(
                budget_obj.get("explicitlyShared"), "campaign budget sharing"
            )
        network = _optional_object(campaign.get("networkSettings"), "campaign network settings")
        if network is not None:
            _keys(
                network,
                {
                    "targetGoogleSearch",
                    "targetSearchNetwork",
                    "targetContentNetwork",
                    "targetPartnerSearchNetwork",
                },
                "campaign network settings",
            )
        geo = _optional_object(campaign.get("geoTargetTypeSetting"), "campaign geo target setting")
        if geo is not None:
            _keys(
                geo,
                {"positiveGeoTargetType", "negativeGeoTargetType"},
                "campaign geo target setting",
            )
        client_ref = _id(campaign.get("id"), "campaign ID")
        # Validate the dates here so a malformed pair fails on this row rather
        # than after the following request.
        start = _campaign_datetime(campaign.get("startDateTime"), timezone)
        end = _campaign_datetime(campaign.get("endDateTime"), timezone)
        if start is not None and end is not None and end < start:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Google Ads campaign schedule is invalid."
            )
        return {
            "campaign_ref": self._reference_for("campaign", client_ref),
            "campaign_client_ref": client_ref,
            "status": _enum(campaign.get("status"), "campaign status"),
            "objective": _enum(campaign.get("advertisingChannelType"), "campaign channel"),
            "bid_strategy": _enum(campaign.get("biddingStrategyType"), "campaign bid strategy"),
            "budget": budget,
            "budget_period": budget_period,
            "budget_delivery_method": budget_delivery,
            "budget_explicitly_shared": budget_shared,
            "start_date": start.date() if start else None,
            "end_date": end.date() if end else None,
            "start_date_time": start,
            "end_date_time": end,
            "target_google_search": _network_flag(network, "targetGoogleSearch"),
            "target_search_network": _network_flag(network, "targetSearchNetwork"),
            "target_content_network": _network_flag(network, "targetContentNetwork"),
            "target_partner_search_network": _network_flag(network, "targetPartnerSearchNetwork"),
            "positive_geo_target_type": (
                None
                if geo is None
                else _optional_enum(geo.get("positiveGeoTargetType"), "positive geo target type")
            ),
            "negative_geo_target_type": (
                None
                if geo is None
                else _optional_enum(geo.get("negativeGeoTargetType"), "negative geo target type")
            ),
        }

    def _ad_schedule_campaign(self, row: dict[str, Any]) -> str:
        """Validate one ad-schedule criterion row and keep only its campaign ID."""
        _keys(row, {"campaign", "campaignCriterion"}, "ad schedule row")
        criterion = _object(row.get("campaignCriterion"), "ad schedule criterion")
        _keys(criterion, {"criterionId", "type"}, "ad schedule criterion")
        _id(criterion.get("criterionId"), "criterion ID")
        if _enum(criterion.get("type"), "criterion type") != "AD_SCHEDULE":
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "Google Ads ad schedule read returned an unrequested criterion type.",
            )
        campaign = _object(row.get("campaign"), "ad schedule campaign")
        _keys(campaign, {"id"}, "ad schedule campaign")
        return _id(campaign.get("id"), "campaign ID")

    def _ad_group(self, row: dict[str, Any], currency: Currency) -> GoogleAdsAdGroup:
        _keys(row, {"campaign", "adGroup"}, "ad group inventory row")
        campaign = _object(row.get("campaign"), "ad group campaign")
        ad_group = _object(row.get("adGroup"), "ad group")
        _keys(campaign, {"id"}, "ad group campaign")
        _keys(ad_group, {"id", "status", "type", "cpcBidMicros"}, "ad group")
        campaign_client_ref = _id(campaign.get("id"), "campaign ID")
        ad_group_client_ref = _id(ad_group.get("id"), "ad group ID")
        return GoogleAdsAdGroup(
            campaign_ref=self._reference_for("campaign", campaign_client_ref),
            campaign_client_ref=campaign_client_ref,
            ad_group_ref=self._reference_for("ad_group", ad_group_client_ref),
            ad_group_client_ref=ad_group_client_ref,
            status=_enum(ad_group.get("status"), "ad group status"),
            ad_group_type=_enum(ad_group.get("type"), "ad group type"),
            bid_amount=_exact_micros(ad_group.get("cpcBidMicros"), currency, "ad group CPC bid"),
        )

    def _ad(self, row: dict[str, Any]) -> GoogleAdsAd:
        _keys(row, {"campaign", "adGroup", "adGroupAd"}, "ad inventory row")
        campaign = _object(row.get("campaign"), "ad campaign")
        ad_group = _object(row.get("adGroup"), "ad group")
        ad_group_ad = _object(row.get("adGroupAd"), "ad")
        _keys(campaign, {"id"}, "ad campaign")
        _keys(ad_group, {"id"}, "ad group")
        _keys(ad_group_ad, {"ad", "status", "policySummary"}, "ad")
        ad = _object(ad_group_ad.get("ad"), "nested ad")
        # `finalUrls` and `responsiveSearchAd` reach this point as the counts
        # `_redact_ad_creative` substituted; no text value is present.
        _keys(ad, {"id", "type", "finalUrls", "responsiveSearchAd"}, "nested ad")
        policy = _optional_object(ad_group_ad.get("policySummary"), "ad policy summary")
        if policy is not None:
            _keys(policy, {"approvalStatus", "reviewStatus"}, "ad policy summary")
        creative = _optional_object(ad.get("responsiveSearchAd"), "responsive search ad")
        if creative is not None:
            _keys(creative, {"headlines", "descriptions"}, "responsive search ad")
        campaign_client_ref = _id(campaign.get("id"), "campaign ID")
        ad_group_client_ref = _id(ad_group.get("id"), "ad group ID")
        ad_client_ref = _id(ad.get("id"), "ad ID")
        return GoogleAdsAd(
            campaign_ref=self._reference_for("campaign", campaign_client_ref),
            campaign_client_ref=campaign_client_ref,
            ad_group_ref=self._reference_for("ad_group", ad_group_client_ref),
            ad_group_client_ref=ad_group_client_ref,
            ad_ref=self._reference_for("ad", ad_client_ref),
            ad_client_ref=ad_client_ref,
            status=_enum(ad_group_ad.get("status"), "ad status"),
            ad_type=_enum(ad.get("type"), "ad type"),
            responsive_search_ad_returned=creative is not None,
            headline_count=(
                None if creative is None else _count(creative.get("headlines"), "RSA headlines")
            ),
            description_count=(
                None
                if creative is None
                else _count(creative.get("descriptions"), "RSA descriptions")
            ),
            final_url_count=_count(ad.get("finalUrls"), "ad final URLs"),
            policy_approval_status=(
                None
                if policy is None
                else _optional_enum(policy.get("approvalStatus"), "ad policy approval status")
            ),
            policy_review_status=(
                None
                if policy is None
                else _optional_enum(policy.get("reviewStatus"), "ad policy review status")
            ),
        )

    def _report_query(self, query: GoogleAdsReportQuery) -> str:
        fields = (
            "campaign.id, segments.date, metrics.impressions, metrics.clicks, metrics.cost_micros"
        )
        source = "campaign"
        order = "segments.date, campaign.id"
        if query.level == "AD":
            fields = (
                "campaign.id, ad_group.id, ad_group_ad.ad.id, segments.date, "
                "metrics.impressions, metrics.clicks, metrics.cost_micros"
            )
            source = "ad_group_ad"
            order = "segments.date, campaign.id, ad_group.id, ad_group_ad.ad.id"
        fields += ", metrics.conversions, metrics.conversions_value, metrics.all_conversions, metrics.all_conversions_value"
        if query.include_campaign_names:
            fields += ", campaign.name"
        return (
            f"SELECT {fields} FROM {source} WHERE segments.date BETWEEN "
            f"'{query.window_start.isoformat()}' AND '{query.window_end.isoformat()}' ORDER BY {order}"
            + _OMIT_RESOURCE_NAMES
        )

    @staticmethod
    def _reject_duplicate_rows(rows: list[PerformanceRow]) -> None:
        # One resource has at most one daily row.  A repeated key would be summed
        # twice by every aggregate, and nothing identifies the authoritative
        # copy, so the whole response is refused rather than repaired.
        if len({(row.resource_ref, row.dimension_value) for row in rows}) != len(rows):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Google Ads report repeated a resource day."
            )

    def _report_row(
        self,
        row: dict[str, Any],
        query: GoogleAdsReportQuery,
        currency: Currency,
        open_day: date,
    ) -> PerformanceRow:
        allowed = {"campaign", "segments", "metrics"}
        if query.level == "AD":
            allowed.add("adGroup")
            allowed.add("adGroupAd")
        _keys(row, allowed, "daily report row")
        campaign = _object(row.get("campaign"), "report campaign")
        segments = _object(row.get("segments"), "report segments")
        metrics = _object(row.get("metrics"), "report metrics")
        _keys(
            campaign, {"id", "name"} if query.include_campaign_names else {"id"}, "report campaign"
        )
        _keys(segments, {"date"}, "report segments")
        _keys(
            metrics,
            {
                "impressions",
                "clicks",
                "costMicros",
                "conversions",
                "conversionsValue",
                "allConversions",
                "allConversionsValue",
            },
            "report metrics",
        )
        report_date = _date(segments.get("date"), "report date")
        if report_date is None or not query.window_start <= report_date <= query.window_end:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Google Ads report date is outside the query."
            )
        campaign_client_ref = _id(campaign.get("id"), "campaign ID")
        campaign_ref = self._reference_for("campaign", campaign_client_ref)
        resource_level: Literal["CAMPAIGN", "AD"] = "CAMPAIGN"
        metric_grain: Literal["CAMPAIGN", "AD"] = "CAMPAIGN"
        resource_type = "GOOGLE_ADS_CAMPAIGN"
        resource_ref = campaign_ref
        parent_ref: str | None = None
        # The customer-local current day is still accruing, so its row is not a
        # settled daily total.
        quality: list[QualityStatus] = [
            "PROCESSING_PENDING" if report_date >= open_day else "COMPLETE"
        ]
        if query.level == "AD":
            ad_group = _object(row.get("adGroup"), "report ad group")
            ad_group_ad = _object(row.get("adGroupAd"), "report ad")
            _keys(ad_group, {"id"}, "report ad group")
            _keys(ad_group_ad, {"ad"}, "report ad")
            ad = _object(ad_group_ad.get("ad"), "report nested ad")
            _keys(ad, {"id"}, "report nested ad")
            ad_group_client_ref = _id(ad_group.get("id"), "ad group ID")
            ad_client_ref = _id(ad.get("id"), "ad ID")
            resource_level = "AD"
            metric_grain = "AD"
            resource_type = "GOOGLE_ADS_AD"
            resource_ref = self._reference_for("ad", ad_client_ref)
            parent_ref = self._reference_for("ad_group", ad_group_client_ref)
            quality.append("CREATIVE_NOT_EXPOSED")
        spend, cost_micros = _reported_micros(metrics.get("costMicros"), currency, "report spend")
        return PerformanceRow(
            report_id=query.report_id,
            resource_level=resource_level,
            metric_grain=metric_grain,
            resource_type=resource_type,
            resource_ref=resource_ref,
            parent_resource_ref=parent_ref,
            campaign_ref=campaign_ref,
            dimension_type="DATE",
            dimension_value=report_date.isoformat(),
            impressions=_decimal(metrics.get("impressions"), "report impressions"),
            clicks=_decimal(metrics.get("clicks"), "report clicks"),
            spend=spend,
            data_quality_statuses=quality,
            provider_extensions={
                "google_ads_cost_micros": cost_micros,
                "google_ads_rounding": "HALF_EVEN_TO_MINOR",
                **(
                    {
                        "google_ads_campaign_name": _string(
                            campaign.get("name"), "campaign name", maximum=255
                        )
                    }
                    if query.include_campaign_names
                    else {}
                ),
            },
        )

    @staticmethod
    def _conversion_rows(
        raw: dict[str, Any],
        performance: PerformanceRow,
        query: GoogleAdsReportQuery,
        currency: Currency,
    ) -> list[ConversionBreakdownRow]:
        metrics = raw["metrics"]
        result: list[ConversionBreakdownRow] = []
        for count_key, value_key, kind in (
            ("conversions", "conversionsValue", "GOOGLE_ADS_CONVERSIONS"),
            ("allConversions", "allConversionsValue", "GOOGLE_ADS_ALL_CONVERSIONS"),
        ):
            if count_key not in metrics and value_key not in metrics:
                continue  # Missing is unavailable, never an invented zero.
            extensions: dict[str, str | int | bool | None] = {
                "google_ads_date": str(performance.dimension_value)
            }
            count = None
            exact_count = None
            amount = None
            for key in (count_key, value_key):
                if key not in metrics:
                    continue
                try:
                    native = canonical_metric_decimal(metrics[key])
                except ValueError:
                    raise AdhelmError(
                        "PROVIDER_RESPONSE_INVALID", "Google Ads conversion metric is invalid."
                    ) from None
                if key == count_key:
                    exact_count = native
                    number = Decimal(native)
                    count = int(number) if number == number.to_integral_value() else None
                else:
                    extensions["google_ads_conversion_value"] = native
                    extensions["google_ads_rounding"] = "HALF_EVEN_TO_MINOR"
                    with localcontext() as context:
                        context.prec = 80
                        minor = int(
                            (Decimal(native) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
                        )
                    if minor > 9_007_199_254_740_991:
                        raise AdhelmError(
                            "PROVIDER_RESPONSE_INVALID",
                            "Google Ads conversion value exceeds the supported money bound.",
                        )
                    amount = Money(currency=currency, minor_units=minor)
            result.append(
                ConversionBreakdownRow(
                    schema_version=2,
                    report_id=query.report_id,
                    campaign_ref=performance.campaign_ref,
                    resource_ref=performance.resource_ref,
                    conversion_source="PROVIDER",
                    conversion_kind=kind,
                    attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
                    conversion_count=count,
                    conversion_count_decimal=exact_count,
                    conversion_value=amount,
                    provider_extensions=extensions,
                    data_quality_statuses=list(
                        dict.fromkeys(
                            [
                                *performance.data_quality_statuses,
                                "NOT_AVAILABLE_FROM_PROVIDER",
                            ]
                        )
                    ),
                )
            )
        return result

    def _matches(
        self, config: GoogleAdsReadConfig, account_ref: str, identity: GoogleAdsIdentity
    ) -> None:
        if config.account_ref != account_ref or identity.account_ref != account_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "Google Ads account differs from discovered identity."
            )
        if identity.customer_client_ref != config.customer_client_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "Google Ads customer differs from discovered identity."
            )

    def _reject_future_window(
        self, query: GoogleAdsReportQuery, identity: GoogleAdsIdentity
    ) -> date:
        """Refuse a future window; return the customer-local date the read began on."""
        today = self._observed_at().astimezone(ZoneInfo(identity.timezone)).date()
        if query.window_end > today:
            raise AdhelmError(
                "INVALID_REPORT_WINDOW",
                "Google Ads report window may not end after the current source-local date.",
            )
        return today

    def _observed_at(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise AdhelmError("CLOCK_INVALID", "Adapter clock must return an aware timestamp.")
        return value.astimezone(UTC)


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Google Ads {context} is invalid.")
    return value


def _optional_object(value: Any, context: str) -> dict[str, Any] | None:
    """An absent nested message is `None`; a present one must be an object."""
    return None if value is None else _object(value, context)


def _optional_bool(value: Any, context: str) -> bool | None:
    """A protobuf JSON boolean, or `None` when the field was not returned.

    A field Google omits at its `false` default is indistinguishable from a
    field the projection never returned, so an absent value stays `None` and no
    reader may treat it as a proven `false`.
    """
    if value is None:
        return None
    if type(value) is not bool:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Google Ads {context} is invalid.")
    return value


def _network_flag(settings: dict[str, Any] | None, field: str) -> bool | None:
    return None if settings is None else _optional_bool(settings.get(field), "campaign network")


def _count(value: Any, context: str) -> int:
    """Read one redacted collection length.

    `_redact_ad_creative` has already replaced the collection with its length,
    so this never sees ad copy or a destination.  A value that is not that
    substituted integer means the redaction contract was not honoured, and the
    read fails rather than continuing with a value of unknown provenance.
    """
    if value is None:
        return 0
    if type(value) is not int or isinstance(value, bool) or not 0 <= value <= _MAX_FINAL_URLS:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Google Ads {context} count is invalid.")
    return value


def _redacted_length(value: Any, maximum: int) -> int:
    """Replace one bounded collection with its length without reading a member.

    Members are checked for their JSON *type* only.  No member value is
    compared, normalized, screened, or placed in a message, so no ad copy or
    destination can escape through this function or through a failure of it.
    """
    if not isinstance(value, list) or len(value) > maximum:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "Google Ads ad component collection is invalid."
        )
    return len(value)


def _redact_ad_creative(page: dict[str, Any]) -> None:
    """Replace ad copy and destination values with their counts, in place.

    This runs on the parsed ad-inventory page before the privacy screen and
    before any other reader.  Google returns responsive-search-ad components
    and final URLs as customer advertising content; this build reads only how
    many of each an ad declared.  Substituting the length here is what makes
    that boundary structural rather than a convention: after this call there is
    no text left in the page for a later parser, log, export, or error to
    reach.
    """
    rows = page.get("results")
    if rows is None:
        return
    if not isinstance(rows, list) or len(rows) > _MAX_PAGE_ROWS:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "Google Ads ad inventory results are invalid."
        )
    for row in rows:
        if not isinstance(row, dict):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Google Ads ad inventory row is invalid."
            )
        ad_group_ad = row.get("adGroupAd")
        if not isinstance(ad_group_ad, dict):
            continue
        ad = ad_group_ad.get("ad")
        if not isinstance(ad, dict):
            continue
        if "finalUrls" in ad:
            ad["finalUrls"] = _redacted_length(ad["finalUrls"], _MAX_FINAL_URLS)
        creative = ad.get("responsiveSearchAd")
        if not isinstance(creative, dict):
            continue
        for field in ("headlines", "descriptions"):
            if field in creative:
                creative[field] = _redacted_length(creative[field], _MAX_CREATIVE_ASSETS)


def _decimal(value: Any, context: str) -> int:
    parsed = _string(value, context, maximum=24)
    amount = _int64_text(parsed)
    if amount is None or amount > 9_007_199_254_740_991:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Google Ads {context} is invalid.")
    return amount
