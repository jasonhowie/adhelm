"""Bounded OpenAI Ads reads for one paused chat-card hierarchy CREATE.

:mod:`adhelm.providers.openai_ads` owns the reviewed read surface, and this module
does not reimplement it: identity discovery is delegated to
:class:`~adhelm.providers.openai_ads.OpenAIAdsClient`, and every bounded list here
reuses that module's own validated response helpers, page bounds and cursor
discipline.

Two reads exist here that the shipped inventory contract deliberately does not
provide.

* **Configured names.**  The inventory models retain no campaign, ad-group or ad
  name, because an audit does not need one.  A CREATE does: the name is the only
  request-controllable handle a semantic before/after projection can compare, and
  it is a value this workspace itself chose.  These parsers therefore retain the
  name and nothing else that the inventory contract excludes -- no ad copy, no
  destination URL, no image URL, no audience material.
* **Declared location targeting.**  The inventory contract records only that a
  targeting object was declared, because such an object can carry audience
  material.  This parser accepts a targeting object whose *only* member is
  ``locations.include``, retains only each entry's documented ``id``, and fails
  closed on anything else -- including the ``custom_audiences`` members the
  compiler never requests.  A response that carries them is drift, not input.

Nothing here mutates, uploads, or authorizes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from adhelm.errors import AdhelmError, ExitCode
from adhelm.providers.openai_ads import (
    OPENAI_ADS_API_HOST,
    OPENAI_ADS_API_VERSION,
    OpenAIAdsClient,
    OpenAIAdsIdentity,
    _client_id,
    _json,
    _keys,
    _native_micros,
    _safe_string,
    _safe_token,
    classify_openai_ads_transport_error,
)
from adhelm.providers.openai_create_contract import (
    FILE_ASSOCIATION_NOT_PROVEN,
    OPENAI_CREATE_MAX_HIERARCHY_MEMBERS,
    OpenAIAdsChatCardHierarchyProjection,
    OpenAIAdsHierarchyMember,
    targeted_location_digest,
)
from adhelm.transport import SafeHttpClient

_MAX_PAGES: Final = 10
_PAGE_SIZE: Final = 500
_MIN_UNIX_TIME: Final = 946_684_800
_MAX_UNIX_TIME: Final = 4_102_444_800

_CAMPAIGN_FIELDS: Final = {
    "id",
    "ad_account_id",
    "created_at",
    "updated_at",
    "name",
    "description",
    "status",
    "start_time",
    "end_time",
    "budget",
    "bidding_type",
    "mode",
    "targeting",
    "conversion_event_setting_ids",
}
_AD_GROUP_FIELDS: Final = {
    "id",
    "created_at",
    "updated_at",
    "name",
    "description",
    "context_hints",
    "status",
    "bidding_config",
}
_AD_FIELDS: Final = {
    "id",
    "name",
    "created_at",
    "updated_at",
    "creative",
    "status",
    "review_status",
}
_CREATIVE_FIELDS: Final = {"type", "title", "body", "file_id", "image_url", "target_url"}
_LIST_FIELDS: Final = {"object", "data", "first_id", "last_id", "has_more"}
_LOCATION_FIELDS: Final = {"id", "type", "country_code", "name", "region_code"}


def _response_invalid(context: str) -> AdhelmError:
    return AdhelmError(
        "PROVIDER_RESPONSE_INVALID",
        f"OpenAI Ads {context} is not a complete allowlisted response.",
        exit_code=ExitCode.PRECONDITION,
    )


def _unix_time(value: object, context: str) -> int:
    if (
        type(value) is not int
        or type(value) is bool
        or not _MIN_UNIX_TIME <= value <= _MAX_UNIX_TIME
    ):
        raise _response_invalid(context)
    return value


def _declared_location_ids(value: object) -> tuple[str, ...]:
    """Accept only a targeting object that declares included locations.

    A ``custom_audiences`` or ``excluded_custom_audiences`` member, an unknown
    member, or an empty include list fails closed.  The compiler never requests
    any of them, so their presence in a readback means the campaign is not the
    one this plan predicted.
    """

    if type(value) is not dict:
        raise _response_invalid("campaign targeting")
    _keys(value, {"locations"}, "campaign targeting")
    locations = value.get("locations")
    if type(locations) is not dict:
        raise _response_invalid("campaign location targeting")
    _keys(locations, {"include"}, "campaign location targeting")
    include = locations.get("include")
    if type(include) is not list or not 1 <= len(include) <= 500:
        raise _response_invalid("campaign location targeting")
    identifiers: list[str] = []
    for item in include:
        if type(item) is not dict:
            raise _response_invalid("campaign location targeting")
        _keys(item, set(_LOCATION_FIELDS), "campaign location targeting")
        identifier = _safe_string(item.get("id"), "campaign location targeting", maximum=20)
        if not identifier.isascii() or not identifier.isdecimal():
            raise _response_invalid("campaign location targeting")
        identifiers.append(identifier)
    return tuple(sorted(set(identifiers)))


@dataclass(frozen=True, slots=True)
class OpenAIAdsCreateCampaignFacts:
    """The exact campaign facts this route projects and compares."""

    client_ref: str
    name: str
    status: str
    bidding_type: str
    lifetime_spend_limit_micros: str
    start_time: int
    end_time: int
    location_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpenAIAdsCreateAdGroupFacts:
    client_ref: str
    name: str
    status: str
    billing_event_type: str
    max_bid_micros: str


@dataclass(frozen=True, slots=True)
class OpenAIAdsCreateAdFacts:
    client_ref: str
    ad_group_client_ref: str
    name: str
    status: str
    creative_type: str
    creative_file_client_ref: str
    title_present: bool
    body_present: bool
    target_url_present: bool


@dataclass(frozen=True, slots=True)
class OpenAIAdsCreateHierarchyObservation:
    """One observed created hierarchy, plus the facts the plan cannot predict."""

    campaign: OpenAIAdsCreateCampaignFacts
    ad_groups: tuple[OpenAIAdsCreateAdGroupFacts, ...]
    ads: tuple[OpenAIAdsCreateAdFacts, ...]
    #: Always present, complete or not: an ordered non-atomic create can leave a
    #: campaign with no ad group, which is a real observation rather than an
    #: absence.  ``projection`` is the strict complete-hierarchy model, and it is
    #: ``None`` exactly when the observed members do not form one.
    members: tuple[OpenAIAdsHierarchyMember, ...]
    projection: OpenAIAdsChatCardHierarchyProjection | None
    file_association_proven: bool


def parse_campaign_object(
    payload: dict[str, Any], *, expected_account_client_ref: str
) -> OpenAIAdsCreateCampaignFacts:
    """Parse one documented campaign resource object under a closed allowlist."""

    _keys(payload, set(_CAMPAIGN_FIELDS), "campaign")
    returned_account = payload.get("ad_account_id")
    if returned_account is not None and (
        _client_id(returned_account, "campaign account ID") != expected_account_client_ref
    ):
        raise AdhelmError(
            "IDENTITY_MISMATCH", "OpenAI Ads campaign differs from the discovered account."
        )
    if payload.get("mode") is not None:
        raise _response_invalid("campaign mode")
    if payload.get("conversion_event_setting_ids") not in (None, []):
        raise _response_invalid("campaign conversion settings")
    budget = payload.get("budget")
    if type(budget) is not dict:
        raise _response_invalid("campaign budget")
    _keys(budget, {"lifetime_spend_limit_micros"}, "campaign budget")
    bidding_type = _safe_string(payload.get("bidding_type"), "campaign bidding type", maximum=80)
    if bidding_type not in {"impressions", "clicks"}:
        raise _response_invalid("campaign bidding type")
    return OpenAIAdsCreateCampaignFacts(
        client_ref=_client_id(payload.get("id"), "campaign ID"),
        name=_safe_string(payload.get("name"), "campaign name", maximum=1_000),
        status=_safe_string(payload.get("status"), "campaign status", maximum=80),
        bidding_type=bidding_type,
        lifetime_spend_limit_micros=_native_micros(
            budget.get("lifetime_spend_limit_micros"), "campaign lifetime spend"
        ),
        start_time=_unix_time(payload.get("start_time"), "campaign start time"),
        end_time=_unix_time(payload.get("end_time"), "campaign end time"),
        location_ids=_declared_location_ids(payload.get("targeting")),
    )


def parse_ad_group_object(payload: dict[str, Any]) -> OpenAIAdsCreateAdGroupFacts:
    """Parse one documented ad-group resource object under a closed allowlist."""

    _keys(payload, set(_AD_GROUP_FIELDS), "ad group")
    if payload.get("context_hints") not in (None, []):
        raise _response_invalid("ad group context hints")
    bidding = payload.get("bidding_config")
    if type(bidding) is not dict:
        raise _response_invalid("ad group bidding configuration")
    _keys(bidding, {"billing_event_type", "max_bid_micros"}, "ad group bidding configuration")
    billing_event_type = _safe_string(
        bidding.get("billing_event_type"), "ad group billing event type", maximum=80
    )
    if billing_event_type not in {"impression", "click"}:
        raise _response_invalid("ad group billing event type")
    return OpenAIAdsCreateAdGroupFacts(
        client_ref=_client_id(payload.get("id"), "ad group ID"),
        name=_safe_string(payload.get("name"), "ad group name", maximum=1_000),
        status=_safe_string(payload.get("status"), "ad group status", maximum=80),
        billing_event_type=billing_event_type,
        max_bid_micros=_native_micros(bidding.get("max_bid_micros"), "ad group maximum bid"),
    )


def parse_ad_object(payload: dict[str, Any], *, ad_group_client_ref: str) -> OpenAIAdsCreateAdFacts:
    """Parse one documented ad resource object, keeping copy as presence only."""

    _keys(payload, set(_AD_FIELDS), "ad")
    creative = payload.get("creative")
    if type(creative) is not dict:
        raise _response_invalid("ad creative")
    _keys(creative, set(_CREATIVE_FIELDS), "ad creative")
    creative_type = _safe_string(creative.get("type"), "ad creative type", maximum=80)
    if creative_type != "chat_card":
        raise _response_invalid("ad creative type")
    return OpenAIAdsCreateAdFacts(
        client_ref=_client_id(payload.get("id"), "ad ID"),
        ad_group_client_ref=ad_group_client_ref,
        name=_safe_string(payload.get("name"), "ad name", maximum=1_000),
        status=_safe_string(payload.get("status"), "ad status", maximum=80),
        creative_type=creative_type,
        creative_file_client_ref=_client_id(creative.get("file_id"), "ad creative file ID"),
        title_present=creative.get("title") is not None,
        body_present=creative.get("body") is not None,
        target_url_present=creative.get("target_url") is not None,
    )


def members_from_observation(
    campaign: OpenAIAdsCreateCampaignFacts,
    ad_groups: tuple[OpenAIAdsCreateAdGroupFacts, ...],
    ads: tuple[OpenAIAdsCreateAdFacts, ...],
    *,
    uploaded_file_client_ref: str,
    creative_content_digest: str,
) -> tuple[OpenAIAdsHierarchyMember, ...]:
    """Reduce one observed hierarchy to the same member multiset a plan predicts.

    The ad member's ``creative_asset_content_digest`` is **not** read from
    OpenAI: no documented Ads route exposes a file's bytes or a provider digest.
    It is the digest this attempt uploaded, admitted into the observation only
    when the observed paused ad references the exact ``file_id`` the upload
    acknowledged in this same attempt.  That is ``PROVEN_UPLOAD_ASSOCIATION``.
    An ad referencing any other file gets the
    ``FILE_ASSOCIATION_NOT_PROVEN`` sentinel, which cannot equal a digest, so the
    hierarchy cannot match the prediction.
    """

    names = {group.client_ref: group.name for group in ad_groups}
    members: list[OpenAIAdsHierarchyMember] = [
        OpenAIAdsHierarchyMember(
            member_kind="CAMPAIGN",
            identity=(campaign.name, campaign.name),
            configuration=(
                ("bidding_type", campaign.bidding_type),
                ("end_time", str(campaign.end_time)),
                ("lifetime_spend_limit_micros", campaign.lifetime_spend_limit_micros),
                ("location_targeting_digest", targeted_location_digest(campaign.location_ids)),
                ("start_time", str(campaign.start_time)),
                ("status", campaign.status),
                ("targeting_declared", "true"),
            ),
        )
    ]
    for group in ad_groups:
        members.append(
            OpenAIAdsHierarchyMember(
                member_kind="AD_GROUP",
                identity=(campaign.name, group.name, group.name),
                configuration=(
                    ("billing_event_type", group.billing_event_type),
                    ("max_bid_micros", group.max_bid_micros),
                    ("status", group.status),
                ),
            )
        )
    for ad in ads:
        parent = names.get(ad.ad_group_client_ref)
        if parent is None:
            raise _response_invalid("ad parent binding")
        members.append(
            OpenAIAdsHierarchyMember(
                member_kind="AD",
                identity=(campaign.name, parent, ad.name, ad.name),
                configuration=(
                    (
                        "creative_asset_content_digest",
                        creative_content_digest
                        if ad.creative_file_client_ref == uploaded_file_client_ref
                        else FILE_ASSOCIATION_NOT_PROVEN,
                    ),
                    ("creative_body_present", "true" if ad.body_present else "false"),
                    ("creative_kind", ad.creative_type),
                    (
                        "creative_target_url_present",
                        "true" if ad.target_url_present else "false",
                    ),
                    ("creative_title_present", "true" if ad.title_present else "false"),
                    ("status", ad.status),
                ),
            )
        )
    return tuple(members)


def projection_from_members(
    campaign_name: str, members: tuple[OpenAIAdsHierarchyMember, ...]
) -> OpenAIAdsChatCardHierarchyProjection | None:
    """Return the strict complete projection, or ``None`` for a partial hierarchy."""

    try:
        return OpenAIAdsChatCardHierarchyProjection(campaign_name=campaign_name, members=members)
    except Exception:
        return None


class OpenAIAdsCreateReader:
    """Fixed bounded v1 reads for one chat-card CREATE, over one transport."""

    __slots__ = ("_client", "_token_getter", "_transport")

    def __init__(self, transport: SafeHttpClient, token_getter: Any) -> None:
        if type(transport) is not SafeHttpClient or not callable(token_getter):
            raise AdhelmError(
                "OPENAI_ADS_CREATE_READ_INVALID",
                "A configured OpenAI Ads transport and token source are required.",
                exit_code=ExitCode.PRECONDITION,
            )
        if OPENAI_ADS_API_HOST not in transport.allowed_hosts:
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID", "OpenAI Ads CREATE requires its fixed API host."
            )
        self._transport = transport
        self._token_getter = token_getter
        self._client = OpenAIAdsClient(transport, token_getter)

    def __repr__(self) -> str:
        return "<OpenAIAdsCreateReader redacted>"

    def discover_identity(self, *, expected_account_client_ref: str) -> OpenAIAdsIdentity:
        """Delegate to the reviewed identity read; never reimplement it."""

        return self._client.discover_identity(
            expected_account_client_ref=expected_account_client_ref
        )

    def campaigns_named(
        self, identity: OpenAIAdsIdentity, name: str
    ) -> tuple[OpenAIAdsCreateCampaignFacts, ...]:
        """Return every account campaign whose configured name equals ``name``.

        A complete bounded listing is required: a partial page would make "no
        campaign with this name" an assumption rather than an observation, so a
        listing that exceeds the page bound fails closed.
        """

        if type(name) is not str or not 3 <= len(name) <= 1_000:
            raise AdhelmError(
                "OPENAI_ADS_CREATE_READ_INVALID",
                "A bounded OpenAI Ads campaign name is required.",
                exit_code=ExitCode.PRECONDITION,
            )
        matches: list[OpenAIAdsCreateCampaignFacts] = []
        seen: set[str] = set()
        after: str | None = None
        for _page in range(_MAX_PAGES):
            params: dict[str, Any] = {"limit": _PAGE_SIZE, "order": "desc"}
            if after is not None:
                params["after"] = after
            data = _json(self._get("/campaigns", params=params), "campaign list")
            _keys(data, set(_LIST_FIELDS), "campaign list")
            page = _page_items(data, "campaign list")
            for item in page:
                if type(item) is not dict:
                    raise _response_invalid("campaign list")
                # Only the name is compared here; the whole closed object is
                # still parsed, so an unknown field fails before a decision.
                facts = parse_campaign_object(
                    item, expected_account_client_ref=identity.account_client_ref
                )
                if facts.client_ref in seen:
                    raise _response_invalid("campaign list")
                seen.add(facts.client_ref)
                if facts.name == name:
                    matches.append(facts)
            after = _next_cursor(data, "campaign list", bool(page))
            if after is None:
                return tuple(matches)
        raise AdhelmError(
            "PAGINATION_LIMIT",
            "OpenAI Ads campaign inventory exceeded the bounded page count.",
            exit_code=ExitCode.PRECONDITION,
        )

    def observe_hierarchy(
        self,
        identity: OpenAIAdsIdentity,
        campaign_client_ref: str,
        *,
        uploaded_file_client_ref: str,
        creative_content_digest: str,
    ) -> OpenAIAdsCreateHierarchyObservation:
        """Read one created campaign, its ad groups and their ads, in order."""

        campaign = parse_campaign_object(
            _json(self._get(f"/campaigns/{_checked_path_id(campaign_client_ref)}"), "campaign"),
            expected_account_client_ref=identity.account_client_ref,
        )
        if campaign.client_ref != campaign_client_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "OpenAI Ads returned a different campaign resource."
            )
        groups = tuple(
            parse_ad_group_object(item)
            for item in self._collection(
                "/ad_groups", {"campaign_id": campaign.client_ref}, "ad group list"
            )
        )
        ads: list[OpenAIAdsCreateAdFacts] = []
        for group in groups:
            ads.extend(
                parse_ad_object(item, ad_group_client_ref=group.client_ref)
                for item in self._collection("/ads", {"ad_group_id": group.client_ref}, "ad list")
            )
        if 1 + len(groups) + len(ads) > OPENAI_CREATE_MAX_HIERARCHY_MEMBERS:
            raise AdhelmError(
                "RESULT_LIMIT_REQUIRED",
                "The observed OpenAI Ads hierarchy exceeds this route's member bound.",
                exit_code=ExitCode.PRECONDITION,
            )
        observed = tuple(ads)
        members = members_from_observation(
            campaign,
            groups,
            observed,
            uploaded_file_client_ref=uploaded_file_client_ref,
            creative_content_digest=creative_content_digest,
        )
        return OpenAIAdsCreateHierarchyObservation(
            campaign=campaign,
            ad_groups=groups,
            ads=observed,
            members=members,
            projection=projection_from_members(campaign.name, members),
            file_association_proven=bool(observed)
            and all(ad.creative_file_client_ref == uploaded_file_client_ref for ad in observed),
        )

    # -- private ------------------------------------------------------------

    def _collection(
        self, path: str, selector: dict[str, Any], context: str
    ) -> tuple[dict[str, Any], ...]:
        items: list[dict[str, Any]] = []
        after: str | None = None
        for _page in range(_MAX_PAGES):
            params: dict[str, Any] = {**selector, "limit": _PAGE_SIZE, "order": "desc"}
            if after is not None:
                params["after"] = after
            data = _json(self._get(path, params=params), context)
            _keys(data, set(_LIST_FIELDS), context)
            page = _page_items(data, context)
            for item in page:
                if type(item) is not dict:
                    raise _response_invalid(context)
                items.append(item)
            if len(items) > OPENAI_CREATE_MAX_HIERARCHY_MEMBERS:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "The observed OpenAI Ads hierarchy exceeds this route's member bound.",
                    exit_code=ExitCode.PRECONDITION,
                )
            after = _next_cursor(data, context, bool(page))
            if after is None:
                return tuple(items)
        raise AdhelmError(
            "PAGINATION_LIMIT",
            f"OpenAI Ads {context} exceeded the bounded page count.",
            exit_code=ExitCode.PRECONDITION,
        )

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        try:
            return self._transport.request(
                "GET",
                f"https://{OPENAI_ADS_API_HOST}/{OPENAI_ADS_API_VERSION}{path}",
                headers={"authorization": f"Bearer {_safe_token(self._token_getter)}"},
                params=params,
            )
        except AdhelmError as error:
            raise classify_openai_ads_transport_error(error) from None


def _checked_path_id(value: object) -> str:
    return _client_id(value, "campaign ID")


def _page_items(data: dict[str, Any], context: str) -> list[Any]:
    if data.get("object") != "list":
        raise _response_invalid(context)
    page = data.get("data")
    if type(page) is not list or len(page) > _PAGE_SIZE:
        raise _response_invalid(context)
    return page


def _next_cursor(data: dict[str, Any], context: str, has_rows: bool) -> str | None:
    has_more = data.get("has_more")
    if type(has_more) is not bool:
        raise _response_invalid(context)
    if not has_more:
        return None
    if not has_rows:
        raise _response_invalid(context)
    return _safe_string(data.get("last_id"), f"{context} cursor", maximum=2_048)


__all__ = [
    "OpenAIAdsCreateAdFacts",
    "OpenAIAdsCreateAdGroupFacts",
    "OpenAIAdsCreateCampaignFacts",
    "OpenAIAdsCreateHierarchyObservation",
    "OpenAIAdsCreateReader",
    "parse_ad_group_object",
    "parse_ad_object",
    "parse_campaign_object",
    "members_from_observation",
    "projection_from_members",
]
