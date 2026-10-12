"""Bounded Google Ads reads for one *existing* paused Search hierarchy.

The sealed :mod:`adhelm.google_ads_pause_planning_session` facade owns the whole
Google Ads EDIT credential boundary: the three protected slots, the
configuration transaction, the no-refresh header callback, the one-attempt
transport, and the retention recheck.  Nothing of that is reimplemented here.
This module adds only the *fixed GAQL reads* an ACTIVATE or a MAINTAIN needs
and that neither the M55 PAUSE queries nor the P04 CREATE queries cover: the
same hierarchy, plus the native ``resource_name`` of every entity a status,
budget, bid, or schedule mutation has to address, plus the ad-group bid and the
ad policy summary.

Every query is a compile-time constant except two interpolations that are
validated first: a campaign name restricted to characters this build can place
in a GAQL string literal without escaping, and a positive int64 resource ID.

This reader is shared by both P05 families.  ``adhelm.google_ads_maintain_*``
imports it rather than issuing a second, subtly different set of reads: two
hand-written observers of one hierarchy would be two chances to disagree about
what "the same hierarchy" means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, cast

import httpx

from adhelm import google_ads_pause_planning_session as planning
from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_ads_create_planning_session import quotable_campaign_name
from adhelm.privacy import validate_data
from adhelm.providers.google_ads_activate_contract import (
    GOOGLE_ADS_ACTIVATE_API_VERSION,
    GOOGLE_ADS_ACTIVATE_HOST,
    GoogleAdsLiveHierarchyProjection,
)
from adhelm.providers.google_ads_campaign_state import (
    GoogleAdsEditIdentity,
    _headers_snapshot,
    _validate_transport,
)
from adhelm.providers.google_ads_create_contract import (
    GoogleAdsSearchHierarchyMember,
    rsa_text_digest,
)

_HTTPX_RESPONSE: Final = httpx.Response
_MAX_RESPONSE_BYTES: Final = 65_536
_MAX_ROWS: Final = 200
_ENUM: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$", re.ASCII)
_FIELD_MASK: Final = re.compile(r"^[A-Za-z0-9_.,]{1,4096}$", re.ASCII)
_POSITIVE_INT64: Final = re.compile(r"^[1-9][0-9]{0,18}$", re.ASCII)
_MAX_INT64: Final = 9_223_372_036_854_775_807
_TOTAL: Final = re.compile(r"^(0|[1-9][0-9]{0,3})$", re.ASCII)
_DATE_TIME: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$", re.ASCII)
_NATIVE_REFERENCE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9]*/[1-9][0-9]{0,19}$", re.ASCII)
#: ``customers/{cid}/{collection}/{id}`` or ``.../{parent_id}~{child_id}``.
_RESOURCE_NAME: Final = re.compile(
    r"^customers/[0-9]{10}/[A-Za-z][A-Za-z0-9]*/[1-9][0-9]{0,18}(~[1-9][0-9]{0,18})?$", re.ASCII
)
_RESPONSE_KEYS: Final = frozenset(
    {
        "fieldMask",
        "metricAttributes",
        "nextPageToken",
        "queryResourceConsumption",
        "results",
        "summaryRow",
        "totalResultsCount",
    }
)

#: The exact approval statuses ``AdGroupAdPolicySummary.approval_status`` can
#: carry that this build treats as activatable.  ``DISAPPROVED`` is refused by
#: the planner; ``UNKNOWN``/``UNSPECIFIED`` are refused as unreadable.
ACTIVATABLE_APPROVAL_STATUSES: Final = frozenset(
    {"APPROVED", "APPROVED_LIMITED", "AREA_OF_INTEREST_ONLY"}
)
NOT_READ_APPROVAL_STATUS: Final = "NOT_READ_NO_POLICY_SUMMARY_RETURNED"

_CAMPAIGN_FIELDS: Final = (
    "campaign.id, campaign.resource_name, campaign.name, campaign.status, "
    "campaign.advertising_channel_type, campaign.bidding_strategy_type, "
    "campaign.start_date_time, campaign.end_date_time, "
    "campaign.network_settings.target_google_search, "
    "campaign.network_settings.target_search_network, "
    "campaign.network_settings.target_content_network, "
    "campaign.network_settings.target_partner_search_network, "
    "campaign.network_settings.target_youtube, "
    "campaign.network_settings.target_google_tv_network, "
    "campaign_budget.resource_name, campaign_budget.explicitly_shared, campaign_budget.period, "
    "campaign_budget.delivery_method, campaign_budget.total_amount_micros"
)


def _read_invalid(detail: str) -> AdhelmError:
    return AdhelmError("GOOGLE_ADS_LIVE_READ_INVALID", detail, exit_code=ExitCode.PRECONDITION)


def _response_invalid() -> AdhelmError:
    return AdhelmError(
        "PROVIDER_RESPONSE_INVALID",
        "The Google Ads live hierarchy observation is not a complete allowlisted response.",
        exit_code=ExitCode.PRECONDITION,
    )


def _campaign_query(name: str) -> str:
    return f"SELECT {_CAMPAIGN_FIELDS} FROM campaign WHERE campaign.name = '{name}'"


def _campaign_status_query(name: str) -> str:
    return (
        "SELECT campaign.resource_name, campaign.status "
        f"FROM campaign WHERE campaign.name = '{name}'"
    )


def _criteria_query(campaign_id: str) -> str:
    return (
        "SELECT campaign_criterion.resource_name, campaign_criterion.type, "
        "campaign_criterion.status, campaign_criterion.negative, "
        "campaign_criterion.location.geo_target_constant, "
        "campaign_criterion.language.language_constant, "
        "campaign_criterion.ad_schedule.day_of_week, campaign_criterion.ad_schedule.start_hour, "
        "campaign_criterion.ad_schedule.start_minute, campaign_criterion.ad_schedule.end_hour, "
        "campaign_criterion.ad_schedule.end_minute "
        f"FROM campaign_criterion WHERE campaign.id = {campaign_id}"
    )


def _ad_group_query(campaign_id: str) -> str:
    return (
        "SELECT ad_group.id, ad_group.resource_name, ad_group.name, ad_group.type, "
        "ad_group.status, ad_group.cpc_bid_micros "
        f"FROM ad_group WHERE campaign.id = {campaign_id}"
    )


def _keyword_query(ad_group_id: str) -> str:
    return (
        "SELECT ad_group_criterion.resource_name, ad_group_criterion.status, "
        "ad_group_criterion.negative, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type "
        f"FROM ad_group_criterion WHERE ad_group.id = {ad_group_id} "
        "AND ad_group_criterion.type = KEYWORD"
    )


def _ad_query(ad_group_id: str) -> str:
    return (
        "SELECT ad_group_ad.resource_name, ad_group_ad.status, "
        "ad_group_ad.policy_summary.approval_status, ad_group_ad.ad.final_urls, "
        "ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions "
        f"FROM ad_group_ad WHERE ad_group.id = {ad_group_id}"
    )


@dataclass(frozen=True, slots=True)
class GoogleAdsLiveHierarchyObservation:
    """One observed existing Search hierarchy and every fact a mutation needs.

    ``*_resource_names`` are the native paths this workspace never chose.  They
    stay in memory for the one mutate body and never enter a retained plan, a
    fingerprint, or a readback record: the semantic projection and the
    pseudonymized readback are what history keeps.
    """

    projection: GoogleAdsLiveHierarchyProjection
    campaign_client_ref: str
    ad_group_client_ref: str
    campaign_resource_name: str
    campaign_budget_resource_name: str
    ad_group_resource_name: str
    ad_group_ad_resource_name: str
    campaign_criterion_resource_names: tuple[str, ...]
    ad_schedule_criterion_resource_names: tuple[str, ...]
    ad_group_criterion_resource_names: tuple[str, ...]
    budget_total_amount_micros: int
    ad_group_cpc_bid_micros: int | None
    campaign_status: str
    start_date_time: str
    end_date_time: str
    ad_final_url_count: int
    policy_approval_status: str


class GoogleAdsLiveHierarchyReader:
    """Fixed existing-hierarchy reads over one sealed Google Ads EDIT session."""

    __slots__ = ("_session",)

    def __init__(self, session: planning.GoogleAdsCampaignPausePlanningSession) -> None:
        if type(session) is not planning.GoogleAdsCampaignPausePlanningSession:
            raise _read_invalid("A sealed Google Ads EDIT planning session is required.")
        self._session = session

    def __repr__(self) -> str:
        return "<GoogleAdsLiveHierarchyReader redacted>"

    def discover_identity(self) -> GoogleAdsEditIdentity:
        identity = self._session._checked_reader().discover_identity()
        if type(identity) is not GoogleAdsEditIdentity:
            raise _response_invalid()
        return identity

    def observe_campaign_status(self, campaign_name: str) -> str | None:
        """Return one campaign's status by name, or ``None`` when it is absent.

        This is the only read that tolerates a ``REMOVED`` campaign: it is the
        archive readback.  GAQL's implicit filtering of removed resources is
        undocumented on any page this build could reach, so both outcomes --
        "one row, ``REMOVED``" and "no rows" -- are accepted here and the
        caller decides what each means.
        """

        name = quotable_campaign_name(campaign_name)
        rows = self._search(_campaign_status_query(name))
        if not rows:
            return None
        if len(rows) != 1:
            raise _read_invalid("More than one Google Ads campaign carries the planned name.")
        campaign = _mapping(rows[0].get("campaign"))
        if set(campaign) != {"resourceName", "status"}:
            raise _response_invalid()
        _resource_name(campaign.get("resourceName"))
        return _enum(campaign.get("status"))

    def observe_live_hierarchy(self, campaign_name: str) -> GoogleAdsLiveHierarchyObservation:
        """Read the whole existing hierarchy by its configured campaign name."""

        name = quotable_campaign_name(campaign_name)
        rows = self._search(_campaign_query(name))
        if len(rows) != 1:
            raise _read_invalid(
                "Exactly one Google Ads campaign with the planned name must be readable."
            )
        campaign_row = rows[0]
        campaign = _mapping(campaign_row.get("campaign"))
        budget = _mapping(campaign_row.get("campaignBudget"))
        campaign_id = _positive_id(campaign.get("id"))
        campaign_resource_name = _resource_name(campaign.get("resourceName"))
        budget_resource_name = _resource_name(budget.get("resourceName"))
        members: list[GoogleAdsSearchHierarchyMember] = [
            _budget_member(campaign, budget),
            _campaign_member(campaign),
        ]
        criterion_names: list[str] = []
        schedule_names: list[str] = []
        for row in self._search(_criteria_query(campaign_id)):
            criterion = _mapping(row.get("campaignCriterion"))
            member, resource_name, is_schedule = _campaign_criterion_member(criterion)
            members.append(member)
            criterion_names.append(resource_name)
            if is_schedule:
                schedule_names.append(resource_name)
        ad_groups = self._search(_ad_group_query(campaign_id))
        if len(ad_groups) != 1:
            raise _read_invalid("The Google Ads campaign must hold exactly one ad group.")
        ad_group = _mapping(ad_groups[0].get("adGroup"))
        ad_group_id = _positive_id(ad_group.get("id"))
        ad_group_resource_name = _resource_name(ad_group.get("resourceName"))
        ad_group_member, cpc_bid_micros = _ad_group_member(ad_group)
        members.append(ad_group_member)
        keyword_names: list[str] = []
        for row in self._search(_keyword_query(ad_group_id)):
            member, resource_name = _keyword_member(_mapping(row.get("adGroupCriterion")))
            members.append(member)
            keyword_names.append(resource_name)
        ads = self._search(_ad_query(ad_group_id))
        if len(ads) != 1:
            raise _read_invalid("The Google Ads ad group must hold exactly one ad.")
        ad_member, ad_resource_name, final_url_count, approval_status = _ad_member(
            _mapping(ads[0].get("adGroupAd"))
        )
        members.append(ad_member)
        try:
            projection = GoogleAdsLiveHierarchyProjection(
                campaign_name=_text(campaign.get("name"), maximum=255),
                members=tuple(members),
            )
        except Exception:
            raise _read_invalid(
                "The observed Google Ads hierarchy is not a complete live Search projection."
            ) from None
        return GoogleAdsLiveHierarchyObservation(
            projection=projection,
            campaign_client_ref=campaign_id,
            ad_group_client_ref=ad_group_id,
            campaign_resource_name=campaign_resource_name,
            campaign_budget_resource_name=budget_resource_name,
            ad_group_resource_name=ad_group_resource_name,
            ad_group_ad_resource_name=ad_resource_name,
            campaign_criterion_resource_names=tuple(criterion_names),
            ad_schedule_criterion_resource_names=tuple(schedule_names),
            ad_group_criterion_resource_names=tuple(keyword_names),
            budget_total_amount_micros=int(_micros(budget.get("totalAmountMicros"))),
            ad_group_cpc_bid_micros=cpc_bid_micros,
            campaign_status=_enum(campaign.get("status")),
            start_date_time=_date_time(campaign.get("startDateTime")),
            end_date_time=_date_time(campaign.get("endDateTime")),
            ad_final_url_count=final_url_count,
            policy_approval_status=approval_status,
        )

    # -- private ------------------------------------------------------------

    def _search(self, query: str) -> tuple[dict[str, object], ...]:
        """Issue one fixed v25 search after the final credential/transport check."""

        transport, headers_getter, target = self._session._checked_reader()._checked_members()
        headers = _headers_snapshot(headers_getter, target)
        # A credential callback can change mutable Python transport state.
        # Recheck the whole one-attempt boundary immediately before the POST.
        _validate_transport(transport)
        try:
            response = transport.request(
                "POST",
                (
                    f"https://{GOOGLE_ADS_ACTIVATE_HOST}/{GOOGLE_ADS_ACTIVATE_API_VERSION}"
                    f"/customers/{target.customer_client_ref}/googleAds:search"
                ),
                headers=headers,
                json={"query": query, "searchSettings": {"returnTotalResultsCount": True}},
                mutation=False,
            )
        except Exception:
            raise AdhelmError(
                "PROVIDER_UNAVAILABLE",
                "The Google Ads live hierarchy observation is unavailable.",
                exit_code=ExitCode.PRECONDITION,
            ) from None
        return _rows(response)


def _rows(response: object) -> tuple[dict[str, object], ...]:
    """Accept a bounded, complete, non-paginated allowlisted search response."""

    if type(response) is not _HTTPX_RESPONSE:
        raise _response_invalid()
    try:
        if type(response.status_code) is not int or response.status_code != 200:
            raise ValueError("status")
        payload = parse_json(response.content, maximum_bytes=_MAX_RESPONSE_BYTES)
        if type(payload) is not dict:
            raise ValueError("response")
        validate_data(payload)
        result = cast(dict[str, object], payload)
        if any(type(key) is not str or key not in _RESPONSE_KEYS for key in result):
            raise ValueError("response keys")
        if "totalResultsCount" not in result:
            raise ValueError("required response fields")
        total = result["totalResultsCount"]
        if type(total) is not str or _TOTAL.fullmatch(total) is None:
            raise ValueError("total")
        if result.get("nextPageToken") not in (None, ""):
            raise ValueError("pagination")
        if result.get("summaryRow") is not None:
            raise ValueError("summary")
        if result.get("metricAttributes") not in (None, []):
            raise ValueError("metrics")
        field_mask = result.get("fieldMask")
        if field_mask is not None and (
            type(field_mask) is not str or _FIELD_MASK.fullmatch(field_mask) is None
        ):
            raise ValueError("field mask")
        rows = result.get("results", [])
        if type(rows) is not list or len(rows) > _MAX_ROWS:
            raise ValueError("results")
        if any(type(row) is not dict for row in rows):
            raise ValueError("row")
        if int(total) != len(rows):
            raise ValueError("total does not describe the returned rows")
        return tuple(cast(list[dict[str, object]], rows))
    except Exception:
        raise _response_invalid() from None


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise _response_invalid()
    return cast(dict[str, object], value)


def _enum(value: object) -> str:
    if type(value) is not str or _ENUM.fullmatch(value) is None:
        raise _response_invalid()
    return value


def _text(value: object, *, maximum: int) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > maximum:
        raise _response_invalid()
    return value


def _flag(value: object) -> str:
    if type(value) is not bool:
        raise _response_invalid()
    return "true" if value else "false"


def _micros(value: object) -> str:
    if type(value) is str and _POSITIVE_INT64.fullmatch(value) is not None:
        return value
    if type(value) is int and 1 <= value <= _MAX_INT64:
        return str(value)
    raise _response_invalid()


def _date_time(value: object) -> str:
    if type(value) is not str or _DATE_TIME.fullmatch(value) is None:
        raise _response_invalid()
    return value


def _constant(value: object) -> str:
    if type(value) is not str or _NATIVE_REFERENCE.fullmatch(value) is None:
        raise _response_invalid()
    return value


def _positive_id(value: object) -> str:
    if type(value) is str and _POSITIVE_INT64.fullmatch(value) is not None:
        if int(value) <= _MAX_INT64:
            return value
    raise _response_invalid()


def _resource_name(value: object) -> str:
    if type(value) is not str or _RESOURCE_NAME.fullmatch(value) is None:
        raise _response_invalid()
    return value


def _hour(value: object) -> str:
    if type(value) is not int or not 0 <= value <= 24:
        raise _response_invalid()
    return str(value)


def _budget_member(
    campaign: dict[str, object], budget: dict[str, object]
) -> GoogleAdsSearchHierarchyMember:
    if set(budget) != {
        "resourceName",
        "explicitlyShared",
        "period",
        "deliveryMethod",
        "totalAmountMicros",
    }:
        raise _response_invalid()
    return GoogleAdsSearchHierarchyMember(
        member_kind="CAMPAIGN_BUDGET",
        identity=(_text(campaign.get("name"), maximum=255),),
        configuration=(
            ("delivery_method", _enum(budget.get("deliveryMethod"))),
            ("explicitly_shared", _flag(budget.get("explicitlyShared"))),
            ("period", _enum(budget.get("period"))),
            ("total_amount_micros", _micros(budget.get("totalAmountMicros"))),
        ),
    )


def _campaign_member(campaign: dict[str, object]) -> GoogleAdsSearchHierarchyMember:
    if set(campaign) != {
        "id",
        "resourceName",
        "name",
        "status",
        "advertisingChannelType",
        "biddingStrategyType",
        "startDateTime",
        "endDateTime",
        "networkSettings",
    }:
        raise _response_invalid()
    network = _mapping(campaign.get("networkSettings"))
    if set(network) != {
        "targetGoogleSearch",
        "targetSearchNetwork",
        "targetContentNetwork",
        "targetPartnerSearchNetwork",
        "targetYoutube",
        "targetGoogleTvNetwork",
    }:
        raise _response_invalid()
    return GoogleAdsSearchHierarchyMember(
        member_kind="CAMPAIGN",
        identity=(_text(campaign.get("name"), maximum=255),),
        configuration=(
            ("advertising_channel_type", _enum(campaign.get("advertisingChannelType"))),
            ("bidding_strategy", _enum(campaign.get("biddingStrategyType"))),
            ("end_date_time", _date_time(campaign.get("endDateTime"))),
            ("start_date_time", _date_time(campaign.get("startDateTime"))),
            ("status", _enum(campaign.get("status"))),
            ("target_content_network", _flag(network.get("targetContentNetwork"))),
            ("target_google_search", _flag(network.get("targetGoogleSearch"))),
            ("target_google_tv_network", _flag(network.get("targetGoogleTvNetwork"))),
            ("target_partner_search_network", _flag(network.get("targetPartnerSearchNetwork"))),
            ("target_search_network", _flag(network.get("targetSearchNetwork"))),
            ("target_youtube", _flag(network.get("targetYoutube"))),
        ),
    )


def _campaign_criterion_member(
    criterion: dict[str, object],
) -> tuple[GoogleAdsSearchHierarchyMember, str, bool]:
    kind = _enum(criterion.get("type"))
    resource_name = _resource_name(criterion.get("resourceName"))
    common = (
        ("negative", _flag(criterion.get("negative"))),
        ("status", _enum(criterion.get("status"))),
    )
    if kind == "LOCATION":
        if set(criterion) != {"resourceName", "type", "status", "negative", "location"}:
            raise _response_invalid()
        location = _mapping(criterion.get("location"))
        if set(location) != {"geoTargetConstant"}:
            raise _response_invalid()
        member = GoogleAdsSearchHierarchyMember(
            member_kind="CAMPAIGN_CRITERION_LOCATION",
            identity=(_constant(location.get("geoTargetConstant")),),
            configuration=common,
        )
        return member, resource_name, False
    if kind == "LANGUAGE":
        if set(criterion) != {"resourceName", "type", "status", "negative", "language"}:
            raise _response_invalid()
        language = _mapping(criterion.get("language"))
        if set(language) != {"languageConstant"}:
            raise _response_invalid()
        member = GoogleAdsSearchHierarchyMember(
            member_kind="CAMPAIGN_CRITERION_LANGUAGE",
            identity=(_constant(language.get("languageConstant")),),
            configuration=common,
        )
        return member, resource_name, False
    if kind == "AD_SCHEDULE":
        if set(criterion) != {"resourceName", "type", "status", "negative", "adSchedule"}:
            raise _response_invalid()
        schedule = _mapping(criterion.get("adSchedule"))
        if set(schedule) != {"dayOfWeek", "startHour", "startMinute", "endHour", "endMinute"}:
            raise _response_invalid()
        member = GoogleAdsSearchHierarchyMember(
            member_kind="CAMPAIGN_CRITERION_AD_SCHEDULE",
            identity=(
                _enum(schedule.get("dayOfWeek")),
                _hour(schedule.get("startHour")),
                _enum(schedule.get("startMinute")),
                _hour(schedule.get("endHour")),
                _enum(schedule.get("endMinute")),
            ),
            configuration=common,
        )
        return member, resource_name, True
    raise _read_invalid("The Google Ads campaign holds an unplanned criterion type.")


def _ad_group_member(
    ad_group: dict[str, object],
) -> tuple[GoogleAdsSearchHierarchyMember, int | None]:
    """Project one ad group, keeping ``cpc_bid_micros`` request-controllable.

    ``AdGroup.cpc_bid_micros`` is `optional int64` and Google omits it when it
    is unset, so an absent bid projects as the explicit string ``NOT_SET``
    rather than as a guessed zero.  It belongs in the member configuration
    because a bid change is exactly one of the maintenance operations whose
    before/after this projection has to distinguish.
    """

    if set(ad_group) - {"cpcBidMicros"} != {"id", "resourceName", "name", "type", "status"}:
        raise _response_invalid()
    raw = ad_group.get("cpcBidMicros")
    bid = None if raw is None else int(_micros(raw))
    return (
        GoogleAdsSearchHierarchyMember(
            member_kind="AD_GROUP",
            identity=(_text(ad_group.get("name"), maximum=255),),
            configuration=(
                ("cpc_bid_micros", "NOT_SET" if bid is None else str(bid)),
                ("status", _enum(ad_group.get("status"))),
                ("type", _enum(ad_group.get("type"))),
            ),
        ),
        bid,
    )


def _keyword_member(criterion: dict[str, object]) -> tuple[GoogleAdsSearchHierarchyMember, str]:
    if set(criterion) != {"resourceName", "status", "negative", "keyword"}:
        raise _response_invalid()
    keyword = _mapping(criterion.get("keyword"))
    if set(keyword) != {"text", "matchType"}:
        raise _response_invalid()
    return (
        GoogleAdsSearchHierarchyMember(
            member_kind="AD_GROUP_CRITERION_KEYWORD",
            identity=(_text(keyword.get("text"), maximum=80), _enum(keyword.get("matchType"))),
            configuration=(
                ("negative", _flag(criterion.get("negative"))),
                ("status", _enum(criterion.get("status"))),
            ),
        ),
        _resource_name(criterion.get("resourceName")),
    )


def _ad_member(
    ad_group_ad: dict[str, object],
) -> tuple[GoogleAdsSearchHierarchyMember, str, int, str]:
    if set(ad_group_ad) - {"policySummary"} != {"resourceName", "status", "ad"}:
        raise _response_invalid()
    ad = _mapping(ad_group_ad.get("ad"))
    if set(ad) != {"finalUrls", "responsiveSearchAd"}:
        raise _response_invalid()
    rsa = _mapping(ad.get("responsiveSearchAd"))
    if set(rsa) != {"headlines", "descriptions"}:
        raise _response_invalid()
    headlines = _assets(rsa.get("headlines"), maximum=30)
    descriptions = _assets(rsa.get("descriptions"), maximum=90)
    final_urls = _urls(ad.get("finalUrls"))
    summary = ad_group_ad.get("policySummary")
    if summary is None:
        approval_status = NOT_READ_APPROVAL_STATUS
    else:
        mapping = _mapping(summary)
        if set(mapping) != {"approvalStatus"}:
            raise _response_invalid()
        approval_status = _enum(mapping.get("approvalStatus"))
    return (
        GoogleAdsSearchHierarchyMember(
            member_kind="AD_GROUP_AD",
            identity=(
                rsa_text_digest(
                    headlines=headlines, descriptions=descriptions, final_urls=final_urls
                ),
            ),
            configuration=(
                ("description_count", str(len(descriptions))),
                ("headline_count", str(len(headlines))),
                ("status", _enum(ad_group_ad.get("status"))),
            ),
        ),
        _resource_name(ad_group_ad.get("resourceName")),
        len(final_urls),
        approval_status,
    )


def _assets(value: object, *, maximum: int) -> tuple[str, ...]:
    if type(value) is not list or not 1 <= len(value) <= 15:
        raise _response_invalid()
    texts: list[str] = []
    for item in value:
        asset = _mapping(item)
        if set(asset) != {"text"}:
            raise _response_invalid()
        texts.append(_text(asset.get("text"), maximum=maximum))
    return tuple(texts)


def _urls(value: object) -> tuple[str, ...]:
    """Require at least one final URL: no activation without a destination."""

    if type(value) is not list or not 1 <= len(value) <= 16:
        raise _response_invalid()
    return tuple(_text(item, maximum=2_000) for item in value)


__all__ = [
    "ACTIVATABLE_APPROVAL_STATUSES",
    "NOT_READ_APPROVAL_STATUS",
    "GoogleAdsLiveHierarchyObservation",
    "GoogleAdsLiveHierarchyReader",
]
