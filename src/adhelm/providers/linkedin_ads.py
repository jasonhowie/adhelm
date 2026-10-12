"""Fixed, read-only LinkedIn Marketing API reporting.

This adapter only uses the documented Marketing REST host and fixed Ad Analytics
finder.  It deliberately has no mutation or generic query capability.  LinkedIn
requires search criteria for campaign-resource search, and this bounded adapter
does not have a documented exhaustive criterion; hierarchy inventory therefore
fails explicitly instead of pretending that a partial status search is complete.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from typing import Any, Final, Literal
from urllib.parse import quote

import httpx
from pydantic import Field, model_validator

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Currency, Money, SafeRef, StrictModel
from adhelm.privacy import validate_data
from adhelm.reporting import NormalizedReport, PerformanceRow, QualityStatus, ReportEnvelope
from adhelm.transport import SafeHttpClient

LINKEDIN_ADS_API_HOST: Final = "api.linkedin.com"
# The documented Advertising API member permissions this adapter depends on.
# Account identity needs `r_ads`; the fixed Ad Analytics report needs
# `r_ads_reporting`. This adapter reads them only as a local precondition; a
# granted scope is never a claim about ad-account role or app approval.
LINKEDIN_ADS_IDENTITY_SCOPE: Final = "r_ads"
LINKEDIN_ADS_REPORTING_SCOPE: Final = "r_ads_reporting"
_HOST = LINKEDIN_ADS_API_HOST
# Microsoft Learn versioning page identifies 202608 as the latest published
# Marketing Version at the time this connector contract was reviewed.
_API_VERSION: Literal["202608"] = "202608"
_MAX_PAGE_SIZE = 1_000
_MAX_PAGES = 10
_USD = "USD"
_GROUP_STATUSES = "ACTIVE,ARCHIVED,CANCELED,DRAFT,PAUSED,PENDING_DELETION,REMOVED"
_CAMPAIGN_STATUSES = "ACTIVE,PAUSED,ARCHIVED,COMPLETED,CANCELED,DRAFT,PENDING_DELETION,REMOVED"
_CAMPAIGN_GROUP_INVENTORY_FIELDS = "id,account,status"
_CAMPAIGN_INVENTORY_FIELDS = "id,account,campaignGroup,status,objectiveType"
_CREATIVE_INVENTORY_FIELDS = "id,account,campaign,intendedStatus"
_MAX_MONEY_MINOR_UNITS = 9_007_199_254_740_991
_DECIMAL_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]+)?")

# The status enumerations this adapter compiles into its mandatory search
# criteria.  A local audit compares a retained snapshot status against these
# reviewed values so an unreviewed value is surfaced rather than guessed at.
LINKEDIN_ADS_REVIEWED_CAMPAIGN_GROUP_STATUSES: Final[tuple[str, ...]] = tuple(
    _GROUP_STATUSES.split(",")
)
LINKEDIN_ADS_REVIEWED_CAMPAIGN_STATUSES: Final[tuple[str, ...]] = tuple(
    _CAMPAIGN_STATUSES.split(",")
)
# The Creatives read returns `intendedStatus`, which the reviewed 202608
# contract does not enumerate for this adapter.  An empty reviewed set means a
# local audit makes no claim about whether a creative status value is valid.
LINKEDIN_ADS_REVIEWED_CREATIVE_STATUSES: Final[tuple[str, ...]] = ()
# `ACTIVE` is the only value in either reviewed enumeration that expresses
# serving intent.  Every other reviewed value is non-serving.
LINKEDIN_ADS_SERVING_INTENT_STATUS: Final = "ACTIVE"

ReadSurfaceStatus = Literal[
    "IMPLEMENTED_FIXED_READ",
    "IMPLEMENTED_FIXED_READ_BOUNDED_NON_EXHAUSTIVE",
    "NOT_IN_REVIEWED_FIXED_READ",
    "NOT_DOCUMENTED",
]
# What this adapter's reviewed 202608 read actually returns.  A local audit
# derives its coverage map from this table instead of restating it, so adding a
# field to the fixed read is the only way to promote a dimension.
# `NOT_IN_REVIEWED_FIXED_READ` says this build does not request the field; it is
# not a claim that LinkedIn documents no such field anywhere.  `NOT_DOCUMENTED`
# says the reviewed primary source documents no such read for this contract.
LINKEDIN_ADS_READ_SURFACES: Final[dict[str, ReadSurfaceStatus]] = {
    "ACCOUNT_IDENTITY": "IMPLEMENTED_FIXED_READ",
    "ACCOUNT_SERVING_STATUSES": "NOT_IN_REVIEWED_FIXED_READ",
    "ACCOUNT_TIMEZONE": "NOT_DOCUMENTED",
    "CAMPAIGN_GROUP_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_OBJECTIVE": "IMPLEMENTED_FIXED_READ",
    "CREATIVE_INVENTORY": "IMPLEMENTED_FIXED_READ",
    "CREATIVE_CAMPAIGN_ASSOCIATION": "IMPLEMENTED_FIXED_READ",
    "CAMPAIGN_DELIVERY_REPORT": "IMPLEMENTED_FIXED_READ_BOUNDED_NON_EXHAUSTIVE",
    # `run_ad_report` compiles the public AD grain to LinkedIn's documented
    # CREATIVE reporting pivot on the same Ad Analytics finder.  A creative-level
    # delivery read therefore exists; a creative *content* read does not.
    "CREATIVE_DELIVERY_REPORT": "IMPLEMENTED_FIXED_READ_BOUNDED_NON_EXHAUSTIVE",
    "CAMPAIGN_TARGETING_CRITERIA": "NOT_IN_REVIEWED_FIXED_READ",
    "CAMPAIGN_BID_AND_BUDGET": "NOT_IN_REVIEWED_FIXED_READ",
    "CAMPAIGN_RUN_SCHEDULE": "NOT_IN_REVIEWED_FIXED_READ",
    "PLACEMENT": "NOT_IN_REVIEWED_FIXED_READ",
    "CREATIVE_CONTENT": "NOT_IN_REVIEWED_FIXED_READ",
    "CREATIVE_REVIEW_STATE": "NOT_IN_REVIEWED_FIXED_READ",
    "LANDING_PAGE_AND_UTM": "NOT_IN_REVIEWED_FIXED_READ",
    "CONVERSION_TRACKING": "NOT_IN_REVIEWED_FIXED_READ",
    "NATIVE_CONVERSION_METRICS": "NOT_IN_REVIEWED_FIXED_READ",
    "ANALYTICS_PAGINATION": "NOT_DOCUMENTED",
}

TokenGetter = Callable[[], str]
ReferenceFor = Callable[[Literal["account", "campaign_group", "campaign", "creative"], str], str]


def _default_reference_for(
    kind: Literal["account", "campaign_group", "campaign", "creative"], value: str
) -> str:
    return f"linkedin_ads.{kind}.{value}"


def _token(getter: TokenGetter) -> str:
    value = getter()
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16_384
        or any(ord(char) < 33 for char in value)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "LinkedIn Ads authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local LinkedIn Ads read profile.",
        )
    return value


def check_linkedin_ads_scopes(
    granted_scopes: object, *, required: tuple[str, ...]
) -> tuple[str, ...]:
    """Check locally recorded permissions before a fixed read; never a provider claim.

    This compares only what the local enrollment recorded against what the
    requested read needs.  LinkedIn does not return granted permissions on a
    Marketing REST read, so a passing check proves the local profile is
    self-consistent, not that the provider will authorize the call.
    """
    if (
        type(granted_scopes) is not tuple
        or not 1 <= len(granted_scopes) <= 8
        or any(type(scope) is not str for scope in granted_scopes)
        or len(set(granted_scopes)) != len(granted_scopes)
    ):
        raise AdhelmError(
            "LINKEDIN_ADS_SCOPE_STATE_INVALID",
            "The locally recorded LinkedIn permissions are invalid.",
            exit_code=4,
            next_action="Re-enroll the configured local LinkedIn Ads profile.",
        )
    missing = tuple(scope for scope in required if scope not in granted_scopes)
    if missing:
        raise AdhelmError(
            "LINKEDIN_ADS_SCOPE_MISSING",
            "The local LinkedIn profile did not record every permission this read needs.",
            exit_code=4,
            next_action=(
                "Re-enroll with r_ads for account identity and r_ads_reporting for delivery "
                "reporting; LinkedIn invalidates earlier tokens when the requested scope set "
                "changes."
            ),
        )
    return tuple(granted_scopes)


def classify_linkedin_ads_transport_error(error: AdhelmError, *, permission: str) -> AdhelmError:
    """Turn one shared-transport refusal into an exact LinkedIn Ads diagnostic.

    The shared transport records the observed HTTP status and, when the
    provider sent a whole-second ``Retry-After``, its bounded value.  Nothing
    from the response body is available here, so no classification can leak
    one.  ``permission`` names the OAuth permission the refused read needs.  A
    status this contract does not classify is returned unchanged rather than
    relabelled into a quota or credential problem it was never shown to be.
    """
    status = getattr(error, "status_code", None)
    retry_after = getattr(error, "retry_after_seconds", None)
    if status in {401, 403}:
        return AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "LinkedIn Ads denied the configured local read profile.",
            exit_code=int(ExitCode.AUTHORIZATION),
            next_action=(
                "Run `adhelm auth linkedin-token status --profile <id>`, then confirm the profile "
                f"recorded {permission} and that the member holds a role on this ad account. "
                "LinkedIn invalidates earlier tokens when the requested scope set changes. "
                "Refresh or re-enroll locally; do not retry the same value."
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
            "LINKEDIN_ADS_RATE_LIMITED",
            "LinkedIn refused the fixed read for exceeding a request quota.",
            exit_code=int(ExitCode.PRECONDITION),
            next_action=(
                f"{disclosed} LinkedIn shows its application, member and resource quotas in "
                "its developer analytics, not in any API response, so this build cannot name "
                "the exhausted one. Wait, then run the same bounded read once more; do not "
                "retry in a loop."
            ),
            status_code=status,
            retry_after_seconds=retry_after,
        )
    return error


def _json(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        parsed = parse_json(response.content, maximum_bytes=8_388_608)
    except (ValueError, UnicodeDecodeError, AdhelmError) as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} response is not JSON."
        ) from exc
    if not isinstance(parsed, dict):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} response has an invalid shape."
        )
    try:
        validate_data(parsed)
    except AdhelmError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "LinkedIn Ads response contains prohibited data."
        ) from exc
    return parsed


def _keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} included an unsupported field."
        )


def _string(value: Any, context: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid.")
    try:
        validate_data(value)
    except AdhelmError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "LinkedIn Ads response contains prohibited data."
        ) from exc
    return value


def _id(value: Any, context: str) -> str:
    # LinkedIn schema calls account and resource identifiers ``long``.  The
    # REST JSON representation may therefore be a JSON integer; configuration
    # and URN suffixes are decimal strings.  Both are exact only below int64.
    if isinstance(value, bool):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid.")
    if isinstance(value, int):
        if value < 0 or value > 9_223_372_036_854_775_807:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid.")
        return str(value)
    parsed = _string(value, context, maximum=20)
    # `str.isdecimal` admits non-ASCII digits that `int` converts, which would
    # put a lookalike identifier into a request path.
    if not (parsed.isascii() and parsed.isdecimal()) or int(parsed) > 9_223_372_036_854_775_807:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid.")
    return parsed


def _account_id(value: str, context: str) -> str:
    # Configuration accepts only an account's internal decimal ID, never a URN.
    return _id(value, context)


def _integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid.")
    if value > 9_007_199_254_740_991:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} exceeds the numeric bound."
        )
    parsed: int = value
    return parsed


def _currency(value: Any) -> Currency:
    parsed = _string(value, "account currency", maximum=3)
    if len(parsed) != 3 or not parsed.isascii() or not parsed.isupper():
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "LinkedIn Ads account currency is invalid.")
    return parsed


def _money(value: Any, currency: Currency, context: str) -> tuple[Money, str]:
    """Project a LinkedIn BigDecimal to cents and retain the exact source string."""
    if currency != _USD:
        raise AdhelmError(
            "UNSUPPORTED_CURRENCY",
            "LinkedIn Ads exact monetary reporting is currently reviewed only for USD.",
        )
    # BigDecimal wire values must arrive as strings here. Parsing a JSON float
    # would lose the source decimal representation before it is retained.
    raw = _string(value, context, maximum=80)
    if not raw.isascii() or _DECIMAL_AMOUNT.fullmatch(raw) is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid.")
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid."
        ) from exc
    if not amount.is_finite() or amount < 0:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid.")
    with localcontext() as context_precision:
        context_precision.prec = 100
        minor = (amount * Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    if minor > _MAX_MONEY_MINOR_UNITS:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID",
            f"LinkedIn Ads {context} exceeds the supported USD money bound.",
        )
    return Money(minor_units=int(minor), currency=currency), raw


def _urn(
    value: Any,
    kind: Literal["account", "campaign_group", "campaign", "creative"],
    context: str,
) -> str:
    raw = _string(value, context, maximum=100)
    prefix = {
        "account": "urn:li:sponsoredAccount:",
        "campaign_group": "urn:li:sponsoredCampaignGroup:",
        "campaign": "urn:li:sponsoredCampaign:",
        "creative": "urn:li:sponsoredCreative:",
    }[kind]
    if not raw.startswith(prefix):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid.")
    return _id(raw[len(prefix) :], context)


def _utc_day(value: Any, context: str) -> date:
    if not isinstance(value, dict):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid.")
    _keys(value, {"year", "month", "day"}, context)
    for name in ("year", "month", "day"):
        if isinstance(value.get(name), bool) or not isinstance(value.get(name), int):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid.")
    try:
        return date(value["year"], value["month"], value["day"])
    except ValueError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid."
        ) from exc


def _date_range(value: Any, context: str) -> tuple[date, date]:
    if not isinstance(value, dict):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid.")
    _keys(value, {"start", "end"}, context)
    start, end = (
        _utc_day(value.get("start"), f"{context} start"),
        _utc_day(value.get("end"), f"{context} end"),
    )
    if end < start:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", f"LinkedIn Ads {context} is invalid.")
    return start, end


def _restli_date(value: date) -> str:
    return f"(year:{value.year},month:{value.month},day:{value.day})"


def _wire_query(*parts: str) -> bytes:
    """Preserve the fixed Rest.li 2.0 query grammar on the request wire."""
    return "&".join(parts).encode("ascii")


def _sponsored_urn(kind: Literal["Account", "Campaign"], identifier: str) -> str:
    # A URN is a scalar inside a Rest.li List, so encode its colons. The List
    # and record delimiters surrounding it are Rest.li grammar and stay literal.
    return f"urn%3Ali%3Asponsored{kind}%3A{quote(identifier, safe='')}"


class LinkedInAdsIdentity(StrictModel):
    schema_version: Literal[1] = 1
    account_ref: SafeRef
    account_client_ref: SafeRef
    # The documented account schema has no IANA timezone field.  Report dates,
    # unlike account metadata, are documented as UTC by the reporting schema.
    timezone: None = None
    currency: Currency
    status: str | None = Field(default=None, min_length=1, max_length=80)
    provider_api_version: Literal["202608"] = _API_VERSION


class LinkedInAdsCampaignGroup(StrictModel):
    campaign_group_ref: SafeRef
    campaign_group_client_ref: SafeRef
    status: str = Field(min_length=1, max_length=80)


class LinkedInAdsCampaign(StrictModel):
    campaign_ref: SafeRef
    campaign_client_ref: SafeRef
    campaign_group_ref: SafeRef | None = None
    status: str = Field(min_length=1, max_length=80)
    objective: str | None = Field(default=None, min_length=1, max_length=80)


class LinkedInAdsCreative(StrictModel):
    creative_ref: SafeRef
    creative_client_ref: SafeRef
    campaign_ref: SafeRef | None = None
    status: str | None = Field(default=None, min_length=1, max_length=80)


class LinkedInAdsInventoryQuery(StrictModel):
    account_ref: SafeRef
    maximum_records: int = Field(default=500, ge=1, le=_MAX_PAGE_SIZE * _MAX_PAGES)
    page_size: int = Field(default=100, ge=1, le=_MAX_PAGE_SIZE)

    @model_validator(mode="after")
    def pages_are_bounded(self) -> LinkedInAdsInventoryQuery:
        if self.maximum_records > self.page_size * _MAX_PAGES:
            raise ValueError("LinkedIn Ads inventory exceeds the ten-page limit")
        return self


class LinkedInAdsReportQuery(StrictModel):
    # AD compiles to LinkedIn's native CREATIVE pivot.  It maps to an AD-level
    # metric grain without asserting a local creative-version or component join.
    level: Literal["CAMPAIGN", "AD"] = "CAMPAIGN"
    account_ref: SafeRef
    report_id: SafeRef
    query_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_start: date
    window_end: date
    source_timezone: Literal["UTC"] = "UTC"
    currency: Currency | None = None
    maximum_rows: int = Field(default=1_000, ge=1, le=_MAX_PAGE_SIZE)
    page_size: int = Field(default=1_000, ge=1, le=_MAX_PAGE_SIZE)

    @model_validator(mode="after")
    def window_and_pages_are_bounded(self) -> LinkedInAdsReportQuery:
        if self.window_end < self.window_start:
            raise ValueError("LinkedIn Ads report window end must not precede its start")
        if (self.window_end - self.window_start).days > 366:
            raise ValueError("LinkedIn Ads report window may include at most 367 days")
        if self.maximum_rows > self.page_size:
            raise ValueError("LinkedIn Ads report bound may not exceed one response page")
        return self


class LinkedInAdsClient:
    """Pinned-host, fixed-field LinkedIn Ads read client; it has no write methods."""

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
                "TRANSPORT_CONFIG_INVALID",
                "LinkedIn Ads adapter requires its fixed Marketing API host.",
            )
        self._transport = transport
        self._token_getter = token_getter
        self._reference_for = reference_for
        self._now = now
        self._connector_version = connector_version

    def discover_identity(
        self,
        account_client_ref: str,
        *,
        expected_account_client_ref: str | None = None,
        granted_scopes: tuple[str, ...] | None = None,
    ) -> LinkedInAdsIdentity:
        """Read the one fixed account identity used to bind a local profile.

        ``granted_scopes`` is an optional local precondition check against the
        permissions an OAuth enrollment recorded.  It never asserts ad-account
        role, application approval, or any provider-side fact.
        """
        # The lifecycle-specific 401/403 diagnosis is opt-in with
        # ``granted_scopes``. Callers that do not record local permissions --
        # `doctor` and the fixed report workflow -- keep the shared
        # ``PROVIDER_ACCESS_REQUIRED`` code every other provider uses.
        account = _account_id(account_client_ref, "configured account ID")
        if granted_scopes is None:
            data = _json(self._get(f"/adAccounts/{account}"), "account")
        else:
            check_linkedin_ads_scopes(granted_scopes, required=(LINKEDIN_ADS_IDENTITY_SCOPE,))
            data = _json(self._identity_get(f"/adAccounts/{account}"), "account")
        # These are the documented account response fields.  They are accepted
        # only in memory; no raw response is retained.
        _keys(
            data,
            {
                "id",
                "currency",
                "status",
                "servingStatuses",
                "type",
                "version",
                "name",
                "reference",
                "test",
                "changeAuditStamps",
                "notifiedOnCampaignOptimization",
                "notifiedOnCreativeApproval",
                "notifiedOnCreativeRejection",
                "notifiedOnEndOfCampaign",
                "notifiedOnNewFeaturesEnabled",
            },
            "account",
        )
        returned = _id(data.get("id"), "account ID")
        if returned != account or (
            expected_account_client_ref is not None
            and returned != _account_id(expected_account_client_ref, "expected account ID")
        ):
            raise AdhelmError("IDENTITY_MISMATCH", "LinkedIn Ads account differs from the profile.")
        status = data.get("status")
        if status is not None:
            status = _string(status, "account status", maximum=80)
        return LinkedInAdsIdentity(
            account_ref=self._reference_for("account", returned),
            account_client_ref=returned,
            currency=_currency(data.get("currency")),
            status=status,
        )

    def list_campaign_groups(
        self, query: LinkedInAdsInventoryQuery, *, identity: LinkedInAdsIdentity
    ) -> list[LinkedInAdsCampaignGroup]:
        account = self._identity_matches(query.account_ref, identity)
        items = self._search_all(
            f"/adAccounts/{account}/adCampaignGroups",
            _wire_query(
                "q=search",
                f"search=(status:(values:List({_GROUP_STATUSES})))",
                "sortOrder=ASCENDING",
                f"fields={_CAMPAIGN_GROUP_INVENTORY_FIELDS}",
            ),
            query,
        )
        return [self._campaign_group(item, identity) for item in items]

    def list_campaigns(
        self, query: LinkedInAdsInventoryQuery, *, identity: LinkedInAdsIdentity
    ) -> list[LinkedInAdsCampaign]:
        account = self._identity_matches(query.account_ref, identity)
        items = self._search_all(
            f"/adAccounts/{account}/adCampaigns",
            _wire_query(
                "q=search",
                f"search=(status:(values:List({_CAMPAIGN_STATUSES})))",
                "sortOrder=ASCENDING",
                f"fields={_CAMPAIGN_INVENTORY_FIELDS}",
            ),
            query,
        )
        return [self._campaign(item, identity) for item in items]

    def list_creatives(
        self,
        query: LinkedInAdsInventoryQuery,
        *,
        identity: LinkedInAdsIdentity,
        campaigns: list[LinkedInAdsCampaign] | None = None,
    ) -> list[LinkedInAdsCreative]:
        """Read creatives for the campaigns of one inventory read.

        A caller that has just listed the campaigns passes that list, so the
        creative scope is exactly the campaign set it retains and the finder is
        not asked for the same campaigns twice.  Without it the campaigns are read here.
        """
        account = self._identity_matches(query.account_ref, identity)
        if campaigns is None:
            campaigns = self.list_campaigns(query, identity=identity)
        else:
            for campaign in campaigns:
                if campaign.campaign_ref != self._reference_for(
                    "campaign", _id(campaign.campaign_client_ref, "campaign ID")
                ):
                    raise AdhelmError(
                        "IDENTITY_MISMATCH",
                        "LinkedIn Ads campaign reference differs from its campaign ID.",
                    )
        collected: list[LinkedInAdsCreative] = []
        for campaign in campaigns:
            remaining = query.maximum_records - len(collected)
            if remaining <= 0:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "LinkedIn Ads creative inventory exceeds the configured complete-result bound.",
                )
            scoped_query = query.model_copy(
                update={"maximum_records": remaining, "page_size": min(query.page_size, remaining)}
            )
            items = self._search_all(
                f"/adAccounts/{account}/creatives",
                _wire_query(
                    "q=criteria",
                    f"campaigns=List({_sponsored_urn('Campaign', campaign.campaign_client_ref)})",
                    "sortOrder=ASCENDING",
                    f"fields={_CREATIVE_INVENTORY_FIELDS}",
                ),
                scoped_query,
                maximum_page_size=100,
                finder_header=True,
            )
            collected.extend(self._creative(item, identity, campaign) for item in items)
        return collected

    def run_campaign_report(
        self,
        query: LinkedInAdsReportQuery,
        *,
        identity: LinkedInAdsIdentity,
        granted_scopes: tuple[str, ...] | None = None,
    ) -> NormalizedReport:
        return self._report(query, identity, expected="CAMPAIGN", granted_scopes=granted_scopes)

    def run_ad_report(
        self,
        query: LinkedInAdsReportQuery,
        *,
        identity: LinkedInAdsIdentity,
        granted_scopes: tuple[str, ...] | None = None,
    ) -> NormalizedReport:
        return self._report(query, identity, expected="AD", granted_scopes=granted_scopes)

    def _search_all(
        self,
        path: str,
        base_query: bytes,
        query: LinkedInAdsInventoryQuery,
        *,
        maximum_page_size: int = _MAX_PAGE_SIZE,
        finder_header: bool = False,
    ) -> list[Any]:
        collected: list[Any] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        for _ in range(_MAX_PAGES):
            remaining = query.maximum_records - len(collected)
            if remaining <= 0:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "LinkedIn Ads inventory exceeds the configured complete-result bound.",
                )
            page_size = min(query.page_size, remaining, maximum_page_size)
            wire_query = base_query + f"&pageSize={page_size}".encode("ascii")
            if token is not None:
                wire_query += b"&pageToken=" + quote(token, safe="").encode("ascii")
            data = _json(
                self._get(path, opaque_query=wire_query, finder_header=finder_header),
                "inventory search",
            )
            page, token = self._search_page(data)
            if len(page) > page_size:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "LinkedIn Ads inventory page exceeds its request bound.",
                )
            if len(collected) + len(page) > query.maximum_records:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "LinkedIn Ads inventory exceeds the configured complete-result bound.",
                )
            collected.extend(page)
            if token is None:
                return collected
            if token in seen_tokens or not page:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "LinkedIn Ads inventory pagination is invalid."
                )
            seen_tokens.add(token)
            if len(collected) == query.maximum_records:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "LinkedIn Ads inventory exceeds the configured complete-result bound.",
                )
        raise AdhelmError("PAGINATION_LIMIT", "LinkedIn Ads inventory exceeded the ten-page limit.")

    def _search_page(self, data: dict[str, Any]) -> tuple[list[Any], str | None]:
        _keys(data, {"elements", "metadata", "paging"}, "inventory search")
        # Cursor metadata remains the only continuation authority. Some live
        # responses also include an empty legacy paging envelope.
        if "paging" in data:
            paging = data["paging"]
            if not isinstance(paging, dict):
                raise AdhelmError("PROVIDER_RESPONSE_INVALID", "LinkedIn Ads paging is invalid.")
            _keys(paging, {"start", "count", "links"}, "inventory paging")
            for field in ("start", "count"):
                if field in paging:
                    _integer(paging[field], "inventory paging " + field)
            if paging.get("links", []) != []:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "LinkedIn Ads legacy paging links are unsupported."
                )
        elements, metadata = data.get("elements"), data.get("metadata")
        if not isinstance(elements, list) or not isinstance(metadata, dict):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "LinkedIn Ads inventory page is invalid."
            )
        _keys(metadata, {"nextPageToken"}, "inventory metadata")
        next_token = metadata.get("nextPageToken")
        if next_token is not None:
            next_token = _string(next_token, "inventory page token", maximum=4096)
            if any(char.isspace() for char in next_token):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID", "LinkedIn Ads inventory page token is invalid."
                )
        return elements, next_token

    def _campaign_group(
        self, value: Any, identity: LinkedInAdsIdentity
    ) -> LinkedInAdsCampaignGroup:
        if not isinstance(value, dict):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "LinkedIn Ads campaign group is invalid."
            )
        _keys(
            value,
            {"account", "id", "status"},
            "campaign group",
        )
        if (
            _urn(value.get("account"), "account", "campaign group account")
            != identity.account_client_ref
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "LinkedIn Ads campaign group belongs to another account."
            )
        identifier = _id(value.get("id"), "campaign group ID")
        return LinkedInAdsCampaignGroup(
            campaign_group_ref=self._reference_for("campaign_group", identifier),
            campaign_group_client_ref=identifier,
            status=_string(value.get("status"), "campaign group status", maximum=80),
        )

    def _campaign(self, value: Any, identity: LinkedInAdsIdentity) -> LinkedInAdsCampaign:
        if not isinstance(value, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "LinkedIn Ads campaign is invalid.")
        _keys(
            value,
            {"account", "campaignGroup", "id", "objectiveType", "status"},
            "campaign",
        )
        if _urn(value.get("account"), "account", "campaign account") != identity.account_client_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "LinkedIn Ads campaign belongs to another account."
            )
        identifier = _id(value.get("id"), "campaign ID")
        group = value.get("campaignGroup")
        return LinkedInAdsCampaign(
            campaign_ref=self._reference_for("campaign", identifier),
            campaign_client_ref=identifier,
            campaign_group_ref=(
                self._reference_for(
                    "campaign_group", _urn(group, "campaign_group", "campaign group")
                )
                if group is not None
                else None
            ),
            status=_string(value.get("status"), "campaign status", maximum=80),
            objective=(
                _string(value["objectiveType"], "campaign objective", maximum=80)
                if value.get("objectiveType") is not None
                else None
            ),
        )

    def _creative(
        self, value: Any, identity: LinkedInAdsIdentity, campaign: LinkedInAdsCampaign
    ) -> LinkedInAdsCreative:
        if not isinstance(value, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "LinkedIn Ads creative is invalid.")
        _keys(
            value,
            {"account", "campaign", "id", "intendedStatus"},
            "creative",
        )
        if _urn(value.get("account"), "account", "creative account") != identity.account_client_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "LinkedIn Ads creative belongs to another account."
            )
        if (
            _urn(value.get("campaign"), "campaign", "creative campaign")
            != campaign.campaign_client_ref
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "LinkedIn Ads creative belongs to another campaign."
            )
        identifier = _urn(value.get("id"), "creative", "creative ID")
        status = value.get("intendedStatus")
        return LinkedInAdsCreative(
            creative_ref=self._reference_for("creative", identifier),
            creative_client_ref=identifier,
            campaign_ref=campaign.campaign_ref,
            status=(_string(status, "creative status", maximum=80) if status is not None else None),
        )

    def _report(
        self,
        query: LinkedInAdsReportQuery,
        identity: LinkedInAdsIdentity,
        *,
        expected: Literal["CAMPAIGN", "AD"],
        granted_scopes: tuple[str, ...] | None = None,
    ) -> NormalizedReport:
        # Ad Analytics needs the reporting permission, not the account-read one.
        # This is the same opt-in local precondition as identity: a caller that
        # records permissions gets them checked before the request.
        if granted_scopes is not None:
            check_linkedin_ads_scopes(granted_scopes, required=(LINKEDIN_ADS_REPORTING_SCOPE,))
        account = self._identity_matches(query.account_ref, identity)
        if query.level != expected:
            raise AdhelmError(
                "QUERY_GRAIN_MISMATCH", "Selected report method differs from query grain."
            )
        if query.currency is not None and query.currency != identity.currency:
            raise AdhelmError(
                "CURRENCY_MISMATCH", "LinkedIn Ads report currency differs from account identity."
            )
        observed_at = self._observed_at()
        if query.window_end > observed_at.date():
            raise AdhelmError(
                "INVALID_REPORT_WINDOW", "LinkedIn Ads report window may not end in the future."
            )
        # The current official Ad Analytics reference explicitly says this
        # finder does not support pagination.  Do not send undocumented offset
        # controls or treat paging metadata as a complete-result proof.
        data = _json(
            self._get(
                "/adAnalytics",
                opaque_query=self._report_query(query, account),
                permission=LINKEDIN_ADS_REPORTING_SCOPE,
            ),
            "analytics",
        )
        page = self._analytics_page(data)
        if len(page) >= query.maximum_rows:
            raise AdhelmError(
                "RESULT_LIMIT_REQUIRED",
                "LinkedIn Ads report reaches its configured one-response result bound.",
                next_action=(
                    "Narrow the report window, for example to one day per report; the Ad "
                    "Analytics finder documents no pagination, so a response at the bound is "
                    "never continued and may be truncated."
                ),
            )
        rows = [self._analytics_row(item, query, identity) for item in page]
        self._reject_duplicate_rows(rows)
        # The window's last day is still accruing when it is the current UTC
        # day.  Nothing else about freshness is supplied, so nothing else is claimed.
        statuses: list[QualityStatus] = ["NOT_AVAILABLE_FROM_PROVIDER"]
        if query.window_end == observed_at.date():
            statuses.insert(0, "PROCESSING_PENDING")
        return NormalizedReport(
            envelope=ReportEnvelope(
                report_id=query.report_id,
                provider="linkedin_ads",
                source_account_ref=query.account_ref,
                query_contract_hash=query.query_contract_hash,
                window_start=query.window_start,
                window_end=query.window_end,
                source_timezone="UTC",
                report_timezone="UTC",
                currency=identity.currency,
                attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
                attribution_window=None,
                observed_at=observed_at,
                data_fresh_through=None,
                data_quality_statuses=statuses,
                provider_api_version=_API_VERSION,
                connector_version=self._connector_version,
            ),
            performance_rows=rows,
        )

    def _report_query(self, query: LinkedInAdsReportQuery, account: str) -> bytes:
        return _wire_query(
            "q=analytics",
            f"pivot={'CAMPAIGN' if query.level == 'CAMPAIGN' else 'CREATIVE'}",
            "timeGranularity=DAILY",
            f"dateRange=(start:{_restli_date(query.window_start)},end:{_restli_date(query.window_end)})",
            f"accounts=List({_sponsored_urn('Account', account)})",
            "fields=clicks,impressions,costInLocalCurrency,pivotValues,dateRange",
        )

    def _analytics_page(self, data: dict[str, Any]) -> list[Any]:
        _keys(data, {"elements", "paging"}, "analytics")
        elements, paging = data.get("elements"), data.get("paging")
        if not isinstance(elements, list) or not isinstance(paging, dict):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "LinkedIn Ads analytics page is invalid."
            )
        _keys(paging, {"count", "start", "links"}, "analytics paging")
        if _integer(paging.get("start"), "analytics paging start") != 0:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "LinkedIn Ads analytics paging is invalid."
            )
        _integer(paging.get("count"), "analytics paging count")
        links = paging.get("links")
        if links is not None and not isinstance(links, list):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "LinkedIn Ads analytics paging is invalid."
            )
        return elements

    @staticmethod
    def _reject_duplicate_rows(rows: list[PerformanceRow]) -> None:
        # One resource has at most one daily row.  A repeated key would be summed
        # twice by every aggregate, and the finder gives no way to tell which copy
        # is authoritative, so the whole response is refused.
        keys = {(row.resource_ref, row.dimension_value) for row in rows}
        if len(keys) != len(rows):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "LinkedIn Ads analytics repeated a resource day.",
            )

    def _analytics_row(
        self, value: Any, query: LinkedInAdsReportQuery, identity: LinkedInAdsIdentity
    ) -> PerformanceRow:
        if not isinstance(value, dict):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "LinkedIn Ads analytics row is invalid.")
        _keys(
            value,
            {"clicks", "impressions", "costInLocalCurrency", "pivotValues", "dateRange"},
            "analytics row",
        )
        pivots = value.get("pivotValues")
        if not isinstance(pivots, list) or len(pivots) != 1:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "LinkedIn Ads analytics pivot is invalid."
            )
        kind: Literal["campaign", "creative"] = (
            "campaign" if query.level == "CAMPAIGN" else "creative"
        )
        identifier = _urn(pivots[0], kind, "analytics pivot")
        row_start, row_end = _date_range(value.get("dateRange"), "analytics date range")
        if row_start != row_end or not query.window_start <= row_start <= query.window_end:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "LinkedIn Ads analytics daily range is invalid."
            )
        spend, native_cost = _money(
            value.get("costInLocalCurrency"), identity.currency, "analytics cost"
        )
        common: dict[str, Any] = {
            "report_id": query.report_id,
            "dimension_type": "DATE",
            "dimension_value": row_start.isoformat(),
            "impressions": _integer(value.get("impressions"), "analytics impressions"),
            "clicks": _integer(value.get("clicks"), "analytics clicks"),
            "spend": spend,
            "provider_extensions": {
                "linkedin_ads_cost_in_local_currency": native_cost,
                "linkedin_ads_rounding": "HALF_EVEN_TO_MINOR",
            },
            "join_status": "EXACT_PROVIDER_ID",
        }
        if kind == "campaign":
            return PerformanceRow(
                **common,
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="LINKEDIN_SPONSORED_CAMPAIGN",
                resource_ref=self._reference_for("campaign", identifier),
                campaign_ref=self._reference_for("campaign", identifier),
            )
        return PerformanceRow(
            **common,
            resource_level="AD",
            metric_grain="AD",
            resource_type="LINKEDIN_SPONSORED_CREATIVE",
            resource_ref=self._reference_for("creative", identifier),
            data_quality_statuses=["CREATIVE_NOT_EXPOSED", "COMBINATION_UNKNOWN"],
        )

    def _identity_get(self, path: str) -> httpx.Response:
        """Read account identity and diagnose an authorization denial precisely.

        The shared transport reports every 401/403 the same way.  Identity is
        the first call an enrollment or refresh makes, so this is where a
        wrong-slot bearer, an expired token, a missing permission, and a missing
        ad-account role are worth separating for the operator.
        """
        try:
            return self._get(path)
        except AdhelmError as error:
            if error.code != "PROVIDER_ACCESS_REQUIRED":
                raise
            raise AdhelmError(
                "LINKEDIN_ADS_AUTHORIZATION_REQUIRED",
                "LinkedIn denied the account identity read for this profile.",
                exit_code=4,
                next_action=(
                    "Check `auth linkedin-token status`. LinkedIn returns 401 when the stored "
                    "bearer is expired, when a refresh token was stored in the bearer slot "
                    "instead of the access-token slot, or when the member lacks r_ads or an ad "
                    "account role. Refresh or re-enroll locally; do not retry the same value."
                ),
            ) from None

    def _identity_matches(self, account_ref: str, identity: LinkedInAdsIdentity) -> str:
        # Typed identities may be supplied directly; SafeRef is not an account-path type.
        account = _account_id(identity.account_client_ref, "identity account ID")
        if identity.account_ref != self._reference_for("account", account):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "LinkedIn Ads identity reference differs from its account ID."
            )
        if account_ref != identity.account_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "LinkedIn Ads query account differs from identity."
            )
        return account

    def _observed_at(self) -> datetime:
        observed = self._now()
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise AdhelmError(
                "TIME_SOURCE_INVALID", "LinkedIn Ads connector clock needs a timezone."
            )
        return observed.astimezone(UTC)

    def _get(
        self,
        path: str,
        *,
        opaque_query: bytes | None = None,
        finder_header: bool = False,
        permission: str = LINKEDIN_ADS_IDENTITY_SCOPE,
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {_token(self._token_getter)}",
            "Linkedin-Version": _API_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        }
        if finder_header:
            headers["X-RestLi-Method"] = "FINDER"
        try:
            return self._transport.request(
                "GET",
                f"https://{_HOST}/rest{path}",
                headers=headers,
                opaque_read_query=opaque_query,
            )
        except AdhelmError as error:
            # Classify the observed status only; the refusal carries no response
            # body, so a LinkedIn-typed diagnostic can never disclose one.
            raise classify_linkedin_ads_transport_error(error, permission=permission) from None
