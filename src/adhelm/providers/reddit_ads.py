"""Fixed read-only Reddit Ads API v3 adapter (fixture-tested only)."""

from __future__ import annotations

import re
from calendar import monthrange
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError, copy_provider_diagnostics
from adhelm.models import Currency, Money, SafeRef, StrictModel
from adhelm.privacy import validate_data
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.transport import SafeHttpClient

REDDIT_ADS_API_HOST: Final = "ads-api.reddit.com"
REDDIT_ADS_USER_AGENT: Final = "adhelm-local/0.1"
REDDIT_ADS_RESOURCE_ID: Final = r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}"
_API = "/api/v3"
_MAX_PAGES = 10
_MAX_RECORDS = 5_000

# The only status values this build reviewed.  No reviewed material enumerates
# Reddit's effective statuses, so a local audit surfaces any other value as
# unreviewed rather than guessing its meaning.  `ACTIVE` is the only value that
# expresses serving intent.
REDDIT_ADS_REVIEWED_STATUSES: Final[tuple[str, ...]] = ("ACTIVE", "PAUSED")
REDDIT_ADS_SERVING_INTENT_STATUS: Final = "ACTIVE"

ReadSurfaceStatus = Literal[
    "IMPLEMENTED_FIXED_READ",
    "IMPLEMENTED_FIXED_READ_SINGLE_RESPONSE",
    "NOT_IN_REVIEWED_FIXED_READ",
    "NOT_DOCUMENTED",
]
# What this adapter's reviewed v3 read actually returns.  A local audit derives
# its coverage map from this table instead of restating it, so adding a field to
# the fixed read is the only way to promote a dimension.
# `NOT_IN_REVIEWED_FIXED_READ` says this build does not retain the field; it is
# not a claim that Reddit documents no such field.  `NOT_DOCUMENTED` says the
# reviewed primary source does not establish the behavior.
# `IMPLEMENTED_FIXED_READ_SINGLE_RESPONSE` is a report read that returns exactly
# one provider response and fails closed when the provider signals a
# continuation, so it is never an exhaustive claim.
REDDIT_ADS_READ_SURFACES: Final[dict[str, ReadSurfaceStatus]] = {
    "ACCOUNT_IDENTITY": "IMPLEMENTED_FIXED_READ",
    "ACCOUNT_SERVING_STATUS": "NOT_IN_REVIEWED_FIXED_READ",
    "ACCOUNT_TIMEZONE": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_OBJECTIVE": "IMPLEMENTED_FIXED_READ",
    "AD_GROUP_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "AD_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_DELIVERY_REPORT": "IMPLEMENTED_FIXED_READ_SINGLE_RESPONSE",
    "AD_DELIVERY_REPORT": "IMPLEMENTED_FIXED_READ_SINGLE_RESPONSE",
    "REPORT_CONTINUATION": "NOT_DOCUMENTED",
    "CAMPAIGN_TARGETING": "NOT_IN_REVIEWED_FIXED_READ",
    "CAMPAIGN_BID_AND_BUDGET": "NOT_IN_REVIEWED_FIXED_READ",
    "CAMPAIGN_RUN_SCHEDULE": "NOT_IN_REVIEWED_FIXED_READ",
    "PLACEMENT": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_CREATIVE_CONTENT": "NOT_IN_REVIEWED_FIXED_READ",
    "AD_REVIEW_STATE": "NOT_IN_REVIEWED_FIXED_READ",
    "LANDING_PAGE_AND_UTM": "NOT_IN_REVIEWED_FIXED_READ",
    "CONVERSION_TRACKING": "NOT_IN_REVIEWED_FIXED_READ",
    "NATIVE_CONVERSION_METRICS": "NOT_IN_REVIEWED_FIXED_READ",
}

TokenGetter = Callable[[], str]
ReferenceFor = Callable[[Literal["account", "campaign", "ad_group", "ad"], str], str]


def _default_reference_for(
    kind: Literal["account", "campaign", "ad_group", "ad"], value: str
) -> str:
    return f"reddit_ads.{kind}.{value}"


def _token(getter: TokenGetter) -> str:
    token = getter()
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 16_384
        or any(ord(c) < 33 for c in token)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED", "Reddit Ads authorization is unavailable.", exit_code=4
        )
    return token


def _explain_denial(error: AdhelmError) -> AdhelmError:
    """Name the recovery for a Reddit denial without changing its code or exit code.

    A Reddit access token lasts one hour, so a `401` is usually an expired token
    that `auth reddit-token refresh` renews, while a `403` is a permission or
    account-role refusal that a refresh cannot fix.  Every other error, and the
    status and retry hint of this one, pass through unchanged.
    """
    if error.code != "PROVIDER_ACCESS_REQUIRED" or error.status_code not in {401, 403}:
        return error
    if error.status_code == 401:
        action = (
            "Reddit access tokens last one hour. For an OS_STORE profile enrolled with "
            "`auth reddit-token enroll`, run `auth reddit-token status --profile <id>`, then "
            "`auth reddit-token refresh --profile <id>`; re-enroll if the refresh is refused. "
            "An EXPLICIT_CI token has no local refresh: whoever configured it must replace it."
        )
    else:
        action = (
            "Reddit refused this account or scope. Confirm the enrolling user can read the ad "
            "account and that the profile enrolled adsread; a refresh will not change this."
        )
    return AdhelmError(
        error.code,
        error.message,
        exit_code=error.exit_code,
        next_action=action,
        status_code=error.status_code,
        retry_after_seconds=error.retry_after_seconds,
    )


def _json(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        data = parse_json(response.content, maximum_bytes=8_388_608)
        validate_data(data)
    except (ValueError, UnicodeDecodeError, AdhelmError) as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Reddit Ads {context} response is invalid."
        ) from exc
    if not isinstance(data, dict):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Reddit Ads {context} response is invalid.")
    return data


def _keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Reddit Ads {context} included an unsupported field."
        )


def _string(value: Any, context: str, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Reddit Ads {context} is invalid.")
    return value


def _id(value: Any, context: str) -> str:
    identifier = _string(value, context, 160)
    if re.fullmatch(REDDIT_ADS_RESOURCE_ID, identifier) is None:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"Reddit Ads {context} is not a resource ID."
        )
    return identifier


def _integer(value: Any, context: str, *, maximum: int = 9_007_199_254_740_991) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"Reddit Ads {context} is invalid.")
    parsed: int = value
    return parsed


def _currency(value: Any) -> Currency:
    currency = _string(value, "currency", 3)
    if currency != "USD":
        raise AdhelmError(
            "UNSUPPORTED_CURRENCY",
            "Reddit Ads exact money conversion is currently reviewed only for USD.",
        )
    return currency


class RedditAdsIdentity(StrictModel):
    account_ref: SafeRef
    account_client_ref: SafeRef
    timezone: str | None = Field(default=None, max_length=80)
    currency: Currency
    provider_api_version: Literal["v3"] = "v3"


class RedditAdsCampaign(StrictModel):
    campaign_ref: SafeRef
    campaign_client_ref: SafeRef
    status: str | None = Field(default=None, max_length=80)
    objective: str | None = Field(default=None, max_length=80)


class RedditAdsAdGroup(StrictModel):
    ad_group_ref: SafeRef
    ad_group_client_ref: SafeRef
    campaign_ref: SafeRef
    status: str | None = Field(default=None, max_length=80)


class RedditAdsAd(StrictModel):
    ad_ref: SafeRef
    ad_client_ref: SafeRef
    ad_group_ref: SafeRef
    campaign_ref: SafeRef
    status: str | None = Field(default=None, max_length=80)


class RedditAdsInventoryQuery(StrictModel):
    account_ref: SafeRef
    maximum_records: int = Field(default=500, ge=1, le=_MAX_RECORDS)


class RedditAdsReportQuery(StrictModel):
    level: Literal["CAMPAIGN", "AD"] = "CAMPAIGN"
    account_ref: SafeRef
    report_id: SafeRef
    query_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_start: date
    window_end: date
    currency: Currency | None = None
    maximum_rows: int = Field(default=500, ge=1, le=_MAX_RECORDS)

    @model_validator(mode="after")
    def valid_window(self) -> RedditAdsReportQuery:
        if self.window_end < self.window_start:
            raise ValueError("Reddit Ads report window end must not precede its start")
        return self


class RedditAdsClient:
    def __init__(
        self,
        transport: SafeHttpClient,
        token_getter: TokenGetter,
        *,
        reference_for: ReferenceFor = _default_reference_for,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        connector_version: str = "0.1.0.dev0",
    ) -> None:
        if REDDIT_ADS_API_HOST not in transport.allowed_hosts:
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID", "Reddit Ads adapter requires its fixed API host."
            )
        self._transport, self._token_getter = transport, token_getter
        self._reference_for, self._now, self._connector_version = (
            reference_for,
            now,
            connector_version,
        )

    def discover_identity(
        self, account_client_ref: str, *, expected_account_client_ref: str | None = None
    ) -> RedditAdsIdentity:
        account = _id(account_client_ref, "configured account ID")
        data = _json(self._get(f"/ad_accounts/{account}"), "account")
        _keys(data, {"data"}, "account")
        body = data.get("data")
        if not isinstance(body, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Reddit Ads account is invalid.")
        _keys(
            body,
            {"id", "currency", "time_zone_id", "status", "type", "business_id", "name"},
            "account",
        )
        returned = _id(body.get("id"), "account ID")
        if returned != account or (
            expected_account_client_ref is not None
            and returned != _id(expected_account_client_ref, "expected account ID")
        ):
            raise AdhelmError("IDENTITY_MISMATCH", "Reddit Ads account differs from the profile.")
        timezone = body.get("time_zone_id")
        return RedditAdsIdentity(
            account_ref=self._reference_for("account", returned),
            account_client_ref=returned,
            timezone=_string(timezone, "account timezone", 80) if timezone is not None else None,
            currency=_currency(body.get("currency")),
        )

    def list_campaigns(
        self, query: RedditAdsInventoryQuery, *, identity: RedditAdsIdentity
    ) -> list[RedditAdsCampaign]:
        return [self._campaign(item, identity) for item in self._list(query, identity, "campaigns")]

    def list_ad_groups(
        self, query: RedditAdsInventoryQuery, *, identity: RedditAdsIdentity
    ) -> list[RedditAdsAdGroup]:
        return [self._ad_group(item, identity) for item in self._list(query, identity, "ad_groups")]

    def list_ads(
        self, query: RedditAdsInventoryQuery, *, identity: RedditAdsIdentity
    ) -> list[RedditAdsAd]:
        return [self._ad(item, identity) for item in self._list(query, identity, "ads")]

    def run_campaign_report(
        self, query: RedditAdsReportQuery, *, identity: RedditAdsIdentity
    ) -> NormalizedReport:
        return self._report(query, identity, "CAMPAIGN")

    def run_ad_report(
        self, query: RedditAdsReportQuery, *, identity: RedditAdsIdentity
    ) -> NormalizedReport:
        return self._report(query, identity, "AD")

    def _list(
        self,
        query: RedditAdsInventoryQuery,
        identity: RedditAdsIdentity,
        edge: Literal["campaigns", "ad_groups", "ads"],
    ) -> list[Any]:
        self._match(query.account_ref, identity)
        path = f"/ad_accounts/{identity.account_client_ref}/{edge}"
        return self._pages(path, query.maximum_records)

    def _pages(self, path: str, maximum: int) -> list[Any]:
        items: list[Any] = []
        next_path = path
        next_query: bytes | None = None
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            data = _json(self._get(next_path, opaque_query=next_query), "list")
            _keys(data, {"data", "pagination"}, "list")
            page, pagination = data.get("data"), data.get("pagination")
            if not isinstance(page, list) or not isinstance(pagination, dict):
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Reddit Ads list page is invalid.")
            _keys(pagination, {"next_url", "previous_url"}, "list pagination")
            if len(items) + len(page) > maximum:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED", "Reddit Ads result exceeds the complete-result bound."
                )
            items.extend(page)
            raw_next = pagination.get("next_url")
            if raw_next is None:
                return items
            next_path, next_query, key = self._next(raw_next, path)
            if key in seen or not page or len(items) == maximum:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "Reddit Ads pagination cannot complete within the result bound.",
                )
            seen.add(key)
        raise AdhelmError("PAGINATION_LIMIT", "Reddit Ads result exceeded the ten-page limit.")

    def _next(self, value: Any, base_path: str) -> tuple[str, bytes, str]:
        raw = _string(value, "pagination URL", 4096)
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError as exc:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Reddit pagination URL is invalid."
            ) from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != REDDIT_ADS_API_HOST
            or parsed.path not in {_API, f"{_API}{base_path}"}
            or parsed.fragment
            or parsed.username
            or parsed.password
            or port not in (None, 443)
            or "\\" in raw
            or any(ord(character) < 32 for character in raw)
        ):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "Reddit Ads pagination URL is outside the fixed operation.",
            )
        if not parsed.query or re.search(r"%(?![0-9a-fA-F]{2})", parsed.query):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Reddit Ads pagination URL is invalid.")
        try:
            raw_query = parsed.query.encode("ascii")
        except UnicodeError as exc:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Reddit cursor must be URL encoded."
            ) from exc
        return parsed.path[len(_API) :], raw_query, raw

    def _report(
        self,
        query: RedditAdsReportQuery,
        identity: RedditAdsIdentity,
        expected: Literal["CAMPAIGN", "AD"],
    ) -> NormalizedReport:
        self._match(query.account_ref, identity)
        if query.level != expected:
            raise AdhelmError(
                "QUERY_GRAIN_MISMATCH", "Selected report method differs from query grain."
            )
        if query.currency is not None and query.currency != identity.currency:
            raise AdhelmError(
                "CURRENCY_MISMATCH", "Reddit Ads report currency differs from account identity."
            )
        observed = self._observed()
        if query.window_end > observed.date():
            raise AdhelmError(
                "INVALID_REPORT_WINDOW", "Reddit Ads report window may not end in the future."
            )
        earliest = date(
            observed.year - 2,
            observed.month,
            min(observed.day, monthrange(observed.year - 2, observed.month)[1]),
        )
        if query.window_start < earliest:
            raise AdhelmError(
                "REPORT_OUTSIDE_RETENTION",
                "Reddit delivery history is limited to the preceding 24 calendar months.",
            )
        breakdowns = ["campaign_id", "date"] if expected == "CAMPAIGN" else ["ad_id", "date"]
        body = {
            "data": {
                "breakdowns": breakdowns,
                "fields": ["impressions", "clicks", "spend"],
                "starts_at": f"{query.window_start.isoformat()}T00:00:00Z",
                "ends_at": f"{query.window_end.isoformat()}T23:59:59Z",
                "time_zone_id": "UTC",
            }
        }
        data = _json(
            self._post(f"/ad_accounts/{identity.account_client_ref}/reports", body), "report"
        )
        _keys(data, {"data", "pagination"}, "report")
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Reddit report must contain metrics data."
            )
        _keys(payload, {"metrics", "metrics_updated_at"}, "report data")
        updated = payload.get("metrics_updated_at")
        if updated is not None:
            try:
                refreshed_at = datetime.fromisoformat(_string(updated, "metrics update time", 80))
                if refreshed_at.tzinfo is None:
                    raise ValueError("Naive provider timestamp")
            except ValueError as exc:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "Reddit metrics update time is invalid."
                ) from exc
            # This is the provider's calculation time, not data-fresh-through proof.
        pagination = data.get("pagination")
        if pagination is not None:
            if not isinstance(pagination, dict):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "Reddit report pagination is invalid."
                )
            _keys(pagination, {"next_url", "previous_url"}, "report pagination")
            if pagination.get("next_url") is not None:
                raise AdhelmError(
                    "REPORT_PAGINATION_UNVERIFIED",
                    "Reddit signalled a report continuation whose method is not documented.",
                    next_action=(
                        "Narrow the report window or use the campaign grain so the provider "
                        "returns one response; no continuation request is guessed and no "
                        "partial report is returned."
                    ),
                )
        rows = payload.get("metrics")
        if not isinstance(rows, list):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Reddit report metrics are invalid.")
        if len(rows) > query.maximum_rows:
            raise AdhelmError(
                "RESULT_LIMIT_REQUIRED",
                "Reddit Ads report cannot complete within the fixed result bound.",
            )
        performance = [self._report_row(row, query, identity, expected) for row in rows]
        keys = [(row.resource_ref, row.dimension_value) for row in performance]
        if len(set(keys)) != len(keys):
            # One fixed breakdown yields one row per resource and day. A repeat
            # would double-count delivery and spend, so it never reaches a total.
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Reddit Ads report repeated a resource and day."
            )
        return NormalizedReport(
            envelope=ReportEnvelope(
                report_id=query.report_id,
                provider="reddit_ads",
                source_account_ref=query.account_ref,
                query_contract_hash=query.query_contract_hash,
                window_start=query.window_start,
                window_end=query.window_end,
                source_timezone="UTC",
                report_timezone="UTC",
                currency=identity.currency,
                attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
                observed_at=observed,
                data_fresh_through=None,
                data_quality_statuses=["PROCESSING_PENDING", "NOT_AVAILABLE_FROM_PROVIDER"],
                provider_api_version="v3",
                connector_version=self._connector_version,
            ),
            performance_rows=performance,
        )

    def _report_row(
        self,
        value: Any,
        query: RedditAdsReportQuery,
        identity: RedditAdsIdentity,
        level: Literal["CAMPAIGN", "AD"],
    ) -> PerformanceRow:
        if not isinstance(value, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Reddit Ads report row is invalid.")
        id_field = "campaign_id" if level == "CAMPAIGN" else "ad_id"
        allowed = {"date", "hour", id_field, "impressions", "clicks", "spend"}
        _keys(value, allowed, "report row")
        try:
            raw_day = _string(value.get("date"), "report day", 10)
            day = date.fromisoformat(raw_day)
            if raw_day != day.isoformat():
                raise ValueError("Report date must have canonical calendar form")
        except ValueError as exc:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Reddit Ads report day is invalid."
            ) from exc
        if not query.window_start <= day <= query.window_end:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Reddit Ads report day is invalid.")
        if value.get("hour") is not None:
            try:
                hour = datetime.fromisoformat(_string(value["hour"], "report hour", 80))
                if (
                    hour.tzinfo is None
                    or hour.utcoffset() != UTC.utcoffset(hour)
                    or hour.date() != day
                    or any((hour.hour, hour.minute, hour.second, hour.microsecond))
                ):
                    raise ValueError("Daily report hour conflicts with UTC date grain")
            except ValueError as exc:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "Reddit daily report hour is invalid."
                ) from exc
        common: dict[str, Any] = {
            "report_id": query.report_id,
            "dimension_type": "DATE",
            "dimension_value": day.isoformat(),
            "impressions": _integer(value.get("impressions"), "impressions"),
            "clicks": _integer(value.get("clicks"), "clicks"),
            "join_status": "UNMAPPED",
        }
        source_micros = _integer(value.get("spend"), "spend", maximum=9_223_372_036_854_775_807)
        rounded_minor = int(
            (Decimal(source_micros) / 10_000).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
        )
        common["spend"] = Money(minor_units=rounded_minor, currency=identity.currency)
        common["provider_extensions"] = {
            "reddit_ads_spend_micros": str(source_micros),
            "reddit_ads_rounding": "HALF_EVEN_TO_MINOR",
        }
        if level == "CAMPAIGN":
            campaign = _id(value.get("campaign_id"), "campaign ID")
            return PerformanceRow(
                **common,
                campaign_ref=self._reference_for("campaign", campaign),
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="REDDIT_CAMPAIGN",
                resource_ref=self._reference_for("campaign", campaign),
            )
        ad = _id(value.get("ad_id"), "ad ID")
        return PerformanceRow(
            **common,
            resource_level="AD",
            metric_grain="AD",
            resource_type="REDDIT_AD",
            resource_ref=self._reference_for("ad", ad),
            data_quality_statuses=["CREATIVE_NOT_EXPOSED", "COMBINATION_UNKNOWN"],
        )

    def _campaign(self, value: Any, identity: RedditAdsIdentity) -> RedditAdsCampaign:
        if not isinstance(value, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Reddit Ads campaign is invalid.")
        _keys(
            value,
            {
                "id",
                "ad_account_id",
                "configured_status",
                "effective_status",
                "objective",
                "name",
                "type",
                "is_max",
            },
            "campaign",
        )
        if _id(value.get("ad_account_id"), "campaign account") != identity.account_client_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "Reddit Ads campaign belongs to another account."
            )
        identifier = _id(value.get("id"), "campaign ID")
        status = value.get("effective_status", value.get("configured_status"))
        return RedditAdsCampaign(
            campaign_ref=self._reference_for("campaign", identifier),
            campaign_client_ref=identifier,
            status=_string(status, "campaign status", 80) if status is not None else None,
            objective=_string(value["objective"], "campaign objective", 80)
            if value.get("objective") is not None
            else None,
        )

    def _ad_group(self, value: Any, identity: RedditAdsIdentity) -> RedditAdsAdGroup:
        if not isinstance(value, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Reddit Ads ad group is invalid.")
        _keys(
            value,
            {"id", "ad_account_id", "campaign_id", "configured_status", "effective_status", "name"},
            "ad group",
        )
        if _id(value.get("ad_account_id"), "ad group account") != identity.account_client_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "Reddit Ads ad group belongs to another account."
            )
        identifier = _id(value.get("id"), "ad group ID")
        status = value.get("effective_status", value.get("configured_status"))
        return RedditAdsAdGroup(
            ad_group_ref=self._reference_for("ad_group", identifier),
            ad_group_client_ref=identifier,
            campaign_ref=self._reference_for(
                "campaign", _id(value.get("campaign_id"), "ad group campaign")
            ),
            status=_string(status, "ad group status", 80) if status is not None else None,
        )

    def _ad(self, value: Any, identity: RedditAdsIdentity) -> RedditAdsAd:
        if not isinstance(value, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Reddit Ads ad is invalid.")
        _keys(
            value,
            {
                "id",
                "ad_account_id",
                "campaign_id",
                "ad_group_id",
                "configured_status",
                "effective_status",
                "name",
            },
            "ad",
        )
        if _id(value.get("ad_account_id"), "ad account") != identity.account_client_ref:
            raise AdhelmError("IDENTITY_MISMATCH", "Reddit Ads ad belongs to another account.")
        identifier = _id(value.get("id"), "ad ID")
        status = value.get("effective_status", value.get("configured_status"))
        return RedditAdsAd(
            ad_ref=self._reference_for("ad", identifier),
            ad_client_ref=identifier,
            ad_group_ref=self._reference_for(
                "ad_group", _id(value.get("ad_group_id"), "ad group ID")
            ),
            campaign_ref=self._reference_for(
                "campaign", _id(value.get("campaign_id"), "campaign ID")
            ),
            status=_string(status, "ad status", 80) if status is not None else None,
        )

    def _match(self, account_ref: str, identity: RedditAdsIdentity) -> None:
        # Typed identities can be supplied directly; SafeRef is not a URL path segment.
        account = _id(identity.account_client_ref, "identity account ID")
        if account_ref != identity.account_ref or identity.account_ref != self._reference_for(
            "account", account
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "Reddit Ads query account differs from identity."
            )

    def _observed(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise AdhelmError("TIME_SOURCE_INVALID", "Reddit Ads clock needs a timezone.")
        return value.astimezone(UTC)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {_token(self._token_getter)}",
            "User-Agent": REDDIT_ADS_USER_AGENT,
        }

    def _get(self, path: str, *, opaque_query: bytes | None = None) -> httpx.Response:
        return self._request("GET", path, opaque_read_query=opaque_query)

    def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self._request("POST", path, json=body)

    def _request(self, method: str, path: str, **options: Any) -> httpx.Response:
        try:
            return self._transport.request(
                method,
                f"https://{REDDIT_ADS_API_HOST}{_API}{path}",
                headers=self._headers(),
                **options,
            )
        except AdhelmError as error:
            explained = copy_provider_diagnostics(error, _explain_denial(error))
            if explained is error:
                # Not a denial this adapter explains: re-raise untouched so the
                # original exception chain (for example a transport failure's
                # cause) survives.
                raise
            raise explained from None
