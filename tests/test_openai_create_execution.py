"""OpenAI Ads paused chat-card hierarchy CREATE on the generic route, with fakes only.

Real here: the P04-OA contract, the recompile-and-bind promotion of a retained
offline candidate, the private local creative vault, the S3 retained record, the
S4 route, a real signed approval receipt from an ephemeral test key, a real
``SafetyKernel`` with its journaled child-egress interface, and a real local
ledger.  Fake here: the operating-system credential store, the HTTP transport,
and the *installed* approval bridge.  Nothing contacts OpenAI Ads, a real
credential store, a native helper, or a network socket, and none of this is
evidence of installed native authority or live provider conformance.

One build dependency stays deliberately visible.  ``ledger.py`` now registers
the additive record kinds ``OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_PLAN`` and
``OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_READBACK_OBSERVATION``, so the readback
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
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_generic_execution_route import _FakeCompositeBridge, _install

from adhelm import ledger as ledger_module
from adhelm import openai_create_execution as execution
from adhelm.assets import store_image
from adhelm.canonical import digest
from adhelm.config import load_configuration, save_configuration
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
from adhelm.models import Configuration, Money, OpenAIAccountKeyProfile
from adhelm.openai_create_execution import (
    OPENAI_CREATE_EXECUTION_ADAPTER_FACTORY,
    OPENAI_CREATE_EXECUTION_PLANNER,
    _retained_action_for_plan,
)
from adhelm.openai_create_readback_records import (
    OPENAI_CREATE_READBACK_RECORD_KIND,
    OpenAIAdsChatCardHierarchyCreateReadbackObservation,
    pseudonymized_resource_ref,
    validate_openai_create_readback_history,
)
from adhelm.plan_cli import ExecutionPlanRequest
from adhelm.providers import openai_create_adapter as adapter_module
from adhelm.providers.openai_compiler import (
    OpenAIAdsAdBrief,
    OpenAIAdsAdGroupBrief,
    OpenAIAdsCampaignBrief,
    OpenAIAdsChatCardCreative,
    OpenAIAdsCreativeAssociation,
    OpenAIAdsDestinationPolicy,
    OpenAIAdsFileReferenceEvidence,
    compile_openai_ads_campaign,
)
from adhelm.providers.openai_create_contract import (
    OPENAI_CREATE_PLAN_RECORD_KIND,
    OpenAIAdsChatCardHierarchyCreatePlanRecord,
    OpenAIAdsChatCardHierarchyCreateSelection,
    create_child_plan,
)
from adhelm.safety import EffectStatus, OperationKind
from adhelm.saved_candidates import retain_candidate
from adhelm.transport import SafeHttpClient

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_ACCOUNT_ID = "account_fixture"
_PROVIDER = "openai_ads"
_ACCOUNT_REF = f"openai_ads.account.{_ACCOUNT_ID}"
_PROFILE_ID = "openai-edit"
_SECRET = "synthetic-openai-create-secret"
_BINDING_KEY = b"fixture-private-binding-key-32-bytes!"
_SHARING_KEY = b"fixture-private-sharing-key-32-bytes"
_CAMPAIGN_ID = "cmpn_101"
_GROUP_IDS = ("adgrp_301", "adgrp_302")
_AD_IDS = ("ad_401", "ad_402")
_FILE_ID = "file_901"
_GOLDEN = Path(__file__).parent / "fixtures" / "openai_ads_chat_card_hierarchy_create_requests.json"


# --- the local creative -----------------------------------------------------


def _png(colour: str = "#123456", size: tuple[int, int] = (8, 4)) -> bytes:
    from PIL import Image

    output = BytesIO()
    Image.new("RGB", size, colour).save(output, format="PNG")
    return output.getvalue()


_IMAGE = _png()
_IMAGE_DIGEST = hashlib.sha256(_IMAGE).hexdigest()
_DECODED = decode_image(_IMAGE)


def _association(title: str, body: str, destination_url: str) -> OpenAIAdsCreativeAssociation:
    asset = CreativeAssetVersion(
        asset_version_id="asset:sha256:"
        + digest(
            {
                "content_digest": _IMAGE_DIGEST,
                "mime_type": _DECODED.mime_type,
                "width_px": _DECODED.width_px,
                "height_px": _DECODED.height_px,
            }
        ),
        content_digest=_IMAGE_DIGEST,
        media_type="IMAGE",
        mime_type=_DECODED.mime_type,
        byte_size=len(_IMAGE),
        width_px=_DECODED.width_px,
        height_px=_DECODED.height_px,
        aspect_ratio="2:1",
    )
    rendition = CreativeRendition.from_asset(
        asset, provider_format="OPENAI_ADS_CHAT_CARD", locale="en-US"
    )
    creative_version = CreativeVersion(
        creative_family_id="family:openai-launch",
        manifest=ServingCreativeManifest(
            provider_format="OPENAI_ADS_CHAT_CARD",
            locale="en-US",
            destination_ref="destination:sha256:" + digest({"target_url": destination_url}),
            rendition_refs=(
                ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
            ),
            components=(
                CreativeComponent(
                    component_id="title",
                    component_role="HEADLINE",
                    ordinal=0,
                    exact_text=title,
                    normalized_text=title.lower(),
                    language="en",
                    provider_slot="title",
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
                CreativeComponent(
                    component_id="body",
                    component_role="PRIMARY_TEXT",
                    ordinal=1,
                    exact_text=body,
                    normalized_text=body.lower(),
                    language="en",
                    provider_slot="body",
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
            ),
        ),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    return OpenAIAdsCreativeAssociation(
        creative_version=creative_version, asset=asset, rendition=rendition
    )


def _ad(handle: str, name: str, title: str, body: str) -> OpenAIAdsAdBrief:
    destination_url = "https://example.test/demo?utm_source=ads"
    return OpenAIAdsAdBrief(
        ad_handle=handle,
        name=name,
        creative=OpenAIAdsChatCardCreative(
            title=title,
            body=body,
            destination_url=destination_url,
            association=_association(title, body, destination_url),
            file_reference=OpenAIAdsFileReferenceEvidence(
                account_ref=_ACCOUNT_REF,
                file_id="file_customer_local_evidence",
                asset_content_digest=_IMAGE_DIGEST,
                evidence_ref="evidence:sha256:" + "a" * 64,
            ),
        ),
    )


def _brief(*, groups: int = 2, ads_per_group: int = 1, **updates: Any) -> OpenAIAdsCampaignBrief:
    ad_groups = tuple(
        OpenAIAdsAdGroupBrief(
            ad_group_handle=f"group-{index}",
            name=f"US group {index}",
            max_bid_micros=60_000 + index,
            ads=tuple(
                _ad(
                    f"card-{index}-{position}",
                    f"Planner card {index} {position}",
                    f"Plan work faster {index}{position}",
                    f"Bring tasks and documents together {index}{position}.",
                )
                for position in range(ads_per_group)
            ),
        )
        for index in range(groups)
    )
    values: dict[str, Any] = {
        "account_ref": _ACCOUNT_REF,
        "account_currency": "USD",
        "currency_minor_unit_exponent": 2,
        "account_timezone": "America/Chicago",
        "campaign_handle": "launch",
        "name": "September launch",
        "start_time": 1_788_264_800,
        "end_time": 1_790_856_000,
        "lifetime_budget": Money(minor_units=2_500, currency="USD"),
        "bidding_type": "clicks",
        "location_ids": ("2000043", "3000194"),
        "destination_policy": OpenAIAdsDestinationPolicy(
            allowed_hosts=("example.test",), allowed_paths=("/demo",)
        ),
        "ad_groups": ad_groups,
    }
    values.update(updates)
    return OpenAIAdsCampaignBrief(**values)


_SPEC = compile_openai_ads_campaign(_brief())
_CAMPAIGN_NAME = _SPEC.campaign_request.name


# --- the fake provider ------------------------------------------------------


class _FakeOpenAIAds:
    """One in-memory ad account behind the fixed v1 read and create routes."""

    def __init__(
        self,
        *,
        directory: Path,
        account_status: str = "active",
        currency: str = "USD",
        timezone: str = "America/Chicago",
        name_matches_before_create: int = 0,
        fail_at: int | None = None,
        extra_ack_field: int | None = None,
    ) -> None:
        self.directory = directory
        self.account_status = account_status
        self.currency = currency
        self.timezone = timezone
        self.name_matches_before_create = name_matches_before_create
        self.fail_at = fail_at
        self.extra_ack_field = extra_ack_field
        self.requests: list[dict[str, Any]] = []
        self.posts = 0
        self.child_starts_at_post: list[int] = []
        self.campaign: dict[str, Any] | None = None
        self.groups: list[dict[str, Any]] = []
        self.ads: dict[str, list[dict[str, Any]]] = {}
        self.uploaded: list[dict[str, Any]] = []

    # -- transport ----------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            return self._read(path, request)
        assert request.method == "POST"
        self.posts += 1
        self.child_starts_at_post.append(self._child_starts())
        record = self._record(request)
        self.requests.append(record)
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
        return {
            "method": request.method,
            "path": request.url.path,
            "body": json.loads(request.content),
        }

    # -- reads --------------------------------------------------------------

    def _read(self, path: str, request: httpx.Request) -> httpx.Response:
        if path == "/v1/ad_account":
            return httpx.Response(
                200,
                json={
                    "id": _ACCOUNT_ID,
                    "name": "Fixture account",
                    "url": "https://example.invalid",
                    "preview_url": None,
                    "status": self.account_status,
                    "timezone": self.timezone,
                    "currency_code": self.currency,
                    "review": {"status": "approved"},
                },
            )
        if path == "/v1/campaigns":
            rows: list[dict[str, Any]] = []
            if self.campaign is not None:
                rows.append(self.campaign)
            elif self.name_matches_before_create:
                rows.extend([self._campaign_object(_CAMPAIGN_ID)] * self.name_matches_before_create)
            return self._list(rows)
        if path.startswith("/v1/campaigns/"):
            assert self.campaign is not None
            return httpx.Response(200, json=self.campaign)
        if path == "/v1/ad_groups":
            campaign_id = request.url.params.get("campaign_id")
            assert campaign_id == _CAMPAIGN_ID
            return self._list(self.groups)
        assert path == "/v1/ads"
        group_id = request.url.params.get("ad_group_id")
        return self._list(self.ads.get(str(group_id), []))

    @staticmethod
    def _list(rows: list[dict[str, Any]]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": rows,
                "first_id": rows[0]["id"] if rows else None,
                "last_id": rows[-1]["id"] if rows else None,
                "has_more": False,
            },
        )

    # -- creates ------------------------------------------------------------

    def _create(self, path: str) -> httpx.Response:
        if path == "/v1/upload":
            self.uploaded.append({"bytes": len(_IMAGE)})
            return httpx.Response(200, json=self._extra({"file_id": _FILE_ID}, 1))
        if path == "/v1/campaigns":
            self.campaign = self._campaign_object(_CAMPAIGN_ID)
            return httpx.Response(200, json=self._extra(dict(self.campaign), 2))
        if path == "/v1/ad_groups":
            index = len(self.groups)
            group = self._group_object(_GROUP_IDS[index], index)
            self.groups.append(group)
            self.ads[group["id"]] = []
            return httpx.Response(200, json=self._extra(dict(group), 3 + index))
        assert path == "/v1/ads"
        created = sum(len(items) for items in self.ads.values())
        group_id = _GROUP_IDS[created]
        ad = self._ad_object(_AD_IDS[created], created)
        self.ads[group_id].append(ad)
        return httpx.Response(200, json=self._extra(dict(ad), 3 + len(_GROUP_IDS) + created))

    def _extra(self, payload: dict[str, Any], index: int) -> dict[str, Any]:
        if self.extra_ack_field == index:
            payload["undocumented_field"] = "surprise"
        return payload

    @staticmethod
    def _campaign_object(campaign_id: str) -> dict[str, Any]:
        campaign = _SPEC.campaign_request
        return {
            "id": campaign_id,
            "ad_account_id": _ACCOUNT_ID,
            "created_at": 1_788_260_800,
            "updated_at": 1_788_260_800,
            "name": campaign.name,
            "description": None,
            "status": campaign.status,
            "start_time": campaign.start_time,
            "end_time": campaign.end_time,
            "budget": {"lifetime_spend_limit_micros": campaign.budget.lifetime_spend_limit_micros},
            "bidding_type": campaign.bidding_type,
            "targeting": {
                "locations": {
                    "include": [
                        {
                            "id": item.id,
                            "type": "region",
                            "country_code": "US",
                            "name": "Region",
                            "region_code": "US-CA",
                        }
                        for item in campaign.targeting.locations.include
                    ]
                }
            },
        }

    @staticmethod
    def _group_object(group_id: str, index: int) -> dict[str, Any]:
        request = _SPEC.ad_groups[index].request
        return {
            "id": group_id,
            "created_at": 1_788_260_800,
            "updated_at": 1_788_260_800,
            "name": request.name,
            "description": None,
            "status": request.status,
            "bidding_config": {
                "billing_event_type": request.bidding_config.billing_event_type,
                "max_bid_micros": request.bidding_config.max_bid_micros,
            },
        }

    @staticmethod
    def _ad_object(ad_id: str, index: int) -> dict[str, Any]:
        compiled = _SPEC.ad_groups[index].ads[0].request
        return {
            "id": ad_id,
            "name": compiled.name,
            "created_at": 1_788_260_800,
            "updated_at": 1_788_260_800,
            "creative": {
                "type": compiled.creative.type,
                "title": compiled.creative.title,
                "body": compiled.creative.body,
                "file_id": _FILE_ID,
                "image_url": "https://cdn.invalid/creative.png",
                "target_url": compiled.creative.target_url,
            },
            "status": compiled.status,
            "review_status": "pending",
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
    if plan and kinds.get(OPENAI_CREATE_PLAN_RECORD_KIND) is not (
        OpenAIAdsChatCardHierarchyCreatePlanRecord
    ):
        monkeypatch.setitem(
            kinds, OPENAI_CREATE_PLAN_RECORD_KIND, OpenAIAdsChatCardHierarchyCreatePlanRecord
        )
    if readback and kinds.get(OPENAI_CREATE_READBACK_RECORD_KIND) is not (
        OpenAIAdsChatCardHierarchyCreateReadbackObservation
    ):
        monkeypatch.setitem(
            kinds,
            OPENAI_CREATE_READBACK_RECORD_KIND,
            OpenAIAdsChatCardHierarchyCreateReadbackObservation,
        )
    if not plan:
        monkeypatch.delitem(kinds, OPENAI_CREATE_PLAN_RECORD_KIND, raising=False)
    if not readback:
        monkeypatch.delitem(kinds, OPENAI_CREATE_READBACK_RECORD_KIND, raising=False)


def _profile(**updates: Any) -> OpenAIAccountKeyProfile:
    return OpenAIAccountKeyProfile.model_validate(
        {
            "profile_kind": "OPENAI_ADS_ACCOUNT_KEY",
            "profile_id": _PROFILE_ID,
            "purpose": "EDIT",
            "account_ref": _ACCOUNT_ID,
            "currency": "USD",
            "timezone": "America/Chicago",
            "credential_ref": "synthetic-openai-key",
            **updates,
        }
    )


class _Prepared:
    def __init__(
        self,
        directory: Path,
        openai: _FakeOpenAIAds,
        candidate_record_digest: str,
        brief: OpenAIAdsCampaignBrief,
    ) -> None:
        self.directory = directory
        self.openai = openai
        self.candidate_record_digest = candidate_record_digest
        self.brief = brief


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan_kind: bool = True,
    readback_kind: bool = True,
    retain: bool = True,
    store_creative: bool = True,
    brief: OpenAIAdsCampaignBrief | None = None,
    secret: str = _SECRET,
    binding_key: bytes = _BINDING_KEY,
    profile: OpenAIAccountKeyProfile | None = None,
    **openai: Any,
) -> _Prepared:
    _install_record_kinds(monkeypatch, plan=plan_kind, readback=readback_kind)
    save_configuration(tmp_path, Configuration(profiles=[profile or _profile()]), replace=True)
    fake = _FakeOpenAIAds(directory=tmp_path, **openai)
    monkeypatch.setattr(execution, "credential", lambda _profile: secret)
    monkeypatch.setattr(execution, "openai_pause_binding_key", lambda _profile: binding_key)
    monkeypatch.setattr(execution, "export_key", lambda: _SHARING_KEY)
    monkeypatch.setattr(adapter_module, "export_key", lambda: _SHARING_KEY)
    for module in (execution, adapter_module):
        monkeypatch.setattr(
            module,
            "_openai_create_http_client",
            lambda: httpx.Client(transport=httpx.MockTransport(fake.handler), trust_env=False),
        )
    if store_creative:
        store_image(tmp_path, _IMAGE, _IMAGE_DIGEST)
    source = brief if brief is not None else _brief()
    record_digest = ""
    if retain:
        record_digest = retain_candidate(Ledger(tmp_path), source)["record_digest"]
    return _Prepared(tmp_path, fake, record_digest, source)


def _selection(prepared: _Prepared, **overrides: Any) -> OpenAIAdsChatCardHierarchyCreateSelection:
    values: dict[str, Any] = {
        "candidate_record_digest": prepared.candidate_record_digest,
        "fresh": prepared.brief,
        "creative_vault_ref": f"vault:sha256:{_IMAGE_DIGEST}",
    }
    values.update(overrides)
    return OpenAIAdsChatCardHierarchyCreateSelection(**values)


def _request(prepared: _Prepared, **overrides: Any) -> ExecutionPlanRequest:
    values: dict[str, Any] = {
        "schema_version": 1,
        "profile_id": _PROFILE_ID,
        "provider": _PROVIDER,
        "operation": "CREATE",
        "account_ref": _ACCOUNT_REF,
        "resource_ref": f"openai_ads.candidate.{prepared.candidate_record_digest}",
        "openai_selection": _selection(prepared),
    }
    values.update(overrides)
    return ExecutionPlanRequest(**values)


def _plan(prepared: _Prepared, **overrides: Any) -> RetainedExecutionPlan:
    configuration = load_configuration(prepared.directory)
    return OPENAI_CREATE_EXECUTION_PLANNER.plan_configured_execution(
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


# --- registration -----------------------------------------------------------


def test_openai_create_is_a_registered_pair_on_both_registries() -> None:
    assert configured_execution_planner(_PROVIDER, OperationKind.CREATE) is (
        OPENAI_CREATE_EXECUTION_PLANNER
    )
    assert configured_execution_adapter(_PROVIDER, OperationKind.CREATE) is (
        OPENAI_CREATE_EXECUTION_ADAPTER_FACTORY
    )
    assert (_PROVIDER, OperationKind.CREATE) in dict(CONFIGURED_EXECUTION_ADAPTERS)
    assert (_PROVIDER, OperationKind.CREATE) in dict(CONFIGURED_EXECUTION_PLANNERS)
    assert {"provider": _PROVIDER, "operation": "CREATE"} in executable_execution_operations()


def test_the_factory_declares_journaled_child_egress_without_reading_anything() -> None:
    """This route is one upload plus one request per created entity."""

    assert OPENAI_CREATE_EXECUTION_ADAPTER_FACTORY.declared_capabilities() == (
        ExecutionAdapterCapabilities(child_egress=True)
    )


def test_registering_a_second_openai_create_adapter_is_refused() -> None:
    with pytest.raises(AdhelmError) as refused:
        register_execution_adapter(
            _PROVIDER, OperationKind.CREATE, OPENAI_CREATE_EXECUTION_ADAPTER_FACTORY
        )
    assert refused.value.code == "EXECUTION_ADAPTER_ALREADY_REGISTERED"


def test_repeated_registration_of_the_exact_singletons_is_a_no_operation() -> None:
    execution.register_openai_create_execution()
    assert configured_execution_adapter(_PROVIDER, OperationKind.CREATE) is (
        OPENAI_CREATE_EXECUTION_ADAPTER_FACTORY
    )


# --- the ordered child plan -------------------------------------------------


def test_the_child_plan_is_one_upload_one_campaign_then_groups_then_ads() -> None:
    children = create_child_plan(_SPEC)

    assert [child.label for child in children] == [
        "upload-creative",
        "create-campaign",
        "create-ad-group",
        "create-ad-group",
        "create-ad",
        "create-ad",
    ]
    assert [child.index for child in children] == [1, 2, 3, 4, 5, 6]
    assert [child.path for child in children] == [
        "/upload",
        "/campaigns",
        "/ad_groups",
        "/ad_groups",
        "/ads",
        "/ads",
    ]


def test_a_hierarchy_above_the_kernel_child_bound_is_refused_at_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1 upload + 1 campaign + 31 groups exceeds MAX_CHILD_EGRESS_COUNT."""

    large = _brief(groups=31, ads_per_group=1)
    prepared = _prepare(tmp_path, monkeypatch, brief=large)

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "OPENAI_ADS_CREATE_HIERARCHY_TOO_LARGE"
    assert prepared.openai.requests == []
    assert prepared.openai.posts == 0


# --- the happy path ---------------------------------------------------------


def test_full_happy_path_applies_verified_with_the_golden_request_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    assert retained.record.expected_child_egress_count == 6

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert result.results[0].provider == _PROVIDER
    assert prepared.openai.posts == 6
    assert json.loads(_GOLDEN.read_text()) == prepared.openai.requests

    # Every child is journaled around its own permit, in order.
    starts = _records(tmp_path, "CHILD_APPLY_STARTED")
    results = _records(tmp_path, "CHILD_APPLY_RESULT")
    assert [row["payload"]["child_index"] for row in starts] == [1, 2, 3, 4, 5, 6]
    assert [row["payload"]["child_label"] for row in starts] == [
        "upload-creative",
        "create-campaign",
        "create-ad-group",
        "create-ad-group",
        "create-ad",
        "create-ad",
    ]
    assert {row["payload"]["effect_status"] for row in results} == {"APPLIED_VERIFIED"}
    # One durable start exists per POST, before that POST.
    assert prepared.openai.child_starts_at_post == [1, 2, 3, 4, 5, 6]


def test_the_retained_readback_records_only_pseudonyms_and_the_proven_association(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    _execute(prepared, retained, monkeypatch)

    rows = _records(tmp_path, OPENAI_CREATE_READBACK_RECORD_KIND)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["observed_hierarchy_relation"] == "MATCHES_PREDICTED_SEMANTICS"
    assert payload["file_association"] == "PROVEN_UPLOAD_ASSOCIATION"
    assert payload["uploaded_file_readback"] == "NOT_AVAILABLE_NO_DOCUMENTED_ADS_FILE_GET"
    assert payload["provider_file_bytes"] == "PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_HASH_READ"
    assert payload["creative_content_digest"] == _IMAGE_DIGEST
    assert payload["creative_byte_length"] == len(_IMAGE)
    assert payload["campaign_resource_ref"] == pseudonymized_resource_ref("campaign", _CAMPAIGN_ID)
    assert payload["creative_file_ref"] == pseudonymized_resource_ref("file", _FILE_ID)
    assert payload["observed_member_count"] == 5


def test_no_key_file_bytes_or_native_identifier_reaches_the_shared_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result, the review, the child journal and the readback stay redacted.

    The retained provider-specific plan record deliberately holds the compiled
    candidate -- the adapter builds its request bodies from it and nothing else
    could -- exactly as the Google CREATE plan record holds its own compiled
    specification.  Every surface a reviewer, an agent, or an export sees is
    checked here instead.
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
                    OPENAI_CREATE_READBACK_RECORD_KIND,
                    "CHILD_APPLY_STARTED",
                    "CHILD_APPLY_RESULT",
                    "CHANGE_EVENT",
                }
            ],
        ],
        default=str,
    )
    for forbidden in (
        _SECRET,
        _BINDING_KEY.decode(),
        _FILE_ID,
        _CAMPAIGN_ID,
        _GROUP_IDS[0],
        _AD_IDS[0],
        _IMAGE.hex(),
        "Plan work faster",
        "https://example.test/demo",
    ):
        assert forbidden not in shared
    # The retained provider plan is the one place the compiled candidate lives.
    plan_record = _records(tmp_path, OPENAI_CREATE_PLAN_RECORD_KIND)[0]
    rendered_plan = json.dumps(plan_record, default=str)
    assert _SECRET not in rendered_plan
    assert _BINDING_KEY.decode() not in rendered_plan
    assert _IMAGE.hex() not in rendered_plan
    assert _IMAGE_DIGEST in rendered_plan


def test_the_plan_review_states_that_an_uploaded_file_cannot_be_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)

    lines = retained.record.review_lines
    assert retained.record.review_source == "COMPILER_CANDIDATE_REVIEW"
    assert any("NOT_AVAILABLE_NO_DOCUMENTED_ADS_FILE_GET" in line for line in lines)
    assert any("PAUSED_AD_READBACK_ONLY" in line for line in lines)
    assert any("child request count: 6" in line for line in lines)
    assert any("initial state: PAUSED_AT_EVERY_LEVEL" in line for line in lines)
    assert any("write retry: NEVER_NO_DOCUMENTED_IDEMPOTENCY" in line for line in lines)


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
    assert prepared.openai.posts == 3
    starts = _records(tmp_path, "CHILD_APPLY_STARTED")
    results = _records(tmp_path, "CHILD_APPLY_RESULT")
    assert [row["payload"]["child_index"] for row in starts] == [1, 2, 3]
    assert [row["payload"]["effect_status"] for row in results] == [
        "APPLIED_VERIFIED",
        "APPLIED_VERIFIED",
        "AMBIGUOUS_REMOTE_STATE",
    ]
    readback = _records(tmp_path, OPENAI_CREATE_READBACK_RECORD_KIND)[0]["payload"]
    assert readback["attempt_completion"] == "CHILDREN_INCOMPLETE"
    assert readback["observed_hierarchy_relation"] == "PARTIAL_HIERARCHY_OBSERVED"
    assert readback["recovery_disposition"] == "RETAIN_REFS_LEAVE_PAUSED_NO_DELETE"
    assert readback["acknowledged_child_labels"] == ["upload-creative", "create-campaign"]
    # No delete, remove, or archive path exists at all.
    assert not any(row["method"] in {"DELETE", "PATCH", "PUT"} for row in prepared.openai.requests)


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
    assert prepared.openai.posts == 1
    assert [row["payload"]["child_index"] for row in _records(tmp_path, "CHILD_APPLY_STARTED")] == [
        1
    ]
    readback = _records(tmp_path, OPENAI_CREATE_READBACK_RECORD_KIND)[0]["payload"]
    assert readback["observed_hierarchy_relation"] == "NO_HIERARCHY_OBSERVED"
    assert readback["file_association"] == "NOT_PROVEN"
    assert readback["acknowledged_child_count"] == 0


@pytest.mark.parametrize("child", [1, 2, 3, 5])
def test_an_acknowledgement_with_an_extra_field_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: int
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, extra_ack_field=child)
    retained = _plan(prepared)

    result = _execute(prepared, retained, monkeypatch)

    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert prepared.openai.posts == child
    results = _records(tmp_path, "CHILD_APPLY_RESULT")
    assert results[-1]["payload"]["effect_status"] == "AMBIGUOUS_REMOTE_STATE"


# --- preconditions before egress -------------------------------------------


def test_conversion_bidding_is_a_typed_not_enabled_refusal_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, retain=False)
    prepared.candidate_record_digest = "0" * 64

    with pytest.raises(AdhelmError) as refused:
        OPENAI_CREATE_EXECUTION_PLANNER.plan_configured_execution(
            prepared.directory,
            _request(
                prepared,
                openai_selection=_selection(prepared, fresh=_brief(bidding_type="conversions")),
            ),
            expected_configuration_digest=digest(load_configuration(prepared.directory)),
        )

    assert refused.value.code == "OPENAI_ADS_CREATE_OPTION_NOT_ENABLED"
    assert "NOT_ENABLED" in refused.value.message
    assert prepared.openai.requests == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"name_matches_before_create": 1}, "OPENAI_ADS_CREATE_NAME_COLLISION"),
        ({"account_status": "suspended"}, "OPENAI_ADS_ACCOUNT_NOT_ACTIVE"),
        ({"timezone": "UTC"}, "OPENAI_ADS_CREATE_ACCOUNT_MISMATCH"),
    ],
)
def test_the_bounded_read_refuses_before_any_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], code: str
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, **overrides)

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == code
    assert prepared.openai.posts == 0
    assert _records(tmp_path, OPENAI_CREATE_PLAN_RECORD_KIND) == []


def test_a_missing_vault_object_refuses_before_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, store_creative=False)

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "ASSET_NOT_AVAILABLE"
    assert prepared.openai.requests == []


def test_a_vault_reference_the_brief_does_not_declare_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)

    with pytest.raises(AdhelmError) as refused:
        _plan(
            prepared,
            openai_selection=_selection(prepared, creative_vault_ref="vault:sha256:" + "b" * 64),
        )

    assert refused.value.code == "OPENAI_ADS_CREATE_CREATIVE_NOT_DECLARED"
    assert prepared.openai.requests == []


def test_a_candidate_digest_mismatch_refuses_before_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    other = _brief(name="Another launch")

    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, openai_selection=_selection(prepared, fresh=other))

    assert refused.value.code == "OPENAI_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH"
    assert prepared.openai.requests == []


def test_a_rotated_account_key_refuses_before_the_adapter_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    monkeypatch.setattr(execution, "credential", lambda _profile: "rotated-openai-secret")

    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)

    assert refused.value.code == "CREDENTIAL_BINDING_MISMATCH"
    assert prepared.openai.posts == 0


def test_a_missing_ledger_record_kind_refuses_before_any_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, readback_kind=False)

    def forbidden(_profile: Any) -> str:
        raise AssertionError("a credential was read")

    monkeypatch.setattr(execution, "credential", forbidden)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "OPENAI_ADS_CREATE_RECORD_KIND_NOT_INSTALLED"
    assert prepared.openai.requests == []


def test_a_build_without_the_upload_transport_seam_refuses_before_any_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    monkeypatch.delattr(SafeHttpClient, "upload_image", raising=False)

    def forbidden(_profile: Any) -> str:
        raise AssertionError("a credential was read")

    monkeypatch.setattr(execution, "credential", forbidden)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)

    assert refused.value.code == "OPENAI_ADS_CREATE_UPLOAD_TRANSPORT_NOT_INSTALLED"
    assert prepared.openai.requests == []


def test_the_unconfigured_bridge_refuses_before_any_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)

    def forbidden(_profile: Any) -> str:
        raise AssertionError("a credential was read")

    monkeypatch.setattr(execution, "credential", forbidden)
    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            prepared.directory,
            ExecutionPlanExecutionRequest(plan_record_digest=retained.record_digest),
        )

    assert refused.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"
    assert prepared.openai.posts == 0


# --- the readback history validator ----------------------------------------


def test_the_readback_history_validator_requires_its_action_and_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    _execute(prepared, retained, monkeypatch)
    records = Ledger(tmp_path).records("changes")

    validate_openai_create_readback_history(records)

    orphan = [row for row in records if row["record_kind"] != OPENAI_CREATE_PLAN_RECORD_KIND]
    with pytest.raises(AdhelmError) as refused:
        validate_openai_create_readback_history(orphan)
    assert refused.value.code == "INVALID_OPENAI_ADS_CREATE_READBACK_HISTORY"

    duplicated = [
        *records,
        [row for row in records if row["record_kind"] == (OPENAI_CREATE_READBACK_RECORD_KIND)][0],
    ]
    with pytest.raises(AdhelmError):
        validate_openai_create_readback_history(duplicated)


def test_a_readback_cannot_claim_a_match_without_the_proven_association() -> None:
    from pydantic import ValidationError

    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        OpenAIAdsChatCardHierarchyCreateReadbackObservation(
            action_id="a" * 64,
            plan_digest="b" * 64,
            subplan_digest="c" * 64,
            attempt_ref="attempt",
            account_ref=_ACCOUNT_REF,
            observation_interval_start=now,
            observation_interval_end=now + timedelta(seconds=1),
            approved_child_count=4,
            acknowledged_child_count=4,
            acknowledged_child_labels=(
                "upload-creative",
                "create-campaign",
                "create-ad-group",
                "create-ad",
            ),
            attempt_completion="CHILDREN_COMPLETE",
            observed_hierarchy_relation="MATCHES_PREDICTED_SEMANTICS",
            observed_ad_group_count=1,
            observed_ad_count=1,
            observed_member_count=3,
            campaign_resource_ref=pseudonymized_resource_ref("campaign", _CAMPAIGN_ID),
            ad_group_resource_refs=(pseudonymized_resource_ref("ad_group", _GROUP_IDS[0]),),
            ad_resource_refs=(pseudonymized_resource_ref("ad", _AD_IDS[0]),),
            creative_content_digest=_IMAGE_DIGEST,
            creative_byte_length=len(_IMAGE),
            creative_media_type="image/png",
            creative_file_ref=None,
            file_association="NOT_PROVEN",
        )


# --- the bounded multipart upload seam --------------------------------------


def _upload_client(handler: Any = None) -> SafeHttpClient:
    def default(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"file_id": _FILE_ID})

    return SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler or default), trust_env=False),
        {"api.ads.openai.com"},
    )


def test_the_upload_seam_sends_exactly_one_bounded_image_part() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(200, json={"file_id": _FILE_ID})

    response = _upload_client(handler).upload_image(
        "https://api.ads.openai.com/v1/upload",
        headers={"authorization": "Bearer synthetic"},
        field_name="file",
        filename="creative.png",
        media_type="image/png",
        content=_IMAGE,
    )

    assert response.status_code == 200
    assert seen["content_type"].startswith("multipart/form-data")
    assert b'name="file"; filename="creative.png"' in seen["body"]
    assert b"Content-Type: image/png" in seen["body"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"field_name": "File"},
        {"filename": "../creative.png"},
        {"filename": "creative.svg"},
        {"media_type": "image/svg+xml"},
        {"content": b""},
        {"content": "not bytes"},
        {"headers": {"authorization": "Bearer x", "content-type": "image/png"}},
    ],
)
def test_the_upload_seam_refuses_anything_outside_its_closed_shape(
    overrides: dict[str, Any],
) -> None:
    values: dict[str, Any] = {
        "headers": {"authorization": "Bearer synthetic"},
        "field_name": "file",
        "filename": "creative.png",
        "media_type": "image/png",
        "content": _IMAGE,
    }
    values.update(overrides)

    with pytest.raises(AdhelmError) as refused:
        _upload_client().upload_image("https://api.ads.openai.com/v1/upload", **values)

    assert refused.value.code in {"INVALID_REQUEST_UPLOAD", "INVALID_REQUEST_HEADERS"}


def test_the_upload_seam_refuses_an_unapproved_host_and_never_retries() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("synthetic loss", request=request)

    with pytest.raises(AdhelmError) as host_refused:
        _upload_client().upload_image(
            "https://elsewhere.invalid/v1/upload",
            headers={"authorization": "Bearer synthetic"},
            field_name="file",
            filename="creative.png",
            media_type="image/png",
            content=_IMAGE,
        )
    assert host_refused.value.code == "UNAPPROVED_EGRESS"

    with pytest.raises(AdhelmError) as lost:
        _upload_client(handler).upload_image(
            "https://api.ads.openai.com/v1/upload",
            headers={"authorization": "Bearer synthetic"},
            field_name="file",
            filename="creative.png",
            media_type="image/png",
            content=_IMAGE,
        )
    assert lost.value.code == "AMBIGUOUS_REMOTE_STATE"
    assert len(attempts) == 1


# --- the command envelope ---------------------------------------------------


def test_the_generic_request_carries_one_named_openai_member_and_refuses_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import ValidationError

    prepared = _prepare(tmp_path, monkeypatch)
    request = _request(prepared)

    assert type(request.openai_selection) is OpenAIAdsChatCardHierarchyCreateSelection
    with pytest.raises(ValidationError):
        ExecutionPlanRequest(
            schema_version=1,
            profile_id=_PROFILE_ID,
            provider=_PROVIDER,
            operation="CREATE",
            account_ref=_ACCOUNT_REF,
            google_selection={},  # type: ignore[call-arg]
        )


def test_the_single_request_seams_refuse_this_multi_request_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    ledger = Ledger(tmp_path)
    action = _retained_action_for_plan(ledger, retained.record.plan)
    subplan = action.plan.subplans[0]
    adapter = adapter_module.OpenAIAdsChatCardHierarchyCreateAdapter(
        action=action,
        transport=_upload_client(),
        credential_supplier=lambda: None,  # type: ignore[arg-type, return-value]
        ledger=ledger,
        issuer=adapter_module._ADAPTER_ISSUER,
    )

    with pytest.raises(AdhelmError) as unguarded:
        adapter.apply(subplan, idempotency_key="a" * 64)
    assert unguarded.value.code == "PERMIT_REQUIRED"

    with pytest.raises(AdhelmError) as single:
        adapter.apply_guarded(subplan, idempotency_key="a" * 64, permit=None)  # type: ignore[arg-type]
    assert single.value.code == "ADAPTER_CHILD_EGRESS_REQUIRED"


def test_a_request_without_the_openai_selection_refuses_before_any_local_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact refusal `scripts/validate_development.py` pins in the wheel."""

    prepared = _prepare(tmp_path, monkeypatch)

    def forbidden(_profile: Any) -> str:
        raise AssertionError("a credential was read")

    monkeypatch.setattr(execution, "credential", forbidden)
    with pytest.raises(AdhelmError) as refused:
        OPENAI_CREATE_EXECUTION_PLANNER.plan_configured_execution(
            prepared.directory,
            _request(prepared, openai_selection=None),
            expected_configuration_digest=digest(load_configuration(prepared.directory)),
        )

    assert refused.value.code == "INVALID_OPENAI_ADS_CREATE_EXECUTION_SELECTION"
    assert refused.value.exit_code == 2
    assert prepared.openai.requests == []
