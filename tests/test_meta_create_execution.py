"""Meta Ads paused single-image hierarchy CREATE on the generic route, with fakes only.

Real here: the P04-MA contract, the recompile-and-bind promotion of a retained
offline candidate, the private local creative vault, the real Meta EDIT token
session with its two inspected credential slots, the S3 retained record, the S4
route, a real signed approval receipt from an ephemeral test key, a real
``SafetyKernel`` with its journaled child-egress interface, and a real local
ledger.  Fake here: the operating-system credential store, the HTTP transport,
and the *installed* approval bridge.  Nothing contacts Meta, a real credential
store, a native helper, or a network socket, and none of this is evidence of
installed native authority or live provider conformance.

One build dependency stays deliberately visible.  ``ledger.py`` now registers the
additive record kinds ``META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_PLAN`` and
``META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_READBACK_OBSERVATION``, so the readback
history validator runs for real here on both append and scan.
``_install_record_kinds`` is therefore an idempotent no-op in this checkout and
remains only so the suite proves the whole path either side of that
registration, and never hides its absence: the planner and the adapter each
refuse closed, before any credential, when a kind is missing.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from test_generic_execution_route import _FakeCompositeBridge, _install

from adhelm import _meta_ads_token_session as shared_session
from adhelm import config as config_module
from adhelm import generic_execution_workflow as workflow
from adhelm import ledger as ledger_module
from adhelm import meta_ads_token_edit_session as edit_session
from adhelm import meta_create_execution as execution
from adhelm import meta_token_credentials as token_credentials
from adhelm import meta_token_inspection as inspection
from adhelm.assets import store_image
from adhelm.canonical import digest
from adhelm.config import load_configuration
from adhelm.creative import (
    CreativeAssetVersion,
    CreativeComponent,
    CreativeRendition,
    CreativeVersion,
    ServingCreativeManifest,
    ServingRendition,
    decode_image,
)
from adhelm.errors import AdhelmError
from adhelm.execution_planning_session import (
    CONFIGURED_EXECUTION_PLANNERS,
    RetainedExecutionPlan,
    configured_execution_planner,
)
from adhelm.generic_execution_workflow import (
    CONFIGURED_EXECUTION_ADAPTERS,
    ExecutionAdapterCapabilities,
    ExecutionPlanExecutionRequest,
    configured_execution_adapter,
    executable_execution_operations,
    execute_retained_execution_plan_with_installed_approval,
    register_execution_adapter,
    unresolved_attempt_catalog,
)
from adhelm.ledger import Ledger
from adhelm.meta_create_execution import (
    META_CREATE_EXECUTION_ADAPTER_FACTORY,
    META_CREATE_EXECUTION_PLANNER,
    _retained_action_for_plan,
)
from adhelm.meta_create_planning_session import near_match_campaign_names
from adhelm.meta_create_readback_records import (
    META_CREATE_READBACK_RECORD_KIND,
    MetaAdsSingleImageHierarchyCreateReadbackObservation,
    pseudonymized_resource_ref,
    validate_meta_create_readback_history,
)
from adhelm.meta_token_credentials import enroll_meta_token_profile
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.models import Money
from adhelm.plan_cli import ExecutionPlanRequest
from adhelm.providers import meta_create_adapter as adapter_module
from adhelm.providers import meta_create_contract as contract_module
from adhelm.providers.meta_compiler import (
    MetaAdsCampaignBrief,
    MetaAdsCreativeAssociation,
    MetaAdsDestinationPolicy,
    MetaAdsImageReferenceEvidence,
    MetaAdsSingleImageLinkCreative,
    compile_meta_ads_campaign,
)
from adhelm.providers.meta_create_adapter import creative_request_form
from adhelm.providers.meta_create_contract import (
    META_CREATE_MAX_FORM_VALUE_LENGTH,
    META_CREATE_PLAN_RECORD_KIND,
    META_CREATE_PLANNING_IMAGE_HASH,
    NOT_READ_BACK_COPY_BEARING_FIELD,
    MetaAdsSingleImageHierarchyCreatePlanRecord,
    MetaAdsSingleImageHierarchyCreateSelection,
    checked_image_hash,
    create_child_plan,
    creative_member_configuration,
    projection_from_compiled_spec,
)
from adhelm.safety import EffectStatus, OperationKind, StateRef
from adhelm.saved_candidates import retain_candidate
from adhelm.transport import SafeHttpClient

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_NOW = datetime(2030, 2, 3, 12, 0, tzinfo=UTC)
_SUBJECT = "meta-create-subject-token"
_INSPECTOR = "meta-create-inspector-token"
_REAL_HTTP_CLIENT = httpx.Client

_PROVIDER = "meta_ads"
_PROFILE_ID = "meta-create-edit"
_ACCOUNT_NATIVE = "12345"
_ACCOUNT_CLIENT_REF = f"act_{_ACCOUNT_NATIVE}"
_ACCOUNT_REF = f"meta_ads.account.{_ACCOUNT_CLIENT_REF}"
_APPLICATION_REF = "67890"
_PAGE_ID = "998877665544332211"
_SHARING_KEY = b"fixture-private-sharing-key-32-bytes"
_CAMPAIGN_ID = "700000000000001"
_ADSET_ID = "700000000000002"
_CREATIVE_ID = "700000000000003"
_AD_ID = "700000000000004"
_RIVAL_CAMPAIGN_ID = "700000000000999"
_RIVAL_CAMPAIGN_ID_BASE = 700_000_000_000_800
_IMAGE_HASH = "b5f5f9a1f3f4f5c6d7e8f90011223344"
_GOLDEN = Path(__file__).parent / "fixtures" / "meta_ads_hierarchy_create_requests.json"


# --- the local creative -----------------------------------------------------


def _png(colour: str = "#123456", size: tuple[int, int] = (8, 4)) -> bytes:
    from PIL import Image

    output = BytesIO()
    Image.new("RGB", size, colour).save(output, format="PNG")
    return output.getvalue()


_IMAGE = _png()
_IMAGE_DIGEST = hashlib.sha256(_IMAGE).hexdigest()
_DECODED = decode_image(_IMAGE)
_ASSET_VERSION_ID = "asset:sha256:" + digest(
    {
        "content_digest": _IMAGE_DIGEST,
        "mime_type": _DECODED.mime_type,
        "width_px": _DECODED.width_px,
        "height_px": _DECODED.height_px,
    }
)


def _association(
    primary_text: str, headline: str, destination_url: str, *, image: bytes = _IMAGE
) -> MetaAdsCreativeAssociation:
    content_digest = hashlib.sha256(image).hexdigest()
    decoded = decode_image(image)
    asset = CreativeAssetVersion(
        asset_version_id="asset:sha256:"
        + digest(
            {
                "content_digest": content_digest,
                "mime_type": decoded.mime_type,
                "width_px": decoded.width_px,
                "height_px": decoded.height_px,
            }
        ),
        content_digest=content_digest,
        media_type="IMAGE",
        mime_type=decoded.mime_type,
        byte_size=len(image),
        width_px=decoded.width_px,
        height_px=decoded.height_px,
        aspect_ratio="2:1",
    )
    rendition = CreativeRendition.from_asset(
        asset, provider_format="META_ADS_SINGLE_IMAGE_LINK", locale="en-US"
    )
    destination_ref = "destination:sha256:" + digest({"target_url": destination_url})
    version = CreativeVersion(
        creative_family_id="family:meta-traffic",
        manifest=ServingCreativeManifest(
            provider_format="META_ADS_SINGLE_IMAGE_LINK",
            locale="en-US",
            destination_ref=destination_ref,
            rendition_refs=(
                ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
            ),
            components=(
                CreativeComponent(
                    component_id="body",
                    component_role="PRIMARY_TEXT",
                    ordinal=0,
                    exact_text=primary_text,
                    normalized_text=primary_text.lower(),
                    language="en",
                    provider_slot="message",
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
                CreativeComponent(
                    component_id="headline",
                    component_role="HEADLINE",
                    ordinal=1,
                    exact_text=headline,
                    normalized_text=headline.lower(),
                    language="en",
                    provider_slot="name",
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
                CreativeComponent(
                    component_id="cta",
                    component_role="CTA_BUTTON",
                    ordinal=2,
                    exact_text="Learn More",
                    normalized_text="learn more",
                    language="en",
                    provider_slot="call_to_action.type",
                    normalized_cta_intent="LEARN_MORE",
                    cta_destination_ref=destination_ref,
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
            ),
        ),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    return MetaAdsCreativeAssociation(creative_version=version, asset=asset, rendition=rendition)


def _brief(**updates: Any) -> MetaAdsCampaignBrief:
    destination_url = "https://example.test/demo?utm_source=meta"
    primary_text = "Bring tasks and documents together."
    headline = "Plan work faster"
    values: dict[str, Any] = {
        "account_ref": _ACCOUNT_REF,
        "account_currency": "USD",
        "currency_minor_unit_exponent": 2,
        "account_timezone": "UTC",
        "campaign_handle": "launch",
        "adset_handle": "us-feed",
        "ad_handle": "image-link",
        "campaign_name": "September launch",
        "adset_name": "US Facebook Feed",
        "ad_name": "September single image",
        "lifetime_budget": Money(minor_units=25_000, currency="USD"),
        "start_time": datetime(2026, 10, 1, tzinfo=UTC),
        "stop_time": datetime(2026, 10, 31, tzinfo=UTC),
        "destination_policy": MetaAdsDestinationPolicy(
            allowed_hosts=("example.test",), allowed_paths=("/demo",)
        ),
        "creative": MetaAdsSingleImageLinkCreative(
            page_id=_PAGE_ID,
            primary_text=primary_text,
            headline=headline,
            destination_url=destination_url,
            association=_association(primary_text, headline, destination_url),
            image_reference=MetaAdsImageReferenceEvidence(
                account_ref=_ACCOUNT_REF,
                image_hash="customer-local-evidence-hash",
                asset_content_digest=_IMAGE_DIGEST,
                evidence_ref="evidence:sha256:" + "a" * 64,
            ),
        ),
    }
    values.update(updates)
    return MetaAdsCampaignBrief(**values)


def _brief_for_image(image: bytes) -> MetaAdsCampaignBrief:
    """A brief whose declared creative is this exact image, not the default one."""

    base = _brief()
    creative = base.creative
    association = _association(
        creative.primary_text, creative.headline, creative.destination_url, image=image
    )
    return _brief(
        creative=creative.model_copy(
            update={
                "association": association,
                "image_reference": creative.image_reference.model_copy(
                    update={"asset_content_digest": hashlib.sha256(image).hexdigest()}
                ),
            }
        )
    )


def _brief_with_copy(primary_text: str) -> MetaAdsCampaignBrief:
    headline = "\u00e9" * 255
    destination_url = "https://example.test/demo?utm_source=meta"
    creative = _brief().creative.model_copy(
        update={
            "primary_text": primary_text,
            "headline": headline,
            "association": _association(primary_text, headline, destination_url),
        }
    )
    return _brief(creative=creative)


def _boundary_brief() -> MetaAdsCampaignBrief:
    """A brief whose creative ``object_story_spec`` is exactly the form-value bound.

    The copy mixes six-character ``\\uXXXX`` escapes and plain characters so the
    planning-time form (with the reserved image hash) is exactly
    ``META_CREATE_MAX_FORM_VALUE_LENGTH`` characters long: one more character
    anywhere in that form is unexpressible.
    """

    base = len(
        creative_request_form(
            compile_meta_ads_campaign(_brief_with_copy("x")),
            image_hash=META_CREATE_PLANNING_IMAGE_HASH,
        )["object_story_spec"]
    )
    accents, pad = divmod(META_CREATE_MAX_FORM_VALUE_LENGTH - (base - 1), 6)
    return _brief_with_copy("\u00e9" * accents + "x" * pad)


_SPEC = compile_meta_ads_campaign(_brief())
_CAMPAIGN_NAME = _SPEC.campaign_request.name


# --- the fake credential store ---------------------------------------------


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


def _draft(**updates: object) -> MetaAccountTokenProfileDraft:
    data: dict[str, object] = {
        "profile_id": _PROFILE_ID,
        "account_ref": _ACCOUNT_NATIVE,
        "client_application_ref": _APPLICATION_REF,
        "currency": "USD",
        "timezone": "UTC",
        "purpose": "EDIT",
        "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
    }
    data.update(updates)
    return MetaAccountTokenProfileDraft.model_validate(data)


def _debug_payload() -> dict[str, object]:
    return {
        "data": {
            "app_id": _APPLICATION_REF,
            "user_id": "54321",
            "is_valid": True,
            "type": "USER",
            "scopes": ["ads_management", "business_management", "public_profile"],
            "issued_at": int((_NOW - timedelta(minutes=1)).timestamp()),
            "expires_at": int((_NOW + timedelta(minutes=30)).timestamp()),
            "data_access_expires_at": int((_NOW + timedelta(days=1)).timestamp()),
        }
    }


# --- the fake provider ------------------------------------------------------


class _FakeMetaGraph:
    """One in-memory ad account behind the fixed v26 read and create routes."""

    def __init__(
        self,
        *,
        directory: Path,
        account_status: int = 1,
        currency: str = "USD",
        timezone: str = "UTC",
        existing_campaign_names: tuple[str, ...] = (),
        fail_at: int | None = None,
        extra_ack_field: int | None = None,
        readback_overrides: dict[str, dict[str, Any]] | None = None,
        hold_images: bool = True,
        competing_campaign: bool = False,
        list_omits_created_campaign: bool = False,
        listed_after_create: tuple[str, ...] = (),
        campaign_page_size: int | None = None,
        campaign_paging_fault: str | None = None,
        image_hash: str = _IMAGE_HASH,
    ) -> None:
        self.directory = directory
        # The hash the fake provider mints for the upload and echoes everywhere.
        self.image_hash = image_hash
        self.account_status = account_status
        self.currency = currency
        self.timezone = timezone
        self.existing_campaign_names = existing_campaign_names
        self.fail_at = fail_at
        self.extra_ack_field = extra_ack_field
        # What a misbehaving provider reads back, per created node kind.
        self.readback_overrides = readback_overrides or {}
        # Whether the account's ``adimages`` read admits holding the upload.
        self.hold_images = hold_images
        # Whether a second campaign with the planned name appears once the
        # first one exists (a concurrent writer racing this attempt).
        self.competing_campaign = competing_campaign
        # Whether the campaign listing lags the create: the campaign exists and
        # reads by identifier, but ``GET /act_{id}/campaigns`` does not list it.
        self.list_omits_created_campaign = list_omits_created_campaign
        # Further campaign names the listing shows once this attempt's campaign
        # exists, whether or not the listing also shows that campaign.
        self.listed_after_create = listed_after_create
        # Serve the account's campaign names in pages of this size, optionally
        # with a looping cursor ("loop") or a next link on a foreign host
        # ("foreign_next").
        self.campaign_page_size = campaign_page_size
        self.campaign_paging_fault = campaign_paging_fault
        self.requests: list[dict[str, Any]] = []
        self.reads: list[str] = []
        self.posts = 0
        self.child_starts_at_post: list[int] = []
        self.campaign: dict[str, Any] | None = None
        self.adset: dict[str, Any] | None = None
        self.creative: dict[str, Any] | None = None
        self.ad: dict[str, Any] | None = None
        self.images: list[str] = []

    # -- transport ----------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            self.reads.append(path)
            return self._read(path, request)
        assert request.method == "POST"
        self.posts += 1
        self.child_starts_at_post.append(self._child_starts())
        self.requests.append(self._record(request))
        if self.fail_at == self.posts:
            raise httpx.ConnectError("synthetic transport loss", request=request)
        return self._create(path)

    def _child_starts(self) -> int:
        return len(
            [
                row
                for row in Ledger(self.directory).records("changes")
                if row["record_kind"] == "CHILD_APPLY_STARTED"
            ]
        )

    @staticmethod
    def _record(request: httpx.Request) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            import re

            body = request.content
            disposition = re.search(rb"Content-Disposition: (form-data[^\r\n]*)", body)
            media = re.search(rb"Content-Type: ([^\r\n]+)", body)
            return {
                "method": request.method,
                "path": request.url.path,
                "multipart": {
                    "content_disposition": (disposition.group(1).decode() if disposition else None),
                    "part_media_type": media.group(1).decode() if media else None,
                    "part_byte_length": len(_IMAGE),
                },
            }
        from urllib.parse import parse_qsl

        return {
            "method": request.method,
            "path": request.url.path,
            "form": dict(sorted(parse_qsl(request.content.decode(), keep_blank_values=True))),
        }

    # -- reads --------------------------------------------------------------

    def _read(self, path: str, request: httpx.Request) -> httpx.Response:
        if path == f"/v26.0/{_ACCOUNT_CLIENT_REF}":
            return httpx.Response(
                200,
                json={
                    "id": _ACCOUNT_NATIVE,
                    "account_status": self.account_status,
                    "currency": self.currency,
                    "timezone_name": self.timezone,
                },
            )
        if path == f"/v26.0/{_ACCOUNT_CLIENT_REF}/campaigns":
            rows = [
                {"id": str(700_000_000_000_100 + index), "name": name}
                for index, name in enumerate(self.existing_campaign_names)
            ]
            if self.campaign is not None and not self.list_omits_created_campaign:
                rows.append({"id": _CAMPAIGN_ID, "name": self.campaign["name"]})
                if self.competing_campaign:
                    rows.append({"id": _RIVAL_CAMPAIGN_ID, "name": self.campaign["name"]})
            if self.campaign is not None:
                rows.extend(
                    {"id": str(_RIVAL_CAMPAIGN_ID_BASE + index), "name": name}
                    for index, name in enumerate(self.listed_after_create)
                )
            return httpx.Response(200, json=self._page(rows, request))
        if path == f"/v26.0/{_ACCOUNT_CLIENT_REF}/adimages":
            hashes = json.loads(request.url.params.get("hashes", "[]"))
            rows = [
                {
                    "hash": value,
                    "name": "creative.png",
                    "width": _DECODED.width_px,
                    "height": _DECODED.height_px,
                    "status": "ACTIVE",
                }
                for value in hashes
                if value in self.images and self.hold_images
            ]
            return httpx.Response(200, json={"data": rows})
        node = path.rsplit("/", 1)[1]
        kind, stored = {
            _CAMPAIGN_ID: ("campaign", self.campaign),
            _ADSET_ID: ("adset", self.adset),
            _CREATIVE_ID: ("creative", self.creative),
            _AD_ID: ("ad", self.ad),
        }[node]
        assert stored is not None, path
        return httpx.Response(200, json={**stored, **self.readback_overrides.get(kind, {})})

    def _page(self, rows: list[dict[str, str]], request: httpx.Request) -> dict[str, Any]:
        size = self.campaign_page_size
        if size is None:
            return {"data": rows}
        after = request.url.params.get("after")
        start = int(after[1:]) if after else 0
        page = rows[start : start + size]
        if start + size >= len(rows):
            return {"data": page}
        cursor = "A1" if self.campaign_paging_fault == "loop" else f"A{start + size}"
        host = (
            "evil.example" if self.campaign_paging_fault == "foreign_next" else "graph.facebook.com"
        )
        return {
            "data": page,
            "paging": {
                "cursors": {"before": "B0", "after": cursor},
                "next": f"https://{host}/v26.0/{_ACCOUNT_CLIENT_REF}/campaigns?after={cursor}",
            },
        }

    # -- creates ------------------------------------------------------------

    def _create(self, path: str) -> httpx.Response:
        edge = path.rsplit("/", 1)[1]
        if edge == "adimages":
            self.images.append(self.image_hash)
            return httpx.Response(
                200,
                json=self._extra(
                    {
                        "images": {
                            "creative.png": {
                                "hash": self.image_hash,
                                "url": "https://scontent.invalid/creative.png",
                                "width": _DECODED.width_px,
                                "height": _DECODED.height_px,
                                "name": "creative.png",
                            }
                        }
                    },
                    1,
                    nested=True,
                ),
            )
        if edge == "campaigns":
            self.campaign = self._campaign_object()
            return httpx.Response(200, json=self._extra({"id": _CAMPAIGN_ID}, 2))
        if edge == "adsets":
            self.adset = self._adset_object()
            return httpx.Response(200, json=self._extra({"id": _ADSET_ID}, 3))
        if edge == "adcreatives":
            self.creative = self._creative_object()
            return httpx.Response(200, json=self._extra({"id": _CREATIVE_ID}, 4))
        assert edge == "ads"
        self.ad = self._ad_object()
        return httpx.Response(200, json=self._extra({"id": _AD_ID}, 5))

    def _extra(
        self, payload: dict[str, Any], index: int, *, nested: bool = False
    ) -> dict[str, Any]:
        if self.extra_ack_field != index:
            return payload
        if nested:
            payload["images"]["creative.png"]["undocumented_field"] = "surprise"
        else:
            payload["undocumented_field"] = "surprise"
        return payload

    @staticmethod
    def _campaign_object() -> dict[str, Any]:
        campaign = _SPEC.campaign_request
        return {
            "id": _CAMPAIGN_ID,
            "account_id": _ACCOUNT_NATIVE,
            "name": campaign.name,
            "objective": campaign.objective,
            "buying_type": campaign.buying_type,
            "special_ad_categories": [],
            "lifetime_budget": str(campaign.lifetime_budget),
            # Graph renders instants with a colon-less offset, e.g. ``+0000``.
            "start_time": campaign.start_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S%z"),
            "stop_time": campaign.stop_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S%z"),
            "configured_status": "PAUSED",
            "effective_status": "PAUSED",
        }

    @staticmethod
    def _adset_object() -> dict[str, Any]:
        adset = _SPEC.adset_request
        return {
            "id": _ADSET_ID,
            "campaign_id": _CAMPAIGN_ID,
            "name": adset.name,
            "configured_status": "PAUSED",
            "effective_status": "PAUSED",
            "billing_event": adset.billing_event,
            "optimization_goal": adset.optimization_goal,
            "bid_strategy": adset.bid_strategy,
            "targeting": {
                "geo_locations": {"countries": list(adset.targeting.geo_locations.countries)},
                "publisher_platforms": list(adset.targeting.publisher_platforms),
                "facebook_positions": list(adset.targeting.facebook_positions),
            },
        }

    def _creative_object(self) -> dict[str, Any]:
        return {
            "id": _CREATIVE_ID,
            "name": _SPEC.creative_request.name,
            "image_hash": self.image_hash,
        }

    @staticmethod
    def _ad_object() -> dict[str, Any]:
        return {
            "id": _AD_ID,
            "adset_id": _ADSET_ID,
            "name": _SPEC.ad_request.name,
            "configured_status": "PAUSED",
            "effective_status": "PAUSED",
            "creative": {"id": _CREATIVE_ID},
        }


# --- harness ----------------------------------------------------------------


def _install_record_kinds(monkeypatch: pytest.MonkeyPatch, *, plan: bool, readback: bool) -> None:
    """Install the additive ``_KINDS`` entries when a build lacks them.

    ``ledger.py`` carries both entries, so this is a no-op here.  It stays
    because the delete branches below are how the two fail-closed build checks
    are exercised, and because a build that vendors an older ledger must still
    refuse rather than create a hierarchy it cannot retain evidence for.
    """

    kinds = ledger_module._KINDS["changes"]
    if plan and kinds.get(META_CREATE_PLAN_RECORD_KIND) is not (
        MetaAdsSingleImageHierarchyCreatePlanRecord
    ):
        monkeypatch.setitem(
            kinds, META_CREATE_PLAN_RECORD_KIND, MetaAdsSingleImageHierarchyCreatePlanRecord
        )
    if readback and kinds.get(META_CREATE_READBACK_RECORD_KIND) is not (
        MetaAdsSingleImageHierarchyCreateReadbackObservation
    ):
        monkeypatch.setitem(
            kinds,
            META_CREATE_READBACK_RECORD_KIND,
            MetaAdsSingleImageHierarchyCreateReadbackObservation,
        )
    if not plan:
        monkeypatch.delitem(kinds, META_CREATE_PLAN_RECORD_KIND, raising=False)
    if not readback:
        monkeypatch.delitem(kinds, META_CREATE_READBACK_RECORD_KIND, raising=False)


class _Prepared:
    def __init__(
        self,
        directory: Path,
        meta: _FakeMetaGraph,
        candidate_record_digest: str,
        brief: MetaAdsCampaignBrief,
        inspections: list[httpx.Request],
    ) -> None:
        self.directory = directory
        self.meta = meta
        self.candidate_record_digest = candidate_record_digest
        self.brief = brief
        self.inspections = inspections


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan_kind: bool = True,
    readback_kind: bool = True,
    retain: bool = True,
    store_creative: bool = True,
    brief: MetaAdsCampaignBrief | None = None,
    subject: str = _SUBJECT,
    purpose: str = "EDIT",
    **meta: Any,
) -> _Prepared:
    _install_record_kinds(monkeypatch, plan=plan_kind, readback=readback_kind)
    store = _MemoryStore()
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        token_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(
        shared_session, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(config_module, "credential_mutation_lock", lambda _reference: nullcontext())
    enroll_meta_token_profile(
        tmp_path,
        _draft(purpose=purpose),
        subject_token_supplier=lambda: subject,
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    fake = _FakeMetaGraph(directory=tmp_path, **meta)
    inspections: list[httpx.Request] = []

    def inspection_client(*, trust_env: bool) -> httpx.Client:
        assert trust_env is False

        def handler(request: httpx.Request) -> httpx.Response:
            inspections.append(request)
            assert request.url.path == "/v26.0/debug_token"
            return httpx.Response(200, json=_debug_payload())

        return _REAL_HTTP_CLIENT(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(inspection.httpx, "Client", inspection_client)
    monkeypatch.setattr(inspection, "_now", lambda: _NOW)
    monkeypatch.setattr(inspection, "_monotonic", lambda: 10.0)
    monkeypatch.setattr(shared_session, "_now", lambda: _NOW)
    monkeypatch.setattr(
        shared_session,
        "_meta_http_client",
        lambda: _REAL_HTTP_CLIENT(transport=httpx.MockTransport(fake.handler), trust_env=False),
    )
    monkeypatch.setattr(execution, "export_key", lambda: _SHARING_KEY)
    monkeypatch.setattr(adapter_module, "export_key", lambda: _SHARING_KEY)
    if store_creative:
        store_image(tmp_path, _IMAGE, _IMAGE_DIGEST)
    source = brief if brief is not None else _brief()
    record_digest = ""
    if retain:
        record_digest = retain_candidate(Ledger(tmp_path), source)["record_digest"]
    return _Prepared(tmp_path, fake, record_digest, source, inspections)


def _selection(prepared: _Prepared, **overrides: Any) -> MetaAdsSingleImageHierarchyCreateSelection:
    values: dict[str, Any] = {
        "candidate_record_digest": prepared.candidate_record_digest,
        "fresh": prepared.brief,
        "creative_vault_ref": f"vault:sha256:{_IMAGE_DIGEST}",
    }
    values.update(overrides)
    return MetaAdsSingleImageHierarchyCreateSelection(**values)


def _request(prepared: _Prepared, **overrides: Any) -> ExecutionPlanRequest:
    values: dict[str, Any] = {
        "schema_version": 1,
        "profile_id": _PROFILE_ID,
        "provider": _PROVIDER,
        "operation": "CREATE",
        "account_ref": _ACCOUNT_REF,
        "resource_ref": f"meta_ads.candidate.{prepared.candidate_record_digest}",
        "meta_selection": _selection(prepared),
    }
    values.update(overrides)
    return ExecutionPlanRequest(**values)


def _plan(prepared: _Prepared, **overrides: Any) -> RetainedExecutionPlan:
    configuration = load_configuration(prepared.directory)
    return META_CREATE_EXECUTION_PLANNER.plan_configured_execution(
        prepared.directory,
        _request(prepared, **overrides),
        expected_configuration_digest=digest(configuration),
    )


def _execute(
    prepared: _Prepared, retained: RetainedExecutionPlan, monkeypatch: pytest.MonkeyPatch
) -> Any:
    _install(monkeypatch, _FakeCompositeBridge())
    return execute_retained_execution_plan_with_installed_approval(
        prepared.directory,
        ExecutionPlanExecutionRequest(plan_record_digest=retained.record_digest),
    )


def _records(directory: Path, kind: str) -> list[dict[str, Any]]:
    return [row for row in Ledger(directory).records("changes") if row["record_kind"] == kind]


def _events(directory: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        row
        for row in _records(directory, "CHANGE_EVENT")
        if row["payload"].get("event_type") == event_type
    ]


# --- registration -----------------------------------------------------------


def test_meta_create_is_a_registered_pair_on_both_registries() -> None:
    assert configured_execution_planner(_PROVIDER, OperationKind.CREATE) is (
        META_CREATE_EXECUTION_PLANNER
    )
    assert configured_execution_adapter(_PROVIDER, OperationKind.CREATE) is (
        META_CREATE_EXECUTION_ADAPTER_FACTORY
    )
    assert (_PROVIDER, OperationKind.CREATE) in dict(CONFIGURED_EXECUTION_ADAPTERS)
    assert (_PROVIDER, OperationKind.CREATE) in dict(CONFIGURED_EXECUTION_PLANNERS)
    assert {"provider": _PROVIDER, "operation": "CREATE"} in executable_execution_operations()


def test_the_factory_declares_journaled_child_egress_without_reading_anything() -> None:
    """This route is one upload plus one request per created entity."""

    assert META_CREATE_EXECUTION_ADAPTER_FACTORY.declared_capabilities() == (
        ExecutionAdapterCapabilities(child_egress=True)
    )


def test_registering_a_second_meta_create_adapter_is_refused() -> None:
    with pytest.raises(AdhelmError) as refused:
        register_execution_adapter(
            _PROVIDER, OperationKind.CREATE, META_CREATE_EXECUTION_ADAPTER_FACTORY
        )
    assert refused.value.code == "EXECUTION_ADAPTER_ALREADY_REGISTERED"


def test_repeated_registration_of_the_exact_singletons_is_a_no_operation() -> None:
    execution.register_meta_create_execution()
    assert configured_execution_adapter(_PROVIDER, OperationKind.CREATE) is (
        META_CREATE_EXECUTION_ADAPTER_FACTORY
    )


# --- the ordered child plan -------------------------------------------------


def test_the_child_plan_is_image_campaign_ad_set_creative_then_ad() -> None:
    children = create_child_plan(_SPEC)

    assert [child.label for child in children] == [
        "upload-image",
        "create-campaign",
        "create-ad-set",
        "create-creative",
        "create-ad",
    ]
    assert [child.index for child in children] == [1, 2, 3, 4, 5]
    assert [child.edge for child in children] == [
        "/adimages",
        "/campaigns",
        "/adsets",
        "/adcreatives",
        "/ads",
    ]


# --- the happy path ---------------------------------------------------------


def test_full_happy_path_applies_verified_with_the_golden_request_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    assert retained.record.expected_child_egress_count == 5

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert result.results[0].provider == _PROVIDER
    assert prepared.meta.posts == 5
    assert json.loads(_GOLDEN.read_text()) == prepared.meta.requests

    starts = _records(tmp_path, "CHILD_APPLY_STARTED")
    results = _records(tmp_path, "CHILD_APPLY_RESULT")
    assert [row["payload"]["child_index"] for row in starts] == [1, 2, 3, 4, 5]
    assert [row["payload"]["child_label"] for row in starts] == [
        "upload-image",
        "create-campaign",
        "create-ad-set",
        "create-creative",
        "create-ad",
    ]
    assert {row["payload"]["effect_status"] for row in results} == {"APPLIED_VERIFIED"}
    # One durable start exists per POST, before that POST.
    assert prepared.meta.child_starts_at_post == [1, 2, 3, 4, 5]


def test_the_retained_readback_records_only_pseudonyms_and_the_proven_association(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    _execute(prepared, retained, monkeypatch)

    rows = _records(tmp_path, META_CREATE_READBACK_RECORD_KIND)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["observed_hierarchy_relation"] == "MATCHES_PREDICTED_SEMANTICS"
    assert payload["image_association"] == "PROVEN_ACCOUNT_IMAGE_AND_CREATIVE_ASSOCIATION"
    assert payload["creative_association"] == "PROVEN_SAME_ATTEMPT_CREATIVE"
    assert payload["uploaded_image_readback"] == "AVAILABLE_ACCOUNT_ADIMAGES_HASHES_READ"
    assert payload["provider_image_bytes"] == ("PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_CONTENT_READ")
    assert payload["creative_content_digest"] == _IMAGE_DIGEST
    assert payload["creative_byte_length"] == len(_IMAGE)
    assert payload["campaign_resource_ref"] == pseudonymized_resource_ref("campaign", _CAMPAIGN_ID)
    assert payload["ad_set_resource_ref"] == pseudonymized_resource_ref("ad_set", _ADSET_ID)
    assert payload["creative_resource_ref"] == pseudonymized_resource_ref("creative", _CREATIVE_ID)
    assert payload["ad_resource_ref"] == pseudonymized_resource_ref("ad", _AD_ID)
    assert payload["image_resource_ref"] == pseudonymized_resource_ref("image", _IMAGE_HASH)
    assert payload["observed_member_count"] == 4
    assert payload["observed_campaign_effective_status"] == "PAUSED"


def test_no_token_image_bytes_or_native_identifier_reaches_the_shared_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result, the review, the child journal and the readback stay redacted.

    The retained provider-specific plan record deliberately holds the compiled
    candidate -- the adapter builds its request bodies from it and nothing else
    could -- exactly as the OpenAI and Google CREATE plan records hold theirs.
    Every surface a reviewer, an agent, or an export sees is checked here.
    """

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    result = _execute(prepared, retained, monkeypatch)

    shared = json.dumps(
        [
            repr(result),
            list(retained.record.review_lines),
            retained.record.model_dump(mode="json"),
            [
                row
                for row in Ledger(tmp_path).records("changes")
                if row["record_kind"]
                in {
                    META_CREATE_READBACK_RECORD_KIND,
                    "CHILD_APPLY_STARTED",
                    "CHILD_APPLY_RESULT",
                    "CHANGE_EVENT",
                }
            ],
        ],
        default=str,
    )
    for forbidden in (
        _SUBJECT,
        _INSPECTOR,
        _IMAGE_HASH,
        _CAMPAIGN_ID,
        _ADSET_ID,
        _CREATIVE_ID,
        _AD_ID,
        _PAGE_ID,
        _IMAGE.hex(),
        "Plan work faster",
        "https://example.test/demo",
    ):
        assert forbidden not in shared
    # The retained provider plan is the one place the compiled candidate lives.
    plan_record = _records(tmp_path, META_CREATE_PLAN_RECORD_KIND)[0]
    rendered_plan = json.dumps(plan_record, default=str)
    assert _SUBJECT not in rendered_plan
    assert _INSPECTOR not in rendered_plan
    assert _IMAGE.hex() not in rendered_plan
    assert _IMAGE_HASH not in rendered_plan
    assert _IMAGE_DIGEST in rendered_plan


def test_the_plan_review_states_the_image_association_and_retry_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)

    lines = retained.record.review_lines
    assert retained.record.review_source == "COMPILER_CANDIDATE_REVIEW"
    assert any("AVAILABLE_ACCOUNT_ADIMAGES_HASHES_READ" in line for line in lines)
    assert any("ACCOUNT_IMAGE_HASH_AND_CREATIVE_READBACK" in line for line in lines)
    assert any("PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_CONTENT_READ" in line for line in lines)
    assert any("child request count: 5" in line for line in lines)
    assert any("initial state: PAUSED_AT_EVERY_PAUSABLE_LEVEL" in line for line in lines)
    assert any("write retry: NEVER_NO_DOCUMENTED_IDEMPOTENCY" in line for line in lines)
    assert any("spend bound source: CAMPAIGN_LIFETIME_BUDGET_MINOR_UNITS" in line for line in lines)


def test_the_spend_bound_is_the_campaign_lifetime_budget_in_minor_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)

    subplan = retained.record.plan.subplans[0]
    assert subplan.max_spend.minor_units == 25_000
    assert subplan.max_spend.currency == "USD"
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    assert action.postcondition.spend_bound_source == "CAMPAIGN_LIFETIME_BUDGET_MINOR_UNITS"
    assert action.postcondition.daily_budget_declared is False


def test_planning_is_deterministic_for_the_same_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    first = _plan(prepared)
    second = _plan(prepared, supersedes_record_digest=first.record_digest)

    action_one = _retained_action_for_plan(Ledger(tmp_path), first.record.plan)
    action_two = _retained_action_for_plan(Ledger(tmp_path), second.record.plan)
    assert action_one.postcondition == action_two.postcondition
    assert action_one.candidate_binding == action_two.candidate_binding
    assert action_one.observation_binding.creative == action_two.observation_binding.creative
    assert first.record.plan.subplans[0].after == second.record.plan.subplans[0].after
    assert first.record.plan.subplans[0].before == second.record.plan.subplans[0].before


# --- failure between children ----------------------------------------------


def test_a_lost_child_leaves_the_earlier_children_retained_and_the_subplan_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Child 3 dies after children 1 and 2 landed; nothing is deleted."""

    prepared = _prepare(tmp_path, monkeypatch, fail_at=3)
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert prepared.meta.posts == 3
    starts = _records(tmp_path, "CHILD_APPLY_STARTED")
    results = _records(tmp_path, "CHILD_APPLY_RESULT")
    assert [row["payload"]["child_index"] for row in starts] == [1, 2, 3]
    assert [row["payload"]["effect_status"] for row in results] == [
        "APPLIED_VERIFIED",
        "APPLIED_VERIFIED",
        "AMBIGUOUS_REMOTE_STATE",
    ]
    readback = _records(tmp_path, META_CREATE_READBACK_RECORD_KIND)[0]["payload"]
    assert readback["attempt_completion"] == "CHILDREN_INCOMPLETE"
    assert readback["observed_hierarchy_relation"] == "PARTIAL_HIERARCHY_OBSERVED"
    assert readback["recovery_disposition"] == "RETAIN_REFS_LEAVE_PAUSED_NO_DELETE"
    assert readback["acknowledged_child_labels"] == ["upload-image", "create-campaign"]
    assert readback["observed_member_count"] == 1
    # No delete, remove, or archive path exists at all.
    assert not any(row["method"] in {"DELETE", "PATCH", "PUT"} for row in prepared.meta.requests)


def test_a_crash_between_children_blocks_every_later_write_and_the_catalog_lists_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CHILD_APPLY_RESULT that never lands leaves that child unresolved.

    ``kernel-generalization.md`` records this exactly: "A ``CHILD_APPLY_RESULT``
    append that itself fails leaves the child unresolved, which is the safe
    state."  That is the same durable shape a process death between two children
    leaves behind, and it is the one this suite can produce hermetically.
    """

    from adhelm.safety import SafetyKernel

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    original = SafetyKernel._append_child_result

    def patched(self: Any, **kwargs: Any) -> None:
        if kwargs.get("child_index") == 3:
            raise OSError("synthetic interruption before the child result was journaled")
        original(self, **kwargs)

    monkeypatch.setattr(SafetyKernel, "_append_child_result", patched)
    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    ledger = Ledger(tmp_path)
    unresolved = ledger.unresolved_child_starts()
    assert len(unresolved) == 1
    assert unresolved[0]["payload"]["child_index"] == 3
    catalog = unresolved_attempt_catalog(ledger)
    rows = catalog["attempts"]
    assert any(row["refusal_code"] == "UNRESOLVED_CHILD_APPLY" for row in rows)
    assert all(row["recoverable"] is False for row in rows if row["level"] == "CHILD")

    # The barrier is global: a second plan cannot even be compiled.
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "RECOVERY_REQUIRED"


def test_an_ambiguous_upload_stops_before_any_further_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, fail_at=1)
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert prepared.meta.posts == 1
    assert [row["payload"]["child_index"] for row in _records(tmp_path, "CHILD_APPLY_STARTED")] == [
        1
    ]
    readback = _records(tmp_path, META_CREATE_READBACK_RECORD_KIND)[0]["payload"]
    # Nothing was acknowledged, so there was nothing to read: no read was made.
    assert readback["observed_hierarchy_relation"] == "READBACK_NOT_ATTEMPTED"
    assert readback["image_association"] == "NOT_PROVEN"
    assert readback["creative_association"] == "NOT_PROVEN"
    assert readback["acknowledged_child_count"] == 0


@pytest.mark.parametrize(
    ("child", "relation", "members"),
    [
        # The upload's response was lost: no identifier, so no read is made.
        (1, "READBACK_NOT_ATTEMPTED", 0),
        # The campaign's response was lost: the name probe lists a campaign with
        # the planned name that this attempt cannot attribute to itself.
        (2, "UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED", 0),
        (3, "PARTIAL_HIERARCHY_OBSERVED", 1),
        (4, "PARTIAL_HIERARCHY_OBSERVED", 2),
        (5, "PARTIAL_HIERARCHY_OBSERVED", 3),
    ],
)
def test_an_acknowledgement_with_an_extra_field_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: int, relation: str, members: int
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, extra_ack_field=child)
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert prepared.meta.posts == child
    results = _records(tmp_path, "CHILD_APPLY_RESULT")
    assert results[-1]["payload"]["effect_status"] == "AMBIGUOUS_REMOTE_STATE"
    readback = _only_readback(tmp_path)
    assert readback["acknowledged_child_count"] == child - 1
    assert readback["observed_hierarchy_relation"] == relation
    assert readback["observed_member_count"] == members
    # Whatever the relation, the record never says more than it observed.
    assert readback["attempt_completion"] == "CHILDREN_INCOMPLETE"
    if members == 0:
        assert readback["campaign_resource_ref"] is None
        assert readback["every_pausable_entity_configured_status"] == "NOT_OBSERVED"


def test_a_transport_loss_before_the_campaign_exists_finds_no_campaign_with_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed name probe that lists nothing is the only "nothing observed"."""

    prepared = _prepare(tmp_path, monkeypatch, fail_at=2)
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    readback = _only_readback(tmp_path)
    assert readback["acknowledged_child_labels"] == ["upload-image"]
    assert readback["observed_hierarchy_relation"] == "NO_HIERARCHY_OBSERVED"
    assert readback["observed_member_count"] == 0


def test_a_created_acknowledgement_carrying_success_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reference's return-type struct names ``success``; this build refuses it.

    Accepting a second member would make the acknowledgement subset wider than
    the one this build reviewed, so a ``{"id": ..., "success": true}`` response
    is ambiguous rather than assumed equivalent.
    """

    with pytest.raises(ValueError):
        adapter_module.created_acknowledgement(
            httpx.Response(200, json={"id": _CAMPAIGN_ID, "success": True})
        )
    assert (
        adapter_module.created_acknowledgement(httpx.Response(200, json={"id": _CAMPAIGN_ID}))
        == _CAMPAIGN_ID
    )


def test_an_upload_acknowledgement_must_name_the_exact_neutral_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {"images": {"customer-photo.png": {"hash": _IMAGE_HASH}}}
    with pytest.raises(ValueError):
        adapter_module.upload_acknowledgement(
            httpx.Response(200, json=body), filename="creative.png"
        )
    assert (
        adapter_module.upload_acknowledgement(
            httpx.Response(200, json={"images": {"creative.png": {"hash": _IMAGE_HASH}}}),
            filename="creative.png",
        )
        == _IMAGE_HASH
    )


# --- a provider that reads back something other than what was asked --------


def _only_readback(directory: Path) -> dict[str, Any]:
    rows = _records(directory, META_CREATE_READBACK_RECORD_KIND)
    assert len(rows) == 1
    payload: dict[str, Any] = rows[0]["payload"]
    return payload


@pytest.mark.parametrize(
    "overrides",
    [
        {"campaign": {"configured_status": "ACTIVE", "effective_status": "ACTIVE"}},
        {"adset": {"configured_status": "ACTIVE", "effective_status": "ACTIVE"}},
        {"ad": {"configured_status": "ACTIVE", "effective_status": "ACTIVE"}},
    ],
    ids=["campaign", "ad-set", "ad"],
)
def test_an_entity_the_provider_reads_back_active_is_never_verified_or_recorded_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, dict[str, Any]]
) -> None:
    """Rule 8: paused creation is a claim only the readback can support.

    The provider acknowledged all five children, so the attempt is complete, but
    one entity reads back ``ACTIVE``.  That must never verify, and the retained
    evidence must not say the hierarchy is paused.
    """

    prepared = _prepare(tmp_path, monkeypatch, readback_overrides=overrides)
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert prepared.meta.posts == 5
    readback = _only_readback(tmp_path)
    assert readback["attempt_completion"] == "CHILDREN_COMPLETE"
    assert readback["observed_hierarchy_relation"] == "DIVERGENT_HIERARCHY_OBSERVED"
    assert readback["observed_member_count"] == 4
    assert readback["every_pausable_entity_configured_status"] == "NOT_ALL_PAUSED"
    assert not any(row["method"] in {"DELETE", "PATCH", "PUT"} for row in prepared.meta.requests)


@pytest.mark.parametrize(
    ("overrides", "campaign_field"),
    [
        ({"campaign": {"lifetime_budget": "2500000"}}, "lifetime budget"),
        ({"campaign": {"stop_time": "2031-01-01T00:00:00+0000"}}, "schedule"),
        ({"adset": {"optimization_goal": "REACH"}}, "optimization goal"),
        ({"adset": {"billing_event": "LINK_CLICKS"}}, "billing event"),
        ({"adset": {"bid_strategy": "COST_CAP"}}, "bid strategy"),
        ({"campaign": {"objective": "OUTCOME_SALES"}}, "objective"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_hierarchy_that_reads_back_with_other_configuration_is_not_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, dict[str, Any]],
    campaign_field: str,
) -> None:
    """Spend-bearing configuration that moved after the create is drift, not success."""

    prepared = _prepare(tmp_path, monkeypatch, readback_overrides=overrides)
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE, campaign_field
    assert prepared.meta.posts == 5
    readback = _only_readback(tmp_path)
    assert readback["observed_hierarchy_relation"] in {
        "DIVERGENT_HIERARCHY_OBSERVED",
        "READBACK_UNAVAILABLE",
    }
    assert readback["conditional_write_proof"] is False


def test_an_image_the_account_does_not_hold_leaves_the_association_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, hold_images=False)
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    readback = _only_readback(tmp_path)
    assert readback["image_association"] == "NOT_PROVEN"
    assert readback["image_resource_ref"] is None
    assert readback["observed_hierarchy_relation"] == "DIVERGENT_HIERARCHY_OBSERVED"


def test_a_creative_that_references_another_image_leaves_the_association_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(
        tmp_path,
        monkeypatch,
        readback_overrides={"creative": {"image_hash": "0" * 32}},
    )
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    readback = _only_readback(tmp_path)
    assert readback["image_association"] == "NOT_PROVEN"
    assert readback["image_resource_ref"] is None
    assert pseudonymized_resource_ref("image", "0" * 32) not in json.dumps(readback)


def test_an_ad_that_references_another_creative_leaves_the_association_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(
        tmp_path,
        monkeypatch,
        readback_overrides={"ad": {"creative": {"id": "700000000000777"}}},
    )
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    readback = _only_readback(tmp_path)
    assert readback["creative_association"] == "NOT_PROVEN"
    assert readback["observed_hierarchy_relation"] == "DIVERGENT_HIERARCHY_OBSERVED"


@pytest.mark.parametrize(
    "overrides",
    [
        {"adset": {"targeting": {"geo_locations": {"countries": ["US"]}, "age_min": 18}}},
        {"campaign": {"special_ad_categories": ["HOUSING"]}},
        {"ad": {"unrequested_field": "surprise"}},
    ],
    ids=["audience-member", "special-category", "unknown-field"],
)
def test_a_readback_outside_the_allowlist_is_recorded_unavailable_not_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, dict[str, Any]]
) -> None:
    """Five acknowledged children and an unreadable hierarchy is not "nothing exists"."""

    prepared = _prepare(tmp_path, monkeypatch, readback_overrides=overrides)
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    readback = _only_readback(tmp_path)
    assert readback["acknowledged_child_count"] == 5
    assert readback["observed_hierarchy_relation"] == "READBACK_UNAVAILABLE"
    assert readback["every_pausable_entity_configured_status"] == "NOT_OBSERVED"
    assert readback["campaign_resource_ref"] is None


def test_a_rival_campaign_with_the_planned_name_is_recorded_as_unattributable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the create, exactly this attempt's campaign may hold the planned name."""

    prepared = _prepare(tmp_path, monkeypatch, competing_campaign=True)
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    readback = _only_readback(tmp_path)
    assert readback["acknowledged_child_count"] == 5
    assert readback["observed_hierarchy_relation"] == "UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED"
    assert readback["observed_member_count"] == 0
    assert pseudonymized_resource_ref("campaign", _RIVAL_CAMPAIGN_ID) not in json.dumps(readback)


@pytest.mark.parametrize(
    ("fail_at", "labels"),
    [
        (
            None,
            ["upload-image", "create-campaign", "create-ad-set", "create-creative", "create-ad"],
        ),
        (3, ["upload-image", "create-campaign"]),
    ],
    ids=["all-children-acknowledged", "fail-at-the-ad-set"],
)
def test_an_acknowledged_campaign_the_listing_omits_is_not_called_unattributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: int | None, labels: list[str]
) -> None:
    """A list that lags the create lists no campaign with the name: say only that.

    The campaign identifier was acknowledged, so this is not the lost-response
    case, and nothing carrying the planned name was listed, so it is not an
    unattributable name either.
    """

    prepared = _prepare(tmp_path, monkeypatch, fail_at=fail_at, list_omits_created_campaign=True)
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    readback = _only_readback(tmp_path)
    assert readback["acknowledged_child_labels"] == labels
    assert readback["observed_hierarchy_relation"] == "ACKNOWLEDGED_CAMPAIGN_NOT_LISTED"
    assert readback["observed_member_count"] == 0
    assert readback["campaign_resource_ref"] is None
    assert readback["every_pausable_entity_configured_status"] == "NOT_OBSERVED"


@pytest.mark.parametrize(
    "listed_name",
    [_CAMPAIGN_NAME.upper(), _CAMPAIGN_NAME],
    ids=["a-near-variant", "a-rival-with-the-exact-name"],
)
def test_an_unlisted_acknowledged_campaign_beside_a_listed_name_is_unattributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listed_name: str
) -> None:
    """The listing omits this attempt's campaign yet lists something carrying the name.

    "Not listed" would misstate that: the probe did list a campaign with the
    planned name or a near variant, which this attempt cannot attribute.
    """

    prepared = _prepare(
        tmp_path,
        monkeypatch,
        list_omits_created_campaign=True,
        listed_after_create=(listed_name,),
    )
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    readback = _only_readback(tmp_path)
    assert "create-campaign" in readback["acknowledged_child_labels"]
    assert readback["observed_hierarchy_relation"] == "UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED"
    assert readback["observed_member_count"] == 0
    assert readback["campaign_resource_ref"] is None
    stray_id = str(_RIVAL_CAMPAIGN_ID_BASE)
    assert stray_id not in json.dumps(readback)
    assert pseudonymized_resource_ref("campaign", stray_id) not in json.dumps(readback)


def test_a_refusal_between_children_still_retains_the_partial_hierarchy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A continuity or expiry refusal before child 3's permit must not lose children 1 and 2.

    Nothing was sent for child 3, but the campaign and the image exist, so the
    attempt still owes the partial readback that names them.
    """

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    original = edit_session._FixedMetaAdsHierarchyCreateEgress._preflight
    calls = {"count": 0}

    def refusing(self: Any) -> None:
        calls["count"] += 1
        if calls["count"] == 3:
            raise AdhelmError("PLAN_EXPIRED", "synthetic expiry between children")
        original(self)

    monkeypatch.setattr(edit_session._FixedMetaAdsHierarchyCreateEgress, "_preflight", refusing)
    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert prepared.meta.posts == 2
    readback = _only_readback(tmp_path)
    assert readback["attempt_completion"] == "CHILDREN_INCOMPLETE"
    assert readback["acknowledged_child_labels"] == ["upload-image", "create-campaign"]
    assert readback["observed_hierarchy_relation"] == "PARTIAL_HIERARCHY_OBSERVED"
    assert [row["payload"]["child_index"] for row in _records(tmp_path, "CHILD_APPLY_STARTED")] == [
        1,
        2,
    ]


def test_a_refusal_before_the_first_child_creates_nothing_and_retains_no_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)

    def refusing(self: Any) -> None:
        raise AdhelmError("PLAN_EXPIRED", "synthetic expiry before the first child")

    monkeypatch.setattr(edit_session._FixedMetaAdsHierarchyCreateEgress, "_preflight", refusing)
    result = _execute(prepared, retained, monkeypatch)

    # This refusal happens inside the kernel's apply, after the durable
    # ``APPLY_STARTED``.  The kernel turns every adapter exception raised there
    # into an ambiguous outcome and keeps no exception code, so the result
    # carries no diagnostic and requires recovery even though nothing was sent.
    # That is why every check that needs no provider read refuses earlier, at
    # factory admission, with its own code (see the tests for those codes).
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert result.results[0].diagnostic_code is None
    assert result.recovery_required is True
    assert prepared.meta.posts == 0
    assert _records(tmp_path, "CHILD_APPLY_STARTED") == []
    assert _records(tmp_path, META_CREATE_READBACK_RECORD_KIND) == []


def test_a_vault_object_that_changed_after_planning_uploads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter repeats the planner's vault check against the retained binding."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    (tmp_path / "assets" / "sha256" / _IMAGE_DIGEST).write_bytes(_png("#654321"))
    inspections = len(prepared.inspections)

    # The vault's own rehash refuses at factory admission, under its own code,
    # before the EDIT credential session opens and before any durable start exists.
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)

    assert refused.value.code == "ASSET_CORRUPT"
    assert prepared.meta.posts == 0
    assert len(prepared.inspections) == inspections
    assert _records(tmp_path, "CHILD_APPLY_STARTED") == []
    assert _events(tmp_path, "APPLY_STARTED") == []
    assert Ledger(tmp_path).unresolved_starts() == []


# --- drift between planning and execution ----------------------------------


@pytest.mark.parametrize(
    ("drift", "code"),
    [
        ({"existing_campaign_names": (_CAMPAIGN_NAME,)}, "REMOTE_DRIFT"),
        ({"existing_campaign_names": (_CAMPAIGN_NAME.lower(),)}, "REMOTE_DRIFT"),
        ({"account_status": 2}, "META_TOKEN_EDIT_BINDING_DRIFT"),
        ({"currency": "EUR"}, "IDENTITY_MISMATCH"),
    ],
    ids=["same-name", "near-name", "account-disabled", "currency-changed"],
)
def test_remote_drift_after_planning_refuses_before_the_first_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: dict[str, Any], code: str
) -> None:
    """Rule 6: re-read, reject drift, then apply.  No child starts on a drifted account."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    for name, value in drift.items():
        setattr(prepared.meta, name, value)

    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)

    assert refused.value.code == code
    assert prepared.meta.posts == 0
    assert _records(tmp_path, "CHILD_APPLY_STARTED") == []
    assert _records(tmp_path, META_CREATE_READBACK_RECORD_KIND) == []


# --- the bounded, paged campaign-name read ---------------------------------


def test_a_collision_on_a_later_page_of_the_name_list_is_still_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial page would turn "no campaign has this name" into an assumption."""

    names = ("Alpha", "Beta", "Gamma", "Delta", _CAMPAIGN_NAME)
    prepared = _prepare(tmp_path, monkeypatch, existing_campaign_names=names, campaign_page_size=2)

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "META_ADS_CREATE_NAME_COLLISION"
    assert [path for path in prepared.meta.reads if path.endswith("/campaigns")] == [
        f"/v26.0/{_ACCOUNT_CLIENT_REF}/campaigns"
    ] * 3
    assert prepared.meta.posts == 0


def test_a_paged_name_list_without_a_collision_plans_and_creates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(
        tmp_path,
        monkeypatch,
        existing_campaign_names=("Alpha", "Beta", "Gamma", "Delta", "Epsilon"),
        campaign_page_size=2,
    )

    result = _execute(prepared, _plan(prepared), monkeypatch)

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED


@pytest.mark.parametrize("fault", ["loop", "foreign_next"])
def test_a_misbehaving_name_list_cursor_refuses_before_any_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    prepared = _prepare(
        tmp_path,
        monkeypatch,
        existing_campaign_names=("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"),
        campaign_page_size=2,
        campaign_paging_fault=fault,
    )

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "PROVIDER_RESPONSE_INVALID"
    assert prepared.meta.posts == 0
    assert _records(tmp_path, META_CREATE_PLAN_RECORD_KIND) == []


def test_an_account_with_more_campaigns_than_the_bounded_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = tuple(f"Existing campaign {index}" for index in range(5_001))
    prepared = _prepare(
        tmp_path, monkeypatch, existing_campaign_names=names, campaign_page_size=500
    )

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code in {"PAGINATION_LIMIT", "RESULT_LIMIT_REQUIRED"}
    assert prepared.meta.posts == 0


# --- preconditions before egress -------------------------------------------


def test_a_feature_gated_brief_is_refused_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compiler's own types exclude these, so the gate is restated, not assumed."""

    prepared = _prepare(tmp_path, monkeypatch, retain=False)
    prepared.candidate_record_digest = "0" * 64
    gated = prepared.brief.model_copy(update={"special_ad_category": "HOUSING"})

    with pytest.raises(AdhelmError) as refused:
        META_CREATE_EXECUTION_PLANNER.plan_configured_execution(
            prepared.directory,
            _request(prepared, meta_selection=_selection(prepared, fresh=gated)),
            expected_configuration_digest=digest(load_configuration(prepared.directory)),
        )

    assert refused.value.code == "META_ADS_CREATE_OPTION_NOT_ENABLED"
    assert "NOT_ENABLED" in refused.value.message
    assert prepared.meta.requests == []
    assert prepared.inspections == []


def test_a_feature_gated_compiled_option_is_refused_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    altered = _SPEC.model_copy(
        update={
            "adset_request": _SPEC.adset_request.model_copy(update={"optimization_goal": "REACH"})
        }
    )
    monkeypatch.setattr(execution, "compile_meta_ads_campaign", lambda _brief: altered)

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "META_ADS_CREATE_OPTION_NOT_ENABLED"
    assert prepared.meta.requests == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"existing_campaign_names": (_CAMPAIGN_NAME,)}, "META_ADS_CREATE_NAME_COLLISION"),
        (
            {"existing_campaign_names": (_CAMPAIGN_NAME.upper(),)},
            "META_ADS_CREATE_NAME_COLLISION_NEAR_MATCH",
        ),
        ({"account_status": 2}, "META_ADS_ACCOUNT_NOT_ACTIVE"),
        # The EDIT session proves the observed account against the *declared*
        # profile before it yields, so an account whose calendar moved is
        # refused one boundary earlier than this route's own check.
        ({"timezone": "America/Chicago"}, "IDENTITY_MISMATCH"),
    ],
)
def test_the_bounded_read_refuses_before_any_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], code: str
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, **overrides)

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == code
    assert prepared.meta.posts == 0
    assert _records(tmp_path, META_CREATE_PLAN_RECORD_KIND) == []


def test_a_candidate_compiled_for_another_account_calendar_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observed account, the profile and the candidate must agree exactly."""

    brief = _brief(account_timezone="America/Chicago")
    prepared = _prepare(tmp_path, monkeypatch, brief=brief)

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "META_ADS_CREATE_ACCOUNT_MISMATCH"
    assert prepared.meta.posts == 0
    assert _records(tmp_path, META_CREATE_PLAN_RECORD_KIND) == []


def test_the_near_match_set_is_a_closed_deterministic_local_enumeration() -> None:
    near = near_match_campaign_names("September launch")

    assert "September launch" not in near
    assert "SEPTEMBER LAUNCH" in near
    assert "september launch" in near
    assert " September launch" in near
    assert near == tuple(sorted(set(near)))
    # A merely mixed-case variant is deliberately *not* claimed to be caught.
    assert "sEpTeMbEr launch" not in near


def test_a_missing_vault_object_refuses_before_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, store_creative=False)

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "ASSET_NOT_AVAILABLE"
    assert prepared.meta.requests == []


def test_a_vault_reference_the_brief_does_not_declare_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)

    with pytest.raises(AdhelmError) as refused:
        _plan(
            prepared,
            meta_selection=_selection(prepared, creative_vault_ref="vault:sha256:" + "b" * 64),
        )

    assert refused.value.code == "META_ADS_CREATE_CREATIVE_NOT_DECLARED"
    assert prepared.meta.requests == []


def test_a_candidate_digest_mismatch_refuses_before_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    other = _brief(campaign_name="Another launch")

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, meta_selection=_selection(prepared, fresh=other))

    assert refused.value.code == "META_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH"
    assert prepared.meta.requests == []


def test_a_compiled_name_the_fixed_form_body_cannot_carry_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared transport's fixed mutation form is printable ASCII only."""

    brief = _brief(campaign_name="Septèmbre lancement")
    prepared = _prepare(tmp_path, monkeypatch, brief=brief)

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "META_ADS_CREATE_REQUEST_NOT_EXPRESSIBLE"
    assert "campaign name" in str(refused.value.next_action)
    assert prepared.meta.requests == []


def test_ad_copy_that_expands_beyond_the_form_bound_is_refused_before_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-ASCII copy is sent as escaped JSON, which can outgrow one form value.

    Each ``\\uXXXX`` escape is six characters, so a maximum-length message and
    headline of non-ASCII text exceed the fixed form value bound.  That has to be
    a planning refusal; after the permit it would read as an ambiguous outcome for
    a request that was never sent.
    """

    primary_text = "\u00e9" * 500
    headline = "\u00e9" * 255
    destination_url = "https://example.test/demo?utm_source=meta"
    creative = _brief().creative.model_copy(
        update={
            "primary_text": primary_text,
            "headline": headline,
            "association": _association(primary_text, headline, destination_url),
        }
    )
    prepared = _prepare(tmp_path, monkeypatch, brief=_brief(creative=creative))

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "META_ADS_CREATE_REQUEST_NOT_EXPRESSIBLE"
    assert "ad creative copy" in str(refused.value.next_action)
    assert prepared.meta.requests == []
    assert prepared.inspections == []
    assert _records(tmp_path, META_CREATE_PLAN_RECORD_KIND) == []


def test_ordinary_non_ascii_copy_is_escaped_and_still_planned_and_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary_text = "Bring tasks and documents together \u2014 faster."
    headline = "Plan work faster"
    destination_url = "https://example.test/demo?utm_source=meta"
    creative = _brief().creative.model_copy(
        update={
            "primary_text": primary_text,
            "association": _association(primary_text, headline, destination_url),
        }
    )
    prepared = _prepare(tmp_path, monkeypatch, brief=_brief(creative=creative))

    result = _execute(prepared, _plan(prepared), monkeypatch)

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    creative_form = prepared.meta.requests[3]["form"]
    assert "\\u2014" in creative_form["object_story_spec"]
    assert creative_form["object_story_spec"].isascii()


# --- the creative form is sized for every hash an acknowledgement can carry --


@pytest.mark.parametrize(
    "hash_value",
    [
        _IMAGE_HASH + "0000",  # 36 hexadecimal characters
        "A" * 32,  # uppercase
        "g" * 32,  # not hexadecimal
        "0" * 31,
        "0" * 33,
        '"' * 32,  # a character JSON doubles
        "\\" * 32,
        "x" * 512,  # the old printable-ASCII bound
        "",
    ],
    ids=[
        "thirty-six-hex",
        "uppercase",
        "not-hex",
        "thirty-one",
        "thirty-three",
        "json-doubled-quote",
        "json-doubled-backslash",
        "old-512-printable-bound",
        "empty",
    ],
)
def test_the_image_hash_grammar_is_closed_so_planning_can_reserve_its_exact_length(
    hash_value: str,
) -> None:
    """Only 32 lowercase hexadecimal characters: nothing that can grow or escape."""

    assert checked_image_hash(META_CREATE_PLANNING_IMAGE_HASH) == META_CREATE_PLANNING_IMAGE_HASH
    assert checked_image_hash(_IMAGE_HASH) == _IMAGE_HASH
    with pytest.raises(ValueError):
        checked_image_hash(hash_value)
    with pytest.raises(ValueError):
        adapter_module.upload_acknowledgement(
            httpx.Response(200, json={"images": {"creative.png": {"hash": hash_value}}}),
            filename="creative.png",
        )


def test_a_creative_form_exactly_at_the_bound_plans_and_executes_with_the_reserved_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, brief=_boundary_brief())
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    creative_form = prepared.meta.requests[3]["form"]
    assert len(creative_form["object_story_spec"]) == META_CREATE_MAX_FORM_VALUE_LENGTH


def test_an_upload_hash_longer_than_the_reserved_one_never_reaches_the_creative_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boundary-length creative plans, and a longer hash cannot overflow it later.

    With the hash grammar open to 512 printable characters, the 36-character hash
    below made the creative form 4,098 characters after the upload, and the
    capability refused it only inside the creative child's journaled start, which
    left a partial hierarchy and an unresolved child for a request never sent.
    """

    prepared = _prepare(
        tmp_path, monkeypatch, brief=_boundary_brief(), image_hash=_IMAGE_HASH + "0000"
    )
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert prepared.meta.posts == 1
    assert [row["payload"]["child_index"] for row in _records(tmp_path, "CHILD_APPLY_STARTED")] == [
        1
    ]
    assert not any(request["path"].endswith("/adcreatives") for request in prepared.meta.requests)


def test_a_form_that_outgrows_the_bound_after_planning_never_journals_its_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: even a widened hash grammar cannot start an unsendable child.

    The grammar is widened here on purpose, so the planning reservation (32
    characters) is no longer exact.  The adapter checks every built form value
    before the child's durable start, so the residual failure is a clean refusal
    between children: the earlier children are read back, and no child start is
    left unresolved for a request that was never sent.
    """

    monkeypatch.setattr(contract_module, "_IMAGE_HASH", re.compile(r"^[a-f0-9]{1,64}$"))
    prepared = _prepare(
        tmp_path, monkeypatch, brief=_boundary_brief(), image_hash=_IMAGE_HASH + "0000"
    )
    retained = _plan(prepared)
    codes: list[str] = []
    original = adapter_module.form_value_expressible

    def spy(value: object) -> bool:
        expressible = original(value)
        if not expressible:
            codes.append("META_ADS_CREATE_REQUEST_NOT_EXPRESSIBLE")
        return expressible

    monkeypatch.setattr(adapter_module, "form_value_expressible", spy)

    result = _execute(prepared, retained, monkeypatch)

    assert codes == ["META_ADS_CREATE_REQUEST_NOT_EXPRESSIBLE"]
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert prepared.meta.posts == 3
    assert [row["payload"]["child_index"] for row in _records(tmp_path, "CHILD_APPLY_STARTED")] == [
        1,
        2,
        3,
    ]
    assert Ledger(tmp_path).unresolved_child_starts() == []
    readback = _only_readback(tmp_path)
    assert readback["acknowledged_child_labels"] == [
        "upload-image",
        "create-campaign",
        "create-ad-set",
    ]
    assert readback["observed_hierarchy_relation"] == "PARTIAL_HIERARCHY_OBSERVED"


def test_a_broken_token_continuity_refuses_before_the_adapter_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The EDIT session's two credential slots must still reproduce the binding."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    store = _MemoryStore()
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)

    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)

    assert refused.value.code in {
        "META_TOKEN_BUNDLE_REQUIRED",
        "META_TOKEN_EDIT_BINDING_DRIFT",
        "META_TOKEN_INSPECTION_UNVERIFIED",
        "PROVIDER_ACCESS_REQUIRED",
    }
    assert prepared.meta.posts == 0


def test_changed_local_configuration_refuses_before_the_adapter_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configuration that moved after planning refuses before the EDIT credential session."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    enroll_meta_token_profile(
        prepared.directory,
        _draft(profile_id="meta-create-edit-second"),
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    before = len(prepared.inspections)

    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)

    assert refused.value.code == "CONFIGURATION_DRIFT"
    assert prepared.meta.posts == 0
    assert len(prepared.inspections) == before


def test_a_missing_ledger_record_kind_refuses_before_any_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, readback_kind=False)

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "META_ADS_CREATE_RECORD_KIND_NOT_INSTALLED"
    assert prepared.meta.requests == []
    assert prepared.inspections == []


def test_a_build_without_the_upload_transport_seam_refuses_before_any_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    monkeypatch.delattr(SafeHttpClient, "upload_image", raising=False)

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "META_ADS_CREATE_UPLOAD_TRANSPORT_NOT_INSTALLED"
    assert prepared.meta.requests == []
    assert prepared.inspections == []


def test_a_read_profile_is_refused_before_any_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, purpose="READ")

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "META_TOKEN_EDIT_PROFILE_REQUIRED"
    assert prepared.meta.requests == []


def test_the_unconfigured_bridge_refuses_before_any_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    before = len(prepared.inspections)

    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            prepared.directory,
            ExecutionPlanExecutionRequest(plan_record_digest=retained.record_digest),
        )

    assert refused.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"
    assert prepared.meta.posts == 0
    assert len(prepared.inspections) == before


# --- every listed precondition, one negative test each -----------------------


def _factory_open(prepared: _Prepared, retained: RetainedExecutionPlan, clock: Any = None) -> Any:
    return META_CREATE_EXECUTION_ADAPTER_FACTORY.open_configured_adapter(
        prepared.directory,
        retained.record,
        Ledger(prepared.directory),
        clock=clock if clock is not None else (lambda: datetime.now(UTC)),
    )


def _gif() -> bytes:
    from PIL import Image

    output = BytesIO()
    Image.new("P", (8, 4), 3).save(output, format="GIF")
    return output.getvalue()


def _oversize_png() -> bytes:
    import os

    from PIL import Image

    output = BytesIO()
    Image.frombytes("RGB", (2400, 1200), os.urandom(2400 * 1200 * 3)).save(output, format="PNG")
    return output.getvalue()


def _plan_for_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image: bytes
) -> tuple[_Prepared, str]:
    image_digest = hashlib.sha256(image).hexdigest()
    prepared = _prepare(tmp_path, monkeypatch, brief=_brief_for_image(image), store_creative=False)
    store_image(tmp_path, image, image_digest)
    return prepared, image_digest


def test_a_creative_of_an_unsupported_media_type_refuses_at_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, image_digest = _plan_for_image(tmp_path, monkeypatch, _gif())

    with pytest.raises(AdhelmError) as refused:
        _plan(
            prepared,
            meta_selection=_selection(prepared, creative_vault_ref=f"vault:sha256:{image_digest}"),
        )

    assert refused.value.code == "META_ADS_CREATE_CREATIVE_MEDIA_TYPE_UNSUPPORTED"
    assert prepared.meta.requests == []
    assert prepared.inspections == []
    assert _records(tmp_path, META_CREATE_PLAN_RECORD_KIND) == []


def test_a_creative_above_the_local_upload_bound_refuses_at_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, image_digest = _plan_for_image(tmp_path, monkeypatch, _oversize_png())

    with pytest.raises(AdhelmError) as refused:
        _plan(
            prepared,
            meta_selection=_selection(prepared, creative_vault_ref=f"vault:sha256:{image_digest}"),
        )

    assert refused.value.code == "META_ADS_CREATE_CREATIVE_TOO_LARGE"
    assert prepared.meta.requests == []
    assert prepared.inspections == []
    assert _records(tmp_path, META_CREATE_PLAN_RECORD_KIND) == []


def test_a_decoded_creative_that_is_not_the_compiled_asset_refuses_at_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        execution,
        "decode_image",
        lambda data: SimpleNamespace(mime_type="image/png", width_px=9, height_px=4),
    )

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "META_ADS_CREATE_CREATIVE_DRIFT"
    assert prepared.meta.requests == []
    assert prepared.inspections == []


def test_a_vault_object_corrupt_before_planning_refuses_at_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    (tmp_path / "assets" / "sha256" / _IMAGE_DIGEST).write_bytes(_png("#654321"))

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "ASSET_CORRUPT"
    assert prepared.meta.requests == []
    assert prepared.inspections == []


def test_changed_configuration_refuses_at_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)

    with pytest.raises(AdhelmError) as refused:
        META_CREATE_EXECUTION_PLANNER.plan_configured_execution(
            prepared.directory,
            _request(prepared),
            expected_configuration_digest="0" * 64,
        )

    assert refused.value.code == "CONFIGURATION_CHANGED"
    assert prepared.meta.requests == []
    assert prepared.inspections == []


def test_an_inspected_token_window_shorter_than_the_plan_refuses_at_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan's own lifetime (300 seconds) must fit inside the token's window."""

    prepared = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(execution, "_now", lambda: _NOW + timedelta(minutes=29))

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "META_TOKEN_WINDOW_TOO_SHORT"
    assert prepared.meta.posts == 0
    assert _records(tmp_path, META_CREATE_PLAN_RECORD_KIND) == []


def test_a_changed_fingerprint_key_refuses_at_admission_before_the_edit_credential_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    inspections = len(prepared.inspections)
    monkeypatch.setattr(adapter_module, "export_key", lambda: b"another-private-key-of-32-bytes!!")

    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)

    assert refused.value.code == "META_ADS_CREATE_FINGERPRINT_KEY_CHANGED"
    assert prepared.meta.posts == 0
    assert len(prepared.inspections) == inspections
    assert _events(tmp_path, "APPLY_STARTED") == []


def test_unreproducible_semantic_states_refuse_at_admission_before_the_edit_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    inspections = len(prepared.inspections)
    original = adapter_module.predicted_states

    def altered(action: Any, *, fingerprint_key: bytes) -> tuple[StateRef, StateRef]:
        before, after = original(action, fingerprint_key=fingerprint_key)
        return before, after.model_copy(update={"fingerprint": "e" * 64})

    monkeypatch.setattr(adapter_module, "predicted_states", altered)

    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)

    assert refused.value.code == "META_ADS_CREATE_STATE_UNREPRODUCIBLE"
    assert prepared.meta.posts == 0
    assert len(prepared.inspections) == inspections
    assert _events(tmp_path, "APPLY_STARTED") == []


def test_a_vault_object_that_decodes_differently_refuses_at_admission_under_its_own_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter's drift branch, reached by a decode that disagrees with the binding."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    inspections = len(prepared.inspections)
    monkeypatch.setattr(
        adapter_module,
        "decode_image",
        lambda data: SimpleNamespace(mime_type="image/png", width_px=9, height_px=4),
    )

    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)

    assert refused.value.code == "META_ADS_CREATE_CREATIVE_DRIFT"
    assert prepared.meta.posts == 0
    assert len(prepared.inspections) == inspections
    assert _records(tmp_path, "CHILD_APPLY_STARTED") == []
    assert _events(tmp_path, "APPLY_STARTED") == []


def test_a_plan_past_its_expiry_refuses_at_the_route_on_the_real_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    inspections = len(prepared.inspections)
    monkeypatch.setattr(workflow, "_now", lambda: datetime.now(UTC) + timedelta(hours=1))

    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)

    assert refused.value.code == "PLAN_EXPIRED"
    assert prepared.meta.posts == 0
    assert len(prepared.inspections) == inspections


def test_a_plan_past_its_expiry_refuses_at_adapter_admission_before_any_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    inspections = len(prepared.inspections)

    with pytest.raises(AdhelmError) as refused:
        with _factory_open(
            prepared, retained, clock=lambda: retained.record.expires_at + timedelta(seconds=1)
        ):
            pass

    assert refused.value.code == "PLAN_EXPIRED"
    assert prepared.meta.posts == 0
    assert len(prepared.inspections) == inspections


def test_a_plan_that_expires_between_children_stops_before_the_next_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter's own expiry check runs on the injected route clock, per child."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)

    def clock() -> datetime:
        value = datetime.now(UTC)
        # Past the 300-second plan, inside the readback's 10-minute interval bound.
        return value + timedelta(minutes=6) if prepared.meta.posts >= 2 else value

    monkeypatch.setattr(workflow, "_now", clock)
    codes: list[str] = []
    original = adapter_module.MetaAdsSingleImageHierarchyCreateAdapter._check_plan_expiry

    def spy(self: Any, action: Any) -> None:
        try:
            original(self, action)
        except AdhelmError as error:
            codes.append(error.code)
            raise

    monkeypatch.setattr(
        adapter_module.MetaAdsSingleImageHierarchyCreateAdapter, "_check_plan_expiry", spy
    )

    result = _execute(prepared, retained, monkeypatch)

    assert "PLAN_EXPIRED" in codes
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert prepared.meta.posts == 2
    assert [row["payload"]["child_index"] for row in _records(tmp_path, "CHILD_APPLY_STARTED")] == [
        1,
        2,
    ]
    readback = _only_readback(tmp_path)
    assert readback["acknowledged_child_labels"] == ["upload-image", "create-campaign"]
    assert readback["observed_hierarchy_relation"] == "PARTIAL_HIERARCHY_OBSERVED"


def test_an_adapter_that_already_handled_an_action_refuses_a_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    second: list[str] = []
    adapter_class = adapter_module.MetaAdsSingleImageHierarchyCreateAdapter
    original = adapter_class.apply_children

    def twice(self: Any, plan: Any, *, idempotency_key: str, execution: Any) -> Any:
        try:
            return original(self, plan, idempotency_key=idempotency_key, execution=execution)
        finally:
            try:
                original(self, plan, idempotency_key=idempotency_key, execution=execution)
            except AdhelmError as error:
                second.append(error.code)

    monkeypatch.setattr(adapter_class, "apply_children", twice)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert second == ["META_ADS_CREATE_ADAPTER_USED"]
    assert prepared.meta.posts == 5


def test_an_unregistered_readback_kind_refuses_at_admission_before_any_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    inspections = len(prepared.inspections)
    monkeypatch.delitem(
        ledger_module._KINDS["changes"], META_CREATE_READBACK_RECORD_KIND, raising=False
    )

    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)

    assert refused.value.code == "META_ADS_CREATE_READBACK_RECORD_KIND_NOT_INSTALLED"
    assert prepared.meta.posts == 0
    assert len(prepared.inspections) == inspections


def test_a_build_that_loses_the_upload_seam_after_planning_refuses_at_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    inspections = len(prepared.inspections)
    monkeypatch.delattr(SafeHttpClient, "upload_image", raising=False)

    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)

    assert refused.value.code == "META_ADS_CREATE_UPLOAD_TRANSPORT_NOT_INSTALLED"
    assert prepared.meta.posts == 0
    assert len(prepared.inspections) == inspections


# --- the creative member proves what was read, nothing more ------------------


def test_the_creative_member_carries_the_not_read_sentinel_on_both_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The creative node read is id, name and image_hash: nothing copy-bearing is read."""

    copy_bearing = (
        "call_to_action",
        "creative_format",
        "destination_declared",
        "headline_declared",
        "page_declared",
        "primary_text_declared",
    )
    predicted = next(
        member
        for member in projection_from_compiled_spec(
            _SPEC, creative_asset_content_digest=_IMAGE_DIGEST
        ).members
        if member.member_kind == "CREATIVE"
    )
    assert dict(predicted.configuration) == dict(creative_member_configuration(_IMAGE_DIGEST))
    assert {key: dict(predicted.configuration)[key] for key in copy_bearing} == {
        key: NOT_READ_BACK_COPY_BEARING_FIELD for key in copy_bearing
    }

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    observed: list[Any] = []
    real = adapter_module.semantic_state_from_members

    def spy(members: Any, **kwargs: Any) -> Any:
        observed.append(members)
        return real(members, **kwargs)

    monkeypatch.setattr(adapter_module, "semantic_state_from_members", spy)
    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    creative_members = [
        dict(member.configuration)
        for members in observed
        for member in members
        if member.member_kind == "CREATIVE"
    ]
    assert creative_members
    for configuration in creative_members:
        assert {key: configuration[key] for key in copy_bearing} == {
            key: NOT_READ_BACK_COPY_BEARING_FIELD for key in copy_bearing
        }
        # No literal copied from the plan (a call to action, a format, a flag).
        assert "LEARN_MORE" not in configuration.values()
        assert "single_image_link" not in configuration.values()
        assert "true" not in configuration.values()
        assert configuration["creative_asset_content_digest"] == _IMAGE_DIGEST


# --- the readback history validator ----------------------------------------


def test_the_readback_history_validator_requires_its_action_and_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    _execute(prepared, retained, monkeypatch)
    records = Ledger(tmp_path).records("changes")

    validate_meta_create_readback_history(records)

    orphan = [row for row in records if row["record_kind"] != META_CREATE_PLAN_RECORD_KIND]
    with pytest.raises(AdhelmError) as refused:
        validate_meta_create_readback_history(orphan)
    assert refused.value.code == "INVALID_META_ADS_CREATE_READBACK_HISTORY"

    duplicated = [
        *records,
        [row for row in records if row["record_kind"] == META_CREATE_READBACK_RECORD_KIND][0],
    ]
    with pytest.raises(AdhelmError):
        validate_meta_create_readback_history(duplicated)


def _readback_values() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "action_id": "a" * 64,
        "plan_digest": "b" * 64,
        "subplan_digest": "c" * 64,
        "attempt_ref": "attempt",
        "account_ref": _ACCOUNT_REF,
        "observation_interval_start": now,
        "observation_interval_end": now + timedelta(seconds=1),
        "acknowledged_child_count": 5,
        "acknowledged_child_labels": (
            "upload-image",
            "create-campaign",
            "create-ad-set",
            "create-creative",
            "create-ad",
        ),
        "attempt_completion": "CHILDREN_COMPLETE",
        "observed_hierarchy_relation": "MATCHES_PREDICTED_SEMANTICS",
        "observed_member_count": 4,
        "every_pausable_entity_configured_status": "PAUSED",
        "campaign_resource_ref": pseudonymized_resource_ref("campaign", _CAMPAIGN_ID),
        "ad_set_resource_ref": pseudonymized_resource_ref("ad_set", _ADSET_ID),
        "creative_resource_ref": pseudonymized_resource_ref("creative", _CREATIVE_ID),
        "ad_resource_ref": pseudonymized_resource_ref("ad", _AD_ID),
        "observed_campaign_effective_status": "PAUSED",
        "creative_content_digest": _IMAGE_DIGEST,
        "creative_byte_length": len(_IMAGE),
        "creative_media_type": "image/png",
        "image_resource_ref": None,
        "image_association": "NOT_PROVEN",
        "creative_association": "PROVEN_SAME_ATTEMPT_CREATIVE",
    }


def test_a_readback_cannot_claim_a_match_without_both_proven_associations() -> None:
    from pydantic import ValidationError

    values = _readback_values()
    with pytest.raises(ValidationError, match="both proven associations"):
        MetaAdsSingleImageHierarchyCreateReadbackObservation(**values)

    proven = {
        **values,
        "image_association": "PROVEN_ACCOUNT_IMAGE_AND_CREATIVE_ASSOCIATION",
        "image_resource_ref": pseudonymized_resource_ref("image", _IMAGE_HASH),
    }
    assert MetaAdsSingleImageHierarchyCreateReadbackObservation(**proven)
    # A readback that saw an unpaused entity can never be recorded as a match.
    with pytest.raises(ValidationError, match="every pausable entity paused"):
        MetaAdsSingleImageHierarchyCreateReadbackObservation(
            **{**proven, "every_pausable_entity_configured_status": "NOT_ALL_PAUSED"}
        )
    # A complete hierarchy cannot be called partial, nor a partial one divergent.
    with pytest.raises(ValidationError, match="partial hierarchy"):
        MetaAdsSingleImageHierarchyCreateReadbackObservation(
            **{**proven, "observed_hierarchy_relation": "PARTIAL_HIERARCHY_OBSERVED"}
        )
    with pytest.raises(ValidationError, match="divergent hierarchy"):
        MetaAdsSingleImageHierarchyCreateReadbackObservation(
            **{
                **proven,
                "observed_hierarchy_relation": "DIVERGENT_HIERARCHY_OBSERVED",
                "attempt_completion": "CHILDREN_INCOMPLETE",
                "acknowledged_child_count": 4,
                "acknowledged_child_labels": proven["acknowledged_child_labels"][:4],
            }
        )
    # An unavailable readback names no resource and asserts no status.
    with pytest.raises(ValidationError, match="unobserved hierarchy"):
        MetaAdsSingleImageHierarchyCreateReadbackObservation(
            **{**proven, "observed_hierarchy_relation": "READBACK_UNAVAILABLE"}
        )


_UNOBSERVED = {
    "observed_member_count": 0,
    "every_pausable_entity_configured_status": "NOT_OBSERVED",
    "campaign_resource_ref": None,
    "ad_set_resource_ref": None,
    "creative_resource_ref": None,
    "ad_resource_ref": None,
    "observed_campaign_effective_status": None,
    "attempt_completion": "CHILDREN_INCOMPLETE",
}
_PREFIX = ("upload-image", "create-campaign", "create-ad-set", "create-creative", "create-ad")


def _unobserved(relation: str, acknowledged: int) -> dict[str, Any]:
    return {
        **_readback_values(),
        **_UNOBSERVED,
        "observed_hierarchy_relation": relation,
        "creative_association": "NOT_PROVEN",
        "acknowledged_child_count": acknowledged,
        "acknowledged_child_labels": _PREFIX[:acknowledged],
    }


def test_each_unobserved_relation_must_agree_with_its_own_child_journal() -> None:
    """A relation is a statement about what was acknowledged, so the model enforces it."""

    from pydantic import ValidationError

    model = MetaAdsSingleImageHierarchyCreateReadbackObservation
    # The coherent shapes.
    assert model(**_unobserved("READBACK_NOT_ATTEMPTED", 0))
    assert model(**_unobserved("NO_HIERARCHY_OBSERVED", 1))
    assert model(**_unobserved("UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED", 2))
    assert model(**_unobserved("UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED", 5) | _complete())
    assert model(**_unobserved("READBACK_UNAVAILABLE", 3))
    assert model(**_unobserved("ACKNOWLEDGED_CAMPAIGN_NOT_LISTED", 2))
    assert model(**_unobserved("ACKNOWLEDGED_CAMPAIGN_NOT_LISTED", 5) | _complete())
    # The same listing statement cannot follow a journal with no campaign child.
    with pytest.raises(ValidationError, match="only after the campaign identifier"):
        model(**_unobserved("ACKNOWLEDGED_CAMPAIGN_NOT_LISTED", 1))

    # "No read made" cannot follow an acknowledged child.
    with pytest.raises(ValidationError, match="not attempted only when nothing"):
        model(**_unobserved("READBACK_NOT_ATTEMPTED", 1))
    # A completed or failed readback follows at least one acknowledged child.
    for relation in (
        "NO_HIERARCHY_OBSERVED",
        "UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED",
        "ACKNOWLEDGED_CAMPAIGN_NOT_LISTED",
        "READBACK_UNAVAILABLE",
    ):
        with pytest.raises(ValidationError, match="follows an acknowledged child"):
            model(**_unobserved(relation, 0))
    # "Nothing found" is impossible once the campaign child was acknowledged.
    with pytest.raises(ValidationError, match="no campaign identifier was acknowledged"):
        model(**_unobserved("NO_HIERARCHY_OBSERVED", 2))
    # Every unobserved relation still names no resource and asserts no status.
    for relation, acknowledged in (
        ("READBACK_NOT_ATTEMPTED", 0),
        ("NO_HIERARCHY_OBSERVED", 1),
        ("UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED", 2),
        ("ACKNOWLEDGED_CAMPAIGN_NOT_LISTED", 2),
    ):
        with pytest.raises(ValidationError, match="unobserved hierarchy"):
            model(
                **{
                    **_unobserved(relation, acknowledged),
                    "campaign_resource_ref": pseudonymized_resource_ref("campaign", _CAMPAIGN_ID),
                }
            )


def _complete() -> dict[str, Any]:
    return {"attempt_completion": "CHILDREN_COMPLETE"}


# --- the command envelope ---------------------------------------------------


def test_the_generic_request_carries_one_named_meta_member_and_refuses_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import ValidationError

    prepared = _prepare(tmp_path, monkeypatch)
    request = _request(prepared)

    assert type(request.meta_selection) is MetaAdsSingleImageHierarchyCreateSelection
    with pytest.raises(ValidationError):
        ExecutionPlanRequest(
            schema_version=1,
            profile_id=_PROFILE_ID,
            provider=_PROVIDER,
            operation="CREATE",
            account_ref=_ACCOUNT_REF,
            tiktok_selection={},  # type: ignore[call-arg]
        )


def test_the_single_request_seams_refuse_this_multi_request_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    ledger = Ledger(tmp_path)
    action = _retained_action_for_plan(ledger, retained.record.plan)
    subplan = action.plan.subplans[0]
    adapter = adapter_module.MetaAdsSingleImageHierarchyCreateAdapter(
        action=action,
        session=None,  # type: ignore[arg-type]
        egress=None,  # type: ignore[arg-type]
        ledger=ledger,
        issuer=adapter_module._ADAPTER_ISSUER,
    )

    with pytest.raises(AdhelmError) as unguarded:
        adapter.apply(subplan, idempotency_key="a" * 64)
    assert unguarded.value.code == "PERMIT_REQUIRED"

    with pytest.raises(AdhelmError) as single:
        adapter.apply_guarded(subplan, idempotency_key="a" * 64, permit=None)  # type: ignore[arg-type]
    assert single.value.code == "ADAPTER_CHILD_EGRESS_REQUIRED"


def test_a_request_without_the_meta_selection_refuses_before_any_local_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact refusal `scripts/validate_development.py` pins in the wheel."""

    prepared = _prepare(tmp_path, monkeypatch)

    with pytest.raises(AdhelmError) as refused:
        META_CREATE_EXECUTION_PLANNER.plan_configured_execution(
            prepared.directory,
            _request(prepared, meta_selection=None),
            expected_configuration_digest=digest(load_configuration(prepared.directory)),
        )

    assert refused.value.code == "INVALID_META_ADS_CREATE_EXECUTION_SELECTION"
    assert refused.value.exit_code == 2
    assert prepared.meta.requests == []
    assert prepared.inspections == []


def test_the_fixed_create_capability_refuses_an_unknown_edge_or_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capability is a closed five-route grammar, not a Graph passthrough."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    with edit_session.configured_meta_ads_token_edit_session(
        prepared.directory,
        _PROFILE_ID,
        expected_configuration_digest=action.execution_binding.configuration_digest,
    ) as session:
        egress = edit_session._issue_fixed_meta_ads_hierarchy_create_egress(
            session,
            account_client_ref=_ACCOUNT_CLIENT_REF,
            plan_digest=action.plan.plan_digest,
            subplan_digest=action.plan.subplans[0].subplan_digest,
        )
        assert egress.account_client_ref == _ACCOUNT_CLIENT_REF
        assert egress._url_path("/campaigns") == f"/v26.0/{_ACCOUNT_CLIENT_REF}/campaigns"
        with pytest.raises(AdhelmError) as unknown_edge:
            egress._url_path("/insights")
        assert unknown_edge.value.code == "META_TOKEN_EDIT_SESSION_INVALID"
        with pytest.raises(AdhelmError):
            egress._create(
                permit=None,  # type: ignore[arg-type]
                attempt_ref="a" * 64,
                child_index=2,
                edge="/campaigns",
                form={"name": "x"},
            )
    assert prepared.meta.posts == 0


def test_the_fixed_create_capability_refuses_an_oversize_value_before_the_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value the private POST would refuse must not consume a child permit first."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    consumed: list[object] = []
    monkeypatch.setattr(
        edit_session._FixedMetaAdsHierarchyCreateEgress,
        "_consume",
        staticmethod(lambda permit, **kwargs: consumed.append(permit)),
    )
    with edit_session.configured_meta_ads_token_edit_session(
        prepared.directory,
        _PROFILE_ID,
        expected_configuration_digest=action.execution_binding.configuration_digest,
    ) as session:
        egress = edit_session._issue_fixed_meta_ads_hierarchy_create_egress(
            session,
            account_client_ref=_ACCOUNT_CLIENT_REF,
            plan_digest=action.plan.plan_digest,
            subplan_digest=action.plan.subplans[0].subplan_digest,
        )
        form = adapter_module.campaign_request_form(action.candidate_binding.specification)
        for broken in ({**form, "name": "x" * 4_097}, {**form, "name": "caf\u00e9"}):
            with pytest.raises(AdhelmError) as refused:
                egress._create(
                    permit=None,  # type: ignore[arg-type]
                    attempt_ref="a" * 64,
                    child_index=2,
                    edge="/campaigns",
                    form=broken,
                )
            assert refused.value.code == "META_TOKEN_EDIT_SESSION_INVALID"
    assert consumed == []
    assert prepared.meta.posts == 0


def test_the_session_post_grammar_accepts_every_account_the_capability_can_name() -> None:
    """The session and the capability bound an ad-account ID identically."""

    from adhelm import _meta_ads_token_session as session_module

    forty = "act_" + "1" * 40
    assert edit_session._ACCOUNT_ID.fullmatch(forty) is not None
    for edge in ("adimages", "campaigns", "adsets", "adcreatives", "ads"):
        path = f"/v26.0/{forty}/{edge}"
        assert session_module._fixed_meta_create_path(path, session_module._EDIT_POLICY) == path
    for refused in (
        "/v26.0/act_" + "1" * 41 + "/ads",
        "/v26.0/act_0123/ads",
        "/v26.0/act_1/insights",
        "/v25.0/act_1/ads",
        "https://graph.facebook.com/v26.0/act_1/ads",
    ):
        with pytest.raises(AdhelmError):
            session_module._fixed_meta_create_path(refused, session_module._EDIT_POLICY)


def test_this_routes_upload_bound_never_exceeds_the_shared_transports() -> None:
    from adhelm.providers.meta_create_contract import META_CREATE_MAX_UPLOAD_BYTES

    assert META_CREATE_MAX_UPLOAD_BYTES <= SafeHttpClient.MAX_UPLOAD_BYTES
