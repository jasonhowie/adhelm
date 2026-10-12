"""Bounded Meta Graph v26 reads for one paused single-image hierarchy CREATE.

:mod:`adhelm.providers.meta_ads` owns the reviewed read surface, and this module
does not reimplement its transport discipline: every bounded read here reuses
that module's own validated response helpers, allowlist checks, and typed
transport-error classification.

Three reads exist here that the shipped inventory contract deliberately does not
provide.

* **Configured names.**  The inventory models retain no campaign, ad-set or ad
  name, because an audit does not need one.  A CREATE does: the name is the only
  request-controllable handle a semantic before/after projection can compare, and
  it is a value this workspace itself chose.  These parsers therefore retain the
  name and nothing else the inventory contract excludes -- no ad copy, no
  destination URL, no Page identifier, no audience material.
* **Declared placement targeting.**  The inventory contract records only that a
  targeting object was declared, because such an object can carry audience
  material.  This parser accepts a targeting object whose *only* members are
  ``geo_locations.countries``, ``publisher_platforms`` and
  ``facebook_positions``, and fails closed on anything else -- including any
  custom-audience member the compiler never requests.  A response that carries
  them is drift, not input.
* **The account's own ad images.**  ``GET /act_{id}/adimages?hashes=[...]`` is
  the documented read that makes the uploaded image an *observation* rather than
  an assumption.  It returns the opaque ``hash`` and intrinsic dimensions only;
  no documented route returns stored bytes or a content digest of them.

Nothing here mutates, uploads, or authorizes.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast
from urllib.parse import parse_qs, urlsplit

from adhelm.errors import AdhelmError, ExitCode
from adhelm.providers.meta_ads import (
    META_ADS_API_HOST,
    META_ADS_API_VERSION,
    MetaAdsIdentity,
    _account_id,
    _id,
    _integer,
    _json,
    _keys,
    _safe_token,
    _string,
    classify_meta_ads_transport_error,
)
from adhelm.providers.meta_create_contract import (
    CREATIVE_ASSOCIATION_NOT_PROVEN,
    CREATIVE_ASSOCIATION_PROVEN,
    IMAGE_ASSOCIATION_NOT_PROVEN,
    META_CREATE_MAX_HIERARCHY_MEMBERS,
    MetaAdsHierarchyMember,
    MetaAdsSingleImageHierarchyProjection,
    checked_image_hash,
    creative_member_configuration,
)
from adhelm.transport import SafeHttpClient

_MAX_PAGES: Final = 10
_PAGE_SIZE: Final = 500
#: The bounded number of account campaign names one planning read will hold.
_MAX_ACCOUNT_CAMPAIGN_NAMES: Final = 5_000
#: The closed bound on the near-variant campaign names one plan compares against.
_MAX_NEAR_MATCH_NAMES: Final = 64
_CURSOR: Final = re.compile(r"^[A-Za-z0-9_~+/=-]{1,4096}$", re.ASCII)

_CAMPAIGN_NAME_FIELDS: Final = "id,name"
_CAMPAIGN_NODE_FIELDS: Final = (
    "id,account_id,name,objective,buying_type,special_ad_categories,"
    "lifetime_budget,start_time,stop_time,configured_status,effective_status"
)
_ADSET_NODE_FIELDS: Final = (
    "id,campaign_id,name,configured_status,effective_status,billing_event,"
    "optimization_goal,bid_strategy,targeting"
)
_CREATIVE_NODE_FIELDS: Final = "id,name,image_hash"
_AD_NODE_FIELDS: Final = "id,adset_id,name,configured_status,effective_status,creative{id}"
_ADIMAGE_FIELDS: Final = "hash,name,width,height,status"

_CONFIGURED_STATUSES: Final = frozenset({"ACTIVE", "ARCHIVED", "DELETED", "PAUSED"})
_EFFECTIVE_STATUSES: Final = frozenset(
    {"ACTIVE", "ARCHIVED", "DELETED", "IN_PROCESS", "PAUSED", "WITH_ISSUES"}
)
_CAMPAIGN_NAME_ROW_FIELDS: Final = {"id", "name"}
_CAMPAIGN_NODE_KEYS: Final = {
    "id",
    "account_id",
    "name",
    "objective",
    "buying_type",
    "special_ad_categories",
    "lifetime_budget",
    "start_time",
    "stop_time",
    "configured_status",
    "effective_status",
}
_ADSET_NODE_KEYS: Final = {
    "id",
    "campaign_id",
    "name",
    "configured_status",
    "effective_status",
    "billing_event",
    "optimization_goal",
    "bid_strategy",
    "targeting",
}
_CREATIVE_NODE_KEYS: Final = {"id", "name", "image_hash"}
_AD_NODE_KEYS: Final = {
    "id",
    "adset_id",
    "name",
    "configured_status",
    "effective_status",
    "creative",
}
_ADIMAGE_KEYS: Final = {"hash", "name", "width", "height", "status"}


def _response_invalid(context: str) -> AdhelmError:
    return AdhelmError(
        "PROVIDER_RESPONSE_INVALID",
        f"Meta Ads {context} is not a complete allowlisted response.",
        exit_code=ExitCode.PRECONDITION,
    )


def _read_invalid(detail: str) -> AdhelmError:
    return AdhelmError("META_ADS_CREATE_READ_INVALID", detail, exit_code=ExitCode.PRECONDITION)


def near_match_campaign_names(name: str) -> tuple[str, ...]:
    """Return the bounded near-variant names this build refuses on, sorted.

    Graph documents no comparison semantics for a campaign name, and some Meta
    surfaces treat names case-insensitively, so this build cannot ask the
    provider for "a campaign whose name matches apart from case".  Meta *does*
    return the account's own campaign names, so unlike P04-GA this build
    enumerates the closed variant set locally and compares against the observed
    names rather than probing the provider for each one.

    The set is the cross product of two seeds (the name and its stripped form),
    the four Unicode normalization forms, five case foldings, and four
    single-space paddings.  It therefore catches a name that differs only by
    surrounding whitespace, by normalization form, or by a *uniform* case
    change.  It does **not** catch an arbitrary mixed-case name such as
    ``sEpTeMbEr launch``, or padding of more than one space; both limits are
    stated in the execution contract rather than implied away.

    The byte-exact name itself is excluded: it is the separate collision check.
    """

    if type(name) is not str or not 1 <= len(name) <= 512:
        raise _read_invalid("A bounded Meta Ads campaign name is required.")
    variants: set[str] = set()
    for seed in (name, name.strip()):
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
    variants.discard(name)
    usable = sorted(
        value
        for value in variants
        if value.strip()
        and len(value.encode("utf-8")) <= 512
        and all(ord(character) >= 32 for character in value)
    )
    if len(usable) > _MAX_NEAR_MATCH_NAMES:
        raise _read_invalid(
            "This campaign name has more near variants than this build will compare."
        )
    return tuple(usable)


@dataclass(frozen=True, slots=True)
class MetaAdsCreateNameProbe:
    """The bounded campaign-name collision observation one plan is built on."""

    observed_names: int
    exact_matches: int
    near_matches: int
    #: The native references of the byte-exact matches, so a *post-create*
    #: readback can resolve the campaign this attempt just made without a second
    #: naming rule.  A planning-time probe that finds any of these refuses.
    exact_client_refs: tuple[str, ...] = ()


#: The documented Graph delivery states this route retains as an observation.
MetaEffectiveStatus = Literal[
    "ACTIVE", "ARCHIVED", "DELETED", "IN_PROCESS", "PAUSED", "WITH_ISSUES"
]


@dataclass(frozen=True, slots=True)
class MetaAdsCreateCampaignFacts:
    """The exact campaign facts this route projects and compares."""

    client_ref: str
    name: str
    objective: str
    buying_type: str
    lifetime_budget_minor_units: str
    start_time_epoch_seconds: str
    stop_time_epoch_seconds: str
    configured_status: str
    effective_status: MetaEffectiveStatus
    special_ad_categories_declared: str


@dataclass(frozen=True, slots=True)
class MetaAdsCreateAdSetFacts:
    client_ref: str
    campaign_client_ref: str
    name: str
    configured_status: str
    effective_status: MetaEffectiveStatus
    billing_event: str
    optimization_goal: str
    bid_strategy: str
    geo_countries: tuple[str, ...]
    publisher_platforms: tuple[str, ...]
    facebook_positions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MetaAdsCreateCreativeFacts:
    client_ref: str
    name: str
    image_hash: str


@dataclass(frozen=True, slots=True)
class MetaAdsCreateAdFacts:
    client_ref: str
    adset_client_ref: str
    creative_client_ref: str
    name: str
    configured_status: str
    effective_status: MetaEffectiveStatus


@dataclass(frozen=True, slots=True)
class MetaAdsCreateImageFacts:
    """One account ad-image readback row: opaque hash and intrinsic size only."""

    image_hash: str
    width_px: int
    height_px: int
    status: str


@dataclass(frozen=True, slots=True)
class MetaAdsCreateHierarchyObservation:
    """One observed created hierarchy, plus the facts the plan cannot predict."""

    campaign: MetaAdsCreateCampaignFacts | None
    adset: MetaAdsCreateAdSetFacts | None
    creative: MetaAdsCreateCreativeFacts | None
    ad: MetaAdsCreateAdFacts | None
    #: Always present, complete or not: an ordered non-atomic create can leave a
    #: campaign with no ad set, which is a real observation rather than an
    #: absence.  ``projection`` is the strict complete-hierarchy model, and it is
    #: ``None`` exactly when the observed members do not form one.
    members: tuple[MetaAdsHierarchyMember, ...]
    projection: MetaAdsSingleImageHierarchyProjection | None
    image_association_proven: bool
    creative_association_proven: bool


def _instant(value: object, context: str) -> str:
    """Parse one Graph timestamp into timezone-independent epoch seconds."""

    text = _string(value, context, maximum=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise _response_invalid(context) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _response_invalid(context)
    seconds = int(parsed.astimezone(UTC).timestamp())
    if not 946_684_800 <= seconds <= 4_102_444_800:
        raise _response_invalid(context)
    return str(seconds)


def _configured_status(value: object, context: str) -> str:
    status = _string(value, context, maximum=40)
    if status not in _CONFIGURED_STATUSES:
        raise _response_invalid(context)
    return status


def _effective_status(value: object, context: str) -> MetaEffectiveStatus:
    status = _string(value, context, maximum=40)
    if status not in _EFFECTIVE_STATUSES:
        raise _response_invalid(context)
    return cast(MetaEffectiveStatus, status)


def _special_ad_categories(value: object) -> str:
    """Accept only "no special ad category declared", in either documented form."""

    if value in (None, [], ["NONE"]):
        return "NONE_DECLARED_EMPTY"
    raise _response_invalid("campaign special ad categories")


def _declared_targeting(value: object) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Accept only the exact placement targeting this compiler requests.

    An audience member, an age bound, an interest, a provider-default targeting
    expansion member, or any unknown member fails closed.  The compiler never
    requests any of them, so their presence in a readback means the ad set is not
    the one this plan predicted.
    """

    if type(value) is not dict:
        raise _response_invalid("ad set targeting")
    _keys(value, {"geo_locations", "publisher_platforms", "facebook_positions"}, "ad set targeting")
    geo = value.get("geo_locations")
    if type(geo) is not dict:
        raise _response_invalid("ad set geo targeting")
    _keys(geo, {"countries"}, "ad set geo targeting")
    return (
        _closed_enum_list(geo.get("countries"), {"US"}, "ad set geo targeting"),
        _closed_enum_list(value.get("publisher_platforms"), {"facebook"}, "ad set placements"),
        _closed_enum_list(value.get("facebook_positions"), {"feed"}, "ad set positions"),
    )


def _closed_enum_list(value: object, allowed: set[str], context: str) -> tuple[str, ...]:
    if type(value) is not list or not 1 <= len(value) <= 8:
        raise _response_invalid(context)
    items = tuple(_string(item, context, maximum=40) for item in value)
    if any(item not in allowed for item in items) or len(set(items)) != len(items):
        raise _response_invalid(context)
    return items


def parse_campaign_node(
    payload: dict[str, Any], *, expected_account_client_ref: str
) -> MetaAdsCreateCampaignFacts:
    """Parse one documented campaign node under a closed allowlist."""

    _keys(payload, set(_CAMPAIGN_NODE_KEYS), "campaign")
    returned_account = payload.get("account_id")
    if returned_account is not None and (
        _account_id(returned_account, "campaign account ID")
        != _account_id(expected_account_client_ref, "expected account ID")
    ):
        raise AdhelmError(
            "IDENTITY_MISMATCH", "Meta Ads campaign differs from the discovered account."
        )
    objective = _string(payload.get("objective"), "campaign objective", maximum=80)
    buying_type = _string(payload.get("buying_type"), "campaign buying type", maximum=80)
    if objective != "OUTCOME_TRAFFIC" or buying_type != "AUCTION":
        raise _response_invalid("campaign objective")
    return MetaAdsCreateCampaignFacts(
        client_ref=_id(payload.get("id"), "campaign ID"),
        name=_string(payload.get("name"), "campaign name", maximum=1_000),
        objective=objective,
        buying_type=buying_type,
        lifetime_budget_minor_units=str(
            _integer(payload.get("lifetime_budget"), "campaign lifetime budget")
        ),
        start_time_epoch_seconds=_instant(payload.get("start_time"), "campaign start time"),
        stop_time_epoch_seconds=_instant(payload.get("stop_time"), "campaign stop time"),
        configured_status=_configured_status(
            payload.get("configured_status"), "campaign configured status"
        ),
        effective_status=_effective_status(
            payload.get("effective_status"), "campaign effective status"
        ),
        special_ad_categories_declared=_special_ad_categories(payload.get("special_ad_categories")),
    )


def parse_adset_node(
    payload: dict[str, Any], *, expected_campaign_client_ref: str
) -> MetaAdsCreateAdSetFacts:
    """Parse one documented ad-set node under a closed allowlist."""

    _keys(payload, set(_ADSET_NODE_KEYS), "ad set")
    campaign_client_ref = _id(payload.get("campaign_id"), "ad set campaign ID")
    if campaign_client_ref != expected_campaign_client_ref:
        raise AdhelmError("IDENTITY_MISMATCH", "Meta Ads ad set belongs to a different campaign.")
    countries, platforms, positions = _declared_targeting(payload.get("targeting"))
    return MetaAdsCreateAdSetFacts(
        client_ref=_id(payload.get("id"), "ad set ID"),
        campaign_client_ref=campaign_client_ref,
        name=_string(payload.get("name"), "ad set name", maximum=1_000),
        configured_status=_configured_status(
            payload.get("configured_status"), "ad set configured status"
        ),
        effective_status=_effective_status(
            payload.get("effective_status"), "ad set effective status"
        ),
        billing_event=_string(payload.get("billing_event"), "ad set billing event", maximum=80),
        optimization_goal=_string(
            payload.get("optimization_goal"), "ad set optimization goal", maximum=80
        ),
        bid_strategy=_string(payload.get("bid_strategy"), "ad set bid strategy", maximum=80),
        geo_countries=countries,
        publisher_platforms=platforms,
        facebook_positions=positions,
    )


def parse_creative_node(payload: dict[str, Any]) -> MetaAdsCreateCreativeFacts:
    """Parse one documented ad-creative node, reading no copy at all.

    ``object_story_spec``, ``title`` and ``body`` carry ad copy, a destination
    URL and a Page identifier.  They are never requested, so no copy text can
    reach this process.  ``image_hash`` is the association fact this route needs.
    """

    _keys(payload, set(_CREATIVE_NODE_KEYS), "ad creative")
    return MetaAdsCreateCreativeFacts(
        client_ref=_id(payload.get("id"), "ad creative ID"),
        name=_string(payload.get("name"), "ad creative name", maximum=1_000),
        image_hash=checked_image_hash(
            _string(payload.get("image_hash"), "ad creative image hash", maximum=512)
        ),
    )


def parse_ad_node(
    payload: dict[str, Any], *, expected_adset_client_ref: str
) -> MetaAdsCreateAdFacts:
    """Parse one documented ad node under a closed allowlist."""

    _keys(payload, set(_AD_NODE_KEYS), "ad")
    adset_client_ref = _id(payload.get("adset_id"), "ad ad-set ID")
    if adset_client_ref != expected_adset_client_ref:
        raise AdhelmError("IDENTITY_MISMATCH", "Meta Ads ad belongs to a different ad set.")
    creative = payload.get("creative")
    if type(creative) is not dict:
        raise _response_invalid("ad creative association")
    _keys(creative, {"id"}, "ad creative association")
    return MetaAdsCreateAdFacts(
        client_ref=_id(payload.get("id"), "ad ID"),
        adset_client_ref=adset_client_ref,
        creative_client_ref=_id(creative.get("id"), "ad creative association ID"),
        name=_string(payload.get("name"), "ad name", maximum=1_000),
        configured_status=_configured_status(
            payload.get("configured_status"), "ad configured status"
        ),
        effective_status=_effective_status(payload.get("effective_status"), "ad effective status"),
    )


def parse_account_image(payload: dict[str, Any]) -> MetaAdsCreateImageFacts:
    """Parse one documented ad-image row: opaque hash and intrinsic size only."""

    _keys(payload, set(_ADIMAGE_KEYS), "account ad image")
    width = payload.get("width")
    height = payload.get("height")
    if (
        type(width) is not int
        or type(width) is bool
        or type(height) is not int
        or type(height) is bool
        or not 1 <= width <= 65_535
        or not 1 <= height <= 65_535
    ):
        raise _response_invalid("account ad image dimensions")
    status = _string(payload.get("status"), "account ad image status", maximum=40)
    if status not in {"ACTIVE", "INTERNAL", "DELETED"}:
        raise _response_invalid("account ad image status")
    return MetaAdsCreateImageFacts(
        image_hash=checked_image_hash(
            _string(payload.get("hash"), "account ad image hash", maximum=512)
        ),
        width_px=width,
        height_px=height,
        status=status,
    )


def members_from_observation(
    campaign: MetaAdsCreateCampaignFacts | None,
    adset: MetaAdsCreateAdSetFacts | None,
    creative: MetaAdsCreateCreativeFacts | None,
    ad: MetaAdsCreateAdFacts | None,
    *,
    creative_content_digest: str,
    image_association_proven: bool,
    creative_association_proven: bool,
) -> tuple[MetaAdsHierarchyMember, ...]:
    """Reduce one observed hierarchy to the same member multiset a plan predicts.

    The creative member's ``creative_asset_content_digest`` is **not** read from
    Meta: no documented route returns the stored image bytes or a content digest
    of them.  It is the digest this attempt uploaded, admitted into the
    observation only when the created ad creative references the exact
    ``image_hash`` the upload acknowledged *and* the account's own ``adimages``
    read proves it holds that hash.  Anything else gets the
    ``IMAGE_ASSOCIATION_NOT_PROVEN`` sentinel, which cannot equal a digest, so
    the hierarchy cannot match its prediction.

    The creative member's call to action, format and presence flags are not read
    either (the node read is ``id``, ``name`` and ``image_hash``), so they carry
    the explicit ``NOT_READ_BACK_COPY_BEARING_FIELD`` sentinel here exactly as
    the prediction does.  A creative match therefore proves the creative's name,
    its image association and its ad association, and no copy or call to action.
    """

    members: list[MetaAdsHierarchyMember] = []
    if campaign is None:
        return ()
    members.append(
        MetaAdsHierarchyMember(
            member_kind="CAMPAIGN",
            identity=(campaign.name, campaign.name),
            configuration=(
                ("buying_type", campaign.buying_type),
                ("configured_status", campaign.configured_status),
                ("lifetime_budget_minor_units", campaign.lifetime_budget_minor_units),
                ("objective", campaign.objective),
                ("special_ad_categories", campaign.special_ad_categories_declared),
                ("start_time_epoch_seconds", campaign.start_time_epoch_seconds),
                ("stop_time_epoch_seconds", campaign.stop_time_epoch_seconds),
            ),
        )
    )
    if adset is not None:
        members.append(
            MetaAdsHierarchyMember(
                member_kind="AD_SET",
                identity=(campaign.name, adset.name, adset.name),
                configuration=(
                    ("bid_strategy", adset.bid_strategy),
                    ("billing_event", adset.billing_event),
                    ("configured_status", adset.configured_status),
                    ("facebook_positions", ",".join(adset.facebook_positions)),
                    ("geo_countries", ",".join(adset.geo_countries)),
                    ("optimization_goal", adset.optimization_goal),
                    ("publisher_platforms", ",".join(adset.publisher_platforms)),
                ),
            )
        )
    if adset is not None and creative is not None:
        members.append(
            MetaAdsHierarchyMember(
                member_kind="CREATIVE",
                identity=(campaign.name, adset.name, creative.name, creative.name),
                configuration=creative_member_configuration(
                    creative_content_digest
                    if image_association_proven
                    else IMAGE_ASSOCIATION_NOT_PROVEN
                ),
            )
        )
    if adset is not None and ad is not None:
        members.append(
            MetaAdsHierarchyMember(
                member_kind="AD",
                identity=(campaign.name, adset.name, ad.name, ad.name),
                configuration=(
                    ("configured_status", ad.configured_status),
                    (
                        "creative_association",
                        CREATIVE_ASSOCIATION_PROVEN
                        if creative_association_proven
                        else CREATIVE_ASSOCIATION_NOT_PROVEN,
                    ),
                ),
            )
        )
    if len(members) > META_CREATE_MAX_HIERARCHY_MEMBERS:  # pragma: no cover - fixed at four
        raise _response_invalid("hierarchy member bound")
    return tuple(members)


def projection_from_members(
    campaign_name: str, members: tuple[MetaAdsHierarchyMember, ...]
) -> MetaAdsSingleImageHierarchyProjection | None:
    """Return the strict complete projection, or ``None`` for a partial hierarchy."""

    try:
        return MetaAdsSingleImageHierarchyProjection(campaign_name=campaign_name, members=members)
    except Exception:
        return None


class MetaAdsCreateReader:
    """Fixed bounded v26 reads for one single-image CREATE, over one transport."""

    __slots__ = ("_token_getter", "_transport")

    def __init__(self, transport: SafeHttpClient, token_getter: Any) -> None:
        if type(transport) is not SafeHttpClient or not callable(token_getter):
            raise _read_invalid("A configured Meta Ads transport and token source are required.")
        if META_ADS_API_HOST not in transport.allowed_hosts:
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID", "Meta Ads CREATE requires its fixed Graph API host."
            )
        self._transport = transport
        self._token_getter = token_getter

    def __repr__(self) -> str:
        return "<MetaAdsCreateReader redacted>"

    def campaign_name_probe(self, identity: MetaAdsIdentity, name: str) -> MetaAdsCreateNameProbe:
        """Compare the planned name with the campaign names this edge lists.

        The read is ``GET /act_{id}/campaigns`` with no ``effective_status``
        filter, paged to a bound.  A partial page would make "no campaign with
        this name" an assumption rather than an observation, so a listing that
        exceeds the page bound fails closed.  What the edge lists by default
        (in particular whether archived or deleted campaigns are included), and
        whether a just-created campaign is visible on the immediate list read,
        are ``UNVERIFIED`` wire facts; see the wire-fact table in
        ``docs/implementation/meta-create-execution.md``.
        """

        if type(name) is not str or not 1 <= len(name) <= 512:
            raise _read_invalid("A bounded Meta Ads campaign name is required.")
        near = set(near_match_campaign_names(name))
        account = _account_id(identity.account_client_ref, "identity account ID")
        observed: list[str] = []
        exact: list[str] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        after: str | None = None
        for _page in range(_MAX_PAGES):
            params: dict[str, Any] = {"fields": _CAMPAIGN_NAME_FIELDS, "limit": _PAGE_SIZE}
            if after is not None:
                params["after"] = after
            data = _json(self._get(f"/{account}/campaigns", params=params), "campaign name list")
            rows, after = _paged(data, "campaign name list", account, "campaigns")
            for item in rows:
                if type(item) is not dict:
                    raise _response_invalid("campaign name list")
                _keys(item, set(_CAMPAIGN_NAME_ROW_FIELDS), "campaign name list")
                identifier = _id(item.get("id"), "campaign ID")
                if identifier in seen_ids:
                    raise _response_invalid("campaign name list")
                seen_ids.add(identifier)
                observed_name = _string(item.get("name"), "campaign name", maximum=1_000)
                observed.append(observed_name)
                if observed_name == name:
                    exact.append(identifier)
            if len(observed) > _MAX_ACCOUNT_CAMPAIGN_NAMES:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "This Meta ad account holds more campaigns than one bounded name read "
                    "will compare.",
                    exit_code=ExitCode.PRECONDITION,
                )
            if after is None:
                return MetaAdsCreateNameProbe(
                    observed_names=len(observed),
                    exact_matches=len(exact),
                    near_matches=sum(item in near for item in observed),
                    exact_client_refs=tuple(exact),
                )
            if after in seen_cursors:
                raise _response_invalid("campaign name list")
            seen_cursors.add(after)
        raise AdhelmError(
            "PAGINATION_LIMIT",
            "Meta Ads campaign inventory exceeded the bounded page count.",
            exit_code=ExitCode.PRECONDITION,
        )

    def observe_hierarchy(
        self,
        identity: MetaAdsIdentity,
        *,
        campaign_client_ref: str,
        adset_client_ref: str,
        creative_client_ref: str,
        ad_client_ref: str,
        uploaded_image_hash: str,
        creative_content_digest: str,
    ) -> MetaAdsCreateHierarchyObservation:
        """Read back whatever this attempt actually created, in order.

        An empty reference means the corresponding child never acknowledged, so
        that node is not read.  A partial hierarchy is an observation, not an
        error: it is exactly what an interrupted ordered create leaves behind.
        """

        account = _account_id(identity.account_client_ref, "identity account ID")
        campaign = None
        adset = None
        creative = None
        ad = None
        image = None
        if campaign_client_ref:
            campaign = parse_campaign_node(
                _json(self._node(campaign_client_ref, _CAMPAIGN_NODE_FIELDS), "campaign"),
                expected_account_client_ref=identity.account_client_ref,
            )
            if campaign.client_ref != campaign_client_ref:
                raise AdhelmError(
                    "IDENTITY_MISMATCH", "Meta Ads returned a different campaign resource."
                )
        if campaign is not None and adset_client_ref:
            adset = parse_adset_node(
                _json(self._node(adset_client_ref, _ADSET_NODE_FIELDS), "ad set"),
                expected_campaign_client_ref=campaign.client_ref,
            )
            if adset.client_ref != adset_client_ref:
                raise AdhelmError(
                    "IDENTITY_MISMATCH", "Meta Ads returned a different ad-set resource."
                )
        if creative_client_ref:
            creative = parse_creative_node(
                _json(self._node(creative_client_ref, _CREATIVE_NODE_FIELDS), "ad creative")
            )
            if creative.client_ref != creative_client_ref:
                raise AdhelmError(
                    "IDENTITY_MISMATCH", "Meta Ads returned a different ad-creative resource."
                )
        if adset is not None and ad_client_ref:
            ad = parse_ad_node(
                _json(self._node(ad_client_ref, _AD_NODE_FIELDS), "ad"),
                expected_adset_client_ref=adset.client_ref,
            )
            if ad.client_ref != ad_client_ref:
                raise AdhelmError("IDENTITY_MISMATCH", "Meta Ads returned a different ad resource.")
        if uploaded_image_hash:
            image = self.account_image(account, uploaded_image_hash)
        image_proven = (
            image is not None
            and creative is not None
            and bool(uploaded_image_hash)
            and image.image_hash == uploaded_image_hash
            and creative.image_hash == uploaded_image_hash
            and image.status == "ACTIVE"
        )
        creative_proven = (
            ad is not None
            and creative is not None
            and ad.creative_client_ref == creative.client_ref
        )
        members = members_from_observation(
            campaign,
            adset,
            creative,
            ad,
            creative_content_digest=creative_content_digest,
            image_association_proven=image_proven,
            creative_association_proven=creative_proven,
        )
        return MetaAdsCreateHierarchyObservation(
            campaign=campaign,
            adset=adset,
            creative=creative,
            ad=ad,
            members=members,
            projection=(
                None if campaign is None else projection_from_members(campaign.name, members)
            ),
            image_association_proven=image_proven,
            creative_association_proven=creative_proven,
        )

    def account_image(self, account: str, image_hash: str) -> MetaAdsCreateImageFacts | None:
        """Read the one documented proof that this account holds the image.

        This is the read OpenAI Ads does not document at all.  It returns the
        opaque provider ``hash`` and the intrinsic dimensions; it does not return
        the stored bytes or any content digest of them, so it proves possession
        and association, never byte equality.
        """

        checked = checked_image_hash(image_hash)
        data = _json(
            self._get(
                f"/{account}/adimages",
                params={
                    "fields": _ADIMAGE_FIELDS,
                    "hashes": json.dumps([checked], ensure_ascii=True, separators=(",", ":")),
                    "limit": 2,
                },
            ),
            "account ad image list",
        )
        rows, after = _paged(data, "account ad image list", account, "adimages")
        if after is not None or len(rows) > 1:
            raise _response_invalid("account ad image list")
        if not rows:
            return None
        if type(rows[0]) is not dict:
            raise _response_invalid("account ad image list")
        observed = parse_account_image(rows[0])
        if observed.image_hash != checked:
            raise _response_invalid("account ad image list")
        return observed

    # -- private ------------------------------------------------------------

    def _node(self, client_ref: str, fields: str) -> Any:
        return self._get(f"/{_id(client_ref, 'node ID')}", params={"fields": fields})

    def _get(self, path: str, *, params: dict[str, Any]) -> Any:
        try:
            return self._transport.request(
                "GET",
                f"https://{META_ADS_API_HOST}/{META_ADS_API_VERSION}{path}",
                headers={"authorization": f"Bearer {_safe_token(self._token_getter)}"},
                params=params,
            )
        except AdhelmError as error:
            raise classify_meta_ads_transport_error(error) from None


def _paged(
    response: dict[str, Any], context: str, account: str, edge: str
) -> tuple[list[Any], str | None]:
    """Apply the shipped Meta cursor-paging discipline to a bounded read."""

    _keys(response, {"data", "paging"}, context)
    page = response.get("data")
    if type(page) is not list or len(page) > _PAGE_SIZE:
        raise _response_invalid(context)
    paging = response.get("paging")
    if paging is None:
        return page, None
    if type(paging) is not dict:
        raise _response_invalid(f"{context} paging")
    _keys(paging, {"cursors", "next", "previous"}, f"{context} paging")
    cursors = paging.get("cursors")
    next_url = paging.get("next")
    if cursors is None:
        if next_url is not None:
            raise _response_invalid(f"{context} paging")
        return page, None
    if type(cursors) is not dict:
        raise _response_invalid(f"{context} cursors")
    _keys(cursors, {"before", "after"}, f"{context} cursors")
    if next_url is None:
        return page, None
    after = cursors.get("after")
    if type(after) is not str or _CURSOR.fullmatch(after) is None:
        raise _response_invalid(f"{context} cursors")
    _validate_next(next_url, after, account, edge, context)
    return page, after


def _validate_next(value: object, after: str, account: str, edge: str, context: str) -> None:
    if type(value) is not str or len(value) > 8_192:
        raise _response_invalid(f"{context} next URL")
    try:
        parsed = urlsplit(value)
        params = parse_qs(parsed.query, strict_parsing=True, max_num_fields=32)
    except ValueError:
        raise _response_invalid(f"{context} next URL") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != META_ADS_API_HOST
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.path != f"/{META_ADS_API_VERSION}/{account}/{edge}"
        or params.get("after") != [after]
    ):
        raise _response_invalid(f"{context} next URL")


__all__ = [
    "MetaAdsCreateAdFacts",
    "MetaAdsCreateAdSetFacts",
    "MetaAdsCreateCampaignFacts",
    "MetaAdsCreateCreativeFacts",
    "MetaAdsCreateHierarchyObservation",
    "MetaAdsCreateImageFacts",
    "MetaAdsCreateNameProbe",
    "MetaAdsCreateReader",
    "members_from_observation",
    "near_match_campaign_names",
    "parse_account_image",
    "parse_ad_node",
    "parse_adset_node",
    "parse_campaign_node",
    "parse_creative_node",
    "projection_from_members",
]
