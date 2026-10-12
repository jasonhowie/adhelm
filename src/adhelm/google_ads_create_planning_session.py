"""Bounded Google Ads reads for one paused Search hierarchy CREATE.

The sealed :mod:`adhelm.google_ads_pause_planning_session` facade owns the whole
Google Ads EDIT credential boundary: the three protected slots, the
configuration transaction, the no-refresh header callback, the one-attempt
transport, and the retention recheck.  Nothing of that is reimplemented here.
This module adds only the *fixed GAQL reads* a CREATE needs and that the two
M55 PAUSE queries do not cover, issued through that facade's own validated
private accessors.

Every query in ``_QUERIES`` is a compile-time constant except for two
interpolations that are validated first: a campaign name restricted to
characters this build can place in a GAQL string literal without escaping, and
a positive int64 resource ID.  A campaign name carrying a quote or backslash is
refused before egress rather than escaped, because this repository builds no
escaped GAQL literal.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, cast

import httpx

from adhelm import google_ads_pause_planning_session as planning
from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.privacy import validate_data
from adhelm.providers.google_ads_campaign_state import (
    GoogleAdsEditIdentity,
    _headers_snapshot,
    _validate_transport,
)
from adhelm.providers.google_ads_create_contract import (
    GOOGLE_ADS_CREATE_API_VERSION,
    GOOGLE_ADS_CREATE_HOST,
    GOOGLE_ADS_CREATE_UNQUOTABLE_NAME_CHARACTERS,
    GoogleAdsAssetAutomationObservation,
    GoogleAdsSearchHierarchyMember,
    GoogleAdsSearchHierarchyProjection,
    rsa_text_digest,
)

_HTTPX_RESPONSE: Final = httpx.Response
_MAX_RESPONSE_BYTES: Final = 65_536
_MAX_ROWS: Final = 200
#: The closed bound on the near-variant campaign names one plan probes for.
_MAX_NEAR_MATCH_NAMES: Final = 64
_ENUM: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$", re.ASCII)
_FIELD_MASK: Final = re.compile(r"^[A-Za-z0-9_.,]{1,4096}$", re.ASCII)
_POSITIVE_INT64: Final = re.compile(r"^[1-9][0-9]{0,18}$", re.ASCII)
_MAX_INT64: Final = 9_223_372_036_854_775_807
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

_CAMPAIGN_FIELDS: Final = (
    "campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
    "campaign.bidding_strategy_type, campaign.start_date_time, campaign.end_date_time, "
    "campaign.network_settings.target_google_search, "
    "campaign.network_settings.target_search_network, "
    "campaign.network_settings.target_content_network, "
    "campaign.network_settings.target_partner_search_network, "
    "campaign.network_settings.target_youtube, "
    "campaign.network_settings.target_google_tv_network, "
    "campaign.geo_target_type_setting.positive_geo_target_type, "
    "campaign.geo_target_type_setting.negative_geo_target_type, "
    "campaign.asset_automation_settings, "
    "campaign_budget.explicitly_shared, campaign_budget.period, "
    "campaign_budget.delivery_method, campaign_budget.total_amount_micros"
)


def _read_invalid(detail: str) -> AdhelmError:
    return AdhelmError("GOOGLE_ADS_CREATE_READ_INVALID", detail, exit_code=ExitCode.PRECONDITION)


def _response_invalid() -> AdhelmError:
    return AdhelmError(
        "PROVIDER_RESPONSE_INVALID",
        "The Google Ads CREATE observation is not a complete allowlisted response.",
        exit_code=ExitCode.PRECONDITION,
    )


def quotable_campaign_name(value: object) -> str:
    """Return a campaign name this build can place in a GAQL string literal."""

    if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > 255:
        raise _read_invalid("A bounded Google Ads campaign name is required.")
    if GOOGLE_ADS_CREATE_UNQUOTABLE_NAME_CHARACTERS & set(value):
        raise AdhelmError(
            "GOOGLE_ADS_CREATE_NAME_NOT_QUERYABLE",
            "This build refuses to construct a quoted GAQL literal for this campaign name.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile a candidate whose campaign name contains no quote or backslash; "
                "no credential was read and no provider request was made."
            ),
        )
    if any(ord(character) < 32 for character in value):
        raise _read_invalid("A Google Ads campaign name cannot contain control characters.")
    return value


def _positive_id(value: object) -> str:
    if type(value) is str and _POSITIVE_INT64.fullmatch(value) is not None:
        if int(value) <= _MAX_INT64:
            return value
    raise _response_invalid()


def _campaign_query(name: str) -> str:
    return f"SELECT {_CAMPAIGN_FIELDS} FROM campaign WHERE campaign.name = '{name}'"


def near_match_campaign_names(name: str) -> tuple[str, ...]:
    """Return the bounded near-variant names this build probes for, sorted.

    GAQL defines no case-insensitive operator and does not document whether
    ``campaign.name =`` compares case-sensitively at all, so this build cannot
    ask the provider for "a campaign whose name matches apart from case".  What
    it can do is enumerate a *closed, deterministic* set of near variants
    locally and probe for those exact strings with the same byte-exact
    comparison it uses everywhere else.

    The set is the cross product of two seeds (the name and its stripped form),
    the four Unicode normalization forms, five case foldings, and four
    single-space paddings.  It therefore catches a name that differs only by
    surrounding whitespace, by normalization form, or by a *uniform* case
    change.  It does **not** catch an arbitrary mixed-case name such as
    ``cUsToMeR search``, or padding of more than one space: no bounded local
    enumeration can, and an unbounded account-wide scan is not a read this
    route is willing to make.  Both limits are stated in the execution
    contract rather than implied away.

    The byte-exact name itself is excluded: it is the separate collision check.
    """

    exact = quotable_campaign_name(name)
    variants: set[str] = set()
    for seed in (exact, exact.strip()):
        for form in ("NFC", "NFD", "NFKC", "NFKD"):
            normalized = unicodedata.normalize(form, seed)
            variants.update(
                {
                    normalized,
                    normalized.casefold(),
                    normalized.lower(),
                    normalized.upper(),
                    normalized.title(),
                }
            )
    variants = {
        padded for value in variants for padded in (value, f" {value}", f"{value} ", f" {value} ")
    }
    variants.discard(exact)
    usable = sorted(
        value
        for value in variants
        if value.strip()
        and len(value.encode("utf-8")) <= 255
        and not (GOOGLE_ADS_CREATE_UNQUOTABLE_NAME_CHARACTERS & set(value))
        and all(ord(character) >= 32 for character in value)
    )
    if len(usable) > _MAX_NEAR_MATCH_NAMES:
        raise _read_invalid(
            "This campaign name has more near variants than this build will probe for."
        )
    return tuple(usable)


def _campaign_name_probe_query(names: tuple[str, ...]) -> str:
    """One bounded read that returns every probed name this customer holds."""

    if not names or len(names) > _MAX_NEAR_MATCH_NAMES + 1:
        raise _read_invalid("A bounded Google Ads campaign-name probe is required.")
    literals = ", ".join(f"'{quotable_campaign_name(name)}'" for name in names)
    return f"SELECT campaign.name FROM campaign WHERE campaign.name IN ({literals})"


def _criteria_query(campaign_id: str) -> str:
    return (
        "SELECT campaign_criterion.type, campaign_criterion.status, "
        "campaign_criterion.negative, campaign_criterion.location.geo_target_constant, "
        "campaign_criterion.language.language_constant, "
        "campaign_criterion.ad_schedule.day_of_week, campaign_criterion.ad_schedule.start_hour, "
        "campaign_criterion.ad_schedule.start_minute, campaign_criterion.ad_schedule.end_hour, "
        "campaign_criterion.ad_schedule.end_minute "
        f"FROM campaign_criterion WHERE campaign.id = {campaign_id}"
    )


def _ad_group_query(campaign_id: str) -> str:
    return (
        "SELECT ad_group.id, ad_group.name, ad_group.type, ad_group.status "
        f"FROM ad_group WHERE campaign.id = {campaign_id}"
    )


def _keyword_query(ad_group_id: str) -> str:
    return (
        "SELECT ad_group_criterion.status, ad_group_criterion.negative, "
        "ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type "
        f"FROM ad_group_criterion WHERE ad_group.id = {ad_group_id} "
        "AND ad_group_criterion.type = KEYWORD"
    )


def _ad_query(ad_group_id: str) -> str:
    return (
        "SELECT ad_group_ad.status, ad_group_ad.ad.final_urls, "
        "ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions "
        f"FROM ad_group_ad WHERE ad_group.id = {ad_group_id}"
    )


@dataclass(frozen=True, slots=True)
class GoogleAdsCreateAccountObservation:
    """The bounded pre-create account facts one CREATE plan is compiled from."""

    identity: GoogleAdsEditIdentity
    campaign_name_matches: int
    campaign_name_near_matches: int
    probed_near_match_names: int


@dataclass(frozen=True, slots=True)
class GoogleAdsCreateHierarchyObservation:
    """One observed created hierarchy, plus the facts the plan cannot predict."""

    projection: GoogleAdsSearchHierarchyProjection
    campaign_client_ref: str
    ad_group_client_ref: str
    positive_geo_target_type: str
    negative_geo_target_type: str
    asset_automation_settings: tuple[GoogleAdsAssetAutomationObservation, ...]


class GoogleAdsSearchCampaignCreateReader:
    """Fixed CREATE reads over one sealed Google Ads EDIT planning session."""

    __slots__ = ("_session",)

    def __init__(self, session: planning.GoogleAdsCampaignPausePlanningSession) -> None:
        if type(session) is not planning.GoogleAdsCampaignPausePlanningSession:
            raise _read_invalid("A sealed Google Ads EDIT planning session is required.")
        self._session = session

    def __repr__(self) -> str:
        return "<GoogleAdsSearchCampaignCreateReader redacted>"

    def discover_identity(self) -> GoogleAdsEditIdentity:
        identity = self._session._checked_reader().discover_identity()
        if type(identity) is not GoogleAdsEditIdentity:
            raise _response_invalid()
        return identity

    def observe_account(self, campaign_name: str) -> GoogleAdsCreateAccountObservation:
        """Read the customer identity and the campaign-name collision probe.

        One bounded ``IN`` read covers the byte-exact name and every near
        variant at once, and the returned names are classified locally with the
        same byte-exact comparison the ``before`` projection uses.  Nothing is
        normalized on either side of that comparison.
        """

        name = quotable_campaign_name(campaign_name)
        identity = self.discover_identity()
        near = near_match_campaign_names(name)
        rows = self._search(_campaign_name_probe_query((name, *near)))
        observed: list[str] = []
        for row in rows:
            campaign = _mapping(row.get("campaign"))
            if set(campaign) != {"name"}:
                raise _response_invalid()
            observed.append(_text(campaign.get("name"), maximum=255))
        exact = sum(1 for value in observed if value == name)
        return GoogleAdsCreateAccountObservation(
            identity=identity,
            campaign_name_matches=exact,
            campaign_name_near_matches=len(observed) - exact,
            probed_near_match_names=len(near),
        )

    def observe_created_hierarchy(self, campaign_name: str) -> GoogleAdsCreateHierarchyObservation:
        """Read back the whole created hierarchy by its configured name."""

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
        members: list[GoogleAdsSearchHierarchyMember] = [
            _budget_member(campaign, budget),
            _campaign_member(campaign),
        ]
        for row in self._search(_criteria_query(campaign_id)):
            members.append(_campaign_criterion_member(_mapping(row.get("campaignCriterion"))))
        ad_groups = self._search(_ad_group_query(campaign_id))
        if len(ad_groups) != 1:
            raise _read_invalid("The created Google Ads campaign must hold exactly one ad group.")
        ad_group = _mapping(ad_groups[0].get("adGroup"))
        ad_group_id = _positive_id(ad_group.get("id"))
        members.append(_ad_group_member(ad_group))
        for row in self._search(_keyword_query(ad_group_id)):
            members.append(_keyword_member(_mapping(row.get("adGroupCriterion"))))
        ads = self._search(_ad_query(ad_group_id))
        if len(ads) != 1:
            raise _read_invalid("The created Google Ads ad group must hold exactly one ad.")
        members.append(_ad_member(_mapping(ads[0].get("adGroupAd"))))
        geo = _mapping(campaign.get("geoTargetTypeSetting"))
        try:
            projection = GoogleAdsSearchHierarchyProjection(
                campaign_name=_text(campaign.get("name"), maximum=255),
                members=tuple(members),
            )
        except Exception:
            raise _read_invalid(
                "The created Google Ads hierarchy is not a complete paused Search projection."
            ) from None
        return GoogleAdsCreateHierarchyObservation(
            projection=projection,
            campaign_client_ref=campaign_id,
            ad_group_client_ref=ad_group_id,
            positive_geo_target_type=_enum(geo.get("positiveGeoTargetType")),
            negative_geo_target_type=_enum(geo.get("negativeGeoTargetType")),
            asset_automation_settings=_asset_automation(campaign.get("assetAutomationSettings")),
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
                    f"https://{GOOGLE_ADS_CREATE_HOST}/{GOOGLE_ADS_CREATE_API_VERSION}"
                    f"/customers/{target.customer_client_ref}/googleAds:search"
                ),
                headers=headers,
                json={"query": query, "searchSettings": {"returnTotalResultsCount": True}},
                mutation=False,
            )
        except AdhelmError:
            raise AdhelmError(
                "PROVIDER_UNAVAILABLE",
                "The Google Ads CREATE observation is unavailable.",
                exit_code=ExitCode.PRECONDITION,
            ) from None
        except Exception:
            raise AdhelmError(
                "PROVIDER_UNAVAILABLE",
                "The Google Ads CREATE observation is unavailable.",
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


_TOTAL: Final = re.compile(r"^(0|[1-9][0-9]{0,3})$", re.ASCII)


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


_DATE_TIME: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$", re.ASCII)
_NATIVE_REFERENCE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9]*/[1-9][0-9]{0,19}$", re.ASCII)


def _constant(value: object) -> str:
    if type(value) is not str or _NATIVE_REFERENCE.fullmatch(value) is None:
        raise _response_invalid()
    return value


def _budget_member(
    campaign: dict[str, object], budget: dict[str, object]
) -> GoogleAdsSearchHierarchyMember:
    if set(budget) != {"explicitlyShared", "period", "deliveryMethod", "totalAmountMicros"}:
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


def _asset_automation(value: object) -> tuple[GoogleAdsAssetAutomationObservation, ...]:
    """Project the campaign's opt-in/out asset-automation settings, in order.

    The compiler never requests these; Google chooses them.  They are observed
    and retained, never predicted or authorized, so they stay out of the
    semantic hierarchy projection and go into the readback record instead.
    """

    if value is None:
        return ()
    if type(value) is not list or len(value) > 32:
        raise _response_invalid()
    settings: list[GoogleAdsAssetAutomationObservation] = []
    for item in value:
        setting = _mapping(item)
        if set(setting) != {"assetAutomationType", "assetAutomationStatus"}:
            raise _response_invalid()
        try:
            settings.append(
                GoogleAdsAssetAutomationObservation(
                    asset_automation_type=_enum(setting.get("assetAutomationType")),
                    asset_automation_status=_enum(setting.get("assetAutomationStatus")),
                )
            )
        except AdhelmError:
            raise
        except Exception:
            raise _response_invalid() from None
    return tuple(settings)


def _campaign_member(campaign: dict[str, object]) -> GoogleAdsSearchHierarchyMember:
    expected = {
        "id",
        "name",
        "status",
        "advertisingChannelType",
        "biddingStrategyType",
        "startDateTime",
        "endDateTime",
        "networkSettings",
        "geoTargetTypeSetting",
    }
    # ``assetAutomationSettings`` is provider-chosen and omitted when empty, so
    # it is accepted here and projected separately rather than being part of the
    # request-controllable member configuration.
    if set(campaign) - {"assetAutomationSettings"} != expected:
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


def _campaign_criterion_member(criterion: dict[str, object]) -> GoogleAdsSearchHierarchyMember:
    kind = _enum(criterion.get("type"))
    common = (
        ("negative", _flag(criterion.get("negative"))),
        ("status", _enum(criterion.get("status"))),
    )
    if kind == "LOCATION":
        if set(criterion) != {"type", "status", "negative", "location"}:
            raise _response_invalid()
        location = _mapping(criterion.get("location"))
        if set(location) != {"geoTargetConstant"}:
            raise _response_invalid()
        return GoogleAdsSearchHierarchyMember(
            member_kind="CAMPAIGN_CRITERION_LOCATION",
            identity=(_constant(location.get("geoTargetConstant")),),
            configuration=common,
        )
    if kind == "LANGUAGE":
        if set(criterion) != {"type", "status", "negative", "language"}:
            raise _response_invalid()
        language = _mapping(criterion.get("language"))
        if set(language) != {"languageConstant"}:
            raise _response_invalid()
        return GoogleAdsSearchHierarchyMember(
            member_kind="CAMPAIGN_CRITERION_LANGUAGE",
            identity=(_constant(language.get("languageConstant")),),
            configuration=common,
        )
    if kind == "AD_SCHEDULE":
        if set(criterion) != {"type", "status", "negative", "adSchedule"}:
            raise _response_invalid()
        schedule = _mapping(criterion.get("adSchedule"))
        if set(schedule) != {"dayOfWeek", "startHour", "startMinute", "endHour", "endMinute"}:
            raise _response_invalid()
        return GoogleAdsSearchHierarchyMember(
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
    raise _read_invalid("The created Google Ads campaign holds an unplanned criterion type.")


def _hour(value: object) -> str:
    if type(value) is not int or not 0 <= value <= 24:
        raise _response_invalid()
    return str(value)


def _ad_group_member(ad_group: dict[str, object]) -> GoogleAdsSearchHierarchyMember:
    if set(ad_group) != {"id", "name", "type", "status"}:
        raise _response_invalid()
    return GoogleAdsSearchHierarchyMember(
        member_kind="AD_GROUP",
        identity=(_text(ad_group.get("name"), maximum=255),),
        configuration=(
            ("status", _enum(ad_group.get("status"))),
            ("type", _enum(ad_group.get("type"))),
        ),
    )


def _keyword_member(criterion: dict[str, object]) -> GoogleAdsSearchHierarchyMember:
    if set(criterion) != {"status", "negative", "keyword"}:
        raise _response_invalid()
    keyword = _mapping(criterion.get("keyword"))
    if set(keyword) != {"text", "matchType"}:
        raise _response_invalid()
    return GoogleAdsSearchHierarchyMember(
        member_kind="AD_GROUP_CRITERION_KEYWORD",
        identity=(_text(keyword.get("text"), maximum=80), _enum(keyword.get("matchType"))),
        configuration=(
            ("negative", _flag(criterion.get("negative"))),
            ("status", _enum(criterion.get("status"))),
        ),
    )


def _ad_member(ad_group_ad: dict[str, object]) -> GoogleAdsSearchHierarchyMember:
    if set(ad_group_ad) != {"status", "ad"}:
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
    return GoogleAdsSearchHierarchyMember(
        member_kind="AD_GROUP_AD",
        identity=(
            rsa_text_digest(headlines=headlines, descriptions=descriptions, final_urls=final_urls),
        ),
        configuration=(
            ("description_count", str(len(descriptions))),
            ("headline_count", str(len(headlines))),
            ("status", _enum(ad_group_ad.get("status"))),
        ),
    )


def _assets(value: object, *, maximum: int) -> tuple[str, ...]:
    if type(value) is not list or not 1 <= len(value) <= 15:
        raise _response_invalid()
    texts: list[str] = []
    for item in value:
        asset = _mapping(item)
        # Only ``text`` is projected.  A pinned field, an asset performance
        # label, or any other member Google adds makes this observation
        # unequal to the prediction, which is the intended outcome.
        if set(asset) != {"text"}:
            raise _response_invalid()
        texts.append(_text(asset.get("text"), maximum=maximum))
    return tuple(texts)


def _urls(value: object) -> tuple[str, ...]:
    if type(value) is not list or len(value) != 1:
        raise _response_invalid()
    return (_text(value[0], maximum=2_000),)


__all__ = [
    "GoogleAdsCreateAccountObservation",
    "GoogleAdsCreateHierarchyObservation",
    "GoogleAdsSearchCampaignCreateReader",
    "near_match_campaign_names",
    "quotable_campaign_name",
]
